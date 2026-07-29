"""In-call resident approval via SMS reply.

Used by voice-agent-people when the model calls ask_resident for an unrecognized
visitor: we text NOTIFY_SMS_TO and this module waits for a YES/NO reply on /sms.

State is process-local. That is fine for a personal door with Cloud Run
`--max-instances=1`; with more than one instance the SMS webhook can land on a
different process than the WebSocket and the wait will time out. Fail-closed
either way — no reply means the door stays shut.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass, field

log = logging.getLogger("op9.approvals")

# One pending approval at a time. A second buzz while one is waiting is denied
# rather than displacing the first — keeps a single YES/NO unambiguous.
_lock = threading.Lock()
_pending: PendingApproval | None = None


@dataclass
class PendingApproval:
    """A door decision waiting on an SMS reply."""

    call_sid: str
    claim: str
    loop: asyncio.AbstractEventLoop
    event: asyncio.Event = field(default_factory=asyncio.Event)
    decision: bool | None = None  # True=open, False=deny, None=unresolved


def parse_reply(body: str) -> bool | None:
    """Map an SMS body to open/deny. None if it is not a recognizable reply."""
    text = (body or "").strip().upper()
    # Strip a leading keyword so "YES please" and "NO thanks" still count.
    token = re.split(r"\s+", text, maxsplit=1)[0] if text else ""
    if token in ("YES", "Y", "OPEN", "OK", "APPROVE"):
        return True
    if token in ("NO", "N", "DENY", "DENIED", "REJECT"):
        return False
    return None


async def try_begin(*, call_sid: str, claim: str) -> PendingApproval | None:
    """Register a pending approval before the SMS is sent.

    Returns None if another approval is already waiting — the caller should deny
    that second buzz. Must run before notify.send_approval_request so a fast YES
    cannot arrive before anyone is listening.
    """
    global _pending
    pending = PendingApproval(
        call_sid=call_sid,
        claim=claim,
        loop=asyncio.get_running_loop(),
    )

    with _lock:
        if _pending is not None and not _pending.event.is_set():
            log.info(
                "approvals: busy (CallSid=%s), rejecting CallSid=%s",
                _pending.call_sid,
                call_sid,
            )
            return None
        _pending = pending
    return pending


async def wait(pending: PendingApproval, timeout: float) -> bool | None:
    """Wait for a YES/NO on `pending`. Returns True/False, or None on timeout."""
    global _pending
    try:
        await asyncio.wait_for(pending.event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        log.info("approvals: CallSid=%s timed out waiting for SMS", pending.call_sid)
        return None
    finally:
        with _lock:
            if _pending is pending:
                _pending = None

    return pending.decision


def resolve(approved: bool) -> str | None:
    """Apply an SMS decision to the current pending approval.

    Thread-safe: the WebSocket waiter and the /sms webhook (or a test thread)
    may run on different loops/threads. Returns the CallSid that was resolved,
    or None if nothing was waiting.
    """
    with _lock:
        pending = _pending
        if pending is None or pending.event.is_set():
            return None
        pending.decision = approved
        call_sid = pending.call_sid
        loop = pending.loop

    # Wake the waiter on the loop that owns the Event.
    loop.call_soon_threadsafe(pending.event.set)
    return call_sid


def peek() -> PendingApproval | None:
    """Current pending approval, if any. For tests and the SMS handler log."""
    return _pending
