# SFAAM NEWS V31.1 — Title Uniqueness Release

> **Tagline:** "No two articles shall share the same title — ever."

## Why V31.1?

V31 fixed security and crash bugs, but a deeper analysis of the article
generation pipeline revealed that **article titles were never checked for
uniqueness**. The DB schema had no UNIQUE constraint on `title`, the dedup
check in scheduler.py ran BEFORE the AI rewrite (so it checked the wrong
title), and the trends pipeline generated titles deterministically from the
trend query (same query → same title every time).

Result: duplicate and near-duplicate titles were slipping through constantly,
especially when:
- The same story was scraped from multiple RSS feeds
- The same trend recurred after the 7-day dedup window
- The AI writer happened to produce similar titles for similar stories
- An admin manually published an article without checking existing titles

V31.1 closes this gap with a multi-layered title uniqueness system.

---

## 🐛 Bugs Fixed

### Bug 1: No title uniqueness check at any point in the pipeline
**Severity:** CRITICAL
**Was:** Articles were inserted into the DB with no check whether the title
already existed. The `article_hash` unique constraint only caught exact
content duplicates (same title + same body), not title-only duplicates.
**Fix:** Added `title_uniqueness.ensure_unique_title()` — called at all 4
insert points (scheduler, engine, google_search, manual publish).

### Bug 2: Dedup check ran BEFORE AI rewrite
**Severity:** CRITICAL
**Was:** `scheduler.py:222` called `_is_duplicate(art["title"], ...)` using
the ORIGINAL RSS title. But the article that got saved used
`result_dict["title"]` — the AI-generated title. So the dedup was checking
the wrong title entirely.
**Fix:** Moved the uniqueness check to AFTER `rewrite_article()`, checking
the actual title that will be saved.

### Bug 3: `_build_title()` was deterministic
**Severity:** HIGH
**Was:** `trends_writer.py:238` just title-cased the trend query. So
"US Canada Trade Tariffs" always produced "US Canada Trade Tariffs" —
every single time. If the same trend recurred after the 7-day dedup
window, the new article had an IDENTICAL title to the old one.
**Fix:** `_build_title()` now appends a random editorial suffix from a
pool of 10 variations (": A Comprehensive Analysis", ": Explained", etc.).
Seeded with timestamp + query hash so the same query at different times
gets different suffixes.

### Bug 4: Only 50 recent articles checked for duplicates
**Severity:** HIGH
**Was:** `scheduler.py:206` loaded only the 50 most recent articles for
dedup checking. An article published 51 articles ago with a similar title
would not be detected.
**Fix:** The new `find_similar_titles()` scans up to 500 recent articles
(configurable via `FUZZY_SCAN_LIMIT`), and the exact-match check uses the
indexed `title_norm` column (O(log n) instead of O(n) scan).

### Bug 5: No fuzzy/normalized title matching
**Severity:** HIGH
**Was:** `_is_duplicate()` used `SequenceMatcher` on raw titles. So
"Pakistan Election" and "PAKISTAN ELECTION!" were treated as different
(similarity ~0.85 due to case + punctuation). And "Pakistan's Election"
vs "Pakistans Election" was similarity ~0.95 — caught, but only by luck.
**Fix:** New `normalize_title()` function applies:
  - Unicode NFKD normalize + strip accents (é → e)
  - Strip apostrophes ("Pakistan's" → "Pakistans")
  - Lowercase
  - Collapse abbreviation periods ("U.S." → "US", not "u s")
  - Remove all punctuation
  - Collapse whitespace

Then `SequenceMatcher` runs on the NORMALIZED titles, so "Pakistan's
Election!" and "pakistans election" both normalize to "pakistans election"
and are detected as exact duplicates.

### Bug 6: No DB-level support for title uniqueness
**Severity:** MEDIUM
**Was:** The `title` column was indexed but had no UNIQUE constraint, and
no normalized version was stored. Every dedup check required scanning
recent articles in Python.
**Fix:** Added `title_norm` column to the Article model (VARCHAR(500),
indexed). Auto-populated on insert by all 4 pipelines. The V31.1 migration
auto-adds the column to existing DBs and backfills it for all existing
articles. A UNIQUE constraint was NOT added (to avoid breaking existing
databases with duplicates) — instead, app-level `ensure_unique_title()`
enforces uniqueness going forward.

### Bug 7: Cross-region duplicates allowed
**Severity:** MEDIUM
**Was:** The old dedup checked articles regardless of region, but the
similarity threshold was 0.85 — too lenient to catch cross-region dupes
with slight variations.
**Fix:** `ensure_unique_title()` checks ALL regions (not just the current
one), so a title used in "World" will be detected when trying to publish
the same title in "USA".

---

## ✨ New Features

### Feature 1: `title_uniqueness.py` module
A new 300-line module providing:
- `normalize_title(title)` — pure function, deterministic normalization
- `compute_title_norm(title)` — alias for `normalize_title` (for column defaults)
- `is_title_taken(session, title, exclude_id=None)` — async exact-match check
- `find_similar_titles(session, title, threshold=0.88, limit=10)` — async fuzzy match
- `ensure_unique_title(session, title, exclude_id=None, style="suffix")` — async, returns a unique title
- `find_all_duplicate_titles(session, limit=100)` — async, for admin dashboard

