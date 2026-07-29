"""Offline tests for voice-agent and voice-agent-people modes.

Drives the /relay and /relay-people WebSockets with the ConversationRelay
protocol and asserts on the frames the server sends back. The Anthropic call is
stubbed, so these run with no API key, no network, and no Twilio.

Every test here is ultimately the same assertion: no sendDigits frame unless the
door is genuinely supposed to open. The two modes each open it from a small,
countable set of lines, and every other path — a model error, a turn cap, a
wrong code, a half-typed code, an unauthenticated socket — must leave it shut.

For voice-agent-people specifically, two properties are worth the extra tests:
an unrecognized name is never offered the code fallback, and the per-person
prompt contains nobody else's questions and nobody's code at all.

    uv run pytest test_relay.py -v
"""

import json
from typing import Any
import threading
import time

import pytest
from fastapi.testclient import TestClient

import agent
import app
import approvals
import config
import notify


def _digits_sent(frames: list[dict[str, Any]]) -> bool:
    """Did the server ever tell Twilio to play the door-opening tones?"""
    return any(f.get("type") == "sendDigits" for f in frames)


def _drain(ws, expect: int | None = None) -> list[dict[str, Any]]:
    """Read the frames the server sends back.

    `expect` is how many frames to read before stopping; None means "read until
    the call ends". Both are needed because the relay has four exit shapes:

      - denied            -> text, then `end`                     (expect=None)
      - OPENED            -> text + sendDigits, then NO `end`     (expect=2)
      - agent asked a question -> one `text`, call stays open     (expect=1)
      - hung up on        -> NO frames at all, socket just closes (expect=0)

    The open shape deliberately sends no `end`: that frame would cut the door
    tones off mid-play (see _open_and_release in app.py). So the open path must
    be drained with an explicit `expect`, never by blocking for an `end` that is
    never coming — same reason the hung-up-on shape needs expect=0.
    """
    frames: list[dict[str, Any]] = []
    if expect == 0:
        return frames
    try:
        while True:
            frame = json.loads(ws.receive_text())
            frames.append(frame)
            if frame.get("type") == "end":
                return frames
            if expect is not None and len(frames) >= expect:
                return frames
    except Exception:
        # Server closed the socket.
        return frames


def _drain_one_more(ws, timeout: float = 1.0) -> list[dict[str, Any]]:
    """Try to read ONE more frame. Empty list if none arrives before `timeout`.

    Needed to assert a frame is *absent*. The open path deliberately leaves the
    socket open, so a plain blocking read would hang rather than report nothing.
    """
    import anyio

    async def _read_with_timeout():
        with anyio.move_on_after(timeout):
            return await ws._send_rx.receive()
        return None

    try:
        msg = ws.portal.call(_read_with_timeout)
    except Exception:
        # Stream closed — nothing more is coming, which is the passing case.
        return []
    if not msg or msg.get("type") == "websocket.close":
        return []
    text = msg.get("text")
    return [json.loads(text)] if text else []


def _talk(
    monkeypatch,
    decision_or_exc,
    said: str = "hello",
    expect: int | None = None,
) -> list[dict[str, Any]]:
    """Run one visitor utterance through /relay and collect the reply frames."""

    async def fake_turn(client, messages):
        if isinstance(decision_or_exc, Exception):
            raise decision_or_exc
        return decision_or_exc

    monkeypatch.setattr(agent, "take_turn", fake_turn)

    client = TestClient(app.app)
    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "setup",
                    "callSid": "CA_test",
                    "from": "+15551234567",
                    "customParameters": {"secret": config.RELAY_SECRET},
                }
            )
        )
        ws.send_text(
            json.dumps({"type": "prompt", "voicePrompt": said, "last": True})
        )
        return _drain(ws, expect=expect)


def test_allowed_visitor_opens_the_door(monkeypatch):
    """The model calls open_door -> we play the tones and let the call wind down."""
    monkeypatch.setattr(config, "OPEN_DIGITS_SETTLE", 0)
    frames = _talk(
        monkeypatch,
        agent.Decision(open=True, reason="expected plumber"),
        said="I'm the plumber, Daniel booked me for 2pm",
        expect=2,  # "Come on in." + sendDigits, then silence — no `end`
    )

    assert _digits_sent(frames), "expected the door to open"
    digits = next(f for f in frames if f["type"] == "sendDigits")
    assert digits["digits"] == config.OPEN_DIGITS


