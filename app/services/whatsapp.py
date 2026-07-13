"""WhatsApp sending — clean, observable delivery path.

WHY A DEDICATED MODULE
----------------------
The previous inline implementation had a delivery bug: it tried free-text
FIRST, and because Meta returns HTTP 200 for free-text even when it silently
drops the message (test numbers only deliver to allow-listed recipients), the
function returned early and the reliable template path was never used. So the
user got nothing.

THE RULE (WhatsApp Cloud, especially a TEST number)
---------------------------------------------------
  * A pre-approved TEMPLATE delivers to anyone (provided the recipient is on
    the test number's allow-list). This is the reliable path.
  * Free-text only delivers inside a 24h customer-service window, or to a
    recipient the business has messaged before. Meta ACCEPTS it (200) but
    DROPS it otherwise — no error. So free-text must never be the primary.

SEND ORDER (fixed)
------------------
  1. TEMPLATE (primary). If the template has a {{1}} body variable, the crafted
     message rides inside it (personalized + delivers). Otherwise the static
     approved template (hello_world) is sent — it delivers, just generic.
  2. FREE-TEXT (fallback ONLY if the template call errors, e.g. template not
     yet approved). Carries the real crafted text for allow-listed recipients.

Every call logs the full Meta response (wamid + message_status) so delivery
is observable instead of silent.
"""

from __future__ import annotations

import httpx

from app.common.logging import log
from app.config import settings


def _base_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json",
    }


def _endpoint() -> str:
    return (f"{settings.whatsapp_api_url}"
            f"/{settings.whatsapp_phone_number_id}/messages")


def _send_template(to_phone: str, message: str) -> dict:
    """Send via the configured template. Carries `message` in {{1}} if enabled.

    Returns Meta's JSON response (caller inspects message_status).
    """
    template = settings.whatsapp_template
    tmpl: dict = {"name": template, "language": {"code": "en_US"}}
    if settings.whatsapp_template_has_var:
        tmpl["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": message[:1024]}],
        }]
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "template",
        "template": tmpl,
    }
    return _post(payload, to_phone)


def _send_text(to_phone: str, message: str) -> dict:
    """Fallback free-text (carries crafted message; only delivers to windowed/
    allow-listed recipients). Returns Meta's JSON response."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"preview_url": False, "body": message},
    }
    return _post(payload, to_phone)


def _post(payload: dict, to_phone: str) -> dict:
    resp = httpx.post(_endpoint(), json=payload,
                      headers=_base_headers(), timeout=15)
    body = resp.json()
    # Log the authoritative delivery signal.
    status = None
    if resp.status_code == 200:
        msgs = body.get("messages") or []
        status = msgs[0].get("message_status") if msgs else "no_message"
    log("whatsapp_send", to=to_phone, http=resp.status_code,
        message_status=status,
        wamid=(body.get("messages") or [{}])[0].get("id"),
        error=body.get("error"))
    if resp.status_code != 200 or "error" in body:
        raise RuntimeError(f"WhatsApp API {resp.status_code}: {body.get('error')}")
    return body


def send(to_phone: str, message: str) -> dict:
    """Send `message` to `to_phone`. Template-primary, free-text fallback.

    Returns Meta's JSON response (caller can read message_status). Raises on
    hard failure so the pipeline can escalate/retry.
    """
    # 1) Template first (reliable on test numbers; carries crafted text if {{1}}).
    try:
        return _send_template(to_phone, message)
    except Exception as exc:  # noqa: BLE001
        # Template failed (e.g. custom template not approved yet) -> free-text.
        log("whatsapp_send", to=to_phone, fallback="free_text",
            reason=type(exc).__name__)
        return _send_text(to_phone, message)
