"""Agent orchestration package.

Holds the LangGraph-driven flows that need multi-step reasoning (compose
text -> act). Kept separate from the core pipeline (lead_service) so the
cron path stays simple and agent code is lazy-imported only when needed.
"""