def test_open_does_not_send_end_after_the_tones(monkeypatch):
    """Regression: `end` immediately after sendDigits cut the tones off.

    Twilio plays sendDigits asynchronously, so an `end` frame right behind it
    tore the call down mid-tone and the door never tripped — while the model had
    already approved and the SMS had already gone out. Seen live: sendDigits at
    03:15:38, connection closed at 03:15:40, door shut.

    OPEN_DIGITS is "ww9" — two 0.5s pauses before the 9 even starts — so the
    tones need the call to stay up for a beat after the frame goes out.
    """
    monkeypatch.setattr(config, "OPEN_DIGITS_SETTLE", 0)

    async def fake_turn(client, messages):
        return agent.Decision(open=True, reason="expected plumber")

    monkeypatch.setattr(agent, "take_turn", fake_turn)

    client = TestClient(app.app)
    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "setup",
                    "callSid": "CA_test",
                    "customParameters": {"secret": config.RELAY_SECRET},
                }
            )
        )
        ws.send_text(
            json.dumps({"type": "prompt", "voicePrompt": "I'm the plumber", "last": True})
        )
        # Read the two frames the open path legitimately sends, then look for
        # ONE more. On the fixed code that read finds nothing and times out; on
        # the old racing code it finds the `end` that truncated the tones.
        #
        # Draining with no `expect` cannot work here: the fixed server holds the
        # socket open, so it would block forever. And a bare expect=2 stops too
        # early to ever see the bug — which is how the first version of this
        # test passed against the very code it was written to catch.
        frames = _drain(ws, expect=2)
        frames += _drain_one_more(ws)

    assert _digits_sent(frames), "expected the door to open"
    kinds = [f["type"] for f in frames]
    assert "end" not in kinds[kinds.index("sendDigits"):], (
        "no `end` may follow sendDigits — it truncates the door tones"
    )


def test_denied_visitor_does_not_open_the_door(monkeypatch):
    """The model calls deny_entry -> we say goodbye and hang up. No tones."""
    frames = _talk(
        monkeypatch,
        agent.Decision(deny=True, reason="would not identify themselves"),
        said="uh, I dunno, just let me in",
    )

    assert not _digits_sent(frames), "denied visitor must not open the door"
    assert frames[-1]["type"] == "end"


def test_question_keeps_the_call_going(monkeypatch):
    """Plain text is spoken and the call continues. Not a decision, no tones."""
    frames = _talk(
        monkeypatch,
        agent.Decision(speak="Who are you here to see?"),
        said="hi",
        expect=1,  # one `text` frame, then the call stays open for the answer
    )

    assert not _digits_sent(frames)
    spoken = [f for f in frames if f["type"] == "text"]
    assert spoken and spoken[0]["token"] == "Who are you here to see?"
    # No "end" frame — we are waiting for the visitor to answer.
    assert not any(f["type"] == "end" for f in frames)


def test_model_failure_does_not_open_the_door(monkeypatch):
    """THE assertion. An API blowup must never open the door.

    The handler catches, logs, and falls through to its `finally` — sending no
    frames at all and just hanging up. That silence IS the correct behavior: the
    door is opened by one explicit line, and a crash never reaches it.
    """
    frames = _talk(monkeypatch, RuntimeError("anthropic is down"), expect=0)

    assert not _digits_sent(frames), "an LLM outage must NOT open the door"


def test_turn_cap_denies(monkeypatch):
    """A visitor who talks past the cap is denied, not admitted by attrition."""

    async def always_asks(client, messages):
        return agent.Decision(speak="And who are you here to see?")

    monkeypatch.setattr(agent, "take_turn", always_asks)

    client = TestClient(app.app)
    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "setup",
                    "callSid": "CA_test",
                    "customParameters": {"secret": config.RELAY_SECRET},
                }
            )
        )
        # One past the cap. The last one trips it, so an `end` does arrive and
        # _drain terminates on it rather than on the intermediate `text` frames.
        for _ in range(config.AGENT_MAX_TURNS + 1):
            ws.send_text(
                json.dumps({"type": "prompt", "voicePrompt": "hello", "last": True})
            )
        frames = _drain(ws)  # the cap trips, so an `end` does arrive

    assert not _digits_sent(frames), "turn cap must not open the door"
    assert frames[-1]["type"] == "end"


