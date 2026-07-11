# Beyond Oil — Lead Qualification & Scoring Agent

An autonomous agent that ingests **monday.com** CRM webhooks, scores and
classifies every inbound lead (**Hot/Warm/Cold**, *distributor* vs
*end-customer*), enriches the CRM, fires **WhatsApp** alerts, and enforces
pipeline hygiene (**zero leads unscored >24h**).

Built with FastAPI + LangChain, SQLAlchemy, Celery, and deployable to Railway.

See `plan.md` (build plan) and `Agent.md` (behavioral spec).

## Quick start (local, zero credentials)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ADAPTER_MODE=mock          # integrations stay mocked (no CRM/WhatsApp needed)
export OPENAI_API_KEY=sk-...      # LLM scoring activates automatically on key presence
python scripts/simulate.py        # scores via the LLM; CRM/WhatsApp are log-stubs
```


Then inspect the audit (admin token from `.env`):

```bash
curl -H "Authorization: Bearer change-me" localhost:8000/audit
```

## Run the API server

```bash
uvicorn app.main:app --reload --port 8000
# POST a webhook:  curl -X POST localhost:8000/webhook/monday -d '...'
# health:          curl localhost:8000/health
```

## Tests

```bash
pytest -q
```

Runs fully offline (mock adapters, SQLite, no Redis/LLM). Includes an eval
harness (`tests/golden/leads.json`) that gates scoring quality.

## Live mode (real integrations)

1. Copy `.env.example` → `.env`.
2. Set `ADAPTER_MODE=live`, fill `MONDAY_*` and `WHATSAPP_*` (or `TWILIO_*`).
3. Set `WEBHOOK_SECRET`, `ADMIN_TOKEN`, `OPENAI_API_KEY`, `DATABASE_URL`
   (Postgres for prod).
4. Run Celery worker for the hygiene sweep:
   ```bash
   celery -A app.scheduler.celery_app.celery_app worker --beat
   ```

## Architecture (nine-layer, see Agent.md)

- **Channels:** monday.com webhook (in) · WhatsApp (out) · admin REST.
- **Orchestration:** `app/services/lead_service.py` (parse→store→score→enrich→alert).
- **LLM gateway:** `app/services/llm_gateway.py` (LangChain + deterministic fallback).
- **Adapters:** `Mock*`/`Live*` for monday + WhatsApp — same core, swappable transport.
- **Storage:** SQLAlchemy (SQLite dev / Postgres prod) + `EventLog` audit trail.
- **Scheduler:** plain `run_hygiene()` wrapped by Celery beat.
- **Observability:** request-id structured logs + `/audit` SLA report.
- **Security:** webhook HMAC verify, admin Bearer token, PII redaction, right-to-erasure.

## Deploy (Railway)

- Web service: this image, command = web (uvicorn).
- Worker service: same image, `ROLE=worker` (Celery beat).
- Add-ons: Postgres + Redis. Map env vars from `.env.example`.
