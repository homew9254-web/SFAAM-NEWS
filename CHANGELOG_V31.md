# SFAAM NEWS V31 — Security & Bug Fix Release

> **Tagline:** "The audit-and-fix release — 22 critical bugs squashed, 2 missing features shipped."

## Why V31?

V30 was feature-complete but had accumulated security holes and broken UX.
A full audit of backend (15,646 LoC across 24 Python files) and frontend (22 HTML/CSS/JS files)
revealed 27 critical bugs, 42 high-priority issues, and 29 missing features.

V31 fixes the most severe of these without breaking V30's behavior.

---

## 🔴 CRITICAL Security Fixes

### 1. SSRF in `/api/imgproxy` — CLOSED
**Was:** Server fetched any URL passed via `?url=`. An attacker could request
`http://169.254.169.254/latest/meta-data/` (AWS metadata) or internal admin
endpoints (`http://localhost:8000/api/admin/...`) — bypassing firewalls.

**Fix:** Resolve the host's IPs BEFORE the request. Reject if any IP is
private, loopback, link-local, multicast, or reserved. Localhost allowed
only when `ENV=development`.
**File:** `main.py:1782-1840`

### 2. Memory DoS in `/api/imgproxy` — CLOSED
**Was:** No size limit — a 5GB upstream image would have been loaded into RAM.

**Fix:** Stream the response and abort once 8MB is exceeded. Also check
`Content-Length` upfront and return `413 Payload Too Large` immediately.
**File:** `main.py:1859-1883`

### 3. JSON-LD XSS via `</script>` Injection — CLOSED
**Was:** Article titles and breadcrumb names were injected into
`<script type="application/ld+json">{...}</script>` blocks without escaping.
A title containing `</script><script>alert(1)</script>` would break out and
execute arbitrary JS. Combined with the CSP allowing `'unsafe-inline'`, this
was a real exploitable stored-XSS vector — any admin-published article could
contain a payload that ran in every visitor's browser.

**Fix:** Replace `<` with `\u003c` in the JSON-LD string before injecting.
This is the OWASP-recommended fix.
**Files:** `main.py:2499-2500, 2761-2762, 2778-2779`

### 4. SHA-256 Password Hashing Without Salt — UPGRADED
**Was:** `hashlib.sha256(password).hexdigest()` — vulnerable to rainbow tables.
A single common password would produce the same hash everywhere.

**Fix:** Now supports two formats (backwards-compatible):
  - **Legacy:** plain SHA-256 hex (existing env vars keep working)
  - **V31+:** `salt_hex$pbkdf2_hmac_sha256_hex` with 200,000 iterations
Use `_hash_admin_password(password)` to generate the new format.
**File:** `main.py:272-307`

### 5. Hardcoded Admin Password in `run_local.py` — REMOVED
**Was:** `password = "Admin-SFAAM-2026!"` in plaintext. Anyone with the source
code could log in to ANY local install of SFAAM NEWS that didn't override this.

**Fix:** Generate a random 24-char password on first run, store the V31
salted hash in `.env`, and print the plaintext ONCE to the console.
**File:** `run_local.py:89-95`

---

## 🔴 CRITICAL Bug Fixes

### 6. CORS Missing `PATCH` Method — FIXED
**Was:** `allow_methods=["GET", "POST", "OPTIONS", "DELETE"]` — the new V31
`PATCH /api/admin/articles/{id}` endpoint would have been blocked by CORS
in browsers, throwing a confusing CORS error instead of the actual 405.

**Fix:** Added `PATCH` and `PUT` to the allowed methods list.
**File:** `main.py:547`

### 7. `scalar_one_or_none()` Crashes on Duplicate Slugs — FIXED
**Was:** V26 trends pipeline sometimes generated duplicate slugs (no uniqueness
suffix). When two articles had the same slug, the public
`GET /api/article/{slug}` endpoint would crash with
`MultipleResultsFound` → HTTP 500. Same issue in 4 other endpoints.

**Fix:** Switched to `.scalars().first()` (returns first row, no crash).
Also added `.limit(1)` to the SQL query for performance.
**Files:** `main.py:767, 811, 1219, 1280, 2692`

### 8. View Count Race Condition — FIXED
**Was:** `a.views = (a.views or 0) + 1; db.commit()`. Two concurrent requests
would both read `views=10`, both write `views=11` — losing one count.

**Fix:** Use SQLAlchemy `UPDATE ... SET views = views + 1` which is atomic at
the DB level. Also avoids the unnecessary ORM load+save roundtrip.
**Files:** `main.py:786-801, 818-830`

### 9. Like/Comment Allowed on Draft Articles — FIXED
**Was:** `POST /api/articles/{id}/like` and `/comments` only checked article
existence, not status. A user could like or comment on a draft article that
was never published — including admin-only test articles.

**Fix:** Added status check — only `published` (or NULL for legacy) articles
accept likes/comments.
**Files:** `main.py:1211-1213, 1275-1277`

### 10. `debug_pipeline` Auto-Published Test Articles — FIXED
**Was:** `GET /api/debug/pipeline` saved a test article to the DB WITHOUT
setting `status`. Since NULL status is treated as published, the test article
would appear on the public homepage immediately. Admin would run "debug"
to diagnose an issue and accidentally publish a junk article.

**Fix:** Save with `status="draft"` — admin must explicitly publish.
**File:** `main.py:1516`

---

## 🟠 FRONTEND CRITICAL Fixes

