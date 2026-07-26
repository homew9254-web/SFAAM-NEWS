"""
resilient_llm.py - SFAAM Automated News Engine (V30 / TRD v1.0)
================================================================
LLM Routing & Resiliency Logic (TRD Section 2)
-----------------------------------------------
    "Primary Engine: Groq API (utilizing Llama-3-70b or equivalent)
     Secondary/Fallback Engine: ... the execution pipeline must
     instantly switch to the Gemini API (Gemini 1.5 Pro) using the
     corresponding regional key."

    "Rate-Limit Exponential Backoff: Implement a sliding sleep timer
     (e.g. wait 2s, then 4s, then 8s) if both Groq and Gemini APIs
     return transient network errors, before marking the queue item
     as failed."  (TRD Section 6)

This module provides a single entry point — `call_llm_with_fallback()` —
that wraps every LLM call with:

  1. Primary: Groq (Llama-3.3-70B-Versatile + model fallback chain)
  2. Fallback: Gemini (gemini-2.0-flash or 1.5 Pro)
  3. Exponential backoff: 2s → 4s → 8s on transient failures
  4. Per-region key isolation (passed in by caller, never read from os.getenv)
  5. Detailed status reporting (which provider succeeded, retry count)

DESIGN NOTES
------------
• This module is INTENTIONALLY stateless and synchronous. The async
  layer is the responsibility of the caller (asyncio.to_thread wrapper).
• We do NOT silently swallow errors — every failure is logged with
  enough detail for post-mortem debugging.
• The module is importable without groq/google-generativeai installed
  (lazy imports inside the call functions) — so unit tests can mock it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Retry configuration (TRD Section 6)
# ─────────────────────────────────────────────────────────────
# Sliding exponential backoff: 2s → 4s → 8s
# After 3 attempts on a single provider, we switch to the fallback.
DEFAULT_BACKOFF_SECONDS = [2, 4, 8]
DEFAULT_MAX_ATTEMPTS_PER_PROVIDER = 3

# TRD Section 6: "configure maximum output tokens (e.g. max_tokens: 4000)
# and instruct the prompt to avoid mid-sentence cut-offs."
DEFAULT_MAX_TOKENS = 4000
LONG_FORM_MAX_TOKENS = 8000  # for 1500-2500+ word articles


# ─────────────────────────────────────────────────────────────
# Model registries (with fallback chains)
# ─────────────────────────────────────────────────────────────
# Groq models — try each in order. If a model is deprecated/decommissioned
# by Groq, the next one is used.
GROQ_MODELS = [
    "llama-3.3-70b-versatile",   # Primary (high-throughput, low-latency)
    "llama-3.1-8b-instant",       # Fallback (smaller, faster)
]

# Gemini models — TRD specifies "Gemini 1.5 Pro" but we also keep newer
# 2.0 Flash as the primary because it's cheaper and equally capable for
# news synthesis. Falls back to 1.5 Pro if 2.0 Flash fails.
GEMINI_MODELS = [
    "gemini-2.0-flash",           # Primary (cheap, fast)
    "gemini-1.5-pro",             # Fallback (TRD-specified)
    "gemini-1.5-flash",           # Last resort (smaller)
]


# ─────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────
@dataclass
class LLMCallResult:
    """Result of an LLM call with full audit metadata.

    `success` is True only if we got non-empty text back.
    `provider` is "groq" | "gemini" | "" (empty on total failure).
    `model` is which specific model succeeded.
    `attempts` is the list of (provider, model, error) tuples we tried.
    `text` is the generated content (empty on failure).
    """
    success: bool
    text: str
    provider: str = ""           # "groq" | "gemini" | ""
    model: str = ""              # which model succeeded
    attempts: list = field(default_factory=list)  # [(provider, model, error), ...]
    total_retries: int = 0
    total_elapsed_s: float = 0.0


# ─────────────────────────────────────────────────────────────
# Error classification
# ─────────────────────────────────────────────────────────────
# Per TRD: "Upon receiving any 4xx/5xx error, rate-limit timeout (429),
# or network exception, the execution pipeline must instantly switch to
# the Gemini API."

# Errors that are TRANSIENT (warrant retry with backoff):
#   • 429 Too Many Requests (rate limit)
#   • 500/502/503/504 server errors
#   • Network timeouts / connection errors
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}

# Errors that are PERMANENT (skip retries, jump straight to fallback):
#   • 400 Bad Request (malformed request)
#   • 401 Unauthorized (bad API key)
#   • 403 Forbidden (key revoked)
#   • 404 Not Found (model decommissioned)
PERMANENT_STATUS_CODES = {400, 401, 403, 404}


def _classify_error(exc: Exception) -> str:
    """Classify an exception as 'transient', 'permanent', or 'unknown'.

    We inspect common attributes across the Groq and Gemini SDKs:
      • exc.status_code  (httpx.HTTPStatusError, groq.APIStatusError)
      • exc.response.status_code  (httpx-style)
    """
    # Direct .status_code attribute
    status = getattr(exc, "status_code", None)
    if status is None:
        # httpx.HTTPStatusError stores it on .response
        resp = getattr(exc, "response", None)
        if resp is not None:
            status = getattr(resp, "status_code", None)
    if status is not None:
        if status in TRANSIENT_STATUS_CODES:
            return "transient"
        if status in PERMANENT_STATUS_CODES:
            return "permanent"
    # Network / timeout errors → transient
    name = type(exc).__name__.lower()
    if any(k in name for k in ("timeout", "connect", "network", "readerror")):
        return "transient"
    return "unknown"


# ─────────────────────────────────────────────────────────────
# Provider call implementations (lazy SDK imports)
# ─────────────────────────────────────────────────────────────
def _call_groq(
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.2,
    timeout: float = 90.0,
) -> str:
    """Call a single Groq model. Raises on failure (caller handles)."""
    from groq import Groq
    client = Groq(api_key=api_key, timeout=timeout)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=0.85,
    )
    return (resp.choices[0].message.content or "").strip()


# ─────────────────────────────────────────────────────────────
# Concurrency safety for Gemini SDK's global configure() (Bug #16 fix)
# ─────────────────────────────────────────────────────────────
# The legacy google-generativeai SDK uses a MODULE-LEVEL global config:
#   genai.configure(api_key=...)
# If two concurrent calls (e.g. V30 engine + V26 legacy trends, or two
# regions running in parallel) call this with different API keys, they
# will overwrite each other's config — last writer wins. This causes
# intermittent 401/403 errors that are nearly impossible to reproduce.
#
# Fix: serialize ALL Gemini calls behind a process-wide lock. This adds
# a tiny bit of latency but guarantees correctness. Since Gemini is only
# used as a fallback (Groq is primary), the throughput impact is minimal.
import threading
_GEMINI_LOCK = threading.Lock()


def _call_gemini(
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.2,
    timeout: float = 90.0,
) -> str:
    """Call a single Gemini model. Raises on failure (caller handles).

    Bug #16 FIX: Serialized via _GEMINI_LOCK to prevent race condition
    on genai.configure() global state.
    Bug #16b FIX: Pass request_options with timeout so the SDK actually
    respects it (previously the timeout parameter was declared but never
    used → silent deadlocks on slow Gemini responses).
    """
    with _GEMINI_LOCK:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        gen_model = genai.GenerativeModel(
            model,
            system_instruction=system_prompt,
            generation_config={
                "max_output_tokens": max_tokens,
                "temperature": temperature,
                "top_p": 0.85,
            },
        )
        # Bug #16b FIX: actually pass the timeout via request_options
        # (the SDK's request_timeout parameter is the supported way).
        # V32.1 BUGFIX: The TypeError fallback used `gen_model.generate_content(user_prompt)`
        # with NO timeout — if the older SDK path was hit, a slow Gemini response
        # would block _GEMINI_LOCK indefinitely and freeze ALL Gemini calls
        # process-wide. Now we wrap the fallback in a threading timeout so
        # even the legacy path can't deadlock the lock.
        try:
            resp = gen_model.generate_content(
                user_prompt,
                request_options={"timeout": timeout},
            )
        except TypeError:
            # Older SDK versions don't support request_options — fall back
            # to a thread-pool call with an explicit timeout.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(gen_model.generate_content, user_prompt)
                try:
                    resp = future.result(timeout=timeout + 5)
                except concurrent.futures.TimeoutError:
                    raise TimeoutError(
                        f"Gemini (legacy SDK) timed out after {timeout + 5}s"
                    )
        if hasattr(resp, "text") and resp.text:
            return resp.text.strip()
        if hasattr(resp, "candidates") and resp.candidates:
            parts = resp.candidates[0].content.parts
            return "".join(p.text for p in parts if hasattr(p, "text")).strip()
        return ""


# ─────────────────────────────────────────────────────────────
# Retry-with-backoff wrapper (single provider)
# ─────────────────────────────────────────────────────────────
def _try_provider_with_backoff(
    *,
    provider: str,           # "groq" | "gemini"
    api_key: str,
    models: list[str],
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    attempts_log: list,      # mutated — appends (provider, model, error)
    backoff_seconds: list[float],
    max_attempts_per_model: int = 1,
    temperature: float = 0.2,
) -> tuple[bool, str, str]:
    """Try a provider with all its models, applying exponential backoff
    on transient errors.

    Returns (success, text, model_used).
    """
    call_fn: Callable = _call_groq if provider == "groq" else _call_gemini

    for model in models:
        for attempt_idx in range(max_attempts_per_model):
            try:
                text = call_fn(
                    api_key, model, system_prompt, user_prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                if text:
                    return True, text, model
                # Empty response — treat as soft failure, try next model
                attempts_log.append((provider, model, "empty response"))
                logger.warning(
                    f"[ResilientLLM] {provider}/{model} returned empty response"
                )
            except Exception as e:
                err_class = _classify_error(e)
                err_msg = f"{type(e).__name__}: {e}"
                attempts_log.append((provider, model, err_msg))
                logger.warning(
                    f"[ResilientLLM] {provider}/{model} attempt {attempt_idx + 1} "
                    f"failed ({err_class}): {err_msg[:200]}"
                )

                # PERMANENT errors → skip to next model immediately
                if err_class == "permanent":
                    break

                # TRANSIENT errors → back off and retry (if attempts remain)
                if err_class == "transient" and attempt_idx < len(backoff_seconds):
                    sleep_s = backoff_seconds[min(attempt_idx, len(backoff_seconds) - 1)]
                    logger.info(
                        f"[ResilientLLM] {provider}/{model} transient error — "
                        f"backing off {sleep_s}s (attempt {attempt_idx + 1}/"
                        f"{max_attempts_per_model})"
                    )
                    time.sleep(sleep_s)
                    continue

                # Unknown errors → try next model (don't waste time retrying)
                break

    return False, "", ""


# ─────────────────────────────────────────────────────────────
# Public API — the single entry point the rest of the engine uses
# ─────────────────────────────────────────────────────────────
def call_llm_with_fallback(
    *,
    region_key: str,
    system_prompt: str,
    user_prompt: str,
    groq_key: str = "",
    gemini_key: str = "",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.2,
    backoff_seconds: Optional[list[float]] = None,
    prefer_provider: str = "auto",   # "auto" | "groq" | "gemini"
) -> LLMCallResult:
    """Call an LLM with full TRD-compliant resilience.

    Per TRD Section 2:
        Primary Engine: Groq
        Secondary/Fallback Engine: Gemini (instant switch on any failure)

    Per TRD Section 6:
        Exponential backoff (2s → 4s → 8s) on transient network errors
        before marking the queue item as failed.

    Args:
        region_key:    Used only for logging (which region triggered this call).
        system_prompt: System prompt.
        user_prompt:   User prompt (already includes fact context).
        groq_key:      Groq API key for THIS region (caller resolves via region_config).
        gemini_key:    Gemini API key for THIS region.
        max_tokens:    Max output tokens. TRD says 4000; we default to 4000
                       but caller can override (e.g. 8000 for 2500-word articles).
        temperature:   Sampling temperature. Default 0.2 (factual, low creativity).
        backoff_seconds: Override the default [2, 4, 8] backoff schedule.
        prefer_provider: "auto" = Groq first (TRD default); "groq" = Groq only;
                       "gemini" = Gemini only.

    Returns:
        LLMCallResult with full audit metadata.
    """
    start = time.monotonic()
    if backoff_seconds is None:
        backoff_seconds = list(DEFAULT_BACKOFF_SECONDS)

    attempts_log: list[tuple[str, str, str]] = []

    # Determine provider order based on prefer_provider
    providers_to_try: list[tuple[str, str, list[str]]] = []
    if prefer_provider in ("auto", "groq") and groq_key:
        providers_to_try.append(("groq", groq_key, GROQ_MODELS))
    if prefer_provider in ("auto", "gemini") and gemini_key:
        providers_to_try.append(("gemini", gemini_key, GEMINI_MODELS))

    if not providers_to_try:
        logger.error(
            f"[ResilientLLM] region={region_key} — NO provider available "
            f"(prefer_provider={prefer_provider}, groq_key_set={bool(groq_key)}, "
            f"gemini_key_set={bool(gemini_key)})"
        )
        return LLMCallResult(
            success=False,
            text="",
            attempts=[("none", "none", "no API key configured")],
            total_elapsed_s=time.monotonic() - start,
        )

    # Try each provider in order
    for provider, api_key, models in providers_to_try:
        success, text, model_used = _try_provider_with_backoff(
            provider=provider,
            api_key=api_key,
            models=models,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            attempts_log=attempts_log,
            backoff_seconds=backoff_seconds,
            max_attempts_per_model=len(backoff_seconds) + 1,
            temperature=temperature,
        )
        if success:
            elapsed = time.monotonic() - start
            logger.info(
                f"[ResilientLLM] region={region_key} — SUCCESS via "
                f"{provider}/{model_used} ({elapsed:.1f}s, "
                f"{len(attempts_log)} attempts)"
            )
            return LLMCallResult(
                success=True,
                text=text,
                provider=provider,
                model=model_used,
                attempts=attempts_log,
                total_retries=len(attempts_log) - 1,
                total_elapsed_s=elapsed,
            )
        # Provider fully failed → fall through to next provider (TRD: "instantly switch")

    # All providers exhausted
    elapsed = time.monotonic() - start
    logger.error(
        f"[ResilientLLM] region={region_key} — ALL PROVIDERS FAILED "
        f"({len(attempts_log)} attempts, {elapsed:.1f}s)"
    )
    return LLMCallResult(
        success=False,
        text="",
        attempts=attempts_log,
        total_retries=len(attempts_log),
        total_elapsed_s=elapsed,
    )


# ─────────────────────────────────────────────────────────────
# CLI for manual testing
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import os
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    # Pull keys from env (TRD naming or legacy)
    from region_config import get_groq_key, get_gemini_key
    region = sys.argv[1] if len(sys.argv) > 1 else "world"
    groq = get_groq_key(region)
    gemini = get_gemini_key(region)

    if not groq and not gemini:
        print(f"No LLM keys configured for region '{region}'.")
        print("Set GROQ_API_KEY_<REGION> and/or GEMINI_API_KEY_<REGION> env vars.")
        sys.exit(1)

    result = call_llm_with_fallback(
        region_key=region,
        system_prompt="You are a helpful assistant. Reply with one sentence.",
        user_prompt="Say hello in one short sentence.",
        groq_key=groq,
        gemini_key=gemini,
        max_tokens=200,
    )

    print()
    print("=" * 60)
    print(f"Success:     {result.success}")
    print(f"Provider:    {result.provider}")
    print(f"Model:       {result.model}")
    print(f"Retries:     {result.total_retries}")
    print(f"Elapsed:     {result.total_elapsed_s:.2f}s")
    print(f"Attempts:    {len(result.attempts)}")
    for p, m, err in result.attempts:
        print(f"  - {p}/{m}: {err}")
    print()
    print("Response:")
    print(result.text)
    print("=" * 60)
