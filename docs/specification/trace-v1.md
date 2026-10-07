# AgentCI Trace Specification — v1

**Status:** enacted · implemented by AgentCI 0.1.0 · `schema_version: 1`

The trace is AgentCI's central data structure: every assertion except pure
output-text checks reads from it, policies evaluate it, and the release verdict
is a judgement over it. This document is the **normative contract** for the v1
trace: the stable, framework-neutral format that lives on disk as
`trace.jsonl` and is the interoperability boundary between an agent, its
recorded behavior, and everything that audits it.

It is the public form of the design recorded in [ADR-0002 (persistence)],
[ADR-0006 (telemetry)], and [ADR-0008 (redaction)], referenced from the model in
`src/agentci/core/trace.py`.

[ADR-0002 (persistence)]: ../adr/0002-trace-persistence-format.md
[ADR-0006 (telemetry)]: ../adr/0006-telemetry-defaults.md
[ADR-0008 (redaction)]: ../adr/0008-redaction-at-serialization.md

## 1. Scope and versioning

A **trace** is an ordered, append-only sequence of **events** describing one
agent run: what the agent tried, what the tools returned, what the model cost,
what policy enforced, and how it ended. A trace is the evidence behind a test.

Versioning rules:

- Every serialized event carries `schema_version`, currently **`1`**
  (`TRACE_SCHEMA_VERSION` in `src/agentci/__about__.py`).
- The value is checked **on load, not on save**: it tells a reader what to
  expect, and a mismatch is a loud `StorageError` that names both versions
  (ADR-0002), never a silent parse.
- A change that alters the meaning of a field, adds or removes an event type,
  or widens the closed grammar *requires* `schema_version: 2`. Additions that
  keep every reader honest are handled through the single sanctioned extension
  point, `metadata` (§7).

## 2. The artifact

- **Format:** JSON Lines — one JSON object per line, in emission order.
- **Location:** `.agentci/runs/<run_id>/trace.jsonl`.
- **Writing:** append-friendly and written incrementally, so an interrupted run
  leaves a readable prefix; memory stays flat (`json.dumps` per event).
- **Reading:** a truncated **final** line is tolerated — it is what a crash
  leaves — and everything before it is kept. Malformed JSON *in the middle*,
  an event that fails validation, or a version mismatch raise `StorageError`:
  "the run was killed" must never be indistinguishable from "this file is
  corrupt".

## 3. Event types (the closed set)

`type` is one of exactly these fourteen values (`EventType`). A completed step
is represented by a `*_started` / `*_completed` pair (for tools and model
calls) that share a `call_id`; consumers collapse the pair into one invocation
(`Trace.tool_calls()` does exactly this).

| type | meaning |
| --- | --- |
| `run_started` | the run began; the first event of a trace |
| `run_completed` | the run ended; normally the last event |
| `model_call_started` | the agent began a model invocation |
| `model_call_completed` | the model invocation returned (or failed) |
| `tool_call_started` | the agent invoked a tool |
| `tool_call_completed` | the tool invocation returned, was mocked, or was denied |
| `retrieval_started` | a retrieval (RAG/vector query) began |
| `retrieval_completed` | the retrieval returned |
| `memory_read` | the agent read a memory value |
| `memory_write` | the agent wrote a memory value |
| `approval_requested` | a human-approval step was requested |
| `approval_granted` | the approval was granted |
| `approval_denied` | the approval was denied |
| `error` | an error that aborted a step (budget, deadline, adapter failure) |

There is no `unknown` type and no free-form "kind". Third-party perspective is
carried in `component` and `metadata`, not by inventing event types (§7).

## 4. Outcome statuses (the closed set)

`status` is one of exactly these five values (`EventStatus`):

| status | meaning |
| --- | --- |
| `pending` | a `*_started` event acknowledging work whose completion is not yet recorded |
| `success` | the step completed normally |
| `error` | the step failed and aborted |
| `denied` | the step was refused by policy or by an approval denial |
| `mocked` | the step was served by a configured mock rather than executed |

`mocked` and `denied` are recorded **as outcomes**, not erased: a trace that
hides "the agent tried to call a side-effecting tool and was refused" would lie
about behavior, which is the one thing a regression check must never do.

## 5. Common fields

