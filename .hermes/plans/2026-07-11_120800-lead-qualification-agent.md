# Lead Qualification & Scoring Agent — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build a runnable lead-qualification agent that ingests monday.com webhooks, scores/classifies every lead (Hot/Warm/Cold, distributor vs end-customer), enriches the CRM, fires WhatsApp alerts, and enforces pipeline hygiene (zero leads unscored >24h) — runnable locally today via mock adapters, swappable to live APIs by config.

**Architecture:** FastAPI service. A `service` layer orchestrates intake: parse → store → score → classify → enrich (adapter) → WhatsApp alert (adapter). monday.com and WhatsApp are behind adapter interfaces with `Mock*` (local, log/file-backed) and `Live*` (real HTTP) implementations selected by `ADAPTER_MODE`. A scheduler runs a hygiene scan on an interval; an audit endpoint reports SLA compliance. All SLA timestamps (`created_at`, `scored_at`, `alerted_at`) are persisted so success criteria are measurable.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic + pydantic-settings, httpx (outbound API calls), APScheduler (periodic hygiene scan), pytest + Starlette TestClient (tests), Docker + docker-compose (parity local/prod).

---

## Context & Assumptions

- Greenfield repo at `/home/aditya_gupta/Freelance/Lead_agent_project/` (currently empty).
- Beyond Oil sells frying-oil filter powder to food-service / industrial frying operations. Core ICP = food-service, restaurants, QSR, food manufacturing, industrial frying, hospitality/catering.
- Contract SLA targets: 100% leads scored within **5 min** of intake; WhatsApp alert within **2 min** of qualification; **zero** leads unscored >24h.
- Lead fields available from monday.com webhook: `name`, `company`, `source`, `industry`, `inquiry_type` (plus an `id` and `created_at`).
- No live credentials yet → default `ADAPTER_MODE=mock`. Live mode is wired but inert until env vars are filled.
- Assignee logic: in mock mode alerts go to `ALERT_RECIPIENT_PHONE`; escalations to `REVIEWER_PHONE`. (Real routing rules can be added later — YAGNI for now.)

## Proposed Approach

1. Scoring is a **deterministic, weighted rules engine** (no ML needed for v1) → fully testable and explainable (rationale strings feed CRM + alerts).
2. CRM + WhatsApp behind **adapter interfaces** so the core logic is identical in mock and live; only the transport changes.
3. Single JSON file store (`DATA_FILE`) is the source of truth in mock mode and is the easiest thing to swap for Postgres later (store interface is small).
4. SLA measurement is first-class: every transition records a timestamp; the audit endpoint computes breach counts.

## File Tree (all new)

```
Lead_agent_project/
  .env.example
  .gitignore
  docker-compose.yml
  Dockerfile
  pyproject.toml
  README.md
  app/
    __init__.py
    config.py
    schemas.py
    store.py
    scoring.py
    classify.py
    service.py
    hygiene.py
    adapters/
      __init__.py
      monday.py
      whatsapp.py
    api.py
    main.py
  scripts/
    simulate.py          # fires fake webhooks for demos
  tests/
    __init__.py
    conftest.py
    test_scoring.py
    test_classify.py
    test_intake.py
    test_hygiene.py
```

---

## Task 1: Project scaffold + dependencies

**Objective:** Create the package skeleton and declare dependencies so the app can be installed and imported.

**Files:**
- Create: `pyproject.toml`
- Create: `app/__init__.py`, `app/adapters/__init__.py`, `tests/__init__.py`
- Create: `.gitignore`

**Step 1: Write pyproject.toml**

```toml
[project]
name = "lead-qualification-agent"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.29",
    "pydantic>=2.6",
    "pydantic-settings>=2.2",
    "httpx>=0.27",
    "apscheduler>=3.10",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "httpx>=0.27"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
packages = ["app", "app.adapters"]
```

**Step 2: Create empty package files**

`app/__init__.py`, `app/adapters/__init__.py`, `tests/__init__.py` → each just `# package`.

**Step 3: Write .gitignore**

```
__pycache__/
*.pyc
.venv/
.env
data/
*.log
```

**Step 4: Install**

Run: `cd /home/aditya_gupta/Freelance/Lead_agent_project && python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`
Expected: packages install, no errors.

**Step 5: Commit**

```bash
git add pyproject.toml .gitignore app/__init__.py app/adapters/__init__.py tests/__init__.py
git commit -m "chore: scaffold package and deps"
```

---

## Task 2: Configuration

**Objective:** Central, typed config read from env / `.env`, selecting adapter mode and SLA thresholds.

