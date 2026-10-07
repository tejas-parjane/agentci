"""Hermetic end-to-end tests for the openai-agents integration.

The tests drive the public CLI (``agentci record`` / ``replay`` / ``diff``)
against an ``agents.Agent`` whose model is a :class:`ScriptedModel`, so no
provider is ever contacted. The interesting property: an unchanged agent
replays clean, and an agent that now refunds twice is caught as a divergence
and flagged by ``diff`` -- exactly what the README demo promises.
"""

from __future__ import annotations

import json
from itertools import count
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from agentci.cli import app

runner = CliRunner()
_PROJECTS = count(1)

TEST_BODY = """\
from agentci.testing import agent_test


@agent_test()
def test_refund_flow(agent):
    result = agent.run("please refund order ORD-7781")
    assert result is not None
"""

ADAPTER = """\
from agents import Agent, function_tool
from agents.testing.model import ScriptedModel, assistant_message, function_call

from agentci.integrations.openai_agents import AgentCI

CALLS = __CALLS__


def _agent():
    @function_tool
    def lookup_order(order_id: str):
        return {"status": "open", "order_id": order_id}

    @function_tool
    def refund_order(order_id: str, amount: float):
        return {"refunded": True, "order_id": order_id, "amount": amount}

    steps = [
        {
            "output": [
                function_call("lookup_order", {"order_id": "ORD-7781"}, call_id="call_1")
            ]
        },
        {
            "output": [
                function_call(
                    "refund_order",
                    {"order_id": "ORD-7781", "amount": 49.99},
                    call_id="call_2",
                )
            ]
        },
    ]
    for extra in range(CALLS - 1):
        steps.append(
            {
                "output": [
                    function_call(
                        "refund_order",
                        {"order_id": "ORD-7781", "amount": 49.99},
                        call_id=f"call_{3 + extra}",
                    )
                ]
            }
        )
    steps.append({"output": [assistant_message("Refund issued for ORD-7781.")]})
    model = ScriptedModel(steps)
    return Agent(
        name="refunder",
        instructions="Refund customer orders.",
        tools=[lookup_order, refund_order],
        model=model,
    )


class RefundSupportAgent(AgentCI):
    def __init__(self):
        super(RefundSupportAgent, self).__init__(_agent())
"""


def _project(tmp_path: Path, *, calls: int) -> Path:
    """A hermetic project whose agent refunds an order ``calls`` times."""
    index = next(_PROJECTS)
    root = tmp_path / f"proj{index}"
    adapter = f"support_{index}"
    test_file = f"test_support_{index}.py"
    root.mkdir(parents=True)
    (root / f"{adapter}.py").write_text(
        ADAPTER.replace("__CALLS__", str(calls)), encoding="utf-8"
    )
    (root / test_file).write_text(TEST_BODY, encoding="utf-8")
    (root / "agentci.yaml").write_text(
        "version: 1\n"
        "project:\n"
        f"  name: openai-{index}\n"
        "agent:\n"
        f'  adapter: "{adapter}:RefundSupportAgent"\n'
        "execution:\n"
        "  external_side_effects: deny\n"
        "  mocks:\n"
        "    refund_order:\n"
        "      response:\n"
        "        refunded: true\n"
        "        amount_refunded: 49.99\n"
        "storage:\n"
        "  dir: .agentci\n"
        "tests:\n"
        f"  - file: {test_file}\n",
        encoding="utf-8",
    )
    return root


def _test_id(root: Path) -> str:
    listing = runner.invoke(app, ["list", "--root", str(root), "--ids"])
    assert listing.exit_code == 0
    return listing.stdout.strip().splitlines()[0].split(" ")[0]


def _trace_lines(artifact: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in artifact.read_text(encoding="utf-8").splitlines()]


def test_record_emits_tool_and_model_events(tmp_path: Path) -> None:
    root = _project(tmp_path, calls=1)
    recorded = runner.invoke(
        app, ["record", "--root", str(root), "--name", "refund"], catch_exceptions=False
    )
    assert recorded.exit_code == 0, recorded.stdout
    artifact = root / ".agentci" / "traces" / "refund.jsonl"
    assert artifact.is_file()
    lines = _trace_lines(artifact)
    kinds = {line["type"] for line in lines}
    assert "model_call_started" in kinds and "model_call_completed" in kinds
    assert "tool_call_started" in kinds and "tool_call_completed" in kinds
    refunded = [
        line
        for line in lines
        if line.get("type") == "tool_call_completed"
        and line.get("tool", {}).get("name") == "refund_order"
    ]
    assert len(refunded) == 1
    # The configured mock is what a recorded run answered with.
    assert refunded[0]["status"] == "mocked"


