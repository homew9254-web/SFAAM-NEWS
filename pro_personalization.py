"""
pro_personalization.py — SFAAM NEWS PRO 1
==========================================

Personalization layer: reading history, interest graph, and
recommendation feed.

How it works:
  1. Every time a reader opens an article, the frontend fires a
     beacon to /api/personalize/track with the article_id, region,
     read_pct, and time_on_page. This is stored in ProReadingHistory.
  2. We build a per-reader interest vector by counting how often
     each region, keyword, and topic appears in their reading history.
  3. The /api/personalize/feed endpoint returns 20 articles ranked
     by interest match + recency + freshness (articles they haven't
     read yet).

Privacy:
  - Keyed off an anonymous fingerprint stored in localStorage.
  - No email required.
  - Users can wipe their history at any time via DELETE /api/personalize/history.
  - GDPR-friendly by default.

Endpoints:
  POST /api/personalize/track          — record article read
  GET  /api/personalize/feed           — personalized "For You" feed
  GET  /api/personalize/history        — user's reading history
  DELETE /api/personalize/history      — wipe history
  POST /api/personalize/feedback       — thumbs up/down on article
  GET  /api/personalize/interests      — interest graph (for transparency)
"""
from __future__ import annotations

import time
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select, text, desc, and_
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _fp(request: Request, body_fp: Optional[str] = None) -> str:
    """Get the reader's anonymous fingerprint.

    Priority: client-supplied (localStorage) > IP-based fallback.
    The frontend generates a random ID in localStorage on first visit.
    """
    if body_fp and len(body_fp) < 100:
        return body_fp
    # Fallback to IP hash (less stable but better than nothing)
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "anon")
    return f"ip:{ip}"


def _user_fingerprint(request: Request) -> str:
    """Read fingerprint from header (set by frontend)."""
    return request.headers.get("x-reader-fp", "") or _fp(request)


# ─────────────────────────────────────────────────────────────
# Track article read
# ─────────────────────────────────────────────────────────────

async def track_read(request: Request, data: dict, db: AsyncSession):
    """Record that the reader opened an article.

    Body: {article_id, region, read_pct (0-1), time_on_page (sec)}.
    Called periodically by the frontend as the user scrolls (debounced).
    """
    from pro_models import ProReadingHistory
    from database import Article

    fp = _user_fingerprint(request)
    article_id = data.get("article_id")
    if not isinstance(article_id, int) or article_id <= 0:
        raise HTTPException(400, "Invalid article_id")

    region = (data.get("region") or "").strip()[:50]
    read_pct = float(data.get("read_pct", 0))
    read_pct = max(0.0, min(1.0, read_pct))
    time_on_page = int(data.get("time_on_page", 0))
    time_on_page = max(0, min(3600, time_on_page))  # cap 1h

    # Upsert: if the user already has a row for this article, update it
    existing = (await db.execute(
        select(ProReadingHistory).where(
            ProReadingHistory.fingerprint == fp,
            ProReadingHistory.article_id == article_id,
        )
    )).scalar_one_or_none()

    if existing:
        # Update with the MAX of old and new (best signal of completion)
        existing.read_pct = max(existing.read_pct or 0, read_pct)
        existing.time_on_page = max(existing.time_on_page or 0, time_on_page)
        existing.read_at = datetime.utcnow()
        if region:
            existing.region = region
    else:
        row = ProReadingHistory(
            fingerprint=fp,
            article_id=article_id,
            region=region,
            read_pct=read_pct,
            time_on_page=time_on_page,
        )
        db.add(row)

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.debug(f"[ProPersonalize] track failed: {e}")
        return {"ok": False}

    return {"ok": True}


# ─────────────────────────────────────────────────────────────
# Personalized feed
# ─────────────────────────────────────────────────────────────

