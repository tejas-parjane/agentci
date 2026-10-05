"""Execution-budget assertions: cost, latency, tokens, retries, steps, loops.

Budget assertions are the reason a probabilistic system can be gated at all
(PRD §9 use cases 3 and 4). Two properties matter more than the thresholds:

* **Determinism.** Given the same trace, the verdict never varies. These checks
  never consult a model.
* **Honest skipping.** When the adapter reported no token usage, cost cannot be
  evaluated, and the result is ``SKIPPED`` with a hint. Reporting ``PASSED``
  would create a gate that can never fail — the exact false confidence the PRD
  warns against (§49).
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Sequence
from typing import Any

from agentci.assertions.base import Kind, make, skip
from agentci.core.result import AssertionResult, RunMetrics
from agentci.core.trace import ToolCall


def max_latency(metrics: RunMetrics, limit_ms: float) -> AssertionResult:
    actual = metrics.latency_ms
    ok = actual <= limit_ms
    return make(
        kind=Kind.EXECUTION.value,
        name="to_have_max_latency",
        description=f"completes within {limit_ms:.0f}ms",
        ok=ok,
        expected=f"<= {limit_ms:.0f}ms",
        actual=round(actual, 1),
        message=(
            f"latency {actual:.0f}ms exceeds the {limit_ms:.0f}ms budget "
            f"({actual / limit_ms:.2f}x over)" if not ok else ""
        ),
        hint="a latency regression is often a model swap or an added tool hop",
    )


def max_cost(metrics: RunMetrics, limit_usd: float, *, reason: str | None = None) -> AssertionResult:
    if metrics.cost_usd is None:
        return skip(
            kind=Kind.EXECUTION.value,
            name="to_have_max_cost",
            description=f"costs at most ${limit_usd:.4f}",
            reason=reason or "the adapter reported no priced token usage",
            hint=(
                "emit ctx.recorder.model_call(...) and set ctx.usage = TokenUsage(...), "
                "or add the model under `pricing:` in agentci.yaml"
            ),
        )
    actual = metrics.cost_usd
    ok = actual <= limit_usd
    return make(
        kind=Kind.EXECUTION.value,
        name="to_have_max_cost",
        description=f"costs at most ${limit_usd:.4f}",
        ok=ok,
        expected=f"<= ${limit_usd:.6f}",
        actual=round(actual, 8),
        message=(
            f"cost ${actual:.6f} exceeds the ${limit_usd:.6f} budget"
            + (f" ({actual / limit_usd:.2f}x over)" if limit_usd > 0 else "")
            + f"\n  tokens: {metrics.total_tokens} across {metrics.tool_calls} tool call(s)"
            if not ok
            else ""
        ),
        hint="compare against `agentci baseline compare` to see whether this is a regression",
    )


def max_tokens(metrics: RunMetrics, limit: int) -> AssertionResult:
    if metrics.total_tokens is None:
        return skip(
            kind=Kind.EXECUTION.value,
            name="to_have_max_tokens",
            description=f"uses at most {limit} tokens",
            reason="the adapter reported no token usage",
            hint="emit TokenUsage from the adapter to enable this gate",
        )
    actual = metrics.total_tokens
    ok = actual <= limit
    return make(
        kind=Kind.EXECUTION.value,
        name="to_have_max_tokens",
        description=f"uses at most {limit} tokens",
        ok=ok,
        expected=f"<= {limit}",
        actual=actual,
        message=(
            f"token usage {actual} exceeds the {limit} budget "
            f"(prompt={metrics.prompt_tokens}, completion={metrics.completion_tokens})"
            if not ok
            else ""
        ),
    )


def max_retries(metrics: RunMetrics, limit: int) -> AssertionResult:
    actual = metrics.retries
    ok = actual <= limit
    return make(
        kind=Kind.EXECUTION.value,
        name="to_have_max_retries",
        description=f"retries at most {limit} time(s)",
        ok=ok,
        expected=f"<= {limit}",
        actual=actual,
        message=(
            f"agent retried {actual} time(s), budget is {limit}; retries usually signal "
            f"flaky tools or a stuck agent" if not ok else ""
        ),
    )


def max_steps(metrics: RunMetrics, limit: int) -> AssertionResult:
    actual = metrics.steps
    ok = actual <= limit
    return make(
        kind=Kind.EXECUTION.value,
        name="to_have_max_steps",
        description=f"takes at most {limit} steps",
        ok=ok,
        expected=f"<= {limit}",
        actual=actual,
        message=(
            f"agent took {actual} steps, budget is {limit}" if not ok else ""
        ),
    )


def no_loop(
    calls: Sequence[ToolCall],
    *,
    threshold: int = 3,
    window: int | None = None,
) -> AssertionResult:
    """Detect a tool loop.

    Two detection strategies, both reported:

    * **Repeated fingerprint.** The same ``tool(arguments)`` called ``threshold``
      or more times anywhere in the run. Catches unbounded retries.
    * **Windowed alternation.** Within any window of ``span`` consecutive calls,
      more than ``threshold`` of them share a fingerprint -- even if no two
      identical calls are adjacent. Catches an agent oscillating
      ``A, B, A, B, ...``, which the adjacency check alone would never flag.

    ``window`` defaults to ``2 * threshold``. It bounds *how far apart* two
    repeats may be and still count as a loop; it does not limit how many calls are
    inspected, because a loop anywhere in the trace is still a loop.
    """
    span = window or threshold * 2
    fingerprints = [_fingerprint(c) for c in calls]

    counts: dict[str, int] = {}
    for fingerprint in fingerprints:
        counts[fingerprint] = counts.get(fingerprint, 0) + 1
    repeats = {fp: n for fp, n in counts.items() if n >= threshold}

    # Longest consecutive run of one fingerprint, over the whole trace.
    max_run = 1
    run = 1
    for previous, current in itertools.pairwise(fingerprints):
        run = run + 1 if current == previous else 1
        max_run = max(max_run, run)

    # Densest fingerprint inside any window of `span` calls. Scanned left to right
    # with a fixed width rather than by slicing, so a 10k-event trace stays linear.
    windowed_peak = 0
    if span > 0 and len(fingerprints) >= 2:
        window_counts: dict[str, int] = {}
        for index, fingerprint in enumerate(fingerprints):
            window_counts[fingerprint] = window_counts.get(fingerprint, 0) + 1
            if index >= span:
                expired = fingerprints[index - span]
                window_counts[expired] -= 1
                if window_counts[expired] == 0:
                    del window_counts[expired]
            # Nothing further to learn once the peak already trips the threshold.
            if windowed_peak < threshold:
                windowed_peak = max(windowed_peak, *window_counts.values())

    consecutive_repeat = max_run - 1
    ok = not repeats and consecutive_repeat < threshold and windowed_peak < threshold

    detail: list[str] = []
    for fingerprint, count in sorted(repeats.items(), key=lambda kv: -kv[1]):
        detail.append(f"{fingerprint} called {count}x")
    if consecutive_repeat >= threshold:
        detail.append(f"{consecutive_repeat} consecutive identical call(s)")
    if windowed_peak >= threshold:
        detail.append(f"{windowed_peak} repeats within {span} calls")

    return make(
        kind=Kind.EXECUTION.value,
        name="to_not_loop",
        description=f"agent does not loop (threshold {threshold})",
        ok=ok,
        expected=f"< {threshold} repeats",
        actual={
            "max_repeats": max(counts.values(), default=0),
            "max_consecutive": max_run,
            "max_in_window": windowed_peak,
        },
        message=(
            "detected tool loop: " + "; ".join(detail[:4]) + f"\n  sequence: {fingerprints[:12]}"
            if detail
            else ""
        ),
        hint=(
            "set budgets.max_steps to cap this in CI; the agent is likely waiting on a "
            "condition its prompt tells it to poll for"
        ),
    )


def _fingerprint(call: ToolCall) -> str:
    try:
        return f"{call.name}({json.dumps(call.arguments, sort_keys=True, default=str)})"
    except (TypeError, ValueError):  # pragma: no cover
        return f"{call.name}({len(call.arguments)} args)"


def summary_line(metrics: RunMetrics) -> str:
    """One-line budget summary used by reports."""
    parts = [f"{metrics.latency_ms:.0f}ms"]
    if metrics.cost_usd is not None:
        parts.append(f"${metrics.cost_usd:.5f}")
    if metrics.total_tokens is not None:
        parts.append(f"{metrics.total_tokens} tokens")
    parts.append(f"{metrics.tool_calls} tools")
    if metrics.retries:
        parts.append(f"{metrics.retries} retries")
    if metrics.errors:
        parts.append(f"{metrics.errors} errors")
    return ", ".join(parts)


def max_tool_call_count(metrics: RunMetrics, limit: int) -> AssertionResult:
    """Budget form of the tool-call cap, evaluated from metrics alone."""
    actual = metrics.tool_calls
    ok = actual <= limit
    return make(
        kind=Kind.EXECUTION.value,
        name="to_have_max_tool_calls",
        description=f"makes at most {limit} tool calls",
        ok=ok,
        expected=f"<= {limit}",
        actual=actual,
        message=f"agent made {actual} tool calls, budget is {limit}" if not ok else "",
    )


#: Unambiguous handles on the check functions above.
#:
#: ``evaluate_budgets`` takes keyword arguments named ``max_tokens``, ``max_steps``
#: and ``max_retries`` -- the same names as these functions. Inside that scope the
#: parameters shadow the globals, so calling the shadowed names would raise
#: ``TypeError: 'int' object is not callable``. Binding the functions here, at
#: module level, keeps both the public API and the parameter names readable.
_TOKEN_CHECK = max_tokens
_RETRY_CHECK = max_retries
_STEP_CHECK = max_steps
_TOOL_COUNT_CHECK = max_tool_call_count


def evaluate_budgets(
    metrics: RunMetrics,
    *,
    max_cost_usd: float | None = None,
    max_latency_ms: float | None = None,
    max_tool_calls: int | None = None,
    max_steps: int | None = None,
    max_retries: int | None = None,
    max_tokens: int | None = None,
) -> list[AssertionResult]:
    """Apply every configured project budget to one invocation.

    Configured budgets always apply, independent of what a test body asserts. A
    test author who forgets ``to_have_max_cost`` must not be able to disable the
    project's cost gate.
    """
    results: list[AssertionResult] = []
    if max_latency_ms is not None:
        results.append(max_latency(metrics, max_latency_ms))
    if max_cost_usd is not None:
        results.append(max_cost(metrics, max_cost_usd))
    if max_tokens is not None:
        results.append(_TOKEN_CHECK(metrics, max_tokens))
    if max_retries is not None:
        results.append(_RETRY_CHECK(metrics, max_retries))
    if max_steps is not None:
        results.append(_STEP_CHECK(metrics, max_steps))
    if max_tool_calls is not None:
        results.append(_TOOL_COUNT_CHECK(metrics, max_tool_calls))
    return results


def describe_metrics(metrics: RunMetrics) -> dict[str, Any]:
    return {
        "latency_ms": round(metrics.latency_ms, 2),
        "cost_usd": metrics.cost_usd,
        "total_tokens": metrics.total_tokens,
        "tool_calls": metrics.tool_calls,
        "steps": metrics.steps,
        "retries": metrics.retries,
        "errors": metrics.errors,
    }


__all__ = [
    "describe_metrics",
    "evaluate_budgets",
    "max_cost",
    "max_latency",
    "max_retries",
    "max_steps",
    "max_tokens",
    "max_tool_call_count",
    "no_loop",
    "summary_line",
]