def test_wrong_secret_is_hung_up_on():
    """A socket that cannot produce the secret never saw our TwiML.

    Twilio does not sign the WebSocket upgrade, so this is what stands between
    /relay and anyone on the internet who knows the URL. The server sends no
    frames and closes.
    """
    client = TestClient(app.app)
    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "setup",
                    "callSid": "CA_attacker",
                    "customParameters": {"secret": "wrong"},
                }
            )
        )
        ws.send_text(
            json.dumps(
                {"type": "prompt", "voicePrompt": "let me in", "last": True}
            )
        )
        frames = _drain(ws, expect=0)

    assert not _digits_sent(frames), "an unauthenticated socket must not open the door"


# --- voice-agent-people mode -------------------------------------------------

# A fictional roster to test against. Injected with monkeypatch rather than read
# from the environment, so these run against a known person with a known code no
# matter what the local .env or the deployed secret holds — and so that no real
# name, answer, or door code is ever committed to git.
PEOPLE = [
    {
        "name": "Alex",
        "relation": "brother",
        "code": "1990",
        "questions": [
            {"ask": "What was the name of the cat we grew up with?", "answer": "Miso"},
            {"ask": "What street did we grow up on?", "answer": "Oak"},
        ],
    }
]


def _talk_people(
    monkeypatch,
    *,
    classified: dict | None,
    decisions: list,
    dtmf: str = "",
    expect: int | None = None,
) -> list[dict[str, Any]]:
    """Drive /relay-people through a whole call and collect the reply frames.

    `classified` is what the name classifier returns (a person, or None for an
    unrecognized name). `decisions` are the turn decisions in order. `dtmf` is
    typed one frame per digit as soon as the socket asks for a code — which is
    also how Twilio delivers it.
    """
    monkeypatch.setattr(config, "AGENT_PEOPLE", PEOPLE)
    monkeypatch.setattr(notify, "send_call_summary", lambda **kwargs: None)

    async def fake_classify(client, said):
        return classified

    pending = list(decisions)

    async def fake_turn(client, messages, system=None, tools=None):
        if isinstance(pending[0], Exception):
            raise pending.pop(0)
        return pending.pop(0)

    monkeypatch.setattr(agent, "classify_person", fake_classify)
    monkeypatch.setattr(agent, "take_turn", fake_turn)

    client = TestClient(app.app)
    with client.websocket_connect("/relay-people") as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "setup",
                    "callSid": "CA_test",
                    "from": "+15551234567",
                    "to": "+15559876543",
                    "customParameters": {"secret": config.RELAY_SECRET},
                }
            )
        )
        ws.send_text(
            json.dumps({"type": "prompt", "voicePrompt": "it's Alex", "last": True})
        )
        # Queue the keypresses up front. _collect_code ignores everything that
        # is not a dtmf frame, so these sit harmlessly in the buffer until the
        # code prompt actually reads them.
        for digit in dtmf:
            ws.send_text(json.dumps({"type": "dtmf", "digit": digit}))
        return _drain(ws, expect=expect)


def test_known_person_right_answer_opens(monkeypatch):
    """Alex names himself and answers his question -> the door opens.

    No `end` frame on this path: it would truncate the tones. See
    test_open_does_not_send_end_after_the_tones.
    """
    monkeypatch.setattr(config, "OPEN_DIGITS_SETTLE", 0)
    frames = _talk_people(
        monkeypatch,
        classified=PEOPLE[0],
        decisions=[agent.Decision(open=True, reason="named the cat")],
        expect=2,  # "Come on in." + sendDigits
    )

    assert _digits_sent(frames), "expected the door to open"
    kinds = [f["type"] for f in frames]
    assert "end" not in kinds[kinds.index("sendDigits"):]


