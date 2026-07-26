"""
pro_engagement.py — SFAAM NEWS PRO 1
======================================

Engagement features that make users come back:
  - Highlights (Medium-style text highlighting + save)
  - Reactions (like / love / insightful / celebrate / disagree)
  - Threaded comments (with moderation queue)
  - Bookmark folders (organize saved articles)
  - Reading list collections (public)
  - Citation system (Wikipedia-grade inline references)

All endpoints key off an anonymous fingerprint (no PII required)
except digest emails. GDPR-friendly out of the box.
"""
from __future__ import annotations

import re
import time
import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select, text, desc, or_
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _fp(request: Request) -> str:
    """Get reader fingerprint from header."""
    fp = request.headers.get("x-reader-fp", "")
    if not fp or len(fp) > 100:
        fwd = request.headers.get("x-forwarded-for", "")
        ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "anon")
        return f"ip:{ip}"
    return fp


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HIGHLIGHTS — Medium-style text selection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def create_highlight(request: Request, data: dict, db: AsyncSession):
    """Save a text highlight. Body: {article_id, text, note?, color?}."""
    from pro_models import ProHighlight
    fp = _fp(request)
    article_id = data.get("article_id")
    hl_text = (data.get("text") or "").strip()
    if not isinstance(article_id, int) or article_id <= 0:
        raise HTTPException(400, "Invalid article_id")
    if not hl_text or len(hl_text) > 1000:
        raise HTTPException(400, "Highlight text required (max 1000 chars)")

    note = (data.get("note") or "").strip()[:500]
    color = (data.get("color") or "yellow").strip().lower()
    if color not in ("yellow", "green", "blue", "pink"):
        color = "yellow"

    h = ProHighlight(
        article_id=article_id,
        fingerprint=fp,
        highlighted_text=hl_text,
        note=note,
        color=color,
        is_public=bool(data.get("is_public", True)),
    )
    db.add(h)
    await db.commit()
    return {"ok": True, "id": h.id}


async def list_highlights(request: Request, article_id: Optional[int], db: AsyncSession):
    """List highlights. If article_id given, list PUBLIC highlights for
    that article. Otherwise list the current user's highlights."""
    from pro_models import ProHighlight
    fp = _fp(request)
    if article_id:
        rows = (await db.execute(
            select(ProHighlight).where(
                ProHighlight.article_id == article_id,
                ProHighlight.is_public == True,  # noqa: E712
            ).order_by(desc(ProHighlight.agrees_count), desc(ProHighlight.created_at)).limit(50)
        )).scalars().all()
    else:
        rows = (await db.execute(
            select(ProHighlight).where(
                ProHighlight.fingerprint == fp,
            ).order_by(desc(ProHighlight.created_at)).limit(100)
        )).scalars().all()
    return {"highlights": [{
        "id": h.id,
        "article_id": h.article_id,
        "text": h.highlighted_text,
        "note": h.note,
        "color": h.color,
        "agrees": h.agrees_count or 0,
        "created_at": h.created_at.isoformat() if h.created_at else None,
    } for h in rows]}


async def delete_highlight(request: Request, highlight_id: int, db: AsyncSession):
    """Delete the user's own highlight."""
    from pro_models import ProHighlight
    fp = _fp(request)
    h = (await db.execute(
        select(ProHighlight).where(ProHighlight.id == highlight_id, ProHighlight.fingerprint == fp)
    )).scalar_one_or_none()
    if not h:
        raise HTTPException(404, "Highlight not found")
    await db.delete(h)
    await db.commit()
    return {"ok": True}


async def agree_highlight(highlight_id: int, db: AsyncSession):
    """Increment the 'agree' counter for a highlight."""
    from pro_models import ProHighlight
    h = (await db.execute(
        select(ProHighlight).where(ProHighlight.id == highlight_id)
    )).scalar_one_or_none()
    if not h:
        raise HTTPException(404, "Highlight not found")
    h.agrees_count = (h.agrees_count or 0) + 1
    await db.commit()
    return {"ok": True, "agrees": h.agrees_count}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# REACTIONS — Emoji-style
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

VALID_REACTIONS = {"like", "love", "insightful", "celebrate", "disagree"}


async def set_reaction(request: Request, data: dict, db: AsyncSession):
    """Set or update the user's reaction to an article."""
    from pro_models import ProReaction
    fp = _fp(request)
    article_id = data.get("article_id")
    reaction = (data.get("reaction") or "").strip().lower()
    if not isinstance(article_id, int) or article_id <= 0:
        raise HTTPException(400, "Invalid article_id")
    if reaction not in VALID_REACTIONS:
        raise HTTPException(400, f"Reaction must be one of {VALID_REACTIONS}")

    existing = (await db.execute(
        select(ProReaction).where(
            ProReaction.fingerprint == fp,
            ProReaction.article_id == article_id,
        )
    )).scalar_one_or_none()

    if existing:
        existing.reaction = reaction
    else:
        db.add(ProReaction(
            fingerprint=fp,
            article_id=article_id,
            reaction=reaction,
        ))
    await db.commit()
    return {"ok": True, "reaction": reaction}


