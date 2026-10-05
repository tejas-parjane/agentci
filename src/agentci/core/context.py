"""The per-invocation context handed to an adapter.

:class:`RunContext` is the adapter's window into AgentCI. It is deliberately
small: a recorder, a tool registry, the run identifier, and read-only limits.
Adapters that need more should be constructed with it rather than reaching into
AgentCI internals.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

from agentci.core.recorder import TraceRecorder
from agentci.core.tools import ToolRegistry
from agentci.core.trace import TokenUsage


class RunContext:
    """State for a single agent invocation."""

    __slots__ = (
        "agent_name",
        "attributes",
        "iteration",
        "max_duration_ms",
        "max_steps",
        "recorder",
        "run_id",
        "tools",
    )

    def __init__(
        self,
        *,
        run_id: str,
        recorder: TraceRecorder,
        tools: ToolRegistry,
        agent_name: str = "agent",
        max_steps: int | None = None,
        max_duration_ms: float | None = None,
        iteration: int = 1,
    ) -> None:
        self.run_id = run_id
        self.recorder = recorder
        self.tools = tools
        self.agent_name = agent_name
        self.max_steps = max_steps
        self.max_duration_ms = max_duration_ms
        self.iteration = iteration
        self.attributes: dict[str, Any] = {}

    # -- convenience wrappers -------------------------------------------------

    @property
    def trace(self) -> TraceRecorder:
        """Alias for :attr:`recorder`, matching the README example ``ctx.trace.*``."""
        return self.recorder

    def set(self, key: str, value: Any) -> None:
        """Attach arbitrary metadata that will appear in the run report."""
        self.attributes[key] = value

    def model_call(self, model: str, **kwargs: Any) -> Any:
        return self.recorder.model_call(model, **kwargs)

    def record_usage(self, model: str, usage: TokenUsage, cost_usd: float | None = None) -> None:
        """Record a completed model call.

        Adapters that cannot use the ``model_call`` context manager (for example,
        one that wraps a streaming SDK) can call this once the call finishes.
        """
        self.recorder.emit(
            "model_call_completed",  # type: ignore[arg-type]
            component=model,
            usage=usage,
            cost_usd=cost_usd,
            metadata={"model": model},
        )

    def __repr__(self) -> str:
        return (
            f"RunContext(run_id={self.run_id!r}, agent={self.agent_name!r}, "
            f"steps={self.recorder.step_count}/{self.max_steps})"
        )

    def __enter__(self) -> Self:
        self.recorder.start(**self.attributes)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        if exc is not None:
            self.recorder.error(f"{type(exc).__name__}: {exc}")
            return
        self.recorder.complete(**self.attributes)


__all__ = ["RunContext"]
