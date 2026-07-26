"""
pro_og_image.py — SFAAM NEWS PRO 1 — Dynamic Open Graph images
================================================================

Generates a branded social-share image for every article on the fly.
The image is a 1200x630 PNG with:
  - SFAAM NEWS logo / brand bar at top
  - Article title (wrapped to 3 lines, large bold)
  - Region tag + reading time
  - Subtle background pattern

URL pattern:
  /api/og-image/{article_id}.png
  /api/og-image/{slug}.png

The image is cached on disk (24h TTL) so repeated shares don't
re-render. Uses Pillow (PIL) — already a common dep for news apps.

If Pillow is not installed, returns 404 gracefully (the frontend
falls back to a static logo image).
"""
from __future__ import annotations

import os
import re
import logging
import hashlib
from io import BytesIO
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import Response, JSONResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

OG_CACHE_DIR = os.path.join(os.path.dirname(__file__), "static", "og-cache")
os.makedirs(OG_CACHE_DIR, exist_ok=True)

# Brand colors
BRAND_ORANGE = (202, 109, 76)
BRAND_DARK = (13, 13, 13)
BRAND_LIGHT = (245, 243, 239)
TEXT_DARK = (26, 26, 26)
TEXT_MUTED = (122, 117, 112)


def _is_pillow_available() -> bool:
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
        return True
    except ImportError:
        return False


def _find_font(weight: str = "regular", size: int = 32) -> "Optional[object]":
    """Find a system font. Falls back to default if none available."""
    from PIL import ImageFont
    candidates = {
        "regular": [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ],
        "bold": [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ],
    }
    for path in candidates.get(weight, candidates["regular"]):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap_text(text: str, font, draw, max_width: int) -> list[str]:
    """Wrap text to fit max_width, returning list of lines."""
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        try:
            w = draw.textlength(test, font=font)
        except Exception:
            w = len(test) * (font.size * 0.5)
        if w <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _render_og_image(title: str, region: str = "", read_time: str = "") -> bytes:
    """Render the OG image and return PNG bytes."""
    from PIL import Image, ImageDraw

    W, H = 1200, 630
    img = Image.new("RGB", (W, H), BRAND_LIGHT)
    draw = ImageDraw.Draw(img)

    # Top brand bar
    draw.rectangle([0, 0, W, 90], fill=BRAND_ORANGE)
    brand_font = _find_font("bold", 36)
    try:
        draw.text((40, 22), "SFAAM NEWS", font=brand_font, fill=BRAND_LIGHT)
    except Exception:
        draw.text((40, 30), "SFAAM NEWS", fill=BRAND_LIGHT)

    # Region tag (top right)
    if region:
        tag_font = _find_font("bold", 18)
        tag_text = region.upper()
        try:
            tw = draw.textlength(tag_text, font=tag_font)
        except Exception:
            tw = len(tag_text) * 10
        draw.rectangle([W - tw - 50, 30, W - 30, 60], fill=BRAND_DARK)
        draw.text((W - tw - 40, 35), tag_text, font=tag_font, fill=BRAND_LIGHT)

    # Title — large, bold, wrapped to 3 lines max
    title_font = _find_font("bold", 56)
    lines = _wrap_text(title, title_font, draw, W - 80)
    # Cap to 3 lines
    if len(lines) > 3:
        lines = lines[:3]
        # Truncate last line with ellipsis
        if lines[2][-1:] != "…":
            lines[2] = lines[2][:-3].rstrip() + "…"
    y = 180
    for line in lines:
        draw.text((40, y), line, font=title_font, fill=TEXT_DARK)
        y += 72

    # Bottom bar — read time + URL
    draw.rectangle([0, H - 60, W, H], fill=BRAND_DARK)
    bottom_font = _find_font("regular", 22)
    bottom_text = f"{read_time} · sfaamnews.com" if read_time else "sfaamnews.com"
    draw.text((40, H - 42), bottom_text, font=bottom_font, fill=BRAND_LIGHT)

    # Save to bytes
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def get_og_image(identifier: str, db: AsyncSession) -> Response:
    """Generate or fetch cached OG image for an article."""
    if not _is_pillow_available():
        raise HTTPException(404, "OG image generation not available (Pillow not installed)")

    # Try to find the article
    from database import Article
    article = None
    if identifier.isdigit():
        article = (await db.execute(
            select(Article).where(Article.id == int(identifier))
        )).scalar_one_or_none()
    else:
        article = (await db.execute(
            select(Article).where(Article.slug == identifier)
        )).scalar_one_or_none()
    if not article:
        raise HTTPException(404, "Article not found")

    # Cache key — include updated_at so changes invalidate cache
    cache_key = hashlib.md5(
        f"{article.id}:{article.updated_at or article.date}".encode()
    ).hexdigest()
    cache_path = os.path.join(OG_CACHE_DIR, f"{cache_key}.png")

    # Cache hit?
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return Response(
                content=f.read(),
                media_type="image/png",
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "X-OG-Cache": "HIT",
                },
            )

    # Render
    read_time_min = max(1, len((article.ai_content or "").split()) // 200)
    png_bytes = _render_og_image(
        title=article.title or "SFAAM NEWS",
        region=article.region or "",
        read_time=f"{read_time_min} min read",
    )

    # Save to cache
    try:
        with open(cache_path, "wb") as f:
            f.write(png_bytes)
    except Exception as e:
        logger.debug(f"[ProOG] cache save failed: {e}")

    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-OG-Cache": "MISS",
        },
    )


def register_pro_og_routes(app: FastAPI, get_db) -> None:
    @app.get("/api/og-image/{identifier}.png")
    async def _og(identifier: str, db=Depends(get_db)):
        return await get_og_image(identifier, db)

    logger.info(f"[ProOG] Routes registered (Pillow available={_is_pillow_available()})")
