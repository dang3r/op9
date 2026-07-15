"""Post-call SMS summaries.

After every completed call, app.py calls send_call_summary to text a summary to
NOTIFY_SMS_TO: who called, whether the door opened, when (in NOTIFY_TIMEZONE),
and — for voice-agent mode — the conversation transcript.

This module owns every SMS concern so app.py stays about call handling. Two
invariants hold no matter what:

  1. It is best-effort. The send is wrapped so a Twilio outage, a bad number, or
     any other failure is logged and swallowed — never raised. The door is
     decided in app.py by one line, and nothing here may perturb it.
  2. It no-ops unless configured. With the account SID, auth token, and
     destination all set it sends; otherwise it returns quietly, so local dev
     and unconfigured deploys behave exactly as before this feature existed.

The SMS is sent *from* the service's own Twilio number, which is passed in per
call (read off the Twilio webhook), not stored in config — the number that
receives the buzz is SMS-capable and doubles as the sender.
"""

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import config

log = logging.getLogger("op9.notify")

# A single SMS segment is 160 GSM-7 chars; a long transcript would otherwise
# fan out into many segments. Cap the body so one runaway call cannot. Chosen
# generously — a screening call is a handful of short turns — but bounded.
_MAX_BODY = 1400


def _configured() -> bool:
    """Do we have everything needed to send? If not, sending is a no-op."""
    return bool(
        config.TWILIO_ACCOUNT_SID
        and config.TWILIO_AUTH_TOKEN
        and config.NOTIFY_SMS_TO
    )


def _now_str() -> str:
    """Current time in the configured zone, e.g. '2026-07-14 09:32 PM EDT'."""
    return datetime.now(ZoneInfo(config.NOTIFY_TIMEZONE)).strftime(
        "%Y-%m-%d %I:%M %p %Z"
    )


def render_transcript(messages: list[dict[str, Any]]) -> str:
    """Turn the relay's message list into plain 'Operator:' / 'Visitor:' lines.

    `messages` is the exact list app.py appends to during a voice-agent call:
    user turns carry a plain string, assistant turns carry a list of content
    blocks (text and/or tool_use). We render the spoken text and skip tool_use
    — the decision the tool represents is already in the outcome/reason lines.
    """
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "user":
            text = content if isinstance(content, str) else _blocks_text(content)
            if text:
                lines.append(f"Visitor: {text}")
        elif role == "assistant":
            text = _blocks_text(content)
            if text:
                lines.append(f"Operator: {text}")

    return "\n".join(lines)


def _blocks_text(content: Any) -> str:
    """Join the text of Anthropic content blocks, ignoring tool_use blocks.

    Blocks arrive as SDK objects (attribute access) during a live call and can
    be plain dicts in other paths, so handle both. A plain string passes
    through unchanged.
    """
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        btype = getattr(block, "type", None)
        if btype is None and isinstance(block, dict):
            btype = block.get("type")
        if btype != "text":
            continue
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(str(text).strip())
    return " ".join(p for p in parts if p)


def build_body(
    *,
    mode: str,
    from_number: str | None,
    approved: bool | None,
    outcome: str,
    reason: str = "",
    transcript: str = "",
) -> str:
    """Compose the SMS body. Pure — no I/O — so it is easy to eyeball."""
    if approved is True:
        verdict = "APPROVED"
    elif approved is False:
        verdict = "DENIED"
    else:
        verdict = "NO DECISION"

    lines = [
        f"op9 · {mode}",
        f"From: {from_number or 'unknown'}",
        f"{verdict} — {outcome}",
        _now_str(),
    ]
    if reason:
        lines.append(f"Reason: {reason}")
    if transcript:
        lines.append("")
        lines.append(transcript)

    body = "\n".join(lines)
    if len(body) > _MAX_BODY:
        body = body[: _MAX_BODY - 1] + "…"
    return body


def send_call_summary(
    *,
    mode: str,
    from_number: str | None,
    to_number: str | None,
    approved: bool | None,
    outcome: str,
    reason: str = "",
    transcript: str = "",
) -> None:
    """Text a one-message summary of a completed call. Best-effort; never raises.

    `to_number` is the service's own Twilio number (the SMS sender), read off
    the Twilio webhook. Without it there is nobody to send from, so we no-op.
    """
    if not _configured():
        log.debug("notify: not configured, skipping SMS")
        return
    if not to_number:
        log.debug("notify: no service number to send from, skipping SMS")
        return

    try:
        # Imported lazily so an unconfigured deploy never pays for the client,
        # and so importing this module has no Twilio dependency at rest.
        from twilio.rest import Client

        body = build_body(
            mode=mode,
            from_number=from_number,
            approved=approved,
            outcome=outcome,
            reason=reason,
            transcript=transcript,
        )
        client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
        client.messages.create(
            to=config.NOTIFY_SMS_TO,
            from_=to_number,
            body=body,
        )
        log.info("notify: sent call summary (%s, %s)", mode, outcome)
    except Exception:
        # A failed text must never affect a call. Log and move on.
        log.exception("notify: failed to send call summary")
