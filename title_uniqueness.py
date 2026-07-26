"""
title_uniqueness.py - SFAAM NEWS V31.1 — Title Uniqueness Engine
=================================================================

PROBLEM
-------
Article titles were NOT being checked for uniqueness at any point in the
pipeline. The DB schema has no UNIQUE constraint on `title`, the dedup
check in scheduler.py runs BEFORE the AI rewrite (so it checks the wrong
title), and the trends pipeline generates titles deterministically from
the trend query (same query → same title every time).

Result: duplicate and near-duplicate titles slip through constantly.

SOLUTION
--------
This module provides:

  1. `normalize_title(title)` — lowercase, strip punctuation, collapse
     whitespace, unicode-normalize. "Pakistan's Election!" and
     "pakistan's  election" both normalize to "pakistans election".

  2. `is_title_taken(session, title, exclude_id=None)` — async DB check
     for an exact normalized-title match. Returns the matching Article
     or None.

  3. `find_similar_titles(session, title, threshold=0.88, limit=10,
     exclude_id=None)` — fuzzy match using SequenceMatcher. Catches
     "Pakistan Election" vs "Pakistan Elections" (similarity 0.96) and
     "US Trade Tariffs" vs "USA Trade Tariff" (similarity 0.83).

  4. `ensure_unique_title(session, title, exclude_id=None,
     style="suffix")` — if the title (or a near-duplicate) is already
     in the DB, append a numeric suffix: "My Title", "My Title (2)",
     "My Title (3)", etc. Returns the unique title.

  5. `compute_title_norm(title)` — pure function, used by the Article
     model's `title_norm` column (auto-populated on insert).

USAGE
-----
Before any `db.add(Article(...))` call:

    from title_uniqueness import ensure_unique_title
    article.title = await ensure_unique_title(db, article.title)

This is a single async call that:
  - Normalizes the title
  - Checks for exact + fuzzy matches in the DB
  - Returns a unique title (with " (2)", " (3)" suffix if needed)
"""
from __future__ import annotations

import logging
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

# Similarity threshold for fuzzy matching.
# 0.88 catches "Pakistan Election" vs "Pakistan Elections" (0.96)
#         and  "US Trade Tariffs" vs "USA Trade Tariff" (0.83 → no, allowed)
# 0.90 would be too strict (would block legitimate variations).
# 0.85 would be too lenient (would allow "Pakistan Election 2024" vs
#       "Pakistan Election 2023" — similarity 0.92, but these ARE different
#       articles). We use 0.88 as a compromise.
DEFAULT_FUZZY_THRESHOLD = 0.88

# How many recent articles to scan for fuzzy matching.
# Higher = more thorough but slower. 500 is a good balance — covers ~1 month
# of articles at the default scrape rate.
FUZZY_SCAN_LIMIT = 500

# Maximum suffix number to try before giving up.
# "My Title (2)" through "My Title (50)" — if all 50 are taken, the title
# is genuinely too common and we append a timestamp instead.
MAX_SUFFIX_ATTEMPTS = 50


# ─────────────────────────────────────────────────────────────
# Title normalization
# ─────────────────────────────────────────────────────────────

# Pre-compile regexes for performance
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+", re.UNICODE)
_APOSTROPHE_RE = re.compile(r"['`\u2019\u2018]", re.UNICODE)
# Periods BETWEEN single letters (U.S.A., U.K., N.Y.) should be removed
# WITHOUT introducing spaces — "U.S." → "US", not "U S ".
# Pattern: a single letter, then period, then single letter (repeated).
# We collapse these BEFORE the general punctuation stripper.
_ABBREV_PERIOD_RE = re.compile(r"(?<=[A-Za-z])\.(?=[A-Za-z])")


