"""The ``agentci`` command line interface.

Exit codes are the contract with CI (``docs/adr/0007-exit-codes.md``), and they
exist because §32 requires product failure to stay distinguishable from agent
failure:

    0  run passed; with ``--fail-on-warn``, warnings block too
    1  a test, gate, or regression check failed
    2  the config was missing or invalid
    3  infrastructure fault (adapter raised, discovery failed)
    4  no tests were selected
    5  an unexpected error inside AgentCI
    130 interrupted

Every failure printed here carries the hint from the raising error. A CLI that
reports *what* went wrong but not *what to do about it* pushes the reader into a
stack trace for the common case.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from agentci.__about__ import __version__
from agentci.core.config import (
    Config,
    dump_config,
    find_config,
    load_config,
    resolve_test_files,
)
from agentci.core.diff import trace_diff
from agentci.core.replay import (
    ReplaySession,
    extract_test_id,
    load_trace_file,
    write_trace_file,
)
from agentci.core.result import Status
from agentci.core.runner import RunOptions, TestRunner
from agentci.core.selection import SelectionPlan, build_plan
from agentci.errors import AgentCIError, ConfigError, ExitCode, ReplayError
from agentci.reporting.models import RunReport
from agentci.reporting.renderers import (
    render_annotations,
    render_json,
    render_step_summary,
)
from agentci.testing import AgentTestCase, discover

app = typer.Typer(
    name="agentci",
    help="CI and release gates for AI agents.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)
baseline_app = typer.Typer(help="Save and inspect run baselines.")
app.add_typer(baseline_app, name="baseline")

console = Console()
err_console = Console(stderr=True, style="bold red")


def _print_error(exc: BaseException, hint: str | None = None) -> None:
    err_console.print(f"{type(exc).__name__}: {exc}")
    text = hint if hint is not None else (exc.hint if isinstance(exc, AgentCIError) else None)
    if text:
        Console(stderr=True, style="yellow").print(f"  hint: {text}")


@contextmanager
def _handle_errors() -> Iterator[None]:
    """Turn exceptions into exit codes so no command has to repeat the mapping."""
    try:
        yield
    except KeyboardInterrupt:
        err_console.print("interrupted")
        raise typer.Exit(ExitCode.INTERRUPTED.value) from None
    except AgentCIError as exc:
        _print_error(exc)
        raise typer.Exit(int(exc.exit_code)) from None
    except typer.Exit:
        raise
    except Exception as exc:
        _print_error(exc)
        Console(stderr=True).print_exception(show_locals=False, max_frames=10)
        raise typer.Exit(ExitCode.INTERNAL_ERROR.value) from None


def _load(config_path: Path | None, root: Path | None) -> tuple[Config, Path]:
    """Resolve the config and the directory every relative path means."""
    base = root or Path.cwd()
    config = load_config(config_path, search_from=base) if config_path else load_config(search_from=base)
    return config, base


def _select(
    config: Config,
    root: Path,
    *,
    tags: list[str],
    test_ids: list[str],
    pattern: str | None,
    repeat: int | None,
    fail_on_warn: bool,
    no_traces: bool,
    selected_by: str,
    selection_reason: str,
    changed: bool = False,
    base: str | None = None,
) -> tuple[list[AgentTestCase], RunOptions, SelectionPlan | None]:
    # Resolved first so that an explicit --test-id / --name / --tag, which are more
    # specific, overwrites the diff as the stated selection reason.
    plan: SelectionPlan | None = build_plan(config, root, base=base) if changed else None
    if plan is not None and plan.enabled:
        selected_by = "diff"
        selection_reason = plan.base or plan.unresolved

    cases = discover(resolve_test_files(config, root), project_root=root)
    if test_ids:
        wanted = set(test_ids)
        cases = [c for c in cases if c.test_id in wanted]
        selected_by = "test_id"
        selection_reason = ",".join(sorted(wanted))
    if pattern:
        lowered = pattern.lower()
        cases = [c for c in cases if lowered in c.name.lower() or lowered in c.test_id.lower()]
        selected_by = "name"
        selection_reason = pattern
    if tags:
        selected_by = "tags"
        selection_reason = ",".join(tags)
    return cases, RunOptions(
        repeat=repeat,
        tags=tags,
        selected_by=selected_by,
        selection_reason=selection_reason,
        selection=plan,
        record_traces=False if no_traces else None,
        fail_on_warn=fail_on_warn,
        root=root,
    ), plan


def _write_github_outputs(report: RunReport) -> None:
    """Post the annotations and step summary the config asked for."""
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        return
    annotations = render_annotations(report)
    if annotations:
        sys.stdout.write(annotations)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with Path(summary_path).open("a", encoding="utf-8") as handle:
                handle.write(render_step_summary(report))
        except OSError as exc:
            err_console.print(f"warning: could not write step summary: {exc}")


def _render_terminal(report: RunReport, *, quiet: bool) -> None:
    summary = report.summary
    if not quiet:
        table = Table(title=None, show_header=True, header_style="bold")
        table.add_column("Status", style="bold")
        table.add_column("Test")
        table.add_column("Duration", justify="right")
        table.add_column("Assertions", justify="right")
        for test in report.tests:
            colour = {
                Status.PASS: "green",
                Status.WARN: "yellow",
                Status.FAIL: "red",
                Status.ERROR: "magenta",
                Status.SKIP: "dim",
            }[test.status]
            evaluated = [a for a in test.assertions if a.status.value != "skipped"]
            counts = (
                f"{sum(1 for a in evaluated if a.status.value == 'passed')}/{len(evaluated)}"
                if evaluated
                else "-"
            )
            table.add_row(
                f"[{colour}]{test.status.value.upper()}[/{colour}]",
                test.name,
                f"{test.duration_ms:.0f}ms",
                counts,
            )
        console.print(table)

    colour = {
        Status.PASS: "green",
        Status.WARN: "yellow",
        Status.FAIL: "red",
        Status.ERROR: "magenta",
        Status.SKIP: "dim",
    }[report.status]
    console.print(
        f"[{colour}]{report.status.value.upper()}[/{colour}] "
        f"{summary.passed}/{summary.total} passed, "
        f"{summary.failed} failed, {summary.errored} errored, "
        f"{summary.skipped} skipped"
    )
    # Coverage is worth stating even when it passes; a skipped-by-selection count
    # is informational, so it gets a line of its own instead of a warning colour.
    if report.run.selection_skipped:
        console.print(
            f"  change-aware selection skipped {report.run.selection_skipped} of "
            f"{summary.total} test(s) against "
            f"{report.run.selection_base or 'the base ref'}"
        )
    for warning in report.warnings:
        Console(style="yellow").print(f"  warning: {warning}")


def _exit_code(report: RunReport) -> int:
    if not report.tests:
        return ExitCode.NO_TESTS.value
    if report.status.is_failure:
        if report.status is Status.ERROR:
            return ExitCode.INFRA_ERROR.value
        return ExitCode.GATE_FAILED.value
    if report.status is Status.WARN:
        return ExitCode.GATE_FAILED.value
    return ExitCode.PASS.value


@app.command("run")
@app.command("test", hidden=True)
def run_command(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to agentci.yaml."),
    root: Path | None = typer.Option(None, "--root", help="Project root; all paths resolve under it."),
    tag: list[str] = typer.Option([], "--tag", "-k", help="Run only tests carrying this tag."),
    test_id: list[str] = typer.Option([], "--test-id", help="Run only these test ids."),
    pattern: str | None = typer.Option(None, "--name", help="Substring match on test name or id."),
    repeat: int | None = typer.Option(None, "--repeat", min=1, help="Repeat every test N times."),
    changed: bool = typer.Option(
        False,
        "--changed",
        help="Run only tests affected by the diff against the base ref.",
    ),
    base_ref: str | None = typer.Option(
        None,
        "--base",
        help="Ref to diff against with --changed. Defaults to AGENTCI_BASE, then origin/main.",
    ),
    fail_on_warn: bool = typer.Option(False, "--fail-on-warn", help="Treat warnings as failures."),
    no_traces: bool = typer.Option(False, "--no-traces", help="Skip writing run artifacts."),
    json_out: bool = typer.Option(False, "--json", help="Print the JSON report to stdout."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only print the summary line."),
    output: Path | None = typer.Option(None, "--output", help="Write the JSON report to this path."),
) -> None:
    """Run the agent test suite and exit with a CI-friendly code."""
    with _handle_errors():
        loaded, base = _load(config, root)
        cases, options, _ = _select(
            loaded,
            base,
            tags=tag,
            test_ids=test_id,
            pattern=pattern,
            repeat=repeat,
            fail_on_warn=fail_on_warn,
            no_traces=no_traces,
            selected_by="all",
            selection_reason="",
            changed=changed,
            base=base_ref,
        )
        if not cases:
            err_console.print("no tests selected")
            raise typer.Exit(ExitCode.NO_TESTS.value)

        report = TestRunner(loaded, options=options).run(cases)

        if json_out:
            console.print_json(render_json(report))
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(render_json(report), encoding="utf-8")

        if not json_out:
            _render_terminal(report, quiet=quiet)
        _write_github_outputs(report)
        raise typer.Exit(_exit_code(report))


# Trace artifact naming. A scenario name becomes a path, so it must be flat and
# safe; this is the same narrowing the codebase applies everywhere an identifier
# crosses the filesystem boundary.
_ARTIFACT_NAME = re.compile(r"[A-Za-z0-9._-]+")


def _safe_artifact_name(name: str) -> str:
    if not re.fullmatch(_ARTIFACT_NAME, name):
        raise ConfigError(
            f"invalid trace artifact name {name!r}",
            hint="use letters, digits, and '.', '_', '-' only",
        )
    return name


def _display_path(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path)


@app.command("record")
def record_command(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to agentci.yaml."),
    root: Path | None = typer.Option(None, "--root", help="Project root; all paths resolve under it."),
    name: str = typer.Option(..., "--name", help="Artifact name; one test -> <name>.jsonl."),
    tag: list[str] = typer.Option([], "--tag", "-k", help="Record only tests carrying this tag."),
    test_id: list[str] = typer.Option([], "--test-id", help="Record only these test ids."),
    match: str | None = typer.Option(None, "--match", help="Substring match on test name or id."),
    json_out: bool = typer.Option(False, "--json", help="Print the artifact index as JSON."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only print the recorded paths."),
) -> None:
    """Run a scenario once and persist a standalone, replayable v1 trace."""
    with _handle_errors():
        loaded, base = _load(config, root)
        safe = _safe_artifact_name(name)
        cases, options, _ = _select(
            loaded,
            base,
            tags=tag,
            test_ids=test_id,
            pattern=match,
            repeat=1,
            fail_on_warn=False,
            no_traces=False,
            selected_by="all",
            selection_reason="",
            changed=False,
            base=None,
        )
        if not cases:
            err_console.print("no tests selected")
            raise typer.Exit(ExitCode.NO_TESTS.value)
        options.record_traces = True

        runner = TestRunner(loaded, options=options)
        report = runner.run(cases)
        traces_root = base / loaded.storage.dir / "traces"

        recorded: list[dict[str, str | int]] = []
        for test in report.tests:
            if not test.iterations:
                continue
            events = runner.store.load_trace(test.iterations[0].run_id)
            if not events:
                continue
            if len(report.tests) == 1:
                target = traces_root / f"{safe}.jsonl"
            else:
                target = traces_root / safe / f"{_safe_artifact_name(test.test_id)}.jsonl"
            write_trace_file(target, events, redactor=runner.redactor)
            recorded.append(
                {
                    "test_id": test.test_id,
                    "path": _display_path(target, base),
                    "run_id": test.iterations[0].run_id,
                    "events": len(events),
                }
            )

        if not recorded:
            err_console.print("nothing was recorded")
            raise typer.Exit(ExitCode.NO_TESTS.value)

        if json_out:
            console.print_json(json.dumps({"schema_version": 1, "name": safe, "traces": recorded}))
        elif not quiet:
            for entry in recorded:
                console.print(
                    f"recorded {entry['test_id']} -> {entry['path']} ({entry['events']} events)"
                )
        raise typer.Exit(ExitCode.PASS.value)


@app.command("replay")
def replay_command(
    trace: Path = typer.Argument(..., help="Recorded v1 trace artifact to replay."),
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to agentci.yaml."),
    root: Path | None = typer.Option(None, "--root", help="Project root; all paths resolve under it."),
    test_id: str | None = typer.Option(
        None, "--test-id", help="Test to replay; default: the one the artifact recorded."
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Write the replayed trace artifact here. Default: <name>-replay.jsonl."
    ),
    json_out: bool = typer.Option(False, "--json", help="Print the JSON report to stdout."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only print the summary line."),
    output: Path | None = typer.Option(None, "--output", help="Write the JSON report to this path."),
) -> None:
    """Re-execute a recorded scenario against the current agent.

    Every tool call is answered from the recorded trace instead of running live,
    so behavior is the only thing that can change. A call the recording never
    answered is refused and marked divergent, which fails the run.
    """
    with _handle_errors():
        loaded, base = _load(config, root)
        events = load_trace_file(trace)
        if not events:
            raise ReplayError(f"{trace} contains no events", hint="re-record the scenario")
        wanted = test_id or extract_test_id(events)
        if not wanted:
            raise ReplayError(
                f"{trace} does not record which test it came from",
                hint="pass --test-id ... to replay it",
            )
        cases, options, _ = _select(
            loaded,
            base,
            tags=[],
            test_ids=[wanted],
            pattern=None,
            repeat=1,
            fail_on_warn=False,
            no_traces=False,
            selected_by="replay",
            selection_reason=str(trace),
            changed=False,
            base=None,
        )
        if not cases:
            err_console.print(f"no test named {wanted!r}")
            raise typer.Exit(ExitCode.NO_TESTS.value)
        options.replay = ReplaySession(events, source=trace.name)
        options.record_traces = True

        runner = TestRunner(loaded, options=options)
        report = runner.run(cases)

        replay_run_id = (
            report.tests[0].iterations[0].run_id
            if report.tests and report.tests[0].iterations
            else None
        )
        artifact = out or base / loaded.storage.dir / "traces" / f"{trace.stem}-replay.jsonl"
        new_events = runner.store.load_trace(replay_run_id) if replay_run_id else []
        write_trace_file(artifact, new_events, redactor=runner.redactor)

        if json_out:
            console.print_json(render_json(report))
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(render_json(report), encoding="utf-8")

        if not json_out:
            _render_terminal(report, quiet=quiet)
            console.print(
                f"replay wrote {len(new_events)} events -> {_display_path(artifact, base)}"
            )
        raise typer.Exit(_exit_code(report))


@app.command("diff")
def diff_command(
    base_trace: Path = typer.Argument(..., help="Earlier trace artifact."),
    head_trace: Path = typer.Argument(..., help="Later trace artifact."),
    json_out: bool = typer.Option(False, "--json", help="Print the machine-readable diff."),
) -> None:
    """Compare two trace artifacts and print what behavior changed.

    The diff is over the behavioral contract of the spec — event kind, order,
    tool identity, arguments, results, and status — never over ids, timing, or
    cost. Exit code is 0 when behavior is unchanged, 1 when it changed.
    """
    with _handle_errors():
        base_events = load_trace_file(base_trace)
        head_events = load_trace_file(head_trace)
        result = trace_diff(
            base_events, head_events, a_label=str(base_trace), b_label=str(head_trace)
        )
        if json_out:
            console.print_json(json.dumps(result.to_dict()))
        else:
            console.print(result.render(), markup=False)
        if result.behavior_changed:
            raise typer.Exit(ExitCode.GATE_FAILED.value)
        raise typer.Exit(ExitCode.PASS.value)


def _gate_reasons(
    report: RunReport, plan: SelectionPlan | None, *, allow_warn: bool
) -> list[str]:
    """Why this change may not ship, in the order a maintainer should read them.

    The selection reason comes first because it is the only one that survives
    ``--allow-warn``: a release it cannot scope to a diff is unverifiable, not
    merely noisy, so relaxing warnings must never relax it.
    """
    reasons: list[str] = []
    if plan is not None and plan.unresolved:
        reasons.append(f"change-aware selection unavailable: {plan.unresolved}")
    if not allow_warn:
        reasons.extend(
            warning
            for warning in report.warnings
            # The runner records the same fact as a warning, and _render_terminal
            # already printed it; listing it twice would read as two problems.
            if not warning.startswith("change-aware selection unavailable:")
        )

    if report.status is Status.ERROR:
        reasons.append(
            f"the run errored: {report.summary.errored} test(s) hit an "
            "infrastructure fault"
        )
    elif report.status is Status.FAIL:
        detail: list[str] = []
        if report.summary.failed:
            detail.append(f"{report.summary.failed} failing test(s)")
        if report.summary.warned:
            detail.append(f"{report.summary.warned} test(s) with warnings")
        if report.summary.assertions_failed:
            detail.append(f"{report.summary.assertions_failed} failing assertion(s)")
        failed_gates = [g.name for g in report.gates if g.status is Status.FAIL]
        if failed_gates:
            detail.append("failing gate(s): " + ", ".join(failed_gates))
        if report.regression.status is Status.FAIL:
            detail.append("regressed against the stored baseline")
        reasons.append("the run failed: " + "; ".join(detail))
    elif report.status is Status.SKIP:
        reasons.append("no tests ran")
    return reasons


def _render_gate(report: RunReport, reasons: list[str], *, quiet: bool) -> None:
    _render_terminal(report, quiet=quiet)
    if reasons:
        Console(style="red").print("RELEASE GATE: BLOCKED")
        for reason in reasons:
            Console(style="red").print(f"  - {reason}")
    else:
        Console(style="green").print("RELEASE GATE: PASS")


@app.command("gate")
def gate_command(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to agentci.yaml."),
    root: Path | None = typer.Option(None, "--root", help="Project root; all paths resolve under it."),
    base_ref: str | None = typer.Option(
        None,
        "--base",
        help="Ref to diff against. Defaults to AGENTCI_BASE, then origin/main.",
    ),
    allow_warn: bool = typer.Option(
        False, "--allow-warn", help="Report warnings without blocking the release."
    ),
    no_traces: bool = typer.Option(False, "--no-traces", help="Skip writing run artifacts."),
    json_out: bool = typer.Option(False, "--json", help="Print the JSON report to stdout."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only print the summary line."),
    output: Path | None = typer.Option(None, "--output", help="Write the JSON report to this path."),
) -> None:
    """Decide whether this change is safe to release.

    Three things separate this from `run`: change-aware selection is always on, so
    the verdict covers the tests this diff can affect; warnings block by default
    (pass --allow-warn to relax that); and a base ref that cannot be resolved blocks
    on its own, because a release the harness cannot scope to a diff is unverifiable.
    `run` stays developer-friendly and runs everything in that case — `gate` does not.
    """
    with _handle_errors():
        loaded, base = _load(config, root)
        cases, options, plan = _select(
            loaded,
            base,
            tags=[],
            test_ids=[],
            pattern=None,
            repeat=None,
            fail_on_warn=not allow_warn,
            no_traces=no_traces,
            selected_by="all",
            selection_reason="",
            changed=True,
            base=base_ref,
        )
        if not cases:
            err_console.print("no tests selected")
            raise typer.Exit(ExitCode.NO_TESTS.value)

        report = TestRunner(loaded, options=options).run(cases)
        reasons = _gate_reasons(report, plan, allow_warn=allow_warn)

        if json_out:
            console.print_json(render_json(report))
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(render_json(report), encoding="utf-8")

        if not json_out:
            _render_gate(report, reasons, quiet=quiet)

        if report.status is Status.ERROR:
            raise typer.Exit(ExitCode.INFRA_ERROR.value)
        if reasons:
            raise typer.Exit(ExitCode.GATE_FAILED.value)
        raise typer.Exit(ExitCode.PASS.value)


@app.command("list")
def list_command(
    config: Path | None = typer.Option(None, "--config", "-c"),
    root: Path | None = typer.Option(None, "--root"),
    tag: list[str] = typer.Option([], "--tag", "-k"),
    show_id: bool = typer.Option(False, "--ids", help="Print test ids instead of names."),
) -> None:
    """Show the tests AgentCI would run, without executing them."""
    with _handle_errors():
        loaded, base = _load(config, root)
        cases = discover(resolve_test_files(loaded, base), project_root=base)
        selected = [c for c in cases if not tag or set(tag).issubset(set(c.tags))]
        if not selected:
            err_console.print("no tests matched")
            raise typer.Exit(ExitCode.NO_TESTS.value)
        for case in selected:
            tags = f"  [{', '.join(case.tags)}]" if case.tags else ""
            label = case.test_id if show_id else case.name
            console.print(f"{label}  {case.file}:{case.line}{tags}")


_INIT_CONFIG = """\
# AgentCI configuration — https://github.com/tejas-parjane/agentci
version: 1