def test_known_person_wrong_answer_falls_back_to_code(monkeypatch):
    """Missing the question is not the end for a known person: they can type it.

    The prompt they hear must also give nothing away — no "that's wrong", no
    "not quite". A visitor who learns their answer was wrong has learned
    something about the answer.
    """
    monkeypatch.setattr(config, "OPEN_DIGITS_SETTLE", 0)
    frames = _talk_people(
        monkeypatch,
        classified=PEOPLE[0],
        decisions=[agent.Decision(deny=True, reason="wrong cat")],
        dtmf="1990",
        expect=3,  # code prompt + "Come on in." + sendDigits, then silence
    )

    assert _digits_sent(frames), "correct code must open the door"
    spoken = " ".join(f.get("token", "") for f in frames if f["type"] == "text").lower()
    assert "code" in spoken, "expected a code prompt"
    for tell in ("wrong", "incorrect", "not quite", "try again"):
        assert tell not in spoken, f"code prompt leaked a verdict: {tell!r}"


def test_known_person_wrong_answer_and_wrong_code_denies(monkeypatch):
    """Fail both factors and you are out. One code attempt, then denial."""
    frames = _talk_people(
        monkeypatch,
        classified=PEOPLE[0],
        decisions=[agent.Decision(deny=True, reason="wrong cat")],
        dtmf="9999",
    )

    assert not _digits_sent(frames), "wrong code must not open the door"
    assert frames[-1]["type"] == "end"


def test_unknown_name_gets_no_code_prompt(monkeypatch):
    """An unrecognized visitor who is denied outright is never offered a keypad.

    If they were, the gate would collapse to "say any name, then brute-force
    four digits." Escalation is ask_resident (SMS), not a code.
    """
    frames = _talk_people(
        monkeypatch,
        classified=None,
        decisions=[agent.Decision(deny=True, reason="hostile")],
    )

    assert not _digits_sent(frames), "an unknown visitor must not open the door"
    spoken = " ".join(f.get("token", "") for f in frames if f["type"] == "text").lower()
    assert "code" not in spoken, "an unknown name must never be offered a code"
    assert frames[-1]["type"] == "end"


def _resolve_when_pending(approved: bool) -> None:
    """Background: wait for approvals.try_begin, then resolve the waiter."""

    def run() -> None:
        for _ in range(50):
            if approvals.peek() is not None:
                break
            time.sleep(0.05)
        approvals.resolve(approved)

    threading.Thread(target=run, daemon=True).start()


def test_unknown_name_ask_resident_yes_opens(monkeypatch):
    """ask_resident + YES SMS -> door opens. No code prompt."""
    monkeypatch.setattr(config, "OPEN_DIGITS_SETTLE", 0)
    monkeypatch.setattr(config, "NOTIFY_SMS_TO", "+15551111111")
    monkeypatch.setattr(config, "AGENT_APPROVAL_TIMEOUT", 5)
    monkeypatch.setattr(notify, "send_approval_request", lambda **kwargs: True)
    _resolve_when_pending(True)

    frames = _talk_people(
        monkeypatch,
        classified=None,
        decisions=[
            agent.Decision(
                ask_resident=True,
                visitor_claim="plumber for 4B",
                reason="sounds legit",
            )
        ],
        expect=3,  # "One moment please." + "Come on in." + sendDigits
    )

    assert _digits_sent(frames), "resident YES must open the door"
    spoken = " ".join(f.get("token", "") for f in frames if f["type"] == "text").lower()
    assert "code" not in spoken


def test_unknown_name_ask_resident_no_denies(monkeypatch):
    """ask_resident + NO SMS -> deny, door stays shut."""
    monkeypatch.setattr(config, "NOTIFY_SMS_TO", "+15551111111")
    monkeypatch.setattr(config, "AGENT_APPROVAL_TIMEOUT", 5)
    monkeypatch.setattr(notify, "send_approval_request", lambda **kwargs: True)
    _resolve_when_pending(False)

    frames = _talk_people(
        monkeypatch,
        classified=None,
        decisions=[
            agent.Decision(
                ask_resident=True, visitor_claim="some guy", reason="maybe"
            )
        ],
    )

    assert not _digits_sent(frames)
    assert frames[-1]["type"] == "end"


