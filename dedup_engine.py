"""
dedup_engine.py - SFAAM Automated News Engine (V30 / TRD v1.0)
===============================================================
Deduplication Engine (TRD Section 6 — "MISSING LOGICAL REQUIREMENTS")
--------------------------------------------------------------------
    "The system must keep a rolling 7-day log of processed keywords/topics
     in the database. If a 3-hour cron cycle detects a topic that has
     already been written about, it must automatically skip to the next
     highest-ranking topic to prevent repetitive articles."

Architecture
------------
This module provides BOTH an in-memory cache (fast, per-process) AND a
database-backed log (durable, survives restarts).

The in-memory cache is the first-line check (microseconds). The DB log
is the source of truth and is consulted on every cycle start to refresh
the in-memory cache.

Database schema (added in database.py):
    class ProcessedTrendKeyword:
        id           = Integer, PK
        region       = String(50), indexed     # which region
        keyword_norm = String(300), indexed    # normalized topic key
        keyword_raw  = String(500)             # original query string
        article_id   = Integer, nullable       # FK -> articles.id (if draft produced)
        processed_at = DateTime, indexed       # when this topic was last seen
        cycle_id     = String(40)              # which 3-hour cycle produced it
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────
ROLLING_WINDOW_DAYS = 7  # TRD: "rolling 7-day log"

# Stopwords for keyword normalization (small list, kept in sync with trend_detector)
# V32.1 BUGFIX: Removed "today", "yesterday", "tomorrow", "news", "report",
# "reports", "reportedly" from stopwords. For a NEWS site these are content
# words — "Today Pakistan election" and "Yesterday Pakistan election" are
# DIFFERENT stories and should NOT normalize to the same key. The old list
# caused false-positive dedup, dropping same-day follow-up coverage.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "must", "shall", "can",
    "this", "that", "these", "those", "it", "its", "they", "them",
    "their", "we", "us", "our", "you", "your", "he", "she", "his", "her",
    "says", "said", "after", "before", "during", "while",
}


# ─────────────────────────────────────────────────────────────
# Keyword normalization
# ─────────────────────────────────────────────────────────────
def normalize_keyword(query: str) -> str:
    """Normalize a trending query into a stable comparison key.

    Rules:
      • Lowercase
      • Strip punctuation
      • Remove stopwords
      • Sort remaining keywords alphabetically (order-independent match)
      • Join with single spaces

    Example:
        "US-Canada Trade Tariffs: Latest Updates"
        → "canada tariffs trade us"

    V32.1 BUGFIX: The old regex `[A-Za-z][A-Za-z0-9']+` required the first
    character to be a LETTER, which silently dropped tokens starting with a
    digit (e.g. "2024" from "Pakistan Election 2024"). Combined with the
    `len(w) > 2` filter, this also dropped "US" (a 2-letter acronym that
    carries critical meaning in "US-Canada tariffs"). Different years and
    different countries normalized to the same key, causing false-positive
    dedup that quietly killed legitimate follow-up articles.
    The new regex preserves leading digits and the filter only drops
    single-character noise.
    """
    if not query:
        return ""
    # Extract words (letters OR digits, including apostrophes inside words)
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9']*", query.lower())
    # Drop stopwords and single-char tokens. Keep 2+ char tokens so
    # "US", "EU", "UK", "AI", "IQ" etc. are preserved as content words.
    kept = sorted({w for w in words if w not in _STOPWORDS and len(w) > 1})
    return " ".join(kept)


# ─────────────────────────────────────────────────────────────
# In-memory cache (per-process)
# ─────────────────────────────────────────────────────────────
# Structure: {region_key: {normalized_keyword: datetime_processed}}
# The cache is refreshed from DB on every cycle start, and updated
# incrementally as new topics are processed.

_memory_cache: dict[str, dict[str, datetime]] = {}
_memory_cache_lock = threading.RLock()
_memory_cache_loaded = False


def _ensure_memory_cache_loaded(region: str) -> None:
    """Lazily load the in-memory cache from DB on first access.

    NOTE: This is the SYNC entry point used by `is_already_processed()`
    during trend detection. It ONLY uses the in-memory cache (no DB call).
    The DB-backed cache refresh happens explicitly via
    `await refresh_cache_from_db()` at the start of every 3-hour cycle
    (called from `automated_news_engine.run_engine_cycle`).

    If the cache has never been loaded yet (cold start), we mark it as
    "loaded" with an empty dict so we don't keep retrying — the next
    cycle will populate it properly.
    """
    global _memory_cache_loaded
    if _memory_cache_loaded:
        return
    with _memory_cache_lock:
        if _memory_cache_loaded:
            return
        # Cold start — mark as loaded with whatever we have (possibly empty).
        # The proper DB refresh happens via `await refresh_cache_from_db()`
        # in run_engine_cycle() before any trend detection runs.
        _memory_cache_loaded = True
        if not _memory_cache:
            logger.info(
                "[DedupEngine] In-memory cache cold-started (empty). "
                "Will be populated by refresh_cache_from_db() on next cycle."
            )


async def refresh_cache_from_db() -> None:
    """Reload the in-memory cache from the database.

    Called at the start of every 3-hour cycle to ensure we have the
    freshest view of what's been processed.

    This is the ONLY place that reads from the DB into the cache —
    `is_already_processed()` is purely in-memory after this load.
    """
    global _memory_cache_loaded
    try:
        from database import AsyncSessionLocal, ProcessedTrendKeyword
        from sqlalchemy import select

        cutoff = datetime.utcnow() - timedelta(days=ROLLING_WINDOW_DAYS)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ProcessedTrendKeyword.region,
                       ProcessedTrendKeyword.keyword_norm,
                       ProcessedTrendKeyword.processed_at)
                .where(ProcessedTrendKeyword.processed_at >= cutoff)
            )
            rows = result.fetchall()

        with _memory_cache_lock:
            _memory_cache.clear()
            for region, kw, processed_at in rows:
                _memory_cache.setdefault(region, {})[kw] = processed_at
        _memory_cache_loaded = True
        logger.info(
            f"[DedupEngine] Loaded {len(rows)} processed keywords from DB "
            f"across {len(_memory_cache)} regions"
        )
    except Exception as e:
        logger.warning(f"[DedupEngine] Could not load cache from DB: {e}")
        _memory_cache_loaded = True  # don't retry every call


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────
@dataclass
class DedupResult:
    """Result of a dedup check."""
    is_duplicate: bool
    normalized_key: str
    last_processed: Optional[datetime] = None
    matched_article_id: Optional[int] = None


def is_already_processed(region: str, query: str) -> DedupResult:
    """Check if a topic has been processed in the last 7 days (in-memory check).

    This is the FAST path used during trend detection. For DB-backed
    confirmation, use check_and_record().

    Args:
        region: Region key (e.g. "pakistan")
        query:  The trending query string

    Returns:
        DedupResult with is_duplicate=True if the topic was processed recently.
    """
    _ensure_memory_cache_loaded(region)
    norm = normalize_keyword(query)
    if not norm:
        return DedupResult(is_duplicate=False, normalized_key="")

    with _memory_cache_lock:
        region_cache = _memory_cache.get(region, {})
        last = region_cache.get(norm)
        if last is None:
            return DedupResult(is_duplicate=False, normalized_key=norm)

        # Check if within rolling window
        cutoff = datetime.utcnow() - timedelta(days=ROLLING_WINDOW_DAYS)
        if last < cutoff:
            return DedupResult(is_duplicate=False, normalized_key=norm)

        return DedupResult(
            is_duplicate=True,
            normalized_key=norm,
            last_processed=last,
        )


def get_skip_set_for_region(region: str) -> set[str]:
    """Return the set of normalized keywords to skip for a region.

    Used by trend_detector.detect_top_trend() to skip already-processed topics.
    """
    _ensure_memory_cache_loaded(region)
    cutoff = datetime.utcnow() - timedelta(days=ROLLING_WINDOW_DAYS)
    with _memory_cache_lock:
        region_cache = _memory_cache.get(region, {})
        return {k for k, v in region_cache.items() if v >= cutoff}


def get_skip_sets_all_regions() -> dict[str, set[str]]:
    """Return skip sets for all regions at once."""
    from region_config import REGIONS
    return {r.key: get_skip_set_for_region(r.key) for r in REGIONS}


async def record_processed(
    region: str,
    query: str,
    *,
    article_id: Optional[int] = None,
    cycle_id: str = "",
) -> None:
    """Record a topic as processed (call AFTER an article draft is saved).

    Updates BOTH the database and the in-memory cache.

    Args:
        region:     Region key
        query:      Original query string
        article_id: ID of the produced Article (if any — None if skipped)
        cycle_id:   Which 3-hour cycle produced this
    """
    norm = normalize_keyword(query)
    if not norm:
        return

    # Update in-memory cache immediately
    with _memory_cache_lock:
        _memory_cache.setdefault(region, {})[norm] = datetime.utcnow()

    # Update DB (durable)
    try:
        from database import AsyncSessionLocal, ProcessedTrendKeyword
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            # Check if this normalized keyword already exists for this region
            existing = await session.execute(
                select(ProcessedTrendKeyword)
                .where(ProcessedTrendKeyword.region == region)
                .where(ProcessedTrendKeyword.keyword_norm == norm)
                .limit(1)
            )
            row = existing.scalar_one_or_none()
            if row:
                # Update existing row's timestamp + article_id
                row.processed_at = datetime.utcnow()
                row.keyword_raw = query  # keep the most recent raw form
                if article_id is not None:
                    row.article_id = article_id
                row.cycle_id = cycle_id or row.cycle_id
            else:
                # Insert new row
                session.add(ProcessedTrendKeyword(
                    region=region,
                    keyword_norm=norm,
                    keyword_raw=query,
                    article_id=article_id,
                    cycle_id=cycle_id,
                    processed_at=datetime.utcnow(),
                ))
            await session.commit()
        logger.debug(
            f"[DedupEngine] Recorded: region={region} key='{norm[:40]}' "
            f"article_id={article_id} cycle={cycle_id[:8]}"
        )
    except Exception as e:
        logger.warning(f"[DedupEngine] Could not record to DB: {e}")


async def cleanup_old_entries() -> int:
    """Delete dedup entries older than ROLLING_WINDOW_DAYS.

    Called by the nightly cleanup job. Returns the number of rows deleted.
    """
    try:
        from database import AsyncSessionLocal, ProcessedTrendKeyword
        from sqlalchemy import text

        cutoff = datetime.utcnow() - timedelta(days=ROLLING_WINDOW_DAYS)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("DELETE FROM processed_trend_keywords WHERE processed_at < :c"),
                {"c": cutoff},
            )
            await session.commit()
            deleted = result.rowcount or 0
            if deleted:
                logger.info(f"[DedupEngine] Cleaned up {deleted} entries older than {ROLLING_WINDOW_DAYS}d")
            return deleted
    except Exception as e:
        logger.warning(f"[DedupEngine] Cleanup failed: {e}")
        return 0


# ─────────────────────────────────────────────────────────────
# CLI for testing
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    test_queries = [
        "US-Canada Trade Tariffs: Latest Updates",
        "Pakistan Election Results 2024",
        "usa canada trade tariffs update",  # should match #1 after normalization
        "Germany Chancellor Visits India",
        "UK Prime Minister Resigns",
    ]
    print("=" * 60)
    print("SFAAM Dedup Engine — Keyword Normalization Test")
    print("=" * 60)
    for q in test_queries:
        norm = normalize_keyword(q)
        print(f"  '{q}'")
        print(f"  → '{norm}'")
        print()

    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        region = sys.argv[2] if len(sys.argv) > 2 else "world"
        for q in test_queries:
            r = is_already_processed(region, q)
            print(f"  region={region} query='{q[:40]}' → dup={r.is_duplicate}")
