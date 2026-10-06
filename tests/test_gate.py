"""Tests for `agentci gate`, the release verdict.

Two properties carry the weight here. A base ref the harness cannot resolve must
block *even with* ``--allow-warn``, because a release that cannot be scoped to a
diff is unverifiable rather than merely noisy -- if relaxing warnings also
relaxed this, ``--allow-warn`` would quietly turn the safety model off. And every
blocked path has to name its reason, because the point of a single command is
that a maintainer learns *why* deployment stopped, not merely that it did.
"""

from __future__ import annotations

import json
import os
import subprocess
from itertools import count
from pathlib import Path

from typer.testing import CliRunner

from agentci.cli import _gate_reasons, app
from agentci.core.result import Status
from agentci.core.selection import SelectionPlan
from agentci.reporting.models import GateResult, RunReport

runner = CliRunner()
_PROJECTS = count(1)

PASSING_TEST = """\
from agentci.testing import agent_test


@agent_test(dependencies=["prompts/*.txt"])
def test_ok(agent):
    assert agent is not None
"""

FAILING_TEST = """\
from agentci.testing import agent_test


@agent_test(dependencies=["prompts/*.txt"])
def test_fails(agent):
    assert False, "boom"
"""

GITIGNORE = "__pycache__/\n.agentci/\n"


def _project(
    tmp_path: Path,
    *,
    unmatched: str = "run",
    body: str = PASSING_TEST,
) -> Path:
    """A committed one-test project that gate can diff against.

    Module names are unique per project: discovery imports these files, and a
    repeated ``test_probe`` would be served from ``sys.modules`` by whichever
    project defined it first -- including the deliberately failing one.
    """
    index = next(_PROJECTS)
    adapter = f"probe_agent_{index}"
    test_file = f"test_probe_{index}.py"
    root = tmp_path / "proj"
    (root / "prompts").mkdir(parents=True)
    (root / "prompts" / "refund.txt").write_text("refund v1\n", encoding="utf-8")
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (root / f"{adapter}.py").write_text(
        "from agentci.core.result import AgentResult\n"
        "\n"
        "\n"
        "def run(user_input: str) -> AgentResult:\n"
        "    return AgentResult(output_text=user_input)\n",
        encoding="utf-8",
    )
    (root / test_file).write_text(body, encoding="utf-8")
    (root / "agentci.yaml").write_text(
        "version: 1\n"
        "project:\n"
        "  name: gate-probe\n"
        "agent:\n"
        f'  adapter: "{adapter}:run"\n'
        "selection:\n"
        "  enabled: true\n"
        f"  unmatched: {unmatched}\n"
        "tests:\n"
        f"  - file: {test_file}\n",
        encoding="utf-8",
    )

    _git(root, "init", "-q")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(root, "config", "user.email", "agentci@example.com")
    _git(root, "config", "user.name", "AgentCI")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")
    return root


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return result.stdout.strip()


def _gate(root: Path, *args: str, base: str = "HEAD") -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["gate", "--root", str(root), "--base", base, *args],
        catch_exceptions=False,
    )
    assert result.stdout is not None
    return result.exit_code, result.stdout


def _run(root: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["run", "--root", str(root), *args],
        catch_exceptions=False,
    )
    assert result.stdout is not None
    return result.exit_code, result.stdout


# -- the verdict, as a pure function -------------------------------------------


def _report(status: Status = Status.PASS) -> RunReport:
    report = RunReport()
    report.run.status = status
    return report


def test_a_clean_run_has_no_reason() -> None:
    assert _gate_reasons(_report(), None, allow_warn=False) == []


def test_an_unresolved_selection_is_reported() -> None:
    plan = SelectionPlan(enabled=True, unresolved="no base ref could be determined")

    reasons = _gate_reasons(_report(), plan, allow_warn=False)

    assert reasons == [
        "change-aware selection unavailable: no base ref could be determined"
    ]


def test_allow_warn_does_not_lift_an_unresolved_selection() -> None:
    plan = SelectionPlan(enabled=True, unresolved="no base ref could be determined")

    assert _gate_reasons(_report(), plan, allow_warn=True) == [
        "change-aware selection unavailable: no base ref could be determined"
    ]


def test_warnings_block_unless_allowed() -> None:
    report = _report()
    report.warnings = ["could not record run artifacts"]

    assert _gate_reasons(report, None, allow_warn=False) == [
        "could not record run artifacts"
    ]
    assert _gate_reasons(report, None, allow_warn=True) == []


def test_selection_skips_alone_do_not_block() -> None:
    """The case the release gate must not punish: skips by change analysis."""
    plan = SelectionPlan(enabled=True, base="origin/main", unmatched="skip")
    report = _report()
    report.run.selection_skipped = 3

    assert _gate_reasons(report, plan, allow_warn=False) == []


def test_selection_skips_do_not_mask_a_real_warning() -> None:
    """Skips being informational must not hide a run-level warning either."""
    plan = SelectionPlan(enabled=True, base="origin/main", unmatched="skip")
    report = _report()
    report.run.selection_skipped = 3
    report.warnings = ["could not record run artifacts"]

    assert _gate_reasons(report, plan, allow_warn=False) == [
        "could not record run artifacts"
    ]
    assert _gate_reasons(report, plan, allow_warn=True) == []


