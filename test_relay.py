"""Offline tests for voice-agent mode.

Drives the /relay WebSocket with the ConversationRelay protocol and asserts on
the frames the server sends back. The Anthropic call is stubbed, so these run
with no API key, no network, and no Twilio.

The assertion that matters is the last one: when the model errors, NO sendDigits
frame is ever sent. The door is opened by one line of code, and every other path
through the loop must leave it shut.

    uv run pytest test_relay.py -v
"""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

import agent
import app
import config


def _digits_sent(frames: list[dict[str, Any]]) -> bool:
    """Did the server ever tell Twilio to play the door-opening tones?"""
    return any(f.get("type") == "sendDigits" for f in frames)


def _drain(ws, expect: int | None = None) -> list[dict[str, Any]]:
    """Read the frames the server sends back.

    `expect` is how many frames to read before stopping; None means "read until
    the call ends". Both are needed because the relay legitimately has three
    exit shapes, and only one of them is chatty:

      - decision reached  -> text, maybe sendDigits, then `end`   (expect=None)
      - agent asked a question -> one `text`, call stays open     (expect=1)
      - hung up on        -> NO frames at all, socket just closes (expect=0)

    That last shape is why we cannot always block on `end`: a rejected socket and
    a crashed turn both correctly send nothing, and a blocking read would hang
    forever waiting for a frame that is never coming.
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
    """The model calls open_door -> we play the tones, then hang up."""
    frames = _talk(
        monkeypatch,
        agent.Decision(open=True, reason="expected plumber"),
        said="I'm the plumber, Daniel booked me for 2pm",
    )

    assert _digits_sent(frames), "expected the door to open"
    digits = next(f for f in frames if f["type"] == "sendDigits")
    assert digits["digits"] == config.OPEN_DIGITS
    assert frames[-1]["type"] == "end"


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
