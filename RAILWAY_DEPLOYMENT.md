# SFAAM NEWS — Railway Deployment Guide (Complete)

This guide walks you through deploying SFAAM NEWS to Railway from scratch.
Estimated time: **10–15 minutes**.

---

## ✅ Prerequisites

1. A [Railway](https://railway.app) account (free trial available).
2. (Optional) A [Groq API key](https://console.groq.com/keys) — free, used for AI article rewriting. Without it, articles fall back to raw RSS summaries (still works, just less polished).
3. The updated `sfaam-news-v15-fixed.zip` file from this delivery.

---

## 📦 Step 1 — Unzip the project locally

```bash
unzip sfaam-news-v15-fixed.zip
cd sfaam-news
```

Verify these files exist:

```
sfaam-news/
├── main.py
├── database.py
├── scraper.py
├── ai_writer.py
├── scheduler.py
├── Dockerfile
├── railway.json
├── nixpacks.toml          ← NEW (fallback builder)
├── Procfile               ← NEW (alt start command)
├── requirements.txt
├── runtime.txt
├── .env.example           ← NEW (env var template)
├── .gitignore             ← NEW
├── .dockerignore          ← NEW
├── manifest.json
├── static/
│   ├── logo.png           ← optimized (73KB)
│   ├── founder.html       ← updated
│   ├── admin.html
│   ├── index.html
│   ├── article.html
│   ├── ... (other HTML)
│   ├── images/
│   │   ├── founder.png       ← your uploaded photo (307KB, 480×596)
│   │   ├── founder-nav.png   ← NEW (64×64 navbar icon, 6.5KB)
│   │   └── placeholder.jpg
│   ├── css/style.css      ← fixed (logo, founder avatar, navbar icon)
│   └── js/
│       ├── app.js         ← updated navbar image
│       └── config.js      ← updated image paths
└── RAILWAY_DEPLOYMENT.md ← you are here
```

---

## 🚂 Step 2 — Create Railway project

1. Go to <https://railway.app/new>.
2. Click **"Deploy from GitHub repo"** if you've pushed it to GitHub, OR click **"Deploy from template"** and upload the unzipped folder.
   - **Easiest path**: push the `sfaam-news/` folder to a new GitHub repo, then connect Railway to it.
3. Railway auto-detects `Dockerfile` and starts building.

---

## 🗄️ Step 3 — Add PostgreSQL + Redis

1. In your Railway project, click **"+ New"** → **"Database"** → **"Add PostgreSQL"**.
2. Click **"+ New"** → **"Database"** → **"Add Redis"** (optional but recommended).
3. Railway auto-creates `DATABASE_URL` and `REDIS_URL` variables.

---

## 🔐 Step 4 — Set environment variables

In Railway, go to your **web service** → **"Variables"** tab. Click **"Raw Editor"** and paste:

```env
SITE_URL=https://your-app-name.up.railway.app
DATABASE_URL=<auto-set by Railway when you add Postgres>
REDIS_URL=<auto-set by Railway when you add Redis>

# ─── ADMIN CREDENTIALS (REQUIRED) ───
ADMIN_PASSWORD_HASH=fdb9be783b6deec0216022f35a0506aeb86abf8bd90334f030e693513201c16e
ADMIN_KEY=pbkVgQ2iUYDQgqE6qu8pXv_5d-1ZI7lwGBU5APDRhG4

# ─── OPTIONAL: IP whitelist for admin (comma-separated, leave empty to allow any) ───
ADMIN_IP_WHITELIST=

# ─── RATE LIMITS (defaults are fine) ───
RATE_LIMIT=100
RATE_WINDOW=60
CONTACT_RATE_LIMIT=5
SUBSCRIBE_RATE_LIMIT=10

# ─── ARTICLE RETENTION (days, 0 = never) ───
DELETE_AFTER_DAYS=30

# ─── CORS ───
CORS_ORIGINS=https://your-app-name.up.railway.app

# ─── AI KEYS (optional — without these, articles use raw RSS summary) ───
GROQ_KEY_WORLD=<your-groq-key>
GROQ_KEY_USA=<your-groq-key>
GROQ_KEY_UK=<your-groq-key>
GROQ_KEY_PAKISTAN=<your-groq-key>
GROQ_KEY_INDIA=<your-groq-key>
GROQ_KEY_GERMANY=<your-groq-key>
# Gemini fallback (optional):
GEMINI_KEY_WORLD=
GEMINI_KEY_USA=
GEMINI_KEY_UK=
GEMINI_KEY_PAKISTAN=
GEMINI_KEY_INDIA=
GEMINI_KEY_GERMANY=
```

> 💡 After your custom domain is connected, update `SITE_URL` and `CORS_ORIGINS` to your real domain (e.g. `https://sfaamnews.com`).

---

## 🚀 Step 5 — Deploy

1. Click **"Deploy"** in Railway.
2. Watch the build logs. First build takes ~2–3 minutes.
3. Once deployed, Railway gives you a URL like `https://sfaam-news-production.up.railway.app`.
4. Visit `/health` to verify: should return JSON `{"status":"healthy", ...}`.
5. Visit `/` to see the homepage.
6. Visit `/admin.html` to log in (see credentials below).

---

## 🔑 Admin credentials (memorize or store in a password manager)

| Field | Value |
|-------|-------|
| **Login URL** | `https://your-app.up.railway.app/admin.html` |
| **Password (plaintext — type this to log in)** | `Ember-Specter-Pulsar-Comet-631*` |
| **SHA-256 Hash (in env var)** | `fdb9be783b6deec0216022f35a0506aeb86abf8bd90334f030e693513201c16e` |
| **Admin API Key (for scripts, in `X-Admin-Key` header)** | `pbkVgQ2iUYDQgqE6qu8pXv_5d-1ZI7lwGBU5APDRhG4` |

> ⚠️ **NEVER** share the plaintext password or commit it to git.
> If you forget it, you must regenerate (run `python3 scripts/gen_admin_creds.py`) and update the `ADMIN_PASSWORD_HASH` env var on Railway.

### Using the Admin API key

```bash
# Example: trigger the scraper pipeline
curl -X POST https://your-app.up.railway.app/api/trigger \
  -H "X-Admin-Key: pbkVgQ2iUYDQgqE6qu8pXv_5d-1ZI7lwGBU5APDRhG4"

# Example: list all articles count by region
curl https://your-app.up.railway.app/api/debug/articles-count \
  -H "X-Admin-Key: pbkVgQ2iUYDQgqE6qu8pXv_5d-1ZI7lwGBU5APDRhG4"
```

---

## 🧪 Step 6 — Trigger the first news scrape

After deploying, articles won't appear until the scraper runs. The scheduler runs hourly automatically, but for the first run:

1. Go to `/admin.html`, log in.
2. Open the **"System"** tab.
3. Click **"Run Pipeline Now"**.
4. Wait ~30 seconds, then refresh the homepage. Articles should appear.

Or via API:

```bash
curl -X POST https://your-app.up.railway.app/api/trigger \
  -H "X-Admin-Key: pbkVgQ2iUYDQgqE6qu8pXv_5d-1ZI7lwGBU5APDRhG4"
```

---

## 🌐 Step 7 — (Optional) Connect a custom domain

1. In Railway, go to your web service → **"Settings"** → **"Networking"** → **"Generate Domain"** (gives a free `*.up.railway.app` URL).
2. For a custom domain like `sfaamnews.com`:
   - Click **"Custom Domain"** → enter your domain.
   - Railway shows you a CNAME record to add at your DNS provider.
   - After DNS propagates (5–60 min), Railway issues a free Let's Encrypt SSL cert.
3. Update `SITE_URL` and `CORS_ORIGINS` env vars to your new domain.

---

## 🩺 Troubleshooting

### Deploy fails / healthcheck fails / container crashes?

This is the most common issue. Here's how to debug it:

1. **Open Railway → your service → "Deployments" tab → click the latest failed deployment**.
2. Scroll to the **bottom of the build logs** — the actual error is usually the last 5-10 lines.
3. Common errors and fixes:

   | Error message | Fix |
   |---|---|
   | `Health check failed: port ... is not listening` | The Dockerfile CMD was hardcoded to port 8000. V16 fixes this — make sure you're using the V16 zip. |
   | `ModuleNotFoundError: No module named 'xxx'` | A Python package is missing. Check `requirements.txt` includes it. |
   | `error: command 'gcc' failed` | System deps missing. V16 Dockerfile adds `gcc`, `g++`, `libxml2-dev`, `libxslt-dev`, `libffi-dev`. |
   | `RuntimeError: Unsupported DATABASE_URL scheme` | `DATABASE_URL` env var has wrong format. Should be `postgresql://...` (Railway auto-sets this when you add Postgres). |
   | `Connection refused` to Postgres | Wait 30s after adding Postgres — it takes time to provision. Then redeploy. |
   | `Could not import 'google.genai'` | V16 removed this dependency. Make sure you're using V16 zip and rebuild. |
   | App starts but `/health` returns 500 | Check the startup logs — V16 logs exactly which step failed (DB init, scheduler, etc.) |

4. **After fixing env vars, you MUST redeploy** — Railway doesn't auto-restart on env var changes for Dockerfile deploys. Click "Deploy" → "Redeploy".

### Articles not appearing?

1. Go to `/admin.html` → **"System"** tab → click **"Debug Pipeline"**.
2. The response shows exactly which check failed:
   - `database` FAIL → check `DATABASE_URL`.
   - `redis` FAIL → check `REDIS_URL` (or remove to use in-memory).
   - `ai_keys` all false → set `GROQ_KEY_*` env vars (or articles use raw RSS).
   - `rss_feeds` FAIL → network issue, check Railway region.
   - `test_save` FAIL → DB permission issue, check Postgres plugin.

### Admin login fails?

- Verify `ADMIN_PASSWORD_HASH` env var is set (not `ADMIN_PASSWORD`).
- Verify you're typing the password **exactly** (case-sensitive, including the trailing `?`).
- After 10 failed attempts from one IP, you're locked out for 15 minutes (brute-force protection).

### Logo / founder image missing?

- Make sure `/static/images/founder.png` returns 200 (visit it directly in browser).
- The image is bundled in the Docker image at `static/images/founder.png`.
- If using a custom domain, verify `SITE_URL` matches — the OG image tags use absolute URLs.

### Want to scale to multiple workers?

Set the start command to:
```
uvicorn main:app --host 0.0.0.0 --port $PORT --workers 4
```
**⚠️ WARNING:** Multiple workers REQUIRE `REDIS_URL` to be set, otherwise rate-limiting and admin sessions will not work consistently across workers.

---

## 💰 Cost estimate (Railway)

- **Hobby plan**: $5/month, includes 500 execution hours + $5 of usage.
  - One web service (always-on): ~$5/month.
  - PostgreSQL: ~$0.20/GB after the free 1GB.
  - Redis: ~$0.10/GB after the free 50MB.
- **Total for a small news site**: ~$5–$8/month.

---

## 📞 Support

If something breaks:

1. Check Railway **Deploy Logs** (most informative).
2. Check the `/health` and `/api/debug/pipeline` endpoints.
3. Check the `ADMIN_PASSWORD_HASH` and `ADMIN_KEY` env vars are set correctly.

That's it — your SFAAM NEWS site is live! 🎉
