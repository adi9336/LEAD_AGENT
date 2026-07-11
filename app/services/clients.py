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


# ===================== monday.com =====================
class MondayClient(ABC):
    """Interface for CRM enrichment."""

    @abstractmethod
    def enrich(self, lead_id: str, *, tier: str, score: int,
               classification: str, rationale: list[str]) -> None:
        """Write score/classification/rationale back to the monday item."""


class MockMonday(MondayClient):
    def enrich(self, lead_id: str, *, tier: str, score: int,
               classification: str, rationale: list[str]) -> None:
        # Log-only; no network. Tests assert this was called.
        print(f"[MockMonday] enrich item={lead_id} "
              f"tier={tier} score={score} class={classification} "
              f"reasons={rationale}")


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
        payload = {
            "messaging_product": "whatsapp",
            "to": to_phone,
            "type": "text",
            "text": {"preview_url": False, "body": message},
        }
        resp = httpx.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {settings.whatsapp_token}"}, timeout=10,
        )
        resp.raise_for_status()


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
