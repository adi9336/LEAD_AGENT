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
from app.services.lead_service import process_lead
from app.services.scoring import rules_score
from app.scheduler.hygiene import run_hygiene


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
def test_process_lead_scores_and_alerts(db: Session):
    lead = LeadInput(id="L1", name="Joe", company="FryCo",
                     source="referral", industry="food service",
                     inquiry_type="request pricing")
    rec = process_lead(db, lead, request_id="t1")
    assert rec.tier == "Hot"
    assert rec.alert_sent is True
    assert rec.scored_at is not None
    assert rec.alerted_at is not None


# ---- hygiene ---------------------------------------------------------------
def test_hygiene_recovers_stale_lead(db: Session):
    # Seed an old, unscored lead (beyond the 24h SLA).
    from datetime import datetime, timedelta, timezone
    from app.database.models import Lead

    old = Lead(id="STALE", name="Old", created_at=datetime.now(timezone.utc)
               - timedelta(hours=30))
    db.add(old)
    db.commit()

    report = run_hygiene(db, request_id="h1")
    assert report["found"] == 1
    assert report["recovered"] == 1
    refreshed = db.get(Lead, "STALE")
    assert refreshed.scored_at is not None


# ---- eval harness (golden set) ---------------------------------------------
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
