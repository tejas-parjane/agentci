# ADR-0001: Core result schema

Status: Accepted

## Context

Every part of AgentCI needs to pass around "what the agent produced". Adapters
are written by users, in whatever style their agent already has: a class with a
`run` method, a coroutine, a generator of events, a function that talks to an
HTTP service. If each of those shaped its own return value, the runner, the
assertions, and the report would each need a conversion layer, and the conversion
layer is where a wrong verdict would be introduced.

The schema also has to survive serialization. It lands in `report.json`, and
`report.json` is what CI parses.

## Decision

Introduce two stable types and require adapters to converge on one of them.

**`AgentResult`** (`agentci.core.result`) is the minimum answer, with exactly
three fields: `output_text`, `trace`, `metadata`. An adapter that just returns
text is done. Usage and cost are deliberately *not* fields here — they live on
the `model_call` events inside `trace`, so a cost figure always travels with the
event that produced it.

**`Trace` / `TraceEvent`** is the full record of what happened, used by adapters
that already emit their own events.

`as_view()` normalizes into the `ResultView` the assertions read. It accepts a
`ResultView` (the return of `agent.run(...)`), an `AgentResult`, or a dict in
`AgentResult` shape, and derives the metrics from the events in `trace`. A bare
`Trace` is rejected on purpose: with no `AgentResult` there is no `output_text`
to assert on, and half a result is the kind of thing that quietly passes.

`AgentResult` is deliberately *not* an enum of outcomes. It carries no
pass/fail/fatal verdict, because the verdict is the test runner's to compute from
assertions, budgets, and policy. An adapter that decided its own status would be
able to report a policy violation as a success.

## Consequences

- Adapters have exactly one conversion point, and it is tested once.
- `expect()` takes a single argument kind per call site. Passing a string (as in
  `expect(result.output_text)`) raises an `ExpectationError` naming what it
  wanted, because asserting on the raw text skips the trace that the tool,
  budget, and policy assertions read.
- Adding a field to `AgentResult` is additive. Changing an existing field's
  meaning requires a schema version bump.
