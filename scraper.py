"""
scraper.py - SFAAM NEWS V7 (Async httpx + Parallel Feed Fetching)
- 5 regions: world, usa, uk, pakistan, india
- Async httpx for parallel feed + body fetching
- cloudscraper kept as a sync fallback (Cloudflare bypass only)
- Multiple content-selector fallbacks for body extraction
- Image URL validation + tracking pixel filtering
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# cloudscraper only used as a sync fallback for Cloudflare-protected sites
try:
    import cloudscraper
    _CLOUDSCRAPER = cloudscraper.create_scraper()
except ImportError:
    _CLOUDSCRAPER = None
    logger.info("cloudscraper not available — using httpx only")

# lxml is faster; fall back to html.parser if missing
try:
    from lxml import etree  # noqa: F401
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"


# ── RSS Sources (V8 — World news reduced to user's preferred 5 sources) ──
RSS_SOURCES = {
    # V8: Per user request — World news ONLY from these 5 premium sources
    "world": [
        {"url": "http://feeds.bbci.co.uk/news/world/rss.xml", "name": "BBC News"},
        {"url": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "name": "The New York Times"},
        {"url": "https://feeds.reuters.com/reuters/worldNews", "name": "Reuters"},
        {"url": "https://www.aljazeera.com/xml/rss/all.xml", "name": "Al Jazeera"},
        {"url": "http://rss.cnn.com/rss/edition_world.rss", "name": "CNN"},
    ],
    "usa": [
        {"url": "http://rss.cnn.com/rss/edition_us.rss", "name": "CNN US"},
        {"url": "https://rss.nytimes.com/services/xml/rss/nyt/US.xml", "name": "NYT US"},
        {"url": "https://feeds.washingtonpost.com/rss/national", "name": "Washington Post"},
        {"url": "https://rssfeeds.usatoday.com/usatoday-NewsTopStories", "name": "USA Today"},
    ],
    "uk": [
        {"url": "http://feeds.bbci.co.uk/news/uk/rss.xml", "name": "BBC UK"},
        {"url": "https://www.theguardian.com/uk/rss", "name": "Guardian UK"},
        {"url": "https://feeds.skynews.com/feeds/rss/uk.xml", "name": "Sky News UK"},
        {"url": "https://www.independent.co.uk/rss", "name": "Independent"},
    ],
    "pakistan": [
        {"url": "https://www.dawn.com/feeds/home", "name": "Dawn"},
        {"url": "https://arynews.tv/feed/", "name": "ARY News"},
        {"url": "https://www.thenews.com.pk/rss/1/1", "name": "The News"},
        {"url": "https://tribune.com.pk/feed/", "name": "Express Tribune"},
    ],
    "india": [
        {"url": "https://timesofindia.indiatimes.com/rssfeedstopstories.cms", "name": "Times of India"},
        {"url": "https://feeds.feedburner.com/ndtvnews-top-stories", "name": "NDTV"},
        {"url": "https://www.thehindu.com/feeder/default.rss", "name": "The Hindu"},
        {"url": "https://www.indiatoday.in/rss/1206578", "name": "India Today"},
    ],
    "germany": [
        {"url": "https://www.spiegel.de/international/index.rss", "name": "Der Spiegel"},
        {"url": "https://www.dw.com/en/rss-en/7853", "name": "Deutsche Welle"},
        {"url": "https://www.thelocal.de/feeds/rss.php", "name": "The Local Germany"},
        {"url": "https://www.handelsblatt.com/contentexport/feed/schlagzeilen", "name": "Handelsblatt"},
    ],
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,en-GB;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


# ── Feed fetching ──

def _fetch_feed_sync(source_url: str, region: str, source_name: str = "Unknown") -> list[dict]:
    """feedparser doesn't have a real async API, so we run it sync.
    The async wrapper below parallelizes across sources using a thread pool."""
    try:
        feed = feedparser.parse(source_url)
        if feed.bozo and hasattr(feed, "status") and feed.status >= 400:
            logger.warning(f"Feed HTTP error [{source_name}]: {feed.status}")
            return []

        result = []
        for entry in feed.entries[:12]:
            summary_text = ""
            if hasattr(entry, "summary") and entry.summary:
                try:
                    summary_text = BeautifulSoup(entry.summary, PARSER).get_text()[:400]
                except Exception:
                    summary_text = entry.summary[:400]

            result.append({
                "title":       entry.get("title", "").strip(),
                "url":         entry.get("link", "").strip(),
                "summary":     summary_text,
                "image_url":   _get_image(entry),
                "source_name": source_name,
                "region":      region,
                "published":   entry.get("published", ""),
            })
        logger.info(f"  [{region}] {source_name}: {len(result)} articles")
        return result
    except Exception as e:
        logger.warning(f"Feed error [{source_name}]: {e}")
        return []


async def _fetch_feed(client: httpx.AsyncClient, source: dict, region: str) -> list[dict]:
    """Async wrapper — fetches feed text via httpx, then parses with feedparser.
    V9: Detailed logging so user can see exactly which feeds are working/failing."""
    url, name = source["url"], source.get("name", "Unknown")
    try:
        # feedparser.parse can accept raw bytes, which avoids a second HTTP fetch
        resp = await client.get(url, timeout=15, follow_redirects=True)
        if resp.status_code >= 400:
            logger.warning(f"  [FEED FAIL] {name} ({region}): HTTP {resp.status_code}")
            return []
        feed = feedparser.parse(resp.content)
        if not feed.entries:
            logger.warning(f"  [FEED EMPTY] {name} ({region}): 0 entries (feed may be broken)")
            return []
    except Exception as e:
        logger.warning(f"  [FEED ERROR] {name} ({region}): {type(e).__name__}: {str(e)[:100]}")
        return []

    result = []
    for entry in feed.entries[:12]:
        summary_text = ""
        if hasattr(entry, "summary") and entry.summary:
            try:
                summary_text = BeautifulSoup(entry.summary, PARSER).get_text()[:400]
            except Exception:
                summary_text = entry.summary[:400]
        result.append({
            "title":       entry.get("title", "").strip(),
            "url":         entry.get("link", "").strip(),
            "summary":     summary_text,
            "image_url":   _get_image(entry),
            "source_name": name,
            "region":      region,
            "published":   entry.get("published", ""),
        })
    logger.info(f"  [FEED OK] {name} ({region}): {len(result)} entries")
    return result


# ── Article body scraping (async httpx with cloudscraper fallback) ──

async def scrape_body(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """Async body scrape with multiple fallback strategies.
    V8: Returns (body_text, image_url) — extracts the article's main
    image from the page HTML as a fallback when RSS didn't include one."""
    body = ""
    image_url = ""
    html_text = ""

    # Strategy 1: async httpx
    try:
        r = await client.get(url, timeout=15, follow_redirects=True)
        if r.status_code == 200:
            html_text = r.text
            body = _extract_text(html_text)
            # Try to extract image even if body succeeded
            image_url = _extract_image_from_html(html_text, url)
            if body and len(body) > 200:
                return body, image_url
    except Exception as e:
        logger.debug(f"httpx failed [{url[:60]}]: {e}")

    # Strategy 2: cloudscraper (sync, Cloudflare bypass) — run in thread
    if _CLOUDSCRAPER:
        try:
            loop = asyncio.get_running_loop()
            html_text = await loop.run_in_executor(None, _cloudscraper_fetch_html, url)
            if html_text:
                body = _extract_text(html_text)
                image_url = _extract_image_from_html(html_text, url)
                if body and len(body) > 200:
                    return body, image_url
        except Exception as e:
            logger.debug(f"cloudscraper failed [{url[:60]}]: {e}")

    return body, image_url


