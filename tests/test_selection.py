"""Tests for change-aware test selection (PRD FR-6).

The property that matters most here is the *unresolved* path. A base ref that
cannot be resolved, or a git checkout where the diff fails, must never reduce the
set of tests that run — that would turn a broken environment into a green build.
Everything else in this file is ordinary matching; that one is a release gate's
load-bearing assumption.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agentci.core.config import Config, load_config
from agentci.core.runner import RunOptions
from agentci.core.runner import TestRunner as Runner
from agentci.core.selection import (
    SelectionPlan,
    build_plan,
    path_matches,
    path_matches_any,
)
from agentci.testing import AgentTestCase

# -- glob matching -------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("tools/refund.py", "tools/*.py", True),
        ("tools/a/refund.py", "tools/*.py", False),
        ("src/app.py", "src/app.py", True),
        ("src/app2.py", "src/app.py", False),
        ("docs/api.md", "docs/*", True),
        ("docs/api/v1.md", "docs/*", False),
        ("a/b.py", "a/**", True),
        ("a/b/c/d.py", "a/**", True),
        ("a/b.py", "a/**/b.py", True),  # ** spans zero directories
        ("a/x/b.py", "a/**/b.py", True),
        ("a/x/y/b.py", "a/**/b.py", True),
        ("z/b.py", "a/**/b.py", False),
        ("conf.py", "**/conf.py", True),  # a leading ** matches zero directories
        ("a/b/conf.py", "**/conf.py", True),
        ("x/y", "x/?", True),
        ("x/yz", "x/?", False),
        # A pattern with no glob characters also covers everything underneath it.
        ("examples/support_agent/agent.py", "examples/support_agent", True),
        ("examples/support_agentX/agent.py", "examples/support_agent", False),
        ("examples/other.py", "examples/support_agent", False),
    ],
)
def test_path_matches(path: str, pattern: str, expected: bool) -> None:
    assert path_matches(path, pattern) is expected


def test_path_matches_any_short_circuits() -> None:
    assert path_matches_any("prompts/refund.txt", ["nope/*", "prompts/*"])
    assert not path_matches_any("prompts/refund.txt", ["nope/*", "other/*"])


# -- building a plan -----------------------------------------------------------


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


CONFIG_TEXT = """\
version: 1
project:
  name: probe
agent:
  adapter: "probe_agent:run"
