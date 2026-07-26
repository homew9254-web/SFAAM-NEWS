"""
pro_push.py — SFAAM NEWS PRO 1 — Web Push notifications
========================================================

Real VAPID-based Web Push (RFC 8291 + RFC 8292). Works on Chrome,
Firefox, Edge, and Android Chrome. Safari iOS 16.4+ supports Web
Push too. Desktop Safari 16+ also supports it.

Setup:
  1. Generate VAPID keys once:
       python pro_push.py --generate-keys
  2. Set env vars:
       PRO_VAPID_PUBLIC_KEY=...
       PRO_VAPID_PRIVATE_KEY=...
       PRO_VAPID_SUBJECT=mailto:editor@sfaamnews.com
  3. The frontend calls /api/push/vapid-public to get the public key.
  4. The frontend calls pushManager.subscribe({applicationServerKey})
     and POSTs the subscription to /api/push/subscribe.
  5. To send a push: call send_push(subscription, payload) from
     the breaking-news pipeline.

Endpoints:
  GET  /api/push/vapid-public        — return public key
  POST /api/push/subscribe           — register a subscription
  POST /api/push/unsubscribe         — remove a subscription
  POST /api/push/test                — admin-only test push
  POST /api/push/breaking/{article_id} — admin-only breaking-news push to all
"""
from __future__ import annotations

import os
import json
import base64
import logging
import asyncio
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# VAPID key management
# ─────────────────────────────────────────────────────────────

# VAPID keys are P-256 elliptic-curve keypairs. We use the `pywebpush`
# library if installed; otherwise we provide a no-op fallback that
# returns graceful errors so the site doesn't crash if push is not
# configured.