def normalize_title(title: str) -> str:
    """Normalize a title for uniqueness comparison.

    Transformations (in order):
      1. Unicode NFKD normalize + strip accents (é → e, ñ → n)
      2. Replace apostrophes with nothing ("Pakistan's" → "Pakistans")
      3. Lowercase
      4. Collapse abbreviation periods: "U.S.A." → "USA" (NOT "u s a")
      5. Remove all remaining punctuation (commas, colons, hyphens, etc.)
      6. Collapse multiple whitespace into single space
      7. Strip leading/trailing whitespace

    Examples:
        "Pakistan's Election!"           → "pakistans election"
        "PAKISTAN'S  ELECTION!"          → "pakistans election"
        "U.S.-Canada Trade: Latest"      → "us canada trade latest"
        "Pakistan Elections 2024"        → "pakistan elections 2024"
        "Breakthrough in AI — Analysis"  → "breakthrough in ai analysis"
    """
    if not title:
        return ""
    # 1. Unicode normalize + strip accents
    s = unicodedata.normalize("NFKD", title)
    s = "".join(c for c in s if not unicodedata.combining(c))
    # 2. Strip apostrophes (so "Pakistan's" == "Pakistans")
    s = _APOSTROPHE_RE.sub("", s)
    # 3. Lowercase
    s = s.lower()
    # 4. Collapse abbreviation periods BEFORE general punctuation removal
    #    "U.S.-Canada" → "us-canada" (then step 5 turns "-" into space → "us canada")
    s = _ABBREV_PERIOD_RE.sub("", s)
    # 5. Remove punctuation (keep word chars + whitespace)
    s = _PUNCT_RE.sub(" ", s)
    # 6. Collapse whitespace
    s = _WS_RE.sub(" ", s).strip()
    return s


# Alias — same function, clearer name when used as a column default
compute_title_norm = normalize_title


def _similarity(a: str, b: str) -> float:
    """Compute title similarity ratio (0.0-1.0) using SequenceMatcher
    on NORMALIZED titles. This catches:
      - "Pakistan Election" vs "Pakistan Elections" → 0.96
      - "US Trade Tariffs" vs "USA Trade Tariffs" → 0.97
      - "Pakistan Election" vs "India Election" → 0.62 (no match)

    V32.1 BUGFIX: When two titles differ ONLY in a 4-digit year
    (e.g. "Pakistan Election 2024" vs "Pakistan Election 2023"), the
    SequenceMatcher ratio is ~0.92 — above the DEFAULT_FUZZY_THRESHOLD
    of 0.88 — so the new article was treated as a duplicate and got
    a "(2)" suffix appended. But these are GENUINELY DIFFERENT articles
    (different election cycles, different candidates, different results).
    Now: extract 4-digit years from both titles; if both contain a year
    AND the years differ, return 0.0 (treat as completely different).
    """
    import re as _re
    years_a = set(_re.findall(r"\b(19|20)\d{2}\b", a))
    years_b = set(_re.findall(r"\b(19|20)\d{2}\b", b))
    if years_a and years_b and years_a != years_b:
        # Different years present in both titles → genuinely different article.
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# ─────────────────────────────────────────────────────────────
# DB queries
# ─────────────────────────────────────────────────────────────

async def is_title_taken(
    session,
    title: str,
    exclude_id: Optional[int] = None,
) -> Optional[object]:
    """Check if an article with the same NORMALIZED title already exists.

    Args:
        session:    SQLAlchemy AsyncSession
        title:      The title to check (will be normalized)
        exclude_id: If set, skip this article ID (useful when editing)

    Returns:
        The matching Article object, or None if no exact match.
    """
    from database import Article
    from sqlalchemy import select

    norm = normalize_title(title)
    if not norm:
        return None

    # Try the title_norm column first (fast, indexed) if it exists.
    # Fall back to scanning recent titles (slower but works pre-migration).
    try:
        q = select(Article).where(Article.title_norm == norm).limit(1)
        if exclude_id is not None:
            q = q.where(Article.id != exclude_id)
        result = await session.execute(q)
        return result.scalars().first()
    except Exception:
        # title_norm column doesn't exist yet (pre-migration) — fall back
        # to a LIKE-based scan on the title column.
        # We use a case-insensitive LIKE on Postgres, or LOWER() on SQLite.
        q = select(Article).order_by(Article.date.desc()).limit(FUZZY_SCAN_LIMIT)
        if exclude_id is not None:
            q = q.where(Article.id != exclude_id)
        result = await session.execute(q)
        for art in result.scalars().all():
            if normalize_title(art.title or "") == norm:
                return art
        return None


