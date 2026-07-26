"""
scheduler.py - SFAAM NEWS V12 (Async Pipeline)
- APScheduler runs the async pipeline on an IntervalTrigger
- Pipeline: scrape → editor agent → journalist agent → dedupe → save
- Overlap prevention via max_instances=1 + coalesce=True
- Failure tracking with webhook alerts after 3 consecutive failures
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from difflib import SequenceMatcher

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database import AsyncSessionLocal, Article, ProcessedURL, delete_old_articles
from scraper import get_new_articles
from ai_writer import rewrite_article, make_slug, make_article_hash, apply_internal_links

# IMPORTANT: logger must be created BEFORE the try/except below so that
# the except branch can use it without raising NameError.
logger = logging.getLogger(__name__)

# V24: Quality control + draft mode
try:
    from quality_control import evaluate_article, quality_score_to_dict, semantic_dedup
    QC_ENABLED = True
except Exception as _qc_err:
    QC_ENABLED = False
    logger.warning(f"Quality control disabled: {_qc_err}")

# V24: Draft mode — articles saved as "draft" instead of "published"
# Admin must publish manually via /api/admin/articles/{id}/publish
DRAFT_MODE = os.getenv("DRAFT_MODE", "1") == "1"

INTERVAL = int(os.getenv("SCRAPE_INTERVAL", "60"))
ARTICLE_DELAY = float(os.getenv("ARTICLE_DELAY", "3"))

# ────────────────────────────────────────────────────────────
#  V24: LEADER ELECTION (Redis-based)
#  Prevents multiple instances from running the scheduler in parallel
#  when the app is scaled horizontally (e.g. Railway replicas).
#  Each instance tries to acquire a 60-second lock; only the holder
#  runs the pipeline. If the holder crashes, the lock auto-expires
#  and another instance picks it up.
# ────────────────────────────────────────────────────────────
import redis as _redis_lib

# FIX: main event loop reference injected by main.py lifespan before scheduler
# starts. Using run_coroutine_threadsafe avoids the "Future attached to a
# different loop" errors that occur when asyncio.run() creates a second loop
# in an APScheduler background thread while asyncpg is bound to the first loop.
MAIN_LOOP: asyncio.AbstractEventLoop | None = None

REDIS_URL = os.getenv("REDIS_URL", "")
LEADER_KEY = "sfaam:scheduler:leader"
# V32.1 BUGFIX: Raised LEADER_TTL from 90s to 600s (10 minutes).
# The full news pipeline (scrape → extract facts → verify → write → save)
# takes 30+ minutes per cycle. With a 90s TTL, the leader lock expired
# MID-PIPELINE — another worker could acquire leadership and start a
# PARALLEL pipeline, producing duplicate articles. The renew-on-check
# logic (r.expire at line 89) only fires when _acquire_leadership is
# called, which happens at cycle START, not during the pipeline.
# 600s is long enough to cover the longest single-article generation
# (~120s) plus network jitter, while still allowing failover within
# 10 minutes if the leader truly crashes.
LEADER_TTL = 600  # seconds
INSTANCE_ID = f"inst-{os.getpid()}-{int(time.time())}"


def _get_redis():
    """Lazy Redis connection (only connect if REDIS_URL set)."""
    if not REDIS_URL:
        return None
    try:
        return _redis_lib.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
    except Exception:
        return None


def _acquire_leadership() -> bool:
    """Try to become leader. Returns True if this instance is the leader."""
    r = _get_redis()
    if r is None:
        # No Redis → single-instance mode → always run
        return True
    try:
        # SET NX = only set if not exists; EX = TTL in seconds
        acquired = r.set(LEADER_KEY, INSTANCE_ID, nx=True, ex=LEADER_TTL)
        if acquired:
            logger.info(f"[V24 Leader] Acquired leadership ({INSTANCE_ID})")
            return True
        # We're not leader — but check if the current leader is us (renewal)
        current = r.get(LEADER_KEY)
        if current == INSTANCE_ID:
            r.expire(LEADER_KEY, LEADER_TTL)  # renew
            return True
        return False
    except Exception as e:
        logger.warning(f"[V24 Leader] Redis error, defaulting to single-instance: {e}")
        return True


def _release_leadership() -> None:
    """Release leadership (only if we hold it). Called on shutdown."""
    r = _get_redis()
    if r is None:
        return
    try:
        # Lua script: only delete if value matches our INSTANCE_ID
        # (prevents us from deleting another instance's lock)
        lua = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        r.eval(lua, 1, LEADER_KEY, INSTANCE_ID)
        logger.info(f"[V24 Leader] Released leadership ({INSTANCE_ID})")
    except Exception:
        pass

# Failure tracking
_FAILURE_COUNT = 0
_MAX_FAILURES = 3
# V26 FIX: env var was named WEBHOOK_URL but .env.example defines ALERT_WEBHOOK_URL.
# Read both for backwards compatibility, prefer ALERT_WEBHOOK_URL.
_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "") or os.getenv("WEBHOOK_URL", "")
_pipeline_stats = {"last_run": None, "last_success": 0, "total_articles": 0}

# V12: Expose pipeline result for main.py /api/pipeline-status endpoint
pipeline_result = {"running": False, "last_error": "", "last_run_ts": "", "saved": 0, "failed": 0, "skipped": 0}


def _similarity(a: str, b: str) -> float:
    """0.0-1.0 similarity between two strings."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


