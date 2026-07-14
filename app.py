import hmac
import json
import logging
from typing import Any

import anthropic
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Connect, Gather, Start, VoiceResponse

import agent
import config

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


def _open_door(response: VoiceResponse) -> None:
    """Play the DTMF tones that trip the intercom's door relay."""
    response.play(digits=config.OPEN_DIGITS)


@app.post("/voice")
async def voice(request: Request) -> Response:
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
    elif config.MODE == "voice-agent":
        # ConversationRelay requires a wss:// URL. base is https:// in
        # production (Cloud Run terminates TLS) and http:// only in local dev.
        ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        twiml = build_voice_agent_twiml(f"{ws_base}relay")
    else:
        log.error("voice: unrecognized mode=%r, refusing", config.MODE)
        response = VoiceResponse()
        response.say("Invalid mode. Goodbye.")
        response.hangup()
        twiml = str(response)

    return Response(content=twiml, media_type="application/xml")


@app.post("/passcode")
async def passcode(request: Request) -> Response:
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

    The closing line ("Come on in." / "Sorry, I can't let you in.") is not
    audible on a real call. A 2.5s sleep here — on the theory that "end" was
    racing the TTS — did NOT fix it, so the cause is something else and the sleep
    was removed rather than tuned. The door still opens correctly; only the final
    spoken line is missing. Unresolved.
    """
    await _send(ws, {"type": "end"})


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
                log.info(
                    "relay: CallSid=%s From=%s connected",
                    call_sid,
                    frame.get("from"),
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
                    await _say(ws, "Come on in.")
                    # The one line in this file that opens the door. Queued ahead
                    # of the hang-up sleep, so the door opens even if the audio
                    # gets clipped.
                    await _send(
                        ws, {"type": "sendDigits", "digits": config.OPEN_DIGITS}
                    )
                    opened = True
                    await _hang_up(ws)
                    return

                if decision.deny:
                    log.info(
                        "relay: CallSid=%s DENY reason=%r", call_sid, decision.reason
                    )
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
        log.info("relay: CallSid=%s caller hung up", call_sid)
    except Exception:
        # An LLM outage, a malformed frame, a bug in this loop — none of them are
        # a reason to open a door. Log it and fall through to the finally.
        log.exception("relay: CallSid=%s failed, denying", call_sid)
    finally:
        if not opened:
            log.info("relay: CallSid=%s closed without opening", call_sid)


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
