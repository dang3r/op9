import json
import os
import secrets

from dotenv import load_dotenv

load_dotenv()

# Call-handling mode: "auto" opens for every caller; "passcode" prompts for a
# DTMF passcode and only opens on a match; "voice-agent" hands the call to an
# LLM that talks to the visitor and decides whether to open;
# "voice-agent-people" is voice-agent plus a per-person challenge and a
# per-person DTMF code to fall back on.
ALLOWED_MODES: list[str] = ["auto", "passcode", "voice-agent", "voice-agent-people"]

MODE: str = os.getenv("MODE", "").strip().lower()
if MODE not in ALLOWED_MODES:
    raise ValueError(f"MODE must be one of: {', '.join(ALLOWED_MODES)}")

OPEN_DIGITS: str = os.getenv("OPEN_DIGITS", "ww9")

# Seconds to hold the ConversationRelay socket open after sending the door
# tones. Twilio plays sendDigits asynchronously, so returning immediately tears
# the call down mid-tone and the door never trips — each "w" in OPEN_DIGITS is a
# 0.5s pause, so "ww9" needs a second before the 9 even starts. Only the
# WebSocket modes need this; auto/passcode play the tones from TwiML, which
# Twilio sequences itself.
OPEN_DIGITS_SETTLE: float = float(os.getenv("OPEN_DIGITS_SETTLE", "3"))
RECORD_CALLS: bool = os.getenv("RECORD_CALLS", "true").lower() == "true"
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")

# Post-call SMS summary. After every completed call, notify.send_call_summary
# texts a summary to NOTIFY_SMS_TO. The Twilio REST client authenticates with
# the account SID + the auth token above; the SMS is sent *from* the service's
# own Twilio number, read off each webhook, so there is no from-number here.
# Notifications are enabled only when the SID, the auth token, and the
# destination are all set — otherwise the helper no-ops, which is what keeps
# local dev and any unconfigured deploy working unchanged.
TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
NOTIFY_SMS_TO: str = os.getenv("NOTIFY_SMS_TO", "")
# IANA zone for the timestamp in the SMS. zoneinfo renders EST/EDT correctly by
# date, so the "EST?" in the ask is just this default.
NOTIFY_TIMEZONE: str = os.getenv("NOTIFY_TIMEZONE", "America/New_York")

# Passcode mode: the single valid passcode. numDigits is derived from its
# length, so there is nothing extra to keep in sync. Required only in passcode
# mode — the other modes never read it.
PASSCODE: str = os.getenv("PASSCODE", "")
PASSCODE_TIMEOUT: int = int(os.getenv("PASSCODE_TIMEOUT", "10"))
if MODE == "passcode" and not PASSCODE:
    raise ValueError("PASSCODE is required when MODE=passcode")

# Logs the raw DTMF digits Twilio decoded, to diagnose a panel that garbles or
# drops tones. This writes the passcode to the logs in plaintext: enable it for
# a test window only, and rotate PASSCODE afterwards.
DEBUG_DTMF: bool = os.getenv("DEBUG_DTMF", "true").strip().lower() == "true"

# Voice-agent mode: Twilio ConversationRelay does the speech-to-text and
# text-to-speech and bridges the call to our /relay WebSocket; we run the LLM
# loop. The model gets exactly two tools, open_door and deny_entry, and the
# door opens only if it calls open_door — every other outcome keeps it shut.
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

# Both agent modes run the same LLM loop, so both need the key.
AGENT_MODES: list[str] = ["voice-agent", "voice-agent-people"]
if MODE in AGENT_MODES and not ANTHROPIC_API_KEY:
    raise ValueError(f"ANTHROPIC_API_KEY is required when MODE={MODE}")

# Haiku is the cheapest and fastest tier. On a phone call latency is the
# binding constraint, and screening a visitor is a short, well-scoped task.
AGENT_MODEL: str = os.getenv("AGENT_MODEL", "claude-haiku-4-5")

# Freeform facts about the resident, injected into the system prompt: who lives
# here, expected deliveries, who is always allowed in. Changeable without a
# redeploy — it is read at import, so a Cloud Run env-var update is enough.
AGENT_CONTEXT: str = os.getenv("AGENT_CONTEXT", "")

# Spoken by Twilio before the model is consulted, so the visitor hears
# something immediately rather than waiting on the first API round trip. In
# voice-agent-people mode it must ask for a name: the visitor's first utterance
# is what gets matched against the roster.
AGENT_GREETING: str = os.getenv("AGENT_GREETING", "Hello, who's there?")

# Hard cap on visitor turns. Hitting it denies entry: a caller who cannot
# explain themselves in this many turns does not get in by attrition.
AGENT_MAX_TURNS: int = int(os.getenv("AGENT_MAX_TURNS", "6"))

