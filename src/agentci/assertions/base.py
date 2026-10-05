"""Shared assertion plumbing.

Assertions are implemented as **pure functions** returning an
:class:`~agentci.core.result.AssertionResult`, with no side effects. That keeps
them unit-testable without a runner and makes them reusable from a future pytest
plugin. The fluent :class:`~agentci.assertions.fluent.Expectation` wrapper only
decides *where* results go.

Two failure policies, chosen once and applied everywhere:

* **Inside a run (accumulate).** Every expectation is recorded and the test body
  runs to completion, so one `agentci test` shows every violation instead of
  stopping at the first. For agent debugging, seeing the whole trajectory of
  problems in one pass is worth far more than fail-fast.
* **Standalone (fail fast).** Calling ``expect()`` outside a run raises on the
  first failure, which is what a user writing a plain script expects.

Skipping is a first-class outcome. When a precondition is absent — no token
usage means no cost gate can be evaluated — the result is ``SKIPPED`` with an
actionable hint, never ``PASSED``. A gate that cannot see its input must not
claim success (PRD §32).
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from agentci.core.result import AssertionOutcome, AssertionResult

MAX_EXCERPT = 400


class AssertionFailed(Exception):
    """Raised by ``expect()`` when used outside a run and an expectation fails."""

    def __init__(self, result: AssertionResult) -> None:
        super().__init__(result.message)
        self.result = result


class ExpectationError(Exception):
    """Raised when an expectation is called with nonsensical arguments."""


def excerpt(value: Any, limit: int = MAX_EXCERPT) -> str:
    """Render ``value`` as a short, single-line, safe-to-print string."""
    if value is None:
        return "None"
    text = value if isinstance(value, str) else repr(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def line(text: str, limit: int = MAX_EXCERPT) -> str:
    """Render a multiline string compactly for a one-line message."""
    return excerpt(text, limit)


@dataclass
class AssertionCollector:
    """Accumulates results for the duration of one test invocation."""

    results: list[AssertionResult] = field(default_factory=list)
    standalone: bool = False

    def add(self, result: AssertionResult) -> AssertionResult:
        self.results.append(result)
        return result

    def extend(self, other: Iterable[AssertionResult]) -> None:
        self.results.extend(other)

    # -- queries --------------------------------------------------------------

    @property
    def failures(self) -> list[AssertionResult]:
        return [r for r in self.results if r.status is AssertionOutcome.FAILED]

    @property
    def errors(self) -> list[AssertionResult]:
        return [r for r in self.results if r.status is AssertionOutcome.ERRORED]

    @property
    def skips(self) -> list[AssertionResult]:
        return [r for r in self.results if r.status is AssertionOutcome.SKIPPED]

    @property
    def by_kind(self) -> dict[str, list[AssertionResult]]:
        out: dict[str, list[AssertionResult]] = {}
        for result in self.results:
            out.setdefault(result.kind, []).append(result)
        return out

    def rate(self, kind: str | None = None) -> tuple[int, int]:
        """Return ``(passed, total)`` for a kind, excluding skips.

        Excluding skips matters: an unevaluable cost assertion must not dilute
        the tool-correctness score.
        """
        relevant = [r for r in self.results if kind is None or r.kind == kind]
        total = [r for r in relevant if r.status is not AssertionOutcome.SKIPPED]
        passed = [r for r in total if r.status is AssertionOutcome.PASSED]
        return len(passed), len(total)

    def score(self, kind: str | None = None) -> float | None:
        passed, total = self.rate(kind)
        if total == 0:
            return None
        return passed / total

    def clear(self) -> None:
        self.results.clear()

    def summary(self) -> str:
        passed, total = self.rate()
        skipped = len(self.skips)
        base = f"{passed}/{total} assertions passed"
        return f"{base} ({skipped} skipped)" if skipped else base


def make(
    *,
    kind: str,
    name: str,
    description: str,
    ok: bool,
    expected: Any = None,
    actual: Any = None,
    message: str = "",
    hint: str | None = None,
) -> AssertionResult:
    """Build a PASSED or FAILED result."""
    status = AssertionOutcome.PASSED if ok else AssertionOutcome.FAILED
    if not message:
        message = f"{name}: {'ok' if ok else 'failed'}"
    if not ok and hint:
        message = f"{message}\n  hint: {hint}"
    return AssertionResult(
        assertion_id=_next_id(kind, name),
        kind=kind,
        name=name,
        description=description,
        status=status,
        expected=expected,
        actual=actual,
        message=message,
    )


def skip(
    *,
    kind: str,
    name: str,
    description: str,
    reason: str,
    hint: str | None = None,
) -> AssertionResult:
    """Build a SKIPPED result carrying an actionable reason."""
    message = f"{name}: skipped ({reason})"
    if hint:
        message = f"{message}\n  hint: {hint}"
    return AssertionResult(
        assertion_id=_next_id(kind, name),
        kind=kind,
        name=name,
        description=description,
        status=AssertionOutcome.SKIPPED,
        expected=None,
        actual=None,
        message=message,
    )


def error(
    *,
    kind: str,
    name: str,
    description: str,
    reason: str,
) -> AssertionResult:
    """Build an ERRORED result for an assertion that could not be evaluated."""
    return AssertionResult(
        assertion_id=_next_id(kind, name),
        kind=kind,
        name=name,
        description=description,
        status=AssertionOutcome.ERRORED,
        message=f"{name}: could not be evaluated ({reason})",
    )


_counters: dict[str, int] = {}


def _next_id(kind: str, name: str) -> str:
    _counters[kind] = _counters.get(kind, 0) + 1
    return f"{kind}.{name}#{_counters[kind]}"


def reset_counter() -> None:
    """Reset assertion id counters. Used by tests for deterministic output."""
    _counters.clear()


class Kind(str, enum.Enum):
    """Assertion categories, aligned with the dimension scores in §18."""

    OUTPUT = "output"
    TOOL = "tool"
    EXECUTION = "execution"
    POLICY = "policy"
    LEAK = "leak"
    REGRESSION = "regression"


def format_order_diff(expected: Sequence[str], actual: Sequence[str]) -> str:
    """Human-readable diff for tool-order mismatches (PRD §42)."""
    return f"Expected: {' -> '.join(expected)}\n  Actual:   {' -> '.join(actual)}"


def coalesce(values: Sequence[Any]) -> list[Any]:
    """Drop consecutive duplicates, preserving order."""
    out: list[Any] = []
    for value in values:
        if not out or out[-1] != value:
            out.append(value)
    return out


__all__ = [
    "MAX_EXCERPT",
    "AssertionCollector",
    "AssertionFailed",
    "ExpectationError",
    "Kind",
    "coalesce",
    "error",
    "excerpt",
    "format_order_diff",
    "line",
    "make",
    "reset_counter",
    "skip",
]
