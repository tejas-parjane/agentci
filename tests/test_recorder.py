"""Behavioral tests for the trace recorder and its budget guards.

The recorder is the only writer of :class:`TraceEvent`, so its guarantees --
true event order, faithful parent linkage, and budgets that stop a runaway agent
rather than merely reporting one afterwards -- are what the rest of the report is
built on.
"""

from __future__ import annotations

import time

import pytest

from agentci.core.recorder import TraceRecorder
from agentci.core.trace import EventStatus, EventType
from agentci.errors import DeadlineExceeded, StepLimitExceeded


def types_of(recorder: TraceRecorder) -> list[EventType]:
    return [event.type for event in recorder.events]


def only(recorder: TraceRecorder, event_type: EventType):
    """The single event of ``event_type``; fails loudly if there are several."""
    matches = [e for e in recorder.events if e.type is event_type]
    assert len(matches) == 1, f"expected 1 {event_type}, got {len(matches)}"
    return matches[0]


def test_start_and_complete_bracket_the_run() -> None:
    recorder = TraceRecorder("run_bracket")
    recorder.start(test="t_example")
    recorder.complete()

    started = only(recorder, EventType.RUN_STARTED)
    assert started.metadata["test"] == "t_example"
    assert started.component is None

    completed = only(recorder, EventType.RUN_COMPLETED)
    assert completed.duration_ms is not None


def test_tool_call_records_arguments_result_and_usage() -> None:
    recorder = TraceRecorder("run_tool")
    recorder.start()

    with recorder.tool_call("lookup_ticket", {"ticket": "T-1"}) as call:
        call.result = {"status": "open"}
        call.usage = {"prompt_tokens": 7, "completion_tokens": 3}
        call.cost_usd = 0.002

    started = only(recorder, EventType.TOOL_CALL_STARTED)
    assert started.tool is not None
    assert started.tool.name == "lookup_ticket"
    assert started.tool.arguments == {"ticket": "T-1"}
    # Pending until the context manager completes it.
    assert started.status is EventStatus.PENDING

    completed = only(recorder, EventType.TOOL_CALL_COMPLETED)
    assert completed.tool is not None
    assert completed.tool.name == "lookup_ticket"
    assert completed.result == {"status": "open"}
    assert completed.status is EventStatus.SUCCESS
    assert completed.usage is not None
    assert completed.usage.prompt_tokens == 7
    assert completed.cost_usd == 0.002


def test_tool_call_nests_children_under_the_call() -> None:
    """Parent linkage is implicit: a nested event attaches to the open call."""
    recorder = TraceRecorder("run_nest")
    recorder.start()

    with recorder.tool_call("outer"), recorder.model_call("gpt-test"):
        pass

    model_started = only(recorder, EventType.MODEL_CALL_STARTED)
    outer_started = only(recorder, EventType.TOOL_CALL_STARTED)
    assert model_started.parent_id == outer_started.event_id


def test_nesting_unwinds_so_later_siblings_are_orphans() -> None:
    """A leaked parent entry would silently reparent every subsequent event."""
    recorder = TraceRecorder("run_unwind")
    recorder.start()

    with recorder.tool_call("first"):
        pass
    with recorder.tool_call("second"):
        pass

    started = [e for e in recorder.events if e.type is EventType.TOOL_CALL_STARTED]
    assert len(started) == 2
    assert started[1].parent_id is None, "second call must not inherit first call as parent"
    assert started[1].tool is not None
    assert started[1].tool.name == "second"

    completed = [e for e in recorder.events if e.type is EventType.TOOL_CALL_COMPLETED]
    assert all(e.parent_id is None for e in completed)


def test_exception_inside_context_still_emits_an_error_event() -> None:
    """The failing call must remain visible in the trace, not vanish on unwind."""
    recorder = TraceRecorder("run_exc")
    recorder.start()

    with pytest.raises(RuntimeError), recorder.tool_call("flaky"):
        raise RuntimeError("provider unavailable")

    errors = [e for e in recorder.events if e.type is EventType.ERROR]
    assert len(errors) == 1
    assert "provider unavailable" in (errors[0].error or "")


def test_step_limit_stops_the_run_at_exactly_max_steps() -> None:
    """The check precedes the start event, so the trace never overshoots."""
    recorder = TraceRecorder("run_steps", max_steps=3)
    recorder.start()

    for _ in range(3):
        with recorder.tool_call("loop"):
            pass

    assert recorder.step_count == 3

    with pytest.raises(StepLimitExceeded), recorder.tool_call("one too many"):
        pass

    assert recorder.step_count == 3, "the rejected call must not appear in the trace"


def test_deadline_raises_once_the_budget_is_gone() -> None:
    """A negative budget cannot be used: it would trip before the run even starts."""
    recorder = TraceRecorder("run_deadline", max_duration_ms=1)
    recorder.start(test="t")

    time.sleep(0.005)

    with pytest.raises(DeadlineExceeded):
        recorder.complete()


def test_step_limit_and_deadline_are_independent() -> None:
    """A generous deadline must not mask an exhausted step budget."""
    recorder = TraceRecorder("run_both", max_steps=1, max_duration_ms=60_000)
    recorder.start()

    with recorder.tool_call("allowed"):
        pass

    with pytest.raises(StepLimitExceeded), recorder.tool_call("blocked"):
        pass


def test_model_call_records_prompt_length_without_the_prompt() -> None:
    """The trace keeps prompt *shape*, never prompt text, to limit leak surface."""
    recorder = TraceRecorder("run_model")
    recorder.start()

    with recorder.model_call("gpt-test", prompt="secret-looking user content"):
        pass

    started = only(recorder, EventType.MODEL_CALL_STARTED)
    assert started.metadata["model"] == "gpt-test"
    assert started.metadata["prompt_chars"] == len("secret-looking user content")
    assert "secret-looking user content" not in str(started.metadata)


def test_retrieval_reports_document_count() -> None:
    recorder = TraceRecorder("run_retrieval")
    recorder.start()

    with recorder.retrieval("kb", query="refund policy") as retrieval:
        retrieval.documents = [{"id": 1}, {"id": 2}]

    completed = only(recorder, EventType.RETRIEVAL_COMPLETED)
    assert completed.metadata["document_count"] == 2
    assert completed.metadata["source"] == "kb"


def test_error_event_is_marked_as_error_status() -> None:
    recorder = TraceRecorder("run_error")

    event = recorder.error("something broke")

    assert event.type is EventType.ERROR
    assert event.status is EventStatus.ERROR
    assert event.error == "something broke"


def test_run_ids_are_stamped_on_every_event() -> None:
    """Events must be attributable when several runs interleave in one artifact."""
    recorder = TraceRecorder("run_stamp")
    recorder.start()
    with recorder.tool_call("t"):
        pass
    recorder.complete()

    assert {e.run_id for e in recorder.events} == {"run_stamp"}


def test_elapsed_ms_is_measured_from_construction() -> None:
    recorder = TraceRecorder("run_elapsed")
    assert recorder.elapsed_ms >= 0.0
    assert recorder.step_count == 0
