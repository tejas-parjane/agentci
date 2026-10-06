"""Tests for baseline comparison (PRD FR-5, 19).

Two properties are load-bearing here. An *unevaluated* regression check is an
unknown, and 19 forbids rendering an unknown as a pass, so every path where no
comparison happened must come back ``SKIP`` and say why. And the gates must fire
on a real regression while staying quiet below the configured noise floor --
otherwise the first near-zero cost in a baseline build-blocks the team.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentci.core.config import Config, load_config
from agentci.core.result import AssertionOutcome, Status
from agentci.core.runner import RunOptions
from agentci.core.runner import TestRunner as Runner
from agentci.reporting.models import (
    AssertionReport,
    IterationReport,
    RegressionSection,
    RunReport,
)
from agentci.reporting.models import (
    TestReport as CaseReport,
)

BASELINE_ID = "run_baseline"
BASELINE_AT = "2026-01-02T03:04:05.000000Z"


def _make_config(tmp_path: Path, regression: str = "") -> Config:
    # TestRunner loads the adapter eagerly, so the module has to exist even though
    # these tests never invoke it.
    (tmp_path / "probe_agent.py").write_text(
        "from agentci.core.result import AgentResult\n"
        "\n"
        "\n"
        "def run(user_input: str) -> AgentResult:\n"
        "    return AgentResult(output_text=user_input)\n",
        encoding="utf-8",
    )
    path = tmp_path / "agentci.yaml"
    text = (
        "version: 1\n"
        "project:\n"
        "  name: probe\n"
        "agent:\n"
        '  adapter: "probe_agent:run"\n'
    )
    if regression:
        text += regression
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def _runner(tmp_path: Path, regression: str = "") -> Runner:
    return Runner(
        _make_config(tmp_path, regression), options=RunOptions(root=tmp_path)
    )


def _test(
    test_id: str,
    *,
    status: Status,
    cost: float | None = None,
    latency: float = 0.0,
    passed: int = 1,
    failed: int = 0,
) -> CaseReport:
    assertions = [
        AssertionReport(
            assertion_id=f"a{n}",
            kind="output",
            name="n",
            description="d",
            status=AssertionOutcome.PASSED,
        )
        for n in range(passed)
    ]
    assertions += [
        AssertionReport(
            assertion_id=f"f{n}",
            kind="output",
            name="n",
            description="d",
            status=AssertionOutcome.FAILED,
        )
        for n in range(failed)
    ]
    return CaseReport(
        test_id=test_id,
        name=test_id,
        file="tests/agentci/test_probe.py",
        status=status,
        assertions=assertions,
        iterations=[
            IterationReport(
                run_id="run_now",
                iteration=1,
                status=status,
                latency_ms=latency,
                cost_usd=cost,
            )
        ],
    )


def _report(*tests: CaseReport) -> RunReport:
    return RunReport(warnings=[], tests=list(tests))


def _write_baseline(tmp_path: Path, tests: dict[str, dict]) -> None:
    path = tmp_path / ".agentci" / "baseline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "agentci_version": "0.1.0",
                "run": {"id": BASELINE_ID, "started_at": BASELINE_AT},
                "tests": tests,
            }
        ),
        encoding="utf-8",
    )


def _entry(status: str = "pass", *, cost: float = 0.0, latency: float = 1.0, score: float = 1.0):
    return {
        "status": status,
        "flakiness": "none",
        "cost_usd": cost,
        "duration_ms": latency,
        "score": score,
    }


# -- what happens when there is nothing to compare ---------------------------


def test_missing_baseline_is_skipped_and_explains_itself(tmp_path: Path) -> None:
    report = _report(_test("t", status=Status.PASS))
    section = _runner(tmp_path)._compare_baseline(report)

    assert section.compared is False
    assert section.status is Status.SKIP
    assert "agentci baseline save" in section.message


def test_unreadable_baseline_warns_and_stays_skipped(tmp_path: Path) -> None:
    path = tmp_path / ".agentci" / "baseline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    report = _report(_test("t", status=Status.PASS))

    section = _runner(tmp_path)._compare_baseline(report)

    assert section.compared is False
    assert section.status is Status.SKIP
    assert report.warnings, "a corrupt baseline must surface as a warning"


def test_baseline_without_a_tests_mapping_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / ".agentci" / "baseline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run": {"id": "r"}}), encoding="utf-8")
    report = _report(_test("t", status=Status.PASS))

    section = _runner(tmp_path)._compare_baseline(report)

    assert section.compared is False
    assert section.status is Status.SKIP
    assert report.warnings


# -- a comparison that happened and found nothing wrong ----------------------


def test_matching_baseline_is_compared_and_passes(tmp_path: Path) -> None:
    _write_baseline(tmp_path, {"t": _entry("pass")})
    report = _report(_test("t", status=Status.PASS))

    section = _runner(tmp_path)._compare_baseline(report)

    assert section.compared is True
    assert section.status is Status.PASS
    assert section.baseline_run_id == BASELINE_ID
    assert section.baseline_created_at is not None
    assert section.new_failures == []
    assert not report.warnings


def test_new_failure_blocks_the_run(tmp_path: Path) -> None:
    _write_baseline(tmp_path, {"t": _entry("pass")})
    report = _report(_test("t", status=Status.FAIL))

    section = _runner(tmp_path)._compare_baseline(report)

    assert section.status is Status.FAIL
    assert section.new_failures == ["t"]
    gate = next(g for g in section.comparisons if g.name == "regression.new_failures")
    assert gate.status is Status.FAIL
    assert gate.source == "regression"


def test_an_error_counts_as_a_new_failure(tmp_path: Path) -> None:
    _write_baseline(tmp_path, {"t": _entry("pass")})
    report = _report(_test("t", status=Status.ERROR))

    section = _runner(tmp_path)._compare_baseline(report)

    assert section.new_failures == ["t"]


def test_a_test_already_failing_is_not_a_new_failure(tmp_path: Path) -> None:
    _write_baseline(tmp_path, {"t": _entry("fail")})
    report = _report(_test("t", status=Status.FAIL))

    section = _runner(tmp_path)._compare_baseline(report)

    assert section.new_failures == []
    assert section.status is Status.PASS


def test_on_regression_warn_downgrades_the_verdict(tmp_path: Path) -> None:
    _write_baseline(tmp_path, {"t": _entry("pass")})
    report = _report(_test("t", status=Status.FAIL))

    section = _runner(tmp_path, "regression:\n  on_regression: warn\n")._compare_baseline(
        report
    )

    assert section.status is Status.WARN


def test_new_failure_gating_can_be_disabled(tmp_path: Path) -> None:
    _write_baseline(tmp_path, {"t": _entry("pass")})
    report = _report(_test("t", status=Status.FAIL))

    section = _runner(
        tmp_path, "regression:\n  fail_on_new_test_failures: false\n"
    )._compare_baseline(report)

    assert section.status is Status.PASS
    assert section.new_failures == ["t"], "the regression is still reported"


def test_recovered_test_is_reported_as_fixed(tmp_path: Path) -> None:
    _write_baseline(tmp_path, {"t": _entry("fail")})
    report = _report(_test("t", status=Status.PASS))

    section = _runner(tmp_path)._compare_baseline(report)

    assert section.fixed == ["t"]
    assert section.status is Status.PASS


def test_baseline_test_absent_from_this_run_does_not_gate(tmp_path: Path) -> None:
    _write_baseline(tmp_path, {"gone": _entry("pass"), "here": _entry("pass")})
    report = _report(_test("here", status=Status.PASS))

    section = _runner(tmp_path)._compare_baseline(report)

    assert section.missing_from_run == ["gone"]
    assert section.status is Status.PASS, "--tag selection drops tests legitimately"


def test_no_shared_tests_is_skipped_not_passed(tmp_path: Path) -> None:
    _write_baseline(tmp_path, {"other": _entry("pass")})
    report = _report(_test("here", status=Status.PASS))

    section = _runner(tmp_path)._compare_baseline(report)

    assert section.status is Status.SKIP
    assert section.compared is True


# -- the relative gates ------------------------------------------------------


def _cost_regression(
    tmp_path: Path,
    *,
    baseline_cost: float,
    current_cost: float,
    rules: str,
) -> RegressionSection:
    _write_baseline(tmp_path, {"t": _entry("pass", cost=baseline_cost)})
    report = _report(_test("t", status=Status.PASS, cost=current_cost))
    return _runner(tmp_path, rules)._compare_baseline(report)


def test_cost_increase_beyond_the_limit_fails(tmp_path: Path) -> None:
    section = _cost_regression(
        tmp_path,
        baseline_cost=0.10,
        current_cost=0.20,
        rules="regression:\n  max_cost_increase_pct: 25\n",
    )

    assert section.status is Status.FAIL
    gate = next(g for g in section.comparisons if g.name == "regression.cost_increase")
    assert gate.status is Status.FAIL
    assert gate.actual == pytest.approx(100.0)


def test_cost_increase_inside_the_limit_passes(tmp_path: Path) -> None:
    section = _cost_regression(
        tmp_path,
        baseline_cost=0.10,
        current_cost=0.11,
        rules="regression:\n  max_cost_increase_pct: 25\n",
    )

    assert section.status is Status.PASS


def test_cost_change_below_the_noise_floor_is_not_a_regression(tmp_path: Path) -> None:
    section = _cost_regression(
        tmp_path,
        baseline_cost=0.10,
        current_cost=0.104,
        rules=(
            "regression:\n"
            "  max_cost_increase_pct: 1\n"
            "  ignore_regression_below_pct: 5\n"
        ),
    )

    assert section.status is Status.PASS
    gate = next(g for g in section.comparisons if g.name == "regression.cost_increase")
    assert gate.status is Status.PASS
    assert "noise floor" in gate.message


def test_zero_baseline_cost_is_undefined_not_a_pass(tmp_path: Path) -> None:
    section = _cost_regression(
        tmp_path,
        baseline_cost=0.0,
        current_cost=0.50,
        rules="regression:\n  max_cost_increase_pct: 25\n",
    )

    gate = next(g for g in section.comparisons if g.name == "regression.cost_increase")
    assert gate.status is Status.SKIP
    assert section.status is Status.PASS, "an undefined gate does not block"


def test_latency_regression_is_gated(tmp_path: Path) -> None:
    _write_baseline(tmp_path, {"t": _entry("pass", latency=10.0)})
    report = _report(_test("t", status=Status.PASS, latency=40.0))

    section = _runner(
        tmp_path, "regression:\n  max_latency_increase_pct: 30\n"
    )._compare_baseline(report)

    assert section.status is Status.FAIL
    gate = next(
        g for g in section.comparisons if g.name == "regression.latency_increase"
    )
    assert gate.status is Status.FAIL
    assert gate.actual == pytest.approx(300.0)


def test_quality_drop_beyond_the_limit_fails(tmp_path: Path) -> None:
    _write_baseline(tmp_path, {"t": _entry("pass", score=1.0)})
    report = _report(_test("t", status=Status.PASS, passed=1, failed=1))

    section = _runner(
        tmp_path, "regression:\n  max_quality_drop: 0.05\n"
    )._compare_baseline(report)

    assert section.status is Status.FAIL
    gate = next(g for g in section.comparisons if g.name == "regression.quality_drop")
    assert gate.status is Status.FAIL
    assert gate.actual == pytest.approx(0.5)


def test_quality_within_the_tolerance_passes(tmp_path: Path) -> None:
    _write_baseline(tmp_path, {"t": _entry("pass", score=1.0)})
    report = _report(_test("t", status=Status.PASS, passed=9, failed=1))

    section = _runner(
        tmp_path, "regression:\n  max_quality_drop: 0.2\n"
    )._compare_baseline(report)

    assert section.status is Status.PASS
