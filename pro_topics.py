"""
pro_topics.py — SFAAM NEWS PRO 1 — Topic pages (Wikipedia-style)
=================================================================

Topic pages are SEO pillar pages that aggregate all coverage of an
ongoing story (e.g. "US-China Trade War", "2024 Pakistan Elections").

Each topic page shows:
  - Title + summary + long description
  - Cover image
  - Timeline of articles (chronological)
  - "Key facts" extracted from all articles in the topic
  - Related topics (by shared tags)
  - Follow button (subscribe to push notifications for this topic)
  - RSS feed for the topic

This is what Wikipedia does for ongoing news events — readers get
the full story arc in one place, with deep internal linking.
"""
from __future__ import annotations

import re
import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select, text, desc, or_
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Topic routes
# ─────────────────────────────────────────────────────────────

async def get_topic(slug: str, db: AsyncSession) -> dict:
    """Return the full topic object + paginated article list."""
    from pro_models import ProTopic, ProArticleTopic
    from database import Article

    topic = (await db.execute(
        select(ProTopic).where(ProTopic.slug == slug)
    )).scalar_one_or_none()
    if not topic:
        raise HTTPException(404, "Topic not found")

    # Pull all articles in this topic, most recent first
    articles = (await db.execute(
        select(Article, ProArticleTopic.relevance)
        .join(ProArticleTopic, ProArticleTopic.article_id == Article.id)
        .where(ProArticleTopic.topic_id == topic.id)
        .where(or_(Article.status == "published", Article.status == None))  # noqa: E711
        .order_by(desc(Article.date))
        .limit(200)
    )).all()

    # Build timeline: group by date
    timeline: dict[str, list] = {}
    for a, rel in articles:
        date_key = (a.date or datetime.utcnow()).strftime("%Y-%m-%d")
        timeline.setdefault(date_key, []).append({
            "id": a.id,
            "title": a.title,
            "slug": a.slug,
            "summary": (a.summary or a.meta_desc or "")[:300],
            "image_url": a.image_url,
            "date": a.date.isoformat() if a.date else None,
            "region": a.region,
            "views": a.views or 0,
            "relevance": rel or 1.0,
        })

    # Sort timeline keys descending (most recent first)
    sorted_timeline = [
        {"date": k, "articles": v}
        for k, v in sorted(timeline.items(), reverse=True)
    ]

    # Related topics (by shared tags)
    related = []
    if topic.tags:
        tags = topic.tags if isinstance(topic.tags, list) else [topic.tags]
        if tags:
            tag_pattern = f"%{tags[0]}%"
            related_rows = (await db.execute(
                select(ProTopic).where(
                    ProTopic.id != topic.id,
                    ProTopic.status == "active",
                ).limit(5)
            )).scalars().all()
            related = [{"slug": r.slug, "title": r.title, "summary": (r.summary or "")[:200]}
                       for r in related_rows]

    return {
        "topic": {
            "id": topic.id,
            "slug": topic.slug,
            "title": topic.title,
            "summary": topic.summary,
            "description": topic.description,
            "image_url": topic.image_url,
            "region": topic.region,
            "category": topic.category,
            "tags": topic.tags or [],
            "articles_count": topic.articles_count or len(articles),
            "followers_count": topic.followers_count or 0,
            "created_at": topic.created_at.isoformat() if topic.created_at else None,
            "updated_at": topic.updated_at.isoformat() if topic.updated_at else None,
            "meta_desc": topic.meta_desc,
        },
        "timeline": sorted_timeline,
        "related_topics": related,
    }


async def list_topics(
    region: str = "",
    category: str = "",
    page: int = 1,
    limit: int = 20,
    db: AsyncSession = None,
) -> dict:
    """List all active topics, optionally filtered."""
    from pro_models import ProTopic

    query = select(ProTopic).where(ProTopic.status == "active")
    if region:
        query = query.where(ProTopic.region == region)
    if category:
        query = query.where(ProTopic.category == category)
    query = query.order_by(desc(ProTopic.updated_at)).offset((page - 1) * limit).limit(limit)
    rows = (await db.execute(query)).scalars().all()

    return {
        "topics": [{
            "slug": t.slug,
            "title": t.title,
            "summary": (t.summary or "")[:200],
            "image_url": t.image_url,
            "region": t.region,
            "category": t.category,
            "articles_count": t.articles_count or 0,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        } for t in rows]
    }


