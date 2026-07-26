# SFAAM NEWS V25 — Change Log (PDF-Spec Compliance Release)

This release fixes all bugs found during code review and brings the system
into compliance with the **SFAAM News Implementation Plan PDF** specification.

---

## 1. Critical Bug Fixes

### 1.1 `requirements.txt` was empty (0 bytes)
**Impact:** App would not start on Railway/Docker — `pip install -r requirements.txt`
installed nothing, all imports failed at runtime.

**Fix:** Populated `requirements.txt` with all 30+ pinned dependencies discovered
by static-analysis of every `import` statement across all `.py` files. Includes:
- Core: `fastapi`, `uvicorn[standard]`, `python-multipart`, `PyYAML`
- DB: `SQLAlchemy`, `asyncpg`, `psycopg2-binary`, `aiosqlite` (local-dev fallback)
- Cache: `redis`, `hiredis`
- Validation: `pydantic`, `pydantic-core`, `email-validator`, `python-dotenv`
- Scraping: `httpx`, `feedparser`, `beautifulsoup4`, `lxml`, `cloudscraper`
- Scheduling: `APScheduler`, `tzlocal`, `tzdata`
- AI: `groq`, `google-generativeai` (and its Google auth deps)
- Optional: `sentry-sdk` (imported lazily, safe to omit)

All versions pinned for reproducible builds.

### 1.2 `static/css/style.min.css` was empty
**Impact:** Future templates that referenced the minified stylesheet would
get a blank file. (Existing HTML uses `style.css`, so this was latent.)

**Fix:** Populated `style.min.css` with a copy of the (now maroon-themed) `style.css`.

### 1.3 `/manifest.json` route returned HTML instead of JSON
**Impact:** PWA installation broke — browsers couldn't fetch the web app manifest.

**Root cause:** `manifest.json` existed only at project root, but the static-file
mount serves from `static/`. The catch-all route fell through to `index.html`.

**Fix:** Copied `manifest.json` → `static/manifest.json`. Verified the route now
returns `Content-Type: application/json` with the correct manifest payload.

### 1.4 `aiosqlite` was a missing transitive dependency
**Impact:** Local development (when `DATABASE_URL` is unset) crashed on import
because `database.py` falls back to `sqlite+aiosqlite:///./sfaam.db`.

**Fix:** Added `aiosqlite==0.20.0` to `requirements.txt`.

---

## 2. PDF-Spec Compliance Fixes

### 2.1 Maroon #800000 Theme (was Orange #D97757)
**PDF requirement:**
> "The platform must utilize a distinct Maroon-themed UI (#800000 primary color)."

**Before:** Primary `--orange: #D97757` (a warm orange) was the dominant brand
color. Maroon `#6B1F2E` was used only as an accent for the premium ad-block
fallback.

