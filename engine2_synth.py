"""
engine2_synth.py - SFAAM Automated News Engine V2 (Clean Rebuild)
====================================================================
STEP 4 of the 6-step workflow: AI Synthesis (5 Articles -> 1 Article)

AI provider: Groq (Llama 3.3 70B) primary, Gemini (2.0 Flash) fallback —
reuses the existing battle-tested resilient_llm.call_llm_with_fallback()
(TRD-compliant resilience: instant fallback + exponential backoff).

Produces the 4 required outputs:
    4.1 Unique title       (SEO-optimized, 8-12 words, not copied)
    4.2 Summary             (1-2 prose paragraphs, no bullets)
    4.3 Overview             (main body, H3/H4 subheadings, max detail)
    4.4 Background History  (5-10 year deep dive)

NOTE on the "max 30,000 words" spec target: free-tier LLM APIs cap
output tokens well below what's needed for a single 30,000-word
generation (Llama-3.3-70B on Groq's free tier tops out around 8K
output tokens ≈ 5-6K words). This module requests the model's
practical maximum (LONG_FORM_MAX_TOKENS) per section and is designed
so the tier config can be raised later if a higher-tier API plan is
used — see MAX_TOKENS_OVERVIEW / MAX_TOKENS_BACKGROUND below.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from region_config import Region, get_groq_key, get_gemini_key
from resilient_llm import call_llm_with_fallback, LONG_FORM_MAX_TOKENS

logger = logging.getLogger(__name__)

MAX_TOKENS_OVERVIEW = LONG_FORM_MAX_TOKENS      # ~8000 tokens
MAX_TOKENS_BACKGROUND = 3000
MAX_SOURCE_CHARS_PER_ARTICLE = 4000             # keep prompt within context window


@dataclass
class SynthResult:
    success: bool
    title: str = ""
    summary: str = ""
    overview_md: str = ""       # markdown, H3/H4 subheadings, no title/images
    background_md: str = ""     # markdown, 5-10 year history
    word_count: int = 0
    llm_provider: str = ""
    llm_model: str = ""
    error: str = ""


def _build_source_digest(scraped_articles: list) -> str:
    parts = []
    for i, a in enumerate(scraped_articles, 1):
        text = (a.text or "")[:MAX_SOURCE_CHARS_PER_ARTICLE]
        parts.append(
            f"SOURCE {i} — {a.title}\n"
            f"(published: {a.published or 'unknown'}, author: {a.author or 'unknown'})\n"
            f"{text}\n"
        )
    return "\n---\n".join(parts)


def _extract_json(text: str) -> dict | None:
    """LLMs sometimes wrap JSON in ```json fences or add preamble — strip it."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _word_count(*texts: str) -> int:
    return sum(len(t.split()) for t in texts if t)


def synthesize_article(region: Region, trend_query: str, scraped_articles: list) -> SynthResult:
    """Step 4: combine up to 5 scraped articles into one detailed article."""
    if not scraped_articles:
        return SynthResult(success=False, error="no scraped articles to synthesize from")

    groq_key = get_groq_key(region.key)
    gemini_key = get_gemini_key(region.key)
    if not groq_key and not gemini_key:
        return SynthResult(success=False, error=f"no AI key configured for region={region.key}")

    digest = _build_source_digest(scraped_articles)

    # ── Call 1: Title + Summary + Overview ──
    system_prompt = (
        "You are a neutral, factual news editor. You are given several source "
        "articles about the same trending topic. Combine them into ONE original, "
        "detailed article. Never copy sentences verbatim from the sources — "
        "rewrite everything in your own words. Stay strictly neutral (no bias "
        "toward any side). Only use facts present in the sources — never invent "
        "or hallucinate details. Respond with ONLY valid JSON, no other text."
    )
    user_prompt = (
        f"Topic (trending in {region.display}): {trend_query}\n\n"
        f"SOURCE ARTICLES:\n{digest}\n\n"
        "Produce a JSON object with exactly these keys:\n"
        '  "title": a unique, SEO-optimized, informative (not clickbait) title, 8-12 words.\n'
        '  "summary": 1-2 short paragraphs of plain prose (NO bullet points), '
        "3-5 sentences total, neutral tone, explaining the whole topic briefly.\n"
        '  "overview_markdown": the main article body in Markdown. Use "### " and '
        '"#### " subheadings to organize sections. Cover maximum detail available '
        "in the sources — all key facts, context, and implications. Neutral tone. "
        "Do not include a top-level title heading (the title is separate). Do not "
        "include images or a sources list here."
    )
    result1 = call_llm_with_fallback(
        region_key=region.key,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        groq_key=groq_key,
        gemini_key=gemini_key,
        max_tokens=MAX_TOKENS_OVERVIEW,
        temperature=0.3,
    )
    if not result1.success:
        return SynthResult(success=False, error=f"LLM call 1 failed: {result1.attempts}")

    parsed = _extract_json(result1.text)
    if not parsed or not all(k in parsed for k in ("title", "summary", "overview_markdown")):
        return SynthResult(success=False, error="LLM call 1 returned malformed JSON")

    # ── Call 2: Background / History section ──
    bg_system = (
        "You are a neutral factual history writer. Given source articles about a "
        "current news topic, write ONLY the historical background section — how "
        "and why this situation developed over the last 5-10 years. Use only facts "
        "present in the sources or well-established public history. Respond with "
        "ONLY valid JSON, no other text."
    )
    bg_user = (
        f"Topic: {trend_query}\n\nSOURCE ARTICLES:\n{digest}\n\n"
        'Produce a JSON object with one key: "background_markdown" — the history/'
        'background section in Markdown, using "### " subheadings, covering the '
        "origin, causes, and timeline of development over the past 5-10 years. "
        "Neutral tone. Do not repeat the main overview content."
    )
    result2 = call_llm_with_fallback(
        region_key=region.key,
        system_prompt=bg_system,
        user_prompt=bg_user,
        groq_key=groq_key,
        gemini_key=gemini_key,
        max_tokens=MAX_TOKENS_BACKGROUND,
        temperature=0.3,
    )
    background_md = ""
    if result2.success:
        parsed_bg = _extract_json(result2.text)
        if parsed_bg and "background_markdown" in parsed_bg:
            background_md = parsed_bg["background_markdown"]
    if not background_md:
        logger.warning(f"[engine2_synth] background section failed for '{trend_query}' — continuing without it")

    title = parsed["title"].strip()
    summary = parsed["summary"].strip()
    overview_md = parsed["overview_markdown"].strip()
    wc = _word_count(summary, overview_md, background_md)

    return SynthResult(
        success=True,
        title=title,
        summary=summary,
        overview_md=overview_md,
        background_md=background_md.strip(),
        word_count=wc,
        llm_provider=result1.provider,
        llm_model=result1.model,
    )