Includes a built-in CLI test (`python title_uniqueness.py`) that verifies
all normalization rules.

### Feature 2: `GET /api/admin/duplicate-titles` endpoint
Admin can now scan the entire DB for duplicate/near-duplicate title groups.
Returns:
```json
{
  "total_groups": 3,
  "total_duplicate_articles": 7,
  "groups": [
    {
      "normalized_title": "pakistan election results",
      "count": 3,
      "articles": [
        {"id": 42, "title": "Pakistan Election Results", "region": "pakistan", ...},
        {"id": 87, "title": "Pakistan's Election Results!", "region": "world", ...},
        {"id": 103, "title": "PAKISTAN ELECTION RESULTS", "region": "usa", ...}
      ]
    }
  ]
}
```
Admin can then use the V31 PATCH endpoint to rename duplicates.

### Feature 3: `POST /api/admin/backfill-title-norm` endpoint
Manually triggers the `title_norm` backfill for existing articles. Useful if:
- The auto-migration was interrupted
- Articles were inserted by an older code path that didn't set `title_norm`
- You want to refresh stale `title_norm` values after manual title edits

Returns `{"scanned": N, "updated": M, "skipped": N-M}`.

---

## 📋 Files Modified

| File | Changes |
|------|---------|
| `title_uniqueness.py` | **NEW** — 300-line title uniqueness engine |
| `database.py` | Added `title_norm` column to Article model + V31.1 migration + backfill |
| `scheduler.py` | Moved dedup check after AI rewrite + calls `ensure_unique_title()` + stores `title_norm` |
| `automated_news_engine.py` | Calls `ensure_unique_title()` before insert + stores `title_norm` |
| `google_search_writer.py` | Calls `ensure_unique_title()` before insert + stores `title_norm` |
| `trends_writer.py` | `_build_title()` now appends random editorial suffix |
| `main.py` | Manual publish calls `ensure_unique_title()` + PATCH recomputes `title_norm` + 2 new admin endpoints |

---

## 🧪 Test Results

**10/10 verification tests PASS:**

1. ✅ All 7 modified files parse cleanly
2. ✅ `title_uniqueness` module imports + normalization works
3. ✅ Article model has `title_norm` column + migration + backfill
4. ✅ `scheduler.py` checks uniqueness AFTER AI rewrite + stores `title_norm`
5. ✅ `automated_news_engine.py` uses `ensure_unique_title`
6. ✅ `google_search_writer.py` uses `ensure_unique_title`
7. ✅ `main.py` manual publish + PATCH + 2 new endpoints
8. ✅ `trends_writer.py` `_build_title()` adds random suffix
9. ✅ **Integration test (real SQLite DB):**
   - Exact duplicate "Pakistan Election Results" → fixed to "Pakistan Election Results (2)" ✓
   - Fuzzy duplicate "Pakistan Election Result" (singular) → fixed to "Pakistan Election Result (2)" ✓
   - Unique title "India Tech Startup Boom" → allowed as-is ✓
   - Duplicate scan found the duplicate group correctly ✓
10. ✅ All new routes registered on app import

---

## 📋 Migration Guide

V31.1 is **backwards-compatible** with V31 databases. The migration is
**automatic and safe**:

1. On startup, `init_db()` detects if `title_norm` column is missing.
2. If missing, it runs `ALTER TABLE articles ADD COLUMN title_norm VARCHAR(500)`.
3. It creates the `idx_title_norm` index.
4. It backfills `title_norm` for all existing articles (in batches of 500).

No downtime required. No manual SQL. Just deploy and restart.

**Optional:** After deploy, an admin can call
`POST /api/admin/backfill-title-norm` to refresh any stale `title_norm`
values (e.g., if articles were edited via direct SQL).

**Optional:** Call `GET /api/admin/duplicate-titles` to see if any
duplicate titles already exist in the DB. Use the V31 PATCH endpoint to
rename them.

---

## 🔒 How Title Uniqueness Is Enforced (Architecture)

```
┌─────────────────────────────────────────────────────────────┐
│  RSS Scraper / Trends / Engine / Manual Publish             │
│                                                             │
│  1. Generate title (AI rewrite / trend query / admin input) │
│                                                             │
│  2. Call ensure_unique_title(db, title)                     │
│     ├─ normalize_title(title)                               │
│     │  → lowercase, strip punctuation, strip accents        │
│     ├─ Check title_norm column for exact match (indexed)    │
│     ├─ If no exact match:                                   │
│     │    Scan 500 recent titles for fuzzy match (≥0.88)     │
│     ├─ If duplicate found:                                  │
│     │    Try "Title (2)", "Title (3)", ... (exact only)     │
│     │    Return first unused suffixed title                  │
│     └─ If no duplicate:                                     │
│          Return title as-is                                 │
│                                                             │
│  3. Insert Article with:                                    │
│     title = unique_title                                    │
│     title_norm = normalize_title(unique_title)              │
│     slug = make_slug(unique_title)  [also deduped]          │
└─────────────────────────────────────────────────────────────┘
```

The `title_norm` column is indexed for O(log n) exact-match lookups.
The fuzzy scan is O(n) but only runs when there's no exact match, and
is capped at 500 articles (~1 month of content).

---

Built with ❤️ by SFAAM Media Group
