# AgentCI

CI and release gates for AI agents.

Agents are nondeterministic. A prompt change, a model upgrade, or a flaky tool can
make an agent worse in ways that pass code review and ship to production. AgentCI
gives agent behaviour the thing ordinary tests cannot: assertions on **what the
agent actually did** — which tools it called, in what order, at what cost, under
what policy — recorded as a normal test suite you run in CI.

Two commands make that suite do release work:

- `agentci run` runs tests *a diff can affect*, so a feedback loop that grows with
  the agent stays fast instead of slowing the whole repository down with it.
- `agentci gate` turns the same suite into a **release verdict**: it diffs the
  change, runs only what is affected, and either prints `RELEASE GATE: PASS` and
  exits 0, or prints the reasons and blocks.

```python
from agentci.assertions import expect
from agentci.testing import agent_test


@agent_test(tags=["smoke"])
def test_refund_is_approved_with_a_policy_check(agent):
    result = agent.run("refund order 4217")

    expect(result).to_contain("refunded")
    expect(result).to_use_tool("lookup_order").to_use_tool("issue_refund")
    expect(result).to_have_max_cost(0.05)
    expect(result).to_not_leak_environment_secrets()
```

## Install

```bash
pip install git+https://github.com/tejas-parjane/agentci.git
```

Requires Python 3.11 or newer.

## Quick start

```bash
cd your-project
agentci init        # writes agentci.yaml + a runnable starter test
agentci list        # show the tests it found
agentci run         # run them, exit with a CI-friendly code
```

`agentci init` scaffolds three files. Point `agent.adapter` in `agentci.yaml` at
your agent, then delete the placeholder:

```yaml
version: 1
project:
  name: my-agent
agent:
  adapter: "my_agent.agent:MyAgent"   # module.path:attribute
tests:
  - dir: tests/agentci
    pattern: "test_*.py"
```

The adapter is the only code you write. It can be a class with a `run` method, an
object exposing a tool registry, or a plain function `(user_input) -> AgentResult`.
Tools the agent invokes through `ctx.tools` are traced, mocked, and subject to the
policy in `agentci.yaml`.

## The release gate

`agentci gate` is how a change gets a *verdict* instead of a checkmark. It is
`agentci run` with every safety net on at once:

- **Change-aware selection is always on.** The suite is diffed against a base ref
  and only the tests this change can affect are run; the rest are skipped with a
  recorded reason. `--base <ref>` overrides the base; otherwise it resolves from
  `AGENTCI_BASE`, then `GITHUB_BASE_REF`, then `origin/main`, then
  `selection.default_base`.
- **Warnings block the release by default.** `--allow-warn` relaxes them.
- **An unresolvable base ref blocks on its own.** `run` falls back to running
  everything in that case (developer-friendly); `gate` refuses to guess
  (release-strict), because a release the harness cannot scope to a diff is
  unverifiable.

Skips are not warnings. A test the diff proves unaffected is the *point* of
selection, so `unmatched: skip` never blocks `gate` by itself: the skip count
(`run.selection_skipped`) and each skipped test's reason stay in the report, and
the gate only blocks on warnings that mean something went wrong — not on
coverage it requested.

A blocked gate prints the verdict *with the reasons*:

```text
RELEASE GATE: BLOCKED
  - change-aware selection unavailable: no base ref could be determined; pass --base,
    set AGENTCI_BASE, or configure selection.default_base
  - the run failed: 1 failing test(s); 1 failing assertion(s)
```

and exits 0 when it is safe to ship, non-zero when it is not. That is the whole
loop: *write a test for the behaviour that must never break, break the agent,
watch the gate name the reason.* The bundled support-agent example ships with a
deliberately regressed refund flow in
`examples/support_agent/refund_regression.py`; point `agentci gate` at it and the
blocked verdict points straight at the money.

## What you can assert

