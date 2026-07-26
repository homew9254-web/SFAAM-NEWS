"""
trends_scraper.py - SFAAM NEWS V26 (Trends Pipeline)

Zero-Hallucination Content Engine — Stage 1 + Stage 2
=====================================================
Stage 1: Fetch top trending Google search queries (last 24h, daily trends RSS)
Stage 2: For each trend, deep-scrape raw facts from authoritative news domains

Authoritative domains (whitelist):
    AP News, Reuters, BBC, NYT, The Guardian, Al Jazeera, NPR,
    Wall Street Journal, Financial Times, Washington Post,
    Bloomberg, CBS News, ABC News, NBC News, Deutsche Welle

No external API keys required — uses Google Trends RSS (public) +
DuckDuckGo HTML (free) + direct article scraping via httpx + BeautifulSoup.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import quote_plus, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Authoritative News Domains (whitelist)
# ─────────────────────────────────────────────────────────────
AUTHORITATIVE_DOMAINS: set[str] = {
    "apnews.com",
    "reuters.com",
    "bbc.com", "bbc.co.uk",
    "nytimes.com",
    "theguardian.com",
    "aljazeera.com",
    "npr.org",
    "wsj.com",
    "ft.com",
    "washingtonpost.com",
    "bloomberg.com",
    "cbsnews.com",
    "abcnews.go.com",
    "nbcnews.com",
    "dw.com",
    "cnbc.com",
    "time.com",
    "newsweek.com",
    "economist.com",
    "politico.com",
    "thehill.com",
    "axios.com",
}

# Substring → domain set for fuzzy matching (e.g. "abcnews.go.com" matches "go.com")
DOMAIN_SUFFIXES = tuple(AUTHORITATIVE_DOMAINS)


def is_authoritative(url: str) -> bool:
    """Return True if the URL belongs to an authoritative news domain."""
    try:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        # remove leading "www."
        if host.startswith("www."):
            host = host[4:]
        if host in AUTHORITATIVE_DOMAINS:
            return True
        # also accept any subdomain of an authoritative domain
        for d in AUTHORITATIVE_DOMAINS:
            if host.endswith("." + d):
                return True
        return False
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# HTTP client
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


def _http_get(url: str, *, timeout: float = 20.0) -> str | None:
    """Fetch URL text. Returns None on any error or non-2xx response."""
    try:
        with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=timeout) as client:
            r = client.get(url)
            if r.status_code >= 400:
                logger.debug(f"  HTTP {r.status_code} for {url[:80]}")
                return None
            return r.text
    except Exception as e:
        logger.debug(f"  fetch error {type(e).__name__}: {url[:80]}")
        return None


# ─────────────────────────────────────────────────────────────
# STAGE 1: Google Trends RSS — top trending queries
# ─────────────────────────────────────────────────────────────
# Public Google Trends RSS endpoint — no API key required.
# Docs: https://trends.google.com/trends/trendingsearches/daily
#
# Geo options: "US", "GB", "IN", "PK", "DE", "" (empty = worldwide)
TRENDS_RSS_URL = "https://trends.google.com/trending/rss?geo={geo}"

# Fallback: Google News RSS for the World section (used if Trends RSS is empty)
GOOGLE_NEWS_WORLD_RSS = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"


@dataclass
class TrendItem:
    query: str
    traffic: str = ""              # e.g. "200K+"
    related_news_url: str = ""     # Google News URL for the query
    image_url: str = ""
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def fetch_trending_queries(geo: str = "", limit: int = 7) -> list[TrendItem]:
    """Fetch up to `limit` trending queries from Google Trends RSS.

    Filters out queries that are mostly non-Latin characters (Chinese, Japanese,
    Korean, Arabic, etc.) because our scraper is tuned for English-language
    authoritative sources. If we get fewer than `limit` valid queries after
    filtering, we supplement with the top Google News headlines.

    Args:
        geo: 2-letter country code, or "" for worldwide.
        limit: max number of trends to return.

    Returns:
        List of TrendItem. Empty list on failure.
    """
    url = TRENDS_RSS_URL.format(geo=quote_plus(geo))
    logger.info(f"[Trends] Fetching trending queries from {url}")
    items: list[TrendItem] = []

    try:
        text = _http_get(url, timeout=15.0)
        if not text:
            logger.warning("[Trends] Google Trends RSS returned empty — falling back to Google News")
            text = _http_get(GOOGLE_NEWS_WORLD_RSS, timeout=15.0)
            if not text:
                return []
            feed = feedparser.parse(text)
            for entry in feed.entries[:limit]:
                title = re.sub(r"\s+-\s+[^-]+$", "", entry.title).strip()
                if title and _is_latin_query(title):
                    items.append(TrendItem(
                        query=title,
                        related_news_url=entry.link,
                        fetched_at=datetime.now(timezone.utc),
                    ))
            return items

        # Parse Trends RSS
        feed = feedparser.parse(text)
        for entry in feed.entries:
            if len(items) >= limit:
                break
            title = (entry.title or "").strip()
            if not title:
                continue
            # Skip non-Latin queries (CJK, Arabic, etc.) — our scraper is
            # tuned for English-language authoritative sources.
            if not _is_latin_query(title):
                logger.debug(f"  Skipping non-Latin trend: '{title[:30]}'")
                continue
            # V32.1 BUGFIX: Google Trends RSS feeds use `ht_approx_traffic`,
            # not `traffic`. The old code always returned "" for traffic,
            # which silently broke the "traffic" ranking signal used by
            # trend_detector.py. Try the correct attribute first, then fall
            # back to the legacy name for older feedparser versions.
            traffic = (
                getattr(entry, "ht_approx_traffic", None)
                or getattr(entry, "traffic", None)
                or ""
            )
            news_url = getattr(entry, "news_url", "") or ""
            image_url = ""
            if hasattr(entry, "ht_picture"):
                image_url = entry.ht_picture
            elif hasattr(entry, "ht_picture_source"):
                image_url = entry.ht_picture_source
            items.append(TrendItem(
                query=title,
                traffic=str(traffic),
                related_news_url=str(news_url) if news_url else "",
                image_url=str(image_url) if image_url else "",
                fetched_at=datetime.now(timezone.utc),
            ))

        # If we got fewer than `limit` Latin queries, supplement with Google News headlines
        if len(items) < limit:
            logger.info(f"[Trends] Only {len(items)} Latin trends — supplementing with Google News headlines")
            news_text = _http_get(GOOGLE_NEWS_WORLD_RSS, timeout=15.0)
            if news_text:
                news_feed = feedparser.parse(news_text)
                for entry in news_feed.entries:
                    if len(items) >= limit:
                        break
                    title = re.sub(r"\s+-\s+[^-]+$", "", entry.title).strip()
                    if title and _is_latin_query(title) and not any(i.query == title for i in items):
                        items.append(TrendItem(
                            query=title,
                            related_news_url=entry.link,
                            fetched_at=datetime.now(timezone.utc),
                        ))

        logger.info(f"[Trends] Got {len(items)} trending queries (after Latin filter)")
    except Exception as e:
        logger.error(f"[Trends] Failed to fetch trends: {type(e).__name__}: {e}")

    return items


def _is_latin_query(query: str) -> bool:
    """Return True if the query is predominantly Latin-script (English-friendly).

    Filters out queries that are mostly CJK / Arabic / Cyrillic / etc.
    A query is considered Latin if at least 60% of its alphabetic characters
    are in ANY Latin Unicode block (Basic Latin, Latin-1 Supplement, Latin
    Extended-A/B/C/D, Latin Extended Additional, etc.).

    V32.1 BUGFIX: The old check `\u0041 <= c.lower() <= \u007a` only matched
    A-Z. Accented characters used by German (ü, ä, ö, ß), French (é, è, ç),
    Spanish (ñ, ¿, ¡), Italian (à, ò, ù), Portuguese (ã, õ, ç), and the
    Scandinavian languages (å, æ, ø) were treated as non-Latin, so trends
    like "Müller Wahl", "Café Français", "Niño migrante" were silently
    dropped. The fix uses Python's `unicodedata` module to detect the
    Unicode category starting with 'L' AND character name starting with
    'LATIN', which matches every Latin extended block.
    """
    if not query:
        return False
    import unicodedata
    alpha_chars = [c for c in query if c.isalpha()]
    if not alpha_chars:
        return False
    latin_count = 0
    for c in alpha_chars:
        try:
            name = unicodedata.name(c, "")
            if name.startswith("LATIN"):
                latin_count += 1
                continue
        except ValueError:
            pass
        # Fallback: Basic Latin A-Z (already covered by name check, but
        # defensive in case unicodedata.name returns empty).
        if '\u0041' <= c <= '\u005a' or '\u0061' <= c <= '\u007a':
            latin_count += 1
    return (latin_count / len(alpha_chars)) >= 0.6


# ─────────────────────────────────────────────────────────────
# STAGE 2a: Find authoritative article URLs for a trend
# ─────────────────────────────────────────────────────────────
# We use Google News RSS filtered by query — this returns recent articles
# from many sources, and we filter to only the authoritative domains.
GOOGLE_NEWS_SEARCH_RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def find_authoritative_urls(query: str, max_results: int = 15) -> list[str]:
    """Search for a query and return URLs from authoritative news domains only.

    V29 FIX: Now ALWAYS runs both DuckDuckGo AND Google News in parallel,
    then merges + deduplicates by domain (max 2 URLs per domain to ensure
    source diversity). Previously, if DuckDuckGo returned even 1 URL,
    Google News was skipped — causing single-source articles.

    Args:
        query: search query string
        max_results: cap on number of URLs to return

    Returns:
        List of article URLs from authoritative domains (diversified across domains).
    """
    # Strategy 1: DuckDuckGo HTML search — returns direct URLs
    ddg_urls = _ddg_search_authoritative(query, max_results=max_results * 2)

    # Strategy 2: Google News RSS — ALWAYS run (not just as fallback)
    gn_urls = _google_news_search_authoritative(query, max_results=max_results * 2)

    # Merge: DDG first (direct URLs, more reliable for scraping), then GN
    seen: set[str] = set()
    merged: list[str] = []
    for url in ddg_urls + gn_urls:
        if url not in seen:
            seen.add(url)
            merged.append(url)

    # V29 FIX: Domain diversification — max 2 URLs per domain
    # This prevents one source (e.g. bbc.com) from dominating all results.
    domain_count: dict[str, int] = {}
    diversified: list[str] = []
    for url in merged:
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        domain_count[host] = domain_count.get(host, 0) + 1
        if domain_count[host] <= 2:  # max 2 URLs per domain
            diversified.append(url)
        if len(diversified) >= max_results:
            break

    logger.info(
        f"[Trends] find_authoritative_urls('{query[:40]}'): "
        f"DDG={len(ddg_urls)}, GN={len(gn_urls)}, merged={len(merged)}, diversified={len(diversified)}"
    )
    return diversified


# DuckDuckGo HTML search endpoint (no API key required)
DDG_HTML_URL = "https://html.duckduckgo.com/html/?q={q}"


def _ddg_search_authoritative(query: str, max_results: int) -> list[str]:
    """Search DuckDuckGo HTML endpoint and return authoritative URLs.

    DDG returns direct article URLs in result snippets — no redirect wrappers.
    We try multiple result-page parses to find up to `max_results` URLs
    from authoritative domains.
    """
    urls: list[str] = []
    seen: set[str] = set()

    # DDG sometimes returns nothing on the first request (anti-bot), so we
    # try the HTML endpoint with a real browser User-Agent. If we get any
    # results, we use them. If we don't, we fall through to Google News.
    url = DDG_HTML_URL.format(q=quote_plus(query))
    text = _http_get(url, timeout=15.0)
    if not text:
        return []
    try:
        soup = BeautifulSoup(text, "lxml")
        # DDG HTML results have links like:
        #   <a class="result__a" href="https://example.com/article">...</a>
        # OR sometimes:
        #   <a class="result__url" href="...">
        for a in soup.select("a.result__a, a.result__url, a.result-link"):
            href = a.get("href", "")
            if not href:
                continue
            # DDG sometimes wraps URLs in a redirect like:
            #   //duckduckgo.com/l/?uddg=https%3A%2F%2F...
            if "uddg=" in href:
                from urllib.parse import parse_qs, urlparse as _up
                qs = parse_qs(_up(href).query)
                if "uddg" in qs:
                    href = qs["uddg"][0]
            if href.startswith("//"):
                href = "https:" + href
            if href in seen:
                continue
            if is_authoritative(href):
                seen.add(href)
                urls.append(href)
            if len(urls) >= max_results:
                break
        return urls
    except Exception as e:
        logger.debug(f"  DDG parse error: {e}")
        return []


# Authoritative source name → domain mapping (for Google News RSS source field)
SOURCE_NAME_TO_DOMAIN = {
    "the new york times": "nytimes.com",
    "new york times": "nytimes.com",
    "reuters": "reuters.com",
    "bbc news": "bbc.com",
    "the bbc": "bbc.com",
    "the guardian": "theguardian.com",
    "guardian": "theguardian.com",
    "al jazeera": "aljazeera.com",
    "npr": "npr.org",
    "associated press": "apnews.com",
    "ap news": "apnews.com",
    "the ap": "apnews.com",
    "wall street journal": "wsj.com",
    "financial times": "ft.com",
    "the washington post": "washingtonpost.com",
    "washington post": "washingtonpost.com",
    "bloomberg": "bloomberg.com",
    "cbs news": "cbsnews.com",
    "abc news": "abcnews.go.com",
    "nbc news": "nbcnews.com",
    "deutsche welle": "dw.com",
    "cnbc": "cnbc.com",
    "time": "time.com",
    "newsweek": "newsweek.com",
    "the economist": "economist.com",
    "politico": "politico.com",
    "the hill": "thehill.com",
    "axios": "axios.com",
    "pbs": "pbs.org",
    "pbs newshour": "pbs.org",
}


def _google_news_search_authoritative(query: str, max_results: int) -> list[str]:
    """Search Google News RSS and return URLs from authoritative sources.

    Since Google News wraps URLs in redirects, we match by the entry's
    source title (e.g. "BBC News", "Reuters") to our authoritative
    domain list. We then try to resolve the redirect — but if resolution
    fails or is slow, we SKIP that entry (we don't return unresolved
    news.google.com URLs because the scraper can't extract body from them).
    """
    url = GOOGLE_NEWS_SEARCH_RSS.format(q=quote_plus(query))
    text = _http_get(url, timeout=15.0)
    if not text:
        return []
    try:
        feed = feedparser.parse(text)
        urls: list[str] = []
        for entry in feed.entries:
            # Get the source name
            source_name = ""
            if hasattr(entry, "source") and hasattr(entry.source, "title"):
                source_name = entry.source.title.lower().strip()
            if not source_name:
                # Try to extract from title "... - Publisher" pattern
                title = getattr(entry, "title", "") or ""
                if " - " in title:
                    source_name = title.rsplit(" - ", 1)[-1].lower().strip()

            # Match against our known authoritative sources
            matched_domain = ""
            for name_key, domain in SOURCE_NAME_TO_DOMAIN.items():
                if name_key in source_name:
                    matched_domain = domain
                    break

            if not matched_domain:
                continue

            # Resolve the Google News URL (with a short timeout — if it
            # takes too long, skip and move on)
            link = getattr(entry, "link", "") or ""
            if not link:
                continue

            # Try resolving with a strict timeout
            resolved = _resolve_google_news_url(link)
            if resolved and is_authoritative(resolved):
                urls.append(resolved)
            # If resolution failed, SKIP — we can't scrape news.google.com URLs
            # for body content (they're JS-rendered pages, not articles).

            if len(urls) >= max_results:
                break
        return urls
    except Exception as e:
        logger.debug(f"  Google News parse error: {e}")
        return []


def _resolve_google_news_url(google_news_url: str) -> str:
    """Resolve a Google News wrapper URL to the final article URL.

    V29 FIX: Increased timeout from 6s to 12s — Railway's network is slower
    and many URLs were timing out, reducing source diversity.
    """
    try:
        with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=12.0) as client:
            r = client.get(google_news_url)
            return str(r.url)
    except Exception as e:
        logger.debug(f"  resolve error: {e}")
        return ""


# ─────────────────────────────────────────────────────────────
# STAGE 2b: Scrape article body text
# ─────────────────────────────────────────────────────────────
# We extract the main article body, strip nav/ads/footer, and return
# clean paragraph text. This is the raw material for fact verification.

# Tags that NEVER contain article body text
_DROP_TAGS = (
    "script", "style", "noscript", "iframe", "svg",
    "nav", "header", "footer", "aside",
    "form", "button",
)

# Common article body container selectors by domain (ordered by specificity)
ARTICLE_BODY_SELECTORS = [
    # AP News
    ("apnews.com",     "div.Article"),
    ("apnews.com",     "div.RichTextStoryBody"),
    # Reuters
    ("reuters.com",    "div.article-body__content__17Uit"),
    ("reuters.com",    "article p"),
    # BBC
    ("bbc.com",        "article div[data-component='text-block']"),
    ("bbc.co.uk",      "article div[data-component='text-block']"),
    ("bbc.com",        "div[data-testid='article-body'] p"),
    # NYT
    ("nytimes.com",    "section[name='articleBody'] p"),
    ("nytimes.com",    "article p"),
    # The Guardian
    ("theguardian.com", "div.article-body-commercial-selector p"),
    ("theguardian.com", "div#maincontent p"),
    # Al Jazeera
    ("aljazeera.com",  "article p"),
    ("aljazeera.com",  "div.wysiwyg p"),
    # NPR
    ("npr.org",        "div#storytext p"),
    # Generic fallbacks
    ("",               "article p"),
    ("",               "main p"),
    ("",               "div.article-body p"),
    ("",               "div.story-body p"),
]


def scrape_article_body(url: str, *, max_chars: int = 8000) -> str:
    """Fetch and extract clean article body text from a URL.

    Args:
        url: article URL
        max_chars: maximum body length to return (truncated to nearest sentence)

    Returns:
        Cleaned article text, or empty string on failure.
    """
    html = _http_get(url, timeout=20.0)
    if not html:
        return ""

    try:
        soup = BeautifulSoup(html, "lxml")

        # Remove non-content tags
        for tag in soup(list(_DROP_TAGS)):
            tag.decompose()

        # Try domain-specific selectors first
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]

        body_text = ""
        for domain_host, selector in ARTICLE_BODY_SELECTORS:
            if domain_host and not host.endswith(domain_host):
                continue
            paragraphs = soup.select(selector)
            if paragraphs:
                body_text = "\n\n".join(
                    p.get_text(separator=" ", strip=True)
                    for p in paragraphs
                    if p.get_text(strip=True)
                )
                if body_text:
                    break

        # Final fallback: all <p> tags
        if not body_text:
            paragraphs = soup.find_all("p")
            body_text = "\n\n".join(
                p.get_text(separator=" ", strip=True)
                for p in paragraphs
                if len(p.get_text(strip=True)) > 30
            )

        # Truncate to max_chars on a sentence boundary
        if len(body_text) > max_chars:
            # find last sentence boundary before max_chars
            cut = body_text.rfind(". ", 0, max_chars)
            if cut == -1:
                cut = max_chars
            body_text = body_text[: cut + 1].strip()

        # Also fetch the page <title> for the source metadata
        title = ""
        if soup.title:
            title = soup.title.get_text(strip=True)[:300]

        # Build a small header so the verifier sees the source context
        header = f"# Source: {host}\n# URL: {url}\n# Title: {title}\n"
        return header + body_text
    except Exception as e:
        logger.debug(f"  scrape error {type(e).__name__} for {url[:80]}")
        return ""


# ─────────────────────────────────────────────────────────────
# STAGE 2c: Aggregate facts from multiple sources for one trend
# ─────────────────────────────────────────────────────────────
@dataclass
class ScrapedSource:
    url: str
    domain: str
    title: str
    snippet: str       # first ~400 chars of body
    full_text: str     # full cleaned body text (truncated to ~8000 chars)


@dataclass
class TrendResearchResult:
    query: str
    sources: list[ScrapedSource]
    total_chars: int


def research_trend(query: str, max_sources: int = 5) -> TrendResearchResult:
    """End-to-end research: find authoritative URLs → scrape each → return sources.

    V29 FIX: Now ensures domain diversity by requesting more URLs and
    capping at 2 per domain, so no single source dominates.

    Args:
        query: trending search query
        max_sources: max number of authoritative sources to fully scrape

    Returns:
        TrendResearchResult with all scraped sources. Empty sources list on failure.
    """
    logger.info(f"[Trends] Researching: '{query}'")
    urls = find_authoritative_urls(query, max_results=max_sources * 4)

    # V29 FIX: Domain diversification — max 2 URLs per domain
    # Ensures multiple perspectives even if one domain dominates search results.
    domain_count: dict[str, int] = {}
    diversified_urls: list[str] = []
    for url in urls:
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        domain_count[host] = domain_count.get(host, 0) + 1
        if domain_count[host] <= 2:  # max 2 per domain
            diversified_urls.append(url)
        if len(diversified_urls) >= max_sources:
            break

    urls = diversified_urls

    sources: list[ScrapedSource] = []
    for url in urls:
        # small delay to be polite
        time.sleep(0.3)
        body = scrape_article_body(url)
        if not body or len(body) < 200:
            continue

        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]

        # Extract title from header
        title = ""
        for line in body.split("\n"):
            if line.startswith("# Title:"):
                title = line.replace("# Title:", "").strip()
                break

        # Strip the header for the snippet
        body_without_header = "\n".join(
            line for line in body.split("\n")
            if not line.startswith("#")
        ).strip()

        snippet = body_without_header[:400]
        sources.append(ScrapedSource(
            url=url,
            domain=host,
            title=title,
            snippet=snippet,
            full_text=body_without_header,
        ))

    total_chars = sum(len(s.full_text) for s in sources)
    logger.info(f"[Trends] '{query[:50]}': {len(sources)} sources scraped, {total_chars} chars total")
    return TrendResearchResult(query=query, sources=sources, total_chars=total_chars)


# ─────────────────────────────────────────────────────────────
# CLI for manual testing
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("=== Stage 1: Fetch trending queries ===")
    trends = fetch_trending_queries(geo="US", limit=7)
    for i, t in enumerate(trends, 1):
        print(f"  {i}. {t.query}  (traffic: {t.traffic})")

    if not trends:
        print("No trends found.")
        raise SystemExit(0)

    print()
    print("=== Stage 2: Research first trend ===")
    result = research_trend(trends[0].query, max_sources=3)
    print(f"Query: {result.query}")
    print(f"Sources: {len(result.sources)}")
    for s in result.sources:
        print(f"\n--- {s.domain} ---")
        print(f"URL:   {s.url}")
        print(f"Title: {s.title}")
        print(f"Snippet ({len(s.snippet)} chars):\n{s.snippet[:300]}...")
