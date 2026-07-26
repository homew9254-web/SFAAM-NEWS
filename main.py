"""
main.py - SFAAM NEWS V26 (Async + Redis + PostgreSQL + Trends Pipeline)

Key upgrades across V7 → V26:
- Async FastAPI endpoints (asyncpg + AsyncSession)
- Redis-backed distributed rate limiting (works across multiple Uvicorn workers)
- Redis-backed admin session store (survives worker restarts)
- ArticleBriefOut model for list/search/trending (excludes heavy ai_content)
- ArticleOut kept ONLY for /api/articles/{id} single-article endpoint
- SHA-256 admin password + secure session tokens + CSRF protection (V24)
- Full security headers (CSP, X-Frame-Options, Permissions-Policy, etc.)
- V26: Trends pipeline — zero-hallucination content engine (every 6h)
  with admin review dashboard at /admin.html → Trends Drafts tab.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

import redis
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import func, or_, select, text

from database import Article, ContactMessage, Subscriber, ArticleLike, ArticleComment, AsyncSessionLocal, get_db, init_db, engine
from scheduler import run_pipeline, start_scheduler

# ── Logging ──
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ──
SITE_URL = os.getenv("SITE_URL", "https://sfaamnews.com")
REGIONS = {"world", "usa", "uk", "pakistan", "india", "germany"}

# ── Admin Auth ──
# V29 FIX: Support BOTH ADMIN_PASSWORD (plain text) and ADMIN_PASSWORD_HASH (pre-hashed).
# If ADMIN_PASSWORD is set, we auto-hash it. This fixes the common misconfiguration
# where users set ADMIN_PASSWORD=xxx in .env but the code only reads ADMIN_PASSWORD_HASH.
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
_raw_admin_password = os.getenv("ADMIN_PASSWORD", "")
if _raw_admin_password and not ADMIN_PASSWORD_HASH:
    ADMIN_PASSWORD_HASH = hashlib.sha256(_raw_admin_password.encode()).hexdigest()
    logger.info("[V29] ADMIN_PASSWORD_HASH auto-generated from ADMIN_PASSWORD env var")

# V29 FIX: Support both ADMIN_KEY and ADMIN_API_KEY env var names.
ADMIN_KEY = os.getenv("ADMIN_KEY", "") or os.getenv("ADMIN_API_KEY", "")
SESSION_TTL = 3600  # 1 hour

# ── Rate Limiting ──
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "100"))           # global: per IP per minute
RATE_WINDOW = int(os.getenv("RATE_WINDOW", "60"))
CONTACT_RATE_LIMIT = int(os.getenv("CONTACT_RATE_LIMIT", "5"))       # per IP per hour
SUBSCRIBE_RATE_LIMIT = int(os.getenv("SUBSCRIBE_RATE_LIMIT", "10"))  # per IP per hour


# ════════════════════════════════════════════════════════════
#  V24: CSRF PROTECTION
#  - GET endpoints are exempt (safe + idempotent)
#  - All POST/PUT/PATCH/DELETE require either:
#      (a) X-CSRF-Token header matching the session's CSRF token, OR
#      (b) X-Admin-Key header (admin API clients — server-to-server), OR
#      (c) Be a /api/admin/login attempt (no session yet)
#  CSRF tokens are issued on successful admin login and rotated hourly.
# ════════════════════════════════════════════════════════════

CSRF_ENABLED = os.getenv("CSRF_ENABLED", "1") == "1"
CSRF_TOKEN_TTL = 3600  # 1 hour


def _issue_csrf_token(session_token: str) -> str:
    """Generate a CSRF token tied to the session. Stored in Redis under
    csrf:{session_token}."""
    csrf = secrets.token_urlsafe(32)
    if _redis_client:
        try:
            _redis_client.setex(f"csrf:{session_token}", CSRF_TOKEN_TTL, csrf)
        except Exception:
            pass
    else:
        # V26 FIX: store the actual CSRF token (was storing only expiry, which
        # meant _verify_csrf_token could never match in memory-fallback mode).
        _mem_csrf[session_token] = csrf
    return csrf


def _verify_csrf_token(session_token: str, csrf_token: str) -> bool:
    """Verify that the CSRF token matches the one issued for this session."""
    if not session_token or not csrf_token:
        return False
    if _redis_client:
        try:
            stored = _redis_client.get(f"csrf:{session_token}")
            return stored is not None and secrets.compare_digest(str(stored), csrf_token)
        except Exception:
            return False
    # Memory fallback — V26 FIX: compare against the stored CSRF token,
    # not against session-expiry timestamps.
    stored = _mem_csrf.get(session_token)
    if not stored:
        return False
    return secrets.compare_digest(stored, csrf_token)


async def csrf_middleware(request: Request, call_next):
    """V24: CSRF protection for state-changing requests.
    Registered after `app = FastAPI(...)` is created (see app.add_middleware
    call below). Function defined here so it has access to CSRF helpers."""
    if not CSRF_ENABLED:
        return await call_next(request)

    # Safe methods — no CSRF check needed
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return await call_next(request)

    # Health/ready checks — exempt
    if request.url.path in ("/health", "/ready"):
        return await call_next(request)

    # Public POST endpoints (subscribe, contact) — exempt from CSRF since
    # they don't modify admin state. The admin login is also exempt (no
    # session yet to issue CSRF token).
    csrf_exempt_paths = {
        "/api/admin/login",
        "/api/contact",
        "/api/subscribe",
        "/api/unsubscribe",
        "/api/articles/{article_id}/comments",  # public comments
        "/api/articles/{article_id}/like",       # public likes
        "/api/polls/{poll_id}/vote",             # public polls
        "/api/quiz/{quiz_id}/submit",            # public quizzes
    }
    path = request.url.path
    is_exempt = path in csrf_exempt_paths
    if not is_exempt:
        for pattern in csrf_exempt_paths:
            if "{" in pattern:
                import re as _re_csrf
                regex = "^" + _re_csrf.sub(r"\{[^}]+\}", r"[^/]+", pattern) + "$"
                if _re_csrf.match(regex, path):
                    is_exempt = True
                    break
    # Also exempt any non-admin POST (public writes don't need CSRF)
    if not path.startswith("/api/admin/"):
        is_exempt = True

    if is_exempt:
        return await call_next(request)

    # Admin endpoints — require CSRF token OR admin key
    session_token = request.headers.get("x-admin-session", "")
    csrf_token    = request.headers.get("x-csrf-token", "")
    admin_key     = request.headers.get("x-admin-key", "")

    # Admin key alone is sufficient (server-to-server API access)
    if ADMIN_KEY and admin_key and secrets.compare_digest(admin_key, ADMIN_KEY):
        return await call_next(request)

    # Otherwise require valid CSRF token
    if not _verify_csrf_token(session_token, csrf_token):
        return JSONResponse(
            status_code=403,
            content={"detail": "CSRF token missing or invalid. Refresh the admin page and try again."}
        )

    return await call_next(request)


# ════════════════════════════════════════════════════════════
#  REDIS — Distributed rate limiter + session store
#  Falls back to in-memory dict if REDIS_URL is not set (local dev only)
# ════════════════════════════════════════════════════════════

REDIS_URL = os.getenv("REDIS_URL", "")
_redis_client: Optional[redis.Redis] = None
_mem_rate_store: dict[str, list[float]] = {}    # fallback only
_mem_sessions: dict[str, float] = {}            # fallback only (session_token -> expiry)
_mem_csrf: dict[str, str] = {}                  # V26 FIX: fallback CSRF store (session_token -> csrf_token)

if REDIS_URL:
    try:
        _redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
        _redis_client.ping()
        logger.info("[SFAAM V26] Redis connected for distributed rate limiting + sessions")
    except Exception as e:
        logger.warning(f"[SFAAM V26] Redis connect failed, falling back to in-memory: {e}")
        _redis_client = None
else:
    logger.warning("[SFAAM V26] REDIS_URL not set — using in-memory rate limit (NOT for production)")


def _check_rate_limit(key: str, limit: int, window: int) -> bool:
    """Sliding-window rate limit. Uses Redis INCR + EXPIRE when available."""
    redis_key = f"rl:{key}"
    if _redis_client:
        try:
            pipe = _redis_client.pipeline()
            pipe.incr(redis_key)
            pipe.expire(redis_key, window)
            count, _ = pipe.execute()
            return count <= limit
        except Exception as e:
            logger.warning(f"Redis rate limit error: {e} — falling back to memory")
    # In-memory fallback (single worker only)
    now = time.time()
    bucket = _mem_rate_store.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < window]
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def _create_session() -> str:
    """Create a secure admin session token, stored in Redis (or memory)."""
    token = secrets.token_urlsafe(32)
    expiry = time.time() + SESSION_TTL
    if _redis_client:
        try:
            _redis_client.setex(f"sess:{token}", SESSION_TTL, str(expiry))
            return token
        except Exception as e:
            logger.warning(f"Redis session error: {e}")
    # Memory fallback + clean expired
    now = time.time()
    expired = [t for t, exp in _mem_sessions.items() if exp < now]
    for t in expired:
        _mem_sessions.pop(t, None)
    _mem_sessions[token] = expiry
    return token


def _verify_session(token: str) -> bool:
    """Verify admin session token."""
    if not token:
        return False
    if _redis_client:
        try:
            val = _redis_client.get(f"sess:{token}")
            return val is not None
        except Exception:
            pass
    expiry = _mem_sessions.get(token, 0)
    if expiry < time.time():
        _mem_sessions.pop(token, None)
        return False
    return True


def _destroy_session(token: str) -> None:
    if _redis_client:
        try:
            _redis_client.delete(f"sess:{token}")
            _redis_client.delete(f"csrf:{token}")  # V26: also clear CSRF
            return
        except Exception:
            pass
    _mem_sessions.pop(token, None)
    _mem_csrf.pop(token, None)  # V26: also clear CSRF


def _verify_admin_password(password: str) -> bool:
    """Verify admin password using SHA-256 hash + constant-time comparison.

    Supports two stored formats (backwards-compatible):
      1. Plain SHA-256 hex (legacy, no salt — vulnerable to rainbow tables but kept
         for backwards compatibility with existing deployments).
      2. Salted format:  salt_hex$hash_hex   (V31+ — recommended)
    """
    if not ADMIN_PASSWORD_HASH or not password:
        return False
    stored = ADMIN_PASSWORD_HASH
    if "$" in stored and len(stored.split("$")) == 2:
        # Salted format
        salt_hex, hash_hex = stored.split("$")
        try:
            salt = bytes.fromhex(salt_hex)
        except ValueError:
            return False
        # PBKDF2-HMAC-SHA256 with 200k iterations — slow enough to resist brute force
        import hmac as _hmac
        computed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()
        return _hmac.compare_digest(computed, hash_hex)
    # Legacy plain SHA-256 (no salt)
    hashed = hashlib.sha256(password.encode()).hexdigest()
    return secrets.compare_digest(hashed, stored)


def _hash_admin_password(password: str) -> str:
    """V31: Generate a salted PBKDF2 password hash for storage.

    Format:  salt_hex$hash_hex
    Use this when generating new admin credentials.
    """
    salt = secrets.token_bytes(16)
    hash_hex = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()
    return f"{salt.hex()}${hash_hex}"


def _is_authed(request: Request) -> bool:
    """V12: Check if request has a valid admin session OR admin key.
    Also supports optional ADMIN_IP_WHITELIST for extra security."""
    # V12: IP whitelist check (if configured)
    admin_ips = os.getenv("ADMIN_IP_WHITELIST", "")
    if admin_ips:
        client_ip = _get_client_ip(request)
        allowed = [ip.strip() for ip in admin_ips.split(",") if ip.strip()]
        if client_ip not in allowed:
            return False

    session_token = request.headers.get("x-admin-session", "")
    admin_key = request.headers.get("x-admin-key", "")
    return _verify_session(session_token) or (
        bool(ADMIN_KEY) and secrets.compare_digest(admin_key, ADMIN_KEY)
    )


def _require_admin(request: Request) -> None:
    """Raise 403 unless request is authed."""
    if not _is_authed(request):
        raise HTTPException(403, "Unauthorized. Please login or provide valid admin key.")


def _make_slug(title: str) -> str:
    import re as _re
    slug = _re.sub(r"[^a-z0-9\s-]", "", title.lower())
    slug = _re.sub(r"\s+", "-", slug.strip())
    slug = _re.sub(r"-+", "-", slug)
    return slug[:80]


# ════════════════════════════════════════════════════════════
#  PYDANTIC MODELS
# ════════════════════════════════════════════════════════════

class ArticleOut(BaseModel):
    """FULL article model — only for /api/articles/{id}."""
    id: int
    title: str
    slug: Optional[str] = None
    original_url: str
    summary: Optional[str] = None
    ai_content: str
    image_url: Optional[str] = None
    region: str
    meta_desc: Optional[str] = None
    keywords: Optional[str] = None
    views: int
    date: datetime
    updated_at: Optional[datetime] = None
    # V18: Wikipedia-killer fields
    tldr_summary: Optional[str] = None
    # V29: Make fact_check_status and audio_status Optional for pre-V18 articles
    fact_check_status: Optional[str] = "under_review"
    audio_url: Optional[str] = None
    audio_status: Optional[str] = "pending"
    # V30 (TRD v1.0): audio_player_token — needed by frontend to know whether
    # to render the top-level audio player OR rely on the inline token in ai_content.
    # Without this field, the frontend would render BOTH (duplicate audio players).
    audio_player_token: Optional[str] = None
    # V30: word_count + tier for display in article header
    word_count: Optional[int] = 0
    word_count_tier: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class ArticleBriefOut(BaseModel):
    """Lightweight model for list/search/trending — excludes ai_content
    (which can be 4000+ words). Cuts payload size dramatically."""
    id: int
    title: str
    slug: Optional[str] = None
    summary: Optional[str] = None
    image_url: Optional[str] = None
    region: str
    views: int
    date: datetime
    reading_time: int
    # V18: Include fact-check status in briefs for badge display
    # V29: Default to "under_review" when NULL (pre-V18 articles)
    fact_check_status: Optional[str] = "under_review"
    model_config = ConfigDict(from_attributes=True)


class ContactIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr
    subject: Optional[str] = Field(None, max_length=200)
    message: str = Field(..., min_length=10, max_length=5000)


class SubscribeIn(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def validate_email_domain(cls, v):
        disposable = {
            "tempmail.com", "10minutemail.com", "guerrillamail.com",
            "mailinator.com", "throwawaymail.com", "fakeinbox.com",
            "tempinbox.com", "sharklasers.com", "getairmail.com",
        }
        domain = v.split("@")[-1].lower()
        if domain in disposable:
            raise ValueError("Disposable email addresses are not allowed")
        return v.lower()


class UnsubscribeIn(BaseModel):
    """V7 fix: unsubscribe now uses POST body instead of query param
    (email addresses should never travel in URLs — they leak into
    server logs, browser history, and Referer headers)."""
    email: EmailStr


class AdminLoginIn(BaseModel):
    password: str


class ManualArticleIn(BaseModel):
    title: str = Field(..., min_length=5, max_length=500)
    content: str = Field(..., min_length=50)
    region: str
    summary: Optional[str] = None
    image_url: Optional[str] = None
    meta_desc: Optional[str] = None
    keywords: Optional[str] = None

    @field_validator("region")
    @classmethod
    def validate_region(cls, v):
        if v not in REGIONS:
            raise ValueError(f"Region must be one of: {', '.join(sorted(REGIONS))}")
        return v


# ── V8: Like + Comment Models ──
class LikeIn(BaseModel):
    action: str = Field(..., pattern="^(like|unlike)$")
    fingerprint: Optional[str] = Field(None, max_length=64)


class CommentIn(BaseModel):
    name: str = Field("Anonymous", max_length=80)
    comment: str = Field(..., min_length=2, max_length=1000)


# ════════════════════════════════════════════════════════════
#  LIFESPAN + APP
# ════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──
    logger.info("=" * 60)
    logger.info("SFAAM NEWS V26 starting up...")
    logger.info("=" * 60)

    # V24: Initialize error tracking + audit logging FIRST
    try:
        from monitoring import init_monitoring
        init_monitoring()
        logger.info("✓ Monitoring initialized (Sentry/Better Stack/audit log)")
    except Exception as e:
        logger.warning(f"Monitoring init failed (non-fatal): {e}")
    logger.info(f"  DATABASE_URL: {('set (' + os.getenv('DATABASE_URL', '')[:20] + '...)') if os.getenv('DATABASE_URL') else 'NOT SET — using SQLite'}")
    logger.info(f"  REDIS_URL:    {'set' if os.getenv('REDIS_URL') else 'NOT SET — in-memory mode'}")
    logger.info(f"  ADMIN_PASSWORD_HASH: {'set' if ADMIN_PASSWORD_HASH else 'NOT SET — admin login disabled!'}")
    logger.info(f"  ADMIN_KEY:    {'set' if ADMIN_KEY else 'NOT SET — API admin access disabled!'}")
    logger.info(f"  PORT:         {os.getenv('PORT', 'not set (default 8000)')}")

    # Check AI keys
    ai_keys_count = sum(1 for r in REGIONS
                        if os.getenv(f"GROQ_KEY_{r.upper()}", "")
                        and os.getenv(f"GROQ_KEY_{r.upper()}", "") != "your_groq_key")
    logger.info(f"  GROQ keys:    {ai_keys_count}/{len(REGIONS)} regions configured")
    if ai_keys_count == 0:
        logger.warning("  ⚠ No GROQ keys set — articles will use raw RSS summary only")

    try:
        await init_db()
        logger.info("✓ Database initialized")
    except Exception as e:
        logger.error(f"✗ Database init FAILED: {type(e).__name__}: {e}")
        logger.error("  → Check DATABASE_URL env var. App will continue but DB queries will fail.")
        # Don't re-raise — let the app start so /health works for debugging

    # Pro tables — must run AFTER init_db() so the engine exists.
    # NOTE: @app.on_event("startup") is silently ignored when a lifespan
    # context manager is present, so create_pro_tables must live here.
    try:
        from pro_models import create_pro_tables
        await create_pro_tables(engine)
        logger.info("✓ Pro database tables verified/created")
    except Exception as e:
        logger.warning(f"⚠ Pro table creation skipped (non-fatal): {type(e).__name__}: {e}")

    # V23: Initialise full-text search (FTS5 for SQLite, GIN tsvector for PG)
    try:
        from database import init_fts
        await init_fts()
        logger.info("✓ Full-text search index ready")
    except Exception as e:
        logger.warning(f"⚠ FTS init skipped: {type(e).__name__}: {e}")

    try:
        # FIX: inject the running event loop into all schedulers BEFORE starting them.
        # APScheduler's BackgroundScheduler runs jobs in daemon threads. Those threads
        # cannot create new asyncpg connections from a fresh asyncio.run() loop because
        # the connection pool was created in THIS loop. By storing a reference here and
        # using asyncio.run_coroutine_threadsafe(), all scheduler threads submit work
        # to this loop instead of spinning up a conflicting one.
        import scheduler as _scheduler_mod
        import trends_scheduler as _trends_sched_mod
        import engine_scheduler as _engine_sched_mod
        import engine2_scheduler as _engine2_sched_mod
        _main_loop = asyncio.get_event_loop()
        _scheduler_mod.MAIN_LOOP = _main_loop
        _trends_sched_mod.MAIN_LOOP = _main_loop
        _engine_sched_mod.MAIN_LOOP = _main_loop
        _engine2_sched_mod.MAIN_LOOP = _main_loop
        start_scheduler()
        logger.info("✓ Scheduler started")
    except Exception as e:
        logger.error(f"✗ Scheduler start FAILED: {type(e).__name__}: {e}")

    # V26: Start the Trends (Zero-Hallucination Content Engine) scheduler — runs every 6 hours.
    # Safe-fail: if the import fails for any reason, app continues normally.
    try:
        from trends_scheduler import start_trends_scheduler
        start_trends_scheduler()
        logger.info("✓ Trends V26 scheduler started (every 6 hours)")
    except Exception as e:
        logger.error(f"✗ Trends scheduler start FAILED (non-fatal): {type(e).__name__}: {e}")

    # V30: Legacy SFAAM Automated News Engine (fact-only pipeline). Disabled by
    # default now that Engine V2 (clean rebuild, full-article pipeline) exists.
    # Set ENGINE_V30_ENABLED=1 to run both side by side.
    if os.getenv("ENGINE_V30_ENABLED", "0") == "1":
        try:
            from engine_scheduler import start_engine_scheduler
            start_engine_scheduler()
            logger.info("✓ SFAAM Automated News Engine V30 scheduler started (every 3 hours, TRD v1.0)")
        except Exception as e:
            logger.error(f"✗ Engine V30 scheduler start FAILED (non-fatal): {type(e).__name__}: {e}")
    else:
        logger.info("… Engine V30 disabled (ENGINE_V30_ENABLED!=1) — Engine V2 is the active pipeline")

    # Engine V2: clean-rebuild pipeline — full article scrape (not just facts),
    # search-driven sources (NewsAPI/GNews/DuckDuckGo), images woven through
    # the content, per TXT spec. Runs every ENGINE2_INTERVAL_HOURS (default 3).
    if os.getenv("ENGINE2_ENABLED", "1") == "1":
        try:
            from engine2_scheduler import start_engine2_scheduler
            start_engine2_scheduler()
            logger.info("✓ Engine V2 scheduler started (search-driven, full-article pipeline)")
        except Exception as e:
            logger.error(f"✗ Engine V2 scheduler start FAILED (non-fatal): {type(e).__name__}: {e}")
    else:
        logger.info("… Engine V2 disabled (ENGINE2_ENABLED=0)")

    logger.info("=" * 60)
    logger.info("SFAAM NEWS V30 LIVE — startup complete")
    logger.info("=" * 60)
    yield
    # ── Graceful shutdown ──
    # V30: stop engine scheduler so we don't leave orphaned asyncio tasks
    logger.info("SFAAM NEWS V30 shutting down...")
    try:
        from engine_scheduler import stop_engine_scheduler, is_running as engine_is_running
        if engine_is_running():
            stop_engine_scheduler()
            logger.info("✓ V30 engine scheduler stopped")
    except Exception as e:
        logger.warning(f"Engine scheduler stop failed (non-fatal): {e}")
    # Engine V2: stop scheduler
    try:
        from engine2_scheduler import stop_engine2_scheduler
        stop_engine2_scheduler()
        logger.info("✓ Engine V2 scheduler stopped")
    except Exception as e:
        logger.warning(f"Engine V2 scheduler stop failed (non-fatal): {e}")
    # V26: stop trends scheduler
    try:
        from trends_scheduler import stop_trends_scheduler
        stop_trends_scheduler()
        logger.info("✓ V26 trends scheduler stopped")
    except Exception:
        pass
    logger.info("SFAAM NEWS V30 shutdown complete.")


app = FastAPI(title="SFAAM NEWS", version="30.0", lifespan=lifespan)

# V24: Register CSRF middleware (defined above; must be registered AFTER
# app is created so the decorator works).
app.middleware("http")(csrf_middleware)

# ── CORS ──
# V29 FIX: In development mode, allow ALL origins so the site works on
# Railway preview URLs, localhost, etc. In production, restrict to specific domains.
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "")
_is_dev = os.getenv("ENV", "development").lower() in ("development", "dev")
if CORS_ORIGINS:
    origins = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()]
elif _is_dev:
    # Development: allow all origins (Railway previews, localhost, etc.)
    origins = ["*"]
else:
    # Production default: only the main domain
    origins = ["https://sfaamnews.com", "https://www.sfaamnews.com"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True if origins != ["*"] else False,
    allow_methods=["GET", "POST", "OPTIONS", "DELETE", "PATCH", "PUT"],
    allow_headers=["Content-Type", "Authorization", "Accept", "X-Admin-Key", "X-Admin-Session", "X-CSRF-Token"],
    max_age=3600,
)


# V12: Request body size limit (10MB max — prevents DoS via huge payloads)
@app.middleware("http")
async def body_size_middleware(request: Request, call_next):
    if request.headers.get("content-length"):
        try:
            size = int(request.headers["content-length"])
            if size > 10 * 1024 * 1024:  # 10MB
                return JSONResponse(status_code=413, content={"detail": "Request body too large (max 10MB)"})
        except (ValueError, TypeError):
            pass
    return await call_next(request)


# ════════════════════════════════════════════════════════════
#  SECURITY + RATE LIMIT MIDDLEWARE
# ════════════════════════════════════════════════════════════

def _get_client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", request.client.host if request.client else "")
    if "," in str(xff):
        return str(xff).split(",")[0].strip()
    return str(xff)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # CRITICAL: Skip rate limiting + security headers for /health and /ready
    # Railway hits /health frequently for health checks — if it gets
    # rate-limited (429) or times out, the deploy fails.
    if request.url.path in ("/health", "/ready"):
        return await call_next(request)

    client_ip = _get_client_ip(request)

    # Global rate limit — Redis-backed (works across workers)
    if not _check_rate_limit(f"global:{client_ip}", RATE_LIMIT, RATE_WINDOW):
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": f"Rate limit exceeded. Max {RATE_LIMIT} requests per {RATE_WINDOW}s."}
        )

    response = await call_next(request)

    # Security headers (V12: Enhanced)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    response.headers["X-Robots-Tag"] = "index, follow"
    # V29 FIX: Only set HSTS in production (over HTTPS). Setting HSTS on
    # Railway's HTTP-only preview URLs breaks the site — browsers refuse
    # to load resources over HTTP after seeing HSTS.
    if not _is_dev:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    # V12: Additional hardening
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    response.headers["X-Download-Options"] = "noopen"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"

    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://plausible.io https://www.googletagmanager.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https: http:; "
        "connect-src 'self' https://plausible.io https://www.google-analytics.com https://region1.google-analytics.com; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )
    response.headers["Content-Security-Policy"] = csp
    return response


# ── Static directory (absolute so path resolution is CWD-independent) ──
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# ── Static Files ──
app.mount("/static", StaticFiles(directory=STATIC_DIR, html=True), name="static")


# ════════════════════════════════════════════════════════════
#  PRO 1 — World-class news platform layer
#  Installs: security middleware, split sitemaps, full-text search,
#  personalization, push notifications, digests, topics, authors,
#  engagement (highlights/reactions/comments/folders/citations),
#  dynamic OG images.
# ════════════════════════════════════════════════════════════
def _install_pro_modules() -> None:
    """Register all Pro modules on the app. Wrapped in a function so
    import errors degrade gracefully (Pro features just don't load)
    rather than crashing the whole site."""
    from functools import wraps

    # Admin guard — wraps endpoints to require admin auth
    def admin_guard(endpoint):
        @wraps(endpoint)
        async def wrapper(*args, **kwargs):
            request: Request = kwargs.get("request") or (args[0] if args else None)
            if not isinstance(request, Request):
                raise HTTPException(401, "Auth required")
            _require_admin(request)
            return await endpoint(*args, **kwargs)
        return wrapper

    # 1. Security middleware (CSP, HSTS, rate-limit, CSRF)
    try:
        from pro_security import install_pro_security
        install_pro_security(app, redis_client=_redis_client)
    except Exception as e:
        logger.warning(f"[Pro] Security module failed to load: {e}")

    # 2. Sitemaps (REPLACES the legacy single /sitemap.xml)
    try:
        from pro_sitemaps import register_pro_sitemap_routes
        register_pro_sitemap_routes(app, get_db)
    except Exception as e:
        logger.warning(f"[Pro] Sitemaps module failed to load: {e}")

    # 3. Search (REPLACES the legacy /api/articles/search)
    try:
        from pro_search import register_pro_search_routes
        register_pro_search_routes(app, get_db)
    except Exception as e:
        logger.warning(f"[Pro] Search module failed to load: {e}")

    # 4. Personalization (reading history, recommendations)
    try:
        from pro_personalization import register_pro_personalization_routes
        register_pro_personalization_routes(app, get_db)
    except Exception as e:
        logger.warning(f"[Pro] Personalization module failed to load: {e}")

    # 5. Push notifications
    try:
        from pro_push import register_pro_push_routes
        register_pro_push_routes(app, get_db, admin_guard)
    except Exception as e:
        logger.warning(f"[Pro] Push module failed to load: {e}")

    # 6. Email digests
    try:
        from pro_digests import register_pro_digest_routes
        register_pro_digest_routes(app, get_db, admin_guard)
    except Exception as e:
        logger.warning(f"[Pro] Digests module failed to load: {e}")

    # 7. Topics + Authors
    try:
        from pro_topics import register_pro_topic_routes
        register_pro_topic_routes(app, get_db)
    except Exception as e:
        logger.warning(f"[Pro] Topics module failed to load: {e}")

    # 8. Engagement (highlights, reactions, comments, folders, citations, corrections)
    try:
        from pro_engagement import register_pro_engagement_routes
        register_pro_engagement_routes(app, get_db, admin_guard)
    except Exception as e:
        logger.warning(f"[Pro] Engagement module failed to load: {e}")

    # 9. Dynamic OG images
    try:
        from pro_og_image import register_pro_og_routes
        register_pro_og_routes(app, get_db)
    except Exception as e:
        logger.warning(f"[Pro] OG image module failed to load: {e}")

    logger.info("[Pro] All modules installed — SFAAM NEWS PRO 1 is live.")


_install_pro_modules()


# ── Pro: also create the Pro DB tables at startup ──
@app.on_event("startup")
async def _pro_create_tables():
    """Create all Pro tables if they don't exist (CREATE IF NOT EXISTS).
    Wrapped in try/except so missing tables don't crash startup."""
    try:
        from pro_models import create_pro_tables
        await create_pro_tables(engine)
        logger.info("[Pro] Database tables verified/created.")
    except Exception as e:
        logger.warning(f"[Pro] Table creation skipped: {e}")


# ════════════════════════════════════════════════════════════
#  V18: REDIS CACHING LAYER (for 1M+ views scaling)
#  Caches hot DB queries (trending, stats, article lists) so
#  thousands of concurrent readers don't all hit the database.
# ════════════════════════════════════════════════════════════

import json as _json

CACHE_TTL_SHORT = 60       # 1 minute — for article lists (fresh content matters)
CACHE_TTL_MED = 300        # 5 minutes — for trending
CACHE_TTL_LONG = 3600      # 1 hour — for stats


def _cache_get(key: str):
    """Get value from Redis cache. Returns None if miss or Redis unavailable."""
    if not _redis_client:
        return None
    try:
        val = _redis_client.get(f"cache:{key}")
        if val:
            return _json.loads(val)
    except Exception as e:
        logger.debug(f"Cache get error for {key}: {e}")
    return None


def _cache_set(key: str, value, ttl: int = CACHE_TTL_SHORT) -> None:
    """Set value in Redis cache with TTL."""
    if not _redis_client:
        return
    try:
        _redis_client.setex(f"cache:{key}", ttl, _json.dumps(value, default=str))
    except Exception as e:
        logger.debug(f"Cache set error for {key}: {e}")


def _cache_invalidate_pattern(pattern: str) -> None:
    """Delete all cache keys matching a pattern (e.g. 'articles:*')."""
    if not _redis_client:
        return
    try:
        keys = list(_redis_client.scan_iter(f"cache:{pattern}"))
        if keys:
            _redis_client.delete(*keys)
            logger.info(f"Cache invalidated: {len(keys)} keys matching '{pattern}'")
    except Exception as e:
        logger.debug(f"Cache invalidate error for {pattern}: {e}")


def _generate_tldr_safe(content: str) -> str:
    """V18: Safe wrapper for TL;DR generation. Never raises."""
    try:
        from ai_writer import _generate_tldr
        return _generate_tldr(content or "")
    except Exception as e:
        logger.debug(f"TL;DR generation failed: {e}")
        return ""


# ════════════════════════════════════════════════════════════
#  API ENDPOINTS
# ════════════════════════════════════════════════════════════

# ── List Articles (uses ArticleBriefOut — no ai_content) ──
@app.get("/api/articles", response_model=list[ArticleBriefOut])
async def list_articles(
    region: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(12, ge=1, le=100),
    db=Depends(get_db),
):
    # V24: Only show published articles (drafts/pending_review hidden from public)
    # V29: Also include articles where status is NULL (pre-V24 articles that
    # were saved before the status column existed). These are treated as published.
    q = select(Article).where(
        or_(Article.status == "published", Article.status == None)  # noqa: E711
    ).order_by(Article.date.desc())
    if region and region in REGIONS:
        q = q.where(Article.region == region)
    q = q.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(q)
    return result.scalars().all()


# ── Trending (uses ArticleBriefOut) — V18: Redis cached (5 min TTL) ──
@app.get("/api/articles/trending", response_model=list[ArticleBriefOut])
async def trending_articles(
    limit: int = Query(6, ge=1, le=20),
    days: int = Query(7, ge=1, le=30),
    db=Depends(get_db),
):
    cache_key = f"trending:{limit}:{days}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    cutoff = datetime.utcnow() - timedelta(days=days)
    # V29: Include NULL status (pre-V24 articles) alongside published
    q = (
        select(Article)
        .where(or_(Article.status == "published", Article.status == None))  # noqa: E711
        .where(Article.date >= cutoff)
        .order_by(Article.views.desc(), Article.date.desc())
        .limit(limit)
    )
    result = await db.execute(q)
    articles = result.scalars().all()
    _cache_set(cache_key, [ArticleBriefOut.model_validate(a).model_dump() for a in articles], CACHE_TTL_MED)
    return articles


# ── V32: Most Read Articles (views-based, separate from trending) ──
# MUST be registered BEFORE /api/articles/{article_id} so FastAPI doesn't
# try to parse "most-read" as an integer article_id.
@app.get("/api/articles/most-read", response_model=list[ArticleBriefOut])
async def most_read_articles(
    limit: int = Query(6, ge=1, le=20),
    days: int = Query(7, ge=1, le=90),
    db=Depends(get_db),
):
    """V32: Return the most-read articles in the last N days (by view count).
    Distinct from /trending which is based on recent likes + comments.
    Most Read is purely views-based — what people are actually reading."""
    cache_key = f"most_read:{limit}:{days}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    cutoff = datetime.utcnow() - timedelta(days=days)
    result = await db.execute(
        select(Article)
        .where(or_(Article.status == "published", Article.status == None))  # noqa: E711
        .where(Article.date >= cutoff)
        .order_by(Article.views.desc())
        .limit(limit)
    )
    articles = result.scalars().all()
    out = [ArticleBriefOut.model_validate(a).model_dump() for a in articles]
    _cache_set(cache_key, out, CACHE_TTL_MED)
    return out


# ── V32: Editor's Picks (admin-curated featured articles) ──
# Also registered before /api/articles/{article_id} for the same reason.
@app.get("/api/articles/editors-picks", response_model=list[ArticleBriefOut])
async def editors_picks(
    limit: int = Query(4, ge=1, le=10),
    db=Depends(get_db),
):
    """V32: Return editor's picks — articles with the highest quality_score
    that have been fact-checked. Used for the homepage 'Editor's Picks' section
    to showcase the best journalism on the site."""
    cache_key = f"editors_picks:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    result = await db.execute(
        select(Article)
        .where(or_(Article.status == "published", Article.status == None))  # noqa: E711
        .where(Article.fact_check_status == "verified")
        .order_by(Article.views.desc())
        .limit(limit * 3)
    )
    articles = result.scalars().all()
    long_articles = [a for a in articles if (a.word_count or 0) >= 1500]
    picks = (long_articles or articles)[:limit]
    out = [ArticleBriefOut.model_validate(a).model_dump() for a in picks]
    _cache_set(cache_key, out, CACHE_TTL_MED)
    return out


