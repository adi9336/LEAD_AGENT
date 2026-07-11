"""Deterministic scoring engine (the always-available fallback).

This module has ZERO external dependencies. It implements the same tier bands
and classification rules as the LLM prompt, so that scoring is 100% available
even when no API key is set or the model is down. The LLM gateway falls back to
this engine and flags ``fallback_used=True`` in its audit row.
"""

from __future__ import annotations

from app.models.schemas import CLASSIFICATIONS, TIERS, LeadInput, ScoreResult

# Core ICP for Beyond Oil: food-service / industrial frying operations.
CORE_INDUSTRY = (
    "food service", "restaurant", "qsr", "food manufacturing", "industrial frying",
    "frying", "hospitality", "catering", "food distribution",
)
ADJACENT_INDUSTRY = ("food", "beverage", "retail", "horeca")
HIGH_INTENT_SOURCE = ("referral", "partner", "direct")
INBOUND_SOURCE = ("website", "inbound", "organic", "trade_show", "trade show")
BUYING_SIGNALS = ("pricing", "price", "demo", "quote", "volume", "bulk", "distribute", "wholesale")
LOW_INTENT_SOURCE = ("paid_ad", "cold_outreach", "social", "ad")


def _points_source(source: str | None) -> tuple[int, str]:
    s = (source or "").lower().strip()
    if s in HIGH_INTENT_SOURCE:
        return 25, "High-intent source (referral/partner/direct)"
    if s in INBOUND_SOURCE:
        return 15, "Inbound source (website/organic/trade show)"
    if s in LOW_INTENT_SOURCE:
        return 8, "Low-intent source (paid/social/cold)"
    return 5, "Unspecified source"


def _points_industry(industry: str | None) -> tuple[int, str]:
    i = (industry or "").lower().strip()
    if any(k in i for k in CORE_INDUSTRY):
        return 30, "Core industry fit (food-service/frying)"
    if any(k in i for k in ADJACENT_INDUSTRY):
        return 18, "Adjacent food industry"
    return 5, "Outside core industry"


def _points_inquiry(inquiry: str | None) -> tuple[int, str]:
    q = (inquiry or "").lower().strip()
    if any(sig in q for sig in BUYING_SIGNALS):
        return 30, "Concrete buying signal in inquiry"
    if q in ("general info", "sample request", "more information"):
        return 15, "Moderate intent inquiry"
    if q in ("just browsing", "curious", ""):
        return 5, "Low-intent / vague inquiry"
    return 10, "Neutral inquiry"


def _classify(lead: LeadInput) -> tuple[str, str]:
    """Return (classification, reason). Distributor intent overrides industry."""
    blob = " ".join(filter(None, [lead.company, lead.source, lead.industry, lead.inquiry_type])).lower()
    if any(sig in blob for sig in ("distribute", "wholesale", "reseller", "bulk")):
        return "distributor", "Distribution/wholesale intent detected"
    return "end_customer", "Direct operation (default)"


def _tier(score: int) -> str:
    if score >= 75:
        return "Hot"
    if score >= 50:
        return "Warm"
    return "Cold"


def rules_score(lead: LeadInput) -> ScoreResult:
    """Compute a deterministic ScoreResult for a lead."""
    p_source, r_source = _points_source(lead.source)
    p_industry, r_industry = _points_industry(lead.industry)
    p_inquiry, r_inquiry = _points_inquiry(lead.inquiry_type)
    # small penalty for fully missing industry/source
    penalty = 0
    if not lead.industry:
        penalty += 5
    if not lead.source:
        penalty += 5
    score = max(0, min(100, p_source + p_industry + p_inquiry - penalty))
    classification, c_reason = _classify(lead)
    return ScoreResult(
        score=score,
        tier=_tier(score),
        classification=classification,
        reasons=[r_source, r_industry, r_inquiry, c_reason],
    )


# Guardrails: validate a dict (e.g. from the LLM) into a ScoreResult.
def validate_score_dict(data: dict) -> ScoreResult | None:
    """Return a ScoreResult if ``data`` matches the contract, else None."""
    try:
        score = int(data["score"])
        tier = str(data["tier"])
        classification = str(data["classification"])
        reasons = list(data.get("reasons", [])) or ["(no reasons provided)"]
    except (KeyError, TypeError, ValueError):
        return None
    if tier not in TIERS or classification not in CLASSIFICATIONS:
        return None
    score = max(0, min(100, score))
    return ScoreResult(score=score, tier=tier, classification=classification, reasons=reasons)
