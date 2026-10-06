"""A deliberately regressed refund flow, used to prove a regression is caught.

The bug is the ordinary one: a "simplify" pass folded the eligibility check out
of the refund path, so every order is refunded -- including one that was already
refunded on 2026-01-14. Nothing about that failure is exotic, and nothing about
it is visible from reading the agent's output unless you know what the output
should have said.

The correct behaviour lives in :meth:`SupportAgent.refund_decision`. This module
overrides *only* that decision, so the fixture differs from the shipped agent in
exactly one method and a failing assertion points straight at the money.

Run it through the harness rather than importing it::

    agentci gate        # after pointing `tests:` at test_refund_regression.py
    python scripts/verify.py    # probe 8 runs the correct expectation against it
"""

from __future__ import annotations

from examples.support_agent.agent import Order, SupportAgent


class SupportAgentWithRefundBug(SupportAgent):
    """Refunds unconditionally. Kept out of the default suite on purpose."""

    name = "support-agent-with-refund-bug"

    def refund_decision(self, order: Order) -> tuple[bool, str]:
        # The bug: `order.refundable` is never consulted, and so is the reason
        # the order was refused in the first place.
        return True, (
            f"Refunded ${order.amount:.2f} for order {order.id}. "
            "A confirmation was sent to the address on file."
        )


__all__ = ["SupportAgentWithRefundBug"]
