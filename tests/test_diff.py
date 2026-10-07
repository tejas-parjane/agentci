"""Tests for ``agentci diff`` and the structural trace comparison it runs.

Behavioral diffing has to tread a line the spec draws: identity and timing are
*recorded* values, never *behavior*. A diff that said "CHANGED" because the
second run was 12ms slower would be noise, not signal. These tests pin where the
line is: the behavioral contract is kinds, order, tool identity, arguments, and
statuses; measurements (latency, cost, tokens) ride along separately.
"""

from __future__ import annotations

from agentci.core.diff import behavior_tokens, trace_diff
from agentci.core.trace import TraceEvent

MODEL = "openai/gpt-5-mini"


def ev(type_: str, **fields: object) -> TraceEvent:
    data: dict[str, object] = {
        "run_id": "run_root",
        "event_id": fields.pop("event_id", "evt_x"),
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


def tool(name: str, args: dict[str, object] | None = None) -> dict[str, object]:
    return {"name": name, "arguments": dict(args or {}), "call_id": "call_x"}


def _traces(*calls: tuple[str, dict[str, object]]) -> list[TraceEvent]:
    returned: list[TraceEvent] = [ev("run_started", component="support")]
    for name, args in calls:
        returned.append(
            ev("tool_call_started", tool=tool(name, args), status="pending")
        )
        returned.append(
            ev("tool_call_completed", tool=tool(name, args), result={"ok": True})
        )
    returned.append(ev("run_completed", component="support"))
    return returned


def test_identical_traces_are_unchanged() -> None:
    result = trace_diff(
        _traces(("lookup_order", {"order_id": "ORD-1"})),
        _traces(("lookup_order", {"order_id": "ORD-1"})),
        a_label="a.jsonl",
        b_label="b.jsonl",
    )
    assert not result.behavior_changed
    assert result.a_label == "a.jsonl" and result.b_label == "b.jsonl"


def test_an_added_call_is_behavior_change() -> None:
    base = _traces(("lookup_customer", {"customer_id": "C-1"}))
    head = _traces(("lookup_customer", {"customer_id": "C-1"}), ("send_email", {"to": "x@y"}))
    result = trace_diff(base, head, a_label="a.jsonl", b_label="b.jsonl")
    assert result.behavior_changed
    assert [t.name for t in result.additions] == ["send_email"]
    assert result.removals == []
    assert result.reorders == []


def test_a_removed_call_is_behavior_change() -> None:
    base = _traces(("refund_order", {"order_id": "ORD-1"}), ("lookup_customer", {"customer_id": "C-1"}))
    head = _traces(("lookup_customer", {"customer_id": "C-1"}))
    result = trace_diff(base, head, a_label="a.jsonl", b_label="b.jsonl")
    assert result.behavior_changed
    assert [t.name for t in result.removals] == ["refund_order"]


def test_changed_arguments_are_behavior_change() -> None:
    base = _traces(("refund_order", {"order_id": "ORD-1"}))
    head = _traces(("refund_order", {"order_id": "ORD-1", "amount": 49.99}))
    result = trace_diff(base, head, a_label="a.jsonl", b_label="b.jsonl")
    assert result.behavior_changed
    # Not a reorder: the same *call* with different arguments is a different line.
    assert result.reorders == []
    assert len(result.additions) == 1 and len(result.removals) == 1


def test_a_reordered_call_is_marked_moved_not_added() -> None:
    base = _traces(
        ("lookup_order", {"order_id": "ORD-1"}),
        ("send_email", {"to": "x@y"}),
    )
    head = _traces(
        ("send_email", {"to": "x@y"}),
        ("lookup_order", {"order_id": "ORD-1"}),
    )
    result = trace_diff(base, head, a_label="a.jsonl", b_label="b.jsonl")
    assert result.behavior_changed
    # The LCS keeps send_email aligned, so lookup_order is the moved call.
    assert [t.name for t in result.reorders] == ["lookup_order"]
    # The twin is a move, not a removal and an addition: nothing disappears.
    assert result.additions == [] and result.removals == []


def test_a_double_refund_surfaces_as_an_addition() -> None:
    base = _traces(("refund_order", {"order_id": "ORD-7781", "amount": 49.99}))
    head = _traces(
        ("refund_order", {"order_id": "ORD-7781", "amount": 49.99}),
        ("refund_order", {"order_id": "ORD-7781", "amount": 49.99}),
    )
    result = trace_diff(base, head, a_label="record.jsonl", b_label="replay.jsonl")
    assert result.behavior_changed
    assert [t.name for t in result.additions] == ["refund_order"]


def test_identity_and_timing_never_trip_the_behavior_line() -> None:
    base = _traces(("lookup_order", {"order_id": "ORD-1"}))
    head = _traces(("lookup_order", {"order_id": "ORD-1"}))
    # Same behavior, different ids, order, and duration: must read unchanged.
    head[0] = head[0].model_copy(update={"run_id": "run_other", "event_id": "evt_other"})
    head[1] = head[1].model_copy(update={"event_id": "evt_other_1"})
    head[-1] = head[-1].model_copy(update={"run_id": "run_other", "duration_ms": 9999})
    result = trace_diff(base, head, a_label="a.jsonl", b_label="b.jsonl")
    assert not result.behavior_changed


def test_model_and_approval_events_are_part_of_the_line() -> None:
    def with_model_and_approval(approved: bool) -> list[TraceEvent]:
        events: list[TraceEvent] = [ev("run_started", component="demo")]
        events.append(ev("model_call_started", component=MODEL, status="pending"))
        events.append(
            ev("approval_granted" if approved else "approval_denied", tool=tool("refund_order"))
        )
        events.append(ev("run_completed", component="demo"))
        return events

    result = trace_diff(
        with_model_and_approval(approved=False),
        with_model_and_approval(approved=True),
        a_label="a.jsonl",
        b_label="b.jsonl",
    )
    assert result.behavior_changed
    kinds = behavior_tokens(with_model_and_approval(approved=True))
    assert [t.kind for t in kinds] == ["model", "approval_granted"]


def test_rendered_diff_names_the_behavior_and_its_measurements() -> None:
    base = _traces(("lookup_order", {"order_id": "ORD-1"}))
    head = _traces(("lookup_order", {"order_id": "ORD-1"}), ("send_email", {"to": "x@y"}))
    result = trace_diff(base, head, a_label="a.jsonl", b_label="b.jsonl")
    text = result.render()
    assert "BEHAVIOR: CHANGED" in text
    assert "1 addition(s)" in text
    assert "added:" in text and "send_email" in text


def test_json_shape_diffs_are_stable() -> None:
    result = trace_diff(
        _traces(("lookup_order", {"order_id": "ORD-1"})),
        _traces(("lookup_order", {"order_id": "ORD-1"}), ("send_email", {"to": "x@y"})),
        a_label="base.jsonl",
        b_label="head.jsonl",
    )
    payload = result.to_dict()
    assert payload["behavior"] == "changed"
    assert payload["counts"]["tool_calls"] == [1, 2]
    assert payload["measurements"]["tokens"][0] >= 0
    assert payload["base"] == "base.jsonl" and payload["head"] == "head.jsonl"
