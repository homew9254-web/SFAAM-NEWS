"""
trend_detector.py - SFAAM Automated News Engine (V30 / TRD v1.0)
=================================================================
Step A: Trend Detection & Viral Filtering (Ranking Engine)
----------------------------------------------------------
Per TRD Section 3, Step A:

    "The system polls Google Trends RSS/API, Twitter/X Trending Endpoints,
    and premium News Aggregator APIs (NewsAPI / GNews / Tavily Context API).
    It cross-references current articles from major global networks (BBC,
    Al Jazeera, Reuters) and applies a ranking algorithm based on
    'Velocity of Searches' and 'Cross-Platform Volume' over the last
    180 minutes."

    "The highest-ranking unique topic per region is isolated for processing."

This module is per-region: each region (World, USA, UK, Pakistan, India,
Germany) gets its own detector call with its own trends_geo code.

SIGNAL SOURCES (in priority order):
  1. Google Trends RSS (free, no key) — primary, always available
  2. NewsAPI.org top headlines (optional, requires NEWSAPI_KEY)
  3. GNews.io top news (optional, requires GNEWS_KEY)
  4. Tavily Context API (optional, requires TAVILY_API_KEY) — used to
     verify a trend has multiple authoritative sources before processing

RANKING ALGORITHM:
  Each detected topic gets a "trend score" combining:
    • Google Trends traffic value (0-100 normalized)
    • Cross-source count (how many of the 3 aggregators mention it)
    • Recency boost (newer = higher score)
    • Domain diversity (more unique domains = higher score)

  The single highest-scoring topic per region is returned. The dedup
  engine (dedup_engine.py) is consulted before returning — if the top
  topic was already processed in the last 7 days, the next-highest is
  returned instead.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from region_config import Region

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────
@dataclass
class DetectedTrend:
    """A single detected trending topic with full audit metadata.

    `trend_score` is the combined ranking score (higher = more viral).
    `sources_seen_on` is which aggregators mentioned this topic
    (cross-platform volume signal).
    """
    query: str                       # The trending query/topic
    region: str                      # Region key
    trend_score: float               # 0-100 normalized
    google_trends_traffic: int = 0   # 0-100 from Google Trends
    cross_source_count: int = 0      # How many aggregators mentioned it
    sources_seen_on: list[str] = field(default_factory=list)  # ["google", "newsapi", "gnews"]
    related_articles: list[dict] = field(default_factory=list)  # [{url, title, domain, snippet}]
    detected_at: str = ""

    def __post_init__(self):
        if not self.detected_at:
            self.detected_at = datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _http_get(url: str, *, headers: Optional[dict] = None, timeout: float = 20.0) -> Optional[str]:
    """Fetch URL text. Returns None on any error or non-2xx response."""
    h = {**DEFAULT_HEADERS, **(headers or {})}
    try:
        with httpx.Client(headers=h, follow_redirects=True, timeout=timeout) as client:
            r = client.get(url)
            if r.status_code >= 400:
                logger.debug(f"  HTTP {r.status_code} for {url[:80]}")
                return None
            return r.text
    except Exception as e:
        logger.debug(f"  fetch error {type(e).__name__}: {url[:80]}")
        return None


# ─────────────────────────────────────────────────────────────
# Signal Source 1: Google Trends RSS (always available, free)
# ─────────────────────────────────────────────────────────────
TRENDS_RSS_URL = "https://trends.google.com/trending/rss?geo={geo}"


def fetch_google_trends(region: Region, limit: int = 20) -> list[dict]:
    """Fetch trending queries from Google Trends RSS.

    Returns a list of dicts: {query, traffic, related_news: [...], published}
    """
    geo = region.trends_geo  # "" = worldwide
    url = TRENDS_RSS_URL.format(geo=geo)
    text = _http_get(url, timeout=15.0)
    if not text:
        logger.warning(f"[TrendDetector] Google Trends RSS returned empty for geo={geo or 'worldwide'}")
        return []

    parsed = feedparser.parse(text)
    results: list[dict] = []
    for entry in parsed.entries[:limit]:
        query = (entry.get("title") or "").strip()
        if not query:
            continue
        # Traffic value is in entry.ht_approx_traffic (Google Trends custom field)
        traffic_str = entry.get("ht_approx_traffic", "0")
        traffic = _parse_traffic(traffic_str)
        # Related news items (Google Trends bundles them)
        related: list[dict] = []
        for news in entry.get("ht_news_item", []):
            related.append({
                "url": news.get("news_item_url", ""),
                "title": news.get("news_item_title", ""),
                "domain": _domain_of(news.get("news_item_url", "")),
                "snippet": news.get("news_item_snippet", ""),
                "source": "google_trends",
            })
        published = entry.get("published", "")
        results.append({
            "query": query,
            "traffic": traffic,
            "related_news": related,
            "published": published,
            "source": "google_trends",
        })
    logger.info(f"[TrendDetector] Google Trends ({geo or 'worldwide'}): {len(results)} queries")
    return results


def _parse_traffic(s: str) -> int:
    """Parse a Google Trends traffic string like '50K+' or '1.2M+' into a number."""
    if not s:
        return 0
    s = s.strip().upper().rstrip("+")
    mult = 1
    if s.endswith("K"):
        mult = 1_000
        s = s[:-1]
    elif s.endswith("M"):
        mult = 1_000_000
        s = s[:-1]
    elif s.endswith("B"):
        mult = 1_000_000_000
        s = s[:-1]
    try:
        return int(float(s) * mult)
    except (ValueError, TypeError):
        return 0


def _domain_of(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────
# Signal Source 2: NewsAPI.org top headlines (optional)
# ─────────────────────────────────────────────────────────────
NEWSAPI_TOP_URL = "https://newsapi.org/v2/top-headlines"


def fetch_newsapi_top(region: Region, limit: int = 20) -> list[dict]:
    """Fetch top headlines from NewsAPI.org.

    Requires NEWSAPI_KEY env var. Returns [] if no key or on error.
    """
    from region_config import get_newsapi_key
    key = get_newsapi_key()
    if not key:
        return []

    params = {
        "apiKey": key,
        "pageSize": limit,
        "category": "general",
    }
    if region.newsapi_country:
        params["country"] = region.newsapi_country

    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(NEWSAPI_TOP_URL, params=params)
            if r.status_code != 200:
                logger.warning(f"[TrendDetector] NewsAPI returned {r.status_code}: {r.text[:200]}")
                return []
            data = r.json()
    except Exception as e:
        logger.warning(f"[TrendDetector] NewsAPI fetch failed: {type(e).__name__}: {e}")
        return []

    results: list[dict] = []
    for art in data.get("articles", [])[:limit]:
        title = (art.get("title") or "").strip()
        if not title or title == "[Removed]":
            continue
        url = art.get("url", "")
        results.append({
            "query": title,  # Use headline as the "query"
            "traffic": 0,    # NewsAPI doesn't provide traffic
            "related_news": [{
                "url": url,
                "title": title,
                "domain": _domain_of(url),
                "snippet": art.get("description", ""),
                "source": "newsapi",
            }],
            "published": art.get("publishedAt", ""),
            "source": "newsapi",
        })
    logger.info(f"[TrendDetector] NewsAPI ({region.newsapi_country or 'worldwide'}): {len(results)} headlines")
    return results


# ─────────────────────────────────────────────────────────────
# Signal Source 3: GNews.io top news (optional)
# ─────────────────────────────────────────────────────────────
GNEWS_TOP_URL = "https://gnews.io/api/v4/top-headlines"


def fetch_gnews_top(region: Region, limit: int = 20) -> list[dict]:
    """Fetch top headlines from GNews.io.

    Requires GNEWS_KEY env var. Returns [] if no key or on error.
    """
    from region_config import get_gnews_key
    key = get_gnews_key()
    if not key:
        return []

    params = {
        "apikey": key,
        "max": limit,
        "lang": "en",
    }
    if region.gnews_country:
        params["country"] = region.gnews_country

    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(GNEWS_TOP_URL, params=params)
            if r.status_code != 200:
                logger.warning(f"[TrendDetector] GNews returned {r.status_code}: {r.text[:200]}")
                return []
            data = r.json()
    except Exception as e:
        logger.warning(f"[TrendDetector] GNews fetch failed: {type(e).__name__}: {e}")
        return []

    results: list[dict] = []
    for art in data.get("articles", [])[:limit]:
        title = (art.get("title") or "").strip()
        if not title:
            continue
        url = art.get("url", "")
        results.append({
            "query": title,
            "traffic": 0,
            "related_news": [{
                "url": url,
                "title": title,
                "domain": _domain_of(url),
                "snippet": art.get("description", ""),
                "source": "gnews",
            }],
            "published": art.get("publishedAt", ""),
            "source": "gnews",
        })
    logger.info(f"[TrendDetector] GNews ({region.gnews_country or 'worldwide'}): {len(results)} headlines")
    return results


# ─────────────────────────────────────────────────────────────
# Topic normalization + cross-source matching
# ─────────────────────────────────────────────────────────────
# Two trends from different sources are considered "the same topic" if
# their normalized forms are similar enough. We use a simple keyword-overlap
# heuristic (TF-IDF would be overkill here).

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "must", "shall", "can",
    "this", "that", "these", "those", "it", "its", "they", "them",
    "their", "we", "us", "our", "you", "your", "he", "she", "his", "her",
    "says", "said", "news", "report", "reports", "reportedly", "today",
    "yesterday", "tomorrow", "after", "before", "during", "while",
}


def _normalize_topic(text: str) -> set[str]:
    """Extract a normalized keyword set from a topic string.

    Used to match the same topic across different aggregators.
    """
    words = re.findall(r"[A-Za-z][A-Za-z0-9']+", text.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def _topics_match(a: str, b: str, threshold: float = 0.4) -> bool:
    """Return True if two topic strings look like the same story.

    Uses Jaccard similarity on normalized keyword sets.
    Threshold 0.4 = at least 40% keyword overlap.
    """
    ka, kb = _normalize_topic(a), _normalize_topic(b)
    if not ka or not kb:
        return False
    intersection = ka & kb
    union = ka | kb
    return len(intersection) / len(union) >= threshold


# ─────────────────────────────────────────────────────────────
# Ranking algorithm
# ─────────────────────────────────────────────────────────────
def _compute_trend_score(
    *,
    google_traffic: int,
    cross_source_count: int,
    domain_diversity: int,
    recency_hours: float,
) -> float:
    """Compute a 0-100 trend score.

    Components (weights sum to 100):
      • Google Trends traffic  (40 pts, log-scaled 0-100)
      • Cross-source count     (30 pts, 1 source=10, 2=20, 3+=30)
      • Domain diversity       (15 pts, 1 domain=5, 2=10, 3+=15)
      • Recency boost          (15 pts, <3h=15, <6h=10, <12h=5, else=0)
    """
    # Traffic: log-scale. 100K+ ≈ 40 pts, 1K ≈ 26 pts, 0 ≈ 0 pts
    if google_traffic > 0:
        traffic_pts = min(40.0, 8.0 + (40.0 - 8.0) * (
            __import__("math").log10(max(google_traffic, 1)) / 5.0
        ))
    else:
        traffic_pts = 0.0

    cross_pts = min(30.0, cross_source_count * 10.0)
    diversity_pts = min(15.0, domain_diversity * 5.0)

    if recency_hours < 3:
        recency_pts = 15.0
    elif recency_hours < 6:
        recency_pts = 10.0
    elif recency_hours < 12:
        recency_pts = 5.0
    else:
        recency_pts = 0.0

    return round(traffic_pts + cross_pts + diversity_pts + recency_pts, 2)


def _parse_published_to_hours(published: str) -> float:
    """Parse an ISO timestamp and return hours since now. 9999.0 if parse fails."""
    if not published:
        return 9999.0
    try:
        # Try ISO format
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return max(0.0, delta.total_seconds() / 3600.0)
    except Exception:
        return 9999.0


# ─────────────────────────────────────────────────────────────
# Cross-source aggregation + ranking
# ─────────────────────────────────────────────────────────────
def _aggregate_and_rank(
    google_results: list[dict],
    newsapi_results: list[dict],
    gnews_results: list[dict],
) -> list[DetectedTrend]:
    """Merge results from all 3 aggregators, dedupe by topic similarity,
    and compute trend scores.

    Returns a list of DetectedTrend objects sorted by trend_score descending.
    """
    # Build candidate list — each candidate is one topic that may appear
    # in 1, 2, or 3 of the aggregators.
    candidates: list[dict] = []
    seen_normalized: list[set[str]] = []

    all_sources = [
        ("google_trends", google_results),
        ("newsapi", newsapi_results),
        ("gnews", gnews_results),
    ]

    for source_name, results in all_sources:
        for r in results:
            query = r["query"]
            norm = _normalize_topic(query)

            # Find an existing candidate that matches this topic
            matched_idx = None
            for i, prev_norm in enumerate(seen_normalized):
                if _topics_match(" ".join(norm), " ".join(prev_norm)):
                    matched_idx = i
                    break

            if matched_idx is None:
                # New candidate
                candidates.append({
                    "query": query,
                    "normalized": norm,
                    "google_traffic": r.get("traffic", 0) if source_name == "google_trends" else 0,
                    "sources_seen_on": [source_name],
                    "related_articles": list(r.get("related_news", [])),
                    "published": r.get("published", ""),
                })
                seen_normalized.append(norm)
            else:
                # Merge into existing candidate
                c = candidates[matched_idx]
                if source_name not in c["sources_seen_on"]:
                    c["sources_seen_on"].append(source_name)
                # Keep the highest traffic value
                if source_name == "google_trends" and r.get("traffic", 0) > c["google_traffic"]:
                    c["google_traffic"] = r["traffic"]
                # Merge related articles
                c["related_articles"].extend(r.get("related_news", []))
                # Keep the most recent published timestamp
                c["published"] = max(c["published"], r.get("published", ""))

    # Now compute scores
    trends: list[DetectedTrend] = []
    for c in candidates:
        # Deduplicate related articles by URL
        seen_urls: set[str] = set()
        unique_articles: list[dict] = []
        for art in c["related_articles"]:
            url = art.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_articles.append(art)
        # Domain diversity = unique domains among related articles
        unique_domains = {art.get("domain", "") for art in unique_articles if art.get("domain")}
        recency_h = _parse_published_to_hours(c["published"])

        score = _compute_trend_score(
            google_traffic=c["google_traffic"],
            cross_source_count=len(c["sources_seen_on"]),
            domain_diversity=len(unique_domains),
            recency_hours=recency_h,
        )

        trends.append(DetectedTrend(
            query=c["query"],
            region="",  # filled in by caller
            trend_score=score,
            google_trends_traffic=c["google_traffic"],
            cross_source_count=len(c["sources_seen_on"]),
            sources_seen_on=c["sources_seen_on"],
            related_articles=unique_articles,
        ))

    # Sort by score descending
    trends.sort(key=lambda t: t.trend_score, reverse=True)
    return trends


# ─────────────────────────────────────────────────────────────
# Public API — detect top trend for a region
# ─────────────────────────────────────────────────────────────
def detect_top_trend(
    region: Region,
    *,
    skip_queries: Optional[set[str]] = None,
    min_score: float = 20.0,
) -> Optional[DetectedTrend]:
    """Detect the single highest-ranking trending topic for a region.

    Per TRD: "The highest-ranking unique topic per region is isolated
    for processing."

    Args:
        region:        Region to detect trends for.
        skip_queries:  Set of normalized queries to skip (used by dedup
                       engine — already-processed topics).
        min_score:     Minimum trend score to consider (filters out noise).

    Returns:
        DetectedTrend, or None if no qualifying trend was found.
    """
    skip_queries = skip_queries or set()

    # Fetch from all configured sources (silently skips unavailable ones)
    google_results = fetch_google_trends(region)
    newsapi_results = fetch_newsapi_top(region)
    gnews_results = fetch_gnews_top(region)

    if not google_results and not newsapi_results and not gnews_results:
        logger.warning(
            f"[TrendDetector] region={region.key}: no signals from any source"
        )
        return None

    # Aggregate + rank
    ranked = _aggregate_and_rank(google_results, newsapi_results, gnews_results)
    logger.info(
        f"[TrendDetector] region={region.key}: {len(ranked)} candidate trends "
        f"ranked (top score={ranked[0].trend_score if ranked else 0:.1f})"
    )

    # Pick the highest-scoring trend that hasn't been skipped
    for trend in ranked:
        if trend.trend_score < min_score:
            logger.info(
                f"[TrendDetector] region={region.key}: top trend '{trend.query[:40]}' "
                f"score={trend.trend_score:.1f} < min_score={min_score} — skipping"
            )
            break

        # Check dedup
        norm_key = " ".join(sorted(_normalize_topic(trend.query)))
        if norm_key in skip_queries:
            logger.info(
                f"[TrendDetector] region={region.key}: '{trend.query[:40]}' "
                f"already processed (dedup) — trying next"
            )
            continue

        # Found our winner
        trend.region = region.key
        logger.info(
            f"[TrendDetector] region={region.key}: SELECTED '{trend.query[:50]}' "
            f"(score={trend.trend_score:.1f}, sources={trend.cross_source_count}, "
            f"articles={len(trend.related_articles)})"
        )
        return trend

    logger.warning(f"[TrendDetector] region={region.key}: no qualifying trend found")
    return None


def detect_top_trends_all_regions(
    *,
    skip_queries_by_region: Optional[dict[str, set[str]]] = None,
    min_score: float = 20.0,
) -> dict[str, Optional[DetectedTrend]]:
    """Detect the top trend for EVERY region in one pass.

    Returns a dict {region_key: DetectedTrend (or None)}.
    Used by the 3-hourly pipeline to fan out across all 6 regions.
    """
    from region_config import REGIONS

    skip_queries_by_region = skip_queries_by_region or {}
    results: dict[str, Optional[DetectedTrend]] = {}

    for region in REGIONS:
        skip = skip_queries_by_region.get(region.key, set())
        try:
            results[region.key] = detect_top_trend(region, skip_queries=skip, min_score=min_score)
        except Exception as e:
            logger.exception(f"[TrendDetector] region={region.key} failed: {e}")
            results[region.key] = None

    return results


# ─────────────────────────────────────────────────────────────
# CLI for manual testing
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    from region_config import REGIONS, get_region

    target = sys.argv[1] if len(sys.argv) > 1 else "world"
    region = get_region(target)
    if not region:
        print(f"Invalid region: {target}. Valid: {[r.key for r in REGIONS]}")
        sys.exit(1)

    trend = detect_top_trend(region)
    if not trend:
        print(f"No qualifying trend detected for region '{target}'.")
        sys.exit(1)

    print()
    print("=" * 60)
    print(f"Top trend for region '{target}':")
    print(json.dumps({
        "query": trend.query,
        "region": trend.region,
        "trend_score": trend.trend_score,
        "google_trends_traffic": trend.google_trends_traffic,
        "cross_source_count": trend.cross_source_count,
        "sources_seen_on": trend.sources_seen_on,
        "related_articles_count": len(trend.related_articles),
        "related_articles": trend.related_articles[:5],
        "detected_at": trend.detected_at,
    }, indent=2))
    print("=" * 60)
