# Contributing to AgentCI

Thanks for helping. AgentCI is a test harness: a change that quietly weakens a
verdict is worse than no change at all, so this guide is short on style nits and
long on the checks that exist to catch that.

## Getting set up

```bash
git clone https://github.com/tejas-parjane/agentci.git
cd agentci
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"      # .venv/bin/pip on macOS/Linux
```

Python 3.11 or newer. Everything below is run from the repository root.

## The four commands

```bash
ruff check .            # lint and import order
mypy src                # strict typing; warnings are errors
python -m pytest        # unit tests
python scripts/verify.py
```

The fourth one is the one people skip and should not. It runs the worked example
suite and then asserts that **seven probes fail** — a credential leak, a step
limit, a policy denial, a bare `assert`, an infrastructure fault, and two more.
If you change something that makes a probe pass, `verify.py` exits non-zero.
That is the harness testing itself.

All four must pass before a pull request is reviewed.

## Where tests live

| Location | What it is | Run by |
| --- | --- | --- |
| `tests/` | Unit tests of AgentCI itself | `python -m pytest` |
| `tests/agentci/` | The example suite, decorated `@agent_test` | `agentci run` (via `verify.py`) |
| `examples/` | The example agent, including deliberately broken ones | the example suite |

`tests/agentci/` is excluded from pytest on purpose: those functions take an
injected `agent`, not pytest fixtures, so collecting them would fail every one.

## Writing a change

- **Explain the why, not the what.** Docstrings and comments here carry the
  reasoning that the code cannot — which failure mode a branch exists to prevent,
  what the alternative was. A comment that restates the line above it is noise.
- **Decisions need an ADR.** Anything that changes a contract (config schema,
  report schema, exit codes, adapter interface, on-disk layout, licence) gets a
  record in `docs/adr/`, numbered in sequence. Reference it from the code that
  implements it.
- **Additive where it matters.** `report.json` and `agentci.yaml` are versioned;
  changing the meaning of an existing field needs a version bump, adding one does
  not.
- **No new dependencies without an ADR.** The dependency surface is deliberately
  small (ADR-0006), and a runtime network call is never acceptable.

## Style

- Line length 100, `ruff` formats the rest. Do not fight the linter with
  per-file ignores unless you can say in a comment why the rule is wrong here.
- `mypy --strict` over `src/`. If a type needs `# type: ignore`, say which error
  and why it cannot be expressed.
- Public behaviour gets a test in `tests/`. Prefer a test that fails when the
  behaviour regresses over one that merely exercises the line.

## Pull requests

- One concern per PR. A refactor mixed with a behaviour change cannot be
  reviewed safely.
- Describe the failure mode the change addresses, and how you know it is fixed.
- If it changes output — reports, exit codes, CLI text, config — say so in the
  PR body; those are the parts other people's pipelines depend on.
- Update `CHANGELOG.md` under an `Unreleased` heading for anything user-visible.

## Reporting bugs and security issues

Bugs go to [GitHub Issues](https://github.com/tejas-parjane/agentci/issues) with
a minimal reproduction. Anything that is a vulnerability — a leaked secret, a
sandbox escape, a policy that can be bypassed — must go through
[SECURITY.md](SECURITY.md) instead, not a public issue.

## Licence

By contributing you agree your contribution is licensed under Apache-2.0, the
same licence as the project (see [ADR-0005](docs/adr/0005-license.md)).
