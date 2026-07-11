"""Beyond Oil — Lead Qualification & Scoring Agent.

A modular FastAPI service that ingests monday.com CRM webhooks, scores and
classifies every inbound lead (Hot/Warm/Cold, distributor vs end-customer),
enriches the CRM, fires WhatsApp alerts, and enforces pipeline hygiene
(zero leads unscored >24h).

Package layout
--------------
app/
  config.py            Typed settings (env / .env)
  common/              Cross-cutting helpers (logging, retry)
  database/            SQLAlchemy engine, session, models, event log
  models/              Pydantic schemas (input + scoring result)
  prompts/             LLM prompt text
  services/            Business logic (gateway, scorer, clients, orchestration)
  scheduler/           Celery + plain hygiene-scan function
  api/                 HTTP routers (webhook + admin/audit)
  main.py              FastAPI app factory
"""

__version__ = "0.1.0"