| Group | Examples |
| --- | --- |
| Output | `to_contain`, `to_equal`, `to_match`, `to_be_json`, `to_have_fields`, `to_match_json_schema` |
| Tools | `to_use_tool`, `to_not_use_tool`, `to_call_tool`, `to_call_tool_with`, `to_follow_tool_order`, `to_only_use_tools`, `to_call_tool_matching_schema` |
| Execution | `to_have_max_cost`, `to_have_max_latency`, `to_have_max_tokens`, `to_have_max_steps`, `to_have_max_retries`, `to_not_loop` |
| Policy | `to_have_no_policy_violations`, `to_have_no_live_side_effects`, `to_require_approval_for` |
| Data | `to_not_leak`, `to_not_leak_environment_secrets`, `to_not_contain_forbidden_data` |

Assertions are **recorded even when they pass**, so the report shows what was
verified, not only what broke.

## Properties that hold

These are enforced by tests, not just documented:

- **Redaction at serialization.** Assertions compare against real values; artifacts
  on disk are scrubbed. Secrets in `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and the
  rest of the usual set never reach a CI log.
- **Missing cost is `SKIP`, never `PASS`.** If the adapter reports no usage and the
  model has no pricing entry, the cost gate is *unevaluated*. An unknown verdict is
  never rendered as a pass.
- **Side effects default to deny.** `external_side_effects: deny` refuses
  `delete_ticket`, `send_email`, and any other tool marked as side-effecting, unless
  it is mocked. Mocks are checked first, because they are not real side effects.
- **Tests without dependencies run by default.** Change-aware selection (`unmatched`)
  only skips a test it can prove is unaffected, not the other way around: no
  `--changed`, no diff, or an unresolvable base never silently reduces coverage. `run`
  warns and runs everything; `gate` treats that as an unverifiable release and blocks.

## Configuration

`agentci.yaml` is strict: unknown keys are an error, and every path is resolved
relative to the project root. The sections you are most likely to touch:

```yaml
budgets:          # enforced for every run, not just when you assert them
  max_latency_ms: 30000
  max_steps: 20

execution:
  external_side_effects: deny
  # A tool is side-effecting if the adapter declares it that way, or if you list
  # it here. Either way it is refused unless mocked.
  side_effecting_tools: [delete_ticket]
  mocks:                                   # matched before any live call
    lookup_order: {response: {status: open}, match_arguments: false}

policies:
  allowed_tools: [lookup_order, issue_refund, escalate]

redaction:
  patterns: [email]        # additive to an always-on credential set
  fields: []               # extra sensitive field names, matched by substring
  scrub_environment: true  # also scrub literal secret-looking env values

report:
  json: true
  markdown: true

storage:
  dir: .agentci
```

Validate and print the effective configuration:

```bash
agentci config
```

## CLI

| Command | Purpose |
| --- | --- |
| `agentci run` (alias `test`) | Run the suite (optionally `--changed`) |
| `agentci gate` | The release verdict: diff-select, strict, prints reasons, exits 0/non-zero |
| `agentci list` | Show discovered tests without running them |
| `agentci init` | Scaffold config, a starter test, and an adapter |
| `agentci config` | Validate `agentci.yaml`, print the resolved config |
| `agentci baseline save` | Store this run as the comparison baseline |
| `agentci baseline show` | Show what a saved baseline contains |
| `agentci version` | Print the version |

Selection: `--tag` / `-k` (repeatable), `--test-id` (repeatable), `--name`
(substring), `--repeat`, `--fail-on-warn`, `--no-traces`, `--json`, `--output FILE`.
Change-aware: `--changed` (run) and `--base <ref>` (run and gate) choose what a diff
can affect.

### Exit codes

Documented in `docs/adr/0007-exit-codes.md`. They exist so CI can tell the
difference between an agent that regressed and a harness that broke:

| Code | Meaning |
| --- | --- |
| 0 | Run passed (and no warnings, with `--fail-on-warn`) |
| 1 | A test, gate, or regression check failed |
| 2 | Configuration or schema error |
| 3 | Infrastructure error (adapter raised, discovery failed) |
| 4 | No tests were selected |
| 5 | Unexpected error inside AgentCI |
| 130 | Interrupted |

## CI integration

On GitHub Actions, AgentCI writes workflow annotations for failed tests and a
Markdown block to `$GITHUB_STEP_SUMMARY`. Run the suite on pull requests and put
`agentci gate` (exit 0 == ship) in the release job:

```yaml
# .github/workflows/pr.yml — catch regressions on the branch
- uses: actions/setup-python@v5
  with:
    python-version: "3.12"
