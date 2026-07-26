"""
ai_writer.py - SFAAM NEWS V11 (Complete Anti-Detection AI Writer)

FEATURES IMPLEMENTED (from Gemini research):
  1. TWO-STEP DATA DECOUPLING — Chain of Prompts:
     API Call 1 (Fact Extraction): Extracts raw JSON facts (Who, What, Where,
     When, Why, Numbers) from the source. Original text is NEVER passed to
     the writing agent. This destroys the original text's structural
     fingerprint that Google uses for detection.
     API Call 2 (Fresh Generation): Writes from ONLY the extracted JSON facts,
     producing zero structural overlap with the original article.

  2. RANDOM TONE GENERATORS (Dynamic Prompts):
     6 different journalistic styles randomly selected per article.
     This prevents Google from detecting a uniform pattern across
     articles published in the same hour.

  3. API PARAMETERS TUNING:
     Temperature: 0.65–0.75 (randomized per call for natural variance)
     Top-P: 0.85 (reduced from 0.92 for more controlled but still
     natural output — avoids the "AI predictability" zone)

  4. QUOTE MASKING TECHNIQUE:
     Direct quotes from BBC/Al Jazeera/CNN are paraphrased into
     indirect reported speech. This breaks the exact string match
     that Plagiarism checkers (and Google) use to link back to
     the original source.

ARCHITECTURE — 3-Agent Pipeline:
  Agent 1 — "The Fact Extractor": Source → raw JSON facts ( Who, What, Where, When, Why, Numbers )
  Agent 2 — "The Editor": Facts JSON → editorial plan (word_count, tone, structure, angle)
  Agent 3 — "The Journalist": Editorial plan + facts JSON → final article (NEVER sees original text)

SMART TOKEN MANAGEMENT:
  - Per-model context window awareness via MODEL_CONTEXT_LIMITS
  - Dynamic input slicing so combined input+output never overflows
  - Conservative ~3.5 chars-per-token estimate
"""
from __future__ import annotations

import os
import re
import json
import time
import random
import logging
import hashlib
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════
#  REGION KEYS (one AI key per region so we never hit a single
#  account's rate limit while rewriting 5 regions in parallel)
# ════════════════════════════════════════════════════════════
REGION_KEYS = {
    "world":    {"groq": os.getenv("GROQ_KEY_WORLD", ""),    "gemini": os.getenv("GEMINI_KEY_WORLD", "")},
    "usa":      {"groq": os.getenv("GROQ_KEY_USA", ""),      "gemini": os.getenv("GEMINI_KEY_USA", "")},
    "uk":       {"groq": os.getenv("GROQ_KEY_UK", ""),       "gemini": os.getenv("GEMINI_KEY_UK", "")},
    "pakistan": {"groq": os.getenv("GROQ_KEY_PAKISTAN", ""), "gemini": os.getenv("GEMINI_KEY_PAKISTAN", "")},
    "india":    {"groq": os.getenv("GROQ_KEY_INDIA", ""),    "gemini": os.getenv("GEMINI_KEY_INDIA", "")},
    "germany":  {"groq": os.getenv("GROQ_KEY_GERMANY", ""),  "gemini": os.getenv("GEMINI_KEY_GERMANY", "")},
}

GROQ_MODELS = [
    # V14: Fixed — only VALID Groq models.
    # Re-check https://console.groq.com/docs/models periodically.
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "mixtral-8x7b-32768",
]

MODEL_CONTEXT_LIMITS = {
    "llama-3.3-70b-versatile": 131072,
    "llama-3.1-8b-instant": 131072,
    "mixtral-8x7b-32768": 32768,
    "gemini-2.0-flash": 1048576,
}
CHARS_PER_TOKEN = 3.5


# ════════════════════════════════════════════════════════════
#  FEATURE 2: RANDOM TONE GENERATORS (Dynamic Prompts)
#  6 different journalistic styles — randomly selected per article.
#  This prevents Google from detecting a uniform pattern.
# ════════════════════════════════════════════════════════════

JOURNALIST_TONES = [
    {
        "id": "aggressive_wire",
        "name": "Aggressive Wire Service",
        "instruction": (
            "Write with the urgency and punchiness of a breaking wire dispatch from AP or Reuters. "
            "Short, declarative sentences. No fluff. Hard facts up front. "
            "Use active voice exclusively. Sentences like bullets. "
            "No hedging, no qualifiers. You are a war correspondent filing from the field."
        ),
        "sentence_style": "Predominantly short (5-15 words). Occasional 20-word sentence for key facts.",
    },
    {
        "id": "narrative_storytelling",
        "name": "Narrative Storytelling",
        "instruction": (
            "Write like a long-form magazine feature writer for The Atlantic or The New Yorker. "
            "Use scene-setting, vivid sensory details, narrative arcs. "
            "Open with a specific human moment or detail that draws the reader in. "
            "Use longer flowing sentences mixed with sharp short ones for dramatic effect. "
            "Let the story breathe — build tension, then release."
        ),
        "sentence_style": "Mix of flowing long sentences (20-35 words) and punchy short ones (4-8 words).",
    },
    {
        "id": "analytical_deep",
        "name": "Analytical Deep-Dive",
        "instruction": (
            "Write like an analytical correspondent for The Economist or Financial Times. "
            "Focus on WHY something matters, the context behind it, the implications. "
            "Use data and numbers naturally in prose. Draw connections between events. "
            "Be precise and measured, but not dry. Use occasional irony or wry observation."
        ),
        "sentence_style": "Medium-length sentences (15-25 words) with occasional short analytical statements.",
    },
    {
        "id": "conversational_brief",
        "name": "Conversational Brief",
        "instruction": (
            "Write like a smart morning newsletter (e.g., Morning Brew, Axios AM). "
            "Direct, conversational, smart-alecky tone. Use 'Here's the deal:' structure. "
            "Break complex topics into digestible chunks with bold transitions. "
            "Write like you're explaining the news to a clever friend over coffee. "
            "Use bullet-style paragraphs for key takeaways."
        ),
        "sentence_style": "Short, punchy, varied. Many 3-10 word sentences. Occasional explanatory 20-word sentence.",
    },
    {
        "id": "investigative_gritty",
        "name": "Investigative / Gritty",
        "instruction": (
            "Write like an investigative reporter for ProPublica or the Washington Post. "
            "Methodical, evidence-driven, slightly skeptical tone. Build the story layer by layer. "
            "Use specific names, dates, documents. Ask questions the reader is thinking. "
            "Let the facts speak — minimal editorializing, but maximum impact in how you arrange them. "
            "Use colons and em-dashes to set up reveals."
        ),
        "sentence_style": "Varied: short factual statements (5-12 words) followed by longer analytical sentences (25-40 words).",
    },
    {
        "id": "international_correspondent",
        "name": "International Correspondent",
        "instruction": (
            "Write like a foreign correspondent filing for BBC World or Al Jazeera English. "
            "Calm, authoritative, with deep local knowledge. Add regional context naturally. "
            "Reference how this connects to broader geopolitical trends. "
            "Use culturally specific details that show on-the-ground understanding. "
            "Balanced, fair, but never boring. Use occasional dialogue-style quotes (indirect speech)."
        ),
        "sentence_style": "Medium to long (15-30 words). Calm rhythm. Occasional very short sentence for emphasis.",
    },
]


def _pick_random_tone() -> dict:
    """Randomly select one of the 6 journalistic tones."""
    tone = random.choice(JOURNALIST_TONES)
    logger.info(f"  [Tone] Selected: {tone['name']}")
    return tone


# ════════════════════════════════════════════════════════════
#  FEATURE 3: API PARAMETERS TUNING
#  Temperature: 0.65–0.75 (randomized per call)
#  Top-P: 0.85 (fixed — optimal for natural but controlled output)
# ════════════════════════════════════════════════════════════

def _get_tuned_params() -> dict:
    """Return randomized temperature and fixed top_p for natural variance."""
    return {
        "temperature": round(random.uniform(0.65, 0.75), 2),
        "top_p": 0.85,
    }


