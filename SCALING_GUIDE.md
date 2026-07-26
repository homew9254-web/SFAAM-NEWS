# SFAAM NEWS V18 — Scaling & CDN Guide

This guide covers everything needed to scale SFAAM NEWS to **1 Million+ views** without crashing.

---

## 1. Cloudflare CDN Setup (FREE — 90% Traffic Reduction)

Cloudflare sits between your visitors and Railway, caching static assets at 300+ edge locations worldwide. This means **90% of requests never reach your Railway app**.

### Step 1: Create Cloudflare Account
1. Go to [cloudflare.com](https://cloudflare.com) → Sign up (free)
2. Click **Add a Site** → Enter your Railway domain (e.g. `sfaam-news-production.up.railway.app`)
3. Select **Free plan**

### Step 2: Configure Caching
In Cloudflare dashboard → **Caching → Configuration**:
- Caching Level: **Standard**
- Browser Cache TTL: **4 hours**
- Always Online: **ON** (serves cached pages if Railway is down)

### Step 3: Page Rules (Critical for News Site)
Go to **Page Rules** → Add these rules:

| Pattern | Setting | Value |
|---------|---------|-------|
| `*/static/*` | Cache Level | Cache Everything |
| `*/static/*` | Edge Cache TTL | 1 hour |
| `*/*.html` | Cache Level | Bypass (HTML must be fresh) |
| `*/api/*` | Cache Level | Bypass (API must be fresh) |
| `*/admin*` | Cache Level | Bypass (Admin never cached) |

### Step 4: Auto Minify (Speed Boost)
Go to **Speed → Optimization**:
- Auto Minify: **JavaScript, CSS, HTML** (all checked)
- Brotli: **ON**
- HTTP/2: **ON**
- HTTP/3: **ON**

### Step 5: Image Optimization
Go to **Speed → Optimization → Polish**:
- Lossless image compression: **ON**
- WebP conversion: **ON** (auto-converts PNG/JPG to WebP)

---

## 2. Redis Caching Layer (Built-in V18)

V18 includes built-in Redis caching for hot endpoints:

| Endpoint | Cache TTL | What it does |
|----------|-----------|--------------|
| `/api/articles/trending` | 5 min | Trending articles cached, DB skipped |
| `/api/stats` | 1 hour | Site stats cached, DB skipped |
| `/api/articles` | 1 min | Article list cached briefly |

### Enable Redis on Railway:
1. Railway dashboard → **+ New → Database → Redis**
2. Railway auto-sets `REDIS_URL` environment variable
3. App auto-detects and enables caching

**Cache invalidation is automatic** — when admin updates an article's fact-check status, all related caches are cleared instantly.

---

## 3. Database Connection Pooling

V18 uses SQLAlchemy's async connection pool with these defaults (already configured):

```python
pool_size=10         # 10 persistent connections
max_overflow=20      # +20 burst connections under load
pool_timeout=30      # Wait 30s for a free connection
pool_recycle=1800    # Recycle every 30 min (defeats idle timeouts)
pool_pre_ping=True   # Auto-detect dead connections
```

### PgBouncer (For 10K+ Concurrent Users)

If you expect 10,000+ simultaneous users, add PgBouncer:

1. Railway → **+ New → Database → PostgreSQL** (already done)
2. In Postgres settings → **Connections** → Enable **PgBouncer** (Railway's built-in pooler)
3. Set `PG_POOL_SIZE=5` (lower, since PgBouncer multiplexes)

---

## 4. Auto-Scaling Configuration

### Railway Scaling
In Railway → your web service → **Settings**:
- **Scaling**: Set min=1, max=5 instances
- **Health check**: `/health` (already configured in `railway.json`)
- **Restart policy**: ON_FAILURE with 5 retries

### Resource Limits
For 1M+ views:
- **CPU**: 2 vCPU per instance
- **RAM**: 2GB per instance
- **Instances**: 3-5 (auto-scaled)

---

## 5. Performance Optimization Checklist

### Already Done in V18:
- ✅ Lazy loading on all images (`loading="lazy"`)
- ✅ Stale-while-revalidate service worker (instant page loads)
- ✅ Redis caching on trending + stats endpoints
- ✅ Connection pooling (10 + 20 burst connections)
- ✅ Health check exempt from rate limiting
- ✅ Gzip compression (via Uvicorn)
- ✅ HTTP/2 support (via Railway)
- ✅ Image error fallback (placeholder on broken images)
- ✅ Pre-ping DB connections (no stale connection errors)

### Manual Setup Needed:
- ☐ Cloudflare CDN (see Section 1 above)
- ☐ Railway auto-scaling (see Section 4 above)
- ☐ WebP images (Cloudflare Polish handles this automatically)

---

## 6. Monitoring for 1M+ Views

### Railway Metrics
Railway dashboard → your service → **Metrics** tab:
- CPU usage (should stay < 70%)
- Memory usage (should stay < 80%)
- Request count
- Response time (should stay < 500ms)

### Cloudflare Analytics
Cloudflare dashboard → **Analytics**:
- Requests served by Cloudflare (cached) vs origin (Railway)
- Threats blocked
- Bandwidth saved

### App Health
Visit these URLs to verify health:
- `/health` — app health
- `/api/debug/pipeline` — pipeline status (admin only)
- `/api/stats` — article counts

---

## 7. Cost Estimate for 1M Views/Month

| Service | Free Tier | At 1M views |
|---------|-----------|-------------|
| Railway (Hobby) | $5/mo, 500hrs | $20/mo (Pro, 5 instances) |
| Railway Postgres | Included | Included |
| Railway Redis | $10/mo | $10/mo |
| Cloudflare | Free | Free (Pro $20/mo optional) |
| Groq API | Free tier | $0 (free tier covers 1M tokens) |
| **Total** | **$5/mo** | **~$50/mo** |

---

## 8. Emergency Procedures

### Site is slow / crashing
1. Check Railway → **Metrics** — is CPU/RAM maxed?
2. Check Cloudflare → **Analytics** — is traffic spiked?
3. If Railway overloaded: increase instances in Railway → **Settings → Scaling**
4. If DB overloaded: enable PgBouncer (Section 3)

### Database connection errors
1. Check Railway → Postgres → **Logs**
2. Verify `DATABASE_URL` is set (auto-set by Railway)
3. The app auto-recycles dead connections (`pool_pre_ping=True`)

### Articles not appearing
1. Visit `/admin.html` → login → click **Stage 1: Scrape Only**
2. Wait 2 minutes → click **Stages 2-5: Publish**
3. Check `/api/pipeline-status` for errors
4. Verify `GROQ_KEY_*` env vars are set in Railway

---

## Support

If you hit scaling issues not covered here:
1. Check Railway **Deploy Logs** first
2. Visit `/admin.html` → **Check Health**
3. Check Cloudflare **Analytics** for traffic patterns
4. Email: `editorial@sfaamnews.com`
