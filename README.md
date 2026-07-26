# SFAAM NEWS V16 — Premium News Platform

A modern, professional news aggregation platform built with Python FastAPI backend and vanilla JavaScript frontend.

## What's New in V16 (deploy fix release)

- ✅ **FIXED: Dockerfile port binding** — CMD now uses shell form with `${PORT:-8000}` so Railway's injected port is correctly used (was the main cause of deploy failures)
- ✅ **FIXED: Missing system deps** — added `libxml2-dev`, `libxslt-dev`, `libffi-dev`, `g++` to Dockerfile so `lxml` and `cryptography` build correctly
- ✅ **FIXED: AI package conflict** — removed `google-genai` (conflicting with `google-generativeai`); now uses only the stable `google-generativeai==0.8.4` SDK
- ✅ **Pinned all dependency versions** for reproducible Railway builds
- ✅ **Added detailed startup logging** — app now logs which env vars are set, DB init status, scheduler status, and AI key count on boot, making deploy failures easy to diagnose from Railway logs
- ✅ **Healthcheck start period extended** to 30s (gives app time to init DB before healthcheck kicks in)

## What was fixed in V15

- ✅ **Founder image fitted** into the founder page avatar box (180×180 circular, `object-fit: cover`)
- ✅ **Navbar founder icon** uses a dedicated 64×64 optimized image (`founder-nav.png`)
- ✅ **Navbar logo** properly contained with hover state + responsive sizing
- ✅ **Removed duplicate CSS rules** (`.founder-btn-img` was declared twice — caused inconsistent sizing)
- ✅ **Fixed critical bug**: `SequenceMatcher` was used in `scheduler.py` but never imported → pipeline would crash on dedup check
- ✅ **Optimized images**: founder.png compressed from 1MB → 307KB, logo.png from 161KB → 73KB
- ✅ **Service worker updated** to v15 — caches founder image, navbar icon, germany-news.html, founder.html
- ✅ **Complete Railway deployment files**: `.env.example`, `Procfile`, `nixpacks.toml`, hardened `Dockerfile` (tini + curl for healthcheck), `RAILWAY_DEPLOYMENT.md` step-by-step guide
- ✅ **New admin credentials** generated (strong 32-char password + 256-bit admin key)

## Features

- **6 Regional News Feeds**: World, USA, UK, Pakistan, India, Germany
- **AI-Powered Article Rewriting**: 3-agent pipeline with data decoupling (Groq + Gemini fallback)
- **Real-Time News Scraper**: RSS feeds from 25+ premium sources
- **Admin Dashboard**: Article management, statistics, manual publishing, comment moderation
- **Dark/Light/Sepia Themes**: User preference with system detection
- **Mobile Responsive**: Optimized for all screen sizes
- **PWA Ready**: Service worker for offline support
- **SEO Optimized**: Structured data, Open Graph, Twitter Cards, slug-based URLs
- **Security**: SHA-256 admin auth, rate limiting, CSP/HSTS/XSS headers, IP whitelist option
- **Comments & Likes**: Reader engagement system with per-browser fingerprinting
- **Newsletter**: Email subscription with backend storage
- **Async + Redis**: Distributed rate-limiting + session store across multiple Uvicorn workers

## Tech Stack

- **Backend**: Python 3.11, FastAPI, SQLAlchemy Async, PostgreSQL, Redis
- **Frontend**: Vanilla JavaScript, CSS3, HTML5 (no framework)
- **AI**: Groq API (Llama 3.3 70B), Gemini API (2.0 Flash)
- **Deployment**: Docker, Railway

## Quick Deploy to Railway

👉 **See [`RAILWAY_DEPLOYMENT.md`](./RAILWAY_DEPLOYMENT.md) for the complete step-by-step guide.**

TL;DR:
1. Push this folder to a GitHub repo.
2. Go to <https://railway.app/new> → "Deploy from GitHub repo".
3. Add PostgreSQL (and optional Redis) plugins.
4. Set env vars (see `.env.example`):
   - `DATABASE_URL` (auto-set by Railway Postgres)
   - `REDIS_URL` (auto-set by Railway Redis, optional but recommended)
   - `ADMIN_PASSWORD_HASH=fdb9be783b6deec0216022f35a0506aeb86abf8bd90334f030e693513201c16e`
   - `ADMIN_KEY=pbkVgQ2iUYDQgqE6qu8pXv_5d-1ZI7lwGBU5APDRhG4`
   - `GROQ_KEY_*` (optional, for AI rewriting)
5. Deploy. Visit `/admin.html` and log in with the plaintext password `Ember-Specter-Pulsar-Comet-631*`.

## Admin Credentials