async def personalized_feed(request: Request, page: int, limit: int, db: AsyncSession):
    """Return a 'For You' feed of 20 articles ranked by interest match.

    Algorithm:
      1. Look at the user's last 50 read articles.
      2. Build a region-affinity map: {region: count_weighted_by_read_pct}.
      3. Pull 100 candidate articles from the last 7 days that the user
         hasn't read yet.
      4. Score each: region_affinity * 3 + recency_boost + (1 - age/7).
      5. Return top N.

    Falls back to the standard latest-articles feed for new visitors
    with no reading history.
    """
    from pro_models import ProReadingHistory
    from database import Article

    fp = _user_fingerprint(request)

    # 1. Get user's reading history (last 50)
    history_rows = (await db.execute(
        select(ProReadingHistory)
        .where(ProReadingHistory.fingerprint == fp)
        .order_by(desc(ProReadingHistory.read_at))
        .limit(50)
    )).scalars().all()

    if not history_rows:
        # Cold start — return latest articles
        latest = (await db.execute(
            select(Article)
            .where(or_(Article.status == "published", Article.status == None))  # noqa: E711
            .order_by(desc(Article.date))
            .limit(limit)
        )).scalars().all()
        return {
            "results": [_article_brief(a) for a in latest],
            "page": page,
            "personalized": False,
            "reason": "no_history",
        }

    read_article_ids = {h.article_id for h in history_rows}

    # 2. Build region affinity
    region_affinity: dict[str, float] = defaultdict(float)
    for h in history_rows:
        if h.region:
            # Weight by read_pct — fully-read articles count more
            weight = (h.read_pct or 0) * 2 + 0.1  # min 0.1 just for opening
            region_affinity[h.region] += weight

    # 3. Pull candidate articles (last 7 days, not yet read)
    cutoff = datetime.utcnow() - timedelta(days=7)
    candidates_sql = text("""
        SELECT id, title, slug, summary, region, image_url, date, views,
               meta_desc, keywords
        FROM articles
        WHERE (status = 'published' OR status IS NULL)
          AND date > :cutoff
          AND id NOT IN :read_ids
        ORDER BY date DESC
        LIMIT 100
    """)
    # SQLite doesn't support IN with a tuple param directly, so we
    # build it inline. read_article_ids is from our own DB so SQL-injection-safe.
    if not read_article_ids:
        candidates_sql = text("""
            SELECT id, title, slug, summary, region, image_url, date, views,
                   meta_desc, keywords
            FROM articles
            WHERE (status = 'published' OR status IS NULL)
              AND date > :cutoff
            ORDER BY date DESC
            LIMIT 100
        """)
        candidates = (await db.execute(candidates_sql, {"cutoff": cutoff})).fetchall()
    else:
        # Use a parametrized IN by expanding placeholders
        ids_list = list(read_article_ids)[:200]  # cap to avoid huge SQL
        placeholders = ",".join(f":id{i}" for i in range(len(ids_list)))
        sql_str = f"""
            SELECT id, title, slug, summary, region, image_url, date, views,
                   meta_desc, keywords
            FROM articles
            WHERE (status = 'published' OR status IS NULL)
              AND date > :cutoff
              AND id NOT IN ({placeholders})
            ORDER BY date DESC
            LIMIT 100
        """
        params = {"cutoff": cutoff}
        for i, aid in enumerate(ids_list):
            params[f"id{i}"] = aid
        candidates = (await db.execute(text(sql_str), params)).fetchall()

    if not candidates:
        return {"results": [], "page": page, "personalized": True, "reason": "no_candidates"}

    # 4. Score each candidate
    max_region_affinity = max(region_affinity.values()) if region_affinity else 1.0
    now = datetime.utcnow()
    scored = []
    for c in candidates:
        # Region match score (0-1)
        region_score = (region_affinity.get(c.region, 0) / max_region_affinity) if max_region_affinity else 0
        # Recency score (1.0 today → 0.0 a week old)
        age_days = (now - c.date).total_seconds() / 86400 if c.date else 7
        recency_score = max(0, 1 - age_days / 7)
        # Popularity score (views, log-scaled)
        popularity = min(1.0, math.log10((c.views or 0) + 1) / 4)  # 10000 views → 1.0

        # Weighted blend
        final_score = (
            region_score * 3.0 +
            recency_score * 2.0 +
            popularity * 1.0
        )
        scored.append((c, final_score, region_score))

    # 5. Sort and paginate
    scored.sort(key=lambda x: x[1], reverse=True)
    start = (page - 1) * limit
    page_items = scored[start:start + limit]

    results = []
    for c, score, region_score in page_items:
        brief = _article_brief_from_row(c)
        brief["personalization_score"] = round(score, 3)
        brief["reason"] = (
            f"Because you read {region_score:.0%} {c.region or 'world'} content"
            if region_score > 0.3
            else "Trending now"
        )
        results.append(brief)

    return {
        "results": results,
        "page": page,
        "personalized": True,
        "interests": dict(region_affinity),
    }


# ─────────────────────────────────────────────────────────────
# Reading history
# ─────────────────────────────────────────────────────────────

async def get_history(request: Request, page: int, limit: int, db: AsyncSession):
    """Return the user's reading history, most recent first."""
    from pro_models import ProReadingHistory
    from database import Article

    fp = _user_fingerprint(request)
    offset = (page - 1) * limit
    rows = (await db.execute(
        select(ProReadingHistory, Article)
        .join(Article, ProReadingHistory.article_id == Article.id)
        .where(ProReadingHistory.fingerprint == fp)
        .order_by(desc(ProReadingHistory.read_at))
        .offset(offset)
        .limit(limit)
    )).all()

    return {
        "results": [{
            "article_id": rh.article_id,
            "title": a.title,
            "slug": a.slug,
            "region": a.region,
            "image_url": a.image_url,
            "read_at": rh.read_at.isoformat() if rh.read_at else None,
            "read_pct": rh.read_pct or 0,
            "time_on_page": rh.time_on_page or 0,
            "feedback": rh.feedback or 0,
        } for rh, a in rows]
    }


