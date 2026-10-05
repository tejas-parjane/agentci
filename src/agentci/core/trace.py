"""Normalized agent trace schema (PRD §FR-2, §14).

The trace is the product's central data structure: every assertion except pure
output-text checks reads from it. It is deliberately framework-neutral and only
*inspired* by OpenTelemetry concepts, so that OTel can be an optional exporter
rather than a dependency (ADR-006).

Design rules that other parts of the codebase rely on:

* **Durations come from a monotonic clock.** ``timestamp`` is wall-clock for
  humans and serialization; ``duration_ms`` is ``perf_counter``-derived so that
  NTP steps can never produce negative or absurd durations.
* **Every event belongs to exactly one run** (``run_id``) and carries a
  ``parent_id`` so nested work (a tool call inside a model call) can be
  reassembled without knowledge of the emitting framework.
* **Events are immutable once emitted.** The recorder is the only writer.
* **Payloads are redacted at serialization time**, not here, so assertions keep
  access to real values while nothing sensitive reaches disk (see ADR-008).
"""

from __future__ import annotations

import enum
import time
import uuid
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from agentci.__about__ import TRACE_SCHEMA_VERSION


def utc_now() -> datetime:
    """Timezone-aware UTC now. Used everywhere instead of ``datetime.now()``."""
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    """Generate a short, prefixed, collision-resistant identifier."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class EventType(str, enum.Enum):
    """The closed set of normalized trace event types (PRD §FR-2)."""

    RUN_STARTED = "run_started"
    MODEL_CALL_STARTED = "model_call_started"
    MODEL_CALL_COMPLETED = "model_call_completed"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    RETRIEVAL_STARTED = "retrieval_started"
    RETRIEVAL_COMPLETED = "retrieval_completed"
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    ERROR = "error"
    RUN_COMPLETED = "run_completed"

    @property
    def is_start(self) -> bool:
        return self.value.endswith("_started")


class EventStatus(str, enum.Enum):
    """Outcome of the step an event describes."""

    PENDING = "pending"
    SUCCESS = "success"
    ERROR = "error"
    DENIED = "denied"
    MOCKED = "mocked"


class TokenUsage(BaseModel):
    """Model token accounting.

    Adapters report this when the underlying provider exposes it. When they do
    not, :attr:`total_tokens` stays ``None`` and cost-dependent assertions
    report ``SKIPPED`` rather than silently passing (§32).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _derive_total(self) -> Self:
        if self.total_tokens is None:
            derived = self.prompt_tokens + self.completion_tokens
            object.__setattr__(self, "total_tokens", derived)
        return self

    def __add__(self, other: TokenUsage) -> TokenUsage:
        if not isinstance(other, TokenUsage):  # pragma: no cover - guard
            return NotImplemented
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )

    def __bool__(self) -> bool:
        return bool(self.prompt_tokens or self.completion_tokens)


class ToolCall(BaseModel):
    """The identity of a tool invocation: its name and its arguments."""

    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str | None = None


class TraceEvent(BaseModel):
    """One normalized step in an agent's execution.

    Serialized form matches the example in PRD §14. Field names are the stable
    public contract; ``metadata`` is the documented extension point.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    run_id: str
    event_id: str
    type: EventType
    timestamp: datetime = Field(default_factory=utc_now)
    parent_id: str | None = None
    component: str | None = None
    tool: ToolCall | None = None
    result: Any | None = None
    usage: TokenUsage | None = None
    cost_usd: float | None = Field(default=None, ge=0)
    duration_ms: float | None = Field(default=None, ge=0)
    status: EventStatus = EventStatus.SUCCESS
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def schema_version(self) -> int:
        return TRACE_SCHEMA_VERSION

    @model_validator(mode="before")
    @classmethod
    def _accept_persisted_schema_version(cls, data: Any) -> Any:
        """Let a previously serialized event validate against this model.

        ``schema_version`` is a computed field, so it appears in serialized output
        but is not a stored field. With ``extra="forbid"`` that combination makes
        the output of :meth:`to_dict` unacceptable to this very class, which broke
        replay outright: every trace AgentCI wrote failed to read back. Dropping
        the key on the way in restores a genuine round trip while still rejecting
        keys nobody has heard of. The value itself is checked by
        :func:`agentci.core.storage.load_trace`, which is where a real version
        mismatch belongs.
        """
        if isinstance(data, dict) and "schema_version" in data:
            data = {key: value for key, value in data.items() if key != "schema_version"}
        return data

    @property
    def is_tool_call(self) -> bool:
        return self.tool is not None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict with enum values flattened."""
        data = self.model_dump(mode="json")
        data["type"] = self.type.value
        data["status"] = self.status.value
        return data


