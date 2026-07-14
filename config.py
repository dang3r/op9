import os
import secrets

from dotenv import load_dotenv

load_dotenv()

# Call-handling mode: "auto" opens for every caller; "passcode" prompts for a
# DTMF passcode and only opens on a match; "voice-agent" hands the call to an
# LLM that talks to the visitor and decides whether to open.
ALLOWED_MODES: list[str] = ["auto", "passcode", "voice-agent"]

MODE: str = os.getenv("MODE", "").strip().lower()
if MODE not in ALLOWED_MODES:
    raise ValueError(f"MODE must be one of: {', '.join(ALLOWED_MODES)}")

OPEN_DIGITS: str = os.getenv("OPEN_DIGITS", "ww9")
RECORD_CALLS: bool = os.getenv("RECORD_CALLS", "true").lower() == "true"
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")

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
if MODE == "voice-agent" and not ANTHROPIC_API_KEY:
    raise ValueError("ANTHROPIC_API_KEY is required when MODE=voice-agent")

# Haiku is the cheapest and fastest tier. On a phone call latency is the
# binding constraint, and screening a visitor is a short, well-scoped task.
AGENT_MODEL: str = os.getenv("AGENT_MODEL", "claude-haiku-4-5")

# Freeform facts about the resident, injected into the system prompt: who lives
# here, expected deliveries, who is always allowed in. Changeable without a
# redeploy — it is read at import, so a Cloud Run env-var update is enough.
AGENT_CONTEXT: str = os.getenv("AGENT_CONTEXT", "")

# Spoken by Twilio before the model is consulted, so the visitor hears
# something immediately rather than waiting on the first API round trip.
AGENT_GREETING: str = os.getenv("AGENT_GREETING", "Hello, who's there?")

# Hard cap on visitor turns. Hitting it denies entry: a caller who cannot
# explain themselves in this many turns does not get in by attrition.
AGENT_MAX_TURNS: int = int(os.getenv("AGENT_MAX_TURNS", "6"))

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
