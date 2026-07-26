"""
region_config.py - SFAAM Automated News Engine (V30 / TRD v1.0)
=================================================================
Multi-Region Architecture & Isolated APIs
------------------------------------------
Per the TRD (Section 2 — "MULTI-REGION ARCHITECTURE & ISOLATED APIs"):

    "Each region must operate with isolated API keys to ensure high
     availability and prevent cross-region rate-limiting or token
     exhaustion."

This module is the SINGLE SOURCE OF TRUTH for:
  • Region registry (6 regions: World, USA, UK, Pakistan, India, Germany)
  • Google Trends geo codes per region
  • Isolated per-region API key resolution for Groq + Gemini
  • Tavily / NewsAPI / GNews key resolution (used by trend_detector)

ENVIRONMENT VARIABLE NAMING (TRD-compliant)
-------------------------------------------
The TRD specifies the naming convention:
    GROQ_API_KEY_PAKISTAN, GEMINI_API_KEY_PAKISTAN, ...

For backwards compatibility with V26-V29 deployments that used
GROQ_KEY_<REGION>, we resolve BOTH names (TRD name takes priority):

    Priority 1: GROQ_API_KEY_<REGION>   (TRD name)
    Priority 2: GROQ_KEY_<REGION>       (legacy V26 name)

Same for Gemini. This means existing deployments continue to work
unchanged, and new deployments can adopt the TRD naming.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Region registry — order matters (drives the multi-region loop)
# ─────────────────────────────────────────────────────────────
# Each Region is defined by:
#   • key      : canonical short name used in DB, URLs, env var suffix
#   • display  : human-readable label for admin UI
#   • trends_geo : Google Trends RSS geo code (2-letter ISO, "" = worldwide)
#   • newsapi_country : NewsAPI.org country code (lowercase 2-letter)
#   • gnews_country  : GNews.io country code (lowercase 2-letter)
#   • search_locale  : BCP-47 locale hint for trend search queries


@dataclass(frozen=True)
class Region:
    key: str
    display: str
    trends_geo: str            # "" = worldwide
    newsapi_country: str       # 2-letter lowercase (e.g. "us", "gb", "pk")
    gnews_country: str         # 2-letter lowercase
    search_locale: str         # e.g. "en-US", "en-GB", "ur-PK"


# The order of this list drives the per-region loop in the 3-hour cycle.
# World is first because it has the broadest signal pool.
REGIONS: list[Region] = [
    Region("world",    "World",    "",     "",  "",  "en-US"),
    Region("usa",      "USA",      "US",   "us", "us", "en-US"),
    Region("uk",       "UK",       "GB",   "gb", "gb", "en-GB"),
    Region("pakistan", "Pakistan", "PK",   "pk", "pk", "en-PK"),
    Region("india",    "India",    "IN",   "in", "in", "en-IN"),
    Region("germany",  "Germany",  "DE",   "de", "de", "de-DE"),
]

# Fast lookup by key
REGION_BY_KEY: dict[str, Region] = {r.key: r for r in REGIONS}

# Set of all valid region keys (used for validation in API endpoints)
VALID_REGIONS: set[str] = {r.key for r in REGIONS}


def get_region(key: str) -> Optional[Region]:
    """Return Region for a key, or None if invalid."""
    return REGION_BY_KEY.get(key.lower())


# ─────────────────────────────────────────────────────────────
# Per-region API key resolution (TRD naming + legacy fallback)
# ─────────────────────────────────────────────────────────────
def _resolve_region_key(provider: str, region_key: str) -> str:
    """Resolve an API key for (provider, region).

    Tries the TRD-compliant name first, then falls back to the legacy
    V26 name. Returns "" if neither is set.

    Args:
        provider:    "GROQ" | "GEMINI"
        region_key:  lowercase region key (e.g. "pakistan")

    Returns:
        API key string, or "" if not configured.
    """
    region_upper = region_key.upper()

    # TRD naming: GROQ_API_KEY_PAKISTAN, GEMINI_API_KEY_PAKISTAN
    trd_name = f"{provider}_API_KEY_{region_upper}"
    val = os.getenv(trd_name, "").strip()
    if val:
        return val

    # Legacy V26 naming: GROQ_KEY_PAKISTAN, GEMINI_KEY_PAKISTAN
    legacy_name = f"{provider}_KEY_{region_upper}"
    val = os.getenv(legacy_name, "").strip()
    if val:
        logger.debug(
            f"[RegionConfig] Using legacy env var {legacy_name} for "
            f"{provider} ({region_key}) — consider migrating to {trd_name}"
        )
        return val

    return ""


def get_groq_key(region_key: str) -> str:
    """Return the Groq API key for a region, or "" if not set."""
    return _resolve_region_key("GROQ", region_key)


def get_gemini_key(region_key: str) -> str:
    """Return the Gemini API key for a region, or "" if not set."""
    return _resolve_region_key("GEMINI", region_key)


def has_any_llm_key(region_key: str) -> bool:
    """Return True if at least one LLM provider is configured for the region."""
    return bool(get_groq_key(region_key) or get_gemini_key(region_key))


# ─────────────────────────────────────────────────────────────
# Trend detector API keys (Tavily, NewsAPI, GNews)
# ─────────────────────────────────────────────────────────────
# These are GLOBAL keys (not per-region) — Tavily/NewsAPI/GNews typically
# don't differentiate by region in their key. Per-region filtering is done
# at the query level using Region.newsapi_country / Region.gnews_country.

def get_tavily_key() -> str:
    """Return the Tavily API key (used for fact extraction in Step B)."""
    return os.getenv("TAVILY_API_KEY", "").strip()


def get_newsapi_key() -> str:
    """Return the NewsAPI.org key (used for trend detection in Step A)."""
    return os.getenv("NEWSAPI_KEY", "").strip()


def get_gnews_key() -> str:
    """Return the GNews.io key (used for trend detection in Step A)."""
    return os.getenv("GNEWS_KEY", "").strip()


def get_perplexity_key() -> str:
    """Return the Perplexity API key (alternative to Tavily for Step B)."""
    return os.getenv("PERPLEXITY_API_KEY", "").strip()


# ─────────────────────────────────────────────────────────────
# Health check / diagnostics
# ─────────────────────────────────────────────────────────────
def get_region_key_status() -> dict:
    """Return a per-region status map showing which keys are configured.

    Used by the admin dashboard to show "Region Health" — admins can
    see at a glance which regions have AI providers configured.

    NOTE: This does NOT leak key values — only booleans.
    """
    status: dict[str, dict] = {}
    for region in REGIONS:
        status[region.key] = {
            "display": region.display,
            "trends_geo": region.trends_geo or "worldwide",
            "groq": bool(get_groq_key(region.key)),
            "gemini": bool(get_gemini_key(region.key)),
            "any_llm": has_any_llm_key(region.key),
        }
    return status


def get_aggregator_key_status() -> dict:
    """Return which trend-aggregator keys are configured."""
    return {
        "tavily":     bool(get_tavily_key()),
        "newsapi":    bool(get_newsapi_key()),
        "gnews":      bool(get_gnews_key()),
        "perplexity": bool(get_perplexity_key()),
    }


# ─────────────────────────────────────────────────────────────
# CLI for diagnostics
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    print("=" * 60)
    print("SFAAM Automated News Engine — Region Configuration")
    print("=" * 60)
    print()
    print("Regions (in pipeline order):")
    for r in REGIONS:
        print(f"  • {r.display:10s} (key={r.key:10s}, geo={r.trends_geo or 'WW':3s})")
    print()
    print("Per-region LLM key status:")
    print(json.dumps(get_region_key_status(), indent=2))
    print()
    print("Trend-aggregator key status:")
    print(json.dumps(get_aggregator_key_status(), indent=2))
    print()
    print("NOTE: Values are NOT printed — only presence/absence.")
    sys.exit(0)