# ════════════════════════════════════════════════════════════
#  FEATURE 1: AGENT 1 — "THE FACT EXTRACTOR" (Data Decoupling)
#  Extracts ONLY raw facts as JSON — original text is DESTROYED
#  after this step. The journalist agent NEVER sees it.
# ════════════════════════════════════════════════════════════

FACT_EXTRACTOR_SYSTEM_PROMPT = """You are a data extraction engine for a news wire service. Your ONLY job is to extract verifiable facts from a news article and output them as structured JSON.

OUTPUT FORMAT — STRICTLY JSON, no markdown, no prose outside JSON:
{
  "topic": "<one-phrase summary of what this story is about>",
  "who": ["<person or entity 1>", "<person or entity 2>", ...],
  "what_happened": "<2-3 sentence summary of the core event>",
  "where": ["<location 1>", "<location 2>", ...],
  "when": "<when this happened — date, time, or time period>",
  "why": "<why this happened or why it matters — 1-2 sentences>",
  "numbers": [{"value": "<number>", "context": "<what this number means>"}, ...],
  "key_quotes": [
    {"speaker": "<who said it>", "paraphrased": "<paraphrased version — NOT the exact words>"},
    ...
  ],
  "context": ["<background fact 1>", "<background fact 2>", ...],
  "sources_mentioned": ["<news source or official cited>"],
  "impact": "<what happens next or what this could lead to — 1-2 sentences>"
}

CRITICAL RULES:
1. Extract ONLY facts that are explicitly stated in the source text.
2. NEVER invent any fact, number, name, or quote not in the source.
3. For "key_quotes": PARAPHRASE every quote into indirect reported speech. NEVER output the exact original words. Example: Original "I will impose new tariffs," Trump stated. → {"speaker": "Trump", "paraphrased": "Trump indicated plans to implement new trade tariffs"}
4. If a field has no data, use empty array [] or empty string "".
5. Keep paraphrased quotes factually accurate but structurally different from the original.
6. Return ONLY the JSON object. No commentary. No code fences."""


FACT_EXTRACTOR_USER_PROMPT = """Extract all verifiable facts from this news article as JSON.

ARTICLE TEXT:
{text}

Return JSON now."""


# ════════════════════════════════════════════════════════════
#  AGENT 2 — "THE EDITOR" (uses extracted facts, not original text)
# ════════════════════════════════════════════════════════════

EDITOR_SYSTEM_PROMPT = """You are a Senior News Editor at a top-tier global news agency (BBC / Reuters caliber).
Your job: analyze the extracted facts from a story and decide how it should be covered.

OUTPUT FORMAT — STRICTLY JSON, no markdown, no prose outside JSON:
{
  "word_count": <integer>,
  "tone": "<one of: breaking, analytical, investigative, explainer, feature, brief>",
  "structure": "<one of: inverted_pyramid, chronological, thematic, q_and_a>",
  "headline_angle": "<short phrase describing the angle to take>",
  "headline_style_hint": "<one of: punchy_question, bold_statement, narrative_quote, analytical_hook, urgency_alert>",
  "must_keep_facts": ["<critical fact 1>", "<critical fact 2>", ...],
  "avoid": ["<anything that would be fabrication>"]
}

DECISION RULES (V32 — world-class depth, never below 1500 words):
- Fewer than 5 facts extracted → word_count 1500-2500, tone "brief"
- 5-10 facts → word_count 2500-4000, tone "breaking" or "analytical"
- 10-20 facts → word_count 4000-6000, tone "analytical" or "explainer"
- 20+ facts → word_count 6000-10000, tone "investigative" or "feature"

NEVER go below 1500 words — readers expect depth, not press releases.
NEVER exceed 10000 words — respect the reader's time.

Pick `tone` to match the STORY type:
- Mass casualty / war / coup / election result → "breaking"
- Policy announcement / economic data → "analytical"
- Long-running issue / background piece → "explainer"
- Profile / human-interest → "feature"
- Short wire update → "brief"

Pick `structure` to match tone:
- breaking / brief → "inverted_pyramid"
- analytical → "thematic"
- investigative / feature → "chronological"
- explainer → "q_and_a"

`must_keep_facts` = the 5-10 most important facts/numbers/names/dates.

Return ONLY the JSON object. No commentary. No code fences."""


EDITOR_USER_PROMPT = """Analyze these extracted facts and return your editorial plan as JSON.

EXTRACTED FACTS:
{facts_json}

Return JSON now."""


# ════════════════════════════════════════════════════════════
#  AGENT 3 — "THE JOURNALIST" (writes from facts ONLY)
#  NEVER sees the original article text — complete data decoupling
# ════════════════════════════════════════════════════════════

