# ADR-0006: Telemetry and outbound network calls default to none

Status: Accepted

## Context

The PRD's requirement is "no external telemetry by default". For a tool that
executes agent code and inspects its output, the useful framing is broader and
stricter: AgentCI must not make **any** outbound network call of its own.

Two places would otherwise reach for the network, and both are subtle:

- **Pricing.** Token prices change, so fetching them would give current numbers.
  It would also mean that running a test suite phones a third party, with the
  model names, run counts, and timing of every run observable from outside.
  Worse, a pricing endpoint being unreachable would have to change a verdict.
- **Tracing.** OpenTelemetry is the obvious instrumentation model. Adopting it
  as a dependency would drag a collector, a protocol, and an exporter into a
  library whose whole job is to produce one local file.

## Decision

- **No telemetry. No analytics, no crash reporting, no update checks, no
  network calls at runtime, ever — not behind a flag.**
- **Pricing is a bundled, versioned table** read from the package. An unknown
  model resolves to no price, which makes the cost assertion `SKIP` with a hint,
  never `PASS`.
- **The trace schema is inspired by OTel but is not OTel.** It is a plain
  framework-neutral `TraceEvent`. Exporting to a collector, if ever wanted, is an
  adapter over this schema rather than the schema being OTel's.

`--offline` is therefore not a mode AgentCI has, because there is nothing to
turn off.

## Consequences

- A run produces exactly the artifacts under `.agentci/` and nothing else.
  This is checkable: the process has no reason to open a socket.
- Cost figures can be stale relative to a vendor's price change. The table
  records `pricing_as_of` in the report so a stale number is visible rather than
  implied, and adapters may supply `cost_usd` directly when they know it.
- Missing pricing degrades a gate to `SKIP`, which is the honest verdict for
  "did not evaluate".
- Any future feature needing a network call has to justify itself in a
  successor to this record, not by extending this one.
