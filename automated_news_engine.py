"""
automated_news_engine.py - SFAAM Automated News Engine (V30 / TRD v1.0)
========================================================================
THE MAIN ORCHESTRATOR — implements the full TRD v1.0 specification.

TRD Section 3: "COGNITIVE PIPELINE FLOW (THE 3-HOUR LOOP)"
----------------------------------------------------------
    "A cron service executes the following sequence every 3 hours:

     Step A: Trend Detection & Viral Filtering
       → polls Google Trends RSS, Twitter/X, News Aggregator APIs
       → cross-references BBC, Al Jazeera, Reuters
       → applies ranking based on 'Velocity of Searches' and
         'Cross-Platform Volume' over the last 180 minutes
       → isolates the highest-ranking unique topic per region

     Step B: Cross-Source Fact Extraction & Validation
       → triggers targeted searches via Tavily / Perplexity
       → scrapes data fragments from 3-5 distinct trusted publications
       → strips emotional language/opinion → strict JSON of core facts
       → SAFETY SHIELD: drops any claim from only one unverified source

     Step C: Algorithmic Word Count & Depth Calculation
       → Small Fact Pool  (3-5 facts)   → 400-600 words
       → Medium Fact Pool (6-12 facts)  → 800-1200 words
       → Large Fact Pool  (13+ facts)   → 1500-2500+ words

     Step D: Dynamic Content Generation (TRD Section 4 — Fields 1-6)
       → FIELD 1: Title (SEO-optimized, no clickbait)
       → FIELD 2: Short Summary (prose, no bullets)
       → FIELD 3: Audio Player Placeholder token
       → FIELD 4: Main Article Body (rich text with h3/h4 headers)
       → FIELD 5: History & Contextual Background (5-10 year deep dive)
       → FIELD 6: Source References (clean anchor URL list)

     Step E: Save as DRAFT (TRD Section 5)
       → "Direct-to-live publishing is strictly prohibited"
       → POST to backend with status='draft'
       → Admin dashboard shows partitioned by Region, word count,
         sources checked, and timestamp"

TRD Section 6: "MISSING LOGICAL REQUIREMENTS"
---------------------------------------------
    Implemented here and in helper modules:
      • Deduplication Engine (dedup_engine.py) — 7-day rolling log
      • Graceful Formatting Sanitation — strip LLM conversational filler
      • Token Windows Management — max_tokens scales with tier
      • Rate-Limit Exponential Backoff (resilient_llm.py) — 2s/4s/8s

PER-REGION ISOLATION (TRD Section 2)
------------------------------------
    Every region (World, USA, UK, Pakistan, India, Germany) runs the
    pipeline with its OWN Groq + Gemini keys. This is non-negotiable —
    it prevents cross-region rate-limiting and token exhaustion.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

from sqlalchemy import select

from database import AsyncSessionLocal, Article, EngineCycleLog
from dedup_engine import (
    get_skip_sets_all_regions,
    record_processed,
    refresh_cache_from_db,
    cleanup_old_entries as cleanup_dedup_entries,
)
from fact_extractor import (
    FactExtractionResult,
    build_fact_context,
    run_fact_extraction_pipeline,
)
from region_config import REGIONS, Region, get_gemini_key, get_groq_key, has_any_llm_key
from resilient_llm import call_llm_with_fallback, LONG_FORM_MAX_TOKENS
from trend_detector import DetectedTrend, detect_top_trends_all_regions
from word_count_calculator import calculate_word_count, get_prompt_instruction

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Engine configuration (env-driven, all TRD-compliant defaults)
# ─────────────────────────────────────────────────────────────
# TRD Section 3: "3-hour micro-cycle (triggering 8 times a day)"
ENGINE_INTERVAL_HOURS = int(os.getenv("ENGINE_INTERVAL_HOURS", "3"))
ENGINE_RUN_ON_STARTUP = os.getenv("ENGINE_RUN_ON_STARTUP", "0") == "1"

# TRD Section 3 Step A: minimum trend score to consider
ENGINE_MIN_TREND_SCORE = float(os.getenv("ENGINE_MIN_TREND_SCORE", "20"))

# TRD Section 3 Step B: "scrapes data fragments from at least 3-5 distinct
# trusted publications"
ENGINE_MAX_SOURCES = int(os.getenv("ENGINE_MAX_SOURCES", "5"))
ENGINE_MIN_SOURCES = int(os.getenv("ENGINE_MIN_SOURCES", "3"))
ENGINE_MIN_VERIFIED_FACTS = int(os.getenv("ENGINE_MIN_VERIFIED_FACTS", "3"))

# TRD Section 5: "Direct-to-live publishing is strictly prohibited"
ENGINE_DRAFT_STATUS = "draft"  # never change to "published"
ENGINE_PIPELINE_VERSION = "v30_trd1.0"


# ─────────────────────────────────────────────────────────────
# Article generation prompt (TRD Section 4 — Fields 1-6)
# ─────────────────────────────────────────────────────────────
# This is the SINGLE LLM call that produces ALL 6 fields as JSON.
# Per TRD Section 4:
#   FIELD 1: Title — SEO-optimized, no clickbait
#   FIELD 2: Short Summary — prose, NO bullets
#   FIELD 3: Audio Player Placeholder — handled by code (not LLM)
#   FIELD 4: Main Article Body — rich text with h3/h4 headers
#   FIELD 5: History & Contextual Background — 5-10 year deep dive
#   FIELD 6: Source References — list of [n] Domain - URL

ARTICLE_SYSTEM_PROMPT = """You are an elite news journalist and historian at SFAAM NEWS. Your task is to write an authoritative, deeply engaging news article using ONLY the verified facts provided.

ABSOLUTE RULES (violation = system failure):
1. USE ONLY the verified facts provided. Each fact has been cross-confirmed by 2+ independent authoritative sources.
2. DO NOT hallucinate, invent, or speculate. If a detail is not in the facts, OMIT it.
3. DO NOT add filler, padding, or generic statements to inflate word count.
4. DO NOT add disclaimers like "according to reports" — state facts directly.
5. Maintain a NEUTRAL, ENCYCLOPEDIC tone — but never a dry one. Be the BBC at its best, not a Wikipedia stub.
6. DO NOT address the reader directly. DO NOT use first person ("I", "we").
7. Length MUST scale with the verified fact volume — follow the TARGET LENGTH instruction precisely.
8. DO NOT cut off mid-sentence. Finish every sentence and section cleanly.

