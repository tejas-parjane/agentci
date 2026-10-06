# Changelog

All notable changes to AgentCI are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The **report schema** is versioned independently of the package: consumers that
parse `report.json` should pin `schema_version`, not the release.

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
