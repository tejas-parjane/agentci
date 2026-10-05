"""Tests for artifact persistence: round-trips, redaction, and path safety.

The round-trip matters most. A trace is written by AgentCI and read back by AgentCI
(``agentci replay``, ``agentci baseline``), and it is serialized with a
``schema_version`` that ``extra="forbid"`` would reject. That combination once made
every trace unreadable while writing perfectly valid files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentci.core.recorder import TraceRecorder
from agentci.core.redaction import Redactor
from agentci.core.result import AgentResult
from agentci.core.storage import (
    RESULT_FILENAME,
    TRACE_FILENAME,
    RunStore,
    StorageError,
    safe_join,
)


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(root=tmp_path / ".agentci")


@pytest.fixture
def sample_events() -> list:
    recorder = TraceRecorder("run_sample")
    recorder.start(test="t_example", iteration=1)
    with recorder.tool_call("lookup_ticket", {"ticket": "T-1"}) as call:
        call.result = {"status": "open"}
        call.usage = {"prompt_tokens": 11, "completion_tokens": 4}
        call.cost_usd = 0.0005
    recorder.complete()
    return recorder.events


def test_trace_round_trips_exactly(store: RunStore, sample_events: list) -> None:
    store.save_trace("run_sample", sample_events)
    loaded = store.load_trace("run_sample")

    assert len(loaded) == len(sample_events)
    for original, restored in zip(sample_events, loaded, strict=True):
        assert restored.event_id == original.event_id
        assert restored.type is original.type
        assert restored.parent_id == original.parent_id
        assert restored.timestamp == original.timestamp
        assert restored.to_dict() == original.to_dict()


def test_trace_is_written_as_one_event_per_line(store: RunStore, sample_events: list) -> None:
    """JSONL keeps a partially written file parseable up to the last good line."""
    stored = store.save_trace("run_sample", sample_events)
    assert stored is not None
    assert stored.trace_path is not None
    assert stored.trace_path.name == TRACE_FILENAME

    lines = stored.trace_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(sample_events)
    assert all(json.loads(line) for line in lines)


def test_missing_trace_reads_as_empty(store: RunStore) -> None:
    assert store.load_trace("run_never_ran") == []


def test_truncated_final_line_keeps_earlier_events(store: RunStore, sample_events: list) -> None:
    """An interrupted run still yields the evidence that was flushed."""
    stored = store.save_trace("run_sample", sample_events)
    assert stored is not None and stored.trace_path is not None

    # The previous line ended cleanly, so the fragment lands on a line of its own
    # and only that line is lost -- exactly what a mid-write power cut looks like.
    with stored.trace_path.open("a", encoding="utf-8") as handle:
        handle.write('{"run_id": "run_sample", "event_id": "evt_')

    loaded = store.load_trace("run_sample")
    assert len(loaded) == len(sample_events)
    assert [e.event_id for e in loaded] == [e.event_id for e in sample_events]


def test_corrupt_middle_line_is_reported_not_swallowed(
    store: RunStore, sample_events: list
) -> None:
    """Silently returning a short trace would look like a successful replay."""
    stored = store.save_trace("run_sample", sample_events)
    assert stored is not None and stored.trace_path is not None

    lines = stored.trace_path.read_text(encoding="utf-8").splitlines()
    lines[1] = "{not json at all"
    stored.trace_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(StorageError, match="not valid JSON"):
        store.load_trace("run_sample")


def test_schema_version_mismatch_is_reported(store: RunStore, sample_events: list) -> None:
    """A future schema must be refused loudly, not parsed on a guess."""
    stored = store.save_trace("run_sample", sample_events)
    assert stored is not None and stored.trace_path is not None

    lines = stored.trace_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["schema_version"] = 9999
    lines[0] = json.dumps(first)
    stored.trace_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(StorageError, match="schema version"):
        store.load_trace("run_sample")


def test_secrets_are_redacted_on_the_way_out(tmp_path: Path) -> None:
    """Artifacts are the thing most likely to be uploaded to CI, so they are the
    thing that must never contain a live credential."""
    store = RunStore(
        root=tmp_path / ".agentci",
        redactor=Redactor(secrets=["sk-live-supersecret"]),
    )
    recorder = TraceRecorder("run_leak")
    recorder.start()
    with recorder.tool_call("pay", {"token": "sk-live-supersecret"}) as call:
        call.result = "sent sk-live-supersecret"
    recorder.complete()

    stored = store.save_trace("run_leak", recorder.events)
    assert stored is not None and stored.trace_path is not None
    raw = stored.trace_path.read_text(encoding="utf-8")

    assert "sk-live-supersecret" not in raw
    # The built-in credential patterns run before literal secret substitution, so
    # an `sk-live-...` value is caught by shape and labelled as an api_key.
    assert "[REDACTED:api_key]" in raw


def test_non_credential_shaped_secret_is_still_redacted(tmp_path: Path) -> None:
    """A secret that no pattern recognises must still be caught by exact match."""
    store = RunStore(
        root=tmp_path / ".agentci",
        redactor=Redactor(secrets=["hunter2-do-not-log"]),
    )
    recorder = TraceRecorder("run_leak2")
    recorder.start()
    recorder.complete()

    stored = store.save_trace("run_leak2", recorder.events)
    assert stored is not None and stored.trace_path is not None
    recorder2 = TraceRecorder("run_leak2")
    recorder2.start(metadata={"note": "hunter2-do-not-log"})
    recorder2.complete()
    store.save_trace("run_leak2", recorder2.events)

    raw = stored.trace_path.read_text(encoding="utf-8")
    assert "hunter2-do-not-log" not in raw
    assert "[REDACTED:secret]" in raw


def test_result_round_trips_and_redacts(tmp_path: Path) -> None:
    store = RunStore(
        root=tmp_path / ".agentci", redactor=Redactor(secrets=["sk-live-abc"])
    )
    result = AgentResult(output_text="token sk-live-abc", metadata={"key": "sk-live-abc"})

    stored = store.save_result("run_res", result)
    assert stored is not None and stored.result_path is not None
    assert stored.result_path.name == RESULT_FILENAME

    loaded = store.load_result("run_res")
    assert loaded["run_id"] == "run_res"
    assert loaded["trace_ref"] == TRACE_FILENAME
    assert "sk-live-abc" not in json.dumps(loaded)


def test_missing_result_reads_as_empty(store: RunStore) -> None:
    assert store.load_result("run_never_ran") == {}


def test_recording_disabled_writes_nothing(store: RunStore, sample_events: list) -> None:
    store.config = store.config.model_copy(update={"record_traces": False})

    assert store.save_trace("run_sample", sample_events) is None
    assert store.save_result("run_sample", AgentResult(output_text="x")) is None


def test_trace_is_capped_by_max_trace_events(store: RunStore, sample_events: list) -> None:
    store.config = store.config.model_copy(update={"max_trace_events": 2})

    store.save_trace("run_sample", sample_events)

    assert len(store.load_trace("run_sample")) == 2


def test_report_is_written_with_a_stable_latest_copy(store: RunStore) -> None:
    written = store.save_report("run_rep", '{"ok": true}', "# Report")

    names = {path.name for path in written}
    assert "report.json" in names
    assert "report.md" in names
    latest = store.latest_report_dir / "report.json"
    assert latest.is_file()
    assert json.loads(latest.read_text(encoding="utf-8")) == {"ok": True}


def test_latest_is_not_written_for_a_missing_markdown(store: RunStore) -> None:
    written = store.save_report("run_rep", '{"ok": true}', None)

    assert all("report.md" not in path.name for path in written)
    assert not (store.latest_report_dir / "report.md").exists()


def test_list_runs_is_newest_first(store: RunStore, sample_events: list) -> None:
    store.save_trace("run_a", sample_events)
    store.save_trace("run_b", sample_events)

    listed = store.list_runs()
    assert set(listed) == {"run_a", "run_b"}
    assert listed[0] == "run_b"


def test_prune_respects_retention(store: RunStore, sample_events: list) -> None:
    store.save_trace("run_old", sample_events)
    store.config = store.config.model_copy(update={"retention_days": 0})
    assert store.prune() == [], "retention 0 must mean keep everything"

    store.config = store.config.model_copy(update={"retention_days": 30})
    assert store.prune() == []


def test_run_id_rejects_path_traversal(store: RunStore) -> None:
    """Run ids become directory names, so `..` must never survive validation."""
    for hostile in ("../../etc", "..", "a/b", "run id", "run\\id", ""):
        with pytest.raises(StorageError):
            store.run_dir(hostile)


def test_run_id_rejects_absurd_length(store: RunStore) -> None:
    with pytest.raises(StorageError, match="too long"):
        store.run_dir("r" * 200)


@pytest.mark.parametrize("parts", [("..",), ("runs", "..", ".."), ("a", "..", "..", "b")])
def test_safe_join_refuses_escapes(tmp_path: Path, parts: tuple[str, ...]) -> None:
    with pytest.raises(StorageError, match="outside the storage root"):
        safe_join(tmp_path / "root", *parts)


def test_safe_join_allows_nested_paths(tmp_path: Path) -> None:
    joined = safe_join(tmp_path / "root", "runs", "run_1")
    assert joined == tmp_path / "root" / "runs" / "run_1"
