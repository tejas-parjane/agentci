"""Render a :class:`~agentci.reporting.models.RunReport` to the formats CI systems read.

Four targets, one source: JSON for machines, Markdown for humans, GitHub workflow
commands for inline PR annotations, and a compact block for ``$GITHUB_STEP_SUMMARY``.
Because they all read the same report, they cannot disagree about a verdict.

Two rules are enforced here rather than in the model:

* **Redaction happens on the way out.** The in-memory report keeps real values so
  gating decisions are exact (ADR-008), and ``redact_report`` is applied by every
  renderer before it serializes. Artifacts are what gets uploaded to CI.
* **An undetermined verdict is never rendered as a pass.** ``SKIP``/``WARN`` keep
  their own wording; nothing is coerced to success because it did not fail.
"""

from __future__ import annotations

import json
from typing import Any

from agentci.core.redaction import Redactor
from agentci.core.result import AssertionOutcome, Status
from agentci.reporting.models import RunReport, TestReport

__all__ = [
    "redact_report",
    "render_annotations",
    "render_json",
    "render_markdown",
    "render_step_summary",
]

_STATUS_GLYPH: dict[Status, str] = {
    Status.PASS: "PASS",
    Status.WARN: "WARN",
    Status.FAIL: "FAIL",
    Status.ERROR: "ERROR",
    Status.SKIP: "SKIP",
}


def redact_report(report: RunReport, redactor: Redactor | None = None) -> RunReport:
    """Return a copy of ``report`` with every string value scrubbed.

    Round-tripping through ``model_dump`` / ``model_validate`` costs one
    traversal but guarantees the renderer cannot forget a field: text lives in
    messages, excerpts, trace events, and assertion values, and scattering
    ``redact_text`` calls across the renderer is how one of them eventually gets
    missed.
    """
    if redactor is None or not redactor.enabled:
        return report
    data: dict[str, Any] = redactor.redact(report.model_dump(mode="json"))
    return RunReport.model_validate(data)


def render_json(report: RunReport, *, redactor: Redactor | None = None, indent: int = 2) -> str:
    """The machine-readable artifact, stable enough to pin a schema against."""
    return json.dumps(
        redact_report(report, redactor).to_dict(),
        indent=indent,
        ensure_ascii=False,
        default=str,
    )


def _cell(value: Any) -> str:
    """Make a value safe to drop inside a Markdown table cell."""
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def _duration(ms: float) -> str:
    if ms >= 1000:
        return f"{ms / 1000:.2f}s"
    return f"{ms:.0f}ms"


def _money(cost: float | None) -> str:
    return "-" if cost is None else f"${cost:.4f}"


def _tests_table(report: RunReport) -> list[str]:
    lines = [
        "| Status | Test | Duration | Cost | Assertions |",
        "| --- | --- | --- | --- | --- |",
    ]
    for test in report.tests:
        evaluated = [a for a in test.assertions if a.status is not AssertionOutcome.SKIPPED]
        if evaluated:
            passed = sum(1 for a in evaluated if a.status is AssertionOutcome.PASSED)
            counts = f"{passed}/{len(evaluated)}"
        else:
            counts = "-"
        name = f"{test.name}"
        if test.file:
            name = f"{name} ({test.file}:{test.line})"
        lines.append(
            f"| {_STATUS_GLYPH[test.status]} | {_cell(name)} | {_duration(test.duration_ms)} "
            f"| {_money(test.cost_usd)} | {counts} |"
        )
    return lines


def _gates_table(report: RunReport) -> list[str]:
    lines = ["| Gate | Status | Actual | Threshold |", "| --- | --- | --- | --- |"]
    for gate in report.gates:
        lines.append(
            f"| {_cell(gate.name)} | {_STATUS_GLYPH[gate.status]} | {_cell(gate.actual)} "
            f"| {_cell(gate.threshold)} |"
        )
    return lines