**After (V25):**
- `--orange`      → `#800000`  (primary maroon, per PDF spec)
- `--orange-light`→ `#A52A2A`
- `--orange-dark` → `#600000`
- `--orange-glow` → `rgba(128, 0, 0, 0.28)`
- `--maroon`      → `#800000`  (aligned with PDF, was #6B1F2E)
- `--maroon-dark` → `#600000`
- `--maroon-light`→ `#A52A2A`
- `--ticker-bg`   → `#800000`  (dark + light themes)
- `manifest.json` `theme_color` → `#800000` (was `#D97757`)
- New explicit aliases: `--primary`, `--primary-light`, `--primary-dark`,
  `--primary-glow` for new CSS code.

All existing CSS rules that referenced `var(--orange)` now render in maroon
automatically — no per-rule edits needed. Verified by full-text search: zero
remaining hard-coded `#D97757` / `#C4633F` / `#E8956A` references in the CSS.

### 2.2 AdSense Safety Filter (NEW — was missing entirely)
**PDF requirement:**
> "quality_control.py: An AI validation layer that ... ensures 100% AdSense
> compliance (filtering out dangerous/violating content), and prevents
> server-crashing edge cases."

**Before:** `quality_control.py` had readability, grammar, SEO, uniqueness,
and fact-check sub-scores, but **NO AdSense policy filter**. Articles could
be published containing content that would violate Google AdSense Program
Policies (adult, violence, hate speech, etc.) and get the site's AdSense
account banned.

**After (V25):** Added a new `adsense_safety_check(title, body)` function
plus a `_ADSENSE_VIOLATION_CATEGORIES` registry with 9 policy categories:

| Category | Severity | Example trigger |
|---|---|---|
| `adult_content` | critical | "pornographic", "sex video", "leaked nudes" |
| `graphic_violence` | critical | "beheading", "how to make a bomb" |
| `hate_speech` | critical | "white power", "ethnic cleansing", "holocaust denial" |
| `dangerous_illegal_acts` | critical | "cocaine trafficking", "meth lab", "credit card fraud tutorial" |
| `terrorism_extremism` | critical | "ISIS recruitment", "lone-wolf attack guide" |
| `weapons_facilitation` | high | "ghost gun kit", "3d printed firearm", "buy AK-47 no background check" |
| `tobacco_alcohol_promotion` | medium | "buy cheap vapes online", "underage drinking guide" |
| `misleading_clickbait` | medium | "one weird trick", "cures cancer in 3 days" |
| `sensitive_events_exploitation` | high | "profit from tragedy", "donate bitcoin to victims" |

**Filter behavior:**
- Any `critical` or `high` violation → article auto-rejected (verdict=`reject`)
- Only `medium` violations → article goes to admin review queue (verdict=`review`)
- No violations → passes (verdict=`pass`)

**Integration with `evaluate_article()`:**
- New `adsense_safety` field added to `QualityScore` dataclass
- AdSense check runs FIRST (short-circuit) and acts as a hard gate
- `reject` verdict from AdSense forces overall `reject` regardless of quality score
- `quality_score_to_dict()` updated to serialize the new field for DB storage

**Scheduler integration:** Already correctly wired in `scheduler.py`:
- `reject` verdict → article is NOT saved to DB (no admin clutter)
- `review` verdict → saved with `status="pending_review"` (admin sees in queue)
- `publish` verdict → saved as `draft` (when `DRAFT_MODE=1`) for admin approval

This matches the PDF spec:
> "The scraper must automatically populate the admin panel with the top
> drafted articles for final manual review before publishing."

### 2.3 Verified Existing PDF-Compliant Features
These were already correctly implemented in V24 and require no changes:

- ✅ `scraper.py` scans 25+ premium RSS sources across 6 regions (World, USA,
  UK, Pakistan, India, Germany) — matches PDF "scans top external news websites"
- ✅ `quality_control.py` does cross-source fact compilation (via the existing
  `fact_check()` and `extract_entities()` functions)
- ✅ `main.py` + `database.py` serve public news pages + hidden authenticated
  admin dashboard at `/admin.html` (login via SHA-256 password hash)
- ✅ `scripts/gen_admin_creds.py` securely generates admin credentials
- ✅ Scraper auto-populates admin panel with drafts (via `DRAFT_MODE=1` default)
- ✅ Dockerfile + Procfile + nixpacks.toml present for Railway deployment
- ✅ `.env.example` documents all environment variables
- ✅ Compliance pages: `privacy.html`, `cookies.html`, `terms.html`,
  `corrections.html` — required for AdSense approval
- ✅ Security hardening: CSRF tokens, CSP/HSTS/XSS headers, rate limiting,
  Redis-backed distributed sessions, IP whitelist option
- ✅ Wikipedia-rival front-end interactivity: TL;DR summaries, fact-check
  badges, timeline data, live updates, polls, quizzes, comments, likes
- ✅ PWA: service worker + manifest for offline support
- ✅ SEO: structured data, Open Graph, Twitter Cards, slug URLs, sitemap.xml,
  robots.txt, RSS feed
- ✅ Scaling: Redis for distributed rate-limiting across multiple Uvicorn
  workers, leader election for scheduler (only one worker runs the scraper)
- ✅ Monitoring: optional Sentry SDK integration (lazy-imported)

---

## 3. Test Results

All tests pass:

```
✓ All 8 Python modules compile without syntax errors
✓ quality_control.adsense_safety_check() correctly:
    - Passes clean news articles (Fed Reserve, market news)
    - Rejects adult content (sex video, porn, leaked nudes)
    - Rejects bomb-making tutorials
    - Rejects hate speech (neo-Nazi propaganda, white power)
    - Rejects drug trafficking (cocaine smuggling, meth lab)
✓ evaluate_article() integration:
    - AdSense reject correctly forces overall verdict = "reject"
    - QualityScore serializes to JSON (for DB storage)
✓ Full FastAPI app boots in TestClient:
    - GET /health                  → 200 (returns feature list)
    - GET /api/stats               → 200 (returns empty stats)
    - GET /                        → 200 (homepage HTML)
    - GET /static/css/style.css    → 200, contains #800000, no #D97757
    - GET /admin.html              → 200 (admin dashboard)
    - POST /api/admin/login        → 200 (returns token + csrf_token)
    - GET /api/articles?region=world → 200 (empty list, expected)
    - GET /manifest.json           → 200, Content-Type: application/json
                                    theme_color: #800000
✓ Total routes registered: 66
```

---

## 4. How to Run Locally

```bash
cd sfaam-news

# 1. Create venv + install deps
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. (Optional) Copy env template
cp .env.example .env
# Edit .env: at minimum set ADMIN_PASSWORD_HASH + ADMIN_KEY (already have defaults)

# 3. Run!
python main.py
# OR: uvicorn main:app --reload --port 8000

# 4. Open http://localhost:8000
# 5. Admin: http://localhost:8000/admin.html
#    Password: [REDACTED — rotated, see README.md]
```

Without a PostgreSQL `DATABASE_URL`, the app auto-falls back to a local
SQLite file (`sfaam.db`) for development. Redis is also optional — falls
back to in-memory rate limiting (single-worker only).

---

## 5. Deploy to Railway

See `RAILWAY_DEPLOYMENT.md` for the complete step-by-step guide.

TL;DR:
1. Push this folder to GitHub.
2. Railway → New → Deploy from GitHub repo.
3. Add PostgreSQL (+ optional Redis) plugins.
4. Set env vars (see `.env.example`):
   - `DATABASE_URL` (auto-set by Railway Postgres)
   - `REDIS_URL` (auto-set by Railway Redis)
   - `ADMIN_PASSWORD_HASH`
   - `ADMIN_KEY`
   - `GROQ_KEY_*` / `GEMINI_KEY_*` (for AI rewriting)
5. Deploy. Visit `/admin.html`.

---

## 6. File Inventory (V25)

```
sfaam-news/
├── main.py                      # FastAPI app (3,287 lines, 66 routes)
├── database.py                  # SQLAlchemy async models
├── scraper.py                   # RSS scraper (25 sources, 6 regions)
├── ai_writer.py                 # 3-agent AI pipeline (Groq + Gemini)
├── google_search_writer.py      # Alt AI writer using Google Search results
├── scheduler.py                 # APScheduler pipeline (with AdSense gate)
├── quality_control.py           # ★ V25: + adsense_safety_check()
├── monitoring.py                # Sentry integration (lazy)
├── scripts/
│   ├── gen_admin_creds.py       # Admin credential generator
│   └── minify.py                # CSS/JS minifier
├── static/
│   ├── index.html, admin.html, article.html, category.html
│   ├── about.html, contact.html, search.html, founder.html
│   ├── privacy.html, terms.html, cookies.html, corrections.html
│   ├── bookmarks.html
│   ├── logo.png, manifest.json  # ★ V25: theme_color=#800000
│   ├── sw.js                    # Service worker
│   ├── css/style.css            # ★ V25: maroon #800000 primary
│   ├── css/style.min.css        # ★ V25: populated (was empty)
│   ├── js/app.js, js/config.js
│   ├── images/ (founder.png, founder-nav.png, placeholder.jpg)
│   └── audio/                   # TTS audio cache
├── Dockerfile                   # Railway-ready (tini + curl healthcheck)
├── Procfile                     # Alt start command
├── nixpacks.toml                # Alt builder config
├── requirements.txt             # ★ V25: populated (was empty)
├── runtime.txt                  # python-3.11
├── manifest.json                # PWA manifest (theme_color=#800000)
├── .env.example                 # All env vars documented
├── .gitignore
├── README.md
├── RAILWAY_DEPLOYMENT.md
├── SCALING_GUIDE.md
└── CHANGELOG_V25.md             # This file
```

---

Built to PDF spec. Ready for AdSense application.
