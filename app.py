import hmac
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Gather, Start, VoiceResponse

import config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("op9")

app = FastAPI(title="op9", description="building entry operator")


def _external_url(request: Request) -> str:
    """Reconstruct the public URL Twilio signed behind a reverse proxy."""
    scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or request.url.netloc
    path = request.url.path
    if request.url.query:
        return f"{scheme}://{host}{path}?{request.url.query}"
    return f"{scheme}://{host}{path}"


def _external_base_url(request: Request) -> str:
    """Public base URL with trailing slash."""
    scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or request.url.netloc
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
    _open_door(response)
    response.hangup()

    return str(response)


def build_passcode_twiml(passcode_action_url: str) -> str:
    """Build TwiML for passcode mode: prompt, then gather the DTMF passcode.

    The visitor has already dialed the door code at the panel to reach us, so we
    prompt for the passcode immediately. numDigits is derived from the configured
    passcode length; Twilio submits automatically on the last key. Not recorded,
    so the passcode tones never land in a recording.
    """
    response = VoiceResponse()

    if not config.PASSCODE:
        # Misconfigured — refuse rather than silently opening.
        log.error("passcode mode active but PASSCODE is unset, refusing")
        response.say("System not configured.")
        response.hangup()
        return str(response)

    gather = Gather(
        input="dtmf",
        num_digits=len(config.PASSCODE),
        action=passcode_action_url,
        method="POST",
        timeout=config.PASSCODE_TIMEOUT,
    )
    gather.say("Enter the passcode.")
    response.append(gather)

    # Reached only if the visitor entered nothing before the timeout. Twilio runs
    # this verb itself rather than calling back, so a timed-out call leaves no
    # /passcode log line: a "voice:" line with no matching "passcode:" line for
    # the same CallSid means the panel sent no tones Twilio could decode.
    response.say("No passcode entered. Goodbye.")
    response.hangup()

    return str(response)


def _open_door(response: VoiceResponse) -> None:
    """Play the DTMF tones that trip the intercom's door relay."""
    response.play(digits=config.OPEN_DIGITS)


@app.post("/voice")
async def voice(request: Request) -> Response:
    """Twilio voice webhook: dispatch to the configured call-handling mode."""
    form = await request.form()
    params = dict(form)
    _validate_twilio_request(request, params)

    log.info(
        "voice: CallSid=%s From=%s mode=%s",
        params.get("CallSid"),
        params.get("From"),
        config.MODE,
    )

    base = _external_base_url(request)
    if config.MODE == "passcode":
        twiml = build_passcode_twiml(f"{base}passcode")
    elif config.MODE == "auto":
        twiml = build_auto_entry_twiml(f"{base}recording-status")
    else:
        log.error("voice: unrecognized mode=%r, refusing", config.MODE)
        response = VoiceResponse()
        response.say("Invalid mode. Goodbye.")
        response.hangup()
        twiml = str(response)

    return Response(content=twiml, media_type="application/xml")


@app.post("/passcode")
async def passcode(request: Request) -> Response:
    """Twilio gather callback: verify the DTMF passcode and open on a match."""
    form = await request.form()
    params = dict(form)
    _validate_twilio_request(request, params)

    digits = str(params.get("Digits", ""))
    response = VoiceResponse()
    matched = bool(config.PASSCODE) and hmac.compare_digest(digits, config.PASSCODE)

    # Length and match are enough to tell "panel sent nothing" (0 digits) from
    # "panel dropped tones" (short) from "wrong or transposed code" (full length,
    # no match). The digits themselves are the passcode, so they are logged only
    # under DEBUG_DTMF.
    log.info(
        "passcode: CallSid=%s digits_len=%d expected_len=%d matched=%s",
        params.get("CallSid"),
        len(digits),
        len(config.PASSCODE),
        matched,
    )
    if config.DEBUG_DTMF:
        log.warning("passcode: DEBUG_DTMF on, raw digits=%r", digits)

    if matched:
        response.say("Opening.")
        _open_door(response)
    else:
        response.say("Incorrect passcode. Goodbye.")

    response.hangup()
    return Response(content=str(response), media_type="application/xml")


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
