"""Route SDK function tools through the AgentCI interception point.

``OpenAIAgentsAdapter`` swaps every reachable ``FunctionTool.on_invoke_tool``
for one that calls ``ctx.tools`` (the :class:`ToolRegistry`). The SDK keeps
doing everything else --- parsing schema-validated arguments, running tool
guardrails and timeouts, stringifying results, deciding the run outcome --- so
an existing agent is intercepted without being rewritten, and a policy
violation raised by the registry fails the run exactly like a normal tool
error.
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import Callable, Iterable
from typing import Any

from agents import FunctionTool

from agentci.core.tools import ToolRegistry

#: The SDK ``ToolContext`` of the invocation currently being routed. The live
#: re-invocation reads it back so the tool body receives the exact context the
#: SDK would have passed it, while the stub data never leaks into the trace.
_current_tool_context: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "agentci_openai_tool_context",
    default=None,
)


def build_live_reinvoker(tool: FunctionTool, /) -> Callable[..., Any]:
    """Declare an SDK function tool as an agentci tool declaration.

    The returned callable is the ``ToolDecl.live`` behind ``{tool.name}``. It is
    reached only during live execution: it hands the tool's original invoker the
    SDK ``ToolContext`` stashed by the routing wrapper, so schema validation,
    guardrails, timeouts and failure handling keep their exact SDK behavior.
    """

    async def live(**arguments: Any) -> Any:
        sdk_context = _current_tool_context.get()
        if sdk_context is None:
            raise RuntimeError(
                f"tool {tool.name!r} is only callable from inside an openai-agents run"
            )
        return await tool.on_invoke_tool(sdk_context, json.dumps(arguments))

    return live


def wrap_tools(agent_tools: Iterable[Any], registry: ToolRegistry) -> list[Any]:
    """Wrap every function tool so its calls route through ``registry``.

    Non-function tools (hosted MCP tools, agent-as-tool, custom tools) are kept
    as-is: they cannot be re-routed without losing their native behavior, and
    are not something a recording can stand in for either.
    """
    wrapped: list[Any] = []
    seen: dict[int, Any] = {}
    for tool in agent_tools:
        if not isinstance(tool, FunctionTool):
            wrapped.append(tool)
            continue
        cached = seen.get(id(tool))
        if cached is not None:
            wrapped.append(cached)
            continue
        wrapper = _wrap_function_tool(tool, registry)
        seen[id(tool)] = wrapper
        wrapped.append(wrapper)
    return wrapped


def _wrap_function_tool(tool: FunctionTool, registry: ToolRegistry) -> FunctionTool:
    """Build the routing twin of ``tool``, preserving its public contract."""

    async def route(ctx: Any, input_json: str) -> Any:
        arguments = _parse_arguments(input_json)
        token = _current_tool_context.set(ctx)
        try:
            result = await registry.ainvoke(tool.name, arguments)
        finally:
            _current_tool_context.reset(token)
        return result

    return FunctionTool(
        name=tool.name,
        description=tool.description,
        params_json_schema=tool.params_json_schema,
        on_invoke_tool=route,
        strict_json_schema=tool.strict_json_schema,
        is_enabled=tool.is_enabled,
        tool_input_guardrails=tool.tool_input_guardrails,
        tool_output_guardrails=tool.tool_output_guardrails,
        needs_approval=tool.needs_approval,
        timeout_seconds=tool.timeout_seconds,
        timeout_behavior=tool.timeout_behavior,
        timeout_error_function=tool.timeout_error_function,
        defer_loading=tool.defer_loading,
        custom_data_extractor=tool.custom_data_extractor,
        allowed_callers=tool.allowed_callers,
        output_json_schema=tool.output_json_schema,
    )


def _parse_arguments(input_json: str) -> dict[str, Any]:
    if not input_json:
        return {}
    parsed = json.loads(input_json)
    if not isinstance(parsed, dict):
        raise ValueError("function tool arguments must be a JSON object")
    return parsed


__all__ = ["build_live_reinvoker", "wrap_tools"]
