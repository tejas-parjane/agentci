# Writing an adapter

An adapter is the only thing AgentCI knows about your agent. It is deliberately
one method: assertions, policy, reports, and replay are all derived from what
the adapter reports, so the contract stays small enough to satisfy by hand.

An adapter is *not* a test. It is the agent under test — the same code in CI as
in production, minus the parts a test is not allowed to reach.

Point `agentci.yaml` at it:

```yaml
agent:
  adapter: "my_agent.agent:run"   # module.path:attribute, resolved from the root
```

---

## Three shapes, in decreasing order of power

### 1. A class (recommended)

Gives you tool declarations and policy mediation.

```python
from agentci.adapters.base import BaseAdapter
from agentci.core.context import RunContext
from agentci.core.result import AgentResult
from agentci.core.tools import ToolDecl


class SupportAgent(BaseAdapter):
    name = "support-agent"

    tools = {
        "lookup_ticket": ToolDecl(name="lookup_ticket", live=lookup_ticket),
        "issue_refund": ToolDecl(name="issue_refund", live=issue_refund, side_effect=True),
    }

    def run(self, user_input: str, ctx: RunContext) -> AgentResult:
        ticket = ctx.tools.lookup_ticket(ticket_id="T-1001")
        return AgentResult(output_text=f"Status is {ticket['status']}")
```

`ctx.tools.<name>(...)` is a proxy, not your function: the allowlist, mocks, and
side-effect gate all run before your code does. See
[policy mediation](#tools-policy-and-side-effects).

### 2. A plain function

The quickest path to a first test.

```python
from agentci.core.result import AgentResult


def run(user_input: str) -> AgentResult:
    return AgentResult(output_text=f"you said: {user_input}")
```

### 3. A generator

For streaming agents. Yield events as you produce them, return the result:

```python
def stream_agent(user_input: str):
    yield TraceEvent(type=EventType.RETRIEVAL_STARTED, ...)
    return AgentResult(output_text="...")
```

---

## The `ctx` object

`RunContext` is your window into AgentCI. It is intentionally small:

| Member | What it is |
| --- | --- |
| `ctx.tools` | The policy-enforcing tool proxy. Never call your tools directly. |
| `ctx.recorder` (alias `ctx.trace`) | Emit events: steps, model calls, errors. |
| `ctx.run_id` | This invocation's id, for correlating your own logs. |
| `ctx.max_steps`, `ctx.max_duration_ms` | Read-only budget limits. |
| `ctx.iteration` | Which repetition of `evaluation.repeat` this is. |
| `ctx.attributes` | Free-form key/value, available to assertions. |
| `ctx.set(key, value)` | Same, as a method. |
| `ctx.model_call(model, **kwargs)` | Context manager recording a model invocation. |
| `ctx.record_usage(model, usage, cost_usd)` | Report usage when a context manager will not fit (streaming SDKs). |

If the adapter signature does not name `ctx`, it is not passed —
`(user_input)` and `(user_input, ctx)` are both accepted.

---

## Tools, policy, and side effects

Declare tools so policy has something to enforce. Two constructors:

```python
from agentci.core.tools import tool

tool.live(lookup_ticket)                          # has a real implementation
tool.remote("delete_ticket", side_effect=True)     # no live body; must be mocked
```

- **`tool.live`** wraps a callable. Its name defaults to `fn.__name__`.
- **`tool.remote`** declares a tool whose real implementation lives behind a
  boundary a test should not cross. With `execution.external_side_effects: deny`
  it fails safely rather than calling anything.

Mark anything that changes state outside the process:

```python
tool.live(issue_refund, side_effect=True)
```

A side-effecting tool is refused unless it is mocked, whether or not you
declared it — `execution.side_effecting_tools` lists the same names as a
belt-and-braces override.

Order of checks on every call:

1. **Mock** from `execution.mocks` — checked first, because a mocked call is not
   a side effect.
2. **Allowlist** in `policies.allowed_tools` (an empty allowlist permits all
   *declared* tools).
3. **Side-effect gate** under `execution.external_side_effects`.
4. Your real implementation.

Calling a tool you did not declare raises rather than falling through:

```text
AttributeError: tool 'shell_exec' is not declared. Declare it on the adapter:
tools={'shell_exec': tool.live(...)} or list it in policies.allowed_tools.
```

---

## Recording model calls and cost

Cost assertions need usage on a `model_call` event. Without it they evaluate to
`SKIP` — never `PASS` — which is the honest verdict for "did not measure this":

```python
with ctx.recorder.model_call("gpt-5-mini", prompt=user_input) as call:
    answer = llm.complete(user_input)
    call.usage = TokenUsage(prompt_tokens=120, completion_tokens=45)
```

When a context manager will not fit — wrapping a streaming SDK, say — report it
once the call finishes instead:

```python
ctx.record_usage("gpt-5-mini", TokenUsage(prompt_tokens=120, completion_tokens=45))
```

If usage is absent but `agent.model` has a bundled price, AgentCI still has
nothing to multiply it by, so the gate stays `SKIP`. The report records
`pricing_as_of` so a figure you do get is never silently stale.

---

## Emitting events vs returning them

Both work, and the runner merges them by `event_id` without duplication:

```python
# emit while working
ctx.recorder.memory_read("customer:T-1001")
ctx.set("plan", "refund")
return AgentResult(output_text="done")

# or return them
return AgentResult(output_text="done", trace=[event, ...])
```

Emitting is better for long-running agents, because a run aborted by a budget
still has its partial trace on disk.

---

## Sync and async

Both are supported; the runner detects it. `async def run(...)` works the same
as `def run(...)`.

---

## Checklist

- [ ] `agentci.yaml` `agent.adapter` resolves (it is validated before tests run).
- [ ] Every tool the agent can call is declared, with `side_effect=True` where
      applicable.
- [ ] Tools are called through `ctx.tools`, never directly.
- [ ] Token usage is reported, or you accept `SKIP` on cost assertions.
- [ ] The adapter does not perform real side effects during a test — configure
      mocks instead.

The worked example, including deliberately broken agents for each failure mode,
is in `examples/support_agent/agent.py`.
