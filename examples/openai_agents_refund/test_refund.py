from agentci.assertions import expect
from agentci.testing import agent_test


@agent_test(tags=["refund"])
def test_refund_happens_exactly_once(agent):
    result = agent.run("please refund order ORD-7781")
    expect(result).to_use_tool("refund_order", times=1)