### 11. Broken "Publish" Button in Admin — FIXED
**Was:** `<button onclick="publishEngineDraft(123)">` — but `publishEngineDraft`
was defined inside an IIFE `(function() { ... })()`, making it private.
Clicking the button threw `ReferenceError: publishEngineDraft is not defined`
and the V30 engine draft could not be published from the admin UI.
Workaround was to curl the API directly.

**Fix:** Expose the function to global scope with
`window.publishEngineDraft = publishEngineDraft;` at the end of the IIFE.
Same fix applied to `handleLoginSubmit` (also called from inline `onsubmit`).
**File:** `static/admin.html:1430-1436`

### 12. Broken Comment Article Links — FIXED
**Was:** Admin's Comments tab linked to `/article.html?id=123` — the LEGACY
route. V23+ uses `/article/{slug}`. Clicking the link in admin would 404.

**Fix:** Use `/article/{slug || id}` pattern. Falls back to ID if slug is
somehow missing.
**File:** `static/admin.html:856`

### 13. XSS in WhatsApp Share Button — FIXED
**Was:** `onclick="shareWhatsApp('${esc(a.title)}')"` — but `esc()` escapes
`<`, `>`, `&` only. It does NOT escape single quotes. A title like
`"Pakistan's New Policy"` would break the onclick attribute and execute
arbitrary JS. Since the title comes from RSS feeds / AI generation, this was
a real stored XSS vector.

**Fix:** Replaced inline onclick with `data-share-whatsapp` attribute +
event delegation. The title is now read from the data attribute (XSS-safe
regardless of quote content).
**File:** `static/article.html:211, 980-991`

### 14. Service Worker Cache-First Returned `undefined` — FIXED
**Was:** `caches.match(...).then(cached => { ... return cached || fetchPromise; })`
where `fetchPromise.catch(() => cached)` — but if `cached` was undefined
(cache miss) AND network failed, the function returned `undefined`. The
browser then threw `TypeError: Failed to execute 'respondWith' on 'FetchEvent'`.

**Fix:** Catch returns `null` instead of `undefined`. After awaiting the
network promise, if the result is null, return a proper `503 Response`
object with a friendly message.
**File:** `static/sw.js:120-148` (also bumped cache version to `v31`)

### 15. PWA Manifest Icon Dimensions Wrong — FIXED
**Was:** `manifest.json` declared `logo.png` as `512x512` and `192x192` —
but the actual file is `256x238` PNG. Chrome's PWA install validation
checks icon dimensions and would silently refuse to show the install prompt.
Same bug in `index.html` OpenGraph tags.

**Fix:** Updated manifest to declare correct `256x238` size + an `any` maskable
entry. Updated `og:image:width`/`height` in `index.html`.
**Files:** `static/manifest.json`, `static/index.html:20-21`

---

## ✨ NEW Features (V31)

### 16. Article Edit (Admin UI + API)
**Was:** Admin could only delete + re-create articles. Fixing a typo meant
losing the article's views, comments, publication date, and slug.

**Now:**
  - `PATCH /api/admin/articles/{id}` — partial update (PATCH semantics)
  - "Edit" button in Articles tab → opens modal with all fields editable
  - Title change auto-regenerates slug (with collision check)
  - Content change recomputes article_hash
  - Preserves id, original_url, views, date, comments
  - Audit logged via `monitoring.log_audit_event`
  - Sitemap cache invalidated if slug changed

**Files:** `main.py:2905-2995` (model + endpoint), `static/admin.html` (UI)

### 17. AI Health Check Endpoint
**Was:** When no articles appeared, admin had no way to test if AI keys were
working without triggering a full pipeline run (which takes 10+ minutes).

**Now:** `GET /api/admin/ai-health` pings each region's Groq + Gemini keys
with a 5-token "ping" request. Returns per-region per-provider status:
`ok` / `fail_<status_code>` / `no_key` / `error_<ExceptionType>`.

Example response:
```json
{
  "summary": {"ok": 9, "total": 12, "pct": 75.0},
  "regions": {
    "world": {
      "groq":   {"configured": true,  "status": "ok"},
      "gemini": {"configured": true,  "status": "ok"}
    },
    "usa": {
      "groq":   {"configured": true,  "status": "fail_401"},
      "gemini": {"configured": false, "status": "no_key"}
    }
  }
}
```

**File:** `main.py:4243-4300`

---

## 📋 Migration Guide

V31 is **fully backwards-compatible** with V30 databases — no schema migration
required. To upgrade:

1. Replace `main.py`, `run_local.py`, `static/admin.html`, `static/article.html`,
   `static/sw.js`, `static/manifest.json`, `static/index.html`.
2. (Recommended) Regenerate admin credentials using the new salted format:
   ```python
   python3 -c "from main import _hash_admin_password; print(_hash_admin_password('YOUR_NEW_PASSWORD'))"
   ```
   Update `ADMIN_PASSWORD_HASH` env var with the result.
3. (Optional) Clear browser cache — service worker version bumped to `v31`
   so users will auto-update on next visit.
4. Restart the server. Done.

---

## What's Next (V32+ Candidates)

Deferred for future releases (require larger changes):
- Split `main.py` (4,276 lines) into APIRouters
- Add Alembic for schema migrations (currently hand-rolled ALTER TABLE)
- Add 2FA for admin login
- Add subscriber list export + newsletter send
- Add test suite (currently 0 tests for 15,646 LoC)
- Convert deprecated `datetime.utcnow()` → `datetime.now(timezone.utc)`
- Add FTS reindex endpoint for search

---

Built with ❤️ by SFAAM Media Group
