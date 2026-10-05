"""Tool-call assertions (PRD §FR-3, "Tool assertions").

This is AgentCI's differentiator: a correct final answer reached by calling
``issue_refund`` before ``get_order`` is a *failed* test, not a pass (PRD §42).
Every assertion here reads the normalized trace, never the adapter's internals.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from agentci.assertions.base import (
    Kind,
    error,
    excerpt,
    format_order_diff,
    line,
    make,
)
from agentci.core.result import AssertionResult
from agentci.core.trace import EventStatus, ToolCall


def _names(calls: Sequence[ToolCall]) -> list[str]:
    return [c.name for c in calls]


def used_tool(
    calls: Sequence[ToolCall], name: str, *, times: int | None = None
) -> AssertionResult:
    count = sum(1 for c in calls if c.name == name)
    if times is None:
        ok = count >= 1
        expected: Any = f">= 1 call to {name}"
        detail = f"{name} was called {count} time(s)" if not ok else ""
    else:
        ok = count == times
        expected = f"exactly {times} call(s) to {name}"
        detail = f"called {count} time(s), expected {times}"
    return make(
        kind=Kind.TOOL.value,
        name="to_use_tool",
        description=f"agent calls {name}",
        ok=ok,
        expected=expected,
        actual=count,
        message=(
            f"{detail}\n  tool sequence: {' -> '.join(_names(calls)) or '<none>'}"
            if not ok
            else ""
        ),
        hint=f"did the agent take the expected path? actual: {_names(calls)}",
    )


def not_used_tool(calls: Sequence[ToolCall], name: str) -> AssertionResult:
    count = sum(1 for c in calls if c.name == name)
    ok = count == 0
    return make(
        kind=Kind.TOOL.value,
        name="to_not_use_tool",
        description=f"agent does not call {name}",
        ok=ok,
        expected="no calls",
        actual=count if not ok else None,
        message=(
            f"agent called forbidden tool {name} {count} time(s)\n"
            f"  tool sequence: {' -> '.join(_names(calls))}"
            if not ok
            else ""
        ),
        hint="remove the tool from the agent's tool list, or from its routing logic",
    )


def tool_call_count(
    calls: Sequence[ToolCall],
    *,
    equals: int | None = None,
    at_least: int | None = None,
    at_most: int | None = None,
) -> AssertionResult:
    count = len(calls)
    failures: list[str] = []
    if equals is not None and count != equals:
        failures.append(f"expected exactly {equals}, got {count}")
    if at_least is not None and count < at_least:
        failures.append(f"expected at least {at_least}, got {count}")
    if at_most is not None and count > at_most:
        failures.append(f"expected at most {at_most}, got {count}")
    if equals is None and at_least is None and at_most is None:
        raise ValueError("tool_call_count requires one of equals, at_least, or at_most")

    return make(
        kind=Kind.TOOL.value,
        name="to_call_tool",
        description=f"agent makes {count} tool call(s)",
        ok=not failures,
        expected=(
            f"equals={equals}" if equals is not None
            else f"between {at_least} and {at_most}" if at_least is not None and at_most is not None
            else f">= {at_least}" if at_least is not None
            else f"<= {at_most}"
        ),
        actual=count,
        message=(
            "; ".join(failures) + f"\n  tool sequence: {' -> '.join(_names(calls))}"
            if failures
            else ""
        ),
    )


def tool_order(
    calls: Sequence[ToolCall], expected: Sequence[str], *, exact: bool = False
) -> AssertionResult:
    """Assert tool order.

    ``exact=False`` (default) requires ``expected`` to appear as an ordered
    *subsequence*, tolerating extra diagnostic lookups around it. ``exact=True``
    requires the sequences to be identical, which is what you want when pinning a
    contract that forbids unnecessary calls.
    """
    actual = _names(calls)
    wanted = list(expected)

    if not wanted:
        raise ValueError("to_follow_tool_order requires a non-empty sequence")

    if exact:
        ok = actual == wanted
    else:
        cursor = 0
        for name in actual:
            if cursor < len(wanted) and name == wanted[cursor]:
                cursor += 1
        ok = cursor == len(wanted)

    if ok:
        return make(
            kind=Kind.TOOL.value,
            name="to_follow_tool_order",
            description=f"tool order {'exactly ' if exact else ''}{wanted}",
            ok=True,
            expected=wanted,
        )
    return make(
        kind=Kind.TOOL.value,
        name="to_follow_tool_order",
        description=f"tool order {'exactly ' if exact else ''}{wanted}",
        ok=False,
        expected=wanted,
        actual=actual,
        message=format_order_diff(wanted, actual),
        hint=(
            "a prompt or routing change likely altered the execution path; "
            "check whether the agent still does the safe thing first"
        ),
    )


def max_tool_calls(calls: Sequence[ToolCall], limit: int) -> AssertionResult:
    count = len(calls)
    ok = count <= limit
    return make(
        kind=Kind.TOOL.value,
        name="to_have_max_tool_calls",
        description=f"agent makes at most {limit} tool calls",
        ok=ok,
        expected=f"<= {limit}",
        actual=count,
        message=(
            f"agent made {count} tool calls, budget is {limit}\n"
            f"  tool sequence: {' -> '.join(_names(calls))}"
            if not ok
            else ""
        ),
    )


def tool_arguments_match(
    calls: Sequence[ToolCall],
    name: str,
    expected: Mapping[str, Any],
    *,
    strict: bool = False,
    every_call: bool = True,
) -> AssertionResult:
    """Assert on the arguments passed to a tool.

    By default ``expected`` is a **subset** match: ``{"order_id": "123"}`` passes
    even if the agent also passed a ``reason`` field. Pass ``strict=True`` to
    require exact argument equality.
    """
    matching = [c for c in calls if c.name == name]
    if not matching:
        return make(
            kind=Kind.TOOL.value,
            name="to_call_tool_with",
            description=f"{name} called with {dict(expected)!r}",
            ok=False,
            expected=dict(expected),
            actual=None,
            message=f"tool {name} was never called",
        )

    subjects = matching if every_call else matching[:1]
    mismatches: list[str] = []
    for call in subjects:
        actual_args = call.arguments
        if strict:
            if dict(actual_args) != dict(expected):
                mismatches.append(
                    f"{excerpt(actual_args, 120)} != {excerpt(dict(expected), 120)}"
                )
        else:
            for key, want in expected.items():
                got = actual_args.get(key, KeyError)
                if got is KeyError or got != want:
                    mismatches.append(
                        f"{key}={excerpt(got, 60)!r} (expected {excerpt(want, 60)!r})"
                    )

    ok = not mismatches
    return make(
        kind=Kind.TOOL.value,
        name="to_call_tool_with",
        description=f"{name} called with {dict(expected)!r}",
        ok=ok,
        expected=dict(expected),
        actual=[c.arguments for c in subjects],
        message=(
            f"{len(mismatches)} argument mismatch(es) on {name}: "
            + "; ".join(mismatches[:4])
            + (f"\n  argument fingerprint: {_fingerprint(subjects[0])}" if subjects else "")
            if not ok
            else ""
        ),
    )


def tool_arguments_schema(
    calls: Sequence[ToolCall], name: str, schema: dict[str, Any]
) -> AssertionResult:
    """Assert a tool's arguments validate against a JSON Schema (draft 2020-12)."""
    matching = [c for c in calls if c.name == name]
    if not matching:
        return make(
            kind=Kind.TOOL.value,
            name="to_call_tool_matching_schema",
            description=f"{name} arguments match schema",
            ok=False,
            expected=excerpt(schema, 100),
            actual=None,
            message=f"tool {name} was never called, so its arguments cannot be validated",
        )
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return error(
            kind=Kind.TOOL.value,
            name="to_call_tool_matching_schema",
            description=f"{name} arguments match schema",
            reason=f"invalid schema: {exc.message}",
        )

    problems: list[str] = []
    for call in matching:
        for violation in Draft202012Validator(schema).iter_errors(call.arguments):
            path = "/".join(str(p) for p in violation.absolute_path) or "<root>"
            problems.append(f"{path}: {violation.message}")

    ok = not problems
    return make(
        kind=Kind.TOOL.value,
        name="to_call_tool_matching_schema",
        description=f"{name} arguments match schema",
        ok=ok,
        expected=excerpt(schema, 100),
        actual=[c.arguments for c in matching],
        message=(
            f"{name} received invalid arguments: " + "; ".join(problems[:4])
            if problems
            else ""
        ),
        hint="check the agent's tool schema and its argument construction",
    )


