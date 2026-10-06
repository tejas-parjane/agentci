"""The report data model — the contract between AgentCI and CI systems.

One :class:`RunReport` is produced per ``agentci test`` invocation and rendered to
JSON, Markdown, HTML, GitHub annotations, and the PR summary. Those renderers all
read *this*, so the formats cannot drift apart.

Design rules:

* **Versioned.** ``schema_version`` is bumped independently of the package version
  whenever the shape changes, so a consumer can pin the shape it parses.
* **Explicit about severity.** ``PASS`` / ``WARN`` / ``FAIL`` / ``ERROR`` / ``SKIP``
  appear everywhere a verdict is possible (§19). An undetermined verdict is never
  rendered as a pass.
* **Dimensions, not a composite.** §18 forbids hiding a policy violation behind an
  average, so :class:`Dimensions` is stored separately and gates reference it.
* **Redaction is applied by the renderer**, not here, so the in-memory report keeps
  full fidelity for gating decisions while files on disk stay clean (ADR-008).
"""

from __future__ import annotations

import enum
import platform
import sys
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from agentci.__about__ import REPORT_SCHEMA_VERSION, __version__
from agentci.core.result import (
    AssertionOutcome,
    AssertionResult,
    PolicyViolation,
    RunMetrics,
    Status,
)
from agentci.core.trace import TraceEvent, new_id, utc_now


class Flakiness(str, enum.Enum):
    """Why a test is not a simple pass or fail (PRD §20)."""

    NONE = "none"
    FLAKY = "flaky"
    CONSISTENTLY_FAILING = "consistently_failing"
    INFRA_ERROR = "infra_error"
    TIMEOUT = "timeout"
    POLICY_VIOLATION = "policy_violation"
    JUDGE_ERROR = "judge_error"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssertionReport(_Model):
    """A serialized :class:`AssertionResult`, flattened for consumers."""

    assertion_id: str
    kind: str
    name: str
    description: str
    status: AssertionOutcome
    expected: Any = None
    actual: Any = None
    message: str = ""

    @classmethod
    def from_result(cls, result: AssertionResult) -> AssertionReport:
        return cls(**result.model_dump())

    @property
    def ok(self) -> bool:
        return self.status is AssertionOutcome.PASSED


class TraceExcerpt(_Model):
    """A trimmed trace, included in reports so failures are inspectable (§7)."""

    run_id: str
    events: list[dict[str, Any]]
    truncated: bool = False

    @classmethod
    def build(cls, events: list[TraceEvent], limit: int) -> Self:
        """Keep the head and tail of a long trace.

        Truncating the middle preserves the run's opening and its final outcome,
        which is where a failure's cause and effect live.
        """
        if limit <= 0:
            return cls(run_id="", events=[], truncated=bool(events))
        if len(events) <= limit:
            return cls(
                run_id=events[0].run_id if events else "",
                events=[e.to_dict() for e in events],
            )
        head = limit * 2 // 3
        tail = limit - head
        kept = [*events[:head], *events[-tail:]]
        return cls(
            run_id=events[0].run_id if events else "",
            events=[e.to_dict() for e in kept],
            truncated=True,
        )


class IterationReport(_Model):
    """One repetition of a test (PRD §20, ``evaluation.repeat``)."""

    run_id: str
    iteration: int
    status: Status
    latency_ms: float = 0.0
    cost_usd: float | None = None
    total_tokens: int | None = None
    tool_calls: int = 0
    steps: int = 0
    retries: int = 0
    error: str | None = None
    error_category: str | None = None
    trace_ref: str | None = Field(default=None, description="storage-relative trace path")

    @classmethod
    def from_metrics(
        cls,
        *,
        run_id: str,
        iteration: int,
        status: Status,
        metrics: RunMetrics,
        error: str | None = None,
        error_category: str | None = None,
        trace_ref: str | None = None,
    ) -> IterationReport:
        return cls(
            run_id=run_id,
            iteration=iteration,
            status=status,
            latency_ms=round(metrics.latency_ms, 2),
            cost_usd=metrics.cost_usd,
            total_tokens=metrics.total_tokens,
            tool_calls=metrics.tool_calls,
            steps=metrics.steps,
            retries=metrics.retries,
            error=error,
            error_category=error_category,
            trace_ref=trace_ref,
        )


class TestReport(_Model):
    """Everything known about one test in this run."""

    test_id: str
    name: str
    file: str
    line: int = 0
    status: Status = Status.SKIP
    tags: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    flakiness: Flakiness = Flakiness.NONE
    iterations: list[IterationReport] = Field(default_factory=list)
    assertions: list[AssertionReport] = Field(default_factory=list)
    policy_violations: list[PolicyViolation] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    trace: TraceExcerpt | None = None
    error: str | None = None
    error_category: str | None = None
    output_excerpt: str = ""

    # -- derived --------------------------------------------------------------

    @property
    def duration_ms(self) -> float:
        return sum(i.latency_ms for i in self.iterations)

    @property
    def cost_usd(self) -> float | None:
        costs = [i.cost_usd for i in self.iterations if i.cost_usd is not None]
        return sum(costs) if costs else None

    @property
    def total_tokens(self) -> int | None:
        counts = [i.total_tokens for i in self.iterations if i.total_tokens is not None]
        return sum(counts) if counts else None

    @property
    def failures(self) -> list[AssertionReport]:
        return [a for a in self.assertions if a.status is AssertionOutcome.FAILED]

    @property
    def skipped_assertions(self) -> list[AssertionReport]:
        return [a for a in self.assertions if a.status is AssertionOutcome.SKIPPED]

    @property
    def passed_iterations(self) -> int:
        return sum(1 for i in self.iterations if i.status is Status.PASS)

    def pass_rate(self) -> float | None:
        if not self.iterations:
            return None
        return self.passed_iterations / len(self.iterations)

    def score(self, kind: str | None = None) -> float | None:
        """Fraction of evaluated assertions passed, optionally for one kind."""
        relevant = [a for a in self.assertions if kind is None or a.kind == kind]
        evaluated = [a for a in relevant if a.status is not AssertionOutcome.SKIPPED]
        if not evaluated:
            return None
        return sum(1 for a in evaluated if a.status is AssertionOutcome.PASSED) / len(evaluated)