- run: pip install git+https://github.com/tejas-parjane/agentci.git
- run: agentci run --changed

# .github/workflows/release.yml — the verdict before shipping
- run: pip install git+https://github.com/tejas-parjane/agentci.git
- run: |
    PREV=$(git describe --tags --abbrev=0 HEAD^ || echo origin/main)
    agentci gate --base "$PREV"
```

Because `gate` is change-aware, the release job scopes to *this* release's diff —
the tests affected since the last release tag — and blocks unless they pass.

Reports land in `.agentci/reports/`:

```
.agentci/
  runs/<run_id>/trace.jsonl   one event per line — an interrupted run stays readable
  runs/<run_id>/result.json
  reports/<run_id>/report.json
  reports/<run_id>/report.md
  reports/latest/             stable path for artifact globs
  baseline.json               saved via `agentci baseline save`
```

## How it works

AgentCI wraps your agent in a **tool registry**. Every call the agent makes passes
through the registry, which applies policy (allowlist, mocks, side-effect
denial) and records a normalized event: tool name, arguments, result, usage, cost,
duration, parent, status.

Those events form a **trace**, which is what the assertions read. Two consequences
worth knowing:

1. Traces are recorded live, so a runaway agent is *stopped* when it crosses a
   budget, not after it finishes. Reports still show the partial evidence for an
   `ERROR` test.
2. Because assertions read the trace, they are unaffected by how your agent is
   implemented — sync, async, multi-step, or a remote service behind an HTTP
   adapter.

## Status

Version 0.1.0 shipped the core runtime, assertion library, policy engine, CLI, and
report schema. The report schema is versioned independently of the package so
consumers can pin the shape they parse.

Since 0.1.0, change-aware selection (`--changed`, diff + `dependencies=`) and the
`agentci gate` release verdict are in place, along with a realistic
customer-support refund example that ships with a deliberate regression the suite
catches.

Not yet wired up:

- HTML report rendering. `report.html` is a reserved option with no renderer
  behind it.
- Replay. The policy engine already accepts replay-supplied mocks, but nothing
  produces them and there is no `agentci replay` command; `HttpAgentAdapter`
  calls its endpoint for real.
- PyPI publication (install from GitHub until then).

## Documentation

- [Trace specification (v1)](docs/specification/trace-v1.md) — the normative
  trace format: the event grammar, what deterministic replay will reconstruct,
  and the redaction boundary. The interoperability contract between your agent
  and AgentCI.
- [Configuration reference](docs/reference/configuration.md) — every key, its
  default, and what it refuses to do.
- [Writing an adapter](docs/guides/adapters.md) — the contract, tool
  declaration, and reporting usage.
- [Decision records](docs/adr/) — why the trace format, licence, redaction
  boundary, and exit codes are what they are.
- [Contributing](CONTRIBUTING.md), [Security policy](SECURITY.md),
  [Changelog](CHANGELOG.md).

## Development

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
ruff check .          # lint
mypy src              # strict typing
pytest                # unit tests
python scripts/verify.py   # example suite + negative-path probes
```

`scripts/verify.py` runs the example suite plus eight probes that must **fail** —
a credential leak, a step limit, a policy denial, a double-refund regression, and
so on. A probe that passes is a regression in AgentCI itself. See
[CONTRIBUTING.md](CONTRIBUTING.md) for what a change needs before it is ready for
review.

## License

Apache-2.0. See [LICENSE](LICENSE).
