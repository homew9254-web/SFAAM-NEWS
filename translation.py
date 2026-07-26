"""
translation.py - SFAAM NEWS V26 — Translation + TTS narration
=============================================================

Provides:
  - translate_article()       : Google Translate free endpoint (no API key)
  - generate_audio_narration(): edge-tts (preferred) or gTTS (fallback)
  - generate_daily_podcast()  : concatenate TTS for top 10 articles
  - SUPPORTED_LANGS           : list of supported language codes

This module is imported lazily by main.py only when the relevant endpoints
are hit, so missing optional deps (edge-tts, gTTS) don't break app startup.

Env vars:
  AUDIO_DIR  — directory for audio files (default: ./static/audio)
  TTS_VOICE  — edge-tts voice (default: en-US-AriaNeural)

Notes:
  - Google Translate's free endpoint is undocumented and rate-limited;
    for production scale, switch to the official Cloud Translation API.
  - edge-tts is Microsoft Edge's neural TTS — free, no API key. Falls
    back to gTTS (Google Translate TTS) if edge-tts is unavailable.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Supported languages (BCP-47 codes)
SUPPORTED_LANGS = {
    "en": {"name": "English", "tts_voice": "en-US-AriaNeural"},
    "ur": {"name": "Urdu", "tts_voice": "ur-PK-UzmaNeural"},
    "ar": {"name": "Arabic", "tts_voice": "ar-SA-HamedNeural"},
    "hi": {"name": "Hindi", "tts_voice": "hi-IN-SwaraNeural"},
    "es": {"name": "Spanish", "tts_voice": "es-ES-ElviraNeural"},
    "fr": {"name": "French", "tts_voice": "fr-FR-DeniseNeural"},
    "de": {"name": "German", "tts_voice": "de-DE-KatjaNeural"},
    "fa": {"name": "Persian", "tts_voice": "fa-IR-DilaraNeural"},
}

AUDIO_DIR = Path(os.getenv("AUDIO_DIR", os.path.join("static", "audio")))
AUDIO_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────
# Translation via Google Translate's free endpoint
# ─────────────────────────────────────────────────────────────
GOOGLE_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"


async def _google_translate_chunk(text: str, target_lang: str, source_lang: str = "en") -> str:
    """Translate a single chunk (≤4500 chars) using Google Translate's free
    undocumented endpoint. Returns the translated text, or empty string on
    failure. Rate-limited; not for high-volume production use."""
    if not text or not text.strip():
        return ""
    if target_lang == source_lang:
        return text
    params = {
        "client": "gtx",
        "sl": source_lang,
        "tl": target_lang,
        "dt": "t",
        "q": text[:4500],  # Google limits ~5000 chars per request
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(GOOGLE_TRANSLATE_URL, params=params,
                                 headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                logger.warning(f"[Translation] HTTP {r.status_code} from Google Translate")
                return ""
            data = r.json()
            if not data or not isinstance(data, list) or not data[0]:
                return ""
            parts = [chunk[0] for chunk in data[0] if chunk and chunk[0]]
            return "".join(parts)
    except Exception as e:
        logger.warning(f"[Translation] failed: {type(e).__name__}: {e}")
        return ""


async def _google_translate(text: str, target_lang: str, source_lang: str = "en") -> str:
    """Translate text of ANY length by splitting on paragraph / sentence
    boundaries into ≤4000-char chunks and translating them concurrently.

    V32.1 BUGFIX: The previous implementation called Google Translate with
    text[:4500], which silently dropped everything past character 4500 —
    meaning half of any 3000-word article (~18K chars) stayed in English.
    This broke the multilingual UX for all 8 supported languages.
    """
    if not text or not text.strip():
        return ""
    if target_lang == source_lang:
        return text

    # Split into chunks at paragraph boundaries when possible, else at
    # sentence boundaries, else hard-wrap. Target ~3500 chars per chunk so
    # we stay safely under Google's ~5000-char limit even after URL-encoding.
    CHUNK_MAX = 3500
    if len(text) <= CHUNK_MAX:
        return await _google_translate_chunk(text, target_lang, source_lang)

    chunks: list[str] = []
    # Split on double-newline (paragraph) first, then single newline, then
    # sentence end, then hard-wrap.
    paragraphs = re.split(r"(?<=\n\n)", text)
    buf = ""
    for para in paragraphs:
        if len(buf) + len(para) <= CHUNK_MAX:
            buf += para
        else:
            if buf:
                chunks.append(buf)
                buf = ""
            if len(para) <= CHUNK_MAX:
                buf = para
            else:
                # Paragraph itself too big — split on single newlines / sentences.
                sentences = re.split(r"(?<=[.!?。！？])\s+", para)
                for sent in sentences:
                    if len(buf) + len(sent) <= CHUNK_MAX:
                        buf += sent + " "
                    else:
                        if buf:
                            chunks.append(buf.rstrip())
                            buf = ""
                        if len(sent) <= CHUNK_MAX:
                            buf = sent + " "
                        else:
                            # Sentence itself too big — hard-wrap.
                            for i in range(0, len(sent), CHUNK_MAX):
                                chunk = sent[i:i + CHUNK_MAX]
                                if len(buf) + len(chunk) <= CHUNK_MAX:
                                    buf += chunk
                                else:
                                    if buf:
                                        chunks.append(buf.rstrip())
                                    buf = chunk
    if buf:
        chunks.append(buf.rstrip())

    if not chunks:
        return await _google_translate_chunk(text, target_lang, source_lang)

    # Translate all chunks concurrently (Google endpoint is stateless).
    results = await asyncio.gather(*[
        _google_translate_chunk(c, target_lang, source_lang) for c in chunks
    ])
    return "\n\n".join(r for r in results if r)


async def translate_article(
    *,
    article_id: int,
    target_lang: str,
    title: str,
    body: str,
    source_lang: str = "en",
) -> dict:
    """Translate an article's title + body to the requested language.
    Returns dict with: title, body, lang, original_lang, article_id.
    On failure, returns {"error": "..."}."""
    if target_lang not in SUPPORTED_LANGS:
        return {"error": f"Unsupported language: {target_lang}"}

    # Translate title + body in parallel (independent requests).
    # V32.1: Body is no longer hard-truncated — _google_translate now chunks
    # internally so the FULL article body gets translated.
    body_full = body or ""
    title_translated, body_translated = await asyncio.gather(
        _google_translate(title or "", target_lang, source_lang),
        _google_translate(body_full, target_lang, source_lang),
    )

    if not title_translated and not body_translated:
        return {"error": "Translation failed (both title and body empty)"}

    return {
        "article_id": article_id,
        "lang": target_lang,
        "lang_name": SUPPORTED_LANGS[target_lang]["name"],
        "original_lang": source_lang,
        "title": title_translated or title,
        "body": body_translated or body_full,
    }


# ─────────────────────────────────────────────────────────────
# Text-To-Speech (TTS) narration
# ─────────────────────────────────────────────────────────────
def _strip_markdown_for_tts(text: str) -> str:
    """Convert markdown to plain text suitable for TTS reading.
    Same logic as main.py's _strip_markdown_for_tts — kept here so the
    translation module is self-contained."""
    if not text:
        return ""
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^\s*>\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _edge_tts_voice_for_lang(lang: str) -> str:
    """Pick an edge-tts voice for the given language code."""
    lang_info = SUPPORTED_LANGS.get(lang)
    if lang_info:
        return lang_info["tts_voice"]
    return "en-US-AriaNeural"


async def _edge_tts_generate(text: str, output_path: Path, voice: str) -> bool:
    """Generate audio using edge-tts. Returns True on success."""
    try:
        import edge_tts
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(output_path))
        return output_path.exists() and output_path.stat().st_size > 1000
    except ImportError:
        logger.info("[TTS] edge-tts not installed — trying gTTS fallback")
        return False
    except Exception as e:
        logger.warning(f"[TTS] edge-tts failed: {e}")
        return False


def _gtts_generate(text: str, output_path: Path, lang: str = "en") -> bool:
    """Generate audio using gTTS (Google Translate TTS). Sync."""
    try:
        from gtts import gTTS
        tts = gTTS(text=text, lang=lang, slow=False)
        tts.save(str(output_path))
        return output_path.exists() and output_path.stat().st_size > 1000
    except ImportError:
        logger.warning("[TTS] gTTS not installed — TTS unavailable")
        return False
    except Exception as e:
        logger.warning(f"[TTS] gTTS failed: {e}")
        return False


async def generate_audio_narration(
    text: str,
    *,
    lang: str = "en",
    article_id: int = 0,
) -> dict:
    """Generate an MP3 narration of `text` in the requested language.
    Returns {"path": "/static/audio/...", "size_bytes": N} on success,
    or {"error": "..."} on failure."""
    if not text or len(text.strip()) < 50:
        return {"error": "Text too short for narration"}

    # V32.1: Raised TTS cap from 5000 → 15000 chars (~15 min of audio).
    # The previous 5000-char cap meant any article longer than ~5 minutes
    # was cut off mid-sentence, often mid-section — readers who tapped
    # "Listen to this article" on a 3000-word long-read only got the intro.
    # 15000 chars covers ~2500 words; articles beyond that get a graceful
    # "Continued in next listening session" notice appended.
    TTS_CHAR_CAP = 15000
    if len(text) > TTS_CHAR_CAP:
        text = text[:TTS_CHAR_CAP].rsplit(". ", 1)[0] + ". " \
               "Continued in next listening session."
    plain = _strip_markdown_for_tts(text)
    if not plain:
        return {"error": "No speakable text after markdown strip"}

    # Filename is deterministic per (article_id, lang, text-hash) so cached
    # files are reused if the user requests the same narration again.
    text_hash = hashlib.sha256(plain.encode()).hexdigest()[:10]
    filename = f"narration_{article_id}_{lang}_{text_hash}.mp3"
    output_path = AUDIO_DIR / filename

    # Reuse cached file if it already exists
    if output_path.exists() and output_path.stat().st_size > 1000:
        return {
            "path": f"/static/audio/{filename}",
            "size_bytes": output_path.stat().st_size,
            "cached": True,
        }

    # Strategy 1: edge-tts (preferred — neural voice)
    voice = _edge_tts_voice_for_lang(lang)
    if await _edge_tts_generate(plain, output_path, voice):
        return {
            "path": f"/static/audio/{filename}",
            "size_bytes": output_path.stat().st_size,
            "engine": "edge-tts",
            "voice": voice,
        }

    # Strategy 2: gTTS (sync — run in thread)
    try:
        success = await asyncio.to_thread(_gtts_generate, plain, output_path, lang)
        if success:
            return {
                "path": f"/static/audio/{filename}",
                "size_bytes": output_path.stat().st_size,
                "engine": "gtts",
            }
    except Exception as e:
        logger.warning(f"[TTS] gTTS async wrapper failed: {e}")

    return {"error": "TTS generation failed — install edge-tts or gtts"}


async def generate_daily_podcast(
    articles: list[dict],
    *,
    lang: str = "en",
) -> dict:
    """Generate a single MP3 'podcast' that narrates the titles + summaries
    of the top N articles.

    Args:
        articles: list of {title, summary} dicts (max 10 recommended).
        lang: target language.

    Returns:
        {"path": "/static/audio/...", "size_bytes": N, "articles": N} or {"error": ...}
    """
    if not articles:
        return {"error": "No articles to narrate"}

    # Build the podcast script
    parts = ["Welcome to SFAAM NEWS daily podcast. Here are today's top stories."]
    for i, a in enumerate(articles, 1):
        title = a.get("title", "").strip()
        summary = (a.get("summary") or "").strip()
        if not title:
            continue
        parts.append(f"Story {i}. {title}.")
        if summary:
            parts.append(summary)
    parts.append("That's all for today. Thank you for listening to SFAAM NEWS.")
    script = " ".join(parts)

    # Cap to ~15000 chars (about 15 minutes of audio)
    script = script[:15000]

    # Use the same narration engine
    result = await generate_audio_narration(script, lang=lang, article_id=0)
    if "error" in result:
        return result
    return {
        "path": result["path"],
        "size_bytes": result["size_bytes"],
        "articles": len(articles),
        "engine": result.get("engine", "unknown"),
    }
