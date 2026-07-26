"""
pro_sitemaps.py — SFAAM NEWS PRO 1 — Split sitemaps + Schema.org
=================================================================

Production-grade sitemap strategy for a news site targeting Google
News inclusion and maximum crawl efficiency.

Sitemap structure:
  /sitemap.xml                 — sitemap INDEX pointing to all sub-sitemaps
  /sitemap-articles.xml        — latest 50000 articles
  /sitemap-articles-archive.xml — older articles (paged by month)
  /sitemap-categories.xml      — /category/{region} pages
  /sitemap-topics.xml          — /topic/{slug} pages
  /sitemap-authors.xml         — /author/{slug} pages
  /sitemap-news.xml            — Google News sitemap (last 48h only)
  /sitemap-static.xml          — /, /about.html, /contact.html, etc.

Schema.org helpers:
  - news_article_schema()       — NewsArticle with full ClaimReview support
  - organization_schema()       — Organization (for footer / homepage)
  - web_site_schema()           — WebSite with SearchAction
  - person_schema()             — author Person
  - breadcrumb_schema()         — BreadcrumbList
  - faq_page_schema()           — FAQPage (extracted from article body)
  - claim_review_schema()       — ClaimReview (for fact-check pieces)
"""
from __future__ import annotations

import re
import logging
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote_plus

from fastapi import FastAPI, Request, Depends
from fastapi.responses import Response, JSONResponse
from sqlalchemy import select, text, or_
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Site URL (must be set in env for correct sitemap URLs)
# ─────────────────────────────────────────────────────────────
import os
SITE_URL = os.getenv("SITE_URL", "https://sfaamnews.com").rstrip("/")


# ─────────────────────────────────────────────────────────────
# XML helpers
# ─────────────────────────────────────────────────────────────

XML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n'


