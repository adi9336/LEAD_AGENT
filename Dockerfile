# ============================================================
# Dockerfile — single image, two commands (web / worker)
# ============================================================
FROM python:3.11-slim

WORKDIR /app

# Install deps first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY . .

# `web`  -> serve the FastAPI app
# `worker` -> run Celery worker + beat
CMD ["sh", "-c", "if [ \"$ROLE\" = \"worker\" ]; then \
      celery -A app.scheduler.celery_app.celery_app worker --beat --loglevel=info; \
      else uvicorn app.main:app --host 0.0.0.0 --port 8000; fi"]