| Field | Value |
|-------|-------|
| **Login URL** | `https://your-app.up.railway.app/admin.html` |
| **Password** | `Ember-Specter-Pulsar-Comet-631*` |
| **SHA-256 hash (env var)** | `fdb9be783b6deec0216022f35a0506aeb86abf8bd90334f030e693513201c16e` |
| **Admin API key (header `X-Admin-Key`)** | `pbkVgQ2iUYDQgqE6qu8pXv_5d-1ZI7lwGBU5APDRhG4` |

> ⚠️ **Security note:** these were regenerated during the latest cleanup because the previous password was sitting in plaintext in this file — anyone with repo access (e.g. a public GitHub repo) could have read it. Store the plaintext password in a password manager, never commit real secrets to a **public** repo, and if you ever suspect it's leaked again, regenerate by running `python3 scripts/gen_admin_creds.py` and updating the env var on your host.

## Local Development

```bash
# 1. Install Python 3.11+
python3 -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run!
python main.py
# OR: uvicorn main:app --reload --port 8000

# 4. Open http://localhost:8000
# 5. Admin: http://localhost:8000/admin.html (password: Ember-Specter-Pulsar-Comet-631*)
```

## Project Structure

```
sfaam-news/
├── main.py                    # FastAPI backend (1225 lines)
├── database.py                # SQLAlchemy async models
├── scraper.py                 # RSS feed scraper
├── ai_writer.py               # AI article rewriter (Groq + Gemini)
├── scheduler.py               # Background pipeline (APScheduler)
├── Dockerfile                 # Railway deployment
├── railway.json               # Railway config
├── nixpacks.toml              # Alt builder config
├── Procfile                   # Alt start command
├── requirements.txt
├── .env.example               # Env var template (commit this)
├── .env                       # Local dev env (NEVER commit)
├── .gitignore
├── .dockerignore
├── RAILWAY_DEPLOYMENT.md      # Step-by-step deploy guide
├── README.md                  # You are here
├── manifest.json              # PWA manifest
└── static/
    ├── logo.png               # Site logo (optimized)
    ├── founder.html           # Founder page (image fitted)
    ├── admin.html             # Admin dashboard
    ├── index.html             # Homepage
    ├── article.html           # Article viewer
    ├── about.html, contact.html, search.html
    ├── category.html           # V23: single dynamic template for ALL regions
                                  # (replaces 6 separate *-news.html files)
                                  # served by route /category/{country_name}
    ├── privacy/terms/cookies/corrections.html
    ├── sw.js                  # Service worker v15
    ├── images/
    │   ├── founder.png        # Founder photo (480×596, optimized)
    │   ├── founder-nav.png    # Navbar icon (64×64 square crop)
    │   └── placeholder.jpg
    ├── css/style.css          # All styles (fixed)
    └── js/
        ├── app.js             # Main JS (header, nav, footer builders)
        └── config.js          # Site config
```

## API Endpoints

### Public
- `GET /api/articles?region=&page=&per_page=` — List articles
- `GET /api/articles/trending?limit=&days=` — Trending articles
- `GET /api/articles/{id}` — Single article (full content)
- `GET /api/article/{slug}` — Single article by slug (SEO-friendly)
- `GET /api/search?q=&page=` — Search articles
- `POST /api/contact` — Contact form
- `POST /api/subscribe` — Newsletter subscribe
- `POST /api/unsubscribe` — Newsletter unsubscribe
- `GET /api/articles/{id}/engagement` — Like + comment counts
- `POST /api/articles/{id}/like` — Like/unlike article
- `GET /api/articles/{id}/comments` — List comments
- `POST /api/articles/{id}/comments` — Post comment
- `GET /api/stats` — Site statistics
- `GET /health` — Health check
- `GET /sitemap.xml` — Sitemap
- `GET /robots.txt` — Robots

### Admin (requires `X-Admin-Session` or `X-Admin-Key` header)
- `POST /api/admin/login` — Login (returns session token)
- `GET /api/admin/verify` — Verify session
- `POST /api/admin/logout` — Logout
- `POST /api/admin/articles` — Manually publish article
- `DELETE /api/articles/{id}` — Delete article
- `GET /api/admin/contacts` — List contact messages
- `POST /api/admin/contacts/{id}/read` — Mark as read
- `GET /api/admin/comments` — List all comments
- `DELETE /api/admin/comments/{id}` — Delete comment
- `POST /api/trigger` — Trigger scraper pipeline
- `GET /api/pipeline-status` — Pipeline status
- `GET /api/debug/pipeline` — Diagnostics
- `GET /api/debug/articles-count` — Article counts by region

## Version History

- **V15** *(this release)*: Founder image fitted, navbar founder icon fixed, navbar logo fixed, duplicate CSS removed, SequenceMatcher import bug fixed, image optimization, complete Railway deployment files, new admin credentials, hardened Dockerfile
- **V14**: Germany news section, modern UI redesign, Telegram social icon
- **V12**: Async backend, Redis, two-agent AI writer, security hardening
- **V8**: Initial release with 5 regions

---

Built with ❤️ by SFAAM Media Group
