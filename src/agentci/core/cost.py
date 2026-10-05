"""Token-to-cost estimation.

Pricing is bundled as a snapshot in :mod:`agentci.core.pricing` and is
**not** fetched at runtime — ADR-006 forbids AgentCI from making network calls
for telemetry or pricing. The snapshot carries an ``as_of`` date so a stale
number is visible rather than silently authoritative, and users can override or
extend it in ``agentci.yaml``::

    pricing:
      gpt-5-mini:
        prompt: 0.00000025      # USD per token
        completion: 0.000002

Lookup order for a model id:

1. exact match
2. longest matching prefix (``gpt-5`` matches ``gpt-5-mini-2026-01-01``)
3. no match -> ``None``

Returning ``None`` matters. An unknown price must make cost assertions report
``SKIPPED`` with a hint, never ``PASS`` on absent data (§32).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agentci.core.pricing import PRICING, PRICING_AS_OF
from agentci.core.trace import TokenUsage


class ModelPrice(BaseModel):
    """Per-token price in USD, with optional cached-input and reasoning rates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: float = Field(ge=0, description="USD per prompt/input token")
    completion: float = Field(ge=0, description="USD per completion/output token")
    cached_prompt: float | None = Field(default=None, ge=0)

    def as_per_million(self) -> ModelPrice:
        return ModelPrice(
            prompt=self.prompt * 1_000_000,
            completion=self.completion * 1_000_000,
            cached_prompt=None if self.cached_prompt is None else self.cached_prompt * 1_000_000,
        )

    def cost(self, usage: TokenUsage) -> float:
        total = self.prompt * usage.prompt_tokens + self.completion * usage.completion_tokens
        return round(total, 10)

    @classmethod
    def from_per_million(cls, data: Mapping[str, Any]) -> ModelPrice:
        """Interpret a config mapping as USD per *million* tokens."""
        return cls(
            prompt=float(data.get("prompt", 0.0)) / 1_000_000,
            completion=float(data.get("completion", 0.0)) / 1_000_000,
            cached_prompt=(
                float(data["cached_prompt"]) / 1_000_000 if data.get("cached_prompt") else None
            ),
        )


class CostModel:
    """Resolves a model id to a price and converts usage into USD."""

    __slots__ = ("_exact", "_prefix_index", "_unknown", "as_of")

    def __init__(self, prices: Mapping[str, ModelPrice] | None = None) -> None:
        merged: dict[str, ModelPrice] = {}
        for name, entry in (PRICING or {}).items():
            merged[name] = ModelPrice.from_per_million(entry)
        for name, price in (prices or {}).items():
            merged[name] = price

        self._exact: dict[str, ModelPrice] = {}
        self._prefix_index: list[tuple[str, ModelPrice]] = []
        for name, price in merged.items():
            self._exact[name] = price
            # Index prefixes so that a dated variant resolves to its family price.
            self._prefix_index.append((name.lower(), price))
        self._prefix_index.sort(key=lambda pair: len(pair[0]), reverse=True)
        self.as_of = PRICING_AS_OF

    def price_for(self, model: str | None) -> ModelPrice | None:
        """Resolve a price for ``model``, or ``None`` if unknown."""
        if not model:
            return None
        key = model.strip()
        if key in self._exact:
            return self._exact[key]
        lowered = key.lower()
        for name, price in self._prefix_index:
            if lowered.startswith(name):
                return price
        return None

    def cost_for(self, model: str | None, usage: TokenUsage | None) -> float | None:
        """Cost in USD, or ``None`` when either the model or the usage is unknown."""
        if usage is None or not usage:
            return None
        price = self.price_for(model)
        if price is None:
            return None
        return price.cost(usage)

    def annotate(self, model: str | None, usage: TokenUsage | None) -> float | None:
        """Alias of :meth:`cost_for` used by the recorder path."""
        return self.cost_for(model, usage)

    def unknown_models(self, models: list[str]) -> list[str]:
        return [m for m in models if m and self.price_for(m) is None]

    def __repr__(self) -> str:
        return f"CostModel(models={len(self._exact)}, as_of={self.as_of})"


@dataclass(frozen=True, slots=True)
class CostUnavailable:
    """Explains why a cost figure is missing, for actionable SKIPPED messages."""

    reason: str
    model: str | None = None
    as_of: str | None = None

    def hint(self) -> str:
        if self.reason == "no_usage":
            return (
                "the adapter reported no token usage. Emit ctx.recorder.model_call(...) "
                "with ctx.usage = TokenUsage(...) so cost gates can be evaluated."
            )
        if self.reason == "unknown_model":
            return (
                f"no bundled price for {self.model!r}. Add one under `pricing:` in "
                "agentci.yaml (USD per token, or per million)."
            )
        return "cost is unavailable"

    def __str__(self) -> str:
        return f"{self.reason}: {self.model or ''}".strip(": ")


class CostEstimator:
    """Convenience wrapper combining a :class:`CostModel` with usage inspection."""

    __slots__ = ("model",)

    def __init__(self, prices: Mapping[str, ModelPrice] | CostModel | None = None) -> None:
        self.model = prices if isinstance(prices, CostModel) else CostModel(prices)

    def for_event(self, model: str | None, usage: TokenUsage | None) -> float | None:
        return self.model.cost_for(model, usage)

    def explain(self, model: str | None, usage: TokenUsage | None) -> CostUnavailable | None:
        """Return why cost is unavailable, or ``None`` if it is available."""
        if usage is None or not usage:
            return CostUnavailable(reason="no_usage", model=model)
        if self.model.price_for(model) is None:
            return CostUnavailable(reason="unknown_model", model=model, as_of=self.model.as_of)
        return None

    def with_overrides(self, prices: Mapping[str, ModelPrice]) -> CostEstimator:
        return CostEstimator(CostModel(prices))


def build_cost_model(config_pricing: Mapping[str, Any] | None = None) -> CostModel:
    """Build a cost model, merging config overrides onto the bundled snapshot.

    Config values are interpreted as **USD per token** if they look like token
    prices, which is the natural unit for a gate. To be explicit, use the
    ``per_million`` form::

        pricing:
          my-model:
            prompt: 0.0000015
            completion: 0.000006
    """
    if not config_pricing:
        return CostModel()

    overrides: dict[str, ModelPrice] = {}
    for name, entry in config_pricing.items():
        if isinstance(entry, ModelPrice):
            overrides[name] = entry
        elif isinstance(entry, int | float):
            overrides[name] = ModelPrice(prompt=float(entry), completion=float(entry))
        elif isinstance(entry, Mapping):
            overrides[name] = ModelPrice.from_per_million(entry)
        else:
            raise ValueError(f"invalid pricing entry for {name!r}: expected mapping or number")
    return CostModel(overrides)


def _validate_positive(value: float) -> float:  # pragma: no cover - retained for docs
    if value < 0:
        raise ValueError("pricing values must be >= 0")
    return value


__all__ = [
    "PRICING_AS_OF",
    "CostEstimator",
    "CostModel",
    "CostUnavailable",
    "ModelPrice",
    "build_cost_model",
]
