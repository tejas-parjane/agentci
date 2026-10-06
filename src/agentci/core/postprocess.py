"""Post-processing: fold a completed run into its verdict.

These are the pure steps between "the tests executed" and "the report is
written" — summarizing, scoring dimensions, evaluating gates, collecting
violations, and finally reducing everything to one :class:`Status`. They live
here rather than on :class:`~agentci.core.runner.TestRunner` because they take a
finished :class:`RunReport` as their input: none of them executes anything, and
each is independently testable without an adapter.

The ordering between them is load-bearing and mirrored by
:meth:`TestRunner.run`::

    collect_violations -> summarize -> dimensions -> absolute_gates
        -> regression -> overall_status

Each stage reads fields the previous stage wrote, so reordering changes results.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agentci.assertions.base import Kind
from agentci.core.config import Config, RegressionConfig
from agentci.core.result import PolicyViolation, Status
from agentci.policy.engine import compliance
from agentci.reporting.models import (
    AssertionReport,
    DimensionsReport,
    Flakiness,
    GateResult,
    RunReport,
    RunSummary,
)


def blocking(violations: Sequence[PolicyViolation]) -> list[PolicyViolation]:
    """Only agent-attributable violations decide a verdict."""
    return [v for v in violations if v.severity is Status.FAIL]


def dedupe_violations(violations: Sequence[PolicyViolation]) -> list[PolicyViolation]:
    """Collapse duplicates, keeping the most severe instance of each.

    Severity matters: a violation seen once in agent scope (FAIL) and once in test
    scope (WARN) must not be collapsed down to the WARN, which would silently stop
    it blocking.
    """
    seen: dict[tuple[str, str, str], PolicyViolation] = {}
    order: list[tuple[str, str, str]] = []
    for violation in violations:
        key = (violation.kind, violation.tool or "", violation.message)
        existing = seen.get(key)
        if existing is None:
            seen[key] = violation
            order.append(key)
        elif violation.severity is Status.FAIL and existing.severity is not Status.FAIL:
            seen[key] = violation
    return [seen[key] for key in order]


def collect_violations(report: RunReport) -> list[PolicyViolation]:
    """Every violation across every test, de-duplicated."""
    return dedupe_violations([v for test in report.tests for v in test.policy_violations])


def summarize(report: RunReport) -> RunSummary:
    """Headline counts over the report's tests."""
    tests = report.tests
    executed = [t for t in tests if t.status is not Status.SKIP]
    costs = [t.cost_usd for t in tests if t.cost_usd is not None]
    tokens = [t.total_tokens for t in tests if t.total_tokens is not None]

    return RunSummary(
        total=len(tests),
        passed=sum(1 for t in tests if t.status is Status.PASS),
        failed=sum(1 for t in tests if t.status is Status.FAIL),
        warned=sum(1 for t in tests if t.status is Status.WARN),
        errored=sum(1 for t in tests if t.status is Status.ERROR),
        skipped=sum(1 for t in tests if t.status is Status.SKIP),
        flaky=sum(1 for t in tests if t.flakiness is Flakiness.FLAKY),
        pass_rate=(
            sum(1 for t in executed if t.status is Status.PASS) / len(executed)
            if executed
            else 0.0
        ),
        duration_ms=sum(t.duration_ms for t in tests),
        cost_usd=sum(costs) if costs else None,
        total_tokens=sum(tokens) if tokens else None,
        assertions_passed=sum(1 for t in tests for a in t.assertions if a.ok),
        assertions_failed=sum(len(t.failures) for t in tests),
        assertions_skipped=sum(len(t.skipped_assertions) for t in tests),
        policy_violations=len(blocking(report.policy_violations)),
    )