# --- voice-agent-people mode -------------------------------------------------
# The roster: people we know by name, each with their own private questions and
# their own DTMF code. A visitor states a name, the model matches it against
# this list, and the challenge is drawn from that person's questions alone.
#
# JSON rather than the freeform prose of AGENT_CONTEXT, because unlike the facts
# this is read by *code*: the loop looks a person up, isolates their questions,
# and compares their code. A person's answers and code never enter a prompt.
#
#   [
#     {
#       "name": "Alex",
#       "aliases": ["Al"],
#       "relation": "brother",
#       "code": "0000",
#       "questions": [
#         {"ask": "What was the name of the cat we grew up with?",
#          "answer": "Miso"}
#       ]
#     }
#   ]
#
# `aliases` and `relation` are optional. Everything else is required, and a
# malformed roster raises at import rather than degrading to a gate that cannot
# challenge anyone.
AGENT_PEOPLE_RAW: str = os.getenv("AGENT_PEOPLE", "")


def _parse_people(raw: str) -> list[dict]:
    """Parse and validate the AGENT_PEOPLE roster.

    Raises on anything malformed. This runs at import, so a bad roster stops the
    process from starting rather than surfacing mid-call, when the only safe
    thing left to do would be to deny everyone.
    """
    if not raw.strip():
        return []

    try:
        people = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"AGENT_PEOPLE is not valid JSON: {exc}") from exc

    if not isinstance(people, list):
        raise ValueError("AGENT_PEOPLE must be a JSON list of people")

    for i, person in enumerate(people):
        where = f"AGENT_PEOPLE[{i}]"
        if not isinstance(person, dict):
            raise ValueError(f"{where} must be an object")

        name = str(person.get("name", "")).strip()
        if not name:
            raise ValueError(f"{where} is missing a name")

        code = str(person.get("code", "")).strip()
        if not code.isdigit():
            raise ValueError(f"{where} ({name}) needs an all-digit code")

        questions = person.get("questions")
        if not isinstance(questions, list) or not questions:
            raise ValueError(f"{where} ({name}) needs at least one question")
        for q in questions:
            if not isinstance(q, dict) or not str(q.get("ask", "")).strip():
                raise ValueError(f"{where} ({name}) has a question with no 'ask'")
            if not str(q.get("answer", "")).strip():
                raise ValueError(f"{where} ({name}) has a question with no 'answer'")

    return people


AGENT_PEOPLE: list[dict] = _parse_people(AGENT_PEOPLE_RAW)

if MODE == "voice-agent-people" and not AGENT_PEOPLE:
    raise ValueError("AGENT_PEOPLE is required when MODE=voice-agent-people")

# Attempts at the fallback code, per call. One: the code is the second factor
# behind a name we already matched, and unlimited tries would turn four digits
# into a keypad brute-force that a caller can run for as long as we stay on the
# line.
AGENT_CODE_ATTEMPTS: int = int(os.getenv("AGENT_CODE_ATTEMPTS", "1"))

# Seconds to wait for the next digit before giving up on a half-entered code.
# Twilio sends one frame per keypress with no notion of "done", so the buffer
# needs its own idle timeout. Shares PASSCODE_TIMEOUT's default for consistency
# with the DTMF handling in passcode mode.
AGENT_CODE_TIMEOUT: int = int(os.getenv("AGENT_CODE_TIMEOUT", str(PASSCODE_TIMEOUT)))

# Seconds to wait for a YES/NO SMS when an unknown visitor buzzes.
# The call stays open for this long; past it we deny. Keep it short enough that
# a stranger is not left on the intercom forever, long enough to dig a phone out.
AGENT_APPROVAL_TIMEOUT: int = int(os.getenv("AGENT_APPROVAL_TIMEOUT", "60"))

# Unset means Twilio's default voice. Set AGENT_TTS_PROVIDER=ElevenLabs plus an
# AGENT_VOICE id to switch — no code change, the agent loop is unaffected.
AGENT_TTS_PROVIDER: str = os.getenv("AGENT_TTS_PROVIDER", "")
AGENT_VOICE: str = os.getenv("AGENT_VOICE", "")

# Twilio signs the HTTP webhooks, but not the WebSocket upgrade, so signature
# validation cannot guard /relay. Instead we mint a secret, hand it to Twilio as
# a <Parameter> on the ConversationRelay TwiML, and require it back in the setup
# frame. Generated per-process when unset: that is sufficient because the TwiML
# and the WebSocket are served by the same process, and it means there is no
# default value an attacker could guess. Set it explicitly to keep it stable
# across multiple Cloud Run instances.
RELAY_SECRET: str = os.getenv("RELAY_SECRET", "") or secrets.token_urlsafe(32)
