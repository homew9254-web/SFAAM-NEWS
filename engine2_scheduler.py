"""
engine2_scheduler.py - SFAAM Automated News Engine V2 (Clean Rebuild)
========================================================================
Runs engine2_orchestrator.run_region_cycle() for all 6 regions, every
ENGINE2_INTERVAL_HOURS (default 3), producing up to 6 new draft
articles per cycle (48/day across 8 cycles).

Concurrency: regions run with BOUNDED parallelism (ENGINE2_MAX_CONCURRENT_REGIONS,
default 2) rather than all 6 at once. Full 6-way parallelism (as worded in the
spec) risks OOM/crash on small hosting instances (e.g. Railway Hobby/Free —
512MB-1GB RAM) since each region concurrently runs async scraping + an LLM
call. 2 at a time keeps peak memory/connections bounded while still being
faster than fully sequential. Raise ENGINE2_MAX_CONCURRENT_REGIONS if you're
on a bigger instance.

Mirrors the existing app's scheduler wiring pattern (APScheduler
BackgroundScheduler + asyncio.run_coroutine_threadsafe into the main
event loop) so it plugs into main.py's lifespan the same way the
V30 engine_scheduler.py does.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import AsyncSessionLocal, EngineCycleLog
from region_config import REGIONS
from engine2_orchestrator import run_region_cycle

logger = logging.getLogger(__name__)

ENGINE2_INTERVAL_HOURS = float(os.getenv("ENGINE2_INTERVAL_HOURS", "3"))
ENGINE2_RUN_ON_STARTUP = os.getenv("ENGINE2_RUN_ON_STARTUP", "0") == "1"
# Bounded parallelism — safe default for small (512MB-1GB) instances.
ENGINE2_MAX_CONCURRENT_REGIONS = int(os.getenv("ENGINE2_MAX_CONCURRENT_REGIONS", "2"))

MAIN_LOOP: asyncio.AbstractEventLoop | None = None
_scheduler: BackgroundScheduler | None = None
_last_cycle_result: dict = {"running": False, "last_run": None, "last_summary": None}


async def run_full_cycle() -> dict:
    """Run all 6 regions with bounded concurrency, log the cycle, return a summary."""
    cycle_id = str(uuid.uuid4())
    started_at = datetime.utcnow()
    _last_cycle_result["running"] = True
    logger.info(
        f"[engine2_scheduler] cycle {cycle_id} starting — {len(REGIONS)} regions "
        f"(max {ENGINE2_MAX_CONCURRENT_REGIONS} concurrent)"
    )

    sem = asyncio.Semaphore(ENGINE2_MAX_CONCURRENT_REGIONS)

    async def _bounded(region):
        async with sem:
            return await run_region_cycle(region)

    outcomes = await asyncio.gather(
        *[_bounded(region) for region in REGIONS],
        return_exceptions=True,
    )

    region_summary = []
    drafts_produced = 0
    drafts_failed = 0
    skipped = 0
    for region, outcome in zip(REGIONS, outcomes):
        if isinstance(outcome, Exception):
            logger.error(f"[engine2_scheduler] region={region.key} crashed: {type(outcome).__name__}: {outcome}")
            region_summary.append({"region": region.key, "status": "failed", "error": str(outcome)})
            drafts_failed += 1
            continue
        region_summary.append({
            "region": outcome.region, "status": outcome.status, "query": outcome.query,
            "article_id": outcome.article_id, "sources": outcome.sources_used,
            "error": outcome.error, "elapsed_s": round(outcome.elapsed_s, 1),
        })
        if outcome.status == "success":
            drafts_produced += 1
        elif outcome.status == "skipped":
            skipped += 1
        else:
            drafts_failed += 1

    completed_at = datetime.utcnow()
    summary = {
        "cycle_id": cycle_id, "drafts_produced": drafts_produced,
        "drafts_failed": drafts_failed, "skipped": skipped,
        "regions": region_summary,
    }

    try:
        async with AsyncSessionLocal() as session:
            session.add(EngineCycleLog(
                cycle_id=cycle_id, started_at=started_at, completed_at=completed_at,
                regions_processed=len(REGIONS), drafts_produced=drafts_produced,
                drafts_failed=drafts_failed, skipped_duplicates=skipped,
                total_elapsed_s=int((completed_at - started_at).total_seconds()),
                status="completed", region_summary=json.dumps(region_summary),
            ))
            await session.commit()
    except Exception as e:
        logger.warning(f"[engine2_scheduler] failed to log cycle: {e}")

    _last_cycle_result.update({"running": False, "last_run": completed_at.isoformat(), "last_summary": summary})
    logger.info(f"[engine2_scheduler] cycle {cycle_id} done — {drafts_produced} drafts, {skipped} skipped, {drafts_failed} failed")
    return summary


def _run_cycle_from_thread():
    """Called by APScheduler's background thread — hops onto the main
    asyncio loop (required because DB sessions/httpx clients are async)."""
    if MAIN_LOOP is None:
        logger.error("[engine2_scheduler] MAIN_LOOP not set — cannot run cycle")
        return
    try:
        future = asyncio.run_coroutine_threadsafe(run_full_cycle(), MAIN_LOOP)
        future.result(timeout=60 * 20)  # 20 min ceiling for a full 6-region cycle
    except Exception as e:
        logger.error(f"[engine2_scheduler] cycle run failed: {type(e).__name__}: {e}")


def start_engine2_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        _run_cycle_from_thread,
        trigger=IntervalTrigger(hours=ENGINE2_INTERVAL_HOURS),
        id="engine2_cycle",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.utcnow() if ENGINE2_RUN_ON_STARTUP else None,
    )
    _scheduler.start()
    logger.info(f"[engine2_scheduler] started — every {ENGINE2_INTERVAL_HOURS}h, run_on_startup={ENGINE2_RUN_ON_STARTUP}")


def stop_engine2_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("[engine2_scheduler] stopped")


def get_status() -> dict:
    return dict(_last_cycle_result)
