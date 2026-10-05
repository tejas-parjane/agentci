"""Result objects: what an adapter returns, and what assertions read.

Three layers, deliberately separated:

``AgentResult``
    The minimal adapter contract (PRD §FR-1). Adapters produce it. It carries
    no derived data, so an adapter author never has to think about budgets.

``RunMetrics``
    Derived, read-only measurements extracted from a trace by the runner.
    Adapters cannot set them; the runner cannot invent them.

``ResultView``
    The ergonomic object assertions consume. Exposes ``output_text``,
    ``tool_calls``, ``metrics`` and friends. ``expect()`` also accepts a raw
    ``AgentResult`` and builds a view on the fly.
"""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agentci.core.trace import EventType, ToolCall, Trace


class Status(str, enum.Enum):
    """Terminal status of a test, gate, or dimension."""

    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    ERROR = "error"
    SKIP = "skip"

    @property
    def is_failure(self) -> bool:
        return self in {Status.FAIL, Status.ERROR}

    def worse_than(self, other: Status) -> bool:
        """Return ``True`` if ``self`` should mask ``other`` when aggregating."""
        order = {Status.SKIP: 0, Status.PASS: 1, Status.WARN: 2, Status.FAIL: 3, Status.ERROR: 4}
        return order[self] > order[other]


class AgentResult(BaseModel):
    """The stable adapter return type (ADR-001).

    Adapters may either emit events live through ``ctx.trace`` or return them in
    ``trace``; the runner merges both without duplicating. ``metadata`` is free
    form and surfaces in reports for debugging (``model``, ``prompt_version``,
    ``agent_version`` ...).
    """

    model_config = ConfigDict(extra="forbid")

    output_text: str = ""
    #: Normalized events. Entries may be :class:`~agentci.core.trace.TraceEvent`
    #: instances or plain dicts; the runner coerces dicts so a malformed
    #: third-party event surfaces as a runner ERROR rather than an import crash.
    trace: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunMetrics(BaseModel):
    """Measurements derived from a trace. Never authored by adapters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    latency_ms: float = 0.0
    cost_usd: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    tool_calls: int = 0
    retries: int = 0
    steps: int = 0
    errors: int = 0
    approvals_requested: int = 0
    approvals_denied: int = 0
    models_used: list[str] = Field(default_factory=list)

    @property
    def has_usage(self) -> bool:
        return self.total_tokens is not None and self.total_tokens > 0

    @property
    def has_cost(self) -> bool:
        return self.cost_usd is not None


def derive_metrics(trace: Trace, *, latency_ms: float | None = None) -> RunMetrics:
    """Extract :class:`RunMetrics` from a normalized trace.

    ``latency_ms`` overrides the trace-derived value so the runner can report
    the true harness wall time (including adapter overhead) rather than the
    span between the first and last recorded event.
    """
    usage = trace.usage_total()
    any_usage = any(e.usage for e in trace.events)
    cost = trace.cost_total()
    any_cost = any(e.cost_usd is not None for e in trace.events)

    models = [
        str(e.component or e.metadata.get("model"))
        for e in trace.events
        if e.type.value.startswith("model_call") and (e.component or e.metadata.get("model"))
    ]

    retries = sum(int(e.metadata.get("retry", 0)) for e in trace.events if e.metadata)
    errors = len(trace.errors())
    approvals_requested = len(trace.by_type(EventType.APPROVAL_REQUESTED))
    approvals_denied = len(trace.by_type(EventType.APPROVAL_DENIED))

    derived_latency = latency_ms if latency_ms is not None else (trace.elapsed_ms() or 0.0)

    return RunMetrics(
        latency_ms=derived_latency,
        cost_usd=cost if any_cost else None,
        prompt_tokens=usage.prompt_tokens if any_usage else None,
        completion_tokens=usage.completion_tokens if any_usage else None,
        total_tokens=usage.total_tokens if any_usage else None,
        tool_calls=trace.step_count(),
        retries=retries,
        steps=trace.step_count(),
        errors=errors,
        approvals_requested=approvals_requested,
        approvals_denied=approvals_denied,
        models_used=sorted(set(models)),
    )


class AssertionOutcome(str, enum.Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERRORED = "errored"


class AssertionResult(BaseModel):
    """The record of one executed expectation.

    Every expectation is recorded, including passing ones, so a report can show
    what was actually verified rather than only what broke.
    """

    model_config = ConfigDict(extra="forbid")

    assertion_id: str
    kind: str = Field(description="output | tool | execution | policy | leak | regression")
    name: str = Field(description="Expectation method name, e.g. to_use_tool")
    description: str
    status: AssertionOutcome
    expected: Any = None
    actual: Any = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status is AssertionOutcome.PASSED


class PolicyViolation(BaseModel):
    """A policy breach detected by the policy engine (never by an assertion)."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(
        description=(
            "denied_tool | unlisted_tool | approval_required | approval_denied | "
            "side_effect_blocked | forbidden_data | max_steps"
        )
    )
    severity: Status = Status.FAIL
    message: str
    tool: str | None = None
    event_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class Dimensions(BaseModel):
    """Dimension-level quality scores (PRD §18).

    Deliberately *not* collapsed into a single composite number. A policy
    violation must never be hidden behind a good average.
    """

    model_config = ConfigDict(extra="forbid")

    task_success: float = 0.0
    tool_correctness: float = 0.0
    policy_compliance: float = 0.0
    reliability: float = 0.0
    cost_efficiency: float | None = None
    latency_ms: float | None = None


