import asyncio
import hmac
import json
import logging
from typing import Any

import anthropic
from fastapi import (
    BackgroundTasks,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Connect, Gather, Start, VoiceResponse

import agent
import approvals
import config
import notify

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("op9")

app = FastAPI(title="op9", description="building entry operator")


def _external_url(request: Request) -> str:
    """Reconstruct the public URL Twilio signed behind a reverse proxy."""
    scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or request.url.netloc
    path = request.url.path
    if request.url.query:
        return f"{scheme}://{host}{path}?{request.url.query}"
    return f"{scheme}://{host}{path}"


def _external_base_url(request: Request) -> str:
    """Public base URL with trailing slash."""
    scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or request.url.netloc
    return f"{scheme}://{host}/"


def _validate_twilio_request(request: Request, params: dict[str, Any]) -> None:
    """Reject requests that fail Twilio signature validation."""
    if not config.TWILIO_AUTH_TOKEN:
        return

    signature = request.headers.get("X-Twilio-Signature", "")
    validator = RequestValidator(config.TWILIO_AUTH_TOKEN)
    if not validator.validate(_external_url(request), params, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")


def build_auto_entry_twiml(recording_callback_url: str) -> str:
    """Build TwiML for automatic entry mode."""
    response = VoiceResponse()

    if config.RECORD_CALLS:
        start = Start()
        start.recording(recording_status_callback=recording_callback_url)
        response.append(start)

    response.say("Opening.")
    _open_door(response)
    response.hangup()

    return str(response)


def build_passcode_twiml(passcode_action_url: str) -> str:
    """Build TwiML for passcode mode: prompt, then gather the DTMF passcode.

    The visitor has already dialed the door code at the panel to reach us, so we
    prompt for the passcode immediately. numDigits is derived from the configured
    passcode length; Twilio submits automatically on the last key. Not recorded,
    so the passcode tones never land in a recording.
    """
    response = VoiceResponse()

    if not config.PASSCODE:
        # Misconfigured — refuse rather than silently opening.
        log.error("passcode mode active but PASSCODE is unset, refusing")
        response.say("System not configured.")
        response.hangup()
        return str(response)

    gather = Gather(
        input="dtmf",
        num_digits=len(config.PASSCODE),
        action=passcode_action_url,
        method="POST",
        timeout=config.PASSCODE_TIMEOUT,
    )
    gather.say("Enter the passcode.")
    response.append(gather)

    # Reached only if the visitor entered nothing before the timeout. Twilio runs
    # this verb itself rather than calling back, so a timed-out call leaves no
    # /passcode log line: a "voice:" line with no matching "passcode:" line for
    # the same CallSid means the panel sent no tones Twilio could decode.
    response.say("No passcode entered. Goodbye.")
    response.hangup()

    return str(response)


def build_voice_agent_twiml(relay_ws_url: str) -> str:
    """Build TwiML for voice-agent mode: hand the call to ConversationRelay.

    Twilio takes over speech-to-text and text-to-speech and bridges the call to
    our /relay WebSocket, where the LLM decides whether to open. The door is
    opened from inside that socket (a sendDigits frame), not from this TwiML.
    """
    response = VoiceResponse()

    if not config.ANTHROPIC_API_KEY:
        # Misconfigured — refuse rather than silently opening.
        log.error("voice-agent mode active but ANTHROPIC_API_KEY is unset, refusing")
        response.say("System not configured.")
        response.hangup()
        return str(response)

    connect = Connect()
    relay = connect.conversation_relay(
        url=relay_ws_url,
        welcome_greeting=config.AGENT_GREETING,
        # Unset -> Twilio's defaults. Set AGENT_TTS_PROVIDER=ElevenLabs plus an
        # AGENT_VOICE id to switch; the agent loop is unaffected either way.
        tts_provider=config.AGENT_TTS_PROVIDER or None,
        voice=config.AGENT_VOICE or None,
    )
    # Twilio signs the HTTP webhooks but not the WebSocket upgrade, so the
    # signature check that guards /voice cannot guard /relay. Hand Twilio a
    # secret here and require it back in the setup frame: anyone who reaches the
    # socket without it never got this TwiML, and is hung up on.
    relay.parameter(name="secret", value=config.RELAY_SECRET)
    response.append(connect)

    return str(response)


def build_people_agent_twiml(relay_ws_url: str) -> str:
    """Build TwiML for voice-agent-people mode.

    Same ConversationRelay handoff as voice-agent, plus DTMF: a visitor who
    misses their question falls back to typing their code, and the keypresses
    have to reach us over the same WebSocket.

    Two Twilio defaults have to be overridden for that fallback to work at all:

      dtmfDetection                — off by default; without it Twilio never
                                     sends the keypresses anywhere.
      reportInputDuringAgentSpeech — "none" since May 2025, meaning digits
                                     pressed while the prompt is still playing
                                     are DISCARDED. People start typing the
                                     moment they hear "enter your code", so
                                     without this their first digits vanish and
                                     a correct code reads as a wrong one.
    """
    response = VoiceResponse()

    if not config.ANTHROPIC_API_KEY:
        # Misconfigured — refuse rather than silently opening.
        log.error("voice-agent-people mode active but ANTHROPIC_API_KEY is unset, refusing")
        response.say("System not configured.")
        response.hangup()
        return str(response)

    if not config.AGENT_PEOPLE:
        # No roster means no challenge is possible. Refuse rather than fall
        # back to something more permissive.
        log.error("voice-agent-people mode active but AGENT_PEOPLE is empty, refusing")
        response.say("System not configured.")
        response.hangup()
        return str(response)

    connect = Connect()
    relay = connect.conversation_relay(
        url=relay_ws_url,
        welcome_greeting=config.AGENT_GREETING,
        tts_provider=config.AGENT_TTS_PROVIDER or None,
        voice=config.AGENT_VOICE or None,
        dtmf_detection=True,
        report_input_during_agent_speech="dtmf",
    )
    relay.parameter(name="secret", value=config.RELAY_SECRET)
    response.append(connect)

    return str(response)


def _open_door(response: VoiceResponse) -> None:
    """Play the DTMF tones that trip the intercom's door relay."""
    response.play(digits=config.OPEN_DIGITS)


@app.post("/voice")
async def voice(request: Request, background: BackgroundTasks) -> Response:
    """Twilio voice webhook: dispatch to the configured call-handling mode."""
    form = await request.form()
    params = dict(form)
    _validate_twilio_request(request, params)

    log.info(
        "voice: CallSid=%s From=%s mode=%s",
        params.get("CallSid"),
        params.get("From"),
        config.MODE,
    )

    base = _external_base_url(request)
    if config.MODE == "passcode":
        twiml = build_passcode_twiml(f"{base}passcode")
    elif config.MODE == "auto":
        twiml = build_auto_entry_twiml(f"{base}recording-status")
        # auto opens for everyone, so the call is decided here and now. Queue the
        # SMS as a background task so the Twilio round-trip does not sit between
        # this request and the door-opening TwiML — the door must not wait on a
        # text. FastAPI runs the blocking send in a threadpool after the response
        # is sent, and notify swallows its own errors either way.
        background.add_task(
            notify.send_call_summary,
            mode="auto",
            from_number=params.get("From"),
            to_number=params.get("To"),
            approved=True,
            outcome="opened",
        )
    elif config.MODE in ("voice-agent", "voice-agent-people"):
        # ConversationRelay requires a wss:// URL. base is https:// in
        # production (Cloud Run terminates TLS) and http:// only in local dev.
        ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        if config.MODE == "voice-agent-people":
            # A separate socket, not a branch inside /relay: the two modes have
            # different state and different failure modes, and voice-agent is
            # the one that is already working in production.
            twiml = build_people_agent_twiml(f"{ws_base}relay-people")
        else:
            twiml = build_voice_agent_twiml(f"{ws_base}relay")
    else:
        log.error("voice: unrecognized mode=%r, refusing", config.MODE)
        response = VoiceResponse()
        response.say("Invalid mode. Goodbye.")
        response.hangup()
        twiml = str(response)

    return Response(content=twiml, media_type="application/xml")


@app.post("/passcode")
async def passcode(request: Request, background: BackgroundTasks) -> Response:
    """Twilio gather callback: verify the DTMF passcode and open on a match."""
    form = await request.form()
    params = dict(form)
    _validate_twilio_request(request, params)

    digits = str(params.get("Digits", ""))
    response = VoiceResponse()
    matched = bool(config.PASSCODE) and hmac.compare_digest(digits, config.PASSCODE)

    # Length and match are enough to tell "panel sent nothing" (0 digits) from
    # "panel dropped tones" (short) from "wrong or transposed code" (full length,
    # no match). The digits themselves are the passcode, so they are logged only
    # under DEBUG_DTMF.
    log.info(
        "passcode: CallSid=%s digits_len=%d expected_len=%d matched=%s",
        params.get("CallSid"),
        len(digits),
        len(config.PASSCODE),
        matched,
    )
    if config.DEBUG_DTMF:
        log.warning("passcode: DEBUG_DTMF on, raw digits=%r", digits)

    if matched:
        response.say("Opening.")
        _open_door(response)
    else:
        response.say("Incorrect passcode. Goodbye.")

    # A timed-out call ("no passcode entered") is served by TwiML in
    # build_passcode_twiml and never reaches here, so it is not notified — the
    # same reason it leaves no "passcode:" log line. Every call that does reach
    # here is a real decision worth a text. Queue it as a background task so the
    # Twilio SMS round-trip never sits between this request and the TwiML.
    background.add_task(
        notify.send_call_summary,
        mode="passcode",
        from_number=params.get("From"),
        to_number=params.get("To"),
        approved=matched,
        outcome="opened" if matched else "incorrect passcode",
    )

    response.hangup()
    return Response(content=str(response), media_type="application/xml")


async def _send(ws: WebSocket, payload: dict[str, Any]) -> None:
    """Send one ConversationRelay frame."""
    await ws.send_text(json.dumps(payload))


async def _say(ws: WebSocket, text: str) -> None:
    """Speak a line to the caller. Twilio does the text-to-speech."""
    await _send(ws, {"type": "text", "token": text, "last": True})


async def _hang_up(ws: WebSocket) -> None:
    """End the call.

    `end` cuts the call at once, discarding anything Twilio has queued but not
    yet played. That is fine on the deny path — the visitor hearing half of
    "Sorry, I can't let you in" costs nothing. It is NOT fine after sendDigits;
    see _open_and_release.
    """
    await _send(ws, {"type": "end"})


async def _open_and_release(ws: WebSocket) -> None:
    """Play the door tones and let the call wind down on its own.

    Deliberately does NOT send `end`. Twilio plays sendDigits asynchronously, so
    an `end` frame immediately afterwards tears the call down mid-tone and the
    intercom hears a truncated sequence — the door does not trip. OPEN_DIGITS is
    "ww9", two 0.5s pauses before the 9 even starts, which makes the window
    wider than it looks.

    Observed on a real call: sendDigits at 03:15:38, connection closed at
    03:15:40. The approval was correct, the SMS went out, and the door stayed
    shut. This is also why the closing line was never audible — `end` was racing
    the audio all along, and a sleep *inside* _hang_up could not fix it because
    that still delays `end` rather than letting the queued audio finish.

    Instead we return and stop reading. The socket closes when the caller hangs
    up or when Twilio finishes and drops it, by which point the tones have
    played in full.
    """
    await _send(ws, {"type": "sendDigits", "digits": config.OPEN_DIGITS})
    # Give Twilio's audio pipeline room to actually emit the tones before this
    # coroutine returns and the connection is torn down.
    await asyncio.sleep(config.OPEN_DIGITS_SETTLE)


@app.websocket("/relay")
async def relay(ws: WebSocket) -> None:
    """ConversationRelay WebSocket: talk to the visitor, decide, open or not.

    The socket is held open for the whole call, so this function *is* the
    session: `messages` is a plain local, and two simultaneous calls are two
    coroutines with two stacks. No session dict, nothing to garbage-collect, and
    no need for Cloud Run session affinity — the state lives inside the
    connection, and a WebSocket is pinned to one instance by definition.

    Fail-closed is structural, not a rule applied at each branch. The door is
    opened by exactly one line below. Every other way out of this function —
    exception, turn cap, API failure, the caller hanging up, a dropped socket —
    simply leaves it shut.
    """
    await ws.accept()

    messages: list[dict[str, Any]] = []
    call_sid = "unknown"
    opened = False
    turns = 0

    # For the post-call SMS, sent once from the finally. caller_from/service_to
    # come off the setup frame; authenticated gates the send so unauthenticated
    # probes (which fail the secret check) never text anyone. outcome/reason are
    # set at whichever terminal branch we leave through; approved is True on open,
    # False on any deny, and stays None if the call ends with no decision.
    caller_from: str | None = None
    service_to: str | None = None
    authenticated = False
    approved: bool | None = None
    outcome = "caller hung up"
    reason = ""

    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

    try:
        async for raw in ws.iter_text():
            frame = json.loads(raw)
            kind = frame.get("type")

            if kind == "setup":
                call_sid = frame.get("callSid", "unknown")
                # The secret came from the TwiML we served. A socket that cannot
                # produce it never saw that TwiML, so it is not Twilio.
                got = (frame.get("customParameters") or {}).get("secret", "")
                if not hmac.compare_digest(got, config.RELAY_SECRET):
                    log.warning("relay: CallSid=%s bad secret, closing", call_sid)
                    return
                # Past the secret check: this is a real Twilio call. The setup
                # frame carries the caller (from) and our own number (to); we
                # text the summary from the latter. authenticated gates that
                # send, so probes that never get here are never texted.
                authenticated = True
                caller_from = frame.get("from")
                service_to = frame.get("to")
                log.info(
                    "relay: CallSid=%s From=%s connected",
                    call_sid,
                    caller_from,
                )
                continue

            if kind == "prompt":
                # Twilio streams partial transcripts; wait for the complete one.
                if not frame.get("last"):
                    continue

                said = (frame.get("voicePrompt") or "").strip()
                if not said:
                    continue

                turns += 1
                if turns > config.AGENT_MAX_TURNS:
                    # A visitor who cannot explain themselves in this many turns
                    # does not get in by wearing us down.
                    log.info("relay: CallSid=%s turn cap reached, denying", call_sid)
                    approved = False
                    outcome = "denied (turn cap)"
                    await _say(ws, "Sorry, I can't let you in. Goodbye.")
                    await _hang_up(ws)
                    return

                log.info("relay: CallSid=%s turn=%d visitor=%r", call_sid, turns, said)
                messages.append({"role": "user", "content": said})

                decision = await agent.take_turn(client, messages)

                if decision.open:
                    log.info(
                        "relay: CallSid=%s OPEN reason=%r", call_sid, decision.reason
                    )
                    approved = True
                    outcome = "opened"
                    reason = decision.reason
                    await _say(ws, "Come on in.")
                    # The one line in this file that opens the door. No _hang_up
                    # after it: `end` would cut the tones off mid-play.
                    await _open_and_release(ws)
                    opened = True
                    return

                if decision.deny:
                    log.info(
                        "relay: CallSid=%s DENY reason=%r", call_sid, decision.reason
                    )
                    approved = False
                    outcome = "denied"
                    reason = decision.reason
                    await _say(ws, "Sorry, I can't let you in. Goodbye.")
                    await _hang_up(ws)
                    return

                await _say(ws, decision.speak or "")
                continue

            if kind == "error":
                log.error(
                    "relay: CallSid=%s twilio error: %s",
                    call_sid,
                    frame.get("description"),
                )
                continue

            # "interrupt" and anything else: nothing to do. The next prompt
            # frame carries whatever the visitor actually said.

    except WebSocketDisconnect:
        # outcome stays "caller hung up" (its default) unless a decision above
        # already set it — a caller can drop after being admitted or denied.
        log.info("relay: CallSid=%s caller hung up", call_sid)
    except Exception:
        # An LLM outage, a malformed frame, a bug in this loop — none of them are
        # a reason to open a door. Log it and fall through to the finally.
        if approved is None:
            outcome = "error"
        log.exception("relay: CallSid=%s failed, denying", call_sid)
    finally:
        if not opened:
            log.info("relay: CallSid=%s closed without opening", call_sid)
        # One summary per call, from the single exit every path funnels through.
        # Gated on authenticated so unauthenticated probes are never texted.
        # Fire-and-forget: awaiting to_thread here holds the WebSocket open for
        # the Twilio round-trip and deadlocks Starlette's TestClient on teardown.
        # notify swallows its own errors; the door decision is already final.
        if authenticated:
            asyncio.create_task(
                asyncio.to_thread(
                    notify.send_call_summary,
                    mode="voice-agent",
                    from_number=caller_from,
                    to_number=service_to,
                    approved=approved,
                    outcome=outcome,
                    reason=reason,
                    transcript=notify.render_transcript(messages),
                )
            )


async def _collect_code(ws: WebSocket, length: int) -> str | None:
    """Read `length` DTMF digits off the socket. None on timeout or hang-up.

    ConversationRelay sends one frame per keypress — {"type":"dtmf","digit":"4"}
    — with no notion of "the caller is done". There is no <Gather numDigits>
    here, so the buffer, the length check, and the idle timeout are all ours.

    The timeout is per digit, not for the whole code: someone hunting for the
    keys on a cold night should not be cut off mid-entry, but a call sitting
    open with three digits typed and nobody there should end.

    Non-DTMF frames are consumed and ignored while we wait. A visitor who keeps
    talking after being asked for a code is not answering the question, and the
    model is not consulted again — this is a keypad prompt, not a conversation.
    """
    digits = ""
    while len(digits) < length:
        try:
            raw = await asyncio.wait_for(
                ws.receive_text(), timeout=config.AGENT_CODE_TIMEOUT
            )
        except asyncio.TimeoutError:
            return None

        frame = json.loads(raw)
        if frame.get("type") != "dtmf":
            continue

        digit = str(frame.get("digit", ""))
        # '*' clears, so a fumbled entry can be restarted without hanging up.
        if digit == "*":
            digits = ""
            continue
        if digit.isdigit():
            digits += digit

    return digits


@app.websocket("/relay-people")
async def relay_people(ws: WebSocket) -> None:
    """ConversationRelay WebSocket for voice-agent-people mode.

    The flow, and which layer owns each step:

      1. Visitor states a name.            model classifies it against the roster
      2. Known name  -> one random question from THEIR corpus.
                                           model asks and judges that question alone
         Unknown name -> escalate or deny. model may call ask_resident (SMS YES/NO)
                                           or deny_entry; it cannot open_door
      3. Wrong answer, known person -> code fallback.   THIS FILE, not the model
      4. ask_resident -> text resident, wait for YES/NO. THIS FILE

    Two properties are load-bearing:

    The model never sees a code. It is not in any prompt, so no amount of
    talking can extract one, and the comparison below is plain code.

    An unknown name is never offered a code. Known names get two factors;
    unknowns get a live resident decision or a denial. Otherwise the attack is
    "say a name nobody recognizes, then brute-force four digits."

    Fail-closed is structural here as it is in /relay: the door is opened by
    three lines below (question passed, code matched, or resident SMS YES) and
    every other exit — turn cap, timeout, exception, hang-up — leaves it shut.
    """
    await ws.accept()

    messages: list[dict[str, Any]] = []
    call_sid = "unknown"
    opened = False
    turns = 0

    # Set once the visitor's name is matched. None means we never matched one,
    # which is what gates the code fallback: no person, no code to fall back to.
    person: dict[str, Any] | None = None
    system_prompt: str | None = None
    # Known path uses open_door/deny_entry; unknown path uses ask_resident/deny.
    turn_tools: list[dict[str, Any]] | None = None

    caller_from: str | None = None
    service_to: str | None = None
    authenticated = False
    approved: bool | None = None
    outcome = "caller hung up"
    reason = ""

    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

    try:
        async for raw in ws.iter_text():
            frame = json.loads(raw)
            kind = frame.get("type")

            if kind == "setup":
                call_sid = frame.get("callSid", "unknown")
                got = (frame.get("customParameters") or {}).get("secret", "")
                if not hmac.compare_digest(got, config.RELAY_SECRET):
                    log.warning("relay-people: CallSid=%s bad secret, closing", call_sid)
                    return
                authenticated = True
                caller_from = frame.get("from")
                service_to = frame.get("to")
                log.info(
                    "relay-people: CallSid=%s From=%s connected", call_sid, caller_from
                )
                continue

            if kind == "prompt":
                if not frame.get("last"):
                    continue

                said = (frame.get("voicePrompt") or "").strip()
                if not said:
                    continue

                turns += 1
                if turns > config.AGENT_MAX_TURNS:
                    log.info(
                        "relay-people: CallSid=%s turn cap reached, denying", call_sid
                    )
                    approved = False
                    outcome = "denied (turn cap)"
                    await _say(ws, "Sorry, I can't let you in. Goodbye.")
                    await _hang_up(ws)
                    return

                # First utterance is the name. Match it, then build the prompt
                # that will run for the rest of the call.
                if system_prompt is None:
                    person = await agent.classify_person(client, said)
                    if person is not None:
                        question = agent.pick_question(person)
                        log.info(
                            "relay-people: CallSid=%s matched person=%r question=%r",
                            call_sid,
                            person.get("name"),
                            question.get("ask"),
                        )
                        system_prompt = agent.build_person_prompt(person, question)
                        turn_tools = agent.TOOLS
                    else:
                        log.info("relay-people: CallSid=%s no person matched", call_sid)
                        system_prompt = agent.build_unknown_prompt()
                        turn_tools = agent.UNKNOWN_TOOLS

                log.info(
                    "relay-people: CallSid=%s turn=%d visitor=%r", call_sid, turns, said
                )
                messages.append({"role": "user", "content": said})

                decision = await agent.take_turn(
                    client, messages, system=system_prompt, tools=turn_tools
                )

                if decision.ask_resident:
                    # Unknown-name path only: text the resident and wait. The
                    # model cannot open the door itself on this path.
                    claim = decision.visitor_claim or said
                    log.info(
                        "relay-people: CallSid=%s ask_resident claim=%r reason=%r",
                        call_sid,
                        claim,
                        decision.reason,
                    )
                    await _say(ws, "One moment please.")
                    pending = await approvals.try_begin(call_sid=call_sid, claim=claim)
                    if pending is None:
                        approved = False
                        outcome = "denied (approval already pending)"
                        reason = decision.reason
                        await _say(ws, "Sorry, I can't let you in. Goodbye.")
                        await _hang_up(ws)
                        return

                    sent = await asyncio.to_thread(
                        notify.send_approval_request,
                        to_number=service_to,
                        claim=claim,
                        timeout=config.AGENT_APPROVAL_TIMEOUT,
                    )
                    if not sent:
                        approvals.resolve(False)
                        await approvals.wait(pending, timeout=1.0)
                        approved = False
                        outcome = "denied (could not text resident)"
                        reason = decision.reason
                        await _say(ws, "Sorry, I can't let you in. Goodbye.")
                        await _hang_up(ws)
                        return

                    result = await approvals.wait(
                        pending, timeout=float(config.AGENT_APPROVAL_TIMEOUT)
                    )
                    if result is True:
                        log.info(
                            "relay-people: CallSid=%s OPEN via resident SMS", call_sid
                        )
                        approved = True
                        outcome = "opened (resident approved by SMS)"
                        reason = decision.reason
                        await _say(ws, "Come on in.")
                        await _open_and_release(ws)
                        opened = True
                        return

                    approved = False
                    if result is False:
                        outcome = "denied (resident rejected by SMS)"
                    else:
                        outcome = "denied (resident SMS timed out)"
                    reason = decision.reason
                    await _say(ws, "Sorry, I can't let you in. Goodbye.")
                    await _hang_up(ws)
                    return

                if decision.open:
                    log.info(
                        "relay-people: CallSid=%s OPEN reason=%r",
                        call_sid,
                        decision.reason,
                    )
                    approved = True
                    outcome = (
                        f"opened ({person['name']} answered)" if person else "opened"
                    )
                    reason = decision.reason
                    await _say(ws, "Come on in.")
                    await _open_and_release(ws)
                    opened = True
                    return

                if decision.deny:
                    # An unmatched visitor has no code, so a denial is final.
                    if person is None:
                        log.info(
                            "relay-people: CallSid=%s DENY (no person) reason=%r",
                            call_sid,
                            decision.reason,
                        )
                        approved = False
                        outcome = "denied"
                        reason = decision.reason
                        await _say(ws, "Sorry, I can't let you in. Goodbye.")
                        await _hang_up(ws)
                        return

                    # A known person who missed their question gets the keypad.
                    # The line is fixed and says nothing about the answer — the
                    # model is forbidden from grading out loud, and this must
                    # not grade for it.
                    code = str(person.get("code", ""))
                    name = str(person.get("name", ""))
                    log.info(
                        "relay-people: CallSid=%s question failed for person=%r, "
                        "falling back to code",
                        call_sid,
                        name,
                    )
                    matched = False
                    for attempt in range(config.AGENT_CODE_ATTEMPTS):
                        await _say(ws, "Please enter your code.")
                        entered = await _collect_code(ws, len(code))
                        if entered is None:
                            log.info(
                                "relay-people: CallSid=%s code entry timed out",
                                call_sid,
                            )
                            break
                        if hmac.compare_digest(entered, code):
                            matched = True
                            break
                        log.info(
                            "relay-people: CallSid=%s wrong code attempt=%d",
                            call_sid,
                            attempt + 1,
                        )

                    if matched:
                        log.info(
                            "relay-people: CallSid=%s OPEN via code person=%r",
                            call_sid,
                            name,
                        )
                        approved = True
                        outcome = f"opened ({name} entered code)"
                        reason = f"missed question, entered {name}'s code"
                        await _say(ws, "Come on in.")
                        await _open_and_release(ws)
                        opened = True
                        return

                    approved = False
                    outcome = "denied (wrong answer and wrong code)"
                    reason = f"claimed to be {name}, missed the question and the code"
                    await _say(ws, "Sorry, I can't let you in. Goodbye.")
                    await _hang_up(ws)
                    return

                await _say(ws, decision.speak or "")
                continue

            if kind == "error":
                log.error(
                    "relay-people: CallSid=%s twilio error: %s",
                    call_sid,
                    frame.get("description"),
                )
                continue

            # DTMF outside a code prompt is nothing to act on: the fallback is
            # offered only after a failed challenge, and reaching it any other
            # way would skip the challenge entirely.

    except WebSocketDisconnect:
        log.info("relay-people: CallSid=%s caller hung up", call_sid)
    except Exception:
        if approved is None:
            outcome = "error"
        log.exception("relay-people: CallSid=%s failed, denying", call_sid)
    finally:
        if not opened:
            log.info("relay-people: CallSid=%s closed without opening", call_sid)
        # Fire-and-forget — same reason as /relay: awaiting the SMS holds the
        # socket open and deadlocks TestClient teardown.
        if authenticated:
            asyncio.create_task(
                asyncio.to_thread(
                    notify.send_call_summary,
                    mode="voice-agent-people",
                    from_number=caller_from,
                    to_number=service_to,
                    approved=approved,
                    outcome=outcome,
                    reason=reason,
                    transcript=notify.render_transcript(messages),
                )
            )


@app.post("/sms")
async def sms(request: Request) -> Response:
    """Twilio inbound SMS: resolve a pending ask_resident YES/NO.

    Only replies from NOTIFY_SMS_TO count — anyone else texting the service
    number is ignored. Unrecognized bodies get a brief hint so a typo is not
    silent; a resolved decision gets a short ack.
    """
    form = await request.form()
    params = dict(form)
    _validate_twilio_request(request, params)

    from_number = str(params.get("From", ""))
    body = str(params.get("Body", ""))
    log.info("sms: From=%s body_len=%d", from_number, len(body))

    empty = Response(content="<Response></Response>", media_type="application/xml")

    if not config.NOTIFY_SMS_TO or from_number != config.NOTIFY_SMS_TO:
        log.warning("sms: ignoring message from non-resident From=%s", from_number)
        return empty

    decision = approvals.parse_reply(body)
    if decision is None:
        log.info("sms: unrecognized reply body=%r", body[:80])
        return Response(
            content=(
                "<Response><Message>Reply YES to open or NO to deny.</Message></Response>"
            ),
            media_type="application/xml",
        )

    call_sid = approvals.resolve(decision)
    if call_sid is None:
        log.info("sms: no pending approval for decision=%s", decision)
        return Response(
            content=(
                "<Response><Message>No door request waiting.</Message></Response>"
            ),
            media_type="application/xml",
        )

    log.info("sms: resolved CallSid=%s approved=%s", call_sid, decision)
    ack = "Opening." if decision else "Denied."
    return Response(
        content=f"<Response><Message>{ack}</Message></Response>",
        media_type="application/xml",
    )


@app.post("/recording-status")
async def recording_status(request: Request) -> Response:
    """Twilio recording callback: log metadata to stdout."""
    form = await request.form()
    params = dict(form)
    _validate_twilio_request(request, params)

    print(
        "Recording callback:",
        f"CallSid={params.get('CallSid')}",
        f"RecordingSid={params.get('RecordingSid')}",
        f"RecordingUrl={params.get('RecordingUrl')}",
        f"RecordingDuration={params.get('RecordingDuration')}",
        f"RecordingStatus={params.get('RecordingStatus')}",
    )

    return Response(status_code=200)
