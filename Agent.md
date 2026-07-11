# Agent.md — Lead Qualification & Scoring Agent (Beyond Oil)

This is the behavioral spec for the agent. `app/services/lead_service.py` + `app/services/lead_scorer.py`
implement it. Keep this doc in sync with code.

---

## 1. Identity & Purpose

- **Who:** an autonomous lead-qualification agent operating inside Beyond Oil's monday.com pipeline.
- **What:** on every new lead, it scores, classifies, enriches the CRM, and alerts the right human —
  fast and without gaps.
- **Why:** the contract is **100% of leads scored within 5 min**, **alert within 2 min of
  qualification**, and **zero leads unscored beyond 24h**.

---

## 2. Trigger

- **Primary:** `POST /webhook/monday` — fired by monday.com when a new item (lead) is created.
- The endpoint verifies the webhook secret/HMAC, parses the payload, and returns `202 Accepted`
  immediately. Heavy work happens asynchronously in `LeadService.process_lead` so monday.com is
  never blocked.
- **Secondary (system):** Celery `hygiene_scan` task runs on a schedule to catch anything the
  real-time path missed.

---

## 3. Intake Workflow (per lead)

1. **Receive & verify** — validate signature; reject with `401` if invalid.
2. **Parse** — extract `id, name, company, source, industry, inquiry_type` (+ `created_at`).
   Missing fields default to `None`; never crash on partial payloads.
3. **Persist** — upsert `Lead` with `created_at = now()` (intake timestamp for SLA).
4. **Score & classify** — call the LangChain scorer (see §5). Result: `score (0-100)`,
   `tier (Hot|Warm|Cold)`, `classification (distributor|end_customer)`, `reasons[]`.
5. **Enrich CRM** — write score/tier/classification/rationale back to monday.com via
   `MondayClient.enrich()`. Log success/failure to `EventLog`.
6. **Alert** — send a WhatsApp message via `WhatsAppClient.alert()` to `ALERT_RECIPIENT_PHONE`.
   Record `alerted_at`. On delivery failure, retry with backoff; after max retries, escalate.
7. **Audit** — every transition writes a timestamp + an `EventLog` row (for retries/failures).

---

## 4. Classification Logic

- **distributor** — reseller/partner/inquiry about wholesale, distribution, or selling Beyond Oil
  (e.g. "want to distribute", "reseller", "bulk/wholesale").
- **end_customer** — direct food-service / industrial frying operation that will use the product
  (restaurant, QSR, hotel, food manufacturer, caterer).
- The LangChain prompt grounds this in Beyond Oil's ICP; output is validated against the closed
  set `{distributor, end_customer}` and rejected (→ fallback) if outside it.

---

## 5. Scoring Model (LangChain)

`app/services/lead_scorer.py` loads `app/prompts/qualification_prompt.txt` and runs a LangChain
`Runnable` with structured output:

```
Input:  lead fields (name, company, source, industry, inquiry_type)
Output: { "score": int 0-100,
          "tier":  "Hot"|"Warm"|"Cold",
          "classification": "distributor"|"end_customer",
          "reasons": [str, ...] }
```

**Tier bands (guidance given to the model, with few-shot examples):**
- **Hot (>=75):** core industry (food-service/restaurant/QSR/food-mfg/industrial frying) AND
  high-intent source (referral/partner/direct) AND concrete buying signal (pricing, demo, volume).
- **Warm (50–74):** core or adjacent industry with moderate intent (website/inbound, general info,
  sample request).
- **Cold (<50):** outside core industry, low-intent source (paid ad/cold/social), or vague
  ("just browsing").

**Fallback (deterministic):** if `ADAPTER_MODE=mock`, the LLM key is missing, or the model returns
invalid/unsupported JSON, a deterministic weighted rules engine produces the result so **100%
scoring is always met**. The fallback reuses the same tier bands and emits an explicit
`reasons` note ("rule-based fallback: LLM unavailable").

---

## 6. Alert Content (WhatsApp)

Short, actionable message to the assignee:
```
🔥 New {tier} lead — Beyond Oil
{name} · {company or "—"}
Type: {classification} | Source: {source}
Score: {score}/100
Why: {reasons joined}
CRM: {monday item link}
```
- **Hot** → immediate alert to `ALERT_RECIPIENT_PHONE`.
- **Warm/Cold** → logged + alerted (less urgent); routing can be tuned later.
- **Escalation** (score/alert failure, or hygiene breach) → alert `REVIEWER_PHONE` with context.

---

## 7. Pipeline Hygiene Loop (Celery)

- `hygiene_scan` runs every `HYGIENE_INTERVAL_MINUTES` (default 60).
- Queries `Lead` where `scored_at IS NULL` and `created_at < now - 24h`.
- For each: attempt re-score via `process_lead` path; on success, enrich + alert normally.
- On persistent failure: set `escalated=True`, `escalated_at=now()`, alert `REVIEWER_PHONE`.
- This guarantees the "zero unscored >24h" SLA even if the real-time webhook was missed/dropped.

