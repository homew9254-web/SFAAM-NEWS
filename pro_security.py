"""
pro_security.py — SFAAM NEWS PRO 1 — Security middleware
========================================================

Production-grade security headers and rate limiting for FastAPI.

Features:
  - CSP (Content Security Policy) with per-route opt-in for inline
  - HSTS (Strict Transport Security) — 2 years + preload
  - X-Frame-Options: DENY (clickjacking protection)
  - X-Content-Type-Options: nosniff (MIME sniffing protection)
  - Referrer-Policy: strict-origin-when-cross-origin
  - Permissions-Policy (lock down camera, mic, geolocation, etc.)
  - Cross-Origin Opener Policy / Embedder Policy
  - Per-IP rate limiting (sliding window via Redis or in-memory)
  - CSRF protection for state-changing routes (admin, votes, comments)

Usage in main.py:
    from pro_security import install_pro_security
    install_pro_security(app, redis_client=_redis_client)

The middleware is SAFE by default — it adds headers but does not
block anything. CSP is report-only mode initially so we can monitor
violations without breaking the site.
"""
from __future__ import annotations

import time
import logging
import os
import hashlib
import secrets
from collections import defaultdict, deque
from typing import Optional, Callable

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Security headers — applied to every response
# ─────────────────────────────────────────────────────────────

# Content Security Policy.
# We use 'report-only' mode initially so we can collect violation
# reports without breaking the site. Once we've reviewed a week of
# reports and confirmed no false positives, switch to enforcing.
# Set PRO_CSP_ENFORCE=true in env to switch to enforcing mode.
CSP_ENFORCE = os.getenv("PRO_CSP_ENFORCE", "false").lower() == "true"

CSP_DIRECTIVES = [
    "default-src 'self'",
    # Inline scripts: we use 'unsafe-inline' for the legacy templates
    # that have onclick= handlers; long-term, replace with nonce-based.
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://fonts.googleapis.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https: blob:",
    "media-src 'self' blob:",
    "connect-src 'self' https:",
    "frame-ancestors 'none'",  # equivalent to X-Frame-Options: DENY
    "form-action 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "upgrade-insecure-requests",
    # Reporting endpoint (see /csp-report below)
    "report-uri /csp-report",
]

CSP_HEADER_VALUE = "; ".join(CSP_DIRECTIVES)


class ProSecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds security headers to every response."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # HSTS — only meaningful over HTTPS, but the header itself is
        # harmless on HTTP (browsers ignore it).
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains; preload"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Lock down browser features we don't use
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), "
            "interest-cohort=(), payment=(), usb=(), "
            "accelerometer=(), gyroscope=(), magnetometer=()"
        )
        # Side-channel-attack mitigations
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
        response.headers["Cross-Origin-Resource-Policy"] = "same-site"
        # CSP — report-only initially
        csp_header = "Content-Security-Policy-Report-Only" if not CSP_ENFORCE else "Content-Security-Policy"
        response.headers[csp_header] = CSP_HEADER_VALUE
        # Cache-control for HTML pages: no-store (we want fresh content)
        # Static assets are handled by FastAPI's StaticFiles with their own headers.
        return response


# ─────────────────────────────────────────────────────────────
# Rate limiting — sliding window per IP
# ─────────────────────────────────────────────────────────────

class ProRateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP sliding-window rate limiter.

    Uses Redis if available (so limits are shared across workers);
    falls back to an in-memory deque per process (limits apply per
    worker only — fine for single-worker deploys).

    Limits are tiered by route sensitivity:
      - /api/admin/*      → 10 req / min  (very tight)
      - /api/comment/*    → 20 req / min  (anti-spam)
      - /api/vote/*       → 30 req / min  (anti-fraud)
      - /api/search       → 60 req / min  (anti-scrape)
      - everything else   → 300 req / min (normal browsing)
    """

    LIMITS = [
        ("/api/admin", 10, 60),
        ("/api/comment", 20, 60),
        ("/api/comments", 20, 60),
        ("/api/polls", 30, 60),
        ("/api/quiz", 30, 60),
        ("/api/newsletter", 5, 300),  # 5 per 5 min — anti-spam
        ("/api/digest", 5, 300),
        ("/api/search", 60, 60),
        ("/api/personalize", 60, 60),
    ]
    DEFAULT_LIMIT = (300, 60)  # 300 req / 60s

    def __init__(self, app, redis_client=None):
        super().__init__(app)
        self.redis = redis_client
        # In-memory fallback: {ip: {route_prefix: deque[timestamps]}}
        self._mem: dict[str, dict[str, deque]] = defaultdict(lambda: defaultdict(deque))
        # Cleanup counter — purge old entries every N requests
        self._cleanup_counter = 0

    def _client_ip(self, request: Request) -> str:
        # Use the leftmost X-Forwarded-For entry (trusted proxy strips/spoofs).
        # Fall back to direct connection IP.
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _limit_for(self, path: str) -> tuple[int, int]:
        for prefix, limit, window in self.LIMITS:
            if path.startswith(prefix):
                return limit, window
        return self.DEFAULT_LIMIT

    async def _check_redis(self, ip: str, path: str, limit: int, window: int) -> bool:
        """Returns True if request is allowed."""
        key = f"pro_rl:{ip}:{path.rsplit('/', 1)[0] if '/' in path[5:] else path}"
        try:
            pipe = self.redis.pipeline()
            now = time.time()
            pipe.zremrangebyscore(key, 0, now - window)
            pipe.zadd(key, {str(now): now})
            pipe.zcard(key)
            pipe.expire(key, window)
            _, _, count, _ = pipe.execute()
            return count <= limit
        except Exception as e:
            logger.debug(f"[ProRateLimit] Redis error, falling back to memory: {e}")
            return self._check_mem(ip, path, limit, window)

    def _check_mem(self, ip: str, path: str, limit: int, window: int) -> bool:
        bucket_key = path.rsplit("/", 1)[0] if len(path) > 5 else path
        dq = self._mem[ip][bucket_key]
        now = time.time()
        # Drop expired
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True

    async def dispatch(self, request: Request, call_next):
        # Only rate-limit API routes — static assets bypass
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)

        ip = self._client_ip(request)
        limit, window = self._limit_for(path)

        allowed = (
            await self._check_redis(ip, path, limit, window)
            if self.redis
            else self._check_mem(ip, path, limit, window)
        )

        # Periodic cleanup of in-memory store
        self._cleanup_counter += 1
        if self._cleanup_counter > 1000 and not self.redis:
            self._cleanup_counter = 0
            now = time.time()
            for ip_key, buckets in list(self._mem.items()):
                for bkey, dq in list(buckets.items()):
                    while dq and now - dq[0] > 3600:
                        dq.popleft()
                    if not dq:
                        del buckets[bkey]
                if not buckets:
                    del self._mem[ip_key]

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "message": "Too many requests. Please slow down.",
                    "retry_after": window,
                },
                headers={
                    "Retry-After": str(window),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Window": str(window),
                },
            )

        return await call_next(request)


# ─────────────────────────────────────────────────────────────
# CSRF protection for state-changing routes
# ─────────────────────────────────────────────────────────────

# Routes that require CSRF tokens. GET/HEAD/OPTIONS are always allowed.
CSRF_PROTECTED_PREFIXES = (
    "/api/admin/",
    "/api/polls/",
    "/api/quiz/",
    "/api/comment",
    "/api/comments",
    "/api/like",
    "/api/highlight",
    "/api/reaction",
    "/api/bookmark",
    "/api/personalize/feedback",
    "/api/newsletter/subscribe",
    "/api/digest/subscribe",
    "/api/push/subscribe",
)


class ProCSRFMiddleware(BaseHTTPMiddleware):
    """Validates CSRF tokens on POST/PUT/DELETE requests to protected routes.

    V32.1 already has CSRF logic in main.py for admin routes — this
    extends it to ALL state-changing endpoints (comments, votes, etc.)
    using the same double-submit cookie pattern.

    The CSRF token is set as a cookie on first GET, and the client
    must echo it back in the X-CSRF-Token header on mutations.
    """

    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method.upper()

        # Generate / refresh CSRF token cookie on every safe request
        if method in self.SAFE_METHODS:
            response = await call_next(request)
            existing = request.cookies.get("sfaam_csrf")
            if not existing:
                token = secrets.token_urlsafe(32)
                response.set_cookie(
                    key="sfaam_csrf",
                    value=token,
                    httponly=False,  # JS needs to read it
                    samesite="lax",
                    secure=request.url.scheme == "https",
                    max_age=60 * 60 * 24 * 365,  # 1 year
                    path="/",
                )
            return response

        # Mutation request — check CSRF if route is protected
        is_protected = any(path.startswith(p) for p in CSRF_PROTECTED_PREFIXES)
        if not is_protected:
            return await call_next(request)
                  # Login endpoint exempt — no session exists yet to carry CSRF token
        if path == "/api/admin/login":
            return await call_next(request)

        cookie_token = request.cookies.get("sfaam_csrf")
        header_token = request.headers.get("x-csrf-token")

        if not cookie_token or not header_token or cookie_token != header_token:
            return JSONResponse(
                status_code=403,
                content={
                    "error": "csrf_failed",
                    "message": "CSRF token missing or invalid. Refresh the page and try again.",
                },
            )

        return await call_next(request)


# ─────────────────────────────────────────────────────────────
# CSP violation report endpoint
# ─────────────────────────────────────────────────────────────

async def csp_report_handler(request: Request) -> JSONResponse:
    """Receives CSP violation reports from browsers and logs them.

    In production, forward these to Sentry or a dedicated log pipeline
    so we can spot attempted XSS / script injection before flipping
    CSP from report-only to enforcing.
    """
    try:
        body = await request.json()
        report = body.get("csp-report", {})
        logger.warning(
            f"[CSP] violation: directive={report.get('violated-directive')} "
            f"uri={report.get('document-uri')} "
            f"source={report.get('source-file')}:{report.get('line-number')} "
            f"blocked={report.get('blocked-uri')}"
        )
    except Exception:
        pass
    return JSONResponse(status_code=204, content=None)


# ─────────────────────────────────────────────────────────────
# Installer — call once at app startup
# ─────────────────────────────────────────────────────────────

def install_pro_security(app: FastAPI, redis_client=None) -> None:
    """Install all Pro security middleware + endpoints on the FastAPI app.

    Order matters: rate-limit runs FIRST (so attackers get 429 before
    wasting CPU on CSRF checks), then CSRF, then security headers
    (applied to the final response).
    """
    # Rate limit (outermost — runs first on request, last on response)
    app.add_middleware(ProRateLimitMiddleware, redis_client=redis_client)
    # CSRF (after rate limit)
    app.add_middleware(ProCSRFMiddleware)
    # Security headers (innermost — applied to response last)
    app.add_middleware(ProSecurityHeadersMiddleware)

    # CSP report endpoint
    app.add_api_route("/csp-report", csp_report_handler, methods=["POST"])

    logger.info(
        f"[ProSecurity] Installed: rate-limit + CSRF + security headers "
        f"(CSP enforce={CSP_ENFORCE})"
    )