# ── Single Article (FULL ArticleOut with ai_content) ──
@app.get("/api/articles/{article_id}", response_model=ArticleOut)
async def get_article(article_id: int, db=Depends(get_db)):
    result = await db.execute(select(Article).where(Article.id == article_id))
    a = result.scalars().first()  # V31: was scalar_one_or_none() — crashes on duplicate IDs
    if not a:
        raise HTTPException(404, "Article not found")
    # Bug #18 FIX: Public endpoint must NOT expose draft/pending_review/rejected
    # articles. Only "published" articles (or NULL status — pre-V24 backfill)
    # are publicly viewable. Drafts are admin-only via /api/admin/engine/drafts
    # and /api/admin/trends/drafts.
    if a.status is not None and a.status != "published":
        raise HTTPException(404, "Article not found")
    # V31: Atomic view count increment — avoids race condition where concurrent
    # requests could lose count. UPDATE ... SET views = views + 1 is atomic at the
    # DB level. Use UPDATE statement directly instead of ORM load+save.
    try:
        from sqlalchemy import update as _update
        await db.execute(
            _update(Article).where(Article.id == article_id).values(
                views=(Article.views or 0) + 1,
                updated_at=datetime.utcnow(),
            )
        )
        await db.commit()
        # Refresh the in-memory object so the response reflects the new count
        await db.refresh(a)
    except Exception:
        await db.rollback()
    return a


# ── Article by Slug (SEO-friendly) ──
@app.get("/api/article/{slug}", response_model=ArticleOut)
async def get_article_by_slug(slug: str, db=Depends(get_db)):
    # V31: Use .scalars().first() — robust against duplicate slugs
    # (multiple results would crash scalar_one_or_none() with MultipleResultsFound)
    result = await db.execute(select(Article).where(Article.slug == slug).limit(1))
    a = result.scalars().first()
    if not a:
        raise HTTPException(404, "Article not found")
    # Bug #18 FIX: same as /api/articles/{id} — drafts must not be publicly
    # accessible via slug URL either.
    if a.status is not None and a.status != "published":
        raise HTTPException(404, "Article not found")
    # V31: Atomic view count increment
    try:
        from sqlalchemy import update as _update
        await db.execute(
            _update(Article).where(Article.id == a.id).values(
                views=(Article.views or 0) + 1,
                updated_at=datetime.utcnow(),
            )
        )
        await db.commit()
        await db.refresh(a)
    except Exception:
        await db.rollback()
    return a


# ── Delete Article (Admin) ──
@app.delete("/api/articles/{article_id}")
async def delete_article(article_id: int, request: Request, db=Depends(get_db)):
    _require_admin(request)
    result = await db.execute(select(Article).where(Article.id == article_id))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "Article not found")
    await db.delete(a)
    await db.commit()
    logger.info(f"Article {article_id} deleted by admin")
    return {"status": "deleted", "id": article_id}


# ── Admin: Manual Publish ──
@app.post("/api/admin/articles", response_model=ArticleOut)
async def publish_article(data: ManualArticleIn, request: Request, db=Depends(get_db)):
    _require_admin(request)
    # V31.1: Title uniqueness check — if the admin's title is a duplicate or
    # near-duplicate of an existing article, append a suffix.
    title = data.title.strip()
    try:
        from title_uniqueness import ensure_unique_title, compute_title_norm
        title = await ensure_unique_title(db, title)
        if title != data.title.strip():
            logger.info(f"[V31.1] Manual publish: title deduped → '{title[:80]}'")
    except Exception as e:
        logger.warning(f"[V31.1] Title uniqueness check failed (saving anyway): {e}")
        title = data.title.strip()

    slug = _make_slug(title)
    base_slug = slug
    n = 1
    while True:
        result = await db.execute(select(Article).where(Article.slug == slug))
        if not result.scalars().first():
            break
        n += 1
        slug = f"{base_slug}-{n}"

    content_hash = hashlib.sha256(data.content.encode()).hexdigest()
    synthetic_url = f"manual://{slug}-{int(time.time())}"

    # V31.1: Compute title_norm for the new article
    _title_norm = None
    try:
        from title_uniqueness import compute_title_norm as _ctn
        _title_norm = _ctn(title)[:500]
    except Exception:
        pass

    article = Article(
        title=title,
        slug=slug,
        original_url=synthetic_url,
        ai_content=data.content.strip(),
        summary=(data.summary or data.content[:280]).strip(),
        image_url=data.image_url or "",
        region=data.region,
        meta_desc=(data.meta_desc or data.content[:160]).strip(),
        keywords=data.keywords or "",
        article_hash=content_hash,
        title_norm=_title_norm,  # V31.1: for fast duplicate detection
        # V18: Auto-generate TL;DR + default fact-check status
        tldr_summary=_generate_tldr_safe(data.content.strip()),
        fact_check_status="under_review",
        audio_status="pending",
    )
    db.add(article)
    await db.commit()
    await db.refresh(article)
    logger.info(f"Article '{article.title}' manually published by admin (id={article.id})")
    return article


# ── Admin Login ──
@app.post("/api/admin/login")
async def admin_login(data: AdminLoginIn, request: Request):
    """V12: Rate-limited admin login to prevent brute-force attacks.
    V24: Also issues a CSRF token on success — admin must include it
    in X-CSRF-Token header for all subsequent state-changing requests."""
    if not ADMIN_PASSWORD_HASH:
        raise HTTPException(500, "Admin password not configured")
    # V12: Rate limit login attempts (10 per 15 minutes per IP)
    client_ip = _get_client_ip(request)
    if not _check_rate_limit(f"login:{client_ip}", 10, 900):
        raise HTTPException(429, "Too many login attempts. Please wait 15 minutes.")
    if not _verify_admin_password(data.password):
        time.sleep(1.5)  # V12: Increased brute-force delay
        # V24: Audit log failed login
        try:
            from monitoring import log_audit_event
            log_audit_event(
                admin_id=client_ip, action="admin.login_failed",
                details={"ip": client_ip},
                ip_address=client_ip, success=False,
            )
        except Exception:
            pass
        raise HTTPException(401, "Invalid password")
    token = _create_session()
    csrf = _issue_csrf_token(token)
    # V24: Audit log successful login
    try:
        from monitoring import log_audit_event
        log_audit_event(
            admin_id=client_ip, action="admin.login_success",
            ip_address=client_ip,
        )
    except Exception:
        pass
    return {"status": "authenticated", "token": token, "csrf_token": csrf}


# ── Admin Verify Session ──
@app.get("/api/admin/verify")
async def admin_verify(request: Request):
    token = request.headers.get("x-admin-session", "")
    if _verify_session(token):
        # V24: Refresh CSRF token on verify (so admin can get a new one
        # without re-logging in)
        csrf = request.headers.get("x-csrf-token", "")
        if not csrf or not _verify_csrf_token(token, csrf):
            csrf = _issue_csrf_token(token)
        return {"status": "authenticated", "csrf_token": csrf}
    raise HTTPException(401, "Session expired or invalid")


# ── Admin Logout ──
@app.post("/api/admin/logout")
async def admin_logout(request: Request):
    token = request.headers.get("x-admin-session", "")
    if token:
        _destroy_session(token)
    return {"status": "logged_out"}


# ── Admin: List Contact Messages ──
@app.get("/api/admin/contacts")
async def list_contacts(request: Request, db=Depends(get_db)):
    _require_admin(request)
    result = await db.execute(select(ContactMessage).order_by(ContactMessage.sent_at.desc()))
    messages = result.scalars().all()
    return [
        {
            "id": m.id, "name": m.name, "email": m.email, "subject": m.subject,
            "message": m.message,
            "sent_at": m.sent_at.isoformat() if m.sent_at else None,
            "read": bool(m.read),
        }
        for m in messages
    ]


