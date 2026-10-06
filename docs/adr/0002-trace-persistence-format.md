# ADR-0002: Trace persistence format

Status: Accepted

## Context

A run's evidence has to be readable by tools that are not AgentCI: `jq` in a CI
log, a diff in a pull request, a script that scans every run for leaked
credentials. It also has to be writable incrementally, because a test that hits a
step limit or a deadline is aborted mid-run and the events recorded so far are
the only explanation of why.

A single JSON document fails the first property when it is truncated and the
second property entirely: nothing can read a `report.json` that was never closed.

## Decision

Persist each run's trace as **JSON Lines** — `trace.jsonl`, one `TraceEvent` per
line, in emission order — under `.agentci/runs/<run_id>/`.

- Append-only, so an interrupted run leaves a readable prefix rather than an
  unreadable file.
- Each line is independently parseable, so a scanner can process a run without
  materializing it.
- `result.json` and `report.json` remain whole documents, since they are only
  written at the end and are what CI reads as a unit.

Every event carries a `schema_version`. It is checked on load, not on save: the
value describes what a reader should expect, and a mismatch is an error that
names the two versions rather than a silent parse failure.

Truncation of the final line is tolerated, because that is precisely what an
interrupted run leaves. Malformed JSON in the middle of a file, an invalid event,
or a different schema version raise `StorageError`. Conflating "the run was
killed" with "this file is corrupt" would make a crash indistinguishable from
tampering.

## Consequences

- Consumers parse line by line and can resume after a partial line.
- `json.dumps` per event, not one large dump, so memory stays flat.
- Round-tripping is a tested property: AgentCI writes a trace and AgentCI reads
  it back under `extra="forbid"`, which caught a real bug where a computed
  `schema_version` field was emitted but not accepted on the way in.
- Gzip or a columnar format would compress better; rejected for v0.1 because
  inspectability was the point.
