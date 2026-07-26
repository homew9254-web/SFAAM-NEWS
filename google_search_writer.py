"""
google_search_writer.py - SFAAM NEWS V24 — Google-Search-Based Article Generator
=================================================================================

Implements the user-requested "Google Search par Based Article Publish" workflow:

  1. Admin provides a topic/keyword (e.g. "Best SEO tips for beginners")
  2. This module searches the web (via DuckDuckGo HTML — no API key needed,
     OR via Google Custom Search API if GOOGLE_CSE_ID + GOOGLE_API_KEY set)
  3. Fetches the top 3-4 result pages and extracts their text content
  4. Sends the aggregated text to the AI writer (ai_writer.rewrite_article)
  5. Saves the result as a DRAFT article (admin reviews + publishes)

This bypasses RSS sources entirely — useful for evergreen / how-to content
that doesn't appear in news feeds.

Usage:
  from google_search_writer import generate_article_from_topic
  result = await generate_article_from_topic(
      topic="Best SEO tips for beginners",
      region="world",
      user_id="admin",
  )
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional
from urllib.parse import quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup

from ai_writer import rewrite_article, make_slug, make_article_hash, apply_internal_links

logger = logging.getLogger(__name__)

# ── Configuration ──
GOOGLE_CSE_ID  = os.getenv("GOOGLE_CSE_ID", "")  # Google Custom Search Engine ID
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")  # Google API key
USE_DUCKDUCKGO = not (GOOGLE_CSE_ID and GOOGLE_API_KEY)  # fall back to DDG HTML
MAX_RESULTS    = int(os.getenv("GOOGLE_SEARCH_MAX", "4"))
MAX_BODY_CHARS = int(os.getenv("GOOGLE_BODY_MAX_CHARS", "8000"))  # per page

# User agent that doesn't get blocked immediately
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


async def _google_cse_search(query: str, num: int = MAX_RESULTS) -> list[dict]:
    """Search via Google Custom Search API (paid — needs API key + CSE ID).
    Returns list of {title, url, snippet}."""
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": query,
        "num": min(num, 10),
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, params=params, headers={"User-Agent": _UA})
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("items", [])[:num]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            })
        return results
    except Exception as e:
        logger.warning(f"Google CSE search failed: {e}")
        return []


async def _duckduckgo_search(query: str, num: int = MAX_RESULTS) -> list[dict]:
    """Search via DuckDuckGo HTML endpoint (free, no API key).
    Parses the HTML result page (DDG doesn't offer a free JSON API anymore)."""
    url = "https://html.duckduckgo.com/html/"
    params = {"q": query, "kl": "us-en"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                url, data=params,
                headers={"User-Agent": _UA, "Referer": "https://duckduckgo.com/"},
            )
            r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        # DDG HTML uses .result blocks with .result__a links
        for block in soup.select(".result")[:num]:
            link = block.select_one(".result__a")
            snippet_el = block.select_one(".result__snippet")
            if not link or not link.get("href"):
                continue
            # DDG wraps links in /l/?uddg=ENCODED_URL
            href = link["href"]
            m = re.search(r"uddg=([^&]+)", href)
            actual_url = (
                __import__("urllib.parse").unquote(m.group(1))
                if m else href
            )
            results.append({
                "title": link.get_text(strip=True),
                "url": actual_url,
                "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
            })
        return results
    except Exception as e:
        logger.warning(f"DuckDuckGo search failed: {e}")
        return []


async def search_web(query: str, num: int = MAX_RESULTS) -> list[dict]:
    """Search the web using configured provider (Google CSE or DuckDuckGo)."""
    if GOOGLE_CSE_ID and GOOGLE_API_KEY:
        return await _google_cse_search(query, num)
    return await _duckduckgo_search(query, num)


async def _fetch_page_text(url: str, client: httpx.AsyncClient) -> str:
    """Fetch a URL and extract its main text content.
    Skips nav/footer/script tags. Truncates to MAX_BODY_CHARS."""
    try:
        r = await client.get(url, headers={"User-Agent": _UA}, follow_redirects=True)
        r.raise_for_status()
        # Skip non-HTML responses
        if "text/html" not in r.headers.get("content-type", "").lower():
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        # Strip noise
        for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                         "noscript", "iframe", "form", "svg"]):
            tag.decompose()
        # Prefer article/main body
        main = soup.find("article") or soup.find("main") or soup.find("body")
        if not main:
            return ""
        text = main.get_text(separator="\n", strip=True)
        # Collapse whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text[:MAX_BODY_CHARS]
    except Exception as e:
        logger.debug(f"Fetch failed for {url}: {e}")
        return ""


async def _gather_source_content(results: list[dict]) -> str:
    """Fetch all result pages in parallel and concatenate their text."""
    if not results:
        return ""
    async with httpx.AsyncClient(timeout=20.0) as client:
        tasks = [_fetch_page_text(r["url"], client) for r in results]
        texts = await asyncio.gather(*tasks, return_exceptions=True)
    # Combine — skip failures
    combined = []
    for r, t in zip(results, texts):
        if isinstance(t, str) and t.strip():
            combined.append(f"### SOURCE: {r['title']}\nURL: {r['url']}\n\n{t}")
    return "\n\n---\n\n".join(combined)


