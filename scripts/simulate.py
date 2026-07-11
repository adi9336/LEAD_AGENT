"""scripts/simulate.py — push fake webhooks for local demos.

Runs the full pipeline in mock mode (no credentials). Use:
    python scripts/simulate.py
Then check the audit:  curl -H "Authorization: Bearer change-me" localhost:8000/audit
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database.session import SessionLocal, init_db  # noqa: E402
from app.models.schemas import LeadInput  # noqa: E402
from app.services.lead_service import process_lead  # noqa: E402

SAMPLE_LEADS = [
    LeadInput(id="L1", name="Joe", company="FryCo",
              source="referral", industry="food service",
              inquiry_type="request pricing"),
    LeadInput(id="L2", name="Sam", company="TechSoft",
              source="paid_ad", industry="software",
              inquiry_type="just browsing"),
    LeadInput(id="L3", name="Pat", company="Distro Inc",
              source="website", industry="food distribution",
              inquiry_type="want to distribute"),
    # Explicitly exercises the British-spelling alias (enquiry).
    LeadInput(id="L4", name="Priya", company="Chips Ltd",
              source="website", industry="restaurant",
              enquiry="request demo"),
]


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        for lead in SAMPLE_LEADS:
            rec = process_lead(db, lead, request_id=f"sim-{lead.id}")
            print(f"-> {lead.id}: tier={rec.tier} score={rec.score} "
                  f"class={rec.classification} alerted={rec.alert_sent}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
