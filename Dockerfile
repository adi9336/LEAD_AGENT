# ============================================================
# Dockerfile — single image, runs the hourly cron job (or the
# optional web API if ROLE=web is set).
# ============================================================
FROM python:3.11-slim

WORKDIR /app

# Install deps first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY . .

# Default: run the hourly cron batch. If ROLE=web, serve the FastAPI app on
# $PORT (optional instant-scoring fast-path). No Celery/Redis needed.
CMD ["sh", "-c", "if [ \"$ROLE\" = \"web\" ]; then \
      uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}; \
      else python scripts/cron_run.py; fi"]
