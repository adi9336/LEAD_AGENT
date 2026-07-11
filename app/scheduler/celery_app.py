"""Celery app + scheduled task.

Celery is LAZY: this module is only imported in live/production runs. The
hygiene LOGIC lives in `hygiene.run_hygiene` (no Celery dependency) so it can be
unit-tested directly. Here we just register a periodic task that calls it.
"""

from __future__ import annotations

from celery import Celery

# Load .env into os.environ so env-reading libs (LangSmith) see tracing flags.
from dotenv import load_dotenv

load_dotenv()

from app.config import settings
from app.scheduler.hygiene import run_hygiene

celery_app = Celery(
    "lead_agent",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

# Periodic schedule (Celery beat). Runs the hygiene sweep on an interval.
celery_app.conf.beat_schedule = {
    "hygiene-scan": {
        "task": "app.scheduler.celery_app.hygiene_task",
        "schedule": settings.hygiene_interval_minutes * 60,  # seconds
    },
}
celery_app.conf.timezone = "UTC"


@celery_app.task(name="app.scheduler.celery_app.hygiene_task")
def hygiene_task() -> dict:
    """Scheduled wrapper around the plain hygiene function."""
    from app.database.session import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        return run_hygiene(db, request_id="celery-hygiene")
    finally:
        db.close()
