# ADR-0009: Change-aware selection and the release gate

Status: Accepted

## Context

A full agent suite is slow and gets slower, and most changes cannot plausibly
affect most tests. Skipping the unaffected tests is therefore the obvious
optimization — and the obvious way to ship a broken release, because a
selection engine that is wrong fails *silently*: the tests that would have
caught the regression are precisely the ones that did not run.

Two further facts shape the decision. The information needed to decide what is
affected is a **git diff**, and a git diff is frequently unavailable or
misleading — no remote in a fresh checkout, a shallow clone that cannot compute
a merge base, a developer with uncommitted work. And AgentCI has two audiences
with opposite tolerances: a developer iterating locally wants to be unblocked,
while CI making a release decision must refuse to certify anything it has not
actually checked.

## Decision

**Make selection provably safe rather than merely fast, and split the verdict
into `run` and `gate` so the two audiences get the tolerance they need.**

A test declares what it reads:

```python
@agent_test(dependencies=["prompts/*.txt", "tools/*"])
def test_refund(agent): ...
```

and `agentci run --changed` skips the tests none of the changed files can
affect. Four rules decide what runs:

1. **`None` is not `[]`.** `changed_files()` returns `None` when the diff could
   not be determined and `[]` when it succeeded and found nothing. Only `[]`
   permits skipping. Collapsing them would turn a broken checkout into a green
   run over an empty suite, which is the worst possible outcome for a release
   gate.
2. **A test with no declared dependencies runs.** It cannot be proven
   unaffected, so it is treated as affected by everything — unless
   `selection.unmatched: skip` is explicitly opted into.
3. **A change to a global path runs everything.** Editing `agentci.yaml` changes
   the harness every test runs inside, so no test may claim independence from it.
4. **The base must resolve.** Precedence is `--base`, then the CI environment
   (`AGENTCI_BASE`, `GITHUB_BASE_REF`), then `origin/main`, then
   `selection.default_base`. If all are empty the plan is *unresolved*.

The diff's left-hand side is the **merge base** of `base` and `HEAD`, so a branch
is measured from where it diverged rather than from a `main` that has moved on.
Its right-hand side is the **working tree**, plus untracked non-ignored files, so
a developer's half-finished edit counts as a change. On a CI checkout the two
sides are identical; locally the difference is the whole question.

**`run` and `gate` differ only in how they treat uncertainty:**

| Condition | `agentci run --changed` | `agentci gate` |
| --- | --- | --- |
| Base unresolved | warning, full suite runs | **BLOCK** |
| Selection skips | informational: `run.selection_skipped` + reason per test | informational, does **not** block |
| Run warnings | ignored | **BLOCK** (`--allow-warn` relaxes) |
| Selection disabled | full suite | full suite |

`gate` always enables selection and reduces the whole run to one verdict: exit 0
to ship, non-zero to stop, with every reason printed under
`RELEASE GATE: BLOCKED`. `--allow-warn` lifts warnings only; an unverifiable
release stays unverifiable, so it is never lifted by a display preference.

**Selection skips are not warnings.** A skipped set means the change analysis
determined the tests are unaffected — that is the point of selection, not a sign
of a broken run. Blocking a release because it skipped the tests it could not
scope to would make `unmatched: skip` (the strategy large suites need) unusable
with `gate`. The distinction the table draws: *"I intentionally skipped tests
because the diff cannot affect them"* is not the same risk as *"something went
wrong while determining what to test."* The first is a statement about coverage
and stays visible as `run.selection_skipped` and a per-test reason; the second
is a base ref that cannot be resolved, and it blocks.

## Consequences

- Safety is a property of the *engine*, not the operator. A wrong base produces
  more test executions, never fewer; the only lever that removes tests is a
  resolved diff plus an explicit `unmatched: skip`.
- Selection costs a handful of `git` calls per invocation and no test
  execution, so the fast path is cheap enough to leave on.
- `report.json` gains `run.selection_base`, `run.selection_changed`, and
  `run.selection_skipped` as additive defaulted fields. `schema_version` stays 1:
  consumers reading old reports see `null`/`[]`/`0`, and no existing field
  changes meaning.
- Every skipped test records its reason in `test.error`, so a skipped run is
  auditable rather than merely quiet.
- `selection.enabled: false` is honoured by both commands and means "always run
  everything". `gate` accepts it, because running the full suite never reduces
  coverage — it is only *believing* you scoped a run that did not happen which
  is dangerous.
- The `runner.py`/post-processing split that preceded this work exists so the
  verdict logic is testable without an adapter; a further engine split is
  deferred until replay needs it, not performed speculatively.