def test_the_selection_warning_is_not_listed_twice() -> None:
    """_render_terminal already prints it, and the plan reason outranks it."""
    report = _report()
    report.warnings = ["change-aware selection unavailable: no base ref"]
    plan = SelectionPlan(enabled=True, unresolved="no base ref")

    assert _gate_reasons(report, plan, allow_warn=False) == [
        "change-aware selection unavailable: no base ref"
    ]


def test_a_failing_gate_is_named() -> None:
    report = _report(Status.FAIL)
    report.gates = [
        GateResult(name="task_success", status=Status.FAIL, threshold=">= 0.90")
    ]

    assert _gate_reasons(report, None, allow_warn=False) == [
        "the run failed: failing gate(s): task_success"
    ]


def test_failing_tests_and_assertions_are_counted() -> None:
    report = _report(Status.FAIL)
    report.summary.failed = 2
    report.summary.assertions_failed = 3

    assert _gate_reasons(report, None, allow_warn=False) == [
        "the run failed: 2 failing test(s); 3 failing assertion(s)"
    ]


def test_an_errored_run_reports_the_infrastructure_fault() -> None:
    report = _report(Status.ERROR)
    report.summary.errored = 1

    assert _gate_reasons(report, None, allow_warn=False) == [
        "the run errored: 1 test(s) hit an infrastructure fault"
    ]


def test_a_regressed_baseline_is_reported() -> None:
    report = _report(Status.FAIL)
    report.regression.status = Status.FAIL

    assert _gate_reasons(report, None, allow_warn=False) == [
        "the run failed: regressed against the stored baseline"
    ]


# -- the command ---------------------------------------------------------------


def test_gate_passes_on_a_clean_run(tmp_path: Path) -> None:
    root = _project(tmp_path)
    code, out = _gate(root)

    assert code == 0
    assert "RELEASE GATE: PASS" in out


def test_gate_blocks_on_an_unresolvable_base(tmp_path: Path) -> None:
    root = _project(tmp_path)
    code, out = _gate(root, base="does-not-exist")

    assert code == 1
    assert "RELEASE GATE: BLOCKED" in out


def test_gate_still_blocks_with_allow_warn(tmp_path: Path) -> None:
    root = _project(tmp_path)
    code, out = _gate(root, "--allow-warn", base="does-not-exist")

    assert code == 1
    assert "RELEASE GATE: BLOCKED" in out


def test_gate_blocks_on_a_failing_test(tmp_path: Path) -> None:
    root = _project(tmp_path, body=FAILING_TEST)
    code, out = _gate(root)

    assert code == 1
    assert "RELEASE GATE: BLOCKED" in out
    assert "the run failed" in out


def test_gate_passes_when_selection_skips_everything(tmp_path: Path) -> None:
    """unmatched: skip + a clean diff must not block the release by itself.

    A skip means the change analysis proved the test unaffected — that is what
    selection is *for*, and blocking on it would make `unmatched: skip` (the
    strategy a large suite needs) unusable inside a gate.
    """
    root = _project(tmp_path, unmatched="skip")
    code, out = _gate(root)

    assert code == 0
    assert "RELEASE GATE: PASS" in out
    # Coverage stays visible: the gate says *what* it skipped, in plain text.
    assert "change-aware selection skipped 1 of 1 test(s) against HEAD" in out


def test_selection_skips_are_informational_in_the_report(tmp_path: Path) -> None:
    """The skip is a `selection_skipped` count, not a warning."""
    root = _project(tmp_path, unmatched="skip")
    code, out = _gate(root, "--json")

    assert code == 0
    report = json.loads(out)
    assert report["run"]["selection_base"] == "HEAD"
    assert report["run"]["selection_skipped"] == 1
    assert report["warnings"] == []
    # The reason lives on the skipped test itself, so a skipped run is auditable.
    assert "no change to" in report["tests"][0]["error"]


def test_run_without_changed_records_no_diff(tmp_path: Path) -> None:
    root = _project(tmp_path)
    code, out = _run(root, "--json")

    assert code == 0
    report = json.loads(out)
    assert report["run"]["selection_base"] is None
    assert report["run"]["selection_changed"] == []


def test_run_with_changed_records_the_diff(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "prompts" / "refund.txt").write_text("refund v2\n", encoding="utf-8")
    code, out = _run(root, "--changed", "--base", "HEAD", "--json")

    assert code == 0
    report = json.loads(out)
    assert report["run"]["selection_base"] == "HEAD"
    assert report["run"]["selection_changed"] == ["prompts/refund.txt"]
    # unmatched: run means the diff only explains the selection, it does not drop
    # the test the change does not touch.
    assert report["summary"]["passed"] == 1


def test_run_without_a_resolvable_base_runs_everything(tmp_path: Path) -> None:
    root = _project(tmp_path, unmatched="skip")
    code, out = _run(root, "--changed", "--base", "does-not-exist", "--json")

    assert code == 0
    report = json.loads(out)
    assert report["summary"]["skipped"] == 0
    assert report["run"]["selection_base"] == "does-not-exist"
    assert any("selection unavailable" in w for w in report["warnings"])
