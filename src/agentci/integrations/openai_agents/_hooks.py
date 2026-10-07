"""SDK run hooks that translate model calls into agentci trace events.

Only the model-call lifecycle is translated. Tool calls are already recorded by
the :class:`ToolRegistry` when they route through ``ctx.tools``; the closed
trace grammar (``docs/specification/trace-v1.md``) has no event type for agent
handoffs, so they are intentionally not fabricated.
"""

from __future__ import annotations

from typing import Any

from agents import Agent, RunContextWrapper
from agents.items import ItemHelpers, ModelResponse
from agents.lifecycle import RunHooksBase
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage

from agentci.core.recorder import TraceRecorder
from agentci.core.trace import EventStatus, EventType, TokenUsage


class AgentCIHooks(RunHooksBase[Any, Agent[Any]]):
    """Translate one SDK run into agentci ``model_call`` events.

    Hooks fire on the worker thread that runs ``Runner.run_sync``, so events
    land on the same contextvars stack as the routed tool calls and parent
    linkage stays coherent. ``on_llm_start``/``on_llm_end`` pair per model
    call; the run loop awaits each call before continuing, so a FIFO stack
    pairs them correctly.
    """

    def __init__(self, recorder: TraceRecorder) -> None:
        self._recorder = recorder
        self._pending: list[tuple[Any, str, str]] = []

    async def on_llm_start(
        self,
        _context: RunContextWrapper[Any],
        agent: Agent[Any],
        system_prompt: str | None,
        _input_items: list[Any],
    ) -> None:
        model = _model_name(agent)
        started = self._recorder.emit(
            EventType.MODEL_CALL_STARTED,
            component=model,
            status=EventStatus.PENDING,
            metadata={
                "model": model,
                "agent": agent.name,
                "prompt_chars": len(system_prompt) if system_prompt else 0,
            },
        )
        self._pending.append((started, model, agent.name))

    async def on_llm_end(
        self,
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        response: ModelResponse,
    ) -> None:
        started, model, name = self._pending.pop() if self._pending else (None, "", "")
        self._recorder.emit(
            EventType.MODEL_CALL_COMPLETED,
            parent_id=getattr(started, "parent_id", None),
            component=model,
            result=_output_text(response),
            usage=_to_usage(response.usage),
            status=EventStatus.SUCCESS,
            metadata={"model": model, "agent": name},
        )


def _model_name(agent: Agent[Any]) -> str:
    model = agent.model
    if isinstance(model, str):
        return model
    attribute = getattr(model, "model_name", None)
    if isinstance(attribute, str) and attribute:
        return attribute
    return type(model).__name__


def _output_text(response: ModelResponse) -> str:
    texts = [
        ItemHelpers.extract_last_content(item)
        for item in response.output
        if isinstance(item, ResponseOutputMessage)
    ]
    return "\n".join(texts)


def _to_usage(usage: Usage | None) -> TokenUsage | None:
    if usage is None:
        return None
    total = usage.total_tokens
    return TokenUsage(
        prompt_tokens=usage.input_tokens,
        completion_tokens=usage.output_tokens,
        total_tokens=total,
    )


__all__ = ["AgentCIHooks"]
