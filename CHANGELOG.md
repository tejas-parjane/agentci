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
