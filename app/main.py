"""FastAPI application factory.

`create_app()` wires the routers and initializes the database. The primary
path is the hourly cron job (`scripts/cron_run.py`); the web server here is an
optional instant-scoring fast-path (monday webhook -> score inline). Keeping
them separate avoids importing FastAPI where not needed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

# Load .env into os.environ early so env-reading libraries (LangSmith) see
# tracing flags at import time. pydantic-settings does NOT populate os.environ.
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI

from app.api import dashboard, routes, webhook
from app.database.session import init_db


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Create tables on startup (idempotent; SQLite-friendly for local dev).
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Beyond Oil — Lead Qualification Agent",
        version="0.1.0",
        description="Scores, classifies, enriches and alerts on monday.com leads.",
        lifespan=_lifespan,
    )
    # Public intake + health
    app.include_router(webhook.router)
    # Admin / audit (token-protected)
    app.include_router(routes.router)
    # Client-facing live dashboard (read-only HTML)
    app.include_router(dashboard.router)
    return app


# Uvicorn entrypoint: `uvicorn app.main:app`
app = create_app()