Every event is a JSON object with exactly this field set and no others
(`extra="forbid"` validation). Optional fields appear as `null` when absent.

| field | type | required | meaning |
| --- | --- | --- | --- |
| `run_id` | string | yes | the run the event belongs to |
| `event_id` | string | yes | per-event identifier |
| `type` | string | yes | one of §3 |
| `timestamp` | string (ISO-8601, UTC) | yes | wall-clock moment; for humans and serialization |
| `schema_version` | integer | yes | **1** in this spec |
| `parent_id` | string \| null | no | parent event id; includes nesting (`run_id`→work) |
| `component` | string \| null | no | the framework/source name (e.g. `openai/gpt-4o-mini`) |
| `tool` | object \| null | no | tool identity: `{name, arguments, call_id}` |
| `result` | any \| null | no | what a step returned |
| `usage` | object \| null | no | model tokens: `{prompt_tokens, completion_tokens, total_tokens}` |
| `cost_usd` | number \| null | no | known step cost in USD; `null` means unknown, never `0` |
| `duration_ms` | number \| null | no | step duration; **must** come from a monotonic clock (NTP can never make it negative) |
| `status` | string | yes | one of §4 |
| `metadata` | object | no | the extension point (§7) |
| `error` | string \| null | no | error message when `status == "error"` or `type == "error"` |

Timing contract: `timestamp` is wall-clock (suspend/resume-aware) and
`duration_ms` is monotonic-clock-derived, so the two can disagree after an NTP
step — that is by design.

Ordering contract: event order **in the array is emission order**. Consumers
must never re-sort by `timestamp`; two events can share a timestamp but order
in the file is the statement about sequencing.

## 6. Parentage and the pairing convention

- A child event's `parent_id` names its enclosing event's `event_id`. The
  recorder maintains an implicit context stack, so nested work — a tool call
  initiated inside a model stream — is reassembled without either side knowing
  the other's identifiers.
- A tool or model invocation emits a `*_started` (with `parent_id` of its
  enclosing scope) and a `*_completed` with the **same** `tool.call_id`. The
  completed event carries `result`, `usage`, `cost_usd`, and `duration_ms`.

## 7. The extension point

Events validate under `extra="forbid"`: an unknown field is a corrupt artifact,
not a "maybe forward-compatible" one — silent tolerance is how schemas rot and
how a regression silently stops being comparable.

