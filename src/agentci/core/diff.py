"""Structural trace comparison: what *behavior* changed between two artifacts.

Per the Trace Specification v1 §8, identity/timing are recorded values but the
behavioral line (types, order, tool identity, statuses) is the contract. This
module diffs that line and reports the rest as measurements: a trace diff tells
you what the agent now *does* differently, and separately how much slower or
costlier that behavior is.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from agentci.core.trace import EventStatus, EventType, TraceEvent


@dataclass(frozen=True)
class BehaviorToken:
    """One unit of observable behavior, in execution order."""

    kind: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[Any, ...]:
        items = tuple(sorted(self.args.items(), key=lambda item: repr(item[0])))
        return (self.kind, self.name, repr(items))


def behavior_tokens(events: list[TraceEvent]) -> list[BehaviorToken]:
    """The behavioral line of a trace: starts of work, in emission order.

    Completed events are folded into the ongoing invocation; outcomes live in
    the summary below rather than the line, so a *sequence* comparison stays
    crisp while status counts (success/mocked/denied/error) are still reported.
    """
    tokens: list[BehaviorToken] = []
    for event in events:
        if event.type is EventType.TOOL_CALL_STARTED and event.tool is not None:
            tokens.append(BehaviorToken("tool", event.tool.name, dict(event.tool.arguments)))
        elif event.type is EventType.MODEL_CALL_STARTED:
            tokens.append(BehaviorToken("model", event.component or ""))
        elif event.type is EventType.RETRIEVAL_STARTED:
            tokens.append(BehaviorToken("retrieval", event.component or ""))
        elif event.type in (EventType.MEMORY_READ, EventType.MEMORY_WRITE):
            tokens.append(BehaviorToken(event.type.value, event.component or ""))
        elif event.type in (EventType.APPROVAL_GRANTED, EventType.APPROVAL_DENIED):
            tokens.append(BehaviorToken(event.type.value, _name_of(event)))
        elif event.type is EventType.ERROR:
            tokens.append(BehaviorToken("error", event.component or "", {"message": event.error or ""}))
    return tokens


def _name_of(event: TraceEvent) -> str:
    if event.tool is not None:
        return event.tool.name
    return event.component or ""


def _lcs(a: list[BehaviorToken], b: list[BehaviorToken]) -> list[tuple[int, int]]:
    """Longest common subsequence of index pairs, in order."""
    n, m = len(a), len(b)
    table = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            table[i][j] = (
                table[i + 1][j + 1] + 1
                if a[i].key() == b[j].key()
                else max(table[i + 1][j], table[i][j + 1])
            )
    pairs: list[tuple[int, int]] = []
    i = j = 0
    while i < n and j < m:
        if a[i].key() == b[j].key():
            pairs.append((i, j))
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _totals(events: list[TraceEvent]) -> dict[str, float]:
    latency = sum(e.duration_ms for e in events if e.duration_ms is not None)
    cost = sum(e.cost_usd for e in events if e.cost_usd is not None)
    tokens = sum(e.usage.total_tokens for e in events if e.usage and e.usage.total_tokens)
    return {"latency_ms": latency, "cost_usd": cost, "tokens": tokens}


def _counts(events: list[TraceEvent]) -> dict[str, Any]:
    return {
        "tool_calls": sum(1 for e in events if e.type is EventType.TOOL_CALL_STARTED),
        "model_calls": sum(1 for e in events if e.type is EventType.MODEL_CALL_STARTED),
        "retrievals": sum(1 for e in events if e.type is EventType.RETRIEVAL_STARTED),
        "memory": sum(1 for e in events if e.type in (EventType.MEMORY_READ, EventType.MEMORY_WRITE)),
        "approvals_granted": sum(1 for e in events if e.type is EventType.APPROVAL_GRANTED),
        "approvals_denied": sum(1 for e in events if e.type is EventType.APPROVAL_DENIED),
        "errors": sum(1 for e in events if e.status is EventStatus.ERROR or e.type is EventType.ERROR),
        "denied": sum(1 for e in events if e.status is EventStatus.DENIED),
        "mocked": sum(1 for e in events if e.status is EventStatus.MOCKED),
    }


@dataclass(frozen=True)
class TraceDiff:
    a_label: str
    b_label: str
    additions: list[BehaviorToken]
    removals: list[BehaviorToken]
    reorders: list[BehaviorToken]
    a_summary: dict[str, Any]
    b_summary: dict[str, Any]

    @property
    def behavior_changed(self) -> bool:
        return bool(self.additions or self.removals or self.reorders)

    def delta(self, key: str) -> float:
        a = self.a_summary.get(key, 0)
        b = self.b_summary.get(key, 0)
        return float(b) - float(a)

    def to_dict(self) -> dict[str, Any]:
        def token_line(tokens: list[BehaviorToken]) -> list[dict[str, Any]]:
            return [{"kind": t.kind, "name": t.name, "arguments": t.args} for t in tokens]

        return {
            "behavior": "changed" if self.behavior_changed else "unchanged",
            "base": self.a_label,
            "head": self.b_label,
            "additions": token_line(self.additions),
            "removals": token_line(self.removals),
            "reorders": token_line(self.reorders),
            "counts": {
                key: [self.a_summary.get(key, 0), self.b_summary.get(key, 0)]
                for key in ("tool_calls", "model_calls", "retrievals", "memory", "errors", "denied", "mocked")
            },
            "approvals": {
                "granted": [self.a_summary.get("approvals_granted", 0), self.b_summary.get("approvals_granted", 0)],
                "denied": [self.a_summary.get("approvals_denied", 0), self.b_summary.get("approvals_denied", 0)],
            },
            "measurements": {
                key: [self.a_summary.get(key, 0), self.b_summary.get(key, 0), self.delta(key)]
                for key in ("latency_ms", "cost_usd", "tokens")
            },
        }

    def render(self) -> str:
        lines = [f"trace diff: {self.a_label} -> {self.b_label}", ""]
        for key, label in (
            ("tool_calls", "tool calls"),
            ("model_calls", "model calls"),
            ("retrievals", "retrievals"),
            ("memory", "memory ops"),
            ("errors", "errors"),
            ("denied", "denied"),
            ("mocked", "mocked"),
        ):
            a = self.a_summary.get(key, 0)
            b = self.b_summary.get(key, 0)
            marker = f"  ({(b - a):+d})" if b != a else ""
            lines.append(f"  {label:<12} {a} -> {b}{marker}")
        approved_a = (self.a_summary.get("approvals_granted", 0), self.a_summary.get("approvals_denied", 0))
        approved_b = (self.b_summary.get("approvals_granted", 0), self.b_summary.get("approvals_denied", 0))
        if approved_a != approved_b:
            lines.append(f"  approvals    {approved_a[0]} granted, {approved_a[1]} denied -> {approved_b[0]} granted, {approved_b[1]} denied")
        for key, unit in (("latency_ms", "ms"), ("tokens", "tokens")):
            a = self.a_summary.get(key, 0)
            b = self.b_summary.get(key, 0)
            if a != b:
                lines.append(f"  {key:<12} {a:.0f}{unit} -> {b:.0f}{unit}  (+{self.delta(key):.0f}{unit})")
        a_cost = self.a_summary.get("cost_usd", 0)
        b_cost = self.b_summary.get("cost_usd", 0)
        if a_cost != b_cost:
            lines.append(f"  cost_usd     ${a_cost:.4f} -> ${b_cost:.4f}  ({self.delta('cost_usd'):+.4f})")

        verdict = "UNCHANGED" if not self.behavior_changed else "CHANGED"
        if self.additions:
            lines += ["", "  added:"]
            lines += [f"    {t.name}  ({_args(t)})" for t in self.additions]
        if self.removals:
            lines += ["", "  removed:"]
            lines += [f"    {t.name}  ({_args(t)})" for t in self.removals]
        if self.reorders:
            lines += ["", "  moved:"]
            lines += [f"    {t.name}  ({_args(t)})" for t in self.reorders]

        words = []
        if self.additions:
            words.append(f"{len(self.additions)} addition(s)")
        if self.removals:
            words.append(f"{len(self.removals)} removal(s)")
        if self.reorders:
            words.append(f"{len(self.reorders)} move(s)")
        lines += ["", f"BEHAVIOR: {verdict}" + (f" ({', '.join(words)})" if words else "")]
        return "\n".join(lines)


def _args(token: BehaviorToken) -> str:
    return ", ".join(f"{key}={value!r}" for key, value in token.args.items()) or "no arguments"


def trace_diff(a_events: list[TraceEvent], b_events: list[TraceEvent], *, a_label: str, b_label: str) -> TraceDiff:
    """Compare two traces and classify the behavioral change."""
    a_tokens = behavior_tokens(a_events)
    b_tokens = behavior_tokens(b_events)
    aligned = _lcs(a_tokens, b_tokens)
    matched_a = {i for i, _ in aligned}
    matched_b = {j for _, j in aligned}
    removals = [t for i, t in enumerate(a_tokens) if i not in matched_a]
    additions = [t for j, t in enumerate(b_tokens) if j not in matched_b]

    by_key_a: dict[tuple[Any, ...], list[BehaviorToken]] = {}
    for token in removals:
        by_key_a.setdefault(token.key(), []).append(token)
    reorders: list[BehaviorToken] = []
    kept_additions: list[BehaviorToken] = []
    for token in additions:
        key = token.key()
        bucket = by_key_a.get(key)
        if bucket:
            bucket.pop()
            reorders.append(token)
        else:
            kept_additions.append(token)

    # A removal whose twin came back elsewhere in the line is a *reorder*, not a
    # removal; only tokens without a counterpart stay in the removed column.
    reorder_keys = Counter(token.key() for token in reorders)
    actual_removals: list[BehaviorToken] = []
    for token in removals:
        key = token.key()
        if reorder_keys[key] > 0:
            reorder_keys[key] -= 1
        else:
            actual_removals.append(token)

    return TraceDiff(
        a_label=a_label,
        b_label=b_label,
        additions=kept_additions,
        removals=actual_removals,
        reorders=reorders,
        a_summary={**_totals(a_events), **_counts(a_events)},
        b_summary={**_totals(b_events), **_counts(b_events)},
    )


__all__ = ["BehaviorToken", "TraceDiff", "behavior_tokens", "trace_diff"]
