"""
trends_writer.py - SFAAM NEWS V26 (Trends Pipeline)

Zero-Hallucination Content Engine — Stage 4
============================================
LLM Synthesis: pass verified facts to the LLM with a STRICT system prompt
that forbids hallucination, filler, or invention.

Supports two providers (same as existing ai_writer.py):
  1. Groq (Llama 3.3 70B)  — preferred, free
  2. Gemini (2.0 Flash)    — fallback, free

If neither API key is set, the writer falls back to a deterministic
"fact-listing" mode that simply formats the verified facts as a readable
article (no LLM call). This keeps the pipeline working without API keys,
though the output is less polished.

Wikipedia-style article structure (per PDF spec):
    - Lead Summary
    - Background
    - Detailed Facts
    - Timeline
    - References
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime

from fact_verifier import VerificationResult, build_fact_context

logger = logging.getLogger(__name__)


# V31.1: Title variation suffixes for trends pipeline.
# The old _build_title() was deterministic — same query always produced the
# same title. This meant that if the same trend recurred after the 7-day
# dedup window, the new article would have an IDENTICAL title to the old one.
# These suffixes add variation so that even the same query produces different
# titles on different runs. The actual dedup is still handled by
# title_uniqueness.ensure_unique_title() at insert time (which appends
# " (2)", " (3)" etc. if needed), but these suffixes reduce the chance
# of that being necessary AND make the titles more editorially interesting.
_TITLE_SUFFIXES = [
    ": A Comprehensive Analysis",
    ": What We Know So Far",
    ": Key Facts and Context",
    ": An In-Depth Look",
    ": The Full Picture",
    ": Explained",
    ": A Detailed Breakdown",
    ": What You Need to Know",
    ": Context and Implications",
    ": A Closer Look",
]


# ─────────────────────────────────────────────────────────────
# STRICT ZERO-HALLUCINATION SYSTEM PROMPT (per PDF V3 spec)
# ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a veteran staff writer at SFAAM NEWS covering a trending topic. Write a comprehensive, deeply engaging article using ONLY the provided verified facts.

ABSOLUTE RULES (violation = system failure):
1. DO NOT hallucinate, guess, or invent ANY information.
2. If a detail is not in the source text, OMIT it entirely. Do NOT paraphrase, do NOT speculate, do NOT infer.
3. Do NOT use filler words or phrases to inflate word count.
4. Do NOT add your own knowledge about the topic, even if it is common knowledge.
5. Do NOT add disclaimers like "according to reports" — just state the fact directly.
6. Use ONLY the verified facts provided below. Each fact has been confirmed by multiple independent sources.
7. If you need a fact that is not in the source text, simply omit that section.

═══════════════════════════════════════════
ENGAGEMENT & STRUCTURE (V32.1 — READER ENGAGEMENT)
═══════════════════════════════════════════
Pure Wikipedia-style encyclopedic prose loses readers in 30 seconds. Instead,
write with the rhythm and pull of long-form journalism while still being
strictly factual.

A) HOOK: Open with a single-sentence punchy paragraph that creates tension,
   curiosity, or stakes. Never start with "This article is about..." or a
   generic definition.

B) NUT GRAF: By the 3rd paragraph, tell the reader WHY this trend matters
   right now — what changes for them, their region, or the world.

C) BURSTINESS: Mix very short sentences (3-7 words) with long ones (25-40
   words). At least 20% of sentences should be under 8 words. Use sentence
   fragments for impact. Example: "Then nothing."

D) FORBIDDEN PHRASES (AI-detector magnets — never use):
   "Furthermore", "Moreover", "Additionally", "In addition", "In conclusion",
   "To summarize", "It is important to note", "In today's world", "When it
   comes to", "At the end of the day", "Delve into", "A testament to",
   "Comprehensive", "Robust", "Seamless", "Leverage", "Firstly", "Secondly".

E) STRONG VERBS: Use concrete verbs ("slammed", "scrambled", "unspooled",
   "snagged") over generic ones ("said", "went", "made") where the facts
   support the connotation.

ARTICLE STRUCTURE (Markdown headings):
## Lead Summary
2-3 paragraphs. Open with the hook. Include the nut graf. Summarize the most
important facts. DO NOT start with a dictionary-style definition.

## Background
Context that helps the reader understand the topic. Use ONLY facts from the
source text. If no background facts are available, OMIT this section entirely
— do NOT write "Background context as derived from the verified facts below"
or any other filler placeholder. Empty placeholder sections hurt SEO and
reader trust.

## Detailed Facts
The bulk of the article. Organize related facts into sub-sections with ###
headings (use the facts' themes as subsection titles). Each fact should be
its own paragraph or part of a paragraph. Add a one-line "why this matters"
after each major fact block when the implications are clear from the facts.

## By the Numbers
If the verified facts contain 3+ quantitative data points, include this
section as a bullet list. Each bullet: "**[Number]** — [one-line explanation
of why it matters]." If fewer than 3 numbers, OMIT the section.

## Timeline
A chronological list of events mentioned in the source text. Format:
- **[Date/Time if mentioned]**: Event description
If no dates are mentioned in the verified facts, OMIT this section entirely.
Do NOT write "Timeline information not available in verified sources." —
empty placeholder lines hurt SEO.

## What Happens Next
A forward-looking section outlining upcoming milestones, decisions, or
expected developments mentioned in or directly implied by the facts. If the
facts genuinely don't support any forward-looking statements, OMIT the
section rather than padding.

## Frequently Asked Questions
4-6 REAL questions a curious reader would ask about this trending topic
(NOT generic "what is this article about"). Answer each in 2-3 sentences
using ONLY verified facts. This captures Google "People Also Ask" traffic.
If the facts do not support at least 4 real questions, OMIT the section.

## References
List every source URL that contributed to this article. Format:
1. [Domain](URL)
2. [Domain](URL)

LENGTH:
- Minimum 1000-1500 words (assuming enough verified facts are provided).
- Maximum 8000 words (only if the verified facts support that length).
- Length must scale DYNAMICALLY with the volume of verified facts. DO NOT pad.

FORMATTING:
- Use Markdown: ## for main sections, ### for sub-sections, **bold** for key terms.
- Use em-dashes (—) and parenthetical asides for editorial voice (sparingly).
- Vary sentence openers — never start three sentences in a row with the same word.
- Use specific numbers from the facts wherever possible.

OUTPUT:
The article body only. No preface, no meta-commentary, no "here is your article".
"""