# ── Admin: Mark Contact as Read ──
@app.post("/api/admin/contacts/{msg_id}/read")
async def mark_contact_read(msg_id: int, request: Request, db=Depends(get_db)):
    _require_admin(request)
    result = await db.execute(select(ContactMessage).where(ContactMessage.id == msg_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(404, "Message not found")
    msg.read = 1
    await db.commit()
    return {"status": "marked_read", "id": msg_id}


# ── Search (V23: FTS5 / GIN full-text search — Wikipedia-fast) ──
@app.get("/api/search", response_model=list[ArticleBriefOut])
async def search(
    q: str = Query(..., min_length=2, max_length=100),
    page: int = Query(1, ge=1),
    per_page: int = Query(12, ge=1, le=50),
    db=Depends(get_db),
):
    """V23: Uses FTS5 (SQLite) or GIN tsvector (PostgreSQL) for instant
    full-text search across millions of articles. Falls back to LIKE if
    the FTS index is missing for any reason (e.g. mid-migration)."""
    from database import IS_POSTGRES, IS_SQLITE
    import re as _re

    # Strip SQL LIKE wildcards + backslash, then collapse whitespace
    safe_q = _re.sub(r"[%_\\]", " ", q)
    safe_q = _re.sub(r"\s+", " ", safe_q).strip()
    if not safe_q or len(safe_q) < 2:
        return []

    # Tokens with length >= 2 (FTS5 ignores single-char tokens)
    tokens = [t for t in safe_q.split() if len(t) >= 2]
    if not tokens:
        return []

    offset = (page - 1) * per_page

    try:
        if IS_SQLITE:
            # FTS5 MATCH syntax — wrap each token in double quotes (so special
            # chars inside the token don't break the parser) and append `*`
            # for prefix matching. e.g. ["pakistan","election"] →
            #   "pakistan"* "election"*
            fts_query = " ".join(f'"{tok}"*' for tok in tokens)
            stmt = text(
                "SELECT a.id FROM articles a "
                "JOIN articles_fts f ON a.id = f.rowid "
                "WHERE articles_fts MATCH :q "
                "ORDER BY bm25(articles_fts) ASC "
                "LIMIT :limit OFFSET :offset"
            )
            result = await db.execute(
                stmt, {"q": fts_query, "limit": per_page, "offset": offset}
            )
            ids = [r[0] for r in result.fetchall()]
            if not ids:
                return []
            # Re-fetch as ORM objects preserving FTS ranking order
            art_result = await db.execute(
                select(Article).where(Article.id.in_(ids))
            )
            arts_by_id = {a.id: a for a in art_result.scalars().all()}
            return [arts_by_id[i] for i in ids if i in arts_by_id]

        elif IS_POSTGRES:
            # GIN tsvector — use plainto_tsquery (safer than to_tsquery,
            # handles user input without & | ! operators)
            stmt = text(
                "SELECT a.id FROM articles a "
                "WHERE to_tsvector('english', coalesce(a.title,'') || ' ' || "
                "coalesce(a.summary,'') || ' ' || coalesce(a.keywords,'')) "
                "@@ plainto_tsquery('english', :q) "
                "ORDER BY ts_rank("
                "  to_tsvector('english', coalesce(a.title,'') || ' ' || "
                "  coalesce(a.summary,'') || ' ' || coalesce(a.keywords,'')), "
                "  plainto_tsquery('english', :q)"
                ") DESC "
                "LIMIT :limit OFFSET :offset"
            )
            result = await db.execute(
                stmt, {"q": safe_q, "limit": per_page, "offset": offset}
            )
            ids = [r[0] for r in result.fetchall()]
            if not ids:
                return []
            art_result = await db.execute(
                select(Article).where(Article.id.in_(ids))
            )
            arts_by_id = {a.id: a for a in art_result.scalars().all()}
            return [arts_by_id[i] for i in ids if i in arts_by_id]

    except Exception as e:
        logger.warning(f"[V23 FTS] Falling back to LIKE search: {e}")

    # ── LIKE fallback (always works, slower on big DBs) ──
    term = f"%{safe_q}%"
    stmt = (
        select(Article)
        .where(or_(
            Article.title.ilike(term),
            Article.summary.ilike(term),
            Article.keywords.ilike(term),
        ))
        .order_by(Article.date.desc())
        .offset(offset)
        .limit(per_page)
    )
    result = await db.execute(stmt)
    return result.scalars().all()


# ── Stats ──
@app.get("/api/stats")
async def stats(db=Depends(get_db)):
    # V18: Cache stats for 1 hour (they don't change often and are hit by every page load)
    cache_key = "stats:global"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    total = (await db.execute(select(func.count(Article.id)))).scalar_one()
    by_region: dict[str, int] = {}
    for r in REGIONS:
        by_region[r] = (await db.execute(
            select(func.count(Article.id)).where(Article.region == r)
        )).scalar_one()
    today = datetime.utcnow().date()
    today_count = (await db.execute(
        select(func.count(Article.id)).where(func.date(Article.date) == today)
    )).scalar_one()
    subscribers = (await db.execute(
        select(func.count(Subscriber.id)).where(Subscriber.active == 1)
    )).scalar_one()
    total_views = (await db.execute(select(func.sum(Article.views)))).scalar_one() or 0
    result = {
        "total": total, "by_region": by_region, "today": today_count,
        "subscribers": subscribers, "total_views": total_views,
    }
    _cache_set(cache_key, result, CACHE_TTL_LONG)
    return result


# ── Contact Form ──
@app.post("/api/contact")
async def contact(data: ContactIn, request: Request, db=Depends(get_db)):
    client_ip = _get_client_ip(request)
    if not _check_rate_limit(f"contact:{client_ip}", CONTACT_RATE_LIMIT, 3600):
        raise HTTPException(429, "Too many contact submissions. Please try again later.")
    try:
        msg = ContactMessage(
            name=data.name, email=data.email,
            subject=data.subject or "No Subject", message=data.message,
        )
        db.add(msg)
        await db.commit()
        logger.info(f"Contact message from {data.email}")
        return {"status": "success", "message": "Message received! We will get back to you soon."}
    except Exception as e:
        await db.rollback()
        logger.error(f"Contact form error: {e}")
        raise HTTPException(500, "Failed to save message. Please try again later.")


# ── Newsletter Subscribe ──
@app.post("/api/subscribe")
async def subscribe(data: SubscribeIn, request: Request, db=Depends(get_db)):
    client_ip = _get_client_ip(request)
    if not _check_rate_limit(f"subscribe:{client_ip}", SUBSCRIBE_RATE_LIMIT, 3600):
        raise HTTPException(429, "Too many subscription attempts. Please try again later.")
    try:
        result = await db.execute(select(Subscriber).where(Subscriber.email == data.email))
        existing = result.scalar_one_or_none()
        if existing:
            if existing.active == 1:
                return {"status": "already_subscribed", "email": data.email}
            existing.active = 1
            await db.commit()
            return {"status": "reactivated", "email": data.email}
        sub = Subscriber(email=data.email)
        db.add(sub)
        await db.commit()
        logger.info(f"New subscriber: {data.email}")
        return {"status": "subscribed", "email": data.email}
    except Exception as e:
        await db.rollback()
        logger.error(f"Subscribe error: {e}")
        raise HTTPException(500, "Failed to subscribe. Please try again.")


# ── Unsubscribe (V7 — POST body, not query param) ──
@app.post("/api/unsubscribe")
async def unsubscribe(data: UnsubscribeIn, db=Depends(get_db)):
    try:
        result = await db.execute(
            select(Subscriber).where(Subscriber.email == data.email.lower().strip())
        )
        sub = result.scalar_one_or_none()
        if not sub:
            raise HTTPException(404, "Email not found")
        sub.active = 0
        await db.commit()
        return {"status": "unsubscribed", "email": data.email}
    except HTTPException:
        raise
    except Exception:
        await db.rollback()
        raise HTTPException(500, "Failed to unsubscribe")


# ════════════════════════════════════════════════════════════
#  V8: ARTICLE ENGAGEMENT ENDPOINTS (Likes + Comments)
# ════════════════════════════════════════════════════════════

def _user_fingerprint(request: Request, provided: Optional[str] = None) -> str:
    """Per-browser fingerprint — uses client IP + a random browser-stored
    ID sent from localStorage. This lets us track likes per-browser without
    requiring user accounts."""
    ip = _get_client_ip(request)
    if provided and len(provided) <= 64:
        return f"{ip}:{provided}"
    return ip


@app.get("/api/articles/{article_id}/engagement")
async def get_engagement(article_id: int, db=Depends(get_db)):
    """Get like count + comment count for an article."""
    likes = (await db.execute(
        select(func.count(ArticleLike.id)).where(ArticleLike.article_id == article_id)
    )).scalar_one()
    comments = (await db.execute(
        select(func.count(ArticleComment.id)).where(ArticleComment.article_id == article_id)
    )).scalar_one()
    return {"likes": likes, "comments": comments}


@app.post("/api/articles/{article_id}/like")
async def toggle_like(article_id: int, data: LikeIn, request: Request, db=Depends(get_db)):
    """Like or unlike an article. Per-browser dedup via fingerprint."""
    # Verify article exists
    art = (await db.execute(select(Article).where(Article.id == article_id))).scalars().first()
    if not art:
        raise HTTPException(404, "Article not found")
    # V31: Block likes on non-published articles (drafts/pending/rejected)
    if art.status is not None and art.status != "published":
        raise HTTPException(404, "Article not found")

    fp = _user_fingerprint(request, data.fingerprint)

    # Rate limit likes (50/hour per fingerprint)
    if not _check_rate_limit(f"like:{fp}", 50, 3600):
        raise HTTPException(429, "Too many like actions. Please slow down.")

    existing = (await db.execute(
        select(ArticleLike).where(
            ArticleLike.article_id == article_id,
            ArticleLike.user_fingerprint == fp,
        )
    )).scalar_one_or_none()

    try:
        if data.action == "like":
            if not existing:
                db.add(ArticleLike(article_id=article_id, user_fingerprint=fp))
                await db.commit()
        else:  # unlike
            if existing:
                await db.delete(existing)
                await db.commit()
    except Exception:
        await db.rollback()

    # Return updated counts
    likes = (await db.execute(
        select(func.count(ArticleLike.id)).where(ArticleLike.article_id == article_id)
    )).scalar_one()
    return {"likes": likes, "action": data.action}


@app.get("/api/articles/{article_id}/comments")
async def list_comments(article_id: int, db=Depends(get_db)):
    """List all comments for an article, newest first."""
    result = await db.execute(
        select(ArticleComment)
        .where(ArticleComment.article_id == article_id)
        .order_by(ArticleComment.created_at.desc())
        .limit(200)
    )
    comments = result.scalars().all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "comment": c.comment,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in comments
    ]


@app.post("/api/articles/{article_id}/comments")
async def post_comment(article_id: int, data: CommentIn, request: Request, db=Depends(get_db)):
    """Post a new comment on an article. Rate-limited per IP."""
    # Verify article exists
    art = (await db.execute(select(Article).where(Article.id == article_id))).scalars().first()
    if not art:
        raise HTTPException(404, "Article not found")
    # V31: Block comments on non-published articles
    if art.status is not None and art.status != "published":
        raise HTTPException(404, "Article not found")

    client_ip = _get_client_ip(request)
    if not _check_rate_limit(f"comment:{client_ip}", 10, 3600):
        raise HTTPException(429, "Too many comments. Please try again later.")

    # Basic XSS/abuse sanitization — strip HTML
    import re as _re
    clean_name = _re.sub(r"<[^>]+>", "", data.name.strip())[:80] or "Anonymous"
    clean_comment = _re.sub(r"<[^>]+>", "", data.comment.strip())[:1000]

    if len(clean_comment) < 2:
        raise HTTPException(400, "Comment too short")

    comment = ArticleComment(
        article_id=article_id,
        name=clean_name,
        comment=clean_comment,
    )
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    return {
        "id": comment.id,
        "name": comment.name,
        "comment": comment.comment,
        "created_at": comment.created_at.isoformat() if comment.created_at else None,
    }


# ── Manual Pipeline Trigger (FIXED: Proper error capture in thread) ──
@app.post("/api/trigger")
async def trigger(request: Request):
    """V13 FIX: Pipeline trigger validates before starting, captures ALL errors
    in the background thread, and stores them in pipeline_result so the admin
    polling endpoint shows the REAL result — never a false success."""
    _require_admin(request)

    # Import pipeline_result so we can reset it from here
    from scheduler import pipeline_result as pr

    # Quick pre-check — verify DB is reachable before starting
    try:
        from sqlalchemy import text as sql_text
        async with AsyncSessionLocal() as test_db:
            await test_db.execute(sql_text("SELECT 1"))
    except Exception as e:
        pr.update({"running": False, "last_error": f"DB unreachable: {type(e).__name__}: {str(e)[:150]}", "saved": 0, "failed": 0, "skipped": 0})
        return JSONResponse(
            status_code=503,
            content={"detail": f"Database not reachable: {type(e).__name__}: {str(e)[:150]}. Pipeline NOT started."}
        )

    # Check if already running
    if pr.get("running", False):
        return JSONResponse(
            status_code=409,
            content={"detail": "Pipeline is already running. Wait for it to finish or check /api/pipeline-status."}
        )

    # Reset result and start in background thread
    pr.update({"running": True, "last_error": "", "last_run_ts": datetime.utcnow().isoformat(), "saved": 0, "failed": 0, "skipped": 0})

    def _run_with_error_capture():
        """Wraps run_pipeline to catch ALL exceptions including silent ones."""
        try:
            run_pipeline()
        except Exception as e:
            logger.error(f"Pipeline thread crashed: {type(e).__name__}: {e}")
            pr.update({"running": False, "last_error": f"Thread crashed: {type(e).__name__}: {str(e)[:300]}", "saved": 0, "failed": 0, "skipped": 0})

    t = threading.Thread(target=_run_with_error_capture, daemon=True)
    t.start()
    return {
        "status": "Pipeline started in background.",
        "check_status": "/api/pipeline-status",
        "note": "Pipeline runs in background. Use the status endpoint or wait for auto-poll."
    }


# ── V12: Pipeline status endpoint — lets admin poll for actual results ──
@app.get("/api/pipeline-status")
async def pipeline_status(request: Request):
    """V12: Returns actual pipeline result. Admin polls this after triggering."""
    _require_admin(request)
    # Import the shared result dict from scheduler
    try:
        from scheduler import pipeline_result
        return pipeline_result
    except ImportError:
        return {"running": False, "last_error": "scheduler module not available", "saved": 0, "failed": 0, "skipped": 0}


# ── V9: DEBUG Pipeline — runs synchronously and returns detailed diagnostics ──
# This endpoint exists so when "no articles are appearing", you can hit this
# and see EXACTLY what's failing: which feeds work, which don't, whether AI
# keys are valid, whether the DB is reachable, etc.
@app.get("/api/debug/pipeline")
async def debug_pipeline(request: Request, db=Depends(get_db)):
    """V12: Now requires admin auth (was public before — security risk)."""
    _require_admin(request)
    client_ip = _get_client_ip(request)
    if not _check_rate_limit(f"debug:{client_ip}", 5, 300):  # 5 per 5 min
        raise HTTPException(429, "Debug endpoint rate-limited. Try again in 5 min.")

    diagnostics = {
        "timestamp": datetime.utcnow().isoformat(),
        "version": "26.0",
        "checks": {},
    }

    # ── Check 1: Database ──
    try:
        from sqlalchemy import text as sql_text
        result = await db.execute(sql_text("SELECT COUNT(*) FROM articles"))
        count = result.scalar_one()
        diagnostics["checks"]["database"] = {
            "status": "ok",
            "articles_in_db": count,
        }
    except Exception as e:
        diagnostics["checks"]["database"] = {
            "status": "FAIL",
            "error": f"{type(e).__name__}: {str(e)[:200]}",
        }

    # ── Check 2: Redis ──
    try:
        if _redis_client:
            _redis_client.ping()
            diagnostics["checks"]["redis"] = {"status": "ok"}
        else:
            diagnostics["checks"]["redis"] = {
                "status": "NOT_CONFIGURED",
                "note": "REDIS_URL not set — using in-memory (single-worker only)",
            }
    except Exception as e:
        diagnostics["checks"]["redis"] = {
            "status": "FAIL",
            "error": f"{type(e).__name__}: {str(e)[:200]}",
        }

    # ── Check 3: AI Keys ──
    ai_keys_status = {}
    for region in REGIONS:
        keys = {
            "groq": bool(os.getenv(f"GROQ_KEY_{region.upper()}", "") and
                        os.getenv(f"GROQ_KEY_{region.upper()}", "") != "your_groq_key"),
            "gemini": bool(os.getenv(f"GEMINI_KEY_{region.upper()}", "") and
                          os.getenv(f"GEMINI_KEY_{region.upper()}", "") != "your_gemini_key"),
        }
        ai_keys_status[region] = keys
    diagnostics["checks"]["ai_keys"] = ai_keys_status
    diagnostics["checks"]["ai_keys_summary"] = {
        "any_valid_groq": any(k["groq"] for k in ai_keys_status.values()),
        "any_valid_gemini": any(k["gemini"] for k in ai_keys_status.values()),
        "note": "If both are false, articles will use emergency fallback (just RSS summary).",
    }

    # ── Check 4: Test RSS feeds (try 2 sources per region) ──
    feed_test_results = {}
    try:
        from scraper import RSS_SOURCES, _fetch_feed
        import httpx
        async with httpx.AsyncClient() as client:
            for region, sources in RSS_SOURCES.items():
                # Test first source of each region
                if sources:
                    source = sources[0]
                    try:
                        entries = await _fetch_feed(client, source, region)
                        feed_test_results[region] = {
                            "source": source["name"],
                            "status": "ok" if entries else "EMPTY",
                            "entries": len(entries),
                        }
                    except Exception as e:
                        feed_test_results[region] = {
                            "source": source["name"],
                            "status": "FAIL",
                            "error": f"{type(e).__name__}: {str(e)[:150]}",
                        }
        diagnostics["checks"]["rss_feeds"] = feed_test_results
    except Exception as e:
        diagnostics["checks"]["rss_feeds"] = {
            "status": "FAIL",
            "error": f"{type(e).__name__}: {str(e)[:200]}",
        }

    # ── Check 5: Run a tiny scrape+save cycle for ONE article ──
    try:
        from scraper import get_new_articles
        # Get 1 new article and try to save it
        diagnostics["checks"]["test_scrape"] = {"status": "running"}
        # Use empty processed set to allow any article
        articles = await get_new_articles(set())
        if not articles:
            diagnostics["checks"]["test_scrape"] = {
                "status": "NO_ARTICLES_FOUND",
                "note": "All RSS feeds failed OR all bodies failed to scrape. Check rss_feeds above.",
            }
        else:
            first = articles[0]
            diagnostics["checks"]["test_scrape"] = {
                "status": "ok",
                "first_article_title": first.get("title", "")[:100],
                "first_article_region": first.get("region", ""),
                "first_article_body_length": len(first.get("full_text", "")),
                "has_image": bool(first.get("image_url")),
                "total_new_articles_found": len(articles),
            }
            # Try to save the first one
            try:
                from ai_writer import rewrite_article, make_slug, make_article_hash
                from database import Article as ArticleModel, ProcessedURL as PURLModel
                # Don't actually rewrite — just save the raw body to test DB write
                test_title = first.get("title", "Test Article")[:200]
                slug = make_slug(test_title)
                # Check if already exists
                existing = (await db.execute(
                    select(Article).where(Article.original_url == first["url"])
                )).scalar_one_or_none()
                if existing:
                    diagnostics["checks"]["test_save"] = {
                        "status": "ALREADY_EXISTS",
                        "article_id": existing.id,
                    }
                else:
                    new_art = ArticleModel(
                        title=test_title,
                        slug=slug,
                        original_url=first["url"],
                        ai_content=first.get("full_text", "No content")[:5000],
                        summary=first.get("summary", "")[:280],
                        image_url=first.get("image_url", ""),
                        region=first["region"],
                        meta_desc=first.get("summary", "")[:155],
                        keywords="",
                        article_hash=make_article_hash(test_title, first.get("full_text", "")),
                        status="draft",  # V31: Save as DRAFT, not published — debug must not auto-publish
                    )
                    db.add(new_art)
                    db.add(PURLModel(url=first["url"]))
                    await db.commit()
                    await db.refresh(new_art)
                    diagnostics["checks"]["test_save"] = {
                        "status": "SAVED_OK",
                        "article_id": new_art.id,
                        "title": new_art.title[:100],
                        "note": "Article saved as DRAFT (V31 fix — debug pipeline no longer auto-publishes). If this works, DB is fine — issue is in AI keys.",
                    }
            except Exception as e:
                await db.rollback()
                diagnostics["checks"]["test_save"] = {
                    "status": "FAIL",
                    "error": f"{type(e).__name__}: {str(e)[:200]}",
                }
    except Exception as e:
        diagnostics["checks"]["test_scrape"] = {
            "status": "FAIL",
            "error": f"{type(e).__name__}: {str(e)[:200]}",
        }

    # ── Final summary ──
    all_checks = diagnostics["checks"]
    failing = [name for name, result in all_checks.items()
               if isinstance(result, dict) and result.get("status") in ("FAIL", "NO_ARTICLES_FOUND")]
    diagnostics["summary"] = {
        "total_checks": len(all_checks),
        "failing_checks": failing,
        "recommendation": (
            "All checks passed — pipeline should be working. Check /api/articles endpoint."
            if not failing else
            f"Failing: {', '.join(failing)}. Fix these issues first."
        ),
    }

    return diagnostics


# ── V9: List articles count by region (FIXED: Admin-only + cross-DB query) ──
@app.get("/api/debug/articles-count")
async def articles_count(request: Request, db=Depends(get_db)):
    """Quick check: how many articles in DB, by region. Requires admin auth."""
    _require_admin(request)
    from sqlalchemy import text as sql_text
    try:
        total = (await db.execute(sql_text("SELECT COUNT(*) FROM articles"))).scalar_one()
        by_region_rows = (await db.execute(
            sql_text("SELECT region, COUNT(*) FROM articles GROUP BY region ORDER BY COUNT(*) DESC")
        )).fetchall()
        # Cross-DB compatible last 24h query
        from database import IS_POSTGRES
        if IS_POSTGRES:
            last_24h = (await db.execute(
                sql_text("SELECT COUNT(*) FROM articles WHERE date > NOW() - INTERVAL '1 day'")
            )).scalar_one()
        else:
            last_24h = (await db.execute(
                sql_text("SELECT COUNT(*) FROM articles WHERE date > datetime('now', '-1 day')")
            )).scalar_one()
        return {
            "total": total,
            "by_region": {row[0]: row[1] for row in by_region_rows},
            "last_24h": last_24h,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


# ════════════════════════════════════════════════════════════
#  V12: ADMIN COMMENT MODERATION
# ════════════════════════════════════════════════════════════

@app.get("/api/admin/comments")
async def admin_list_comments(request: Request, db=Depends(get_db)):
    """List all comments across all articles for admin moderation."""
    _require_admin(request)
    result = await db.execute(
        select(ArticleComment).order_by(ArticleComment.created_at.desc()).limit(200)
    )
    comments = result.scalars().all()
    return [
        {
            "id": c.id, "article_id": c.article_id, "name": c.name,
            "comment": c.comment,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in comments
    ]


@app.delete("/api/admin/comments/{comment_id}")
async def admin_delete_comment(comment_id: int, request: Request, db=Depends(get_db)):
    """Admin can delete any comment."""
    _require_admin(request)
    result = await db.execute(
        select(ArticleComment).where(ArticleComment.id == comment_id)
    )
    comment = result.scalar_one_or_none()
    if not comment:
        raise HTTPException(404, "Comment not found")
    await db.delete(comment)
    await db.commit()
    logger.info(f"Comment {comment_id} deleted by admin")
    return {"status": "deleted", "id": comment_id}


# ════════════════════════════════════════════════════════════
#  V18: WIKIPEDIA-KILLER ENDPOINTS
#  - Fact-check status management (admin)
#  - TL;DR summary generation (auto)
# ════════════════════════════════════════════════════════════

class FactCheckUpdateIn(BaseModel):
    """V18: Admin can update fact-check status of an article."""
    status: str = Field(..., pattern="^(verified|under_review|community_fact_checked)$")


@app.patch("/api/admin/articles/{article_id}/fact-check")
async def update_fact_check_status(article_id: int, data: FactCheckUpdateIn, request: Request, db=Depends(get_db)):
    """V18: Update an article's fact-check verification status.
    Statuses: verified | under_review | community_fact_checked"""
    _require_admin(request)
    result = await db.execute(select(Article).where(Article.id == article_id))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "Article not found")
    a.fact_check_status = data.status
    a.updated_at = datetime.utcnow()
    await db.commit()
    # Invalidate caches that include fact_check_status
    _cache_invalidate_pattern("trending:*")
    _cache_invalidate_pattern("articles:*")
    logger.info(f"Article {article_id} fact-check status → {data.status}")
    return {"status": "updated", "id": article_id, "fact_check_status": data.status}


@app.post("/api/admin/articles/{article_id}/regenerate-tldr")
async def regenerate_tldr(article_id: int, request: Request, db=Depends(get_db)):
    """V18: Regenerate TL;DR summary for an article (admin only).
    Uses the local _generate_tldr function from ai_writer."""
    _require_admin(request)
    result = await db.execute(select(Article).where(Article.id == article_id))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "Article not found")
    from ai_writer import _generate_tldr
    a.tldr_summary = _generate_tldr(a.ai_content or "")
    a.updated_at = datetime.utcnow()
    await db.commit()
    logger.info(f"Article {article_id} TL;DR regenerated")
    return {"status": "updated", "id": article_id, "tldr_summary": a.tldr_summary}


# ── Health Check ──
# CRITICAL: This endpoint MUST always return 200.
# Railway uses it for health checks — if it returns 500, the deploy fails.
# So we wrap EVERYTHING in try/except and never let it crash.
@app.get("/health")
async def health():
    """Liveness probe — always returns 200 if the process is alive.
    Railway/K8s use this to decide whether to restart the container.
    Does NOT check DB / Redis (those are /ready's job)."""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "30.0",
        "features": ["async_db", "redis_rate_limit", "two_agent_ai", "ad_fallback",
                     "image_proxy", "tldr_summaries", "fact_check_badges",
                     "fts5_search", "dynamic_categories", "server_rendered_seo",
                     "rss_feed", "draft_mode", "quality_control", "fact_check_ai",
                     "semantic_dedup", "ai_recommendations", "leader_election",
                     "google_search_writer", "conversational_style", "audit_logs",
                     "trends_pipeline", "zero_hallucination_engine", "csrf_protection",
                     "multilingual_translation", "ai_voice_narration", "daily_podcast",
                     # V30 (TRD v1.0) features
                     "automated_news_engine", "multi_region_isolation",
                     "per_region_llm_keys", "resilient_llm_fallback",
                     "exponential_backoff", "dynamic_word_count_tiers",
                     "cross_source_fact_verification", "rolling_dedup_engine",
                     "engine_cycle_audit_log", "trend_detection_ranking"],
    }