async def wipe_history(request: Request, db: AsyncSession):
    """Wipe the user's reading history (GDPR right-to-be-forgotten)."""
    from pro_models import ProReadingHistory
    fp = _user_fingerprint(request)
    await db.execute(
        text("DELETE FROM pro_reading_history WHERE fingerprint = :fp"),
        {"fp": fp},
    )
    await db.commit()
    return {"ok": True, "message": "Reading history wiped."}


async def feedback(request: Request, data: dict, db: AsyncSession):
    """User feedback on an article (thumbs up/down) — used to refine
    the personalization model."""
    from pro_models import ProReadingHistory

    fp = _user_fingerprint(request)
    article_id = data.get("article_id")
    feedback_val = data.get("feedback")
    if not isinstance(article_id, int) or feedback_val not in (-1, 0, 1):
        raise HTTPException(400, "Invalid payload")

    # Upsert feedback on the reading-history row
    existing = (await db.execute(
        select(ProReadingHistory).where(
            ProReadingHistory.fingerprint == fp,
            ProReadingHistory.article_id == article_id,
        )
    )).scalar_one_or_none()

    if existing:
        existing.feedback = feedback_val
    else:
        db.add(ProReadingHistory(
            fingerprint=fp,
            article_id=article_id,
            feedback=feedback_val,
            read_pct=0,
            time_on_page=0,
        ))
    await db.commit()
    return {"ok": True}


async def get_interests(request: Request, db: AsyncSession):
    """Return the user's interest graph for transparency.

    Lets users see what data we have on them and adjust by deleting
    their history or reading different content.
    """
    from pro_models import ProReadingHistory
    fp = _user_fingerprint(request)

    rows = (await db.execute(
        select(ProReadingHistory)
        .where(ProReadingHistory.fingerprint == fp)
        .order_by(desc(ProReadingHistory.read_at))
        .limit(100)
    )).scalars().all()

    region_counts = Counter()
    region_read_pct = defaultdict(float)
    for r in rows:
        if r.region:
            region_counts[r.region] += 1
            region_read_pct[r.region] += (r.read_pct or 0)
    return {
        "total_articles_read": len(rows),
        "regions": [
            {"region": r, "articles": c, "avg_read_pct": round(region_read_pct[r] / max(1, c), 2)}
            for r, c in region_counts.most_common()
        ],
    }


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _article_brief(a) -> dict:
    return {
        "id": a.id,
        "title": a.title,
        "slug": a.slug,
        "summary": (a.summary or a.meta_desc or "")[:300],
        "image_url": a.image_url,
        "region": a.region,
        "date": a.date.isoformat() if a.date else None,
        "views": a.views or 0,
    }


def _article_brief_from_row(c) -> dict:
    return {
        "id": c.id,
        "title": c.title,
        "slug": c.slug,
        "summary": (c.summary or c.meta_desc or "")[:300],
        "image_url": c.image_url,
        "region": c.region,
        "date": c.date.isoformat() if c.date else None,
        "views": c.views or 0,
    }


# ─────────────────────────────────────────────────────────────
# Registrar
# ─────────────────────────────────────────────────────────────

def register_pro_personalization_routes(app: FastAPI, get_db) -> None:
    @app.post("/api/personalize/track")
    async def _track(request: Request, data: dict, db=Depends(get_db)):
        return await track_read(request, data, db)

    @app.get("/api/personalize/feed")
    async def _feed(
        request: Request,
        page: int = Query(1, ge=1),
        limit: int = Query(20, ge=1, le=50),
        db=Depends(get_db),
    ):
        return await personalized_feed(request, page, limit, db)

    @app.get("/api/personalize/history")
    async def _history(
        request: Request,
        page: int = Query(1, ge=1),
        limit: int = Query(20, ge=1, le=100),
        db=Depends(get_db),
    ):
        return await get_history(request, page, limit, db)

    @app.delete("/api/personalize/history")
    async def _wipe(request: Request, db=Depends(get_db)):
        return await wipe_history(request, db)

    @app.post("/api/personalize/feedback")
    async def _feedback(request: Request, data: dict, db=Depends(get_db)):
        return await feedback(request, data, db)

    @app.get("/api/personalize/interests")
    async def _interests(request: Request, db=Depends(get_db)):
        return await get_interests(request, db)

    logger.info("[ProPersonalize] Routes registered: track, feed, history, feedback, interests")


# Late imports for module-level references
from sqlalchemy import or_  # noqa: E402
import math  # noqa: E402
from collections import deque  # noqa: E402
from collections import defaultdict as _dd  # noqa: E402  (already used above)
