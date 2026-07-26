"""
engine2_search.py - SFAAM Automated News Engine V2 (Clean Rebuild)
====================================================================
STEP 2 of the 6-step workflow: Article Search (News Channels Talash)

Per the spec: this is search-driven, NOT a fixed list of RSS channels.
The search engine decides who wrote about the trending fact.

    Primary  : NewsAPI.org + GNews.io   (100 requests/day free tier each)
    Fallback : DuckDuckGo HTML scraping (unlimited, rate-limited)

Returns up to 5 candidate article URLs (deduplicated by domain, most
authoritative + most recent first) for engine2_scraper.py to fetch.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote_plus, urlparse

import httpx

from region_config import Region, get_newsapi_key, get_gnews_key

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 12.0
MAX_RESULTS = 5


@dataclass
class SearchResult:
    url: str
    title: str
    source: str          # domain / outlet name
    published_at: str = ""
    snippet: str = ""


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def _dedup_by_domain(results: list[SearchResult], limit: int) -> list[SearchResult]:
    seen: set[str] = set()
    out: list[SearchResult] = []
    for r in results:
        d = _domain(r.url)
        if not d or d in seen:
            continue
        seen.add(d)
        out.append(r)
        if len(out) >= limit:
            break
    return out


async def _search_newsapi(client: httpx.AsyncClient, query: str, region: Region) -> list[SearchResult]:
    key = get_newsapi_key()
    if not key:
        return []
    try:
        resp = await client.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": query,
                "language": "en",
                "sortBy": "relevancy",
                "pageSize": 10,
                "apiKey": key,
            },
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning(f"[engine2_search] NewsAPI non-200 ({resp.status_code}) for '{query}'")
            return []
        data = resp.json()
        out = []
        for a in data.get("articles", []):
            if not a.get("url") or not a.get("title"):
                continue
            out.append(SearchResult(
                url=a["url"],
                title=a["title"],
                source=(a.get("source") or {}).get("name", _domain(a["url"])),
                published_at=a.get("publishedAt", ""),
                snippet=a.get("description") or "",
            ))
        return out
    except Exception as e:
        logger.warning(f"[engine2_search] NewsAPI failed for '{query}': {type(e).__name__}: {e}")
        return []


async def _search_gnews(client: httpx.AsyncClient, query: str, region: Region) -> list[SearchResult]:
    key = get_gnews_key()
    if not key:
        return []
    try:
        params = {
            "q": query,
            "lang": "en",
            "max": 10,
            "apikey": key,
        }
        if region.gnews_country:
            params["country"] = region.gnews_country
        resp = await client.get("https://gnews.io/api/v4/search", params=params, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            logger.warning(f"[engine2_search] GNews non-200 ({resp.status_code}) for '{query}'")
            return []
        data = resp.json()
        out = []
        for a in data.get("articles", []):
            if not a.get("url") or not a.get("title"):
                continue
            out.append(SearchResult(
                url=a["url"],
                title=a["title"],
                source=(a.get("source") or {}).get("name", _domain(a["url"])),
                published_at=a.get("publishedAt", ""),
                snippet=a.get("description") or "",
            ))
        return out
    except Exception as e:
        logger.warning(f"[engine2_search] GNews failed for '{query}': {type(e).__name__}: {e}")
        return []


async def _search_duckduckgo(client: httpx.AsyncClient, query: str) -> list[SearchResult]:
    """Free, no-API-key fallback. Scrapes the HTML-only DDG results page."""
    try:
        resp = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"{query} news"},
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning(f"[engine2_search] DuckDuckGo non-200 ({resp.status_code}) for '{query}'")
            return []
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "lxml")
        out = []
        for a in soup.select("a.result__a")[:15]:
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if not href or not title:
                continue
            out.append(SearchResult(url=href, title=title, source=_domain(href)))
        return out
    except Exception as e:
        logger.warning(f"[engine2_search] DuckDuckGo failed for '{query}': {type(e).__name__}: {e}")
        return []


async def search_articles(query: str, region: Region, max_results: int = MAX_RESULTS) -> list[SearchResult]:
    """Search-driven article discovery (Step 2).

    Tries NewsAPI + GNews in parallel first (primary). If together they
    don't produce enough distinct-domain results, falls back to DuckDuckGo.
    """
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        newsapi_results, gnews_results = [], []
        try:
            import asyncio
            newsapi_results, gnews_results = await asyncio.gather(
                _search_newsapi(client, query, region),
                _search_gnews(client, query, region),
            )
        except Exception as e:
            logger.warning(f"[engine2_search] primary search error: {e}")

        combined = newsapi_results + gnews_results
        deduped = _dedup_by_domain(combined, max_results)

        if len(deduped) < max_results:
            logger.info(f"[engine2_search] only {len(deduped)} primary results for '{query}', trying DuckDuckGo fallback")
            ddg_results = await _search_duckduckgo(client, query)
            deduped = _dedup_by_domain(deduped + ddg_results, max_results)

        logger.info(f"[engine2_search] '{query}' → {len(deduped)} candidate articles")
        return deduped
