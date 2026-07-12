import os

from dotenv import load_dotenv

load_dotenv()

# Call-handling mode: "auto" opens for every caller; "passcode" prompts for a
# DTMF passcode and only opens on a match.
ALLOWED_MODES: list[str] = ["auto", "passcode"]

MODE: str = os.getenv("MODE", "").strip().lower()
if MODE not in ALLOWED_MODES:
    raise ValueError(f"MODE must be one of: {', '.join(ALLOWED_MODES)}")

OPEN_DIGITS: str = os.getenv("OPEN_DIGITS", "ww9")
RECORD_CALLS: bool = os.getenv("RECORD_CALLS", "true").lower() == "true"
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")

# Passcode mode: the single valid passcode. numDigits is derived from its
# length, so there is nothing extra to keep in sync.
# make passcode mandatory
PASSCODE: str = os.getenv("PASSCODE", "")
PASSCODE_TIMEOUT: int = int(os.getenv("PASSCODE_TIMEOUT", "10"))
if not PASSCODE:
    raise ValueError("PASSCODE is required")

# Logs the raw DTMF digits Twilio decoded, to diagnose a panel that garbles or
# drops tones. This writes the passcode to the logs in plaintext: enable it for
# a test window only, and rotate PASSCODE afterwards.
DEBUG_DTMF: bool = os.getenv("DEBUG_DTMF", "true").strip().lower() == "true"