VAPID_PUBLIC_KEY = os.getenv("PRO_VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("PRO_VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.getenv("PRO_VAPID_SUBJECT", "mailto:editor@sfaamnews.com")


def _pywebpush_available() -> bool:
    try:
        import pywebpush  # noqa: F401
        return True
    except ImportError:
        return False


def generate_vapid_keys() -> tuple[str, str]:
    """Generate a fresh VAPID keypair. Run once and store in env.

    Returns (public_key_b64, private_key_b64).
    """
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        raise RuntimeError(
            "cryptography library is required to generate VAPID keys. "
            "pip install cryptography"
        )

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()

    pub_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    priv_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PKCS8,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    pub_b64 = base64.urlsafe_b64encode(pub_bytes).decode().rstrip("=")
    priv_b64 = base64.urlsafe_b64encode(priv_bytes).decode().rstrip("=")
    return pub_b64, priv_b64


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────

async def get_vapid_public(request: Request):
    """Return the VAPID public key as raw Base64-URL (no padding).

    The frontend uses this as the applicationServerKey argument to
    pushManager.subscribe().
    """
    if not VAPID_PUBLIC_KEY:
        return {"enabled": False, "public_key": None}
    return {"enabled": True, "public_key": VAPID_PUBLIC_KEY}


async def subscribe(request: Request, data: dict, db: AsyncSession):
    """Register a Web Push subscription.

    Body: {endpoint, keys: {p256dh, auth}, topics: [...], region: "..."}.
    """
    from pro_models import ProPushSubscription

    endpoint = (data.get("endpoint") or "").strip()
    keys = data.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    auth = (keys.get("auth") or "").strip()

    if not endpoint or not p256dh or not auth:
        raise HTTPException(400, "Missing endpoint or keys")

    if len(endpoint) > 1000 or len(p256dh) > 300 or len(auth) > 200:
        raise HTTPException(400, "Payload too large")

    fp = (data.get("fingerprint") or request.headers.get("x-reader-fp") or "anon")[:100]
    topics = data.get("topics") or []
    region = (data.get("region") or "").strip()[:50]

    # Upsert: subscriptions are unique by endpoint
    existing = (await db.execute(
        select(ProPushSubscription).where(ProPushSubscription.endpoint == endpoint)
    )).scalar_one_or_none()

    if existing:
        existing.fingerprint = fp
        existing.p256dh = p256dh
        existing.auth = auth
        existing.topics = topics
        existing.region = region
        existing.is_active = True
    else:
        db.add(ProPushSubscription(
            fingerprint=fp,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            topics=topics,
            region=region,
            is_active=True,
        ))
    await db.commit()
    return {"ok": True, "message": "Subscribed to push notifications"}


async def unsubscribe(request: Request, data: dict, db: AsyncSession):
    """Mark a push subscription as inactive."""
    from pro_models import ProPushSubscription
    endpoint = (data.get("endpoint") or "").strip()
    if not endpoint:
        raise HTTPException(400, "Missing endpoint")
    await db.execute(
        text("UPDATE pro_push_subscriptions SET is_active = 0 WHERE endpoint = :e"),
        {"e": endpoint},
    )
    await db.commit()
    return {"ok": True}


# ─────────────────────────────────────────────────────────────
# Sending pushes (called from breaking-news pipeline, not an endpoint)
# ─────────────────────────────────────────────────────────────

async def send_push_to_subscription(subscription, payload: dict) -> bool:
    """Send a single push notification. Returns True on success."""
    if not _pywebpush_available() or not VAPID_PRIVATE_KEY:
        logger.debug("[ProPush] pywebpush not installed or VAPID not configured — skipping")
        return False

    try:
        from pywebpush import webpush, WebPushException

        sub_info = {
            "endpoint": subscription.endpoint,
            "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
        }
        # Run in thread — pywebpush is sync
        def _send():
            return webpush(
                subscription_info=sub_info,
                data=json.dumps(payload),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=3600,
            )
        await asyncio.to_thread(_send)
        return True
    except Exception as e:
        logger.warning(f"[ProPush] send failed to {subscription.endpoint[:60]}...: {e}")
        return False


async def broadcast_breaking(article, db: AsyncSession) -> dict:
    """Send a breaking-news push to ALL active subscribers.

    Called from main.py when an article is published with the
    'breaking' flag, or from the admin pipeline.
    """
    from pro_models import ProPushSubscription

    subs = (await db.execute(
        select(ProPushSubscription).where(ProPushSubscription.is_active == True)  # noqa: E712
    )).scalars().all()

    if not subs:
        return {"sent": 0, "failed": 0, "total": 0}

    payload = {
        "title": "🚨 Breaking: " + (article.title or "")[:100],
        "body": (article.summary or "")[:200],
        "url": f"/article/{article.slug}" if article.slug else f"/article/{article.id}",
        "tag": "breaking",
        "icon": "/static/logo.png",
        "badge": "/static/logo.png",
        "data": {"article_id": article.id},
        "actions": [
            {"action": "read", "title": "Read now"},
            {"action": "dismiss", "title": "Not now"},
        ],
    }

    sent = 0
    failed = 0
    # Send in batches of 50 to avoid overwhelming the event loop
    BATCH = 50
    for i in range(0, len(subs), BATCH):
        batch = subs[i:i + BATCH]
        results = await asyncio.gather(*[send_push_to_subscription(s, payload) for s in batch])
        sent += sum(1 for r in results if r)
        failed += sum(1 for r in results if not r)
    return {"sent": sent, "failed": failed, "total": len(subs)}


# ─────────────────────────────────────────────────────────────
# Admin endpoints
# ─────────────────────────────────────────────────────────────

async def admin_test_push(request: Request, db: AsyncSession):
    """Admin-only: send a test push to the requester's own subscription."""
    from pro_models import ProPushSubscription
    # Require admin auth — caller (main.py) wires this through the router
    fp = request.headers.get("x-reader-fp", "")
    if not fp:
        raise HTTPException(400, "Missing reader fingerprint header")
    sub = (await db.execute(
        select(ProPushSubscription).where(
            ProPushSubscription.fingerprint == fp,
            ProPushSubscription.is_active == True,  # noqa: E712
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(404, "No active push subscription for this fingerprint")
    ok = await send_push_to_subscription(sub, {
        "title": "✅ SFAAM Push Test",
        "body": "If you see this, push notifications are working.",
        "url": "/",
        "tag": "test",
    })
    return {"ok": ok}


async def admin_breaking_push(article_id: int, request: Request, db: AsyncSession):
    """Admin-only: broadcast a breaking-news push for an article."""
    from database import Article
    article = (await db.execute(
        select(Article).where(Article.id == article_id)
    )).scalar_one_or_none()
    if not article:
        raise HTTPException(404, "Article not found")
    result = await broadcast_breaking(article, db)
    return result


# ─────────────────────────────────────────────────────────────
# Registrar
# ─────────────────────────────────────────────────────────────

def register_pro_push_routes(app: FastAPI, get_db, admin_guard) -> None:
    """Register push routes. `admin_guard` is a callable that wraps
    a route to require admin auth (typically `_require_admin` from main.py).
    """
    @app.get("/api/push/vapid-public")
    async def _vapid(request: Request):
        return await get_vapid_public(request)

    @app.post("/api/push/subscribe")
    async def _sub(request: Request, data: dict, db=Depends(get_db)):
        return await subscribe(request, data, db)

    @app.post("/api/push/unsubscribe")
    async def _unsub(request: Request, data: dict, db=Depends(get_db)):
        return await unsubscribe(request, data, db)

    @app.post("/api/push/test")
    @admin_guard
    async def _test(request: Request, db=Depends(get_db)):
        return await admin_test_push(request, db)

    @app.post("/api/push/breaking/{article_id}")
    @admin_guard
    async def _breaking(article_id: int, request: Request, db=Depends(get_db)):
        return await admin_breaking_push(article_id, request, db)

    logger.info(f"[ProPush] Routes registered (VAPID configured={bool(VAPID_PUBLIC_KEY)})")


if __name__ == "__main__":
    import sys
    if "--generate-keys" in sys.argv:
        pub, priv = generate_vapid_keys()
        print("# Add these to your .env:")
        print(f'PRO_VAPID_PUBLIC_KEY="{pub}"')
        print(f'PRO_VAPID_PRIVATE_KEY="{priv}"')
        print(f'PRO_VAPID_SUBJECT="mailto:editor@sfaamnews.com"')
        sys.exit(0)
    print("Use --generate-keys to create VAPID keys")