# ─────────────────────────────────────────────────────────────
# User prompt template
# ─────────────────────────────────────────────────────────────
USER_PROMPT_TEMPLATE = """Write a Wikipedia-style article about the trending topic: "{query}"

Use ONLY the verified facts below. Each fact has been cross-verified across multiple authoritative news sources.

{fact_context}

Remember: ZERO hallucination. If something is not in the facts above, do not write it.
Write the article now."""


# ─────────────────────────────────────────────────────────────
# Provider clients
# ─────────────────────────────────────────────────────────────
# V26 FIX: use a LIST of Groq models (matches ai_writer.py resilience pattern)
# so a single deprecated/decommissioned model doesn't break the whole pipeline.
GROQ_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
GEMINI_MODEL = "gemini-2.0-flash"


def _call_groq(api_key: str, system_prompt: str, user_prompt: str, max_tokens: int = 6000) -> str:
    """Call Groq API. Tries each model in GROQ_MODELS in order.
    Returns the article text, or empty string on failure."""
    from groq import Groq
    client = Groq(api_key=api_key)
    for model in GROQ_MODELS:
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.2,  # low temp = less creative = more factual
                top_p=0.85,
            )
            content = (resp.choices[0].message.content or "").strip()
            if content:
                return content
        except Exception as e:
            logger.warning(f"[TrendsWriter] Groq {model} failed: {type(e).__name__}: {e}")
            continue
    return ""