def _trace_block(test: TestReport) -> list[str]:
    excerpt = test.trace
    if excerpt is None or not excerpt.events:
        return []

    lines = [
        "",
        "<details><summary>Trace</summary>",
        "",
        "| # | Event | Status | Duration | Detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    for index, event in enumerate(excerpt.events, start=1):
        detail_parts = []
        if isinstance(event, dict):
            tool = event.get("tool")
            if isinstance(tool, dict) and tool.get("name"):
                detail_parts.append(str(tool["name"]))
            if event.get("error"):
                detail_parts.append(str(event["error"]))
            elif event.get("result") is not None:
                detail_parts.append(str(event["result"])[:80])
        event_type = event.get("type", "?") if isinstance(event, dict) else str(event)
        status = event.get("status", "?") if isinstance(event, dict) else "?"
        duration = event.get("duration_ms")
        elapsed = f"{duration:.0f}ms" if duration is not None else "-"
        lines.append(
            f"| {index} | {_cell(event_type)} | {_cell(status)} | {_cell(elapsed)} "
            f"| {_cell('; '.join(detail_parts))} |"
        )
    if excerpt.truncated:
        lines += ["", "_Trace truncated in the middle; head and tail preserved._"]
    lines += ["", "</details>"]
    return lines


def _failures_section(report: RunReport, *, include_traces: bool) -> list[str]:
    failures = report.failures
    if not failures:
        return ["", "## Failures", "", "None."]
    lines = ["", "## Failures", ""]
    for test in failures:
        lines.append(f"### {_STATUS_GLYPH[test.status]} — {_cell(test.name)}")
        lines.append("")
        lines.append(f"`{test.file}:{test.line}`" if test.file else f"`{test.test_id}`")
        if test.error:
            lines += ["", "```text", str(test.error), "```"]
        if test.flakiness.value != "none":
            lines += ["", f"- flakiness: `{test.flakiness.value}`"]
        if test.error_category:
            lines.append(f"- category: `{test.error_category}`")

        if test.failures:
            lines += ["", "| Failed assertion | Expected | Actual |", "| --- | --- | --- |"]
            for assertion in test.failures:
                lines.append(
                    f"| {_cell(assertion.name)} | {_cell(assertion.expected)} "
                    f"| {_cell(assertion.actual)} |"
                )

        if test.policy_violations:
            lines += ["", "Policy violations:", ""]
            for violation in test.policy_violations:
                lines.append(
                    f"- `{violation.severity.value}` {violation.kind}: "
                    f"{_cell(violation.message)}"
                )

        if test.output_excerpt:
            lines += ["", "```text", str(test.output_excerpt).rstrip(), "```"]

        if include_traces:
            lines += _trace_block(test)
        lines.append("")

    return lines


def _policy_section(report: RunReport) -> list[str]:
    violations = report.policy_violations
    if not violations:
        return ["", "## Policy", "", "No violations."]
    blocking = [v for v in violations if v.severity is Status.FAIL]
    lines = [
        "",
        "## Policy",
        "",
        f"{len(violations)} violation(s), {len(blocking)} blocking.",
        "",
        "| Severity | Kind | Message |",
        "| --- | --- | --- |",
    ]
    for violation in violations:
        lines.append(
            f"| {_STATUS_GLYPH[violation.severity]} | {_cell(violation.kind)} "
            f"| {_cell(violation.message)} |"
        )
    return lines


def _regression_section(report: RunReport) -> list[str]:
    regression = report.regression
    lines = ["", "## Regression", ""]
    if not regression.compared:
        lines.append(f"Skipped: {_cell(regression.message)}.")
        return lines

    lines.append(f"Compared against baseline `{regression.baseline_run_id}`.")
    if regression.new_failures:
        lines += ["", "New failures:", ""]
        lines += [f"- {_cell(name)}" for name in regression.new_failures]
    if regression.fixed:
        lines += ["", "Fixed:", ""]
        lines += [f"- {_cell(name)}" for name in regression.fixed]
    if regression.missing_from_run:
        lines += ["", "Missing from this run:", ""]
        lines += [f"- {_cell(name)}" for name in regression.missing_from_run]
    if regression.message:
        lines += ["", _cell(regression.message)]
    return lines


def render_markdown(
    report: RunReport,
    *,
    redactor: Redactor | None = None,
    include_traces: bool = True,
) -> str:
    """A human-readable report for job output, artifacts, and PR comments."""
    report = redact_report(report, redactor)
    summary = report.summary

    status = _STATUS_GLYPH[report.status]
    lines = [
        "# AgentCI run report",
        "",
        f"**Status:** `{status}` · **Tests:** {summary.passed}/{summary.total} passed",
        "",
        "> Generated by AgentCI — verdicts are authoritative; formatting is not.",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Tests | {summary.total} |",
        f"| Passed | {summary.passed} |",
        f"| Failed | {summary.failed} |",
        f"| Errored | {summary.errored} |",
        f"| Skipped | {summary.skipped} |",
        f"| Flaky | {summary.flaky} |",
        f"| Pass rate | {summary.pass_rate:.1%} |",
        f"| Duration | {_duration(summary.duration_ms)} |",
        f"| Cost | {_money(summary.cost_usd)} |",
        f"| Tokens | {summary.total_tokens if summary.total_tokens is not None else '-'} |",
        f"| Assertions | {summary.assertions_passed} passed, "
        f"{summary.assertions_failed} failed, {summary.assertions_skipped} skipped |",
        f"| Policy violations | {summary.policy_violations} |",
    ]

    lines += ["", "## Tests", "", *_tests_table(report)]

    if report.gates:
        lines += ["", "## Gates", "", *_gates_table(report)]

    lines += _failures_section(report, include_traces=include_traces)
    lines += _policy_section(report)
    lines += _regression_section(report)

    if report.dimensions.as_dict():
        lines += ["", "## Dimensions", "", "| Dimension | Score |", "| --- | --- |"]
        for name, value in report.dimensions.as_dict().items():
            lines.append(f"| {name} | {value:.3f} |")

    lines += ["", "## Environment", ""]
    environment = report.environment
    lines += [
        f"- agentci {environment.agentci_version}, Python {environment.python_version}",
        f"- agent `{environment.agent}`"
        + (f" on `{environment.agent_model}`" if environment.agent_model else ""),
    ]
    if environment.git_commit:
        dirty = " (dirty)" if environment.git_dirty else ""
        lines.append(f"- commit `{environment.git_commit}` on `{environment.git_branch}`{dirty}")
    if environment.ci:
        lines.append(f"- CI: {environment.ci_provider or 'unknown'}")

    if report.warnings:
        lines += ["", "## Warnings", ""]
        lines += [f"- {_cell(warning)}" for warning in report.warnings]

    lines += ["", f"Run `{report.run.id}` started {report.run.started_at}."]
    return "\n".join(lines) + "\n"


def render_step_summary(
    report: RunReport, *, redactor: Redactor | None = None
) -> str:
    """The compact block posted to ``$GITHUB_STEP_SUMMARY``.

    Deliberately omits trace excerpts: a step summary is a glance, not a
    debugging surface, and pasting 40 events into every job's summary page makes
    the summary useless.
    """
    report = redact_report(report, redactor)
    summary = report.summary
    status = _STATUS_GLYPH[report.status]

    lines = [
        "### AgentCI",
        "",
        f"**`{status}`** — {summary.passed}/{summary.total} tests passed"
        + (f", {summary.failed} failed" if summary.failed else "")
        + (f", {summary.errored} errored" if summary.errored else ""),
        "",
    ]
    if report.failures:
        lines += ["| Failed test | Category |", "| --- | --- |"]
        for test in report.failures:
            lines.append(f"| {_cell(test.name)} | {_cell(test.error_category or '-')} |")
    if report.blocking_gates:
        lines += ["", "Blocking gates: " + ", ".join(g.name for g in report.blocking_gates)]
    if report.regression.compared and report.regression.new_failures:
        lines += ["", f"{len(report.regression.new_failures)} new failure(s) vs baseline."]
    return "\n".join(lines) + "\n"


def _escape_annotation(text: str) -> str:
    """Escape a value for a GitHub workflow command (``%``, CR, LF)."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def render_annotations(report: RunReport, *, redactor: Redactor | None = None) -> str:
    """GitHub Actions workflow commands, so failures annotate the diff itself.

    Only ``FAIL`` and ``ERROR`` become annotations. ``WARN`` is deliberately left
    out: an inline annotation is a call to action, and a deliberate probe that a
    test set up on purpose is not one. ``SKIP`` is not a verdict at all.
    """
    report = redact_report(report, redactor)
    lines: list[str] = []
    for test in report.tests:
        if not test.status.is_failure:
            continue
        message = str(test.error or f"{test.name} {test.status.value}")
        location = f"file={test.file}" if test.file else ""
        if test.file and test.line:
            location += f",line={test.line}"
        title = _escape_annotation(test.name)
        joined = ", ".join(part for part in (location, f"title={title}") if part)
        lines.append(f"::error {joined}::{_escape_annotation(message)}")

    for warning in report.warnings:
        lines.append(f"::warning::{_escape_annotation(str(warning))}")

    if report.status.is_failure and not report.failures:
        lines.append(
            f"::error title=AgentCI {_escape_annotation(status_text(report))}::"
            f"{_escape_annotation('; '.join(g.name for g in report.blocking_gates) or 'run failed')}"
        )
    return "\n".join(lines) + ("\n" if lines else "")


def status_text(report: RunReport) -> str:
    """Human-facing word for the run's verdict, used in titles and headers."""
    return _STATUS_GLYPH[report.status]
