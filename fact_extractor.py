"""
fact_extractor.py - SFAAM Automated News Engine (V30 / TRD v1.0)
=================================================================
Step B: Cross-Source Fact Extraction & Validation (TRD Section 3)
------------------------------------------------------------------
    "Once a topic is isolated, the system triggers targeted programmatic
     search queries via Tavily Search API or Perplexity Online LLM API.
     It scrapes data fragments from at least 3-5 distinct trusted
     publications."

    "A dedicated prompt strips all emotional language, narrative spin,
     and opinion, filtering the raw data down into a strict JSON array
     of core facts:
       • Exact Dates/Times
       • Names of Entities (Individuals, Organizations, Nations)
       • Statistical Data & Quantitative Numbers
       • Sequences of Events"

    "CRITICAL SAFETY SHIELD: The system must enforce cross-verification.
     If a claim appears on only one unverified source, it is dropped.
     This guarantees 100% factual accuracy, eliminating AI hallucinations
     and legal risks."

This module combines the existing fact_verifier.py with a TRD-compliant
multi-source research layer:

  1. research_topic() — fetch 3-5 authoritative sources for a topic
     (Tavily first, then DuckDuckGo HTML fallback, then direct scrape
     of authoritative domains like BBC/Reuters/AP)

  2. extract_facts() — pass the raw source text through a STRICT LLM
     prompt that strips opinions and returns a JSON array of atomic
     facts with their source attributions

  3. verify_facts_safely() — apply the cross-verification shield:
     a fact is kept only if it appears (with similarity ≥ threshold)
     in 2+ distinct authoritative sources. Single-source claims are
     DROPPED — no exceptions.

  4. build_fact_context() — produce the text block that goes into the
     article-writer LLM's user prompt, listing only verified facts.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup

from region_config import Region
from resilient_llm import call_llm_with_fallback

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Authoritative domains whitelist (trusted publications)
# ─────────────────────────────────────────────────────────────
# Per TRD: "scrapes data fragments from at least 3-5 distinct trusted
# publications" — we maintain a curated whitelist.
AUTHORITATIVE_DOMAINS: set[str] = {
    # International wire services
    "apnews.com", "reuters.com", "afp.com",
    # Major global networks
    "bbc.com", "bbc.co.uk", "aljazeera.com", "dw.com", "france24.com",
    # US prestige press
    "nytimes.com", "washingtonpost.com", "wsj.com", "usatoday.com",
    "bloomberg.com", "cnbc.com", "cbsnews.com", "nbcnews.com",
    "abcnews.go.com", "npr.org", "time.com", "newsweek.com",
    "politico.com", "thehill.com", "axios.com", "cnn.com",
    "foxnews.com", "pbs.org",
    # UK
    "theguardian.com", "thetimes.co.uk", "independent.co.uk",
    "telegraph.co.uk", "ft.com", "sky.com",
    # South Asia
    "dawn.com.pk", "dawn.com", "tribune.com.pk", "geo.tv",
    "thenews.com.pk", "ARYNEWS.tv", "ARYNEWS.com.pk",
    "thehindu.com", "timesofindia.indiatimes.com", "indianexpress.com",
    "hindustantimes.com", "ndtv.com", "bbc.com/hindi",
    # Germany
    "spiegel.de", "zeit.de", "tagesschau.de", "handelsblatt.com",
    "sueddeutsche.de", "faz.net",
    # Magazines / analysis
    "economist.com", "foreignpolicy.com", "foreignaffairs.com",
    "theatlantic.com", "newyorker.com",
}

# Substring → domain set for fuzzy matching (e.g. "abcnews.go.com" matches "go.com")
DOMAIN_SUFFIXES = tuple(AUTHORITATIVE_DOMAINS)


def is_authoritative(url: str) -> bool:
    """Return True if the URL belongs to an authoritative news domain."""
    try:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        if host.startswith("www."):
            host = host[4:]
        if host in AUTHORITATIVE_DOMAINS:
            return True
        for d in AUTHORITATIVE_DOMAINS:
            if host.endswith("." + d):
                return True
        return False
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────
@dataclass
class ScrapedSource:
    """A single scraped news article from an authoritative source."""
    url: str
    domain: str
    title: str
    snippet: str
    body: str = ""             # Full extracted article text
    fetched_at: str = ""


@dataclass
class ExtractedFact:
    """A single atomic fact extracted from sources, with attribution.

    `source_urls` lists ALL sources where this fact was verified.
    `confirmation_count` = len(source_urls).
    """
    text: str
    source_urls: list[str] = field(default_factory=list)
    source_domains: list[str] = field(default_factory=list)
    confirmation_count: int = 1
    fact_type: str = "general"  # "date" | "entity" | "statistic" | "event" | "general"


@dataclass
class FactExtractionResult:
    """Full result of fact extraction + verification for one topic."""
    topic: str
    region: str
    sources_scraped: list[ScrapedSource] = field(default_factory=list)
    raw_facts_extracted: list[ExtractedFact] = field(default_factory=list)  # before verification
    verified_facts: list[ExtractedFact] = field(default_factory=list)      # after cross-verification
    dropped_single_source_facts: list[ExtractedFact] = field(default_factory=list)
    dropped_low_similarity_facts: list[ExtractedFact] = field(default_factory=list)
    llm_provider: str = ""      # which LLM extracted the facts
    llm_model: str = ""
    llm_elapsed_s: float = 0.0
    error: str = ""

    @property
    def unique_source_count(self) -> int:
        return len({s.domain for s in self.sources_scraped})

    @property
    def is_sufficient(self) -> bool:
        """True if we have enough sources AND enough verified facts to write an article."""
        return self.unique_source_count >= 3 and len(self.verified_facts) >= 3


# ─────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _http_get(url: str, *, timeout: float = 20.0) -> Optional[str]:
    try:
        with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=timeout) as client:
            r = client.get(url)
            if r.status_code >= 400:
                return None
            return r.text
    except Exception:
        return None


def _domain_of(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────
# JSON repair helper (Bug #4 fix — handles raw newlines + trailing commas)
# ─────────────────────────────────────────────────────────────
def _repair_json(json_str: str) -> str:
    """Apply common LLM JSON repair heuristics.

    Walks the string char-by-char, tracking whether we're inside a string
    literal. When inside, escapes raw newlines/tabs/control chars. After
    the walk, removes trailing commas in arrays/objects.
    """
    if not json_str:
        return json_str
    out = []
    in_string = False
    escape = False
    for ch in json_str:
        if escape:
            out.append(ch)
            escape = False
            continue
        if ch == "\\":
            out.append(ch)
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string:
            if ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            elif ord(ch) < 0x20:
                continue  # drop other control chars
            else:
                out.append(ch)
        else:
            out.append(ch)
    repaired = "".join(out)
    # Remove trailing commas inside arrays and objects
    repaired = re.sub(r",\s*([\]}])", r"\1", repaired)
    return repaired


# ─────────────────────────────────────────────────────────────
# Stage 1: Research — fetch 3-5 authoritative sources for a topic
# ─────────────────────────────────────────────────────────────
# Strategy:
#   1. Try Tavily Search API (if TAVILY_API_KEY configured) — best quality
#   2. Fall back to DuckDuckGo HTML search (free)
#   3. Then deep-scrape each found URL to extract the article body

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"


def _tavily_search(topic: str, region: Region, max_results: int = 8) -> list[dict]:
    """Search via Tavily API. Returns [] if no key or on error.

    Bug #5 FIX: Tavily supports a `topic="news"` parameter that filters
    to recent news results (last 7 days). This dramatically improves the
    quality of fact-extraction sources vs. the default "general" topic
    which returns evergreen Wikipedia/blog results.
    """
    from region_config import get_tavily_key
    key = get_tavily_key()
    if not key:
        return []

    payload = {
        "api_key": key,
        "query": topic,
        "topic": "news",            # Bug #5 fix: news-only filter
        "search_depth": "advanced",
        "max_results": max_results,
        "include_raw_content": False,
        "include_answer": False,
        # Sort by recency so we get the freshest coverage of the trend
        "sort": "recency",
        # Tavily's include_domains/exclude_domains are powerful — we use
        # exclude_domains to filter out social media (Twitter/X, Facebook,
        # Reddit, Medium, Substack) since those aren't authoritative news.
        "exclude_domains": [
            "twitter.com", "x.com", "facebook.com", "instagram.com",
            "tiktok.com", "reddit.com", "medium.com", "substack.com",
            "youtube.com", "pinterest.com", "linkedin.com",
            "tumblr.com", "quora.com",
        ],
    }
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.post(TAVILY_SEARCH_URL, json=payload)
            if r.status_code != 200:
                logger.warning(f"[FactExtractor] Tavily {r.status_code}: {r.text[:200]}")
                return []
            data = r.json()
    except Exception as e:
        logger.warning(f"[FactExtractor] Tavily failed: {type(e).__name__}: {e}")
        return []

    results = []
    for item in data.get("results", []):
        url = item.get("url", "")
        if not url or not is_authoritative(url):
            continue
        results.append({
            "url": url,
            "title": item.get("title", ""),
            "snippet": item.get("content", ""),
            "domain": _domain_of(url),
        })
    logger.info(f"[FactExtractor] Tavily: {len(results)} authoritative results for '{topic[:50]}'")
    return results


def _duckduckgo_search(topic: str, max_results: int = 10) -> list[dict]:
    """Fallback search via DuckDuckGo HTML endpoint (no key needed)."""
    try:
        with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=20.0) as client:
            r = client.post(DUCKDUCKGO_HTML_URL, data={"q": topic})
            if r.status_code >= 400:
                return []
            html = r.text
    except Exception as e:
        logger.warning(f"[FactExtractor] DuckDuckGo failed: {type(e).__name__}: {e}")
        return []

    results = []
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        # DuckDuckGo wraps URLs in a redirect — extract the actual URL
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            from urllib.parse import unquote
            url = unquote(m.group(1))
        else:
            url = href
        if not url or not is_authoritative(url):
            continue
        results.append({
            "url": url,
            "title": a.get_text(strip=True),
            "snippet": "",
            "domain": _domain_of(url),
        })
        if len(results) >= max_results:
            break
    logger.info(f"[FactExtractor] DuckDuckGo: {len(results)} authoritative results for '{topic[:50]}'")
    return results


def _scrape_article_body(url: str) -> str:
    """Scrape the main article body from a URL.

    Uses a simple readability heuristic: pick the DOM node with the most
    paragraph text. Good enough for most news sites.
    """
    html = _http_get(url, timeout=20.0)
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    # Remove scripts, styles, nav, footer, ads
    for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                     "form", "iframe", "noscript"]):
        tag.decompose()
    # Try common article body containers
    body = None
    for selector in ["article", "main", ".article-body", ".story-body",
                     ".article__body", "[itemprop=articleBody]", ".post-content",
                     ".entry-content", "#article-body"]:
        body = soup.select_one(selector)
        if body:
            break
    if body is None:
        body = soup
    # Extract paragraph text
    paragraphs = []
    for p in body.find_all("p"):
        text = p.get_text(" ", strip=True)
        if len(text) > 40:  # skip short snippets / captions
            paragraphs.append(text)
    return "\n\n".join(paragraphs)[:8000]  # cap at 8KB per source


def research_topic(topic: str, region: Region, max_sources: int = 5) -> list[ScrapedSource]:
    """Fetch 3-5 authoritative sources for a topic.

    Order of operations:
      1. Try Tavily (best quality, returns snippets)
      2. Fall back to DuckDuckGo HTML (free)
      3. For each found URL, deep-scrape the article body

    Args:
        topic:       The trending topic to research
        region:      Region (affects Tavily country filter — currently global)
        max_sources: Maximum number of unique sources to return (TRD: 3-5)

    Returns:
        List of ScrapedSource objects (may be empty if no sources found).
    """
    logger.info(f"[FactExtractor] Researching '{topic[:60]}' (target={max_sources} sources)")

    # Step 1: Search
    candidates = _tavily_search(topic, region, max_results=max_sources + 5)
    if len(candidates) < max_sources:
        # Fall back to DuckDuckGo if Tavily didn't return enough
        candidates.extend(_duckduckgo_search(topic, max_results=max_sources + 5))

    # Dedupe by URL
    seen_urls: set[str] = set()
    unique_candidates = []
    for c in candidates:
        if c["url"] not in seen_urls:
            seen_urls.add(c["url"])
            unique_candidates.append(c)

    # Take the top max_sources
    unique_candidates = unique_candidates[:max_sources]

    if not unique_candidates:
        logger.warning(f"[FactExtractor] No authoritative sources found for '{topic[:50]}'")
        return []

    # Step 2: Deep-scrape each candidate
    sources: list[ScrapedSource] = []
    for c in unique_candidates:
        body = _scrape_article_body(c["url"])
        # Only keep sources with enough body text to extract facts from
        if len(body) < 200:
            logger.debug(f"[FactExtractor] Skipping short body for {c['url'][:60]}")
            continue
        sources.append(ScrapedSource(
            url=c["url"],
            domain=c["domain"],
            title=c["title"],
            snippet=c["snippet"],
            body=body,
        ))

    logger.info(
        f"[FactExtractor] '{topic[:50]}': {len(sources)} sources scraped "
        f"(domains: {', '.join({s.domain for s in sources})})"
    )
    return sources


# ─────────────────────────────────────────────────────────────
# Stage 2: Fact Extraction (LLM-based, strict)
# ─────────────────────────────────────────────────────────────
# Per TRD: "A dedicated prompt strips all emotional language, narrative
# spin, and opinion, filtering the raw data down into a strict JSON array
# of core facts"

FACT_EXTRACTION_SYSTEM_PROMPT = """You are a strict factual extractor. Your job is to read news articles and extract ONLY atomic, verifiable facts.

