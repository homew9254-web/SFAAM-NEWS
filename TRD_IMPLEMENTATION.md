# SFAAM Automated News Engine — TRD v1.0 Implementation Map

This document maps every requirement from the Technical Requirements
Document (TRD v1.0) to its concrete implementation in this codebase.

Use it as a compliance checklist and onboarding guide for new developers.

---

## TRD Section 1 — Project Overview & Goal

> "Build an autonomous, industrial-grade news intelligence engine that
> runs on a 3-hour micro-cycle (8 cycles/day), identifies the single
> most viral news event per region, gathers multi-source verified
> facts, dynamically calculates article depth, and injects into the
> Admin Dashboard's Draft Mode for manual editorial sign-off. Final
> goal: curate the 5 most powerful articles daily."

| Requirement | Implementation |
|---|---|
| 3-hour micro-cycle | `engine_scheduler.py` — `IntervalTrigger(hours=ENGINE_INTERVAL_HOURS)` with default = 3 |
| 8 cycles/day | Mathematical consequence of 3-hour interval (24 ÷ 3 = 8) |
| Per-region isolation | `region_config.py` — 6 regions, isolated API keys per region |
| Multi-source verified facts | `fact_extractor.py` — research_topic() fetches 3-5 sources, verify_facts_safely() cross-confirms |
| Dynamic article depth | `word_count_calculator.py` — small/medium/large tiers based on fact count |
| Draft Mode injection | `automated_news_engine.py:_save_draft_article()` — saves with `status='draft'` |
| Admin picks top 5 daily | Admin dashboard `/admin.html → News Engine` tab with Publish buttons |

---

## TRD Section 2 — Multi-Region Architecture & Isolated APIs

> "Each region must operate with isolated API keys to ensure high
> availability and prevent cross-region rate-limiting."

### Target Regions (6)
World, USA, UK, Pakistan, India, Germany

**Implementation:** `region_config.py` → `REGIONS` list (frozen dataclass)
with `key`, `display`, `trends_geo`, `newsapi_country`, `gnews_country`,
`search_locale` for each region.

### LLM Routing & Resiliency

> "Primary Engine: Groq API (Llama-3-70b or equivalent)
> Secondary/Fallback Engine: Gemini API (Gemini 1.5 Pro)"

**Implementation:** `resilient_llm.py`

- `GROQ_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]` —
  primary chain (70B first, 8B fallback within Groq)
