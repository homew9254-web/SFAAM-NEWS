"""
engine2_images.py - SFAAM Automated News Engine V2 (Clean Rebuild)
=====================================================================
STEP 5 of the 6-step workflow: Image Placement Strategy

Images are distributed through the content, not bunched at the top:
    - 1 hero image (with the title)
    - 2-3 images inside the Overview section (between subheadings)
    - 1-2 images inside the Background History section
    - Total 4-6 images per article, each with source attribution

Output format: Markdown image syntax `![alt](url)` — the existing
frontend's mdToHtml() already converts this into `<figure><img><figcaption>`,
so no frontend changes are needed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class PlacedImage:
    url: str
    caption: str


@dataclass
class ImagePlacementResult:
    hero_image_url: str
    overview_md: str      # overview markdown with 2-3 images woven in
    background_md: str    # background markdown with 1-2 images woven in
    images_used: list[PlacedImage]


def _collect_candidate_images(scraped_articles: list) -> list[PlacedImage]:
    seen_urls: set[str] = set()
    out: list[PlacedImage] = []
    for a in scraped_articles:
        source_name = a.source_domain or a.url.split("/")[2] if "//" in a.url else ""
        for img in a.images:
            if img.url in seen_urls:
                continue
            seen_urls.add(img.url)
            caption = img.caption or img.alt or a.title
            attribution = f"{caption} — Photo: {source_name}" if source_name else caption
            out.append(PlacedImage(url=img.url, caption=attribution))
    return out


def _insert_after_headings(markdown_text: str, images: list[PlacedImage], every_n_headings: int = 2) -> str:
    """Insert one image markdown block after every `every_n_headings`-th
    subheading (### or ####) in the text. Falls back to appending at the
    end if there aren't enough headings."""
    if not images:
        return markdown_text

    lines = markdown_text.split("\n")
    heading_positions = [i for i, ln in enumerate(lines) if re.match(r"^#{3,4}\s", ln)]

    img_queue = list(images)
    insert_at: dict[int, list[PlacedImage]] = {}
    heading_count = 0
    for pos in heading_positions:
        heading_count += 1
        if heading_count % every_n_headings == 0 and img_queue:
            insert_at.setdefault(pos, []).append(img_queue.pop(0))

    # If headings were too sparse to place all images, append the rest at the end
    trailing = img_queue

    out_lines: list[str] = []
    for i, ln in enumerate(lines):
        out_lines.append(ln)
        if i in insert_at:
            out_lines.append("")
            for img in insert_at[i]:
                out_lines.append(f"![{img.caption}]({img.url})")
            out_lines.append("")

    result = "\n".join(out_lines)
    if trailing:
        extra = "\n\n" + "\n\n".join(f"![{img.caption}]({img.url})" for img in trailing)
        result += extra
    return result


def place_images(overview_md: str, background_md: str, scraped_articles: list) -> ImagePlacementResult:
    """Step 5: pick 4-6 images total and weave them through the article."""
    candidates = _collect_candidate_images(scraped_articles)

    hero = candidates[0] if candidates else None
    remaining = candidates[1:]

    overview_images = remaining[:3]
    background_images = remaining[3:5]
    used = ([hero] if hero else []) + overview_images + background_images

    overview_final = _insert_after_headings(overview_md, overview_images, every_n_headings=2)
    background_final = _insert_after_headings(background_md, background_images, every_n_headings=2) if background_md else ""

    return ImagePlacementResult(
        hero_image_url=hero.url if hero else "",
        overview_md=overview_final,
        background_md=background_final,
        images_used=used,
    )
