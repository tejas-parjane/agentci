# Configuration reference

`agentci.yaml` is the whole of AgentCI's configuration. It is parsed by a strict
typed schema: **unknown keys are errors, not warnings**, `version:` is required,
and every relative path resolves against the project root rather than the
directory you happened to run from.

Validate a file and print what it actually resolves to:

```bash
agentci config              # resolved configuration, aliases included
agentci config --dump out.yaml
```

```yaml
version: 1                  # required; must equal CONFIG_VERSION (currently 1)

project:
  name: support-agent       # appears in reports
  version: "0.3.1"
  description: Refund and escalation assistant
```

---

## `agent`

```yaml
agent:
  adapter: "my_agent.agent:run"   # REQUIRED: module.path:attribute
  name: support-agent             # display name; falls back to project.name
  model: gpt-5-mini               # used for cost estimation
  options: {}                     # passed to the adapter untouched
```

The adapter is resolved against the project root and loaded eagerly when the
runner starts, so a bad path fails before any test runs. Accepted shapes:

| Shape | Example |
| --- | --- |
| Function | `run(user_input) -> AgentResult` |
| Function with context | `run(user_input, ctx) -> AgentResult` |
| Class | `MyAgent` with a `run` method |
| Adapter | anything implementing the `AgentAdapter` protocol |

See [Writing an adapter](../guides/adapters.md).

---

## `evaluation`

```yaml
evaluation:
  repeat: 1                  # repetitions per test
  minimum_pass_rate: 1.0     # fraction of repetitions that must pass
  allow_flaky: true          # WARN rather than FAIL when repeats disagree
```

Per-test overrides are declared on the test itself, not here.

---

## `budgets`

Applied by the runner to **every** invocation, whether or not the test body
asserted them. A breach stops the run rather than being reported after it
finishes.

```yaml
budgets:
  max_cost_usd: 0.05
  max_latency_ms: 5000
  max_tool_calls: 10
  max_steps: 20
  max_retries: 3
  max_tokens: 50000
```

All are optional; an unset budget is not enforced. When a budget cannot be
evaluated — no pricing, no usage reported — the corresponding check is `SKIP`,
never `PASS`.

---

## `policies`

```yaml
policies:
  allowed_tools: [lookup_ticket, escalate_ticket]   # allowlist of declared tools
  denied_tools: []                                  # checked after the allowlist
  approval_required: []                             # needs an approval to run
  forbidden_data_patterns: [password, credit_card]  # scanned in output and calls
```

An empty `allowed_tools` means "no allowlist" — every *declared* tool is
permitted, subject to the side-effect gate below. An undeclared tool call is an
error regardless of this list.

---

## `execution`

Side-effect safety and mocking. **Deny is the default.**

```yaml
execution:
  external_side_effects: deny    # deny | allow
  side_effecting_tools: [delete_ticket]
  mocks:
    lookup_ticket:
      response: {id: T-1001, status: open}
      match_arguments: false
      delay_ms: null
```

A tool is side-effecting if the adapter declares `ToolDecl(side_effect=True)`
**or** if you list it here. Side-effecting tools are refused unless mocked, and
mocks are checked *before* the side-effect gate because they do not touch
anything real.

`delay_ms` exists to test latency budgets deterministically.

---

## `gates`

Release gates (§18). Absence of a threshold means *do not gate on this
dimension*. Each threshold accepts three spellings:

```yaml
gates:
  task_success: 0.90                # bare number means >= 
  policy_compliance: "== 1.0"
  max_cost_usd: {lte: 0.05}
  reliability: ">= 0.95"
```

Supported operators: `>`, `>=`, `<`, `<=`, `==`, `!=`. A mapping form accepts
`gt`/`gte`/`lt`/`lte`/`eq`, and multiple constraints are ANDed, so
`{gte: 0.9, lte: 1.0}` is a band.

Gates evaluated: `task_success`, `tool_correctness`, `policy_compliance`,
`reliability`, `max_cost_usd`, `max_latency_ms`, `max_tokens`.

---

## `regression`

Relative comparison against a baseline recorded by `agentci baseline save`.

```yaml
regression:
  baseline: .agentci/baseline.json
  max_quality_drop: 0.05            # absolute, 0..1
  max_cost_increase_pct: 25
  max_latency_increase_pct: 30
  on_regression: fail               # fail | warn
  fail_on_new_test_failures: true
  ignore_regression_below_pct: 5.0
```

- A test passing in the baseline and failing now is a **new failure** and
  blocks when `fail_on_new_test_failures` is true.
- Cost and latency are compared as percentage change over the tests both runs
  share. A change within `ignore_regression_below_pct` passes with the reason
  stated; a zero baseline makes the ratio undefined and yields `SKIP`.