def _cloudscraper_fetch_html(url: str) -> str:
    """Sync cloudscraper call — returns raw HTML. Wrapped in run_in_executor by caller."""
    try:
        r = _CLOUDSCRAPER.get(url, timeout=15, headers=HEADERS)
        if r.status_code == 200:
            return r.text
    except Exception as e:
        logger.debug(f"cloudscraper error: {e}")
    return ""


def _cloudscraper_fetch(url: str) -> str:
    """Legacy sync wrapper — returns extracted text only."""
    html = _cloudscraper_fetch_html(url)
    return _extract_text(html) if html else ""


def _extract_text(html: str) -> str:
    """Extract readable text from HTML with multiple content selectors."""
    try:
        soup = BeautifulSoup(html, PARSER)
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return ""

    # Remove unwanted tags
    for tag in soup(["script", "style", "nav", "footer", "aside", "header", "form", "iframe", "noscript", "svg"]):
        tag.decompose()

    content_selectors = [
        "article", "main", ".article-body", ".story-body", ".post-content",
        "#content", ".article__body", ".entry-content", ".article-content",
        ".story-content", ".news-content", "[itemprop='articleBody']",
        ".body-content", ".article-text",
    ]
    for sel in content_selectors:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 500:
                return text[:8000]

    paragraphs = soup.find_all("p")
    if paragraphs:
        text = "\n\n".join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 50)
        if len(text) > 200:
            return text[:8000]

    return soup.get_text(separator="\n", strip=True)[:8000]


