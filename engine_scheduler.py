"""
engine_scheduler.py - SFAAM Automated News Engine (V30 / TRD v1.0)
===================================================================
The 3-Hour Cron Scheduler
--------------------------
Per TRD Section 3: "A cron service executes the following sequence
every 3 hours."

This module wires the engine into APScheduler with:
  • 3-hourly interval (configurable via ENGINE_INTERVAL_HOURS)
  • Optional startup run (off by default — controlled by ENGINE_RUN_ON_STARTUP)
  • Max 1 concurrent instance (coalesce=True prevents pile-up if a cycle
    takes longer than 3 hours)
  • Nightly cleanup job at 2 AM UTC (prunes 7-day-old dedup entries)

DESIGN NOTES
------------
• The scheduler is async (AsyncIOScheduler) so it shares the FastAPI
  event loop — no extra thread needed.
• The first cycle does NOT run on startup by default (per existing
  Railway deployment pattern). Set ENGINE_RUN_ON_STARTUP=1 to override.
• This module is INDEPENDENT of the legacy trends_scheduler.py — both
  can run side-by-side. We recommend disabling the legacy scheduler
  (set TRENDS_INTERVAL_HOURS=999999 or remove start_trends_scheduler()
  call from main.py) once the new engine is verified working.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from automated_news_engine import (
    ENGINE_INTERVAL_HOURS,
    ENGINE_RUN_ON_STARTUP,
    nightly_cleanup,
    run_engine_cycle,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Scheduler singleton
# ─────────────────────────────────────────────────────────────
_scheduler = None
_started = False
# Bug #13 FIX: holder for background startup tasks so they don't get GC'd.
# This set is module-level and lives for the lifetime of the process.
# Tasks auto-remove themselves via add_done_callback when complete.
_startup_tasks: set = set()


def start_engine_scheduler() -> None:
    """Start the 3-hourly engine scheduler + nightly cleanup job.

    Safe to call multiple times — only the first call starts a scheduler.
    """
    global _scheduler, _started
    if _started or _scheduler is not None:
        logger.info("[EngineScheduler] Already running")
        return

    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.error(
            "[EngineScheduler] APScheduler not installed — "
            "engine scheduler disabled (pip install apscheduler)"
        )
        return

    _scheduler = AsyncIOScheduler(timezone="UTC")

    # Main 3-hourly engine cycle
    _scheduler.add_job(
        run_engine_cycle,
        trigger=IntervalTrigger(hours=ENGINE_INTERVAL_HOURS),
        id="sfaam_engine_cycle",
        replace_existing=True,
        max_instances=1,        # never run two cycles in parallel
        coalesce=True,          # if multiple triggers fire while one is running, run once
        misfire_grace_time=600, # 10 min grace if the server was overloaded
    )

    # Nightly cleanup at 2:00 AM UTC
    _scheduler.add_job(
        nightly_cleanup,
        trigger=CronTrigger(hour=2, minute=0),
        id="sfaam_engine_nightly_cleanup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    _scheduler.start()
    _started = True

    logger.info(
        f"[EngineScheduler] Started — engine runs every {ENGINE_INTERVAL_HOURS}h "
        f"+ nightly cleanup at 02:00 UTC "
        f"(startup_run={ENGINE_RUN_ON_STARTUP})"
    )

    # Optional: run once on startup (after a delay to let FastAPI fully init)
    if ENGINE_RUN_ON_STARTUP:
        async def _startup_run():
            try:
                await asyncio.sleep(30)  # let DB + Redis + other services init
                logger.info("[EngineScheduler] Running startup cycle...")
                await run_engine_cycle()
            except Exception as e:
                logger.exception(f"[EngineScheduler] Startup cycle crashed: {e}")

        try:
            loop = asyncio.get_running_loop()
            # Bug #13 FIX: store task reference in module-level _startup_tasks
            # set to prevent garbage collection. Task auto-removes itself
            # via add_done_callback when complete.
            task = loop.create_task(_startup_run())
            _startup_tasks.add(task)
            task.add_done_callback(_startup_tasks.discard)
        except RuntimeError:
            logger.warning("[EngineScheduler] No running event loop — startup run deferred")


def stop_engine_scheduler() -> None:
    """Graceful shutdown."""
    global _scheduler, _started
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
        _started = False
        logger.info("[EngineScheduler] Stopped")


def is_running() -> bool:
    """Return True if the scheduler is currently active."""
    return _started


def get_scheduler_info() -> dict:
    """Return scheduler status info for the admin dashboard."""
    jobs = []
    if _scheduler is not None:
        for job in _scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            })
    return {
        "running": _started,
        "interval_hours": ENGINE_INTERVAL_HOURS,
        "run_on_startup": ENGINE_RUN_ON_STARTUP,
        "jobs": jobs,
    }


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    async def _main():
        # Run a single cycle immediately for testing
        start_engine_scheduler()
        await asyncio.sleep(2)  # let scheduler init
        result = await run_engine_cycle()
        print(json.dumps(result, indent=2, default=str))
        stop_engine_scheduler()

    asyncio.run(_main())