The single sanctioned extension point is `metadata` — a free-form object that
adapters and recorders may fill (model name, prompt length, adapter version, a
tool's declared schema, ...). Metadata is **descriptive**, never load-bearing
for assertions: an assertion may read it, but the semantics of the core fields
in §5–§6 are normative and independent of any key inside `metadata`.

## 8. Determinism and the replay contract

A trace is split into two kinds of data, because replay and diff treat them
differently:

- **Identity and timing:** `run_id`, `event_id`, `timestamp`, `duration_ms`,
  and `cost_usd` are recorded values, not behavioral promises.
- **Behavior:** `type`, the `tool` identity (name, arguments, `call_id`),
  `parent_id` structure, `result`, `status`, `error`, `component`, and the
  *order* of events are the behavioral record.

Deterministic replay is defined against the **behavioral** record. A replay
re-executes an agent and asks: *given the same inputs, does it still produce
the same behavior*? It must not attempt to reproduce identity or timing —
matching `type`/order/`tool`/`result`/`status`/`usage` while re-deriving
`timestamp`/`duration_ms`/`cost_usd` is the correct, honest contract. Anything
that claims byte-identical replay is testing the wrong thing.

Consequently, a **trace diff** (a future consumer of this spec) compares the
behavior line: added/removed/reordered tool or model calls, changed
arguments/results, changed statuses, changed usage. Timing and cost deltas are
reported as *measurements*, never as the definition of behavioral change.

## 9. Redaction boundary

Redaction happens **at serialization, not recording** (ADR-0008):

- In-memory — what assertions and the policy engine see — is the truth: real
  values, so `to_not_leak` can verify the agent actually *handled* a customer's
  email rather than asserting against already-scrubbed text.
- On disk — `trace.jsonl`, `result.json`, markdown, PR annotations — payloads
  (`tool.arguments`, `result`, `metadata`, `component`) pass through the
  redactor first: named patterns (credentials, PII), field-name matching for
  configured keys, and literal scrubbing of secret-looking environment values.

The stored form is therefore already scrubbed, and that is what replay loads.
A trace you can safely paste into a bug report is a hard product property
(SECURITY.md threat model), not a formatting concern.

## 10. Interoperability

- The schema is framework-neutral and only *inspired* by OpenTelemetry, not
  OTel: no collector, protocol, or exporter is involved (ADR-0006). An OTel
  exporter, if ever wanted, is an *adapter over this schema*, never the schema
  itself.
- Adapters translate between an agent (sync, async, multi-step, or a remote
  service behind an HTTP adapter) and this event grammar. The trace is how an
  agent's behavior becomes inspectable regardless of its implementation.
- AgentCI makes **no outbound network calls of its own**, ever (ADR-0006). The
  trace is a local artifact.

## 11. Canonical example

A complete successful run, exactly as it appears on disk (this is the golden
fixture enforced by the conformance suite in §12). Event ids are abbreviated
below but are opaque strings in practice.

```text
.agentci/runs/run_canonical/trace.jsonl
```

```json
{"run_id":"run_canonical","event_id":"evt_01","type":"run_started","timestamp":"2026-10-07T12:00:00Z","parent_id":null,"component":"demo","tool":null,"result":null,"usage":null,"cost_usd":null,"duration_ms":null,"status":"success","metadata":{"test":"t_refund","iteration":1},"error":null,"schema_version":1}
{"run_id":"run_canonical","event_id":"evt_02","type":"model_call_started","timestamp":"2026-10-07T12:00:00Z","parent_id":"evt_01","component":"openai/gpt-4o-mini","tool":null,"result":null,"usage":null,"cost_usd":null,"duration_ms":null,"status":"pending","metadata":{"model":"openai/gpt-4o-mini","provider":"openai","prompt_chars":42},"error":null,"schema_version":1}
{"run_id":"run_canonical","event_id":"evt_03","type":"model_call_completed","timestamp":"2026-10-07T12:00:00Z","parent_id":"evt_01","component":"openai/gpt-4o-mini","tool":null,"result":"{\"tool\":\"lookup_order\"}","usage":{"prompt_tokens":25,"completion_tokens":17,"total_tokens":42},"cost_usd":0.00041,"duration_ms":812,"status":"success","metadata":{},"error":null,"schema_version":1}
{"run_id":"run_canonical","event_id":"evt_04","type":"tool_call_started","timestamp":"2026-10-07T12:00:01Z","parent_id":"evt_01","component":null,"tool":{"name":"lookup_order","arguments":{"order_id":"ORD-7781"},"call_id":"call_a"},"result":null,"usage":null,"cost_usd":null,"duration_ms":null,"status":"pending","metadata":{},"error":null,"schema_version":1}
{"run_id":"run_canonical","event_id":"evt_05","type":"tool_call_completed","timestamp":"2026-10-07T12:00:01Z","parent_id":"evt_01","component":null,"tool":{"name":"lookup_order","arguments":{"order_id":"ORD-7781"},"call_id":"call_a"},"result":{"status":"shipped","customer":"[REDACTED:email]"},"usage":null,"cost_usd":null,"duration_ms":2,"status":"success","metadata":{},"error":null,"schema_version":1}
{"run_id":"run_canonical","event_id":"evt_06","type":"retrieval_started","timestamp":"2026-10-07T12:00:01Z","parent_id":"evt_01","component":"faq","tool":null,"result":null,"usage":null,"cost_usd":null,"duration_ms":null,"status":"pending","metadata":{"source":"faq/refunds.md","query":"can I refund a shipped order"},"error":null,"schema_version":1}
{"run_id":"run_canonical","event_id":"evt_07","type":"retrieval_completed","timestamp":"2026-10-07T12:00:01Z","parent_id":"evt_01","component":"faq","tool":null,"result":{"document_count":2},"usage":null,"cost_usd":null,"duration_ms":41,"status":"success","metadata":{},"error":null,"schema_version":1}
{"run_id":"run_canonical","event_id":"evt_08","type":"approval_requested","timestamp":"2026-10-07T12:00:02Z","parent_id":"evt_01","component":null,"tool":{"name":"issue_refund","arguments":{},"call_id":"call_b"},"result":null,"usage":null,"cost_usd":null,"duration_ms":null,"status":"pending","metadata":{},"error":null,"schema_version":1}
{"run_id":"run_canonical","event_id":"evt_09","type":"approval_denied","timestamp":"2026-10-07T12:00:02Z","parent_id":"evt_01","component":null,"tool":{"name":"issue_refund","arguments":{},"call_id":"call_b"},"result":{"reason":"approval_required"},"usage":null,"cost_usd":null,"duration_ms":null,"status":"denied","metadata":{},"error":null,"schema_version":1}
{"run_id":"run_canonical","event_id":"evt_10","type":"memory_read","timestamp":"2026-10-07T12:00:02Z","parent_id":"evt_01","component":null,"tool":null,"result":{"key":"refund_policy","value":"refunds require manager approval"},"usage":null,"cost_usd":null,"duration_ms":null,"status":"success","metadata":{"key":"refund_policy"},"error":null,"schema_version":1}
{"run_id":"run_canonical","event_id":"evt_11","type":"tool_call_completed","timestamp":"2026-10-07T12:00:03Z","parent_id":"evt_01","component":null,"tool":{"name":"notify_manager","arguments":{"order_id":"ORD-7781"},"call_id":"call_c"},"result":{"ok":true},"usage":null,"cost_usd":null,"duration_ms":1,"status":"mocked","metadata":{},"error":null,"schema_version":1}
{"run_id":"run_canonical","event_id":"evt_12","type":"run_completed","timestamp":"2026-10-07T12:00:03Z","parent_id":null,"component":"demo","tool":null,"result":null,"usage":null,"cost_usd":null,"duration_ms":3,"status":"success","metadata":{},"error":null,"schema_version":1}
```

What this run says, read against §3–§6:

- `evt_01` starts the run (`component: demo`, metadata records the test and
  iteration).
- The model stream (`evt_02`→`evt_03`) is one invocation: pending start with
  `parent_id: evt_01`, success completion with usage and cost. Its `result` is
  the model's tool-call text.
- `evt_04`→`evt_05` is the `lookup_order` invocation. `call_id: call_a`
  pairs them. Its `result` carried `ada@example.com`, which is **already
  scrubbed** on disk — `[REDACTED:email]` proves §9.
- `evt_08`→`evt_09` records the approval lifecycle: requested, then denied
  with `status: denied`.
- `evt_11` is a **mocked** side effect: the agent got its `notify_manager`
  "return value", and the trace says so rather than pretending it ran.
- `evt_12` closes the run with the total duration.
- Every line carries `"schema_version": 1`, and every line is independently
  parseable (JSONL, §2).

## 12. Conformance

The v1 contract is locked by `tests/test_trace_spec.py`, which asserts:

1. **Closed grammar** — `EventType` has exactly the fourteen values of §3,
   `EventStatus` exactly the five of §4, and `schema_version` is `1`.
2. **The canonical example parses** — every line of §11 validates into
   `TraceEvent` under `extra="forbid"` and round-trips through `to_dict()`
   byte-for-byte (including `schema_version`), i.e. the stored artifact
   {...}↦ parsed {...}↦ re-serialized is identical.
3. **The artifact survives the store** — the canonical trace written via
   `RunStore.save_trace` reads back identically through `load_trace`, and the
   file itself carries one event per line, each with `schema_version: 1`.
4. **The grammar is closed in both directions** — an event with an unknown
   field is rejected (`extra="forbid"`), and `TRACE_SCHEMA_VERSION` is pinned
   to `1` so a bump is a deliberate, reviewed act.
5. **Redaction happens on the boundary** — the unredacted value present in
   memory (e.g. an email in `result`) is what assertions see, while the same
   event serialized to disk carries the redacted placeholder.

A schema version bump is therefore not a code edit — it is this specification
changing too, with the old version's conformance suite kept for readers of
older artifacts.

## 13. Open questions for successors

These do not block v1 but are recorded so the boundary is explicit:

- A **span/segment** concept (analogous to OTel spans) is deliberately absent;
  `parent_id` trees already cover nesting, and a span layer can be added in v2
  if consumers need aggregate views without walking the tree.
- `usage` currently describes model tokens. Tool-level usage (rows scanned,
  retrieval candidates) is left to `result`/`metadata` in v1.
- Battery/latency histograms and percentile reporting are a reporting-layer
  concern, not a trace-format concern.