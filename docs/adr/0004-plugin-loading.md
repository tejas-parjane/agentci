# ADR-0004: Plugin loading

Status: Accepted (v0.1 scope; entry points deferred)

## Context

The PRD asks for pluggable adapters, policies, and reporters, and offers two
mechanisms: Python entry points, or explicit registration. Entry points are the
better long-term answer — they let `pip install agentci-http` contribute an
adapter without editing anything — but they require the plugin to already be
installed into the same interpreter, and they make "which plugin ran" a
question answered by the environment rather than by the config file in the
repository.

AgentCI also has a hard constraint here: loading a plugin means executing
third-party code, and a test harness that discovers and runs code on import is a
poor place to be guessing.

## Decision

**v0.1 loads adapters by explicit `module.path:attribute` string from
`agentci.yaml`, resolved against the project root.** No entry points, no
auto-discovery, no import-time side effects.

```yaml
agent:
  adapter: "my_agent.agent:run"
```

The same applies to everything else user-supplied: policies are expressed as
data in `policies:` (allowlists, denylists, patterns), mocks as data in
`execution.mocks:`. There is no `plugins:` section.

The loaded target is inspected, not assumed: `_accepts_ctx()` reads the
signature so `(user_input)` and `(user_input, ctx)` both work, and the kind
(class, function, async, generator) is determined by inspection. Everything an
adapter can declare — including `ToolDecl.side_effect` — is declared at that
one address.

Entry points are deferred to a release that ships at least one out-of-tree
plugin, so the mechanism is designed against a real consumer rather than a
hypothetical one.

## Consequences

- Which code runs is fully determined by a file in the repository. No install
  order effects, no stale entry-point caches.
- `adapter` is validated eagerly when the runner is constructed, so a typo
  fails before any test runs, with the project root named in the error.
- A third-party adapter today means a dependency and a path, not a package
  namespace. That is the trade for v0.1.
