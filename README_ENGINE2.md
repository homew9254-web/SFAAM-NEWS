# Engine V2 — Clean Rebuild (SFAAM Automated News Engine)

Naya article-generation pipeline jo `New_Text_Document.txt` ki spec follow karta hai.
Existing website (static HTML frontend, admin panel, database) **bilkul waisi hi
rehti hai** — sirf naya backend workflow add hua hai jo usi database mein drafts
save karta hai.

## Naye files (7 files, ~1,600 lines)

| File | Step | Kaam |
|---|---|---|
| `engine2_trends.py` | 1 | Google Trends RSS (per region) + BBC/Reuters/Al Jazeera se cross-verify |
| `engine2_search.py` | 2 | NewsAPI + GNews (primary), DuckDuckGo scraping (fallback) — top 5 articles |
| `engine2_scraper.py` | 3 | Full article text + images scrape (httpx + cloudscraper fallback) |
| `engine2_synth.py` | 4 | AI synthesis — Groq (primary) + Gemini (fallback) → title, summary, overview, background |
| `engine2_images.py` | 5 | 4-6 images ko content ke beech distribute karta hai (hero + overview + background) |
| `engine2_orchestrator.py` | 1-6 | Ek region ke liye poora workflow chalata hai, draft save karta hai |
| `engine2_scheduler.py` | — | Har 3 ghante baad 6 regions parallel mein chalata hai (APScheduler) |

**Reused (nahi likha dobara, existing files already TRD-compliant thay):**
`region_config.py` (6 regions + per-region API keys), `resilient_llm.py` (Groq→Gemini
fallback + backoff), `dedup_engine.py`, `title_uniqueness.py`, `database.py`.

## Kaise kaam karta hai

Draft usi `Article` table mein `status='draft'`, `pipeline_version='engine_v2'` ke
saath save hota hai — admin panel (`/admin`) mein "Drafts" tab mein wesay hi dikhega
jaise pehle dikhta tha. Article content **Markdown** format mein hai (frontend ka
existing `mdToHtml()` converter ise render karta hai) — isliye **frontend mein koi
change nahi karna pada**.

```
[Overview section — ### subheadings, 2-3 images beech mein]

## Background & History
[5-10 year context — 1-2 images beech mein]

## Sources
- [Article Title](url) — Source Name
```

Hero image `image_url` field mein separately save hoti hai (jaise pehle hota tha).

## Purana engine (V30) ka kya hua?

Delete nahi kiya — bas `main.py` mein `ENGINE_V30_ENABLED=0` (default) se disable
kar diya hai taake dono engines ek sath drafts na banayein. Agar wapas chalana ho
to `.env` mein `ENGINE_V30_ENABLED=1` set kar dein (dono sath chal sakte hain).

## Environment variables (naye)

```
ENGINE2_ENABLED=1                    # Engine V2 on/off
ENGINE_V30_ENABLED=0                 # legacy engine off by default
ENGINE2_INTERVAL_HOURS=3             # spec: har 3 ghante
ENGINE2_RUN_ON_STARTUP=0             # testing ke liye 1 kar dein
ENGINE2_MAX_CONCURRENT_REGIONS=2     # Railway Hobby/Free (512MB-1GB) ke liye safe
```

**Note:** Spec mein 6 regions "parallel" chalane ka likha hai, lekin Railway
Hobby/Free plan (512MB-1GB RAM) pe 6-way full parallel se instance crash/restart
ho sakta hai (har region ek sath scraping + LLM call karta hai). Isliye default
`ENGINE2_MAX_CONCURRENT_REGIONS=2` rakha hai — 2 regions ek sath, baaki queue
mein wait karte hain. Agar bade Railway plan (2GB+) pe upgrade karo to yeh
value 4-6 tak badha sakte ho.

API keys — **abhi placeholder hain, aapko khud daalne hain** (`.env` file mein):

```
GROQ_API_KEY_WORLD / USA / UK / PAKISTAN / INDIA / GERMANY
GEMINI_API_KEY_WORLD / USA / UK / PAKISTAN / INDIA / GERMANY
NEWSAPI_KEY
GNEWS_KEY
```

- Groq (free): https://console.groq.com/keys
- Gemini (free): https://aistudio.google.com/apikey
- NewsAPI (free, 100 req/day): https://newsapi.org
- GNews (free, 100 req/day): https://gnews.io

Keys ke bina engine gracefully skip ho jayega us region ke liye (crash nahi hoga) —
lekin AI synthesis ke liye kam az kam ek Groq ya Gemini key **zaroori** hai.
NewsAPI/GNews ke bina bhi chalega (DuckDuckGo fallback), lekin results kam reliable
honge.

## Admin API (naye endpoints)

- `GET /api/admin/engine2/status` — status, pending/published draft counts, key health
- `POST /api/admin/engine2/run` — turant ek pura cycle (6 regions) manually chalao
- `GET /api/admin/engine2/drafts` — Engine V2 ke drafts list karo

## Important limitation (30,000-word target)

Spec mein "max 30,000 words" likha hai — yeh free-tier LLM APIs (Groq/Gemini free
tier) ke output-token limits se practically possible nahi (ek single call mein
~5,000-6,000 words tak hi ja sakta hai). Is build mein overview + background do
alag AI calls hain, jo har call ka max output use karte hain (~5-6K words total
realistic max). Agar zyada length chahiye to paid/higher-tier API plan chahiye
hoga — `engine2_synth.py` mein `MAX_TOKENS_OVERVIEW` / `MAX_TOKENS_BACKGROUND`
values badha sakte hain.

## Deploy se pehle

1. `.env` mein API keys daalein (upar wali list)
2. `ENGINE2_RUN_ON_STARTUP=1` set karke ek baar local/staging pe test karein
3. Admin dashboard mein draft article check karein (formatting, images, sources)
4. Satisfied ho to `ENGINE2_RUN_ON_STARTUP=0` wapas kar dein production ke liye
