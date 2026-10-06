"""Policy enforcement evaluated over a completed trace (PRD §FR-3, §22).

Two enforcement layers, described in :mod:`agentci.core.tools`:

1. **Pre-execution.** :class:`~agentci.core.tools.ToolRegistry` refuses denied
   tools, unapproved tools, and unmocked side effects *before* they run.
2. **Post-execution.** This module re-derives every rule from the trace.

The second layer is not redundant. Adapters that call functions directly, catch
broad exceptions, or run outside ``ctx.tools`` bypass layer one entirely. Layer
two still catches them, so the guarantee does not depend on adapter discipline —
which matters because the promise is "no production side effect occurs unless
explicitly enabled" (AC-10), not "well-behaved adapters are safe".

Policy violations are **hard failures**, never warnings. A run with one violation
scores ``policy_compliance: 0.0`` and cannot be averaged away by a good task score
(PRD §18).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from agentci.core.config import Config, ExecutionConfig, Policies
from agentci.core.redaction import Redactor
from agentci.core.result import PolicyViolation, RunMetrics, Status
from agentci.core.tools import MockEntry, SideEffectPolicy, ToolRegistry
from agentci.core.trace import EventStatus, EventType, Trace


@dataclass
class PolicyContext:
    """Everything the engine needs, pre-resolved from config."""

    policies: Policies
    execution: ExecutionConfig
    redactor: Redactor
    metrics: RunMetrics
    allowed_tools: set[str] = field(default_factory=set)
    denied_tools: set[str] = field(default_factory=set)
    approval_required: set[str] = field(default_factory=set)
    side_effecting_tools: set[str] = field(default_factory=set)
    side_effect_policy: SideEffectPolicy = SideEffectPolicy.DENY
    max_steps: int | None = None

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        redactor: Redactor,
        metrics: RunMetrics,
        granted_approvals: set[str] | None = None,
    ) -> PolicyContext:
        del granted_approvals  # approvals are read from the trace, not carried here
        return cls(
            policies=config.policies,
            execution=config.execution,
            redactor=redactor,
            metrics=metrics,
            allowed_tools=set(config.policies.allowed_tools),
            denied_tools=set(config.policies.denied_tools),
            approval_required=set(config.policies.approval_required),
            side_effecting_tools=set(config.execution.side_effecting_tools),
            side_effect_policy=SideEffectPolicy(config.execution.external_side_effects),
            max_steps=config.budgets.max_steps,
        )


class PolicyEngine:
    """Evaluates a trace against project policy."""

    __slots__ = ("context",)

    def __init__(self, context: PolicyContext) -> None:
        self.context = context

    @classmethod
    def from_config(cls, config: Config, *, redactor: Redactor, metrics: RunMetrics) -> PolicyEngine:
        return cls(PolicyContext.from_config(config, redactor=redactor, metrics=metrics))

    def evaluate(
        self, trace: Trace, *, scope: frozenset[str] | None = None
    ) -> list[PolicyViolation]:
        """Return every violation, most severe first.

        ``scope`` is the set of event ids attributable to the agent under test.

        This distinction exists because a policy test must be *able* to trigger a
        violation on purpose: a suite that asserts "the deny gate closes" has to
        call the denied tool, and the runner must not then fail that test for the
        denial it just asserted. So violations from in-scope events are ``FAIL``
        and block; violations from out-of-scope events -- the test body poking at
        ``ctx.tools`` directly -- are still reported, but demoted to ``WARN`` so
        they stay visible in the report without deciding the verdict.

        Pass ``scope=None`` to treat every event as agent behaviour.
        """
        if scope is None:
            return self._evaluate(trace)

        in_scope = _subtrace(trace, lambda e: e.event_id in scope)
        out_scope = _subtrace(trace, lambda e: e.event_id not in scope)

        violations = self._evaluate(in_scope)
        for violation in self._evaluate(out_scope):
            # Mutable by design: the model is not frozen, and demoting in place
            # keeps the original message, tool and event_id intact for the report.
            violation.severity = Status.WARN
            violations.append(violation)
        return _dedupe(violations)

    def _evaluate(self, trace: Trace) -> list[PolicyViolation]:
        violations: list[PolicyViolation] = []
        granted, denied = trace.approvals()
        invoked = trace.tool_calls()
        names = [call.name for call in invoked]

        violations.extend(self._check_denied(trace, names))
        violations.extend(self._check_allowlist(trace, names))
        violations.extend(self._check_approvals(invoked, set(granted), set(denied)))
        violations.extend(self._check_side_effects(trace, names))
        violations.extend(self._check_steps(trace))
        violations.extend(self._check_data(trace))
        violations.extend(self._check_errors(trace))

        return _dedupe(violations)

    # -- individual rules -----------------------------------------------------

    def _check_denied(self, trace: Trace, names: list[str]) -> list[PolicyViolation]:
        out: list[PolicyViolation] = []
        for name in dict.fromkeys(names):
            if name in self.context.denied_tools:
                event = next(
                    (e for e in trace.tool_events(name) if e.type is EventType.TOOL_CALL_STARTED),
                    None,
                )
                out.append(
                    PolicyViolation(
                        kind="denied_tool",
                        severity=Status.FAIL,
                        message=f"tool {name!r} is listed in policies.denied_tools but was called",
                        tool=name,
                        event_id=event.event_id if event else None,
                        detail={"arguments": _safe_args(event)},
                    )
                )
        return out

    def _check_allowlist(self, trace: Trace, names: list[str]) -> list[PolicyViolation]:
        allowed = self.context.allowed_tools
        if not allowed:
            return []
        out: list[PolicyViolation] = []
        for name in dict.fromkeys(names):
            if name in allowed or name in self.context.denied_tools:
                continue
            event = next(
                (e for e in trace.tool_events(name) if e.type is EventType.TOOL_CALL_STARTED),
                None,
            )
            out.append(
                PolicyViolation(
                    kind="unlisted_tool",
                    severity=Status.FAIL,
                    message=(
                        f"tool {name!r} is not in policies.allowed_tools "
                        f"(allowlist: {sorted(allowed)})"
                    ),
                    tool=name,
                    event_id=event.event_id if event else None,
                    detail={"arguments": _safe_args(event)},
                )
            )
        return out

    def _check_approvals(
        self,
        invoked: list[Any],
        granted: set[str],
        denied: set[str],
    ) -> list[PolicyViolation]:
        required = self.context.approval_required
        if not required:
            return []
        out: list[PolicyViolation] = []
        for call in invoked:
            if call.name not in required:
                continue
            if call.name in granted:
                continue
            out.append(
                PolicyViolation(
                    kind="approval_denied" if call.name in denied else "approval_required",
                    severity=Status.FAIL,
                    message=(
                        f"tool {call.name!r} requires approval and none was granted"
                        if call.name not in denied
                        else f"tool {call.name!r} had its approval denied but was still called"
                    ),
                    tool=call.name,
                    detail={"arguments": call.arguments},
                )
            )
        return out

    def _check_side_effects(self, trace: Trace, names: list[str]) -> list[PolicyViolation]:
        """Catch a live side effect even when the adapter bypassed ``ctx.tools``.

        Detected by cross-referencing declared side-effecting tools against the
        completion status of their calls: ``success`` means it really ran.
        """
        if self.context.side_effect_policy is SideEffectPolicy.ALLOW:
            return []
        out: list[PolicyViolation] = []
        for name in dict.fromkeys(names):
            if name not in self.context.side_effecting_tools:
                continue
            for event in trace.tool_events(name):
                if event.type is not EventType.TOOL_CALL_COMPLETED:
                    continue
                if event.status in (EventStatus.MOCKED, EventStatus.DENIED):
                    continue
                out.append(
                    PolicyViolation(
                        kind="side_effect_blocked",
                        severity=Status.FAIL,
                        message=(
                            f"side-effecting tool {name!r} executed live while "
                            f"execution.external_side_effects is 'deny'"
                        ),
                        tool=name,
                        event_id=event.event_id,
                        detail={"arguments": _safe_args(event), "status": event.status.value},
                    )
                )
        return out

    def _check_steps(self, trace: Trace) -> list[PolicyViolation]:
        limit = self.context.max_steps
        if limit is None:
            return []
        actual = trace.step_count()
        if actual <= limit:
            return []
        return [
            PolicyViolation(
                kind="max_steps",
                severity=Status.FAIL,
                message=f"agent took {actual} steps, budget is {limit}",
                detail={"actual": actual, "limit": limit},
            )
        ]

    def _check_data(self, trace: Trace) -> list[PolicyViolation]:
        patterns = self.context.policies.forbidden_data_patterns
        if not patterns:
            return []
        known = self.context.redactor.pattern_names
        out: list[PolicyViolation] = []
        for event in trace.events:
            blobs: list[tuple[str, Any]] = []
            if event.tool is not None and event.tool.arguments:
                blobs.append(("arguments", event.tool.arguments))
            if event.result is not None:
                blobs.append(("result", event.result))
            for label, blob in blobs:
                text = str(blob)
                for pattern in patterns:
                    hit = (
                        pattern in known
                        and pattern
                        in self.context.redactor.find_leaks(text, extra_patterns=[pattern])
                    ) or (pattern not in known and pattern in text)
                    if hit:
                        out.append(
                            PolicyViolation(
                                kind="forbidden_data",
                                severity=Status.FAIL,
                                message=(
                                    f"forbidden data pattern {pattern!r} present in "
                                    f"{event.type.value} {label}"
                                ),
                                tool=event.tool.name if event.tool else None,
                                event_id=event.event_id,
                                detail={"pattern": pattern, "surface": label},
                            )
                        )
        return out

    def _check_errors(self, trace: Trace) -> list[PolicyViolation]:
        """Surface denied/blocked calls, which indicate a caught policy breach."""
        out: list[PolicyViolation] = []
        for event in trace.events:
            if event.type is not EventType.TOOL_CALL_COMPLETED:
                continue
            if event.status is EventStatus.DENIED and event.tool is not None:
                reason = _reason(event.result)
                out.append(
                    PolicyViolation(
                        kind="side_effect_blocked"
                        if reason == "side_effect_denied"
                        else "denied_tool",
                        severity=Status.FAIL,
                        message=(
                            f"tool {event.tool.name!r} was attempted but blocked "
                            f"({reason or 'policy'})"
                        ),
                        tool=event.tool.name,
                        event_id=event.event_id,
                        detail={"reason": reason},
                    )
                )
        return out

    # -- tool registry --------------------------------------------------------

    def build_registry(
        self,
        declarations: Mapping[str, Any] | None = None,
        *,
        recorder: Any = None,
        granted_approvals: set[str] | None = None,
        extra_mocks: Mapping[str, MockEntry] | None = None,
    ) -> ToolRegistry:
        """Construct the runtime :class:`ToolRegistry` implied by policy.

        Config-declared mocks are merged with any replay-supplied mocks; replay
        mocks win, because they reflect what actually happened.
        """
        mocks: dict[str, MockEntry] = {}
        for name, spec in self.context.execution.mocks.items():
            mocks[name] = MockEntry(
                response=spec.response,
                match_arguments=spec.match_arguments,
                source="agentci.yaml",
            )
        mocks.update(extra_mocks or {})

        return ToolRegistry(
            declarations=declarations or {},
            recorder=recorder,
            mocks=mocks,
            denied_tools=self.context.denied_tools,
            allowed_tools=self.context.allowed_tools or None,
            approval_required=self.context.approval_required,
            granted_approvals=granted_approvals or set(),
            side_effect_policy=self.context.side_effect_policy,
            side_effecting_tools=self.context.side_effecting_tools,
        )


def compliance(violations: list[PolicyViolation]) -> float:
    """``policy_compliance`` dimension: 1.0 when clean, 0.0 when any violation.

    Deliberately binary and unforgiving. A "mostly compliant" policy score is an
    invitation to gate at ``>= 0.95``, which tolerates the exact failure the
    policy exists to prevent.
    """
    return 0.0 if violations else 1.0


def _safe_args(event: Any) -> dict[str, Any]:
    if event is None or event.tool is None:
        return {}
    return dict(event.tool.arguments)


def _reason(result: Any) -> str:
    if isinstance(result, Mapping):
        return str(result.get("reason", ""))
    return ""


def _subtrace(trace: Trace, predicate: Callable[[Any], bool]) -> Trace:
    """A :class:`Trace` holding the events matching ``predicate``."""
    return Trace(run_id=trace.run_id, events=[e for e in trace.events if predicate(e)])


def _dedupe(violations: list[PolicyViolation]) -> list[PolicyViolation]:
    seen: set[tuple[str, str | None, str]] = set()
    out: list[PolicyViolation] = []
    for violation in violations:
        key = (violation.kind, violation.tool, violation.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(violation)
    return out


__all__ = ["PolicyContext", "PolicyEngine", "compliance"]
