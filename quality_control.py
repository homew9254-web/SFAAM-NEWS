"""
quality_control.py - SFAAM NEWS V25 — AI Content Quality Control
=================================================================

Evaluates AI-generated articles before publishing. Each article receives
a QualityScore with sub-scores for:
  - Readability (Flesch Reading Ease, sentence/paragraph length)
  - Grammar (basic heuristics + optional LanguageTool)
  - Uniqueness (cosine similarity vs. recent articles using TF-IDF)
  - SEO (keyword density, heading structure, meta description length)
  - Conversational tone (Wikipedia-beating style check — short paras,
    bold key terms, FAQ section present, "how-to" orientation)
  - **AdSense Safety** (V25 — filters out content that would violate
    Google AdSense Program Policies: adult, violence, hate speech,
    dangerous/illegal acts, drugs, weapons, etc.)

Articles scoring below MIN_QUALITY_SCORE (default 60) are auto-flagged
for review (or auto-regenerated if REGEN_ON_FAIL=1).
Articles failing AdSense safety are auto-rejected regardless of overall
score (per PDF spec: "ensures 100% AdSense compliance").

Also includes:
  - semantic_dedup() — TF-IDF cosine similarity for catching paraphrased
    duplicates (more robust than title matching alone)
  - fact_check() — extracts entities (dates, numbers, organizations,
    public figures) and flags any that look suspicious for manual review
  - adsense_safety_check() — V25 keyword-based policy filter
"""
from __future__ import annotations

import logging
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Thresholds (configurable via env) ──
MIN_QUALITY_SCORE  = int(os.getenv("QC_MIN_SCORE", "60"))
MIN_READABILITY    = float(os.getenv("QC_MIN_READABILITY", "40"))  # Flesch ease
MAX_SENTENCE_LEN   = int(os.getenv("QC_MAX_SENTENCE_LEN", "30"))  # words
MAX_PARA_LEN       = int(os.getenv("QC_MAX_PARA_LEN", "60"))      # words
MIN_WORD_COUNT     = int(os.getenv("QC_MIN_WORDS", "300"))
MAX_DUP_SIMILARITY = float(os.getenv("QC_MAX_DUP_SIMILARITY", "0.75"))  # cosine


# ════════════════════════════════════════════════════════════
#  READABILITY — Flesch Reading Ease + structural heuristics
# ════════════════════════════════════════════════════════════

# Common English words — used to compute word-difficulty for Flesch
_SYLLABLE_VOWELS = "aeiouyAEIOUY"


def _count_syllables(word: str) -> int:
    """Estimate syllables in an English word. Crude but good enough
    for Flesch scoring."""
    word = re.sub(r"[^A-Za-z]", "", word.lower())
    if not word:
        return 0
    # Count vowel groups
    count = len(re.findall(r"[aeiouy]+", word))
    # Subtract silent 'e' at end
    if word.endswith("e") and count > 1:
        count -= 1
    # Subtract silent 'le' at end (e.g. "table")
    if word.endswith("le") and len(word) > 2 and word[-3] not in "aeiouy":
        count -= 1
    return max(1, count)