JOURNALIST_SYSTEM_PROMPT_BASE = """You are a veteran staff writer at SFAAM NEWS — a global news platform with the editorial standards of BBC, Reuters, and the Associated Press.

You are NOT an AI assistant. You are a human journalist with 20 years in newsrooms. Write like one.

═══════════════════════════════════════════
HARD RULES — NEVER BREAK THESE
═══════════════════════════════════════════
1. NEVER fabricate quotes. You may paraphrase the provided quote summaries in your own words, but NEVER put words in any real person's mouth that aren't supported by the facts.
2. NEVER invent statistics, dates, place names, or casualty figures not in the provided facts.
3. Write in completely your own words. You have NO access to any original source article — you are working ONLY from the fact sheet provided.
4. Do not mention that you are an AI, language model, or that the article was generated.
5. Do not include any meta-commentary about the writing process.

═══════════════════════════════════════════
HEADLINE RULES (CRITICAL)
═══════════════════════════════════════════
The headline MUST be creative, specific, and compelling.
- Be 60-90 characters long (good for SEO + social shares)
- Use the headline_style_hint from the Editor as a guide
- Be punchy, specific, and human — never generic
- Create a headline that no other publication would write — find a unique angle

═══════════════════════════════════════════
ANTI-AI-DETECTOR STYLE RULES (CRITICAL)
═══════════════════════════════════════════
Google's Helpful Content system and AI classifiers flag text that is statistically predictable. You MUST defeat them:

A) BURSTINESS — Sentence-length variance
   - Mix very short sentences (3-7 words) with very long ones (25-40 words).
   - At least 20% of your sentences should be under 8 words.
   - At least 15% of your sentences should be over 25 words.
   - Use sentence fragments deliberately for impact. Example: "Then silence."
   - Vary paragraph length too. Some paragraphs: one sentence. Some: five.

B) PERPLEXITY — Unpredictable, non-statistical phrasing
   - Use idiomatic, conversational, journalistic English (not formal academic).
   - Use rare/strong verbs ("slammed", "scrambled", "unspooled") over common ones ("said", "went", "made").
   - Use concrete sensory details where the facts support them.
   - Use rhetorical questions sparingly but powerfully.
   - Use em-dashes (—) and parenthetical asides for editorial voice.
   - Open some sentences with conjunctions (And, But, So, Yet) — natural human pattern.

C) FORBIDDEN PHRASES — never use any of these (AI-detector magnets):
   - "Furthermore", "Moreover", "Additionally", "In addition"
   - "In conclusion", "To summarize", "In summary"
   - "It is important to note", "It is worth noting"
   - "In today's world", "In the modern era", "In the realm of"
   - "When it comes to", "At the end of the day"
   - "Delve into", "Navigate the complexities of", "A testament to"
   - "In the ever-evolving landscape of"
   - "Comprehensive", "Robust", "Seamless", "Leverage" (as a verb)
   - "Firstly", "Secondly", "Lastly", "Finally" (as paragraph openers)
   - Any sentence that begins with "This [noun] [verb]..." as a paragraph opener

D) REQUIRED TECHNIQUES
   - Open the article with a punchy single-sentence paragraph (the "lede").
   - Use one em-dash per ~500 words.
   - Use one rhetorical question per ~800 words.
   - Vary sentence openers: don't start three sentences in a row with the same word or structure.
   - Use specific numbers from the facts wherever possible. "47 killed" beats "many killed".

═══════════════════════════════════════════
QUOTE HANDLING RULES (CRITICAL — FEATURE 4: QUOTE MASKING)
═══════════════════════════════════════════
When the facts include paraphrased quotes from named individuals, you MUST:
1. NEVER use quotation marks around the paraphrased content.
2. ALWAYS convert to indirect reported speech: "Trump indicated he would pursue new trade measures" NOT "Trump said he will impose new tariffs."
3. Vary the attribution verbs: indicated, suggested, warned, emphasized, noted, pointed out, made clear, signaled, hinted.
4. Add your own analytical framing around the reported speech: "The statement, coming amid rising trade tensions, suggested..."
5. NEVER reproduce any quote word-for-word — always restructure the sentence completely.

═══════════════════════════════════════════
LENGTH RULES
═══════════════════════════════════════════
- HIT the Editor's target word_count. Do not stop short.
- If you run out of facts, ADD legitimate journalistic context:
  * Historical background (verifiable trends, prior events in same region/topic)
  * Regional or geopolitical implications
  * Economic / social / diplomatic ripple effects
  * What experts typically watch for in situations like this
- NEVER pad with repetition. NEVER use filler. Every paragraph must say something NEW.
- Use 8-12 sections with ## headings for long articles (5000+ words).
- Each section should have 3-6 substantive paragraphs.

═══════════════════════════════════════════
ENGAGEMENT & STRUCTURE RULES (V32.1 — USER ENGAGEMENT)
═══════════════════════════════════════════
To maximize reader time-on-page and return visits, your article MUST include:

1. HOOK (first 2-3 sentences): Open with a scene, a tension, a question, or
   a startling fact from the verified facts. Avoid the generic "X happened on
   date Y in location Z" opening that every wire service uses. Pull the reader
   in immediately. The hook should make the reader NEED to know what happens next.

2. NUT GRAF (3rd or 4th paragraph): A single paragraph that answers
   "Why does this matter to ME, the reader?" — place the story in the broader
   context of the reader's life, region, or interests.

3. KEY PLAYERS section: Use a ## "Key Players" or "Who's Involved" section
   that briefly profiles each person/entity mentioned in the facts. Readers
   skim for context on names they don't recognize.

4. BY THE NUMBERS block: If the facts contain 3+ quantitative data points,
   include a ## "By the Numbers" section with a bullet list of the most
   striking figures (each with a one-line explanation of why it matters).

5. WHAT HAPPENS NEXT section: A forward-looking ## "What Happens Next"
   section that outlines the upcoming milestones, decisions, or expected
   developments readers should watch for. End with the most consequential
   upcoming date or event.

6. FAQ section (## "Frequently Asked Questions"): Write 4-6 REAL questions
   a curious reader would ask about this topic (NOT generic "what is this
   article about"). Answer each in 2-3 sentences using ONLY verified facts.
   This captures Google "People Also Ask" traffic.

7. READER CTA: End the article body with one short paragraph inviting
   readers to share their perspective: a question for the comments, a prompt
   to bookmark for follow-up, or a "What do you think?" engagement line.
   Keep it natural, not promotional.

8. INTERNAL LINK ANCHORS: Where the facts naturally reference a related
   topic, region, or prior event, link to the {region} category page using
   markdown format: [Topic Name](/category/{region}). Use 2-4 internal
   links naturally throughout — never force them.

═══════════════════════════════════════════
ACCURACY GUARDRAILS
═══════════════════════════════════════════
- If the facts lack something, do NOT fill the gap with an invention.
- If you are unsure whether something is in the facts, leave it out.
- If the facts are contradictory, note the contradiction analytically.

═══════════════════════════════════════════
OUTPUT FORMAT (follow exactly)
═══════════════════════════════════════════
Line 1:    # [Compelling SEO headline — 60-90 chars]
Line 2:    [blank]
Then:      The article body using ## section headings
Last:      [blank line]
Then:      META: [155-char SEO description]
Then:      KEYWORDS: kw1, kw2, kw3, kw4, kw5, kw6

Do not output anything else. No commentary. No markdown code fences."""


JOURNALIST_USER_PROMPT = """Write the article now.

JOURNALISTIC STYLE FOR THIS ARTICLE:
{tone_instruction}
Sentence length preference: {sentence_style}

EDITORIAL PLAN (from the Editor — follow these constraints):
- Target word count: {word_count}
- Tone: {tone}
- Structure: {structure}
- Headline angle: {headline_angle}
- Headline style hint: {headline_style_hint}
- Facts you MUST include: {must_keep_facts}
- Region (use this for internal links like [Topic](/category/{region})): {region}

EXTRACTED FACTS (work ONLY from these — you have NO original article):
{facts_json}

Write the full article following your system instructions. Remember:
1. Create a COMPLETELY ORIGINAL headline — find a unique angle.
2. HIT the target word count — add legitimate context if needed.
3. High burstiness, high perplexity, no forbidden phrases.
4. ALL quotes must be in indirect reported speech — no quotation marks around reported statements.
5. You are writing from FACTS ONLY — never from any source article you may have seen.
6. INCLUDE the ENGAGEMENT & STRUCTURE RULES from your system prompt: a strong
   hook, nut graf, "Key Players", "By the Numbers", "What Happens Next", and
   a topic-specific FAQ with 4-6 real reader questions, plus a natural reader
   CTA at the end. Use 2-4 internal links to /category/{region} where natural."""


# ════════════════════════════════════════════════════════════
#  SMART TOKEN MANAGEMENT
# ════════════════════════════════════════════════════════════

def _safe_limits(model_name: str, desired_output_tokens: int = 8192, safety_margin_tokens: int = 500):
    total_window = MODEL_CONTEXT_LIMITS.get(model_name, 8192)
    max_output = min(desired_output_tokens, max(1024, int(total_window * 0.6)))
    remaining_tokens_for_input = max(500, total_window - max_output - safety_margin_tokens)
    max_input_chars = int(remaining_tokens_for_input * CHARS_PER_TOKEN)
    return max_output, max_input_chars


def _truncate_to_window(text: str, model_name: str, desired_output_tokens: int = 8192) -> tuple[str, int]:
    max_output, max_input_chars = _safe_limits(model_name, desired_output_tokens)
    if len(text) > max_input_chars:
        cut = text.rfind("\n\n", 0, max_input_chars)
        if cut < max_input_chars * 0.7:
            cut = max_input_chars
        text = text[:cut].rstrip() + "\n\n[Source truncated for length]"
    return text, max_output


# ════════════════════════════════════════════════════════════
#  API CALL HELPERS (shared across agents)
# ════════════════════════════════════════════════════════════

