"""
pro_digests.py — SFAAM NEWS PRO 1 — Email digests
===================================================

Personalized daily/weekly email digests. Each subscriber picks:
  - frequency: daily | weekly | breaking-only
  - regions: ["us", "pk", "world"]
  - topics: ["us-china-trade-war", ...]

The digest scheduler runs daily at 6 AM local time and weekly on
Monday 6 AM. For each due subscriber, it:
  1. Pulls the top N articles matching their interests from the
     last 24h (daily) or 7d (weekly).
  2. Renders an HTML email using a template.
  3. Sends via SMTP (or prints to console if SMTP not configured).
  4. Records last_sent_at.

Endpoints:
  POST /api/digest/subscribe          — subscribe (sends confirmation email)
  GET  /api/digest/confirm/{token}    — double-opt-in confirmation
  GET  /api/digest/unsubscribe/{token} — one-click unsubscribe (RFC 8058)
  POST /api/digest/send-now           — admin: trigger digest send now

Environment:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM
"""
from __future__ import annotations

import os
import smtplib
import secrets
import logging
import asyncio
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends, Query
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select, text, desc, or_
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

SITE_URL = os.getenv("SITE_URL", "https://sfaamnews.com").rstrip("/")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "SFAAM NEWS <digest@sfaamnews.com>")


# ─────────────────────────────────────────────────────────────
# Subscribe / confirm / unsubscribe
# ─────────────────────────────────────────────────────────────

async def subscribe(request: Request, data: dict, db: AsyncSession) -> JSONResponse:
    """Subscribe to a digest. Sends a confirmation email (double opt-in)."""
    from pro_models import ProDigestSubscriber
    import re

    email = (data.get("email") or "").strip().lower()
    if not email or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "Invalid email")

    frequency = (data.get("frequency") or "daily").strip().lower()
    if frequency not in ("daily", "weekly", "breaking-only"):
        raise HTTPException(400, "frequency must be daily | weekly | breaking-only")

    regions = data.get("regions") or []
    topics = data.get("topics") or []
    if not isinstance(regions, list) or not isinstance(topics, list):
        raise HTTPException(400, "regions and topics must be arrays")

    # Upsert
    existing = (await db.execute(
        select(ProDigestSubscriber).where(ProDigestSubscriber.email == email)
    )).scalar_one_or_none()

    confirm_token = secrets.token_urlsafe(32)
    unsub_token = secrets.token_urlsafe(32)

    if existing:
        if existing.status == "confirmed":
            return {"ok": True, "message": "Already subscribed.", "already_subscribed": True}
        existing.frequency = frequency
        existing.regions = regions
        existing.topics = topics
        existing.confirm_token = confirm_token
        existing.unsub_token = unsub_token
        existing.status = "pending"
    else:
        db.add(ProDigestSubscriber(
            email=email,
            frequency=frequency,
            regions=regions,
            topics=topics,
            status="pending",
            confirm_token=confirm_token,
            unsub_token=unsub_token,
        ))
    await db.commit()

    # Send confirmation email (async, don't block the response)
    asyncio.create_task(_send_confirmation_email(email, confirm_token))

    return {
        "ok": True,
        "message": "Confirmation email sent. Check your inbox to confirm.",
    }


async def confirm(token: str, db: AsyncSession) -> HTMLResponse:
    """Confirm a digest subscription (double opt-in)."""
    from pro_models import ProDigestSubscriber
    sub = (await db.execute(
        select(ProDigestSubscriber).where(ProDigestSubscriber.confirm_token == token)
    )).scalar_one_or_none()
    if not sub:
        return HTMLResponse(content="<h1>Invalid or expired confirmation link.</h1>", status_code=400)
    sub.status = "confirmed"
    sub.confirm_token = None
    await db.commit()
    return HTMLResponse(content=f"""
<!doctype html><html><head><title>Subscribed — SFAAM NEWS</title></head>
<body style="font-family:Inter,sans-serif;max-width:600px;margin:60px auto;padding:24px;text-align:center;">
  <h1 style="color:#CA6D4C;">✅ You're subscribed!</h1>
  <p>You'll now receive the SFAAM NEWS {sub.frequency} digest at <strong>{sub.email}</strong>.</p>
  <p><a href="{SITE_URL}/" style="color:#CA6D4C;">← Back to SFAAM NEWS</a></p>
</body></html>
    """)


async def unsubscribe(token: str, db: AsyncSession) -> HTMLResponse:
    """One-click unsubscribe (RFC 8058 List-Unsubscribe-Post)."""
    from pro_models import ProDigestSubscriber
    sub = (await db.execute(
        select(ProDigestSubscriber).where(ProDigestSubscriber.unsub_token == token)
    )).scalar_one_or_none()
    if not sub:
        return HTMLResponse(content="<h1>Invalid unsubscribe link.</h1>", status_code=400)
    sub.status = "unsubscribed"
    await db.commit()
    return HTMLResponse(content=f"""
<!doctype html><html><head><title>Unsubscribed — SFAAM NEWS</title></head>
<body style="font-family:Inter,sans-serif;max-width:600px;margin:60px auto;padding:24px;text-align:center;">
  <h1 style="color:#CA6D4C;">You've been unsubscribed.</h1>
  <p>{sub.email} will no longer receive SFAAM NEWS digests.</p>
  <p>We're sorry to see you go. You can <a href="{SITE_URL}/" style="color:#CA6D4C;">resubscribe</a> anytime.</p>
</body></html>
    """)


