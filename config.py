import os

from dotenv import load_dotenv

load_dotenv()

OPEN_DIGITS: str = os.getenv("OPEN_DIGITS", "ww9")
RECORD_CALLS: bool = os.getenv("RECORD_CALLS", "true").lower() in {
    "1",
    "true",
    "yes",
}
TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
