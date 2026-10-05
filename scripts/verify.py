"""Ad-hoc verification harness used during development.

Not part of the shipped test suite -- run it directly to print a readable summary
of the example suite plus the negative-path probes that must never pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agentci.adapters.python import load_adapter  # noqa: E402
from agentci.assertions import expect  # noqa: E402
from agentci.core.config import load_config, resolve_test_files  # noqa: E402
from agentci.core.runner import RunOptions, TestRunner  # noqa: E402
from agentci.testing import AgentTestCase, discover  # noqa: E402


def example_suite() -> int:
    cfg = load_config(ROOT / "agentci.yaml")
    cases = discover(resolve_test_files(cfg, ROOT), project_root=ROOT)
    report = TestRunner(cfg, options=RunOptions(root=ROOT)).run(cases)
    print(
        f"example suite : {report.run.status.value} | "
        f"{report.summary.passed}/{report.summary.total} tests | "
        f"{report.summary.assertions_passed} assertions passed, "
        f"{report.summary.assertions_failed} failed"
    )
    return 0 if report.run.status.value == "pass" else 1


def probe(adapter_ref, body, label, budgets=None) -> bool:
    cfg = load_config(ROOT / "agentci.yaml")
    if budgets:
        cfg = cfg.model_copy(update={"budgets": cfg.budgets.model_copy(update=budgets)})
    runner = TestRunner(
        cfg,
        adapter=load_adapter(adapter_ref, project_root=ROOT),
        options=RunOptions(root=ROOT, record_traces=False),
    )
    report = runner.run(
        [AgentTestCase(fn=body, name=label, test_id=label, file="probe.py", line=1)]
    )
    test = report.tests[0]
    detail = (test.error or "").splitlines()[0][:88] if test.error else ""
    print(f"  {test.status.value:5} {label:36} {detail}")
    return test.status.value not in {"pass"}


def negative_paths() -> int:
    """Every probe here must NOT pass. A probe that passes is a bug in AgentCI."""
    print("negative paths (each must fail):")
    checks = [
        probe(
            "examples.support_agent.agent:run_agent_slow",
            lambda agent: agent.run("hi"),
            "latency budget exceeded",
            {"max_latency_ms": 50},
        ),
        probe(
            "examples.support_agent.agent:run_agent_looping",
            lambda agent: agent.run("go"),
            "step limit exceeded",
            {"max_steps": 3},
        ),
        probe(
            "examples.support_agent.agent:run_agent_secretive",
            lambda agent: agent.run("my key?"),
            "credential leaked into output",
        ),
        probe(
            "examples.support_agent.agent:run_agent_undeclared_tool",
            lambda agent: agent.run("go"),
            "agent called undeclared tool",
        ),
        probe(
            "examples.support_agent.agent:run_agent",
            lambda agent: (_ for _ in ()).throw(AssertionError("boom")),
            "bare assert in test body",
        ),
        probe(
            "examples.support_agent.agent:run_agent",
            lambda agent: (_ for _ in ()).throw(RuntimeError("infra")),
            "infrastructure fault",
        ),
        probe(
            # SupportAgent, not the returned-trace `run_agent`: a plain-function
            # adapter declares no tools, so its `ctx.tools.call` would be refused
            # as undeclared and the probe would never reach the assertion.
            "examples.support_agent.agent:SupportAgent",
            lambda agent: expect(agent.run("T-1001")).to_contain("no such text"),
            "assertion genuinely fails",
        ),
    ]
    missed = [i for i, ok in enumerate(checks, 1) if not ok]
    if missed:
        print(f"  !! {len(missed)} probe(s) wrongly passed: {missed}")
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(example_suite() | negative_paths())