@app.get("/ready")
async def readiness(db=Depends(get_db)):
    """Readiness probe — returns 200 only when the app is ready to serve
    real traffic. Checks DB, Redis, and scheduler. Returns 503 if any
    critical dependency is down so the load balancer stops sending traffic."""
    checks = {"db": "unknown", "redis": "unknown", "scheduler": "unknown"}
    all_ok = True

    # ── DB check ──
    try:
        from sqlalchemy import text as _sql_text
        await db.execute(_sql_text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"down: {type(e).__name__}"
        all_ok = False

    # ── Redis check (optional — degraded mode is acceptable) ──
    try:
        if _redis_client is None:
            checks["redis"] = "not_configured"
        else:
            _redis_client.ping()
            checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"down: {type(e).__name__}"
        # Redis down → degraded but still ready (in-memory fallback works
        # for single-instance; for multi-instance we'd want 503)

    # ── Scheduler check ──
    try:
        from scheduler import pipeline_result
        if pipeline_result.get("running"):
            checks["scheduler"] = "running"
        else:
            checks["scheduler"] = "idle"
    except Exception:
        checks["scheduler"] = "unknown"

    http_status = 200 if all_ok else 503
    return JSONResponse(
        status_code=http_status,
        content={
            "status": "ready" if all_ok else "not_ready",
            "timestamp": datetime.utcnow().isoformat(),
            "checks": checks,
        },
    )


# ════════════════════════════════════════════════════════════
# V21: IMAGE PROXY ENDPOINT — UPGRADED WITH PILLOW
# Solves "article images not showing" AND "image quality not good" by:
#  1. Fetching publisher images server-side (bypasses hotlink protection)
#  2. Forcing HTTPS upstream (avoids mixed-content blocking on HTTPS site)
#  3. V21: Pillow-based optimization:
#     - Converts to RGB (strips alpha for JPEG/WebP)
#     - Resizes to max 1600px wide (configurable via ?w= param)
#     - Sharpens low-res images using Lanczos + UnsharpMask
#     - Returns WebP by default (smaller, better quality than JPEG)
#       with graceful fallback to JPEG for legacy browsers
#     - Quality 85 (optimal balance: ~30% smaller than Q90, visually identical)
#  4. Streaming with proper Content-Type + Cache-Control headers
#  5. Returns 404 (not 500) on failure so <img onerror> shows placeholder
# ════════════════════════════════════════════════════════════

# V21: Lazy-load Pillow (only when imgproxy is actually called)
_PILLOW_AVAILABLE = None  # type: ignore

def _get_pillow():
    """Lazy-load Pillow. Returns Image module or None if unavailable."""
    global _PILLOW_AVAILABLE
    if _PILLOW_AVAILABLE is False:
        return None
    if _PILLOW_AVAILABLE is not None:
        return _PILLOW_AVAILABLE
    try:
        from PIL import Image, ImageFilter
        _PILLOW_AVAILABLE = Image
        _PILLOW_AVAILABLE._filter = ImageFilter  # type: ignore
        return _PILLOW_AVAILABLE
    except ImportError:
        logger.warning("[V21] Pillow not installed — imgproxy will stream raw bytes (no quality enhancement). Install with: pip install Pillow")
        _PILLOW_AVAILABLE = False
        return None


def _optimize_image(content_bytes: bytes, max_width: int = 1600, quality: int = 85) -> tuple[bytes, str]:
    """V21: Optimize an image using Pillow.
    Returns (optimized_bytes, content_type).
    Falls back to original bytes + JPEG type if Pillow fails."""
    Image = _get_pillow()
    if Image is None:
        return content_bytes, "image/jpeg"
    try:
        import io
        from PIL import ImageFilter

        img = Image.open(io.BytesIO(content_bytes))
        # Strip alpha for JPEG/WebP output (otherwise PIL crashes on RGBA->JPEG)
        if img.mode in ("RGBA", "LA", "P"):
            # Composite onto white background for transparent PNGs/GIFs
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Resize if wider than max_width (preserves aspect ratio, Lanczos = best quality)
        original_width, original_height = img.size
        if original_width > max_width:
            new_height = int((max_width / original_width) * original_height)
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
            # V21: Apply light sharpening to compensate for downscale softness
            img = img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=80, threshold=2))

        # Save as WebP (better quality at smaller size) — JPEG fallback handled by Accept header
        out = io.BytesIO()
        # V21: Decide format based on browser Accept header (handled by caller)
        out_format = "WEBP"
        out_options = {"quality": quality, "method": 4}  # method 4 = good compression/speed balance
        try:
            img.save(out, format=out_format, **out_options)
            return out.getvalue(), "image/webp"
        except Exception:
            # WebP failed (very rare) — fall back to JPEG
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
            return out.getvalue(), "image/jpeg"
    except Exception as e:
        logger.debug(f"[V21] Image optimization failed: {e} — serving raw bytes")
        return content_bytes, "image/jpeg"