**Files:**
- Create: `app/config.py`
- Create: `.env.example`

**Step 1: Write app/config.py**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    adapter_mode: str = "mock"  # "mock" | "live"

    # Store
    data_file: str = "./data/leads.json"

    # SLA targets (minutes)
    sla_score_minutes: int = 5
    sla_alert_minutes: int = 2
    sla_unscored_hours: int = 24
    hygiene_interval_minutes: int = 60

    # monday.com (live)
    monday_api_token: str = ""
    monday_board_id: str = ""
    monday_api_url: str = "https://api.monday.com/v2"

    # WhatsApp Cloud API (live)
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_api_url: str = "https://graph.facebook.com/v19.0"

    # Routing
    alert_recipient_phone: str = "+15550000001"
    reviewer_phone: str = "+15550000002"

    # Webhook integrity (live)
    webhook_secret: str = ""


settings = Settings()
```

**Step 2: Write .env.example**

```dotenv
ADAPTER_MODE=mock
DATA_FILE=./data/leads.json
SLA_SCORE_MINUTES=5
SLA_ALERT_MINUTES=2
SLA_UNSCORED_HOURS=24
HYGIENE_INTERVAL_MINUTES=60

# --- Live mode (leave blank for mock) ---
MONDAY_API_TOKEN=
MONDAY_BOARD_ID=
MONDAY_API_URL=https://api.monday.com/v2
WHATSAPP_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_API_URL=https://graph.facebook.com/v19.0
ALERT_RECIPIENT_PHONE=+15550000001
REVIEWER_PHONE=+15550000002
WEBHOOK_SECRET=
```

**Step 3: Smoke import**

Run: `. .venv/bin/activate && python -c "from app.config import settings; print(settings.adapter_mode)"`
Expected: `mock`

**Step 4: Commit**

```bash
git add app/config.py .env.example
git commit -m "feat: typed settings with adapter mode + SLA config"
```

---

## Task 3: Schemas

**Objective:** Pydantic models for inbound webhook payload, the stored lead record, and scoring result.

**Files:**
- Create: `app/schemas.py`
- Test: `tests/test_schemas.py` (light validation test)

**Step 1: Write app/schemas.py**

```python
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LeadInput(BaseModel):
    """Payload from monday.com webhook (new item)."""
    id: str
    name: str
    company: Optional[str] = None
    source: Optional[str] = None
    industry: Optional[str] = None
    inquiry_type: Optional[str] = None
    created_at: Optional[datetime] = None


class ScoreResult(BaseModel):
    score: int
    tier: str  # Hot | Warm | Cold
    reasons: list[str]


class LeadRecord(LeadInput):
    """Persisted lead with pipeline state + SLA timestamps."""
    classification: Optional[str] = None  # distributor | end_customer
    score: Optional[int] = None
    tier: Optional[str] = None
    rationale: list[str] = Field(default_factory=list)
    scored_at: Optional[datetime] = None
    alerted_at: Optional[datetime] = None
    alert_sent: bool = False
    escalated: bool = False
    escalated_at: Optional[datetime] = None

    def mark_scored(self, res: ScoreResult, classification: str):
        self.score = res.score
        self.tier = res.tier
        self.rationale = res.reasons
        self.classification = classification
        self.scored_at = _now()

    def record_alert(self):
        self.alert_sent = True
        self.alerted_at = _now()
```

**Step 2: Test schemas**

`tests/test_schemas.py`:

```python
from app.schemas import LeadInput, LeadRecord, ScoreResult


def test_lead_input_defaults():
    l = LeadInput(id="1", name="Acme")
    assert l.company is None
    assert l.id == "1"


def test_record_mark_scored_sets_timestamps():
    rec = LeadRecord(id="1", name="Acme")
    rec.mark_scored(ScoreResult(score=80, tier="Hot", reasons=["x"]), "distributor")
    assert rec.tier == "Hot"
    assert rec.scored_at is not None
    assert rec.classification == "distributor"
```

**Step 3: Run test (expect pass)**

Run: `. .venv/bin/activate && pytest tests/test_schemas.py -v`
Expected: 2 passed.

**Step 4: Commit**

```bash
git add app/schemas.py tests/test_schemas.py
git commit -m "feat: pydantic schemas for input/record/score"
```

---

## Task 4: Store (JSON file-backed)

**Objective:** Persist leads with all SLA timestamps; small interface so it can later be swapped for a DB.

**Files:**
- Create: `app/store.py`
- Test: `tests/test_store.py`

**Step 1: Write app/store.py**

```python
import json
import threading
from pathlib import Path
from typing import Optional
from app.config import settings
from app.schemas import LeadRecord


