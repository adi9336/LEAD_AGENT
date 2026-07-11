"""scripts/cron_run.py — the hourly batch entry point.

Run by the scheduler (Render Cron Job, or a system crontab, or `python
scripts/cron_run.py` locally). It fetches leads that still need scoring from
monday, then scores + delivers each (LLM with retry/backoff for a REAL score,
falling back to rules only after retries are exhausted). Exit code is non-zero
on a partial failure so the scheduler can alert/retry.

Usage:
    python scripts/cron_run.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Load .env into os.environ BEFORE importing the app. LangSmith (and other
# env-reading libraries) read os.environ at import time; pydantic-settings
# loads .env into its own Settings object but does NOT populate os.environ,
# so without this explicit load the tracing flag is invisible to LangSmith.
from dotenv import load_dotenv

load_dotenv()

# Make the repo root importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database.session import SessionLocal, init_db  # noqa: E402
from app.services.lead_service import run_cron  # noqa: E402


def main() -> int:
    init_db()
    db = SessionLocal()
    try:
        report = run_cron(db)
    finally:
        db.close()
    print(f"[cron] due={report['due']} scored={report['scored']} "
          f"failed={report['failed']}")
    # Non-zero exit on any failure so the scheduler surfaces it.
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
