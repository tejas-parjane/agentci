"""Output-text assertions (PRD §FR-3, "Output assertions").

Covers the deterministic subset: containment, exact equality, regex, and JSON
structure. Notably absent is any semantic judgement about whether an answer is
*good* — that belongs to an optional LLM judge (PRD §FR-4), never to the core
runner.
"""

from __future__ import annotations

import json as jsonlib
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from agentci.assertions.base import Kind, error, excerpt, line, make, skip
from agentci.core.result import AssertionResult


def contains(text: str, needle: str, *, case_sensitive: bool = True) -> AssertionResult:
    haystack = text if case_sensitive else text.lower()
    probe = needle if case_sensitive else needle.lower()
    ok = probe in haystack
    return make(
        kind=Kind.OUTPUT.value,
        name="to_contain",
        description=f"output contains {excerpt(needle, 80)!r}",
        ok=ok,
        expected=needle,
        actual=line(text, 160) if not ok else None,
        message=(
            f"expected output to contain {excerpt(needle, 120)!r}\n"
            f"  actual: {line(text)}" if not ok else ""
        ),
    )


def not_contains(text: str, needle: str, *, case_sensitive: bool = True) -> AssertionResult:
    haystack = text if case_sensitive else text.lower()
    probe = needle if case_sensitive else needle.lower()
    ok = probe not in haystack
    return make(
        kind=Kind.OUTPUT.value,
        name="to_not_contain",
        description=f"output does not contain {excerpt(needle, 80)!r}",
        ok=ok,
        expected=f"not {excerpt(needle, 80)!r}",
        actual=line(text, 160) if not ok else None,
        message=(
            f"output must not contain {excerpt(needle, 120)!r}\n"
            f"  found in: {line(text)}" if not ok else ""
        ),
    )


def exact_match(text: str, expected: str) -> AssertionResult:
    ok = text.strip() == expected.strip()
    return make(
        kind=Kind.OUTPUT.value,
        name="to_equal",
        description="output matches exactly",
        ok=ok,
        expected=expected,
        actual=text if not ok else None,
        message=(
            f"output does not match exactly\n  expected: {line(expected)}\n"
            f"  actual:   {line(text)}" if not ok else ""
        ),
    )


def matches_regex(text: str, pattern: str) -> AssertionResult:
    try:
        compiled = re.compile(pattern, re.DOTALL)
    except re.error as exc:
        return error(
            kind=Kind.OUTPUT.value,
            name="to_match",
            description=f"output matches /{pattern}/",
            reason=f"invalid regex: {exc}",
        )
    ok = compiled.search(text) is not None
    return make(
        kind=Kind.OUTPUT.value,
        name="to_match",
        description=f"output matches /{pattern}/",
        ok=ok,
        expected=pattern,
        actual=line(text, 160) if not ok else None,
        message=(f"output does not match /{pattern}/\n  actual: {line(text)}" if not ok else ""),
    )


def parse_json(text: str) -> tuple[Any | None, str | None]:
    """Parse ``text`` as JSON, returning ``(value, error_message)``."""
    try:
        return jsonlib.loads(text), None
    except jsonlib.JSONDecodeError as exc:
        return None, f"{exc.msg} at line {exc.lineno} column {exc.colno}"


def is_json(text: str) -> AssertionResult:
    _value, problem = parse_json(text)
    ok = problem is None
    return make(
        kind=Kind.OUTPUT.value,
        name="to_be_json",
        description="output is valid JSON",
        ok=ok,
        expected="valid JSON",
        actual=problem,
        message=f"output is not valid JSON: {problem}\n  text: {line(text)}" if not ok else "",
    )


def has_fields(text: str, fields: list[str], *, required_only: bool = False) -> AssertionResult:
    """Assert a JSON object contains ``fields``.

    ``required_only=True`` asserts exactly these and no others, which is how you
    pin a machine-readable contract rather than just spot-checking keys.
    """
    value, problem = parse_json(text)
    if problem is not None:
        return skip(
            kind=Kind.OUTPUT.value,
            name="to_have_fields",
            description=f"output JSON has fields {fields}",
            reason=f"output is not JSON ({problem})",
            hint="use to_be_json first, or return JSON from the agent",
        )
    if not isinstance(value, dict):
        return error(
            kind=Kind.OUTPUT.value,
            name="to_have_fields",
            description=f"output JSON has fields {fields}",
            reason=f"expected a JSON object, got {type(value).__name__}",
        )

    keys = set(value)
    missing = [f for f in fields if f not in keys]
    extra = sorted(keys - set(fields)) if required_only else []

    ok = not missing and not extra
    parts = []
    if missing:
        parts.append(f"missing: {missing}")
    if extra:
        parts.append(f"unexpected: {extra}")
    return make(
        kind=Kind.OUTPUT.value,
        name="to_have_fields",
        description=(
            f"output JSON has {'exactly ' if required_only else ''}fields {fields}"
        ),
        ok=ok,
        expected=fields,
        actual=sorted(keys),
        message="output JSON field mismatch; " + "; ".join(parts) if parts else "",
    )


def match_json_schema(text: str, schema: dict[str, Any]) -> AssertionResult:
    """Validate output JSON against a JSON Schema."""
    value, problem = parse_json(text)
    if problem is not None:
        return skip(
            kind=Kind.OUTPUT.value,
            name="to_match_json_schema",
            description="output matches JSON schema",
            reason=f"output is not JSON ({problem})",
        )
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return error(
            kind=Kind.OUTPUT.value,
            name="to_match_json_schema",
            description="output matches JSON schema",
            reason=f"invalid schema: {exc.message}",
        )

    violations = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda e: list(e.absolute_path),
    )
    ok = not violations
    detail = "; ".join(
        f"{'/'.join(str(p) for p in v.absolute_path) or '<root>'}: {v.message}"
        for v in violations[:5]
    )
    return make(
        kind=Kind.OUTPUT.value,
        name="to_match_json_schema",
        description="output matches JSON schema",
        ok=ok,
        expected=excerpt(schema, 120),
        actual=value if not ok else None,
        message=f"output violates schema: {detail}" if not ok else "",
        hint="check the agent's response shape, or relax the schema",
    )


def output_field(text: str, dotted: str) -> Any | None:
    """Read ``a.b.c`` out of a JSON output, or ``None``."""
    value, problem = parse_json(text)
    if problem is not None or value is None:
        return None
    current: Any = value
    for part in dotted.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


__all__ = [
    "contains",
    "exact_match",
    "has_fields",
    "is_json",
    "match_json_schema",
    "matches_regex",
    "not_contains",
    "output_field",
    "parse_json",
]
