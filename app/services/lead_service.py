"""Lead orchestration — the core pipeline.

Two entry points:

  ``intake_lead``  — fast, called by the webhook. Persists the lead and
      enqueues scoring on the Celery queue (or runs it inline when Redis is
      unavailable, e.g. local dev). Returns immediately (202-friendly).

  ``score_and_deliver`` — the queued unit of work. Scores the lead with the
      LLM (retried with backoff by Celery for a REAL score), then enriches the
      CRM and alerts via WhatsApp. Falls back to the rules engine only after
      every retry is exhausted, so transient LLM errors never silently
      downgrade a lead to a fake score.

Flow:
  intake -> [queue] -> score (retry LLM) -> enrich CRM -> alert -> audit
On any external failure it retries (bounded) and, as a last resort, escalates
to the reviewer. A lead is NEVER left unscored.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.common.logging import log
from app.common.retry import with_retry
from app.config import settings
from app.database.models import EventLog, Lead
from app.models.schemas import LeadInput
from app.services.clients import get_monday_client, get_whatsapp_client
from app.services.llm_gateway import score_lead


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_alert_message(lead: Lead, tier: str, score: int, reasons: list[str]) -> str:
    """Format the WhatsApp alert text (no PII beyond name/company)."""
    company = lead.company or "-"
    return (
        f"🔥 New {tier} lead — Beyond Oil\n"
        f"{lead.name} · {company}\n"
        f"Type: {lead.classification} | Source: {lead.source or '-'}\n"
        f"Score: {score}/100\n"
        f"Why: {'; '.join(reasons)}"
    )


def _event(db: Session, lead_id: str | None, event: str, status: str,
           detail: str, request_id: str | None) -> None:
    """Append an audit row (detail MUST NOT contain PII)."""
    db.add(EventLog(lead_id=lead_id, event=event, status=status,
                    detail=detail, request_id=request_id))
    db.commit()


def intake_lead(db: Session, lead_in: LeadInput, *, request_id: str | None = None) -> Lead:
    """Persist the lead. Returns the (pending) Lead.

    The caller is responsible for scoring (e.g. ``run_cron`` calls
    ``score_and_deliver`` directly, or the webhook does the same inline).
    """
    rid = request_id

    # 1. Persist (idempotent upsert keyed by monday id)
    lead = db.get(Lead, lead_in.id)
    if lead is None:
        lead = Lead(id=lead_in.id, name=lead_in.name, company=lead_in.company,
                   source=lead_in.source, industry=lead_in.industry,
                   inquiry_type=lead_in.inquiry_type,
                   created_at=lead_in.created_at or _now())
        db.add(lead)
    else:
        # Re-processing the same id: refresh fields, keep prior timestamps.
        lead.name = lead_in.name
        lead.company = lead_in.company
        lead.source = lead_in.source
        lead.industry = lead_in.industry
        lead.inquiry_type = lead_in.inquiry_type
    db.commit()
    log("intake", request_id=rid, lead_id=lead.id)
    return lead


def score_and_deliver(db: Session, lead_id: str, *, request_id: str | None = None,
                     allow_fallback: bool = False) -> Lead | None:
    """Score, enrich CRM, and alert — orchestrated as a LangGraph flow.

    The per-lead control flow (score -> route by tier -> Hot agent / static
    alert -> enrich -> audit) lives in ``app.agents.lead_graph``. This wrapper
    keeps the original signature so ``run_cron`` / tests / webhook are
    unchanged, and delegates the work to the graph. ``allow_fallback`` is
    accepted for signature compatibility (the leaf retry logic lives inside
    the graph's score node).
    """
    rid = request_id or f"score-{lead_id}"
    lead = db.get(Lead, lead_id)
    if lead is None:
        log("scored", request_id=rid, lead_id=lead_id, error="lead_not_found")
        return None

    # Delegate the whole per-lead pipeline to the LangGraph flow.
    from app.agents.lead_graph import run_lead_graph
    run_lead_graph(db, lead_id, request_id=rid)

    return db.get(Lead, lead_id)


def _lead_fields(lead: Lead) -> dict:
    """Snapshot Lead columns into a LeadInput-shaped dict (for re-scoring)."""
    return {
        "id": lead.id,
        "name": lead.name,
        "company": lead.company,
        "source": lead.source,
        "industry": lead.industry,
        "inquiry_type": lead.inquiry_type,
        "created_at": lead.created_at,
    }


def run_cron(db: Session, *, request_id: str | None = None) -> dict:
    """Hourly cron entry point.

    Fetches leads that still need scoring from monday, then scores+delivers
    each. Scoring retries the LLM with exponential backoff (via score_lead's
    allow_fallback=False + this loop) so transient LLM failures get a REAL
    score rather than the rules fallback. Only after local retries are
    exhausted does a lead fall back to rules, so it is never left unscored.

    Returns a small report for logs/monitoring.
    """
    from app.config import settings
    from app.services.clients import get_monday_client

    rid = request_id or f"cron-{_now().strftime('%Y%m%d%H%M')}"
    monday = get_monday_client()
    due = monday.fetch_due_leads()
    scored = 0
    failed = 0
    for lead_in in due:
        try:
            # Persist (idempotent) then score+deliver. score_and_deliver
            # retries the LLM internally; allow_fallback=False keeps retries
            # honest, True only here as the final safety net.
            intake_lead(db, lead_in, request_id=f"{rid}-{lead_in.id}")
            score_and_deliver(db, lead_in.id,
                              request_id=f"{rid}-{lead_in.id}",
                              allow_fallback=True)
            scored += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log("cron_item_failed", request_id=rid, lead_id=lead_in.id,
                error=type(exc).__name__)
    log("cron_run", request_id=rid, due=len(due), scored=scored, failed=failed)
    return {"due": len(due), "scored": scored, "failed": failed}


def _escalate(db: Session, lead: Lead, rid: str | None, *, reason: str) -> None:
    if lead.escalated:
        return
    whatsapp = get_whatsapp_client()
    msg = f"⚠️ Escalation ({reason}) for lead {lead.id} ({lead.name})"
    try:
        with_retry(lambda: whatsapp.send(settings.reviewer_phone, msg),
                   request_id=rid, event="escalated")
    except Exception:  # noqa: BLE001 - escalation itself failing is logged only
        pass
    lead.escalated = True
    lead.escalated_at = _now()
    db.commit()
    _event(db, lead.id, "escalated", "escalation", reason, rid)
    log("escalated", request_id=rid, lead_id=lead.id, reason=reason)