@app.get("/api/imgproxy")
async def imgproxy(request: Request):
    """V21: Stream an external image through SFAAM's server WITH optimization.
    V31: HARDENED — blocks SSRF to private/internal IPs, enforces size limit.

    Usage:  <img src="/api/imgproxy?url=https%3A%2F%2Fcnn.com%2Fimg.jpg&w=900">

    Query params:
      url  (required)  The upstream image URL
      w    (optional)  Max width in pixels (default: 1600, capped at 2400)
      q    (optional)  Quality 1-95 (default: 85)

    - Replaces http:// with https:// to avoid mixed-content blocking
    - Sends a desktop User-Agent so publishers don't block the request
    - V21: Pillow optimizes: resize + sharpen + WebP conversion
    - V21: 30-day Cache-Control (browsers + CDNs cache the optimized version)
    - V31: SSRF protection — blocks 169.254.169.254, 127.x, 10.x, 192.168.x, ::1, fc00::/7
    - V31: 8MB response size cap (prevents memory DoS)
    - Returns 404 on any error so the frontend <img onerror> shows the placeholder
    """
    import ipaddress
    import socket

    url = request.query_params.get("url", "")
    if not url:
        raise HTTPException(404, "No URL provided")

    # Force HTTPS to avoid mixed-content blocking
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]

    # Only allow http/https schemes
    if not url.startswith(("http://", "https://")):
        raise HTTPException(404, "Invalid URL scheme")

    # V31: SSRF protection — resolve host and reject private/internal IPs
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            raise HTTPException(404, "Invalid URL")
        # Resolve all IPs for the host
        try:
            addrinfos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            raise HTTPException(404, "Host unresolvable")
        for ai in addrinfos:
            ip = ipaddress.ip_address(ai[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                # Allow localhost ONLY in development
                if not (os.getenv("ENV") == "development" and ip.is_loopback):
                    logger.warning(f"[V31] imgproxy SSRF blocked: {host} -> {ip}")
                    raise HTTPException(403, "Blocked by SSRF protection")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"imgproxy URL validation error: {e}")
        raise HTTPException(404, "Invalid URL")

    # V21: Parse width + quality params with safe bounds
    try:
        max_width = min(int(request.query_params.get("w", "1600")), 2400)
        if max_width < 50:
            max_width = 1600
    except (ValueError, TypeError):
        max_width = 1600
    try:
        quality = int(request.query_params.get("q", "85"))
        quality = max(40, min(95, quality))
    except (ValueError, TypeError):
        quality = 85

    # V21: Check browser Accept header — if WebP not supported, fall back to JPEG
    accept = request.headers.get("accept", "").lower()
    supports_webp = "image/webp" in accept

    try:
        import httpx
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*,*/*;q=0.8",
            "Referer": url.split("/")[0] + "//" + (url.split("/")[2] if len(url.split("/")) > 2 else ""),
        }
        # V31: 8MB hard cap to prevent memory DoS via huge upstream images
        MAX_IMG_BYTES = 8 * 1024 * 1024
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            # Use stream() so we can abort as soon as we exceed the size cap
            async with client.stream("GET", url, headers=headers) as r:
                if r.status_code != 200:
                    raise HTTPException(404, "Image fetch failed")
                content_length = r.headers.get("content-length")
                if content_length and int(content_length) > MAX_IMG_BYTES:
                    raise HTTPException(413, "Image too large")
                chunks = []
                total = 0
                async for chunk in r.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_IMG_BYTES:
                        raise HTTPException(413, "Image too large")
                    chunks.append(chunk)
                content = b"".join(chunks)
            if not content:
                raise HTTPException(404, "Image fetch failed")

            # V21: Optimize with Pillow (resize + sharpen + WebP)
            if supports_webp:
                optimized, content_type = _optimize_image(content, max_width, quality)
            else:
                # Browser doesn't support WebP — skip optimization, serve raw bytes
                optimized = content
                content_type = r.headers.get("Content-Type", "image/jpeg")
                if not content_type.startswith("image/"):
                    content_type = "image/jpeg"

            # V21: Aggressive caching — optimized images don't change
            response = Response(
                content=optimized,
                media_type=content_type,
                headers={
                    "Cache-Control": "public, max-age=2592000, immutable",  # 30 days, immutable
                    "X-Content-Type-Options": "nosniff",
                    "Vary": "Accept",  # So CDNs cache WebP/JPEG variants separately
                },
            )
            return response
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"imgproxy failed for {url[:80]}: {e}")
        raise HTTPException(404, "Image unavailable")


# ════════════════════════════════════════════════════════════
# V21: "SUNO" FEATURE — TEXT-TO-SPEECH (Podcast for every article)
# Uses edge-tts (Microsoft Edge TTS — free, natural voices, no API key)
# Falls back to gTTS if edge-tts is unavailable.
# Audio is generated on-demand (first play) and cached on disk.
# ════════════════════════════════════════════════════════════

AUDIO_DIR = os.path.join("static", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)


def _strip_markdown_for_tts(text: str) -> str:
    """Convert markdown to plain text suitable for TTS reading.
    Removes markdown headings, links, image tags, and code blocks while
    preserving readability for the audio listener."""
    import re as _re
    if not text:
        return ""
    # Remove headings markers (keep the text)
    text = _re.sub(r"^#{1,6}\s+", "", text, flags=_re.MULTILINE)
    # Remove bold/italic markers
    text = _re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    # Remove links [text](url) -> text
    text = _re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Remove image tags
    text = _re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    # Remove code blocks
    text = _re.sub(r"```[\s\S]*?```", "", text)
    text = _re.sub(r"`([^`]+)`", r"\1", text)
    # Remove blockquotes markers
    text = _re.sub(r"^\s*>\s+", "", text, flags=_re.MULTILINE)
    # Remove list markers
    text = _re.sub(r"^\s*[-*]\s+", "", text, flags=_re.MULTILINE)
    # Collapse whitespace
    text = _re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def _generate_tts_audio(text: str, output_path: str) -> bool:
    """V21: Generate TTS audio using edge-tts (Microsoft Edge TTS).
    Returns True on success, False on failure.
    Falls back to gTTS if edge-tts is unavailable."""
    if not text or len(text.strip()) < 50:
        return False

    # Limit text length to prevent runaway processing
    if len(text) > 50000:
        text = text[:50000] + ". The article continues. Please read the full text on the website."

    # Strategy 1: edge-tts (free, Microsoft natural voices)
    try:
        import edge_tts
        # Use a natural English voice — "en-US-AriaNeural" is female, very natural
        voice = os.getenv("TTS_VOICE", "en-US-AriaNeural")
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(output_path)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            return True
    except ImportError:
        logger.info("[V21 TTS] edge-tts not installed — trying gTTS fallback")
    except Exception as e:
        logger.warning(f"[V21 TTS] edge-tts failed: {e} — trying gTTS fallback")

    # Strategy 2: gTTS (Google Translate TTS — free, simpler voice)
    try:
        from gtts import gTTS
        tts = gTTS(text=text, lang='en', slow=False)
        tts.save(output_path)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            return True
    except ImportError:
        logger.warning("[V21 TTS] gTTS not installed either — TTS unavailable")
    except Exception as e:
        logger.warning(f"[V21 TTS] gTTS failed: {e}")

    return False


@app.post("/api/articles/{article_id}/generate-audio")
async def generate_article_audio(article_id: int, request: Request, db=Depends(get_db)):
    """V21: Generate TTS audio for an article.
    Can be called by admin (force regenerate) or automatically on first access.
    Returns the audio URL if successful."""
    # Allow public access (no admin required) — this is called when user clicks Play
    # But rate-limit to prevent abuse
    client_ip = _get_client_ip(request)
    if not _check_rate_limit(f"tts:{client_ip}", 20, 3600):  # 20 generations per hour
        raise HTTPException(429, "Too many audio generation requests. Please try again later.")

    result = await db.execute(select(Article).where(Article.id == article_id))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "Article not found")

    # If audio already ready, return it
    if a.audio_status == "ready" and a.audio_url:
        audio_path = a.audio_url.lstrip("/")
        if os.path.exists(audio_path):
            return {"status": "ready", "audio_url": a.audio_url}

    # Mark as processing
    a.audio_status = "processing"
    await db.commit()

    # Generate audio
    plain_text = _strip_markdown_for_tts(a.ai_content or "")
    # Prepend title for context
    full_text = f"{a.title}. {plain_text}"
    output_filename = f"article_{article_id}.mp3"
    output_path = os.path.join(AUDIO_DIR, output_filename)

    success = await _generate_tts_audio(full_text, output_path)

    if success:
        a.audio_url = f"/static/audio/{output_filename}"
        a.audio_status = "ready"
        await db.commit()
        logger.info(f"[V21 TTS] Audio generated for article {article_id}")
        return {"status": "ready", "audio_url": a.audio_url}
    else:
        a.audio_status = "failed"
        await db.commit()
        raise HTTPException(500, "Audio generation failed. Please try again later.")


# ════════════════════════════════════════════════════════════
# V21: INTERACTIVE POLLS — One-click voting on articles
# ════════════════════════════════════════════════════════════

from database import Poll, PollVote, Quiz  # noqa: E402


@app.get("/api/articles/{article_id}/polls")
async def get_article_polls(article_id: int, db=Depends(get_db)):
    """Get all active polls for an article, with vote counts."""
    result = await db.execute(
        select(Poll).where(Poll.article_id == article_id, Poll.is_active == 1)
    )
    polls = result.scalars().all()
    out = []
    for p in polls:
        options = [o.strip() for o in p.options.split(",") if o.strip()]
        # Get vote counts per option
        vote_counts = []
        total_votes = 0
        for i in range(len(options)):
            count = (await db.execute(
                select(func.count(PollVote.id)).where(
                    PollVote.poll_id == p.id,
                    PollVote.option_index == i,
                )
            )).scalar_one()
            vote_counts.append(count)
            total_votes += count
        out.append({
            "id": p.id,
            "question": p.question,
            "options": options,
            "vote_counts": vote_counts,
            "total_votes": total_votes,
        })
    return out


@app.post("/api/polls/{poll_id}/vote")
async def vote_poll(poll_id: int, data: dict, request: Request, db=Depends(get_db)):
    """Vote on a poll. Body: {"option_index": 0, "fingerprint": "fp_..."}.
    One vote per fingerprint per poll."""
    result = await db.execute(select(Poll).where(Poll.id == poll_id, Poll.is_active == 1))
    poll = result.scalar_one_or_none()
    if not poll:
        raise HTTPException(404, "Poll not found")

    # V32.1 BUGFIX: The old code did `int(data.get("option_index", -1))`
    # which raises ValueError (500 error) on non-numeric input like
    # {"option_index": "abc"} or {"option_index": true}. Now we validate
    # the type first and return a clean 400 instead.
    raw_idx = data.get("option_index", -1)
    if isinstance(raw_idx, bool) or not isinstance(raw_idx, (int, float, str)):
        raise HTTPException(400, "Invalid option index: must be a number")
    try:
        option_index = int(raw_idx)
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid option index: not a valid integer")
    options = [o.strip() for o in poll.options.split(",") if o.strip()]
    if option_index < 0 or option_index >= len(options):
        raise HTTPException(400, "Invalid option index")

    fp = _user_fingerprint(request, data.get("fingerprint"))

    # Check if already voted
    existing = (await db.execute(
        select(PollVote).where(PollVote.poll_id == poll_id, PollVote.user_fingerprint == fp)
    )).scalar_one_or_none()
    if existing:
        # Update existing vote (let user change their mind)
        existing.option_index = option_index
        await db.commit()
    else:
        db.add(PollVote(poll_id=poll_id, option_index=option_index, user_fingerprint=fp))
        await db.commit()

    # Return updated counts
    vote_counts = []
    total_votes = 0
    for i in range(len(options)):
        count = (await db.execute(
            select(func.count(PollVote.id)).where(
                PollVote.poll_id == poll_id,
                PollVote.option_index == i,
            )
        )).scalar_one()
        vote_counts.append(count)
        total_votes += count
    return {"status": "voted", "vote_counts": vote_counts, "total_votes": total_votes}


# Admin: Create a poll
class PollCreateIn(BaseModel):
    article_id: int
    question: str = Field(..., min_length=5, max_length=500)
    options: str = Field(..., min_length=5)  # comma-separated


@app.post("/api/admin/polls")
async def create_poll(data: PollCreateIn, request: Request, db=Depends(get_db)):
    _require_admin(request)
    options = [o.strip() for o in data.options.split(",") if o.strip()]
    if len(options) < 2:
        raise HTTPException(400, "At least 2 options required")
    poll = Poll(article_id=data.article_id, question=data.question, options=",".join(options))
    db.add(poll)
    await db.commit()
    await db.refresh(poll)
    return {"status": "created", "id": poll.id}


# ════════════════════════════════════════════════════════════
# V21: QUIZZES — 3-question end-of-article engagement
# ════════════════════════════════════════════════════════════

@app.get("/api/articles/{article_id}/quiz")
async def get_article_quiz(article_id: int, db=Depends(get_db)):
    """Get the active quiz for an article (without revealing correct answers)."""
    result = await db.execute(
        select(Quiz).where(Quiz.article_id == article_id, Quiz.is_active == 1)
    )
    quiz = result.scalar_one_or_none()
    if not quiz:
        return {"available": False}
    import json as _json
    try:
        questions = _json.loads(quiz.questions)
        # Strip correct_index for the client (so user can't cheat by inspecting)
        safe_questions = []
        for q in questions:
            safe_questions.append({
                "question": q.get("question", ""),
                "options": q.get("options", []),
            })
        return {
            "available": True,
            "id": quiz.id,
            "title": quiz.title,
            "questions": safe_questions,
        }
    except Exception as e:
        logger.warning(f"Quiz parse error: {e}")
        return {"available": False}


@app.post("/api/quiz/{quiz_id}/submit")
async def submit_quiz(quiz_id: int, data: dict, db=Depends(get_db)):
    """Submit quiz answers. Body: {"answers": [0, 2, 1]} (option indices).
    Returns score + correct answers."""
    result = await db.execute(select(Quiz).where(Quiz.id == quiz_id))
    quiz = result.scalar_one_or_none()
    if not quiz:
        raise HTTPException(404, "Quiz not found")
    import json as _json
    try:
        questions = _json.loads(quiz.questions)
    except Exception:
        raise HTTPException(500, "Quiz data corrupted")

    answers = data.get("answers", [])
    # V32.1 BUGFIX: Validate answers is a list of integers. The old code
    # called int(answers[i]) which raises ValueError on non-numeric input.
    if not isinstance(answers, list):
        raise HTTPException(400, "answers must be a list of integers")
    if len(answers) != len(questions):
        raise HTTPException(400, f"Expected {len(questions)} answers, got {len(answers)}")

    score = 0
    results = []
    for i, q in enumerate(questions):
        correct_idx = q.get("correct_index", -1)
        raw_ans = answers[i]
        # V32.1: type-safe int conversion
        if isinstance(raw_ans, bool) or not isinstance(raw_ans, (int, float, str)):
            raise HTTPException(400, f"Answer {i} must be a number")
        try:
            user_idx = int(raw_ans)
        except (ValueError, TypeError):
            raise HTTPException(400, f"Answer {i} is not a valid integer")
        is_correct = user_idx == correct_idx
        if is_correct:
            score += 1
        results.append({
            "question": q.get("question", ""),
            "options": q.get("options", []),
            "correct_index": correct_idx,
            "user_index": user_idx,
            "is_correct": is_correct,
        })

    return {"score": score, "total": len(questions), "results": results}


# Admin: Create a quiz
class QuizCreateIn(BaseModel):
    article_id: int
    title: str = Field(..., min_length=5, max_length=300)
    questions: str  # JSON string


@app.post("/api/admin/quizzes")
async def create_quiz(data: QuizCreateIn, request: Request, db=Depends(get_db)):
    _require_admin(request)
    import json as _json
    try:
        questions = _json.loads(data.questions)
        if not isinstance(questions, list) or len(questions) < 1:
            raise ValueError("questions must be a non-empty list")
        for q in questions:
            if "question" not in q or "options" not in q or "correct_index" not in q:
                raise ValueError("each question needs question, options, correct_index")
            if not isinstance(q["options"], list) or len(q["options"]) < 2:
                raise ValueError("each question needs at least 2 options")
    except (ValueError, _json.JSONDecodeError) as e:
        raise HTTPException(400, f"Invalid questions JSON: {e}")
    quiz = Quiz(article_id=data.article_id, title=data.title, questions=data.questions)
    db.add(quiz)
    await db.commit()
    await db.refresh(quiz)
    return {"status": "created", "id": quiz.id}


# ════════════════════════════════════════════════════════════
# V21: LIVE PROGRESS TRACKER — Auto-refreshing live updates
# ════════════════════════════════════════════════════════════

@app.get("/api/articles/{article_id}/live-updates")
async def get_live_updates(article_id: int, db=Depends(get_db)):
    """Get the live updates feed for an article (if it's a live event)."""
    result = await db.execute(select(Article).where(Article.id == article_id))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "Article not found")
    if not a.is_live:
        return {"is_live": False, "updates": []}
    import json as _json
    try:
        updates = _json.loads(a.live_updates) if a.live_updates else []
    except Exception:
        updates = []
    return {
        "is_live": True,
        "updates": updates,
        "last_updated": (a.updated_at or a.date).isoformat() if a.updated_at else None,
    }


# Admin: Add a live update to an article
class LiveUpdateIn(BaseModel):
    text: str = Field(..., min_length=5, max_length=1000)
    status: str = Field("update", pattern="^(update|breaking|resolved|alert)$")


@app.post("/api/admin/articles/{article_id}/live-update")
async def add_live_update(article_id: int, data: LiveUpdateIn, request: Request, db=Depends(get_db)):
    """Add a new live update to an article. Marks the article as live."""
    _require_admin(request)
    result = await db.execute(select(Article).where(Article.id == article_id))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "Article not found")
    import json as _json
    try:
        updates = _json.loads(a.live_updates) if a.live_updates else []
    except Exception:
        updates = []
    updates.insert(0, {
        "time": datetime.utcnow().isoformat(),
        "text": data.text,
        "status": data.status,
    })
    # Keep only latest 50 updates
    updates = updates[:50]
    a.live_updates = _json.dumps(updates)
    a.is_live = 1 if data.status != "resolved" else 0
    a.updated_at = datetime.utcnow()
    await db.commit()
    _cache_invalidate_pattern(f"article:{article_id}:*")
    return {"status": "added", "total_updates": len(updates)}


# Admin: Auto-generate Timeline + Myths/Facts from article content using AI
@app.post("/api/admin/articles/{article_id}/generate-extras")
async def generate_article_extras(article_id: int, request: Request, db=Depends(get_db)):
    """V21: Auto-generate timeline_data + myths_facts from article content using AI.
    Uses the same Groq/Gemini keys as the article writer."""
    _require_admin(request)
    result = await db.execute(select(Article).where(Article.id == article_id))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "Article not found")

    import json as _json
    # Get region keys
    region = a.region or "world"
    groq_key = os.getenv(f"GROQ_KEY_{region.upper()}", "")
    gemini_key = os.getenv(f"GEMINI_KEY_{region.upper()}", "")

    timeline_data = []
    myths_facts = []

    # Try to generate timeline + myths/facts via AI
    # Strategy: Use Groq first, fall back to Gemini
    prompt = f"""Analyze this news article and extract:
1. A timeline of key events (chronological). Output as JSON array of objects: [{{"year": "2024", "title": "Event title", "description": "What happened"}}]
2. Common myths vs facts about this topic. Output as JSON array: [{{"myth": "Common misconception", "fact": "The actual truth"}}]

Article title: {a.title}
Article content (first 3000 chars):
{(a.ai_content or '')[:3000]}

Output ONLY a JSON object with this exact structure:
{{
  "timeline": [{{"year": "...", "title": "...", "description": "..."}}],
  "myths_facts": [{{"myth": "...", "fact": "..."}}]
}}

If no clear timeline or myths are found, return empty arrays. Maximum 8 timeline items and 5 myths/facts."""

    ai_result = None
    # Try Groq
    if groq_key:
        try:
            from ai_writer import _groq_call, GROQ_MODELS
            for model in GROQ_MODELS[:1]:  # just try the first model
                ai_result = _groq_call(
                    groq_key, model,
                    "You are a JSON-only output engine. Analyze news articles and extract structured data.",
                    prompt, 2000, 0.4, 0.85, json_mode=True,
                )
                if ai_result:
                    break
        except Exception as e:
            logger.warning(f"Timeline AI gen (Groq) failed: {e}")

    # Try Gemini if Groq failed
    if not ai_result and gemini_key:
        try:
            from ai_writer import _gemini_call
            ai_result = _gemini_call(gemini_key, "You are a JSON-only output engine.", prompt, 2000, 0.4, 0.85)
            if ai_result:
                # Strip markdown code fences
                import re as _re
                ai_result = _re.sub(r"^```(?:json)?\s*\n?", "", ai_result.strip())
                ai_result = _re.sub(r"\n?```\s*$", "", ai_result)
        except Exception as e:
            logger.warning(f"Timeline AI gen (Gemini) failed: {e}")

    if ai_result:
        try:
            parsed = _json.loads(ai_result)
            timeline_data = parsed.get("timeline", [])[:8]
            myths_facts = parsed.get("myths_facts", [])[:5]
        except _json.JSONDecodeError as e:
            logger.warning(f"Timeline JSON parse failed: {e}")

    # Save to article
    a.timeline_data = _json.dumps(timeline_data) if timeline_data else None
    a.myths_facts = _json.dumps(myths_facts) if myths_facts else None
    a.updated_at = datetime.utcnow()
    await db.commit()
    logger.info(f"[V21] Generated extras for article {article_id}: timeline={len(timeline_data)}, myths={len(myths_facts)}")
    return {
        "status": "generated",
        "timeline_items": len(timeline_data),
        "myths_facts_items": len(myths_facts),
    }


# ════════════════════════════════════════════════════════════
#  V23: DYNAMIC CATEGORY ROUTE — single template for ALL regions
#  Replaces 6 static *-news.html files with one /category/{name} route
#  The template (static/category.html) reads region from the URL.
# ════════════════════════════════════════════════════════════
@app.get("/category/{country_name}")
async def category_page(country_name: str, db=Depends(get_db)):
    """V32: Serve the category template with server-rendered SEO meta AND
    server-rendered article cards. Articles are now injected directly into
    the HTML so the page loads instantly (no skeleton screen, no waiting
    for the JS fetch() to complete). The JS still handles infinite scroll
    for additional pages, but the first 12 articles are visible immediately.

    This is the "direct page open" experience the user requested — like
    opening a static HTML page, not a SPA that needs to fetch data first.
    """
    name = country_name.strip().lower()
    if name not in REGIONS:
        raise HTTPException(404, f"Unknown category: {country_name}")

    # Read the template
    tpl_path = "static/category.html"
    try:
        with open(tpl_path, "r", encoding="utf-8") as f:
            tpl = f.read()
    except Exception:
        return FileResponse(tpl_path)

    # Build region metadata
    import re as _re_cat
    region_labels = {
        "world": "World News", "usa": "USA News", "uk": "UK News",
        "pakistan": "Pakistan News", "india": "India News", "germany": "Germany News"
    }
    region_label = region_labels.get(name, name.title())
    region_desc = f"Latest {region_label} from SFAAM NEWS. Breaking headlines, in-depth analysis and trusted journalism updated continuously."
    canonical = f"{SITE_URL}/category/{name}"
    image_url = f"{SITE_URL}/static/logo.png"

    esc = _html_lib.escape
    title_seo = esc(f"{region_label} — SFAAM NEWS")
    meta_desc = esc(region_desc)

    # JSON-LD schema
    ld_schema = _json.dumps({
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": region_label,
        "url": canonical,
        "description": region_desc,
        "isPartOf": {"@type": "WebSite", "name": "SFAAM NEWS", "url": SITE_URL},
        "publisher": {
            "@type": "NewsMediaOrganization",
            "name": "SFAAM NEWS",
            "url": SITE_URL,
            "logo": {"@type": "ImageObject", "url": f"{SITE_URL}/static/logo.png"}
        }
    }, ensure_ascii=False)
    # V31: Escape </script> to prevent XSS via JSON-LD injection
    ld_schema = ld_schema.replace("<", "\\u003c")

    # V32: Fetch first 12 articles from DB for server-side rendering.
    # This eliminates the skeleton loading screen — users see real content
    # the moment the HTML arrives. JS handles page 2+ via infinite scroll.
    server_articles_html = ""
    try:
        result = await db.execute(
            select(Article)
            .where(or_(Article.status == "published", Article.status == None))  # noqa: E711
            .where(Article.region == name)
            .order_by(Article.date.desc())
            .limit(12)
        )
        articles = result.scalars().all()
        if articles:
            cards = []
            for a in articles:
                # Build a minimal card HTML — matches the JS buildNewsCard layout
                title_esc = esc(a.title or "")
                summary_esc = esc((a.summary or a.meta_desc or "")[:160])
                region_upper = esc((a.region or "NEWS").upper())
                img_html = ""
                if a.image_url:
                    img_html = (
                        f'<div class="news-image-wrap">'
                        f'<img src="/api/imgproxy?url={esc(a.image_url)}&w=600" '
                        f'alt="{title_esc}" loading="lazy" width="400" height="200" '
                        f'onerror="this.parentElement.innerHTML=\'<div class=&quot;news-image-ph&quot;>&#128240;</div>\'"/>'
                        f'<span class="news-badge">{region_upper}</span></div>'
                    )
                else:
                    img_html = (
                        f'<div class="news-image-wrap">'
                        f'<div class="news-image-ph">&#128240;</div>'
                        f'<span class="news-badge">{region_upper}</span></div>'
                    )
                date_str = a.date.strftime("%b %d, %Y") if a.date else ""
                slug = a.slug or a.id
                word_count = getattr(a, "word_count", 0) or 0
                reading_time = max(1, word_count // 200) if word_count else 0
                reading_html = f'<span class="reading-time">&#128344; {reading_time} min read</span>' if reading_time else ""
                cards.append(
                    f'<article class="news-card" data-id="{a.id}">'
                    f'<a href="/article/{slug}" class="news-card-link" style="text-decoration:none;color:inherit;">'
                    f'{img_html}'
                    f'<div class="news-card-body">'
                    f'<h3 class="news-card-title">{title_esc}</h3>'
                    f'<p class="news-card-summary">{summary_esc}</p>'
                    f'<div class="news-card-meta"><span class="news-card-date">{date_str}</span>{reading_html}</div>'
                    f'</div></a></article>'
                )
            server_articles_html = "".join(cards)
            logger.debug(f"[V32] Category {name}: server-rendered {len(articles)} article cards")
    except Exception as e:
        logger.warning(f"[V32] Could not server-render articles for {name}: {e}")

    # Inject SEO meta block before </head>
    seo_block = f"""
<!-- V29 SERVER-RENDERED SEO META — crawlable without JS -->
<title>{title_seo}</title>
<meta name="description" content="{meta_desc}"/>
<link rel="canonical" href="{canonical}"/>
<meta property="og:title" content="{title_seo}"/>
<meta property="og:description" content="{meta_desc}"/>
<meta property="og:type" content="website"/>
<meta property="og:url" content="{canonical}"/>
<meta property="og:image" content="{image_url}"/>
<meta property="og:site_name" content="SFAAM NEWS"/>
<meta property="og:locale" content="en_US"/>
<meta name="twitter:card" content="summary_large_image"/>
<meta name="twitter:site" content="@sfaamnews"/>
<meta name="twitter:title" content="{title_seo}"/>
<meta name="twitter:description" content="{meta_desc}"/>
<meta name="twitter:image" content="{image_url}"/>
<script type="application/ld+json">{ld_schema}</script>
<!-- /V29 SEO META -->"""

    out = tpl
    # Drop placeholder title
    out = _re_cat.sub(r"<title[^>]*>[^<]*</title>\s*", "", out, count=1)
    out = out.replace("</head>", seo_block + "\n</head>", 1)
    # V32: Inject server-rendered article cards into the newsGrid div.
    # The JS will detect these and skip the skeleton + first-page fetch.
    if server_articles_html:
        # Replace the empty newsGrid div with one containing server-rendered cards
        out = out.replace(
            '<div class="news-grid" id="newsGrid"></div>',
            f'<div class="news-grid" id="newsGrid" data-server-rendered="1">{server_articles_html}</div>'
        )
    return HTMLResponse(out)


# ── V23: 301 redirects for old /{region}-news.html URLs ──
# Preserves Google indexing — old URLs permanently redirect to the new
# dynamic /category/{region} route instead of returning 404.
from fastapi.responses import RedirectResponse

@app.get("/{region}-news.html")
async def redirect_old_region_html(region: str):
    name = region.strip().lower()
    if name in REGIONS:
        return RedirectResponse(
            url=f"/category/{name}",
            status_code=301,  # permanent redirect — SEO transfers link juice
            headers={"Cache-Control": "public, max-age=86400"}
        )
    raise HTTPException(404, "Not found")


# ── Sitemap (V23 — slug-based SEO URLs + dynamic category pages + paged) ──
@app.get("/sitemap.xml")
async def sitemap(db=Depends(get_db)):
    """V23: Dynamic XML sitemap — queries DB every call so new articles
    appear instantly. Pages at 5,000 URLs per file to stay under Google's
    50,000 URL limit per sitemap."""
    # Cache hit (Redis) — saves DB round-trip on high-traffic crawls
    cache_key = "sitemap:xml"
    cached = _cache_get(cache_key)
    if cached:
        return Response(content=cached, media_type="application/xml")

    result = await db.execute(
        select(Article)
        .where(or_(Article.status == "published", Article.status == None))  # noqa: E711
        .order_by(Article.date.desc()).limit(5000)
    )
    articles = result.scalars().all()

    # Article URLs — use /article/{slug} (the new dynamic route)
    art_urls = "\n".join([
        f"  <url>\n    <loc>{SITE_URL}/article/{a.slug}</loc>\n"
        f"    <lastmod>{(a.updated_at or a.date).strftime('%Y-%m-%d')}</lastmod>\n"
        f"    <changefreq>weekly</changefreq>\n    <priority>0.8</priority>\n  </url>"
        if a.slug else
        f"  <url>\n    <loc>{SITE_URL}/article/{a.id}</loc>\n"
        f"    <lastmod>{(a.updated_at or a.date).strftime('%Y-%m-%d')}</lastmod>\n"
        f"    <changefreq>weekly</changefreq>\n    <priority>0.8</priority>\n  </url>"
        for a in articles
    ])
    # Dynamic category pages — /category/{name}
    page_urls = "\n".join([
        f"  <url>\n    <loc>{SITE_URL}/category/{p}</loc>\n"
        f"    <changefreq>hourly</changefreq>\n    <priority>1.0</priority>\n  </url>"
        for p in sorted(REGIONS)
    ])
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{SITE_URL}/</loc><changefreq>hourly</changefreq><priority>1.0</priority></url>
  <url><loc>{SITE_URL}/about.html</loc><changefreq>weekly</changefreq><priority>0.6</priority></url>
  <url><loc>{SITE_URL}/founder.html</loc><changefreq>monthly</changefreq><priority>0.5</priority></url>
  <url><loc>{SITE_URL}/contact.html</loc><changefreq>monthly</changefreq><priority>0.5</priority></url>
  <url><loc>{SITE_URL}/search.html</loc><changefreq>weekly</changefreq><priority>0.5</priority></url>
  <url><loc>{SITE_URL}/trends.html</loc><changefreq>hourly</changefreq><priority>0.7</priority></url>
  <url><loc>{SITE_URL}/privacy.html</loc><changefreq>yearly</changefreq><priority>0.3</priority></url>
  <url><loc>{SITE_URL}/terms.html</loc><changefreq>yearly</changefreq><priority>0.3</priority></url>
  <url><loc>{SITE_URL}/cookies.html</loc><changefreq>yearly</changefreq><priority>0.3</priority></url>
  <url><loc>{SITE_URL}/corrections.html</loc><changefreq>yearly</changefreq><priority>0.3</priority></url>
{page_urls}
{art_urls}
</urlset>"""
    _cache_set(cache_key, xml, CACHE_TTL_SHORT)  # 1-minute cache
    return Response(content=xml, media_type="application/xml")


# ── V23: RSS Feed (auto-discoverable, updated every fetch) ──
@app.get("/rss.xml")
async def rss_feed(db=Depends(get_db)):
    """Dynamic RSS 2.0 feed of the latest 50 articles. Lets readers
    subscribe via feed readers and gives Google another discovery path."""
    cache_key = "rss:xml"
    cached = _cache_get(cache_key)
    if cached:
        return Response(content=cached, media_type="application/rss+xml")

    result = await db.execute(
        select(Article)
        .where(or_(Article.status == "published", Article.status == None))  # noqa: E711
        .order_by(Article.date.desc()).limit(50)
    )
    articles = result.scalars().all()
    import xml.sax.saxutils as _su
    items = "\n".join([
        f"""    <item>
      <title>{_su.escape(a.title)}</title>
      <link>{SITE_URL}/article/{a.slug or a.id}</link>
      <guid isPermaLink="true">{SITE_URL}/article/{a.slug or a.id}</guid>
      <description>{_su.escape((a.meta_desc or a.summary or a.title)[:300])}</description>
      <category>{_su.escape(a.region)}</category>
      <pubDate>{a.date.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate>
    </item>"""
        for a in articles
    ])
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>SFAAM NEWS — Breaking News, World News &amp; Analysis</title>
    <link>{SITE_URL}</link>
    <atom:link href="{SITE_URL}/rss.xml" rel="self" type="application/rss+xml" />
    <description>Trusted journalism from around the world. Breaking news, in-depth analysis, exclusive reports.</description>
    <language>en-us</language>
    <lastBuildDate>{datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')}</lastBuildDate>
    <generator>SFAAM NEWS V26</generator>
{items}
  </channel>
</rss>"""
    _cache_set(cache_key, xml, CACHE_TTL_SHORT)
    return Response(content=xml, media_type="application/rss+xml")


# ── Robots ──
@app.get("/robots.txt")
async def robots():
    return PlainTextResponse(
        content=(f"User-agent: *\n"
                 f"Allow: /\n"
                 f"Disallow: /admin.html\n"
                 f"Disallow: /api/admin/\n"
                 f"Disallow: /api/articles/*/like\n"
                 f"Disallow: /api/articles/*/comments\n\n"
                 f"Sitemap: {SITE_URL}/sitemap.xml\n\n"
                 f"User-agent: GPTBot\n"
                 f"Disallow: /\n\n"
                 f"User-agent: ChatGPT-User\n"
                 f"Disallow: /")
    )


# ════════════════════════════════════════════════════════════
#  V23: SERVER-RENDERED ARTICLE PAGE (Fix #3)
#  We read the article from DB and inject SEO meta tags + JSON-LD
#  schema directly into the HTML BEFORE serving it. This way:
#  - Googlebot sees perfect meta tags even with JS disabled
#  - OpenGraph / Twitter Card previews work in any chat app
#  - Article Schema Markup qualifies the page for Rich Results
# ════════════════════════════════════════════════════════════
import html as _html_lib

_ARTICLE_HTML_CACHE: dict[str, str] = {}  # path → rendered html (in-memory, small)

