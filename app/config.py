"""Central, typed configuration.

All runtime configuration is read from environment variables (and an optional
``.env`` file) via pydantic-settings. Nothing here performs I/O or imports heavy
dependencies, so this module is safe to import anywhere (tests, CLI, workers).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings.

    Defaults are chosen so the project runs end-to-end with zero credentials:
    ``ADAPTER_MODE=mock`` uses log-backed adapters + a deterministic rules
    engine, and ``DATABASE_URL`` points at a local SQLite file.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # unknown vars (e.g. CI) are ignored, not fatal
    )

    # ---- Runtime mode ----
    adapter_mode: str = "mock"      # "mock" | "live"
    alert_provider: str = "whatsapp"  # "whatsapp" (Cloud API) | "twilio"

    # ---- Database ----
    # SQLite for local/dev; set a Postgres URL for production (same models).
    database_url: str = "sqlite:///./lead_agent.db"

    # ---- Scheduler (live mode only) ----
    redis_url: str = "redis://localhost:6379/0"

    # ---- SLA targets (see plan.md / Agent.md) ----
    sla_score_minutes: int = 5       # 100% leads scored within 5 min of intake
    sla_alert_minutes: int = 2       # WhatsApp alert within 2 min of qualification
    sla_unscored_hours: int = 24     # zero leads unscored beyond 24h
    hygiene_interval_minutes: int = 60  # Celery beat cadence for the sweep

    # ---- monday.com (live) ----
    monday_api_token: str = ""
    monday_board_id: str = ""
    monday_api_url: str = "https://api.monday.com/v2"

    # ---- WhatsApp Cloud API (live) ----
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_api_url: str = "https://graph.facebook.com/v19.0"

    # ---- Twilio WhatsApp (live, only if ALERT_PROVIDER=twilio) ----
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    # ---- Alert routing ----
    alert_recipient_phone: str = "+15550000001"  # assignee for qualified leads
    reviewer_phone: str = "+15550000002"          # escalation target

    # ---- Security ----
    webhook_secret: str = ""   # monday webhook shared secret (empty = unverified/dev)
    admin_token: str = ""      # Bearer token for /leads and /audit

    # ---- LLM (live scoring only) ----
    openai_api_key: str = ""
    llm_model: str = "gpt-4o-mini"


# Single shared instance. Imported across the app as `from app.config import settings`.
settings = Settings()