- With no baseline on disk the section reports `skip` with the reason — never
  `pass`.

---

## `redaction`

Applied when values are serialized, so assertions still see real values. See
[ADR-0008](../adr/0008-redaction-at-serialization.md).

```yaml
redaction:
  enabled: true
  patterns: [email, phone, ssn]      # additional to an always-on credential set
  fields: [authorization, cookie]    # matched against field names, by substring
  scrub_environment: true            # also scrub secret-looking env values
```

Built-in credential patterns fire before your `patterns`, and both fire before
literal-secret substitution.

---

## `selection`

Change-aware selection: skip the tests a diff cannot affect. Activate it on a
single run with `agentci run --changed`, or make it the release verdict with
`agentci gate`, which always turns it on.

```yaml
selection:
  enabled: true
  unmatched: run        # run | skip
  default_base: null    # last resort when neither --base nor the CI env has one
  global_paths:         # a change here runs every test
    - agentci.yaml
    - agentci.yml
    - .agentci.yaml
```

A test opts in by declaring what it reads:

```python
@agent_test(dependencies=["prompts/*.txt", "tools/*"])
def test_refund(agent): ...
```

Patterns are repo-relative POSIX globs. `*` and `?` stay inside one path
segment (`tools/*.py` does **not** match `tools/a/b.py`), `**` spans directories,
and a pattern with no glob characters also matches everything underneath it —
`dependencies: [examples/support_agent]` means the directory and its contents.

**Base resolution**, in order: the `--base` flag, then `AGENTCI_BASE` /
`GITHUB_BASE_REF`, then `origin/main` (or `origin/master`), then
`default_base`. The diff runs from the merge base of that ref and `HEAD` to the
**working tree**, so uncommitted and untracked work counts as a change too.

**Safety rules.** These are not configurable, because each one fails toward
running more tests rather than fewer:

- If no base resolves, nothing is skipped and the run carries a warning.
- `unmatched: run` (the default) runs a test that declares no dependencies.
- `unmatched: skip` skips it, recording
  `change-aware selection: declares no dependencies, and selection.unmatched is
  'skip'` as the reason on the test itself.
- A change to any `global_paths` entry runs everything.

Skips are never silent: each one records `no change to <patterns> since <base>`
as the reason on the test itself, and the run's report carries a
`run.selection_skipped` count (printed as an informational line, not a warning).
A skipped set is the point of selection — the change analysis says the tests are
unaffected — so `agentci gate` does **not** block on it.

Warnings that genuinely block are the ones that mean "something about the run
itself went wrong": an unresolvable base, a persistence failure, and so on.
`agentci gate` treats those as blocking (pass `--allow-warn` to relax them) and
blocks outright on an unresolvable base, which `--allow-warn` does not lift — see
[ADR-0009](../adr/0009-change-aware-selection.md). Set `enabled: false` to turn
selection off entirely; both commands then run the full suite.

---

## `storage`

```yaml
storage:
  dir: .agentci               # resolved against the project root
  record_traces: true         # also gates report.json / report.md writing
  retention_days: 30
  max_trace_events: 10000     # excerpt cap per test
```

Layout:

```text
.agentci/
  runs/<run_id>/trace.jsonl      one event per line
  runs/<run_id>/result.json
  reports/<run_id>/report.json
  reports/<run_id>/report.md
  reports/latest/                stable path for artifact globs
  baseline.json
```

---

## `report`

```yaml
report:
  json: true                    # JSON key maps to json_enabled in the schema
  markdown: true
  html: false
  ci_annotations: true
  ci_summary: true
  trace_excerpt_events: 40
```

`ci_annotations` and `ci_summary` emit GitHub workflow annotations and write
`$GITHUB_STEP_SUMMARY` only when `GITHUB_ACTIONS=true`.

---

## `tests`

```yaml
tests:
  - file: tests/agentci/test_support_agent.py
  - dir: tests/agentci
    pattern: "test_*.py"
    tags: [smoke]
```

`dir` entries are globbed against the **project root**, not the working
directory. Missing `file:` entries are a configuration error; a `dir:` that
matches nothing is not.

---

## `pricing` and `mcp`

Free-form pass-through maps reserved for per-model pricing overrides and MCP
server definitions. They are validated as mappings but not yet interpreted by
v0.1.

---

## Errors

Every configuration problem raises `ConfigError` naming the offending key, or
the missing path when it is a file that cannot be found:

```text
ConfigError: test file(s) declared in agentci.yaml do not exist:
tests/agentci/test_policy.py
  hint: run `agentci init` or fix the paths under `tests:`

ConfigError: policies.denied_tool: Extra inputs are not permitted
```

Exit code `2` for all of them.
