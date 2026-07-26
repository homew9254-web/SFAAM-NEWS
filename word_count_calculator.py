"""
word_count_calculator.py - SFAAM Automated News Engine (V30 / TRD v1.0)
=======================================================================
Step C: Algorithmic Word Count & Depth Calculation (TRD Section 3)
-------------------------------------------------------------------
    "The system evaluates the size and complexity of the verified JSON
     fact pool to choose a target length dynamically:

       • Small Fact Pool  (3-5 unique core data points):    400-600 words
       • Medium Fact Pool (6-12 unique core data points):   800-1200 words
       • Large Fact Pool  (13+ interconnected data points
         / Major Global Events):                            1500-2500+ words

     The system forces the LLM to deep-dive into the implications,
     maximizing analytical length without adding fluff or fabrications."

This module is intentionally pure (no I/O, no logging side effects
beyond debug). It takes a fact count and returns:
  • Target word count range (min, max)
  • Tier label ("small" | "medium" | "large")
  • Recommended max_tokens for the LLM call (to avoid mid-sentence cut-off)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


# ─────────────────────────────────────────────────────────────
# Tier definitions (TRD Section 3, Step C)
# ─────────────────────────────────────────────────────────────
# Each tier defines:
#   • fact_range:  (min, max_inclusive) fact count
#   • word_range:  (min, max) target word count
#   • max_tokens:  recommended LLM max_tokens (≈1.5 words/token to leave
#                  headroom for prompt + avoid cut-off mid-sentence)
#   • label:       human-readable tier name


@dataclass(frozen=True)
class WordCountTier:
    label: str
    fact_min: int          # inclusive
    fact_max: int          # inclusive (use 9999 for "no upper bound")
    word_min: int
    word_max: int
    max_tokens: int        # recommended LLM max_tokens for this tier

    def in_range(self, fact_count: int) -> bool:
        return self.fact_min <= fact_count <= self.fact_max


TIERS: list[WordCountTier] = [
    # V32: Increased all word counts for "world-class" depth.
    # Old: small=400-600, medium=800-1200, large=1500-2500
    # New: small=800-1200, medium=1500-2500, large=3000-5000
    # Reasoning: World-class news sites (BBC, Reuters long-reads, NYT) average
    # 1500-3000 words for breaking news and 4000-8000 for in-depth analysis.
    # The old "small" tier (400-600) was barely a press release — readers left.
    # New floor is 800 words (still concise for low-fact stories).

    # Small: 3-5 facts → 800-1200 words (was 400-600).
    # max_tokens=4000 leaves headroom for title + summary + body + JSON overhead.
    WordCountTier(
        label="small",
        fact_min=3,
        fact_max=5,
        word_min=800,
        word_max=1200,
        max_tokens=4000,
    ),
    # Medium: 6-12 facts → 1500-2500 words (was 800-1200).
    # max_tokens=8000 leaves room for all 4 JSON fields + deep analysis.
    WordCountTier(
        label="medium",
        fact_min=6,
        fact_max=12,
        word_min=1500,
        word_max=2500,
        max_tokens=8000,
    ),
    # Large: 13+ facts → 3000-5000+ words (was 1500-2500).
    # V32.1 BUGFIX: max_tokens was 16000, which EXCEEDS Groq's hard output
    # cap of 8192 for llama-3.3-70b/llama-3.1-8b AND Gemini 2.0 Flash's 8192.
    # The API silently truncates or returns a 422 → large articles either
    # cut off mid-sentence or never generate at all.
    # Fix: cap at 8000 (safe under both providers) and instruct the LLM
    # in the prompt to chunk deep-dive analysis across multiple H2 sections
    # so 5000-word articles fit within 8000 tokens (~1.6 words/token).
    WordCountTier(
        label="large",
        fact_min=13,
        fact_max=9999,
        word_min=3000,
        word_max=5000,
        max_tokens=8000,
    ),
]

# Below 2 facts, the TRD doesn't specify a tier — we treat it as
# "insufficient" (the fact extractor should have already rejected this
# case via MIN_FACTS_PER_ARTICLE).
# V32.1: Lowered from 3 → 2 so high-traffic breaking-news moments (1-2 facts
# confirmed across multiple sources) still publish a short brief instead of
# being silently dropped. Quality control will still gate anything below
# the small-tier minimum.
INSUFFICIENT_THRESHOLD = 2


@dataclass
class WordCountCalculation:
    """Result of a word-count calculation. Immutable snapshot."""
    fact_count: int
    tier: WordCountTier
    target_min: int
    target_max: int
    target_mid: int        # midpoint — useful as a single target value
    max_tokens: int
    is_sufficient: bool    # False if fact_count < INSUFFICIENT_THRESHOLD


def calculate_word_count(fact_count: int) -> WordCountCalculation:
    """Calculate the target word count and max_tokens for a given fact count.

    Args:
        fact_count: Number of unique verified facts (cross-source confirmed).

    Returns:
        WordCountCalculation with tier + ranges.
    """
    if fact_count < INSUFFICIENT_THRESHOLD:
        # Insufficient facts — return a degenerate tier (caller should skip)
        return WordCountCalculation(
            fact_count=fact_count,
            tier=TIERS[0],  # use small tier as fallback shape
            target_min=0,
            target_max=0,
            target_mid=0,
            max_tokens=0,
            is_sufficient=False,
        )

    for tier in TIERS:
        if tier.in_range(fact_count):
            mid = (tier.word_min + tier.word_max) // 2
            return WordCountCalculation(
                fact_count=fact_count,
                tier=tier,
                target_min=tier.word_min,
                target_max=tier.word_max,
                target_mid=mid,
                max_tokens=tier.max_tokens,
                is_sufficient=True,
            )

    # Should never reach here (large tier has fact_max=9999), but defensive
    raise RuntimeError(f"No tier matched fact_count={fact_count}")


def get_prompt_instruction(calc: WordCountCalculation) -> str:
    """Return a snippet of prompt text instructing the LLM on target length.

    Paste this into the user prompt so the LLM knows exactly how long the
    article should be.

    Per TRD Section 6 (Token Windows Management): "instruct the prompt to
    avoid mid-sentence cut-offs."
    """
    if not calc.is_sufficient:
        return (
            f"WARNING: Only {calc.fact_count} verified facts available. "
            f"Minimum 3 required. Do NOT write the article."
        )

    return (
        f"TARGET LENGTH: {calc.target_min}-{calc.target_max} words "
        f"(tier: {calc.tier.label}, based on {calc.fact_count} verified facts).\n"
        f"LENGTH DISCIPLINE: Scale your output to match the verified fact "
        f"volume. Do NOT pad with filler. Do NOT repeat facts. Do NOT cut "
        f"off mid-sentence — finish every sentence and section. If you are "
        f"running low on facts, end the article cleanly rather than padding.\n"
        f"DEPTH: For the '{calc.tier.label}' tier, focus on "
        + (
            "concise factual coverage of each data point."
            if calc.tier.label == "small"
            else "thorough analysis of each fact plus its implications."
            if calc.tier.label == "medium"
            else "deep-dive analysis: implications, historical parallels, "
                 "stakeholder positions, and forward-looking context. "
                 "Maximize analytical depth without fabrication."
        )
    )


# ─────────────────────────────────────────────────────────────
# CLI for manual testing
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    print("=" * 70)
    print("SFAAM Word Count Calculator — TRD Section 3, Step C")
    print("=" * 70)
    print()
    print("Tier table:")
    print(f"  {'Tier':8s} {'Facts':12s} {'Words':14s} {'max_tokens':>12s}")
    print("  " + "-" * 50)
    for t in TIERS:
        fact_range = f"{t.fact_min}-{t.fact_max if t.fact_max < 9999 else '∞'}"
        word_range = f"{t.word_min}-{t.word_max}+"
        print(f"  {t.label:8s} {fact_range:12s} {word_range:14s} {t.max_tokens:>12d}")
    print()

    test_counts = [int(x) for x in sys.argv[1:]] or [2, 3, 5, 8, 12, 15, 20, 50]
    for n in test_counts:
        calc = calculate_word_count(n)
        print(f"  {n:3d} facts → tier={calc.tier.label:6s} "
              f"target={calc.target_min:4d}-{calc.target_max:4d} words, "
              f"max_tokens={calc.max_tokens:5d}, "
              f"sufficient={calc.is_sufficient}")
    print()
    print("Example prompt instruction for 10 facts:")
    print("-" * 70)
    print(get_prompt_instruction(calculate_word_count(10)))
    print("-" * 70)
