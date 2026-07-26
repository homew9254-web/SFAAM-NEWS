# SFAAM NEWS V26 — Changelog

## V26 (Trends Pipeline) — Zero-Hallucination Content Engine

### New Files

| File | Purpose |
|------|---------|
| `trends_scraper.py`     | Stage 1+2: Fetch top Google Trends queries + deep-scrape authoritative news sources (BBC, Reuters, AP, NYT, Guardian, etc.) |
| `fact_verifier.py`      | Stage 3: Cross-Verification Engine — extracts atomic facts from each source, keeps only facts confirmed by 2+ sources (Jaccard similarity) |
| `trends_writer.py`      | Stage 4: LLM synthesis with strict zero-hallucination system prompt. Supports Groq + Gemini + fallback mode. Produces Wikipedia-style articles (Lead / Background / Detailed Facts / Timeline / References) |
| `trends_scheduler.py`   | Stage 5: APScheduler job running every 6 hours. Orchestrates the full pipeline and saves drafts to DB. |
| `static/trends.html`    | Public Trends page — shows admin-approved trend articles with the new Orange #CA6D4C theme. |

### Modified Files

| File | Changes |
|------|---------|
| `database.py`            | Added `TrendQuery` model + 8 new columns on `Article`: `is_trends`, `trend_query`, `fact_sources`, `verified_facts`, `source_count`, `word_count`, `references_data`, `pipeline_version`. Auto-migration for PostgreSQL. |
| `main.py`                | Added 8 new API endpoints (5 admin + 3 public) for Trends management. Registered Trends scheduler in lifespan. |
| `static/admin.html`      | New "🔥 Trends Drafts" tab with stats, draft table, draft detail modal (with verified facts + references), publish/delete actions. |
| `static/js/app.js`       | Added "🔥 Trends" link to the public navbar. |
| `static/css/style.css`   | Maroon #800000 → Orange #CA6D4C rebrand (49 replacements). |
| `static/css/style.min.css` | Same color migration. |
| `static/manifest.json`   | `theme_color` updated to #CA6D4C. |
| `.env.example`           | Added V26 Trends pipeline env vars (TRENDS_GEO, TRENDS_LIMIT, TRENDS_MAX_SOURCES, TRENDS_MIN_FACTS, TRENDS_INTERVAL_HOURS, TRENDS_AI_PROVIDER, TRENDS_GROQ_KEY, TRENDS_GEMINI_KEY). |

### New API Endpoints

#### Admin (requires `X-Admin-Session` or `X-Admin-Key` header)

- `GET  /api/admin/trends/status` — current pipeline status (running, last cycle, drafts pending)
- `POST /api/admin/trends/run` — manually trigger a Trends pipeline cycle
- `GET  /api/admin/trends/drafts?status_filter=draft&per_page=20` — paginated list of trend drafts
- `GET  /api/admin/trends/{article_id}` — full draft detail with verified facts, sources, references
- `POST /api/admin/trends/{article_id}/publish` — publish a draft (admin approval)
- `DELETE /api/admin/trends/{article_id}` — delete a draft (admin rejection)

#### Public

- `GET /api/trends` — list of published trend articles
- `GET /api/trends/{slug}` — single published trend article

