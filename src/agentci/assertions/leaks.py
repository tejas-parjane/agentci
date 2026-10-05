"""Data-leakage assertions (PRD §FR-3 ``forbidden_data_patterns``, AC-11).

The scan is deliberately **wide**. A leak does not have to reach the final
answer to be a leak: credentials sitting in a tool argument, an intermediate
retrieval result, or trace metadata are all evidence the agent handled data it
should not have carried. Narrow scans produce false comfort.

What is *not* done here: removing the data. ``to_not_leak`` reads real values so
it can report precisely where the leak occurred; redaction happens later, at the
serialization boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agentci.assertions.base import Kind, line, make, skip
from agentci.core.result import AssertionResult
from agentci.core.trace import Trace


def _surfaces(trace: Trace, output_text: str) -> list[tuple[str, str]]:
    """Every place a secret could plausibly hide, labelled for the failure message."""
    surfaces: list[tuple[str, str]] = []
    if output_text:
        surfaces.append(("output_text", output_text))
    for event in trace.events:
        label = f"{event.type.value}"
        if event.tool is not None:
            label += f":{event.tool.name}"
            if event.tool.arguments:
                surfaces.append((f"{label} arguments", str(event.tool.arguments)))
        if event.result is not None:
            surfaces.append((f"{label} result", line(str(event.result), 8000)))
        if event.metadata:
            surfaces.append((f"{label} metadata", line(str(event.metadata), 4000)))
    return surfaces


def not_leak(
    trace: Trace,
    output_text: str,
    redactor: Any,
    patterns: Sequence[str],
) -> AssertionResult:
    """Assert none of ``patterns`` appear anywhere in the run.

    Each entry is either the name of a built-in pattern (``email``, ``ssn``,
    ``jwt``, ``api_key``, ``authorization``, ``credit_card``, ``phone``,
    ``private_key``, ``aws_access_key``) or a literal substring to look for.
    Literal matching makes it trivial to add a project-specific identifier::

        expect(r).to_not_leak("email", "ACCT-9931")
    """
    if not patterns:
        return skip(
            kind=Kind.LEAK.value,
            name="to_not_leak",
            description="no forbidden data",
            reason="no patterns were given",
        )

    known: frozenset[str] = getattr(redactor, "pattern_names", frozenset())
    findings: list[str] = []
    surfaces = _surfaces(trace, output_text)

    for surface_name, blob in surfaces:
        for pattern in patterns:
            if pattern in known:
                hits = [
                    hit
                    for hit in redactor.find_leaks(blob, extra_patterns=[pattern])
                    if hit == pattern
                ]
                if hits:
                    findings.append(f"{pattern} in {surface_name}")
            elif pattern and pattern in blob:
                findings.append(f"{pattern!r} in {surface_name}")

    ok = not findings
    unique = sorted(set(findings))
    return make(
        kind=Kind.LEAK.value,
        name="to_not_leak",
        description=f"run does not expose {list(patterns)}",
        ok=ok,
        expected=0,
        actual=unique[:8] if unique else None,
        message=(
            "leaked data detected: " + "; ".join(unique[:8]) if unique else ""
        ),
        hint=(
            "the value itself is intentionally omitted from this message; inspect the "
            "trace in the report, which is redacted"
        ),
    )


def no_secret_in_environment_derived_fields(
    trace: Trace, redactor: Any
) -> AssertionResult:
    """Assert no value sourced from a secret environment variable appears.

    The strongest leakage check available: it does not need to know what shape
    the secret has, only that AgentCI saw it in the environment and the run never
    emitted it.
    """
    secrets = list(getattr(redactor, "_secrets", ()) or ())
    if not secrets:
        return skip(
            kind=Kind.LEAK.value,
            name="to_not_leak_environment_secrets",
            description="no environment secret appears in the run",
            reason="no secret-looking environment variables were visible",
        )

    offenders = []
    for surface_name, blob in _surfaces(trace, ""):
        if any(secret in blob for secret in secrets):
            offenders.append(surface_name)

    return make(
        kind=Kind.LEAK.value,
        name="to_not_leak_environment_secrets",
        description="no environment secret appears in the run",
        ok=not offenders,
        expected=0,
        actual=offenders[:6] if offenders else None,
        message=(
            f"a value from the environment appeared in: {sorted(set(offenders))[:6]}"
            if offenders
            else ""
        ),
    )


def leak_scan(trace: Trace, output_text: str, redactor: Any) -> AssertionResult:
    """Always-on scan across every credential pattern, regardless of configuration.

    Complements :func:`not_leak`: this one needs no test author to ask, so a
    suite that never thinks about leakage still catches a JWT in a trace.
    """
    credential_patterns = sorted(
        getattr(redactor, "pattern_names", frozenset())
        & {"jwt", "api_key", "authorization", "private_key", "aws_access_key"}
    )
    if not credential_patterns:
        return skip(
            kind=Kind.LEAK.value,
            name="to_not_leak_credentials",
            description="no credential material appears in the run",
            reason="the redactor is disabled",
        )
    return not_leak(trace, output_text, redactor, credential_patterns)


__all__ = [
    "leak_scan",
    "no_secret_in_environment_derived_fields",
    "not_leak",
]