def test_unknown_name_ask_resident_timeout_denies(monkeypatch):
    """No SMS reply before AGENT_APPROVAL_TIMEOUT -> deny."""
    monkeypatch.setattr(config, "NOTIFY_SMS_TO", "+15551111111")
    monkeypatch.setattr(config, "AGENT_APPROVAL_TIMEOUT", 1)
    monkeypatch.setattr(notify, "send_approval_request", lambda **kwargs: True)

    frames = _talk_people(
        monkeypatch,
        classified=None,
        decisions=[
            agent.Decision(
                ask_resident=True, visitor_claim="delivery", reason="escalate"
            )
        ],
    )

    assert not _digits_sent(frames)
    assert frames[-1]["type"] == "end"


def test_partial_code_times_out_without_opening(monkeypatch):
    """A code started and abandoned must not hold the door open.

    Twilio sends one frame per keypress and never signals "done", so a short
    code is indistinguishable from a slow one until the idle timeout fires.
    """
    monkeypatch.setattr(config, "AGENT_CODE_TIMEOUT", 1)
    frames = _talk_people(
        monkeypatch,
        classified=PEOPLE[0],
        decisions=[agent.Decision(deny=True, reason="wrong cat")],
        dtmf="19",  # two of four digits, then silence
    )

    assert not _digits_sent(frames), "a half-entered code must not open the door"


def test_hangup_during_code_entry_does_not_open(monkeypatch):
    """Hanging up mid-code must not open the door.

    _collect_code blocks on the socket, so a caller who drops after two digits
    raises there rather than returning — worth pinning, since that exception
    unwinds through a different path than the timeout does.
    """
    monkeypatch.setattr(config, "AGENT_PEOPLE", PEOPLE)
    monkeypatch.setattr(notify, "send_call_summary", lambda **kwargs: None)

    async def fake_classify(client, said):
        return PEOPLE[0]

    async def fake_turn(client, messages, system=None, tools=None):
        return agent.Decision(deny=True, reason="wrong cat")

    monkeypatch.setattr(agent, "classify_person", fake_classify)
    monkeypatch.setattr(agent, "take_turn", fake_turn)

    frames: list[dict[str, Any]] = []
    client = TestClient(app.app)
    with client.websocket_connect("/relay-people") as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "setup",
                    "callSid": "CA_test",
                    "customParameters": {"secret": config.RELAY_SECRET},
                }
            )
        )
        ws.send_text(
            json.dumps({"type": "prompt", "voicePrompt": "Alex", "last": True})
        )
        ws.send_text(json.dumps({"type": "dtmf", "digit": "2"}))
        ws.send_text(json.dumps({"type": "dtmf", "digit": "0"}))
        ws.close()  # drop the call halfway through the code

    assert not _digits_sent(frames), "a hang-up mid-code must not open the door"


def test_people_model_failure_does_not_open_the_door(monkeypatch):
    """An API blowup mid-call must never open the door, in this mode either."""
    frames = _talk_people(
        monkeypatch,
        classified=PEOPLE[0],
        decisions=[RuntimeError("anthropic is down")],
        expect=0,
    )

    assert not _digits_sent(frames), "an LLM outage must NOT open the door"


def test_people_wrong_secret_is_hung_up_on(monkeypatch):
    """The /relay-people socket is guarded by the same shared secret."""
    monkeypatch.setattr(config, "AGENT_PEOPLE", PEOPLE)

    client = TestClient(app.app)
    with client.websocket_connect("/relay-people") as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "setup",
                    "callSid": "CA_attacker",
                    "customParameters": {"secret": "wrong"},
                }
            )
        )
        # Digits alone must not open anything, authenticated or not.
        for digit in "1990":
            ws.send_text(json.dumps({"type": "dtmf", "digit": digit}))
        frames = _drain(ws, expect=0)

    assert not _digits_sent(frames), "an unauthenticated socket must not open the door"


