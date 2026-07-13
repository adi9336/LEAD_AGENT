"""Cron trigger endpoint — lets an external scheduler fire the hourly batch
over HTTP instead of a long-lived worker process.

Why HTTP and not a container loop:
  - Render's FREE web tier is what we keep; the old ``while true`` worker was
    a paid resource. An HTTP trigger lets a FREE scheduler (GitHub Actions on
    ``schedule: '0 * * * *'``) hit this endpoint once an hour. No paid worker.
  - The batch runs in a background thread and we return 202 immediately, so it
    is immune to proxy/request timeouts (the work keeps going even if the
    caller's connection closes).

Auth: an ``x-cron-secret`` header must match settings.cron_secret. If no
secret is configured (dev), the endpoint is open — same dev-friendly pattern
used by admin_token / webhook_secret.
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.config import settings
from app.database.session import SessionLocal, init_db
from app.services.lead_service import run_cron

router = APIRouter()


def _require_cron_secret(x_cron_secret: str | None = Header(default=None)) -> None:
    if not settings.cron_secret:
        return  # dev: no secret configured -> open
    if x_cron_secret != settings.cron_secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid cron secret")


@router.get("/api/cron", status_code=status.HTTP_202_ACCEPTED,
            dependencies=[Depends(_require_cron_secret)])
def trigger_cron() -> dict:
    """Fire the hourly batch (fetch -> score -> write -> alert).

    Runs in a background thread so the caller gets 202 immediately — immune to
    request timeouts. Auth via the ``x-cron-secret`` header (open in dev).
    """
    result: dict = {}

    def _go() -> None:
        init_db()
        db = SessionLocal()
        try:
            result["report"] = run_cron(db)
        except Exception as exc:  # noqa: BLE001 - surface, don't crash the thread
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            db.close()

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    return {
        "status": "accepted",
        "detail": "hourly batch started in background; check /health and the audit log",
    }
