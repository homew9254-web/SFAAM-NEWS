/* ============================================================
   pro-engagement.js — SFAAM NEWS PRO 1
   ============================================================
   Front-end engagement layer:
   - Command palette (Cmd+K / Ctrl+K)
   - Highlight & save (Medium-style)
   - Reactions (emoji-style)
   - Citation hover cards
   - Reading history tracker (beacon to /api/personalize/track)
   - Push notification subscription
   - Breaking news banner (live updates)
   - Skeleton screens for article loading
   - Reader fingerprint generation (anonymous, in localStorage)

   Loaded on every page via <script src="js/pro-engagement.js">.
   Requires app.js (esc, showToast, fetch helpers).
   ============================================================ */

(function () {
  'use strict';

  // ────────────────────────────────────────────────────────
  // 1. READER FINGERPRINT
  // ────────────────────────────────────────────────────────
  const FP_KEY = 'sfaam_reader_fp';
  function getReaderFP() {
    let fp = localStorage.getItem(FP_KEY);
    if (!fp) {
      // Generate a stable random ID
      fp = 'r-' + Math.random().toString(36).slice(2, 12) + '-' + Date.now().toString(36);
      localStorage.setItem(FP_KEY, fp);
    }
    return fp;
  }
  // Expose globally so app.js can use it for likes/comments
  window.SFAAM_FP = getReaderFP();

  // Attach to every fetch so the server knows who we are
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    init = init || {};
    init.headers = init.headers || {};
    if (init.headers instanceof Headers) {
      if (!init.headers.has('x-reader-fp')) init.headers.set('x-reader-fp', window.SFAAM_FP);
    } else if (typeof init.headers === 'object') {
      if (!init.headers['x-reader-fp']) init.headers['x-reader-fp'] = window.SFAAM_FP;
    }
    // Attach CSRF token for state-changing requests
    const method = (init.method || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') {
      const csrf = document.cookie.match(/sfaam_csrf=([^;]+)/);
      if (csrf) {
        if (init.headers instanceof Headers) {
          if (!init.headers.has('x-csrf-token')) init.headers.set('x-csrf-token', csrf[1]);
        } else {
          if (!init.headers['x-csrf-token']) init.headers['x-csrf-token'] = csrf[1];
        }
      }
    }
    return origFetch.call(this, input, init);
  };

  // ────────────────────────────────────────────────────────
  // 2. READING HISTORY TRACKER
  // ────────────────────────────────────────────────────────
  // Tracks scroll depth + time on page. Sends beacons every 15s
  // and on page unload. Only fires on article pages.
  function initReadingTracker() {
    const articleEl = document.querySelector('.article-body-full') || document.getElementById('articleBody');
    if (!articleEl) return;

    // Pull article ID + region from the page (set by article.html)
    const articleId = window.SFAAM_ARTICLE_ID;
    const region = window.SFAAM_ARTICLE_REGION || '';
    if (!articleId) return;

    let startTime = Date.now();
    let maxScrollPct = 0;
    let lastBeaconTime = Date.now();

    function computeScrollPct() {
      const docHeight = document.documentElement.scrollHeight - window.innerHeight;
      if (docHeight <= 0) return 1;
      const scrollPct = window.scrollY / docHeight;
      return Math.max(0, Math.min(1, scrollPct));
    }

    function sendBeacon(final) {
      const now = Date.now();
      const timeOnPage = Math.floor((now - startTime) / 1000);
      if (!final && timeOnPage < 5) return; // skip if <5s
      const payload = {
        article_id: articleId,
        region: region,
        read_pct: computeScrollPct(),
        time_on_page: timeOnPage,
      };
      // Use sendBeacon for unload (non-blocking)
      if (navigator.sendBeacon) {
        const blob = new Blob([JSON.stringify(payload)], { type: 'application/json' });
        navigator.sendBeacon('/api/personalize/track', blob);
      } else {
        fetch('/api/personalize/track', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          keepalive: true,
        }).catch(function () {});
      }
      lastBeaconTime = now;
      maxScrollPct = Math.max(maxScrollPct, payload.read_pct);
    }

    // Periodic beacon
    setInterval(function () { sendBeacon(false); }, 15000);
    // Unload beacon
    window.addEventListener('pagehide', function () { sendBeacon(true); });
    window.addEventListener('beforeunload', function () { sendBeacon(true); });
    // Track scroll
    let scrollTicking = false;
    window.addEventListener('scroll', function () {
      if (scrollTicking) return;
      scrollTicking = true;
      requestAnimationFrame(function () { scrollTicking = false; });
    }, { passive: true });
  }

  // ────────────────────────────────────────────────────────
  // 3. COMMAND PALETTE (Cmd+K / Ctrl+K)
  // ────────────────────────────────────────────────────────
  function initCommandPalette() {
    if (document.getElementById('proCmdPalette')) return;

    const overlay = document.createElement('div');
    overlay.id = 'proCmdPalette';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);backdrop-filter:blur(4px);z-index:9999;display:none;align-items:flex-start;justify-content:center;padding-top:15vh;';
    overlay.innerHTML = `
      <div role="dialog" aria-modal="true" aria-label="Command palette"
           style="width:min(640px,92vw);background:var(--bg-2,#fff);border:1px solid var(--border,#ddd);border-radius:16px;overflow:hidden;box-shadow:0 24px 64px rgba(0,0,0,0.3);">
        <div style="padding:16px 20px;border-bottom:1px solid var(--border,#ddd);display:flex;gap:12px;align-items:center;">
          <span style="font-size:20px;color:var(--orange,#CA6D4C);">&#128269;</span>
          <input type="text" id="proCmdInput" placeholder="Search articles, topics, authors..."
                 autocomplete="off"
                 style="flex:1;border:none;background:transparent;color:var(--text,#111);font-size:16px;outline:none;font-family:inherit;" />
          <kbd style="font-size:11px;padding:4px 8px;background:var(--bg-3,#eee);border-radius:4px;color:var(--text-dim,#666);">ESC</kbd>
        </div>
        <div id="proCmdResults" style="max-height:60vh;overflow-y:auto;"></div>
        <div style="padding:8px 16px;border-top:1px solid var(--border,#ddd);font-size:11px;color:var(--text-dim,#666);display:flex;gap:16px;">
          <span>&#8593; &#8595; navigate</span>
          <span>&#8629; open</span>
          <span><kbd style="font-size:10px;">/</kbd> focus search</span>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    const input = overlay.querySelector('#proCmdInput');
    const results = overlay.querySelector('#proCmdResults');
    let activeIdx = -1;
    let currentItems = [];

    function open() {
      overlay.style.display = 'flex';
      input.value = '';
      results.innerHTML = '';
      setTimeout(function () { input.focus(); }, 50);
    }
    function close() {
      overlay.style.display = 'none';
      activeIdx = -1;
    }
    function render(items) {
      currentItems = items;
      activeIdx = -1;
      if (!items.length) {
        results.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text-dim,#666);">No results</div>';
        return;
      }
      results.innerHTML = items.map(function (it, i) {
        const icon = it.type === 'topic' ? '&#128279;' :
                     it.type === 'author' ? '&#9997;' :
                     it.type === 'page' ? '&#128196;' : '&#128240;';
        return '<a href="' + it.url + '" data-idx="' + i + '" class="pro-cmd-item" style="display:flex;gap:12px;align-items:center;padding:12px 20px;text-decoration:none;color:var(--text,#111);border-bottom:1px solid var(--border,#ddd);">' +
               '<span style="font-size:18px;">' + icon + '</span>' +
               '<div style="flex:1;min-width:0;">' +
                 '<div style="font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + (it.title || it.name || '') + '</div>' +
                 (it.subtitle ? '<div style="font-size:12px;color:var(--text-dim,#666);">' + it.subtitle + '</div>' : '') +
               '</div>' +
               '<span style="font-size:10px;text-transform:uppercase;color:var(--text-dim,#666);">' + (it.type || '') + '</span>' +
               '</a>';
      }).join('');
      // Add click handlers
      results.querySelectorAll('.pro-cmd-item').forEach(function (el) {
        el.addEventListener('mouseenter', function () {
          activeIdx = parseInt(el.dataset.idx, 10);
          updateActive();
        });
      });
    }
    function updateActive() {
      results.querySelectorAll('.pro-cmd-item').forEach(function (el, i) {
        el.style.background = (i === activeIdx) ? 'var(--bg-3,#eee)' : '';
      });
    }

    let debounceTimer;
    input.addEventListener('input', function () {
      clearTimeout(debounceTimer);
      const q = input.value.trim();
      if (!q) {
        render([]);
        return;
      }
      debounceTimer = setTimeout(function () {
        // Fetch suggestions
        fetch('/api/search/suggest?q=' + encodeURIComponent(q) + '&limit=8')
          .then(function (r) { return r.json(); })
          .then(function (data) {
            const items = (data.suggestions || []).map(function (s) {
              return {
                type: 'article', title: s.title, subtitle: s.region,
                url: s.url,
              };
            });
            // Add quick-link pages
            const pages = [
              { type: 'page', title: 'Home', url: '/' },
              { type: 'page', title: 'Trending', url: '/trends.html' },
              { type: 'page', title: 'Bookmarks', url: '/bookmarks.html' },
              { type: 'page', title: 'Search', url: '/search.html?q=' + encodeURIComponent(q) },
            ].filter(function (p) {
              return p.title.toLowerCase().includes(q.toLowerCase()) || p.type === 'page' && q.length < 3;
            }).slice(0, 2);
            render(items.concat(pages));
          })
          .catch(function () { render([]); });
      }, 180);
    });

    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        activeIdx = Math.min(currentItems.length - 1, activeIdx + 1);
        updateActive();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        activeIdx = Math.max(-1, activeIdx - 1);
        updateActive();
      } else if (e.key === 'Enter') {
        if (activeIdx >= 0 && currentItems[activeIdx]) {
          window.location.href = currentItems[activeIdx].url;
        } else if (currentItems[0]) {
          window.location.href = currentItems[0].url;
        }
      } else if (e.key === 'Escape') {
        close();
      }
    });

    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) close();
    });

    // Global keyboard shortcuts
    document.addEventListener('keydown', function (e) {
      // Cmd+K / Ctrl+K — open palette
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        if (overlay.style.display === 'flex') close(); else open();
      }
      // "/" — focus search (when not already typing in an input)
      if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
        e.preventDefault();
        open();
      }
      // ESC — close
      if (e.key === 'Escape' && overlay.style.display === 'flex') {
        close();
      }
    });
  }

  // ────────────────────────────────────────────────────────
  // 4. HIGHLIGHT & SAVE (Medium-style)
  // ────────────────────────────────────────────────────────
  function initHighlighter() {
    const article = document.querySelector('.article-body-full') || document.getElementById('articleBody');
    if (!article) return;

    let selTimer;
    article.addEventListener('mouseup', function () {
      clearTimeout(selTimer);
      selTimer = setTimeout(showHighlightPopup, 10);
    });
    article.addEventListener('touchend', function () {
      clearTimeout(selTimer);
      selTimer = setTimeout(showHighlightPopup, 10);
    });

    function showHighlightPopup() {
      // Remove any existing popup
      const existing = document.getElementById('proHighlightPopup');
      if (existing) existing.remove();

      const sel = window.getSelection();
      const text = sel.toString().trim();
      if (!text || text.length < 5 || text.length > 1000) return;
      // Only highlight inside article body
      const range = sel.getRangeAt(0);
      if (!article.contains(range.commonAncestorContainer)) return;

      const rect = range.getBoundingClientRect();
      const popup = document.createElement('div');
      popup.id = 'proHighlightPopup';
      popup.style.cssText = 'position:fixed;left:' + (rect.left + rect.width / 2 - 100) + 'px;top:' + (rect.top - 50) + 'px;background:var(--bg-2,#fff);border:1px solid var(--border,#ddd);border-radius:24px;padding:4px;display:flex;gap:2px;box-shadow:0 8px 24px rgba(0,0,0,0.18);z-index:9998;';
      popup.innerHTML = `
        <button data-color="yellow" title="Highlight" style="width:32px;height:32px;border-radius:50%;border:none;background:#FFEB3B;cursor:pointer;"></button>
        <button data-color="green" title="Important" style="width:32px;height:32px;border-radius:50%;border:none;background:#81C784;cursor:pointer;"></button>
        <button data-color="blue" title="Insight" style="width:32px;height:32px;border-radius:50%;border:none;background:#64B5F6;cursor:pointer;"></button>
        <button data-color="pink" title="Disagree" style="width:32px;height:32px;border-radius:50%;border:none;background:#F06292;cursor:pointer;"></button>
        <button data-action="copy" title="Copy" style="width:32px;height:32px;border-radius:50%;border:none;background:var(--bg-3,#eee);cursor:pointer;font-size:14px;">&#128203;</button>
        <button data-action="share" title="Share" style="width:32px;height:32px;border-radius:50%;border:none;background:var(--bg-3,#eee);cursor:pointer;font-size:14px;">&#128279;</button>
      `;
      document.body.appendChild(popup);

      popup.addEventListener('click', function (e) {
        const btn = e.target.closest('button');
        if (!btn) return;
        const color = btn.dataset.color;
        const action = btn.dataset.action;
        if (color) {
          saveHighlight(text, color, range);
        } else if (action === 'copy') {
          navigator.clipboard.writeText(text);
          showToast('Copied to clipboard');
        } else if (action === 'share') {
          const shareUrl = window.location.href;
          const shareText = '"' + text + '" — ' + shareUrl;
          if (navigator.share) {
            navigator.share({ text: shareText, url: shareUrl }).catch(function () {});
          } else {
            navigator.clipboard.writeText(shareText);
            showToast('Quote + link copied');
          }
        }
        popup.remove();
        window.getSelection().removeAllRanges();
      });

      // Auto-remove on outside click
      setTimeout(function () {
        document.addEventListener('mousedown', function remove(ev) {
          if (!popup.contains(ev.target)) {
            popup.remove();
            document.removeEventListener('mousedown', remove);
          }
        });
      }, 100);
    }

    function saveHighlight(text, color, range) {
      const articleId = window.SFAAM_ARTICLE_ID;
      if (!articleId) return;
      // Apply visual highlight
      try {
        const span = document.createElement('mark');
        span.className = 'pro-highlight pro-highlight-' + color;
        span.dataset.text = text;
        range.surroundContents(span);
      } catch (e) {
        // surroundContents fails on multi-paragraph selections; skip visual
      }
      // Save to server
      fetch('/api/highlight', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ article_id: articleId, text: text, color: color }),
      }).then(function (r) { return r.json(); }).then(function (d) {
        showToast('Highlighted');
      }).catch(function () { showToast('Highlight failed'); });
    }
  }

  // ────────────────────────────────────────────────────────
  // 5. REACTIONS (emoji-style)
  // ────────────────────────────────────────────────────────
  function initReactions() {
    if (!window.SFAAM_ARTICLE_ID) return;
    const container = document.querySelector('.v32-newsletter-cta');
    if (!container) {
      // Insert before newsletter CTA
      const articleBody = document.getElementById('articleBody');
      if (!articleBody) return;
      const div = document.createElement('div');
      div.id = 'proReactions';
      div.style.cssText = 'margin:24px 0;padding:20px;background:var(--bg-2,#f5f3ef);border-radius:12px;text-align:center;';
      articleBody.parentNode.insertBefore(div, articleBody.nextSibling);
    }
    const target = document.getElementById('proReactions');
    if (!target) return;
    const reactions = [
      { key: 'like', emoji: '&#128077;', label: 'Like' },
      { key: 'love', emoji: '&#10084;', label: 'Love' },
      { key: 'insightful', emoji: '&#128161;', label: 'Insightful' },
      { key: 'celebrate', emoji: '&#127881;', label: 'Celebrate' },
      { key: 'disagree', emoji: '&#128078;', label: 'Disagree' },
    ];
    target.innerHTML = '<p style="margin:0 0 12px;font-size:13px;color:var(--text-muted,#666);">What did you think of this story?</p>' +
      '<div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap;">' +
      reactions.map(function (r) {
        return '<button data-reaction="' + r.key + '" title="' + r.label + '" style="padding:10px 16px;border:1px solid var(--border,#ddd);background:var(--bg,#fff);color:var(--text,#111);border-radius:24px;cursor:pointer;font-size:18px;font-family:inherit;display:flex;align-items:center;gap:6px;">' +
               '<span>' + r.emoji + '</span>' +
               '<span class="pro-rx-count" data-key="' + r.key + '" style="font-size:13px;font-weight:600;">0</span>' +
               '</button>';
      }).join('') +
      '</div>';

    // Load counts
    fetch('/api/reaction/' + window.SFAAM_ARTICLE_ID).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.reactions) return;
      Object.keys(d.reactions).forEach(function (k) {
        const el = target.querySelector('.pro-rx-count[data-key="' + k + '"]');
        if (el) el.textContent = d.reactions[k];
      });
    }).catch(function () {});

    // Load user's reaction
    fetch('/api/reaction/' + window.SFAAM_ARTICLE_ID + '/me').then(function (r) { return r.json(); }).then(function (d) {
      if (d.reaction) {
        const btn = target.querySelector('button[data-reaction="' + d.reaction + '"]');
        if (btn) {
          btn.style.background = 'var(--orange,#CA6D4C)';
          btn.style.color = '#fff';
          btn.style.borderColor = 'var(--orange,#CA6D4C)';
        }
      }
    }).catch(function () {});

    // Click handler
    target.addEventListener('click', function (e) {
      const btn = e.target.closest('button[data-reaction]');
      if (!btn) return;
      const key = btn.dataset.reaction;
      fetch('/api/reaction', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ article_id: window.SFAAM_ARTICLE_ID, reaction: key }),
      }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.ok) {
          // Toggle visual state
          target.querySelectorAll('button[data-reaction]').forEach(function (b) {
            b.style.background = 'var(--bg,#fff)';
            b.style.color = 'var(--text,#111)';
            b.style.borderColor = 'var(--border,#ddd)';
          });
          btn.style.background = 'var(--orange,#CA6D4C)';
          btn.style.color = '#fff';
          btn.style.borderColor = 'var(--orange,#CA6D4C)';
          // Refresh counts
          fetch('/api/reaction/' + window.SFAAM_ARTICLE_ID).then(function (r) { return r.json(); }).then(function (dd) {
            if (!dd.reactions) return;
            Object.keys(dd.reactions).forEach(function (k) {
              const el = target.querySelector('.pro-rx-count[data-key="' + k + '"]');
              if (el) el.textContent = dd.reactions[k];
            });
          });
        }
      }).catch(function () { showToast('Reaction failed'); });
    });
  }

  // ────────────────────────────────────────────────────────
  // 6. CITATION HOVER CARDS
  // ────────────────────────────────────────────────────────
  function initCitations() {
    if (!window.SFAAM_ARTICLE_ID) return;
    // Fetch citations and decorate [1][2] markers in the article body
    fetch('/api/citations/' + window.SFAAM_ARTICLE_ID).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.citations || !d.citations.length) return;
      const body = document.getElementById('articleBody');
      if (!body) return;
      // Walk text nodes, replace [n] with superscript link
      const citationMap = {};
      d.citations.forEach(function (c) { citationMap[c.position] = c; });
      const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, null);
      const matches = [];
      while (walker.nextNode()) {
        const node = walker.currentNode;
        const m = node.nodeValue.match(/\[(\d+)\]/g);
        if (m) matches.push({ node: node, matches: m });
      }
      matches.forEach(function (item) {
        const original = item.node.nodeValue;
        const parts = original.split(/(\[\d+\])/);
        const frag = document.createDocumentFragment();
        parts.forEach(function (part) {
          const m = part.match(/^\[(\d+)\]$/);
          if (m) {
            const n = parseInt(m[1], 10);
            const cit = citationMap[n];
            if (cit) {
              const sup = document.createElement('sup');
              const a = document.createElement('a');
              a.href = cit.source_url;
              a.target = '_blank';
              a.rel = 'noopener noreferrer nofollow';
              a.textContent = '[' + n + ']';
              a.className = 'pro-citation';
              a.dataset.title = cit.source_title || cit.source_domain || 'Source';
              a.dataset.url = cit.source_url;
              a.dataset.quote = cit.quoted_text || '';
              a.dataset.domain = cit.source_domain || '';
              a.style.cssText = 'color:var(--orange,#CA6D4C);font-weight:700;text-decoration:none;cursor:pointer;';
              sup.appendChild(a);
              frag.appendChild(sup);
              return;
            }
          }
          if (part) frag.appendChild(document.createTextNode(part));
        });
        item.node.parentNode.replaceChild(frag, item.node);
      });

      // Hover cards
      let hoverCard;
      document.addEventListener('mouseover', function (e) {
        const link = e.target.closest('a.pro-citation');
        if (!link) return;
        if (hoverCard) hoverCard.remove();
        hoverCard = document.createElement('div');
        hoverCard.className = 'pro-citation-card';
        const quote = link.dataset.quote;
        const title = link.dataset.title;
        const domain = link.dataset.domain;
        hoverCard.style.cssText = 'position:fixed;background:var(--bg-2,#fff);border:1px solid var(--border,#ddd);border-radius:8px;padding:12px 16px;max-width:340px;box-shadow:0 8px 24px rgba(0,0,0,0.18);z-index:9998;font-size:13px;color:var(--text,#111);pointer-events:none;';
        hoverCard.innerHTML = (domain ? '<div style="font-weight:700;color:var(--orange,#CA6D4C);font-size:11px;text-transform:uppercase;margin-bottom:4px;">' + domain + '</div>' : '') +
          (title ? '<div style="font-weight:600;margin-bottom:6px;">' + title + '</div>' : '') +
          (quote ? '<div style="color:var(--text-muted,#666);font-style:italic;line-height:1.4;">&ldquo;' + quote.substring(0, 200) + '&rdquo;</div>' : '');
        const rect = link.getBoundingClientRect();
        hoverCard.style.left = Math.min(rect.left, window.innerWidth - 360) + 'px';
        hoverCard.style.top = (rect.bottom + 6) + 'px';
        document.body.appendChild(hoverCard);
      });
      document.addEventListener('mouseout', function (e) {
        if (e.target.closest('a.pro-citation') && hoverCard) {
          hoverCard.remove();
          hoverCard = null;
        }
      });
    }).catch(function () {});
  }

  // ────────────────────────────────────────────────────────
  // 7. PUSH NOTIFICATION SUBSCRIPTION
  // ────────────────────────────────────────────────────────
  function initPushPrompt() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
    if (Notification.permission === 'granted' || Notification.permission === 'denied') return;
    // Show a soft prompt after 30s on the site (one-time)
    if (sessionStorage.getItem('pro_push_prompted')) return;
    sessionStorage.setItem('pro_push_prompted', '1');
    setTimeout(function () {
      const banner = document.createElement('div');
      banner.style.cssText = 'position:fixed;bottom:16px;left:16px;right:16px;max-width:480px;margin:0 auto;background:var(--bg-2,#fff);border:2px solid var(--orange,#CA6D4C);border-radius:12px;padding:16px;box-shadow:0 8px 32px rgba(0,0,0,0.2);z-index:9997;';
      banner.innerHTML = '<div style="display:flex;gap:12px;align-items:center;">' +
        '<div style="font-size:32px;">&#128276;</div>' +
        '<div style="flex:1;">' +
        '<div style="font-weight:700;margin-bottom:2px;">Get breaking news alerts</div>' +
        '<div style="font-size:12px;color:var(--text-muted,#666);">Be the first to know when big stories break.</div>' +
        '</div>' +
        '<button id="proPushYes" style="background:var(--orange,#CA6D4C);color:#fff;border:none;border-radius:8px;padding:8px 14px;font-weight:600;cursor:pointer;font-family:inherit;">Allow</button>' +
        '<button id="proPushNo" style="background:transparent;border:none;color:var(--text-dim,#666);cursor:pointer;font-size:18px;padding:4px 8px;">&times;</button>' +
        '</div>';
      document.body.appendChild(banner);
      banner.querySelector('#proPushYes').addEventListener('click', function () {
        subscribeToPush();
        banner.remove();
      });
      banner.querySelector('#proPushNo').addEventListener('click', function () { banner.remove(); });
    }, 30000);
  }

  function subscribeToPush() {
    navigator.serviceWorker.ready.then(function (reg) {
      // Get VAPID public key
      fetch('/api/push/vapid-public').then(function (r) { return r.json(); }).then(function (d) {
        if (!d.enabled) {
          showToast('Push notifications not configured.');
          return;
        }
        const key = urlBase64ToUint8Array(d.public_key);
        return reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: key,
        });
      }).then(function (sub) {
        if (!sub) return;
        return fetch('/api/push/subscribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            endpoint: sub.endpoint,
            keys: sub.getKey('p256dh') ? {
              p256dh: btoa(String.fromCharCode.apply(null, new Uint8Array(sub.getKey('p256dh')))),
              auth: btoa(String.fromCharCode.apply(null, new Uint8Array(sub.getKey('auth')))),
            } : {},
            fingerprint: window.SFAAM_FP,
            region: window.SFAAM_ARTICLE_REGION || '',
          }),
        });
      }).then(function (r) {
        showToast('Subscribed to push notifications');
      }).catch(function (e) {
        console.warn('[ProPush] subscribe failed:', e);
        showToast('Push subscription failed');
      });
    });
  }

  function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(base64);
    const arr = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
    return arr;
  }

  // ────────────────────────────────────────────────────────
  // 8. BREAKING NEWS BANNER (live updates via polling)
  // ────────────────────────────────────────────────────────
  function initBreakingNewsBanner() {
    if (document.getElementById('proBreakingBanner')) return;
    const banner = document.createElement('div');
    banner.id = 'proBreakingBanner';
    banner.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#d32f2f;color:#fff;padding:10px 16px;font-size:14px;font-weight:600;z-index:9990;display:none;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,0.2);';
    banner.innerHTML = '<span style="display:inline-block;background:#fff;color:#d32f2f;padding:2px 8px;border-radius:4px;font-weight:700;margin-right:8px;">BREAKING</span>' +
      '<span id="proBreakingText"></span>' +
      '<a id="proBreakingLink" href="#" style="color:#fff;text-decoration:underline;margin-left:8px;">Read &rarr;</a>' +
      '<button id="proBreakingClose" style="background:transparent;border:none;color:#fff;float:right;cursor:pointer;font-size:18px;">&times;</button>';
    document.body.appendChild(banner);
    banner.querySelector('#proBreakingClose').addEventListener('click', function () {
      banner.style.display = 'none';
      sessionStorage.setItem('pro_breaking_dismissed', banner.dataset.id || '1');
    });

    // Poll every 60s for breaking news
    function checkBreaking() {
      fetch('/api/articles?limit=1&breaking=1').then(function (r) { return r.json(); }).then(function (d) {
        const articles = d.articles || d.results || d;
        if (!Array.isArray(articles) || !articles.length) return;
        const article = articles[0];
        if (!article) return;
        const dismissedId = sessionStorage.getItem('pro_breaking_dismissed');
        if (String(article.id) === dismissedId) return;
        // Only show if published within last 30 min
        const age = (Date.now() - new Date(article.date).getTime()) / 60000;
        if (age > 30) return;
        banner.querySelector('#proBreakingText').textContent = article.title;
        banner.querySelector('#proBreakingLink').href = '/article/' + (article.slug || article.id);
        banner.dataset.id = article.id;
        banner.style.display = 'block';
        // Auto-hide after 30s if not interacted
        setTimeout(function () {
          if (banner.style.display === 'block' && banner.dataset.id === String(article.id)) {
            banner.style.display = 'none';
          }
        }, 30000);
      }).catch(function () {});
    }
    checkBreaking();
    setInterval(checkBreaking, 60000);
  }

  // ────────────────────────────────────────────────────────
  // 9. SKELETON SCREENS — replace spinners on dynamic content
  // ────────────────────────────────────────────────────────
  window.SFAAM_skeleton = function (n) {
    n = n || 3;
    let html = '';
    for (let i = 0; i < n; i++) {
      html += '<div class="pro-skeleton-card">' +
        '<div class="pro-skeleton-img"></div>' +
        '<div class="pro-skeleton-line" style="width:80%;height:18px;"></div>' +
        '<div class="pro-skeleton-line" style="width:60%;height:14px;margin-top:8px;"></div>' +
        '</div>';
    }
    return html;
  };

  // ────────────────────────────────────────────────────────
  // INIT — runs on DOMContentLoaded
  // ────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    initReadingTracker();
    initCommandPalette();
    initHighlighter();
    initReactions();
    initCitations();
    initPushPrompt();
    initBreakingNewsBanner();
    console.log('%cSFAAM NEWS PRO 1', 'color:#CA6D4C;font-weight:700;font-size:14px;', 'engagement layer loaded');
  });
})();
