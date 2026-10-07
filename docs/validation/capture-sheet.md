# Engineer validation — capture sheet

One row per engineer. Fill this privately; the brief handed to the engineer is
`brief-to-engineer.md`. Aggregate the five signals at the bottom, then apply
the decision rule.

Engineer initials / date:
Environment (OS, Python version):
Install path used (`pip install agentci-py…` from PyPI? wheel? `git+`?):
Was a virtualenv clean at start?:

## Signals

1. **Time to first successful run** (`agentci run` passes)
   - elapsed:
   - single install attempt or iterations:
2. **Where they got stuck** (quote the error / the sentence they couldn't parse)
   - step number:
   - what they expected instead:
   - did they stop, or push through?:
3. **Can they explain `BLOCK`?**
   - their own words (`agentci gate` prints `RELEASE GATE: BLOCKED`):
   - accurate (they can say why it blocked: a test fails / replay diverged)?:
4. **Would they put it in CI?**
   - explicitly asked?
   - their answer + one-line reason:
5. **What would they expect next?** (their words — roadmap signal)

## Closing questions (verbatim quotes)

- "Would you trust this to block a production agent release? Why or why not?"
- "What would you expect to exist next for you to consider using it in a real
  project?"

## Aggregation

| Signal | Notes / quotes | Verdict (clear-confusing-mixed) |
| --- | --- | --- |
| Time to first run | | |
| Stuck point | | |
| Explains `BLOCK` | | |
| Would put in CI | | |
| Expected next | | |

## Triage rubric (apply to every finding)

Fixes before release are **P0 and P1 only**. P2 is logged on the roadmap and
does not move the release.

- **P0 — blocks the protocol.** Can't install/run; can't understand how to
  record; replay doesn't reproduce; diff doesn't identify the behavioral
  change; gate doesn't correctly block the regression; Python 3.11/3.12
  incompatibility.
- **P1 — significant friction.** README ambiguity, confusing CLI, unclear error
  messages, unclear trace/replay artifacts, unclear explanation of why a
  release was blocked.
- **P2 — feature requests.** More frameworks, dashboards, more assertions,
  cloud features, more integrations. Log, defer, do not fix before the tag.

## Decision rule

- **The primary qualitative signal** is not "did it work" but: *after seeing
  `RELEASE GATE: BLOCKED`, could the engineer immediately explain what changed,
  why it matters, and what to fix?* A consistent "it blocked but I don't know
  why" outweighs any feature request.
- **All-clear** — every engineer reached `RELEASE GATE: BLOCKED` unaided and
  explained the block; no P0/P1 findings: publish `v0.1.1`.
- **Tag condition** — if several engineers independently say "I would put this
  in CI," that is the adoption signal to tag `v0.1.1`.
- **Any stall** — at least one engineer failed, got silently confused, or
  authored a wrong explanation, or any P0/P1 finding landed: fix the
  README/quickstart first, walk the clean environment again, and re-validate
  one or two engineers before tagging. Passing tests are not product readiness.

Record here the exact fixes the stall required, so the next round only
re-validates the changed surface.