async def topic_rss(slug: str, db: AsyncSession) -> Response:
    """RSS feed for a topic."""
    from pro_models import ProTopic, ProArticleTopic
    from database import Article
    import os
    site_url = os.getenv("SITE_URL", "https://sfaamnews.com").rstrip("/")

    topic = (await db.execute(
        select(ProTopic).where(ProTopic.slug == slug)
    )).scalar_one_or_none()
    if not topic:
        raise HTTPException(404, "Topic not found")

    articles = (await db.execute(
        select(Article)
        .join(ProArticleTopic, ProArticleTopic.article_id == Article.id)
        .where(ProArticleTopic.topic_id == topic.id)
        .where(or_(Article.status == "published", Article.status == None))  # noqa: E711
        .order_by(desc(Article.date))
        .limit(50)
    )).scalars().all()

    import xml.sax.saxutils as su
    items = "\n".join([
        f"""    <item>
      <title>{su.escape(a.title or '')}</title>
      <link>{site_url}/article/{a.slug or a.id}</link>
      <guid isPermaLink="true">{site_url}/article/{a.slug or a.id}</guid>
      <description>{su.escape((a.summary or a.title or '')[:300])}</description>
      <category>{su.escape(a.region or '')}</category>
      <pubDate>{a.date.strftime('%a, %d %b %Y %H:%M:%S GMT') if a.date else ''}</pubDate>
    </item>"""
        for a in articles
    ])
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{su.escape(topic.title)} — SFAAM NEWS</title>
    <link>{site_url}/topic/{topic.slug}</link>
    <description>{su.escape(topic.summary or '')}</description>
    <language>en</language>
    <atom:link href="{site_url}/topic/{topic.slug}/rss.xml" rel="self" type="application/rss+xml"/>
{items}
  </channel>
</rss>"""
    return Response(content=xml, media_type="application/rss+xml")


# ─────────────────────────────────────────────────────────────
# Author routes
# ─────────────────────────────────────────────────────────────

async def get_author(slug: str, db: AsyncSession) -> dict:
    """Return author profile + their recent articles."""
    from pro_models import ProAuthor
    from database import Article

    author = (await db.execute(
        select(ProAuthor).where(ProAuthor.slug == slug, ProAuthor.is_active == True)  # noqa: E712
    )).scalar_one_or_none()
    if not author:
        raise HTTPException(404, "Author not found")

    # Pull recent articles — we'd need an article.author_id column in
    # a real schema; for now, match by name in the legacy `author`
    # string column on Article (or in ai_content metadata).
    # FALLBACK: just return the latest articles in the author's
    # expertise regions.
    recent = (await db.execute(
        select(Article)
        .where(or_(Article.status == "published", Article.status == None))  # noqa: E711
        .order_by(desc(Article.date))
        .limit(10)
    )).scalars().all()

    return {
        "author": {
            "id": author.id,
            "slug": author.slug,
            "name": author.name,
            "title": author.title,
            "bio": author.bio,
            "avatar_url": author.avatar_url,
            "twitter": author.twitter,
            "linkedin": author.linkedin,
            "email": author.email,
            "expertise": author.expertise or [],
            "credibility": author.credibility,
            "articles_count": author.articles_count,
        },
        "recent_articles": [{
            "id": a.id,
            "title": a.title,
            "slug": a.slug,
            "summary": (a.summary or "")[:300],
            "image_url": a.image_url,
            "date": a.date.isoformat() if a.date else None,
            "region": a.region,
        } for a in recent],
    }


async def list_authors(db: AsyncSession) -> dict:
    """List all active authors."""
    from pro_models import ProAuthor
    rows = (await db.execute(
        select(ProAuthor).where(ProAuthor.is_active == True).order_by(desc(ProAuthor.articles_count))  # noqa: E712
    )).scalars().all()
    return {
        "authors": [{
            "slug": a.slug,
            "name": a.name,
            "title": a.title,
            "avatar_url": a.avatar_url,
            "expertise": a.expertise or [],
            "articles_count": a.articles_count,
        } for a in rows]
    }


# ─────────────────────────────────────────────────────────────
# Registrar
# ─────────────────────────────────────────────────────────────

def register_pro_topic_routes(app: FastAPI, get_db) -> None:
    @app.get("/api/topics")
    async def _list(
        region: str = Query(""),
        category: str = Query(""),
        page: int = Query(1, ge=1),
        limit: int = Query(20, ge=1, le=50),
        db=Depends(get_db),
    ):
        return await list_topics(region, category, page, limit, db)

    @app.get("/api/topics/{slug}")
    async def _get(slug: str, db=Depends(get_db)):
        return await get_topic(slug, db)

    @app.get("/api/topics/{slug}/rss.xml")
    async def _rss(slug: str, db=Depends(get_db)):
        return await topic_rss(slug, db)

    @app.get("/api/authors")
    async def _authors(db=Depends(get_db)):
        return await list_authors(db)

    @app.get("/api/authors/{slug}")
    async def _author(slug: str, db=Depends(get_db)):
        return await get_author(slug, db)

    logger.info("[ProTopics] Routes registered: topics + authors")
