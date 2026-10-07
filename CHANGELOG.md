# Changelog

All notable changes to AgentCI are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The **report schema** is versioned independently of the package: consumers that
parse `report.json` should pin `schema_version`, not the release.

## [Unreleased]

### Added

- **Change-aware selection.** `agentci run --changed` skips the tests a diff
  cannot affect, using `dependencies=` declared on `@agent_test`. Diffing runs
  from the merge base to the working tree, so uncommitted and untracked work
  counts as a change. `--base` overrides the ref; otherwise `AGENTCI_BASE`,
  `GITHUB_BASE_REF`, `origin/main`, and `selection.default_base` are consulted in
  that order.
- **`agentci gate`**, the release verdict: always selects, prints
  `RELEASE GATE: PASS` or `RELEASE GATE: BLOCKED` with the reasons, and exits 0
  or non-zero. Warnings block by default (`--allow-warn` relaxes them), and a
  base ref that cannot be resolved blocks on its own.
- `run.selection_base` and `run.selection_changed` in `report.json`, both
  additive and defaulted, so `schema_version` stays 1.
- `tests/test_selection.py` and `tests/test_gate.py`.
- `examples/support_agent/refund_regression.py`, a deliberately regressed refund
  flow that the shipped expectations catch, plus its probe in `scripts/verify.py`.
- `tests/test_policy_assertions.py`.
- **AgentCI Trace Specification v1** — the normative contract for
  `trace.jsonl` (`docs/specification/trace-v1.md`): the closed event grammar,
  the order/parentage conventions, the determinism contract for replay, and
  the serialization-time redaction boundary. `schema_version` stays 1 and is
  pinned by `tests/test_trace_spec.py`, which also enforces the canonical
  example byte-for-byte through the store.
- **`agentci record --name <scenario>`**, which runs a scenario once and exports
  a standalone v1 trace artifact (`.agentci/traces/<name>.jsonl`, or one file per
  test under `<name>/` for multi-test scenarios). Replay's ground truth lives
  there, next to the per-run artifacts.
- **`agentci replay <trace.jsonl>`**, which re-executes the recorded scenario
  against the *current* agent with every tool call answered from the artifact —
  replay outranks config mocks and live implementations. A call the recording
  never answered is refused, recorded as `replay: divergent`, and fails the run,
  so a replay is never silently "fine". The replayed trace is exported side by
  side with the recording for diffing.
- **`agentci diff <a.jsonl> <b.jsonl>`**, which compares two artifacts over the
  spec's behavioral line — event kinds, order, tool identity, arguments, and
  statuses — while reporting latency/cost/tokens as measurements, not behavior.
  Exits 0 unchanged / 1 changed.
- `tests/test_replay.py` and `tests/test_diff.py`, covering the session's FIFO
  answering, the record→replay→diff round trip, and a replay that catches an
  agent now refunding twice.
- **Async tool routing.** `ToolRegistry.ainvoke()` is the async twin of
  `invoke()` with the same policy layering — replay answers first, then config
  mocks, then approved live execution — so an async agent calls through the same
  trace, redaction, and gate machinery as a sync one.
- **`agentci.integrations.openai_agents`**, an adapter that runs an existing
  openai-agents application without a rewrite: `AgentCI(root_agent)` re-roots the
  reachable graph (handoffs included, cloned per run) so every `FunctionTool`
  routes through the registry, runs the SDK on a harness-owned event loop with
  tracing disabled, and records `model_call` events alongside tool events.
  Installable via the `openai-agents` extra. Covered by
  `tests/test_openai_agents.py`, which drives record→replay→diff end to end
  against a `ScriptedModel` and blocks a second refund.
- **PyPI publication job** in `.github/workflows/release.yml`: on `v*` tags the
  built wheel is published to PyPI as `agentci-py` via trusted publishing.
  Install docs now use `pip install agentci-py` /
  `pip install "agentci-py[openai-agents]"`.

### Changed

- `default_base_ref()` and `changed_files()` take an explicit working directory.
  Without one they probed the process working directory for `origin/main`, which
  is not necessarily the project under test.
- The forbidden-data checks (`forbidden_data_patterns` policy and
  `to_not_contain_forbidden_data`) attribute a hit to the configured pattern
  only. They previously used `contains_secret`, which is true when *any* active
  pattern matches, so a tool result carrying an email could be reported as a
  `credit_card` violation.
- The bundled support agent now has a refund flow with an overridable
  `refund_decision`, read-only `lookup_customer`/`lookup_order` tools, and mocked
  `refund_order`/`send_email` side effects, with `lookup_customer`, `lookup_order`,
  `refund_order`, and `send_email` added to the allowlist.
- Change-aware selection skips are now an **informational outcome**, not
  warnings. The report records them as `run.selection_skipped` (with the reason
  on each skipped test), so `agentci gate` passes on `unmatched: skip` — a
  release must not block because the diff proved unaffected tests were skipped.
  Warnings that mean "something went wrong" (an unresolvable base, a persistence
  fault) still block as before.

## [0.1.0] - 2026-10-06

First release. Everything below is new.

### Added

**Runtime**

- `@agent_test` decorator and discovery over configured files, with per-test
  tags, ids, dependencies, and repeat overrides.
- `TestRunner` invoking cases sync or async, merging live and returned events by
  `event_id`, and folding assertions, budgets, policy, gates, and regression into
  one `RunReport`.
- Budget enforcement that stops a runaway run rather than reporting on it after
  the fact, with the partial evidence still written for `ERROR` tests.
- Windowed loop detection, including alternating A,B,A,B patterns.
- Classification that keeps `AssertionError`/step-limit/deadline as quality
  failures distinct from infrastructure `ERROR`s.

**Assertions**

- `expect()` fluent surface over output, JSON structure, tool identity, order,
  count, arguments, retries, cost, latency, tokens, steps, policy, and leakage.
- Assertions are recorded when they pass, so a report shows what was verified and
  not only what broke.
- Missing cost or usage evaluates to `SKIP`, never `PASS`.

**Policy and safety**

- Policy as code: allowlist, denylist, approval requirements, and forbidden data
  patterns.
- Side effects deny by default; mocks are checked before the side-effect gate
  because a mocked call touches nothing.
- Redaction at the serialization boundary — assertions see real values, artifacts
  do not.

**Storage and reporting**

- JSONL traces with a checked `schema_version`, tolerating truncation from an
  interrupted run while rejecting real corruption.
- Versioned `report.json`, Markdown rendering, workflow annotations, and a
  `$GITHUB_STEP_SUMMARY` block.
- Baseline capture and comparison with quality, cost, and latency gates, new-
  failure blocking, and a configurable noise floor.

**CLI and integration**

- `agentci run|test|list|init|config|baseline|version`, with `--root`, selection
  by tag/id/name, `--json`, and `--fail-on-warn`.
- Seven documented exit codes so CI can tell a regression from a broken harness.
- Apache-2.0 licence, strict typed YAML configuration, and a worked example with
  thirteen tests plus seven negative probes that must fail.

### Known gaps

Deferred rather than broken, and called out so no one discovers them the hard
way:

- PyPI publication — install from GitHub until the first tag is verified.
- HTML report rendering: `report.html` is a reserved option with no renderer
  behind it.
- Replay: the policy engine already accepts replay-supplied mocks, but nothing
  produces them and there is no `agentci replay` command. `HttpAgentAdapter`
  calls its endpoint for real.
- Adapter entry-point discovery (ADR-0004).
