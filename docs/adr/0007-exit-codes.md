# ADR-0007: Exit codes

Status: Accepted

## Context

A CI system learns almost everything about a test run from the process exit
code. Collapsing failures into `0` or `1` loses the distinction between "the
agent got worse" and "the harness never ran" — and the second one, if reported
as the first, sends a maintainer to debug an agent that was never exercised.

A single generic failure code is worse than useless for automation: a pipeline
cannot retry an infrastructure fault, skip publishing on a config error, or
tell an interrupted run from a real one.

## Decision

AgentCI exits with one of seven codes, defined by `ExitCode` in
`agentci.errors` and never by a literal integer at the call site.

| Code | Name | Meaning |
| --- | --- | --- |
| `0` | `PASS` | Every test passed. Also the verdict when warnings exist but `--fail-on-warn` was not given. |
| `1` | `GATE_FAILED` | A test failed, a release gate failed, or a baseline regression blocked. The suite ran and the agent is not good enough. |
| `2` | `CONFIG_ERROR` | `agentci.yaml` is missing, invalid, or fails schema validation. Nothing was executed. |
| `3` | `INFRA_ERROR` | The adapter raised, discovery failed, or an artifact could not be written. Something is broken in the environment or the harness. |
| `4` | `NO_TESTS` | The suite is empty or selection matched nothing. Distinct from `0` so an empty suite cannot pass a gate. |
| `5` | `INTERNAL_ERROR` | An unexpected error inside AgentCI itself. A bug in this project. |
| `130` | `INTERRUPTED` | SIGINT/KeyboardInterrupt, following the shell convention of `128 + SIGINT`. |

The ordering rule inside the runner is separate from and consistent with this:
`ERROR` outranks `FAIL` for the run's status, because a faulted test is what a
maintainer fixes first.

`--fail-on-warn` converts warnings to a failing *verdict* rather than only
changing the exit code, so the artifact and CI agree.

## Consequences

- Callers can `case $? in 3) retry ;; 2) fail config review ;; esac`.
- The codes are part of the public contract: adding one is compatible, changing
  the meaning of an existing one is a breaking change.
- `scripts/verify.py` asserts on them, so a regression in classification fails
  the suite rather than a pipeline.
