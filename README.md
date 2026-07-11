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
- **Scoring queue:** the webhook persists the lead and enqueues scoring on a
  Celery queue (Redis broker). The `score_and_deliver` task retries the LLM
  with exponential backoff (`SCORE_MAX_RETRIES`, `SCORE_RETRY_BACKOFF`) so
  transient LLM failures (timeouts, rate limits) are retried for a REAL score
  rather than silently degrading to the rules engine. Only after all retries
  are exhausted does it fall back to rules (lead is never left unscored).
  Without Redis, scoring runs inline (synchronous) so local dev needs no broker.
- **Security:** webhook HMAC verify, admin Bearer token, PII redaction, right-to-erasure.

## Deploy (Railway)

The repo is Railway-ready out of the box:

- `Dockerfile` builds one image; the `CMD` runs `web` (uvicorn) by default,
  or `worker` (Celery beat) when `ROLE=worker`.
- `Procfile` + `railway.toml` declare both services; Railway injects `PORT`
  and the Postgres/Redis add-on URLs as env vars.
- `.dockerignore` keeps secrets and local state out of the image.

Steps:
1. Push the repo to GitHub and create a Railway project from it.
2. Add two services from the same image:
   - **web** — no extra env (Dockerfile defaults to uvicorn).
   - **worker** — set `ROLE=worker` (Celery hygiene sweep).
3. Add the **Postgres** add-on; Railway sets `DATABASE_URL` automatically.
   Add **Redis** for the worker (`REDIS_URL`).
4. In the Railway **Variables** panel, set every value from `.env.example`:
   `ADAPTER_MODE=live`, `MONDAY_*`, `WHATSAPP_*` (or `TWILIO_*`),
   `OPENAI_API_KEY`, `LANGSMITH_*`, and the two secrets
   `WEBHOOK_SECRET` + `ADMIN_TOKEN`. Generate strong random values:
   `openssl rand -hex 24`.
5. Deploy. The web service health-checks `/health`.

## Deploy (Render)

The repo is Render-ready via `render.yaml` (Blueprint):

- `Dockerfile` builds one image; the `CMD` runs `web` (uvicorn on `$PORT`)
  by default, or `worker` (Celery beat) when `ROLE=worker`.
- `render.yaml` declares two services (web + worker) and two add-ons
  (Postgres + Redis). Redis is the Celery broker for the scoring retry queue;
  Postgres is the database (`DATABASE_URL` injected automatically).

Steps:
1. Push the repo to GitHub.
2. Render dashboard -> New -> Blueprint -> connect the repo. Render reads
   `render.yaml` and creates `lead-agent-web`, `lead-agent-worker`,
   `lead-agent-db` (Postgres), `lead-agent-redis` (Redis).
3. In each service's Environment, set the secret values (marked `sync: false`
   in `render.yaml`): `OPENAI_API_KEY`, `MONDAY_API_TOKEN`, `MONDAY_BOARD_ID`,
   `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `ALERT_RECIPIENT_PHONE`,
   `REVIEWER_PHONE`, `ADMIN_TOKEN`, `WEBHOOK_SECRET`, `LANGSMITH_API_KEY`.
   Generate strong random values: `openssl rand -hex 24`.
4. Deploy. The web service health-checks `/health`; the worker consumes the
   scoring queue from Redis.

Note: with no Redis (or no worker service) the app still runs — scoring falls
back to inline (synchronous), so local/dev and a web-only deploy work without
the queue, just without background retry.

## Go live end-to-end (automatic webhook)

So far leads are scored when you POST to `/webhook/monday` manually. To make
it automatic, register a monday webhook so new board items are scored without
you doing anything:

1. Expose the server publicly (any stable HTTPS URL monday can reach):
   - Local test: `cloudflared tunnel --url http://localhost:8000`
   - Railway: use the deployed service URL (stable, no tunnel needed).
2. Create the subscription (board 5029839272, event = create_item):
   ```bash
   python - <<'PY'
   import httpx, os
   from dotenv import load_dotenv; load_dotenv()
   TOKEN=os.getenv("MONDAY_API_TOKEN"); BID=os.getenv("MONDAY_BOARD_ID")
   URL="<YOUR_PUBLIC_URL>/webhook/monday"   # e.g. https://lead-agent-web.onrender.com/webhook/monday
   q='mutation($b:ID!,$u:String!,$e:WebhookEventType!){create_webhook(board_id:$b,url:$u,event:$e){id}}'
   r=httpx.post("https://api.monday.com/v2",
       json={"query":q,"variables":{"b":BID,"u":URL,"e":"create_item"}},
       headers={"Authorization":f"Bearer {TOKEN}"}, timeout=25)
   print(r.json())
   PY
   ```
   monday will GET `/webhook/monday?challenge=...` to verify, then start
   POSTing on every new item.
3. If you set `WEBHOOK_SECRET`, configure the **same** value as the webhook
   secret in monday (monday sends it as `X-Monday-Webhook-Secret`; the app
   HMAC-verifies the body). Leave it empty only for local dev.

After this, every new monday item is scored by the LLM, written back to the
board (Status/Score/Classification/Rationale), and you get a WhatsApp alert —
no manual step.

## Security notes

- `WEBHOOK_SECRET` enables HMAC verification of inbound webhooks (set in both
  `.env` and the monday webhook config).
- `ADMIN_TOKEN` protects `/leads` and `/audit` (send `Authorization: Bearer
  <token>`).
- All secrets live in env / Railway's secret store only — never in code or git.
