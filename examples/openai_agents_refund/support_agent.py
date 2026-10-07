"""A minimal, offline openai-agents support agent, wired through AgentCI.

``CALLS`` is the behaviour under test: how many refunds the agent issues for one
order. The accompanying test asserts exactly one, so flipping ``CALLS`` to ``2``
is the deliberate regression the README flow (record -> replay -> diff -> gate)
is supposed to catch.
"""

from agents import Agent, function_tool
from agents.testing.model import ScriptedModel, assistant_message, function_call

from agentci.integrations.openai_agents import AgentCI

CALLS = 1


@function_tool
def lookup_order(order_id: str):
    return {"status": "open", "order_id": order_id}


@function_tool
def refund_order(order_id: str, amount: float):
    return {"refunded": True, "order_id": order_id, "amount": amount}


def _root_agent():
    steps = [
        {"output": [function_call("lookup_order", {"order_id": "ORD-7781"}, call_id="call_1")]},
    ]
    for _ in range(CALLS):
        steps.append(
            {"output": [function_call("refund_order", {"order_id": "ORD-7781", "amount": 49.99}, call_id=f"call_{2 + _}")]}
        )
    steps.append({"output": [assistant_message("Refund issued for ORD-7781.")]})
    return Agent(
        name="refunder",
        instructions="Refund customer orders.",
        tools=[lookup_order, refund_order],
        # Deterministic stand-in: a real engine like OpenAI calls the same tools.
        model=ScriptedModel(steps),
    )


class SupportAgent(AgentCI):  # yaml: agent.adapter: "support_agent:SupportAgent"
    def __init__(self):
        super().__init__(_root_agent())