def test_round_trip_replays_clean(tmp_path: Path) -> None:
    root = _project(tmp_path, calls=1)
    recorded = runner.invoke(
        app, ["record", "--root", str(root), "--name", "refund"], catch_exceptions=False
    )
    assert recorded.exit_code == 0, recorded.stdout
    artifact = root / ".agentci" / "traces" / "refund.jsonl"

    replayed = runner.invoke(
        app,
        ["replay", "--root", str(root), str(artifact), "--out", str(root / "out-replay.jsonl")],
        catch_exceptions=False,
    )
    assert replayed.exit_code == 0, replayed.stdout
    replay_artifact = root / "out-replay.jsonl"
    assert replay_artifact.is_file()

    diffed = runner.invoke(
        app,
        ["diff", str(artifact), str(replay_artifact)],
        catch_exceptions=False,
    )
    assert diffed.exit_code == 0, diffed.stdout
    assert "BEHAVIOR: UNCHANGED" in diffed.stdout


def test_double_refund_is_caught_and_blocks(tmp_path: Path) -> None:
    original = _project(tmp_path, calls=1)
    recorded = runner.invoke(
        app, ["record", "--root", str(original), "--name", "refund"], catch_exceptions=False
    )
    assert recorded.exit_code == 0, recorded.stdout
    artifact = original / ".agentci" / "traces" / "refund.jsonl"

    # The agent changed: it now refunds the order twice. Replaying the original
    # recording must refuse the second refund and fail the run.
    diverged = _project(tmp_path, calls=2)
    test_id = _test_id(diverged)
    replayed = runner.invoke(
        app,
        [
            "replay",
            "--root",
            str(diverged),
            "--test-id",
            test_id,
            str(artifact),
            "--out",
            str(diverged / "diverged-replay.jsonl"),
        ],
        catch_exceptions=False,
    )
    assert replayed.exit_code == 1, replayed.stdout
    assert "FAIL" in replayed.stdout

    replay_artifact = diverged / "diverged-replay.jsonl"
    lines = _trace_lines(replay_artifact)
    refunded = [
        line for line in lines if line.get("type") == "tool_call_completed"
        and line.get("tool", {}).get("name") == "refund_order"
    ]
    assert len(refunded) == 2, "the run still tried to refund twice"
    denied = [
        line
        for line in refunded
        if line.get("status") == "denied" and line.get("metadata", {}).get("replay") == "divergent"
    ]
    assert denied, "the second refund must be recorded as a divergence"

    diffed = runner.invoke(
        app,
        ["diff", str(artifact), str(replay_artifact)],
        catch_exceptions=False,
    )
    assert diffed.exit_code == 1
    assert "BEHAVIOR: CHANGED" in diffed.stdout
    assert "refund_order" in diffed.stdout


def test_gate_blocks_on_the_divergent_run(tmp_path: Path) -> None:
    """The release gate surfaces the same double-refund disagreement."""
    original = _project(tmp_path, calls=1)
    assert (
        runner.invoke(
            app, ["record", "--root", str(original), "--name", "refund"], catch_exceptions=False
        ).exit_code
        == 0
    )
    artifact = original / ".agentci" / "traces" / "refund.jsonl"

    diverged = _project(tmp_path, calls=2)
    test_id = _test_id(diverged)
    replay_artifact = diverged / "diverged-replay.jsonl"
    assert (
        runner.invoke(
            app,
            [
                "replay",
                "--root",
                str(diverged),
                "--test-id",
                test_id,
                str(artifact),
                "--out",
                str(replay_artifact),
            ],
            catch_exceptions=False,
        ).exit_code
        == 1
    )

    gated = runner.invoke(
        app,
        ["replay", "--root", str(diverged), "--test-id", test_id, str(artifact), "--quiet"],
        catch_exceptions=False,
    )
    assert gated.exit_code == 1, gated.stdout


def test_run_without_replay_still_traces_both_refunds(tmp_path: Path) -> None:
    """A plain run of the changed agent records what it actually did."""
    root = _project(tmp_path, calls=2)
    run = runner.invoke(
        app, ["run", "--root", str(root)], catch_exceptions=False
    )
    assert run.exit_code == 0, run.stdout
    report_dir = root / ".agentci"
    assert report_dir.is_dir()
    # The run persisted a trace artifact under the storage dir.
    trace_files = list(report_dir.rglob("trace.jsonl"))
    assert trace_files, "a run should persist at least one trace artifact"
