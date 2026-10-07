"""Recorded trace artifacts and deterministic replay.

The Trace Specification v1 defines the file format; this module owns the
artifact lifecycle built on it: ``agentci record`` writes a standalone v1
artifact, ``agentci replay`` re-executes the same scenario against the *current*
agent with every tool call answered from that artifact, and ``agentci diff``
compares two artifacts (see ``core/diff.py``).

Replay determinism contract (§8 of the spec): the artifact is the behavioral
record. Replay binds each tool invocation the agent now performs to the
recorded answer for the same tool name, in recorded order; if the agent calls a
tool the recording never answered, the harness refuses the call (recorded as a
``denied`` outcome + a ``replay: divergent`` marker) and hands the agent a
refusal — the agent may continue, but the replay run *fails* and the divergent
call is visible in the exported trace for ``diff``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentci.core.redaction import Redactor
from agentci.core.trace import EventType, TraceEvent
from agentci.errors import ReplayError


def load_trace_file(path: Path) -> list[TraceEvent]:
    """Read a v1 trace artifact (JSONL) into validated events.

    Follows the persistence rules (ADR-0002): a truncated *final* line is
    tolerated, malformed JSON in the middle, an invalid event, or a version
    mismatch is raised loudly — silence would make a corrupt artifact look like
    a successful replay.
    """
    if not path.is_file():
        raise ReplayError(
            f"no trace artifact at {path}",
            hint="run `agentci record --name <scenario>` first",
        )
    from agentci.__about__ import TRACE_SCHEMA_VERSION

    lines = path.read_text(encoding="utf-8").splitlines()
    events: list[TraceEvent] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        is_last = index == len(lines) - 1
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            if is_last:
                break
            raise ReplayError(
                f"{path}: line {index + 1} is not valid JSON",
                hint="the artifact appears to be corrupt rather than truncated",
            ) from exc
        if isinstance(data, dict) and data.get("schema_version") != TRACE_SCHEMA_VERSION:
            raise ReplayError(
                f"{path}: trace schema version {data.get('schema_version')} cannot be "
                f"read by this version of AgentCI (expects {TRACE_SCHEMA_VERSION})",
                hint="re-record the trace, or upgrade AgentCI",
            )
        try:
            events.append(TraceEvent.model_validate(data))
        except ValueError as exc:
            if is_last:
                break
            raise ReplayError(
                f"{path}: line {index + 1} is not a valid trace event",
                hint="the artifact appears to be corrupt rather than truncated",
            ) from exc
    return events


def write_trace_file(path: Path, events: Sequence[TraceEvent], *, redactor: Redactor) -> None:
    """Write events as a v1 trace artifact (JSONL), redacted, exactly like the
    per-run ``trace.jsonl`` that the spec pins."""
    path.parent.mkdir(parents=True, exist_ok=True)
    scrubbed = redactor.redact_events(list(events))
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for event in scrubbed:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False, default=str) + "\n")


def extract_test_id(events: Sequence[TraceEvent]) -> str | None:
    """The test a recorded run belongs to, from the ``run_started`` metadata."""
    for event in events:
        if event.type is EventType.RUN_STARTED:
            test = event.metadata.get("test")
            if isinstance(test, str) and test:
                return test
    return None


@dataclass(frozen=True, slots=True)
class ReplayAnswer:
    """The recorded stand-in for one tool invocation."""

    result: Any
    args_differ: bool


@dataclass(slots=True)
class _Queued:
    result: Any
    recorded_args: dict[str, Any]


class ReplaySession:
    """Answers every tool the agent calls from a recorded trace, FIFO per name.

    A tool call consumes the next answer recorded for that tool name. A call the
    recording never answered returns ``None`` — the caller records it as a
    divergence rather than guessing a response.
    """

    def __init__(self, events: Sequence[TraceEvent], *, source: str = "") -> None:
        self.source = source
        self._answers: dict[str, list[_Queued]] = {}
        completed: dict[str, TraceEvent] = {}
        for event in events:
            if event.type is EventType.TOOL_CALL_COMPLETED and event.tool and event.tool.call_id:
                completed[event.tool.call_id] = event
        for event in events:
            if event.type is not EventType.TOOL_CALL_STARTED or event.tool is None:
                continue
            call = event.tool
            done = completed.get(call.call_id or "")
            self._answers.setdefault(call.name, []).append(
                _Queued(result=done.result if done is not None else None, recorded_args=dict(call.arguments))
            )

    def serve(self, name: str, arguments: Mapping[str, Any]) -> ReplayAnswer | None:
        queue = self._answers.get(name)
        if not queue:
            return None
        queued = queue.pop(0)
        return ReplayAnswer(
            result=queued.result,
            args_differ=dict(arguments) != queued.recorded_args,
        )

    def covers(self, name: str) -> bool:
        return bool(self._answers.get(name))


__all__ = [
    "ReplayAnswer",
    "ReplaySession",
    "extract_test_id",
    "load_trace_file",
    "write_trace_file",
]