async def _is_duplicate(title: str, text: str, existing_articles: list[Article], threshold: float = 0.85) -> bool:
    """Check if article is similar to any existing one.
    V12: Title similarity + content hash.
    V24: ALSO uses semantic (TF-IDF cosine) similarity to catch paraphrased
    duplicates that title-matching misses."""
    new_hash = make_article_hash(title, text)

    # V24: Build list of existing article texts for semantic check
    existing_texts = []
    for article in existing_articles:
        existing_texts.append(article.ai_content or article.summary or article.title)

    # Semantic dedup — catches paraphrased duplicates
    if QC_ENABLED and text:
        try:
            is_sem_dup, max_sim, _ = semantic_dedup(text, existing_texts)
            if is_sem_dup:
                logger.info(f"  Semantic duplicate detected (similarity={max_sim})")
                return True
        except Exception as e:
            logger.debug(f"Semantic dedup failed (falling back to title): {e}")

    # Title similarity (legacy, still useful)
    for article in existing_articles:
        if _similarity(title, article.title) >= threshold:
            return True
        if article.article_hash and new_hash == article.article_hash:
            return True
    return False


def _send_alert(message: str) -> None:
    """Send webhook alert on repeated failures."""
    if not _WEBHOOK_URL:
        return
    try:
        import httpx
        httpx.post(_WEBHOOK_URL, json={
            "text": f"[SFAAM NEWS V26 Alert] {message}",
            "timestamp": time.time(),
        }, timeout=10)
    except Exception as e:
        logger.warning(f"Webhook alert failed: {e}")


