from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Start, VoiceResponse

import config

app = FastAPI(title="op9", description="building entry operator")


def _external_url(request: Request) -> str:
    """Reconstruct the public URL Twilio signed behind a reverse proxy."""
    scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = (
        request.headers.get("X-Forwarded-Host")
        or request.headers.get("Host")
        or request.url.netloc
    )
    path = request.url.path
    if request.url.query:
        return f"{scheme}://{host}{path}?{request.url.query}"
    return f"{scheme}://{host}{path}"


def _external_base_url(request: Request) -> str:
    """Public base URL with trailing slash."""
    scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = (
        request.headers.get("X-Forwarded-Host")
        or request.headers.get("Host")
        or request.url.netloc
    )
    return f"{scheme}://{host}/"


def _validate_twilio_request(request: Request, params: dict[str, Any]) -> None:
    """Reject requests that fail Twilio signature validation."""
    if not config.TWILIO_AUTH_TOKEN:
        return

    signature = request.headers.get("X-Twilio-Signature", "")
    validator = RequestValidator(config.TWILIO_AUTH_TOKEN)
    if not validator.validate(_external_url(request), params, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")


def build_auto_entry_twiml(recording_callback_url: str) -> str:
    """Build TwiML for automatic entry mode."""
    response = VoiceResponse()

    if config.RECORD_CALLS:
        start = Start()
        start.recording(recording_status_callback=recording_callback_url)
        response.append(start)

    response.say("Opening.")
    response.play(digits=config.OPEN_DIGITS)
    response.hangup()

    return str(response)


@app.post("/voice")
async def voice(request: Request) -> Response:
    """Twilio voice webhook: accept the call and open the door."""
    form = await request.form()
    params = dict(form)
    _validate_twilio_request(request, params)

    recording_callback_url = f"{_external_base_url(request)}recording-status"
    twiml = build_auto_entry_twiml(recording_callback_url)

    return Response(content=twiml, media_type="application/xml")


@app.post("/recording-status")
async def recording_status(request: Request) -> Response:
    """Twilio recording callback: log metadata to stdout."""
    form = await request.form()
    params = dict(form)
    _validate_twilio_request(request, params)

    print(
        "Recording callback:",
        f"CallSid={params.get('CallSid')}",
        f"RecordingSid={params.get('RecordingSid')}",
        f"RecordingUrl={params.get('RecordingUrl')}",
        f"RecordingDuration={params.get('RecordingDuration')}",
        f"RecordingStatus={params.get('RecordingStatus')}",
    )

    return Response(status_code=200)
