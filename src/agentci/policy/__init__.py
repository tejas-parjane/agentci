"""Policy-as-code: the rules an agent must obey, enforced on every run.

``policies`` in ``agentci.yaml`` is the version-controlled statement of what the
agent is allowed to do. :mod:`agentci.policy.engine` enforces it both before and
after execution; this package holds the engine and its shared context.

Separated from ``core`` because policy is a *product concept* rather than a
mechanism: it is where the community contributes reusable rules (PRD §37, "Policy
Marketplace").
"""

from agentci.policy.engine import PolicyContext, PolicyEngine, compliance

__all__ = ["PolicyContext", "PolicyEngine", "compliance"]