async def generate_article_from_topic(
    topic: str,
    region: str = "world",
    user_id: str = "admin",
    save_to_db: bool = True,
) -> dict:
    """End-to-end Google-search-based article generation.

    Steps:
      1. Web search for `topic`
      2. Fetch top 4 result pages
      3. AI-rewrite the aggregated content
      4. Save as DRAFT article (status='draft', source_type='google_search')

    Returns:
      {
        "status": "ok" | "error",
        "message": str,
        "article_id": int | None,
        "title": str,
        "slug": str,
        "sources_used": int,
        "quality_score": dict | None,
      }
    """
    from database import AsyncSessionLocal, Article
    from sqlalchemy import select

    # Step 1: Search
    logger.info(f"[Google-Search-Writer] Searching for: '{topic}'")
    results = await search_web(topic, num=MAX_RESULTS)
    if not results:
        return {
            "status": "error",
            "message": "No search results found for the topic",
            "article_id": None, "title": "", "slug": "",
            "sources_used": 0, "quality_score": None,
        }
    logger.info(f"[Google-Search-Writer] Found {len(results)} results")

    # Step 2: Fetch + aggregate source content
    source_text = await _gather_source_content(results)
    if len(source_text) < 200:
        return {
            "status": "error",
            "message": "Could not extract enough content from search results",
            "article_id": None, "title": "", "slug": "",
            "sources_used": len(results), "quality_score": None,
        }

    # Step 3: AI rewrite (uses the existing 3-agent pipeline)
    logger.info(f"[Google-Search-Writer] AI-rewriting {len(source_text)} chars")
    result_dict = rewrite_article(source_text, fallback_title=topic, region=region)
    body = result_dict.get("body", "")
    if not body or len(body.strip()) < 50:
        return {
            "status": "error",
            "message": "AI writer produced empty content",
            "article_id": None, "title": topic, "slug": "",
            "sources_used": len(results), "quality_score": None,
        }

    # Step 4: Quality control + internal linking + save
    qc_score_json = None
    try:
        from quality_control import evaluate_article, quality_score_to_dict
        import json as _json
        qc = evaluate_article(
            title=result_dict["title"],
            body=body,
            meta_desc=result_dict.get("meta_desc", ""),
            keywords=result_dict.get("keywords", ""),
        )
        qc_score_json = _json.dumps(quality_score_to_dict(qc))
        # If QC rejects, return without saving — admin can manually retry
        if qc.verdict == "reject":
            return {
                "status": "error",
                "message": f"Quality control rejected: {'; '.join(qc.reasons)}",
                "article_id": None,
                "title": result_dict["title"],
                "slug": make_slug(result_dict["title"]),
                "sources_used": len(results),
                "quality_score": quality_score_to_dict(qc),
            }
    except Exception as e:
        logger.warning(f"QC failed (saving anyway): {e}")

    # Final status — Google-search articles ALWAYS start as drafts (admin must publish)
    final_status = "draft"

    if not save_to_db:
        return {
            "status": "ok",
            "message": "Generated (not saved — save_to_db=False)",
            "article_id": None,
            "title": result_dict["title"],
            "slug": make_slug(result_dict["title"]),
            "sources_used": len(results),
            "quality_score": qc_score_json,
        }

    # Save to DB
    art_hash = make_article_hash(result_dict["title"], body)
    # V26 FIX: include a per-call timestamp in original_url so the same topic
    # can be regenerated later without violating the UNIQUE constraint on
    # articles.original_url.
    import time as _time

    # V31.1: Title uniqueness check — append suffix if duplicate
    _title_norm_value = None
    try:
        from title_uniqueness import ensure_unique_title, compute_title_norm
        async with AsyncSessionLocal() as check_db:
            result_dict["title"] = await ensure_unique_title(check_db, result_dict["title"])
        _title_norm_value = compute_title_norm(result_dict["title"])[:500]
    except Exception as title_fix_err:
        logger.warning(f"[Google-Search-Writer] Title uniqueness check failed: {title_fix_err}")

    new_article = Article(
        title=result_dict["title"][:500],
        slug=make_slug(result_dict["title"]),
        original_url=f"google-search://{quote_plus(topic)}-{int(_time.time())}",
        ai_content=body,
        summary=(result_dict.get("meta_desc") or "")[:280],
        image_url="",
        region=region,
        meta_desc=result_dict.get("meta_desc", ""),
        keywords=result_dict.get("keywords", "") or topic,
        article_hash=art_hash,
        # V31.1: Store normalized title for fast duplicate detection
        title_norm=_title_norm_value,
        tldr_summary=result_dict.get("tldr_summary", ""),
        fact_check_status="under_review",
        audio_status="pending",
        status=final_status,
        quality_score=qc_score_json,
        source_type="google_search",
        search_keyword=topic[:300],
    )

    async with AsyncSessionLocal() as db:
        try:
            db.add(new_article)
            await db.commit()
            await db.refresh(new_article)
            logger.info(
                f"[Google-Search-Writer] Saved draft article id={new_article.id} "
                f"from topic '{topic[:50]}'"
            )
            # Audit log
            try:
                from monitoring import log_audit_event
                log_audit_event(
                    admin_id=user_id,
                    action="article.generate_from_search",
                    target_type="article",
                    target_id=new_article.id,
                    details={"topic": topic, "sources": len(results), "region": region},
                )
            except Exception:
                pass

            return {
                "status": "ok",
                "message": f"Article generated from {len(results)} sources and saved as draft",
                "article_id": new_article.id,
                "title": new_article.title,
                "slug": new_article.slug,
                "sources_used": len(results),
                "quality_score": qc_score_json,
            }
        except Exception as e:
            await db.rollback()
            logger.error(f"[Google-Search-Writer] Save failed: {e}")
            return {
                "status": "error",
                "message": f"Save failed: {type(e).__name__}: {str(e)[:200]}",
                "article_id": None,
                "title": result_dict["title"],
                "slug": make_slug(result_dict["title"]),
                "sources_used": len(results),
                "quality_score": qc_score_json,
            }
