# ADR-0008: Redaction happens at the serialization boundary

Status: Accepted

## Context

Two requirements pull against each other. Assertions must see **real values** —
`expect(result).to_not_leak(...)` is meaningless if the input was already
scrubbed, and a policy check that inspects tool arguments has to see the
argument that was actually passed. Artifacts must contain **no secrets**,
because `report.json` and `trace.jsonl` are uploaded as CI artifacts, pasted
into issues, and diffed.

Redacting early satisfies the second requirement and destroys the first.
Redacting late satisfies the first and risks forgetting a path.

## Decision

**Redact where values become durable, and nowhere else.**

Assertions, policy evaluation, and budgets all read the live objects. Redaction
is applied by `Redactor` inside `to_dict()`/serialization and by `redact_report`
inside every renderer, so any path that can reach disk or a terminal passes
through it. In-process, nothing is altered.

Ordering inside a redactor is deliberate:

1. **Always-on credential patterns** (`api_key`, `Authorization: Bearer …`,
   private-key blocks, …) fire first, independent of configuration. A key shaped
   like a credential is redacted whether or not anyone registered its value.
2. **Configured `patterns:`** and sensitive **`fields:`** (by name, substring
   match) next — `password`, `cookie`, and anything the project adds.
3. **Literal secret values** from `secrets:` and from secret-looking environment
   variables (`scrub_environment`) last, so a known secret is replaced even
   where it appears in free text.

Doing (3) first would let a credential-shaped key survive because its value
never happened to be registered; doing (1) first means the common case is
covered with no configuration at all.

Output is `[REDACTED:<name>]`, naming *which* control fired rather than
replacing with a uniform mask — a report that says the field was `api_key`
tells a reviewer the redaction was expected, where `████████` says only that
something matched.

## Consequences

- `expect()` sees exactly what the agent produced; the file does not. This is
  tested by asserting on real values while the written artifact is checked for
  the placeholder.
- Redaction is a property of writers, not of readers, so a new artifact type is
  safe only if it serializes through `to_dict()` or a renderer. New outputs must
  be added to a renderer rather than dumped with `json.dumps` directly.
- Over-redaction is possible and is a configuration concern, not a safety one:
  it fails a test visibly rather than leaking silently.
- Disabling redaction (`redaction.enabled: false`) produces a config warning on
  every run.
