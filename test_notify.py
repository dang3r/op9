"""Notification submission must finish before the passcode response returns."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

import app
import config
import notify


@pytest.mark.parametrize("digits,approved", [("1234", True), ("9999", False)])
def test_passcode_waits_for_notification(monkeypatch, digits, approved):
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(config, "PASSCODE", "1234")
    monkeypatch.setattr(config, "DEBUG_DTMF", False)
    entered = threading.Event()
    release = threading.Event()
    notifications = []

    def send_summary(**kwargs):
        notifications.append(kwargs)
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(notify, "send_call_summary", send_summary)

    async def form():
        return {"Digits": digits, "From": "+15550000001", "To": "+15550000002"}

    async def run():
        pending = asyncio.create_task(app.passcode(SimpleNamespace(form=form)))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert not pending.done(), "response must wait for notification submission"
        finally:
            release.set()
        response = await pending
        assert (b"<Play" in response.body) is approved
        assert notifications[0]["approved"] is approved

    asyncio.run(run())


def test_notification_timeout_keeps_opening_response(monkeypatch, caplog):
    from fastapi.testclient import TestClient
    from twilio.rest import Client

    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(config, "PASSCODE", "1234")
    monkeypatch.setattr(config, "DEBUG_DTMF", False)

    def fail_send(*args, **kwargs):
        assert kwargs["http_client"].timeout == 3
        raise TimeoutError("Twilio unavailable")

    def configured_send(**kwargs):
        # Enable outbound auth only after webhook validation has completed.
        monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", "test-account")
        monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "test-token")
        monkeypatch.setattr(config, "NOTIFY_SMS_TO", "+15550000003")
        original_send(**kwargs)

    original_send = notify.send_call_summary
    monkeypatch.setattr(Client, "__init__", fail_send)
    monkeypatch.setattr(notify, "send_call_summary", configured_send)
    with TestClient(app.app) as client:
        response = client.post("/passcode", data={"Digits": "1234", "To": "+15550000002"})
    assert response.status_code == 200
    assert "<Play" in response.text
    assert "failed to send" in caplog.text