async def _run_pipeline_async() -> None:
    """Main async pipeline — fetch, rewrite, dedupe, store."""
    global _FAILURE_COUNT, _pipeline_stats, pipeline_result
    logger.info("=" * 50)
    logger.info("SFAAM NEWS V26 Pipeline Started")
    logger.info("=" * 50)

    pipeline_result["running"] = True
    pipeline_result["last_error"] = ""
    pipeline_result["last_run_ts"] = datetime.utcnow().isoformat()

    saved = failed = skipped = 0
    async with AsyncSessionLocal() as db:
        try:
            # ── V8: Load processed URLs from last 14 days only ──
            # This fixes "stale articles" — the old code loaded ALL processed URLs
            # ever, so even a fresh re-publish of the same story URL would be
            # skipped forever. Limiting to 14 days lets us re-fetch older URLs
            # if they get republished with new content (very common for news).
            cutoff = datetime.utcnow() - timedelta(days=14)
            result = await db.execute(
                select(ProcessedURL.url).where(ProcessedURL.saved_at >= cutoff)
            )
            processed = {r[0] for r in result.fetchall()}

            # ── Load recent articles for dedupe ──
            recent_result = await db.execute(
                select(Article).order_by(Article.date.desc()).limit(50)
            )
            recent_articles = list(recent_result.scalars().all())

            # ── Scrape (async) ──
            articles = await get_new_articles(processed)
            total = len(articles)
            logger.info(f"Processing {total} articles (processed_urls in last 14d: {len(processed)})...")

            # ── Rewrite + save (sequential — AI rate-limit friendly) ──
            _last_fail_reason = ""
            for i, art in enumerate(articles, 1):
                try:
                    logger.info(f"  [{i}/{total}] Rewriting: {art['title'][:50]}...")

                    # Dedupe check
                    if await _is_duplicate(art["title"], art.get("full_text", ""), recent_articles):
                        logger.info(f"  Duplicate skipped: {art['title'][:50]}")
                        skipped += 1
                        continue

                    # AI rewrite (has internal fallbacks — always returns a dict)
                    result_dict = rewrite_article(
                        art["full_text"], art["title"], art["region"]
                    )

                    body = result_dict.get("body", "")
                    if not body or len(body.strip()) < 50:
                        logger.warning(f"  Article body too short ({len(body)} chars), skipping: {art['title'][:50]}")
                        skipped += 1
                        continue

                    # V23: Wikipedia-style deep internal linking — scan body for
                    # phrases matching existing article titles and auto-link them.
                    # Pass recent_articles (which contains up to 50 prior articles)
                    # so the new article gets woven into the existing site graph.
                    try:
                        body = apply_internal_links(body, recent_articles, max_links=8)
                    except Exception as link_err:
                        logger.warning(f"  Internal linking skipped: {link_err}")

                    # V24: Quality Control — evaluate before saving
                    qc_score_json = None
                    final_status = "published"  # default
                    if QC_ENABLED:
                        try:
                            existing_texts = [a.ai_content or a.summary or a.title for a in recent_articles]
                            qc = evaluate_article(
                                title=result_dict["title"],
                                body=body,
                                meta_desc=result_dict.get("meta_desc", ""),
                                keywords=result_dict.get("keywords", ""),
                                existing_texts=existing_texts,
                            )
                            import json as _qc_json
                            qc_score_json = _qc_json.dumps(quality_score_to_dict(qc))

                            # Verdict → status mapping
                            if qc.verdict == "reject":
                                logger.warning(
                                    f"  QC REJECT: {result_dict['title'][:50]} — "
                                    f"{'; '.join(qc.reasons)}"
                                )
                                skipped += 1
                                continue  # skip saving — don't waste DB rows on rejects
                            elif qc.verdict == "review":
                                # Save as pending_review (admin must approve)
                                final_status = "pending_review"
                            # else: "publish" → fall through to draft/published logic
                            logger.info(
                                f"  QC: verdict={qc.verdict}, overall={qc.overall}, "
                                f"readability={qc.readability}, words={qc.word_count}"
                            )
                        except Exception as qc_err:
                            logger.warning(f"  QC evaluation failed (saving anyway): {qc_err}")

                    # V24: Draft Mode — if enabled, save as "draft" instead of "published"
                    # Admin reviews in the dashboard and clicks "Publish" to make visible.
                    if DRAFT_MODE and final_status == "published":
                        final_status = "draft"

                    # V31.1: Title uniqueness check — happens AFTER AI rewrite,
                    # checks the ACTUAL title that will be saved (not the original
                    # RSS title). If the AI-generated title is a duplicate or
                    # near-duplicate of an existing article, a numeric suffix is
                    # appended: "My Title (2)", "My Title (3)", etc.
                    try:
                        from title_uniqueness import ensure_unique_title, compute_title_norm
                        result_dict["title"] = await ensure_unique_title(
                            db, result_dict["title"]
                        )
                    except Exception as title_fix_err:
                        logger.warning(
                            f"  Title uniqueness check failed (saving anyway): {title_fix_err}"
                        )

                    art_hash = make_article_hash(result_dict["title"], body)
                    new_article = Article(
                        title=result_dict["title"][:500],
                        slug=make_slug(result_dict["title"]),
                        original_url=art["url"],
                        ai_content=body,
                        summary=(result_dict.get("meta_desc") or "")[:280],
                        image_url=art.get("image_url", ""),
                        region=art["region"],
                        meta_desc=result_dict.get("meta_desc", ""),
                        keywords=result_dict.get("keywords", ""),
                        article_hash=art_hash,
                        # V31.1: Store normalized title for fast duplicate detection
                        title_norm=compute_title_norm(result_dict["title"])[:500],
                        # V18: Wikipedia-killer features
                        tldr_summary=result_dict.get("tldr_summary", ""),
                        fact_check_status="under_review",  # Default; admin can upgrade later
                        audio_status="pending",
                        # V24: Draft mode + QC
                        status=final_status,
                        quality_score=qc_score_json,
                        source_type="rss",
                    )
                    db.add(new_article)
                    db.add(ProcessedURL(url=art["url"]))
                    await db.commit()

                    saved += 1
                    recent_articles.insert(0, new_article)
                    recent_articles = recent_articles[:50]
                    logger.info(f"  [{art['region'].upper()}] SAVED ({final_status}): {result_dict['title'][:50]}")

                    # Rate-limit protection
                    await asyncio.sleep(ARTICLE_DELAY)

                except IntegrityError:
                    # Article URL already exists in DB — skip silently (not a failure)
                    await db.rollback()
                    skipped += 1
                    logger.info(f"  Already in DB (skipped): {art['title'][:50]}")
                except Exception as e:
                    await db.rollback()
                    failed += 1
                    _last_fail_reason = f"{type(e).__name__}: {str(e)[:150]}"
                    logger.error(f"  Article FAILED: {art['title'][:50]} — {_last_fail_reason}")
                    await asyncio.sleep(2)

            if saved > 0:
                _FAILURE_COUNT = 0
                _pipeline_stats["last_success"] = time.time()

            _pipeline_stats["last_run"] = time.time()
            _pipeline_stats["total_articles"] += saved

        except Exception as e:
            await db.rollback()
            logger.error(f"Pipeline error: {e}")
            pipeline_result["running"] = False
            pipeline_result["last_error"] = f"{type(e).__name__}: {str(e)[:300]}"
            _FAILURE_COUNT += 1
            if _FAILURE_COUNT >= _MAX_FAILURES:
                _send_alert(f"Pipeline failed {_FAILURE_COUNT} times. Last error: {str(e)[:200]}")

    pipeline_result.update({
        "running": False,
        "saved": saved, "failed": failed, "skipped": skipped,
        "last_error": _last_fail_reason if (failed > 0 and saved == 0) else "",
    })

    logger.info("=" * 50)
    logger.info(f"Pipeline Complete: {saved} saved, {failed} failed, {skipped} duplicates skipped")
    logger.info("=" * 50)

    # V23: Invalidate sitemap + RSS cache so newly-saved articles appear
    # in Google's next crawl. Wrapped in try/except so a cache outage
    # never breaks the pipeline.
    if saved > 0:
        try:
            # Lazy import avoids circular dependency (main imports scheduler)
            import main as _main_mod
            if hasattr(_main_mod, "invalidate_sitemap_cache"):
                _main_mod.invalidate_sitemap_cache()
        except Exception as e:
            logger.debug(f"Sitemap cache invalidation skipped: {e}")