class Trace(BaseModel):
    """An ordered, append-only collection of :class:`TraceEvent`.

    ``Trace`` is what every assertion queries. It is deliberately *not* a tree:
    parent/child linkage is preserved as data (``parent_id``) so that a consumer
    can build a tree only if it wants to, without the recorder paying for it.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(default_factory=lambda: new_id("run"))
    events: list[TraceEvent] = Field(default_factory=list)

    def __iter__(self) -> Iterator[TraceEvent]:  # type: ignore[override]
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)

    def __bool__(self) -> bool:
        return bool(self.events)

    def append(self, event: TraceEvent) -> None:
        self.events.append(event)

    def extend(self, events: Iterable[TraceEvent]) -> None:
        self.events.extend(events)

    # -- queries used by assertions -------------------------------------------

    def by_type(self, *types: EventType) -> list[TraceEvent]:
        wanted = set(types)
        return [e for e in self.events if e.type in wanted]

    def tool_calls(self) -> list[ToolCall]:
        """Every tool invocation, in execution order, de-duplicated by ``call_id``.

        A tool call produces a ``tool_call_started`` and a ``tool_call_completed``
        event. Assertions care about *invocations*, so this collapses the pair.
        Events without a ``call_id`` (adapter-supplied) are deduplicated by
        event position.
        """
        seen: set[str] = set()
        calls: list[ToolCall] = []
        for event in self.events:
            if event.type is not EventType.TOOL_CALL_STARTED or event.tool is None:
                continue
            key = event.tool.call_id or event.event_id
            if key in seen:
                continue
            seen.add(key)
            calls.append(event.tool)
        return calls

    def tool_names(self) -> list[str]:
        return [call.name for call in self.tool_calls()]

    def tool_events(self, name: str) -> list[TraceEvent]:
        """Every event mentioning the named tool, start and completion alike."""
        return [e for e in self.events if e.tool is not None and e.tool.name == name]

    def first_tool_call(self, name: str) -> ToolCall | None:
        for call in self.tool_calls():
            if call.name == name:
                return call
        return None

    def errors(self) -> list[TraceEvent]:
        return [e for e in self.events if e.status is EventStatus.ERROR or e.type is EventType.ERROR]

    def approvals(self) -> tuple[list[str], list[str]]:
        """Return ``(granted, denied)`` tool names based on approval events."""
        granted: list[str] = []
        denied: list[str] = []
        for event in self.events:
            name = event.tool.name if event.tool else event.component
            if name is None:
                continue
            if event.type is EventType.APPROVAL_GRANTED:
                granted.append(name)
            elif event.type is EventType.APPROVAL_DENIED:
                denied.append(name)
        return granted, denied

    def child_of(self, event_id: str) -> list[TraceEvent]:
        return [e for e in self.events if e.parent_id == event_id]

    def usage_total(self) -> TokenUsage:
        """Sum token usage across every event that reports it."""
        total = TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        for event in self.events:
            if event.usage:
                total = total + event.usage
        return total

    def cost_total(self) -> float:
        """Sum cost across every event that reports it."""
        return sum(e.cost_usd for e in self.events if e.cost_usd is not None)

    def step_count(self) -> int:
        """Number of tool invocations; this is what ``budgets.max_steps`` bounds."""
        return len(self.tool_calls())

    def elapsed_ms(self) -> float | None:
        """Wall-clock span of the run, from ``run_started`` to ``run_completed``."""
        if not self.events:
            return None
        return (self.events[-1].timestamp - self.events[0].timestamp).total_seconds() * 1000

    def summary(self) -> list[str]:
        """Compact single-line-per-event rendering, used by reports and ``replay``."""
        lines: list[str] = []
        for event in self.events:
            indent = ""
            if event.type in {EventType.TOOL_CALL_COMPLETED, EventType.RETRIEVAL_COMPLETED}:
                indent = "  "
            label = event.type.value
            if event.tool:
                label += f" {event.tool.name}"
            elif event.component:
                label += f" {event.component}"
            duration = f" [{event.duration_ms:.0f}ms]" if event.duration_ms else ""
            lines.append(f"{indent}{label} ({event.status.value}){duration}")
        return lines


class MonotonicTimer:
    """A monotonic stopwatch.

    Used for every ``duration_ms`` AgentCI measures itself. Wall-clock
    adjustments (NTP, suspend/resume, DST) cannot corrupt it.
    """

    __slots__ = ("_start",)

    def __init__(self) -> None:
        self._start = time.perf_counter()

    def reset(self) -> None:
        self._start = time.perf_counter()

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000.0

    @property
    def elapsed_s(self) -> float:
        return time.perf_counter() - self._start

    def __enter__(self) -> Self:
        self.reset()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.reset()