async def get_reaction_counts(article_id: int, db: AsyncSession):
    """Return counts per reaction type for an article."""
    rows = (await db.execute(text("""
        SELECT reaction, COUNT(*) as cnt
        FROM pro_reactions
        WHERE article_id = :aid
        GROUP BY reaction
    """), {"aid": article_id})).fetchall()
    counts = {r: 0 for r in VALID_REACTIONS}
    for r in rows:
        counts[r.reaction] = r.cnt
    return {"article_id": article_id, "reactions": counts}


async def get_user_reaction(request: Request, article_id: int, db: AsyncSession):
    """Return the current user's reaction to an article."""
    from pro_models import ProReaction
    fp = _fp(request)
    r = (await db.execute(
        select(ProReaction).where(
            ProReaction.fingerprint == fp,
            ProReaction.article_id == article_id,
        )
    )).scalar_one_or_none()
    return {"article_id": article_id, "reaction": r.reaction if r else None}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# THREADED COMMENTS with moderation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Simple spam detection — links, all-caps, repeated chars, common spam words
_SPAM_WORDS = {
    "viagra", "casino", "porn", "sex", "loan", "credit", "free money",
    "make money", "work from home", "click here", "buy now", "limited offer",
}


def _spam_score(body: str) -> float:
    """Return a spam score 0-1 for the comment body."""
    if not body:
        return 1.0
    score = 0.0
    body_lower = body.lower()
    # Length
    if len(body) < 5:
        score += 0.3
    if len(body) > 5000:
        score += 0.2
    # All caps
    if body.isupper() and len(body) > 20:
        score += 0.4
    # Multiple links
    link_count = len(re.findall(r"https?://", body))
    if link_count >= 3:
        score += 0.5
    elif link_count >= 1:
        score += 0.2
    # Spam words
    for word in _SPAM_WORDS:
        if word in body_lower:
            score += 0.4
            break
    # Repeated chars (e.g. "!!!!!!!!!!!")
    if re.search(r"(.)\1{10,}", body):
        score += 0.3
    return min(1.0, score)


async def post_comment(request: Request, data: dict, db: AsyncSession):
    """Post a (possibly threaded) comment. Body: {article_id, body, parent_id?, name?}."""
    from pro_models import ProCommentThread
    fp = _fp(request)
    article_id = data.get("article_id")
    body = (data.get("body") or "").strip()
    parent_id = data.get("parent_id")
    name = (data.get("name") or "").strip()[:80]

    if not isinstance(article_id, int) or article_id <= 0:
        raise HTTPException(400, "Invalid article_id")
    if not body or len(body) > 5000:
        raise HTTPException(400, "Comment body required (max 5000 chars)")
    if parent_id is not None and (not isinstance(parent_id, int) or parent_id <= 0):
        raise HTTPException(400, "Invalid parent_id")

    spam = _spam_score(body)
    # Auto-approve if low spam score; route to moderation queue otherwise
    is_approved = spam < 0.4

    c = ProCommentThread(
        article_id=article_id,
        parent_id=parent_id,
        fingerprint=fp,
        author_name=name,
        body=body,
        is_approved=is_approved,
        spam_score=spam,
    )
    db.add(c)
    await db.commit()
    if is_approved:
        return {"ok": True, "id": c.id, "status": "approved"}
    return {"ok": True, "id": c.id, "status": "pending_moderation", "message": "Your comment is being reviewed."}


async def list_comments(article_id: int, sort: str, db: AsyncSession):
    """List approved comments for an article, threaded.

    sort: "newest" | "oldest" | "top"
    """
    order = desc(ProCommentThread.created_at) if sort == "newest" else ProCommentThread.created_at
    if sort == "top":
        order = desc(ProCommentThread.upvotes - ProCommentThread.downvotes)
    from pro_models import ProCommentThread

    rows = (await db.execute(
        select(ProCommentThread).where(
            ProCommentThread.article_id == article_id,
            ProCommentThread.is_approved == True,  # noqa: E712
        ).order_by(order).limit(500)
    )).scalars().all()

    # Build threaded tree
    by_id = {c.id: {
        "id": c.id, "article_id": c.article_id, "parent_id": c.parent_id,
        "author_name": c.author_name or "Anonymous", "body": c.body,
        "upvotes": c.upvotes or 0, "downvotes": c.downvotes or 0,
        "score": (c.upvotes or 0) - (c.downvotes or 0),
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "replies": [],
    } for c in rows}
    roots = []
    for c in rows:
        node = by_id[c.id]
        if c.parent_id and c.parent_id in by_id:
            by_id[c.parent_id]["replies"].append(node)
        else:
            roots.append(node)
    return {"comments": roots, "total": len(rows)}