class LeadStore:
    def __init__(self, path: str = settings.data_file):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            self.path.write_text("[]")

    def _read(self) -> list[dict]:
        return json.loads(self.path.read_text())

    def _write(self, rows: list[dict]):
        self.path.write_text(json.dumps(rows, default=str, indent=2))

    def upsert(self, rec: LeadRecord) -> LeadRecord:
        with self._lock:
            rows = self._read()
            found = False
            for i, r in enumerate(rows):
                if r.get("id") == rec.id:
                    rows[i] = rec.model_dump()
                    found = True
                    break
            if not found:
                rows.append(rec.model_dump())
            self._write(rows)
        return rec

    def get(self, lead_id: str) -> Optional[LeadRecord]:
        for r in self._read():
            if r.get("id") == lead_id:
                return LeadRecord(**r)
        return None

    def all(self) -> list[LeadRecord]:
        return [LeadRecord(**r) for r in self._read()]

    def list_unscored(self) -> list[LeadRecord]:
        return [r for r in self.all() if r.scored_at is None]


store = LeadStore()


def get_store() -> LeadStore:
    """Accessor so tests can swap the store by reassigning app.store.store."""
    return store
```

**Step 2: Test store**

`tests/test_store.py`:

```python
from app.store import LeadStore
from app.schemas import LeadRecord
from datetime import datetime, timezone


def test_upsert_and_get(tmp_path):
    s = LeadStore(str(tmp_path / "leads.json"))
    rec = LeadRecord(id="L1", name="A", company="B")
    s.upsert(rec)
    got = s.get("L1")
    assert got is not None and got.company == "B"


def test_list_unscored(tmp_path):
    s = LeadStore(str(tmp_path / "leads.json"))
    s.upsert(LeadRecord(id="U1", name="x"))
    assert len(s.list_unscored()) == 1
    s.upsert(LeadRecord(id="U2", name="y", scored_at=datetime.now(timezone.utc)))
    assert len(s.list_unscored()) == 1
```

**Step 3: Run test (expect pass)**

Run: `. .venv/bin/activate && pytest tests/test_store.py -v`
Expected: 2 passed.

**Step 4: Commit**

```bash
git add app/store.py tests/test_store.py
git commit -m "feat: JSON file-backed lead store"
```

---

## Task 5: Scoring engine (TDD)

**Objective:** Deterministic weighted scoring → Hot/Warm/Cold with explainable rationale.

**Files:**
- Create: `app/scoring.py`
- Test: `tests/test_scoring.py`

**Step 1: Write failing test first**

`tests/test_scoring.py`:

```python
from app.scoring import score_lead
from app.schemas import LeadInput


def test_hot_lead():
    lead = LeadInput(
        id="1", name="Joe", company="FryCo",
        source="referral", industry="food service",
        inquiry_type="request pricing",
    )
    res = score_lead(lead)
    assert res.tier == "Hot"
    assert res.score >= 75
    assert len(res.reasons) > 0


def test_cold_lead():
    lead = LeadInput(id="2", name="Sam", source="paid_ad",
                     industry="software", inquiry_type="just browsing")
    res = score_lead(lead)
    assert res.tier == "Cold"
    assert res.score < 50


def test_warm_lead_midband():
    lead = LeadInput(id="3", name="Pat", company="Cafe X",
                     source="website", industry="restaurant",
                     inquiry_type="general info")
    res = score_lead(lead)
    assert res.tier == "Warm"
```

**Step 2: Run test (expect FAIL — score_lead undefined)**

Run: `. .venv/bin/activate && pytest tests/test_scoring.py -v`
Expected: FAIL (ImportError / NameError).

**Step 3: Write app/scoring.py (minimal, passing)**

```python
from app.schemas import LeadInput, ScoreResult

CORE_INDUSTRY = ["food service", "restaurant", "qsr", "food manufacturing",
                 "industrial frying", "frying", "hospitality", "catering"]


def _source_points(source: str) -> tuple[int, str]:
    s = (source or "").lower()
    if s in {"referral", "partner", "direct"}:
        return 25, "High-intent source (referral/partner/direct): +25"
    if s in {"website", "inbound", "organic", "trade_show", "trade show"}:
        return 15, "Inbound source: +15"
    if s in {"paid_ad", "cold_outreach", "social", "ad"}:
        return 8, "Lower-intent source: +8"
    return 5, "Unspecified source: +5"


