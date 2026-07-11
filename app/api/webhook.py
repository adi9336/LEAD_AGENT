"""Webhook router — monday.com inbound lead intake.

Security: verifies the shared webhook secret (header) before processing.
Returns 202 immediately so monday.com is never blocked; heavy work runs in
`process_lead` (sync here; can be pushed to a worker later without API change).
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
from app.services.lead_service import process_lead

router = APIRouter()


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
    process_lead(db, lead_in, request_id=rid)
    log("intake_accepted", request_id=rid, lead_id=lead_in.id)
    return {"status": "accepted", "lead_id": lead_in.id, "request_id": rid}


def _extract_lead(body: bytes) -> dict:
    """Parse monday webhook JSON into LeadInput fields.

    monday.com webhooks nest the item under `event.data.itemData` (or similar).
    We read the common fields and tolerate missing optional ones.
    """
    import json

    msg = json.loads(body)
    item = msg.get("event", {}).get("data", {}).get("itemData") or msg.get("item", {})
    column_values = {
        cv.get("id"): cv.get("text") or cv.get("value")
        for cv in item.get("column_values", [])
        if isinstance(cv, dict)
    }
    return {
        "id": str(item.get("id")),
        "name": item.get("name", "Unknown"),
        "company": column_values.get("company"),
        "source": column_values.get("source"),
        "industry": column_values.get("industry"),
        # Accept either spelling — Beyond Oil's board may use "enquiry" (UK).
        "inquiry_type": column_values.get("inquiry_type") or column_values.get("enquiry"),
        "enquiry": column_values.get("enquiry") or column_values.get("inquiry_type"),
    }
