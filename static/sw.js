/* ============================================
   SFAAM NEWS V23 - Service Worker (Optimized)
   - Cache-first for CSS / JS / fonts / images  (instant repeat loads)
   - Network-first for HTML navigation          (fresh content + offline fallback)
   - Network-only for /api/                     (data must be live)
   - Dynamic /category/* and /article/* routing support
   ============================================ */

const CACHE_VERSION = 'v31';
const STATIC_CACHE  = `sfaam-static-${CACHE_VERSION}`;
const RUNTIME_CACHE = `sfaam-runtime-${CACHE_VERSION}`;

// Core assets we pre-cache on install. Removed per-category HTML files
// (they no longer exist — replaced by single dynamic /category/{name} route).
const STATIC_ASSETS = [
  '/',
  '/static/css/style.css',
  '/static/css/style.min.css',
  '/static/js/app.js',
  '/static/js/app.min.js',
  '/static/js/config.js',
  '/static/logo.png',
  '/static/manifest.json',
  '/static/images/placeholder.jpg',
  '/static/images/founder.png',
  '/static/images/founder-nav.png',
  '/static/index.html',
  '/static/article.html',
  '/static/category.html',
  '/static/search.html',
  '/static/about.html',
  '/static/contact.html',
  '/static/founder.html',
  '/static/terms.html',
  '/static/privacy.html',
  '/static/cookies.html',
  '/static/corrections.html',
  '/static/bookmarks.html',
  '/static/admin.html',
  '/sitemap.xml',
  '/rss.xml'
];

// ─────────────────────────────────────────────
// INSTALL — pre-cache core assets (fail-tolerant)
// ─────────────────────────────────────────────
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(STATIC_CACHE)
      .then(cache => Promise.allSettled(STATIC_ASSETS.map(url => cache.add(url))))
      .then(() => self.skipWaiting())
      .catch(err => console.log('[SW] Cache install error:', err))
  );
});

// ─────────────────────────────────────────────
// ACTIVATE — drop old caches + claim all clients
// ─────────────────────────────────────────────
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys
          .filter(k => k !== STATIC_CACHE && k !== RUNTIME_CACHE)
          .map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// ─────────────────────────────────────────────
// FETCH — strategy router
// ─────────────────────────────────────────────
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;

  const url = new URL(e.request.url);

  // Same-origin only — never intercept cross-origin requests
  if (url.origin !== self.location.origin) return;

  // ── API calls: network only (data must be fresh) ──
  if (url.pathname.startsWith('/api/')) {
    return;
  }

  // ── Admin page: always network-first (security) ──
  if (url.pathname.includes('admin')) {
    e.respondWith(
      fetch(e.request).catch(() =>
        new Response('Admin panel requires network connection.', { status: 503 })
      )
    );
    return;
  }

  // ── HTML navigations: network-first with offline fallback ──
  // Covers: /, /article/{slug}, /category/{name}, *.html
  if (e.request.mode === 'navigate' ||
      url.pathname.endsWith('.html') ||
      url.pathname.startsWith('/article/') ||
      url.pathname.startsWith('/category/')) {
    e.respondWith(
      fetch(e.request)
        .then(res => {
          // Cache a copy for offline use
          if (res.ok) {
            const clone = res.clone();
            caches.open(RUNTIME_CACHE).then(cache => cache.put(e.request, clone));
          }
          return res;
        })
        .catch(() => caches.match(e.request).then(cached => {
          // V32.1 BUGFIX: When offline, an article page (e.g. /article/some-slug)
          // that is NOT in the cache was falling back to the HOMEPAGE — readers
          // tapping a saved bookmark saw the homepage instead of the article.
          // The bookmarks.html page advertises "Read offline" but the SW was
          // silently redirecting to home. Fix: if no cached article, return a
          // clear offline article page rather than the homepage. Only fall back
          // to the homepage for non-article navigations (e.g. /, /about.html).
          if (cached) return cached;
          if (url.pathname.startsWith('/article/')) {
            return new Response(
              '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
              '<meta name="viewport" content="width=device-width,initial-scale=1.0">' +
              '<title>Offline — SFAAM NEWS</title>' +
              '<link rel="stylesheet" href="/static/css/style.css"/>' +
              '</head><body style="max-width:600px;margin:60px auto;padding:24px;font-family:Inter,system-ui,sans-serif;color:#222;">' +
              '<h1 style="color:#CA6D4C;">&#128268; You are offline</h1>' +
              '<p>This article has not been cached for offline reading yet.</p>' +
              '<p>To read articles offline, open them once while online so the service worker can cache them. Bookmarked articles are cached automatically when you visit them.</p>' +
              '<p><a href="/" style="color:#CA6D4C;font-weight:600;">&larr; Back to SFAAM NEWS home</a></p>' +
              '</body></html>',
              {
                status: 503,
                statusText: 'Service Unavailable',
                headers: { 'Content-Type': 'text/html; charset=utf-8' }
              }
            );
          }
          return caches.match('/static/index.html');
        }))
    );
    return;
  }

  // ── CSS / JS / images / fonts: CACHE-FIRST ──
  // This is the V23 optimization: serve from cache instantly (0ms on repeat
  // visits), then fetch a fresh copy in the background for next time.
  // This dramatically improves Core Web Vitals (LCP, FCP) for repeat visitors.
  // V31 FIX: When both cache miss AND network fail, return a proper 503
  // Response instead of `undefined` (which threw TypeError in browser console).
  e.respondWith(
    caches.match(e.request).then(cached => {
      const fetchPromise = fetch(e.request)
        .then(res => {
          if (res.ok && (res.type === 'basic' || res.type === 'cors')) {
            const clone = res.clone();
            caches.open(RUNTIME_CACHE).then(cache => cache.put(e.request, clone));
          }
          return res;
        })
        .catch(() => null);  // V31: return null instead of `cached` (already handled below)
      // Return cached immediately if available, else wait for network
      return cached || fetchPromise.then(res => {
        if (res) return res;
        // Both cache miss and network fail — return a graceful 503 Response
        return new Response('Offline and resource not cached.', {
          status: 503,
          statusText: 'Service Unavailable',
          headers: { 'Content-Type': 'text/plain; charset=utf-8' }
        });
      });
    })
  );
});

// ─────────────────────────────────────────────
// PUSH NOTIFICATIONS
// ─────────────────────────────────────────────
self.addEventListener('push', e => {
  const data = e.data?.json() || {};
  e.waitUntil(
    self.registration.showNotification(data.title || 'SFAAM NEWS', {
      body: data.body || 'Breaking news update',
      icon: '/static/images/founder-nav.png',
      badge: '/static/images/founder-nav.png',
      data: { url: data.url || '/' },
      tag: data.tag || 'breaking-news',
      requireInteraction: false
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = e.notification.data?.url || '/';
  e.waitUntil(clients.openWindow(url));
});

// ─────────────────────────────────────────────
// MESSAGE — allow page to trigger skipWaiting (for instant SW updates)
// ─────────────────────────────────────────────
self.addEventListener('message', e => {
  if (e.data === 'SKIP_WAITING') self.skipWaiting();
});
