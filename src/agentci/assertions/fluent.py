"""The fluent ``expect()`` surface (PRD §13).

``expect(result).to_use_tool("get_order")`` reads as English and is what the
README leads with. Every method delegates to a pure function in a sibling module
and records the outcome into the active :class:`AssertionCollector`.

The collector is resolved through a :class:`~contextvars.ContextVar` rather than a
constructor argument. That is what lets a test body read naturally — no fixture,
no ``with`` block, no injected object — while the runner still captures every
result from inside. It also makes the same assertion code usable from a plain
script, a notebook, or a future pytest plugin.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextvars import ContextVar
from typing import Any, Self

from agentci.assertions import execution as _execution
from agentci.assertions import leaks as _leaks
from agentci.assertions import output as _output
from agentci.assertions import policy as _policy
from agentci.assertions import tools as _tools
from agentci.assertions.base import AssertionCollector, AssertionFailed, ExpectationError
from agentci.core.redaction import Redactor, default_redactor
from agentci.core.result import AssertionResult, ResultView, as_view
from agentci.core.trace import ToolCall

_active_collector: ContextVar[AssertionCollector | None] = ContextVar(
    "agentci_assertion_collector", default=None
)
_active_redactor: ContextVar[Redactor | None] = ContextVar("agentci_redactor", default=None)


class _Activation:
    """One activation, with a reset callback.

    ``ContextVar.set`` returns a token bound to a single variable, and ``reset``
    must be called on that exact variable. Wrapping every activation in one small
    object -- rather than returning raw tokens -- keeps the binding at the point of
    creation, so :func:`deactivate` never has to guess which variable a token came
    from or hand out mismatched ``Token`` types.
    """

    __slots__ = ("_reset",)

    def __init__(self, reset: Callable[[], None]) -> None:
        self._reset = reset

    def reset(self) -> None:
        self._reset()


def _set_collector(collector: AssertionCollector) -> _Activation:
    token = _active_collector.set(collector)
    return _Activation(lambda: _active_collector.reset(token))


def _set_redactor(redactor: Redactor) -> _Activation:
    token = _active_redactor.set(redactor)
    return _Activation(lambda: _active_redactor.reset(token))


def _set_both(collector: AssertionCollector, redactor: Redactor) -> _Activation:
    collector_token = _active_collector.set(collector)
    redactor_token = _active_redactor.set(redactor)

    def reset() -> None:
        # Reverse order of setting, so a failure in one reset cannot leave the
        # other variable pointing at a dead collector.
        _active_redactor.reset(redactor_token)
        _active_collector.reset(collector_token)

    return _Activation(reset)


#: What :func:`activate` / :func:`activate_redactor` hand back, and what
#: :func:`deactivate` accepts.
Activation = _Activation


def activate(collector: AssertionCollector, redactor: Redactor | None = None) -> Activation:
    """Install a collector -- and optionally a redactor -- as active.

    Returns a handle for :func:`deactivate`, which unwinds everything set here.

    The two are activated together deliberately: a test body's assertions must see
    the *same* redactor that the runner will use when writing the report. If they
    diverged, ``to_not_leak`` could pass against a strict redactor while the
    artifact on disk still contained what the assertion claimed was absent.

    :func:`activate_redactor` exists for the rarer case of swapping only the
    redactor; prefer this function.
    """
    if redactor is None:
        return _set_collector(collector)
    return _set_both(collector, redactor)


def activate_redactor(redactor: Redactor) -> Activation:
    """Install only a redactor, leaving the active collector alone."""
    return _set_redactor(redactor)


def deactivate(token: Activation) -> None:
    """Undo :func:`activate` or :func:`activate_redactor`."""
    token.reset()


def deactivate_redactor(token: Activation) -> None:
    """Undo :func:`activate_redactor`.

    Kept as a distinct name for symmetry with :func:`activate_redactor`;
    :func:`deactivate` handles the same token.
    """
    token.reset()


def current_collector() -> AssertionCollector | None:
    return _active_collector.get()


def current_redactor() -> Redactor:
    return _active_redactor.get() or default_redactor()


class Expectation:
    """Chainable expectations over a single :class:`ResultView`.

    Methods return ``self`` so they can be chained, though the README examples
    read better as one call per line.
    """

    __slots__ = ("_collector", "_redactor", "_view")

    def __init__(
        self,
        view: ResultView,
        collector: AssertionCollector,
        redactor: Redactor | None = None,
    ) -> None:
        self._view = view
        self._collector = collector
        self._redactor = redactor or current_redactor()

    # -- internals ------------------------------------------------------------

    def _emit(self, result: AssertionResult) -> Self:
        self._collector.add(result)
        if self._collector.standalone and not result.ok and result.status.value == "failed":
            raise AssertionFailed(result)
        return self

    @property
    def view(self) -> ResultView:
        return self._view

    @property
    def results(self) -> list[AssertionResult]:
        return self._collector.results

    def _calls(self) -> Sequence[ToolCall]:
        return self._view.tool_calls

    def _require_calls(self, method: str) -> Sequence[ToolCall]:
        calls = self._calls()
        if not calls:
            raise ExpectationError(
                f"{method}() requires at least one tool call, but the trace has none. "
                "Either the agent did not use tools, or it called them directly "
                "instead of routing through ctx.tools."
            )
        return calls

    # -- output ---------------------------------------------------------------

    def to_contain(self, needle: str, *, case_sensitive: bool = True) -> Self:
        return self._emit(
            _output.contains(self._view.output_text, needle, case_sensitive=case_sensitive)
        )

    def to_not_contain(self, needle: str, *, case_sensitive: bool = True) -> Self:
        return self._emit(
            _output.not_contains(self._view.output_text, needle, case_sensitive=case_sensitive)
        )

    def to_equal(self, expected: str) -> Self:
        return self._emit(_output.exact_match(self._view.output_text, expected))

    def to_match(self, pattern: str) -> Self:
        return self._emit(_output.matches_regex(self._view.output_text, pattern))

    def to_be_json(self) -> Self:
        return self._emit(_output.is_json(self._view.output_text))

    def to_have_fields(self, fields: Sequence[str], *, required_only: bool = False) -> Self:
        return self._emit(
            _output.has_fields(self._view.output_text, list(fields), required_only=required_only)
        )

    def to_match_json_schema(self, schema: dict[str, Any]) -> Self:
        return self._emit(_output.match_json_schema(self._view.output_text, schema))

    # -- tools ----------------------------------------------------------------

    def to_use_tool(self, name: str, *, times: int | None = None) -> Self:
        return self._emit(_tools.used_tool(self._calls(), name, times=times))

    def to_not_use_tool(self, name: str) -> Self:
        return self._emit(_tools.not_used_tool(self._calls(), name))

    def to_call_tool(
        self,
        *,
        equals: int | None = None,
        at_least: int | None = None,
        at_most: int | None = None,
    ) -> Self:
        return self._emit(
            _tools.tool_call_count(self._calls(), equals=equals, at_least=at_least, at_most=at_most)
        )

    def to_follow_tool_order(self, expected: Sequence[str], *, exact: bool = False) -> Self:
        return self._emit(_tools.tool_order(self._calls(), list(expected), exact=exact))

    def to_have_max_tool_calls(self, limit: int) -> Self:
        return self._emit(_tools.max_tool_calls(self._calls(), limit))

    def to_call_tool_with(
        self,
        name: str,
        expected: dict[str, Any] | None = None,
        *,
        strict: bool = False,
        every_call: bool = True,
        **kwargs: Any,
    ) -> Self:
        """Assert a tool received particular arguments.

        Keyword arguments are collected into the expectation, so both spellings
        read well::

            expect(r).to_call_tool_with("issue_refund", order_id="123")
            expect(r).to_call_tool_with("issue_refund", {"order_id": "123"})
        """
        merged = {**(expected or {}), **kwargs}
        return self._emit(
            _tools.tool_arguments_match(
                self._require_calls("to_call_tool_with"),
                name,
                merged,
                strict=strict,
                every_call=every_call,
            )
        )

    def to_call_tool_matching_schema(self, name: str, schema: dict[str, Any]) -> Self:
        return self._emit(_tools.tool_arguments_schema(self._calls(), name, schema))

    def to_succeed(self, name: str) -> Self:
        return self._emit(_tools.tool_succeeded(self._view.trace, name))

    def to_not_retry(self, name: str, *, at_most: int = 0) -> Self:
        return self._emit(_tools.no_tool_retries(self._calls(), name, at_most=at_most))

    # -- execution budgets ----------------------------------------------------

    def to_have_max_latency(self, limit_ms: float) -> Self:
        return self._emit(_execution.max_latency(self._view.metrics, limit_ms))

    def to_have_max_cost(self, limit_usd: float) -> Self:
        return self._emit(_execution.max_cost(self._view.metrics, limit_usd))

    def to_have_max_tokens(self, limit: int) -> Self:
        return self._emit(_execution.max_tokens(self._view.metrics, limit))

    def to_have_max_retries(self, limit: int) -> Self:
        return self._emit(_execution.max_retries(self._view.metrics, limit))

    def to_have_max_steps(self, limit: int) -> Self:
        return self._emit(_execution.max_steps(self._view.metrics, limit))

    def to_not_loop(self, *, threshold: int = 3) -> Self:
        return self._emit(_execution.no_loop(self._calls(), threshold=threshold))

    # -- policy ---------------------------------------------------------------

    def to_only_use_tools(self, allowed: Sequence[str]) -> Self:
        return self._emit(_policy.only_uses_tools(self._calls(), list(allowed)))

    def to_require_approval_for(self, tool: str) -> Self:
        return self._emit(_policy.requires_approval(self._view.trace, tool))

    def to_demand_approval_for(self, tool: str) -> Self:
        return self._emit(_policy.approval_was_demanded(self._view.trace, tool))

    def to_have_no_policy_violations(self) -> Self:
        return self._emit(_policy.no_policy_violations(self._view.policy_violations))

    def to_have_no_live_side_effects(self) -> Self:
        return self._emit(_policy.no_external_side_effects(self._view.trace))

    def to_not_contain_forbidden_data(self, patterns: Sequence[str]) -> Self:
        return self._emit(
            _policy.forbidden_data_absent(self._view.trace, self._redactor, list(patterns))
        )

    # -- leakage --------------------------------------------------------------

    def to_not_leak(self, *patterns: str) -> Self:
        """Assert the run never exposed the given patterns or literals anywhere."""
        return self._emit(
            _leaks.not_leak(self._view.trace, self._view.output_text, self._redactor, list(patterns))
        )

    def to_not_leak_environment_secrets(self) -> Self:
        return self._emit(
            _leaks.no_secret_in_environment_derived_fields(self._view.trace, self._redactor)
        )


def expect(result: Any) -> Expectation:
    """Start an expectation chain.

    ``result`` is normally the return of ``agent.run(...)``. A raw
    :class:`~agentci.core.result.AgentResult` also works, which is convenient when
    driving an adapter by hand.
    """
    view = as_view(result)
    collector = _active_collector.get()
    if collector is None:
        # Standalone use: fail fast, the way a script author expects.
        collector = AssertionCollector(standalone=True)
        _active_collector.set(collector)
    return Expectation(view, collector)


__all__ = [
    "Expectation",
    "activate",
    "activate_redactor",
    "current_collector",
    "current_redactor",
    "deactivate",
    "deactivate_redactor",
    "expect",
]