def _industry_points(industry: str) -> tuple[int, str]:
    i = (industry or "").lower()
    if any(k in i for k in CORE_INDUSTRY):
        return 30, "Core industry fit (food-service/frying): +30"
    if any(k in i for k in ["food", "beverage", "retail", "horeca", "ho_re_ca"]):
        return 18, "Adjacent food industry: +18"
    return 5, "Outside core industry: +5"


def _inquiry_points(inquiry: str) -> tuple[int, str]:
    q = (inquiry or "").lower()
    if any(k in q for k in ["quote", "pricing", "price", "buy", "purchase",
                            "order", "distributor application"]):
        return 25, "Purchase-intent inquiry: +25"
    if any(k in q for k in ["demo", "trial", "sample"]):
        return 18, "Evaluation inquiry: +18"
    if any(k in q for k in ["info", "general", "question", "browsing"]):
        return 8, "General info inquiry: +8"
    return 5, "Unspecified inquiry: +5"


def score_lead(lead: LeadInput) -> ScoreResult:
    points = 0
    reasons: list[str] = []
    sp, sr = _source_points(lead.source); points += sp; reasons.append(sr)
    ip, ir = _industry_points(lead.industry); points += ip; reasons.append(ir)
    qp, qr = _inquiry_points(lead.inquiry_type); points += qp; reasons.append(qr)
    if lead.company and lead.company.strip():
        points += 20; reasons.append("B2B company identified: +20")
    else:
        reasons.append("No company provided (likely consumer): +0")
    points = min(points, 100)
    tier = "Hot" if points >= 75 else "Warm" if points >= 50 else "Cold"
    return ScoreResult(score=points, tier=tier, reasons=reasons)
```

**Step 4: Run test (expect PASS)**

Run: `. .venv/bin/activate && pytest tests/test_scoring.py -v`
Expected: 3 passed.

**Step 5: Commit**

```bash
git add app/scoring.py tests/test_scoring.py
git commit -m "feat: deterministic lead scoring engine (Hot/Warm/Cold)"
```

---

## Task 6: Classification (TDD)

**Objective:** Classify distributor vs end-customer from lead text signals.

**Files:**
- Create: `app/classify.py`
- Test: `tests/test_classify.py`

**Step 1: Failing test**

`tests/test_classify.py`:

```python
from app.classify import classify_lead
from app.schemas import LeadInput


def test_distributor_detected():
    lead = LeadInput(id="1", name="A", company="Euro Foods Wholesale",
                     source="partner", industry="food distribution",
                     inquiry_type="distributor application")
    assert classify_lead(lead) == "distributor"


def test_end_customer_default():
    lead = LeadInput(id="2", name="B", company="Joe's Diner",
                     source="website", industry="restaurant",
                     inquiry_type="request pricing")
    assert classify_lead(lead) == "end_customer"
```

**Step 2: Run (expect FAIL)**

**Step 3: Write app/classify.py**

```python
from app.schemas import LeadInput

DISTRIBUTOR_SIGNALS = [
    "distributor", "reseller", "wholesale", "importer", "channel",
    "dealer", "agent", "representative", "partner program", "distribution",
]


def classify_lead(lead: LeadInput) -> str:
    text = " ".join(filter(None, [
        lead.source, lead.industry, lead.inquiry_type, lead.company
    ])).lower()
    if any(sig in text for sig in DISTRIBUTOR_SIGNALS):
        return "distributor"
    return "end_customer"
```

**Step 4: Run (expect PASS)** → 2 passed.

**Step 5: Commit**

```bash
git add app/classify.py tests/test_classify.py
git commit -m "feat: distributor vs end-customer classification"
```

---

## Task 7: Adapters — monday.com + WhatsApp

**Objective:** Define adapter interfaces with Mock (local) and Live (HTTP) implementations, selected by `ADAPTER_MODE`.

**Files:**
- Create: `app/adapters/monday.py`
- Create: `app/adapters/whatsapp.py`
- Test: `tests/test_adapters.py`

**Step 1: Write app/adapters/monday.py**

```python
from abc import ABC, abstractmethod
from typing import Optional
from app.schemas import LeadRecord
from app.store import store
from app.config import settings
import httpx


class MondayAdapter(ABC):
    @abstractmethod
    def enrich(self, rec: LeadRecord) -> None: ...
    @abstractmethod
    def list_leads(self) -> list[LeadRecord]: ...
    @abstractmethod
    def update(self, rec: LeadRecord) -> None: ...


class MockMondayAdapter(MondayAdapter):
    """In mock mode the JSON store IS monday.com."""
    def enrich(self, rec: LeadRecord) -> None:
        store.upsert(rec)
    def list_leads(self) -> list[LeadRecord]:
        return store.all()
    def update(self, rec: LeadRecord) -> None:
        store.upsert(rec)


