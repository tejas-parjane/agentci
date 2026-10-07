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

## Decision rule

- **All-clear** — every engineer reached `RELEASE GATE: BLOCKED` unaided, and
  could explain the block: publish `v0.1.1`, then recruit early OSS adopters.
- **Any stall** — at least one engineer failed, got silently confused, or
  authored a wrong explanation: fix the README/quickstart first, re-run the
  clean-environment walkthrough, then repeat one or two validations before
  tagging. Passing tests are not product readiness.

Record here the exact fixes the stall required, so the next round only
re-validates the changed surface.