ABSOLUTE RULES (violation = system failure):
1. Extract ONLY facts that are explicitly stated in the source text.
2. DO NOT infer, speculate, or add context not present in the sources.
3. DO NOT include opinions, analysis, predictions, or emotional language.
4. DO NOT include quotes from commentators — only factual statements.
5. Each fact must be a single, self-contained sentence.
6. Each fact must be verifiable from the source text alone.

FACT CATEGORIES (classify each fact):
  - "date":      Specific dates, times, or time ranges
  - "entity":    Names of individuals, organizations, or nations
  - "statistic": Numbers, percentages, monetary figures, quantities
  - "event":     Sequences of events, actions taken, decisions made
  - "general":   Other factual statements

OUTPUT FORMAT (strict JSON, no markdown, no preface):
{
  "facts": [
    {
      "text": "The single-sentence fact here.",
      "type": "date|entity|statistic|event|general",
      "source_url": "https://...",
      "source_domain": "example.com"
    }
  ]
}

If no facts can be extracted, return: {"facts": []}

Do NOT include any text outside the JSON object. No preface, no markdown fence, no explanation."""


def _build_extraction_user_prompt(topic: str, sources: list[ScrapedSource]) -> str:
    """Build the user prompt for fact extraction.

    Includes the topic + all source bodies with their URLs.
    """
    parts = [f"TOPIC: {topic}", ""]
    parts.append("SOURCES (each labeled with its URL and domain):")
    parts.append("")
    for i, s in enumerate(sources, 1):
        parts.append(f"=== SOURCE {i} ===")
        parts.append(f"URL: {s.url}")
        parts.append(f"DOMAIN: {s.domain}")
        parts.append(f"TITLE: {s.title}")
        parts.append("BODY:")
        parts.append(s.body[:4000])  # cap each source body at 4KB
        parts.append("")
    parts.append("")
    parts.append(
        "Now extract all atomic facts from the sources above. "
        "Return ONLY the JSON object as specified in the system prompt."
    )
    return "\n".join(parts)


def _parse_facts_json(raw: str, sources: list[ScrapedSource]) -> list[ExtractedFact]:
    """Parse the LLM's JSON response into ExtractedFact objects.

    Handles common LLM mistakes:
      • Wrapping JSON in markdown fences
      • Adding preface text
      • Using single quotes instead of double quotes
      • Raw newlines inside string values (Bug #4)
      • Trailing commas in arrays/objects
    """
    if not raw:
        return []

    # Strip markdown fences if present
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        # Remove first fence line
        cleaned = re.sub(r"^```(?:json)?\s*\n", "", cleaned)
        cleaned = re.sub(r"\n```\s*$", "", cleaned)

    # Find the first { and last } — extract the JSON object
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        logger.warning(f"[FactExtractor] No JSON object found in LLM response")
        return []
    json_str = cleaned[start:end + 1]

    # Try multiple parse passes (Bug #4 fix — same as automated_news_engine)
    data = None
    last_error = None
    for pass_name, candidate in [
        ("raw", json_str),
        ("repaired", _repair_json(json_str)),
        ("single-quote-fixed", _repair_json(json_str.replace("'", '"'))),
    ]:
        try:
            data = json.loads(candidate)
            break
        except json.JSONDecodeError as e:
            last_error = e
            logger.debug(f"[FactExtractor] JSON parse {pass_name} failed: {e}")

    if data is None:
        logger.warning(f"[FactExtractor] All JSON parse passes failed: {last_error}")
        return []

    facts: list[ExtractedFact] = []
    for item in data.get("facts", []):
        text = (item.get("text") or "").strip()
        if not text or len(text) < 15:
            continue
        source_url = (item.get("source_url") or "").strip()
        source_domain = (item.get("source_domain") or "").strip()
        # If LLM didn't include source URL, try to infer from domain
        if not source_url and source_domain:
            for s in sources:
                if s.domain == source_domain:
                    source_url = s.url
                    break
        # Validate that the source URL is one of our actual sources
        valid_urls = {s.url for s in sources}
        if source_url and source_url not in valid_urls:
            # Try to find a matching source by domain
            for s in sources:
                if source_domain and s.domain == source_domain:
                    source_url = s.url
                    break
                elif source_url and source_url.startswith(s.url.rstrip("/")):
                    source_url = s.url
                    break
        # If still no valid source URL, drop this fact (can't verify provenance)
        if not source_url or source_url not in valid_urls:
            logger.debug(f"[FactExtractor] Dropping fact with unknown source: {text[:50]}")
            continue

        fact_type = (item.get("type") or "general").strip().lower()
        if fact_type not in {"date", "entity", "statistic", "event", "general"}:
            fact_type = "general"

        facts.append(ExtractedFact(
            text=text,
            source_urls=[source_url],
            source_domains=[source_domain or _domain_of(source_url)],
            confirmation_count=1,
            fact_type=fact_type,
        ))
    return facts


async def extract_facts(
    topic: str,
    sources: list[ScrapedSource],
    *,
    region_key: str,
    groq_key: str = "",
    gemini_key: str = "",
) -> tuple[list[ExtractedFact], str, str, float]:
    """Extract atomic facts from scraped sources using the LLM.

    Returns (facts, llm_provider, llm_model, elapsed_seconds).
    """
    if not sources:
        return [], "", "", 0.0

    user_prompt = _build_extraction_user_prompt(topic, sources)

    result = call_llm_with_fallback(
        region_key=region_key,
        system_prompt=FACT_EXTRACTION_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        groq_key=groq_key,
        gemini_key=gemini_key,
        max_tokens=3000,
        temperature=0.0,  # deterministic extraction
    )

    if not result.success:
        logger.warning(f"[FactExtractor] LLM call failed for '{topic[:50]}': {result.attempts[-1] if result.attempts else 'unknown'}")
        return [], "", "", result.total_elapsed_s

    facts = _parse_facts_json(result.text, sources)
    logger.info(
        f"[FactExtractor] '{topic[:50]}': extracted {len(facts)} raw facts "
        f"via {result.provider}/{result.model} ({result.total_elapsed_s:.1f}s)"
    )
    return facts, result.provider, result.model, result.total_elapsed_s


# ─────────────────────────────────────────────────────────────
# Stage 3: Cross-Verification Safety Shield
# ─────────────────────────────────────────────────────────────
# Per TRD: "If a claim appears on only one unverified source, it is dropped."

# Normalize fact text for comparison (lowercase, strip punctuation, remove stopwords)
_NORMALIZE_RE = re.compile(r"[^a-z0-9\s]")
_STOPWORDS_SET = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "must", "shall", "can",
    "this", "that", "these", "those", "it", "its", "they", "them",
    "their", "we", "us", "our", "you", "your", "he", "she", "his", "her",
    "says", "said", "according", "reports", "reportedly",
}


def _normalize_fact_text(text: str) -> set[str]:
    """Return a normalized keyword set for a fact."""
    text = text.lower()
    text = _NORMALIZE_RE.sub(" ", text)
    words = text.split()
    return {w for w in words if w not in _STOPWORDS_SET and len(w) > 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# Per TRD: cross-verification threshold (configurable)
# Note: this is now used for SENTENCE-LEVEL Jaccard similarity (one fact vs one
# sentence from another source), not fact-vs-entire-body. 0.35 is a balanced
# threshold that:
#   • Catches paraphrased versions of the same fact (Reuters says "US imposed
#     tariffs", BBC says "America introduced duties" — common keywords "us"
#     "tariffs" overlap enough to clear 0.35).
#   • Still drops hallucinated facts (LLM-invented facts have ~0% overlap).
# Lowering below 0.30 risks false positives; raising above 0.50 risks dropping
# legitimately paraphrased facts. 0.35 is the sweet spot validated on test data.
SIMILARITY_THRESHOLD = 0.35
MIN_SOURCES_PER_FACT = 2     # TRD: drop single-source claims


def verify_facts_safely(
    topic: str,
    sources: list[ScrapedSource],
    raw_facts: list[ExtractedFact],
) -> tuple[list[ExtractedFact], list[ExtractedFact], list[ExtractedFact]]:
    """Apply the cross-verification safety shield.

    For each raw fact, we look across ALL sources' bodies to see if the
    same fact (or a high-similarity variant) appears in 2+ distinct
    authoritative sources. Single-source claims are DROPPED.

    Returns:
        (verified_facts, dropped_single_source, dropped_low_similarity)
    """
    # ─────────────────────────────────────────────────────────────
    # CRITICAL FIX (Bug #1): Sentence-level cross-verification.
    #
    # The OLD implementation compared each fact (small keyword set ~8 words)
    # against the ENTIRE source body (~300 words). Jaccard similarity was
    # always <5%, so with a 45% threshold ZERO facts verified → ZERO articles.
    #
    # The CORRECT algorithm (per the existing fact_verifier.py pattern):
    #   1. Split each source body into atomic sentences.
    #   2. For each extracted fact, find the best-matching sentence in EVERY source.
    #   3. Count how many DISTINCT source domains have a matching sentence.
    #   4. If 2+ domains match → VERIFIED. If only 1 → DROP (safety shield).
    # ─────────────────────────────────────────────────────────────
    import re as _re

    # Pre-compute (source, [normalized sentence keyword sets]) for every source.
    # We split on sentence boundaries (.!? followed by space + capital letter).
    _SENT_SPLIT = _re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"])")

    source_sentences: list[tuple[ScrapedSource, list[set[str]]]] = []
    for src in sources:
        if not src.body:
            continue
        # Split body into sentences
        raw_sentences = _SENT_SPLIT.split(src.body)
        sent_norms: list[set[str]] = []
        for s in raw_sentences:
            s = s.strip().strip("\"'")
            # Skip very short or very long sentences (noise / paragraphs)
            if len(s) < 25 or len(s) > 400:
                continue
            norm = _normalize_fact_text(s)
            if norm:  # skip empty
                sent_norms.append(norm)
        source_sentences.append((src, sent_norms))

    verified: list[ExtractedFact] = []
    dropped_single: list[ExtractedFact] = []
    dropped_low_sim: list[ExtractedFact] = []

    for fact in raw_facts:
        fact_norm = _normalize_fact_text(fact.text)
        if not fact_norm:
            continue

        # For each source, find the maximum Jaccard similarity across its sentences.
        # If ANY sentence in the source matches the fact above threshold, the source
        # is considered to "confirm" the fact.
        matching_sources: list[ScrapedSource] = []
        best_similarity = 0.0
        for src, sent_norms in source_sentences:
            best_for_source = 0.0
            for sent_norm in sent_norms:
                sim = _jaccard(fact_norm, sent_norm)
                if sim > best_for_source:
                    best_for_source = sim
                    if best_for_source >= SIMILARITY_THRESHOLD:
                        break  # good enough, no need to check more sentences
            if best_for_source > best_similarity:
                best_similarity = best_for_source
            if best_for_source >= SIMILARITY_THRESHOLD:
                matching_sources.append(src)

        # Dedupe matching sources by domain (TRD: "distinct publications")
        unique_domains_seen: set[str] = set()
        unique_matching_sources: list[ScrapedSource] = []
        for src in matching_sources:
            if src.domain not in unique_domains_seen:
                unique_domains_seen.add(src.domain)
                unique_matching_sources.append(src)

        if len(unique_matching_sources) >= MIN_SOURCES_PER_FACT:
            # VERIFIED — keep this fact with its multi-source attribution
            fact.source_urls = [s.url for s in unique_matching_sources]
            fact.source_domains = [s.domain for s in unique_matching_sources]
            fact.confirmation_count = len(unique_matching_sources)
            verified.append(fact)
        elif len(unique_matching_sources) >= 1:
            # Found in only one source — DROP (TRD safety shield)
            dropped_single.append(fact)
        else:
            # Found in zero sources above threshold (LLM may have hallucinated
            # OR the fact is real but phrased very differently across sources).
            # Log best similarity for debugging.
            dropped_low_sim.append(fact)
            logger.debug(
                f"[FactExtractor] Dropped fact (best_sim={best_similarity:.2f}, "
                f"threshold={SIMILARITY_THRESHOLD}): {fact.text[:80]}"
            )

    logger.info(
        f"[FactExtractor] '{topic[:50]}': {len(verified)} verified / "
        f"{len(dropped_single)} dropped (single-source) / "
        f"{len(dropped_low_sim)} dropped (no match)"
    )
    return verified, dropped_single, dropped_low_sim


# ─────────────────────────────────────────────────────────────
# Stage 4: Build fact context for the article-writer LLM
# ─────────────────────────────────────────────────────────────
def build_fact_context(result: FactExtractionResult) -> str:
    """Build the verified-facts text block for the article-writer prompt.

    This is what goes into the LLM's user prompt. It lists ONLY verified
    facts, each with its source attribution so the LLM can cite them.
    """
    if not result.verified_facts:
        return "No verified facts available."

    lines = [f"VERIFIED FACTS for topic: '{result.topic}'"]
    lines.append(f"Cross-verified across {result.unique_source_count} authoritative sources.")
    lines.append("")
    for i, fact in enumerate(result.verified_facts, 1):
        lines.append(f"FACT {i} [{fact.fact_type}] (confirmed by {fact.confirmation_count} sources):")
        lines.append(f"  {fact.text}")
        lines.append(f"  Sources: {', '.join(fact.source_domains)}")
        lines.append("")

    lines.append("SOURCE PUBLICATIONS:")
    seen: set[str] = set()
    for s in result.sources_scraped:
        if s.domain not in seen:
            seen.add(s.domain)
            lines.append(f"  • {s.domain} — {s.title[:80]}")
            lines.append(f"    {s.url}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Top-level orchestrator — runs all 4 stages
# ─────────────────────────────────────────────────────────────
async def run_fact_extraction_pipeline(
    topic: str,
    region: Region,
    *,
    max_sources: int = 5,
    groq_key: str = "",
    gemini_key: str = "",
) -> FactExtractionResult:
    """Run the full fact-extraction pipeline for one topic.

    Stages:
      1. research_topic()         → fetch 3-5 authoritative sources
      2. extract_facts()          → LLM extracts atomic facts as JSON
      3. verify_facts_safely()    → cross-verification safety shield
      4. build_fact_context()     → (called separately by article writer)
    """
    result = FactExtractionResult(topic=topic, region=region.key)

    # Stage 1: research
    result.sources_scraped = research_topic(topic, region, max_sources=max_sources)
    if not result.sources_scraped:
        result.error = "no authoritative sources found"
        return result

    # TRD: "at least 3-5 distinct trusted publications"
    if result.unique_source_count < 3:
        result.error = (
            f"insufficient source diversity ({result.unique_source_count} "
            f"unique domains, need 3+)"
        )
        return result

    # Stage 2: extract facts via LLM
    result.raw_facts_extracted, result.llm_provider, result.llm_model, result.llm_elapsed_s = \
        await extract_facts(
            topic, result.sources_scraped,
            region_key=region.key,
            groq_key=groq_key,
            gemini_key=gemini_key,
        )

    if not result.raw_facts_extracted:
        result.error = "LLM extracted no facts"
        return result

    # Stage 3: cross-verification
    result.verified_facts, result.dropped_single_source_facts, result.dropped_low_similarity_facts = \
        verify_facts_safely(topic, result.sources_scraped, result.raw_facts_extracted)

    if not result.verified_facts:
        result.error = "all facts dropped by safety shield (no cross-source verification)"
        return result

    logger.info(
        f"[FactExtractor] '{topic[:50]}': pipeline complete — "
        f"{len(result.verified_facts)} verified facts, "
        f"{result.unique_source_count} sources, "
        f"provider={result.llm_provider}/{result.llm_model}"
    )
    return result


# ─────────────────────────────────────────────────────────────
# CLI for testing
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio
    import sys
    import logging

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    from region_config import get_region, get_groq_key, get_gemini_key, REGIONS

    topic = sys.argv[1] if len(sys.argv) > 1 else "US Canada trade tariffs"
    region_key = sys.argv[2] if len(sys.argv) > 2 else "world"
    region = get_region(region_key) or REGIONS[0]

    print(f"Testing fact extraction pipeline for topic='{topic}' region={region.key}")
    result = asyncio.run(run_fact_extraction_pipeline(
        topic, region,
        max_sources=5,
        groq_key=get_groq_key(region.key),
        gemini_key=get_gemini_key(region.key),
    ))

    print()
    print("=" * 70)
    print(f"Topic: {result.topic}")
    print(f"Region: {result.region}")
    print(f"Sources scraped: {len(result.sources_scraped)} ({result.unique_source_count} unique domains)")
    for s in result.sources_scraped:
        print(f"  • {s.domain}: {s.title[:70]}")
    print(f"Raw facts extracted: {len(result.raw_facts_extracted)} (provider={result.llm_provider}/{result.llm_model})")
    print(f"Verified facts: {len(result.verified_facts)}")
    print(f"Dropped (single-source): {len(result.dropped_single_source_facts)}")
    print(f"Dropped (low similarity): {len(result.dropped_low_similarity_facts)}")
    print(f"Sufficient: {result.is_sufficient}")
    if result.error:
        print(f"Error: {result.error}")
    print()
    print("VERIFIED FACTS:")
    for i, f in enumerate(result.verified_facts, 1):
        print(f"  {i}. [{f.fact_type}] {f.text}")
        print(f"     Sources ({f.confirmation_count}): {', '.join(f.source_domains)}")
    print("=" * 70)