class ResultView:
    """Ergonomic, read-only facade over an :class:`AgentResult`.

    Instances are created by :func:`as_view`. They intentionally subclass
    nothing: duck typing keeps ``expect()`` usable with adapters that return a
    ``ResultView``, an ``AgentResult``, or any object exposing ``output_text``
    and ``trace``.
    """

    __slots__ = ("metrics", "policy_violations", "result", "trace")

    def __init__(
        self,
        result: AgentResult,
        trace: Trace,
        metrics: RunMetrics,
        policy_violations: list[PolicyViolation] | None = None,
    ) -> None:
        self.result = result
        self.trace = trace
        self.metrics = metrics
        self.policy_violations = policy_violations or []

    # -- adapter contract passthrough ----------------------------------------

    @property
    def output_text(self) -> str:
        return self.result.output_text

    @property
    def metadata(self) -> dict[str, Any]:
        return self.result.metadata

    # -- convenience ----------------------------------------------------------

    @property
    def tool_calls(self) -> list[ToolCall]:
        return self.trace.tool_calls()

    @property
    def tool_names(self) -> list[str]:
        return self.trace.tool_names()

    @property
    def output(self) -> str:
        return self.result.output_text

    @property
    def cost_usd(self) -> float | None:
        return self.metrics.cost_usd

    @property
    def latency_ms(self) -> float:
        return self.metrics.latency_ms

    @property
    def total_tokens(self) -> int | None:
        return self.metrics.total_tokens

    def has_tool(self, name: str) -> bool:
        return name in self.tool_names

    def repr_excerpt(self, limit: int = 400) -> str:
        text = self.output_text or ""
        return text if len(text) <= limit else text[:limit] + "..."

    def __repr__(self) -> str:
        return (
            f"ResultView(tools={len(self.tool_names)}, "
            f"latency_ms={self.metrics.latency_ms:.0f}, "
            f"cost={self.metrics.cost_usd}, "
            f"violations={len(self.policy_violations)})"
        )


def as_view(obj: Any) -> ResultView:
    """Coerce ``obj`` into a :class:`ResultView`.

    Accepts a ``ResultView`` (returned as-is), an ``AgentResult`` (metrics
    derived on the fly), or a dict in ``AgentResult`` shape.
    """
    if isinstance(obj, ResultView):
        return obj

    if isinstance(obj, dict):
        obj = AgentResult.model_validate(obj)

    if not isinstance(obj, AgentResult):
        raise TypeError(
            "expect() requires an AgentResult or ResultView, "
            f"got {type(obj).__name__}. "
            "Did you call expect() on something other than agent.run(...)?"
        )

    raw_events = [e for e in obj.trace if hasattr(e, "event_id")]
    trace = Trace(
        run_id=raw_events[0].run_id if raw_events else "",
        events=raw_events,
    )
    return ResultView(result=obj, trace=trace, metrics=derive_metrics(trace))