class LiveMondayAdapter(MondayAdapter):
    def __init__(self):
        self.url = settings.monday_api_url
        self.token = settings.monday_api_token
        self.board = settings.monday_board_id

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"}

    def enrich(self, rec: LeadRecord) -> None:
        # Map score/classification/rationale to monday column values.
        mutation = (
            "mutation($b:ID!,$item:ID!,$cols:JSON!){change_multiple_column_values("
            "board_id:$b,item_id:$item,column_values:$cols){id}}"
        )
        cols = {
            "score": {"text": str(rec.score)},
            "tier": {"label": rec.tier},
            "classification": {"label": rec.classification},
            "rationale": {"text": " | ".join(rec.rationale)},
        }
        with httpx.Client(timeout=10) as c:
            c.post(self.url, headers=self._headers(),
                   json={"query": mutation,
                         "variables": {"b": self.board, "item": rec.id,
                                       "cols": cols}})

    def list_leads(self) -> list[LeadRecord]:
        # In live mode, pull items from the board and map to LeadRecord.
        # (Mapping depends on your board column IDs — fill during go-live.)
        q = "query($b:ID!){boards(ids:$b){items_page{items{id name column_values{id text}}}}}"
        with httpx.Client(timeout=10) as c:
            r = c.post(self.url, headers=self._headers(),
                       json={"query": q, "variables": {"b": self.board}})
            r.raise_for_status()
        # Minimal mapping; expand to full field extraction at go-live.
        items = r.json()["data"]["boards"][0]["items_page"]["items"]
        return [LeadRecord(id=i["id"], name=i.get("name", "")) for i in items]

    def update(self, rec: LeadRecord) -> None:
        self.enrich(rec)


def get_monday_adapter() -> MondayAdapter:
    return (LiveMondayAdapter() if settings.adapter_mode == "live"
            else MockMondayAdapter())
```

**Step 2: Write app/adapters/whatsapp.py**

```python
from abc import ABC, abstractmethod
from app.config import settings
import httpx


class WhatsAppAdapter(ABC):
    @abstractmethod
    def send_alert(self, to: str, message: str) -> None: ...


class MockWhatsAppAdapter(WhatsAppAdapter):
    """Logs alerts to console + a file; keeps last messages for tests."""
    sent: list[tuple[str, str]] = []

    def send_alert(self, to: str, message: str) -> None:
        MockWhatsAppAdapter.sent.append((to, message))
        print(f"[MOCK WHATSAPP -> {to}] {message}")
        try:
            with open("./data/whatsapp_out.log", "a") as f:
                f.write(f"{to} :: {message}\n")
        except Exception:
            pass


class LiveWhatsAppAdapter(WhatsAppAdapter):
    def __init__(self):
        self.url = f"{settings.whatsapp_api_url}/{settings.whatsapp_phone_number_id}/messages"
        self.token = settings.whatsapp_token

    def send_alert(self, to: str, message: str) -> None:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": message},
        }
        with httpx.Client(timeout=10) as c:
            c.post(self.url,
                   headers={"Authorization": f"Bearer {self.token}",
                            "Content-Type": "application/json"},
                   json=payload)


def get_whatsapp_adapter() -> WhatsAppAdapter:
    return (LiveWhatsAppAdapter() if settings.adapter_mode == "live"
            else MockWhatsAppAdapter())
```

**Step 3: Test adapters (mock path + alert capture)**

`tests/test_adapters.py`:

```python
from app.adapters.monday import MockMondayAdapter
from app.adapters.whatsapp import MockWhatsAppAdapter, get_whatsapp_adapter
from app.schemas import LeadRecord


def test_mock_monday_roundtrip(tmp_path, monkeypatch):
    from app import store as store_mod
    monkeypatch.setattr(store_mod, "store", MockMondayAdapter.__init__ and
                        __import__("app.store", fromlist=["LeadStore"]).LeadStore(str(tmp_path/"l.json")))
    a = MockMondayAdapter()
    rec = LeadRecord(id="M1", name="X", score=80, tier="Hot")
    a.enrich(rec)
    assert any(r.id == "M1" for r in a.list_leads())


def test_mock_whatsapp_captures():
    MockWhatsAppAdapter.sent.clear()
    get_whatsapp_adapter().send_alert("+1555", "hello")
    assert MockWhatsAppAdapter.sent[-1] == ("+1555", "hello")