project:
  name: my-agent

agent:
  # module.path:attribute of the agent under test. A class with a `run` method,
  # an AgentAdapter, or a plain `(user_input) -> AgentResult` function.
  # This matches my_agent/agent.py written by `agentci init`.
  adapter: "my_agent.agent:run"
  # Used for cost estimation when the adapter does not report token usage.
  # model: gpt-4o-mini

tests:
  - dir: tests/agentci
    pattern: "test_*.py"

budgets:
  max_latency_ms: 30000
  max_steps: 20

execution:
  # Side effects on real systems are denied unless mocked or allowed explicitly.
  external_side_effects: deny

report:
  json: true
  markdown: true

storage:
  dir: .agentci
"""

_INIT_TEST = '''\
"""Example AgentCI test.

Run it with `agentci run`. Assertions are recorded even when they pass, so the
report shows what was actually verified rather than only what broke.
"""

from agentci.assertions import expect
from agentci.testing import agent_test


@agent_test(tags=["smoke"])
def test_agent_echoes_the_input(agent):
    result = agent.run("hello")

    expect(result).to_contain("hello")
    expect(result).to_have_max_steps(5)
'''

_INIT_ADAPTER = '''\
"""Example adapter: how AgentCI sees your agent.

Replace the body of `run` with a call to your real agent.