def _xml_escape(s: str) -> str:
    if not s:
        return ""
    return (s
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


def _iso_date(d) -> str:
    if not d:
        return datetime.utcnow().strftime("%Y-%m-%d")
    if isinstance(d, str):
        try:
            d = datetime.fromisoformat(d.replace("Z", ""))
        except Exception:
            return datetime.utcnow().strftime("%Y-%m-%d")
    return d.strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────
# Sitemap index
# ─────────────────────────────────────────────────────────────

async def sitemap_index(request: Request, db: AsyncSession = None) -> Response:
    """Sitemap index pointing to all sub-sitemaps."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    sub_sitemaps = [
        ("/sitemap-articles.xml", today, "1.0"),
        ("/sitemap-articles-archive.xml", today, "0.6"),
        ("/sitemap-categories.xml", today, "0.9"),
        ("/sitemap-topics.xml", today, "0.8"),
        ("/sitemap-authors.xml", today, "0.7"),
        ("/sitemap-news.xml", today, "1.0"),
        ("/sitemap-static.xml", today, "0.5"),
    ]
    body = XML_HEADER + '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for loc, lastmod, priority in sub_sitemaps:
        body += (
            f"  <sitemap>\n"
            f"    <loc>{SITE_URL}{loc}</loc>\n"
            f"    <lastmod>{lastmod}</lastmod>\n"
            f"  </sitemap>\n"
        )
    body += "</sitemapindex>"
    return Response(content=body, media_type="application/xml")


# ─────────────────────────────────────────────────────────────
# Articles sitemap (latest 50K)
# ─────────────────────────────────────────────────────────────

async def sitemap_articles(request: Request, db: AsyncSession) -> Response:
    """Latest 50,000 articles."""
    result = await db.execute(text("""
        SELECT id, slug, title, date, updated_at, region
        FROM articles
        WHERE (status = 'published' OR status IS NULL)
        ORDER BY date DESC
        LIMIT 50000
    """))
    rows = result.fetchall()
    body = XML_HEADER + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for r in rows:
        slug = r.slug or f"id-{r.id}"
        lastmod = _iso_date(r.updated_at or r.date)
        body += (
            f"  <url>\n"
            f"    <loc>{SITE_URL}/article/{_xml_escape(slug)}</loc>\n"
            f"    <lastmod>{lastmod}</lastmod>\n"
            f"    <changefreq>weekly</changefreq>\n"
            f"    <priority>0.8</priority>\n"
            f"  </url>\n"
        )
    body += "</urlset>"
    return Response(content=body, media_type="application/xml")


# ─────────────────────────────────────────────────────────────
# Archive sitemap (older articles)
# ─────────────────────────────────────────────────────────────

async def sitemap_archive(request: Request, db: AsyncSession) -> Response:
    """Articles older than the latest 50K (capped at 50K per sitemap)."""
    result = await db.execute(text("""
        SELECT id, slug, date, updated_at
        FROM articles
        WHERE (status = 'published' OR status IS NULL)
        ORDER BY date DESC
        LIMIT 50000 OFFSET 50000
    """))
    rows = result.fetchall()
    body = XML_HEADER + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for r in rows:
        slug = r.slug or f"id-{r.id}"
        body += (
            f"  <url>\n"
            f"    <loc>{SITE_URL}/article/{_xml_escape(slug)}</loc>\n"
            f"    <lastmod>{_iso_date(r.updated_at or r.date)}</lastmod>\n"
            f"    <changefreq>monthly</changefreq>\n"
            f"    <priority>0.5</priority>\n"
            f"  </url>\n"
        )
    body += "</urlset>"
    return Response(content=body, media_type="application/xml")


# ─────────────────────────────────────────────────────────────
# Categories sitemap
# ─────────────────────────────────────────────────────────────

REGIONS = ["world", "us", "uk", "pk", "in", "de"]
CATEGORIES = ["politics", "economy", "sports", "tech", "science", "health", "culture"]


async def sitemap_categories(request: Request) -> Response:
    """All /category/{region} and /category/{region}/{topic} pages."""
    body = XML_HEADER + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    today = datetime.utcnow().strftime("%Y-%m-%d")
    for region in REGIONS:
        body += (
            f"  <url>\n"
            f"    <loc>{SITE_URL}/category/{region}</loc>\n"
            f"    <lastmod>{today}</lastmod>\n"
            f"    <changefreq>hourly</changefreq>\n"
            f"    <priority>1.0</priority>\n"
            f"  </url>\n"
        )
        for cat in CATEGORIES:
            body += (
                f"  <url>\n"
                f"    <loc>{SITE_URL}/category/{region}/{cat}</loc>\n"
                f"    <lastmod>{today}</lastmod>\n"
                f"    <changefreq>hourly</changefreq>\n"
                f"    <priority>0.9</priority>\n"
                f"  </url>\n"
            )
    body += "</urlset>"
    return Response(content=body, media_type="application/xml")


# ─────────────────────────────────────────────────────────────
# Topics sitemap
# ─────────────────────────────────────────────────────────────

async def sitemap_topics(request: Request, db: AsyncSession) -> Response:
    """All /topic/{slug} pages."""
    try:
        result = await db.execute(text("""
            SELECT slug, title, updated_at FROM pro_topics
            WHERE status = 'active' ORDER BY updated_at DESC LIMIT 50000
        """))
        rows = result.fetchall()
    except Exception:
        rows = []  # table might not exist yet
    body = XML_HEADER + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for r in rows:
        body += (
            f"  <url>\n"
            f"    <loc>{SITE_URL}/topic/{_xml_escape(r.slug)}</loc>\n"
            f"    <lastmod>{_iso_date(r.updated_at)}</lastmod>\n"
            f"    <changefreq>hourly</changefreq>\n"
            f"    <priority>0.8</priority>\n"
            f"  </url>\n"
        )
    body += "</urlset>"
    return Response(content=body, media_type="application/xml")


# ─────────────────────────────────────────────────────────────
# Authors sitemap
# ─────────────────────────────────────────────────────────────

async def sitemap_authors(request: Request, db: AsyncSession) -> Response:
    """All /author/{slug} pages."""
    try:
        result = await db.execute(text("""
            SELECT slug, name, updated_at FROM pro_authors
            WHERE is_active = 1 ORDER BY updated_at DESC LIMIT 50000
        """))
        rows = result.fetchall()
    except Exception:
        rows = []
    body = XML_HEADER + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for r in rows:
        body += (
            f"  <url>\n"
            f"    <loc>{SITE_URL}/author/{_xml_escape(r.slug)}</loc>\n"
            f"    <lastmod>{_iso_date(r.updated_at)}</lastmod>\n"
            f"    <changefreq>weekly</changefreq>\n"
            f"    <priority>0.7</priority>\n"
            f"  </url>\n"
        )
    body += "</urlset>"
    return Response(content=body, media_type="application/xml")


# ─────────────────────────────────────────────────────────────
# Google News sitemap (last 48h)
# ─────────────────────────────────────────────────────────────

async def sitemap_news(request: Request, db: AsyncSession) -> Response:
    """Google News sitemap — only articles from the last 48 hours.

    Google News has specific requirements:
      - <news:news> namespace
      - <news:publication_date> in W3C format
      - <news:title> (escape & < >)
      - Only last 48h of articles
      - Max 1000 URLs per sitemap
    """
    cutoff = datetime.utcnow() - timedelta(hours=48)
    result = await db.execute(text("""
        SELECT id, slug, title, date, region
        FROM articles
        WHERE (status = 'published' OR status IS NULL)
          AND date > :cutoff
        ORDER BY date DESC
        LIMIT 1000
    """), {"cutoff": cutoff})
    rows = result.fetchall()

    body = (
        XML_HEADER +
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">\n'
    )
    for r in rows:
        slug = r.slug or f"id-{r.id}"
        pub_date = r.date.strftime("%Y-%m-%dT%H:%M:%S+00:00") if r.date else datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00")
        body += (
            f"  <url>\n"
            f"    <loc>{SITE_URL}/article/{_xml_escape(slug)}</loc>\n"
            f"    <news:news>\n"
            f"      <news:publication>\n"
            f"        <news:name>SFAAM NEWS</news:name>\n"
            f"        <news:language>en</news:language>\n"
            f"      </news:publication>\n"
            f"      <news:publication_date>{pub_date}</news:publication_date>\n"
            f"      <news:title>{_xml_escape(r.title or '')}</news:title>\n"
            f"    </news:news>\n"
            f"  </url>\n"
        )
    body += "</urlset>"
    return Response(content=body, media_type="application/xml")


# ─────────────────────────────────────────────────────────────
# Static pages sitemap
# ─────────────────────────────────────────────────────────────

async def sitemap_static(request: Request) -> Response:
    """All static HTML pages."""
    static_pages = [
        ("/", "1.0", "hourly"),
        ("/about.html", "0.6", "monthly"),
        ("/founder.html", "0.5", "monthly"),
        ("/contact.html", "0.5", "monthly"),
        ("/search.html", "0.4", "weekly"),
        ("/trends.html", "0.7", "hourly"),
        ("/bookmarks.html", "0.4", "weekly"),
        ("/corrections.html", "0.5", "daily"),
        ("/privacy.html", "0.3", "yearly"),
        ("/terms.html", "0.3", "yearly"),
        ("/cookies.html", "0.3", "yearly"),
    ]
    body = XML_HEADER + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    today = datetime.utcnow().strftime("%Y-%m-%d")
    for path, priority, freq in static_pages:
        body += (
            f"  <url>\n"
            f"    <loc>{SITE_URL}{path}</loc>\n"
            f"    <lastmod>{today}</lastmod>\n"
            f"    <changefreq>{freq}</changefreq>\n"
            f"    <priority>{priority}</priority>\n"
            f"  </url>\n"
        )
    body += "</urlset>"
    return Response(content=body, media_type="application/xml")


# ─────────────────────────────────────────────────────────────
# Schema.org helpers
# ─────────────────────────────────────────────────────────────

def organization_schema() -> dict:
    """Organization schema for the homepage footer."""
    return {
        "@context": "https://schema.org",
        "@type": "NewsMediaOrganization",
        "name": "SFAAM NEWS",
        "url": SITE_URL,
        "logo": {
            "@type": "ImageObject",
            "url": f"{SITE_URL}/static/logo.png",
            "width": 512,
            "height": 512,
        },
        "sameAs": [
            "https://twitter.com/sfaamnews",
            "https://facebook.com/sfaamnews",
            "https://youtube.com/@sfaamnews",
            "https://www.linkedin.com/company/sfaamnews",
        ],
        "ethicsPolicy": f"{SITE_URL}/ethics.html",
        "missionCoveragePrioritiesPolicy": f"{SITE_URL}/editorial-standards.html",
        "verificationFactCheckingPolicy": f"{SITE_URL}/fact-check-policy.html",
        "actionableFeedbackPolicy": f"{SITE_URL}/corrections.html",
        "diversityPolicy": f"{SITE_URL}/diversity.html",
        "masthead": f"{SITE_URL}/about.html",
        "publishingPrinciples": f"{SITE_URL}/editorial-standards.html",
    }


def web_site_schema() -> dict:
    """WebSite schema with SearchAction — enables Google sitelinks search box."""
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": "SFAAM NEWS",
        "url": SITE_URL,
        "potentialAction": {
            "@type": "SearchAction",
            "target": {
                "@type": "EntryPoint",
                "urlTemplate": f"{SITE_URL}/search.html?q={{search_term_string}}",
            },
            "query-input": "required name=search_term_string",
        },
    }


def person_schema(author) -> dict:
    """Person schema for author pages."""
    return {
        "@context": "https://schema.org",
        "@type": "Person",
        "name": author.name,
        "url": f"{SITE_URL}/author/{author.slug}",
        "jobTitle": author.title or "Journalist",
        "description": author.bio or "",
        "image": author.avatar_url or "",
        "sameAs": [
            s for s in [author.twitter, author.linkedin] if s
        ],
        "knowsAbout": author.expertise or [],
    }


def breadcrumb_schema(items: list[dict]) -> dict:
    """BreadcrumbList schema."""
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": i + 1,
                "name": it["name"],
                "item": f"{SITE_URL}{it['url']}" if it["url"].startswith("/") else it["url"],
            }
            for i, it in enumerate(items)
        ],
    }


def news_article_schema(article, citations: list = None, authors: list = None) -> dict:
    """Full NewsArticle schema with citations + author + claimReview."""
    schema = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": (article.title or "")[:110],
        "description": article.meta_desc or article.summary or "",
        "image": [
            article.image_url or f"{SITE_URL}/static/logo.png",
        ],
        "datePublished": article.date.isoformat() if article.date else None,
        "dateModified": (article.updated_at or article.date).isoformat() if article.updated_at or article.date else None,
        "url": f"{SITE_URL}/article/{article.slug}",
        "mainEntityOfPage": {
            "@type": "WebPage",
            "@id": f"{SITE_URL}/article/{article.slug}",
        },
        "publisher": {
            "@type": "Organization",
            "name": "SFAAM NEWS",
            "logo": {
                "@type": "ImageObject",
                "url": f"{SITE_URL}/static/logo.png",
            },
        },
        "author": [
            {"@type": "Person", "name": a.name, "url": f"{SITE_URL}/author/{a.slug}"}
            for a in (authors or [])
        ] or [{"@type": "Organization", "name": "SFAAM NEWS Editorial Team"}],
        "articleSection": article.region or "world",
        "keywords": (article.keywords or "").split(",")[:10] if article.keywords else [],
        "inLanguage": "en",
        "isAccessibleForFree": True,
        "wordCount": len((article.ai_content or "").split()),
    }
    # Add citations as references
    if citations:
        schema["citation"] = [
            {
                "@type": "CreativeWork",
                "url": c.source_url,
                "headline": c.source_title or "",
                "datePublished": c.source_date.isoformat() if c.source_date else None,
            }
            for c in citations
        ]
    return schema


def faq_page_schema(article_body: str) -> Optional[dict]:
    """Extract FAQ section from article body and return FAQPage schema.

    Returns None if no FAQ section is found. The schema looks like:
      {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
          {"@type": "Question", "name": "...", "acceptedAnswer": {"@type": "Answer", "text": "..."}}
        ]
      }
    """
    # Find the FAQ section
    m = re.search(
        r"##\s*(?:Frequently\s+Asked\s+Questions|FAQ)\s*\n(.*?)(?=\n##\s|$)",
        article_body or "",
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return None
    faq_text = m.group(1)

    # Parse Q/A pairs. Accept **Q: ...** / A: ... or ### Question / Answer patterns
    questions = []
    # Pattern 1: **Q: question**\nA: answer
    for qm in re.finditer(
        r"\*\*Q[:\s]+(.+?)\*\*\s*\n\s*A[:\s]+(.+?)(?=\n\s*\*\*Q|\Z)",
        faq_text, re.DOTALL,
    ):
        questions.append({
            "@type": "Question",
            "name": qm.group(1).strip(),
            "acceptedAnswer": {
                "@type": "Answer",
                "text": qm.group(2).strip().replace("\n", " "),
            },
        })
    # Pattern 2: ### Question\nanswer
    if not questions:
        for qm in re.finditer(
            r"###\s+(.+?)\n\s*(.+?)(?=\n###|\Z)",
            faq_text, re.DOTALL,
        ):
            questions.append({
                "@type": "Question",
                "name": qm.group(1).strip(),
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": qm.group(2).strip().replace("\n", " "),
                },
            })

    if not questions:
        return None
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": questions,
    }


def claim_review_schema(article, claim: str, rating: str, rating_value: str = None) -> dict:
    """ClaimReview schema for fact-check articles.

    `rating` is one of: "true", "false", "mixed", "mostly-true",
    "mostly-false", "unverified".
    """
    rating_map = {
        "true": "True",
        "false": "False",
        "mixed": "Mixed",
        "mostly-true": "Mostly True",
        "mostly-false": "Mostly False",
        "unverified": "Unverified",
    }
    return {
        "@context": "https://schema.org",
        "@type": "ClaimReview",
        "datePublished": article.date.isoformat() if article.date else None,
        "url": f"{SITE_URL}/article/{article.slug}",
        "itemReviewed": {
            "@type": "CreativeWork",
            "author": {"@type": "Organization", "name": "Source claim"},
            "datePublished": article.date.isoformat() if article.date else None,
            "name": claim[:200],
        },
        "claimReviewed": claim[:200],
        "reviewRating": {
            "@type": "Rating",
            "ratingValue": rating_value or rating,
            "alternateName": rating_map.get(rating, rating),
            "bestRating": "true",
            "worstRating": "false",
        },
        "author": {
            "@type": "Organization",
            "name": "SFAAM NEWS Fact Check",
            "url": SITE_URL,
        },
        "publisher": {
            "@type": "Organization",
            "name": "SFAAM NEWS",
            "logo": {
                "@type": "ImageObject",
                "url": f"{SITE_URL}/static/logo.png",
            },
        },
    }


# ─────────────────────────────────────────────────────────────
# Registrar
# ─────────────────────────────────────────────────────────────

def register_pro_sitemap_routes(app: FastAPI, get_db) -> None:
    """Register all Pro sitemap routes. REPLACES the legacy single
    /sitemap.xml — install_pro_sitemaps also handles the redirect
    from old URLs.
    """
    @app.get("/sitemap.xml")
    async def _index(request: Request):
        return await sitemap_index(request)

    @app.get("/sitemap-articles.xml")
    async def _articles(request: Request, db=Depends(get_db)):
        return await sitemap_articles(request, db)

    @app.get("/sitemap-articles-archive.xml")
    async def _archive(request: Request, db=Depends(get_db)):
        return await sitemap_archive(request, db)

    @app.get("/sitemap-categories.xml")
    async def _categories(request: Request):
        return await sitemap_categories(request)

    @app.get("/sitemap-topics.xml")
    async def _topics(request: Request, db=Depends(get_db)):
        return await sitemap_topics(request, db)

    @app.get("/sitemap-authors.xml")
    async def _authors(request: Request, db=Depends(get_db)):
        return await sitemap_authors(request, db)

    @app.get("/sitemap-news.xml")
    async def _news(request: Request, db=Depends(get_db)):
        return await sitemap_news(request, db)

    @app.get("/sitemap-static.xml")
    async def _static(request: Request):
        return await sitemap_static(request)

    logger.info("[ProSitemaps] Routes registered: index + 7 sub-sitemaps")
