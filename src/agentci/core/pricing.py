"""Bundled model price snapshot.

**This is a snapshot, not an authoritative price list.** Values are USD per
million tokens as of :data:`PRICING_AS_OF` and are provided so that cost gates
work out of the box on a fresh install. Providers change prices; verify before
relying on a budget, and override any entry in ``agentci.yaml``::

    pricing:
      gpt-5-mini:
        prompt: 0.25          # USD per million tokens
        completion: 2.00

An unknown model resolves to ``None``, which makes cost assertions ``SKIPPED``
with an actionable hint rather than silently passing. That is deliberate: a cost
gate that cannot see the price must not report success (PRD §32).

AgentCI never fetches pricing at runtime (ADR-006). No network call is made, no
usage data leaves the machine.
"""

from __future__ import annotations

from typing import Final

#: Date this price table was last reviewed. Surfaced in reports so a reader can
#: judge whether the numbers are stale.
PRICING_AS_OF: Final = "2026-01-15"

#: USD per million tokens: {"prompt": <float>, "completion": <float>}
#:
#: Keys act as prefixes, so a dated variant such as ``gpt-5-mini-2026-01-01``
#: resolves to its family entry. Add ``cached_prompt`` where applicable.
PRICING: Final[dict[str, dict[str, float]]] = {
    # -- OpenAI ------------------------------------------------------------
    "gpt-5": {"prompt": 1.25, "completion": 10.00, "cached_prompt": 0.125},
    "gpt-5-mini": {"prompt": 0.25, "completion": 2.00, "cached_prompt": 0.025},
    "gpt-5-nano": {"prompt": 0.05, "completion": 0.40, "cached_prompt": 0.005},
    "gpt-4.1": {"prompt": 2.00, "completion": 8.00, "cached_prompt": 0.50},
    "gpt-4.1-mini": {"prompt": 0.40, "completion": 1.60, "cached_prompt": 0.10},
    "gpt-4.1-nano": {"prompt": 0.10, "completion": 0.40, "cached_prompt": 0.025},
    "gpt-4o": {"prompt": 2.50, "completion": 10.00, "cached_prompt": 1.25},
    "gpt-4o-mini": {"prompt": 0.15, "completion": 0.60, "cached_prompt": 0.075},
    "o3": {"prompt": 2.00, "completion": 8.00, "cached_prompt": 0.50},
    "o3-mini": {"prompt": 1.10, "completion": 4.40, "cached_prompt": 0.55},
    "o4-mini": {"prompt": 1.10, "completion": 4.40, "cached_prompt": 0.275},
    # -- Anthropic ---------------------------------------------------------
    "claude-opus-4": {"prompt": 15.00, "completion": 75.00, "cached_prompt": 1.50},
    "claude-sonnet-4": {"prompt": 3.00, "completion": 15.00, "cached_prompt": 0.30},
    "claude-haiku-4": {"prompt": 1.00, "completion": 5.00, "cached_prompt": 0.10},
    "claude-3-7-sonnet": {"prompt": 3.00, "completion": 15.00, "cached_prompt": 0.30},
    "claude-3-5-sonnet": {"prompt": 3.00, "completion": 15.00, "cached_prompt": 0.30},
    "claude-3-5-haiku": {"prompt": 0.80, "completion": 4.00, "cached_prompt": 0.08},
    "claude-3-opus": {"prompt": 15.00, "completion": 75.00, "cached_prompt": 1.50},
    "claude-3-haiku": {"prompt": 0.25, "completion": 1.25, "cached_prompt": 0.03},
    # -- Google ------------------------------------------------------------
    "gemini-2.5-pro": {"prompt": 1.25, "completion": 10.00, "cached_prompt": 0.31},
    "gemini-2.5-flash": {"prompt": 0.30, "completion": 2.50, "cached_prompt": 0.075},
    "gemini-2.0-flash": {"prompt": 0.10, "completion": 0.40, "cached_prompt": 0.025},
    "gemini-1.5-pro": {"prompt": 1.25, "completion": 5.00},
    "gemini-1.5-flash": {"prompt": 0.075, "completion": 0.30},
    # -- Meta (open weights, commonly self-hosted) -----------------------
    "llama-3.3-70b": {"prompt": 0.00, "completion": 0.00},
    "llama-3.1-70b": {"prompt": 0.00, "completion": 0.00},
    "llama-3.1-8b": {"prompt": 0.00, "completion": 0.00},
    # -- Mistral ------------------------------------------------------------
    "mistral-large": {"prompt": 2.00, "completion": 6.00},
    "mistral-small": {"prompt": 0.20, "completion": 0.60},
    # -- DeepSeek -----------------------------------------------------------
    "deepseek-chat": {"prompt": 0.27, "completion": 1.10, "cached_prompt": 0.07},
    "deepseek-reasoner": {"prompt": 0.55, "completion": 2.19, "cached_prompt": 0.14},
    # -- Qwen ---------------------------------------------------------------
    "qwen-max": {"prompt": 1.60, "completion": 6.40},
    "qwen-plus": {"prompt": 0.40, "completion": 1.20},
}