Two signatures are accepted:

    run(user_input: str) -> AgentResult
    run(user_input: str, ctx: RunContext) -> AgentResult

Take `ctx` when you need `ctx.tools` (the policy-enforcing tool proxy) or
`ctx.recorder` (for tracing steps and model calls). Keep real side effects out of
this path: they are denied by default.
"""

from __future__ import annotations

from agentci.core.result import AgentResult


def run(user_input: str) -> AgentResult:
    """Answer the input."""
    return AgentResult(output_text=f"you said: {user_input}")
'''


@app.command("init")
def init_command(
    root: Path = typer.Option(Path("."), "--root", "--path", help="Directory to scaffold."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
) -> None:
    """Create agentci.yaml plus a runnable starter test and adapter."""
    with _handle_errors():
        directory = root.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        targets = {
            directory / "agentci.yaml": _INIT_CONFIG,
            directory / "tests" / "agentci" / "test_example.py": _INIT_TEST,
            directory / "my_agent" / "agent.py": _INIT_ADAPTER,
        }
        for target, content in targets.items():
            if target.exists() and not force:
                console.print(f"skip  {target} (exists, use --force)")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
            console.print(f"write {target}")
        console.print(
            "\nNext: point `agent.adapter` at your agent, then run [bold]agentci run[/bold]."
        )


@app.command("config")
def config_command(
    config: Path | None = typer.Option(None, "--config", "-c"),
    root: Path | None = typer.Option(None, "--root"),
    path: Path | None = typer.Option(None, "--dump", help="Write the resolved config here."),
) -> None:
    """Validate agentci.yaml and print the effective configuration."""
    with _handle_errors():
        loaded, _ = _load(config, root)
        text = dump_config(loaded)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            console.print(f"wrote {path}")
        else:
            console.print(text, markup=False)


@app.command("version")
def version_command() -> None:
    """Print the AgentCI version."""
    console.print(f"agentci {__version__}")


@baseline_app.command("save")
def baseline_save(
    config: Path | None = typer.Option(None, "--config", "-c"),
    root: Path | None = typer.Option(None, "--root"),
    from_report: Path | None = typer.Option(
        None, "--from", help="Use an existing report.json instead of running tests."
    ),
) -> None:
    """Run the suite and store the result as the comparison baseline."""
    with _handle_errors():
        loaded, base = _load(config, root)
        if from_report is not None:
            report = RunReport.from_dict(json.loads(from_report.read_text(encoding="utf-8")))
        else:
            cases = discover(resolve_test_files(loaded, base), project_root=base)
            if not cases:
                err_console.print("no tests selected")
                raise typer.Exit(ExitCode.NO_TESTS.value)
            report = TestRunner(loaded, options=RunOptions(root=base)).run(cases)

        baseline_path = base / loaded.regression.baseline
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": report.schema_version,
            "agentci_version": __version__,
            "run": report.run.model_dump(mode="json"),
            "tests": {
                test.test_id: {
                    "status": test.status.value,
                    "flakiness": test.flakiness.value,
                    "cost_usd": test.cost_usd,
                    "duration_ms": test.duration_ms,
                    "score": test.score(),
                }
                for test in report.tests
            },
        }
        baseline_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        console.print(f"baseline written to {baseline_path} ({len(report.tests)} tests)")


@baseline_app.command("show")
def baseline_show(
    root: Path | None = typer.Option(None, "--root"),
) -> None:
    """Show what a saved baseline contains."""
    with _handle_errors():
        base = root or Path.cwd()
        config_file = find_config(base)
        if config_file is None:
            raise ConfigError(
                "no agentci.yaml found",
                hint="run `agentci init` to create one, or pass --root",
            )
        loaded = load_config(config_file)
        baseline_path = base / loaded.regression.baseline
        if not baseline_path.is_file():
            raise ConfigError(
                f"no baseline at {baseline_path}",
                hint="run `agentci baseline save` first",
            )
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        console.print(f"run:   {payload.get('run', {}).get('id', '?')}")
        console.print(f"tests: {len(payload.get('tests', {}))}")
        for test_id, entry in payload.get("tests", {}).items():
            console.print(f"  {entry.get('status', '?'):6} {test_id}")


def main() -> None:
    """Console-script entry point (``agentci``)."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