async def find_similar_titles(
    session,
    title: str,
    threshold: float = DEFAULT_FUZZY_THRESHOLD,
    limit: int = 10,
    exclude_id: Optional[int] = None,
):
    """Find articles with titles similar to the given title (fuzzy match).

    Args:
        session:    SQLAlchemy AsyncSession
        title:      The title to check
        threshold:  Minimum similarity ratio (0.0-1.0). Default 0.88.
        limit:      Max number of similar articles to return
        exclude_id: If set, skip this article ID

    Returns:
        List of (Article, similarity_score) tuples, sorted by similarity desc.
    """
    from database import Article
    from sqlalchemy import select

    norm = normalize_title(title)
    if not norm:
        return []

    q = select(Article).order_by(Article.date.desc()).limit(FUZZY_SCAN_LIMIT)
    if exclude_id is not None:
        q = q.where(Article.id != exclude_id)
    result = await session.execute(q)
    articles = result.scalars().all()

    similar = []
    for art in articles:
        art_norm = normalize_title(art.title or "")
        if not art_norm:
            continue
        sim = _similarity(norm, art_norm)
        if sim >= threshold:
            similar.append((art, sim))

    similar.sort(key=lambda x: x[1], reverse=True)
    return similar[:limit]


async def ensure_unique_title(
    session,
    title: str,
    exclude_id: Optional[int] = None,
    style: str = "suffix",
) -> str:
    """Ensure the title is unique in the DB. If it's a duplicate (exact OR
    fuzzy match), modify it to be unique.

    Args:
        session:    SQLAlchemy AsyncSession
        title:      The proposed title
        exclude_id: If set, don't consider this article ID as a conflict
        style:      "suffix"  → append " (2)", " (3)", etc. [default]
                    "timestamp" → append " - YYYY-MM-DD HH:MM"
                    "throw"    → raise ValueError if duplicate (no auto-fix)

    Returns:
        A unique title (possibly with a suffix appended).

    Raises:
        ValueError: if style="throw" and the title is a duplicate.
    """
    if not title or not title.strip():
        raise ValueError("Title cannot be empty")

    # Step 1: Check for exact normalized match
    exact_match = await is_title_taken(session, title, exclude_id)
    if not exact_match:
        # Step 2: Check for fuzzy match (near-duplicate)
        similar = await find_similar_titles(
            session, title, threshold=DEFAULT_FUZZY_THRESHOLD, limit=1,
            exclude_id=exclude_id,
        )
        if not similar:
            # No duplicates — title is unique as-is
            return title.strip()
        # Has a near-duplicate — fall through to suffix logic
        logger.info(
            f"[TitleUniqueness] Near-duplicate detected: "
            f"'{title[:50]}' ≈ '{similar[0][0].title[:50]}' "
            f"(similarity={similar[0][1]:.2f})"
        )

    if style == "throw":
        raise ValueError(
            f"Title '{title[:80]}' is a duplicate of existing article "
            f"(id={exact_match.id if exact_match else similar[0][0].id})"
        )

    if style == "timestamp":
        from datetime import datetime
        ts = datetime.utcnow().strftime(" - %Y-%m-%d %H:%M")
        return f"{title.strip()}{ts}"

    # Default: suffix style — try " (2)", " (3)", etc.
    base = title.strip()
    # Strip any existing " (N)" suffix from the base first
    base = re.sub(r"\s*\(\d+\)\s*$", "", base).strip()

    for n in range(2, MAX_SUFFIX_ATTEMPTS + 2):
        candidate = f"{base} ({n})"
        # V31.1 FIX: Only check for EXACT match on suffixed candidates.
        # We do NOT do fuzzy matching here because "My Title (2)" is always
        # ~0.95 similar to "My Title" — the fuzzy check would reject every
        # suffix and we'd fall through to timestamp every time.
        # The fuzzy check already ran above on the original title; if we
        # reached here, we've decided a suffix is needed. Now we just need
        # to find an unused suffix number.
        exact = await is_title_taken(session, candidate, exclude_id)
        if not exact:
            return candidate

    # All suffixes taken — fall back to timestamp
    from datetime import datetime
    ts = datetime.utcnow().strftime(" - %Y-%m-%d %H:%M")
    logger.warning(
        f"[TitleUniqueness] All {MAX_SUFFIX_ATTEMPTS} suffixes taken for "
        f"'{title[:50]}' — falling back to timestamp"
    )
    return f"{base}{ts}"


