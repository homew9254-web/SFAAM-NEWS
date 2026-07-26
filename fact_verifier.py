"""
fact_verifier.py - SFAAM NEWS V26 (Trends Pipeline)

Zero-Hallucination Content Engine — Stage 3
============================================
Cross-Verification Engine: extract atomic facts from each scraped source
and keep facts that appear in 1+ sources (configurable via MIN_SOURCES_PER_FACT).

How it works:
1. Each source's body text is split into sentences (atomic fact candidates).
2. We normalize each sentence (lowercase, strip punctuation, remove stopwords).
3. We compute Jaccard similarity between sentence pairs across sources.
4. A sentence is "verified" if it has similarity >= SIMILARITY_THRESHOLD with at
   least one sentence from ANOTHER source (when MIN_SOURCES_PER_FACT >= 2),
   or simply kept as a single-source fact (when MIN_SOURCES_PER_FACT == 1).
5. We also count distinct sources that contributed to each verified fact
   (so the LLM can be told "this fact appears in N independent sources").

This is a deterministic, no-AI stage — pure algorithmic verification.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

from trends_scraper import ScrapedSource

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Sentence splitting + normalization
# ─────────────────────────────────────────────────────────────
# We split on sentence boundaries (.!? followed by space or end-of-string).
# We also split on common paragraph markers.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
# Pattern to detect junk sentences (URLs, copyright, bylines, etc.)
_JUNK_RE = re.compile(
    r"^(?:"
    r"sign up|subscribe|cookie|privacy policy|terms of|all rights reserved|"
    r"©|http|www\.|photo by|image by|getty images|reuters/|ap photo|"
    r"advertisement|sponsored|read more|click here|share this|"
    r"sign in|log in|register|newsletter|download the app|follow us|"
    r"reporting by|editing by|additional reporting|written by"
    r")",
    re.IGNORECASE,
)
# Boilerplate phrases that often appear in news articles and aren't facts
_BOILERPLATE_RE = re.compile(
    r"(?:"
    r"sign up for|subscribe to|this advertisement|has not been independently verified|"
    r"click here to|download our app|follow us on|newsletter|"
    r"please refresh|may have been updated|"
    r"this story has been shared|this story has been viewed|"
    r"©\s*\d{4}|all rights reserved"
    r")",
    re.IGNORECASE,
)


def split_sentences(text: str) -> list[str]:
    """Split text into clean, fact-candidate sentences.

    Filters out:
    - Sentences shorter than 25 chars or longer than 400 chars
    - Junk sentences (URLs, copyright, bylines, CTAs)
    - Boilerplate phrases
    """
    if not text:
        return []
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Split on sentence boundaries
    raw = _SENTENCE_SPLIT_RE.split(text)

    sentences: list[str] = []
    for s in raw:
        s = s.strip().strip("\"'")
        if not s:
            continue
        # Length filter — too short = noise, too long = paragraph
        if len(s) < 25 or len(s) > 400:
            continue
        # Junk filter
        if _JUNK_RE.search(s):
            continue
        if _BOILERPLATE_RE.search(s):
            continue
        # Skip if it's mostly a number (e.g. "5.")
        if re.fullmatch(r"[\d\s,.]+", s):
            continue
        sentences.append(s)
    return sentences


# ─────────────────────────────────────────────────────────────
# Stopwords for normalization (small list — just to remove noise)
# ─────────────────────────────────────────────────────────────
_STOPWORDS = frozenset(
    """
    a an the and or but if then else of to in on at by for with from into
    is are was were be been being have has had do does did will would shall
    should may might must can could this that these those it its their his
    her him he she they them we us i you your our my as also not no nor so
    than too very more most some any all each every other such only own same
    about above after again against before below between during further here
    how itself me myself off out over per same than there under up what when
    where which while who whom why with within without according said says
    reported added noted
    """.split()
)


def normalize_sentence(s: str) -> set[str]:
    """Return a set of significant tokens for Jaccard similarity."""
    tokens = re.findall(r"[a-z0-9]+", s.lower())
    return {t for t in tokens if len(t) > 2 and t not in _STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two token sets."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    union = len(a | b)
    return inter / union


# ─────────────────────────────────────────────────────────────
# Verified fact data model
# ─────────────────────────────────────────────────────────────
@dataclass
class VerifiedFact:
    """A single fact that has been confirmed by 2+ independent sources."""
    text: str                       # the original sentence (from the first matching source)
    source_urls: list[str]          # all source URLs that contain a similar sentence
    source_domains: list[str]       # distinct domains contributing to this fact
    confirmation_count: int         # len(source_domains)
    max_similarity: float           # highest similarity score observed


@dataclass
class VerificationResult:
    query: str
    total_input_sentences: int
    total_verified_facts: int
    facts: list[VerifiedFact]
    sources_used: list[str]         # all source URLs that contributed at least one fact
    rejected_sources: list[str]     # sources that contributed zero verified facts


# ─────────────────────────────────────────────────────────────
# Cross-verification engine
# ─────────────────────────────────────────────────────────────
# News articles from different outlets rarely use IDENTICAL wording for
# the same fact. Two journalists may both report "Trump imposed 100% tariffs
# on Canada" but write it as:
#   Source A: "President Trump announced a 100% tariff on Canadian goods."
#   Source B: "Trump threatened Canada with a 100 percent tariff on imports."
#
# To catch these semantically-equivalent facts, we use a LOW Jaccard
# threshold (0.30 = ~30% significant-token overlap).
#
# MIN_SOURCES_PER_FACT = 1 means: if we only have 1 source for a trend,
# we still extract facts (single-source facts). The confirmation_count
# field on each VerifiedFact tells the admin how many sources confirmed
# each fact — they can review accordingly.
SIMILARITY_THRESHOLD = 0.30  # Jaccard threshold for "same fact"
MIN_SOURCES_PER_FACT = 2     # V29 FIX: min 2 sources for a fact to be kept — prevents single-source repetition


def verify_facts(query: str, sources: list[ScrapedSource]) -> VerificationResult:
    """Cross-verify facts across multiple scraped sources.

    Args:
        query: the trending query (for logging)
        sources: list of ScrapedSource objects (typically 3-5)

    Returns:
        VerificationResult with only facts confirmed by >= MIN_SOURCES_PER_FACT sources.
    """
    logger.info(f"[Verify] Cross-verifying {len(sources)} sources for '{query[:50]}'")

    if not sources:
        return VerificationResult(
            query=query,
            total_input_sentences=0,
            total_verified_facts=0,
            facts=[],
            sources_used=[],
            rejected_sources=[],
        )

    # 1. Extract sentences from each source + tokenize
    per_source: list[tuple[ScrapedSource, list[str], list[set[str]]]] = []
    total_input = 0
    for src in sources:
        sents = split_sentences(src.full_text)
        tokens = [normalize_sentence(s) for s in sents]
        per_source.append((src, sents, tokens))
        total_input += len(sents)

    # 2. For each sentence in source[0..N], find matching sentences in OTHER sources
    verified: list[VerifiedFact] = []
    seen_texts: set[str] = set()  # de-dupe verified facts

    for i, (src_i, sents_i, tokens_i) in enumerate(per_source):
        for sent, tok in zip(sents_i, tokens_i):
            if not tok:
                continue

            # Find best match in every OTHER source
            matching_sources: list[tuple[ScrapedSource, float]] = []
            for j, (src_j, sents_j, tokens_j) in enumerate(per_source):
                if i == j:
                    continue
                best_score = 0.0
                for tok_j in tokens_j:
                    if not tok_j:
                        continue
                    score = jaccard(tok, tok_j)
                    if score > best_score:
                        best_score = score
                        if score >= 0.85:  # strong match — no need to keep scanning
                            break
                if best_score >= SIMILARITY_THRESHOLD:
                    matching_sources.append((src_j, best_score))

            # Dedupe matching sources by domain
            domain_map: dict[str, tuple[ScrapedSource, float]] = {}
            for s, score in matching_sources:
                if s.domain not in domain_map or score > domain_map[s.domain][1]:
                    domain_map[s.domain] = (s, score)

            # Include the source where the sentence originally came from
            all_domains = set(domain_map.keys())
            all_domains.add(src_i.domain)

            if len(all_domains) >= MIN_SOURCES_PER_FACT:
                # Build the verified fact
                source_urls = [src_i.url] + [s.url for s, _ in matching_sources]
                # Dedupe URLs but keep order
                seen_urls: set[str] = set()
                deduped_urls: list[str] = []
                for u in source_urls:
                    if u not in seen_urls:
                        seen_urls.add(u)
                        deduped_urls.append(u)

                # De-dupe the sentence itself (avoid near-identical verified facts).
                # V26 FIX: use a hash of the FULL sorted token set (was truncated
                # to 100 chars, which could collide for long sentences sharing
                # the same first ~15 tokens).
                import hashlib as _hashlib
                sent_key = _hashlib.sha1(
                    " ".join(sorted(tok)).encode()
                ).hexdigest()
                if sent_key in seen_texts:
                    continue
                seen_texts.add(sent_key)

                max_sim = max((s for _, s in matching_sources), default=0.0)
                verified.append(VerifiedFact(
                    text=sent,
                    source_urls=deduped_urls,
                    source_domains=list(all_domains),
                    confirmation_count=len(all_domains),
                    max_similarity=round(max_sim, 3),
                ))

    # 3. Sort by confirmation count (most sources first), then by similarity
    verified.sort(key=lambda f: (f.confirmation_count, f.max_similarity), reverse=True)

    # 4. Determine which sources contributed vs which were rejected
    sources_used_set = set()
    for f in verified:
        for u in f.source_urls:
            sources_used_set.add(u)
    sources_used = [s.url for s in sources if s.url in sources_used_set]
    rejected_sources = [s.url for s in sources if s.url not in sources_used_set]

    logger.info(
        f"[Verify] '{query[:50]}': {total_input} sentences → {len(verified)} verified facts "
        f"({len(sources_used)} sources used, {len(rejected_sources)} rejected)"
    )

    return VerificationResult(
        query=query,
        total_input_sentences=total_input,
        total_verified_facts=len(verified),
        facts=verified,
        sources_used=sources_used,
        rejected_sources=rejected_sources,
    )


# ─────────────────────────────────────────────────────────────
# Build the LLM prompt context from verified facts
# ─────────────────────────────────────────────────────────────
def build_fact_context(verification: VerificationResult, max_chars: int = 12000) -> str:
    """Render the verified facts as a clean text block that will be passed to
    the LLM. This is the ONLY input the LLM is allowed to use.

    The format is:
        # Trending Query: <query>
        # Sources Consulted: <N>
        # Verified Facts: <M>

        ## Fact 1 [confirmed by 3 sources: reuters.com, bbc.com, apnews.com]
        <fact sentence>

        ## Fact 2 [confirmed by 2 sources: ...]
        ...

        ## References
        1. <url 1>
        2. <url 2>
        ...
    """
    lines: list[str] = []
    lines.append(f"# Trending Query: {verification.query}")
    lines.append(f"# Sources Consulted: {len(set(verification.sources_used))}")
    lines.append(f"# Verified Facts: {verification.total_verified_facts}")
    lines.append("")
    lines.append("# INSTRUCTIONS FOR LLM: Use ONLY the facts below. Do NOT invent.")
    lines.append("# If you need more context, omit the section — DO NOT guess.")
    lines.append("")

    char_count = 0
    for i, fact in enumerate(verification.facts, 1):
        header = f"## Fact {i} [confirmed by {fact.confirmation_count} sources: {', '.join(fact.source_domains)}]"
        body = fact.text
        block = header + "\n" + body + "\n"
        if char_count + len(block) > max_chars:
            lines.append(f"... (truncated at {max_chars} chars; {len(verification.facts) - i + 1} more facts omitted)")
            break
        lines.append(block)
        char_count += len(block)

    # References section
    lines.append("## References")
    seen: set[str] = set()
    for f in verification.facts:
        for url in f.source_urls:
            if url not in seen:
                seen.add(url)
                lines.append(f"- {url}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# CLI for manual testing
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Mock sources
    src1 = ScrapedSource(
        url="https://www.bbc.com/news/world-12345678",
        domain="bbc.com",
        title="Test Article",
        snippet="",
        full_text="The United States imposed new tariffs on Canadian steel imports on Tuesday. "
                  "Canada responded with retaliatory measures. The dispute escalated tensions between the two neighbors. "
                  "Officials from both countries are scheduled to meet next week.",
    )
    src2 = ScrapedSource(
        url="https://www.reuters.com/world/us/test-article-2026-07-17",
        domain="reuters.com",
        title="Test Article",
        snippet="",
        full_text="The US announced new tariffs on Canadian steel. "
                  "Canada announced retaliatory measures in response. "
                  "Talks between the two nations are scheduled for next week. "
                  "Markets reacted negatively to the news.",
    )
    src3 = ScrapedSource(
        url="https://apnews.com/article/test-12345",
        domain="apnews.com",
        title="Test Article",
        snippet="",
        full_text="According to officials, the new tariffs will take effect next month. "
                  "Spokespeople declined to comment on specific dollar amounts.",
    )

    result = verify_facts("US Canada tariffs", [src1, src2, src3])
    print(f"\n{result.total_input_sentences} input sentences → {result.total_verified_facts} verified facts")
    for f in result.facts:
        print(f"\n  [{f.confirmation_count} sources] {f.text}")
        for u in f.source_urls:
            print(f"    - {u}")

    print("\n" + "=" * 60)
    print("LLM context:")
    print("=" * 60)
    print(build_fact_context(result))