---

## 8. Failure Handling & Retries

- **Webhook verify fail** → `401`, drop (monday.com can retry; we log).
- **monday enrich fail** → retry w/ backoff (3 tries); record each attempt in `EventLog`; do not
  block the alert.
- **WhatsApp send fail** → retry w/ backoff (3 tries); if still failing → escalate to reviewer.
- **Scorer fail** → deterministic fallback (never leave a lead unscored).
- Every retry/failure is an `EventLog` row: `(lead_id, event, status, detail, ts)`. `detail`
  redacts PII (phone/name).

---

## 9. Guardrails & Compliance

- **Webhook integrity:** verify secret/HMAC before processing (reject unsigned).
- **Output validation:** LLM output must match the schema + closed enum sets; otherwise fallback.
- **PII:** do not log names/phones in `EventLog.detail`; mask.
- **NDA/external:** this is an external engagement under NDA — store only necessary lead fields,
  keep credentials in env (never committed), and never echo secrets in responses/logs.
- **Idempotency:** lead upsert is keyed by monday `id`, so duplicate webhook deliveries don't
  double-score (re-score only updates; alert is suppressed if `alerted_at` already set).

---

## 10. Configuration (env)

| Var | Meaning |
|---|---|
| `ADAPTER_MODE` | `mock` (no creds, log-backed) | `live` |
| `ALERT_PROVIDER` | `whatsapp` (Cloud API) | `twilio` |
| `DATABASE_URL` | Postgres connection string |
| `REDIS_URL` | Celery broker |
| `SLA_SCORE_MINUTES` / `SLA_ALERT_MINUTES` / `SLA_UNSCORED_HOURS` | thresholds |
| `HYGIENE_INTERVAL_MINUTES` | Celery scan cadence |
| `MONDAY_API_TOKEN` / `MONDAY_BOARD_ID` / `MONDAY_API_URL` | live monday |
| `WHATSAPP_TOKEN` / `WHATSAPP_PHONE_NUMBER_ID` / `WHATSAPP_API_URL` | live Cloud API |
| `TWILIO_*` | if Twilio selected |
| `ALERT_RECIPIENT_PHONE` / `REVIEWER_PHONE` | routing |
| `WEBHOOK_SECRET` | monday webhook verification |
| `OPENAI_API_KEY` (or LangChain model key) | for live scoring |

Default `ADAPTER_MODE=mock` → whole pipeline runs with zero credentials (verifiable locally).

---

## 11. LLM Gateway & Eval Harness (gap #1, #2)

- **Single gateway:** all model calls go through `app/services/llm_gateway.py`. It owns the
  LangChain client, sets a timeout, records an audit row per call (model, latency_ms, tokens,
  prompt_version, fallback_used), and returns a validated `ScoreResult`. No other module imports
  the LLM SDK directly.
- **Keyless operation:** if no `OPENAI_API_KEY` (or `ADAPTER_MODE=mock`), the gateway uses a
  deterministic rules engine so 100% scoring still holds. The audit row records
  `fallback_used=true`.
- **Eval harness:** `tests/golden/leads.json` is a frozen, labeled set (tier + classification per
  lead). `tests/test_eval.py` asserts the scorer reproduces the labels above a pass threshold
  (e.g. >=90%). Prompt/model changes must not drop below threshold — this is the regression gate.

## 12. Observability (gap #3)

- Every request gets a `request_id` (from `X-Request-ID` header or generated). It is attached to
  the `LeadService` context and printed in every structured log line + `EventLog` row.
- Structured logging (`app/common/logging.py`) emits one line per transition:
  `intake | scored | enriched | alerted | escalated | retry | failure`, each with `request_id`,
  `lead_id`, latency, and outcome. No PII in log messages (names/phones masked).

## 13. Security Hardening (gap #4, #5, #7)

- **Admin auth:** `/leads` and `/audit` require `Authorization: Bearer <ADMIN_TOKEN>` (env). Public
  `/webhook/monday` and `/health` stay open but are secret/rate-gated.
- **Retention & deletion (NDA):** leads carry `created_at`; `anonymize_lead(lead_id)` clears PII
  fields and sets `deleted_at` on request (right-to-erasure). `EventLog` retains only non-PII
  metadata for audit.
- **Least privilege:** monday token scoped to the single board; WhatsApp token scoped to the phone
  number id. Documented in `.env.example`. Secrets live only in env / Railway secret store, never
  committed.

## 14. Success Criteria (how we know it works)

- `/audit` reports: 100% leads scored within `SLA_SCORE_MINUTES`; 100% alerts within
  `SLA_ALERT_MINUTES`; 0 leads `scored_at IS NULL` older than `SLA_UNSCORED_HOURS`.
- In mock mode, `scripts/simulate.py` + `pytest` demonstrate all of the above with no creds.
- In live mode (after creds added), same code path hits real monday.com + WhatsApp.
