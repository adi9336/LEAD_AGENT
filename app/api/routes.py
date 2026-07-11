"""Admin / audit router.

`/health` is public. `/leads` and `/audit` require `Authorization: Bearer
<ADMIN_TOKEN>` (see Agent.md §13). The audit endpoint computes SLA compliance
from persisted timestamps — the single source of truth for success criteria.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database.models import EventLog, Lead
from app.database.session import get_session

router = APIRouter()


def _require_admin(authorization: str | None = Header(default=None)) -> None:
    if not settings.admin_token:
        return  # dev: no token configured
    if authorization != f"Bearer {settings.admin_token}":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin token")


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/leads")
def list_leads(
    db: Session = Depends(get_session),
    _: None = Depends(_require_admin),
):
    leads = db.query(Lead).filter(Lead.deleted_at.is_(None)).all()
    return [l.to_dict() for l in leads]


@router.get("/audit")
def audit(
    db: Session = Depends(get_session),
    _: None = Depends(_require_admin),
):
    """Compute SLA compliance from persisted timestamps."""
    now = datetime.now(timezone.utc)
    leads = db.query(Lead).filter(Lead.deleted_at.is_(None)).all()

    scored_ok = alert_ok = 0
    unscored_stale = 0
    for l in leads:
        if l.scored_at is not None:
            if (l.scored_at - l.created_at).total_seconds() / 60 <= settings.sla_score_minutes:
                scored_ok += 1
        else:
            age_h = (now - l.created_at).total_seconds() / 3600
            if age_h > settings.sla_unscored_hours:
                unscored_stale += 1
        if l.alerted_at is not None and l.scored_at is not None:
            if (l.alerted_at - l.scored_at).total_seconds() / 60 <= settings.sla_alert_minutes:
                alert_ok += 1

    total = len(leads)
    return {
        "total_leads": total,
        "scored_within_sla": scored_ok,
        "alerts_within_sla": alert_ok,
        "unscored_older_than_sla": unscored_stale,
        "sla": {
            "score_minutes": settings.sla_score_minutes,
            "alert_minutes": settings.sla_alert_minutes,
            "unscored_hours": settings.sla_unscored_hours,
        },
    }