def run_pipeline() -> None:
    """Sync entry point for APScheduler or manual trigger.
    V13 FIX: Creates a fresh event loop properly and catches ALL exceptions
    so pipeline_result always reflects the real outcome.
    V24 FIX: Checks leader election first — non-leader instances skip the
    pipeline entirely (prevents duplicate work in multi-instance deploys)."""
    # V24: Leader election — only the leader runs the pipeline
    if not _acquire_leadership():
        logger.info(f"[V24 Leader] Skipping pipeline — not leader (instance={INSTANCE_ID})")
        pipeline_result.update({
            "running": False,
            "last_error": "Skipped: another instance is leader",
        })
        return

    try:
        # FIX: prefer the main event loop (injected by main.py at startup) so that
        # asyncpg connections from the shared pool are not "attached to a different loop".
        if MAIN_LOOP is not None and MAIN_LOOP.is_running():
            future = asyncio.run_coroutine_threadsafe(_run_pipeline_async(), MAIN_LOOP)
            future.result(timeout=600)  # 10 min max
        else:
            asyncio.run(_run_pipeline_async())
    except Exception as e:
        logger.error(f"run_pipeline fatal error: {type(e).__name__}: {e}")
        pipeline_result.update({
            "running": False,
            "last_error": f"Fatal: {type(e).__name__}: {str(e)[:300]}",
        })