class GateResult(_Model):
    """One evaluated release gate (§18)."""

    name: str
    status: Status
    actual: float | str | None = None
    threshold: str | None = None
    message: str = ""
    source: str = "gate"  # gate | policy | regression | budget | absolute


class DimensionsReport(_Model):
    """Dimension scores (§18). Absent values mean "not measured"."""

    task_success: float | None = None
    tool_correctness: float | None = None
    policy_compliance: float | None = None
    reliability: float | None = None
    cost_efficiency: float | None = None
    latency_ms: float | None = None

    def as_dict(self) -> dict[str, float]:
        return {
            name: value
            for name, value in self.model_dump().items()
            if isinstance(value, int | float)
        }


class RunSummary(_Model):
    """Headline numbers, matching the CLI's terminal output."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    warned: int = 0
    errored: int = 0
    skipped: int = 0
    flaky: int = 0
    pass_rate: float = 0.0
    duration_ms: float = 0.0
    cost_usd: float | None = None
    total_tokens: int | None = None
    assertions_passed: int = 0
    assertions_failed: int = 0
    assertions_skipped: int = 0
    policy_violations: int = 0


class RegressionSection(_Model):
    """Comparison against a stored baseline (§FR-5)."""

    compared: bool = False
    baseline_run_id: str | None = None
    baseline_created_at: datetime | None = None
    status: Status = Status.SKIP
    comparisons: list[GateResult] = Field(default_factory=list)
    new_failures: list[str] = Field(default_factory=list)
    fixed: list[str] = Field(default_factory=list)
    missing_from_run: list[str] = Field(default_factory=list)
    message: str = ""

    @classmethod
    def not_compared(cls, reason: str = "no baseline configured") -> RegressionSection:
        """Default state: no comparison was attempted.

        Deliberately ``SKIP`` rather than ``PASS``. An unevaluated regression check
        is an unknown, and §19 forbids rendering an unknown as a pass.
        """
        return cls(compared=False, status=Status.SKIP, message=reason)


class PlatformInfo(_Model):
    """Interpreter and OS details, enough to explain a platform-only failure."""

    system: str = platform.system()
    release: str = platform.release()
    machine: str = platform.machine()
    python_implementation: str = platform.python_implementation()


class EnvironmentReport(_Model):
    """Where the run happened. Useful when a result only reproduces locally."""

    agentci_version: str = __version__
    python_version: str = sys.version.split()[0]
    platform: PlatformInfo = Field(default_factory=PlatformInfo)
    agent: str = ""
    agent_model: str | None = None
    config_version: int = 1
    git_commit: str | None = None
    git_branch: str | None = None
    git_dirty: bool | None = None
    ci: bool = False
    ci_provider: str | None = None
    pricing_as_of: str | None = None


class RunIdentity(_Model):
    """Who ran what, when."""

    id: str = Field(default_factory=lambda: new_id("run"))
    project: str = "agent"
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    status: Status = Status.PASS
    selected_by: str = "all"
    selection_reason: str = ""
    #: The ref the diff ran against, and the files it reported. Empty when no
    #: change-aware selection was requested. Additive: absent from v0.1.0 reports.
    selection_base: str | None = None
    selection_changed: list[str] = Field(default_factory=list)
    #: Tests the change analysis skipped. Informational, not a warning: the diff
    #: says they are unaffected, which is the point of selection. Each one also
    #: carries its own reason in its report entry. Additive, absent from v0.1.0.
    selection_skipped: int = 0
    repeat: int = 1
    minimum_pass_rate: float = 1.0
    run_ids: list[str] = Field(default_factory=list)


class RunReport(_Model):
    """The complete machine-readable result of a run."""

    schema_version: int = REPORT_SCHEMA_VERSION
    run: RunIdentity = Field(default_factory=RunIdentity)
    summary: RunSummary = Field(default_factory=RunSummary)
    dimensions: DimensionsReport = Field(default_factory=DimensionsReport)
    gates: list[GateResult] = Field(default_factory=list)
    tests: list[TestReport] = Field(default_factory=list)
    policy_violations: list[PolicyViolation] = Field(default_factory=list)
    regression: RegressionSection = Field(default_factory=RegressionSection.not_compared)
    environment: EnvironmentReport = Field(default_factory=EnvironmentReport)
    warnings: list[str] = Field(default_factory=list)

    # -- convenience ----------------------------------------------------------

    @property
    def status(self) -> Status:
        return self.run.status

    @property
    def failures(self) -> list[TestReport]:
        return [t for t in self.tests if t.status.is_failure]

    @property
    def blocking_gates(self) -> list[GateResult]:
        return [g for g in self.gates if g.status is Status.FAIL]

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunReport:
        return cls.model_validate(data)


__all__ = [
    "AssertionReport",
    "DimensionsReport",
    "EnvironmentReport",
    "Flakiness",
    "GateResult",
    "IterationReport",
    "PlatformInfo",
    "RegressionSection",
    "RunIdentity",
    "RunReport",
    "RunSummary",
    "TestReport",
    "TraceExcerpt",
]