def dimensions(report: RunReport, config: Config) -> DimensionsReport:
    """Dimension scores over every evaluated assertion (PRD §18).

    Deliberately not collapsed into a composite: each dimension is
    independently gateable, so a policy violation cannot be averaged away by a
    good task score.
    """
    executed = [t for t in report.tests if t.status is not Status.SKIP]
    blocking_violations = blocking(report.policy_violations)
    if not executed:
        return DimensionsReport(policy_compliance=compliance(blocking_violations))

    behavioural = [a for t in executed for a in t.assertions]
    tool_assertions = [a for a in behavioural if a.kind == Kind.TOOL.value]

    def rate(assertions: list[AssertionReport]) -> float | None:
        evaluated = [a for a in assertions if a.status.value != "skipped"]
        if not evaluated:
            return None
        return sum(1 for a in evaluated if a.ok) / len(evaluated)

    latencies = [t.duration_ms for t in executed if t.duration_ms > 0]
    costs = [t.cost_usd for t in executed if t.cost_usd is not None]
    budget = config.budgets.max_cost_usd

    cost_efficiency: float | None = None
    if costs and budget:
        cost_efficiency = sum(min(1.0, budget / c) if c > 0 else 1.0 for c in costs) / len(
            costs
        )

    return DimensionsReport(
        task_success=rate(behavioural),
        tool_correctness=rate(tool_assertions),
        policy_compliance=compliance(blocking_violations),
        reliability=sum(1 for t in executed if t.status is Status.PASS) / len(executed),
        cost_efficiency=cost_efficiency,
        latency_ms=(sum(latencies) / len(latencies)) if latencies else None,
    )


def absolute_gates(report: RunReport, config: Config) -> list[GateResult]:
    """Evaluate configured dimension gates (§18) plus absolute blocks.

    Absolute blocks come second but matter equally: a failing test or an
    infrastructure error always blocks, whether or not a dimension threshold
    happened to notice it.
    """
    gates: list[GateResult] = []
    summary = report.summary
    dimensions_report = report.dimensions

    values: dict[str, float | None] = {
        "task_success": dimensions_report.task_success,
        "tool_correctness": dimensions_report.tool_correctness,
        "policy_compliance": dimensions_report.policy_compliance,
        "reliability": dimensions_report.reliability,
        "max_cost_usd": summary.cost_usd,
        "max_latency_ms": (
            max((t.duration_ms for t in report.tests), default=0.0) or None
        ),
        "max_tokens": float(summary.total_tokens) if summary.total_tokens else None,
    }

    for name, threshold in config.gates.active().items():
        actual = values.get(name)
        if actual is None:
            gates.append(
                GateResult(
                    name=name,
                    status=Status.SKIP,
                    threshold=threshold.target,
                    message="no data to evaluate this gate",
                    source="gate",
                )
            )
            continue
        ok = threshold.evaluate(actual)
        gates.append(
            GateResult(
                name=name,
                status=Status.PASS if ok else Status.FAIL,
                actual=actual,
                threshold=threshold.target,
                message=""
                if ok
                else f"{name} gate failed ({threshold.describe(actual)})",
                source="gate",
            )
        )

    if summary.failed:
        gates.append(
            GateResult(
                name="tests",
                status=Status.FAIL,
                actual=float(summary.failed),
                threshold="0 failing",
                message=f"{summary.failed} test(s) failed",
                source="absolute",
            )
        )
    if summary.errored:
        gates.append(
            GateResult(
                name="errors",
                status=Status.FAIL,
                actual=float(summary.errored),
                threshold="0 errors",
                message=(
                    f"{summary.errored} test(s) ended in ERROR; an infrastructure fault "
                    f"is never reported as a pass"
                ),
                source="absolute",
            )
        )
    if summary.policy_violations:
        gates.append(
            GateResult(
                name="policy",
                status=Status.FAIL,
                actual=float(summary.policy_violations),
                threshold="0 violations",
                message=f"{summary.policy_violations} policy violation(s)",
                source="policy",
            )
        )
    return gates


