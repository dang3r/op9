"""Real telephone test. Never places a call without RUN_PHONE_E2E=1."""

import json
import os
import re
import subprocess
import time
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dotenv import load_dotenv
from requests.exceptions import RequestException
from twilio.base.exceptions import TwilioException
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

from tests.phone_gather import caller_instructions, fetch_receipt

TERMINAL = {"completed", "busy", "failed", "no-answer", "canceled"}


@pytest.mark.phone_e2e
@pytest.mark.skipif(os.getenv("RUN_PHONE_E2E") != "1", reason="Live phone test is opt-in")
def test_correct_passcode_returns_unlock_tone():
    load_dotenv()
    names = ("TWILIO_ACCOUNT_SID", "E2E_CALLER_NUMBER", "E2E_SERVICE_NUMBER", "E2E_PASSCODE", "E2E_CALLBACK_URL")
    missing = [name for name in names if not os.getenv(name)]
    assert not missing, f"Missing settings: {', '.join(missing)}"
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    key, secret = os.getenv("TWILIO_API_KEY"), os.getenv("TWILIO_API_SECRET")
    if key or secret:
        assert key and secret, "Set both TWILIO_API_KEY and TWILIO_API_SECRET"
        username, password = key, secret
    else:
        username, password = sid, os.getenv("TWILIO_AUTH_TOKEN")
        assert password, "Set TWILIO_AUTH_TOKEN or an API key/secret pair"

    caller, service = os.environ["E2E_CALLER_NUMBER"], os.environ["E2E_SERVICE_NUMBER"]
    assert all(re.fullmatch(r"\+[1-9][0-9]{7,14}", n) for n in (caller, service)), "Use E.164 numbers"
    assert caller != service, "Caller and service numbers must differ"
    code = os.environ["E2E_PASSCODE"]
    assert code.isascii() and code.isdigit(), "Passcode must contain ASCII digits only"

    directory = Path(os.getenv("E2E_ARTIFACT_DIR", "artifacts/phone-e2e")) / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    )
    directory.mkdir(parents=True)
    print(f"\nPhone E2E artifacts: {directory.resolve()}")
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "result": "failed"}
    client = Client(username, password, sid, http_client=TwilioHttpClient(timeout=10, max_retries=0))
    instructions = caller_instructions(code, os.environ["E2E_CALLBACK_URL"])
    project = os.getenv("E2E_GCP_PROJECT", "operator9")
    log_service = os.getenv("E2E_CLOUD_RUN_SERVICE", "op9")
    call = None
    started = time.monotonic()
    try:
        # Check log access before placing a billable call.
        fetch_receipt("CA" + "0" * 32, report["started_at"], project, log_service)
        call = client.calls.create(
            from_=caller,
            to=service,
            twiml=instructions,
            timeout=30,
        )
        report["call_sid"] = call.sid
        print(f"Caller CallSid: {call.sid}")
        deadline = time.monotonic() + 90
        digits = None
        while time.monotonic() < deadline:
            digits = fetch_receipt(call.sid, report["started_at"], project, log_service)
            if digits is not None:
                break
            current = client.calls(call.sid).fetch()
            report["call_status"] = current.status
            assert current.status not in {"busy", "failed", "no-answer", "canceled"}, (
                f"Call ended with status {current.status}"
            )
            # A completed call can have a receipt still being ingested by Logging.
            time.sleep(2)
        report["received_digits"] = digits
        assert digits is not None, "No signed Gather receipt in Cloud Logging within 90 seconds"
        assert digits == "9", f"Expected unlock digit 9; Gather received {digits!r}"
        report["result"] = "passed"
        print(f"Gather received 9; receipt observed after {time.monotonic() - started:.2f}s")
    except (RequestException, TwilioException, subprocess.SubprocessError, FileNotFoundError) as exc:
        # Network tracebacks can include authenticated request headers and the
        # passcode-bearing TwiML. Report the error class without those objects.
        report["error_type"] = type(exc).__name__
        pytest.fail(
            f"Phone test API request failed ({type(exc).__name__}); see {directory}",
            pytrace=False,
        )
    finally:
        # Do not leave a caller running when an assertion or log lookup fails.
        if call is not None:
            try:
                if client.calls(call.sid).fetch().status not in TERMINAL:
                    client.calls(call.sid).update(status="completed")
            except Exception:
                warnings.warn(f"Could not confirm cleanup of call {call.sid}; check Twilio", stacklevel=1)
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (directory / "result.json").write_text(json.dumps(report, indent=2) + "\n")