# ─────────────────────────────────────────────────────────────
# Email rendering + sending
# ─────────────────────────────────────────────────────────────

async def _send_confirmation_email(email: str, token: str) -> None:
    """Send the double-opt-in confirmation email."""
    confirm_url = f"{SITE_URL}/api/digest/confirm/{token}"
    html = f"""
<!doctype html><html><body style="font-family:Inter,sans-serif;max-width:600px;margin:0 auto;padding:24px;">
  <h1 style="color:#CA6D4C;">Confirm your SFAAM NEWS subscription</h1>
  <p>You're one click away from getting the world's most important news in your inbox.</p>
  <p style="margin:32px 0;">
    <a href="{confirm_url}" style="background:#CA6D4C;color:#fff;padding:14px 28px;border-radius:8px;text-decoration:none;font-weight:700;">
      Confirm my subscription
    </a>
  </p>
  <p style="font-size:12px;color:#888;">If you didn't subscribe, you can safely ignore this email.</p>
  <hr style="margin:32px 0;border:none;border-top:1px solid #eee;">
  <p style="font-size:12px;color:#888;">SFAAM NEWS · https://sfaamnews.com</p>
</body></html>
    """
    await _send_email(email, "Confirm your SFAAM NEWS subscription", html)


async def _send_email(to_email: str, subject: str, html_body: str) -> bool:
    """Send an email via SMTP. Returns True on success.

    If SMTP_HOST is not configured, logs the email to the console
    (useful for local development).
    """
    if not SMTP_HOST:
        logger.info(f"[ProDigest] (no SMTP) To: {to_email} | Subject: {subject}")
        logger.info(f"[ProDigest] HTML body:\n{html_body[:500]}...")
        return True

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = SMTP_FROM
        msg["To"] = to_email
        msg["Subject"] = subject
        # List-Unsubscribe headers for one-click unsubscribe (RFC 8058)
        unsub_url = f"{SITE_URL}/api/digest/unsubscribe/{secrets.token_urlsafe(16)}"
        msg["List-Unsubscribe"] = f"<{unsub_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        msg.attach(MIMEText(html_body, "html"))

        def _send_sync():
            if SMTP_PORT == 465:
                server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
            else:
                server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
                server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())
            server.quit()

        await asyncio.to_thread(_send_sync)
        return True
    except Exception as e:
        logger.warning(f"[ProDigest] Email send failed to {to_email}: {e}")
        return False


async def _render_digest_html(sub, articles: list) -> str:
    """Render the personalized digest HTML."""
    if not articles:
        # Don't send an empty digest
        return ""

    today = datetime.utcnow().strftime("%B %d, %Y")
    articles_html = ""
    for i, a in enumerate(articles[:10], 1):
        img_html = f'<img src="{a.image_url}" alt="" style="width:100%;max-width:120px;border-radius:8px;float:right;margin-left:16px;">' if a.image_url else ""
        articles_html += f"""
        <div style="margin-bottom:32px;padding-bottom:24px;border-bottom:1px solid #eee;overflow:hidden;">
          {img_html}
          <h3 style="margin:0 0 8px;font-size:18px;"><a href="{SITE_URL}/article/{a.slug}" style="color:#1a1a1a;text-decoration:none;">{a.title}</a></h3>
          <p style="margin:0 0 8px;font-size:14px;color:#555;">{(a.summary or '')[:200]}...</p>
          <p style="font-size:12px;color:#888;">{a.region.upper()} · {a.date.strftime('%b %d, %Y') if a.date else ''} · {(a.views or 0):,} views</p>
        </div>
        """

    unsub_url = f"{SITE_URL}/api/digest/unsubscribe/{sub.unsub_token}"
    return f"""
<!doctype html><html><body style="font-family:Inter,-apple-system,sans-serif;background:#f5f3ef;margin:0;padding:24px;">
  <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;">
    <div style="background:#CA6D4C;padding:24px 32px;color:#fff;">
      <h1 style="margin:0;font-size:24px;">SFAAM NEWS Digest</h1>
      <p style="margin:4px 0 0;opacity:0.9;font-size:14px;">{today} · {sub.frequency.upper()}</p>
    </div>
    <div style="padding:32px;">
      <p style="font-size:14px;color:#666;margin:0 0 24px;">Your personalized selection of the day's most important stories.</p>
      {articles_html}
      <div style="margin-top:32px;padding:20px;background:#f5f3ef;border-radius:8px;text-align:center;">
        <p style="margin:0;font-size:14px;color:#555;">Want more? Visit <a href="{SITE_URL}/" style="color:#CA6D4C;">sfaamnews.com</a></p>
      </div>
      <p style="font-size:11px;color:#888;margin-top:24px;text-align:center;">
        You're receiving this because you subscribed to SFAAM NEWS digests.
        <br><a href="{unsub_url}" style="color:#888;">Unsubscribe</a> · <a href="{SITE_URL}/privacy.html" style="color:#888;">Privacy</a>
      </p>
    </div>
  </div>
</body></html>
    """


