## What failure mode does this address?

<!-- A bug's mechanism, or the gap you cannot currently test for. "Add X" is a
     title, not a description. -->

## How do you know it is fixed?

<!-- The test, probe, or reproduction that fails before and passes after. If
     nothing demonstrates it, say why that is acceptable. -->

## Contract impact

Anything checked here needs an ADR in `docs/adr/` and a `CHANGELOG.md` entry
before this can merge.

- [ ] `report.json` schema
- [ ] `agentci.yaml` keys or semantics
- [ ] Exit codes or CLI output
- [ ] Adapter interface (`AgentAdapter`, `AgentResult`)
- [ ] On-disk layout under `.agentci/`
- [ ] New dependency
- [ ] None of the above

## Checks

All four, run from the repository root:

- [ ] `ruff check .`
- [ ] `mypy src`
- [ ] `python -m pytest`
- [ ] `python scripts/verify.py`

`verify.py` asserts that seven negative-path probes **fail**. If a probe now
passes, that is a regression in AgentCI — please do not adjust the probe.

## Notes for the reviewer

<!-- Anything subtle: the alternative you rejected, a branch that only exists to
     handle a specific failure, a comment in the code that explains the why. -->