### Pipeline Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                   TRENDS PIPELINE (every 6 hours)               │
└─────────────────────────────────────────────────────────────────┘

  ┌──────────────────┐
  │  Stage 1: FETCH  │  Google Trends RSS (public, no API key)
  │  Top 7 queries   │  → filters out non-Latin queries
  └────────┬─────────┘  → supplements with Google News headlines if needed
           │
  ┌────────▼─────────┐
  │  Stage 2: RESEARCH│  For each trend:
  │  Authoritative   │   1. DuckDuckGo HTML search → direct URLs
  │  source scraping │   2. Google News RSS fallback (with source-name
  │                  │      matching + URL resolution)
  │                  │   3. Scrape article body via httpx + BeautifulSoup
  └────────┬─────────┘
           │
  ┌────────▼─────────┐
  │  Stage 3: VERIFY │  Cross-Verification Engine:
  │  Facts (RAG)     │   - Split each source into atomic sentences
  │                  │   - Compute Jaccard similarity across sources
  │                  │   - Keep only facts with confirmation_count >= 1
  │                  │   - Each fact records: text, source_urls, domains,
  │                  │     confirmation_count, max_similarity
  └────────┬─────────┘
           │
  ┌────────▼─────────┐
  │  Stage 4: WRITE  │  Strict zero-hallucination LLM call:
  │  Wikipedia-style │   - System prompt forbids guessing/invention
  │  article         │   - Word count scales with verified facts
  │                  │   - Structure: Lead / Background / Detailed Facts
  │                  │     / Timeline / References
  │                  │   - Providers: Groq (Llama 3.3 70B) → Gemini
  │                  │     (2.0 Flash) → deterministic fallback
  └────────┬─────────┘
           │
  ┌────────▼─────────┐
  │  Stage 5: SAVE   │  Insert into articles table with:
  │  as DRAFT        │   - status='draft', is_trends=1
  │                  │   - fact_sources, verified_facts, references_data
  │                  │     stored as JSON for admin review
  │                  │   - Also inserts TrendQuery row for audit trail
  └────────┬─────────┘
           │
  ┌────────▼─────────┐
  │  Stage 6: REVIEW │  Admin opens /admin.html → "Trends Drafts" tab
  │  (manual)        │   - Sees: title, source count, fact count, words
  │                  │   - Clicks "View" → sees all verified facts +
  │                  │     full article + references
  │                  │   - Clicks "Publish" or "Delete"
  └──────────────────┘
```

### Configuration (Environment Variables)

```bash
# Geo for Google Trends ("" = worldwide, "US", "GB", "IN", "PK", "DE")
TRENDS_GEO=

# Number of trending queries per cycle (default: 7, per PDF spec)
TRENDS_LIMIT=7

# Max authoritative sources to scrape per trend (default: 5)
TRENDS_MAX_SOURCES=5

# Minimum verified facts required to produce an article (default: 3)
TRENDS_MIN_FACTS=3

# Pipeline interval in hours (default: 6, per PDF spec)
TRENDS_INTERVAL_HOURS=6

# Run once on app startup? (default: 0)
TRENDS_RUN_ON_STARTUP=0

# AI provider: auto | groq | gemini | fallback (default: auto)
TRENDS_AI_PROVIDER=auto

# Optional: dedicated API keys for Trends pipeline
# (falls back to GROQ_KEY_WORLD / GEMINI_KEY_WORLD if not set)
TRENDS_GROQ_KEY=
TRENDS_GEMINI_KEY=
```

### Color Rebrand (V25 → V26)

All maroon-family colors replaced with the new orange #CA6D4C palette:

| Old (Maroon)  | New (Orange)  | Use                    |
|---------------|---------------|------------------------|
| `#800000`     | `#CA6D4C`     | Primary brand          |
| `#8B0000`     | `#B05A3D`     | Darker variant         |
| `#600000`     | `#A04E33`     | Darkest variant        |
| `#A52A2A`     | `#D88A6B`     | Lighter variant        |
| `#5A2010`     | `#7A3D26`     | Brown-orange           |
| `#8B3A20`     | `#A85535`     | Medium brown           |
| `rgba(128,0,0,X)` | `rgba(202,109,76,X)` | Glow effects   |

100 replacements across `style.css`, `style.min.css`, `manifest.json` (×2).

### Admin Credentials (unchanged from V25)

| Field | Value |
|-------|-------|
| Login URL    | `/admin.html` |
| Password     | `[REDACTED — rotated, see README.md]` |
| Admin API key | `[REDACTED — rotated, see README.md]` |

### Local Dev

```bash
cd sfaam-news
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit if needed
python main.py
# Open http://localhost:8000
# Admin: http://localhost:8000/admin.html (password: [REDACTED — rotated, see README.md])
# Trends: http://localhost:8000/trends.html
```

### Production Deployment (Railway)

Same as V25 — see `RAILWAY_DEPLOYMENT.md`. The Trends scheduler starts
automatically on app startup (set `TRENDS_RUN_ON_STARTUP=0` in production
if you only want scheduled runs).

For AI-powered article generation (instead of the fallback fact-listing
mode), set at least one of:
- `GROQ_KEY_WORLD` (free: https://console.groq.com/keys)
- `GEMINI_KEY_WORLD` (free: https://aistudio.google.com/apikey)
- `TRENDS_GROQ_KEY` / `TRENDS_GEMINI_KEY` (dedicated keys for Trends)
