# Railway reads this. ROLE is set per-service in the Railway dashboard
# (or railway.toml) so the same image runs both the web API and the worker.
web: uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
worker: celery -A app.scheduler.celery_app.celery_app worker --beat --loglevel=info
