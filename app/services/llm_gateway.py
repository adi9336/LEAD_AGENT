"""LLM Gateway — the ONLY module that talks to the language model.

Why a gateway: centralizes model access, timeout, audit logging, and the
deterministic fallback. No other module imports langchain/openai directly, so
the rest of the codebase runs with zero LLM dependencies.

Decoupling rule (important):
  LLM scoring is driven by the PRESENCE OF A KEY (or explicit USE_LLM=true),
  NOT by ADAPTER_MODE. ADAPTER_MODE only controls the integrations
  (monday / WhatsApp). This lets you score with the LLM while the CRM/WhatsApp
  stay mocked — exactly the "I have a key but no CRM yet" case.

  - key present / USE_LLM=true  -> LLM scores (falls back to rules on any error)
  - no key / USE_LLM=false      -> deterministic rules engine (100% available)
"""

from __future__ import annotations

import json
import time

from app.config import settings
from app.common.logging import log
from app.models.schemas import LeadInput, ScoreResult
from app.services.scoring import rules_score, validate_score_dict


def _use_llm() -> bool:
    """Decide whether to use the LLM: explicit flag OR a key is present."""
    if settings.use_llm:
        return True
    if settings.use_llm is False:
        return False
    return bool(settings.openai_api_key)  # auto: key present -> use LLM


def score_lead(lead: LeadInput, *, request_id: str | None = None) -> ScoreResult:
    """Score a lead via LLM (if available) with deterministic fallback.

    Returns:
        ScoreResult — from the LLM when available & valid, otherwise from the
        rules engine. Never raises on model failure (guarantees 100% scoring).
    """
    # --- Keyless / forced-rules path ----------------------------------------
    if not _use_llm():
        result = rules_score(lead)
        log("scored", request_id=request_id, mode="rules_fallback", tier=result.tier)
        return result

    # --- Live LLM path (LangChain, lazy-imported) ---------------------------
    try:
        import langchain_openai  # noqa: F401  (ensures installed)
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI

        prompt_text = open(_prompt_path()).read()
        llm = ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.openai_api_key,
            temperature=0,
            timeout=10,  # bounded wait so SLA is protected
        )
        start = time.monotonic()
        raw = llm.invoke([
            SystemMessage(content=prompt_text),
            HumanMessage(content=_format_input(lead)),
        ])
        latency_ms = int((time.monotonic() - start) * 1000)

        # Extract the JSON object from the model text (robust to prose/wrapping).
        parsed = _extract_json(raw.content) if hasattr(raw, "content") else None
        result = validate_score_dict(parsed) if isinstance(parsed, dict) else None
        if result is None:
            # Model returned malformed output -> fall back, keep pipeline moving.
            log("scored", request_id=request_id, mode="llm_invalid_fallback")
            return rules_score(lead)

        log("scored", request_id=request_id, mode="llm", tier=result.tier,
            latency_ms=latency_ms)
        return result
    except Exception as exc:  # noqa: BLE001 - never block scoring on model error
        log("scored", request_id=request_id, mode="llm_error_fallback",
            error=type(exc).__name__)
        return rules_score(lead)


def _extract_json(text: str) -> dict | None:
    """Pull the first {...} JSON object out of arbitrary model output."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except (ValueError, TypeError):
            return None
    return None


# ---- helpers ---------------------------------------------------------------
def _prompt_path() -> str:
    from pathlib import Path
    return str(Path(__file__).resolve().parent.parent / "prompts" / "qualification_prompt.txt")


def _format_input(lead: LeadInput) -> str:
    """Render the lead as plain text for the HumanMessage (brace-safe)."""
    return (
        f"Lead to qualify:\n"
        f"name: {lead.name}\n"
        f"company: {lead.company or '-'}\n"
        f"source: {lead.source or '-'}\n"
        f"industry: {lead.industry or '-'}\n"
        f"inquiry_type: {lead.inquiry_type or '-'}\n"
        "Return only the JSON object."
    )