def start_scheduler():
    """Start the background scheduler. Returns the scheduler instance."""
    s = BackgroundScheduler(
        job_defaults={
            "coalesce": True,        # if multiple runs queued, only fire once
            "max_instances": 1,      # never overlap pipeline runs
            "misfire_grace_time": 60,
        }
    )

    s.add_job(
        run_pipeline,
        IntervalTrigger(minutes=INTERVAL),
        id="fetch_news",
        replace_existing=True,
        next_run_time=datetime.now(),  # V8: Run once immediately on startup
    )

    # Midnight cleanup — async-safe via fresh event loop
    async def _cleanup():
        await delete_old_articles()

    def _cleanup_sync():
        try:
            asyncio.run(_cleanup())
        except RuntimeError:
            loop = asyncio.get_event_loop()
            loop.create_task(_cleanup())

    s.add_job(
        _cleanup_sync,
        CronTrigger(hour=0, minute=0),
        id="cleanup",
        replace_existing=True,
    )

    # V24: Daily automated database backup at 2 AM UTC
    # Runs pg_dump for Postgres or copies the SQLite file.
    # Only runs on the leader instance (so multi-instance deploys
    # don't produce duplicate backups).
    if os.getenv("AUTO_BACKUP_ENABLED", "1") == "1":
        def _backup_sync():
            if not _acquire_leadership():
                logger.info("[V24 Backup] Skipping — not leader")
                return
            try:
                from database import IS_POSTGRES, IS_SQLITE, DATABASE_URL
                import subprocess, shutil, os as _os
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                backup_dir = Path(_os.getenv("BACKUP_DIR", "/tmp"))
                backup_dir.mkdir(parents=True, exist_ok=True)
                if IS_POSTGRES:
                    import re as _re
                    m = _re.match(
                        r"postgresql(?:\+asyncpg)?://([^:]+):([^@]+)@([^:/]+)(?::(\d+))?/(.+)",
                        DATABASE_URL,
                    )
                    if m:
                        user, password, host, port, dbname = m.groups()
                        env = _os.environ.copy()
                        env["PGPASSWORD"] = password
                        cmd = [
                            "pg_dump", "-h", host, "-p", port or "5432",
                            "-U", user, "-F", "c",
                            "-f", str(backup_dir / f"sfaam_backup_{ts}.dump"),
                            dbname,
                        ]
                        subprocess.run(cmd, env=env, check=True, timeout=300, capture_output=True)
                        logger.info(f"[V24 Backup] PostgreSQL dump created: sfaam_backup_{ts}.dump")
                elif IS_SQLITE:
                    db_path = DATABASE_URL.replace("sqlite+aiosqlite:///", "").replace("sqlite:///", "")
                    shutil.copy2(db_path, backup_dir / f"sfaam_backup_{ts}.db")
                    logger.info(f"[V24 Backup] SQLite copy created: sfaam_backup_{ts}.db")
                # V24: Keep only last 7 backups (auto-rotation)
                backups = sorted(backup_dir.glob("sfaam_backup_*"))
                for old in backups[:-7]:
                    try:
                        old.unlink()
                        logger.info(f"[V24 Backup] Removed old backup: {old.name}")
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"[V24 Backup] Failed: {e}")
                try:
                    from monitoring import capture_exception
                    capture_exception(e, context={"job": "daily_backup"})
                except Exception:
                    pass

        s.add_job(
            _backup_sync,
            CronTrigger(hour=2, minute=0),
            id="daily_backup",
            replace_existing=True,
        )
        logger.info("[V24] Daily DB backup scheduled for 2:00 AM UTC")

    s.start()
    logger.info(f"Scheduler started — fetching news every {INTERVAL} minutes (V8: also runs once on startup)")
    return s
