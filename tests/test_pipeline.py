"""Unit + eval tests. Run with:  pytest -q  (no creds, no Redis, no LLM needed)

These exercise the REAL pipeline in mock mode using a temporary SQLite DB, so
the build is verifiable before any integration exists.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.database.session import Base, SessionLocal, engine
from app.models.schemas import LeadInput
from app.services.lead_service import intake_lead, score_and_deliver
from app.services.scoring import rules_score


@pytest.fixture()
def db():
    # Fresh in-memory-ish SQLite file per test for isolation.
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
    Base.metadata.drop_all(bind=engine)


# ---- scoring engine --------------------------------------------------------
def test_rules_hot():
    r = rules_score(LeadInput(id="1", name="x", company="FryCo",
                              source="referral", industry="food service",
                              inquiry_type="request pricing"))
    assert r.tier == "Hot"
    assert r.classification == "end_customer"


def test_rules_cold():
    r = rules_score(LeadInput(id="2", name="y", source="paid_ad",
                              industry="software", inquiry_type="just browsing"))
    assert r.tier == "Cold"


def test_enquiry_alias():
    """British-spelling 'enquiry' must be treated as inquiry_type."""
    lead = LeadInput(
        id="E1", name="Priya", company="Chips Ltd",
        source="website", industry="restaurant",
        enquiry="request demo",  # only the UK spelling provided
    )
    assert lead.inquiry_type == "request demo"
    r = rules_score(lead)
    assert r.tier == "Hot"        # demo signal should push Hot
    assert r.classification == "end_customer"


def test_rules_distributor():
    r = rules_score(LeadInput(id="3", name="z", company="Distro Inc",
                              source="website", industry="food distribution",
                              inquiry_type="want to distribute"))
    assert r.classification == "distributor"


# ---- full intake pipeline --------------------------------------------------
def test_intake_scores_and_alerts(db: Session):
    # Force mock adapters + rules scoring so the test is deterministic and
    # fully offline (no live WhatsApp/monday/LLM calls), regardless of .env.
    from app.config import settings
    prev_mode = settings.adapter_mode
    prev_llm = settings.use_llm
    settings.adapter_mode = "mock"
    settings.use_llm = False
    try:
        lead = LeadInput(id="L1", name="Joe", company="FryCo",
                         source="referral", industry="food service",
                         inquiry_type="request pricing")
        intake_lead(db, lead, request_id="t1")
        rec = score_and_deliver(db, "L1", request_id="t1")
        assert rec is not None
        assert rec.tier == "Hot"
        assert rec.alert_sent is True
        assert rec.scored_at is not None
        assert rec.alerted_at is not None
    finally:
        settings.adapter_mode = prev_mode
        settings.use_llm = prev_llm


# ---- golden eval -----------------------------------------------------------
def test_golden_eval():
    """Scorer must reproduce frozen labels at >= 90% (Agent.md §11)."""
    golden = json.loads((Path(__file__).parent / "golden" / "leads.json").read_text())
    passed = 0
    for case in golden:
        lead = LeadInput(id=case["id"], name=case["name"], company=case.get("company"),
                        source=case.get("source"), industry=case.get("industry"),
                        inquiry_type=case.get("inquiry_type"))
        res = rules_score(lead)
        ok = res.tier == case["expect_tier"] and res.classification == case["expect_class"]
        if ok:
            passed += 1
        else:
            print(f"MISMATCH {case['id']}: got {res.tier}/{res.classification} "
                  f"want {case['expect_tier']}/{case['expect_class']}")
    threshold = 0.9
    assert passed / len(golden) >= threshold, f"eval pass rate {passed}/{len(golden)}"


# ---- LiveMonday GraphQL shape (mocked transport, no token) -----------------
def test_live_monday_enrich_request_shape():
    """LiveMonday must POST the exact GraphQL mutation + column-value map.

    Uses httpx MockTransport (no network, no token) to assert the request
    body matches monday's expected shape with the real board column IDs.
    """
    import httpx
    from app.services.clients import LiveMonday
    from app.config import settings

    captured = {}

    def _handler(request):
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": {"change_multiple_column_values": {"id": "1"}}})

    client = LiveMonday()
    with httpx.Client(transport=httpx.MockTransport(_handler)) as mock:
        # Patch the lazy httpx.post used inside enrich by monkeypatching the module's import.
        import app.services.clients as clients
        orig_post = httpx.post
        httpx.post = lambda *a, **k: mock.post(*a, **k)  # route to mock
        try:
            client.enrich("123", tier="Hot", score=90,
                          classification="end_customer",
                          rationale=["core industry", "referral"])
        finally:
            httpx.post = orig_post

    # Assertions on the captured GraphQL payload (group is a JSON *string* per monday API)
    body = json.loads(captured["body"])
    assert "change_multiple_column_values" in body["query"]
    vars_ = body["variables"]
    assert vars_["bid"] == settings.monday_board_id
    assert vars_["iid"] == "123"
    cv = json.loads(vars_["group"])
    assert cv[settings.monday_col_status] == {"label": "Hot"}
    assert cv[settings.monday_col_score] == 90
    assert cv[settings.monday_col_classification] == {"label": "End Customer"}
    assert cv[settings.monday_col_rationale] == {"text": "core industry | referral"}
    assert captured["auth"] == f"Bearer {settings.monday_api_token}"


# ---- Webhook GET challenge (monday subscription handshake) -----------------
def test_webhook_challenge_handshake():
    from fastapi.testclient import TestClient
    from app.main import app

    c = TestClient(app)
    r = c.get("/webhook/monday", params={"challenge": "abc123"})
    assert r.status_code == 200
    assert r.json() == {"challenge": "abc123"}


# ---- Scoring queue: retry/backoff is wired (offline, no Redis) -------------
def test_scoring_retry_on_llm_failure():
    """score_lead(allow_fallback=False) must RAISE on LLM failure so the
    caller's with_retry loop retries for a REAL score instead of silently
    falling back to rules."""
    from app.services.llm_gateway import score_lead
    from app.models.schemas import LeadInput
    from app.config import settings

    lead = LeadInput(id="Q1", name="x", company="FryCo",
                     source="referral", industry="food service",
                     inquiry_type="request pricing")
    prev_llm = settings.use_llm
    prev_key = settings.openai_api_key
    settings.use_llm = True
    settings.openai_api_key = "sk-bad-key"   # force the LLM call to raise
    try:
        raised = False
        try:
            score_lead(lead, request_id="q", allow_fallback=False)
        except Exception:
            raised = True
        assert raised, "allow_fallback=False must raise on LLM failure (enables retry)"
    finally:
        settings.use_llm = prev_llm
        settings.openai_api_key = prev_key
