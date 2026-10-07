"""OpenAI Agents SDK integration.

This package is opt-in: importing ``agentci`` or ``agentci.integrations`` never
loads the SDK. Importing this subpackage requires ``openai-agents`` to be
installed (``pip install agentci-py[openai-agents]``).

Typical wiring::

    from agentci.integrations.openai_agents import AgentCI

    support_agent = AgentCI(_build_support_agent())
"""

from agentci.integrations.openai_agents.adapter import AgentCI, OpenAIAgentsAdapter

__all__ = ["AgentCI", "OpenAIAgentsAdapter"]
