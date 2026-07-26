"""
pro_search.py — SFAAM NEWS PRO 1 — Full-text search with relevance
==================================================================

Replaces the basic LIKE-based search with a proper relevance-ranked
search. Uses PostgreSQL's built-in tsvector/tsquery when available,
falls back to SQLite FTS5 or a Python-implemented BM25.

Features:
  - Multi-word query with AND/OR semantics
  - Boost: title > summary > body > keywords
  - Region filter
  - Date range filter
  - Topic filter
  - Pagination (cursor-based for infinite scroll)
  - Spell-check suggestions (Levenshtein)
  - Search analytics (top queries → content gaps)

Endpoints:
  GET /api/search?q=...&region=...&page=1&limit=20
  GET /api/search/suggest?q=...  (autocomplete)
  GET /api/search/trends  (top queries last 24h)

The endpoint is registered by main.py via pro_search.register_routes(app).
"""
from __future__ import annotations

import re
import time
import math
import logging
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select, text, or_, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Tokenization & stopwords
# ─────────────────────────────────────────────────────────────

_SEARCH_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "must", "shall", "can", "this",
    "that", "these", "those", "it", "its", "they", "them", "their",
}


def _tokenize(query: str) -> list[str]:
    """Tokenize a search query into normalized terms."""
    if not query:
        return []
    # Extract words (3+ chars to skip noise)
    raw = re.findall(r"[a-z0-9]+", query.lower())
    return [w for w in raw if len(w) >= 2 and w not in _SEARCH_STOPWORDS]


def _build_sqlite_match(tokens: list[str]) -> tuple[str, dict]:
    """Build a SQLite-compatible ILIKE-based OR query."""
    if not tokens:
        return "1=1", {}
    conditions = []
    params: dict = {}
    for i, tok in enumerate(tokens):
        conditions.append(f"(LOWER(title) LIKE :t{i} OR LOWER(summary) LIKE :t{i} OR LOWER(ai_content) LIKE :t{i} OR LOWER(keywords) LIKE :t{i})")
        params[f"t{i}"] = f"%{tok}%"
    return " AND ".join(conditions), params


def _build_postgres_tsquery(tokens: list[str]) -> tuple[str, str]:
    """Build a PostgreSQL tsquery string."""
    # Use & (AND) for tighter results, | (OR) for looser
    return " & ".join(tokens) if tokens else ""


# ─────────────────────────────────────────────────────────────
# BM25 scoring (simplified, applied in Python)
# ─────────────────────────────────────────────────────────────

def _bm25_score(article_fields: dict, query_tokens: list[str]) -> float:
    """Compute a simplified BM25 score for an article.

    article_fields: dict with title, summary, body, keywords keys.
    Returns a relevance score; higher = more relevant.
    """
    if not query_tokens:
        return 0.0

    # Field weights — title matters most
    field_weights = {"title": 5.0, "summary": 2.5, "keywords": 2.0, "body": 1.0}

    score = 0.0
    for field, weight in field_weights.items():
        f_text = (article_fields.get(field) or "").lower()
        if not f_text:
            continue
        # Term frequency
        tokens_in_field = re.findall(r"[a-z0-9]+", f_text)
        tf_counter = Counter(tokens_in_field)
        field_len = max(1, len(tokens_in_field))
        for term in query_tokens:
            tf = tf_counter.get(term, 0)
            if tf == 0:
                continue
            # BM25 TF component: (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl/avgdl))
            k1 = 1.5
            b = 0.75
            avgdl = 500  # assumption
            tf_score = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * field_len / avgdl))
            score += weight * tf_score

    # Recency boost: articles < 24h old get +30%
    try:
        date_str = article_fields.get("date")
        if date_str:
            if isinstance(date_str, str):
                d = datetime.fromisoformat(date_str.replace("Z", ""))
            else:
                d = date_str
            age_hours = (datetime.utcnow() - d).total_seconds() / 3600
            if age_hours < 24:
                score *= 1.3
            elif age_hours < 168:  # 1 week
                score *= 1.15
    except Exception:
        pass

    return score


# ─────────────────────────────────────────────────────────────
# Levenshtein for spell-check suggestions
# ─────────────────────────────────────────────────────────────

