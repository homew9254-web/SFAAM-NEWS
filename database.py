"""
database.py - SFAAM NEWS V12 (Async PostgreSQL Enterprise)
- Async SQLAlchemy 2.0 + asyncpg (PostgreSQL primary)
- Auto-fallback to aiosqlite for local dev / when DATABASE_URL=sqlite
- Async connection pool with pre-ping + statement timeout
- Composite indexes for common query patterns (region+date, slug, date desc)
- article_hash + slug for deduplication and SEO-friendly URLs
- ContactMessage + Subscriber tables for admin features
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import AsyncGenerator

from dotenv import load_dotenv
from sqlalchemy import Column, Integer, String, Text, DateTime, Index, event, text
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

load_dotenv()
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./sfaam.db")
# V24: Production safety — in PROD mode, REFUSE to silently fall back to SQLite.
# A missing/invalid DATABASE_URL in production means data loss on container
# restart. Fail fast instead. In dev, fallback is still allowed for convenience.
PROD_MODE = os.getenv("ENV", "development").lower() in ("production", "prod")

# V18: Defensive — if DATABASE_URL has an unsupported scheme (e.g. leaked from a
# dev machine's env like "file:/..."), fall back to SQLite instead of crashing.
# This does NOT affect Railway Postgres (which uses "postgres://" or "postgresql://").
if not (DATABASE_URL.startswith("postgres") or DATABASE_URL.startswith("sqlite")):
    if PROD_MODE:
        raise RuntimeError(
            f"FATAL: DATABASE_URL has unsupported scheme '{DATABASE_URL[:30]}' "
            f"in PRODUCTION mode. Refusing to fall back to SQLite (would lose "
            f"data on restart). Set DATABASE_URL=postgresql://... in your env."
        )
    DATABASE_URL = "sqlite+aiosqlite:///./sfaam.db"

# V24: If using SQLite in production, refuse to start (unless explicitly overridden)
if PROD_MODE and DATABASE_URL.startswith("sqlite") and os.getenv("ALLOW_PROD_SQLITE", "") != "1":
    raise RuntimeError(
        f"FATAL: SQLite detected in PRODUCTION mode. "
        f"Set DATABASE_URL to a PostgreSQL connection string, or set "
        f"ALLOW_PROD_SQLITE=1 to acknowledge data-loss risk and proceed."
    )
DELETE_AFTER = int(os.getenv("DELETE_AFTER_DAYS", "30"))

# Detect dialect so we can apply dialect-specific pragmas / settings
IS_SQLITE = DATABASE_URL.startswith("sqlite")
IS_POSTGRES = DATABASE_URL.startswith("postgres")


# ── Engine ──
# pool_pre_ping  → silently recycle dead connections (Postgres restarts)
# pool_size      → 10 conns (good for Railway's small instances)
# max_overflow   → +20 burst conns under load
# pool_timeout   → 30s wait for a free conn before failing
# pool_recycle   → recycle every 1800s (defeats idle timeouts on managed PG)
engine_kwargs = {
    "echo": False,
    "pool_pre_ping": True,
}

if IS_POSTGRES:
    engine_kwargs.update(
        pool_size=int(os.getenv("PG_POOL_SIZE", "10")),
        max_overflow=int(os.getenv("PG_MAX_OVERFLOW", "20")),
        pool_timeout=30,
        pool_recycle=1800,
    )
    # asyncpg expects postgresql+asyncpg:// scheme
    if DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )
    elif DATABASE_URL.startswith("postgres://"):
        # Railway often hands out postgres:// → convert
        DATABASE_URL = DATABASE_URL.replace(
            "postgres://", "postgresql+asyncpg://", 1
        )

    # FIX: asyncpg does NOT support the sslmode query parameter — it uses
    # connect_args={"ssl": ...} instead. Strip sslmode from the URL and
    # translate it to the asyncpg ssl kwarg to avoid:
    #   TypeError: connect() got an unexpected keyword argument 'sslmode'
    try:
        from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
        _parsed = urlparse(DATABASE_URL)
        _qs = parse_qs(_parsed.query, keep_blank_values=True)
        _sslmode = _qs.pop("sslmode", [None])[0]
        # Rebuild URL without sslmode
        _new_query = urlencode({k: v[0] for k, v in _qs.items()})
        DATABASE_URL = urlunparse(_parsed._replace(query=_new_query))
        # Map psycopg2-style sslmode values to asyncpg ssl kwarg
        if _sslmode in ("require", "verify-ca", "verify-full"):
            engine_kwargs.setdefault("connect_args", {})["ssl"] = True
        elif _sslmode in ("disable", "disabled", "allow", "prefer"):
            engine_kwargs.setdefault("connect_args", {})["ssl"] = False
        # if sslmode not present, let asyncpg decide (default is no SSL)
    except Exception as _ssl_err:
        logger.warning(f"[DB] sslmode fixup failed (non-fatal): {_ssl_err}")
elif IS_SQLITE:
    if DATABASE_URL.startswith("sqlite:///"):
        DATABASE_URL = DATABASE_URL.replace(
            "sqlite:///", "sqlite+aiosqlite:///", 1
        )
    # SQLite-specific args only valid for the aiosqlite dialect
    engine_kwargs["connect_args"] = {"timeout": 15, "check_same_thread": False}
else:
    raise RuntimeError(
        f"Unsupported DATABASE_URL scheme. Use postgres:// or sqlite:///. Got: {DATABASE_URL}"
    )

engine = create_async_engine(DATABASE_URL, **engine_kwargs)


# Apply SQLite WAL pragmas on every fresh connection — WAL lets readers and
# the background scraper writer coexist without "database is locked" errors.
if IS_SQLITE:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA busy_timeout=15000;")
        cur.close()


# Async session factory — fastapi Depends() will pull from this
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


# ── Models ──
class Article(Base):
    __tablename__ = "articles"

    id           = Column(Integer, primary_key=True, index=True)
    title        = Column(String(500), nullable=False, index=True)
    # V31.1: Normalized title for uniqueness checks (lowercase, no punctuation,
    # accents stripped). Auto-populated by app code on insert. Indexed for fast
    # duplicate detection. NOT unique-constrained (we use app-level check +
    # suffix appending in title_uniqueness.ensure_unique_title) to avoid
    # breaking existing data with duplicates.
    title_norm   = Column(String(500), nullable=True, index=True)
    slug         = Column(String(600), nullable=True, index=True)
    original_url = Column(String(1000), unique=True, nullable=False)
    ai_content   = Column(Text, nullable=False)
    summary      = Column(Text, nullable=True)
    image_url    = Column(String(1000), nullable=True)
    region       = Column(String(50), nullable=False, index=True)
    meta_desc    = Column(String(300), nullable=True)
    keywords     = Column(String(500), nullable=True)
    views        = Column(Integer, default=0)
    date         = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    article_hash = Column(String(64), nullable=True, index=True)
    # V18: Wikipedia-killer features
    tldr_summary     = Column(Text, nullable=True)        # AI 3-bullet Quick Summary
    fact_check_status = Column(String(30), default="under_review", index=True)  # verified | under_review | community_fact_checked
    audio_url        = Column(String(1000), nullable=True)  # TTS audio path
    audio_status     = Column(String(20), default="pending")  # pending | processing | ready | failed
    # V21: New Wikipedia-rival features (stored as JSON strings)
    timeline_data    = Column(Text, nullable=True)       # JSON: [{year, title, description, image}]
    myths_facts      = Column(Text, nullable=True)       # JSON: [{myth, fact}]
    is_live          = Column(Integer, default=0)        # 1 if live event tracker active
    live_updates     = Column(Text, nullable=True)       # JSON: [{time, text, status}]
    # V24: Draft mode + quality control
    status           = Column(String(20), default="published", index=True)  # published | draft | pending_review | rejected
    quality_score    = Column(Text, nullable=True)       # JSON: full QualityScore breakdown
    source_type      = Column(String(20), default="rss") # rss | google_search | manual | trends
    search_keyword   = Column(String(300), nullable=True) # if source_type=google_search, the keyword used

    # V26 (Trends): Zero-hallucination factual content engine fields
    is_trends        = Column(Integer, default=0, index=True)         # 1 if generated by Trends pipeline
    trend_query      = Column(String(500), nullable=True, index=True) # original Google trend query
    fact_sources     = Column(Text, nullable=True)        # JSON: list of {url, domain, title, snippet}
    verified_facts   = Column(Text, nullable=True)        # JSON: list of verified fact strings (cross-checked across 1+ sources; see fact_verifier.MIN_SOURCES_PER_FACT)
    source_count     = Column(Integer, default=0)         # number of distinct sources used
    word_count       = Column(Integer, default=0)         # AI-generated content word count (for dynamic length audit)
    references_data  = Column(Text, nullable=True)        # JSON: list of reference URLs (Wikipedia-style citations)
    pipeline_version = Column(String(20), default="v26")  # which pipeline produced this article

    # V30 (SFAAM Automated News Engine / TRD v1.0): New fields per TRD Section 4
    # FIELD 5: History & Contextual Background section (separate from main body)
    history_context   = Column(Text, nullable=True)        # rich text: 5-10 year historical context
    # FIELD 3: Audio Player Placeholder token (frontend hook)
    audio_player_token = Column(String(100), nullable=True)  # token like {{AUDIO_PLAYER:abc123}}
    # TRD Section 3 Step C: word count tier metadata
    word_count_tier   = Column(String(20), nullable=True)  # small | medium | large
    word_count_target = Column(Integer, default=0)         # target word count (midpoint of tier)
    # TRD Section 3 Step A: trend ranking metadata (for admin dashboard display)
    trend_score       = Column(Integer, default=0)         # 0-100 ranking score from Step A
    cross_source_count = Column(Integer, default=0)        # how many aggregators mentioned the trend
    # TRD Section 2: per-region LLM provider tracking
    llm_provider      = Column(String(20), nullable=True)  # groq | gemini | fallback
    llm_model         = Column(String(100), nullable=True) # which specific model was used
    # TRD Section 3 Step B: fact extraction audit
    raw_facts_count   = Column(Integer, default=0)         # facts extracted by LLM before verification
    dropped_facts_count = Column(Integer, default=0)       # facts dropped by safety shield
    fact_extraction_elapsed_s = Column(Integer, default=0) # time spent on fact extraction (seconds)
    article_generation_elapsed_s = Column(Integer, default=0)  # time spent on article generation (seconds)

    __table_args__ = (
        Index("idx_region_date", "region", "date"),
        Index("idx_slug", "slug"),
        Index("idx_date_desc", date.desc()),
        Index("idx_fact_check", "fact_check_status"),
        Index("idx_status", "status"),
        Index("idx_status_date", "status", date.desc()),
    )

    @property
    def reading_time(self) -> int:
        """Estimated reading time in minutes, ~200 words/min."""
        words = len(self.ai_content.split()) if self.ai_content else 0
        return max(1, round(words / 200))


class ProcessedURL(Base):
    __tablename__ = "processed_urls"
    id       = Column(Integer, primary_key=True)
    url      = Column(String(1000), unique=True, nullable=False)
    saved_at = Column(DateTime, default=datetime.utcnow)


# ── V30 (SFAAM Automated News Engine / TRD v1.0): Deduplication Engine ──
# Per TRD Section 6: "The system must keep a rolling 7-day log of
# processed keywords/topics in the database."
class ProcessedTrendKeyword(Base):
    """Rolling 7-day log of processed trending topics per region.

    Used by dedup_engine.py to skip topics that have already been
    written about in the last 7 days. Indexed on (region, processed_at)
    for fast pruning of expired entries.
    """
    __tablename__ = "processed_trend_keywords"
    id            = Column(Integer, primary_key=True, index=True)
    region        = Column(String(50), nullable=False, index=True)  # world | usa | uk | pakistan | india | germany
    keyword_norm  = Column(String(300), nullable=False, index=True)  # normalized topic key (stopwords removed, sorted)
    keyword_raw   = Column(String(500), nullable=True)               # original trending query string
    article_id    = Column(Integer, nullable=True, index=True)        # FK -> articles.id (if draft produced)
    cycle_id      = Column(String(40), nullable=True)                # UUID of the 3-hour cycle
    processed_at  = Column(DateTime, default=datetime.utcnow, index=True)
    __table_args__ = (
        Index("idx_dedup_region_norm", "region", "keyword_norm"),
        Index("idx_dedup_processed", "processed_at"),
    )


# ── V30 (SFAAM Automated News Engine / TRD v1.0): Pipeline Cycle Audit Log ──
# Records every 3-hour cycle run for admin visibility and debugging.
class EngineCycleLog(Base):
    """One row per 3-hour cycle. Tracks per-region outcomes for the
    admin dashboard's 'Engine Activity' view.
    """
    __tablename__ = "engine_cycle_logs"
    id            = Column(Integer, primary_key=True, index=True)
    cycle_id      = Column(String(40), nullable=False, index=True)   # UUID per 3-hour cycle
    started_at    = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at  = Column(DateTime, nullable=True)
    regions_processed = Column(Integer, default=0)                    # how many of the 6 regions were attempted
    drafts_produced   = Column(Integer, default=0)                    # how many drafts saved
    drafts_failed     = Column(Integer, default=0)                    # how many regions failed to produce a draft
    skipped_duplicates = Column(Integer, default=0)                   # trends skipped due to dedup
    total_elapsed_s   = Column(Integer, default=0)
    status        = Column(String(20), default="running")             # running | completed | failed | partial
    error         = Column(Text, nullable=True)
    # Per-region summary as JSON: [{region, query, status, sources, facts, article_id, error}]
    region_summary = Column(Text, nullable=True)
    __table_args__ = (
        Index("idx_cycle_started", "started_at"),
    )


# ── V26 (Trends): Zero-Hallucination Content Engine ──
class TrendQuery(Base):
    """Tracks every trending query fetched from Google Trends RSS.
    One row per (query, fetch_cycle). The 6-hourly scheduler writes 7 rows
    per cycle, so the admin can see what was searched and whether an
    article draft was produced."""
    __tablename__ = "trend_queries"
    id            = Column(Integer, primary_key=True, index=True)
    query         = Column(String(500), nullable=False, index=True)
    fetched_at    = Column(DateTime, default=datetime.utcnow, index=True)
    cycle_id      = Column(String(40), nullable=True, index=True)  # UUID per 6-hour cycle
    country       = Column(String(10), default="world")           # trends region
    article_id    = Column(Integer, nullable=True, index=True)     # FK -> articles.id (if a draft was produced)
    sources_found = Column(Integer, default=0)                     # how many authoritative sources returned
    facts_verified = Column(Integer, default=0)                    # how many facts passed cross-verification
    status        = Column(String(30), default="pending")          # pending | researching | verified | drafted | failed
    error         = Column(Text, nullable=True)
    __table_args__ = (
        Index("idx_trend_cycle", "cycle_id"),
        Index("idx_trend_query_date", "query", "fetched_at"),
    )


class Subscriber(Base):
    __tablename__ = "subscribers"
    id            = Column(Integer, primary_key=True)
    email         = Column(String(255), unique=True, nullable=False, index=True)
    subscribed_at = Column(DateTime, default=datetime.utcnow)
    active        = Column(Integer, default=1)


class ContactMessage(Base):
    __tablename__ = "contact_messages"
    id      = Column(Integer, primary_key=True)
    name    = Column(String(200), nullable=False)
    email   = Column(String(255), nullable=False, index=True)
    subject = Column(String(300), nullable=True)
    message = Column(Text, nullable=False)
    sent_at = Column(DateTime, default=datetime.utcnow)
    read    = Column(Integer, default=0)


# ── V8: Article Engagement (Likes + Comments) ──
class ArticleLike(Base):
    """Tracks per-article likes. Uses a per-browser fingerprint (sent from
    client localStorage) so we don't need user accounts but can still
    prevent one user from liking the same article thousands of times."""
    __tablename__ = "article_likes"
    id            = Column(Integer, primary_key=True)
    article_id    = Column(Integer, nullable=False, index=True)
    user_fingerprint = Column(String(64), nullable=False)  # IP or random browser id
    created_at    = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_article_fp", "article_id", "user_fingerprint", unique=True),
    )


class ArticleComment(Base):
    """Reader comments on articles. No login required — readers can post
    with just a name. Comments are stored server-side and shown to everyone."""
    __tablename__ = "article_comments"
    id         = Column(Integer, primary_key=True)
    article_id = Column(Integer, nullable=False, index=True)
    name       = Column(String(80), nullable=False)
    comment    = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# ── V21: Interactive Polls (one-click voting) ──
class Poll(Base):
    """Article-attached polls. Admin creates them; users vote with one tap.
    A user (per fingerprint) can vote only once per poll."""
    __tablename__ = "polls"
    id         = Column(Integer, primary_key=True)
    article_id = Column(Integer, nullable=False, index=True)
    question   = Column(String(500), nullable=False)
    # Comma-separated options, e.g. "Yes,No,Maybe"
    options    = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active  = Column(Integer, default=1)


class PollVote(Base):
    """Records a single vote on a poll. Dedupes by user_fingerprint."""
    __tablename__ = "poll_votes"
    id              = Column(Integer, primary_key=True)
    poll_id         = Column(Integer, nullable=False, index=True)
    option_index    = Column(Integer, nullable=False)  # 0-based index into Poll.options
    user_fingerprint = Column(String(64), nullable=False)
    voted_at        = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_poll_fp", "poll_id", "user_fingerprint", unique=True),
    )


# ── V21: Quizzes (3-question end-of-article engagement) ──
class Quiz(Base):
    """Article-attached quiz with multiple questions."""
    __tablename__ = "quizzes"
    id         = Column(Integer, primary_key=True)
    article_id = Column(Integer, nullable=False, index=True)
    title      = Column(String(300), nullable=False)
    # JSON: [{question, options:[...], correct_index}, ...]
    questions  = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active  = Column(Integer, default=1)


# ── Async Dependency for FastAPI ──
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async session. Rolls back on exception, always closes."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create tables on app startup. Idempotent.
    V18: Also auto-migrates existing tables by adding missing columns (ALTER TABLE)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # V18: Auto-migration — add new columns to existing tables if missing.
    # This is necessary because Railway Postgres already has the articles table;
    # create_all only creates missing tables, not missing columns.
    if IS_POSTGRES:
        async with engine.begin() as conn:
            # Check which columns exist on articles table
            result = await conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='articles'"
            ))
            existing_cols = {r[0] for r in result.fetchall()}
            new_cols = {
                "tldr_summary": "TEXT",
                "fact_check_status": "VARCHAR(30) DEFAULT 'under_review'",
                "audio_url": "VARCHAR(1000)",
                "audio_status": "VARCHAR(20) DEFAULT 'pending'",
                # V21: New Wikipedia-rival feature columns
                "timeline_data": "TEXT",
                "myths_facts": "TEXT",
                "is_live": "INTEGER DEFAULT 0",
                "live_updates": "TEXT",
                # V24: Draft mode + quality control + google-search source
                "status": "VARCHAR(20) DEFAULT 'published'",
                "quality_score": "TEXT",
                "source_type": "VARCHAR(20) DEFAULT 'rss'",
                "search_keyword": "VARCHAR(300)",
                # V26 (Trends): Zero-hallucination content engine
                "is_trends": "INTEGER DEFAULT 0",
                "trend_query": "VARCHAR(500)",
                "fact_sources": "TEXT",
                "verified_facts": "TEXT",
                "source_count": "INTEGER DEFAULT 0",
                "word_count": "INTEGER DEFAULT 0",
                "references_data": "TEXT",
                "pipeline_version": "VARCHAR(20) DEFAULT 'v26'",
                # V30 (SFAAM Automated News Engine / TRD v1.0)
                "history_context": "TEXT",
                "audio_player_token": "VARCHAR(100)",
                "word_count_tier": "VARCHAR(20)",
                "word_count_target": "INTEGER DEFAULT 0",
                "trend_score": "INTEGER DEFAULT 0",
                "cross_source_count": "INTEGER DEFAULT 0",
                "llm_provider": "VARCHAR(20)",
                "llm_model": "VARCHAR(100)",
                "raw_facts_count": "INTEGER DEFAULT 0",
                "dropped_facts_count": "INTEGER DEFAULT 0",
                "fact_extraction_elapsed_s": "INTEGER DEFAULT 0",
                "article_generation_elapsed_s": "INTEGER DEFAULT 0",
            }
            for col, coltype in new_cols.items():
                if col not in existing_cols:
                    logger.info(f"[V18 Migration] Adding column articles.{col} ({coltype})")
                    try:
                        await conn.execute(text(
                            f"ALTER TABLE articles ADD COLUMN {col} {coltype}"
                        ))
                        if col == "fact_check_status":
                            await conn.execute(text(
                                "CREATE INDEX IF NOT EXISTS idx_fact_check ON articles (fact_check_status)"
                            ))
                        if col == "status":
                            await conn.execute(text(
                                "CREATE INDEX IF NOT EXISTS idx_status ON articles (status)"
                            ))
                            await conn.execute(text(
                                "CREATE INDEX IF NOT EXISTS idx_status_date ON articles (status, date DESC)"
                            ))
                        if col == "is_trends":
                            await conn.execute(text(
                                "CREATE INDEX IF NOT EXISTS idx_is_trends ON articles (is_trends)"
                            ))
                        if col == "trend_query":
                            await conn.execute(text(
                                "CREATE INDEX IF NOT EXISTS idx_trend_query ON articles (trend_query)"
                            ))
                    except Exception as e:
                        logger.warning(f"[V18 Migration] Could not add {col}: {e}")

            # V29: Backfill NULL status rows to 'published' — pre-V24 articles
            # that were saved before the status column existed have NULL status,
            # which causes them to be hidden from public API queries.
            try:
                result = await conn.execute(text(
                    "SELECT COUNT(*) FROM articles WHERE status IS NULL"
                ))
                null_count = result.scalar_one()
                if null_count > 0:
                    await conn.execute(text(
                        "UPDATE articles SET status = 'published' WHERE status IS NULL"
                    ))
                    logger.info(f"[V29 Migration] Updated {null_count} articles with NULL status → 'published'")
            except Exception as e:
                logger.warning(f"[V29 Migration] Could not backfill NULL status: {e}")

            # V30 (SFAAM Automated News Engine / TRD v1.0): Backfill pipeline_version
            # on existing trends articles so admin filters work consistently.
            try:
                result = await conn.execute(text(
                    "SELECT COUNT(*) FROM articles WHERE is_trends = 1 AND pipeline_version IS NULL"
                ))
                null_count = result.scalar_one()
                if null_count > 0:
                    await conn.execute(text(
                        "UPDATE articles SET pipeline_version = 'v26' "
                        "WHERE is_trends = 1 AND pipeline_version IS NULL"
                    ))
                    logger.info(f"[V30 Migration] Backfilled pipeline_version on {null_count} trends articles")
            except Exception as e:
                logger.warning(f"[V30 Migration] Could not backfill pipeline_version: {e}")

            # V30 Bug #17 FIX: Add UNIQUE constraint on article_hash to prevent
            # duplicate articles if the engine ever runs concurrently (e.g.
            # manual trigger + scheduled trigger). The in-memory _engine_status
            # flag prevents this in normal operation, but DB-level uniqueness
            # is the last line of defense. Safe to attempt multiple times —
            # if the constraint already exists, the IF NOT EXISTS-equivalent
            # check will skip it (Postgres doesn't support IF NOT EXISTS on
            # ALTER TABLE ADD CONSTRAINT, so we check information_schema first).
            try:
                # Check if the unique index already exists
                result = await conn.execute(text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'articles' AND indexname = 'idx_article_hash_unique'"
                ))
                if result.rowcount == 0:
                    # First, check for existing duplicates — if any exist,
                    # we can't add the unique constraint (would fail).
                    dup_check = await conn.execute(text(
                        "SELECT article_hash, COUNT(*) as c FROM articles "
                        "WHERE article_hash IS NOT NULL "
                        "GROUP BY article_hash HAVING COUNT(*) > 1 LIMIT 1"
                    ))
                    if dup_check.rowcount == 0:
                        await conn.execute(text(
                            "CREATE UNIQUE INDEX idx_article_hash_unique "
                            "ON articles (article_hash)"
                        ))
                        logger.info("[V30 Migration] Added UNIQUE index on articles.article_hash")
                    else:
                        logger.warning(
                            "[V30 Migration] SKIPPED unique index on article_hash — "
                            "duplicate hashes exist in DB. Run cleanup first."
                        )
            except Exception as e:
                # Non-fatal — the in-memory running flag is the primary guard
                logger.warning(f"[V30 Migration] Could not add unique index on article_hash: {e}")

            # V31.1 Migration: Add title_norm column + index for title uniqueness
            # checks. Backfill existing rows with normalized titles.
            try:
                if "title_norm" not in existing_cols:
                    logger.info("[V31.1 Migration] Adding column articles.title_norm (VARCHAR(500))")
                    await conn.execute(text(
                        "ALTER TABLE articles ADD COLUMN title_norm VARCHAR(500)"
                    ))
                    await conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS idx_title_norm ON articles (title_norm)"
                    ))
                    logger.info("[V31.1 Migration] Added title_norm column + index")
            except Exception as e:
                logger.warning(f"[V31.1 Migration] Could not add title_norm column: {e}")

            # V31.1: Backfill title_norm for existing rows (those with NULL title_norm).
            # Done in batches to avoid locking the table on large DBs.
            try:
                from title_uniqueness import normalize_title
                # SQLite doesn't support LIMIT on UPDATE, so we use a subquery.
                # Batch size: 500 rows at a time.
                BATCH_SIZE = 500
                total_updated = 0
                while True:
                    if IS_SQLITE:
                        rows = await conn.execute(text(
                            "SELECT id, title FROM articles "
                            "WHERE title_norm IS NULL LIMIT :batch"
                        ), {"batch": BATCH_SIZE})
                    else:
                        rows = await conn.execute(text(
                            "SELECT id, title FROM articles "
                            "WHERE title_norm IS NULL LIMIT :batch"
                        ), {"batch": BATCH_SIZE})
                    items = rows.fetchall()
                    if not items:
                        break
                    for art_id, art_title in items:
                        norm = normalize_title(art_title or "")
                        if norm:
                            await conn.execute(text(
                                "UPDATE articles SET title_norm = :norm WHERE id = :id"
                            ), {"norm": norm[:500], "id": art_id})
                    total_updated += len(items)
                    if len(items) < BATCH_SIZE:
                        break
                if total_updated > 0:
                    logger.info(
                        f"[V31.1 Migration] Backfilled title_norm on {total_updated} articles"
                    )
            except Exception as e:
                logger.warning(f"[V31.1 Migration] Could not backfill title_norm: {e}")

    # Optional PG tuning — only no-op if not Postgres
    if IS_POSTGRES:
        async with engine.begin() as conn:
            await conn.execute(text("SET statement_timeout = 30000;"))  # 30s
    print("[SFAAM NEWS V26] Async database ready.")


# ════════════════════════════════════════════════════════════
#  V23: FULL-TEXT SEARCH INITIALIZATION
#  - SQLite: FTS5 virtual table + triggers to keep it in sync
#  - PostgreSQL: GIN tsvector index on (title + summary + keywords)
#  Both approaches give milliseconds search across millions of rows.
# ════════════════════════════════════════════════════════════
async def init_fts() -> None:
    """Create full-text search index. Safe to call repeatedly (idempotent)."""
    async with engine.begin() as conn:
        if IS_SQLITE:
            # ── SQLite FTS5 ──
            # 1. Create virtual table if not exists
            await conn.execute(text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5("
                "title, summary, keywords, content='articles', content_rowid='id'"
                ");"
            ))
            # 2. AFTER INSERT trigger — populate FTS row
            await conn.execute(text(
                "CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN "
                "  INSERT INTO articles_fts(rowid, title, summary, keywords) "
                "  VALUES (new.id, new.title, new.summary, new.keywords); "
                "END;"
            ))
            # 3. AFTER DELETE trigger — clear FTS row
            await conn.execute(text(
                "CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN "
                "  INSERT INTO articles_fts(articles_fts, rowid, title, summary, keywords) "
                "  VALUES('delete', old.id, old.title, old.summary, old.keywords); "
                "END;"
            ))
            # 4. AFTER UPDATE trigger — keep FTS in sync
            await conn.execute(text(
                "CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN "
                "  INSERT INTO articles_fts(articles_fts, rowid, title, summary, keywords) "
                "  VALUES('delete', old.id, old.title, old.summary, old.keywords); "
                "  INSERT INTO articles_fts(rowid, title, summary, keywords) "
                "  VALUES (new.id, new.title, new.summary, new.keywords); "
                "END;"
            ))
            # 5. Backfill: any existing rows not yet in FTS
            try:
                await conn.execute(text(
                    "INSERT INTO articles_fts(articles_fts) VALUES('rebuild');"
                ))
            except Exception as e:
                logger.debug(f"FTS rebuild skipped (may already be in sync): {e}")
            logger.info("[V23 FTS] SQLite FTS5 index ready (articles_fts)")

        elif IS_POSTGRES:
            # ── PostgreSQL GIN tsvector ──
            # 1. Functional index on the concatenated tsvector
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_articles_fts "
                "ON articles USING GIN ("
                "  to_tsvector('english', "
                "    coalesce(title,'') || ' ' || coalesce(summary,'') || ' ' || coalesce(keywords,'')"
                "  )"
                ");"
            ))
            # 2. Trigram index for ILIKE-style prefix searches (faster fallback)
            try:
                await conn.execute(text(
                    "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
                ))
                await conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_articles_title_trgm "
                    "ON articles USING GIN (title gin_trgm_ops);"
                ))
            except Exception as e:
                logger.debug(f"pg_trgm extension skipped (may lack permission): {e}")
            logger.info("[V23 FTS] PostgreSQL GIN tsvector index ready")


async def fts_reindex() -> None:
    """Force a full rebuild of the FTS index. Use sparingly (e.g. after
    a large data migration). Safe to call repeatedly."""
    async with engine.begin() as conn:
        if IS_SQLITE:
            try:
                await conn.execute(text(
                    "INSERT INTO articles_fts(articles_fts) VALUES('rebuild');"
                ))
                logger.info("[V23 FTS] SQLite FTS index rebuilt")
            except Exception as e:
                logger.warning(f"[V23 FTS] rebuild failed: {e}")
        elif IS_POSTGRES:
            try:
                await conn.execute(text("REINDEX INDEX idx_articles_fts;"))
                logger.info("[V23 FTS] PostgreSQL FTS index reindexed")
            except Exception as e:
                logger.warning(f"[V23 FTS] reindex failed: {e}")


async def delete_old_articles() -> None:
    """Background cleanup of articles older than DELETE_AFTER days."""
    async with AsyncSessionLocal() as db:
        try:
            cutoff = datetime.utcnow() - timedelta(days=DELETE_AFTER)
            # Pull old URLs first so we can clean ProcessedURL rows too
            result = await db.execute(
                text("SELECT original_url FROM articles WHERE date < :c"),
                {"c": cutoff},
            )
            old_urls = [r[0] for r in result.fetchall()]
            # Delete old articles
            await db.execute(text("DELETE FROM articles WHERE date < :c"), {"c": cutoff})
            # Also clean up processed_urls (V12: works for both PG and SQLite)
            if old_urls:
                if IS_POSTGRES:
                    await db.execute(
                        text("DELETE FROM processed_urls WHERE url = ANY(:u)"),
                        {"u": old_urls},
                    )
                else:
                    for url in old_urls:
                        await db.execute(
                            text("DELETE FROM processed_urls WHERE url = :u"),
                            {"u": url},
                        )
            await db.commit()
            if old_urls:
                print(f"[SFAAM NEWS V26] {len(old_urls)} old articles deleted.")
        except Exception as e:
            await db.rollback()
            print(f"Cleanup error: {e}")