@app.get("/article/{slug:path}")
async def article_slug_redirect(slug: str, db=Depends(get_db)):
    """V23: Server-render article page with injected SEO meta tags,
    OpenGraph, Twitter Card, and JSON-LD NewsArticle schema markup.
    The article.html template still hydrates interactive features via JS,
    but ALL SEO-critical metadata is present in the raw HTML response.
    V24: Only published articles are publicly accessible. Drafts and
    pending_review articles return 404 to the public (admin can preview
    via /api/admin/articles/{id})."""
    clean_slug = slug.replace('.html', '').strip().rstrip('/')
    # Article lookup — supports both slug and numeric id
    if clean_slug.isdigit():
        result = await db.execute(select(Article).where(Article.id == int(clean_slug)))
    else:
        result = await db.execute(select(Article).where(Article.slug == clean_slug))
    article = result.scalars().first()  # V31: was scalar_one_or_none() — crashes on duplicate slugs
    if not article:
        raise HTTPException(404, "Article not found")
    # V24: Hide non-published articles from the public
    # V29: NULL status means pre-V24 article — treat as published
    art_status = getattr(article, "status", "published") or "published"
    if art_status != "published":
        raise HTTPException(404, "Article not found")

    # Read the template once and cache it
    tpl_path = "static/article.html"
    try:
        tpl = _ARTICLE_HTML_CACHE.get("_tpl")
        if not tpl:
            with open(tpl_path, "r", encoding="utf-8") as f:
                tpl = f.read()
            _ARTICLE_HTML_CACHE["_tpl"] = tpl
    except Exception:
        # If template can't be read, fall back to bare FileResponse
        return FileResponse(tpl_path)

    # ── Build SEO meta block (server-rendered, crawlable) ──
    esc = _html_lib.escape
    title_seo    = esc(f"{article.title} — SFAAM NEWS")
    meta_desc    = esc((article.meta_desc or article.summary or article.title)[:155])
    keywords_seo = esc(article.keywords or "")
    canonical    = f"{SITE_URL}/article/{article.slug or article.id}"
    page_url     = canonical
    image_url    = article.image_url or f"{SITE_URL}/static/logo.png"
    date_pub     = article.date.strftime("%Y-%m-%dT%H:%M:%S+00:00") if article.date else ""
    date_mod     = (article.updated_at or article.date).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    # JSON-LD NewsArticle schema (Rich Snippets qualifier)
    import json as _json_for_ld
    ld_schema = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": article.title,
        "image": [image_url],
        "datePublished": date_pub,
        "dateModified": date_mod,
        "author": {
            "@type": "Organization",
            "name": "SFAAM NEWS",
            "url": SITE_URL
        },
        "publisher": {
            "@type": "NewsMediaOrganization",
            "name": "SFAAM NEWS",
            "logo": {
                "@type": "ImageObject",
                "url": f"{SITE_URL}/static/logo.png",
                "width": 512,
                "height": 512
            }
        },
        "description": meta_desc,
        "keywords": keywords_seo,
        "articleSection": article.region,
        "mainEntityOfPage": {
            "@type": "WebPage",
            "@id": page_url
        },
        "isPartOf": {
            "@type": "CollectionPage",
            "url": f"{SITE_URL}/category/{article.region}"
        }
    }
    ld_json = _json_for_ld.dumps(ld_schema, ensure_ascii=False)
    # V31: Escape </script> to prevent XSS via JSON-LD injection
    ld_json = ld_json.replace("<", "\\u003c")

    # BreadcrumbList schema (extra Rich Result opportunity)
    ld_breadcrumb = _json_for_ld.dumps({
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home",
             "item": f"{SITE_URL}/"},
            {"@type": "ListItem", "position": 2,
             "name": f"{article.region.title()} News",
             "item": f"{SITE_URL}/category/{article.region}"},
            {"@type": "ListItem", "position": 3, "name": article.title[:60],
             "item": page_url},
        ]
    }, ensure_ascii=False)
    # V31: Escape </script> to prevent XSS via JSON-LD injection
    ld_breadcrumb = ld_breadcrumb.replace("<", "\\u003c")

    # Inject everything inside <head> (before </head>)
    seo_block = f"""
<!-- V23 SERVER-RENDERED SEO META — crawlable without JS -->
<title>{title_seo}</title>
<meta name="description" content="{meta_desc}"/>
<meta name="keywords" content="{keywords_seo}"/>
<link rel="canonical" href="{canonical}"/>
<meta property="og:type" content="article"/>
<meta property="og:title" content="{title_seo}"/>
<meta property="og:description" content="{meta_desc}"/>
<meta property="og:url" content="{page_url}"/>
<meta property="og:image" content="{image_url}"/>
<meta property="og:site_name" content="SFAAM NEWS"/>
<meta property="og:locale" content="en_US"/>
<meta property="article:published_time" content="{date_pub}"/>
<meta property="article:modified_time" content="{date_mod}"/>
<meta property="article:section" content="{esc(article.region)}"/>
<meta name="twitter:card" content="summary_large_image"/>
<meta name="twitter:site" content="@sfaamnews"/>
<meta name="twitter:creator" content="@sfaamnews"/>
<meta name="twitter:title" content="{title_seo}"/>
<meta name="twitter:description" content="{meta_desc}"/>
<meta name="twitter:image" content="{image_url}"/>
<script type="application/ld+json" id="ld-newsarticle">{ld_json}</script>
<script type="application/ld+json" id="ld-breadcrumb">{ld_breadcrumb}</script>
<!-- /V23 SEO META -->"""

    # Replace the existing minimal <title> tag and inject SEO block before </head>
    import re as _re_inj
    out = tpl
    # Drop the placeholder title (will be replaced by our SEO block)
    out = _re_inj.sub(r"<title>[^<]*</title>\s*", "", out, count=1)
    # Inject our SEO block right before </head>
    out = out.replace("</head>", seo_block + "\n</head>", 1)
    return HTMLResponse(out)


# ── V23: Sitemap cache invalidation helper (called by scheduler) ──
def invalidate_sitemap_cache() -> None:
    """Clear sitemap + RSS cache so newly-published articles appear
    immediately in Google's next crawl. Called by scheduler after
    every successful pipeline run."""
    if _redis_client:
        try:
            _redis_client.delete("cache:sitemap:xml", "cache:rss:xml")
            logger.info("[V23] Sitemap + RSS cache invalidated")
        except Exception as e:
            logger.debug(f"Sitemap cache invalidation failed: {e}")


# ════════════════════════════════════════════════════════════
#  V24: DRAFT MODE + QUALITY CONTROL ENDPOINTS
#  Admin can list drafts, publish them, view QC scores, etc.
# ════════════════════════════════════════════════════════════

@app.get("/api/admin/drafts")
async def list_drafts(
    request: Request,
    status: str = "draft",
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db=Depends(get_db),
):
    """List articles pending admin review (drafts / pending_review / rejected).
    Default status=draft. Use status=all to list every non-published article."""
    _require_admin(request)
    q = select(Article)
    if status != "all":
        q = q.where(Article.status == status)
    else:
        q = q.where(Article.status != "published")
    q = q.order_by(Article.date.desc()).offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(q)
    articles = result.scalars().all()
    return [
        {
            "id": a.id, "title": a.title, "slug": a.slug,
            "region": a.region, "status": a.status,
            "source_type": getattr(a, "source_type", "rss"),
            "search_keyword": getattr(a, "search_keyword", None),
            "date": a.date.isoformat() if a.date else None,
            "quality_score": _safe_json_parse(getattr(a, "quality_score", None)),
            "word_count": len((a.ai_content or "").split()),
            "summary": (a.summary or "")[:200],
        }
        for a in articles
    ]


@app.post("/api/admin/articles/{article_id}/publish")
async def publish_article(article_id: int, request: Request, db=Depends(get_db)):
    """Publish a draft/pending_review article. Sets status='published'
    and clears it for public visibility."""
    _require_admin(request)
    result = await db.execute(select(Article).where(Article.id == article_id))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "Article not found")
    if a.status == "published":
        return {"status": "already_published", "id": article_id}
    old_status = a.status
    a.status = "published"
    a.updated_at = datetime.utcnow()
    await db.commit()
    # V23: invalidate sitemap cache
    invalidate_sitemap_cache()
    # V24: audit log
    try:
        from monitoring import log_audit_event
        log_audit_event(
            admin_id=_get_client_ip(request),
            action="article.publish",
            target_type="article",
            target_id=article_id,
            details={"old_status": old_status, "title": a.title[:80]},
            ip_address=_get_client_ip(request),
        )
    except Exception:
        pass
    logger.info(f"[V24] Article {article_id} published by admin")
    return {"status": "published", "id": article_id, "title": a.title}


# ── V31: Edit Article (Admin) ──
# Previously the admin could only delete + re-create. This patch endpoint lets
# the admin fix typos, update content, change region/keywords without losing
# the article's slug, views, comments, or publication date.
class ArticleEditIn(BaseModel):
    """Editable fields for an existing article. All optional — only supplied
    fields will be updated (PATCH semantics)."""
    title: Optional[str] = Field(None, min_length=3, max_length=300)
    summary: Optional[str] = Field(None, max_length=500)
    ai_content: Optional[str] = Field(None, min_length=10)
    image_url: Optional[str] = Field(None, max_length=2000)
    region: Optional[str] = Field(None, max_length=50)
    keywords: Optional[str] = Field(None, max_length=500)
    meta_desc: Optional[str] = Field(None, max_length=200)
    tldr_summary: Optional[str] = Field(None, max_length=1000)
    # V33 BUGFIX: Allow admin to change the publication status directly from
    # the Edit modal. Without this field, the admin can edit a draft's title
    # and body but cannot publish it in the same action — they have to close
    # the modal and click a separate Publish button on the articles list.
    # Allowed values: published | draft | pending_review | rejected
    status: Optional[str] = Field(None, pattern="^(published|draft|pending_review|rejected)$")


# ── V33 BUGFIX: Admin GET single-article endpoint ──
# The public /api/articles/{id} endpoint filters out non-published articles
# (returns 404 for drafts, pending_review, rejected). This made it impossible
# for the admin Edit modal to load draft articles — openEditModal() fetched
# /api/articles/{id} and got 404 for any non-published article.
# This admin-only endpoint returns the article regardless of status, so the
# Edit modal works for drafts too.
@app.get("/api/admin/articles/{article_id}")
async def admin_get_article(article_id: int, request: Request, db=Depends(get_db)):
    """Admin-only: fetch a single article by ID, regardless of status.
    Returns ALL fields (including ai_content, status, fact_check_status) so
    the Edit modal can populate every editable field."""
    _require_admin(request)
    result = await db.execute(select(Article).where(Article.id == article_id))
    a = result.scalars().first()
    if not a:
        raise HTTPException(404, "Article not found")
    return {
        "id": a.id,
        "title": a.title,
        "slug": a.slug,
        "original_url": a.original_url,
        "summary": a.summary,
        "ai_content": a.ai_content,
        "image_url": a.image_url,
        "region": a.region,
        "meta_desc": a.meta_desc,
        "keywords": a.keywords,
        "views": a.views or 0,
        "date": a.date.isoformat() if a.date else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
        "tldr_summary": a.tldr_summary,
        "fact_check_status": a.fact_check_status,
        "audio_url": a.audio_url,
        "audio_status": a.audio_status,
        "word_count": a.word_count or 0,
        "word_count_tier": a.word_count_tier,
        "status": a.status or "published",
        "source_type": getattr(a, "source_type", "rss"),
        "is_trends": bool(getattr(a, "is_trends", 0)),
    }


@app.patch("/api/admin/articles/{article_id}")
async def edit_article(article_id: int, data: ArticleEditIn, request: Request, db=Depends(get_db)):
    """V31: Edit an existing article. PATCH semantics — only provided fields
    are updated. Preserves id, slug, original_url, views, date, and comments.

    If title is changed and the new slug collides with an existing article,
    a numeric suffix is appended (same logic as manual publish)."""
    _require_admin(request)
    result = await db.execute(select(Article).where(Article.id == article_id))
    a = result.scalars().first()
    if not a:
        raise HTTPException(404, "Article not found")

    updates = data.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(400, "No fields to update")

    # V33 BUGFIX: Capture old status BEFORE applying updates, so we can detect
    # whether the admin is publishing/unpublishing the article and log it.
    old_status = a.status

    # If title is being changed, regenerate slug (with collision check)
    if "title" in updates and updates["title"] != a.title:
        new_slug = _make_slug(updates["title"])
        base_slug = new_slug
        n = 1
        while True:
            existing = (await db.execute(
                select(Article).where(Article.slug == new_slug, Article.id != article_id)
            )).scalars().first()
            if not existing:
                break
            n += 1
            new_slug = f"{base_slug}-{n}"
        a.slug = new_slug

    # Apply all updates
    for field, value in updates.items():
        if field != "title":  # title already set above (via slug logic)
            setattr(a, field, value)
    if "title" in updates:
        a.title = updates["title"]

    # Recompute content hash if content changed
    if "ai_content" in updates:
        a.article_hash = hashlib.sha256(
            (a.title + updates["ai_content"]).encode()
        ).hexdigest()

    a.updated_at = datetime.utcnow()
    # V31.1: If title changed, recompute title_norm
    if "title" in updates:
        try:
            from title_uniqueness import compute_title_norm
            a.title_norm = compute_title_norm(a.title)[:500]
        except Exception:
            pass
    await db.commit()
    await db.refresh(a)

    # V33 BUGFIX: If the admin changed the status via the Edit modal
    # (e.g. draft → published), log it as a publish action so the audit
    # trail is complete. We compare old_status to the new value.
    new_status = updates.get("status", old_status)
    status_changed = "status" in updates and updates["status"] != old_status
    if status_changed and new_status == "published":
        try:
            from monitoring import log_audit_event
            log_audit_event(
                admin_id=_get_client_ip(request),
                action="article.publish_via_edit",
                target_type="article",
                target_id=article_id,
                details={"old_status": old_status, "title": a.title[:80]},
                ip_address=_get_client_ip(request),
            )
        except Exception:
            pass
        logger.info(f"[V33] Article {article_id} published via Edit modal (was: {old_status})")

    # Audit log
    try:
        from monitoring import log_audit_event
        log_audit_event(
            admin_id=_get_client_ip(request),
            action="article.edit",
            target_type="article",
            target_id=article_id,
            details={"fields": list(updates.keys()), "title": a.title[:80]},
            ip_address=_get_client_ip(request),
        )
    except Exception:
        pass

    # Invalidate sitemap (slug or status might have changed)
    invalidate_sitemap_cache()

    logger.info(f"[V31] Article {article_id} edited by admin (fields: {list(updates.keys())})")
    return {
        "status": "updated",
        "id": article_id,
        "title": a.title,
        "slug": a.slug,
        "updated_fields": list(updates.keys()),
        "article_status": a.status,
    }


@app.post("/api/admin/articles/{article_id}/reject")
async def reject_article(article_id: int, request: Request, db=Depends(get_db)):
    """Mark an article as rejected (won't show in drafts anymore)."""
    _require_admin(request)
    result = await db.execute(select(Article).where(Article.id == article_id))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "Article not found")
    a.status = "rejected"
    a.updated_at = datetime.utcnow()
    await db.commit()
    try:
        from monitoring import log_audit_event
        log_audit_event(
            admin_id=_get_client_ip(request), action="article.reject",
            target_type="article", target_id=article_id,
            details={"title": a.title[:80]}, ip_address=_get_client_ip(request),
        )
    except Exception:
        pass
    return {"status": "rejected", "id": article_id}


@app.get("/api/admin/articles/{article_id}/quality")
async def get_article_quality(article_id: int, request: Request, db=Depends(get_db)):
    """Fetch the QualityScore breakdown for an article. If the article
    was saved before V24 (no score), re-run QC on demand."""
    _require_admin(request)
    result = await db.execute(select(Article).where(Article.id == article_id))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "Article not found")
    qs = _safe_json_parse(getattr(a, "quality_score", None))
    if qs:
        return qs
    # Re-run QC on demand
    try:
        from quality_control import evaluate_article, quality_score_to_dict
        qc = evaluate_article(
            title=a.title, body=a.ai_content or "",
            meta_desc=a.meta_desc or "", keywords=a.keywords or "",
        )
        return quality_score_to_dict(qc)
    except Exception as e:
        return {"error": f"QC re-run failed: {e}"}


def _safe_json_parse(s):
    """Parse a JSON string safely. Returns None on failure."""
    if not s:
        return None
    try:
        import json as _j
        return _j.loads(s)
    except Exception:
        return None


# ════════════════════════════════════════════════════════════
#  V24: GOOGLE-SEARCH-BASED ARTICLE GENERATION
#  Admin gives a topic → system searches → AI writes → saves as draft
# ════════════════════════════════════════════════════════════

class GoogleSearchWriteIn(BaseModel):
    """Input model for the Google-search-based article writer."""
    topic: str = Field(..., min_length=3, max_length=300,
                       description="Topic or keyword to search for")
    region: str = Field("world",
                        description="One of: world, usa, uk, pakistan, india, germany")

    @field_validator("region")
    @classmethod
    def validate_region(cls, v):
        if v not in REGIONS:
            raise ValueError(f"Region must be one of: {', '.join(sorted(REGIONS))}")
        return v


@app.post("/api/admin/generate-from-search")
async def generate_from_search(data: GoogleSearchWriteIn, request: Request):
    """V24: Generate an article from a Google/DuckDuckGo search.
    Fetches top results, AI-rewrites them, saves as DRAFT for admin review.

    Required: admin auth. Rate-limited to 1 call / 30s per admin to avoid abuse."""
    _require_admin(request)
    client_ip = _get_client_ip(request)
    if not _check_rate_limit(f"gen_search:{client_ip}", 2, 60):
        raise HTTPException(429, "Too many generation requests. Please wait a minute.")

    try:
        from google_search_writer import generate_article_from_topic
        result = await generate_article_from_topic(
            topic=data.topic,
            region=data.region,
            user_id=client_ip,
        )
        # V24: audit log
        try:
            from monitoring import log_audit_event
            log_audit_event(
                admin_id=client_ip,
                action="article.generate_from_search",
                target_type="article" if result.get("article_id") else None,
                target_id=result.get("article_id"),
                details={"topic": data.topic[:80], "region": data.region,
                         "status": result.get("status")},
                ip_address=client_ip,
            )
        except Exception:
            pass
        return result
    except Exception as e:
        logger.error(f"Google-search generation failed: {e}", exc_info=True)
        raise HTTPException(500, f"Generation failed: {type(e).__name__}: {str(e)[:200]}")


# ════════════════════════════════════════════════════════════
#  V24: AI RECOMMENDATION ENGINE — related articles
#  Uses TF-IDF cosine similarity (lightweight, no external deps)
# ════════════════════════════════════════════════════════════

