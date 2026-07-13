"""External integration clients (adapters).

Each integration has an interface with two implementations:
  - Mock* : log-backed, no network. Used in ADAPTER_MODE=mock and in tests.
  - Live* : real HTTP calls (lazy-imports httpx).

Selecting the implementation by config keeps the core pipeline identical in mock
and live mode — only the transport changes. This is what makes the whole system
verifiable with zero credentials.
"""

from __future__ import annotations

import json

from abc import ABC, abstractmethod

from app.config import settings
from app.models.schemas import LeadInput


# ===================== monday.com =====================
class MondayClient(ABC):
    """Interface for CRM enrichment + lead fetching."""

    @abstractmethod
    def enrich(self, lead_id: str, *, tier: str, score: int,
               classification: str, rationale: list[str]) -> None:
        """Write score/classification/rationale back to the monday item."""

    @abstractmethod
    def fetch_due_leads(self) -> list[LeadInput]:
        """Return leads that still need scoring (Status column empty)."""

    @abstractmethod
    def fetch_all_leads(self) -> list[dict]:
        """Return every lead on the board with its scored columns, newest first.

        Each dict: id, name, company, source, industry, inquiry_type, tier,
        score, classification, rationale. Used by the dashboard so it reads
        from monday (the system of record) rather than container-local SQLite.
        """


class MockMonday(MondayClient):
    def enrich(self, lead_id: str, *, tier: str, score: int,
               classification: str, rationale: list[str]) -> None:
        # Log-only; no network. Tests assert this was called.
        print(f"[MockMonday] enrich item={lead_id} "
              f"tier={tier} score={score} class={classification} "
              f"reasons={rationale}")

    def fetch_due_leads(self) -> list[LeadInput]:
        # No network in mock mode; the cron run simply has nothing to do.
        return []

    def fetch_all_leads(self) -> list[dict]:
        # No network in mock mode.
        return []


# monday Classification status column uses these exact labels (Title Case, space),
# which differ from our internal snake_case. Map internal -> board label.
CLASSIFICATION_LABELS = {
    "end_customer": "End Customer",
    "distributor": "Distributor",
}


