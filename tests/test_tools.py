"""Tests for the ToolRegistry invocation ladder, sync and async.

``ToolRegistry.invoke`` and ``ToolRegistry.ainvoke`` must make identical
decisions --- deny, approval, replay, mock, side-effect gate --- and differ only
in how a live implementation is executed: synchronously versus awaited. The
decision ladder lives in one private method, so the async twin cannot drift from
the sync behavior every existing adapter depends on.
"""

from __future__ import annotations

import asyncio

import pytest

from agentci.core.recorder import TraceRecorder
from agentci.core.replay import ReplaySession
from agentci.core.tools import MockEntry, ToolDecl, ToolRegistry
from agentci.core.trace import EventStatus, EventType, ToolCall, TraceEvent
from agentci.errors import PolicyViolationError, SideEffectBlocked


def _completed(recorder: TraceRecorder, name: str):
    """The single completion event for ``name``, failing loudly if ambiguous."""
    matches = [
        e
        for e in recorder.events
        if e.type is EventType.TOOL_CALL_COMPLETED and e.tool is not None and e.tool.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def _recorded_session() -> ReplaySession:
    """A recording containing exactly one answered ``refund_order`` call."""

    def ev(type_: str, **fields: object) -> TraceEvent:
        data: dict[str, object] = {
            "run_id": "run_r",
            "event_id": f"evt_{type_}",
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

    return ReplaySession(
        [
            ev(
                "tool_call_started",
                tool=ToolCall(name="refund_order", arguments={}, call_id="c1"),
                status=EventStatus.PENDING,
            ),
            ev(
                "tool_call_completed",
                tool=ToolCall(name="refund_order", arguments={}, call_id="c1"),
                result={"refunded": True},
            ),
        ],
        source="unit.jsonl",
    )


def test_denied_tool_denies_for_both_twins() -> None:
    with pytest.raises(PolicyViolationError):
        ToolRegistry(denied_tools={"refund"}).invoke("refund")
    with pytest.raises(PolicyViolationError):
        asyncio.run(ToolRegistry(denied_tools={"refund"}).ainvoke("refund"))


def test_approval_required_denies_for_both_twins() -> None:
    with pytest.raises(PolicyViolationError):
        ToolRegistry(approval_required={"refund"}).invoke("refund")
    with pytest.raises(PolicyViolationError):
        asyncio.run(ToolRegistry(approval_required={"refund"}).ainvoke("refund"))


def test_ainvoke_serves_a_config_mock() -> None:
    recorder = TraceRecorder("run_mock")
    recorder.start()
    registry = ToolRegistry(
        recorder=recorder,
        mocks={"ship": MockEntry(response={"sent": True})},
    )
    assert asyncio.run(registry.ainvoke("ship", {"to": "x"})) == {"sent": True}
    completed = _completed(recorder, "ship")
    assert completed.status is EventStatus.MOCKED
    assert completed.metadata["mock_source"] == "config"


def test_ainvoke_serves_replay_answers_and_refuses_divergence() -> None:
    recorder = TraceRecorder("run_replay")
    recorder.start()
    registry = ToolRegistry(recorder=recorder)
    registry.attach_replay(_recorded_session())

    assert asyncio.run(registry.ainvoke("refund_order", {})) == {"refunded": True}

    # The recording answered one call; the next refund is behavior the
    # recording never saw, so it is refused as a divergence rather than
    # silently answered like it happened.
    assert asyncio.run(registry.ainvoke("refund_order", {})) == {
        "reason": "not_in_recorded_trace"
    }
    denied = [e for e in recorder.events if e.status is EventStatus.DENIED]
    assert any(e.metadata.get("replay") == "divergent" for e in denied)


def test_ainvoke_executes_a_sync_live_call_within_a_trace() -> None:
    called: list[dict[str, object]] = []

    def refund(**kwargs: object) -> dict[str, object]:
        called.append(kwargs)
        return {"refunded": True, "cost_usd": 0.5}

    recorder = TraceRecorder("run_live")
    recorder.start()
    registry = ToolRegistry(
        recorder=recorder,
        declarations={"refund": ToolDecl(name="refund", live=refund)},
    )
    result = asyncio.run(registry.ainvoke("refund", {"amount": 10}))
    assert called == [{"amount": 10}]
    assert result == {"refunded": True, "cost_usd": 0.5}
    completed = _completed(recorder, "refund")
    assert completed.status is EventStatus.SUCCESS
    assert completed.cost_usd == 0.5


def test_ainvoke_awaits_an_async_live_implementation() -> None:
    async def refund(**kwargs: object) -> dict[str, object]:
        await asyncio.sleep(0)
        return {"refunded": True}

    recorder = TraceRecorder("run_alive")
    recorder.start()
    registry = ToolRegistry(
        recorder=recorder,
        declarations={"refund": ToolDecl(name="refund", live=refund)},
    )
    assert asyncio.run(registry.ainvoke("refund")) == {"refunded": True}
    completed = _completed(recorder, "refund")
    assert completed.status is EventStatus.SUCCESS


def test_side_effect_gate_blocks_both_twins() -> None:
    decl = ToolDecl(name="wipe", live=lambda: None, side_effect=True)
    for executor in (
        lambda r: r.invoke("wipe"),
        lambda r: asyncio.run(r.ainvoke("wipe")),
    ):
        with pytest.raises(SideEffectBlocked):
            executor(ToolRegistry(declarations={"wipe": decl}))


def test_mock_argument_mismatch_raises_for_ainvoke() -> None:
    registry = ToolRegistry(
        mocks={
            "lookup": MockEntry(
                response={}, match_arguments=True, arguments={"id": "1"}
            )
        }
    )
    with pytest.raises(PolicyViolationError):
        asyncio.run(registry.ainvoke("lookup", {"id": "2"}))
    assert asyncio.run(registry.ainvoke("lookup", {"id": "1"})) == {}


def test_ainvoke_reports_a_missing_declaration_like_invoke() -> None:
    for executor in (
        lambda r: r.invoke("ghost"),
        lambda r: asyncio.run(r.ainvoke("ghost")),
    ):
        with pytest.raises(PolicyViolationError):
            executor(ToolRegistry())