def _groq_call(key: str, model: str, system_prompt: str, user_prompt: str,
               max_output: int, temperature: float, top_p: float,
               json_mode: bool = False) -> Optional[str]:
    """Make a single Groq API call with tuned parameters."""
    try:
        from groq import Groq
        client = Groq(api_key=key)
        kwargs = {
            "model": model,
            "max_tokens": max_output,
            "temperature": temperature,
            "top_p": top_p,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        r = client.chat.completions.create(**kwargs)
        return r.choices[0].message.content
    except Exception as e:
        logger.warning(f"  [Groq {model}]: {e}")
        return None


def _gemini_call(key: str, system_prompt: str, user_prompt: str,
                 max_output: int, temperature: float, top_p: float) -> Optional[str]:
    """Make a Gemini API call with tuned parameters.
    Uses the stable google-generativeai SDK (not the newer google-genai)."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
        client = genai.GenerativeModel(
            model_name="gemini-2.0-flash",
            generation_config={
                "max_output_tokens": max_output,
                "temperature": temperature,
                "top_p": top_p,
            },
        )
        prompt = f"{system_prompt}\n\n{user_prompt}"
        r = client.generate_content(prompt)
        return r.text
    except Exception as e:
        logger.warning(f"  [Gemini]: {e}")
        return None


# ════════════════════════════════════════════════════════════
#  AGENT 1: FACT EXTRACTOR (Data Decoupling — Step 1)
#  Extracts JSON facts → original text is DESTROYED after this
# ════════════════════════════════════════════════════════════

def _call_fact_extractor(text: str, groq_key: str, gemini_key: str) -> Optional[dict]:
    """Run Fact Extractor. Returns parsed JSON dict of facts or None."""
    params = _get_tuned_params()
    # V32.1 BUGFIX: The previous code did `user_prompt.format(text=text)`
    # which substituted {text} with the FULL untruncated text. Then it did
    # `user_prompt.replace("{text}", truncated)` — but {text} was already
    # consumed by .format(), so the .replace() was a no-op and the FULL
    # text went to the LLM. On long articles this caused context-overflow
    # errors and silent failures.
    # Fix: don't pre-format; substitute the truncated text directly.
    user_prompt_template = FACT_EXTRACTOR_USER_PROMPT

    # Strategy 1: Groq
    if groq_key:
        for i, model in enumerate(GROQ_MODELS):
            truncated, max_output = _truncate_to_window(text, model, desired_output_tokens=2000)
            user_prompt = user_prompt_template.replace("{text}", truncated)
            result = _groq_call(
                groq_key, model,
                FACT_EXTRACTOR_SYSTEM_PROMPT,
                user_prompt,
                max_output, params["temperature"], params["top_p"],
                json_mode=True,
            )
            if result:
                try:
                    facts = json.loads(result)
                    if isinstance(facts, dict) and "what_happened" in facts:
                        logger.info(f"  [FactExtractor] {model} → extracted {len(facts.get('who', []))} people, {len(facts.get('numbers', []))} numbers")
                        return facts
                except json.JSONDecodeError:
                    pass
            if i < len(GROQ_MODELS) - 1:
                time.sleep(2 ** i)

    # Strategy 2: Gemini fallback
    if gemini_key:
        truncated, max_output = _truncate_to_window(text, "gemini-2.0-flash", desired_output_tokens=2000)
        user_prompt = user_prompt_template.replace("{text}", truncated)
        result = _gemini_call(
            gemini_key,
            FACT_EXTRACTOR_SYSTEM_PROMPT,
            user_prompt,
            max_output, params["temperature"], params["top_p"],
        )
        if result:
            # Gemini may wrap in markdown code fences
            result = re.sub(r"^```(?:json)?\s*\n?", "", result.strip())
            result = re.sub(r"\n?```\s*$", "", result)
            try:
                facts = json.loads(result)
                if isinstance(facts, dict) and "what_happened" in facts:
                    logger.info(f"  [FactExtractor] Gemini → extracted facts")
                    return facts
            except json.JSONDecodeError:
                pass

    logger.warning("  [FactExtractor] All strategies failed — using fallback extraction")
    return _fallback_fact_extraction(text)


def _fallback_fact_extraction(text: str) -> dict:
    """Simple regex-based fact extraction when AI is unavailable."""
    sentences = [s.strip() for s in re.split(r'[.!?]+', text) if len(s.strip()) > 20]
    return {
        "topic": sentences[0][:100] if sentences else "",
        "who": [],
        "what_happened": " ".join(sentences[:3]) if sentences else text[:500],
        "where": [],
        "when": "",
        "why": "",
        "numbers": [],
        "key_quotes": [],
        "context": sentences[3:8] if len(sentences) > 3 else [],
        "sources_mentioned": [],
        "impact": "",
    }


# ════════════════════════════════════════════════════════════
#  AGENT 2: EDITOR (works from extracted facts only)
# ════════════════════════════════════════════════════════════

def _call_editor(facts: dict, groq_key: str) -> dict:
    """Run Agent 2 (Editor). Returns parsed JSON dict with editorial plan."""
    params = _get_tuned_params()
    facts_json = json.dumps(facts, ensure_ascii=False, indent=2)
    user_prompt = EDITOR_USER_PROMPT.format(facts_json=facts_json)

    if groq_key:
        for i, model in enumerate(GROQ_MODELS[:2]):
            truncated, max_output = _truncate_to_window(facts_json, model, desired_output_tokens=1500)
            result = _groq_call(
                groq_key, model,
                EDITOR_SYSTEM_PROMPT,
                EDITOR_USER_PROMPT.format(facts_json=truncated),
                max_output, 0.3, 0.85,  # Editor: low temp for deterministic decisions
                json_mode=True,
            )
            if result:
                try:
                    plan = json.loads(result)
                    if "word_count" in plan and "tone" in plan:
                        logger.info(f"  [Editor] {model} → {plan.get('tone')}, {plan.get('word_count')} words")
                        return plan
                except json.JSONDecodeError:
                    pass
            time.sleep(1)

    # Heuristic fallback
    return _heuristic_editor_plan(facts)


def _heuristic_editor_plan(facts: dict) -> dict:
    """Fallback editor plan when AI editor is unavailable.
    V32: World-class word counts — aligned with the new EDITOR_SYSTEM_PROMPT.
    Old V21 values were too high (3500-10000) and caused LLM cut-offs; the
    new values match what the LLM can actually produce in one call."""
    fact_count = sum(len(v) if isinstance(v, list) else 1 for v in facts.values())
    if fact_count < 5:
        return {"word_count": 2000, "tone": "brief", "structure": "inverted_pyramid",
                "headline_angle": "what just happened and why it matters",
                "headline_style_hint": "punchy_question", "must_keep_facts": []}
    elif fact_count < 10:
        return {"word_count": 3000, "tone": "breaking", "structure": "inverted_pyramid",
                "headline_angle": "the story behind the headline",
                "headline_style_hint": "bold_statement", "must_keep_facts": []}
    elif fact_count < 20:
        return {"word_count": 5000, "tone": "analytical", "structure": "thematic",
                "headline_angle": "why this matters and what comes next",
                "headline_style_hint": "analytical_hook", "must_keep_facts": []}
    return {"word_count": 8000, "tone": "investigative", "structure": "chronological",
            "headline_angle": "the full picture and what it means for you",
            "headline_style_hint": "narrative_quote", "must_keep_facts": []}


# ════════════════════════════════════════════════════════════
#  AGENT 3: JOURNALIST (writes from facts + editorial plan ONLY)
#  NEVER sees original article text — COMPLETE DATA DECOUPLING
# ════════════════════════════════════════════════════════════

def _call_journalist(facts: dict, plan: dict, tone: dict, groq_key: str, gemini_key: str, region: str = "world") -> Optional[str]:
    """Run Agent 3 (Journalist). Returns article markdown or None."""
    params = _get_tuned_params()
    facts_json = json.dumps(facts, ensure_ascii=False, indent=2)
    user_prompt = JOURNALIST_USER_PROMPT.format(
        tone_instruction=tone["instruction"],
        sentence_style=tone["sentence_style"],
        word_count=plan.get("word_count", 2000),
        tone=plan.get("tone", "analytical"),
        structure=plan.get("structure", "thematic"),
        headline_angle=plan.get("headline_angle", ""),
        headline_style_hint=plan.get("headline_style_hint", "bold_statement"),
        must_keep_facts=json.dumps(plan.get("must_keep_facts", []), ensure_ascii=False),
        facts_json=facts_json,
        region=region or "world",
    )
    system_prompt = JOURNALIST_SYSTEM_PROMPT_BASE

    # Strategy 1: Groq
    if groq_key:
        for i, model in enumerate(GROQ_MODELS):
            truncated_facts = facts_json
            if len(facts_json) > 50000:
                truncated_facts = facts_json[:50000] + '\n\n[Facts truncated]'
            truncated_user = user_prompt.replace(facts_json, truncated_facts)
            _, max_output = _truncate_to_window(truncated_user, model, desired_output_tokens=8192)
            result = _groq_call(
                groq_key, model,
                system_prompt,
                truncated_user,
                max_output, params["temperature"], params["top_p"],
            )
            if result and _wc(result) >= 800:
                logger.info(f"  [Journalist] {model} → {_wc(result)} words (temp={params['temperature']}, tone={tone['name']})")
                return result
            if i < len(GROQ_MODELS) - 1:
                time.sleep(2 ** i)

    # Strategy 2: Gemini fallback
    if gemini_key:
        _, max_output = _truncate_to_window(user_prompt, "gemini-2.0-flash", desired_output_tokens=8192)
        result = _gemini_call(
            gemini_key,
            system_prompt,
            user_prompt,
            max_output, params["temperature"], params["top_p"],
        )
        if result and _wc(result) >= 600:
            logger.info(f"  [Journalist] Gemini → {_wc(result)} words (temp={params['temperature']}, tone={tone['name']})")
            return result

    return None


# ════════════════════════════════════════════════════════════
#  FEATURE 4: QUOTE MASKING — Post-processing
#  Catches any remaining direct quotes that slipped through
# ════════════════════════════════════════════════════════════

# Patterns for quotes that need masking
_QUOTE_PATTERNS = [
    # "Quote," Speaker said. → paraphrased
    (re.compile(r'"([^"]+)"\s*,?\s*(?:said|stated|declared|announced|explained|added|noted|told reporters|told AFP|told Reuters|told BBC|said in a statement)\s+([A-Z][a-zA-Z\s]+?)(?:\s*\.|\s*$)', re.M),
     lambda m: _mask_quote(m.group(1), m.group(2).strip())),
    # Speaker said, "Quote." → paraphrased
    (re.compile(r'([A-Z][a-zA-Z\s]+?)\s+(?:said|stated|declared|announced|explained|added|noted)\s*,?\s*"([^"]+)"', re.M),
     lambda m: _mask_quote(m.group(2), m.group(1).strip())),
]


def _mask_quote(quote_text: str, speaker: str) -> str:
    """Convert a direct quote to indirect reported speech."""
    # Attribution verb variety
    verbs = ["indicated", "suggested", "emphasized", "pointed out", "noted",
             "made clear", "signaled", "warned", "stressed", "underscored"]
    verb = random.choice(verbs)
    # V32.1 BUGFIX: Lowercasing the first letter turned "I will impose tariffs"
    # into "i will impose tariffs" — grammatically incorrect. We now lowercase
    # the first letter ONLY if it's not the pronoun "I" (in English, "I" is
    # always uppercase). We also preserve acronyms like "NASA announced".
    paraphrased = quote_text.strip()
    if paraphrased:
        first = paraphrased[0]
        rest = paraphrased[1:]
        # Don't lowercase "I" (English first-person pronoun) or single-letter
        # acronyms followed by a capital ("NASA", "EU", "US", etc.).
        if first == "I" and (not rest or rest[0] in " .,;:!?"):
            # Standalone "I" pronoun — keep uppercase
            pass
        elif first.isupper() and rest and rest[0].isupper():
            # Looks like an acronym (e.g. "NASA", "EUROPE") — preserve case
            pass
        else:
            paraphrased = first.lower() + rest
        # Remove trailing period to avoid double punctuation
        if paraphrased.endswith("."):
            paraphrased = paraphrased[:-1]
    return f"{speaker} {verb} that {paraphrased}"


def _apply_quote_masking(content: str) -> str:
    """Post-processing pass to mask any remaining direct quotes."""
    for pattern, replacer in _QUOTE_PATTERNS:
        content = pattern.sub(replacer, content)
    return content


# ════════════════════════════════════════════════════════════
#  POST-PROCESSING — humanization pass (regex-based, free)
# ════════════════════════════════════════════════════════════

_FORBIDDEN_PATTERNS = [
    (re.compile(r"\b(Furthermore|Moreover|Additionally|In addition),\s*", re.I), ""),
    (re.compile(r"\bIn conclusion,?\s*", re.I), ""),
    (re.compile(r"\bIt is important to note that\s*", re.I), ""),
    (re.compile(r"\bIt is worth noting that\s*", re.I), ""),
    (re.compile(r"\bIn today's world,?\s*", re.I), ""),
    (re.compile(r"\bIn the ever-evolving landscape of\s*", re.I), ""),
    (re.compile(r"\bdelve into\b", re.I), "examine"),
    (re.compile(r"\bleverage(d)?\b", re.I), r"use\1"),
    (re.compile(r"\bA testament to\b", re.I), "Proof of"),
]


def _humanize_pass(content: str) -> str:
    """Light regex pass to strip common AI-tell phrases + quote masking.
    V21: Now also runs _sanitize_content() to strip ALL numbers/signs artifacts
    that AI sometimes leaves in published articles (orphan citations, smart
    quotes, dangling punctuation, etc.).
    V24: Also applies conversational style — short paragraphs, bold key
    terms, FAQ section — to beat Wikipedia's boring academic style."""
    # First: apply quote masking
    content = _apply_quote_masking(content)
    # Then: strip forbidden AI phrases
    for pattern, replacement in _FORBIDDEN_PATTERNS:
        content = pattern.sub(replacement, content)
    # V21: Final sanitizer — strips ALL numbers/signs/punctuation artifacts
    content = _sanitize_content(content)
    # Collapse double spaces (sanitizer may have created new ones)
    content = re.sub(r"  +", " ", content)
    content = re.sub(r"\n{3,}", "\n\n", content)
    # V24: Conversational style pass (Wikipedia-beating)
    content = apply_conversational_style(content)
    return content


# ════════════════════════════════════════════════════════════
#  V24: CONVERSATIONAL STYLE (Wikipedia-beating)
#  Wikipedia writes for academics. We write for humans.
#  - Split paragraphs that are too long (> 3 sentences)
#  - Bold the first occurrence of key terms
#  - Auto-append an FAQ section if missing
#  - Convert passive → active voice (light heuristic)
# ════════════════════════════════════════════════════════════

# Sentence-end splitter (preserves abbreviations like "U.S." and "Dr.")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")

# Light list of common nouns worth bolding on first mention (only proper-ish
# nouns; common words would create noise). Empty by default — admins can
# configure via env BOLD_KEYWORDS="Pakistan,Election,..."
_BOLD_KW = [
    w.strip() for w in os.getenv("BOLD_KEYWORDS", "").split(",")
    if w.strip() and len(w.strip()) >= 4
]


def _split_long_paragraphs(text: str, max_sentences: int = 3) -> str:
    """Break any paragraph longer than `max_sentences` sentences into smaller ones.
    Wikipedia-style walls of text become punchy 2-3 sentence blocks."""
    out_paras = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        # Skip markdown headings, list items, code blocks
        if para.startswith(("#", "-", "*", "```", ">")):
            out_paras.append(para)
            continue
        sentences = _SENT_SPLIT.split(para)
        if len(sentences) <= max_sentences:
            out_paras.append(para)
            continue
        # Re-bundle into chunks of max_sentences
        chunks = []
        for i in range(0, len(sentences), max_sentences):
            chunk = " ".join(sentences[i:i + max_sentences]).strip()
            if chunk:
                chunks.append(chunk)
        out_paras.append("\n\n".join(chunks))
    return "\n\n".join(out_paras)


def _bold_key_terms(text: str) -> str:
    """Bold the FIRST occurrence of each configured keyword.
    Only operates outside markdown links/code spans. Idempotent — won't
    double-bold something that's already **bold**."""
    if not _BOLD_KW:
        return text
    seen = set()
    for kw in _BOLD_KW:
        # Match whole word, case-insensitive, NOT already inside ** or [link](url)
        pattern = re.compile(
            r"(?<!\*\*)(?<!\[)\b(" + re.escape(kw) + r")\b(?!\]|\*\*)(?!\]\()",
            re.IGNORECASE,
        )
        # Find first match outside markdown
        m = pattern.search(text)
        if m and kw.lower() not in seen:
            start, end = m.start(), m.end()
            # Check we're not inside a code span
            before = text[:start]
            if before.count("`") % 2 == 0:
                replacement = f"**{text[start:end]}**"
                text = text[:start] + replacement + text[end:]
                seen.add(kw.lower())
    return text


# V32.1 QUALITY FIX: Removed the generic 3-question FAQ template that was
# appended to EVERY article verbatim. Three problems with it:
#   1. Duplicate content across all articles → Google thin-content penalty.
#   2. The "Is this information verified?" Q&A was self-promotional, not
#      a real user query — readers smell marketing copy and bounce.
#   3. The journalist prompt now generates a TOPIC-SPECIFIC FAQ section
#      with real "People Also Ask" style questions derived from the facts,
#      so this generic filler is no longer needed.
# If the journalist prompt fails to produce a FAQ, we leave the article
# without one rather than pad it with boilerplate.


def _ensure_faq_section(text: str, region: str = "world") -> str:
    """No-op stub kept for backward compatibility — the journalist prompt
    now generates topic-specific FAQs. Generic FAQ templates were removed
    because they created duplicate-content penalties across articles."""
    return text


def apply_conversational_style(text: str, region: str = "world") -> str:
    """V24: Apply all conversational-style transformations.
    Safe to call multiple times — each sub-function is idempotent."""
    if not text or len(text) < 100:
        return text
    try:
        text = _split_long_paragraphs(text, max_sentences=3)
        text = _bold_key_terms(text)
        text = _ensure_faq_section(text, region=region)
    except Exception as e:
        logger.debug(f"Conversational style pass failed (non-fatal): {e}")
    return text


def _sanitize_content(content: str) -> str:
    """V21: Comprehensive content sanitizer.

    Fixes the 'numbers and signs appearing in published articles' bug by
    stripping ALL common AI / web-scraping artifacts:

    1. Reference markers:     [1], [2], [^1], [citation needed], (1), (Source 3)
    2. Bare URL-only lines:   https://example.com/page
    3. Hash-like strings:     a1b2c3... (40+ hex chars)
    4. DOI references:        doi:10.xxxx/yyy
    5. Source attribution:    "Source: CNN", "Via Reuters", "Retrieved from..."
    6. Empty markdown:        **  ***  ###  ##  (orphan formatting)
    7. Smart quotes/em-dashes:    " " ' ' ... -> " " ' ' ... (normalize)
    8. Orphan punctuation:    ", .", " ."  (left by phrase removal)
    9. Trailing commas:       "..., " at end of sentence
    10. Mid-sentence numbers in brackets:  "increased 5% [3]." -> "increased 5%."
    11. Empty list markers:   "- " or "* " with nothing after
    12. Encoded HTML entities: &amp; &lt; &gt; (already-escaped once, don't double-escape)
    13. Trailing whitespace per line
    14. Multiple blank lines
    """
    if not content:
        return ""

    # ── Step 1: Normalize Unicode smart quotes / dashes / ellipsis to ASCII ──
    # This is THE most common cause of "weird signs" in published articles.
    smart_replacements = {
        "\u2018": "'",   # left single quote
        "\u2019": "'",   # right single quote / apostrophe
        "\u201A": "'",   # single low-9 quote
        "\u201B": "'",   # single high-reversed-9
        "\u201C": '"',   # left double quote
        "\u201D": '"',   # right double quote
        "\u201E": '"',   # double low-9 quote
        "\u2013": "-",   # en dash -> hyphen
        # V32.1: Preserve em dash (U+2014) as the literal "—" character.
        # The journalist prompt explicitly instructs the LLM to use em-dashes
        # for editorial voice ("Use em-dashes (—) and parenthetical asides"
        # and "Use one em-dash per ~500 words"). The previous sanitizer
        # collapsed em-dashes to hyphens, defeating the editorial style
        # instruction and producing flat, AI-detector-friendly prose.
        # Em-dashes render correctly in every modern browser — no need to
        # sanitize them away.
        "\u2026": "...", # ellipsis
        "\u00A0": " ",   # non-breaking space -> regular space
        "\u2022": "-",   # bullet -> dash (we use CSS for bullets)
        "\u2027": "-",   # hyphenation point
        "\u2043": "-",   # hyphen bullet
        "\u2212": "-",   # minus sign
        "\u2010": "-",   # hyphen
        "\u2011": "-",   # non-breaking hyphen
        "\uFEFF": "",    # zero-width no-break space (BOM)
        "\u200B": "",    # zero-width space
        "\u200C": "",    # zero-width non-joiner
        "\u200D": "",    # zero-width joiner
        "\u00AD": "",    # soft hyphen
    }
    for smart, plain in smart_replacements.items():
        content = content.replace(smart, plain)

    # ── Step 2: Strip reference markers EVERYWHERE (not just at line start) ──
    # [1], [2], [12], [1,2,3], [^1], [^12]
    content = re.sub(r"\[\^?\d+(?:[-,\s\d]+)?\]", "", content)
    # [citation needed], [source needed], [verification needed]
    content = re.sub(r"\[(?:citation|source|verification|further|dubious)\s+needed\]", "", content, flags=re.IGNORECASE)
    # (1), (2), (12) — only when they look like inline references, NOT prices like ($5) or (1-0)
    # Match (digits) preceded by a word/space (not $ or letter)
    content = re.sub(r"(?<=\s)\(\d{1,3}\)", "", content)
    # (Source 3), (Ref 2), (Citation 5)
    content = re.sub(r"\((?:Source|Ref|Citation|Reference)\s+\d+\)", "", content, flags=re.IGNORECASE)
    # V21: (Source: name) — inline source attribution without a number, e.g. "(Source: Reuters)"
    content = re.sub(r"\((?:Source|Ref|Citation|Reference):\s*[^)]+\)", "", content, flags=re.IGNORECASE)
    # V21: "(Via Reuters)" / "(Per CNN)" inline attributions
    content = re.sub(r"\((?:Via|Per|According to):\s*[^)]+\)", "", content, flags=re.IGNORECASE)

    # ── Step 3: Remove AI source attribution sections entirely ──
    # Matches "## Sources" / "### References" / "## Source Attribution" and everything until
    # the next ## heading or META/KEYWORDS or end of string
    content = re.sub(
        r"^#{1,3}\s*(?:Sources?|References?|Citations?|Source\s+Attribution|Source\s+Credit|Bibliography)\s*\n[\s\S]*?(?=\n#{1,3}\s|\nMETA:|\nKEYWORDS:|$(?![\s\S]))",
        "",
        content,
        flags=re.MULTILINE | re.IGNORECASE,
    )

    # ── Step 4: Remove leftover META: and KEYWORDS: lines ──
    content = re.sub(r"^META:.*$", "", content, flags=re.MULTILINE)
    content = re.sub(r"^KEYWORDS:.*$", "", content, flags=re.MULTILINE)

    # ── Step 5: Remove bare URL-only lines ──
    content = re.sub(r"^\s*https?://\S+\s*$", "", content, flags=re.MULTILINE)

    # ── Step 6: Remove long hash-like strings (32+ hex chars on their own line) ──
    content = re.sub(r"^\s*[a-f0-9]{32,}\s*$", "", content, flags=re.MULTILINE | re.IGNORECASE)

    # ── Step 7: Remove DOI-style references ──
    content = re.sub(r"^\s*doi:\s*\S+\s*$", "", content, flags=re.MULTILINE | re.IGNORECASE)

    # ── Step 8: Remove "Source: ...", "Via ...", "Retrieved from: ..." standalone lines ──
    content = re.sub(
        r"^\s*(?:Source|Via|Retrieved\s+from|Available\s+at|Read\s+more\s+at|See\s+also|h/t):\s*\S.*$",
        "",
        content,
        flags=re.MULTILINE | re.IGNORECASE,
    )

    # ── Step 9: Remove empty markdown formatting (orphan **, ###, etc.) ──
    # **  ***  ____  ----  ====  (sequences of just punctuation/symbols)
    content = re.sub(r"^\s*\*{2,}\s*$", "", content, flags=re.MULTILINE)
    content = re.sub(r"^\s*#{2,}\s*$", "", content, flags=re.MULTILINE)
    content = re.sub(r"^\s*[-=]{3,}\s*$", "", content, flags=re.MULTILINE)
    # Orphan bold/italic markers: **word** where word is empty -> just **
    content = re.sub(r"\*\*(?=\s)", "", content)  # ** followed by space
    content = re.sub(r"(?<=\s)\*\*", "", content)  # ** preceded by space

    # ── Step 10: Fix orphan punctuation left by AI-phrase removal ──
    # The regex pass removes "Furthermore, " etc., which can leave ", word" or " .word" patterns
    # Fix ". Word" at start of sentence (orphan period)
    content = re.sub(r"(?<=\n)\s*\.\s+([A-Z])", r"\1", content)
    # Fix ", word" at start of line (orphan comma)
    content = re.sub(r"(?<=\n)\s*,\s+", "", content)
    # Fix " ," -> ","
    content = re.sub(r"\s+,", ",", content)
    # Fix " ." -> "."
    content = re.sub(r"\s+\.", ".", content)
    # Fix " ;" -> ";"
    content = re.sub(r"\s+;", ";", content)
    # Fix " :" -> ":"
    content = re.sub(r"\s+:", ":", content)
    # Fix double periods "..."  that aren't ellipsis intent -> "."
    content = re.sub(r"\.{2}(?!\.)", ".", content)
    # Fix ",." -> "."
    content = re.sub(r",\.", ".", content)
    # Fix "., " -> ". "
    content = re.sub(r"\.,", ".", content)

    # ── Step 11: Fix orphan parentheses ──
    # " )" -> ")" and "( " -> "("
    content = re.sub(r"\s+\)", ")", content)
    content = re.sub(r"\(\s+", "(", content)
    # Empty parens "()" -> ""
    content = re.sub(r"\(\s*\)", "", content)

    # ── Step 12: Remove empty list markers (lines with just "- " or "* ") ──
    content = re.sub(r"^\s*[-*]\s*$", "", content, flags=re.MULTILINE)

    # ── Step 13: Fix double-escaped HTML entities ──
    # If AI emitted &amp;lt; instead of &lt;, decode once
    content = content.replace("&amp;lt;", "&lt;").replace("&amp;gt;", "&gt;")
    content = content.replace("&amp;amp;", "&amp;")

    # ── Step 14: Strip trailing whitespace per line ──
    content = re.sub(r"[ \t]+$", "", content, flags=re.MULTILINE)

    # ── Step 15: Collapse 3+ blank lines into 2 ──
    content = re.sub(r"\n{3,}", "\n\n", content)

    # ── Step 16: Strip leading/trailing whitespace on whole content ──
    content = content.strip()

    return content


# ════════════════════════════════════════════════════════════
#  PUBLIC ENTRYPOINT — 3-Agent Pipeline
# ════════════════════════════════════════════════════════════

def rewrite_article(text: str, fallback_title: str = "", region: str = "world") -> dict:
    """
    V11 Three-Agent Rewrite Pipeline with Data Decoupling:
    1. Fact Extractor: source text → JSON facts (original text DESTROYED)
    2. Editor: JSON facts → editorial plan
    3. Journalist: editorial plan + JSON facts → original article (NEVER sees source)

    Also includes: Random Tone Selection, Tuned API Params, Quote Masking.
    Returns {title, body, meta_desc, keywords}.
    """
    keys = REGION_KEYS.get(region, REGION_KEYS["world"])
    source_wc = _wc(text)

    # ── Step 1: FACT EXTRACTION (Data Decoupling — Step 1) ──
    logger.info(f"  [{region}] Starting 3-agent pipeline (source: {source_wc} words)")
    facts = _call_fact_extractor(text, keys["groq"], keys["gemini"])
    if facts is None:
        facts = _fallback_fact_extraction(text)

    # ★ ORIGINAL TEXT IS NOW DESTROYED — journalist will NEVER see it ★
    # We deliberately do NOT pass `text` to any subsequent agent.
    facts_json_str = json.dumps(facts, ensure_ascii=False)
    logger.info(f"  [{region}] Facts extracted: {len(facts_json_str)} chars — original text decoupled")

    # ── Step 2: EDITOR (works from facts only) ──
    plan = _call_editor(facts, keys["groq"])
    if plan is None:
        plan = _heuristic_editor_plan(facts)
    target_wc = int(plan.get("word_count", 2000))
    logger.info(f"  [{region}] Editor plan: tone={plan.get('tone')}, target={target_wc}w")

    # ── Step 3: JOURNALIST with RANDOM TONE (works from facts only) ──
    tone = _pick_random_tone()
    # V32: Lower accept threshold to 60% of target (was 50% with 1500 floor).
    # This allows the journalist to hit closer to the target without being
    # rejected for being slightly short. If the LLM produces at least 60%
    # of the target word count, we accept it (otherwise we retry).
    accept_threshold = max(800, int(target_wc * 0.6))
    result = _call_journalist(facts, plan, tone, keys["groq"], keys["gemini"])

    if result and _wc(result) >= accept_threshold:
        result = _humanize_pass(result)  # Includes quote masking
        return _parse(result, fallback_title)

    # ── Final fallback ──
    logger.error(f"  [{region}] All AI strategies failed — emergency format")
    return {
        "title": fallback_title,
        "body": _emergency_from_facts(facts),
        "meta_desc": fallback_title[:155],
        "keywords": "",
        "tldr_summary": _generate_tldr(_emergency_from_facts(facts)),
    }


# ════════════════════════════════════════════════════════════
#  PARSING + UTILS
# ════════════════════════════════════════════════════════════

def _emergency_from_facts(facts: dict) -> str:
    """Fallback article from extracted facts when all AI services fail."""
    sections = []
    if facts.get("what_happened"):
        sections.append(f"## Overview\n\n{facts['what_happened']}")
    if facts.get("who"):
        sections.append(f"## Key People\n\n" + ", ".join(facts["who"]))
    if facts.get("numbers"):
        num_lines = [f"- {n.get('context', n.get('value', ''))}: {n.get('value', '')}" for n in facts["numbers"]]
        sections.append("## Key Numbers\n\n" + "\n".join(num_lines))
    if facts.get("context"):
        sections.append("## Background\n\n" + "\n\n".join(str(c) for c in facts["context"][:5]))
    return "\n\n".join(sections) if sections else "## Overview\n\nNo details available."


def _emergency(text: str) -> str:
    """Plain-text fallback (legacy — used only if fact extraction also fails)."""
    paras = [p.strip() for p in text.split("\n") if p.strip() and len(p.strip()) > 30]
    return "## Overview\n\n" + "\n\n".join(paras[:20])


def _wc(text: str) -> int:
    return len(text.split()) if text else 0


def _parse(content: str, fallback_title: str) -> dict:
    """Parse the journalist's output into {title, body, meta_desc, keywords, tldr_summary}.
    V18: Also extracts a TL;DR summary from the article body if present,
    or generates one from the first 3 key sentences."""
    if not content:
        return {"title": fallback_title, "body": _emergency(""), "meta_desc": fallback_title[:155], "keywords": "", "tldr_summary": ""}

    lines = content.strip().split("\n")
    title = fallback_title
    meta_desc = ""
    keywords = ""
    body_lines = []
    found_title = False

    for line in lines:
        s = line.strip()
        if s.startswith("# ") and not found_title:
            title = s[2:].strip()
            found_title = True
        elif s.startswith("META:"):
            meta_desc = s[5:].strip()
        elif s.startswith("KEYWORDS:"):
            keywords = s[9:].strip()
        else:
            body_lines.append(line)

    body = "\n".join(body_lines).strip()
    body = re.sub(r"^META:.*$", "", body, flags=re.MULTILINE).strip()
    body = re.sub(r"^KEYWORDS:.*$", "", body, flags=re.MULTILINE).strip()
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    if not meta_desc:
        clean = re.sub(r"[#*\[\]]", "", body)
        meta_desc = " ".join(clean.split())[:155]

    # V18: Generate TL;DR summary (3 bullet points from the article)
    tldr = _generate_tldr(body)

    logger.info(f"  Final article: {_wc(body)} words")
    return {"title": title, "body": body, "meta_desc": meta_desc, "keywords": keywords, "tldr_summary": tldr}


def _generate_tldr(body: str) -> str:
    """V18: Generate a 3-bullet TL;DR summary from the article body.
    Strategy: Extract first 3 meaningful sentences from paragraphs that aren't headings.
    Falls back to empty string if body is too short."""
    if not body or len(body.strip()) < 100:
        return ""

    # Split into paragraphs and skip headings
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    candidate_sentences = []
    for para in paragraphs:
        # Skip markdown headings, lists, and short paragraphs
        if para.startswith("#"):
            continue
        if para.startswith("-") or para.startswith("*"):
            continue
        if len(para) < 50:
            continue
        # Split paragraph into sentences
        import re as _re
        sentences = _re.split(r'(?<=[.!?])\s+', para)
        for sent in sentences:
            sent = sent.strip()
            # Skip sentences that are too short or look like metadata
            if len(sent) < 40:
                continue
            if sent.startswith("META:") or sent.startswith("KEYWORDS:"):
                continue
            # Clean markdown
            clean = _re.sub(r"[*_`\[\]]", "", sent)
            if clean and len(clean) > 40:
                candidate_sentences.append(clean)
            if len(candidate_sentences) >= 3:
                break
        if len(candidate_sentences) >= 3:
            break

    if not candidate_sentences:
        return ""

    # Take first 3, format as bullets
    bullets = candidate_sentences[:3]
    return "\n".join(f"• {b}" for b in bullets)


def make_slug(title: str) -> str:
    """SEO-friendly URL slug."""
    slug = re.sub(r"[^a-z0-9\s-]", "", title.lower())
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)
    return slug[:80]