"""


def _make_repo(tmp_path: Path, *, selection: str = "") -> tuple[Path, Config]:
    """A git repo with a baseline commit and an ``agentci.yaml``."""
    root = tmp_path / "repo"
    (root / "prompts").mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "src").mkdir()
    (root / "prompts" / "refund.txt").write_text("refund v1\n", encoding="utf-8")
    (root / "prompts" / "system.txt").write_text("system v1\n", encoding="utf-8")
    (root / "tools" / "refund.py").write_text("def refund(): ...\n", encoding="utf-8")
    (root / "src" / "app.py").write_text("print('v1')\n", encoding="utf-8")
    (root / "agentci.yaml").write_text(CONFIG_TEXT + selection, encoding="utf-8")
    (root / "probe_agent.py").write_text(
        "from agentci.core.result import AgentResult\n"
        "\n"
        "\n"
        "def run(user_input: str) -> AgentResult:\n"
        "    return AgentResult(output_text=user_input)\n",
        encoding="utf-8",
    )

    _git(root, "init", "-q")
    # Pin the branch name: default_base_ref() rewrites a bare value to origin/<x>,
    # so the environment test below needs a ref that survives that rewriting.
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(root, "config", "user.email", "agentci@example.com")
    _git(root, "config", "user.name", "AgentCI")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")
    return root, load_config(root / "agentci.yaml")


def _change(root: Path, relative: str, contents: str) -> None:
    (root / relative).write_text(contents, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"change {relative}")


def test_disabled_selection_never_consults_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = _make_repo(tmp_path, selection="selection:\n  enabled: false\n")

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("git must not be consulted when selection is disabled")

    monkeypatch.setattr("agentci.core.selection.changed_files", explode)

    plan = build_plan(config, root, base="HEAD")
    assert plan.enabled is False
    assert plan.active is False
    assert plan.unresolved == ""


def test_missing_base_is_unresolved_not_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = _make_repo(tmp_path)
    monkeypatch.delenv("AGENTCI_BASE", raising=False)
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    # No remote in this repo, so default_base_ref() cannot suggest origin/main.
    plan = build_plan(config, root)

    assert plan.enabled is True
    assert plan.active is False
    assert plan.changed == ()
    assert "no base ref" in plan.unresolved


def test_unknown_ref_is_unresolved(tmp_path: Path) -> None:
    root, config = _make_repo(tmp_path)
    plan = build_plan(config, root, base="does-not-exist")

    assert plan.active is False
    assert plan.base == "does-not-exist"
    assert "git could not diff" in plan.unresolved


def test_changed_files_are_reported(tmp_path: Path) -> None:
    root, config = _make_repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    _change(root, "prompts/refund.txt", "refund v2\n")

    plan = build_plan(config, root, base=base)

    assert plan.active is True
    assert plan.base == base
    assert plan.changed == ("prompts/refund.txt",)
    assert plan.global_hit is False


def test_no_changes_is_a_resolved_empty_diff(tmp_path: Path) -> None:
    root, config = _make_repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")

    plan = build_plan(config, root, base=base)

    assert plan.active is True
    assert plan.changed == ()


def test_uncommitted_work_still_counts_as_a_change(tmp_path: Path) -> None:
    root, config = _make_repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    (root / "prompts" / "refund.txt").write_text("refund wip\n", encoding="utf-8")

    plan = build_plan(config, root, base=base)

    assert plan.active is True
    assert plan.changed == ("prompts/refund.txt",)


def test_untracked_files_still_count_as_a_change(tmp_path: Path) -> None:
    root, config = _make_repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    (root / "tools" / "new_tool.py").write_text("def new_tool(): ...\n", encoding="utf-8")

    plan = build_plan(config, root, base=base)

    assert plan.active is True
    assert plan.changed == ("tools/new_tool.py",)


def test_a_branch_is_measured_from_its_merge_base(tmp_path: Path) -> None:
    """``main`` moving on must not be mistaken for a change on the branch."""
    root, config = _make_repo(tmp_path)
    _git(root, "checkout", "-q", "-b", "feature")
    _change(root, "prompts/refund.txt", "refund v2\n")
    _git(root, "checkout", "-q", "main")
    _change(root, "src/app.py", "print('main moved on')\n")
    _git(root, "checkout", "-q", "feature")

    plan = build_plan(config, root, base="main")

    assert plan.active is True
    assert plan.changed == ("prompts/refund.txt",)


def test_change_to_a_global_path_affects_everything(tmp_path: Path) -> None:
    root, config = _make_repo(
        tmp_path,
        selection="selection:\n  global_paths:\n    - prompts/system.txt\n",
    )
    base = _git(root, "rev-parse", "HEAD")
    _change(root, "prompts/system.txt", "system v2\n")

    plan = build_plan(config, root, base=base)

    assert plan.global_hit is True
    assert plan.active is True


def test_explicit_base_wins_over_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = _make_repo(tmp_path)
    monkeypatch.setenv("AGENTCI_BASE", "does-not-exist")
    base = _git(root, "rev-parse", "HEAD")

    plan = build_plan(config, root, base=base)

    assert plan.active is True
    assert plan.base == base


def test_environment_base_is_used_when_no_flag_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = _make_repo(tmp_path)
    _git(root, "checkout", "-q", "-b", "feature")
    _change(root, "src/app.py", "print('v2')\n")
    # A bare value would be rewritten to origin/<value>, and this repo has no
    # remote, so a fully qualified ref is the only form default_base_ref() keeps.
    monkeypatch.setenv("AGENTCI_BASE", "refs/heads/main")

    plan = build_plan(config, root)

    assert plan.active is True
    assert plan.base == "refs/heads/main"
    assert plan.changed == ("src/app.py",)


def test_config_default_base_is_the_last_resort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = _make_repo(
        tmp_path, selection="selection:\n  default_base: HEAD~1\n"
    )
    _change(root, "src/app.py", "print('v2')\n")
    monkeypatch.delenv("AGENTCI_BASE", raising=False)
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)

    plan = build_plan(config, root)

    assert plan.base == "HEAD~1"
    assert plan.changed == ("src/app.py",)


# -- evaluating a plan ---------------------------------------------------------


def _case(test_id: str, dependencies: list[str]) -> AgentTestCase:
    def fn() -> None:
        return None

    return AgentTestCase(
        fn=fn,
        name=test_id,
        test_id=test_id,
        dependencies=dependencies,
    )


def _plan(**kwargs: object) -> SelectionPlan:
    return SelectionPlan(**kwargs)  # type: ignore[arg-type]


def test_a_disabled_plan_runs_everything() -> None:
    assert _plan(enabled=False).evaluate(_case("t", ["prompts/refund.txt"])) == (True, "")


def test_an_unresolved_plan_runs_everything() -> None:
    plan = _plan(enabled=True, unresolved="no base ref could be determined")
    assert plan.evaluate(_case("t", ["prompts/refund.txt"])) == (True, "")


def test_a_global_hit_runs_everything() -> None:
    plan = _plan(enabled=True, base="main", global_hit=True, unmatched="skip")
    assert plan.evaluate(_case("t", ["prompts/refund.txt"])) == (True, "")


def test_no_dependencies_runs_by_default() -> None:
    plan = _plan(enabled=True, base="main", unmatched="run")
    assert plan.evaluate(_case("t", [])) == (True, "")


def test_no_dependencies_is_skipped_only_when_opted_in() -> None:
    plan = _plan(enabled=True, base="main", unmatched="skip")
    should_run, reason = plan.evaluate(_case("t", []))

    assert should_run is False
    assert "declares no dependencies" in reason


def test_a_changed_dependency_keeps_the_test() -> None:
    plan = _plan(
        enabled=True,
        base="main",
        changed=("prompts/refund.txt",),
        unmatched="skip",
    )
    assert plan.evaluate(_case("t", ["prompts/refund.txt", "tools/*.py"])) == (True, "")


def test_any_one_changed_dependency_keeps_the_test() -> None:
    plan = _plan(
        enabled=True,
        base="main",
        changed=("tools/refund.py",),
        unmatched="skip",
    )
    assert plan.evaluate(_case("t", ["prompts/refund.txt", "tools/*.py"])) == (True, "")


def test_an_unchanged_dependency_is_skipped_and_says_why() -> None:
    plan = _plan(
        enabled=True,
        base="origin/main",
        changed=("src/app.py",),
        unmatched="skip",
    )
    should_run, reason = plan.evaluate(_case("t", ["prompts/refund.txt"]))

    assert should_run is False
    assert "prompts/refund.txt" in reason
    assert "origin/main" in reason


def test_an_unchanged_dependency_still_runs_by_default() -> None:
    plan = _plan(
        enabled=True,
        base="main",
        changed=("src/app.py",),
        unmatched="run",
    )
    assert plan.evaluate(_case("t", ["prompts/refund.txt"])) == (True, "")


# -- through the runner --------------------------------------------------------


def _runner(
    root: Path,
    config: Config,
    plan: SelectionPlan | None,
) -> Runner:
    return Runner(config, options=RunOptions(root=root, selection=plan))


def test_skipped_tests_reach_the_report_with_a_reason(tmp_path: Path) -> None:
    root, config = _make_repo(tmp_path, selection="selection:\n  unmatched: skip\n")
    plan = _plan(
        enabled=True,
        base="origin/main",
        changed=("src/app.py",),
        unmatched="skip",
    )
    cases = [_case("affects_refund", ["prompts/refund.txt"]), _case("affects_tools", ["tools/*"])]

    report = _runner(root, config, plan).run(cases)

    assert [test.status.value for test in report.tests] == ["skip", "skip"]
    assert "prompts/refund.txt" in (report.tests[0].error or "")
    assert "origin/main" in (report.tests[0].error or "")
    assert report.summary.skipped == 2
    assert any("skipped 2 of 2" in warning for warning in report.warnings)


def test_a_matched_dependency_still_runs(tmp_path: Path) -> None:
    root, config = _make_repo(tmp_path, selection="selection:\n  unmatched: skip\n")
    plan = _plan(
        enabled=True,
        base="main",
        changed=("prompts/refund.txt",),
        unmatched="skip",
    )
    cases = [_case("affects_refund", ["prompts/refund.txt"]), _case("affects_tools", ["tools/*"])]

    report = _runner(root, config, plan).run(cases)

    statuses = {test.test_id: test.status.value for test in report.tests}
    assert statuses == {"affects_refund": "pass", "affects_tools": "skip"}


def test_an_unresolved_plan_runs_everything_and_warns(tmp_path: Path) -> None:
    root, config = _make_repo(tmp_path, selection="selection:\n  unmatched: skip\n")
    plan = _plan(
        enabled=True,
        base=None,
        unmatched="skip",
        unresolved="no base ref could be determined",
    )
    cases = [_case("affects_refund", ["prompts/refund.txt"]), _case("bare", [])]

    report = _runner(root, config, plan).run(cases)

    assert [test.status.value for test in report.tests] == ["pass", "pass"]
    assert any("selection unavailable" in warning for warning in report.warnings)
    assert not any("skipped" in warning for warning in report.warnings)


def test_without_a_plan_the_report_carries_no_selection(tmp_path: Path) -> None:
    root, config = _make_repo(tmp_path)
    cases = [_case("bare", [])]

    report = _runner(root, config, None).run(cases)

    assert report.run.selection_base is None
    assert report.run.selection_changed == []
    assert report.run.selected_by == "all"
    assert report.summary.skipped == 0


def test_the_run_identity_records_the_diff(tmp_path: Path) -> None:
    root, config = _make_repo(tmp_path, selection="selection:\n  unmatched: skip\n")
    plan = _plan(
        enabled=True,
        base="origin/main",
        changed=("src/app.py", "prompts/refund.txt"),
        unmatched="skip",
    )

    report = _runner(root, config, plan).run([_case("bare", [])])

    assert report.run.selection_base == "origin/main"
    assert report.run.selection_changed == ["src/app.py", "prompts/refund.txt"]
    assert report.run.selected_by == "all"
