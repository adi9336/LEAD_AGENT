"""Client-facing live dashboard.

A single self-contained HTML page (no build step, no JS framework) that reads
the real Lead + EventLog tables and renders:
  - headline stats (total leads, Hot/Warm/Cold, alerts sent)
  - SLA compliance summary
  - a table of recently scored leads with their rationale

This is the visual surface for demos / screenshots — point a client at
``/dashboard`` (or screen-record it) to *show* the agent working instead of
describing it. Read-only; no admin token required so it can be shown freely
(it shows lead names/companies — put behind auth if that is sensitive).
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database.models import EventLog, Lead
from app.database.session import get_session

router = APIRouter()

_TIER_COLOR = {"Hot": "#ef4444", "Warm": "#f59e0b", "Cold": "#3b82f6"}


def _fmt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs}h ago"
    return f"{hrs // 24}d ago"


def _rationale_text(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return " · ".join(str(p) for p in parsed)
        return str(parsed)
    except (ValueError, TypeError):
        return raw


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(db: Session = Depends(get_session)):
    # SOURCE OF TRUTH:
    #  - LIVE mode (deployed cron writes to monday): read straight from the
    #    monday board via the client, so the dashboard reflects the system of
    #    record even though cron + web run in separate containers (separate
    #    SQLite files).
    #  - MOCK/LOCAL: read the local audit DB (the cron's data lives there).
    from app.services.clients import get_monday_client

    live = get_monday_client()
    try:
        leads_raw = live.fetch_all_leads()
        source = "monday"
    except Exception:
        # network/cred issue -> fall back to local SQLite audit log
        leads_raw = db.query(Lead).filter(Lead.deleted_at.is_(None)).all()
        source = "local"

    # unified accessor: monday rows are dicts, local rows are ORM objects
    if source == "monday":
        def _get(l, k):
            return l.get(k)
    else:
        def _get(l, k):
            return getattr(l, k, None)
    leads = leads_raw

    total = len(leads)
    hot = sum(1 for l in leads if _get(l, "tier") == "Hot")
    warm = sum(1 for l in leads if _get(l, "tier") == "Warm")
    cold = sum(1 for l in leads if _get(l, "tier") == "Cold")
    scored = sum(1 for l in leads if _get(l, "score") is not None)
    alerts = sum(1 for l in leads if _get(l, "alert_sent"))

    last_run = (
        db.query(EventLog)
        .filter(EventLog.event == "cron_run")
        .order_by(EventLog.created_at.desc())
        .first()
    )
    last_run_txt = _fmt(last_run.created_at) if last_run else "no runs yet"

    # --- build the leads table rows ---
    rows = []
    for l in leads[:50]:
        tier = _get(l, "tier") or "—"
        color = _TIER_COLOR.get(tier, "#64748b")
        badge = (
            f'<span class="badge" style="background:{color}1a;color:{color};'
            f'border:1px solid {color}55">{html.escape(tier)}</span>'
        )
        score = f"{_get(l, 'score')}" if _get(l, "score") is not None else "—"
        rationale = _rationale_text(_get(l, "rationale"))
        rows.append(
            "<tr>"
            f"<td class='name'>{html.escape(_get(l, 'name') or '—')}</td>"
            f"<td>{html.escape(_get(l, 'company') or '—')}</td>"
            f"<td>{html.escape(_get(l, 'industry') or '—')}</td>"
            f"<td style='text-align:center'>{badge}</td>"
            f"<td style='text-align:center;font-weight:700'>{score}</td>"
            f"<td class='rationale'>{html.escape(rationale)}</td>"
            f"<td class='muted'>{'✓' if _get(l, 'alert_sent') else '—'}</td>"
            f"<td class='muted'>{_fmt(_get(l, 'scored_at'))}</td>"
            "</tr>"
        )
    rows_html = "\n".join(rows) or (
        "<tr><td colspan='8' class='muted' style='text-align:center;padding:32px'>"
        "No leads yet — run the agent to populate the board.</td></tr>"
    )

    def stat(label: str, value, sub: str = "", color: str = "#e2e8f0") -> str:
        sub_html = f"<div class='stat-sub'>{html.escape(sub)}</div>" if sub else ""
        return (
            f"<div class='stat'><div class='stat-val' style='color:{color}'>{value}</div>"
            f"<div class='stat-label'>{html.escape(label)}</div>{sub_html}</div>"
        )

    stats = "".join([
        stat("Total leads", total, f"{scored} scored"),
        stat("Hot", hot, "high priority", _TIER_COLOR["Hot"]),
        stat("Warm", warm, "follow up", _TIER_COLOR["Warm"]),
        stat("Cold", cold, "nurture", _TIER_COLOR["Cold"]),
        stat("WhatsApp alerts", alerts, "sent to sales", "#22c55e"),
    ])

    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Lead Agent — Live Dashboard</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family:'Segoe UI',system-ui,-apple-system,sans-serif;
    background:#0b1120; color:#e2e8f0; }}
  .wrap {{ max-width:1200px; margin:0 auto; padding:32px 24px 64px; }}
  header {{ display:flex; align-items:center; justify-content:space-between;
    flex-wrap:wrap; gap:12px; margin-bottom:28px; }}
  h1 {{ font-size:22px; margin:0; font-weight:700; letter-spacing:-.3px; }}
  h1 .dot {{ color:#22c55e; }}
  .sub {{ color:#94a3b8; font-size:13px; margin-top:4px; }}
  .live {{ display:inline-flex; align-items:center; gap:7px; font-size:12px;
    color:#22c55e; background:#22c55e12; padding:6px 12px; border-radius:20px;
    border:1px solid #22c55e33; }}
  .live .pulse {{ width:8px; height:8px; border-radius:50%; background:#22c55e;
    animation:p 1.6s infinite; }}
  @keyframes p {{ 0%,100%{{opacity:1}} 50%{{opacity:.3}} }}
  .stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
    gap:14px; margin-bottom:28px; }}
  .stat {{ background:#111a2e; border:1px solid #1e293b; border-radius:14px;
    padding:18px 20px; }}
  .stat-val {{ font-size:34px; font-weight:800; line-height:1; }}
  .stat-label {{ font-size:13px; color:#cbd5e1; margin-top:8px; font-weight:600; }}
  .stat-sub {{ font-size:11px; color:#64748b; margin-top:2px; }}
  .card {{ background:#111a2e; border:1px solid #1e293b; border-radius:14px;
    overflow:hidden; }}
  .card h2 {{ font-size:14px; margin:0; padding:16px 20px; border-bottom:1px solid #1e293b;
    color:#cbd5e1; font-weight:600; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ text-align:left; padding:11px 14px; color:#64748b; font-weight:600;
    font-size:11px; text-transform:uppercase; letter-spacing:.5px;
    border-bottom:1px solid #1e293b; }}
  td {{ padding:12px 14px; border-bottom:1px solid #16203a; vertical-align:top; }}
  tr:last-child td {{ border-bottom:none; }}
  tr:hover td {{ background:#16203a55; }}
  .name {{ font-weight:600; color:#f1f5f9; }}
  .rationale {{ color:#94a3b8; max-width:340px; font-size:12px; line-height:1.5; }}
  .muted {{ color:#64748b; text-align:center; }}
  .badge {{ padding:3px 10px; border-radius:20px; font-size:11px; font-weight:700; }}
  footer {{ margin-top:24px; color:#475569; font-size:12px; text-align:center; }}
</style></head>
<body><div class="wrap">
  <header>
    <div>
      <h1>Lead Qualification Agent <span class="dot">●</span></h1>
      <div class="sub">Autonomous lead scoring · monday.com → LLM → WhatsApp · last run {last_run_txt}</div>
    </div>
    <div class="live"><span class="pulse"></span> LIVE · auto-refresh 30s</div>
  </header>
  <div class="stats">{stats}</div>
  <div class="card">
    <h2>Recently scored leads</h2>
    <table>
      <thead><tr>
        <th>Lead</th><th>Company</th><th>Industry</th><th style="text-align:center">Tier</th>
        <th style="text-align:center">Score</th><th>Rationale</th>
        <th style="text-align:center">Alert</th><th>Scored</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
  <footer>Beyond Oil · Lead Agent · powered by FastAPI + LangChain · {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}</footer>
</div></body></html>""")
