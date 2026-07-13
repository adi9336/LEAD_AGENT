# ============================================================
# Dockerfile — single image that serves the FastAPI web app.
#
# The hourly batch is triggered over HTTP by an EXTERNAL scheduler
# (GitHub Actions `schedule: '0 * * * *'` hitting /api/cron), so the
# container only runs the web server. This keeps the deployment on
# Render's FREE web tier — no paid worker needed.
#
# Build args let the same image serve either role if desired, but the
# default CMD serves the web API on $PORT.
# ============================================================
FROM python:3.11-slim

WORKDIR /app

# Install deps first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY . .

# Serve the FastAPI app. $PORT is injected by the platform.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
