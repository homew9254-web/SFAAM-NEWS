"""
engine2_trends.py - SFAAM Automated News Engine V2 (Clean Rebuild)
=====================================================================
STEP 1 of the 6-step workflow: Trend Detection (Viral Fact Talash)

Every 3 hours, for EACH region independently, find the top viral
trending fact/topic:
    - Source: Google Trends RSS (free, reliable) per region geo
    - Cross-check against major news sites (BBC, Reuters, Al Jazeera)
      to verify it's genuinely being covered (>=2-3 major sources)
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import feedparser
import httpx

from region_config import Region
from dedup_engine import normalize_keyword

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 10.0

# Major outlets used to cross-verify a trend is genuinely being covered.
VERIFICATION_FEEDS = [
    ("BBC", "https://feeds.bbci.co.uk/news/rss.xml"),
    ("Reuters", "https://www.reutersagency.com/feed/?best-topics=top-news&post_type=best"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
]

GOOGLE_TRENDS_RSS = "https://trends.google.com/trending/rss?geo={geo}"


@dataclass
class TrendCandidate:
    query: str
    traffic_hint: str = ""
    cross_source_count: int = 0


async def _fetch_google_trends(geo: str) -> list[TrendCandidate]:
    url = GOOGLE_TRENDS_RSS.format(geo=geo or "US")
    try:
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            logger.warning(f"[engine2_trends] Google Trends non-200 ({resp.status_code}) for geo={geo}")
            return []
        parsed = feedparser.parse(resp.text)
        out = []
        for entry in parsed.entries[:10]:
            title = getattr(entry, "title", "").strip()
            if not title:
                continue
            traffic = ""
            if hasattr(entry, "ht_approx_traffic"):
                traffic = str(entry.ht_approx_traffic)
            out.append(TrendCandidate(query=title, traffic_hint=traffic))
        return out
    except Exception as e:
        logger.warning(f"[engine2_trends] Google Trends fetch failed for geo={geo}: {type(e).__name__}: {e}")
        return []


async def _fetch_feed_titles(url: str) -> list[str]:
    try:
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return []
        parsed = feedparser.parse(resp.text)
        return [getattr(e, "title", "") for e in parsed.entries[:60]]
    except Exception as e:
        logger.info(f"[engine2_trends] verification feed failed ({url}): {type(e).__name__}: {e}")
        return []


def _mentions_query(query: str, titles: list[str]) -> bool:
    q_words = {w.lower() for w in query.split() if len(w) > 3}
    if not q_words:
        return False
    for t in titles:
        t_lower = t.lower()
        hits = sum(1 for w in q_words if w in t_lower)
        if hits >= max(1, len(q_words) // 2):
            return True
    return False


async def get_regional_trend(region: Region, skip_queries: set[str] | None = None) -> TrendCandidate | None:
    """Return the top verified viral trend for a region (Step 1).

    Verification: the candidate must be mentioned (partial keyword match)
    by at least 1 of the major cross-check feeds, unless none are reachable
    (in which case we fall back to the top Google Trends item so the cycle
    can still proceed).
    """
    skip_queries = skip_queries or set()
    candidates = await _fetch_google_trends(region.trends_geo)
    candidates = [c for c in candidates if normalize_keyword(c.query) not in skip_queries]
    if not candidates:
        logger.info(f"[engine2_trends] no trend candidates for region={region.key}")
        return None

    verification_results = await asyncio.gather(
        *[_fetch_feed_titles(url) for _, url in VERIFICATION_FEEDS],
        return_exceptions=True,
    )
    all_titles: list[str] = []
    any_feed_reachable = False
    for r in verification_results:
        if isinstance(r, list) and r:
            any_feed_reachable = True
            all_titles.extend(r)

    for c in candidates:
        if not any_feed_reachable:
            c.cross_source_count = 0
            continue
        c.cross_source_count = 1 if _mentions_query(c.query, all_titles) else 0

    verified = [c for c in candidates if c.cross_source_count > 0]
    chosen = verified[0] if verified else candidates[0]
    logger.info(
        f"[engine2_trends] region={region.key} chosen trend='{chosen.query}' "
        f"verified={bool(verified)}"
    )
    return chosen
