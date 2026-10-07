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
pip install agentci-py
# with the OpenAI Agents integration:
pip install "agentci-py[openai-agents]"
```

Requires Python 3.11 or newer. Before the first PyPI release you can install from
Git instead:

```bash
pip install git+https://github.com/tejas-parjane/agentci.git
```

## 60-second quickstart

```bash
cd your-project
pip install "agentci-py[openai-agents]"

agentci init          # agentci.yaml + a starter test that runs offline
agentci run           # PASS — no API key needed
```

That proves the harness works. Wire your agent (the [OpenAI Agents
integration](#run-an-existing-openai-agents-project-without-rewriting-it) takes
an existing `agents.Agent` with zero rewrite), assert the behaviour that must
never break, and gate it:

```bash
agentci record --name refund          # capture today's behaviour once
# ... an engineer touches the refund flow ...
agentci replay .agentci/traces/refund.jsonl          # FAIL: the run diverged
agentci diff .agentci/traces/refund.jsonl .agentci/traces/refund-replay.jsonl
agentci gate                                        # RELEASE GATE: BLOCKED
```

One loop, four words repeated in every release: **record, replay, diff, block.**
Each step is a real agentci command with a real exit code, and the rest of this
README says what each one verifies.

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

### Run an existing openai-agents project without rewriting it

AgentCI ships an adapter for [openai-agents](https://github.com/openai/openai-agents-python)
so an application you already built can be run, traced, mocked, and release-gated
as-is:

```bash
pip install "agentci-py[openai-agents]"
```

Wrap the root agent once, point `agent.adapter` at it, and assert the behaviour
that must never break. The demo pins a deterministic scripted model (from
`agents.testing`) so the whole flow runs offline with no API key; swap in your
agent's own model/provider and the trace, replay, and gate work exactly the same:

```python
# support_agent.py
from agents import Agent, function_tool
from agents.testing.model import ScriptedModel, assistant_message, function_call
from agentci.integrations.openai_agents import AgentCI

@function_tool
def refund_order(order_id: str, amount: float):
    return {"refunded": True, "order_id": order_id, "amount": amount}

root = Agent(
    name="refunder",
    instructions="Refund customer orders.",
    tools=[refund_order],
    # Deterministic stand-in: a real engine like OpenAI will call the same tools.
    model=ScriptedModel([
        {"output": [function_call("refund_order", {"order_id": "ORD-7781", "amount": 49.99}, call_id="call_1")]},
        {"output": [assistant_message("Refund issued for ORD-7781.")]},
    ]),
)

class SupportAgent(AgentCI):          # yaml: agent.adapter: "support_agent:SupportAgent"
    def __init__(self):
        super().__init__(root)
```

```python
# test_refund.py
from agentci.assertions import expect
from agentci.testing import agent_test


@agent_test(tags=["refund"])
def test_refund_happens_exactly_once(agent):
    result = agent.run("please refund order ORD-7781")
    expect(result).to_use_tool("refund_order", times=1)
```

```yaml
# agentci.yaml
version: 1
project:
  name: support
agent:
  adapter: "support_agent:SupportAgent"
execution:
  # Refunds are real money: denied unless mocked; the mock answers the recording.
  external_side_effects: deny
  mocks:
    refund_order:
      response: {refunded: true, amount_refunded: 49.99}
storage:
  dir: .agentci
tests:
  - file: test_refund.py
```

Every function tool in the reachable agent graph (handoffs included) routes
through AgentCI's registry, so the policy, mocks, replay, and trace below work
against a `refund_order` you never had to change. The SDK itself is untouched:
`Runner.run` still performs the loop, just on a harness-owned event loop, and the
agent graph is cloned per run so the original tool objects stay pristine.

Now the loop. Capture today's behaviour (one refund) as ground truth:

```text
$ agentci run
PASS 1/1 passed, 0 failed, 0 errored, 0 skipped

$ agentci record --name refund
recorded agentci_test_refund_test_refund_happens_exactly_once -> .agentci/traces/refund.jsonl (11 events)
```

An engineer "fixes" order handling — as perversely subtle as a batch refund loop —
and the agent now refunds the order twice. The recording still answers only one:

```text
$ agentci replay .agentci/traces/refund.jsonl
FAIL 0/1 passed, 1 failed, 0 errored, 0 skipped
replay wrote 15 events -> .agentci/traces/refund-replay.jsonl
```

Every tool call the recording never saw is refused and marked `replay: divergent`,
so a replay is never silently "fine". Then look at what actually changed between
the recording and the replay:

```text
$ agentci diff .agentci/traces/refund.jsonl .agentci/traces/refund-replay.jsonl
trace diff: .agentci/traces/refund.jsonl -> .agentci/traces/refund-replay.jsonl

  tool calls   2 -> 3  (+1)
  model calls  3 -> 4  (+1)
  denied       0 -> 1  (+1)

  added:
    refund_order  (order_id='ORD-7781', amount=49.99)

BEHAVIOR: CHANGED (2 addition(s))
```

A regression refund is now a name in a diff, not a spot check. Point the release
job at the same suite and the change cannot ship:

```text
$ agentci gate
RELEASE GATE: BLOCKED
  - the run failed: 1 failing test(s); 1 failing assertion(s); failing gate(s): tests
```

That is the full loop for an agent your team already built: **record the behaviour
that must never break, break the agent, watch the gate name the reason.**

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
| `agentci record --name <scenario>` | Run a scenario once and export a standalone replay artifact |
| `agentci replay <trace.jsonl>` | Re-run the recorded scenario; calls outside it fail the run |
| `agentci diff <a.jsonl> <b.jsonl>` | Compare two trace artifacts; exit 0 unchanged, 1 changed |
| `agentci version` | Print the version |

The openai-agents adapter lives in `agentci.integrations.openai_agents`
(`pip install "agentci-py[openai-agents]"`).

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
- run: pip install "agentci-py[openai-agents]"
- run: agentci run --changed

# .github/workflows/release.yml — the verdict before shipping
- run: pip install "agentci-py[openai-agents]"
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

Replay is wired end to end against the Trace Specification v1: `agentci record`
exports a scenario as a standalone artifact, `agentci replay` re-executes it with
every tool call answered from the recording (a call the recording never saw is
refused, recorded as `replay: divergent`, and fails the run), and `agentci diff`
tells you what behavior changed between two artifacts.

The `openai-agents` integration lands the same guarantees on an existing
`agents.Agent` without a rewrite: `AgentCI(root_agent)` re-roots the reachable
graph (handoffs included) so every function tool routes through the registry, and
the SDK runs on a harness-owned event loop with tracing disabled. Model calls are
recorded as `model_call` trace events alongside the tool events. Install it with
`agentci-py[openai-agents]`.

Not yet wired up:

- HTML report rendering. `report.html` is a reserved option with no renderer
  behind it.

The release workflow publishes the `agentci-py` wheel to PyPI on `v*` tags via
trusted publishing; until the first tag is created, install from GitHub.

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
