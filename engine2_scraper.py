"""
engine2_scraper.py - SFAAM Automated News Engine V2 (Clean Rebuild)
======================================================================
STEP 3 of the 6-step workflow: Full Article Scrape (Text + Images)

Not just facts — the FULL article body + images are scraped per source,
per the spec:
    - title, full body text, all images (src/alt/caption), author,
      publication date, source URL.
    - Max 5 articles per region per cycle.

Anti-blocking measures:
    - Realistic User-Agent header
    - 2-3s delay between requests
    - cloudscraper fallback for Cloudflare-protected sites
    - httpx async client for parallel fetching (bounded concurrency)
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 15.0
MIN_DELAY_S = 2.0
MAX_DELAY_S = 3.0
MAX_CONCURRENCY = 3
MIN_IMAGE_WIDTH = 300          # skip tiny icons/avatars where width is known
MAX_IMAGES_PER_ARTICLE = 6
MAX_BODY_CHARS = 20000         # guard against runaway pages


@dataclass
class ScrapedImage:
    url: str
    alt: str = ""
    caption: str = ""


@dataclass
class ScrapedArticle:
    url: str
    title: str = ""
    text: str = ""
    author: str = ""
    published: str = ""
    images: list[ScrapedImage] = field(default_factory=list)
    source_domain: str = ""


def _clean_text(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _extract_from_html(url: str, html: str) -> ScrapedArticle | None:
    soup = BeautifulSoup(html, "lxml")

    # Title: prefer og:title, fall back to <h1>, then <title>
    title = ""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    elif soup.find("h1"):
        title = soup.find("h1").get_text(strip=True)
    elif soup.title:
        title = soup.title.get_text(strip=True)

    # Author
    author = ""
    for sel in [
        {"name": "meta", "attrs": {"name": "author"}},
        {"name": "meta", "attrs": {"property": "article:author"}},
    ]:
        tag = soup.find(sel["name"], attrs=sel["attrs"])
        if tag and tag.get("content"):
            author = tag["content"].strip()
            break

    # Published date
    published = ""
    for sel in [
        {"name": "meta", "attrs": {"property": "article:published_time"}},
        {"name": "meta", "attrs": {"name": "date"}},
        {"name": "time", "attrs": {}},
    ]:
        tag = soup.find(sel["name"], attrs=sel["attrs"])
        if tag:
            published = (tag.get("content") or tag.get("datetime") or tag.get_text(strip=True) or "").strip()
            if published:
                break

    # Body: prefer <article>, else largest cluster of <p> tags
    body_container = soup.find("article")
    if not body_container:
        candidates = soup.find_all(["div", "section", "main"])
        best, best_len = None, 0
        for c in candidates:
            plen = sum(len(p.get_text()) for p in c.find_all("p", recursive=False)) + \
                   sum(len(p.get_text()) for p in c.find_all("p"))
            if plen > best_len:
                best, best_len = c, plen
        body_container = best or soup

    paragraphs = [p.get_text(" ", strip=True) for p in body_container.find_all("p")]
    text = _clean_text("\n\n".join(p for p in paragraphs if len(p) > 40))[:MAX_BODY_CHARS]

    if not text or len(text) < 200:
        return None  # not enough content — likely paywalled/blocked

    # Images: og:image (hero) + inline <img> tags within the body
    images: list[ScrapedImage] = []
    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        images.append(ScrapedImage(url=og_image["content"].strip(), alt=title, caption=title))

    for img in body_container.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue
        try:
            w = int(img.get("width", 0) or 0)
        except ValueError:
            w = 0
        if w and w < MIN_IMAGE_WIDTH:
            continue
        if any(bad in src.lower() for bad in ["icon", "logo", "avatar", "sprite", "1x1", "pixel"]):
            continue
        alt = img.get("alt", "") or title
        if src not in [i.url for i in images]:
            images.append(ScrapedImage(url=src, alt=alt, caption=alt))
        if len(images) >= MAX_IMAGES_PER_ARTICLE:
            break

    return ScrapedArticle(
        url=url,
        title=title or "Untitled",
        text=text,
        author=author,
        published=published,
        images=images[:MAX_IMAGES_PER_ARTICLE],
        source_domain=urlparse(url).netloc.replace("www.", ""),
    )


async def _fetch_with_httpx(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        resp = await client.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
        if resp.status_code == 200 and resp.text:
            return resp.text
        logger.info(f"[engine2_scraper] httpx got {resp.status_code} for {url}")
        return None
    except Exception as e:
        logger.info(f"[engine2_scraper] httpx failed for {url}: {type(e).__name__}: {e}")
        return None


def _fetch_with_cloudscraper(url: str) -> str | None:
    """Sync fallback for Cloudflare-protected sites. Run via asyncio.to_thread."""
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper()
        resp = scraper.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
        if resp.status_code == 200 and resp.text:
            return resp.text
        return None
    except Exception as e:
        logger.info(f"[engine2_scraper] cloudscraper failed for {url}: {type(e).__name__}: {e}")
        return None


async def scrape_one(client: httpx.AsyncClient, url: str) -> ScrapedArticle | None:
    html = await _fetch_with_httpx(client, url)
    if not html:
        html = await asyncio.to_thread(_fetch_with_cloudscraper, url)
    if not html:
        return None
    try:
        return _extract_from_html(url, html)
    except Exception as e:
        logger.warning(f"[engine2_scraper] parse failed for {url}: {type(e).__name__}: {e}")
        return None


async def scrape_batch(urls: list[str], max_articles: int = 5) -> list[ScrapedArticle]:
    """Scrape up to `max_articles` URLs, with polite delays + bounded concurrency."""
    urls = urls[:max_articles]
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    results: list[ScrapedArticle] = []

    async def _worker(client: httpx.AsyncClient, url: str):
        async with sem:
            await asyncio.sleep(random.uniform(MIN_DELAY_S, MAX_DELAY_S))
            article = await scrape_one(client, url)
            if article:
                results.append(article)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        await asyncio.gather(*[_worker(client, u) for u in urls])

    logger.info(f"[engine2_scraper] scraped {len(results)}/{len(urls)} articles successfully")
    return results
