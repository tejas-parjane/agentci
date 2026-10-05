"""Adapter implementations.

``base``
    The contract and a convenience base class.
``python``
    In-process loading and invocation of sync, async, and streaming adapters.
``http``
    Testing an agent that lives behind an HTTP boundary.

Framework integrations (LangGraph, OpenAI Agents, MCP — PRD §22) are deliberately
absent from the core. They will be separate distributions built on the adapter
protocol, so that AgentCI never depends on a framework and a framework upgrade
cannot break the test runner.
"""

from agentci.adapters.base import AgentAdapter, BaseAdapter, ToolDecl, Toolset, tool
from agentci.adapters.python import (
    InvocationResult,
    LoadedAdapter,
    classify_error,
    import_target,
    invoke,
    load_adapter,
)

__all__ = [
    "AgentAdapter",
    "BaseAdapter",
    "InvocationResult",
    "LoadedAdapter",
    "ToolDecl",
    "Toolset",
    "classify_error",
    "import_target",
    "invoke",
    "load_adapter",
    "tool",
]
