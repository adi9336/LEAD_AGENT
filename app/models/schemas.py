"""Pydantic schemas: validated inbound payload + scoring result.

These describe the *contract* of the system. The webhook layer validates
incoming monday.com data into `LeadInput`; the scorer returns `ScoreResult`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

# Allowed closed sets — used by guardrails to reject invalid model output.
TIERS = ("Hot", "Warm", "Cold")
CLASSIFICATIONS = ("distributor", "end_customer")


def _utcnow() -> datetime:
    """Current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


class LeadInput(BaseModel):
    """Validated payload from a monday.com new-item webhook.

    Only these fields are consumed. Missing optional fields become ``None``;
    the scorer treats ``None`` as "unknown" rather than crashing.
    """

    id: str                                   # monday item id (primary key)
    name: str
    company: Optional[str] = None
    source: Optional[str] = None
    industry: Optional[str] = None
    inquiry_type: Optional[str] = None
    created_at: Optional[datetime] = None     # intake time; defaults to now()


class ScoreResult(BaseModel):
    """Deterministic scoring output (used by gateway + fallback)."""

    score: int                                # 0-100
    tier: str                                 # Hot | Warm | Cold
    classification: str                       # distributor | end_customer
    reasons: list[str] = Field(default_factory=list)
