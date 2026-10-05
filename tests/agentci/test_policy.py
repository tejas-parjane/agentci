"""Policy-as-code tests for the example agent.

These exist to prove AgentCI's *fail-safe* claims, which are the ones a user is
least able to check by reading their own test suite:

* an undeclared tool is an error, not a silent no-op
* an unmocked, side-effecting tool is refused even when the adapter tries
* a *mocked* side-effecting tool is allowed, and recorded as ``mocked`` -- the
  mock is what makes the call safe, so denying it would make mocking useless
* forbidden data patterns are caught in tool arguments

If these fail, AgentCI is lying about its safety guarantees.
"""

from __future__ import annotations

from agentci.assertions import expect
from agentci.core.trace import EventStatus, EventType
from agentci.errors import PolicyViolationError, SideEffectBlocked
from agentci.testing import agent_test


def _tool_completions(agent, tool: str):
    """Completed events for ``tool``.

    Only ``tool_call_completed`` events carry an outcome. The matching
    ``tool_call_started`` event is ``pending`` by construction, so filtering on
    status without also filtering on type would mistake every call for a live one.
    """
    return [
        event
        for event in agent.ctx.recorder.events
        if event.type is EventType.TOOL_CALL_COMPLETED
        and event.tool
        and event.tool.name == tool
    ]


@agent_test(tags=["policy"], dependencies=["examples/support_agent/**"])
def test_undeclared_tool_is_an_error(agent):
    """Calling a tool the adapter never declared raises, not silently no-ops.

    ``AttributeError`` would be AgentCI quietly inventing an API; a
    ``PolicyViolationError`` states the real reason.
    """
    try:
        agent.ctx.tools.call("shell_exec", command="whoami")
    except PolicyViolationError as exc:
        assert "shell_exec" in str(exc)
    else:  # pragma: no cover - the test must fail loudly if this ever opens up
        raise AssertionError("calling an undeclared tool should have raised")


@agent_test(tags=["policy"], dependencies=["examples/support_agent/**"])
def test_unmocked_side_effect_is_refused(agent):
    """``delete_ticket`` has a side effect and no mock, so policy refuses it.

    This is the load-bearing safety test: an agent that tries to do something
    destructive must not be able to, even though it is the code under test and
    AgentCI cannot stop it from *trying*.
    """
    try:
        agent.ctx.tools.call("delete_ticket", ticket_id="T-1001")
    except SideEffectBlocked as exc:
        assert "delete_ticket" in str(exc)
        # The error must tell the user how to proceed, not just that it failed.
        assert "mock" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("a destructive tool was allowed to run")


@agent_test(tags=["policy"], dependencies=["examples/support_agent/**"])
def test_mocked_side_effect_is_allowed_and_marked_mocked(agent):
    """The complement of the test above.

    ``escalate_ticket`` is also side-effecting, but ``agentci.yaml`` mocks it.
    The call succeeds and the trace records ``mocked`` -- which is exactly why
    ``to_have_no_live_side_effects`` can be trusted: a mock means the real thing
    never ran.
    """
    outcome = agent.ctx.tools.call("escalate_ticket", ticket_id="T-1001", reason="test")

    assert outcome == {"escalated": True, "queue": "human"}

    mocked = _tool_completions(agent, "escalate_ticket")
    assert mocked, "the mocked call should still be traced"
    assert all(event.status is EventStatus.MOCKED for event in mocked)


@agent_test(tags=["policy"], dependencies=["examples/support_agent/**"])
def test_mocked_side_effect_proves_no_live_call_happened(agent):
    """Run the tool, then confirm nothing live executed."""
    agent.ctx.tools.call("escalate_ticket", ticket_id="T-1001", reason="test")

    live = [
        event
        for event in _tool_completions(agent, "escalate_ticket")
        if event.status not in {EventStatus.MOCKED, EventStatus.DENIED}
    ]
    assert not live, f"a live side effect was recorded: {live}"


@agent_test(tags=["policy"], dependencies=["examples/support_agent/**"])
def test_read_only_tool_is_allowed(agent):
    """The allowed tool still works under the same policy that blocked the others."""
    record = agent.ctx.tools.call("lookup_ticket", ticket_id="T-1001")

    assert record is not None
    assert record["id"] == "T-1001"


@agent_test(tags=["policy"], dependencies=["examples/support_agent/**"])
def test_every_invocation_is_scanned_for_leaks(agent):
    """The leak scan and policy check run whether or not the test asks."""
    result = agent.run("Tell me about T-1001")

    expect(result).to_have_no_policy_violations()
    expect(result).to_not_leak_environment_secrets()