═══════════════════════════════════════════
ENGAGEMENT & STRUCTURE (V32.1 — READER ENGAGEMENT)
═══════════════════════════════════════════
Encyclopedic ≠ boring. The most-respected long-form journalism (Reuters
long-reads, NYT investigations, BBC analyses) is strictly factual AND
gripping. Use these techniques:

A) HOOK: Open the body with a single-sentence punchy paragraph that
   creates tension, stakes, or curiosity. NEVER open with "On [date],
   [entity] [verb] [object]" — that's wire-service filler. Find the
   most consequential or surprising fact and lead with it.

B) NUT GRAF: By the 3rd paragraph of the body, explain WHY this story
   matters right now — the geopolitical, economic, or human stakes.

C) BURSTINESS (defeats AI detectors + reads like human prose):
   • Mix very short sentences (3-7 words) with very long ones (25-40 words).
   • At least 20% of sentences under 8 words. At least 15% over 25 words.
   • Use deliberate sentence fragments for impact. Example: "Then silence."
   • Vary paragraph length too. Some paragraphs: one sentence. Some: five.

D) FORBIDDEN PHRASES (AI-detector magnets — never use):
   "Furthermore", "Moreover", "Additionally", "In addition", "In conclusion",
   "To summarize", "In summary", "It is important to note", "It is worth
   noting", "In today's world", "In the modern era", "When it comes to",
   "At the end of the day", "Delve into", "Navigate the complexities of",
   "A testament to", "In the ever-evolving landscape of", "Comprehensive",
   "Robust", "Seamless", "Leverage" (as a verb), "Firstly", "Secondly",
   "Lastly", "Finally" (as paragraph openers), and any sentence that begins
   with "This [noun] [verb]..." as a paragraph opener.

E) STRONG VERBS: Use concrete verbs ("slammed", "scrambled", "unspooled",
   "snagged") over generic ones ("said", "went", "made") where the facts
   support the connotation.

