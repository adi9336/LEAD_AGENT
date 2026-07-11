"""Structured logging with request-id propagation.

We avoid noisy third-party logging libs. A single helper gives every log line a
consistent JSON-ish shape with a `request_id` so a lead's journey (intake ->
scored -> enriched -> alerted) can be traced end-to-end. PII (name/phone) is
never passed into these messages.
"""

import logging
import sys
import uuid
from contextvars import ContextVar

# ---- request-scoped context ------------------------------------------------
# A ContextVar lets every log call within one request share the same id without
# threading it through every function signature.
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    """Generate a fresh request id (UUID4, short)."""
    return uuid.uuid4().hex[:16]


def set_request_id(rid: str | None) -> None:
    """Bind a request id for the current context (call once per request)."""
    _request_id.set(rid)


def get_request_id() -> str:
    """Return the current request id, creating one if none is set."""
    rid = _request_id.get()
    if rid is None:
        rid = new_request_id()
        _request_id.set(rid)
    return rid


# ---- logger ----------------------------------------------------------------
_logger = logging.getLogger("lead_agent")
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)


def log(event: str, request_id: str | None = None, **fields) -> None:
    """Emit one structured log line for a pipeline transition.

    Args:
        event: transition name (intake|scored|enriched|alerted|escalated|retry|failure)
        request_id: optional id; falls back to the context var
        **fields: extra key/values to include (NEVER PII)
    """
    rid = request_id or get_request_id()
    parts = [f"event={event}", f"request_id={rid}"]
    for k, v in fields.items():
        parts.append(f"{k}={v}")
    _logger.info(" ".join(parts))
