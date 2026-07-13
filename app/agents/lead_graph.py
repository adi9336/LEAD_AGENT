"""Lead pipeline as a LangGraph flow.

This is the orchestration graph for a SINGLE lead. The cron (run_cron) keeps
the batch loop — it calls ``run_lead_graph`` once per due lead. LangGraph's
``Send`` fan-out is overkill for our flat hourly batch, so we keep the loop
and let the graph own the per-lead control flow, which is exactly what the
user wanted: the whole pipeline expressed as an agent graph.

NODES (each @traceable so LangSmith shows the full flow per lead):

    START -> score -> route_tier
                            |-- tier == Hot  -> hot_alert -> enrich -> audit -> END
                            |-- otherwise    -> static_alert -> enrich -> audit -> END

Leaf logic (LLM scoring, monday enrich, WhatsApp send, the Hot-lead agent)
is REUSED unchanged from app.services.* — the graph only wires the order and
decides the alert path by tier. Retries stay inside the leaf functions
(with_retry), so the graph itself is simple and deterministic.

PII rule: audit EventLog rows never carry PII (enforced by _event).
"""

from __future__ import annotations

from typing import TypedDict

from app.common.logging import log
from app.common.retry import with_retry
from app.config import settings
from app.models.schemas import LeadInput
from app.services.clients import get_monday_client, get_whatsapp_client
from app.services.lead_service import (
    Lead,
    _event,
    _escalate,
    _lead_fields,
    _now,
)
from app.services.llm_gateway import score_lead


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
class LeadState(TypedDict, total=False):
    """State threaded through the per-lead graph."""

    db: object            # SQLAlchemy Session (not serializable; held in-memory only)
    lead_id: str
    request_id: str | None
    result: object        # ScoreResult from the LLM/rules
    tier: str
    score: int
    classification: str
    reasons: list[str]
    error: str | None
    status: str           # "ok" | "escalated" | "failed"


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #
def _traceable(fn):
    try:
        from langsmith import traceable
        return traceable(name=fn.__name__)(fn)
    except Exception:
        return fn


def _node_score(state: LeadState) -> dict:
    """Score the lead (real LLM w/ retry, rules fallback)."""
    db = state["db"]
    lead = db.get(Lead, state["lead_id"])
    rid = state["request_id"]
    lead_input = LeadInput(**_lead_fields(lead))
    try:
        result = with_retry(
            lambda: score_lead(lead_input, request_id=rid, allow_fallback=False),
            max_attempts=settings.score_max_retries,
            base_delay=settings.score_retry_backoff,
            request_id=rid, event="scored",
        )
    except Exception:
        result = score_lead(lead_input, request_id=rid, allow_fallback=True)
    # persist score
    lead.score = result.score
    lead.tier = result.tier
    lead.classification = result.classification
    lead.rationale = __import__("json").dumps(result.reasons)
    lead.scored_at = _now()
    db.commit()
    log("scored", request_id=rid, lead_id=lead.id, tier=result.tier, score=result.score)
    return {
        "result": result, "tier": result.tier, "score": result.score,
        "classification": result.classification, "reasons": result.reasons,
    }


def _route_tier(state: LeadState) -> str:
    """Conditional edge: which alert path to take."""
    return "hot" if state.get("tier") == "Hot" else "standard"


def _node_hot_alert(state: LeadState) -> dict:
    """Hot lead -> LangGraph Hot-lead agent (compose + send)."""
    from app.agents.whatsapp_hot import run_hot_lead_flow

    db = state["db"]
    lead = db.get(Lead, state["lead_id"])
    rid = state["request_id"]
    snapshot = {
        "id": lead.id, "name": lead.name, "company": lead.company,
        "industry": lead.industry, "source": lead.source,
    }
    flow = run_hot_lead_flow(
        snapshot, state["score"], state["classification"],
        state.get("reasons", []), request_id=rid,
    )
    if flow.get("send_status") == "sent":
        lead.alert_sent = True
        lead.alerted_at = _now()
        db.commit()
        _event(db, lead.id, "alerted", "ok", "hot_agent sent", rid)
        return {"error": None}
    _event(db, lead.id, "alerted", "failure",
           flow.get("error", "hot_agent_failed"), rid)
    _escalate(db, lead, rid, reason="hot_agent_failed")
    return {"error": "hot_agent_failed"}


def _node_static_alert(state: LeadState) -> dict:
    """Warm/Cold leads -> NO WhatsApp notification.

    Only HOT leads alert (via the hot-agent flow). Warm/Cold are scored and
    synced to monday (enrich) but stay silent on WhatsApp, so the sales rep
    only gets a ping for genuinely hot leads — not 11 messages per run.
    """
    db = state["db"]
    lead = db.get(Lead, state["lead_id"])
    rid = state["request_id"]
    _event(db, lead.id, "alerted", "skipped",
           f"tier={state.get('tier')} (no alert for non-Hot)", rid)
    return {"error": None}


def _node_enrich(state: LeadState) -> dict:
    """Write the score/tier/rationale back to monday (CRM of record)."""
    db = state["db"]
    lead = db.get(Lead, state["lead_id"])
    rid = state["request_id"]
    monday = get_monday_client()
    try:
        with_retry(
            lambda: monday.enrich(lead.id, tier=state["tier"], score=state["score"],
                                  classification=state["classification"],
                                  rationale=state["reasons"]),
            request_id=rid, event="enriched",
        )
        _event(db, lead.id, "enriched", "ok", "crm updated", rid)
        return {"error": None}
    except Exception as exc:  # noqa: BLE001
        _event(db, lead.id, "enriched", "failure", f"{type(exc).__name__}", rid)
        _escalate(db, lead, rid, reason="crm_enrich_failed")
        return {"error": f"enrich_failed:{exc}"}


def _node_audit(state: LeadState) -> dict:
    """Final audit row + status for the caller."""
    db = state["db"]
    lead = db.get(Lead, state["lead_id"])
    rid = state["request_id"]
    status = "ok" if not lead.escalated else "escalated"
    _event(db, lead.id, "lead_processed", status, f"tier={state.get('tier')}", rid)
    return {"status": status}


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #
def build_lead_graph():
    """Compile the per-lead orchestration graph (lazy import langgraph)."""
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(LeadState)
    g.add_node("score", _traceable(_node_score))
    g.add_node("hot_alert", _traceable(_node_hot_alert))
    g.add_node("static_alert", _traceable(_node_static_alert))
    g.add_node("enrich", _traceable(_node_enrich))
    g.add_node("audit", _traceable(_node_audit))

    g.add_edge(START, "score")
    g.add_conditional_edges("score", _route_tier, {"hot": "hot_alert", "standard": "static_alert"})
    g.add_edge("hot_alert", "enrich")
    g.add_edge("static_alert", "enrich")
    g.add_edge("enrich", "audit")
    g.add_edge("audit", END)
    return g.compile()


def run_lead_graph(db, lead_id: str, *, request_id: str | None = None) -> dict:
    """Run the full per-lead pipeline as a LangGraph flow.

    Returns the final state (status etc.). The Lead row is persisted by the
    nodes; the caller (run_cron) reads it back if needed.
    """
    rid = request_id or f"graph-{lead_id}"
    graph = build_lead_graph()
    initial: LeadState = {"db": db, "lead_id": lead_id, "request_id": rid, "error": None}
    log("lead_graph_start", request_id=rid, lead_id=lead_id)
    final = graph.invoke(initial)
    log("lead_graph_done", request_id=rid, lead_id=lead_id, status=final.get("status"))
    return final
