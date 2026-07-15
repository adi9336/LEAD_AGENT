"""Batch cron pipeline as a PARALLEL LangGraph flow.

The hourly cron used to score leads one-by-one (a Python ``for`` loop calling
``run_lead_graph`` per lead). This module replaces that with a LangGraph
fan-out: every due lead is dispatched as its OWN concurrent stream via the
``Send`` API, so N leads are scored/enriched/alerted in parallel instead of
serially. That collapses the run's wall-clock time roughly N-fold, which is
what lets many leads finish inside a serverless function's time limit.

    START --(fan_out: one Send per lead)--> process_one (xN in parallel) --> collect --> END

Design rules (match the rest of the codebase):
  - langgraph is LAZY-imported inside builders so importing this module stays
    cheap and the test-suite / mock path never needs langgraph unless used.
  - CONCURRENCY IS BOUNDED by ``settings.cron_max_concurrency`` (passed as
    LangGraph's ``max_concurrency`` config) so we never open unlimited
    LLM / monday / SQLite connections at once.
  - THREAD SAFETY: SQLAlchemy Session objects are NOT thread-safe and parallel
    branches run on different threads, so each ``process_one`` branch opens its
    OWN ``SessionLocal()`` and closes it — it never shares the caller's session.
  - The per-lead control flow (score -> route -> alert -> enrich -> audit) is
    REUSED unchanged from ``app.agents.lead_graph.run_lead_graph``.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from app.common.logging import log
from app.config import settings


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
class BatchState(TypedDict, total=False):
    """State for the outer batch graph.

    ``lead_ids`` is the work list; ``request_id`` correlates the whole run;
    ``results`` accumulates one entry per processed lead. The ``operator.add``
    reducer lets parallel branches append concurrently without clobbering.
    """

    lead_ids: list[str]
    request_id: str
    results: Annotated[list, operator.add]


class _BranchInput(TypedDict):
    """Payload delivered to each parallel ``process_one`` branch via Send."""

    lead_id: str
    request_id: str


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #
def _fan_out(state: BatchState):
    """Conditional entry edge: emit one ``Send`` per lead -> parallel branches.

    Returning a list of ``Send`` objects tells LangGraph to run the target
    node once per item, concurrently (bounded by ``max_concurrency`` config).
    """
    from langgraph.types import Send

    rid = state["request_id"]
    return [
        Send("process_one", {"lead_id": lead_id, "request_id": rid})
        for lead_id in state["lead_ids"]
    ]


def _process_one(payload: _BranchInput) -> dict:
    """Process ONE lead in its own thread + its own DB session.

    Runs the existing per-lead LangGraph pipeline. Opens a fresh session
    because parallel branches execute on different threads and SQLAlchemy
    sessions are not thread-safe. Never raises: a failing lead is recorded in
    ``results`` so one bad lead can't abort the whole batch.
    """
    # Imported here (not at module top) to avoid a circular import:
    # lead_graph imports lead_service, which we don't want to pull in eagerly.
    from app.agents.lead_graph import run_lead_graph
    from app.database.session import SessionLocal

    lead_id = payload["lead_id"]
    rid = payload["request_id"]
    db = SessionLocal()
    try:
        final = run_lead_graph(db, lead_id, request_id=f"{rid}-{lead_id}")
        return {"results": [{"lead_id": lead_id, "status": final.get("status", "ok")}]}
    except Exception as exc:  # noqa: BLE001 - isolate one lead's failure
        log("cron_item_failed", request_id=rid, lead_id=lead_id,
            error=type(exc).__name__)
        return {"results": [{"lead_id": lead_id, "status": "failed"}]}
    finally:
        db.close()


def _collect(state: BatchState) -> dict:
    """Join point after all parallel branches complete (no-op aggregator)."""
    return {}


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #
def build_batch_graph():
    """Compile the parallel batch graph (lazy import langgraph)."""
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(BatchState)
    g.add_node("process_one", _process_one)
    g.add_node("collect", _collect)

    # START fans out to N parallel `process_one` branches; each then flows to
    # the single `collect` join node, which LangGraph runs once all branches
    # in the super-step have finished.
    g.add_conditional_edges(START, _fan_out, ["process_one"])
    g.add_edge("process_one", "collect")
    g.add_edge("collect", END)
    return g.compile()


def run_batch_graph(lead_ids: list[str], *, request_id: str) -> list[dict]:
    """Run all leads through the parallel pipeline. Returns per-lead results.

    Concurrency is bounded by ``settings.cron_max_concurrency`` via LangGraph's
    ``max_concurrency`` config so we never exceed a safe number of simultaneous
    LLM / monday / DB operations.
    """
    if not lead_ids:
        return []
    graph = build_batch_graph()
    initial: BatchState = {
        "lead_ids": lead_ids, "request_id": request_id, "results": [],
    }
    log("cron_batch_start", request_id=request_id, leads=len(lead_ids),
        max_concurrency=settings.cron_max_concurrency)
    final = graph.invoke(
        initial,
        config={"max_concurrency": max(1, settings.cron_max_concurrency)},
    )
    results = final.get("results", [])
    log("cron_batch_done", request_id=request_id, processed=len(results))
    return results