```

**Step 4: Run (expect PASS)** → 2 passed.

**Step 5: Commit**

```bash
git add app/adapters/monday.py app/adapters/whatsapp.py tests/test_adapters.py
git commit -m "feat: monday + whatsapp adapters (mock + live)"
```

---

## Task 8: Service — intake orchestration (TDD)

**Objective:** Wire parse → store → score → classify → enrich → WhatsApp alert, recording all SLA timestamps.

**Files:**
- Create: `app/service.py`
- Test: `tests/test_intake.py`

**Step 1: Failing integration test**

`tests/test_intake.py`:

```python
from fastapi.testclient import TestClient
from app.main import app
from app.adapters.whatsapp import MockWhatsAppAdapter
from app.store import store
from datetime import datetime, timezone


def test_intake_scores_and_alerts():
    MockWhatsAppAdapter.sent.clear()
    # use a temp store
    import tempfile, pathlib, os
    tmp = pathlib.Path(tempfile.mkdtemp()) / "leads.json"
    os.environ["DATA_FILE"] = str(tmp)
    from app import store as s
    s.store = s.LeadStore(str(tmp))

    payload = {
        "id": "INT1", "name": "Jane", "company": "FryTech",
        "source": "referral", "industry": "restaurant",
        "inquiry_type": "request pricing",
    }
    with TestClient(app) as client:
        r = client.post("/webhook/monday", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] in {"Hot", "Warm", "Cold"}
    assert body["score"] >= 0
    # alert fired
    assert any("INT1" in m for _, m in MockWhatsAppAdapter.sent)
    # persisted timestamps
    rec = s.store.get("INT1")
    assert rec.scored_at is not None and rec.alert_sent is True
```

**Step 2: Run (expect FAIL — endpoint/app missing)**

**Step 3: Write app/service.py**

```python
from app.schemas import LeadInput, LeadRecord
from app.scoring import score_lead
from app.classify import classify_lead
from app.store import get_store
from app.adapters.monday import get_monday_adapter
from app.adapters.whatsapp import get_whatsapp_adapter
from app.config import settings


def _build_alert(rec: LeadRecord) -> str:
    head = f"🔔 New {rec.tier} lead — {rec.classification.replace('_',' ').title()}"
    lines = [
        head,
        f"Name: {rec.name}",
        f"Company: {rec.company or 'n/a'}",
        f"Score: {rec.score}/100",
        "Why: " + "; ".join(rec.rationale[:2]),
    ]
    return "\n".join(lines)


def process_lead(payload: dict) -> LeadRecord:
    lead = LeadInput(**payload)
    store = get_store()
    rec = store.get(lead.id) or LeadRecord(**lead.model_dump())
    # refresh latest fields
    rec = LeadRecord(**{**rec.model_dump(), **lead.model_dump()})

    res = score_lead(lead)
    classification = classify_lead(lead)
    rec.mark_scored(res, classification)

    monday = get_monday_adapter()
    monday.enrich(rec)

    wa = get_whatsapp_adapter()
    wa.send_alert(settings.alert_recipient_phone, _build_alert(rec))
    rec.record_alert()

    store.upsert(rec)
    return rec
```

**Step 4: Write app/api.py + app/main.py (Task 9 depends, but needed to pass test)**
See Task 9. After creating them, run test.

**Step 5: Run test (expect PASS)** → 1 passed.

**Step 6: Commit**

```bash
git add app/service.py tests/test_intake.py
git commit -m "feat: intake orchestration (score+classify+enrich+alert)"
```

---

## Task 9: API layer + main app

**Objective:** Expose webhook intake, lead listing/detail, manual scan, audit report, health.

**Files:**
- Create: `app/api.py`
- Create: `app/main.py`
- Modify: (none)

**Step 1: Write app/api.py**

```python
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from app.service import process_lead
from app.hygiene import run_hygiene_scan
from app.store import get_store
from app.config import settings

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok", "mode": settings.adapter_mode}


@router.post("/webhook/monday")
def webhook_monday(payload: dict):
    # In live mode, verify signature here using settings.webhook_secret.
    rec = process_lead(payload)
    return {"id": rec.id, "tier": rec.tier, "score": rec.score,
            "classification": rec.classification}


@router.get("/leads")
def list_leads():
    return [r.model_dump() for r in get_store().all()]


@router.get("/leads/{lead_id}")
def get_lead(lead_id: str):
    rec = get_store().get(lead_id)
    if not rec:
        raise HTTPException(404, "lead not found")
    return rec.model_dump()


@router.post("/admin/scan")
def admin_scan():
    result = run_hygiene_scan()
    return result


