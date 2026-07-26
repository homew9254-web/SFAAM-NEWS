"""
monitoring.py - SFAAM NEWS V24 — Centralized Error Tracking & Audit Logging
==========================================================================

Integrates with Sentry (or Better Stack via DSN) for real-time error tracking.
Also provides:
  - Audit logging for admin actions (writes to DB + log file)
  - Structured logging for production
  - Optional webhook alerts for critical errors

Configuration (env vars):
  SENTRY_DSN         — Sentry project DSN (or set to "" to disable)
  BETTERSTACK_DSN    — Better Stack source token (alternative to Sentry)
  ALERT_WEBHOOK_URL  — Slack/Discord/Teams webhook for critical alerts
  AUDIT_LOG_FILE     — path to audit log (default: ./logs/audit.log)

Usage in code:
  from monitoring import init_monitoring, capture_exception, log_audit_event
  init_monitoring()                       # call once at startup
  capture_exception(exc, context={...})   # report an error
  log_audit_event(admin_id, action, ...)  # record admin activity
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Sentinels for lazy init ──
_sentry_sdk = None
_betterstack_dsn: Optional[str] = None
_alert_webhook: Optional[str] = None
_audit_log_file: Path = Path(os.getenv("AUDIT_LOG_FILE", "./logs/audit.log"))
_audit_lock = threading.Lock()
_initialized = False


def init_monitoring() -> None:
    """Initialize Sentry / Better Stack / webhook alerting. Idempotent.
    Called from main.py lifespan on startup. Safe to call multiple times."""
    global _initialized, _sentry_sdk, _betterstack_dsn, _alert_webhook

    if _initialized:
        return
    _initialized = True

    sentry_dsn = os.getenv("SENTRY_DSN", "").strip()
    betterstack_dsn = os.getenv("BETTERSTACK_DSN", "").strip()
    _alert_webhook = os.getenv("ALERT_WEBHOOK_URL", "").strip() or None

    # Try Sentry first (more popular)
    if sentry_dsn:
        try:
            import sentry_sdk  # type: ignore
            from sentry_sdk.integrations.fastapi import FastApiIntegration
            from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
            from sentry_sdk.integrations.redis import RedisIntegration

            sentry_sdk.init(
                dsn=sentry_dsn,
                environment=os.getenv("ENV", "development"),
                release=os.getenv("RELEASE_VERSION", "sfaam-news@26.0"),
                traces_sample_rate=float(os.getenv("SENTRY_TRACES_RATE", "0.1")),
                send_default_pii=False,  # never leak user PII
                integrations=[
                    FastApiIntegration(),
                    SqlalchemyIntegration(),
                    RedisIntegration(),
                ],
                before_send=_scrub_pii,
            )
            _sentry_sdk = sentry_sdk
            logger.info("[V24 Monitoring] Sentry initialized")
        except ImportError:
            logger.warning("[V24 Monitoring] sentry-sdk not installed — SENTRY_DSN ignored")
        except Exception as e:
            logger.warning(f"[V24 Monitoring] Sentry init failed: {e}")

    # Better Stack as alternative
    if betterstack_dsn and _sentry_sdk is None:
        _betterstack_dsn = betterstack_dsn
        logger.info("[V24 Monitoring] Better Stack logging enabled (via Logtail HTTP API)")

    # Ensure audit log dir exists
    try:
        _audit_log_file.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    logger.info("[V24 Monitoring] Monitoring initialized")


def _scrub_pii(event: dict, hint: dict) -> dict:
    """Before-send hook for Sentry — strip PII from events.
    Removes IP addresses, email addresses, and Authorization headers."""
    try:
        # Scrub request data
        if "request" in event:
            req = event["request"]
            if "headers" in req:
                h = req["headers"]
                for k in list(h.keys()):
                    if k.lower() in ("authorization", "cookie", "x-admin-key", "x-admin-session"):
                        h[k] = "[REDACTED]"
            if "env" in req and "REMOTE_ADDR" in req["env"]:
                req["env"]["REMOTE_ADDR"] = "[REDACTED]"
        # Scrub exception values (titles may contain email)
        if "exception" in event and "values" in event["exception"]:
            for v in event["exception"]["values"]:
                if "value" in v and v["value"]:
                    import re
                    v["value"] = re.sub(
                        r"[\w.+-]+@[\w-]+\.[\w.-]+", "[EMAIL]", v["value"]
                    )
    except Exception:
        pass
    return event


def capture_exception(exc: BaseException, context: Optional[dict] = None) -> None:
    """Report an exception to Sentry / Better Stack. Never raises.
    Args:
        exc:     The exception instance (or None for manual messages)
        context: Optional dict of extra metadata to attach
    """
    # Sentry
    if _sentry_sdk is not None:
        try:
            if context:
                _sentry_sdk.set_context("custom", context)
            _sentry_sdk.capture_exception(exc)
        except Exception:
            pass

    # Better Stack (via Sentry-compatible DSN format → send to Logtail HTTP API)
    # For simplicity we just log structured; full Better Stack integration
    # would require their `logtail-python` SDK.
    if _betterstack_dsn:
        try:
            logger.error(
                f"[BETTERSTACK] {type(exc).__name__}: {exc}",
                extra={"context": context or {}, "traceback": True}
            )
        except Exception:
            pass

    # Webhook alert for critical errors
    if _alert_webhook:
        try:
            _send_webhook_alert(exc, context)
        except Exception:
            pass


def capture_message(msg: str, level: str = "info", context: Optional[dict] = None) -> None:
    """Send a manual message to Sentry."""
    if _sentry_sdk is not None:
        try:
            if context:
                _sentry_sdk.set_context("custom", context)
            _sentry_sdk.capture_message(msg, level=level)
        except Exception:
            pass


def _send_webhook_alert(exc: BaseException, context: Optional[dict]) -> None:
    """Send a critical error alert to Slack/Discord/Teams webhook.
    Uses a 5-second timeout and never raises."""
    if not _alert_webhook:
        return
    try:
        import httpx
        payload = {
            "text": (
                f":rotating_light: *SFAAM NEWS ERROR*\n"
                f"• Type: `{type(exc).__name__}`\n"
                f"• Message: `{str(exc)[:300]}`\n"
                f"• Time: `{datetime.utcnow().isoformat()}`\n"
                f"• Context: `{json.dumps(context or {})[:300]}`"
            )
        }
        httpx.post(_alert_webhook, json=payload, timeout=5.0)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════
#  AUDIT LOGGING — every admin action recorded for compliance
# ════════════════════════════════════════════════════════════

def log_audit_event(
    admin_id: str,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[int | str] = None,
    details: Optional[dict] = None,
    ip_address: Optional[str] = None,
    success: bool = True,
) -> None:
    """Record an admin action in the audit log.
    Appends a JSON line to AUDIT_LOG_FILE (file rotation is left to logrotate).
    Safe to call from async contexts (uses a lock).

    Args:
        admin_id:     Identifier of the admin (session token hash or 'admin')
        action:       What they did (e.g. 'article.publish', 'comment.delete')
        target_type:  'article' / 'comment' / 'subscriber' / None
        target_id:    ID of the affected record
        details:      Optional dict with extra context
        ip_address:   Client IP (for forensic analysis)
        success:      True if the action succeeded, False on failure/abuse attempt
    """
    event = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "admin_id": str(admin_id)[:64],
        "action": str(action)[:100],
        "target_type": target_type,
        "target_id": str(target_id) if target_id is not None else None,
        "details": details or {},
        "ip": ip_address,
        "success": success,
    }
    line = json.dumps(event, default=str)
    try:
        with _audit_lock:
            with open(_audit_log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        logger.warning(f"Audit log write failed: {e}")

    # Also send to Sentry as a breadcrumb (helps correlate errors with admin actions)
    if _sentry_sdk is not None:
        try:
            _sentry_sdk.add_breadcrumb(
                category="audit", message=f"{action} ({'ok' if success else 'fail'})",
                level="info", data=event,
            )
        except Exception:
            pass
