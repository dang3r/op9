"""Caller instructions and Cloud Logging receipts for the live Gather test."""

import json
import re
import subprocess
from urllib.parse import urlparse

from twilio.twiml.voice_response import VoiceResponse


def caller_instructions(code: str, callback_url: str) -> str:
    parsed = urlparse(callback_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("E2E_CALLBACK_URL must be an HTTPS URL without a query or fragment")
    instructions = VoiceResponse()
    instructions.pause(length=3)
    # Twilio forbids Play digits inside Gather. Send the code, then listen.
    instructions.play(digits=code)
    instructions.gather(
        input="dtmf", num_digits=1, timeout=20, finish_on_key="",
        action=callback_url, method="POST", action_on_empty_result=True,
    )
    instructions.hangup()
    return str(instructions)


def receipt_from_logs(entries: list[dict], call_sid: str) -> str | None:
    pattern = re.compile(r"phone-e2e: CallSid=" + re.escape(call_sid) + r" Digits=([0-9*#ABCD]|\(empty\))$")
    for entry in entries:
        match = pattern.search(entry.get("textPayload", ""))
        if match:
            return "" if match[1] == "(empty)" else match[1]
    return None


def fetch_receipt(call_sid: str, since: str, project: str, service: str) -> str | None:
    # JSON quoting keeps filter values literal; no shell is involved.
    filters = [
        'resource.type="cloud_run_revision"',
        "resource.labels.service_name=" + json.dumps(service),
        "timestamp>=" + json.dumps(since),
        "textPayload:" + json.dumps(f"phone-e2e: CallSid={call_sid} "),
    ]
    result = subprocess.run(
        ["gcloud", "logging", "read", " AND ".join(filters),
         "--project", project, "--limit", "20", "--format=json"],
        check=True, capture_output=True, text=True, timeout=20,
    )
    return receipt_from_logs(json.loads(result.stdout), call_sid)