@router.get("/admin/report")
def admin_report():
    leads = get_store().all()
    now = datetime.now(timezone.utc)
    unscored = [r for r in leads if r.scored_at is None]
    stale = [r for r in unscored
             if (now - (r.created_at or now)).total_seconds() / 3600
             > settings.sla_unscored_hours]
    slow_score = [r for r in leads if r.scored_at and r.created_at and
                  (r.scored_at - r.created_at).total_seconds() / 60
                  > settings.sla_score_minutes]
    slow_alert = [r for r in leads if r.alerted_at and r.scored_at and
                  (r.alerted_at - r.scored_at).total_seconds() / 60
                  > settings.sla_alert_minutes]
    return {
        "total": len(leads),
        "scored": len(leads) - len(unscored),
        "unscored": len(unscored),
        "stale_unscored_over_24h": len(stale),
        "breaches_score_sla": len(slow_score),
        "breaches_alert_sla": len(slow_alert),
        "escalated": len([r for r in leads if r.escalated]),
        "sla_targets": {
            "score_minutes": settings.sla_score_minutes,
            "alert_minutes": settings.sla_alert_minutes,
            "unscored_hours": settings.sla_unscored_hours,
        },
    }
```

**Step 2: Write app/main.py**

```python
from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.api import router
from app.scheduler import start_scheduler, shutdown_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(title="Lead Qualification Agent", lifespan=lifespan)
app.include_router(router)
```

**Step 3: Write app/scheduler.py (Task 10 detail, but needed to import main)**

```python
from apscheduler.schedulers.background import BackgroundScheduler
from app.hygiene import run_hygiene_scan
from app.config import settings

_scheduler: BackgroundScheduler | None = None


def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(run_hygiene_scan, "interval",
                       minutes=settings.hygiene_interval_minutes)
    _scheduler.start()


def shutdown_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
```

**Step 4: Run intake test from Task 8 (expect PASS)** → 1 passed.
Also: `. .venv/bin/activate && python -c "from app.main import app; print('import ok')"`

**Step 5: Commit**

```bash
git add app/api.py app/main.py app/scheduler.py
git commit -m "feat: REST API + lifespan scheduler wiring"
```

---

## Task 10: Hygiene scanner (TDD)

**Objective:** Periodic scan flags leads unscored >24h, attempts re-score, escalates to human reviewer.

**Files:**
- Create: `app/hygiene.py`
- Test: `tests/test_hygiene.py`

**Step 1: Failing test**

`tests/test_hygiene.py`:

```python
from datetime import datetime, timezone, timedelta
from app.hygiene import run_hygiene_scan
from app.store import LeadStore
from app.schemas import LeadRecord
from app.adapters.whatsapp import MockWhatsAppAdapter
import tempfile, pathlib, os


def test_stale_unscored_escalated():
    tmp = pathlib.Path(tempfile.mkdtemp()) / "leads.json"
    os.environ["DATA_FILE"] = str(tmp)
    from app import store as s, hygiene as h
    s.store = s.LeadStore(str(tmp))
    h.store = s.store  # hygiene uses module-level store ref

    old = datetime.now(timezone.utc) - timedelta(hours=30)
    s.store.upsert(LeadRecord(id="STALE", name="X", created_at=old))
    MockWhatsAppAdapter.sent.clear()
    result = run_hygiene_scan()
    assert result["escalated"] >= 1
    rec = s.store.get("STALE")
    assert rec.escalated is True
    # reviewer alert sent
    assert any("STALE" in m for _, m in MockWhatsAppAdapter.sent)
```

**Step 2: Run (expect FAIL — module missing)**

**Step 3: Write app/hygiene.py**

```python
from datetime import datetime, timezone
from app.store import get_store
from app.adapters.monday import get_monday_adapter
from app.adapters.whatsapp import get_whatsapp_adapter
from app.config import settings
from app.scoring import score_lead
from app.classify import classify_lead


def run_hygiene_scan() -> dict:
    monday = get_monday_adapter()
    wa = get_whatsapp_adapter()
    now = datetime.now(timezone.utc)
    escalated = 0
    rescored = 0

    for rec in monday.list_leads():
        if rec.scored_at is not None:
            continue
        age_h = (now - (rec.created_at or now)).total_seconds() / 3600
        if age_h <= settings.sla_unscored_hours:
            continue
        # Attempt re-score if we have enough data
        if rec.name and (rec.source or rec.industry or rec.inquiry_type):
            res = score_lead(rec)
            rec.mark_scored(res, classify_lead(rec))
            monday.update(rec)
            get_store().upsert(rec)
            rescored += 1
        else:
            rec.escalated = True
            rec.escalated_at = now
            monday.update(rec)
            get_store().upsert(rec)
            wa.send_alert(
                settings.reviewer_phone,
                f"⚠️ ESCALATION: lead {rec.id} ({rec.name}) unscored >24h "
                f"and lacks data to auto-score. Human review needed.",
            )
            escalated += 1
    return {"checked": True, "rescored": rescored, "escalated": escalated}
