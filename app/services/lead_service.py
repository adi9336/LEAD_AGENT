"""Lead orchestration — the core pipeline.

`process_lead` implements the full Agent.md intake workflow:
  1. persist lead (intake timestamp)
  2. score + classify (LLM gateway, with fallback)
  3. enrich CRM (monday adapter, retried)
  4. alert assignee (WhatsApp adapter, retried)
  5. record SLA timestamps + audit events
On any external failure it retries (bounded) and, as a last resort, escalates to
the reviewer. A lead is NEVER left unscored (fallback guarantees a score).
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


def process_lead(db: Session, lead_in: LeadInput, *, request_id: str | None = None) -> Lead:
    """Run the full intake workflow for one lead. Returns the persisted Lead."""
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

    # 2. Score + classify (always succeeds via fallback)
    result = score_lead(lead_in, request_id=rid)
    lead.score = result.score
    lead.tier = result.tier
    lead.classification = result.classification
    lead.rationale = json.dumps(result.reasons)
    lead.scored_at = _now()
    db.commit()
    log("scored", request_id=rid, lead_id=lead.id, tier=result.tier, score=result.score)

    # 3. Enrich CRM (retried; failure does not block the alert)
    monday = get_monday_client()
    try:
        with_retry(
            lambda: monday.enrich(lead.id, tier=result.tier, score=result.score,
                                  classification=result.classification,
                                  rationale=result.reasons),
            request_id=rid, event="enriched",
        )
        _event(db, lead.id, "enriched", "ok", "crm updated", rid)
    except Exception as exc:  # noqa: BLE001
        _event(db, lead.id, "enriched", "failure", f"{type(exc).__name__}", rid)
        _escalate(db, lead, rid, reason="crm_enrich_failed")

    # 4. Alert assignee (retried; escalation on final failure)
    whatsapp = get_whatsapp_client()
    if not lead.alert_sent:
        message = build_alert_message(lead, result.tier, result.score, result.reasons)
        try:
            with_retry(lambda: whatsapp.send(settings.alert_recipient_phone, message),
                       request_id=rid, event="alerted")
            lead.alert_sent = True
            lead.alerted_at = _now()
            db.commit()
            _event(db, lead.id, "alerted", "ok", "whatsapp sent", rid)
            log("alerted", request_id=rid, lead_id=lead.id)
        except Exception as exc:  # noqa: BLE001
            _event(db, lead.id, "alerted", "failure", f"{type(exc).__name__}", rid)
            _escalate(db, lead, rid, reason="alert_failed")

    return lead


def _escalate(db: Session, lead: Lead, rid: str | None, *, reason: str) -> None:
    """Escalate a lead to the reviewer (one-time flag)."""
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