# ─────────────────────────────────────────────────────────────
# Digest scheduler — called by APScheduler daily at 6 AM
# ─────────────────────────────────────────────────────────────

async def run_digest_send(frequency: str = "daily") -> dict:
    """Pull all due subscribers and send their digests.

    Called by the scheduler. Also callable via admin endpoint for
    manual triggers.
    """
    from pro_models import ProDigestSubscriber
    from database import Article, AsyncSessionLocal

    sent_count = 0
    skipped_count = 0
    failed_count = 0

    async with AsyncSessionLocal() as db:
        # Find all confirmed subs due for this frequency
        cutoff_hours = 24 if frequency == "daily" else 168  # 7 days
        cutoff = datetime.utcnow() - timedelta(hours=cutoff_hours - 1)
        subs = (await db.execute(
            select(ProDigestSubscriber).where(
                ProDigestSubscriber.status == "confirmed",
                ProDigestSubscriber.frequency == frequency,
                or_(
                    ProDigestSubscriber.last_sent_at == None,  # noqa: E711
                    ProDigestSubscriber.last_sent_at < cutoff,
                ),
            ).limit(1000)
        )).scalars().all()

        for sub in subs:
            try:
                # Pull articles matching their interests
                articles = await _fetch_articles_for_subscriber(sub, db, hours=cutoff_hours)
                if not articles:
                    skipped_count += 1
                    continue
                html = await _render_digest_html(sub, articles)
                if not html:
                    skipped_count += 1
                    continue
                subject = f"SFAAM NEWS {frequency.title()} Digest — {datetime.utcnow().strftime('%b %d')}"
                ok = await _send_email(sub.email, subject, html)
                if ok:
                    sub.last_sent_at = datetime.utcnow()
                    sent_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                logger.warning(f"[ProDigest] Failed for {sub.email}: {e}")
                failed_count += 1

        await db.commit()

    logger.info(f"[ProDigest] frequency={frequency} sent={sent_count} skipped={skipped_count} failed={failed_count}")
    return {"frequency": frequency, "sent": sent_count, "skipped": skipped_count, "failed": failed_count}


async def _fetch_articles_for_subscriber(sub, db: AsyncSession, hours: int) -> list:
    """Pull the top N articles matching the subscriber's interests."""
    from database import Article
    cutoff = datetime.utcnow() - timedelta(hours=hours)

    query = select(Article).where(
        or_(Article.status == "published", Article.status == None),  # noqa: E711
        Article.date > cutoff,
    )

    # Region filter
    if sub.regions:
        query = query.where(Article.region.in_(sub.regions))

    query = query.order_by(desc(Article.views)).limit(20)
    rows = (await db.execute(query)).scalars().all()

    # If subscriber has topic interests, boost articles in those topics
    if sub.topics and rows:
        from pro_models import ProArticleTopic
        topic_article_ids = set()
        for topic_slug in sub.topics[:10]:
            try:
                topic_rows = (await db.execute(text("""
                    SELECT pat.article_id FROM pro_article_topics pat
                    JOIN pro_topics t ON t.id = pat.topic_id
                    WHERE t.slug = :slug
                """), {"slug": topic_slug})).fetchall()
                topic_article_ids.update(r[0] for r in topic_rows)
            except Exception:
                pass
        # Sort: topic-matched first, then by views
        rows.sort(key=lambda a: (
            a.id in topic_article_ids,
            a.views or 0,
        ), reverse=True)

    return rows[:10]


# ─────────────────────────────────────────────────────────────
# Registrar
# ─────────────────────────────────────────────────────────────

def register_pro_digest_routes(app: FastAPI, get_db, admin_guard) -> None:
    @app.post("/api/digest/subscribe")
    async def _sub(request: Request, data: dict, db=Depends(get_db)):
        return await subscribe(request, data, db)

    @app.get("/api/digest/confirm/{token}")
    async def _confirm(token: str, db=Depends(get_db)):
        return await confirm(token, db)

    @app.get("/api/digest/unsubscribe/{token}")
    async def _unsub(token: str, db=Depends(get_db)):
        return await unsubscribe(token, db)

    @app.post("/api/digest/send-now")
    @admin_guard
    async def _send_now(request: Request, frequency: str = "daily"):
        return await run_digest_send(frequency)

    logger.info("[ProDigests] Routes registered: subscribe, confirm, unsubscribe, send-now")