def tool_succeeded(trace: Any, name: str) -> AssertionResult:
    """Assert every call to ``name`` completed successfully.

    A tool that returned an error may still have produced a plausible-looking
    final answer, which is exactly the failure mode this catches.
    """
    completed = [
        e
        for e in trace.events
        if e.tool is not None and e.tool.name == name and e.type.value == "tool_call_completed"
    ]
    if not completed:
        return make(
            kind=Kind.TOOL.value,
            name="to_succeed",
            description=f"{name} completes successfully",
            ok=False,
            expected="all calls succeed",
            actual=None,
            message=f"no completed call to {name} was recorded",
        )
    bad = [e for e in completed if e.status in (EventStatus.ERROR, EventStatus.DENIED)]
    ok = not bad
    return make(
        kind=Kind.TOOL.value,
        name="to_succeed",
        description=f"{name} completes successfully",
        ok=ok,
        expected="all calls succeed",
        actual=[e.status.value for e in bad] if bad else None,
        message=(
            f"{len(bad)}/{len(completed)} call(s) to {name} failed: "
            + "; ".join(line(e.error or e.status.value, 100) for e in bad[:3])
            if bad
            else ""
        ),
    )


def no_tool_retries(
    calls: Sequence[ToolCall], name: str, *, at_most: int = 0
) -> AssertionResult:
    """Assert ``name`` was not retried more than ``at_most`` times.

    Retries are detected as repeated calls carrying the same argument
    fingerprint, which catches both an agent-level retry loop and a tool-level one.
    """
    matching = [c for c in calls if c.name == name]
    fingerprints = [_fingerprint(c) for c in matching]
    repeats = sum(count - 1 for count in _counts(fingerprints).values() if count > 1)
    ok = repeats <= at_most
    return make(
        kind=Kind.TOOL.value,
        name="to_not_retry",
        description=f"{name} is retried at most {at_most} time(s)",
        ok=ok,
        expected=f"<= {at_most} retries",
        actual=repeats,
        message=(
            f"{name} was called with identical arguments {repeats} time(s) beyond the "
            f"first; distinct argument fingerprints: {fingerprints}"
            if not ok
            else ""
        ),
    )


def _counts(items: Sequence[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return out


def _fingerprint(call: ToolCall) -> str:
    """Stable identity for a call, used for retry and loop detection.

    ``sort_keys`` makes ``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` the same
    fingerprint, which is what an observer of behaviour would conclude.
    """
    try:
        return f"{call.name}({json.dumps(call.arguments, sort_keys=True, default=str)})"
    except (TypeError, ValueError):  # pragma: no cover - exotic values
        return f"{call.name}({sorted(map(str, call.arguments.items()))})"


__all__ = [
    "max_tool_calls",
    "no_tool_retries",
    "not_used_tool",
    "tool_arguments_match",
    "tool_arguments_schema",
    "tool_call_count",
    "tool_order",
    "tool_succeeded",
    "used_tool",
]