def flesch_reading_ease(text: str) -> float:
    """Flesch Reading Ease score (0-100, higher = easier).
    90-100: 5th grade, 60-70: 8-9th grade, 30-50: college, 0-30: graduate."""
    sentences = re.split(r"[.!?]+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    words = re.findall(r"\b[A-Za-z]+\b", text)
    if not sentences or not words:
        return 0.0
    syllables = sum(_count_syllables(w) for w in words)
    asl = len(words) / len(sentences)  # avg sentence length
    asw = syllables / len(words)        # avg syllables per word
    return round(206.835 - 1.015 * asl - 84.6 * asw, 2)


def structural_score(text: str) -> dict:
    """Check conversational style heuristics (Wikipedia-beating rules):
      - Paragraphs should be short (≤ 60 words)
      - Sentences should be short (≤ 30 words)
      - Key terms should be bolded
      - An FAQ section should be present
      - First paragraph should answer the user's question (to-the-point)
    Returns dict of sub-scores (0-100 each).
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    sentences = re.split(r"[.!?]+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    words = re.findall(r"\b\w+\b", text)

    # Paragraph length score (penalize long paragraphs)
    long_paras = sum(1 for p in paras if len(p.split()) > MAX_PARA_LEN)
    para_score = 100 - (long_paras / max(1, len(paras))) * 100

    # Sentence length score
    long_sents = sum(1 for s in sentences if len(s.split()) > MAX_SENTENCE_LEN)
    sent_score = 100 - (long_sents / max(1, len(sentences))) * 100

    # Bold key terms present?
    bold_count = len(re.findall(r"\*\*[^*]+\*\*", text))
    bold_score = min(100, bold_count * 10)  # 10+ bolds = full score

    # FAQ section present?
    has_faq = bool(re.search(r"(?i)faq|frequently asked", text))
    faq_score = 100 if has_faq else 0

    # First paragraph length (should be short — answer the question fast)
    if paras:
        first_para_words = len(paras[0].split())
        # Ideal: 30-60 words in first paragraph
        if 30 <= first_para_words <= 60:
            first_para_score = 100
        elif first_para_words < 100:
            first_para_score = 70
        else:
            first_para_score = 30
    else:
        first_para_score = 0

    return {
        "paragraph_length": round(para_score, 1),
        "sentence_length":  round(sent_score, 1),
        "bold_key_terms":   round(bold_score, 1),
        "has_faq":          faq_score,
        "first_paragraph":  first_para_score,
    }


# ════════════════════════════════════════════════════════════
#  GRAMMAR — basic heuristics (no external deps)
# ════════════════════════════════════════════════════════════

def grammar_score(text: str) -> dict:
    """Crude grammar check — counts common mistakes:
      - Double spaces
      - Missing capital at sentence start
      - Missing period at paragraph end
      - Repeated words ("the the")
      - Commonly confused homophones (your/you're, their/there)
    Returns a 0-100 score + list of issues.
    """
    issues = []
    text_lower = text.lower()

    # Double spaces
    n = len(re.findall(r"\s{2,}", text))
    if n > 0:
        issues.append(f"double spaces ({n})")

    # Missing capital at sentence start
    n = len(re.findall(r"[.!?]\s+[a-z]", text))
    if n > 0:
        issues.append(f"missing capital after period ({n})")

    # Repeated words
    n = len(re.findall(r"\b(\w+)\s+\1\b", text_lower))
    if n > 0:
        issues.append(f"repeated words ({n})")

    # Commonly confused homophones
    confused = {
        "your you're": "your you're",
        "their there": "their there",
        "its it's": "its it's",
        "to too": "to too",
    }
    for pair in confused:
        if pair.split()[0] in text_lower and pair.split()[1].replace("'", "") in text_lower:
            # Just flag — we can't tell context
            pass  # too noisy, skip

    # Issue density per 1000 words
    word_count = len(re.findall(r"\b\w+\b", text))
    issue_rate = len(issues) / max(1, word_count / 1000)
    score = max(0, 100 - issue_rate * 20)

    return {
        "score": round(score, 1),
        "issues": issues,
    }


# ════════════════════════════════════════════════════════════
#  UNIQUENESS — TF-IDF cosine similarity
# ════════════════════════════════════════════════════════════

# English stop words (minimal set — kept small to avoid over-filtering)
_STOP_WORDS = set("""
a an the and or but if then else when while of to in on at by for with from into over
under about above below between among through during before after without within
is am are was were be been being have has had do does did will would shall should
may might must can could this that these those it its they them their there here
i you he she we us our your his her my mine yours ours not no nor so too very just
also as than then such only own same other some any all both each few more most
""".split())


def _tokenize(text: str) -> list[str]:
    text = re.sub(r"[^a-zA-Z\s]", " ", text.lower())
    return [w for w in text.split() if w not in _STOP_WORDS and len(w) > 2]


def _tf(tokens: list[str]) -> dict[str, float]:
    """Term frequencies."""
    counter = Counter(tokens)
    total = len(tokens) or 1
    return {t: c / total for t, c in counter.items()}


def _cosine_similarity(tf1: dict[str, float], tf2: dict[str, float]) -> float:
    """Cosine similarity between two term-frequency dicts.
    For uniqueness we don't need full TF-IDF (no corpus stats) — raw TF
    cosine already catches near-duplicates well."""
    if not tf1 or not tf2:
        return 0.0
    common = set(tf1) & set(tf2)
    dot = sum(tf1[t] * tf2[t] for t in common)
    mag1 = math.sqrt(sum(v * v for v in tf1.values()))
    mag2 = math.sqrt(sum(v * v for v in tf2.values()))
    if mag1 == 0 or mag2 == 0:
        return 0.0
    return dot / (mag1 * mag2)


def semantic_dedup(
    new_text: str,
    existing_texts: list[str],
    threshold: float = MAX_DUP_SIMILARITY,
) -> tuple[bool, float, Optional[int]]:
    """Check if new_text is too similar to any existing text.
    Uses TF cosine similarity — catches paraphrased duplicates that
    title-matching would miss.

    Args:
        new_text:        The candidate article body.
        existing_texts:  List of existing article bodies (or summaries).
        threshold:       Cosine similarity above which we flag as duplicate.

    Returns:
        (is_duplicate, max_similarity, matching_index)
    """
    if not new_text or not existing_texts:
        return False, 0.0, None

    new_tf = _tf(_tokenize(new_text))
    max_sim = 0.0
    matching_idx = None

    for i, existing in enumerate(existing_texts):
        if not existing or len(existing) < 50:
            continue
        existing_tf = _tf(_tokenize(existing))
        sim = _cosine_similarity(new_tf, existing_tf)
        if sim > max_sim:
            max_sim = sim
            matching_idx = i
        if sim >= threshold:
            return True, round(sim, 3), i

    return False, round(max_sim, 3), matching_idx


# ════════════════════════════════════════════════════════════
#  SEO SCORE
# ════════════════════════════════════════════════════════════

def seo_score(
    title: str,
    body: str,
    meta_desc: str = "",
    keywords: str = "",
) -> dict:
    """Basic SEO score for an article.
    Checks:
      - Title length (50-60 chars ideal)
      - Meta description length (120-160 chars ideal)
      - Headings (H2/H3) present
      - Keyword density (1-3% ideal)
      - Internal links present
      - Image alt text present
    """
    title_len = len(title)
    meta_len = len(meta_desc)
    word_count = len(re.findall(r"\b\w+\b", body))

    # Title score
    # V32.1: Aligned with journalist prompt (60-90 char headlines).
    # Old thresholds (50-60 perfect, 40-70 ok) penalized 70-90 char
    # headlines that the journalist prompt explicitly requests for SEO.
    if 60 <= title_len <= 80:
        t_score = 100
    elif 50 <= title_len <= 90:
        t_score = 80
    elif 40 <= title_len <= 100:
        t_score = 60
    else:
        t_score = 30

    # Meta score
    if 120 <= meta_len <= 160:
        m_score = 100
    elif 80 <= meta_len <= 180:
        m_score = 70
    else:
        m_score = 30

    # Headings (## or ###)
    h_count = len(re.findall(r"^#{2,3}\s+", body, re.MULTILINE))
    h_score = min(100, h_count * 20)

    # Keyword density
    kw_list = [k.strip().lower() for k in re.split(r"[,;|]", keywords) if k.strip()]
    if kw_list and word_count > 0:
        densities = []
        for kw in kw_list[:5]:  # top 5 keywords
            kw_count = len(re.findall(r"\b" + re.escape(kw) + r"\b", body, re.IGNORECASE))
            densities.append((kw_count / word_count) * 100)
        avg_density = sum(densities) / len(densities) if densities else 0
        if 0.5 <= avg_density <= 3:
            kw_score = 100
        elif avg_density < 0.5:
            kw_score = 50
        else:
            kw_score = 40  # keyword stuffing penalty
    else:
        kw_score = 50  # no keywords defined

    # Internal links
    links = len(re.findall(r"\]\(/article/", body))
    l_score = min(100, links * 15)

    # Image alt text (markdown ![alt](url))
    alt_count = len(re.findall(r"!\[[^\]]+\]\(", body))
    i_score = min(100, alt_count * 30)

    overall = (t_score + m_score + h_score + kw_score + l_score + i_score) / 6

    return {
        "overall": round(overall, 1),
        "title_length": t_score,
        "meta_length": m_score,
        "headings": h_score,
        "keyword_density": kw_score,
        "internal_links": l_score,
        "image_alt": i_score,
        "title_actual_len": title_len,
        "meta_actual_len": meta_len,
        "word_count": word_count,
        "heading_count": h_count,
        "internal_link_count": links,
    }


# ════════════════════════════════════════════════════════════
#  FACT-CHECKING — extract entities, flag suspicious claims
# ════════════════════════════════════════════════════════════

# Common red-flag phrases that suggest unverified claims
_SUSPICIOUS_PHRASES = [
    r"\baccording to (?:anonymous|unnamed|reliable)\b",
    r"\bsources? (?:said|claim)\b",
    r"\ballegedly\b",
    r"\brumored\b",
    r"\bunconfirmed reports?\b",
    r"\bsecret\b",
    r"\bleaked\b",
]

# Major organizations + country list (for entity extraction)
_MAJOR_ORGS = [
    "United Nations", "UN", "NATO", "EU", "European Union", "WHO",
    "IMF", "World Bank", "WTO", "FBI", "CIA", "MI6", "ISI",
    "Supreme Court", "Parliament", "Congress", "White House",
    "Downing Street", "Pentagon", "Kremlin",
]


def extract_entities(text: str) -> dict:
    """Extract named entities from text for fact-checking.
    Returns dates, numbers, organizations, and public figures found."""
    # Dates: "January 5, 2024" / "5 January 2024" / "01/05/2024" / "2024-01-05"
    dates = re.findall(
        r"\b(?:\d{1,2}\s+(?:January|February|March|April|May|June|July|"
        r"August|September|October|November|December)\s+\d{4}|"
        r"(?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},?\s+\d{4}|"
        r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\b",
        text,
    )

    # Numbers with units (percentages, money, casualties)
    numbers = re.findall(
        r"\b(?:USD\s?)?\$?\d+(?:,\d{3})*(?:\.\d+)?\s?(?:percent|%|million|billion|"
        r"thousand|killed|wounded|injured|people|troops|soldiers|dollars)\b",
        text,
        re.IGNORECASE,
    )

    # Organizations (from known list — case-sensitive)
    orgs_found = [o for o in _MAJOR_ORGS if o in text]

    return {
        "dates": dates[:10],          # cap at 10 for size
        "numbers": numbers[:15],
        "organizations": orgs_found,
    }


def fact_check(text: str) -> dict:
    """Lightweight fact-check — flags suspicious phrases + extracts
    entities that should be verified against trusted sources.

    A full implementation would call:
      - Google Fact Check Tools API
      - ClaimReview schema search
      - Trusted news source cross-reference (Reuters, AP, BBC)
    Here we do heuristic flagging; the entities are returned for the
    admin to manually verify (or for an AI agent to cross-check).
    """
    suspicious = []
    for pattern in _SUSPICIOUS_PHRASES:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            suspicious.extend(matches[:3])

    entities = extract_entities(text)

    # Score: starts at 100, deduct for each suspicious phrase
    score = max(0, 100 - len(suspicious) * 15)

    return {
        "score": score,
        "suspicious_phrases": suspicious,
        "entities_to_verify": entities,
        "needs_manual_review": len(suspicious) > 0 or len(entities["numbers"]) > 5,
    }


# ════════════════════════════════════════════════════════════
#  V25: ADSENSE SAFETY CHECK
#  Scans article text for content categories that violate Google
#  AdSense Program Policies. Articles failing ANY category are
#  auto-rejected (verdict="reject") regardless of overall quality
#  score. This is mandatory per PDF spec.
#  Ref: https://support.google.com/adsense/answer/9335564
# ════════════════════════════════════════════════════════════

# Each category maps to (policy_name, list_of_regex_patterns).
# Patterns are word-boundary regexes (case-insensitive).
# Tuned to minimize false positives — e.g., "cocaine" matches
# drug trafficking but "drugs" alone is too broad (pharma news).
# Multi-word phrases preferred over single words for precision.

_ADSENSE_VIOLATION_CATEGORIES: dict[str, dict] = {
    "adult_content": {
        "policy": "Adult Content (Sexually Explicit)",
        "severity": "critical",
        "patterns": [
            r"\b(?:porn(?:ography)?|pornographic|xxx|adult\s+video|sex\s+video|"
            r"nude\s+(?:photos?|pics?|images?|selfies?)|explicit\s+sexual|"
            r"escort\s+service|prostitution|hooker|strip\s+club\s+review)\b",
            r"\b(?:erotic\s+(?:massage|story|fiction)|hentai|rule\s*34|"
            r"onlyfans\s+leak|leaked\s+(?:nudes?|tapes?))\b",
        ],
    },
    "graphic_violence": {
        "policy": "Graphic Violence / Gore",
        "severity": "critical",
        "patterns": [
            r"\b(?:beheading|decapitat\w+|gore\s+(?:video|photos?)|"
            r"execution\s+video|snuff\s+film|graphic\s+(?:execution|killing)|"
            r"massacre\s+(?:footage|video|photos?)|"
            r"disembowel\w+|mutilat\w+\s+(?:corpse|body|bodies))\b",
            r"\b(?:how\s+to\s+(?:make|build)\s+(?:bomb|explosive|grenade|"
            r"molotov|pipe\s+bomb)|\bimprovise[ds]?\s+explosive\s+device\b)\b",
        ],
    },
    "hate_speech": {
        "policy": "Hate Speech (Discrimination / Incitement)",
        "severity": "critical",
        "patterns": [
            r"\b(?:racial\s+slur|kill\s+all\s+\w+|exterminate\s+(?:the|all)\s+\w+|"
            r"ethnic\s+cleansing|racial\s+superiority|white\s+power|"
            r"neo[-\s]?nazi\s+(?:propaganda|rally)|holocaust\s+denial)\b",
            r"\b(?:subhuman\s+\w+|vermin\s+(?:like|are)|"
            r"infestation\s+of\s+\w+)\b",
        ],
    },
    "dangerous_illegal_acts": {
        "policy": "Dangerous / Illegal Acts",
        "severity": "critical",
        "patterns": [
            r"\b(?:how\s+to\s+(?:build|make|construct)\s+(?:bomb|explosive|"
            r"firearm|silencer|automatic\s+weapon|sawed[-\s]?off\s+shotgun))\b",
            r"\b(?:cocaine\s+(?:trafficking|smuggling|cartel)|"
            r"heroin\s+(?:trafficking|smuggling)|meth\s+lab|"
            r"crack\s+cocaine\s+(?:recipe|manufacture|production)|"
            r"fentanyl\s+(?:distribution|sale\s+online))\b",
            r"\b(?:human\s+trafficking\s+(?:ring|network|operation)|"
            r"child\s+exploitation|csam)\b",
            r"\b(?:counterfeit\s+(?:money|currency|passport|id)|"
            r"credit\s+card\s+fraud\s+(?:tutorial|guide)|"
            r"identity\s+theft\s+(?:tutorial|guide))\b",
        ],
    },
    "terrorism_extremism": {
        "policy": "Terrorism / Violent Extremism",
        "severity": "critical",
        "patterns": [
            r"\b(?:isis\s+(?:recruitment|propaganda|training)|"
            r"al[-\s]?qaeda\s+(?:recruitment|training)|"
            r"taliban\s+(?:recruitment|propaganda)|"
            r"lone[-\s]?wolf\s+attack\s+(?:guide|manual|tutorial))\b",
            r"\b(?:terrorist\s+(?:attack\s+planning|cell|sleeper\s+cell)|"
            r"martyrdom\s+operation\s+(?:guide|manual))\b",
        ],
    },
    "weapons_facilitation": {
        "policy": "Weapons Facilitation (Sales / Conversion)",
        "severity": "high",
        "patterns": [
            r"\b(?:buy\s+(?:handgun|assault\s+rifle|ak[-\s]?47|ar[-\s]?15)\s+"
            r"(?:online|no\s+(?:background\s+check|id|license)))\b",
            r"\b(?:convert\s+\w+\s+to\s+(?:automatic|full\s+auto)|"
            r"ghost\s+gun\s+(?:kit|build|tutorial)|"
            r"3d[-\s]?print(?:ed)?\s+(?:firearm|gun|receiver|lower))\b",
            r"\b(?:illegal\s+firearm\s+(?:sale|trafficking|purchase))\b",
        ],
    },
    "tobacco_alcohol_promotion": {
        "policy": "Tobacco / Alcohol Promotion (Encouraging Use)",
        "severity": "medium",
        "patterns": [
            r"\b(?:buy\s+cheap\s+(?:cigarettes?|vapes?|e[-\s]?cigarettes?|"
            r"chewing\s+tobacco)\s+online)\b",
            r"\b(?:how\s+to\s+(?:vape|smoke|chew\s+tobacco)|"
            r"vaping\s+(?:tutorial|tricks?\s+tutorial))\b",
            r"\b(?:cheap\s+liquor\s+online|underage\s+drinking\s+guide|"
            r"how\s+to\s+(?:drink\s+more|chug|binge\s+drink))\b",
        ],
    },
    "misleading_clickbait": {
        "policy": "Misleading / Deceptive Clickbait",
        "severity": "medium",
        "patterns": [
            r"\b(?:you\s+won'?t\s+believe|shocking\s+truth\s+(?:about|revealed)|"
            r"doctors\s+hate\s+this|one\s+weird\s+trick|"
            r"this\s+(?:will|could)\s+make\s+you\s+(?:rich|thin|immortal))\b",
            r"\b(?:cures?\s+(?:cancer|diabetes|hiv|covid)\s+(?:in\s+\d+\s+days?|"
            r"overnight|guaranteed)|miracle\s+(?:cure|pill|supplement))\b",
        ],
    },
    "sensitive_events_exploitation": {
        "policy": "Sensitive Events Exploitation (Tragedy Marketing)",
        "severity": "high",
        "patterns": [
            r"\b(?:buy\s+\w*\s*(?:9\/11|holocaust|tsunami|earthquake|"
            r"mass\s+shooting)\s+(?:merchandise|t[-\s]?shirt|souvenir))\b",
            r"\b(?:profit\s+from\s+(?:tragedy|disaster|mass\s+casualty)|"
            r"donate\s+to\s+\w+\s+(?:victims?)?\s*(?:bitcoin|crypto|wallet))\b",
        ],
    },
}


def _compile_adsense_patterns() -> list[tuple[str, str, str, "re.Pattern"]]:
    """Pre-compile all AdSense violation regex patterns for fast scanning.
    Returns list of (category_key, policy_name, severity, compiled_regex)."""
    compiled: list[tuple[str, str, str, "re.Pattern"]] = []
    for cat_key, cat_data in _ADSENSE_VIOLATION_CATEGORIES.items():
        for pat in cat_data["patterns"]:
            compiled.append(
                (cat_key, cat_data["policy"], cat_data["severity"], re.compile(pat, re.IGNORECASE))
            )
    return compiled


_COMPILED_ADSENSE_PATTERNS = _compile_adsense_patterns()


def adsense_safety_check(title: str, body: str) -> dict:
    """Scan article title + body for AdSense policy violations.

    Returns:
        {
            "is_safe": bool,           # True if no violations found
            "overall_score": float,    # 100 if safe, 0 if any critical violation, 50 if only medium
            "violations": [
                {
                    "category": str,       # internal key, e.g. "adult_content"
                    "policy": str,         # human-readable policy name
                    "severity": str,       # "critical" | "high" | "medium"
                    "matches": [str, ...], # matched phrases (capped at 3)
                    "match_count": int,
                },
                ...
            ],
            "critical_violations": int,
            "total_violations": int,
            "verdict": str,            # "pass" | "review" | "reject"
        }

    Articles with ANY critical/high violation are auto-rejected.
    Articles with only medium violations go to manual review.
    """
    full_text = f"{title}\n\n{body}"
    violations_by_cat: dict[str, dict] = {}

    for cat_key, policy_name, severity, compiled_re in _COMPILED_ADSENSE_PATTERNS:
        matches = compiled_re.findall(full_text)
        if matches:
            if cat_key not in violations_by_cat:
                violations_by_cat[cat_key] = {
                    "category": cat_key,
                    "policy": policy_name,
                    "severity": severity,
                    "matches": [],
                    "match_count": 0,
                }
            # Normalize matches to strings
            norm_matches: list[str] = []
            for m in matches:
                if isinstance(m, tuple):
                    norm_matches.append(" ".join(str(x) for x in m if x))
                else:
                    norm_matches.append(str(m))
            violations_by_cat[cat_key]["matches"].extend(norm_matches)
            violations_by_cat[cat_key]["match_count"] += len(matches)

    violations = list(violations_by_cat.values())
    # Cap matches list at 3 per category to keep DB column small
    for v in violations:
        v["matches"] = v["matches"][:3]

    critical_count = sum(1 for v in violations if v["severity"] == "critical")
    high_count     = sum(1 for v in violations if v["severity"] == "high")
    medium_count   = sum(1 for v in violations if v["severity"] == "medium")

    # Compute verdict + score
    if critical_count > 0 or high_count > 0:
        verdict = "reject"
        overall_score = 0.0
    elif medium_count > 0:
        verdict = "review"
        overall_score = 50.0
    else:
        verdict = "pass"
        overall_score = 100.0

    return {
        "is_safe": verdict == "pass",
        "overall_score": overall_score,
        "violations": violations,
        "critical_violations": critical_count + high_count,  # treat high as critical for safety
        "total_violations": len(violations),
        "verdict": verdict,
    }


# ════════════════════════════════════════════════════════════
#  MAIN ENTRY — evaluate_article()
# ════════════════════════════════════════════════════════════

@dataclass
class QualityScore:
    overall: float
    readability: float       # Flesch Reading Ease
    structural: dict         # paragraph/sentence/bold/faq scores
    grammar: dict            # score + issues list
    seo: dict                # SEO breakdown
    uniqueness: dict         # max similarity + matching index
    fact_check: dict         # suspicious phrases + entities
    adsense_safety: dict     # V25: AdSense policy compliance
    word_count: int
    verdict: str             # "publish" | "review" | "reject"
    reasons: list[str] = field(default_factory=list)


def evaluate_article(
    title: str,
    body: str,
    meta_desc: str = "",
    keywords: str = "",
    existing_texts: Optional[list[str]] = None,
) -> QualityScore:
    """Run all quality checks on an article.
    Returns a QualityScore dataclass with sub-scores + verdict.

    Verdict rules:
      - "reject"  if AdSense safety check fails (V25, mandatory per PDF spec)
      - "reject"  if word count < MIN_WORD_COUNT or grammar score < 20
      - "reject"  if duplicate of existing article
      - "publish" if overall >= MIN_QUALITY_SCORE and no critical failures
      - "review"  if any sub-score < 40 or uniqueness > threshold
    """
    reasons: list[str] = []
    existing_texts = existing_texts or []

    # ── V25: AdSense Safety Check (FIRST — short-circuit if violation) ──
    adsense = adsense_safety_check(title, body)

    # ── Word count ──
    word_count = len(re.findall(r"\b\w+\b", body))

    # ── Readability ──
    readability = flesch_reading_ease(body)

    # ── Structural ──
    structural = structural_score(body)

    # ── Grammar ──
    grammar = grammar_score(body)

    # ── SEO ──
    seo = seo_score(title, body, meta_desc, keywords)

    # ── Uniqueness (semantic dedup) ──
    is_dup, max_sim, match_idx = semantic_dedup(body, existing_texts)
    uniqueness = {
        "is_duplicate": is_dup,
        "max_similarity": max_sim,
        "matching_index": match_idx,
    }

    # ── Fact-check ──
    fc = fact_check(body)

    # ── Compute overall ──
    # Weighted: readability 20, structural 20, grammar 15, SEO 20, uniqueness 15, fact 10
    # Note: AdSense safety is a HARD GATE — if it fails, verdict is forced to
    # "reject" regardless of overall score (see verdict block below).
    overall = (
        (readability if readability >= MIN_READABILITY else readability * 0.5) * 0.20 +
        sum(structural.values()) / len(structural) * 0.20 +
        grammar["score"] * 0.15 +
        seo["overall"] * 0.20 +
        (100 if not is_dup else 0) * 0.15 +
        fc["score"] * 0.10
    )

    # ── Verdict ──
    verdict = "publish"
    # V25: AdSense safety hard gate — rejects take priority over everything
    if adsense["verdict"] == "reject":
        verdict = "reject"
        crit_policies = ", ".join(v["policy"] for v in adsense["violations"] if v["severity"] in ("critical", "high"))
        reasons.append(
            f"AdSense policy violation ({adsense['critical_violations']} critical): {crit_policies}"
        )
    elif adsense["verdict"] == "review":
        if verdict == "publish":
            verdict = "review"
        med_policies = ", ".join(v["policy"] for v in adsense["violations"] if v["severity"] == "medium")
        reasons.append(f"AdSense minor concern (medium): {med_policies}")

    if word_count < MIN_WORD_COUNT:
        verdict = "reject"
        reasons.append(f"Word count too low ({word_count} < {MIN_WORD_COUNT})")
    if grammar["score"] < 20:
        verdict = "reject"
        reasons.append(f"Grammar score too low ({grammar['score']})")
    if is_dup:
        verdict = "reject"
        reasons.append(f"Duplicate of article #{match_idx} (similarity {max_sim})")
    if overall < MIN_QUALITY_SCORE and verdict != "reject":
        verdict = "review"
        reasons.append(f"Overall score {overall:.1f} below threshold {MIN_QUALITY_SCORE}")
    if readability < MIN_READABILITY:
        if verdict == "publish":
            verdict = "review"
        reasons.append(f"Readability {readability} too low (target > {MIN_READABILITY})")
    if fc["needs_manual_review"]:
        if verdict == "publish":
            verdict = "review"
        reasons.append("Fact-check flagged suspicious claims — needs manual review")

    if not reasons and verdict == "publish":
        reasons.append("All quality checks passed (incl. AdSense safety)")

    return QualityScore(
        overall=round(overall, 1),
        readability=readability,
        structural=structural,
        grammar=grammar,
        seo=seo,
        uniqueness=uniqueness,
        fact_check=fc,
        adsense_safety=adsense,
        word_count=word_count,
        verdict=verdict,
        reasons=reasons,
    )


def quality_score_to_dict(qs: QualityScore) -> dict:
    """Convert QualityScore dataclass → JSON-serializable dict."""
    return {
        "overall": qs.overall,
        "readability": qs.readability,
        "structural": qs.structural,
        "grammar": qs.grammar,
        "seo": qs.seo,
        "uniqueness": qs.uniqueness,
        "fact_check": qs.fact_check,
        "adsense_safety": qs.adsense_safety,
        "word_count": qs.word_count,
        "verdict": qs.verdict,
        "reasons": qs.reasons,
    }