# ── V8: Better image extraction from article body ──
# Tries multiple strategies because every publisher uses different markup.
# This is what fixes the "no images on world news" issue.
IMAGE_SELECTORS = [
    "article img",
    "main img",
    ".article-body img",
    ".story-body img",
    ".post-content img",
    ".article__body img",
    ".entry-content img",
    ".article-content img",
    ".story-content img",
    "[itemprop='articleBody'] img",
    ".body-content img",
    "figure img",
    "picture source",
    "img[srcset]",
]


def _extract_image_from_html(html: str, article_url: str = "") -> str:
    """Try to find the main article image from the page HTML.
    Returns a validated absolute URL, or empty string if none found."""
    try:
        soup = BeautifulSoup(html, PARSER)
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return ""

    # Strategy 1: Open Graph meta tag (most reliable for modern publishers)
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        url = _validate_image_url(og_img["content"])
        if url:
            return _absolutize(url, article_url)

    # Strategy 2: Twitter card image
    tw_img = soup.find("meta", attrs={"name": "twitter:image"})
    if tw_img and tw_img.get("content"):
        url = _validate_image_url(tw_img["content"])
        if url:
            return _absolutize(url, article_url)

    # Strategy 3: schema.org ImageObject
    for meta in soup.find_all("meta", attrs={"itemprop": "image"}):
        if meta.get("content"):
            url = _validate_image_url(meta["content"])
            if url:
                return _absolutize(url, article_url)

    # Strategy 4: Walk through content-scoped image selectors
    for sel in IMAGE_SELECTORS:
        for img in soup.select(sel):
            # Skip tiny icons / tracking pixels / data URIs
            src = img.get("src") or ""
            srcset = img.get("srcset") or ""
            data_src = img.get("data-src") or img.get("data-original") or ""
            width = img.get("width", "")
            height = img.get("height", "")

            # Skip if explicitly tiny
            try:
                if width and int(width) < 100:
                    continue
                if height and int(height) < 100:
                    continue
            except (ValueError, TypeError):
                pass

            # Prefer data-src (lazy-loaded) > srcset > src
            candidate = data_src or src or ""
            if not candidate and srcset:
                # Take the first URL from srcset
                candidate = srcset.split(",")[0].split(" ")[0]

            if candidate:
                url = _validate_image_url(candidate)
                if url:
                    return _absolutize(url, article_url)

    # Strategy 5: First <img> in the page that looks like content (not icon)
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src:
            continue
        # Skip obvious icons/logos/tracking pixels
        src_lower = src.lower()
        if any(skip in src_lower for skip in ["logo", "icon", "sprite", "1x1", "pixel", "avatar", "gravatar"]):
            continue
        try:
            w = int(img.get("width", 0))
            h = int(img.get("height", 0))
            if w and w < 100:
                continue
            if h and h < 100:
                continue
        except (ValueError, TypeError):
            pass
        url = _validate_image_url(src)
        if url:
            return _absolutize(url, article_url)

    return ""


def _absolutize(url: str, base: str) -> str:
    """Convert a possibly-relative URL to absolute using the article's base URL."""
    if not url or not base:
        return url
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("//"):
        return "https:" + url
    try:
        from urllib.parse import urljoin
        return urljoin(base, url)
    except Exception:
        return url


