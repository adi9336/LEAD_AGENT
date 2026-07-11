"""Scoring queue — Celery task + enqueue helper.

The webhook calls ``enqueue_scoring`` which pushes ``score_and_deliver_task``
onto the broker. The task retries the LLM with exponential backoff (Celery
``autoretry``) so transient failures are retried for a REAL score instead of
falling back to rules.

If Celery/Redis isn't reachable (e.g. local dev), ``enqueue_scoring`` runs the
work inline (synchronously) so the pipeline still works without a broker — the
retry/backoff benefit only applies in the queued (production) path.
"""

from __future__ import annotations

from app.common.logging import log


def enqueue_scoring(lead_id: str, *, request_id: str | None = None) -> None:
    """Queue scoring for ``lead_id``. Falls back to inline run without Redis.

    The inline fallback keeps local/dev runs working with zero infra. In
    production (ROLE=worker Celery running + Redis up) the task is enqueued and
    retried off the request path.
    """
    try:
        from app.scheduler.celery_app import celery_app
        # .delay() sends to the broker; if Redis is down this raises and we
        # catch below and run inline.
        score_and_deliver_task.delay(lead_id, request_id=request_id)
        log("queued", request_id=request_id, lead_id=lead_id, transport="celery")
    except Exception as exc:  # noqa: BLE001 - no broker -> run inline
        log("queued", request_id=request_id, lead_id=lead_id,
            transport="inline", detail=type(exc).__name__)
        from app.database.session import SessionLocal, init_db
        init_db()
        db = SessionLocal()
        try:
            from app.services.lead_service import score_and_deliver
            score_and_deliver(db, lead_id, request_id=request_id)
        finally:
            db.close()


def _make_task():
    """Build the Celery task lazily so importing this module never requires Redis."""
    from app.scheduler.celery_app import celery_app
    from app.config import settings
    from app.database.session import SessionLocal, init_db
    from app.services.lead_service import score_and_deliver
    from celery.exceptions import MaxRetriesExceededError

    @celery_app.task(
        name="app.services.tasks.score_and_deliver_task",
        bind=True,
        autoretry_for=(Exception,),
        max_retries=settings.score_max_retries,
        retry_backoff=settings.score_retry_backoff,
        retry_backoff_max=600,
        retry_jitter=True,
    )
    def score_and_deliver_task(self, lead_id: str, *, request_id: str | None = None):
        """Score + enrich + alert a lead, retried with backoff on failure.

        During retries ``allow_fallback=False`` so a transient LLM error is
        retried for a REAL score rather than degrading to rules. When Celery
        gives up (MaxRetriesExceeded), we run once more allowing the rules
        fallback so the lead is never left unscored.
        """
        init_db()
        db = SessionLocal()
        try:
            try:
                score_and_deliver(db, lead_id, request_id=request_id)
            except MaxRetriesExceededError:
                # All retries done -> final attempt with rules fallback allowed.
                score_and_deliver(db, lead_id, request_id=request_id,
                                  allow_fallback=True)
        finally:
            db.close()

    return score_and_deliver_task

# Imported by lead_service.intake_lead. Built lazily to avoid a hard Celery/Redis
# dependency at import time.
try:
    score_and_deliver_task = _make_task()
except Exception:  # noqa: BLE001 - broker unreachable at import; rebuilt on enqueue
    score_and_deliver_task = None  # type: ignore[assignment]
