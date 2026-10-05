"""Policy assertions available from inside a test body (PRD §FR-3).

The policy *engine* in :mod:`agentci.policy.engine` enforces project-wide rules
and produces violations that fail a test regardless of what it asserts. These
assertions are the complementary, per-test view: they let a test state an
expectation about a specific tool so the intent is legible in the test file rather
than buried in config.

Typical use: assert an approval was actually recorded, or that the run produced
no engine-detected violations, so a failure points at the test rather than at
project policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from agentci.assertions.base import Kind, excerpt, line, make, skip
from agentci.core.result import AssertionResult
from agentci.core.trace import EventType, ToolCall, Trace

if TYPE_CHECKING:
    from agentci.core.result import PolicyViolation


def only_uses_tools(calls: Sequence[ToolCall], allowed: Sequence[str]) -> AssertionResult:
    """Assert every tool called appears in ``allowed``."""
    allowed_set = set(allowed)
    used = [c.name for c in calls]
    offending = [name for name in used if name not in allowed_set]
    ok = not offending
    return make(
        kind=Kind.POLICY.value,
        name="to_only_use_tools",
        description="agent stays within the allowed tool set",
        ok=ok,
        expected=sorted(allowed_set),
        actual=offending if offending else None,
        message=(
            f"agent used unapproved tool(s): {sorted(set(offending))}\n"
            f"  allowed: {sorted(allowed_set)}\n  actual: {used}"
            if offending
            else ""
        ),
        hint="either the agent is wrong or the allowlist is out of date; decide which",
    )


def requires_approval(trace: Trace, tool: str) -> AssertionResult:
    """Assert the run recorded an explicit approval for ``tool``."""
    requested = [e for e in trace.events if e.type is EventType.APPROVAL_REQUESTED and e.tool
                 and e.tool.name == tool]
    granted = [e for e in trace.events if e.type is EventType.APPROVAL_GRANTED and e.tool
               and e.tool.name == tool]
    denied = [e for e in trace.events if e.type is EventType.APPROVAL_DENIED and e.tool
              and e.tool.name == tool]

    if denied and not granted:
        return make(
            kind=Kind.POLICY.value,
            name="to_require_approval_for",
            description=f"{tool} was explicitly approved",
            ok=False,
            expected="approved",
            actual="denied",
            message=f"approval for {tool} was denied; the agent proceeded anyway",
        )
    if not requested and not granted:
        return skip(
            kind=Kind.POLICY.value,
            name="to_require_approval_for",
            description=f"{tool} was explicitly approved",
            reason=f"no approval event for {tool} was recorded",
            hint=(
                "emit ctx.recorder.approval_requested(...) and approval_granted(...) "
                "around the call"
            ),
        )
    ok = bool(granted)
    return make(
        kind=Kind.POLICY.value,
        name="to_require_approval_for",
        description=f"{tool} was explicitly approved",
        ok=ok,
        expected="granted",
        actual="granted" if ok else None,
        message=f"no approval_granted event for {tool}" if not ok else "",
    )


def approval_was_demanded(trace: Trace, tool: str) -> AssertionResult:
    """Assert an approval was *requested* before ``tool`` ran."""
    requested = [
        e for e in trace.events if e.type is EventType.APPROVAL_REQUESTED
        and e.tool and e.tool.name == tool
    ]
    ok = bool(requested)
    return make(
        kind=Kind.POLICY.value,
        name="to_demand_approval_for",
        description=f"{tool} is gated behind an approval request",
        ok=ok,
        expected=">= 1 approval_requested",
        actual=len(requested),
        message=(
            f"{tool} ran without requesting approval, so a human-in-the-loop control "
            f"was bypassed" if not ok else ""
        ),
    )


def no_policy_violations(violations: Sequence[PolicyViolation]) -> AssertionResult:
    """Assert the policy engine found nothing."""
    ok = not violations
    return make(
        kind=Kind.POLICY.value,
        name="to_have_no_policy_violations",
        description="no policy violations",
        ok=ok,
        expected=0,
        actual=len(violations) if violations else None,
        message=(
            "\n".join(f"  [{v.kind}] {v.message}" for v in violations[:6])
            if violations
            else ""
        ),
        hint="inspect the trace excerpt in the report to see the offending call",
    )


def no_external_side_effects(trace: Trace) -> AssertionResult:
    """Assert every tool call actually executed against a real target.

    ``MOCKED`` and ``DENIED`` statuses are fine; anything that ran live is not.
    This is a testable expression of AC-10.
    """
    live = [
        e
        for e in trace.events
        if e.type is EventType.TOOL_CALL_COMPLETED
        and e.tool is not None
        and e.status.value == "success"
    ]
    ok = not live
    return make(
        kind=Kind.POLICY.value,
        name="to_have_no_live_side_effects",
        description="no tool executed a live side effect",
        ok=ok,
        expected=0,
        actual=[e.tool.name for e in live if e.tool] if live else None,
        message=(
            f"{len(live)} tool(s) executed live: "
            + ", ".join(e.tool.name for e in live[:6] if e.tool)
            if live
            else ""
        ),
        hint="mock the tool or assert against a sandboxed target",
    )


def forbidden_data_absent(trace: Trace, redactor: Any, patterns: Sequence[str]) -> AssertionResult:
    """Assert no forbidden data pattern appears anywhere in the run.

    Scans output text, tool arguments, and tool results. Named patterns resolve
    through the redaction library; raw strings are treated as literal substrings,
    which is what you want for a domain-specific identifier.
    """
    blobs: list[tuple[str, str]] = [("output", "")]
    for event in trace.events:
        if event.tool is not None:
            blobs.append((f"tool_call {event.tool.name}", str(event.tool.arguments)))
            blobs.append((f"tool_result {event.tool.name}", line(str(event.result), 4000)))
        if event.metadata:
            blobs.append((f"metadata {event.type.value}", line(str(event.metadata), 2000)))

    findings: list[str] = []
    for name, blob in blobs:
        if not blob:
            continue
        for pattern in patterns:
            if pattern in redactor.pattern_names:
                if redactor.contains_secret(blob, extra_patterns=[pattern]):
                    findings.append(f"{pattern} in {name}")
            elif pattern in blob:
                findings.append(f"{pattern!r} in {name}")

    ok = not findings
    return make(
        kind=Kind.POLICY.value,
        name="to_not_contain_forbidden_data",
        description=f"run contains no forbidden data ({excerpt(list(patterns), 80)})",
        ok=ok,
        expected=0,
        actual=findings[:6] if findings else None,
        message=(
            f"forbidden data found: {'; '.join(findings[:6])}" if findings else ""
        ),
        hint="the agent carried data it had no reason to carry",
    )


__all__ = [
    "approval_was_demanded",
    "forbidden_data_absent",
    "no_external_side_effects",
    "no_policy_violations",
    "only_uses_tools",
    "requires_approval",
]
