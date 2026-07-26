# ============================================
# SFAAM NEWS V16 - Railway Deployment Dockerfile
# Production-ready, fixes port binding issue
# ============================================

FROM python:3.11-slim

# ── Set working directory ──
WORKDIR /app

# ── Set Python env vars for production ──
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ── Install system dependencies ──
# gcc + libpq-dev: needed to build asyncpg
# libxml2-dev + libxslt-dev: needed to build lxml
# curl: needed for HEALTHCHECK
# tini: tiny init system to properly handle signals (SIGTERM)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        libpq-dev \
        libxml2-dev \
        libxslt-dev \
        libffi-dev \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

# ── Copy requirements first (better Docker layer caching) ──
COPY requirements.txt .

# ── Install Python dependencies ──
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy application code ──
COPY . .

# ── Ensure static directories exist (defensive) ──
RUN mkdir -p static/images static/css static/js

# ── Health check (Railway uses this) ──
# Uses $PORT which Railway injects at runtime
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-8000}/health" || exit 1

# ── Expose port (Railway injects $PORT) ──
EXPOSE 8000

# ── Use tini for proper signal handling (clean container shutdown) ──
ENTRYPOINT ["/usr/bin/tini", "--"]

# ── Start Uvicorn ──
# IMPORTANT: Use shell form so $PORT is expanded at runtime.
# Railway injects a random $PORT (e.g. 5432, 8080) — the app MUST listen on it.
# Using exec form ["uvicorn", ...] would NOT expand $PORT and cause healthcheck to fail.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
