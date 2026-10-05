"""Exception hierarchy and stable process exit codes.

Every error AgentCI raises intentionally derives from :class:`AgentCIError`, which
carries the exit code the CLI should terminate with. This keeps §32 ("differentiate
product failure from agent failure") enforceable: an infrastructure fault never
silently becomes a quality ``PASS``.

Exit codes (also documented in README and ``docs/adr/0007-exit-codes.md``):

=====  ==========================================================
Code   Meaning
=====  ==========================================================
0      Gate passed (and no warnings, when ``--fail-on warn``)
1      A test assertion or release gate failed
2      Configuration / schema error (startup failure)
3      Infrastructure error (adapter raised, judge unavailable, ...)
4      No tests were selected or matched
5      Internal AgentCI error (a bug in AgentCI)
130    Interrupted (SIGINT)
=====  ==========================================================
"""

from __future__ import annotations

import enum
from typing import Any


class ExitCode(enum.IntEnum):
    """Stable, machine-readable process exit codes."""

    PASS = 0
    GATE_FAILED = 1
    CONFIG_ERROR = 2
    INFRA_ERROR = 3
    NO_TESTS = 4
    INTERNAL_ERROR = 5
    INTERRUPTED = 130


class AgentCIError(Exception):
    """Base class for every error AgentCI raises deliberately.

    Carries a human ``hint`` and a machine ``detail``. Both exist because a test
    failure needs to answer two different questions at once: *what went wrong*
    (message) and *what to do about it* (hint), while a caller parsing the report
    needs the structured facts without scraping prose (detail).

    ``detail`` is deliberately excluded from ``__str__``: it can contain tool
    arguments, which may include user data.
    """

    exit_code: ExitCode = ExitCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.detail: dict[str, Any] = detail or {}

    def __str__(self) -> str:
        return self.message


class ConfigError(AgentCIError):
    """``agentci.yaml`` is missing, unreadable, or semantically invalid."""

    exit_code = ExitCode.CONFIG_ERROR


class AdapterError(AgentCIError):
    """The configured adapter could not be imported or constructed."""

    exit_code = ExitCode.INFRA_ERROR


class DiscoveryError(AgentCIError):
    """Test files could not be imported or no ``@agent_test`` was found."""

    exit_code = ExitCode.INFRA_ERROR


class PolicyViolationError(AgentCIError):
    """Raised by the tool proxy when policy forbids a call. Recorded, not raised to users."""

    exit_code = ExitCode.GATE_FAILED


class StepLimitExceeded(AgentCIError):
    """The agent exceeded ``budgets.max_steps`` and was stopped."""

    exit_code = ExitCode.GATE_FAILED

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"agent exceeded the maximum of {limit} steps",
            hint="raise budgets.max_steps, or treat this as an agent loop",
        )
        self.limit = limit


class DeadlineExceeded(AgentCIError):
    """The agent exceeded the configured wall-clock budget."""

    exit_code = ExitCode.GATE_FAILED

    def __init__(self, budget_ms: float) -> None:
        super().__init__(
            f"agent exceeded the maximum wall-clock budget of {budget_ms:.0f}ms",
            hint="raise budgets.max_latency_ms, or treat this as a hang",
        )
        self.budget_ms = budget_ms


class SideEffectBlocked(AgentCIError):
    """A live external side effect was refused because policy denies it."""

    exit_code = ExitCode.GATE_FAILED


class ReplayError(AgentCIError):
    """A recorded run could not be located or replayed."""

    exit_code = ExitCode.INFRA_ERROR


__all__ = [
    "AdapterError",
    "AgentCIError",
    "ConfigError",
    "DeadlineExceeded",
    "DiscoveryError",
    "ExitCode",
    "PolicyViolationError",
    "ReplayError",
    "SideEffectBlocked",
    "StepLimitExceeded",
]
