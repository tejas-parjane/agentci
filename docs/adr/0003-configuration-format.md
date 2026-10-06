# ADR-0003: Configuration format

Status: Accepted

## Context

`agentci.yaml` decides which tools an agent may call, what a run costs before it
is allowed to finish, and which paths are treated as test files. Every one of
those is a safety control. A configuration language that silently ignores a key
it does not recognize turns a typo into a disabled control:

```yaml
policies:
  denied_tool: [delete_ticket]   # real key is denied_tools
```

Under lenient parsing this is a no-op and the tool runs.

## Decision

**YAML**, parsed by a **strict typed Pydantic schema**.

- `version:` is required at the top level and must equal `CONFIG_VERSION`
  (`1`). A breaking change increments it, so an old config on a new AgentCI is
  an explicit error instead of a silent reinterpretation.
- Unknown keys are errors anywhere, with the dotted location of the offender
  (`policies.denied_tool`) in the message. Not warnings: a warning scrolls past
  in CI, and this one means a gate is off.
- Every relative path resolves against the project root, never the process
  working directory, so `agentci run --root <dir>` behaves the same from
  anywhere.
- Reported keys stay snake_case in YAML via field aliases rather than Python
  property overrides — a `json` property on a Pydantic model shadows
  `BaseModel.json()` and breaks serialization in a way no type checker notices.

TOML was considered and rejected: it has no notion of nested structures typed as
lightly as `mocks: {tool: {response: ...}}` reads in YAML, and JSON loses
comments, which a file whose comments explain *why a tool is denied* needs.

## Consequences

- Configs are validated before a single test runs, so a bad key fails in
  milliseconds with a path to the line.
- `agentci config` dumps the resolved configuration, aliases included, which is
  how you confirm what a config actually means rather than what it looks like.
- Adding a config key is backwards compatible; removing or retyping one requires
  a `version:` bump.
