"""Run an existing openai-agents application without rewriting it.

``AgentCI(root_agent)`` re-roots an ``agents.Agent`` onto agentci: every
function tool in the reachable agent graph routes through ``ctx.tools``, so
agentci's replay answers, mocks, denials, approvals and side-effect gates apply
to an agent the project never modified. Tool calls and model calls land in the
same trace artifact as any other adapter.

Each ``run`` clones the reachable agent graph with routing twins of every
function tool (``agent.clone`` becomes a fresh list, so the shared original
agent and tool objects are never mutated), then runs ``Runner.run_sync`` on a
worker thread: the SDK refuses to run when a loop is already active, and the
harness may invoke the adapter from inside a test.
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agents import Agent, FunctionTool, RunConfig, Runner

from agentci.adapters.base import BaseAdapter
from agentci.core.context import RunContext
from agentci.core.result import AgentResult
from agentci.core.tools import ToolDecl, Toolset
from agentci.errors import DeadlineExceeded

from ._hooks import AgentCIHooks
from ._tools import build_live_reinvoker, wrap_tools


class OpenAIAgentsAdapter(BaseAdapter):
    """An agentci adapter in front of an openai-agents application."""

    name: str = "openai-agents"

    def __init__(
        self,
        root_agent: Agent[Any],
        *,
        name: str | None = None,
        max_turns: int = 20,
    ) -> None:
        self._root_agent = root_agent
        self.max_turns = max_turns
        if name is not None:
            self.name = name
        self.tools = Toolset(
            {
                tool.name: ToolDecl(
                    name=tool.name,
                    live=build_live_reinvoker(tool),
                    description=tool.description,
                )
                for tool in _function_tools(root_agent)
            }
        )

    def run(self, user_input: str, ctx: RunContext) -> AgentResult:
        wrapped = _wrap_agent_graph(self._root_agent, ctx.tools)
        hooks = AgentCIHooks(ctx.recorder)
        timeout_s: float | None = None
        if ctx.max_duration_ms is not None:
            timeout_s = max(1.0, ctx.max_duration_ms / 1000.0)
        result = _run_in_worker(timeout_s, Runner.run(
                wrapped,
                user_input,
                max_turns=self.max_turns,
                hooks=hooks,
                run_config=RunConfig(tracing_disabled=True),
            ))
        output: Any = result.final_output
        if not isinstance(output, str):
            output = "" if output is None else str(output)
        return AgentResult(output_text=output, metadata={"agent": result.last_agent.name})


class AgentCI(OpenAIAgentsAdapter):
    """Wrap an openai-agents application for AgentCI.

    Use it directly when a project points ``agent.adapter`` at a module where an
    instance is available::

        adapter = AgentCI(_build_support_agent())

    Subclass it for the no-argument ``module:Class`` form in ``agentci.yaml``::

        class SupportAgent(AgentCI):
            def __init__(self) -> None:
                super().__init__(_build_support_agent())
    """

    def __init__(
        self,
        root_agent: Agent[Any],
        *,
        name: str = "openai-agents",
        max_turns: int = 20,
    ) -> None:
        super().__init__(root_agent, name=name, max_turns=max_turns)


def _function_tools(root: Agent[Any]) -> list[FunctionTool]:
    """Every function tool reachable from ``root``, once, across handoffs."""
    tools: dict[int, FunctionTool] = {}
    seen_agents: set[int] = set()
    stack: list[Agent[Any]] = [root]
    while stack:
        agent = stack.pop()
        if id(agent) in seen_agents:
            continue
        seen_agents.add(id(agent))
        for tool in agent.tools:
            if isinstance(tool, FunctionTool) and id(tool) not in tools:
                tools[id(tool)] = tool
        stack.extend(hop for hop in agent.handoffs if isinstance(hop, Agent))
    return list(tools.values())


def _wrap_agent_graph(root: Agent[Any], registry: Any) -> Agent[Any]:
    """Clone the reachable graph, routing every function tool.

    ``memo`` is keyed on identity so cycles and shared sub-agents are cloned
    exactly once and the original graph keeps every shared tool object.
    """
    memo: dict[int, Agent[Any]] = {}

    def wrap(agent: Agent[Any]) -> Agent[Any]:
        key = id(agent)
        cached = memo.get(key)
        if cached is not None:
            return cached
        wrapped_tools = wrap_tools(agent.tools, registry)
        memo[key] = agent.clone(tools=wrapped_tools)
        if agent.handoffs:
            wrapped_handoffs = [
                wrap(hop) if isinstance(hop, Agent) else hop for hop in agent.handoffs
            ]
            memo[key] = agent.clone(tools=wrapped_tools, handoffs=wrapped_handoffs)
        return memo[key]

    return wrap(root)


def _run_in_worker(timeout: float | None, awaitable: Awaitable[Any]) -> Any:
    """Run a blocking SDK coroutine on a fresh thread with a private loop.

    ``Runner.run_sync`` raises when a loop is already active and leaves its
    thread's default loop open for reuse; since this harness may call the
    adapter from inside a running loop and never reuses worker threads, a
    private loop that is always closed keeps both behaviors out of the way.
    ``warnings.catch_warnings`` scopes the SDK's ``asyncio`` deprecation
    chatter, which a warning-as-error suite would otherwise turn into a
    failure. Shutting the pool down without waiting on timeout keeps a hung
    SDK run from stalling the whole suite.
    """

    def invoke() -> Any:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(awaitable)
            finally:
                loop.close()

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(invoke)
    try:
        outcome = future.result(timeout=timeout)
    except TimeoutError:
        pool.shutdown(wait=False, cancel_futures=True)
        raise DeadlineExceeded(timeout or 0) from None
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return outcome


__all__ = ["AgentCI", "OpenAIAgentsAdapter"]
