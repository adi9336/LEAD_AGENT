"""Pipeline hygiene — zero leads unscored beyond SLA.

`run_hygiene(db)` is a PLAIN function with no Celery/Redis dependency, so it is
fully unit-testable. The Celery task below merely wraps it. This separation
keeps the logic clean and the tests fast.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.common.logging import log
from app.database.models import Lead
from app.services.lead_service import score_and_deliver


def run_hygiene(db: Session, *, request_id: str | None = None) -> dict:
    """Find leads unscored past the SLA window and recover/escalate them.

    Returns a small report (counts) for logging/audit.
    """
    from app.config import settings

    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.sla_unscored_hours)
    stale = (
        db.query(Lead)
        .filter(Lead.scored_at.is_(None), Lead.created_at < cutoff,
                Lead.deleted_at.is_(None))
        .all()
    )
    recovered = 0
    escalated = 0
    for lead in stale:
        # Re-attempt scoring/enrich/alert via the same retryable pipeline.
        try:
            score_and_deliver(db, lead.id, request_id=request_id or f"hygiene-{lead.id}")
            if lead.scored_at is not None:
                recovered += 1
            else:
                escalated += 1
        except Exception:  # noqa: BLE001
            escalated += 1
    log("hygiene", request_id=request_id, found=len(stale),
        recovered=recovered, escalated=escalated)
    return {"found": len(stale), "recovered": recovered, "escalated": escalated}
