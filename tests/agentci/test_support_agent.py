"""The AgentCI test suite for the bundled support agent.

This is what an AgentCI user actually writes. Note what is *absent*: no manual
budget math, no token counting, no redaction plumbing, no policy assertions that
duplicate config. The runner applies project budgets, the leak scan, and policy on
every invocation; a test body only states what is specific to the behaviour under
test.

Run it with::

    agentci test
"""

from __future__ import annotations

from agentci.assertions import expect
from agentci.testing import agent_test


@agent_test(tags=["smoke"], dependencies=["examples/support_agent/**"])
def test_refund_status_is_explained(agent):
    """A ticket lookup produces the documented answer and shows its work."""
    result = agent.run("Where is my refund for T-1001?")

    expect(result).to_contain("3-5 business days")
    expect(result).to_use_tool("lookup_ticket")
    expect(result).to_call_tool_with("lookup_ticket", ticket_id="T-1001")


@agent_test(tags=["smoke"], dependencies=["examples/support_agent/**"])
def test_unknown_ticket_asks_for_clarification(agent):
    """The agent must not invent an answer for a ticket it cannot find."""
    result = agent.run("I need help with my account")

    expect(result).to_contain("ticket number")
    expect(result).to_not_use_tool("escalate_ticket")


@agent_test(tags=["smoke"], dependencies=["examples/support_agent/**"])
def test_answer_is_deterministic(agent):
    """Same question, same answer.

    Worth asserting explicitly: it is what lets the suite run with
    ``evaluation.repeat: 1`` instead of paying for repetitions to average out
    nondeterminism.
    """
    first = agent.run("Status of T-1002?")
    second = agent.run("Status of T-1002?")

    expect(first).to_equal(second.output_text)


@agent_test(tags=["tools"], dependencies=["examples/support_agent/**"])
def test_tool_arguments_are_recorded(agent):
    """Arguments reach the trace, so an audit can reconstruct the call."""
    agent.run("Please check T-1003")

    expect(agent.last).to_call_tool_with("lookup_ticket", ticket_id="T-1003")
    expect(agent.last).to_have_max_tool_calls(3)


@agent_test(tags=["tools", "policy"], dependencies=["examples/support_agent/**"])
def test_no_side_effecting_tool_on_a_plain_question(agent):
    """A read-only question must not escalate.

    ``escalate_ticket`` has an external side effect and is denied by default, so
    calling it would raise rather than silently succeed.
    """
    result = agent.run("What is the status of T-1001?")

    expect(result).to_not_use_tool("escalate_ticket")
    expect(result).to_have_no_live_side_effects()


@agent_test(tags=["output"], dependencies=["examples/support_agent/**"])
def test_no_secret_material_in_output(agent):
    """Output is scrubbed at the serialization boundary."""
    expect(agent.run("Anything about T-1001?")).to_not_leak()


@agent_test(tags=["output"], dependencies=["examples/support_agent/**"])
def test_answer_mentions_the_ticket(agent):
    result = agent.run("Refund status for T-1001 please")

    expect(result).to_match(r"T-1001|refund")
    expect(result).to_not_contain("Traceback")