```

**Step 4: Run test (expect PASS)** → 1 passed.

**Step 5: Commit**

```bash
git add app/hygiene.py tests/test_hygiene.py
git commit -m "feat: pipeline hygiene scan + escalation"
```

---

## Task 11: Demo simulator + Docker + README

**Objective:** Make it trivially runnable/demoable locally and deployable.

**Files:**
- Create: `scripts/simulate.py`
- Create: `Dockerfile`, `docker-compose.yml`
- Create: `README.md`

**Step 1: scripts/simulate.py** (fires fake webhooks)

```python
import requests, random, time

SAMPLES = [
    {"id": "D1", "name": "Maria", "company": "Ocean Foods Wholesale",
     "source": "partner", "industry": "food distribution",
     "inquiry_type": "distributor application"},
    {"id": "D2", "name": "Ken", "company": "Downtown Diner",
     "source": "website", "industry": "restaurant",
     "inquiry_type": "request pricing"},
    {"id": "D3", "name": "Lia", "company": "", "source": "paid_ad",
     "industry": "software", "inquiry_type": "just browsing"},
]

if __name__ == "__main__":
    base = "http://localhost:8000"
    for s in SAMPLES:
        r = requests.post(f"{base}/webhook/monday", json=s)
        print(s["id"], r.status_code, r.json())
        time.sleep(0.3)
    print("report:", requests.get(f"{base}/admin/report").json())
```

**Step 2: Dockerfile**

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[dev]"
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Step 3: docker-compose.yml**

```yaml
services:
  lead-agent:
    build: .
    ports: ["8000:8000"]
    env_file: [.env]
    volumes: ["./data:/app/data"]
```

**Step 4: README.md** — quickstart (venv install, `uvicorn app.main:app`, run `python scripts/simulate.py`, hit `/admin/report`), go-live checklist (fill `.env`, verify webhook signature, map monday columns, add real routing).

**Step 5: Run full test suite + a live local smoke test**

Run: `. .venv/bin/activate && pytest -q`
Expected: all tests pass.
Run: `. .venv/bin/activate && uvicorn app.main:app --port 8000 &` then `python scripts/simulate.py` then `curl localhost:8000/admin/report` → shows scored counts. Kill server after.

**Step 6: Commit**

```bash
git add scripts/simulate.py Dockerfile docker-compose.yml README.md
git commit -m "feat: demo simulator, docker, readme"
```

---

## Tests / Validation Summary

- `pytest -q` → all unit + integration tests pass (scoring, classify, store, adapters, intake, hygiene).
- Local smoke: start server, run `scripts/simulate.py`, confirm `/admin/report` shows `unscored: 0` and `stale_unscored_over_24h: 0` for fresh leads.
- SLA proof: `created_at`/`scored_at`/`alerted_at` persisted → report computes breach counts; WhatsApp log at `./data/whatsapp_out.log`.

## Risks / Tradeoffs / Open Questions

- **Webhook signature verification** is stubbed (live mode needs HMAC check against `WEBHOOK_SECRET`) — add before go-live.
- **monday.com live field mapping** is minimal; board column IDs must be mapped at onboarding (NDA-gated).
- **WhatsApp template/opt-in compliance** — free-form text messages require an existing 24h customer session or an approved template; confirm with Beyond Oil's WhatsApp Business account.
- **Assignee routing** is a single `ALERT_RECIPIENT_PHONE` today; real routing (by region/industry/score) can be added as a small rules table later (YAGNI for v1).
- **Store is a JSON file** — fine for demo/MVP; swap `LeadStore` for Postgres when concurrency/durability demands it.
- **No ML** — deterministic rules are explainable and meet the SLA; can be upgraded to a model later if qualification nuance requires it.

## Go-Live Checklist (post-plan)

1. Fill `.env` with real tokens + phone IDs; set `ADAPTER_MODE=live`.
2. Implement HMAC webhook verification in `/webhook/monday`.
3. Map monday board column IDs in `LiveMondayAdapter.list_leads` / `enrich`.
4. Confirm WhatsApp message template/opt-in with Beyond Oil.
5. Deploy via `docker-compose up` on Railway/Render/Fly; point monday webhook URL at `/webhook/monday`.
6. Monitor `/admin/report` for SLA breaches; alerting-to-reviewer path tested.