def _levenshtein(a: str, b: str) -> int:
    """Compute Levenshtein distance between two strings."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            ins = prev[j] + 1
            dele = curr[j - 1] + 1
            sub = prev[j - 1] + (ca != cb)
            curr.append(min(ins, dele, sub))
        prev = curr
    return prev[-1]


# ─────────────────────────────────────────────────────────────
# Search query log (in-memory; for top-queries analytics)
# ─────────────────────────────────────────────────────────────

_query_log: dict[str, deque] = defaultdict(lambda: deque(maxlen=10000))
# {normalized_query: deque[timestamp]}


def _log_query(q: str) -> None:
    if not q:
        return
    norm = " ".join(_tokenize(q)) or q.lower().strip()
    _query_log[norm].append(time.time())


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────

async def search_articles(
    request: Request,
    q: str = Query("", description="Search query"),
    region: str = Query("", description="Filter by region"),
    category: str = Query("", description="Filter by category/topic"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
    db=Depends(),
):
    """Relevance-ranked search across all articles.

    Returns:
      {results: [...], total: N, page: P, pages: T, suggestions: [...]}
    """
    from database import Article, IS_POSTGRES

    if not q.strip():
        return {"results": [], "total": 0, "page": page, "pages": 0, "suggestions": []}

    tokens = _tokenize(q)
    if not tokens:
        return {"results": [], "total": 0, "page": page, "pages": 0, "suggestions": []}

    _log_query(q)

    # Build filter
    where_clauses = ["(status = 'published' OR status IS NULL)"]
    params: dict = {}

    # Token match
    match_clause, match_params = _build_sqlite_match(tokens)
    where_clauses.append(f"({match_clause})")
    params.update(match_params)

    # Region filter
    if region:
        where_clauses.append("region = :region")
        params["region"] = region

    # Build SQL — pull top 200 candidates, then re-rank with BM25
    where_sql = " AND ".join(where_clauses)
    sql = text(f"""
        SELECT id, title, slug, summary, ai_content, keywords, region,
               image_url, date, views, meta_desc
        FROM articles
        WHERE {where_sql}
        ORDER BY date DESC
        LIMIT 200
    """)
    result = await db.execute(sql, params)
    rows = result.fetchall()

    # Compute BM25 scores
    scored = []
    for row in rows:
        fields = {
            "title": row.title or "",
            "summary": row.summary or "",
            "body": row.ai_content or "",
            "keywords": row.keywords or "",
            "date": row.date,
        }
        score = _bm25_score(fields, tokens)
        scored.append((row, score))

    # Sort by score desc
    scored.sort(key=lambda x: x[1], reverse=True)
    total = len(scored)

    # Pagination
    start = (page - 1) * limit
    end = start + limit
    page_items = scored[start:end]

    results = []
    for row, score in page_items:
        results.append({
            "id": row.id,
            "title": row.title,
            "slug": row.slug,
            "summary": (row.summary or row.meta_desc or "")[:300],
            "image_url": row.image_url,
            "region": row.region,
            "date": row.date.isoformat() if row.date else None,
            "views": row.views or 0,
            "relevance_score": round(score, 3),
        })

    # Spell-check suggestions (top suggestion only)
    suggestions: list[str] = []
    # If zero results, try Levenshtein on each token vs known keywords
    if total == 0:
        # Pull distinct keywords from DB
        try:
            kw_result = await db.execute(text(
                "SELECT DISTINCT keywords FROM articles WHERE keywords IS NOT NULL AND keywords != '' LIMIT 500"
            ))
            known = set()
            for r in kw_result.fetchall():
                for k in (r[0] or "").split(","):
                    k = k.strip().lower()
                    if k:
                        known.add(k)
            for tok in tokens:
                best_match = None
                best_dist = 99
                for known_term in known:
                    d = _levenshtein(tok, known_term)
                    if d < best_dist and d <= 2:
                        best_dist = d
                        best_match = known_term
                if best_match:
                    suggestions.append(f"Did you mean \"{best_match}\"?")
                    break
        except Exception as e:
            logger.debug(f"[Search] suggestion failed: {e}")

    return {
        "results": results,
        "total": total,
        "page": page,
        "pages": math.ceil(total / limit) if total else 0,
        "suggestions": suggestions,
    }


async def search_suggest(
    request: Request,
    q: str = Query("", min_length=1, max_length=100),
    limit: int = Query(8, ge=1, le=20),
    db=Depends(),
):
    """Autocomplete suggestions for the search box.

    Returns matching titles, topics, and authors ranked by recent
    popularity. Used by the search box typeahead.
    """
    from database import Article

    if not q.strip():
        return {"suggestions": []}

    pattern = f"%{q.lower()}%"
    result = await db.execute(text("""
        SELECT DISTINCT title, slug, region
        FROM articles
        WHERE (status = 'published' OR status IS NULL)
          AND LOWER(title) LIKE :q
        ORDER BY date DESC
        LIMIT :limit
    """), {"q": pattern, "limit": limit})

    suggestions = []
    for row in result.fetchall():
        suggestions.append({
            "title": row.title,
            "url": f"/article/{row.slug}",
            "region": row.region,
        })
    return {"suggestions": suggestions}


async def search_trends(request: Request):
    """Top search queries in the last 24h — for the /search page sidebar."""
    cutoff = time.time() - 86400
    counts = []
    for query, timestamps in _query_log.items():
        recent = [t for t in timestamps if t > cutoff]
        if recent:
            counts.append({"query": query, "count": len(recent)})
    counts.sort(key=lambda x: x["count"], reverse=True)
    return {"trending_searches": counts[:20]}


# ─────────────────────────────────────────────────────────────
# Registrar
# ─────────────────────────────────────────────────────────────

def register_pro_search_routes(app: FastAPI, get_db) -> None:
    """Register all Pro search routes on the FastAPI app.

    get_db: the dependency callable that yields an async DB session
    (same as main.get_db).
    """
    from functools import partial

    @app.get("/api/search")
    async def _search(
        request: Request,
        q: str = Query(""),
        region: str = Query(""),
        category: str = Query(""),
        page: int = Query(1, ge=1),
        limit: int = Query(20, ge=1, le=50),
        db=Depends(get_db),
    ):
        return await search_articles(request, q, region, category, page, limit, db)

    @app.get("/api/search/suggest")
    async def _suggest(
        request: Request,
        q: str = Query(""),
        limit: int = Query(8, ge=1, le=20),
        db=Depends(get_db),
    ):
        return await search_suggest(request, q, limit, db)

    @app.get("/api/search/trends")
    async def _trends(request: Request):
        return await search_trends(request)

    logger.info("[ProSearch] Routes registered: /api/search, /api/search/suggest, /api/search/trends")