@app.get("/api/articles/{article_id}/recommendations")
async def article_recommendations(
    article_id: int,
    limit: int = Query(4, ge=1, le=10),
    db=Depends(get_db),
):
    """Get recommended articles for a given article.
    Strategy:
      1. Same region, most recent (cheap baseline)
      2. Refined by TF-IDF cosine similarity of title+summary vs. other articles
    Returns up to `limit` articles (excluding the current one).
    """
    # Get the source article
    result = await db.execute(select(Article).where(Article.id == article_id))
    src = result.scalar_one_or_none()
    if not src:
        raise HTTPException(404, "Article not found")

    # Cache key — short TTL since recs don't change often
    cache_key = f"recs:{article_id}:{limit}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    # Get candidate articles (same region, published, not this one)
    # V29: Include NULL status (pre-V24 articles) as published
    _published_or_null = or_(Article.status == "published", Article.status == None)  # noqa: E711
    cand_result = await db.execute(
        select(Article)
        .where(Article.id != article_id)
        .where(Article.region == src.region)
        .where(_published_or_null)
        .order_by(Article.date.desc())
        .limit(50)
    )
    candidates = cand_result.scalars().all()

    # If not enough same-region candidates, fall back to any region
    if len(candidates) < limit:
        more_result = await db.execute(
            select(Article)
            .where(Article.id != article_id)
            .where(Article.region != src.region)
            .where(_published_or_null)
            .order_by(Article.date.desc())
            .limit(50 - len(candidates))
        )
        candidates.extend(more_result.scalars().all())

    if not candidates:
        return []

    # Score by similarity
    try:
        from quality_control import _tf, _tokenize, _cosine_similarity
        src_text = f"{src.title} {src.summary or ''} {src.keywords or ''}"
        src_tf = _tf(_tokenize(src_text))
        scored = []
        for c in candidates:
            cand_text = f"{c.title} {c.summary or ''} {c.keywords or ''}"
            cand_tf = _tf(_tokenize(cand_text))
            sim = _cosine_similarity(src_tf, cand_tf)
            scored.append((sim, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:limit]
    except Exception:
        # Fall back to most-recent ordering
        top = [(0.0, c) for c in candidates[:limit]]

    result_payload = [
        {
            "id": c.id, "title": c.title, "slug": c.slug,
            "summary": (c.summary or "")[:200],
            "image_url": c.image_url,
            "region": c.region,
            "date": c.date.isoformat() if c.date else None,
            "reading_time": c.reading_time,
            "similarity_score": round(sim, 3),
        }
        for sim, c in top
    ]
    _cache_set(cache_key, result_payload, CACHE_TTL_MED)
    return result_payload


# ════════════════════════════════════════════════════════════
#  V24: AI NATURAL-LANGUAGE SEARCH
#  "Aaj Pakistan ki technology news dikhao" → matches Pakistan + tech
# ════════════════════════════════════════════════════════════

@app.get("/api/ai-search")
async def ai_search(
    q: str = Query(..., min_length=2, max_length=200,
                   description="Natural-language query, e.g. 'latest Pakistan cricket news'"),
    limit: int = Query(10, ge=1, le=50),
    db=Depends(get_db),
):
    """V24: Natural-language search. Parses the user query for:
      - Regions mentioned (pakistan, usa, uk, india, germany, world)
      - Topics mentioned (cricket, election, economy, technology, etc.)
    Then runs FTS5 search on the extracted keywords. Falls back to plain
    FTS search if no entities are recognized.

    This is a lightweight NLP search — not a full LLM call. For better
    results, integrate with an embedding model (e.g. OpenAI ada-002)."""
    import re as _re
    q_lower = q.lower()

    # Step 1: Extract region(s)
    region_aliases = {
        "pakistan": ["pakistan", "pakistani", "islamabad", "lahore", "karachi"],
        "india":    ["india", "indian", "delhi", "mumbai", "bengaluru"],
        "usa":      ["usa", "america", "american", "united states", "washington", "new york"],
        "uk":       ["uk", "britain", "british", "england", "london"],
        "germany":  ["germany", "german", "berlin", "munich"],
        "world":    ["world", "global", "international"],
    }
    detected_regions = set()
    for region, aliases in region_aliases.items():
        if any(a in q_lower for a in aliases):
            detected_regions.add(region)

    # Step 2: Strip common question words to get the actual topic
    stopwords = [
        "aaj", "today", "latest", "news", "dikhao", "show", "find", "the",
        "ki", "ka", "ke", "mein", "in", "of", "about", "for", "and", "or",
        "from", "with", "what", "who", "when", "where", "why", "how",
        "headlines", "updates", "story", "stories",
    ]
    cleaned = q_lower
    for sw in stopwords:
        cleaned = _re.sub(rf"\b{sw}\b", " ", cleaned)
    cleaned = _re.sub(r"\s+", " ", cleaned).strip()

    # Use the cleaned query for FTS, falling back to original
    search_term = cleaned if len(cleaned) >= 3 else q.strip()

    # Step 3: Build query
    # If regions detected, search within those; otherwise global
    from database import IS_SQLITE, IS_POSTGRES
    tokens = [t for t in search_term.split() if len(t) >= 2]

    if not tokens:
        # Just return recent articles from detected regions (or all)
        # V29: Include NULL status as published
        stmt = select(Article).where(or_(Article.status == "published", Article.status == None))  # noqa: E711
        if detected_regions:
            stmt = stmt.where(Article.region.in_(detected_regions))
        stmt = stmt.order_by(Article.date.desc()).limit(limit)
        result = await db.execute(stmt)
        return [{"query_parsed": {"regions": list(detected_regions), "topic": search_term},
                 "results": [_brief_article(a) for a in result.scalars().all()]}]

    # FTS query
    try:
        if IS_SQLITE:
            fts_q = " ".join(f'"{t}"*' for t in tokens)
            sql = text(
                "SELECT a.id FROM articles a "
                "JOIN articles_fts f ON a.id = f.rowid "
                "WHERE articles_fts MATCH :q AND (a.status = 'published' OR a.status IS NULL) "
                + ("AND a.region IN :regions " if detected_regions else "")
                + "ORDER BY bm25(articles_fts) ASC LIMIT :limit"
            )
            params = {"q": fts_q, "limit": limit}
            if detected_regions:
                # SQLite doesn't support tuple-IN with named param easily; use placeholders
                placeholders = ",".join([f":r{i}" for i in range(len(detected_regions))])
                sql = text(
                    f"SELECT a.id FROM articles a "
                    f"JOIN articles_fts f ON a.id = f.rowid "
                    f"WHERE articles_fts MATCH :q AND (a.status = 'published' OR a.status IS NULL) "
                    f"AND a.region IN ({placeholders}) "
                    f"ORDER BY bm25(articles_fts) ASC LIMIT :limit"
                )
                for i, r in enumerate(detected_regions):
                    params[f"r{i}"] = r
            r = await db.execute(sql, params)
            ids = [row[0] for row in r.fetchall()]
        else:
            # Postgres fallback
            sql = text(
                "SELECT a.id FROM articles a "
                "WHERE to_tsvector('english', coalesce(a.title,'') || ' ' || "
                "coalesce(a.summary,'') || ' ' || coalesce(a.keywords,'')) "
                "@@ plainto_tsquery('english', :q) "
                "AND (a.status = 'published' OR a.status IS NULL) "
                + ("AND a.region = ANY(:regions) " if detected_regions else "")
                + "ORDER BY ts_rank DESC LIMIT :limit"
            )
            params = {"q": search_term, "limit": limit}
            if detected_regions:
                params["regions"] = list(detected_regions)
            r = await db.execute(sql, params)
            ids = [row[0] for row in r.fetchall()]

        if ids:
            art_result = await db.execute(
                select(Article).where(Article.id.in_(ids))
            )
            arts_by_id = {a.id: a for a in art_result.scalars().all()}
            ordered = [arts_by_id[i] for i in ids if i in arts_by_id]
            return [{"query_parsed": {"regions": list(detected_regions), "topic": search_term, "tokens": tokens},
                     "results": [_brief_article(a) for a in ordered]}]
    except Exception as e:
        logger.warning(f"AI search FTS failed: {e}")

    # Fallback: plain LIKE search
    term = f"%{search_term}%"
    stmt = (
        select(Article)
        .where(or_(Article.status == "published", Article.status == None))  # noqa: E711
        .where(or_(
            Article.title.ilike(term),
            Article.summary.ilike(term),
            Article.keywords.ilike(term),
        ))
    )
    if detected_regions:
        stmt = stmt.where(Article.region.in_(detected_regions))
    stmt = stmt.order_by(Article.date.desc()).limit(limit)
    result = await db.execute(stmt)
    return [{"query_parsed": {"regions": list(detected_regions), "topic": search_term, "fallback": "like"},
             "results": [_brief_article(a) for a in result.scalars().all()]}]


def _brief_article(a) -> dict:
    """Lightweight article dict for search/recommendation results."""
    return {
        "id": a.id, "title": a.title, "slug": a.slug,
        "summary": (a.summary or "")[:200],
        "image_url": a.image_url, "region": a.region,
        "date": a.date.isoformat() if a.date else None,
        "reading_time": a.reading_time,
        "fact_check_status": a.fact_check_status,
    }


# ════════════════════════════════════════════════════════════
#  V24: BREAKING NEWS + TRENDING TOPICS DETECTION
# ════════════════════════════════════════════════════════════

@app.get("/api/admin/breaking-news")
async def detect_breaking_news(
    request: Request,
    hours: int = Query(6, ge=1, le=48),
    min_sources: int = Query(2, ge=2, le=5),
    db=Depends(get_db),
):
    """V24: Detect breaking news — topics covered by multiple articles
    in the last `hours`. If the same event appears in 2+ articles from
    different RSS sources within the window, it's likely breaking.

    Algorithm:
      1. Fetch recent articles (last `hours`)
      2. Group by semantic similarity (TF-IDF cosine > 0.4)
      3. Return groups with >= `min_sources` articles
    """
    _require_admin(request)
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    result = await db.execute(
        select(Article)
        .where(Article.date >= cutoff)
        .where(or_(Article.status == "published", Article.status == None))  # noqa: E711
        .order_by(Article.date.desc())
        .limit(100)
    )
    articles = result.scalars().all()

    if len(articles) < min_sources:
        return {"breaking": [], "message": "Not enough recent articles to detect breaking news"}

    # Cluster by TF-IDF similarity
    try:
        from quality_control import _tf, _tokenize, _cosine_similarity
        # Pre-compute TF vectors
        tfs = []
        for a in articles:
            text = f"{a.title} {a.summary or ''}"
            tfs.append(_tf(_tokenize(text)))

        # Greedy clustering
        clusters: list[list[int]] = []  # list of article indices
        for i, tf_i in enumerate(tfs):
            placed = False
            for cluster in clusters:
                # Compare to first article in cluster (representative)
                rep_idx = cluster[0]
                sim = _cosine_similarity(tf_i, tfs[rep_idx])
                if sim >= 0.4:
                    cluster.append(i)
                    placed = True
                    break
            if not placed:
                clusters.append([i])

        # Filter to clusters with >= min_sources articles
        breaking = []
        for cluster in clusters:
            if len(cluster) < min_sources:
                continue
            cluster_arts = [articles[i] for i in cluster]
            breaking.append({
                "topic": cluster_arts[0].title,  # use first as representative
                "article_count": len(cluster_arts),
                "regions": list(set(a.region for a in cluster_arts)),
                "first_seen": min(a.date for a in cluster_arts).isoformat(),
                "latest_seen": max(a.date for a in cluster_arts).isoformat(),
                "articles": [
                    {"id": a.id, "title": a.title, "slug": a.slug,
                     "region": a.region, "date": a.date.isoformat()}
                    for a in cluster_arts[:5]
                ],
            })
        # Sort by article_count desc
        breaking.sort(key=lambda x: x["article_count"], reverse=True)
        return {"breaking": breaking[:10], "checked_articles": len(articles)}
    except Exception as e:
        return {"error": f"Clustering failed: {e}", "checked_articles": len(articles)}


# ════════════════════════════════════════════════════════════
#  V24: AUTOMATED DATABASE BACKUP (PG dump for Postgres)
# ════════════════════════════════════════════════════════════

@app.post("/api/admin/backup-db")
async def backup_database(request: Request):
    """V24: Trigger an on-demand database backup.
    For PostgreSQL: runs `pg_dump` to a file in /tmp and returns the path.
    For SQLite: copies the .db file.
    Requires admin auth. The file must be downloaded via SFTP / S3 sync
    (this endpoint just creates the dump)."""
    _require_admin(request)
    from database import IS_POSTGRES, IS_SQLITE, DATABASE_URL
    import subprocess
    import tempfile

    try:
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        backup_path = f"/tmp/sfaam_backup_{timestamp}"

        if IS_POSTGRES:
            # Parse connection info from DATABASE_URL
            # Format: postgresql+asyncpg://user:pass@host:port/dbname
            import re as _re
            m = _re.match(
                r"postgresql(?:\+asyncpg)?://([^:]+):([^@]+)@([^:/]+)(?::(\d+))?/(.+)",
                DATABASE_URL,
            )
            if not m:
                raise HTTPException(500, "Cannot parse DATABASE_URL for pg_dump")
            user, password, host, port, dbname = m.groups()
            env = os.environ.copy()
            env["PGPASSWORD"] = password
            cmd = [
                "pg_dump",
                "-h", host,
                "-p", port or "5432",
                "-U", user,
                "-F", "c",  # custom format (compressed)
                "-f", backup_path,
                dbname,
            ]
            subprocess.run(cmd, env=env, check=True, timeout=120, capture_output=True)
            size = os.path.getsize(backup_path)
            return {
                "status": "ok", "format": "pg_dump custom",
                "path": backup_path, "size_bytes": size,
                "timestamp": timestamp,
            }
        elif IS_SQLITE:
            import shutil
            # Parse SQLite path
            db_path = DATABASE_URL.replace("sqlite+aiosqlite:///", "").replace("sqlite:///", "")
            shutil.copy2(db_path, backup_path + ".db")
            size = os.path.getsize(backup_path + ".db")
            return {
                "status": "ok", "format": "sqlite copy",
                "path": backup_path + ".db", "size_bytes": size,
                "timestamp": timestamp,
            }
        else:
            raise HTTPException(500, "Unknown database type")
    except subprocess.CalledProcessError as e:
        logger.error(f"Backup failed: {e.stderr}")
        raise HTTPException(500, f"pg_dump failed: {e.stderr.decode()[:200]}")
    except Exception as e:
        logger.error(f"Backup error: {e}")
        raise HTTPException(500, f"Backup failed: {type(e).__name__}: {str(e)[:200]}")


# ════════════════════════════════════════════════════════════
#  V24: MULTILINGUAL TRANSLATION + AI VOICE NARRATION
# ════════════════════════════════════════════════════════════

@app.get("/api/articles/{article_id}/translate")
async def translate_article_endpoint(
    article_id: int,
    lang: str = Query("ur", description="Target language: ur, ar, hi, es, fr, de, fa, en"),
    db=Depends(get_db),
):
    """V24: Translate an article's title + body to the requested language.
    Uses Google Translate's free endpoint (no API key). Results cached
    in-memory for 24 hours to avoid repeated calls."""
    if lang not in ("ur", "ar", "hi", "es", "fr", "de", "fa", "en"):
        raise HTTPException(400, f"Unsupported language. Use: ur, ar, hi, es, fr, de, fa, en")

    result = await db.execute(select(Article).where(Article.id == article_id))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "Article not found")

    # Cache key
    cache_key = f"trans:{article_id}:{lang}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    try:
        from translation import translate_article
        translated = await translate_article(
            article_id=article_id, target_lang=lang,
            title=a.title, body=a.ai_content or "",
        )
        if "error" in translated:
            raise HTTPException(500, translated["error"])
        _cache_set(cache_key, translated, 86400)  # cache 24h
        return translated
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Translation failed for article {article_id}: {e}")
        raise HTTPException(500, f"Translation failed: {type(e).__name__}")


@app.post("/api/articles/{article_id}/narrate")
async def narrate_article_endpoint(
    article_id: int,
    lang: str = Query("en", description="Narration language"),
    request: Request = None,
    db=Depends(get_db),
):
    """V24: Generate AI voice narration (MP3) for an article.
    Uses edge-tts (Microsoft neural voices) by default; falls back to gTTS.
    Audio file is saved to /static/audio/ and served via StaticFiles mount."""
    result = await db.execute(select(Article).where(Article.id == article_id))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "Article not found")

    # Use first ~3000 chars (avoid generating hour-long MP3s)
    text = (a.ai_content or "")[:3000]
    if not text:
        raise HTTPException(400, "Article has no content to narrate")

    try:
        from translation import generate_audio_narration
        result = await generate_audio_narration(text, lang=lang, article_id=article_id)
        if "error" in result:
            raise HTTPException(500, result["error"])

        # Update article's audio_url field so the audio player picks it up
        a.audio_url = result["path"]
        a.audio_status = "ready"
        await db.commit()

        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Narration failed for article {article_id}: {e}")
        raise HTTPException(500, f"Narration failed: {type(e).__name__}")


@app.get("/api/podcast/daily")
async def daily_podcast_endpoint(
    lang: str = Query("en"),
    db=Depends(get_db),
):
    """V24: Generate (or fetch cached) daily news podcast — top 10 articles
    narrated as a single MP3 file. Regenerated daily."""
    if lang not in ("en", "ur", "ar", "hi", "es", "fr", "de", "fa"):
        raise HTTPException(400, "Unsupported language")

    cache_key = f"podcast:daily:{lang}:{datetime.utcnow().strftime('%Y%m%d')}"
    cached = _cache_get(cache_key)
    if cached and "path" in cached:
        return cached

    # Get top 10 articles
    result = await db.execute(
        select(Article)
        .where(or_(Article.status == "published", Article.status == None))  # noqa: E711
        .order_by(Article.date.desc())
        .limit(10)
    )
    articles = result.scalars().all()
    if not articles:
        raise HTTPException(404, "No articles available for podcast")

    article_dicts = [
        {"title": a.title, "summary": (a.summary or "")[:300]}
        for a in articles
    ]

    try:
        from translation import generate_daily_podcast
        podcast = await generate_daily_podcast(article_dicts, lang=lang)
        if "error" in podcast:
            raise HTTPException(500, podcast["error"])
        _cache_set(cache_key, podcast, 3600)  # cache 1 hour
        return podcast
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Podcast generation failed: {type(e).__name__}")


@app.get("/api/languages")
async def list_supported_languages():
    """V24: List supported translation + TTS languages."""
    from translation import SUPPORTED_LANGS
    return {"languages": SUPPORTED_LANGS}


# ════════════════════════════════════════════════════════════
#  V26 (Trends): Zero-Hallucination Content Engine — Admin Routes
#  - /api/admin/trends/drafts           list all trend-generated drafts
#  - /api/admin/trends/status           current pipeline status
#  - /api/admin/trends/run              manually trigger a cycle
#  - /api/admin/trends/{id}/publish     publish a trend draft
#  - /api/admin/trends/{id}/delete      delete a trend draft
#  - /api/admin/trends/{id}             full trend draft detail (with verified facts)
#  - /api/trends                        public list of published trends
#  - /api/trends/{slug}                 public single trend article
# ════════════════════════════════════════════════════════════

@app.get("/api/admin/trends/status")
async def trends_pipeline_status(request: Request):
    """Current Trends pipeline status — running flag, last cycle, counts."""
    _require_admin(request)
    try:
        from trends_scheduler import get_trends_status
        status = get_trends_status()
        # Also count drafts in DB
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(func.count(Article.id)).where(
                    Article.is_trends == 1,
                    Article.status == "draft",
                )
            )
            status["drafts_pending"] = int(result.scalar() or 0)
            result = await db.execute(
                select(func.count(Article.id)).where(
                    Article.is_trends == 1,
                    Article.status == "published",
                )
            )
            status["drafts_published"] = int(result.scalar() or 0)
        return status
    except ImportError:
        return {"running": False, "error": "trends_scheduler not installed"}
    except Exception as e:
        return {"running": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/admin/trends/run")
async def trends_trigger_cycle(request: Request, background_tasks: BackgroundTasks):
    """Manually trigger a Trends pipeline cycle (runs in background)."""
    _require_admin(request)
    try:
        from trends_scheduler import run_trends_cycle, get_trends_status
        if get_trends_status().get("running"):
            return {"status": "already_running", "message": "Pipeline is already running"}
        background_tasks.add_task(run_trends_cycle)
        return {"status": "triggered", "message": "Trends cycle started in background"}
    except ImportError:
        raise HTTPException(500, "trends_scheduler not installed")
    except Exception as e:
        raise HTTPException(500, f"Failed to trigger: {e}")


@app.get("/api/admin/trends/drafts")
async def trends_list_drafts(
    request: Request,
    status_filter: str = "draft",  # draft | published | rejected | all
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db=Depends(get_db),
):
    """List all trend-generated articles (default: only drafts pending review)."""
    _require_admin(request)
    stmt = select(Article).where(Article.is_trends == 1)
    if status_filter != "all":
        stmt = stmt.where(Article.status == status_filter)
    stmt = stmt.order_by(Article.date.desc())
    # count
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar() or 0
    # paginate
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(stmt)
    articles = result.scalars().all()
    return {
        "total": int(total),
        "page": page,
        "per_page": per_page,
        "status_filter": status_filter,
        "drafts": [_trends_draft_brief(a) for a in articles],
    }


def _trends_draft_brief(a) -> dict:
    """Compact representation of a trend draft for admin lists."""
    import json as _json
    verified_count = 0
    if a.verified_facts:
        try:
            verified_count = len(_json.loads(a.verified_facts))
        except Exception:
            verified_count = 0
    return {
        "id": a.id,
        "title": a.title,
        "slug": a.slug,
        "trend_query": a.trend_query,
        "status": a.status,
        "source_count": a.source_count,
        "facts_verified": verified_count,
        "word_count": a.word_count,
        "pipeline_version": a.pipeline_version,
        "date": a.date.isoformat() if a.date else None,
    }


@app.get("/api/admin/trends/{article_id}")
async def trends_draft_detail(article_id: int, request: Request, db=Depends(get_db)):
    """Full detail of one trend draft — including all verified facts and sources
    so the admin can review BEFORE publishing."""
    _require_admin(request)
    article = await db.get(Article, article_id)
    if not article or not article.is_trends:
        raise HTTPException(404, "Trend draft not found")
    import json as _json
    fact_sources = []
    verified_facts = []
    references = []
    try:
        if article.fact_sources:
            fact_sources = _json.loads(article.fact_sources)
    except Exception:
        pass
    try:
        if article.verified_facts:
            verified_facts = _json.loads(article.verified_facts)
    except Exception:
        pass
    try:
        if article.references_data:
            references = _json.loads(article.references_data)
    except Exception:
        pass
    return {
        "id": article.id,
        "title": article.title,
        "slug": article.slug,
        "trend_query": article.trend_query,
        "status": article.status,
        "content": article.ai_content,
        "summary": article.summary,
        "word_count": article.word_count,
        "source_count": article.source_count,
        "pipeline_version": article.pipeline_version,
        "fact_sources": fact_sources,
        "verified_facts": verified_facts,
        "references": references,
        "date": article.date.isoformat() if article.date else None,
    }


@app.post("/api/admin/trends/{article_id}/publish")
async def trends_publish_draft(article_id: int, request: Request, db=Depends(get_db)):
    """Publish a trend draft — admin approval step."""
    _require_admin(request)
    article = await db.get(Article, article_id)
    if not article or not article.is_trends:
        raise HTTPException(404, "Trend draft not found")
    article.status = "published"
    article.fact_check_status = "verified"
    await db.commit()
    invalidate_sitemap_cache()
    return {"status": "published", "article_id": article_id, "title": article.title}


@app.delete("/api/admin/trends/{article_id}")
async def trends_delete_draft(article_id: int, request: Request, db=Depends(get_db)):
    """Delete a trend draft entirely (admin rejection)."""
    _require_admin(request)
    article = await db.get(Article, article_id)
    if not article or not article.is_trends:
        raise HTTPException(404, "Trend draft not found")
    title = article.title
    await db.delete(article)
    await db.commit()
    return {"status": "deleted", "article_id": article_id, "title": title}


# ── Public Trends endpoints (only published trend articles) ──

@app.get("/api/trends")
async def public_trends_list(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
    db=Depends(get_db),
):
    """Public list of PUBLISHED trend articles (admin-approved only)."""
    stmt = select(Article).where(
        Article.is_trends == 1,
        Article.status == "published",
    ).order_by(Article.date.desc())
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar() or 0
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(stmt)
    articles = result.scalars().all()
    return {
        "total": int(total),
        "page": page,
        "per_page": per_page,
        "trends": [_trends_public_brief(a) for a in articles],
    }


def _trends_public_brief(a) -> dict:
    return {
        "id": a.id,
        "title": a.title,
        "slug": a.slug,
        "summary": a.summary,
        "trend_query": a.trend_query,
        "word_count": a.word_count,
        "source_count": a.source_count,
        "date": a.date.isoformat() if a.date else None,
    }


@app.get("/api/trends/{slug:path}")
async def public_trends_by_slug(slug: str, db=Depends(get_db)):
    """Public single trend article (must be published)."""
    stmt = select(Article).where(
        Article.is_trends == 1,
        Article.status == "published",
        Article.slug == slug,
    ).limit(1)
    result = await db.execute(stmt)
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(404, "Trend article not found")
    import json as _json
    references = []
    try:
        if article.references_data:
            references = _json.loads(article.references_data)
    except Exception:
        pass
    return {
        "id": article.id,
        "title": article.title,
        "slug": article.slug,
        "content": article.ai_content,
        "summary": article.summary,
        "trend_query": article.trend_query,
        "word_count": article.word_count,
        "source_count": article.source_count,
        "references": references,
        "date": article.date.isoformat() if article.date else None,
    }


# ── V29: Short region URL redirects ──
# /usa → /category/usa, /uk → /category/uk, etc.
# These are the URLs users actually type, so they MUST work.
# NOTE: the short-region redirect /{region_name} was removed because it
# intercepted ALL single-segment paths (sw.js, about.html, logo.png, etc.)
# before the catch-all could handle them. The redirect is now handled inside
# the catch-all below.


# ════════════════════════════════════════════════════════════
#  V30 (SFAAM Automated News Engine / TRD v1.0): Admin Routes
#  - /api/admin/engine/status            current engine + scheduler status
#  - /api/admin/engine/regions           per-region LLM key + trend source health
#  - /api/admin/engine/run               manually trigger a full 3-hour cycle
#  - /api/admin/engine/run/{region}      manually trigger a single region
#  - /api/admin/engine/drafts            list all engine-generated drafts (V30)
#  - /api/admin/engine/cycles            list recent cycle logs
#  - /api/admin/engine/cycles/{id}       full cycle detail with per-region results
#  - /api/admin/engine/{id}/publish      publish an engine draft
# ════════════════════════════════════════════════════════════

@app.get("/api/admin/engine/status")
async def engine_status(request: Request):
    """Return current V30 engine + scheduler status for the admin dashboard."""
    _require_admin(request)
    try:
        from automated_news_engine import get_engine_status
        from engine_scheduler import get_scheduler_info
        from region_config import get_region_key_status, get_aggregator_key_status

        status = get_engine_status()
        status["scheduler"] = get_scheduler_info()
        status["regions"] = get_region_key_status()
        status["aggregators"] = get_aggregator_key_status()

        # Count V30 drafts + cycle logs in DB
        async with AsyncSessionLocal() as db:
            from database import EngineCycleLog
            result = await db.execute(
                select(func.count(Article.id)).where(
                    Article.is_trends == 1,
                    Article.pipeline_version == "v30_trd1.0",
                    Article.status == "draft",
                )
            )
            status["drafts_pending"] = int(result.scalar() or 0)
            result = await db.execute(
                select(func.count(Article.id)).where(
                    Article.is_trends == 1,
                    Article.pipeline_version == "v30_trd1.0",
                    Article.status == "published",
                )
            )
            status["drafts_published"] = int(result.scalar() or 0)
            # Last 5 cycle logs
            result = await db.execute(
                select(EngineCycleLog)
                .order_by(EngineCycleLog.started_at.desc())
                .limit(5)
            )
            status["recent_cycles"] = [
                {
                    "cycle_id": c.cycle_id,
                    "started_at": c.started_at.isoformat() if c.started_at else None,
                    "completed_at": c.completed_at.isoformat() if c.completed_at else None,
                    "status": c.status,
                    "regions_processed": c.regions_processed,
                    "drafts_produced": c.drafts_produced,
                    "drafts_failed": c.drafts_failed,
                    "skipped_duplicates": c.skipped_duplicates,
                    "total_elapsed_s": c.total_elapsed_s,
                    "error": c.error,
                }
                for c in result.scalars().all()
            ]
        return status
    except Exception as e:
        logger.exception(f"[V30] engine status error: {e}")
        raise HTTPException(500, f"Engine status error: {e}")


@app.post("/api/admin/engine/run")
async def engine_run_now(request: Request, region: Optional[str] = None):
    """Manually trigger a V30 engine cycle (full or single-region).

    If `region` query param is given (e.g. /api/admin/engine/run?region=pakistan),
    only that region runs. Otherwise all 6 regions run sequentially.
    """
    _require_admin(request)
    # V30 FIX (Bug #19): Rate limit manual triggers to max 6/hour (one every
    # 10 min). Prevents admin from spamming the button if the engine takes
    # longer than expected. The 3-hour scheduler is unaffected.
    client_ip = _get_client_ip(request)
    if not _check_rate_limit(f"engine_run:{client_ip}", 6, 3600):
        raise HTTPException(429, "Too many engine run requests. Max 6 per hour. Wait 10 minutes.")
    try:
        from automated_news_engine import run_engine_cycle, get_engine_status
        if get_engine_status().get("running"):
            raise HTTPException(409, "Engine cycle already running — wait for it to complete")
        regions = [region] if region else None
        # Bug #13 FIX: Store task reference to prevent garbage collection.
        # Python's asyncio docs warn that create_task() without retaining a
        # reference can cause the task to be GC'd before completion.
        # Also wrap in a coroutine that logs exceptions explicitly (otherwise
        # asyncio swallows them with just a "Task exception was never retrieved" warning).
        import asyncio

        async def _safe_run_cycle():
            try:
                await run_engine_cycle(run_only_regions=regions)
            except Exception as e:
                logger.exception(f"[V30] Background engine cycle crashed: {e}")

        # Store on the app state so the reference lives as long as the request
        # handler coroutine (which is long enough — the task itself holds the
        # event loop reference once it starts running).
        task = asyncio.create_task(_safe_run_cycle())
        # Keep a reference on the FastAPI app state to prevent GC
        if not hasattr(app.state, "_engine_tasks"):
            app.state._engine_tasks = set()
        app.state._engine_tasks.add(task)
        task.add_done_callback(app.state._engine_tasks.discard)

        return {
            "status": "triggered",
            "regions": regions or "all",
            "message": "Engine cycle started — check /api/admin/engine/status for progress",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[V30] engine run trigger error: {e}")
        raise HTTPException(500, f"Engine run trigger error: {e}")


# ════════════════════════════════════════════════════════════
# ENGINE V2 (clean rebuild — search-driven, full-article pipeline)
#  - /api/admin/engine2/status           current status + recent cycles
#  - /api/admin/engine2/run              manually trigger a full cycle (all regions)
#  - /api/admin/engine2/drafts           list Engine V2 drafts
# ════════════════════════════════════════════════════════════

@app.get("/api/admin/engine2/status")
async def engine2_status(request: Request):
    """Return current Engine V2 status for the admin dashboard."""
    _require_admin(request)
    try:
        from engine2_scheduler import get_status
        from region_config import get_region_key_status, get_aggregator_key_status

        status = get_status()
        status["regions"] = get_region_key_status()
        status["aggregators"] = get_aggregator_key_status()

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(func.count(Article.id)).where(
                    Article.pipeline_version == "engine_v2",
                    Article.status == "draft",
                )
            )
            status["drafts_pending"] = int(result.scalar() or 0)
            result = await db.execute(
                select(func.count(Article.id)).where(
                    Article.pipeline_version == "engine_v2",
                    Article.status == "published",
                )
            )
            status["drafts_published"] = int(result.scalar() or 0)
        return status
    except Exception as e:
        logger.exception(f"[EngineV2] status error: {e}")
        raise HTTPException(500, f"Engine V2 status error: {e}")


@app.post("/api/admin/engine2/run")
async def engine2_run_now(request: Request):
    """Manually trigger a full Engine V2 cycle (all 6 regions in parallel)."""
    _require_admin(request)
    client_ip = _get_client_ip(request)
    if not _check_rate_limit(f"engine2_run:{client_ip}", 6, 3600):
        raise HTTPException(429, "Too many engine run requests. Max 6 per hour. Wait 10 minutes.")
    try:
        from engine2_scheduler import run_full_cycle, get_status
        if get_status().get("running"):
            raise HTTPException(409, "Engine V2 cycle already running — wait for it to complete")
        import asyncio

        async def _safe_run_cycle():
            try:
                await run_full_cycle()
            except Exception as e:
                logger.exception(f"[EngineV2] Background cycle crashed: {e}")

        task = asyncio.create_task(_safe_run_cycle())
        if not hasattr(app.state, "_engine2_tasks"):
            app.state._engine2_tasks = set()
        app.state._engine2_tasks.add(task)
        task.add_done_callback(app.state._engine2_tasks.discard)

        return {
            "status": "triggered",
            "regions": "all",
            "message": "Engine V2 cycle started — check /api/admin/engine2/status for progress",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[EngineV2] run trigger error: {e}")
        raise HTTPException(500, f"Engine V2 run trigger error: {e}")


@app.get("/api/admin/engine2/drafts")
async def engine2_list_drafts(request: Request, limit: int = 50):
    """List Engine V2-generated drafts (status='draft'), newest first."""
    _require_admin(request)
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Article)
                .where(Article.pipeline_version == "engine_v2", Article.status == "draft")
                .order_by(Article.date.desc())
                .limit(min(limit, 200))
            )
            drafts = result.scalars().all()
            return {
                "count": len(drafts),
                "drafts": [
                    {
                        "id": a.id, "title": a.title, "region": a.region,
                        "summary": a.summary, "image_url": a.image_url,
                        "word_count": a.word_count, "source_count": a.source_count,
                        "trend_query": a.trend_query, "llm_provider": a.llm_provider,
                        "date": a.date.isoformat() if a.date else None,
                    }
                    for a in drafts
                ],
            }
    except Exception as e:
        logger.exception(f"[EngineV2] drafts list error: {e}")
        raise HTTPException(500, f"Engine V2 drafts list error: {e}")