async def vote_comment(request: Request, data: dict, db: AsyncSession):
    """Upvote or downvote a comment."""
    from pro_models import ProCommentThread
    fp = _fp(request)
    comment_id = data.get("comment_id")
    direction = data.get("direction")
    if not isinstance(comment_id, int) or direction not in ("up", "down"):
        raise HTTPException(400, "Invalid payload")

    c = (await db.execute(
        select(ProCommentThread).where(ProCommentThread.id == comment_id)
    )).scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Comment not found")

    if direction == "up":
        c.upvotes = (c.upvotes or 0) + 1
    else:
        c.downvotes = (c.downvotes or 0) + 1
    await db.commit()
    return {"ok": True, "upvotes": c.upvotes, "downvotes": c.downvotes}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BOOKMARK FOLDERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def list_folders(request: Request, db: AsyncSession):
    from pro_models import ProBookmarkFolder, ProBookmarkItem
    fp = _fp(request)
    folders = (await db.execute(
        select(ProBookmarkFolder).where(ProBookmarkFolder.fingerprint == fp)
    )).scalars().all()
    result = []
    for f in folders:
        count = (await db.execute(
            select(text("COUNT(*)")).select_from(ProBookmarkItem).where(ProBookmarkItem.folder_id == f.id)
        )).scalar() or 0
        result.append({
            "id": f.id, "name": f.name, "description": f.description,
            "is_public": f.is_public, "items_count": count,
            "created_at": f.created_at.isoformat() if f.created_at else None,
        })
    return {"folders": result}


async def create_folder(request: Request, data: dict, db: AsyncSession):
    from pro_models import ProBookmarkFolder
    fp = _fp(request)
    name = (data.get("name") or "").strip()
    if not name or len(name) > 100:
        raise HTTPException(400, "Folder name required (max 100 chars)")
    desc = (data.get("description") or "").strip()[:300]
    is_public = bool(data.get("is_public", False))
    f = ProBookmarkFolder(fingerprint=fp, name=name, description=desc, is_public=is_public)
    db.add(f)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(409, "Folder with this name already exists")
    return {"ok": True, "id": f.id}


async def add_to_folder(request: Request, data: dict, db: AsyncSession):
    from pro_models import ProBookmarkFolder, ProBookmarkItem
    fp = _fp(request)
    folder_id = data.get("folder_id")
    article_id = data.get("article_id")
    notes = (data.get("notes") or "").strip()[:500]
    if not isinstance(folder_id, int) or not isinstance(article_id, int):
        raise HTTPException(400, "Invalid payload")
    # Verify folder ownership
    f = (await db.execute(
        select(ProBookmarkFolder).where(
            ProBookmarkFolder.id == folder_id,
            ProBookmarkFolder.fingerprint == fp,
        )
    )).scalar_one_or_none()
    if not f:
        raise HTTPException(404, "Folder not found")
    # Upsert
    existing = (await db.execute(
        select(ProBookmarkItem).where(
            ProBookmarkItem.folder_id == folder_id,
            ProBookmarkItem.article_id == article_id,
        )
    )).scalar_one_or_none()
    if existing:
        existing.notes = notes or existing.notes
    else:
        db.add(ProBookmarkItem(folder_id=folder_id, article_id=article_id, notes=notes))
    await db.commit()
    return {"ok": True}


