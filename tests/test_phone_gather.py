import logging
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

import app
import config
from tests.phone_gather import caller_instructions, receipt_from_logs

CALL = "CA" + "a" * 32
URL = "https://testserver/phone-test/dtmf"


@pytest.mark.parametrize("digit", ["9", "7", ""])
def test_signed_gather_callback_records_actual_digit(monkeypatch, caplog, digit):
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "test-token")
    params = {"CallSid": CALL, "Digits": digit}
    signature = RequestValidator("test-token").compute_signature(URL, params)
    with caplog.at_level(logging.INFO, logger="op9"):
        response = TestClient(app.app, base_url="https://testserver").post(
            "/phone-test/dtmf", data=params, headers={"X-Twilio-Signature": signature},
        )
    assert response.status_code == 200
    assert "<Hangup" in response.text
    assert receipt_from_logs([{"textPayload": r.message} for r in caplog.records], CALL) == digit


@pytest.mark.parametrize("token,expected", [("test-token", 403), ("", 503)])
def test_unsigned_callback_cannot_fabricate_success(monkeypatch, caplog, token, expected):
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", token)
    response = TestClient(app.app).post("/phone-test/dtmf", data={"CallSid": CALL, "Digits": "9"})
    assert response.status_code == expected
    assert "phone-e2e:" not in caplog.text


def test_receipt_must_belong_to_exact_call():
    entries = [{"textPayload": f"INFO op9 phone-e2e: CallSid={CALL} Digits=9"}]
    assert receipt_from_logs(entries, "CA" + "b" * 32) is None
    assert receipt_from_logs(entries, CALL) == "9"


def test_code_is_sent_before_listening_and_never_inside_gather():
    root = ElementTree.fromstring(caller_instructions("1234", URL))
    assert [child.tag for child in root] == ["Pause", "Play", "Gather", "Hangup"]
    assert root.find("Play").attrib["digits"] == "1234"
    gather = root.find("Gather")
    assert gather.find("Play") is None
    assert gather.attrib["numDigits"] == "1"
    assert gather.attrib["action"] == URL
    assert gather.attrib["actionOnEmptyResult"] == "true"


def test_callback_rejects_multiple_digits_and_log_injection(monkeypatch):
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "test-token")
    params = {"CallSid": CALL, "Digits": "9\n"}
    signature = RequestValidator("test-token").compute_signature(URL, params)
    response = TestClient(app.app, base_url="https://testserver").post(
        "/phone-test/dtmf", data=params, headers={"X-Twilio-Signature": signature},
    )
    assert response.status_code == 400
