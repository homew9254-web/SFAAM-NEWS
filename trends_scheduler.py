"""
trends_scheduler.py - SFAAM NEWS V26 (Trends Pipeline)

Zero-Hallucination Content Engine — Stage 5
============================================
Orchestrates the full Trends pipeline:

    [Every 6 hours]
        │
        ├─ Stage 1: trends_scraper.fetch_trending_queries(7)
        │           → 7 trending Google queries
        │
        ├─ For each query:
        │   ├─ Stage 2: trends_scraper.research_trend()
        │   │           → scrape authoritative sources
        │   │
        │   ├─ Stage 3: fact_verifier.verify_facts()
        │   │           → keep only facts confirmed by 2+ sources
        │   │
        │   ├─ Stage 4: trends_writer.write_trends_article()
        │   │           → strict RAG LLM call (Groq/Gemini/fallback)
        │   │
        │   └─ Stage 6: save as DRAFT in DB (status="draft", is_trends=1)
        │
        └─ Admin can review drafts at /admin.html → "Trends Drafts" tab

Scheduler: APScheduler (interval=6 hours, also runs once on startup)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from database import AsyncSessionLocal, Article, TrendQuery
from fact_verifier import verify_facts
from trends_scraper import fetch_trending_queries, research_trend
from trends_writer import write_trends_article
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────
TRENDS_GEO = os.getenv("TRENDS_GEO", "")               # "" = worldwide
TRENDS_LIMIT = int(os.getenv("TRENDS_LIMIT", "7"))      # top 7 trends per cycle
TRENDS_MAX_SOURCES = int(os.getenv("TRENDS_MAX_SOURCES", "8"))  # V29 FIX: increased from 5 to 8 for better source diversity
TRENDS_MIN_FACTS = int(os.getenv("TRENDS_MIN_FACTS", "2"))  # V29 FIX: reduced from 3 to 2 (since MIN_SOURCES_PER_FACT=2 is stricter)
TRENDS_AI_PROVIDER = os.getenv("TRENDS_AI_PROVIDER", "auto")  # auto | groq | gemini | fallback
TRENDS_INTERVAL_HOURS = int(os.getenv("TRENDS_INTERVAL_HOURS", "6"))
TRENDS_RUN_ON_STARTUP = os.getenv("TRENDS_RUN_ON_STARTUP", "1") == "1"


# ─────────────────────────────────────────────────────────────
# Status tracking (in-memory, single-process)
# ─────────────────────────────────────────────────────────────
_trends_status = {
    "running": False,
    "last_run_ts": None,
    "last_cycle_id": None,
    "last_error": "",
    "trends_processed": 0,
    "drafts_produced": 0,
    "drafts_failed": 0,
    "current_query": "",
}


def get_trends_status() -> dict:
    """Return current pipeline status (for admin dashboard)."""
    return dict(_trends_status)


# ─────────────────────────────────────────────────────────────
# AI key resolution
# ─────────────────────────────────────────────────────────────
def _resolve_ai_keys() -> tuple[str, str]:
    """Return (groq_key, gemini_key) for the Trends pipeline.

    Tries TRENDS_GROQ_KEY / TRENDS_GEMINI_KEY first, then falls back to
    the WORLD region keys (which are typically the most widely available).
    """
    groq = (
        os.getenv("TRENDS_GROQ_KEY", "")
        or os.getenv("GROQ_KEY_WORLD", "")
        or os.getenv("GROQ_KEY_USA", "")
    )
    gemini = (
        os.getenv("TRENDS_GEMINI_KEY", "")
        or os.getenv("GEMINI_KEY_WORLD", "")
        or os.getenv("GEMINI_KEY_USA", "")
    )
    if TRENDS_AI_PROVIDER == "groq":
        gemini = ""
    elif TRENDS_AI_PROVIDER == "gemini":
        groq = ""
    elif TRENDS_AI_PROVIDER == "fallback":
        groq = gemini = ""
    return groq, gemini


# ─────────────────────────────────────────────────────────────
# Save a Trends draft to the DB
# ─────────────────────────────────────────────────────────────
async def _save_trends_draft(
    *,
    trend_query: str,
    article_data,  # TrendsArticle
    verification,  # VerificationResult
    sources,       # list[ScrapedSource]
    cycle_id: str,
) -> int | None:
    """Insert a new Article row with status='draft', is_trends=1.

    Returns the new article ID, or None on failure.
    """
    # Build a deterministic hash so we don't re-save the same trend twice in one cycle
    hash_input = f"trends|{trend_query}|{cycle_id}".encode()
    article_hash = hashlib.sha256(hash_input).hexdigest()

    # Serialize fact_sources JSON
    fact_sources_json = json.dumps([
        {
            "url": s.url,
            "domain": s.domain,
            "title": s.title,
            "snippet": s.snippet,
        }
        for s in sources
    ], ensure_ascii=False)

    # Serialize verified_facts JSON
    verified_facts_json = json.dumps([
        {
            "text": f.text,
            "source_urls": f.source_urls,
            "source_domains": f.source_domains,
            "confirmation_count": f.confirmation_count,
        }
        for f in verification.facts
    ], ensure_ascii=False)

    # References JSON
    references_json = json.dumps(article_data.references, ensure_ascii=False)

    # Build a slug from the title
    slug_base = article_data.title.lower()
    slug_base = "".join(c if c.isalnum() or c.isspace() else " " for c in slug_base)
    slug_base = "-".join(slug_base.split())
    slug = f"trends/{slug_base}"[:600]

    # Meta description from summary
    meta_desc = (article_data.summary or article_data.title)[:300]

    # Pick the best image: prefer Google Trends image_url, else first source's image (none for now)
    image_url = ""

    async with AsyncSessionLocal() as session:
        # Check for duplicate (same hash in last 24h)
        existing = await session.execute(
            select(Article).where(Article.article_hash == article_hash).limit(1)
        )
        if existing.scalar_one_or_none():
            logger.info(f"[Trends] Skipping duplicate: '{trend_query[:50]}' (already drafted this cycle)")
            return None

        article = Article(
            title=article_data.title,
            slug=slug,
            # V26 FIX: include cycle_id in original_url so the same trend can be
            # re-drafted in a later cycle without hitting the UNIQUE constraint
            # on original_url. Also URL-encode the trend_query.
            original_url=f"https://trends.google.com/?q={quote_plus(trend_query)}&cycle={cycle_id}",
            ai_content=article_data.content,
            summary=article_data.summary,
            image_url=image_url,
            region="world",
            meta_desc=meta_desc,
            keywords=trend_query,
            article_hash=article_hash,
            tldr_summary=article_data.summary,
            fact_check_status="verified",
            status="draft",   # always draft — admin reviews before publishing
            source_type="trends",
            search_keyword=trend_query,
            is_trends=1,
            trend_query=trend_query,
            fact_sources=fact_sources_json,
            verified_facts=verified_facts_json,
            source_count=len(sources),
            word_count=article_data.word_count,
            references_data=references_json,
            pipeline_version="v26",
        )
        session.add(article)
        await session.commit()
        await session.refresh(article)
        logger.info(
            f"[Trends] Saved DRAFT id={article.id} '{trend_query[:50]}' "
            f"({article_data.word_count} words, provider={article_data.provider})"
        )
        return article.id


# ─────────────────────────────────────────────────────────────
# Save trend metadata to TrendQuery table
# ─────────────────────────────────────────────────────────────
async def _save_trend_query_record(
    *,
    query: str,
    cycle_id: str,
    sources_found: int,
    facts_verified: int,
    article_id: int | None,
    status: str,
    error: str = "",
) -> None:
    """Insert a TrendQuery row for audit trail."""
    try:
        async with AsyncSessionLocal() as session:
            record = TrendQuery(
                query=query,
                cycle_id=cycle_id,
                country=TRENDS_GEO or "world",
                article_id=article_id,
                sources_found=sources_found,
                facts_verified=facts_verified,
                status=status,
                error=error,
            )
            session.add(record)
            await session.commit()
    except Exception as e:
        logger.warning(f"[Trends] Could not save TrendQuery record: {e}")


# ─────────────────────────────────────────────────────────────
# Process a single trend
# ─────────────────────────────────────────────────────────────
async def _process_one_trend(
    trend,  # TrendItem
    cycle_id: str,
    groq_key: str,
    gemini_key: str,
) -> dict:
    """Process a single trending query end-to-end.

    Returns a summary dict: {query, status, sources, facts, article_id, error}
    """
    query = trend.query
    _trends_status["current_query"] = query
    logger.info(f"[Trends] === Processing trend: '{query}' ===")

    try:
        # Stage 2: research
        result = await asyncio.to_thread(research_trend, query, TRENDS_MAX_SOURCES)
        if not result.sources:
            logger.warning(f"[Trends] No sources for '{query[:50]}' — skipping")
            await _save_trend_query_record(
                query=query, cycle_id=cycle_id,
                sources_found=0, facts_verified=0,
                article_id=None, status="failed",
                error="no authoritative sources found",
            )
            return {"query": query, "status": "failed", "error": "no sources"}

        # V29 FIX: Require at least 2 DIFFERENT domains among sources.
        # This prevents the "one source repeated throughout article" bug.
        unique_domains = set(s.domain for s in result.sources)
        if len(unique_domains) < 2:
            logger.warning(
                f"[Trends] '{query[:50]}': only {len(unique_domains)} unique domain(s) "
                f"({', '.join(unique_domains)}) — need at least 2 for diversity, skipping"
            )
            await _save_trend_query_record(
                query=query, cycle_id=cycle_id,
                sources_found=len(result.sources), facts_verified=0,
                article_id=None, status="failed",
                error=f"insufficient domain diversity ({len(unique_domains)} unique domains, need 2+)",
            )
            return {"query": query, "status": "failed", "error": "single source only"}

        # Stage 3: verify facts
        verification = verify_facts(query, result.sources)
        if verification.total_verified_facts < TRENDS_MIN_FACTS:
            logger.warning(
                f"[Trends] '{query[:50]}': only {verification.total_verified_facts} verified facts "
                f"(min={TRENDS_MIN_FACTS}) — skipping"
            )
            await _save_trend_query_record(
                query=query, cycle_id=cycle_id,
                sources_found=len(result.sources),
                facts_verified=verification.total_verified_facts,
                article_id=None, status="failed",
                error=f"insufficient verified facts ({verification.total_verified_facts}<{TRENDS_MIN_FACTS})",
            )
            return {"query": query, "status": "failed", "error": "insufficient facts"}

        # Stage 4: write article
        # write_trends_article takes groq_key/gemini_key as keyword-only args,
        # so we use a lambda wrapper for asyncio.to_thread.
        article = await asyncio.to_thread(
            lambda: write_trends_article(
                query, verification,
                groq_key=groq_key, gemini_key=gemini_key,
            )
        )

        # Stage 6: save as draft
        article_id = await _save_trends_draft(
            trend_query=query,
            article_data=article,
            verification=verification,
            sources=result.sources,
            cycle_id=cycle_id,
        )

        await _save_trend_query_record(
            query=query, cycle_id=cycle_id,
            sources_found=len(result.sources),
            facts_verified=verification.total_verified_facts,
            article_id=article_id,
            status="drafted" if article_id else "failed",
            error="" if article_id else "duplicate or save failed",
        )

        return {
            "query": query,
            "status": "drafted" if article_id else "failed",
            "sources": len(result.sources),
            "facts": verification.total_verified_facts,
            "article_id": article_id,
            "word_count": article.word_count,
            "provider": article.provider,
        }

    except Exception as e:
        logger.exception(f"[Trends] Error processing '{query}': {e}")
        await _save_trend_query_record(
            query=query, cycle_id=cycle_id,
            sources_found=0, facts_verified=0,
            article_id=None, status="failed",
            error=f"{type(e).__name__}: {e}",
        )
        return {"query": query, "status": "failed", "error": str(e)}


# ─────────────────────────────────────────────────────────────
# Run a full Trends cycle
# ─────────────────────────────────────────────────────────────
async def run_trends_cycle() -> dict:
    """Run one full 6-hour cycle of the Trends pipeline.

    Returns a summary dict.
    """
    if _trends_status["running"]:
        logger.warning("[Trends] Pipeline already running — skipping this cycle")
        return {"status": "skipped", "reason": "already running"}

    _trends_status["running"] = True
    _trends_status["last_error"] = ""
    _trends_status["current_query"] = ""
    cycle_id = str(uuid.uuid4())
    _trends_status["last_cycle_id"] = cycle_id
    _trends_status["last_run_ts"] = datetime.now(timezone.utc).isoformat()

    logger.info("=" * 70)
    logger.info(f"[Trends] V26 Pipeline Started — cycle_id={cycle_id}")
    logger.info(f"[Trends] GEO={TRENDS_GEO or 'worldwide'}, LIMIT={TRENDS_LIMIT}, "
                f"MAX_SOURCES={TRENDS_MAX_SOURCES}, MIN_FACTS={TRENDS_MIN_FACTS}")
    logger.info("=" * 70)

    groq_key, gemini_key = _resolve_ai_keys()
    if not groq_key and not gemini_key:
        logger.warning("[Trends] No AI keys set — using fallback (fact-listing) mode")

    summary = {
        "cycle_id": cycle_id,
        "started_at": _trends_status["last_run_ts"],
        "trends_processed": 0,
        "drafts_produced": 0,
        "drafts_failed": 0,
        "results": [],
    }

    try:
        # Stage 1: fetch trends
        trends = await asyncio.to_thread(fetch_trending_queries, TRENDS_GEO, TRENDS_LIMIT)
        if not trends:
            logger.warning("[Trends] No trending queries fetched — aborting cycle")
            _trends_status["last_error"] = "no trends fetched"
            summary["error"] = "no trends fetched"
            return summary

        logger.info(f"[Trends] Fetched {len(trends)} trending queries")

        # Process each trend sequentially (be polite to source servers)
        for i, trend in enumerate(trends, 1):
            logger.info(f"[Trends] [{i}/{len(trends)}] Processing: {trend.query}")
            result = await _process_one_trend(trend, cycle_id, groq_key, gemini_key)
            summary["results"].append(result)
            summary["trends_processed"] += 1
            if result["status"] == "drafted":
                summary["drafts_produced"] += 1
                _trends_status["drafts_produced"] += 1
            else:
                summary["drafts_failed"] += 1
                _trends_status["drafts_failed"] += 1

        logger.info("=" * 70)
        logger.info(
            f"[Trends] V26 Pipeline Complete — "
            f"{summary['drafts_produced']}/{summary['trends_processed']} drafts produced"
        )
        logger.info("=" * 70)

    except Exception as e:
        logger.exception(f"[Trends] Pipeline fatal error: {e}")
        _trends_status["last_error"] = f"{type(e).__name__}: {e}"
        summary["error"] = str(e)
    finally:
        _trends_status["running"] = False
        _trends_status["current_query"] = ""
        _trends_status["trends_processed"] += summary.get("trends_processed", 0)

    return summary


# ─────────────────────────────────────────────────────────────
# APScheduler integration
# ─────────────────────────────────────────────────────────────
_scheduler = None
# V32.1: Strong references to startup tasks so the GC can't collect them.
_startup_tasks: set = set()


def start_trends_scheduler() -> None:
    """Start the 6-hourly Trends pipeline.

    Safe to call multiple times — only the first call starts a scheduler.
    """
    global _scheduler
    if _scheduler is not None:
        logger.info("[Trends] Scheduler already running")
        return

    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.error("[Trends] APScheduler not installed — Trends pipeline disabled")
        return

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        run_trends_cycle,
        trigger=IntervalTrigger(hours=TRENDS_INTERVAL_HOURS),
        id="trends_pipeline",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info(
        f"[Trends] Scheduler started — runs every {TRENDS_INTERVAL_HOURS} hours "
        f"(also runs on startup={TRENDS_RUN_ON_STARTUP})"
    )

    # Run once on startup (after a short delay so the regular news pipeline can start first)
    if TRENDS_RUN_ON_STARTUP:
        async def _startup_run():
            await asyncio.sleep(15)  # let other services init first
            logger.info("[Trends] Running startup cycle...")
            await run_trends_cycle()

        # V32.1 BUGFIX: Retain a strong reference to the startup task.
        # loop.create_task() returns a Task that the GC can collect if no
        # reference is held — the task silently vanishes before completion.
        # This is the classic "Bug #13" pattern that engine_scheduler.py
        # already fixed but trends_scheduler.py missed.
        try:
            loop = asyncio.get_running_loop()
            _startup_task = loop.create_task(_startup_run())
            _startup_tasks.add(_startup_task)
            _startup_task.add_done_callback(_startup_tasks.discard)
        except RuntimeError:
            # No running loop yet (e.g., called from sync context)
            logger.warning("[Trends] No running event loop — startup run deferred")


def stop_trends_scheduler() -> None:
    """Graceful shutdown."""
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
