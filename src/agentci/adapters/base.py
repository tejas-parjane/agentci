"""The adapter contract (PRD §FR-1, ADR-001).

An adapter is the only thing AgentCI knows about an agent. It is intentionally
one method, because everything else in the product — assertions, policy, reports,
replay — is derived from what the adapter reports.

Three ways to supply one, in decreasing order of power:

**1. A class** (recommended; gives you tools and policy mediation)::

    class SupportAgent(AgentAdapter):
        name = "support-agent"
        tools = Toolset({...})

        def run(self, user_input: str, ctx: RunContext) -> AgentResult:
            ...

**2. A plain function** (quickest path to a first test)::

    def run_agent(user_input: str) -> AgentResult:
        return AgentResult(output_text="...")

**3. A generator** (streaming traces for long-running agents)::

    def stream_agent(user_input: str):
        yield TraceEvent(...)   # emitted as produced
        return AgentResult(...)

Framework neutrality (PRD §4, pillar 6) is achieved here: the contract mentions
only AgentCI's own types, so a LangGraph app, an OpenAI Agents app, or hand-rolled
code all satisfy it without an AgentCI dependency on any of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from agentci.core.context import RunContext
from agentci.core.result import AgentResult
from agentci.core.tools import ToolDecl, Toolset, tool

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["AgentAdapter", "ToolDecl", "Toolset", "tool"]


@runtime_checkable
class AgentAdapter(Protocol):
    """Structural protocol satisfied by :class:`agentci.core.adapters.base.AgentAdapter`."""

    name: str

    def run(self, user_input: str, ctx: RunContext) -> AgentResult | Iterator[Any]:
        """Execute the agent once and report what it did.

        May be sync or async. May either emit events through ``ctx.recorder`` while
        working, or return them in ``AgentResult.trace`` — the runner merges both
        without duplication.
        """
        ...


class BaseAdapter:
    """Convenient base class providing the ``tools`` declaration pattern.

    Subclasses override :meth:`run` and usually nothing else::

        class SupportAgent(BaseAdapter):
            name = "support-agent"
            tools = Toolset({
                "search_customer": tool.live(search_customer),
                "issue_refund": tool.live(issue_refund, side_effect=True),
            })

            def run(self, user_input: str, ctx: RunContext) -> AgentResult:
                order = ctx.tools.get_order(order_id="123")
                return AgentResult(output_text="Refund issued")
    """

    #: Display name, used in reports.
    name: str = "agent"

    #: Declared tools. Optional; a plain mapping of name to callable also works.
    tools: Any = Toolset()

    #: Extra keyword arguments merged into the RunContext attributes.
    metadata: dict[str, Any] = {}

    def run(self, user_input: str, ctx: RunContext) -> AgentResult:
        raise NotImplementedError(
            f"{type(self).__name__} must implement run(user_input, ctx)"
        )

    # -- helpers available to subclasses -------------------------------------

    def declared_tools(self) -> dict[str, ToolDecl]:
        """Normalize :attr:`tools` into ``{name: ToolDecl}``.

        Accepts a :class:`~agentci.core.tools.Toolset`, a plain dict of
        ``{name: callable}``, or a list of ``ToolDecl``.
        """
        raw = self.tools
        if isinstance(raw, Toolset):
            return dict(raw)
        if isinstance(raw, dict):
            return {
                name: (
                    value
                    if isinstance(value, ToolDecl)
                    else ToolDecl(name=name, live=value)
                )
                for name, value in raw.items()
            }
        if isinstance(raw, list):
            return {decl.name: decl for decl in raw}
        return {}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r})"
