"""The trace recorder: the only writer of :class:`TraceEvent` objects.

Adapters use this to emit events *as they happen* rather than assembling a trace
afterwards. That distinction matters for three reasons:

1. **Budgets can be enforced.** :meth:`TraceRecorder.tool_call` raises
   :class:`~agentci.errors.StepLimitExceeded` the moment a limit is crossed, so a
   runaway agent is stopped instead of being awaited to completion.
2. **Traces are faithful.** Events land in true execution order, with real
   durations, even if the agent later raises.
3. **Reports can show partial evidence.** An ERROR test still has a trace.

Parent/child linkage uses a :class:`contextvars.ContextVar` rather than a plain
instance attribute. Under sync code that behaves like thread-local state; under
``asyncio`` each task receives a copy of the context, so two concurrently
awaited tool calls cannot corrupt each other's parent chain.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import TracebackType
from typing import Any, Self

from agentci.core.trace import (
    EventStatus,
    EventType,
    MonotonicTimer,
    TokenUsage,
    ToolCall,
    Trace,
    TraceEvent,
    new_id,
    utc_now,
)
from agentci.errors import DeadlineExceeded, StepLimitExceeded

_parent_stack: ContextVar[tuple[str, ...]] = ContextVar("agentci_parent_stack", default=())


class EventContext:
    """Base handle returned by the recorder's context managers.

    Adapters assign to :attr:`result`, :attr:`usage`, :attr:`cost_usd` and
    :attr:`metadata` inside the ``with`` block; the recorder serializes them onto
    the completion event on exit.
    """

    __slots__ = ("_recorder", "_started", "_timer", "event")

    def __init__(self, recorder: TraceRecorder, event: TraceEvent) -> None:
        self._recorder = recorder
        self.event = event
        self._timer = MonotonicTimer()
        self._started = False

    def __enter__(self) -> Self:
        self._started = True
        self._timer.reset()
        return self

    @property
    def call_id(self) -> str:
        # `or` rather than a conditional: `ToolCall.call_id` is itself optional,
        # so a tool event with no call id must still fall back to the event id.
        tool = self.event.tool
        return (tool.call_id if tool else None) or self.event.event_id

    def _finalize(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        if exc is not None:
            self._recorder.emit(
                EventType.ERROR,
                parent_id=self.event.parent_id,
                component=self.event.component,
                tool=self.event.tool,
                error=f"{type(exc).__name__}: {exc}",
                status=EventStatus.ERROR,
                duration_ms=self._timer.elapsed_ms,
            )
        self._finalize()


class ToolCallContext(EventContext):
    """Handle for :meth:`TraceRecorder.tool_call`."""

    __slots__ = ("cost_usd", "metadata", "result", "status", "usage")

    def __init__(self, recorder: TraceRecorder, event: TraceEvent) -> None:
        super().__init__(recorder, event)
        self.result: Any = None
        self.usage: TokenUsage | None = None
        self.cost_usd: float | None = None
        self.metadata: dict[str, Any] = {}
        self.status: EventStatus = EventStatus.SUCCESS

    def _finalize(self) -> None:
        self._recorder.emit(
            EventType.TOOL_CALL_COMPLETED,
            parent_id=self.event.parent_id,
            component=self.event.component,
            tool=self.event.tool,
            result=self.result,
            usage=self.usage,
            cost_usd=self.cost_usd,
            duration_ms=self._timer.elapsed_ms,
            status=self.status,
            metadata={**self.event.metadata, **self.metadata},
        )


class ModelCallContext(EventContext):
    """Handle for :meth:`TraceRecorder.model_call`."""

    __slots__ = ("cost_usd", "metadata", "result", "status", "usage")

    def __init__(self, recorder: TraceRecorder, event: TraceEvent) -> None:
        super().__init__(recorder, event)
        self.result: Any = None
        self.usage: TokenUsage | None = None
        self.cost_usd: float | None = None
        self.metadata: dict[str, Any] = {}
        self.status: EventStatus = EventStatus.SUCCESS

    def _finalize(self) -> None:
        self._recorder.emit(
            EventType.MODEL_CALL_COMPLETED,
            parent_id=self.event.parent_id,
            component=self.event.component,
            result=self.result,
            usage=self.usage,
            cost_usd=self.cost_usd,
            duration_ms=self._timer.elapsed_ms,
            status=self.status,
            metadata={**self.event.metadata, **self.metadata},
        )


class RetrievalContext(EventContext):
    """Handle for :meth:`TraceRecorder.retrieval`."""

    __slots__ = ("documents", "metadata", "result", "status")

    def __init__(self, recorder: TraceRecorder, event: TraceEvent) -> None:
        super().__init__(recorder, event)
        self.result: Any = None
        self.documents: list[Any] = []
        self.metadata: dict[str, Any] = {}
        self.status: EventStatus = EventStatus.SUCCESS

    def _finalize(self) -> None:
        self._recorder.emit(
            EventType.RETRIEVAL_COMPLETED,
            parent_id=self.event.parent_id,
            component=self.event.component,
            result=self.result,
            duration_ms=self._timer.elapsed_ms,
            status=self.status,
            metadata={
                **self.event.metadata,
                **self.metadata,
                "document_count": len(self.documents),
            },
        )


class TraceRecorder:
    """Collects normalized events for a single agent run."""

    def __init__(
        self,
        run_id: str | None = None,
        *,
        max_steps: int | None = None,
        max_duration_ms: float | None = None,
        component: str | None = None,
    ) -> None:
        self.run_id = run_id or new_id("run")
        self.trace = Trace(run_id=self.run_id)
        self.max_steps = max_steps
        self.max_duration_ms = max_duration_ms
        self.component = component
        self._timer = MonotonicTimer()
        self._tokens = _parent_stack.set(())

    # -- low level ------------------------------------------------------------

    def emit(
        self,
        event_type: EventType,
        *,
        parent_id: str | None = None,
        run_id: str | None = None,
        **fields: Any,
    ) -> TraceEvent:
        """Append an event and return it.

        ``parent_id`` defaults to the innermost open context, which is what makes
        ``with rec.tool_call(...)`` nest correctly without the caller threading
        identifiers through.
        """
        self._check_deadline()
        parent = parent_id
        if parent is None:
            stack = _parent_stack.get()
            parent = stack[-1] if stack else None

        event = TraceEvent(
            run_id=run_id or self.run_id,
            event_id=new_id("evt"),
            type=event_type,
            timestamp=utc_now(),
            parent_id=parent,
            **fields,
        )
        self.trace.events.append(event)
        return event

    def _check_deadline(self) -> None:
        if self.max_duration_ms is not None and self._timer.elapsed_ms > self.max_duration_ms:
            raise DeadlineExceeded(self.max_duration_ms)

    def _check_steps(self) -> None:
        if self.max_steps is not None and self.trace.step_count() >= self.max_steps:
            raise StepLimitExceeded(self.max_steps)

    @contextmanager
    def _scoped_parent(self, event_id: str) -> Iterator[None]:
        token = _parent_stack.set((*_parent_stack.get(), event_id))
        try:
            yield
        finally:
            _parent_stack.reset(token)

    # -- lifecycle ------------------------------------------------------------

    def start(self, **metadata: Any) -> TraceEvent:
        self._timer.reset()
        return self.emit(EventType.RUN_STARTED, component=self.component, metadata=metadata)

    def complete(self, **metadata: Any) -> TraceEvent:
        return self.emit(
            EventType.RUN_COMPLETED,
            component=self.component,
            duration_ms=self._timer.elapsed_ms,
            metadata=metadata,
        )

    def error(self, message: str, **fields: Any) -> TraceEvent:
        return self.emit(
            EventType.ERROR,
            component=self.component,
            error=message,
            status=EventStatus.ERROR,
            **fields,
        )

    # -- typed context managers ----------------------------------------------

    @contextmanager
    def tool_call(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        component: str | None = None,
        call_id: str | None = None,
        **metadata: Any,
    ) -> Iterator[ToolCallContext]:
        """Record a tool invocation, enforcing ``max_steps``.

        The step check happens *before* the start event is emitted, so exceeding
        the budget produces exactly ``max_steps`` tool events and one error,
        rather than overshooting.
        """
        self._check_steps()
        call = ToolCall(name=name, arguments=dict(arguments or {}), call_id=call_id or new_id("call"))
        start = self.emit(
            EventType.TOOL_CALL_STARTED,
            component=component or self.component,
            tool=call,
            status=EventStatus.PENDING,
            metadata=metadata,
        )
        ctx = ToolCallContext(self, start)
        with self._scoped_parent(start.event_id), ctx:
            yield ctx

    @contextmanager
    def model_call(
        self,
        model: str,
        *,
        component: str | None = None,
        prompt: str | None = None,
        **metadata: Any,
    ) -> Iterator[ModelCallContext]:
        """Record a model invocation and its token usage."""
        start = self.emit(
            EventType.MODEL_CALL_STARTED,
            component=component or model,
            status=EventStatus.PENDING,
            metadata={"model": model, **metadata},
        )
        if prompt is not None:
            start.metadata["prompt_chars"] = len(prompt)
        ctx = ModelCallContext(self, start)
        with self._scoped_parent(start.event_id), ctx:
            yield ctx

    @contextmanager
    def retrieval(
        self,
        source: str,
        *,
        query: str | None = None,
        component: str | None = None,
        **metadata: Any,
    ) -> Iterator[RetrievalContext]:
        """Record a retrieval step and how many documents it returned."""
        start = self.emit(
            EventType.RETRIEVAL_STARTED,
            component=component or source,
            metadata={"source": source, **metadata},
        )
        if query is not None:
            start.metadata["query_chars"] = len(query)
        ctx = RetrievalContext(self, start)
        with self._scoped_parent(start.event_id), ctx:
            yield ctx

    # -- discrete events ------------------------------------------------------

    def memory_read(self, key: str, **metadata: Any) -> TraceEvent:
        return self.emit(
            EventType.MEMORY_READ, component="memory", metadata={"key": key, **metadata}
        )

    def memory_write(self, key: str, value: Any = None, **metadata: Any) -> TraceEvent:
        return self.emit(
            EventType.MEMORY_WRITE,
            component="memory",
            metadata={"key": key, "value_present": value is not None, **metadata},
        )

    def approval_requested(self, tool: str, **metadata: Any) -> TraceEvent:
        return self.emit(
            EventType.APPROVAL_REQUESTED,
            tool=ToolCall(name=tool),
            metadata=metadata,
        )

    def approval_granted(self, tool: str, **metadata: Any) -> TraceEvent:
        return self.emit(EventType.APPROVAL_GRANTED, tool=ToolCall(name=tool), metadata=metadata)

    def approval_denied(self, tool: str, **metadata: Any) -> TraceEvent:
        return self.emit(
            EventType.APPROVAL_DENIED, tool=ToolCall(name=tool), status=EventStatus.DENIED,
            metadata=metadata,
        )

    # -- accessors ------------------------------------------------------------

    @property
    def events(self) -> list[TraceEvent]:
        return self.trace.events

    @property
    def step_count(self) -> int:
        return self.trace.step_count()

    @property
    def elapsed_ms(self) -> float:
        return self._timer.elapsed_ms
