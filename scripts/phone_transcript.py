"""Best-effort CI diagnostics for the caller's received audio."""
import html
import json
import os
import re
import sys
import time
from pathlib import Path


def fetch_audio(directory, report):
    import requests

    account = os.environ["TWILIO_ACCOUNT_SID"]
    auth = (os.environ["TWILIO_API_KEY"], os.environ["TWILIO_API_SECRET"])
    call = report.get("call_sid", "")
    if not re.fullmatch(r"CA[0-9a-fA-F]{32}", call):
        raise ValueError("no caller ID")
    base = f"https://api.twilio.com/2010-04-01/Accounts/{account}"
    for _ in range(20):
        response = requests.get(f"{base}/Calls/{call}/Recordings.json", auth=auth, timeout=10)
        response.raise_for_status()
        for recording in response.json()["recordings"]:
            if recording["call_sid"] == call and recording["status"] == "completed":
                response = requests.get(f"{base}/Recordings/{recording['sid']}.wav", auth=auth, timeout=15)
                response.raise_for_status()
                (directory / "incoming.wav").write_bytes(response.content)
                return
        time.sleep(2)
    raise TimeoutError("recording unavailable")


def transcribe(directory):
    from faster_whisper import WhisperModel

    model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(directory / "incoming.wav"), language="en",
                                   condition_on_previous_text=False, vad_filter=True)
    (directory / "transcript.txt").write_text(" ".join(s.text.strip() for s in segments))


def summary(directory, report):
    path = directory / "transcript.txt"
    speech = path.read_text().strip() if path.exists() else "Transcript unavailable."
    # The staging branch controls what is spoken. Render it as literal text.
    code = os.getenv("E2E_PASSCODE")
    if code:
        speech = speech.replace(code, "[redacted]")
    if path.exists():
        (directory / "call-transcript.txt").write_text((speech or "No speech detected.") + "\n")
    digit = report.get("received_digits")
    observed = digit if digit in list("0123456789*#ABCD") else "none"
    return (f"### Staging phone call\n\nCommit `{os.environ['TEST_SHA']}` · "
            f"Test **{report.get('result', 'incomplete')}** · Received DTMF **{observed}**\n\n"
            f"<pre>{html.escape(speech[:3000] or 'No speech detected.')}</pre>\n")


if __name__ == "__main__":
    directory = Path(os.environ["E2E_ARTIFACT_DIR"])
    reports = list(directory.glob("*/result.json"))
    report = json.loads(reports[0].read_text()) if len(reports) == 1 else {}
    action = sys.argv[1]
    if action == "summary":
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as out:
            out.write(summary(directory, report))
    elif action == "fetch":
        fetch_audio(directory, report)
    elif action == "transcribe":
        transcribe(directory)