def make_article_hash(title: str, text: str) -> str:
    """Hash for deduplication based on title + first 500 chars."""
    content = (title + text[:500]).lower().strip()
    return hashlib.sha256(content.encode()).hexdigest()


# ════════════════════════════════════════════════════════════
#  V23: DEEP INTERNAL LINKING (Wikipedia-style auto-links)
#  Scans new article body for phrases matching existing article titles
#  or keywords, and converts them into <a href="/article/{slug}"> links.
#  This creates the dense web of cross-references that makes Wikipedia
#  rank so well — every page links to many related pages.
# ════════════════════════════════════════════════════════════

# Markdown special chars we must avoid when inserting links
_MD_SPECIAL = set("[]()`*_~#<>")

def _is_inside_link_or_code(text: str, pos: int) -> bool:
    """Heuristic: is the position inside a markdown link, code span, or
    inline code? Crude but effective — looks for the nearest opening
    delimiter to the left and checks if it's unclosed."""
    # Look back for unbalanced [ or `
    brackets = 0
    backticks = 0
    i = pos - 1
    while i >= 0:
        c = text[i]
        if c == '`':
            backticks += 1
        elif c == ']':
            brackets -= 1
        elif c == '[':
            brackets += 1
        # Stop scanning if we hit a paragraph break
        if c == '\n' and i + 1 < pos and text[i + 1] == '\n':
            break
        i -= 1
    return (backticks % 2) != 0 or brackets > 0