- `GEMINI_MODELS = ["gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"]` —
  fallback chain (TRD specifies 1.5 Pro; we include newer 2.0 Flash as
  the primary because it's cheaper and equally capable for news synthesis)
- `call_llm_with_fallback()` is the single entry point — tries Groq first,
  instantly switches to Gemini on any failure (4xx/5xx/429/network exception)
- Error classification: `_classify_error()` distinguishes transient vs.
  permanent errors (transient = retry with backoff; permanent = skip
  to next model/provider immediately)

### Environment Variable Naming (TRD-compliant)

> "GROQ_API_KEY_PAKISTAN, GEMINI_API_KEY_PAKISTAN"

**Implementation:** `region_config.py:_resolve_region_key()`

- Resolves BOTH naming conventions:
  - TRD name (priority 1): `GROQ_API_KEY_<REGION>`, `GEMINI_API_KEY_<REGION>`
  - Legacy V26 name (fallback): `GROQ_KEY_<REGION>`, `GEMINI_KEY_<REGION>`
- This means existing V26 deployments continue to work unchanged, and
  new deployments can adopt the TRD naming.

### Failure Logging

> "Failures must be logged immediately with alerts routed to the system administrator."

**Implementation:** Every LLM call returns an `LLMCallResult` with full
audit metadata: `attempts` list (every provider + model tried, with the
error message for each), `total_retries`, `total_elapsed_s`. This is
persisted to the `Article` row (columns: `llm_provider`, `llm_model`,
`fact_extraction_elapsed_s`, `article_generation_elapsed_s`) AND to the
`engine_cycle_logs` table for admin visibility.

For external alerting (Slack/Discord/Teams), set `ALERT_WEBHOOK_URL` in
the environment (already supported by the existing `monitoring.py`).

---

## TRD Section 3 — Cognitive Pipeline Flow (The 3-Hour Loop)

### Step A: Trend Detection & Viral Filtering

> "Polls Google Trends RSS/API, Twitter/X Trending Endpoints, and premium
> News Aggregator APIs (NewsAPI / GNews / Tavily). Cross-references BBC,
> Al Jazeera, Reuters. Ranking based on 'Velocity of Searches' and
> 'Cross-Platform Volume' over the last 180 minutes. Highest-ranking
> unique topic per region is isolated."

**Implementation:** `trend_detector.py`

- **Signal sources:**
  1. `fetch_google_trends()` — Google Trends RSS (free, always available)
  2. `fetch_newsapi_top()` — NewsAPI.org top headlines (requires `NEWSAPI_KEY`)
  3. `fetch_gnews_top()` — GNews.io top news (requires `GNEWS_KEY`)
- **Cross-source aggregation:** `_aggregate_and_rank()` matches the same
  topic across aggregators using Jaccard similarity on normalized
  keyword sets (threshold = 40% overlap). When a topic is seen on
  multiple aggregators, `cross_source_count` and `sources_seen_on` are
  merged.
- **Ranking algorithm:** `_compute_trend_score()` returns 0-100 score:
  - Google Trends traffic (40 pts, log-scaled)
  - Cross-source count (30 pts — 1 source = 10 pts, 2 = 20, 3+ = 30)
  - Domain diversity (15 pts — unique domains among related articles)
  - Recency boost (15 pts — <3h = 15, <6h = 10, <12h = 5)
- **Per-region isolation:** Each region uses its own Google Trends geo
  code (US, GB, PK, IN, DE, or "" for worldwide).
- **Highest-ranking topic selection:** `detect_top_trend()` iterates
  ranked candidates and returns the first one that:
  1. Has score ≥ `ENGINE_MIN_TREND_SCORE` (default 20)
  2. Is NOT in the dedup skip set for that region

### Step B: Cross-Source Fact Extraction & Validation

> "Triggers targeted programmatic search via Tavily / Perplexity.
> Scrapes data fragments from 3-5 distinct trusted publications.
> Strips emotional language/opinion → strict JSON array of core facts.
> CRITICAL SAFETY SHIELD: drops claims from only one unverified source."

**Implementation:** `fact_extractor.py`

- **Source research:** `research_topic()` — tries Tavily first (best
  quality, returns snippets), falls back to DuckDuckGo HTML (free).
  For each found URL, deep-scrapes the article body (up to 8KB per
  source, max 5 sources). Authoritative domain whitelist (40+ domains)
  includes BBC, Reuters, AP, NYT, Al Jazeera, DW, Dawn, The Hindu, etc.
- **3-5 source minimum:** `run_fact_extraction_pipeline()` rejects
  topics with fewer than 3 unique authoritative domains.
- **LLM fact extraction:** `extract_facts()` uses a STRICT system prompt
  that:
  - Forbids hallucination/inference/speculation
  - Returns strict JSON: `{facts: [{text, type, source_url, source_domain}]}`
  - Classifies each fact as `date`, `entity`, `statistic`, `event`, or
    `general` (TRD Section 3 Step B bullet list)
- **Cross-verification safety shield:** `verify_facts_safely()`:
  - For each extracted fact, computes Jaccard similarity against every
    source body's normalized keyword set
  - A fact is "verified" only if it appears (similarity ≥ 0.45) in
    2+ DISTINCT authoritative domains
  - Single-source claims are DROPPED (no exceptions)
  - Returns three lists: `verified_facts`, `dropped_single_source_facts`,
    `dropped_low_similarity_facts` (for admin audit)

### Step C: Algorithmic Word Count & Depth Calculation

> "Small (3-5 facts): 400-600 words. Medium (6-12 facts): 800-1200 words.
> Large (13+ facts): 1500-2500+ words. Deep-dive into implications."

**Implementation:** `word_count_calculator.py`

- Three tiers hardcoded as `WordCountTier` dataclasses:
  - Small: 3-5 facts → 400-600 words, max_tokens=1500
  - Medium: 6-12 facts → 800-1200 words, max_tokens=3000
  - Large: 13+ facts → 1500-2500 words, max_tokens=8000
- `calculate_word_count(fact_count)` returns `WordCountCalculation` with
  tier, target range, midpoint, and recommended max_tokens.
- `get_prompt_instruction(calc)` produces the length-discipline snippet
  injected into the LLM's user prompt.

---

## TRD Section 4 — Dynamic Content Structure (Fields 1-6)

### FIELD 1: Title
> "SEO-optimized, compelling, high CTR, entirely accurate. No clickbait."

**Implementation:** Generated by the LLM in the article JSON. The system
prompt enforces:
- Max 90 characters
- Title Case
- "NO clickbait ('You won't believe...', 'Shocking...')"
- "ENTIRELY accurate to the facts"

### FIELD 2: Short Summary
> "Concise narrative summary. STRICTLY NO bullet points. Single prose block."

**Implementation:** LLM returns `summary` field. System prompt enforces:
- "Single polished prose paragraph (2-3 sentences)"
- "STRICTLY NO bullet points, NO markdown, NO headings"
- "Must flow seamlessly as a single narrative block"

### FIELD 3: Audio Player Placeholder
> "Insert the frontend audio token/hook directly below the Short Summary."

**Implementation:** `_generate_audio_token()` in `automated_news_engine.py`
produces a token like `{{AUDIO_PLAYER:abc123}}`. This token is inserted
into the saved `ai_content` between the summary and the body. The
frontend's `static/js/app.js` should resolve this token to the actual
audio player widget. (The existing audio player asset is NOT altered —
per TRD.)

### FIELD 4: Main Article Body
> "Highly detailed, deeply descriptive analysis matching the calculated
> word count. Use engaging semantic headers (<h3>, <h4>)."

**Implementation:** LLM returns `body` field. System prompt enforces:
- "Use semantic headers: ## for main sections, ### for sub-sections"
- "Divide content into thematic sections based on the verified facts"
- "Vary vocabulary. Avoid repeating the same phrases."
- "Each section must add NEW information, not restate the summary."
- "Use **bold** for key entities on first mention"
- Length scaled dynamically per Step C

### FIELD 5: History & Contextual Background
> "Leverage historical context. Trade dispute → chart last 5-10 years.
> Use verified historical facts — DO NOT invent dates, treaties, events."

**Implementation:**
- New DB column: `articles.history_context` (TEXT)
- LLM returns `history_context` field as part of the JSON response
- System prompt instructs: "For trade disputes, chart the last 5-10
  years. For political events, provide background. For conflicts,
  summarize historical timeline."
- Saved as a separate `## History & Contextual Background` section in
  `ai_content` (appended after the body)

### FIELD 6: Source References
> "Explicitly list source names and clean anchor URLs."

**Implementation:**
- `references_data` column (already existed in V26) stores JSON list
- `automated_news_engine.py:_save_draft_article()` builds references
  from `fact_result.verified_facts[*].source_urls` (deduped) + all
  scraped sources
- Rendered in `ai_content` as a `## Sources` section with numbered
  Markdown links: `1. [domain](url)`

---

## TRD Section 5 — Editorial Engine & Publishing Pipeline

> "Direct-to-live publishing is strictly prohibited. All automated
> outputs must be securely POSTed with status='Draft'."

**Implementation:**

- `automated_news_engine.py:ENGINE_DRAFT_STATUS = "draft"` — hardcoded
  constant, never set to "published"
- `_save_draft_article()` always creates Article rows with
  `status='draft'` — there is no code path that auto-publishes V30
  engine output
- Admin must click **Publish** in the dashboard
  (`POST /api/admin/engine/{id}/publish`) to flip status to "published"
- Dashboard displays drafts partitioned by Region, with calculated word
  count, sources checked, and timestamp (per TRD Section 5 requirement)
- Per-cycle produces up to 6 drafts (one per region) → 8 cycles/day →
  up to 48 drafts/day → admin picks top 5 to publish

---

## TRD Section 6 — Missing Logical Requirements (Stability)

### Deduplication Engine
> "Rolling 7-day log of processed keywords/topics. If a topic was
> already written about, skip to the next highest-ranking topic."

**Implementation:** `dedup_engine.py`

- New DB table: `processed_trend_keywords` with columns:
  `region`, `keyword_norm`, `keyword_raw`, `article_id`, `cycle_id`,
  `processed_at`
- Rolling window: 7 days (`ROLLING_WINDOW_DAYS = 7`)
- Two-tier check:
  1. In-memory cache (fast, per-process, microseconds)
  2. DB-backed log (durable, survives restarts)
- `is_already_processed(region, query)` — fast in-memory check used
  during trend detection
- `record_processed(region, query, article_id, cycle_id)` — called after
  every draft is saved (or every failed attempt) to update both
  in-memory cache and DB
- `cleanup_old_entries()` — nightly cleanup job (2 AM UTC) prunes entries
  older than 7 days

### Graceful Formatting Sanitation
> "Run a regex or parser filter on LLM output to strip conversational
> filler ('Here is the article you requested:') before storing."

**Implementation:** `automated_news_engine.py:_strip_filler()`

- 12 regex patterns matched against common LLM prefaces:
  - "Here is the article..."
  - "Certainly!"
  - "Sure!"
  - "I'll write..."
  - "Below is..."
  - Markdown fence lines (` ```json `, ` ``` `)
- Applied to the raw LLM response before JSON parsing
- Applied AGAIN to each parsed field (title, summary, body,
  history_context) before saving

### Token Windows Management
> "Configure maximum output tokens (e.g., max_tokens: 4000) and instruct
> the prompt to avoid mid-sentence cut-offs."

**Implementation:** `word_count_calculator.py:TIERS[*].max_tokens`

- Small tier: max_tokens = 1500
- Medium tier: max_tokens = 3000
- Large tier: max_tokens = 8000 (above TRD's 4000 floor to support
  the "1500-2500+ word major event" case)
- `get_prompt_instruction()` adds explicit instruction: "Do NOT cut off
  mid-sentence — finish every sentence and section."

### Rate-Limit Exponential Backoff
> "Sliding sleep timer (2s → 4s → 8s) on transient network errors."

**Implementation:** `resilient_llm.py`

- `DEFAULT_BACKOFF_SECONDS = [2, 4, 8]` (TRD-compliant)
- `_try_provider_with_backoff()` applies the backoff schedule per
  (provider, model) pair:
  - Attempt 1: try, fail with transient error → sleep 2s
  - Attempt 2: try, fail with transient error → sleep 4s
  - Attempt 3: try, fail with transient error → sleep 8s
  - Attempt 4: try, fail → mark model as failed, try next model
- Configurable via `LLM_BACKOFF_SECONDS` env var (comma-separated)

---

## New Database Tables & Columns (V30 Migration)

### New Tables
1. `processed_trend_keywords` — 7-day dedup log (TRD Section 6)
2. `engine_cycle_logs` — per-cycle audit trail for admin dashboard

### New Columns on `articles` Table
- `history_context` (TEXT) — TRD Field 5
- `audio_player_token` (VARCHAR 100) — TRD Field 3
- `word_count_tier` (VARCHAR 20) — small/medium/large (TRD Step C)
- `word_count_target` (INTEGER) — midpoint of tier range
- `trend_score` (INTEGER) — 0-100 from Step A ranking
- `cross_source_count` (INTEGER) — how many aggregators mentioned it
- `llm_provider` (VARCHAR 20) — groq | gemini
- `llm_model` (VARCHAR 100) — specific model used
- `raw_facts_count` (INTEGER) — facts extracted by LLM before verification
- `dropped_facts_count` (INTEGER) — facts dropped by safety shield
- `fact_extraction_elapsed_s` (INTEGER) — time spent on Step B
- `article_generation_elapsed_s` (INTEGER) — time spent on Step D

### Migration Safety
- `init_db()` auto-migrates existing Postgres databases via `ALTER TABLE
  ADD COLUMN` for every new column (idempotent — safe to run on
  existing V26 deployments)
- Backfills NULL `pipeline_version` rows on existing trends articles
  so admin filters work consistently
- Backfills NULL `status` rows to 'published' (V29 backfill, retained)

---

## New Admin API Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/admin/engine/status` | GET | Engine + scheduler status, region health, recent cycles |
| `/api/admin/engine/regions` | GET | Per-region LLM key + aggregator key health |
| `/api/admin/engine/run` | POST | Trigger full cycle (all 6 regions) |
| `/api/admin/engine/run?region=pakistan` | POST | Trigger single-region cycle |
| `/api/admin/engine/drafts` | GET | List V30 drafts (with TRD metadata) |
| `/api/admin/engine/drafts?region=usa` | GET | Filtered by region |
| `/api/admin/engine/cycles` | GET | List recent cycle logs |
| `/api/admin/engine/cycles/{cycle_id}` | GET | Full cycle detail with per-region summary |
| `/api/admin/engine/{article_id}/publish` | POST | Publish a draft (admin sign-off) |

---

## Admin Dashboard (UI)

A new **"⚙ News Engine"** tab is added to `/admin.html` with:

- 4 stat cards: Drafts Pending / Published / Total Cycles / Last Cycle
- Pipeline info banner (purple theme) with "Run Full Cycle" and
  "Run Single Region" buttons + region dropdown
- Provider health chips showing which regions have Groq/Gemini keys
  configured + which aggregators (Tavily/NewsAPI/GNews) are active
- Drafts table with 11 columns: ID, Title/Trend, Region, Tier, Words,
  Sources, Facts (raw/verified), Provider, Trend Score, Date, Actions
- Cycle history table: Cycle ID, Started, Duration, Regions, Drafts,
  Failed, Dedup Skips, Status (color-coded)

---

## Backward Compatibility

The V30 engine **coexists** with the legacy V26 Trends pipeline:

- The legacy `trends_scheduler.py` (6-hour cycle, single-region) still
  runs by default. Existing drafts from V26 are unaffected.
- The new `engine_scheduler.py` (3-hour cycle, multi-region) starts
  alongside it. New drafts are tagged with `pipeline_version='v30_trd1.0'`.
- Admin dashboard has separate tabs for each ("Trends Drafts" for V26,
  "News Engine" for V30).
- To disable the legacy pipeline: set `TRENDS_INTERVAL_HOURS=999999` or
  remove the `start_trends_scheduler()` call in `main.py`.

---

## Configuration Reference

All V30 engine env vars (with TRD-compliant defaults):

| Env Var | Default | Purpose |
|---|---|---|
| `ENGINE_INTERVAL_HOURS` | `3` | Cycle interval (TRD: 3 hours) |
| `ENGINE_RUN_ON_STARTUP` | `0` | Run once on app startup |
| `ENGINE_MIN_TREND_SCORE` | `20` | Min trend score to qualify |
| `ENGINE_MAX_SOURCES` | `5` | Max sources per topic (TRD: 3-5) |
| `ENGINE_MIN_SOURCES` | `3` | Min unique domains required |
| `ENGINE_MIN_VERIFIED_FACTS` | `3` | Min verified facts to write article |
| `LLM_BACKOFF_SECONDS` | `2,4,8` | Exponential backoff schedule |
| `GROQ_API_KEY_<REGION>` | — | Per-region Groq key (TRD naming) |
| `GEMINI_API_KEY_<REGION>` | — | Per-region Gemini key (TRD naming) |
| `TAVILY_API_KEY` | — | Tavily search (for fact extraction) |
| `NEWSAPI_KEY` | — | NewsAPI.org (for trend detection) |
| `GNEWS_KEY` | — | GNews.io (for trend detection) |
| `PERPLEXITY_API_KEY` | — | Perplexity (alternative to Tavily) |

---

## File Manifest (V30 New Files)

```
sfaam-news/
├── automated_news_engine.py     ← MAIN ORCHESTRATOR (TRD pipeline)
├── engine_scheduler.py          ← APScheduler wiring (3-hour cron)
├── region_config.py             ← 6-region registry + isolated API keys
├── resilient_llm.py             ← Groq+Gemini fallback + exponential backoff
├── trend_detector.py            ← Step A: multi-source trend detection + ranking
├── fact_extractor.py            ← Step B: research + extract + safety shield
├── word_count_calculator.py     ← Step C: dynamic word count tiers
├── dedup_engine.py              ← 7-day rolling dedup log
├── database.py                  ← (UPDATED) New columns + 2 new tables
├── main.py                      ← (UPDATED) New admin endpoints + scheduler
├── .env.example                 ← (UPDATED) TRD naming + new engine vars
├── static/admin.html            ← (UPDATED) New "News Engine" tab + JS
└── TRD_IMPLEMENTATION.md        ← This file
```

---

## Verification Checklist

Before deploying to production, verify:

- [ ] All 6 regions have at least one LLM key configured (Groq OR Gemini)
      — check `/api/admin/engine/regions` endpoint
- [ ] At least one trend aggregator key is configured (Tavily/NewsAPI/GNews)
      — strongly recommended for Step A quality
- [ ] `DATABASE_URL` points to PostgreSQL (not SQLite) for production
- [ ] `ENV=production` is set
- [ ] `ADMIN_PASSWORD` or `ADMIN_PASSWORD_HASH` is set
- [ ] `ADMIN_KEY` is set (32+ random chars)
- [ ] Database migration runs cleanly on app startup (check logs for
      `[V30 Migration]` or `[V18 Migration] Adding column` messages)
- [ ] Manual cycle trigger works: `POST /api/admin/engine/run?region=world`
      produces a draft within 5 minutes
- [ ] Draft appears in admin dashboard under "⚙ News Engine" tab
- [ ] Clicking Publish on a draft flips its status to "published" and
      it appears on the public site

---

Built per TRD v1.0 specification. — SFAAM Media Group
