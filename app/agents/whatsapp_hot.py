"""WhatsApp Hot-Lead Agent — a LangGraph flow.

WHEN: a lead scores ``Hot``.
WHAT: an LLM composes a personalized WhatsApp message from the lead's full
detail (name, company, industry, source),
then the agent sends it to the sales reviewer.

WHY LANGGRAPH: the user asked for the hot-lead handling to be a *set of agent
calls wrapped in a flow* rather than a single inline template. LangGraph gives
us an explicit, observable, retry-able two-step graph:

        START -> compose_message -> send_whatsapp -> END

Each node is wrapped with ``@traceable`` so the whole flow shows up in
LangSmith as one run with two child steps — exactly the "better execution /
observability" the user wanted.

DESIGN RULES (match the rest of the codebase):
  - LLM/langgraph are LAZY-imported: this module is only imported when a lead
    is actually Hot, so Warm/Cold paths and the test-suite never pull it in.
  - The LLM call is bounded (timeout) and retried via ``with_retry``.
  - PII-minimal: we send the lead's business detail to the *internal* sales
    reviewer (not the lead), so name/company/industry/score are appropriate.
  - Sending uses the existing WhatsApp client; the composed text is the
    payload. Free-text delivers to allow-listed / 24h-windowed recipients;
    the client falls back to the approved template when free-text fails.
"""

from __future__ import annotations

import time
from typing import TypedDict

from app.common.logging import log
from app.common.retry import with_retry
from app.config import settings


# --------------------------------------------------------------------------- #
# Graph state
# --------------------------------------------------------------------------- #
class HotLeadState(TypedDict, total=False):
    """Shared state passed between LangGraph nodes."""

    lead: dict            # snapshot of lead fields (business detail only)
    score: int
    classification: str
    reasons: list[str]
    request_id: str | None
    composed_message: str
    send_status: str      # "sent" | "template_fallback" | "failed"
    error: str | None


# --------------------------------------------------------------------------- #
# LLM message composition (node 1)
# --------------------------------------------------------------------------- #
def _compose_with_llm(lead: dict, score: int, classification: str,
                      reasons: list[str], request_id: str | None) -> str:
    """Ask the LLM to write a tight, human WhatsApp message for the sales rep.

    Returns a one-to-three line message — never a wall of text. Bounded + retried.
    """
    import langchain_openai  # noqa: F401  (ensures installed)
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    system = (
        "You are the messaging agent for 'Beyond Oil' lead qualification. "
        "Write a SHORT, clean WhatsApp message (max 3 lines, ~280 chars) for "
        "the INTERNAL sales reviewer about a newly scored HOT lead. "
        "Tone: crisp, professional, urgent-but-not-pushy. "
        "MUST DO: greet briefly, name the lead + company + industry + source, "
        "and say in one sentence why they're a strong fit (use the context "
        "provided). "
        "MUST NOT: do NOT print any 'Score', 'Status', 'Classification', "
        "'Rationale', or 'Why hot' labels or values. Do NOT use markdown, "
        "emojis, bullet points, or fake promises. No questions. "
        "Output ONLY the message text, ready to send."
    )
    human = (
        f"Lead: {lead.get('name', '-')}\n"
        f"Company: {lead.get('company', '-')}\n"
        f"Industry: {lead.get('industry', '-')}\n"
        f"Source: {lead.get('source', '-')}\n"
        f"Score: {score}/100\n"
        f"Classification: {classification}\n"
        f"Why hot: {'; '.join(reasons) if reasons else '-'}"
    )

    llm = ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        temperature=0.3,
        timeout=10,
    )
    start = time.monotonic()
    raw = llm.invoke([
        SystemMessage(content=system),
        HumanMessage(content=human),
    ])
    latency_ms = int((time.monotonic() - start) * 1000)
    text = raw.content if hasattr(raw, "content") else str(raw)
    log("hot_agent_compose", request_id=request_id, latency_ms=latency_ms,
        chars=len(text))
    return text.strip()


def _node_compose(state: HotLeadState) -> HotLeadState:
    """LangGraph node: compose the personalized message via the LLM."""
    rid = state.get("request_id")
    try:
        msg = with_retry(
            lambda: _compose_with_llm(
                state["lead"], state["score"], state["classification"],
                state.get("reasons", []), rid),
            request_id=rid, event="hot_agent_compose",
        )
        return {"composed_message": msg, "error": None}
    except Exception as exc:  # noqa: BLE001
        # LLM failed after retries -> compose a clean fallback (no score/
        # status labels) so the lead still gets alerted, never silent.
        fb = (
            f"New HOT lead: {state['lead'].get('name', '-')} "
            f"({state['lead'].get('company', '-')}, "
            f"{state['lead'].get('industry', '-')}). "
            f"Strong fit — follow up promptly."
        )
        log("hot_agent_compose", request_id=rid, mode="fallback",
            error=type(exc).__name__)
        return {"composed_message": fb, "error": f"compose_fallback:{exc}"}


# --------------------------------------------------------------------------- #
# WhatsApp send (node 2)
# --------------------------------------------------------------------------- #
def _node_send(state: HotLeadState) -> HotLeadState:
    """LangGraph node: send the composed message via the WhatsApp client."""
    from app.services.clients import get_whatsapp_client

    whatsapp = get_whatsapp_client()
    to = settings.alert_recipient_phone
    text = state.get("composed_message", "")
    rid = state.get("request_id")
    try:
        whatsapp.send(to, text)  # client handles free-text + template fallback
        log("hot_agent_sent", request_id=rid, to=to)
        return {"send_status": "sent", "error": None}
    except Exception as exc:  # noqa: BLE001
        log("hot_agent_sent", request_id=rid, status="failed",
            error=type(exc).__name__)
        return {"send_status": "failed", "error": f"send_failed:{exc}"}


# --------------------------------------------------------------------------- #
# Graph assembly
# --------------------------------------------------------------------------- #
def _traceable(fn):
    """Wrap a node with LangSmith tracing when the SDK is present (optional)."""
    try:
        from langsmith import traceable
        return traceable(name=fn.__name__)(fn)
    except Exception:
        return fn


def build_hot_lead_graph():
    """Construct the Hot-lead LangGraph (lazy import inside caller)."""
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(HotLeadState)
    g.add_node("compose_message", _traceable(_node_compose))
    g.add_node("send_whatsapp", _traceable(_node_send))
    g.add_edge(START, "compose_message")
    g.add_edge("compose_message", "send_whatsapp")
    g.add_edge("send_whatsapp", END)
    return g.compile()


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def run_hot_lead_flow(lead: dict, score: int, classification: str,
                      reasons: list[str], *, request_id: str | None = None):
    """Run the Hot-lead WhatsApp agent.

    Args:
        lead: business-detail snapshot (name/company/industry/source).
        score, classification, reasons: the LLM score result.
        request_id: correlation id for logs/traces.

    Returns the final state (composed_message + send_status).
    """
    rid = request_id or f"hot-{lead.get('id', '?')}"
    graph = build_hot_lead_graph()
    initial: HotLeadState = {
        "lead": lead, "score": score, "classification": classification,
        "reasons": reasons, "request_id": rid, "error": None,
    }
    log("hot_agent_start", request_id=rid, lead_id=lead.get("id"))
    final = graph.invoke(initial)
    log("hot_agent_done", request_id=rid, status=final.get("send_status"))
    return final
