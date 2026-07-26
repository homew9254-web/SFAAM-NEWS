"""
engine2_orchestrator.py - SFAAM Automated News Engine V2 (Clean Rebuild)
===========================================================================
Ties Steps 1-6 together for ONE region:

    1. engine2_trends   -> viral trend for this region
    2. engine2_search    -> up to 5 candidate article URLs
    3. engine2_scraper    -> full text + images per article
    4. engine2_synth       -> AI-combined title/summary/overview/background
    5. engine2_images       -> weave images through the content
    6. save Article(status="draft") -> admin reviews & publishes

This module deliberately reuses the existing, already-tested
infrastructure (region_config, resilient_llm, database, dedup_engine,
title_uniqueness) rather than re-implementing it — per the spec's
"clean rebuild" goal of a small, focused codebase.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select

from database import AsyncSessionLocal, Article
from dedup_engine import get_skip_set_for_region, record_processed
from region_config import Region
from title_uniqueness import ensure_unique_title

import engine2_trends
import engine2_search
import engine2_scraper
import engine2_synth
import engine2_images

logger = logging.getLogger(__name__)

MAX_SOURCE_ARTICLES = 5


@dataclass
class RegionCycleOutcome:
    region: str
    status: str            # "success" | "skipped" | "failed"
    query: str = ""
    article_id: int | None = None
    sources_used: int = 0
    error: str = ""
    elapsed_s: float = 0.0


def _build_final_markdown(overview_md: str, background_md: str, sources: list) -> str:
    """Combine overview + background + sources into ONE markdown blob for
    the `ai_content` field — the existing frontend renders this field
    directly via its markdown-to-HTML converter, so no frontend edits
    are needed."""
    parts = [overview_md.strip()]
    if background_md.strip():
        parts.append("## Background & History\n\n" + background_md.strip())
    if sources:
        lines = ["## Sources"]
        for s in sources:
            label = f"{s.get('title', s['url'])} — {s.get('source', '')}".strip(" —")
            lines.append(f"- [{label}]({s['url']})")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


async def run_region_cycle(region: Region) -> RegionCycleOutcome:
    start = time.monotonic()

    # ── Step 1: Trend Detection ──
    skip_set = get_skip_set_for_region(region.key)
    trend = await engine2_trends.get_regional_trend(region, skip_queries=skip_set)
    if not trend:
        return RegionCycleOutcome(region=region.key, status="skipped", error="no trend found", elapsed_s=time.monotonic() - start)

    # ── Step 2: Article Search ──
    search_results = await engine2_search.search_articles(trend.query, region, max_results=MAX_SOURCE_ARTICLES)
    if not search_results:
        return RegionCycleOutcome(region=region.key, status="skipped", query=trend.query, error="no articles found", elapsed_s=time.monotonic() - start)

    # ── Step 3: Full Article Scrape ──
    urls = [r.url for r in search_results]
    scraped = await engine2_scraper.scrape_batch(urls, max_articles=MAX_SOURCE_ARTICLES)
    if len(scraped) < 2:
        return RegionCycleOutcome(
            region=region.key, status="skipped", query=trend.query,
            error=f"only {len(scraped)} article(s) scraped successfully (need >=2)",
            elapsed_s=time.monotonic() - start,
        )

    # ── Step 4: AI Synthesis ──
    synth = engine2_synth.synthesize_article(region, trend.query, scraped)
    if not synth.success:
        return RegionCycleOutcome(
            region=region.key, status="failed", query=trend.query,
            error=synth.error, sources_used=len(scraped), elapsed_s=time.monotonic() - start,
        )

    # ── Step 5: Image Placement ──
    placement = engine2_images.place_images(synth.overview_md, synth.background_md, scraped)

    sources_meta = [
        {"url": a.url, "title": a.title, "source": a.source_domain, "author": a.author, "published": a.published}
        for a in scraped
    ]
    final_markdown = _build_final_markdown(placement.overview_md, placement.background_md, sources_meta)

    # ── Step 6: Save as Draft ──
    # `original_url` has a UNIQUE constraint in the DB. Since this engine
    # synthesizes from 5 sources (not one), we tag the primary source URL
    # with the region so two regions covering the same source story don't
    # collide on that constraint.
    sep = "&" if "?" in scraped[0].url else "?"
    tagged_original_url = f"{scraped[0].url}{sep}sfaam_engine2_region={region.key}"

    async with AsyncSessionLocal() as session:
        unique_title = await ensure_unique_title(session, synth.title)
        article = Article(
            title=unique_title,
            original_url=tagged_original_url,
            ai_content=final_markdown,
            summary=synth.summary,
            image_url=placement.hero_image_url,
            region=region.key,
            status="draft",
            source_type="engine_v2",
            search_keyword=trend.query,
            history_context=placement.background_md,
            references_data=json.dumps(sources_meta),
            source_count=len(scraped),
            word_count=synth.word_count,
            llm_provider=synth.llm_provider,
            llm_model=synth.llm_model,
            pipeline_version="engine_v2",
            is_trends=1,
            trend_query=trend.query,
            cross_source_count=trend.cross_source_count,
        )
        session.add(article)
        try:
            await session.commit()
            await session.refresh(article)
        except Exception as e:
            await session.rollback()
            return RegionCycleOutcome(
                region=region.key, status="failed", query=trend.query,
                error=f"DB save failed: {type(e).__name__}: {e}",
                sources_used=len(scraped), elapsed_s=time.monotonic() - start,
            )

    await record_processed(region.key, trend.query, article_id=article.id)

    logger.info(f"[engine2] region={region.key} SAVED draft article_id={article.id} title='{unique_title}'")
    return RegionCycleOutcome(
        region=region.key, status="success", query=trend.query,
        article_id=article.id, sources_used=len(scraped),
        elapsed_s=time.monotonic() - start,
    )
