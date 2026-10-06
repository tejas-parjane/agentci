# ADR-0005: License

Status: Accepted

## Context

The PRD asks for MIT vs Apache-2.0 to be evaluated before public launch. The
choice is not about permissions — both are permissive — it is about what
happens when someone embeds AgentCI in a commercial product, or forks it, or
ships an adapter built on it.

## Decision

**Apache-2.0.**

The deciding difference is the express patent grant. Apache-2.0 grants
contributors' patents covering the work and terminates the grant if the
recipient sues over patents relating to the work. MIT has no grant at all, which
matters more than usual for a project whose subject matter — calling tools,
sending prompts, scanning output for credentials — sits close to areas with
dense patent activity.

Secondary reasons:

- `NOTICE` preservation keeps attribution durable through forks, which a
  fast-moving test harness accumulating integrations will need.
- It is what the surrounding ecosystem defaults to for infrastructure, so
  adopters do not need a licence review for this dependency.
- The grant is irrevocable for users who comply, which is the property a CI
  dependency has to have: builds must not be hostage to a future licence change.

## Consequences

- `LICENSE` holds the Apache-2.0 text; `pyproject.toml` declares
  `license = "Apache-2.0"` with `license-files = ["LICENSE"]`, so the licence
  ships inside the wheel rather than only in the repository.
- Contributions are under the same terms, stated in `CONTRIBUTING.md`.
- No source file carries a boilerplate header; the repository-level licence and
  the package metadata are the record, which keeps diffs free of header churn.
