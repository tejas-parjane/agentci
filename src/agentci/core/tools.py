"""Tool declaration, interception, mocking, and side-effect control.

This module is the **single interception point** for everything the agent does to
the outside world. Routing tools through ``ctx.tools`` gives four guarantees that
post-hoc trace inspection alone cannot:

* **Policy is enforced before execution** (PRD §FR-3, AC-6). A denied tool never
  runs.
* **Destructive tools are blocked by default** (AC-10). ``external_side_effects:
  deny`` is the default posture.
* **Mocks substitute deterministically** (AC-12), so a destructive or expensive
  tool can be exercised in CI without live execution.
* **Every call is traced**, including mocks, so assertions and reports see one
  consistent trajectory.

Defence in depth: adapters that call functions directly (never touching
``ctx.tools``) are *not* protected at runtime, but the policy engine still
detects the violation afterwards from the trace. Agreed runtime enforcement,
detected-but-late enforcement, and full trace visibility — three layers, no gap.

Interception requires adapter cooperation, so adapters are pointed at
``ctx.tools.<name>(...)``. See ``docs/guides/adapters.md``.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from agentci.core.recorder import TraceRecorder
from agentci.core.trace import EventStatus, TokenUsage
from agentci.errors import PolicyViolationError, SideEffectBlocked


class SideEffectPolicy(str, enum.Enum):
    DENY = "deny"
    ALLOW = "allow"


class PolicyDecision(str, enum.Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True, slots=True)
class ToolDecl:
    """How a single tool was declared."""

    name: str
    live: Callable[..., Any] | None = None
    side_effect: bool = False
    description: str | None = None


@dataclass(slots=True)
class MockEntry:
    """A recorded or configured stand-in for a tool.

    ``match_arguments`` controls lookup strictness. Mocked replay sets it to
    ``True`` so a tool called with *different* arguments than recorded fails
    loudly instead of silently returning a stale response — a silent wrong mock
    is worse than no mock.
    """

    response: Any
    match_arguments: bool = False
    arguments: dict[str, Any] = field(default_factory=dict)
    source: str = "config"

    def matches(self, arguments: Mapping[str, Any]) -> bool:
        if not self.match_arguments:
            return True
        return dict(arguments) == dict(self.arguments)


class ToolRegistry:
    """The object exposed to adapters as ``ctx.tools``.

    Supports three access styles, all equivalent::

        ctx.tools.get_order(order_id="123")     # attribute style
        ctx.tools["get_order"](order_id="123")  # item style
        ctx.tools.call("get_order", order_id="123")

    Use :meth:`live` to obtain a deliberately unmediated reference to the real
    implementation. It is still traced, but bypasses mocks and the side-effect
    gate — so it refuses to run when policy denies external side effects.
    """

    def __init__(
        self,
        declarations: Mapping[str, ToolDecl] | None = None,
        *,
        recorder: TraceRecorder | None = None,
        mocks: Mapping[str, MockEntry] | None = None,
        denied_tools: set[str] | None = None,
        allowed_tools: set[str] | None = None,
        approval_required: set[str] | None = None,
        granted_approvals: set[str] | None = None,
        side_effect_policy: SideEffectPolicy = SideEffectPolicy.DENY,
        side_effecting_tools: set[str] | None = None,
    ) -> None:
        self._declarations: dict[str, ToolDecl] = dict(declarations or {})
        self._recorder = recorder
        self._mocks = dict(mocks or {})
        self._replay: Any = None
        self.denied_tools = set(denied_tools or ())
        self.allowed_tools = set(allowed_tools) if allowed_tools else set()
        self.approval_required = set(approval_required or ())
        self.granted_approvals = set(granted_approvals or ())
        self.side_effect_policy = side_effect_policy
        self.side_effecting_tools = set(side_effecting_tools or ())

    # -- policy ---------------------------------------------------------------

    def decide(self, name: str) -> PolicyDecision:
        """Precedence: deny beats approval beats allowlist.

        An empty ``allowed_tools`` means "no whitelist configured", which is
        different from "nothing is allowed" — a missing allowlist must not fail
        every test in a suite that only uses denied_tools.
        """
        if name in self.denied_tools:
            return PolicyDecision.DENY
        if name in self.approval_required and name not in self.granted_approvals:
            return PolicyDecision.REQUIRE_APPROVAL
        if self.allowed_tools and name not in self.allowed_tools:
            return PolicyDecision.DENY
        return PolicyDecision.ALLOW

    def is_side_effecting(self, name: str) -> bool:
        decl = self._declarations.get(name)
        return name in self.side_effecting_tools or bool(decl and decl.side_effect)

    # -- invocation -----------------------------------------------------------

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._declarations:
            raise AttributeError(
                f"tool {name!r} is not declared. Declare it on the adapter: "
                f"tools={{{name!r}: tool.live(...)}} or list it in policies.allowed_tools."
            )
        return self._bind(name, bypass_mock=False)

    def __getitem__(self, name: str) -> Callable[..., Any]:
        if name not in self._declarations:
            raise KeyError(f"tool {name!r} is not declared")
        return self._bind(name, bypass_mock=False)

    def call(self, name: str, /, **kwargs: Any) -> Any:
        """Explicit-name invocation, e.g. ``ctx.tools.call("get_order", id="1")``.

        Raises :class:`PolicyViolationError` for an unknown name rather than
        ``KeyError``: this is the spelling adapter authors reach for when a tool
        name is dynamic, and "that tool does not exist" is a policy problem worth
        naming precisely. ``ctx.tools["name"]`` keeps ``KeyError`` for genuine
        mapping semantics.
        """
        if name not in self._declarations:
            self._record_undeclared(name, kwargs)
            raise PolicyViolationError(
                f"tool {name!r} is not declared by this adapter",
                detail={"tool": name, "declared": self.names()},
                hint=(
                    "declare it on the adapter's `tools`, e.g. "
                    f'tools={{"{name}": tool.live(fn)}}'
                ),
            )
        return self[name](**kwargs)

    def _record_undeclared(self, name: str, arguments: Mapping[str, Any]) -> None:
        if self._recorder is None:
            return
        self._record(
            name,
            arguments,
            status=EventStatus.DENIED,
            result={"reason": "undeclared_tool"},
        )

    def names(self) -> list[str]:
        return sorted(self._declarations)

    def live(self, name: str, /) -> Callable[..., Any]:
        """Return the real implementation, bypassing mocks.

        Still refuses to execute when external side effects are denied, so
        ``.live()`` cannot be used to smuggle a destructive call past the gate.
        """
        return self._bind(name, bypass_mock=True)

    def _bind(self, name: str, *, bypass_mock: bool) -> Callable[..., Any]:
        def invoke(**kwargs: Any) -> Any:
            return self.invoke(name, kwargs, bypass_mock=bypass_mock)

        invoke.__name__ = name
        return invoke

    def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        bypass_mock: bool = False,
    ) -> Any:
        """Run policy, then replay answers or mocks, then the live implementation."""
        args = dict(arguments or {})
        decision = self.decide(name)

        if decision is PolicyDecision.DENY:
            self._record(name, args, status=EventStatus.DENIED, result={"reason": "denied_by_policy"})
            raise PolicyViolationError(
                f"tool {name!r} is denied by policy",
                hint="remove it from policies.denied_tools or add it to policies.allowed_tools",
            )

        if decision is PolicyDecision.REQUIRE_APPROVAL:
            self._record(
                name, args, status=EventStatus.DENIED, result={"reason": "approval_required"}
            )
            raise PolicyViolationError(
                f"tool {name!r} requires human approval that was not granted",
                hint=(
                    "emit ctx.recorder.approval_granted(...) before calling it, or drop it from "
                    "policies.approval_required"
                ),
            )

        if self._replay is not None and not bypass_mock:
            # The recorded artifact is the ground truth of what happened, so it
            # outranks both config mocks and any live implementation: the point of
            # replay is "does the agent still behave like the recording", and
            # answering a call a config mock also happens to cover would conceal a
            # behavioral change.
            answer = self._replay.serve(name, args)
            if answer is None:
                # Behavior the recording never saw. Refuse like any other denied
                # call and hand the agent an explicit refusal, so its handling of
                # it is part of the replayed trace.
                self._record(
                    name,
                    args,
                    status=EventStatus.DENIED,
                    result={"reason": "not_in_recorded_trace"},
                    metadata={
                        "replay": "divergent",
                        "mock_source": f"replay:{self._replay.source}",
                    },
                )
                return {"reason": "not_in_recorded_trace"}
            metadata: dict[str, Any] = {"mock_source": f"replay:{self._replay.source}"}
            if answer.args_differ:
                metadata["replay_args_differ"] = True
            self._record(
                name,
                args,
                status=EventStatus.MOCKED,
                result=answer.result,
                metadata=metadata,
            )
            return answer.result

        mock = self._mocks.get(name)
        if mock is not None and not bypass_mock:
            if not mock.matches(args):
                raise PolicyViolationError(
                    f"tool {name!r} was called with arguments that do not match the "
                    f"recorded mock from {mock.source}",
                    hint="re-record the run or relax the mock to match_arguments: false",
                )
            self._record(
                name,
                args,
                status=EventStatus.MOCKED,
                result=mock.response,
                metadata={"mock_source": mock.source},
            )
            return mock.response

        if self.is_side_effecting(name) and self.side_effect_policy is SideEffectPolicy.DENY:
            self._record(
                name, args, status=EventStatus.DENIED, result={"reason": "side_effect_denied"}
            )
            raise SideEffectBlocked(
                f"refusing to execute side-effecting tool {name!r} with external side effects denied",
                hint=(
                    "mock it (execution.mocks), or explicitly set "
                    "execution.external_side_effects: allow"
                ),
            )

        decl = self._declarations.get(name)
        if decl is None or decl.live is None:
            raise PolicyViolationError(
                f"tool {name!r} has no live implementation available",
                hint="declare it with tool.live(fn) or provide a mock",
            )

        if self._recorder is None:
            # No recorder means the registry was built outside a run. Execute the
            # tool untraced rather than refusing it: losing observability is
            # strictly better than breaking a caller that does not want tracing.
            return decl.live(**args)

        with self._recorder.tool_call(name, args) as call:
            outcome = decl.live(**args)
            call.result = outcome
            usage, cost = _extract_usage(outcome)
            call.usage = usage
            call.cost_usd = cost
            return outcome

    # -- helpers --------------------------------------------------------------

    def _record(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        status: EventStatus,
        result: Any,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Record a non-executing tool event (denied, mocked, blocked)."""
        if self._recorder is None:
            return
        # tool_call() enforces max_steps; go through it so budgeted runs still
        # stop correctly, then overwrite the completion status.
        with self._recorder.tool_call(name, dict(arguments)) as call:
            call.result = result
            call.status = status
            call.metadata = dict(metadata or {})

    def add_mock(self, name: str, entry: MockEntry) -> None:
        self._mocks[name] = entry

    def attach_replay(self, session: Any) -> None:
        """Serve every tool call from a recorded session (``agentci replay``)."""
        self._replay = session

    def grant_approval(self, name: str) -> None:
        self.granted_approvals.add(name)

    def __contains__(self, name: object) -> bool:
        return name in self._declarations

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._declarations))

    def __repr__(self) -> str:
        return (
            f"ToolRegistry(tools={sorted(self._declarations)}, "
            f"mocked={sorted(self._mocks)}, policy={self.side_effect_policy.value})"
        )


