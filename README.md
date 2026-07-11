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

## How it runs (architecture)

This is a **scheduled batch**, not an always-on server. Every hour a cron job:
1. fetches leads that still need scoring from monday.com (`fetch_due_leads` —
   items whose Status column is still empty),
2. scores each with the LLM (**retried with backoff** for a REAL score; only
   after retries are exhausted does it fall back to rules, so a lead is never
   left unscored),
3. writes Status/Score/Classification/Rationale back to the board and sends a
   WhatsApp alert.

monday.com is the **system of record** — the scored state lives in the board's
Status column, which is re-read every run, so no always-on database is needed.
The optional FastAPI webhook (below) is a *fast-path* for instant scoring; it
is not required for the hourly model.

## Deploy — Render Cron Job (cheapest, recommended)

`render.yaml` declares a single **Cron Job** (no 24/7 web/worker, no Redis,
no Postgres add-on) that runs `python scripts/cron_run.py` hourly. SQLite is
used locally as an audit log only.

Steps:
1. Push the repo to GitHub.
2. Render dashboard -> New -> Blueprint -> connect the repo. Render reads
   `render.yaml` and creates `lead-agent-cron` with schedule `0 * * * *`.
3. In the cron job's Environment, set the secret values (marked `sync: false`
   in `render.yaml`): `OPENAI_API_KEY`, `MONDAY_API_TOKEN`, `MONDAY_BOARD_ID`,
   `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `ALERT_RECIPIENT_PHONE`,
   `REVIEWER_PHONE`, `ADMIN_TOKEN`, `WEBHOOK_SECRET`, `LANGSMITH_API_KEY`.
   Generate strong random values: `openssl rand -hex 24`.
4. Deploy. The job runs hourly; check the run logs for `due=/scored=/failed=`.

Alternative (free, your own machine): run `python scripts/cron_run.py` from a
system cron (`0 * * * * cd /path && /path/.venv/bin/python scripts/cron_run.py`)
or `celery -A app.scheduler.celery_app.celery_app beat` for the queue variant.

## Deploy — Railway / Render web+worker (optional fast-path)

If you also want instant (event-driven) scoring via the monday webhook, the
web+worker layout is still supported (see `railway.toml` / the earlier web+
worker `render.yaml` shape). That needs Redis + Postgres and costs more. For
the hourly batch, the Cron Job above is sufficient and cheapest.

## Go live (webhook fast-path, optional)

The cron covers scoring automatically. To also score instantly on item create,
register a monday webhook so new board items POST to `/webhook/monday`:

1. Expose the web service publicly (Railway/Render web URL, or local
   `cloudflared tunnel --url http://localhost:8000`).
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

Without the webhook, the **hourly cron still scores every new lead** — just up
to an hour later. That is the default, cheapest setup.

## Security notes

- `WEBHOOK_SECRET` enables HMAC verification of inbound webhooks (set in both
  `.env` and the monday webhook config). Unused by the cron path.
- `ADMIN_TOKEN` protects `/leads` and `/audit` (send `Authorization: Bearer
***  <token>`).
- All secrets live in env / the platform's secret store only — never in code or git.
