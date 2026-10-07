"""Conformance suite for the AgentCI Trace Specification v1.

Locks `docs/specification/trace-v1.md`: the closed event grammar, the canonical
example (which this suite mirrors as data), the on-disk artifact contract, and
the redaction boundary. A change that alters any of these is a *spec change*,
not a code tweak, and requires a version bump plus a successor document.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentci.__about__ import TRACE_SCHEMA_VERSION
from agentci.core.recorder import TraceRecorder
from agentci.core.redaction import Redactor
from agentci.core.storage import RunStore
from agentci.core.trace import EventStatus, EventType, TraceEvent

# -- canonical example (mirrors docs/specification/trace-v1.md §11) -----------


def _event(*, event_id: str, type_: str, timestamp: str, **fields: Any) -> dict[str, Any]:
    full = {
        "run_id": "run_canonical",
        "event_id": event_id,
        "type": type_,
        "timestamp": timestamp,
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
    full.update(fields)
    return full


CANONICAL: list[dict[str, Any]] = [
    _event(
        event_id="evt_01",
        type_="run_started",
        timestamp="2026-10-07T12:00:00Z",
        component="demo",
        metadata={"test": "t_refund", "iteration": 1},
    ),
    _event(
        event_id="evt_02",
        type_="model_call_started",
        timestamp="2026-10-07T12:00:00Z",
        parent_id="evt_01",
        component="openai/gpt-4o-mini",
        status="pending",
        metadata={"model": "openai/gpt-4o-mini", "provider": "openai", "prompt_chars": 42},
    ),
    _event(
        event_id="evt_03",
        type_="model_call_completed",
        timestamp="2026-10-07T12:00:00Z",
        parent_id="evt_01",
        component="openai/gpt-4o-mini",
        result='{"tool":"lookup_order"}',
        usage={"prompt_tokens": 25, "completion_tokens": 17, "total_tokens": 42},
        cost_usd=0.00041,
        duration_ms=812,
    ),
    _event(
        event_id="evt_04",
        type_="tool_call_started",
        timestamp="2026-10-07T12:00:01Z",
        parent_id="evt_01",
        tool={"name": "lookup_order", "arguments": {"order_id": "ORD-7781"}, "call_id": "call_a"},
        status="pending",
    ),
    _event(
        event_id="evt_05",
        type_="tool_call_completed",
        timestamp="2026-10-07T12:00:01Z",
        parent_id="evt_01",
        tool={"name": "lookup_order", "arguments": {"order_id": "ORD-7781"}, "call_id": "call_a"},
        result={"status": "shipped", "customer": "[REDACTED:email]"},
        duration_ms=2,
    ),
    _event(
        event_id="evt_06",
        type_="retrieval_started",
        timestamp="2026-10-07T12:00:01Z",
        parent_id="evt_01",
        component="faq",
        status="pending",
        metadata={"source": "faq/refunds.md", "query": "can I refund a shipped order"},
    ),
    _event(
        event_id="evt_07",
        type_="retrieval_completed",
        timestamp="2026-10-07T12:00:01Z",
        parent_id="evt_01",
        component="faq",
        result={"document_count": 2},
        duration_ms=41,
    ),
    _event(
        event_id="evt_08",
        type_="approval_requested",
        timestamp="2026-10-07T12:00:02Z",
        parent_id="evt_01",
        tool={"name": "issue_refund", "arguments": {}, "call_id": "call_b"},
        status="pending",
    ),
    _event(
        event_id="evt_09",
        type_="approval_denied",
        timestamp="2026-10-07T12:00:02Z",
        parent_id="evt_01",
        tool={"name": "issue_refund", "arguments": {}, "call_id": "call_b"},
        result={"reason": "approval_required"},
        status="denied",
    ),
    _event(
        event_id="evt_10",
        type_="memory_read",
        timestamp="2026-10-07T12:00:02Z",
        parent_id="evt_01",
        result={"key": "refund_policy", "value": "refunds require manager approval"},
        metadata={"key": "refund_policy"},
    ),
    _event(
        event_id="evt_11",
        type_="tool_call_completed",
        timestamp="2026-10-07T12:00:03Z",
        parent_id="evt_01",
        tool={"name": "notify_manager", "arguments": {"order_id": "ORD-7781"}, "call_id": "call_c"},
        result={"ok": True},
        duration_ms=1,
        status="mocked",
    ),
    _event(
        event_id="evt_12",
        type_="run_completed",
        timestamp="2026-10-07T12:00:03Z",
        component="demo",
        duration_ms=3,
    ),
]


# -- the closed grammar --------------------------------------------------------


def test_the_event_grammar_is_a_closed_set() -> None:
    assert set(EventType) == {
        EventType.RUN_STARTED,
        EventType.MODEL_CALL_STARTED,
        EventType.MODEL_CALL_COMPLETED,
        EventType.TOOL_CALL_STARTED,
        EventType.TOOL_CALL_COMPLETED,
        EventType.RETRIEVAL_STARTED,
        EventType.RETRIEVAL_COMPLETED,
        EventType.MEMORY_READ,
        EventType.MEMORY_WRITE,
        EventType.APPROVAL_REQUESTED,
        EventType.APPROVAL_GRANTED,
        EventType.APPROVAL_DENIED,
        EventType.ERROR,
        EventType.RUN_COMPLETED,
    }


def test_the_status_set_is_a_closed_set() -> None:
    assert set(EventStatus) == {
        EventStatus.PENDING,
        EventStatus.SUCCESS,
        EventStatus.ERROR,
        EventStatus.DENIED,
        EventStatus.MOCKED,
    }


def test_schema_version_is_pinned_to_one() -> None:
    assert TRACE_SCHEMA_VERSION == 1


# -- the canonical example parses byte-for-byte --------------------------------


def test_each_canonical_event_round_trips_exactly() -> None:
    for line in CANONICAL:
        event = TraceEvent.model_validate(line)
        assert event.to_dict() == line


def test_an_unknown_field_is_rejected() -> None:
    """The grammar is closed in both directions: no silent widening of v1."""
    bogus = dict(CANONICAL[0], wat=1)
    with pytest.raises(ValueError, match="extra"):
        TraceEvent.model_validate(bogus)


# -- the artifact survives the store -------------------------------------------


def test_the_canonical_trace_survives_the_store(tmp_path: Path) -> None:
    store = RunStore(root=tmp_path / ".agentci", redactor=Redactor(enabled=False))
    events = [TraceEvent.model_validate(line) for line in CANONICAL]

    stored = store.save_trace("run_canonical", events)
    assert stored is not None and stored.trace_path is not None

    text = stored.trace_path.read_text(encoding="utf-8")
    lines = [json.loads(line) for line in text.splitlines()]
    assert len(lines) == len(CANONICAL)
    assert all(line["schema_version"] == 1 for line in lines)
    # Emission order is the ordering contract: it survives the round trip.
    expected_types = [line["type"] for line in CANONICAL]
    assert [line["type"] for line in lines] == expected_types

    loaded = store.load_trace("run_canonical")
    assert [event.to_dict() for event in loaded] == CANONICAL


# -- redaction happens at the serialization boundary, not at recording ----------


def test_redaction_happens_at_the_serialization_boundary(tmp_path: Path) -> None:
    recorder = TraceRecorder("run_leak")
    recorder.start()
    with recorder.tool_call("lookup_order", {"order_id": "ORD-1"}) as call:
        call.result = {"customer": "ada@example.com"}
    recorder.complete()

    # In memory — what assertions see — the real value is intact (§9).
    completed = [e for e in recorder.events if e.type is EventType.TOOL_CALL_COMPLETED]
    assert completed and completed[0].result == {"customer": "ada@example.com"}

    # On disk — what replay loads and CI logs — it is scrubbed (§9).
    store = RunStore(root=tmp_path / ".agentci", redactor=Redactor(patterns=["email"]))
    stored = store.save_trace("run_leak", recorder.events)
    assert stored is not None and stored.trace_path is not None
    text = stored.trace_path.read_text(encoding="utf-8")
    assert "ada@example.com" not in text
    assert "[REDACTED:email]" in text

    loaded = store.load_trace("run_leak")
    restored = [e for e in loaded if e.type is EventType.TOOL_CALL_COMPLETED]
    assert restored and restored[0].result == {"customer": "[REDACTED:email]"}