def _is_word_boundary(text: str, start: int, end: int) -> bool:
    """Check that match at [start, end) is bounded by non-word characters
    (so we don't link 'Iran' inside 'Pakistani')."""
    if start > 0:
        prev = text[start - 1]
        if prev.isalnum() or prev in "-_":
            return False
    if end < len(text):
        nxt = text[end]
        if nxt.isalnum() or nxt in "-_":
            return False
    return True


def apply_internal_links(body: str, existing_articles: list, max_links: int = 8) -> str:
    """Wikipedia-style auto-linking.

    Args:
        body:               The article body (markdown).
        existing_articles:  Iterable of Article objects (must have .title
                            and .slug attributes). The new article itself
                            must NOT be in this list (avoid self-links).
        max_links:          Hard cap on inserted links — prevents SEO
                            over-optimization penalty (~5-10 is safe).

    Returns:
        The body with up to `max_links` phrases converted to markdown links:
            [phrase](/article/{slug})

    Strategy: for each existing article, try matching its title at
    progressively shorter lengths (first 6 words → 5 → 4 → 3). This
    catches both verbatim title mentions AND shorter, more natural
    references that appear in body text.
    """
    if not body or not existing_articles:
        return body

    # ── 1. Build candidate phrases for each article ──
    # For each article we generate up to 4 candidate phrases of decreasing
    # length. Longest candidates win (sorted first), so we prefer the
    # most specific match when multiple candidates would match.
    candidates = []  # list of (phrase_lower, slug, original_phrase)
    seen_phrases = set()
    for art in existing_articles:
        title = getattr(art, "title", "") or ""
        slug = getattr(art, "slug", "") or ""
        if not slug or not title:
            continue
        # V23.1: Strip HTML tags, parentheticals, and odd punctuation
        title_clean = re.sub(r"<[^>]+>", " ", title)             # HTML tags
        title_clean = re.sub(r"&[a-zA-Z]+;", " ", title_clean)    # HTML entities
        title_clean = re.sub(r"\s+", " ", title_clean).strip()
        title_clean = re.sub(r"[\(\[][^\)\]]*[\)\]]", "", title_clean).strip()
        # Strip leading non-word chars
        title_clean = re.sub(r"^[^A-Za-z0-9]+", "", title_clean)
        if len(title_clean) < 10:
            continue
        words = title_clean.split()
        if len(words) < 2:
            continue

        # Generate candidate phrases at multiple lengths: 6, 5, 4, 3 words
        # (or fewer if the title itself is shorter)
        lengths = sorted(set([3, 4, 5, 6, len(words)]))
        lengths = [l for l in lengths if l <= len(words) and l >= 3]
        for n in lengths:
            phrase = " ".join(words[:n])
            key = phrase.lower()
            if key in seen_phrases:
                continue
            seen_phrases.add(key)
            candidates.append((key, slug, phrase))

    # Sort candidates by phrase length DESC — link longest matches first
    # so "Pakistan General Election Results 2024" is linked before
    # "Pakistan General Election Results" before "Pakistan General Election".
    candidates.sort(key=lambda c: len(c[0]), reverse=True)

    # ── 2. Walk body, replacing the first occurrence of each phrase ──
    body_lower = body.lower()
    occupied: list[tuple[int, int]] = []
    links_inserted = 0
    used_slugs: set[str] = set()  # don't link the same article twice

    def _overlaps(start: int, end: int) -> bool:
        for s, e in occupied:
            if start < e and end > s:
                return True
        return False

    for phrase_lower, slug, original_phrase in candidates:
        if links_inserted >= max_links:
            break
        if slug in used_slugs:
            continue  # already linked this article
        search_from = 0
        while True:
            idx = body_lower.find(phrase_lower, search_from)
            if idx < 0:
                break
            end = idx + len(phrase_lower)
            if (
                _is_word_boundary(body, idx, end)
                and not _is_inside_link_or_code(body, idx)
                and not _overlaps(idx, end)
            ):
                original_text = body[idx:end]
                link_md = f"[{original_text}](/article/{slug})"
                body = body[:idx] + link_md + body[end:]
                occupied.append((idx, idx + len(link_md)))
                occupied.sort()
                body_lower = body.lower()
                links_inserted += 1
                used_slugs.add(slug)
                break
            search_from = idx + 1

    logger.info(f"  [V23 Interlink] Inserted {links_inserted} internal links "
                f"(of {len(candidates)} candidates)")
    return body