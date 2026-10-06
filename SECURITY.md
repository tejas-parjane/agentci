# Security policy

## Reporting a vulnerability

**Do not open a public issue for a vulnerability.**

Report it privately through
[GitHub Security Advisories](https://github.com/tejas-parjane/agentci/security/advisories/new).
That opens a private thread visible only to the maintainers, and gives us a
channel to coordinate a fix and a disclosure date.

Include, if you have it:

- what an attacker gains, and from which position (local user, CI job, someone
  reading an artifact)
- a minimal reproduction — a config, a trace, or a test that shows it
- affected version (`agentci version`)

If you cannot use advisories, contact the maintainer privately through the
address on their GitHub profile and mention it is a security report.

## What counts

In scope:

- a secret reaching `report.json`, `trace.jsonl`, a terminal, or a CI log when
  redaction should have caught it
- a side effect executing under `execution.external_side_effects: deny`
- a policy check that can be bypassed — allowlist, approval, forbidden-data
- path traversal or arbitrary file write through a configured path
- code execution beyond what installing and running AgentCI already implies

Out of scope:

- an agent behaving badly because a test asserted nothing about it
- a secret you put in the report yourself with `redaction.enabled: false` (we
  warn on every run, but we cannot stop you)
- dependency CVEs with no demonstrated impact on the above; report them to the
  upstream project

## Threat model worth stating plainly

AgentCI **runs arbitrary Python**. Test files and adapters are executed by
design, exactly like `pytest` — there is no sandbox, and a config pointing
`agent.adapter` at a path is a code-loading primitive. Treat `agentci.yaml` and
`tests/` with the same trust you give your own source tree, and never run it
against a repository you do not trust.

AgentCI also **handles secrets it does not need to see**. Its job is to assert on
output that may contain them, which is why redaction happens at the
serialization boundary ([ADR-0008](docs/adr/0008-redaction-at-serialization.md))
and why the always-on credential patterns fire before any configuration of
yours. If you find a value that reaches disk unredacted, that is a bug, not a
misconfiguration, unless redaction was disabled.

And AgentCI **makes no outbound network calls** ([ADR-0006](docs/adr/0006-telemetry-defaults.md)).
If you observe one, that is a finding regardless of impact.

## Supported versions

Security fixes land on the latest release only. There are no long-term support
branches yet.

| Version | Supported |
| --- | --- |
| `0.1.x` | yes |
| older | no |

## Disclosure

We aim to acknowledge a report within five working days, and to ship a fix or a
public explanation within thirty days of confirmation — faster for anything
actively exploitable. Credit is given in the advisory unless you prefer not to
be named.
