"""Retry helper with bounded exponential backoff.

Every external call (monday enrich, WhatsApp alert) goes through `with_retry`
so failures are retried a fixed number of times before being escalated. This is
the only place retry/backoff logic lives — clients stay simple.
"""

import time

from app.common.logging import log


def with_retry(
    fn,
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    request_id: str | None = None,
    event: str = "retry",
):
    """Call ``fn`` and retry on exception with exponential backoff.

    Args:
        fn: zero-arg callable that performs the side-effecting work.
        max_attempts: total tries (1 = no retry).
        base_delay: initial backoff seconds; doubles each attempt.
        request_id: for structured logs.
        event: log event label.

    Returns:
        The return value of ``fn`` on success.

    Raises:
        The last exception after all attempts are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - we re-raise after retries
            last_exc = exc
            log(event, request_id=request_id, attempt=attempt,
                error=type(exc).__name__, detail=str(exc)[:120])
            if attempt < max_attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
    assert last_exc is not None
    raise last_exc
