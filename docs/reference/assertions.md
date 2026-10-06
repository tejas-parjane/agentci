# Assertion reference

Every assertion is a method on `expect(result)`. They return the same object, so
they chain, though one per line reads better in a test file.

```python
from agentci import agent_test, expect

@agent_test
def refunds_never_exceed_the_order(agent):
    result = agent.run("Refund order #47")

    expect(result).to_contain("refunded")
    expect(result).to_use_tool("issue_refund")
    expect(result).to_call_tool_with("issue_refund", order_id="47")
    expect(result).to_have_max_cost(0.02)
```

## What `expect()` accepts

`expect(result)` takes the `ResultView` returned by `agent.run(...)`. A raw
`AgentResult` works too, which is convenient when driving an adapter by hand.

```python
result = agent.run("Refund order #47")   # ResultView
expect(result).to_contain("refunded")
```

A dict in `AgentResult` shape also validates and works. Anything else raises
`TypeError`. In particular a bare `Trace` is not enough — assertions need the
metrics and output alongside it — and neither is `result.output_text`, since a
string has no trace to assert tool calls against.

## Where failures go

Inside an `@agent_test` body, a failed assertion is recorded and the next
assertion still runs, so one test reports every unmet expectation rather than
only the first. Outside a test body (a plain script), `expect()` is standalone
and raises `AssertionFailed` on the first failure.

A missing prerequisite is reported as `SKIP`, never as a pass: no usage data
means `to_have_max_cost` skips rather than green-lights.

---

## Output

| Assertion | Passes when |
| --- | --- |
| `to_contain(needle, *, case_sensitive=True)` | output contains `needle` |
| `to_not_contain(needle, *, case_sensitive=True)` | output does not contain it |
| `to_equal(expected)` | output is exactly `expected` |
| `to_match(pattern)` | output matches the regex `pattern` |
| `to_be_json()` | output parses as JSON |
| `to_have_fields(fields, *, required_only=False)` | the parsed output has those keys |
| `to_match_json_schema(schema)` | the parsed output satisfies `schema` |

`to_have_fields` checks the top level of the parsed JSON. The default only
requires the listed keys to be present and ignores extras;
`required_only=True` pins the exact key set, so an unexpected key fails it —
that is the mode for pinning a machine-readable contract.

## Tool calls

| Assertion | Passes when |
| --- | --- |
| `to_use_tool(name, *, times=None)` | `name` was called — `times` times if given |
| `to_not_use_tool(name)` | `name` was never called |
| `to_call_tool(*, equals=None, at_least=None, at_most=None)` | the total call count is in range |
| `to_follow_tool_order(expected, *, exact=False)` | the expected calls appear in that order (`exact=True` requires an identical sequence) |
| `to_have_max_tool_calls(limit)` | total calls never exceed `limit` |
| `to_call_tool_with(name, expected=None, *, strict=False, every_call=True, **kwargs)` | arguments matched |
| `to_call_tool_matching_schema(name, schema)` | arguments satisfy `schema` |
| `to_succeed(name)` | every call to `name` completed successfully |
| `to_not_retry(name, *, at_most=0)` | `name` was retried no more than `at_most` times |

`to_call_tool_with` accepts either spelling:

```python
expect(result).to_call_tool_with("issue_refund", order_id="47")
expect(result).to_call_tool_with("issue_refund", {"order_id": "47"})
```

`strict=True` requires an exact argument set; by default the expectation is a
subset. `every_call=True` (the default) requires *each* call to `name` to match;
`False` checks only the first call, which is how you pin the arguments of the
initial call without constraining later retries.

`to_call_tool_with` **raises** `ExpectationError` if the trace contains no tool
calls at all, rather than reporting a failed assertion. The distinction matters:
a test that asserts arguments and got none is usually misusing the harness, and
the message says so — either the agent did not use tools, or it called them
directly instead of routing through `ctx.tools`.

`to_succeed` handles the empty case by failing normally, with
"no completed call to `name` was recorded".

## Execution budgets

| Assertion | Passes when |
| --- | --- |
| `to_have_max_latency(limit_ms)` | wall-clock latency ≤ `limit_ms` |
| `to_have_max_cost(limit_usd)` | computed cost ≤ `limit_usd` |
| `to_have_max_tokens(limit)` | total tokens ≤ `limit` |
| `to_have_max_retries(limit)` | retries ≤ `limit` |
| `to_have_max_steps(limit)` | steps ≤ `limit` |
| `to_not_loop(*, threshold=3)` | no tool call repeats `threshold` times |

Cost and token assertions report `SKIP` when the run recorded no usage — never
`PASS`. An unmeasured run must not look like a cheap one.

`to_not_loop` is redundantly detected on purpose. A fingerprint is
`tool(arguments)`, and it trips if that fingerprint appears `threshold` times
*anywhere* in the run, `threshold` times *consecutively*, or `threshold` times
inside a window of `2 * threshold` calls (`window=` overrides that span). The
windowed check is what catches an agent oscillating `A, B, A, B, ...`, which
adjacency alone would never flag.

## Policy

| Assertion | Passes when |
| --- | --- |
| `to_only_use_tools(allowed)` | every tool called is in `allowed` |
| `to_require_approval_for(tool)` | the run recorded an explicit approval for `tool` |
| `to_demand_approval_for(tool)` | an approval was *requested* before `tool` ran |
| `to_have_no_policy_violations()` | the policy engine found nothing |
| `to_have_no_live_side_effects()` | no tool executed against a real target |
| `to_not_contain_forbidden_data(patterns)` | no pattern appears anywhere in the run |

`to_require_approval_for` and `to_demand_approval_for` are deliberately
different: the first asserts approval was *granted and recorded*, the second
that approval was *asked for*. A test that only wants to know the agent did not
skip the gate uses the second; one that needs the approval on the record uses
the first.

`to_have_no_live_side_effects` is the assertion form of
`execution.external_side_effects: deny`. Calls with a `MOCKED` or `DENIED`
status pass; a tool that actually ran does not.

## Leakage

| Assertion | Passes when |
| --- | --- |
| `to_not_leak(*patterns)` | no pattern or literal appears anywhere in the run |
| `to_not_leak_environment_secrets()` | no environment-derived secret reached the run |

Both search the trace as well as the output — a secret that only ever appeared
in a tool argument still fails.

Each argument to `to_not_leak` is either the name of a built-in pattern or a
literal substring to look for:

| Built-in pattern names |
| --- |
| `email`, `ssn`, `jwt`, `api_key`, `authorization`, `credit_card`, `phone`, `private_key`, `aws_access_key` |

```python
expect(result).to_not_leak_environment_secrets()
expect(result).to_not_leak("email", "credit_card")            # named patterns
expect(result).to_not_leak("sk_live_", "ACCT-9931")           # project-specific
```

## Related

- What gates do outside the test body: [exit codes](../adr/0007-exit-codes.md)
- When redaction runs relative to assertions:
  [ADR-0008](../adr/0008-redaction-at-serialization.md)
- The budget and threshold keys these assertions read:
  [Configuration reference](configuration.md)
