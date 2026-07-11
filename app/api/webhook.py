"""Webhook router — monday.com inbound lead intake.

Security: verifies the shared webhook secret (header) before processing.
Returns 202 immediately so monday.com is never blocked; scoring is enqueued
on the Celery queue (`intake_lead` persists + `score_and_deliver` retries the
LLM with backoff) so heavy work runs off the request path.
"""

from __future__ import annotations

import hmac
import hashlib

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.common.logging import get_request_id, log, new_request_id, set_request_id
from app.config import settings
from app.database.session import get_session
from app.models.schemas import LeadInput
from app.services.lead_service import intake_lead

router = APIRouter()


@router.get("/webhook/monday")
async def webhook_monday_challenge(challenge: str | None = None):
    """monday.com webhook subscription handshake.

    When you create a webhook via the API, monday immediately GETs this URL
    with a `?challenge=...` param and expects the same value echoed back as
    JSON: {"challenge": "..."}. Without this, the webhook fails to register.
    """
    return {"challenge": challenge}


def _verify_secret(payload: bytes, signature: str | None) -> None:
    """Reject requests whose shared secret does not match (dev: empty = allow)."""
    if not settings.webhook_secret:
        return  # dev / unverified
    if not signature:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing signature")
    expected = hmac.new(
        settings.webhook_secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")


@router.post("/webhook/monday", status_code=status.HTTP_202_ACCEPTED)
async def webhook_monday(
    request: Request,
    db: Session = Depends(get_session),
    x_monday_webhook_secret: str | None = Header(default=None),
):
    """Receive a monday.com new-item webhook and queue scoring."""
    body = await request.body()
    _verify_secret(body, x_monday_webhook_secret)

    rid = new_request_id()
    set_request_id(rid)

    # monday sends a nested payload; extract the lead fields defensively.
    try:
        data = _extract_lead(body)
    except Exception as exc:  # noqa: BLE001
        log("intake", request_id=rid, error="parse_failed", detail=str(exc)[:120])
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid payload")

    lead_in = LeadInput(**data)
    intake_lead(db, lead_in, request_id=rid)
    # Optional fast-path: score inline (the hourly cron is the primary path).
    from app.services.lead_service import score_and_deliver
    score_and_deliver(db, lead_in.id, request_id=rid, allow_fallback=True)
    log("intake_accepted", request_id=rid, lead_id=lead_in.id)
    return {"status": "accepted", "lead_id": lead_in.id, "request_id": rid}


def _extract_lead(body: bytes) -> dict:
    """Parse monday webhook JSON into LeadInput fields.

    monday.com webhooks nest the item under ``event.data.itemData`` (current
    API) or a top-level ``item`` (older). Column values are keyed by the
    board's real column IDs, so we map them via ``settings.monday_col_*``
    rather than assuming generic names.
    """
    import json

    msg = json.loads(body)
    item = msg.get("event", {}).get("data", {}).get("itemData") or msg.get("item", {})
    cv = {
        c.get("id"): c.get("text") or c.get("value")
        for c in item.get("column_values", [])
        if isinstance(c, dict)
    }
    name = cv.get(settings.monday_col_name) or item.get("name", "Unknown")
    return {
        "id": str(item.get("id")),
        "name": name,
        "company": cv.get(settings.monday_col_company),
        "source": cv.get(settings.monday_col_source),
        "industry": cv.get(settings.monday_col_industry),
        # Accept either spelling — Beyond Oil's board uses "enquiry" (UK).
        "inquiry_type": cv.get(settings.monday_col_enquiry) or cv.get(settings.monday_col_inquiry_type),
        "enquiry": cv.get(settings.monday_col_enquiry) or cv.get(settings.monday_col_inquiry_type),
    }