class LiveMonday(MondayClient):
    """Writes enrichment back to monday.com via GraphQL.

    Column IDs come from ``settings`` (fetched from the real board). monday
    status columns expect ``{"label": "..."}``; the numbers column expects a
    bare int; long-text expects ``{"text": "..."}``. The ``column_values``
    variable is passed as a JSON *string* (monday's expected format).
    """

    def enrich(self, lead_id: str, *, tier: str, score: int,
               classification: str, rationale: list[str]) -> None:
        import httpx  # lazy import; only needed in live mode

        class_label = CLASSIFICATION_LABELS.get(classification, classification)
        column_values = {
            settings.monday_col_status: {"label": tier},            # Hot|Warm|Cold
            settings.monday_col_score: score,                      # numbers col wants bare int
            settings.monday_col_classification: {"label": class_label},  # End Customer|Distributor
            settings.monday_col_rationale: {"text": " | ".join(rationale)},
        }
        query = """
        mutation ($bid: ID!, $iid: ID!, $group: JSON!) {
          change_multiple_column_values(item_id: $iid, board_id: $bid,
            column_values: $group) { id }
        }
        """
        # monday expects column_values as a JSON *string* for the JSON! variable.
        resp = httpx.post(
            settings.monday_api_url,
            json={"query": query, "variables": {
                "bid": settings.monday_board_id, "iid": str(lead_id),
                "group": json.dumps(column_values)}},
            headers={"Authorization": f"Bearer {settings.monday_api_token}"},
            timeout=10,
        )
        resp.raise_for_status()

    def fetch_due_leads(self) -> list[LeadInput]:
        import httpx

        # Pull items with their column values; keep only those whose Status
        # (color_mm55yz2s) column is still empty -> "needs scoring".
        query = """
        query ($b: ID!) {
          boards(ids: [$b]) {
            items_page(limit: 100) {
              items {
                id
                name
                column_values {
                  id
                  text
                  ... on StatusValue { label }
                }
              }
            }
          }
        }
        """
        resp = httpx.post(
            settings.monday_api_url,
            json={"query": query, "variables": {"b": settings.monday_board_id}},
            headers={"Authorization": f"Bearer {settings.monday_api_token}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        items = (data.get("boards") or [{}])[0] \
            .get("items_page", {}).get("items", [])

        due: list[LeadInput] = []
        for item in items:
            cols = {c["id"]: (c.get("label") or c.get("text")) for c in item["column_values"]}
            # Already scored? Skip (Status column has a label).
            if cols.get(settings.monday_col_status):
                continue
            due.append(LeadInput(
                id=item["id"],
                name=item.get("name"),
                company=cols.get(settings.monday_col_company),
                source=cols.get(settings.monday_col_source),
                industry=cols.get(settings.monday_col_industry),
                inquiry_type=cols.get(settings.monday_col_inquiry_type),
            ))
        return due

    def fetch_all_leads(self) -> list[dict]:
        import httpx

        # Pull every item with its scored columns. monday returns column
        # values keyed by column id; map them back to friendly field names.
        query = """
        query ($b: ID!) {
          boards(ids: [$b]) {
            items_page(limit: 100) {
              items {
                id
                name
                column_values {
                  id
                  text
                  ... on StatusValue { label }
                }
              }
            }
          }
        }
        """
        resp = httpx.post(
            settings.monday_api_url,
            json={"query": query, "variables": {"b": settings.monday_board_id}},
            headers={"Authorization": f"Bearer {settings.monday_api_token}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        items = (data.get("boards") or [{}])[0] \
            .get("items_page", {}).get("items", [])

        leads: list[dict] = []
        for item in items:
            cols = {c["id"]: (c.get("label") or c.get("text"))
                    for c in item["column_values"]}
            score_raw = cols.get(settings.monday_col_score)
            try:
                score = int(score_raw) if score_raw not in (None, "") else None
            except (TypeError, ValueError):
                score = None
            leads.append({
                "id": item["id"],
                "name": item.get("name"),
                "company": cols.get(settings.monday_col_company),
                "source": cols.get(settings.monday_col_source),
                "industry": cols.get(settings.monday_col_industry),
                "inquiry_type": cols.get(settings.monday_col_inquiry_type),
                "tier": cols.get(settings.monday_col_status),
                "score": score,
                "classification": cols.get(settings.monday_col_classification),
                "rationale": cols.get(settings.monday_col_rationale),
            })
        # Newest first by item id (monday ids are monotonic).
        leads.sort(key=lambda d: d["id"], reverse=True)
        return leads


def get_monday_client() -> MondayClient:
    # Live only if explicitly live AND credentials are present; otherwise mock
    # so the pipeline never hard-fails on a missing CRM connection.
    if settings.adapter_mode == "live" and settings.monday_api_token and settings.monday_board_id:
        return LiveMonday()
    return MockMonday()


# ===================== WhatsApp =====================
class WhatsAppClient(ABC):
    """Interface for alert delivery."""

    @abstractmethod
    def send(self, to_phone: str, message: str) -> None:
        """Deliver a text message to ``to_phone``."""


class MockWhatsApp(WhatsAppClient):
    def send(self, to_phone: str, message: str) -> None:
        print(f"[MockWhatsApp] -> {to_phone}: {message}")


class LiveCloudWhatsApp(WhatsAppClient):
    """Meta WhatsApp Cloud API."""

    def send(self, to_phone: str, message: str) -> None:
        import httpx

        url = f"{settings.whatsapp_api_url}/{settings.whatsapp_phone_number_id}/messages"
        # Primary: free-text (carries the real composed message). On a WhatsApp
        # test number free-text only delivers to allow-listed / 24h-windowed
        # recipients; Meta accepts it but silently drops it otherwise.
        # Fallback: the pre-approved hello_world template, which delivers to
        # anyone. The template body is static, so the composed detail is lost
        # in the fallback case — but the alert still reaches the phone.
        text_payload = {
            "messaging_product": "whatsapp",
            "to": to_phone,
            "type": "text",
            "text": {"preview_url": False, "body": message},
        }
        resp = httpx.post(
            url, json=text_payload,
            headers={"Authorization": f"Bearer {settings.whatsapp_token}"}, timeout=10,
        )
        if resp.status_code == 200 and "error" not in resp.json():
            return  # free-text delivered (or accepted) — done
        # Free-text failed (e.g. recipient not deliverable) -> template fallback
        tmpl_payload = {
            "messaging_product": "whatsapp",
            "to": to_phone,
            "type": "template",
            "template": {"name": "hello_world", "language": {"code": "en_US"}},
        }
        resp2 = httpx.post(
            url, json=tmpl_payload,
            headers={"Authorization": f"Bearer {settings.whatsapp_token}"}, timeout=10,
        )
        resp2.raise_for_status()
        body = resp2.json()
        if "error" in body:
            raise RuntimeError(f"WhatsApp Cloud API error: {body['error']}")


class LiveTwilioWhatsApp(WhatsAppClient):
    """Twilio WhatsApp (sandbox/number)."""

    def send(self, to_phone: str, message: str) -> None:
        import httpx

        url = (f"https://api.twilio.com/2010-04-01/Accounts/"
               f"{settings.twilio_account_sid}/Messages.json")
        data = {
            "To": f"whatsapp:{to_phone}",
            "From": f"whatsapp:{settings.twilio_from_number}",
            "Body": message,
        }
        resp = httpx.post(
            url, data=data,
            auth=(settings.twilio_account_sid, settings.twilio_auth_token), timeout=10,
        )
        resp.raise_for_status()


def get_whatsapp_client() -> WhatsAppClient:
    if settings.adapter_mode != "live":
        return MockWhatsApp()
    # Live mode: only use the real client if its credentials are present.
    # Otherwise fall back to the log-only mock so the pipeline doesn't
    # fail/escalate on a missing alert integration (alerts resume once
    # credentials are added to .env).
    if settings.alert_provider == "twilio":
        if settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_from_number:
            return LiveTwilioWhatsApp()
        return MockWhatsApp()
    if settings.whatsapp_token and settings.whatsapp_phone_number_id:
        return LiveCloudWhatsApp()
    return MockWhatsApp()