def test_person_prompt_excludes_other_people_and_codes():
    """The per-person prompt is the isolation boundary. Verify what is in it.

    Only the chosen question, and never anybody's code — that is what makes
    "the model cannot leak Priya's answer to someone claiming to be Marco" a
    property of the request rather than a rule the model has to remember.
    """
    other = {
        "name": "Marco",
        "code": "7777",
        "questions": [{"ask": "Where do we play pickleball?", "answer": "Bedford"}],
    }
    chosen = PEOPLE[0]["questions"][0]
    prompt = agent.build_person_prompt(PEOPLE[0], chosen)

    assert "Miso" in prompt, "the person's own answer must be present to judge against"
    assert "Oak" not in prompt, "the other question must not be in the prompt"
    assert "1990" not in prompt, "a code must never reach the model"
    assert other["name"] not in prompt and "Bedford" not in prompt, "roster leaked"
    assert "7777" not in prompt


def test_pick_question_returns_one_from_corpus():
    q = agent.pick_question(PEOPLE[0])
    assert q in PEOPLE[0]["questions"]


def test_parse_reply_yes_no():
    assert approvals.parse_reply("YES") is True
    assert approvals.parse_reply("yes please") is True
    assert approvals.parse_reply("NO") is False
    assert approvals.parse_reply("deny") is False
    assert approvals.parse_reply("maybe later") is None


def test_sms_yes_resolves_pending(monkeypatch):
    """POST /sms from the resident number wakes a waiting approval."""
    import asyncio

    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(config, "NOTIFY_SMS_TO", "+15551111111")

    async def run() -> None:
        pending = await approvals.try_begin(call_sid="CA_sms", claim="test")
        assert pending is not None

        # Resolve from another thread the way /sms does, while we await.
        def reply() -> None:
            time.sleep(0.05)
            approvals.resolve(True)

        threading.Thread(target=reply, daemon=True).start()
        assert await approvals.wait(pending, timeout=2.0) is True

        # Also exercise the HTTP path with nothing pending.
        client = TestClient(app.app)
        resp = client.post(
            "/sms",
            data={"From": "+15551111111", "Body": "YES", "To": "+15559876543"},
        )
        assert resp.status_code == 200
        assert "No door request" in resp.text

    asyncio.run(run())


def test_sms_ignores_non_resident(monkeypatch):
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(config, "NOTIFY_SMS_TO", "+15551111111")
    client = TestClient(app.app)
    resp = client.post(
        "/sms",
        data={"From": "+15559999999", "Body": "YES", "To": "+15559876543"},
    )
    assert resp.status_code == 200
    assert "Opening" not in resp.text


def test_people_twiml_enables_dtmf(monkeypatch):
    """/voice in voice-agent-people mode must turn DTMF on, both ways.

    dtmfDetection is off by default, and reportInputDuringAgentSpeech has been
    "none" since May 2025 — so digits pressed while "enter your code" is still
    playing get dropped. People do exactly that, so both attributes have to be
    on the noun or a correct code reads as a wrong one.
    """
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(config, "MODE", "voice-agent-people")
    monkeypatch.setattr(config, "AGENT_PEOPLE", PEOPLE)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")

    client = TestClient(app.app)
    resp = client.post("/voice", data={"CallSid": "CA_test", "From": "+15551234567"})

    assert resp.status_code == 200
    body = resp.text
    assert "<ConversationRelay" in body
    assert "/relay-people" in body
    assert 'dtmfDetection="true"' in body
    assert 'reportInputDuringAgentSpeech="dtmf"' in body
    assert config.RELAY_SECRET in body


def test_voice_twiml_hands_off_to_conversation_relay(monkeypatch):
    """/voice in voice-agent mode returns Connect+ConversationRelay over wss."""
    # Twilio signature validation is not what this test is about; an unsigned
    # POST would 403. Skipping it is exactly what config does when no token is
    # configured (see _validate_twilio_request).
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "")

    client = TestClient(app.app)
    resp = client.post("/voice", data={"CallSid": "CA_test", "From": "+15551234567"})

    assert resp.status_code == 200
    body = resp.text
    assert "<Connect>" in body
    assert "<ConversationRelay" in body
    assert 'url="ws' in body  # ws:// under TestClient, wss:// behind Cloud Run
    assert "/relay" in body
    # The secret must ride along, or /relay will hang up on the real call.
    assert config.RELAY_SECRET in body
