"""AgentCI — CI and release gates for AI agents.

Import surface for test authors and adapter authors. Two groups:

* **Writing tests** — :func:`agent_test`, :func:`expect`, and the result types the
  injected ``agent`` gives back.
* **Writing adapters** — :class:`AgentResult`, :class:`TraceRecorder`,
  :class:`TokenUsage`, and the errors raised when a policy refuses a tool.

Everything else (config, policy engine, storage) is deliberately not exported:
it is reached through ``agentci.yaml`` or the CLI, and exporting it would make it
public API before it is ready to be.
"""

from agentci.__about__ import TRACE_SCHEMA_VERSION, __version__
from agentci.assertions import expect
from agentci.core.result import (
    AgentResult,
    PolicyViolation,
    RunMetrics,
    Status,
)
from agentci.core.trace import TokenUsage, Trace, TraceEvent
from agentci.errors import (
    AdapterError,
    AgentCIError,
    ConfigError,
    DeadlineExceeded,
    DiscoveryError,
    ExitCode,
    PolicyViolationError,
    ReplayError,
    SideEffectBlocked,
    StepLimitExceeded,
)
from agentci.reporting.models import RunReport, TestReport
from agentci.testing import agent_test

__all__ = [
    "TRACE_SCHEMA_VERSION",
    "AdapterError",
    "AgentCIError",
    "AgentResult",
    "ConfigError",
    "DeadlineExceeded",
    "DiscoveryError",
    "ExitCode",
    "PolicyViolation",
    "PolicyViolationError",
    "ReplayError",
    "RunMetrics",
    "RunReport",
    "SideEffectBlocked",
    "Status",
    "StepLimitExceeded",
    "TestReport",
    "TokenUsage",
    "Trace",
    "TraceEvent",
    "__version__",
    "agent_test",
    "expect",
]