# ── Public: get all new articles (async) ──

async def get_new_articles(processed_urls: set[str]) -> list[dict]:
    """Fetch new articles from all RSS sources in parallel."""
    new: list[dict] = []
    feed_stats = {region: {"total": 0, "new": 0} for region in RSS_SOURCES}

    async with httpx.AsyncClient(headers=HEADERS) as client:
        # ── Phase 1: Fetch all feeds in parallel ──
        feed_tasks = []
        for region, sources in RSS_SOURCES.items():
            for source in sources:
                feed_tasks.append(_fetch_feed(client, source, region))
        feed_results = await asyncio.gather(*feed_tasks, return_exceptions=True)

        # Flatten + dedupe against processed_urls
        all_entries: list[dict] = []
        for result in feed_results:
            if isinstance(result, Exception) or not result:
                continue
            for entry in result:
                if not entry.get("url") or entry["url"] in processed_urls:
                    continue
                if not entry.get("title") or len(entry["title"]) < 5:
                    continue
                feed_stats[entry["region"]]["total"] += 1
                all_entries.append(entry)

        # ── Phase 2: Scrape bodies in parallel (rate-limited concurrency) ──
        # Cap concurrent body fetches to avoid hammering publisher sites
        sem = asyncio.Semaphore(5)

        async def _bounded_scrape(entry: dict) -> Optional[dict]:
            async with sem:
                body, scraped_image = await scrape_body(client, entry["url"])
                # V9 FIX: If body scraping fails, fall back to RSS summary.
                # Previously — if BBC/NYT/etc. blocked the scraper, the article
                # was silently dropped. Now we ALWAYS save at least the RSS
                # summary so the site is never empty.
                if not body or len(body) < 100:
                    rss_summary = entry.get("summary", "")
                    if rss_summary and len(rss_summary) > 50:
                        body = rss_summary
                        logger.info(f"    Using RSS summary as body (scrape failed): {entry['title'][:40]}")
                    else:
                        logger.warning(f"    Skipping — no body AND no summary: {entry['title'][:40]}")
                        return None
                entry["full_text"] = body
                # V8: If RSS didn't give us an image, use the one we scraped
                if not entry.get("image_url") and scraped_image:
                    entry["image_url"] = scraped_image
                elif entry.get("image_url"):
                    entry["image_url"] = _validate_image_url(entry["image_url"])
                return entry

        scrape_tasks = [_bounded_scrape(e) for e in all_entries]
        scrape_results = await asyncio.gather(*scrape_tasks, return_exceptions=True)

        for r in scrape_results:
            if isinstance(r, dict):
                new.append(r)
                feed_stats[r["region"]]["new"] += 1

    for region, stats in feed_stats.items():
        logger.info(f"  [{region.upper()}] {stats['new']}/{stats['total']} new articles")
    logger.info(f"[SFAAM NEWS V26] {len(new)} new articles found")
    return new


# ── Image helpers ──

def _get_image(entry) -> str:
    """Extract image URL from an RSS entry."""
    if hasattr(entry, "media_content") and entry.media_content:
        for media in entry.media_content:
            url = media.get("url", "")
            if url:
                return url
    if hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            if enc.get("type", "").startswith("image"):
                return enc.get("href", "")
    if hasattr(entry, "summary") and entry.summary and "<img" in entry.summary:
        try:
            soup = BeautifulSoup(entry.summary, PARSER)
            img = soup.find("img")
            if img:
                return img.get("src", "")
        except Exception:
            pass
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        return entry.media_thumbnail[0].get("url", "")
    return ""


def _validate_image_url(url: str) -> str:
    """Validate and clean image URL.
    V19: Forces HTTPS to avoid mixed-content blocking when the site is served over HTTPS."""
    if not url:
        return ""
    # V19: Force HTTPS — browsers block HTTP images on HTTPS sites (mixed content)
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""
    if not parsed.netloc:
        return ""
    url_lower = url.lower()
    if any(ext in url_lower for ext in ["1x1", "tracking", "pixel", "beacon"]):
        return ""
    return url