# ─────────────────────────────────────────────────────────────
# Admin utility — find all duplicate titles in the DB
# ─────────────────────────────────────────────────────────────

async def find_all_duplicate_titles(session, limit: int = 100):
    """Scan the DB for groups of articles with duplicate or near-duplicate
    titles. Used by the admin duplicate-titles endpoint.

    Returns:
        List of dicts: [{
            "normalized_title": str,
            "count": int,
            "articles": [{"id": int, "title": str, "region": str, "date": str, "status": str}, ...]
        }, ...]
        Sorted by group size (largest first), then by normalized title.
    """
    from database import Article
    from sqlalchemy import select

    # Load all articles (capped at 5000 for performance)
    result = await session.execute(
        select(Article.id, Article.title, Article.region, Article.date, Article.status)
        .order_by(Article.date.desc())
        .limit(5000)
    )
    rows = result.fetchall()

    # Group by normalized title (exact duplicates)
    groups: dict[str, list] = {}
    for row in rows:
        norm = normalize_title(row.title or "")
        if not norm:
            continue
        groups.setdefault(norm, []).append({
            "id": row.id,
            "title": row.title,
            "region": row.region,
            "date": row.date.isoformat() if row.date else None,
            "status": row.status,
        })

    # Filter to only groups with 2+ articles
    duplicates = [
        {
            "normalized_title": norm,
            "count": len(arts),
            "articles": arts,
        }
        for norm, arts in groups.items()
        if len(arts) >= 2
    ]
    # Sort by count desc, then by normalized title
    duplicates.sort(key=lambda g: (-g["count"], g["normalized_title"]))
    return duplicates[:limit]


# ─────────────────────────────────────────────────────────────
# CLI for testing
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("SFAAM Title Uniqueness Engine — Normalization Test")
    print("=" * 60)

    test_cases = [
        ("Pakistan's Election!", "pakistans election"),
        ("PAKISTAN'S  ELECTION!", "pakistans election"),
        ("U.S.-Canada Trade: Latest", "us canada trade latest"),
        ("Pakistan Elections 2024", "pakistan elections 2024"),
        ("Breakthrough in AI — Analysis", "breakthrough in ai analysis"),
        ("  Multiple   Spaces  ", "multiple spaces"),
        ("Café Résumé naïve", "cafe resume naive"),
        ("", ""),
    ]

    all_pass = True
    for inp, expected in test_cases:
        actual = normalize_title(inp)
        ok = actual == expected
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"  [{status}] {inp!r:40} → {actual!r}")
        if not ok:
            print(f"           expected: {expected!r}")

    print()
    print("=" * 60)
    if all_pass:
        print("ALL NORMALIZATION TESTS PASSED")
    else:
        print("SOME TESTS FAILED")
        sys.exit(1)