@app.get("/api/admin/engine/drafts")
async def engine_drafts(
    request: Request,
    region: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db=Depends(get_db),
):
    """List all V30 engine-generated drafts (status='draft'), newest first.

    Optional `region` filter. Returns TRD-specific metadata (word_count_tier,
    trend_score, source_count, llm_provider) for the admin dashboard.
    """
    _require_admin(request)
    stmt = select(Article).where(
        Article.is_trends == 1,
        Article.pipeline_version == "v30_trd1.0",
        Article.status == "draft",
    )
    if region:
        if region not in REGIONS:
            raise HTTPException(400, f"Invalid region. Valid: {sorted(REGIONS)}")
        stmt = stmt.where(Article.region == region)
    stmt = stmt.order_by(Article.date.desc())
    # Count total
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar() or 0
    # Paginate
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(stmt)
    articles = result.scalars().all()

    return {
        "total": int(total),
        "page": page,
        "per_page": per_page,
        "region_filter": region,
        "drafts": [
            {
                "id": a.id,
                "title": a.title,
                "slug": a.slug,
                "summary": a.summary,
                "region": a.region,
                "trend_query": a.trend_query,
                "trend_score": a.trend_score,
                "cross_source_count": a.cross_source_count,
                "source_count": a.source_count,
                "word_count": a.word_count,
                "word_count_tier": a.word_count_tier,
                "word_count_target": a.word_count_target,
                "llm_provider": a.llm_provider,
                "llm_model": a.llm_model,
                "raw_facts_count": a.raw_facts_count,
                "dropped_facts_count": a.dropped_facts_count,
                "fact_extraction_elapsed_s": a.fact_extraction_elapsed_s,
                "article_generation_elapsed_s": a.article_generation_elapsed_s,
                "date": a.date.isoformat() if a.date else None,
            }
            for a in articles
        ],
    }


@app.get("/api/admin/engine/cycles")
async def engine_cycles(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db=Depends(get_db),
):
    """List recent V30 engine cycle logs (admin audit trail)."""
    _require_admin(request)
    from database import EngineCycleLog
    stmt = select(EngineCycleLog).order_by(EngineCycleLog.started_at.desc())
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar() or 0
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(stmt)
    cycles = result.scalars().all()
    return {
        "total": int(total),
        "page": page,
        "per_page": per_page,
        "cycles": [
            {
                "id": c.id,
                "cycle_id": c.cycle_id,
                "started_at": c.started_at.isoformat() if c.started_at else None,
                "completed_at": c.completed_at.isoformat() if c.completed_at else None,
                "status": c.status,
                "regions_processed": c.regions_processed,
                "drafts_produced": c.drafts_produced,
                "drafts_failed": c.drafts_failed,
                "skipped_duplicates": c.skipped_duplicates,
                "total_elapsed_s": c.total_elapsed_s,
                "error": c.error,
            }
            for c in cycles
        ],
    }


@app.get("/api/admin/engine/cycles/{cycle_id}")
async def engine_cycle_detail(cycle_id: str, request: Request, db=Depends(get_db)):
    """Full detail for one V30 engine cycle, including per-region results."""
    _require_admin(request)
    from database import EngineCycleLog
    result = await db.execute(
        select(EngineCycleLog).where(EngineCycleLog.cycle_id == cycle_id).limit(1)
    )
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Cycle not found")
    import json as _json
    region_summary = []
    try:
        if c.region_summary:
            region_summary = _json.loads(c.region_summary)
    except Exception:
        pass
    return {
        "id": c.id,
        "cycle_id": c.cycle_id,
        "started_at": c.started_at.isoformat() if c.started_at else None,
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
        "status": c.status,
        "regions_processed": c.regions_processed,
        "drafts_produced": c.drafts_produced,
        "drafts_failed": c.drafts_failed,
        "skipped_duplicates": c.skipped_duplicates,
        "total_elapsed_s": c.total_elapsed_s,
        "error": c.error,
        "region_summary": region_summary,
    }


@app.post("/api/admin/engine/{article_id}/publish")
async def engine_publish_draft(article_id: int, request: Request, db=Depends(get_db)):
    """Publish a V30 engine draft (admin editorial sign-off).

    Per TRD Section 5: "Admin must execute the 'Publish' command on the
    top 5 absolute masterpieces."
    """
    _require_admin(request)
    result = await db.execute(
        select(Article).where(
            Article.id == article_id,
            Article.pipeline_version == "v30_trd1.0",
        ).limit(1)
    )
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(404, "V30 engine draft not found")
    if article.status == "published":
        raise HTTPException(409, "Article already published")
    article.status = "published"
    article.updated_at = datetime.utcnow()
    await db.commit()
    # V30 FIX (Bug #21): Audit log the publish action
    try:
        from monitoring import log_audit_event
        log_audit_event(
            admin_id=request.headers.get("x-admin-session", "")[:16] or "admin",
            action="engine.article.publish",
            target_type="article", target_id=article_id,
            details={"title": article.title[:100], "region": article.region,
                     "word_count": article.word_count, "tier": article.word_count_tier},
            ip_address=_get_client_ip(request),
            success=True,
        )
    except Exception:
        pass  # audit log is best-effort
    # V23: invalidate sitemap cache so the newly-published article appears
    try:
        invalidate_sitemap_cache()
    except Exception:
        pass
    logger.info(f"[V30] Admin published article id={article_id} '{article.title[:50]}'")
    return {
        "status": "published",
        "article_id": article_id,
        "title": article.title,
        "region": article.region,
    }


@app.get("/api/admin/engine/regions")
async def engine_regions_health(request: Request):
    """Return per-region LLM key + trend aggregator health (TRD Section 2)."""
    _require_admin(request)
    try:
        from region_config import get_region_key_status, get_aggregator_key_status, REGIONS
        return {
            "regions": get_region_key_status(),
            "aggregators": get_aggregator_key_status(),
            "region_order": [r.key for r in REGIONS],
        }
    except Exception as e:
        raise HTTPException(500, f"Region health error: {e}")


# ── V32: Live Readers Count (real-time social proof) ──
# Tracks unique browsers viewing articles in the last 5 minutes.
# Stored in Redis if available, else in-memory dict.
_live_readers: dict = {}  # {fingerprint: last_seen_timestamp}
_LIVE_READERS_WINDOW = 300  # 5 minutes


@app.get("/api/live-readers")
async def live_readers_count(request: Request):
    """V32: Return the current number of readers on the site (last 5 min).
    Used for the 'X readers right now' social proof widget."""
    import time as _time
    now = _time.time()
    cutoff = now - _LIVE_READERS_WINDOW
    # Clean up old entries
    if _redis_client:
        try:
            # Use a Redis sorted set with timestamp as score
            _redis_client.zremrangebyscore("live_readers", 0, cutoff)
            count = _redis_client.zcard("live_readers")
            # Add this visitor
            fp = _user_fingerprint(request, "")
            _redis_client.zadd("live_readers", {fp: now})
            return {"readers": max(count, 1), "window_seconds": _LIVE_READERS_WINDOW}
        except Exception:
            pass
    # In-memory fallback
    global _live_readers
    _live_readers = {k: v for k, v in _live_readers.items() if v >= cutoff}
    fp = _user_fingerprint(request, "")
    _live_readers[fp] = now
    return {"readers": max(len(_live_readers), 1), "window_seconds": _LIVE_READERS_WINDOW}


# ── V32: Duplicate Titles Report (Admin) ──
# Scans the DB for articles with duplicate or near-duplicate titles.
# Returns groups of conflicting articles so the admin can rename or delete.
@app.get("/api/admin/duplicate-titles")
async def duplicate_titles_report(request: Request, db=Depends(get_db)):
    """V31.1: Find all groups of articles with duplicate normalized titles.
    Returns groups sorted by size (largest first). Each group includes the
    normalized title, count, and full article metadata for each duplicate."""
    _require_admin(request)
    try:
        from title_uniqueness import find_all_duplicate_titles
        duplicates = await find_all_duplicate_titles(db, limit=100)
        return {
            "total_groups": len(duplicates),
            "total_duplicate_articles": sum(g["count"] for g in duplicates),
            "groups": duplicates,
        }
    except Exception as e:
        logger.error(f"[V31.1] duplicate-titles endpoint error: {e}")
        raise HTTPException(500, f"Could not scan for duplicates: {e}")


# ── V31.1: Backfill title_norm (Admin) ──
# Manually trigger the title_norm backfill for existing articles that were
# saved before V31.1. Useful if the auto-migration was interrupted.
@app.post("/api/admin/backfill-title-norm")
async def backfill_title_norm(request: Request, db=Depends(get_db)):
    """V31.1: Backfill title_norm column for all articles with NULL title_norm.
    Also fixes any articles with stale title_norm (doesn't match current title)."""
    _require_admin(request)
    try:
        from title_uniqueness import compute_title_norm
        from sqlalchemy import text
        # Find articles with NULL or stale title_norm
        result = await db.execute(text(
            "SELECT id, title, title_norm FROM articles "
            "WHERE title_norm IS NULL "
            "OR title_norm != '' "
            "ORDER BY id"
        ))
        rows = result.fetchall()
        fixed = 0
        for art_id, art_title, existing_norm in rows:
            new_norm = compute_title_norm(art_title or "")[:500]
            if new_norm != (existing_norm or ""):
                await db.execute(text(
                    "UPDATE articles SET title_norm = :norm WHERE id = :id"
                ), {"norm": new_norm, "id": art_id})
                fixed += 1
        await db.commit()
        return {
            "status": "ok",
            "scanned": len(rows),
            "updated": fixed,
            "skipped": len(rows) - fixed,
        }
    except Exception as e:
        logger.error(f"[V31.1] backfill-title-norm error: {e}")
        raise HTTPException(500, f"Backfill failed: {e}")


# ── V31: AI Health Check (Admin) ──
# Quick lightweight test of each region's Groq + Gemini keys without running a
# full pipeline. Pings each AI provider with a 5-token "ping" request and
# reports which keys are working. Useful for debugging "why no articles today?"
@app.get("/api/admin/ai-health")
async def ai_health_check(request: Request):
    """V31: Test each region's AI keys (Groq + Gemini) with a tiny ping request.
    Returns per-region per-provider status: ok / fail / no-key.
    Does NOT write to DB — read-only diagnostic."""
    _require_admin(request)
    results = {}
    regions = ["world", "usa", "uk", "pakistan", "india", "germany"]
    for region in regions:
        groq_key = os.getenv(f"GROQ_KEY_{region.upper()}", "")
        gemini_key = os.getenv(f"GEMINI_KEY_{region.upper()}", "")
        region_result = {"groq": {"configured": bool(groq_key), "status": "skipped"},
                         "gemini": {"configured": bool(gemini_key), "status": "skipped"}}
        # Test Groq with a tiny 5-token ping (no actual content generation)
        if groq_key:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=8.0) as client:
                    r = await client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                        json={"model": "llama-3.3-70b-versatile",
                              "messages": [{"role": "user", "content": "ping"}],
                              "max_tokens": 5},
                    )
                    region_result["groq"]["status"] = "ok" if r.status_code == 200 else f"fail_{r.status_code}"
            except Exception as e:
                region_result["groq"]["status"] = f"error: {type(e).__name__}"
        else:
            region_result["groq"]["status"] = "no_key"
        # Test Gemini with a tiny ping
        if gemini_key:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=8.0) as client:
                    r = await client.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_key}",
                        headers={"Content-Type": "application/json"},
                        json={"contents": [{"parts": [{"text": "ping"}]}],
                              "generationConfig": {"maxOutputTokens": 5}},
                    )
                    region_result["gemini"]["status"] = "ok" if r.status_code == 200 else f"fail_{r.status_code}"
            except Exception as e:
                region_result["gemini"]["status"] = f"error: {type(e).__name__}"
        else:
            region_result["gemini"]["status"] = "no_key"
        results[region] = region_result
    # Summary count
    ok_count = sum(1 for r in results.values() for p in r.values() if p["status"] == "ok")
    total = len(regions) * 2
    return {
        "summary": {"ok": ok_count, "total": total, "pct": round(ok_count / total * 100, 1) if total else 0},
        "regions": results,
    }


# ── V32.1: Newsletter subscribe endpoint ──
# Used by the article-bottom newsletter CTA added in V32.1. Stores the
# email in a simple JSON file (data/newsletter_subscribers.json) — no DB
# table needed, no external service required. For production scale,
# replace with a real ESP integration (Mailchimp, Sendinblue, etc.).
_NEWSLETTER_FILE = os.path.join(os.path.dirname(__file__), "data", "newsletter_subscribers.json")


@app.post("/api/newsletter/subscribe")
async def newsletter_subscribe(data: dict, request: Request):
    """Subscribe an email to the SFAAM NEWS newsletter.
    Body: {"email": "...", "source": "article_bottom" | "homepage" | "footer"}.
    Returns {"success": true, "message": "..."}."""
    import re as _re
    import time as _time
    email = (data.get("email") or "").strip().lower()
    source = (data.get("source") or "unknown").strip()[:50]
    if not email or not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "Invalid email address")
    try:
        os.makedirs(os.path.dirname(_NEWSLETTER_FILE), exist_ok=True)
        existing = []
        if os.path.exists(_NEWSLETTER_FILE):
            with open(_NEWSLETTER_FILE, "r", encoding="utf-8") as f:
                try:
                    existing = _json.load(f)
                except Exception:
                    existing = []
        # Don't double-add
        if any(s.get("email") == email for s in existing):
            return {"success": True, "message": "You're already subscribed!", "already_subscribed": True}
        existing.append({
            "email": email,
            "source": source,
            "subscribed_at": _time.time(),
            "ip": request.client.host if request.client else "",
        })
        with open(_NEWSLETTER_FILE, "w", encoding="utf-8") as f:
            _json.dump(existing, f, indent=2)
        logger.info(f"[Newsletter] New subscriber from {source}: {email}")
        return {"success": True, "message": "Subscribed successfully. Check your inbox for confirmation."}
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"[Newsletter] Subscribe failed: {e}")
        raise HTTPException(500, "Subscription failed. Please try again later.")


# ── Frontend Serve (MUST BE LAST) ──
# FIX: use STATIC_DIR (absolute) so path resolution is CWD-independent.
# Also handles short region redirects (/world → /category/world) that were
# previously in a separate /{region_name} route which blocked all root-level
# static files (sw.js, manifest.json, logo.png, etc.) from being served.
@app.get("/{full_path:path}")
async def serve(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(404, "API endpoint not found")

    # Short-region redirect: /world → /category/world (was a separate route
    # that caused 404 for every non-region single-segment path like sw.js)
    if "/" not in full_path and full_path.strip().lower() in REGIONS:
        return RedirectResponse(
            url=f"/category/{full_path.strip().lower()}",
            status_code=301,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    target = os.path.join(STATIC_DIR, full_path) if full_path else os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(target) and os.path.isfile(target):
        return FileResponse(target)
    # V28: genuinely unmatched paths get a real 404 status (not a silent
    # 200 fallback to the homepage) — better for SEO and for anyone
    # following a broken/typo'd link. Falls back to index.html only if
    # 404.html itself is somehow missing.
    not_found_page = os.path.join(STATIC_DIR, "404.html")
    if os.path.exists(not_found_page):
        return FileResponse(not_found_page, status_code=404)
    index = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    raise HTTPException(404, "Page not found")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", 8000)),
        reload=False,
    )