def relative_gate(
    *,
    name: str,
    label: str,
    limit: float,
    ignore_below: float,
    before: float,
    after: float,
) -> GateResult:
    """Compare a magnitude against its baseline as a percentage change.

    Two guards keep this honest. A zero baseline makes the ratio undefined, which
    is ``SKIP`` rather than a zero-percent pass; and a change within ``ignore_below``
    passes with the reason stated, so noise from a near-zero baseline cannot
    build-block anyone.
    """
    threshold = f"<= {limit}%"
    if before <= 0:
        return GateResult(
            name=name,
            status=Status.SKIP,
            threshold=threshold,
            message=f"baseline {label} is zero, so a relative change is undefined",
            source="regression",
        )
    change = (after - before) / before * 100.0
    if change <= 0:
        return GateResult(
            name=name,
            status=Status.PASS,
            actual=round(change, 2),
            threshold=threshold,
            message=f"{label} improved {change:+.1f}%",
            source="regression",
        )
    if change <= ignore_below:
        return GateResult(
            name=name,
            status=Status.PASS,
            actual=round(change, 2),
            threshold=threshold,
            message=(
                f"{label} rose {change:.1f}%, within the {ignore_below}% noise floor"
            ),
            source="regression",
        )
    return GateResult(
        name=name,
        status=Status.PASS if change <= limit else Status.FAIL,
        actual=round(change, 2),
        threshold=threshold,
        message=(
            f"{label} rose {change:.1f}% (baseline {before:.4f} -> {after:.4f})"
            if change > limit
            else ""
        ),
        source="regression",
    )


def regression_gates(
    cfg: RegressionConfig,
    baseline: dict[str, Any],
    current: dict[str, Any],
    shared: list[str],
) -> list[GateResult]:
    """Evaluate the configured relative gates over tests both runs share."""
    gates: list[GateResult] = []

    if cfg.max_quality_drop is not None and shared:
        before = [float(baseline[tid].get("score") or 0.0) for tid in shared]
        after = [current[tid].score() for tid in shared]
        if all(value is not None for value in after):
            baseline_quality = sum(before) / len(before)
            current_quality = sum(value for value in after if value is not None) / len(after)
            drop = baseline_quality - current_quality
            ok = drop <= cfg.max_quality_drop
            gates.append(
                GateResult(
                    name="regression.quality_drop",
                    status=Status.PASS if ok else Status.FAIL,
                    actual=round(drop, 4),
                    threshold=f"<= {cfg.max_quality_drop}",
                    message=(
                        ""
                        if ok
                        else f"quality dropped {drop:.3f}: "
                        f"{baseline_quality:.3f} -> {current_quality:.3f}"
                    ),
                    source="regression",
                )
            )

    if cfg.max_cost_increase_pct is not None and shared:
        gates.append(
            relative_gate(
                name="regression.cost_increase",
                label="cost",
                limit=cfg.max_cost_increase_pct,
                ignore_below=cfg.ignore_regression_below_pct,
                before=sum(float(baseline[tid].get("cost_usd") or 0.0) for tid in shared),
                after=sum(current[tid].cost_usd or 0.0 for tid in shared),
            )
        )

    if cfg.max_latency_increase_pct is not None and shared:
        gates.append(
            relative_gate(
                name="regression.latency_increase",
                label="latency",
                limit=cfg.max_latency_increase_pct,
                ignore_below=cfg.ignore_regression_below_pct,
                before=sum(float(baseline[tid].get("duration_ms") or 0.0) for tid in shared),
                after=sum(current[tid].duration_ms or 0.0 for tid in shared),
            )
        )

    return gates


def overall_status(report: RunReport, fail_on_warn: bool) -> Status:
    """Fold every dimension into one verdict, worst first.

    ``ERROR`` outranks ``FAIL`` deliberately (§32). When a test's infrastructure
    faulted, that is the thing a maintainer fixes first, and reporting the run
    as a mere quality failure would bury it behind assertion noise.

    ``fail_on_warn`` is a *gate* policy rather than a display preference: it
    changes the verdict rather than only the exit code, otherwise an artifact
    would claim WARN while CI reports the same run as failed.
    """
    if not report.tests:
        return Status.SKIP
    if report.summary.errored:
        return Status.ERROR
    if any(g.status is Status.FAIL for g in report.gates):
        return Status.FAIL
    if report.regression.status is Status.FAIL:
        return Status.FAIL
    if report.regression.status is Status.WARN or report.summary.warned:
        return Status.FAIL if fail_on_warn else Status.WARN
    return Status.PASS


__all__ = [
    "absolute_gates",
    "blocking",
    "collect_violations",
    "dedupe_violations",
    "dimensions",
    "overall_status",
    "regression_gates",
    "relative_gate",
    "summarize",
]