F) REQUIRED BODY SECTIONS (## headings):
   1. Lead / Hook section (the opening punchy paragraphs).
   2. "Key Players" — brief profiles of each person/entity mentioned in
      the facts. Readers skim for context on names they don't recognize.
   3. Thematic sub-sections (### under main ##) organized by the facts'
      themes — each fact gets its own paragraph or sub-section.
   4. "By the Numbers" — bullet list of the 3+ most striking quantitative
      data points, each with a one-line "why it matters" explanation.
      OMIT if fewer than 3 numbers in the verified facts.
   5. "What Happens Next" — forward-looking section outlining upcoming
      milestones, decisions, or expected developments mentioned in or
      directly implied by the facts. OMIT if no forward-looking facts.
   6. "Frequently Asked Questions" — 4-6 REAL reader questions answered
      in 2-3 sentences each, using ONLY verified facts. Captures Google
      "People Also Ask" traffic. OMIT if facts don't support 4+ questions.
   7. "Historical Context" — move the history_context FIELD 5 content
      here as a ## section at the end of the body so readers see it
      inline (the separate history_context field is for the sidebar widget).

G) INTERNAL LINKS: Where the facts naturally reference a related topic,
   region, or prior event, link to the {region} category page using
   markdown format: [Topic Name](/category/{region}). Use 2-4 internal
   links naturally throughout — never force them.

OUTPUT FORMAT — return a single JSON object with EXACTLY these fields:
{
  "title": "...",                       // FIELD 1: SEO-optimized, compelling, no clickbait
  "summary": "...",                     // FIELD 2: Single prose paragraph, NO bullets, 2-3 sentences
  "body": "...",                        // FIELD 4: Rich text with Markdown ## and ### headings
  "history_context": "..."              // FIELD 5: 5-10 year historical deep dive, rich text
}

FIELD-BY-FIELD RULES:

FIELD 1 — title:
  • Compelling and high-CTR but ENTIRELY accurate to the facts.
  • NO clickbait ("You won't believe...", "Shocking...").
  • 60-90 characters (SEO optimal; the old 90-char max was too generous).
  • Title Case capitalization.
  • Find a unique angle — never write the headline another publication would.

FIELD 2 — summary:
  • A single polished prose paragraph (2-3 sentences).
  • STRICTLY NO bullet points, NO markdown, NO headings.
  • Must flow seamlessly as a single narrative block.
  • Should give the reader the "who, what, when, where, why" in 50-80 words.
  • Open with the most consequential fact, not the date.

FIELD 4 — body (Main Article Body):
  • Highly detailed, deeply descriptive analysis matching the TARGET LENGTH.
  • Use semantic headers: ## for main sections, ### for sub-sections.
  • INCLUDE all the REQUIRED BODY SECTIONS listed in section F above.
  • Each section must add NEW information, not restate the summary.
  • Use **bold** for key entities (names, organizations, nations) on first mention.
  • Use em-dashes (—) and parenthetical asides for editorial voice (sparingly).
  • Vary sentence openers — never start three sentences in a row with the same word.
  • Use specific numbers from the facts wherever possible.

FIELD 5 — history_context (History & Contextual Background):
  • Leverage historical context surrounding the entities involved.
  • For trade disputes, chart the last 5-10 years of economic relations.
  • For political events, provide background on the parties/figures involved.
  • For conflicts, summarize the historical timeline leading to current events.
  • Use ONLY verified historical facts — DO NOT invent dates, treaties, or events.
  • If insufficient historical context exists in the verified facts, write a brief
    background section using only what IS available (do not pad).

FORMATTING:
  • Use Markdown throughout (## ### headings, **bold**, *italic*, lists where appropriate).
  • Plain text inside JSON strings — escape newlines as \\n, quotes as \\".
  • NO preface, NO meta-commentary, NO "Here is your article". Just the JSON object.

Return ONLY the JSON object. No markdown fence, no explanation."""


def _build_article_user_prompt(
    topic: str,
    region: Region,
    fact_result: FactExtractionResult,
    word_count_calc,
) -> str:
    """Build the user prompt for article generation."""
    fact_context = build_fact_context(fact_result)
    length_instruction = get_prompt_instruction(word_count_calc)

    return f"""TOPIC: {topic}
REGION: {region.display}

{length_instruction}

{fact_context}

Generate the article now as a JSON object with fields: title, summary, body, history_context.
Use ONLY the verified facts above. Do NOT add any information not present in the facts.
Return ONLY the JSON object — no markdown fence, no explanation."""


# ─────────────────────────────────────────────────────────────
# Graceful Formatting Sanitation (TRD Section 6)
# ─────────────────────────────────────────────────────────────
# "The backend must run a regex or parser filter on the LLM output to
# strip out conversational filler (e.g. 'Here is the article you requested:')
# before storing it in the database."
#
# IMPORTANT (Bug #5): _strip_filler is ONLY called on INDIVIDUAL parsed
# field values (title, summary, body, history_context) — NEVER on the
# raw LLM JSON response. Calling it on raw JSON could corrupt the JSON
# structure (e.g. if a body field legitimately starts with "Here is the
# article about..."). The raw JSON is parsed first, THEN each field is
# sanitized.
#
# Patterns are anchored at start-of-string (^) and require the filler
# phrase to be at the very beginning — this prevents false matches in
# the middle of an article body.

_FILLER_PATTERNS = [
    # Must be at the very start of the field (anchored with ^)
    r"^here\s+is\s+the\s+article.*$",
    r"^here\s+is\s+your\s+article.*$",
    r"^certainly!.*$",
    r"^sure!.*$",
    r"^of\s+course!.*$",
    r"^i'll\s+write\s+.*article.*$",
    r"^below\s+is\s+.*article.*$",
    r"^sure,\s+here.*$",
    r"^absolutely!.*$",
    r"^let\s+me\s+(write|create|generate).*$",
    r"^```(?:json)?\s*$",       # stray markdown fence lines (shouldn't be in fields, but safe)
    r"^```\s*$",
]
_FILLER_RE = re.compile("|".join(_FILLER_PATTERNS), re.IGNORECASE | re.MULTILINE)


def _strip_filler(raw: str) -> str:
    """Strip conversational filler from a SINGLE FIELD (not raw JSON).

    ⚠️ Bug #5 FIX: This function is now ONLY safe to call on individual
    parsed field values (title, summary, body, history_context) — NOT
    on the raw LLM JSON response. Calling it on the raw response could
    corrupt the JSON structure if a body field legitimately contains
    text starting with "Here is the article..." (which is rare but
    possible).
    """
    if not raw:
        return ""
    # Remove filler lines (only at start of lines)
    cleaned = _FILLER_RE.sub("", raw)
    return cleaned.strip()


def _repair_llm_json(json_str: str) -> str:
    """Apply common LLM JSON repair heuristics.

    LLMs frequently produce invalid JSON. This function fixes the most
    common issues BEFORE we hand off to json.loads:

      1. Raw newlines inside string values (must be \\n)
      2. Raw tabs inside string values (must be \\t)
      3. Trailing commas in arrays/objects (JSON spec forbids these)
      4. Single-quoted strings ('value' → "value")
      5. Unescaped control chars (CR, FF, etc.)

    The fix is conservative — we only touch characters INSIDE string
    values, never the JSON structure itself.
    """
    if not json_str:
        return json_str

    # Step 1: Walk the string char-by-char, tracking whether we're inside
    # a string literal. When inside, escape raw newlines/tabs/control chars.
    out = []
    in_string = False
    escape = False
    for ch in json_str:
        if escape:
            # Previous char was a backslash — pass this char through as-is
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
            # Inside a string — escape problematic chars
            if ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            elif ord(ch) < 0x20:
                # Other control chars — drop them (safer than escaping)
                continue
            else:
                out.append(ch)
        else:
            out.append(ch)

    repaired = "".join(out)

    # Step 2: Remove trailing commas inside arrays and objects.
    # Matches: `,` followed by optional whitespace then `]` or `}`.
    repaired = re.sub(r",\s*([\]}])", r"\1", repaired)

    return repaired


def _parse_article_json(raw: str) -> Optional[dict]:
    """Parse the LLM's article JSON response.

    Handles common LLM JSON mistakes:
      • Markdown code fences (```json ... ```)
      • Preface text before the JSON
      • Raw newlines inside string values (Bug #4)
      • Trailing commas in arrays/objects
      • Single-quoted strings
      • Other control characters

    Tries multiple repair passes before giving up. If all fail, returns
    None (caller treats as a failed article-gen attempt — but does NOT
    record dedup, so the topic will be retried next cycle).
    """
    if not raw:
        return None

    # Strip markdown fences ONLY (not the filler regex, which is unsafe on raw JSON)
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*\n", "", cleaned)
        cleaned = re.sub(r"\n```\s*$", "", cleaned)

    # Find the first { and last }
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        logger.warning("[ArticleGen] No JSON object found in LLM response")
        return None
    json_str = cleaned[start:end + 1]

    # Pass 1: try as-is (in case the LLM produced valid JSON)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e1:
        logger.debug(f"[ArticleGen] Pass 1 (raw) failed: {e1}")

    # Pass 2: repair common LLM issues (newlines in strings, trailing commas)
    try:
        repaired = _repair_llm_json(json_str)
        return json.loads(repaired)
    except json.JSONDecodeError as e2:
        logger.debug(f"[ArticleGen] Pass 2 (repaired) failed: {e2}")

    # Pass 3: replace single quotes with double quotes (last resort — risky)
    try:
        fixed = json_str.replace("'", '"')
        repaired = _repair_llm_json(fixed)
        return json.loads(repaired)
    except json.JSONDecodeError as e3:
        logger.warning(
            f"[ArticleGen] All JSON parse passes failed. Last error: {e3}. "
            f"Raw response (first 300 chars): {raw[:300]!r}"
        )
        return None


# ─────────────────────────────────────────────────────────────
# Audio Player Placeholder (TRD Section 4 — FIELD 3)
# ─────────────────────────────────────────────────────────────
# "Insert the frontend audio token/hook directly below the Short Summary.
#  (Do not alter the existing functional player asset.)"
#
# We generate a stable token per article that the frontend can resolve
# to its audio player widget. The token format is {{AUDIO_PLAYER:<id>}}.
# The frontend's article.html should replace this token with the
# actual <audio-player> component (which already exists in static/js/app.js).

def _generate_audio_token(article_id: Optional[int] = None) -> str:
    """Generate an audio player placeholder token."""
    suffix = article_id if article_id else secrets.token_hex(6)
    return f"{{{{AUDIO_PLAYER:{suffix}}}}}"


# ─────────────────────────────────────────────────────────────
# Article generation result type
# ─────────────────────────────────────────────────────────────
@dataclass
class GeneratedArticle:
    """Result of article generation for one topic + region."""
    title: str
    summary: str
    body: str
    history_context: str
    audio_player_token: str
    references: list[str]
    word_count: int
    word_count_tier: str
    word_count_target: int
    llm_provider: str
    llm_model: str
    llm_elapsed_s: float
    success: bool
    error: str = ""


async def generate_article(
    topic: str,
    region: Region,
    fact_result: FactExtractionResult,
    *,
    groq_key: str = "",
    gemini_key: str = "",
) -> GeneratedArticle:
    """Generate a full article (TRD Fields 1-6) from verified facts.

    Per TRD Section 4: produces title, summary, body, history_context.
    Audio player placeholder is inserted by code (not LLM).
    References are pulled from the verified facts' source list.
    """
    # Step C: calculate word count tier based on fact pool
    fact_count = len(fact_result.verified_facts)
    word_count_calc = calculate_word_count(fact_count)

    if not word_count_calc.is_sufficient:
        return GeneratedArticle(
            title="", summary="", body="", history_context="",
            audio_player_token="", references=[], word_count=0,
            word_count_tier="", word_count_target=0,
            llm_provider="", llm_model="", llm_elapsed_s=0.0,
            success=False,
            error=f"insufficient facts ({fact_count} < 3)",
        )

    # Build the prompt
    user_prompt = _build_article_user_prompt(topic, region, fact_result, word_count_calc)

    # Step D: LLM call with TRD-compliant resilience.
    # V32.1 BUGFIX: call_llm_with_fallback is a SYNC function (it uses
    # synchronous Groq/Gemini SDKs and time.sleep backoff). Calling it
    # directly from this async function BLOCKS the FastAPI event loop
    # for 30-90s per article — combined with fact_extractor sync calls,
    # a single engine cycle froze the entire site for ~13 minutes every
    # 3 hours. Wrap in asyncio.to_thread so it runs in a worker thread.
    result = await asyncio.to_thread(
        call_llm_with_fallback,
        region_key=region.key,
        system_prompt=ARTICLE_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        groq_key=groq_key,
        gemini_key=gemini_key,
        max_tokens=word_count_calc.max_tokens,
        temperature=0.3,  # slightly higher than extraction for narrative flow
    )

    if not result.success:
        return GeneratedArticle(
            title="", summary="", body="", history_context="",
            audio_player_token="", references=[], word_count=0,
            word_count_tier=word_count_calc.tier.label,
            word_count_target=word_count_calc.target_mid,
            llm_provider="", llm_model="",
            llm_elapsed_s=result.total_elapsed_s,
            success=False,
            error=f"LLM call failed: {result.attempts[-1] if result.attempts else 'unknown'}",
        )

    # Parse the JSON response
    article_data = _parse_article_json(result.text)
    if not article_data:
        return GeneratedArticle(
            title="", summary="", body="", history_context="",
            audio_player_token="", references=[], word_count=0,
            word_count_tier=word_count_calc.tier.label,
            word_count_target=word_count_calc.target_mid,
            llm_provider=result.provider, llm_model=result.model,
            llm_elapsed_s=result.total_elapsed_s,
            success=False,
            error="could not parse LLM JSON response",
        )

    title = (article_data.get("title") or "").strip()
    summary = (article_data.get("summary") or "").strip()
    body = (article_data.get("body") or "").strip()
    history_context = (article_data.get("history_context") or "").strip()

    # Sanitize: strip filler from each field
    title = _strip_filler(title)[:500]
    summary = _strip_filler(summary)
    body = _strip_filler(body)
    history_context = _strip_filler(history_context)

    if not title or not body:
        return GeneratedArticle(
            title=title, summary=summary, body=body, history_context=history_context,
            audio_player_token="", references=[], word_count=0,
            word_count_tier=word_count_calc.tier.label,
            word_count_target=word_count_calc.target_mid,
            llm_provider=result.provider, llm_model=result.model,
            llm_elapsed_s=result.total_elapsed_s,
            success=False,
            error="empty title or body after parsing",
        )

    # Compute actual word count from body
    word_count = len(body.split())

    # Generate audio player placeholder token
    audio_token = _generate_audio_token()

    # Build references list (TRD FIELD 6) from verified facts' sources
    seen_urls: set[str] = set()
    references: list[str] = []
    for fact in fact_result.verified_facts:
        for url in fact.source_urls:
            if url and url not in seen_urls:
                seen_urls.add(url)
                references.append(url)
    # Also include any scraped sources not already in the list
    for src in fact_result.sources_scraped:
        if src.url and src.url not in seen_urls:
            seen_urls.add(src.url)
            references.append(src.url)

    logger.info(
        f"[ArticleGen] '{topic[:50]}' region={region.key}: "
        f"{word_count} words (tier={word_count_calc.tier.label}, "
        f"target={word_count_calc.target_min}-{word_count_calc.target_max}), "
        f"provider={result.provider}/{result.model}, "
        f"refs={len(references)}, "
        f"elapsed={result.total_elapsed_s:.1f}s"
    )

    return GeneratedArticle(
        title=title,
        summary=summary,
        body=body,
        history_context=history_context,
        audio_player_token=audio_token,
        references=references,
        word_count=word_count,
        word_count_tier=word_count_calc.tier.label,
        word_count_target=word_count_calc.target_mid,
        llm_provider=result.provider,
        llm_model=result.model,
        llm_elapsed_s=result.total_elapsed_s,
        success=True,
    )


# ─────────────────────────────────────────────────────────────
# Save article as DRAFT (TRD Section 5)
# ─────────────────────────────────────────────────────────────
async def _save_draft_article(
    *,
    topic: str,
    region: Region,
    detected_trend: DetectedTrend,
    fact_result: FactExtractionResult,
    article: GeneratedArticle,
    cycle_id: str,
) -> Optional[int]:
    """Save a generated article as a DRAFT in the database.

    Per TRD Section 5: "All automated outputs must be securely POSTed
    to the backend with the status attribute explicitly set to 'Draft'."

    Returns the new article ID, or None on failure.
    """
    # Build a deterministic hash to prevent duplicate saves in same cycle
    hash_input = f"engine|{region.key}|{topic}|{cycle_id}".encode()
    article_hash = hashlib.sha256(hash_input).hexdigest()

    # Serialize fact_sources JSON (TRD audit field)
    fact_sources_json = json.dumps([
        {
            "url": s.url, "domain": s.domain,
            "title": s.title, "snippet": s.snippet,
        }
        for s in fact_result.sources_scraped
    ], ensure_ascii=False)

    # Serialize verified_facts JSON
    verified_facts_json = json.dumps([
        {
            "text": f.text,
            "type": f.fact_type,
            "source_urls": f.source_urls,
            "source_domains": f.source_domains,
            "confirmation_count": f.confirmation_count,
        }
        for f in fact_result.verified_facts
    ], ensure_ascii=False)

    # References JSON (TRD FIELD 6)
    references_json = json.dumps(article.references, ensure_ascii=False)

    # Build slug
    # Bug #14 FIX: Add a short timestamp suffix to guarantee slug uniqueness.
    # Without this, if the same topic recurs after the 7-day dedup window
    # expires, the new article would produce the same slug as the old one.
    # The public /api/article/{slug} endpoint uses scalar_one_or_none() which
    # raises MultipleResultsFound on duplicate slugs → article page crashes.
    slug_base = article.title.lower()
    slug_base = "".join(c if c.isalnum() or c.isspace() else " " for c in slug_base)
    slug_base = "-".join(slug_base.split())
    # Append YYYYMMDD-HHMM suffix for uniqueness (e.g. "us-canada-tariffs-20260721-1430")
    timestamp_suffix = datetime.utcnow().strftime("%Y%m%d-%H%M")
    slug = f"engine/{region.key}/{slug_base}-{timestamp_suffix}"[:600]

    # Meta description from summary
    meta_desc = (article.summary or article.title)[:300]

    # Build the full content for ai_content.
    #
    # V30 FIX (Bug #10): Do NOT prepend the summary to ai_content. The summary
    # is stored separately in the `summary` column AND rendered separately by
    # buildTldrSection(a.tldr_summary) above the article body. Prepending it
    # here would cause the summary to appear TWICE on the article page.
    #
    # The audio player token (TRD FIELD 3) still goes at the very top of
    # ai_content so it renders between the TLDR summary box and the main body.
    #
    # Structure of ai_content:
    #   {{AUDIO_PLAYER:xxx}}              ← TRD FIELD 3 (audio player hook)
    #
    #   <body>                            ← TRD FIELD 4 (main article body
    #     ## Section 1                       with ## and ### headers)
    #     ...
    #     ## Section N
    #
    #   ## History & Contextual Background  ← TRD FIELD 5
    #   <history_context>
    #
    #   ## Sources                          ← TRD FIELD 6
    #   1. [domain](url)
    #   2. [domain](url)
    full_content_parts = []
    # Audio player token FIRST (after the TLDR summary box on the page, which
    # is rendered separately by buildTldrSection — not our concern here)
    full_content_parts.append(article.audio_player_token)  # FIELD 3 placeholder
    full_content_parts.append("")
    # Main body
    full_content_parts.append(article.body)
    # History & Contextual Background (TRD FIELD 5)
    if article.history_context:
        full_content_parts.append("")
        full_content_parts.append("## History & Contextual Background")
        full_content_parts.append("")
        full_content_parts.append(article.history_context)
    # Sources (TRD FIELD 6)
    if article.references:
        full_content_parts.append("")
        full_content_parts.append("## Sources")
        full_content_parts.append("")
        for i, url in enumerate(article.references, 1):
            from urllib.parse import urlparse
            domain = urlparse(url).hostname or "source"
            if domain.startswith("www."):
                domain = domain[4:]
            full_content_parts.append(f"{i}. [{domain}]({url})")

    full_content = "\n".join(full_content_parts)

    async with AsyncSessionLocal() as session:
        # Check for duplicate (same hash in last 24h)
        existing = await session.execute(
            select(Article).where(Article.article_hash == article_hash).limit(1)
        )
        if existing.scalar_one_or_none():
            logger.info(
                f"[Engine] Skipping duplicate save: '{topic[:50]}' "
                f"region={region.key} (already drafted this cycle)"
            )
            return None

        # V31.1: Title uniqueness check — if the AI-generated title is a
        # duplicate or near-duplicate of an existing article, append a suffix.
        # This is critical for the trends pipeline because the same trend query
        # can produce nearly identical titles across cycles.
        _title_norm_value = None
        try:
            from title_uniqueness import ensure_unique_title, compute_title_norm
            unique_title = await ensure_unique_title(session, article.title)
            if unique_title != article.title:
                logger.info(
                    f"[Engine] Title deduped: '{article.title[:50]}' → '{unique_title[:50]}'"
                )
                article.title = unique_title
                # V32.1 BUGFIX: DO NOT overwrite the engine/region/timestamp
                # slug with a bare make_slug(unique_title). The bare slug has
                # no uniqueness suffix, and the slug column has no UNIQUE
                # constraint (database.py:142), so duplicate slugs would
                # collide and /api/article/{slug} would serve the WRONG
                # article. Instead, preserve the engine/region prefix and
                # timestamp suffix — only the slug_base (title-derived part)
                # is regenerated from the deduped title.
                new_slug_base = "".join(
                    c if c.isalnum() or c.isspace() else " "
                    for c in unique_title.lower()
                )
                new_slug_base = "-".join(new_slug_base.split())
                slug = f"engine/{region.key}/{new_slug_base}-{timestamp_suffix}"[:600]
            _title_norm_value = compute_title_norm(article.title)[:500]
        except Exception as title_fix_err:
            logger.warning(f"[Engine] Title uniqueness check failed: {title_fix_err}")

        article_row = Article(
            title=article.title,
            slug=slug,
            # V31.1: Store normalized title for fast duplicate detection
            title_norm=_title_norm_value,
            original_url=f"https://trends.google.com/?q={quote_plus(topic)}&cycle={cycle_id}&region={region.key}",
            ai_content=full_content,
            summary=article.summary,
            image_url="",
            region=region.key,
            meta_desc=meta_desc,
            keywords=topic,
            article_hash=article_hash,
            tldr_summary=article.summary,
            fact_check_status="verified",
            status=ENGINE_DRAFT_STATUS,   # ALWAYS draft — admin reviews before publishing
            source_type="trends",
            search_keyword=topic,
            is_trends=1,
            trend_query=topic,
            fact_sources=fact_sources_json,
            verified_facts=verified_facts_json,
            source_count=fact_result.unique_source_count,
            word_count=article.word_count,
            references_data=references_json,
            pipeline_version=ENGINE_PIPELINE_VERSION,
            # V30 new fields
            history_context=article.history_context,
            audio_player_token=article.audio_player_token,
            word_count_tier=article.word_count_tier,
            word_count_target=article.word_count_target,
            trend_score=int(detected_trend.trend_score),
            cross_source_count=detected_trend.cross_source_count,
            llm_provider=article.llm_provider,
            llm_model=article.llm_model,
            raw_facts_count=len(fact_result.raw_facts_extracted),
            dropped_facts_count=(
                len(fact_result.dropped_single_source_facts)
                + len(fact_result.dropped_low_similarity_facts)
            ),
            fact_extraction_elapsed_s=int(fact_result.llm_elapsed_s),
            article_generation_elapsed_s=int(article.llm_elapsed_s),
        )
        session.add(article_row)
        await session.commit()
        await session.refresh(article_row)

        logger.info(
            f"[Engine] SAVED DRAFT id={article_row.id} region={region.key} "
            f"'{topic[:50]}' ({article.word_count} words, "
            f"tier={article.word_count_tier}, "
            f"provider={article.llm_provider})"
        )
        return article_row.id


# ─────────────────────────────────────────────────────────────
# Per-region pipeline
# ─────────────────────────────────────────────────────────────
@dataclass
class RegionCycleResult:
    """Result of running the pipeline for one region in one cycle."""
    region: str
    topic: str = ""
    detected_trend_score: float = 0.0
    sources_scraped: int = 0
    unique_source_count: int = 0
    raw_facts_count: int = 0
    verified_facts_count: int = 0
    dropped_facts_count: int = 0
    word_count: int = 0
    word_count_tier: str = ""
    llm_provider: str = ""
    llm_model: str = ""
    article_id: Optional[int] = None
    status: str = "skipped"   # detected | researched | extracted | drafted | failed | skipped_dedup | no_trend
    error: str = ""
    elapsed_s: float = 0.0


async def _process_region(
    region: Region,
    cycle_id: str,
    skip_queries: set[str],
) -> RegionCycleResult:
    """Run the full pipeline (Steps A-D) for a single region."""
    start = time.monotonic()
    result = RegionCycleResult(region=region.key)

    # Per-region API keys (TRD Section 2: isolated per region)
    groq_key = get_groq_key(region.key)
    gemini_key = get_gemini_key(region.key)

    if not has_any_llm_key(region.key):
        result.status = "failed"
        result.error = f"no LLM API keys configured for region '{region.key}'"
        result.elapsed_s = time.monotonic() - start
        logger.warning(f"[Engine] region={region.key}: {result.error}")
        return result

    try:
        # Step A: Trend Detection
        from trend_detector import detect_top_trend
        trend = detect_top_trend(region, skip_queries=skip_queries,
                                  min_score=ENGINE_MIN_TREND_SCORE)
        if trend is None:
            result.status = "no_trend"
            result.elapsed_s = time.monotonic() - start
            logger.info(f"[Engine] region={region.key}: no qualifying trend detected")
            return result

        result.topic = trend.query
        result.detected_trend_score = trend.trend_score
        result.status = "detected"

        # Step B: Fact Extraction + Verification
        fact_result = await run_fact_extraction_pipeline(
            trend.query, region,
            max_sources=ENGINE_MAX_SOURCES,
            groq_key=groq_key,
            gemini_key=gemini_key,
        )

        if fact_result.error or not fact_result.is_sufficient:
            result.status = "failed"
            result.error = fact_result.error or "insufficient facts for article"
            result.sources_scraped = len(fact_result.sources_scraped)
            result.unique_source_count = fact_result.unique_source_count
            result.raw_facts_count = len(fact_result.raw_facts_extracted)
            result.verified_facts_count = len(fact_result.verified_facts)
            result.elapsed_s = time.monotonic() - start
            logger.warning(f"[Engine] region={region.key}: {result.error}")

            # ── Bug #2 FIX: only record dedup on PERMANENT failures ──
            # If we record on transient failures (LLM timeout, 429, network
            # blip), the topic gets blacklisted for 7 days even though it
            # could have succeeded next cycle. That would starve the pipeline.
            #
            # PERMANENT failures (record dedup so we don't retry):
            #   • "no authoritative sources found"  — topic has no news coverage
            #   • "insufficient source diversity"   — topic covered by <3 outlets
            #   • "LLM extracted no facts"          — sources exist but no facts
            #   • "all facts dropped by safety shield" — facts unverifiable
            #   • "insufficient facts for article"  — fewer than 3 verified facts
            #
            # TRANSIENT failures (DO NOT record dedup — retry next cycle):
            #   • LLM call failed (timeout, 429, network)
            #   • Could not parse LLM JSON
            #   • DB save error
            permanent_failure_markers = (
                "no authoritative sources",
                "insufficient source diversity",
                "LLM extracted no facts",
                "all facts dropped by safety shield",
                "insufficient facts",
                "insufficient facts for article",
            )
            if any(marker in (result.error or "") for marker in permanent_failure_markers):
                logger.info(
                    f"[Engine] region={region.key}: permanent failure — recording "
                    f"dedup for '{trend.query[:40]}'"
                )
                await record_processed(region.key, trend.query, article_id=None, cycle_id=cycle_id)
            else:
                logger.info(
                    f"[Engine] region={region.key}: transient failure ('{result.error[:60]}') "
                    f"— NOT recording dedup, will retry next cycle"
                )
            return result

        result.sources_scraped = len(fact_result.sources_scraped)
        result.unique_source_count = fact_result.unique_source_count
        result.raw_facts_count = len(fact_result.raw_facts_extracted)
        result.verified_facts_count = len(fact_result.verified_facts)
        result.dropped_facts_count = (
            len(fact_result.dropped_single_source_facts)
            + len(fact_result.dropped_low_similarity_facts)
        )
        result.status = "extracted"

        # Step C + D: Calculate word count + Generate article
        article = await generate_article(
            trend.query, region, fact_result,
            groq_key=groq_key,
            gemini_key=gemini_key,
        )

        if not article.success:
            result.status = "failed"
            result.error = article.error
            result.elapsed_s = time.monotonic() - start
            logger.warning(f"[Engine] region={region.key}: article generation failed: {article.error}")
            # ── Bug #2 FIX: do NOT record dedup on article-gen failures ──
            # Article-gen failures are almost always transient (LLM timeout,
            # JSON parse error, empty response). The fact extraction already
            # succeeded — we just need to retry the article generation next
            # cycle. Recording dedup here would waste a perfectly good
            # researched topic.
            logger.info(
                f"[Engine] region={region.key}: article-gen failure — NOT recording "
                f"dedup, will retry topic '{trend.query[:40]}' next cycle"
            )
            return result

        result.word_count = article.word_count
        result.word_count_tier = article.word_count_tier
        result.llm_provider = article.llm_provider
        result.llm_model = article.llm_model

        # Step E: Save as DRAFT (TRD Section 5)
        article_id = await _save_draft_article(
            topic=trend.query, region=region,
            detected_trend=trend, fact_result=fact_result,
            article=article, cycle_id=cycle_id,
        )

        if article_id:
            result.article_id = article_id
            result.status = "drafted"
            # Record in dedup log so we don't write about this topic again for 7 days
            await record_processed(region.key, trend.query, article_id=article_id, cycle_id=cycle_id)
        else:
            result.status = "failed"
            result.error = "duplicate save or DB error"
            await record_processed(region.key, trend.query, article_id=None, cycle_id=cycle_id)

        result.elapsed_s = time.monotonic() - start
        logger.info(
            f"[Engine] region={region.key}: COMPLETE — '{trend.query[:40]}' "
            f"article_id={article_id} ({result.elapsed_s:.1f}s)"
        )
        return result

    except Exception as e:
        result.status = "failed"
        result.error = f"{type(e).__name__}: {e}"
        result.elapsed_s = time.monotonic() - start
        logger.exception(f"[Engine] region={region.key}: pipeline error: {e}")
        # V30 FIX (Bug #21): Send per-region errors to monitoring too.
        # These are non-fatal (other regions continue) but admins should
        # know which regions are failing.
        try:
            from monitoring import capture_exception as _capture_exc
            _capture_exc(e, context={
                "cycle_id": cycle_id,
                "region": region.key,
                "phase": "process_region",
                "topic": result.topic or "",
            })
        except Exception:
            pass  # monitoring is best-effort
        return result


# ─────────────────────────────────────────────────────────────
# Engine status (in-memory, for admin dashboard)
# ─────────────────────────────────────────────────────────────
_engine_status = {
    "running": False,
    "current_cycle_id": "",
    "started_at": "",
    "last_cycle_id": "",
    "last_completed_at": "",
    "last_status": "",   # completed | failed | partial
    "last_error": "",
    "regions_in_progress": [],
    "current_region": "",
    "current_topic": "",
    # Lifetime counters
    "total_cycles": 0,
    "total_drafts_produced": 0,
    "total_drafts_failed": 0,
    "total_duplicates_skipped": 0,
}


def get_engine_status() -> dict:
    """Return current engine status (for admin dashboard)."""
    return dict(_engine_status)


# ─────────────────────────────────────────────────────────────
# Main 3-hour cycle — runs all 6 regions
# ─────────────────────────────────────────────────────────────
async def run_engine_cycle(*, run_only_regions: Optional[list[str]] = None) -> dict:
    """Run one full 3-hour cycle of the SFAAM Automated News Engine.

    Per TRD: runs across all 6 regions (World, USA, UK, Pakistan, India,
    Germany) and produces at most 1 draft per region (so up to 6 drafts
    per cycle, up to 48 per day).

    Args:
        run_only_regions: Optional list of region keys to run (default: all 6).
                          Used by the admin "trigger single region" endpoint.

    Returns:
        Summary dict with per-region results.
    """
    if _engine_status["running"]:
        logger.warning("[Engine] Cycle already running — skipping")
        return {"status": "skipped", "reason": "already running"}

    # V30 FIX (Bug #22): Leader election for horizontal scaling.
    # If the app is deployed with multiple instances (Railway scale-out,
    # K8s replicas, etc.), only ONE instance should run the engine at a
    # time — otherwise we'd get duplicate articles from each instance.
    # The existing scheduler.py has this logic; we reuse it.
    _engine_leader_acquired = False  # default — set True only if we win
    try:
        from scheduler import _acquire_leadership, _release_leadership
        if not _acquire_leadership():
            logger.info("[Engine] Another instance holds leadership — skipping this cycle")
            return {"status": "skipped", "reason": "not leader (another instance is running the engine)"}
        _engine_leader_acquired = True
    except ImportError:
        # scheduler.py not available — fall back to single-instance mode
        pass
    except Exception as e:
        logger.warning(f"[Engine] Leader election failed ({e}) — proceeding in single-instance mode")

    _engine_status["running"] = True
    _engine_status["current_cycle_id"] = str(uuid.uuid4())
    _engine_status["started_at"] = datetime.now(timezone.utc).isoformat()
    _engine_status["last_error"] = ""
    _engine_status["regions_in_progress"] = []
    _engine_status["current_region"] = ""
    _engine_status["current_topic"] = ""

    cycle_id = _engine_status["current_cycle_id"]
    started_at = datetime.utcnow()

    logger.info("=" * 70)
    logger.info(f"[Engine] V30 TRD v1.0 Pipeline Started — cycle_id={cycle_id}")
    logger.info(f"[Engine] Interval: every {ENGINE_INTERVAL_HOURS}h | "
                f"Regions: {[r.key for r in REGIONS]}")
    logger.info("=" * 70)

    # Refresh dedup cache from DB (so we have the latest skip set).
    # V32.1 BUGFIX: refresh_cache_from_db is an ASYNC function (it uses
    # async DB session). Wrapping an async function in asyncio.to_thread
    # raises RuntimeError ("coroutine was never awaited") because
    # to_thread expects a sync callable — it schedules the coroutine
    # object as if it were a regular function, the coroutine never runs,
    # and Python logs the unawaited-coroutine warning. Fix: just await
    # it directly.
    await refresh_cache_from_db()
    skip_sets = get_skip_sets_all_regions()

    # Determine which regions to run
    regions_to_run = [r for r in REGIONS if not run_only_regions or r.key in run_only_regions]
    _engine_status["regions_in_progress"] = [r.key for r in regions_to_run]

    # Save cycle log row (status=running)
    cycle_log_id: Optional[int] = None
    try:
        async with AsyncSessionLocal() as session:
            cycle_log = EngineCycleLog(
                cycle_id=cycle_id,
                started_at=started_at,
                status="running",
            )
            session.add(cycle_log)
            await session.commit()
            await session.refresh(cycle_log)
            cycle_log_id = cycle_log.id
    except Exception as e:
        logger.warning(f"[Engine] Could not create cycle log row: {e}")

    # Run each region sequentially (be polite to source servers and LLM rate limits)
    region_results: list[RegionCycleResult] = []
    summary = {
        "cycle_id": cycle_id,
        "started_at": _engine_status["started_at"],
        "regions_processed": 0,
        "drafts_produced": 0,
        "drafts_failed": 0,
        "skipped_duplicates": 0,
        "results": [],
    }

    try:
        for region in regions_to_run:
            _engine_status["current_region"] = region.key
            logger.info(f"[Engine] [{len(region_results) + 1}/{len(regions_to_run)}] "
                        f"Processing region: {region.display}")

            skip = skip_sets.get(region.key, set())
            result = await _process_region(region, cycle_id, skip)
            region_results.append(result)
            summary["regions_processed"] += 1
            summary["results"].append({
                "region": result.region,
                "topic": result.topic,
                "status": result.status,
                "sources_scraped": result.sources_scraped,
                "unique_source_count": result.unique_source_count,
                "verified_facts_count": result.verified_facts_count,
                "word_count": result.word_count,
                "word_count_tier": result.word_count_tier,
                "llm_provider": result.llm_provider,
                "article_id": result.article_id,
                "error": result.error,
                "elapsed_s": round(result.elapsed_s, 2),
                "detected_trend_score": result.detected_trend_score,
            })

            if result.status == "drafted":
                summary["drafts_produced"] += 1
                _engine_status["total_drafts_produced"] += 1
            elif result.status == "skipped_dedup":
                summary["skipped_duplicates"] += 1
                _engine_status["total_duplicates_skipped"] += 1
            else:
                summary["drafts_failed"] += 1
                _engine_status["total_drafts_failed"] += 1

        _engine_status["total_cycles"] += 1

        # Final status
        if summary["drafts_produced"] == len(regions_to_run):
            _engine_status["last_status"] = "completed"
        elif summary["drafts_produced"] > 0:
            _engine_status["last_status"] = "partial"
        else:
            _engine_status["last_status"] = "failed"

        logger.info("=" * 70)
        logger.info(
            f"[Engine] V30 Pipeline Complete — "
            f"{summary['drafts_produced']}/{summary['regions_processed']} drafts produced, "
            f"{summary['drafts_failed']} failed, "
            f"{summary['skipped_duplicates']} dedup-skipped"
        )
        logger.info("=" * 70)

    except Exception as e:
        logger.exception(f"[Engine] Pipeline fatal error: {e}")
        _engine_status["last_error"] = f"{type(e).__name__}: {e}"
        _engine_status["last_status"] = "failed"
        summary["error"] = str(e)
        # V30 FIX (Bug #21): Send critical engine errors to monitoring
        # (Sentry/Better Stack/Slack webhook) so admins get alerted.
        try:
            from monitoring import capture_exception as _capture_exc
            _capture_exc(e, context={"cycle_id": cycle_id, "phase": "engine_cycle"})
        except Exception:
            pass  # monitoring is best-effort, don't crash on it
    finally:
        _engine_status["running"] = False
        _engine_status["current_region"] = ""
        _engine_status["current_topic"] = ""
        _engine_status["last_cycle_id"] = cycle_id
        _engine_status["last_completed_at"] = datetime.now(timezone.utc).isoformat()
        _engine_status["regions_in_progress"] = []
        # V30 FIX (Bug #22): Release leadership so another instance can run
        # the next cycle. Safe to call even if we never acquired leadership.
        try:
            if _engine_leader_acquired:
                from scheduler import _release_leadership
                _release_leadership()
        except Exception:
            pass

    # Update cycle log row with final results
    completed_at = datetime.utcnow()
    total_elapsed = int((completed_at - started_at).total_seconds())
    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import update
            await session.execute(
                update(EngineCycleLog)
                .where(EngineCycleLog.id == cycle_log_id)
                .values(
                    completed_at=completed_at,
                    regions_processed=summary["regions_processed"],
                    drafts_produced=summary["drafts_produced"],
                    drafts_failed=summary["drafts_failed"],
                    skipped_duplicates=summary["skipped_duplicates"],
                    total_elapsed_s=total_elapsed,
                    status=_engine_status["last_status"],
                    error=_engine_status.get("last_error", ""),
                    region_summary=json.dumps(summary["results"], ensure_ascii=False),
                )
            )
            await session.commit()
    except Exception as e:
        logger.warning(f"[Engine] Could not update cycle log row: {e}")

    summary["completed_at"] = _engine_status["last_completed_at"]
    summary["status"] = _engine_status["last_status"]
    summary["total_elapsed_s"] = total_elapsed
    return summary


# ─────────────────────────────────────────────────────────────
# Nightly cleanup — prune old dedup entries + cycle logs
# ─────────────────────────────────────────────────────────────
async def nightly_cleanup() -> dict:
    """Run nightly maintenance tasks:
       1. Prune dedup entries older than 7 days (TRD rolling window)
       2. Prune cycle logs older than 90 days (audit retention)
    """
    dedup_deleted = await cleanup_dedup_entries()

    cycle_logs_deleted = 0
    try:
        from datetime import timedelta
        from sqlalchemy import text
        cutoff = datetime.utcnow() - timedelta(days=90)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("DELETE FROM engine_cycle_logs WHERE started_at < :c"),
                {"c": cutoff},
            )
            await session.commit()
            cycle_logs_deleted = result.rowcount or 0
    except Exception as e:
        logger.warning(f"[Engine] Cycle log cleanup failed: {e}")

    logger.info(
        f"[Engine] Nightly cleanup: pruned {dedup_deleted} dedup entries, "
        f"{cycle_logs_deleted} cycle logs"
    )
    return {"dedup_pruned": dedup_deleted, "cycle_logs_pruned": cycle_logs_deleted}


# ─────────────────────────────────────────────────────────────
# CLI for manual testing
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio
    import logging
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    # Allow running a single region: python automated_news_engine.py pakistan
    regions = [sys.argv[1]] if len(sys.argv) > 1 else None
    result = asyncio.run(run_engine_cycle(run_only_regions=regions))

    print()
    print("=" * 70)
    print("SFAAM Automated News Engine — Cycle Result")
    print("=" * 70)
    print(json.dumps(result, indent=2, ensure_ascii=False))