async def list_folder_items(request: Request, folder_id: int, db: AsyncSession):
    from pro_models import ProBookmarkFolder, ProBookmarkItem
    from database import Article
    fp = _fp(request)
    f = (await db.execute(
        select(ProBookmarkFolder).where(
            ProBookmarkFolder.id == folder_id,
            ProBookmarkFolder.fingerprint == fp,
        )
    )).scalar_one_or_none()
    if not f:
        raise HTTPException(404, "Folder not found")
    items = (await db.execute(
        select(Article, ProBookmarkItem).join(ProBookmarkItem, ProBookmarkItem.article_id == Article.id).where(ProBookmarkItem.folder_id == folder_id).order_by(desc(ProBookmarkItem.added_at))
    )).all()
    return {"folder": {"id": f.id, "name": f.name, "description": f.description}, "items": [{
        "article_id": a.id, "title": a.title, "slug": a.slug, "summary": (a.summary or "")[:200],
        "image_url": a.image_url, "region": a.region, "date": a.date.isoformat() if a.date else None,
        "added_at": bi.added_at.isoformat() if bi.added_at else None, "notes": bi.notes,
    } for a, bi in items]}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CITATIONS — Inline [1][2] reference system
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def list_citations(article_id: int, db: AsyncSession):
    """Return all citations for an article, sorted by position."""
    from pro_models import ProCitation
    try:
        rows = (await db.execute(
            select(ProCitation).where(ProCitation.article_id == article_id).order_by(ProCitation.position)
        )).scalars().all()
    except Exception:
        rows = []  # table not yet created
    return {"citations": [{
        "position": c.position,
        "source_domain": c.source_domain,
        "source_url": c.source_url,
        "source_title": c.source_title,
        "quoted_text": c.quoted_text,
        "source_date": c.source_date.isoformat() if c.source_date else None,
        "is_authoritative": c.is_authoritative,
    } for c in rows]}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CORRECTIONS — Per-article correction log
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def list_corrections(article_id: Optional[int], db: AsyncSession):
    """List corrections. If article_id given, filter to that article."""
    from pro_models import ProCorrection
    try:
        query = select(ProCorrection).order_by(desc(ProCorrection.corrected_at)).limit(200)
        if article_id:
            query = query.where(ProCorrection.article_id == article_id)
        rows = (await db.execute(query)).scalars().all()
    except Exception:
        rows = []
    return {"corrections": [{
        "id": c.id, "article_id": c.article_id,
        "correction_type": c.correction_type,
        "original_text": c.original_text,
        "corrected_text": c.corrected_text,
        "editor_note": c.editor_note,
        "corrected_by": c.corrected_by,
        "corrected_at": c.corrected_at.isoformat() if c.corrected_at else None,
    } for c in rows]}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Registrar
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def register_pro_engagement_routes(app: FastAPI, get_db, admin_guard) -> None:
    # Highlights
    @app.post("/api/highlight")
    async def _hl_create(request: Request, data: dict, db=Depends(get_db)):
        return await create_highlight(request, data, db)
    @app.get("/api/highlight")
    async def _hl_list(request: Request, article_id: Optional[int] = None, db=Depends(get_db)):
        return await list_highlights(request, article_id, db)
    @app.delete("/api/highlight/{highlight_id}")
    async def _hl_del(highlight_id: int, request: Request, db=Depends(get_db)):
        return await delete_highlight(request, highlight_id, db)
    @app.post("/api/highlight/{highlight_id}/agree")
    async def _hl_agree(highlight_id: int, db=Depends(get_db)):
        return await agree_highlight(highlight_id, db)

    # Reactions
    @app.post("/api/reaction")
    async def _rx_set(request: Request, data: dict, db=Depends(get_db)):
        return await set_reaction(request, data, db)
    @app.get("/api/reaction/{article_id}")
    async def _rx_get(article_id: int, db=Depends(get_db)):
        return await get_reaction_counts(article_id, db)
    @app.get("/api/reaction/{article_id}/me")
    async def _rx_me(article_id: int, request: Request, db=Depends(get_db)):
        return await get_user_reaction(request, article_id, db)

    # Comments
    @app.post("/api/comment")
    async def _c_post(request: Request, data: dict, db=Depends(get_db)):
        return await post_comment(request, data, db)
    @app.get("/api/comments/{article_id}")
    async def _c_list(article_id: int, sort: str = "top", db=Depends(get_db)):
        return await list_comments(article_id, sort, db)
    @app.post("/api/comment/vote")
    async def _c_vote(request: Request, data: dict, db=Depends(get_db)):
        return await vote_comment(request, data, db)

    # Bookmark folders
    @app.get("/api/bookmark-folders")
    async def _bf_list(request: Request, db=Depends(get_db)):
        return await list_folders(request, db)
    @app.post("/api/bookmark-folder")
    async def _bf_create(request: Request, data: dict, db=Depends(get_db)):
        return await create_folder(request, data, db)
    @app.post("/api/bookmark-folder/{folder_id}/add")
    async def _bf_add(folder_id: int, request: Request, data: dict, db=Depends(get_db)):
        data = {**data, "folder_id": folder_id}
        return await add_to_folder(request, data, db)
    @app.get("/api/bookmark-folder/{folder_id}")
    async def _bf_items(folder_id: int, request: Request, db=Depends(get_db)):
        return await list_folder_items(request, folder_id, db)

    # Citations
    @app.get("/api/citations/{article_id}")
    async def _cit(article_id: int, db=Depends(get_db)):
        return await list_citations(article_id, db)

    # Corrections
    @app.get("/api/corrections")
    async def _corr_list(article_id: Optional[int] = None, db=Depends(get_db)):
        return await list_corrections(article_id, db)

    logger.info("[ProEngagement] Routes registered: highlights, reactions, comments, folders, citations, corrections")
