"""External integration clients (adapters).

Each integration has an interface with two implementations:
  - Mock* : log-backed, no network. Used in ADAPTER_MODE=mock and in tests.
  - Live* : real HTTP calls (lazy-imports httpx).

Selecting the implementation by config keeps the core pipeline identical in mock
and live mode — only the transport changes. This is what makes the whole system
verifiable with zero credentials.
"""

from __future__ import annotations

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


class LiveMonday(MondayClient):
    def enrich(self, lead_id: str, *, tier: str, score: int,
               classification: str, rationale: list[str]) -> None:
        import httpx  # lazy import; only needed in live mode

        query = """
        mutation ($bid: ID!, $iid: ID!, $group: JSON) {
          change_multiple_column_values(item_id: $iid, board_id: $bid,
            column_values: $group) { id }
        }
        """
        # Column IDs must match the real board; placeholders shown.
        column_values = {
            "score": {"text": str(score)},
            "tier": {"label": tier},
            "classification": {"label": classification},
            "rationale": {"text": " | ".join(rationale)},
        }
        resp = httpx.post(
            settings.monday_api_url,
            json={"query": query, "variables": {
                "bid": settings.monday_board_id, "iid": lead_id,
                "group": column_values}},
            headers={"Authorization": f"Bearer {settings.monday_api_token}"},
            timeout=10,
        )
        resp.raise_for_status()


def get_monday_client() -> MondayClient:
    return LiveMonday() if settings.adapter_mode == "live" else MockMonday()


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
    if settings.alert_provider == "twilio":
        return LiveTwilioWhatsApp()
    return LiveCloudWhatsApp()