def _call_gemini(api_key: str, system_prompt: str, user_prompt: str, max_tokens: int = 6000) -> str:
    """Call Gemini API. Returns the article text, or empty string on failure."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            GEMINI_MODEL,
            system_instruction=system_prompt,
            generation_config={
                "max_output_tokens": max_tokens,
                "temperature": 0.2,
                "top_p": 0.85,
            },
        )
        resp = model.generate_content(user_prompt)
        # Gemini may return parts
        if hasattr(resp, "text") and resp.text:
            return resp.text.strip()
        if hasattr(resp, "candidates") and resp.candidates:
            parts = resp.candidates[0].content.parts
            return "".join(p.text for p in parts if hasattr(p, "text")).strip()
        return ""
    except Exception as e:
        logger.warning(f"[TrendsWriter] Gemini call failed: {type(e).__name__}: {e}")
        return ""


# ─────────────────────────────────────────────────────────────
# Fallback: no-LLM mode (deterministic fact-listing article)
# ─────────────────────────────────────────────────────────────
def _fallback_article(query: str, verification: VerificationResult) -> str:
    """Build a basic article from verified facts WITHOUT an LLM call.

    This is used when no AI API keys are configured. The output is less
    polished but still 100% factual (it's just a structured list of facts).
    """
    lines: list[str] = []
    lines.append("## Lead Summary")
    lines.append("")
    lines.append(f"This article compiles verified facts about **{query}**, "
                 f"cross-confirmed across {len(set(verification.sources_used))} "
                 f"authoritative news sources. All facts below have been verified "
                 f"by appearing in at least two independent sources.")
    lines.append("")
    lines.append("## Background")
    lines.append("")
    lines.append("Background context as derived from the verified facts below.")
    lines.append("")
    lines.append("## Detailed Facts")
    lines.append("")
    for i, fact in enumerate(verification.facts, 1):
        lines.append(f"### Fact {i} (confirmed by {fact.confirmation_count} sources)")
        lines.append("")
        lines.append(fact.text)
        lines.append("")
        lines.append(f"*Sources: {', '.join(fact.source_domains)}*")
        lines.append("")
    lines.append("## Timeline")
    lines.append("")
    lines.append("Timeline information not available in verified sources.")
    lines.append("")
    lines.append("## References")
    lines.append("")
    seen: set[str] = set()
    seen_domains: set[str] = set()
    n = 0
    for f in verification.facts:
        for url in f.source_urls:
            if url not in seen:
                seen.add(url)
                n += 1
                # V26 FIX: derive domain from the URL itself (not from
                # f.source_domains[0] which may belong to a different URL).
                domain = "source"
                try:
                    from urllib.parse import urlparse
                    host = urlparse(url).hostname or ""
                    if host:
                        if host.startswith("www."):
                            host = host[4:]
                        domain = host
                except Exception:
                    pass
                lines.append(f"{n}. [{domain}]({url})")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────
@dataclass
class TrendsArticle:
    title: str
    content: str
    summary: str
    word_count: int
    references: list[str]
    provider: str            # "groq" | "gemini" | "fallback"
    fact_context: str        # the context block passed to the LLM (for audit)


def _build_title(query: str) -> str:
    """Generate a clean article title from the trend query.

    V31.1: Now appends a random editorial suffix from _TITLE_SUFFIXES.
    This prevents the same trend query from producing identical titles
    across runs (which caused duplicate-title bugs when the same trend
    recurred after the 7-day dedup window)."""
    # Title-case the query, but keep small words lowercase (Wikipedia style)
    words = query.split()
    if not words:
        return query
    small = {"a", "an", "the", "of", "in", "on", "at", "by", "for", "and", "or", "but", "to", "with"}
    titled: list[str] = []
    for i, w in enumerate(words):
        wl = w.lower()
        if i > 0 and wl in small:
            titled.append(wl)
        else:
            titled.append(w[:1].upper() + w[1:].lower())
    title = " ".join(titled)
    # Strip trailing punctuation
    title = title.rstrip(".!?,;:")
    # V32.1 BUGFIX: Replaced `hash(query)` with `hashlib.md5(query).`
    # Python's built-in `hash()` is randomized per process via PYTHONHASHSEED
    # for security — the SAME query gets DIFFERENT hashes in different
    # worker processes, which breaks suffix rotation determinism. md5 is
    # deterministic and fast for short strings.
    query_hash = int(hashlib.md5(query.encode("utf-8")).hexdigest(), 16) & 0xFFFFFFFF
    rng = random.Random(query_hash + int(datetime.utcnow().timestamp()))
    suffix = rng.choice(_TITLE_SUFFIXES)
    return f"{title}{suffix}"


def _extract_summary(content: str) -> str:
    """Pull a 1-2 sentence summary from the article's Lead Summary section."""
    if not content:
        return ""
    # Find "## Lead Summary" section
    m = re.search(r"##\s*Lead Summary\s*\n+(.*?)(?=\n##\s|$)", content, re.DOTALL)
    if not m:
        # Fallback: first non-empty paragraph
        for para in content.split("\n\n"):
            para = para.strip()
            if para and not para.startswith("#"):
                return para[:300]
        return content[:300]
    lead = m.group(1).strip()
    # Take first 2 sentences
    sentences = re.split(r"(?<=[.!?])\s+", lead)
    summary = " ".join(sentences[:2]).strip()
    return summary[:400] if summary else lead[:300]


def _extract_references(content: str) -> list[str]:
    """Extract reference URLs from the article's References section."""
    if not content:
        return []
    refs: list[str] = []
    # Find References section
    m = re.search(r"##\s*References?\s*\n+(.*?)(?=\n##\s|$)", content, re.DOTALL)
    if not m:
        return refs
    ref_block = m.group(1)
    # Match markdown links: [text](url)
    for mm in re.finditer(r"\[(?:[^\]]+)\]\((https?://[^)]+)\)", ref_block):
        refs.append(mm.group(1))
    # Or plain URLs
    if not refs:
        for mm in re.finditer(r"(https?://[^\s)]+)", ref_block):
            refs.append(mm.group(1).rstrip(".,;"))
    return refs


def write_trends_article(
    query: str,
    verification: VerificationResult,
    *,
    groq_key: str = "",
    gemini_key: str = "",
) -> TrendsArticle:
    """Generate a Wikipedia-style article from verified facts.

    Args:
        query: the trending search query
        verification: VerificationResult from fact_verifier
        groq_key: optional Groq API key (preferred)
        gemini_key: optional Gemini API key (fallback)

    Returns:
        TrendsArticle with title, content, summary, references, and audit metadata.
    """
    title = _build_title(query)
    fact_context = build_fact_context(verification)
    user_prompt = USER_PROMPT_TEMPLATE.format(query=query, fact_context=fact_context)

    content = ""
    provider = "fallback"

    # Try Groq first
    if groq_key and groq_key != "your_groq_key":
        logger.info(f"[TrendsWriter] Using Groq ({GROQ_MODELS[0]} + fallbacks) for '{query[:50]}'")
        content = _call_groq(groq_key, SYSTEM_PROMPT, user_prompt, max_tokens=6000)
        if content:
            provider = "groq"

    # Fallback to Gemini
    if not content and gemini_key and gemini_key != "your_gemini_key":
        logger.info(f"[TrendsWriter] Using Gemini ({GEMINI_MODEL}) for '{query[:50]}'")
        content = _call_gemini(gemini_key, SYSTEM_PROMPT, user_prompt, max_tokens=6000)
        if content:
            provider = "gemini"

    # Final fallback: deterministic fact-listing article
    if not content:
        logger.warning(f"[TrendsWriter] No LLM available — using fallback article for '{query[:50]}'")
        content = _fallback_article(query, verification)
        provider = "fallback"

    # Compute metadata
    word_count = len(content.split())
    summary = _extract_summary(content)
    references = _extract_references(content)

    # If references extraction failed, use the sources from verification
    if not references:
        seen: set[str] = set()
        for f in verification.facts:
            for url in f.source_urls:
                if url not in seen:
                    seen.add(url)
                    references.append(url)

    logger.info(
        f"[TrendsWriter] '{query[:50]}': {word_count} words, {len(references)} refs, provider={provider}"
    )

    return TrendsArticle(
        title=title,
        content=content,
        summary=summary,
        word_count=word_count,
        references=references,
        provider=provider,
        fact_context=fact_context,
    )


# ─────────────────────────────────────────────────────────────
# CLI for manual testing
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from fact_verifier import VerifiedFact

    # Mock verification result
    mock_facts = [
        VerifiedFact(
            text="The United States imposed new tariffs on Canadian steel imports on Tuesday.",
            source_urls=["https://bbc.com/news/1", "https://reuters.com/2", "https://apnews.com/3"],
            source_domains=["bbc.com", "reuters.com", "apnews.com"],
            confirmation_count=3,
            max_similarity=0.85,
        ),
        VerifiedFact(
            text="Canada responded with retaliatory tariffs on US goods.",
            source_urls=["https://bbc.com/news/1", "https://reuters.com/2"],
            source_domains=["bbc.com", "reuters.com"],
            confirmation_count=2,
            max_similarity=0.72,
        ),
    ]
    mock_verification = VerificationResult(
        query="US Canada trade tariffs",
        total_input_sentences=10,
        total_verified_facts=2,
        facts=mock_facts,
        sources_used=["https://bbc.com/news/1", "https://reuters.com/2", "https://apnews.com/3"],
        rejected_sources=[],
    )

    article = write_trends_article(
        mock_verification.query,
        mock_verification,
        groq_key=os.getenv("GROQ_KEY_WORLD", ""),
        gemini_key=os.getenv("GEMINI_KEY_WORLD", ""),
    )
    print(f"\nTitle: {article.title}")
    print(f"Provider: {article.provider}")
    print(f"Words: {article.word_count}")
    print(f"References: {len(article.references)}")
    print()
    print("=" * 70)
    print(article.content[:2000])
    print("=" * 70)
    if len(article.content) > 2000:
        print(f"... ({len(article.content) - 2000} more chars)")
