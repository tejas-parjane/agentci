"""Tests for ``agentci record`` / ``agentci replay`` and the replay hook.

Replay means: run the *current* agent, but answer every tool call from a recorded
artifact instead of the live world. Two properties matter:

* an unchanged agent replays clean — calls are answered in recorded order and the
  run passes;
* a changed agent is caught — a call the recording never answered is refused,
  recorded as ``replay: divergent``, and fails the run, so a replay is never
  silently "fine".
"""

from __future__ import annotations

import json
from itertools import count
from pathlib import Path

from typer.testing import CliRunner

from agentci.cli import app
from agentci.core.replay import ReplaySession, extract_test_id
from agentci.core.trace import EventStatus, ToolCall, TraceEvent

runner = CliRunner()
_PROJECTS = count(1)

TEST_BODY = """\
from agentci.testing import agent_test


@agent_test()
def test_replay_target(agent):
    result = agent.run("please refund order ORD-7781")
    assert result is not None
"""

ADAPTER = """\
from agentci.adapters.base import BaseAdapter
from agentci.core.result import AgentResult
from agentci.core.tools import ToolDecl

CALLS = {calls}


class RefunderAgent(BaseAdapter):
    name = "refunder"

    tools = {{
        "refund_order": ToolDecl(
            name="refund_order",
            side_effect=True,
            description="Refund an order.",
        ),
    }}

    def run(self, user_input: str, ctx):
        for _ in range(CALLS):
            ctx.tools.call("refund_order", order_id="ORD-7781", amount=49.99)
        return AgentResult(output_text="done")
"""


def _project(tmp_path: Path, *, calls: int) -> Path:
    """A hermetic project whose agent refunds an order ``calls`` times."""
    index = next(_PROJECTS)
    root = tmp_path / f"proj{index}"
    adapter = f"refunder_{index}"
    test_file = f"test_replay_{index}.py"
    root.mkdir(parents=True)
    (root / f"{adapter}.py").write_text(
        ADAPTER.format(calls=calls), encoding="utf-8"
    )
    (root / test_file).write_text(TEST_BODY, encoding="utf-8")
    (root / "agentci.yaml").write_text(
        "version: 1\n"
        "project:\n"
        f"  name: replay-{index}\n"
        "agent:\n"
        f'  adapter: "{adapter}:RefunderAgent"\n'
        "execution:\n"
        "  external_side_effects: deny\n"
        "  mocks:\n"
        f"    refund_order:\n"
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


# -- the session ---------------------------------------------------------------


def _session_test_events() -> list[TraceEvent]:
    def ev(type_: str, **fields: object) -> TraceEvent:
        data: dict[str, object] = {
            "run_id": "run_1",
            "event_id": "evt_x",
            "type": type_,
            "timestamp": "2026-10-07T12:00:00Z",
            "parent_id": None,
            "component": None,
            "tool": None,
            "result": None,
            "usage": None,
            "cost_usd": None,
            "duration_ms": None,
            "status": "success",
            "metadata": {},
            "error": None,
            "schema_version": 1,
        }
        data.update(fields)
        return TraceEvent.model_validate(data)

    return [
        ev("run_started", component="demo", metadata={"test": "refund", "iteration": 1}),
        ev(
            "tool_call_started",
            tool=ToolCall(name="refund_order", arguments={"order_id": "ORD-1"}, call_id="c1"),
            status=EventStatus.PENDING,
        ),
        ev(
            "tool_call_completed",
            tool=ToolCall(name="refund_order", arguments={"order_id": "ORD-1"}, call_id="c1"),
            result={"refunded": True},
        ),
        ev(
            "tool_call_started",
            tool=ToolCall(name="send_email", arguments={"to": "x@y"}, call_id="c2"),
            status=EventStatus.PENDING,
        ),
        ev(
            "tool_call_completed",
            tool=ToolCall(name="send_email", arguments={"to": "x@y"}, call_id="c2"),
            result={"queued": True},
            status=EventStatus.MOCKED,
        ),
        ev("run_completed", component="demo"),
    ]


def test_session_serves_answers_fifo_per_tool_name() -> None:
    session = ReplaySession(_session_test_events(), source="demo.jsonl")
    first = session.serve("refund_order", {"order_id": "ORD-1"})
    assert first is not None and first.result == {"refunded": True}
    assert not first.args_differ
    email = session.serve("send_email", {"to": "x@y"})
    assert email is not None and email.result == {"queued": True}
    # Queue consumed: a second call to a name with only one recorded answer is
    # not in the recording.
    assert session.serve("refund_order", {"order_id": "ORD-1"}) is None
    assert session.serve("shell_exec", {"command": "ls"}) is None


def test_session_marks_argument_differences() -> None:
    session = ReplaySession(_session_test_events(), source="demo.jsonl")
    changed = session.serve("refund_order", {"order_id": "ORD-2"})
    assert changed is not None and changed.args_differ


def test_session_extracts_the_recorded_test_id() -> None:
    assert extract_test_id(_session_test_events()) == "refund"
    assert extract_test_id([]) is None


# -- end to end across the CLI -------------------------------------------------


def test_record_replay_diff_round_trip_is_unchanged(tmp_path: Path) -> None:
    root = _project(tmp_path, calls=1)

    recorded = runner.invoke(app, ["record", "--root", str(root), "--name", "refund"], catch_exceptions=False)
    assert recorded.exit_code == 0
    artifact = root / ".agentci" / "traces" / "refund.jsonl"
    assert artifact.is_file()
    assert "refund_order" in artifact.read_text(encoding="utf-8")
    assert "schema_version" in artifact.read_text(encoding="utf-8")

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


def test_replay_catches_an_agent_that_now_refunds_twice(tmp_path: Path) -> None:
    original = _project(tmp_path, calls=1)
    recorded = runner.invoke(app, ["record", "--root", str(original), "--name", "refund"], catch_exceptions=False)
    assert recorded.exit_code == 0
    artifact = original / ".agentci" / "traces" / "refund.jsonl"

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
    text = replay_artifact.read_text(encoding="utf-8")
    lines = [json.loads(line) for line in text.splitlines()]
    denied = [
        e
        for e in lines
        if e.get("type") == "tool_call_completed"
        and e.get("status") == "denied"
        and e.get("metadata", {}).get("replay") == "divergent"
    ]
    assert denied, "the second refund must be recorded as a replay divergence"

    diffed = runner.invoke(
        app,
        ["diff", str(artifact), str(replay_artifact)],
        catch_exceptions=False,
    )
    assert diffed.exit_code == 1
    assert "BEHAVIOR: CHANGED" in diffed.stdout
    assert "refund_order" in diffed.stdout


def test_record_rejects_an_unsafe_artifact_name(tmp_path: Path) -> None:
    root = _project(tmp_path, calls=1)
    result = runner.invoke(app, ["record", "--root", str(root), "--name", "../../evil"], catch_exceptions=False)
    assert result.exit_code == 2
    assert "invalid trace artifact name" in result.output


def test_replay_rejects_a_missing_artifact(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["replay", "--root", str(tmp_path), str(tmp_path / "nope.jsonl")],
        catch_exceptions=False,
    )
    assert result.exit_code == 3
    assert "no trace artifact" in result.output


def test_diff_rejects_a_missing_artifact(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["diff", str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl")],
        catch_exceptions=False,
    )
    assert result.exit_code == 3
