"""Central, typed configuration.

All runtime configuration is read from environment variables (and an optional
``.env`` file) via pydantic-settings. Nothing here performs I/O or imports heavy
dependencies, so this module is safe to import anywhere (tests, CLI, workers).
"""

from typing import Optional

from pydantic import field_validator
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
    adapter_mode: str = "mock"      # "mock" | "live"  (controls integrations only)
    alert_provider: str = "whatsapp"  # "whatsapp" (Cloud API) | "twilio"

    # LLM scoring: None=auto (use LLM if OPENAI_API_KEY set), True=force, False=force rules.
    # Declared Optional[bool]; the validator maps ""/"null"/"none" -> None so an
    # empty USE_LLM= in .env.example does not crash startup.
    use_llm: Optional[bool] = None

    @field_validator("use_llm", mode="before")
    @classmethod
    def _coerce_bool(cls, v):
        if v is None or v == "" or str(v).lower() in ("null", "none"):
            return None
        if str(v).lower() in ("1", "true", "yes", "on"):
            return True
        if str(v).lower() in ("0", "false", "no", "off"):
            return False
        return None

    # ---- Database ----
    # SQLite for local/dev; set a Postgres URL for production (same models).
    database_url: str = "sqlite:///./lead_agent.db"

    # ---- Queue / scoring reliability ----
    # Scoring runs on a Celery queue (when Redis is available) with retry/backoff
    # so transient LLM failures are retried for a REAL score instead of silently
    # falling back to the rules engine.
    redis_url: str = "redis://localhost:6379/0"
    score_max_retries: int = 5          # LLM attempts before rules fallback
    score_retry_backoff: int = 15       # seconds (Celery exponential backoff base)

    # ---- SLA targets (see plan.md / Agent.md) ----
    sla_score_minutes: int = 5       # 100% leads scored within 5 min of intake
    sla_alert_minutes: int = 2       # WhatsApp alert within 2 min of qualification
    sla_unscored_hours: int = 24     # zero leads unscored beyond 24h
    hygiene_interval_minutes: int = 60  # Celery beat cadence for the sweep

    # ---- monday.com (live) ----
    monday_api_token: str = ""
    monday_board_id: str = ""
    monday_api_url: str = "https://api.monday.com/v2"
    # Column-ID map (fetched from your board; see curl in README/setup).
    # READ fields (inbound from webhook):
    monday_col_name: str = "name"
    monday_col_company: str = "text_mm55ken1"
    monday_col_source: str = "text_mm552667"
    monday_col_industry: str = "text_mm55n0s8"
    monday_col_enquiry: str = "text_mm5551t0"
    # Board has a single "Enquiry" text column; map both spellings to it.
    monday_col_inquiry_type: str = "text_mm5551t0"
    # WRITE fields (enrichment back to CRM):
    monday_col_status: str = "color_mm55yz2s"       # Hot/Warm/Cold
    monday_col_score: str = "numeric_mm552zp2"       # 0-100
    monday_col_classification: str = "color_mm55bsrk"  # distributor/end_customer
    monday_col_rationale: str = "long_text_mm551ga"  # reasons
    # Webhook board subscription challenge secret (optional; monday sends ?challenge=)
    monday_webhook_challenge: str = ""

    # ---- WhatsApp Cloud API (live) ----
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_api_url: str = "https://graph.facebook.com/v19.0"
    # Template used to deliver alerts. On a WhatsApp test number, free-text is
    # accepted by Meta but SILENTLY DROPPED (only allow-listed / 24h-windowed
    # recipients receive it). A template delivers to anyone. If the template
    # has a {{1}} body variable (e.g. a custom "lead_alert" template), the
    # crafted message rides in that variable; otherwise the static template
    # body is sent. Default "hello_world" is pre-approved for the test number.
    whatsapp_template: str = "hello_world"
    whatsapp_template_has_var: bool = False  # set True when the template uses {{1}}

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
