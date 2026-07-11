"""LLM Gateway — the ONLY module that talks to the language model.

Why a gateway: centralizes model access, timeout, audit logging, and the
deterministic fallback. No other module imports langchain/openai directly, so
the rest of the codebase runs with zero LLM dependencies (mock mode or when no
key is set).

Keyless operation: if `ADAPTER_MODE=mock` or `OPENAI_API_KEY` is empty, the
gateway immediately returns the rules engine result with `fallback_used=True`.
"""

from __future__ import annotations

import json
import time

from app.config import settings
from app.common.logging import log
from app.models.schemas import LeadInput, ScoreResult
from app.services.scoring import rules_score, validate_score_dict


def score_lead(lead: LeadInput, *, request_id: str | None = None) -> ScoreResult:
    """Score a lead via LLM (live) with deterministic fallback.

    Returns:
        ScoreResult — from the LLM when available & valid, otherwise from the
        rules engine. Never raises on model failure (guarantees 100% scoring).
    """
    # --- Keyless / mock path ------------------------------------------------
    if settings.adapter_mode == "mock" or not settings.openai_api_key:
        result = rules_score(lead)
        log("scored", request_id=request_id, mode="rules_fallback", tier=result.tier)
        return result

    # --- Live LLM path (LangChain, lazy-imported) ---------------------------
    try:
        import langchain_openai  # noqa: F401  (ensures installed)
        from langchain_core.prompts import PromptTemplate
        from langchain_core.output_parsers import JsonOutputParser
        from langchain_openai import ChatOpenAI

        prompt_text = open(_prompt_path()).read()
        parser = JsonOutputParser()
        llm = ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.openai_api_key,
            temperature=0,
            timeout=10,  # bounded wait so SLA is protected
        )
        chain = (
            PromptTemplate.from_template(prompt_text)
            | llm
            | parser
        )
        start = time.monotonic()
        raw = chain.invoke(_format_input(lead))
        latency_ms = int((time.monotonic() - start) * 1000)

        parsed = validate_score_dict(raw) if isinstance(raw, dict) else None
        if parsed is None:
            # Model returned malformed output -> fall back, keep pipeline moving.
            log("scored", request_id=request_id, mode="llm_invalid_fallback")
            return rules_score(lead)

        log("scored", request_id=request_id, mode="llm", tier=parsed.tier,
            latency_ms=latency_ms)
        return parsed
    except Exception as exc:  # noqa: BLE001 - never block scoring on model error
        log("scored", request_id=request_id, mode="llm_error_fallback",
            error=type(exc).__name__)
        return rules_score(lead)


# ---- helpers ---------------------------------------------------------------
def _prompt_path() -> str:
    from pathlib import Path
    return str(Path(__file__).resolve().parent.parent / "prompts" / "qualification_prompt.txt")


def _format_input(lead: LeadInput) -> dict:
    """Build the template variables from a lead."""
    return {
        "name": lead.name,
        "company": lead.company or "-",
        "source": lead.source or "-",
        "industry": lead.industry or "-",
        "inquiry_type": lead.inquiry_type or "-",
    }