def _extract_usage(outcome: Any) -> tuple[TokenUsage | None, float | None]:
    """Best-effort token/cost extraction from a live tool result.

    Tools that call a model may return usage in several shapes. Anything we
    cannot confidently read stays ``None``, which makes cost-dependent
    assertions report ``SKIPPED`` rather than passing on absent data (§32).
    """
    if not isinstance(outcome, Mapping):
        return None, None

    usage: TokenUsage | None = None
    prompt = outcome.get("prompt_tokens", outcome.get("input_tokens"))
    completion = outcome.get("completion_tokens", outcome.get("output_tokens"))
    total = outcome.get("total_tokens")
    if isinstance(prompt, int) or isinstance(completion, int) or isinstance(total, int):
        usage = TokenUsage(
            prompt_tokens=int(prompt or 0),
            completion_tokens=int(completion or 0),
            total_tokens=int(total) if isinstance(total, int) else None,
        )

    cost = outcome.get("cost_usd", outcome.get("cost"))
    return usage, float(cost) if isinstance(cost, int | float) else None


class Toolset(dict[str, ToolDecl]):
    """Helper for declaring an adapter's tools.

    ``Toolset`` is a plain ``dict[str, ToolDecl]`` subclass, so an adapter can
    also declare tools as a literal mapping of name to implementation. Both forms
    are accepted because both read naturally::

        tools = Toolset({"search": tool.live(search)})
        tools = Toolset(search=tool.live(search))

    Later declarations override earlier ones for the same name.
    """

    def __init__(
        self,
        declarations: Mapping[str, ToolDecl | Callable[..., Any]] | None = None,
        /,
        **kwargs: ToolDecl | Callable[..., Any],
    ) -> None:
        merged: dict[str, ToolDecl] = {}
        for name, decl in {**(declarations or {}), **kwargs}.items():
            if isinstance(decl, ToolDecl):
                merged[decl.name or name] = decl
            elif callable(decl):
                merged[name] = ToolDecl(name=name, live=decl)
            else:
                raise TypeError(f"tool {name!r} must be a ToolDecl or a callable")
        super().__init__(merged)

    def live(self, fn: Callable[..., Any], /, **kwargs: Any) -> Toolset:
        name = kwargs.pop("name", fn.__name__)
        self[name] = ToolDecl(name=name, live=fn, **kwargs)
        return self

    def add(self, decl: ToolDecl) -> Toolset:
        self[decl.name] = decl
        return self


class _ToolFactory:
    """Namespace exposed as ``agentci.tool`` for readable adapter declarations."""

    @staticmethod
    def live(fn: Callable[..., Any], /, *, name: str | None = None, **kwargs: Any) -> ToolDecl:
        """Declare a tool with a live implementation."""
        return ToolDecl(name=name or fn.__name__, live=fn, **kwargs)

    @staticmethod
    def remote(name: str, *, side_effect: bool = False, description: str | None = None) -> ToolDecl:
        """Declare a tool that is expected to be mocked in CI.

        Use for tools whose real implementation lives behind a network boundary
        the test should not reach. With ``execution.external_side_effects: deny``
        this fails safely instead of making a live call.
        """
        return ToolDecl(name=name, live=None, side_effect=side_effect, description=description)


tool = _ToolFactory()


__all__ = [
    "MockEntry",
    "PolicyDecision",
    "SideEffectPolicy",
    "ToolDecl",
    "ToolRegistry",
    "Toolset",
    "tool",
]
