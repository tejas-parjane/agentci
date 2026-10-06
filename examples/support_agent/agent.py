"""A deliberately small customer-support agent, used as AgentCI's worked example.

It exists to be *testable*, not clever. Every behaviour an AgentCI test might
assert on is observable through the trace:

* a read-only lookup tool, so ``to_use_tool`` has something to check
* a write tool flagged ``side_effect=True``, so policy has something to refuse
* a refund flow that must check eligibility *before* it touches money, which is
  the kind of ordering bug a regression test exists to catch
* deterministic output, so the example suite does not flake

Several run styles are provided, because AgentCI must accept all of them and
produce the *same* view:

:class:`SupportAgent`
    Records live through ``ctx.recorder``; tools are mocked from config.
:class:`LiveSupportAgent`
    Records live and executes its read-only tool for real.
:func:`run_agent`
    Returns its events in ``AgentResult.trace`` instead of recording them.
:func:`run_agent_slow`, :func:`run_agent_secretive`, :func:`run_agent_flaky`
    Deliberately broken agents, one per failure mode AgentCI must detect.
"""

from __future__ import annotations

import itertools
import re
import time
from dataclasses import dataclass

from agentci.adapters.base import BaseAdapter
from agentci.core.context import RunContext
from agentci.core.result import AgentResult
from agentci.core.tools import ToolDecl
from agentci.core.trace import EventStatus, EventType, TokenUsage, ToolCall

MODEL = "gpt-5-mini"

TICKETS: dict[str, dict[str, str]] = {
    "T-1001": {
        "id": "T-1001",
        "subject": "Refund never arrived",
        "status": "open",
        "priority": "high",
        "assignee": "dana",
    },
    "T-1002": {
        "id": "T-1002",
        "subject": "Cannot log in after SSO change",
        "status": "open",
        "priority": "urgent",
        "assignee": "sam",
    },
    "T-1003": {
        "id": "T-1003",
        "subject": "Invoice shows wrong seat count",
        "status": "pending",
        "priority": "low",
        "assignee": "dana",
    },
}

#: Canned answers keyed by ticket id. Deterministic output is what lets the
#: example suite run with ``evaluation.repeat: 1``.
ANSWERS: dict[str, str] = {
    "T-1001": "Your refund was issued on 2026-01-14 and should settle in 3-5 business days.",
    "T-1002": "SSO is failing because your IdP metadata is stale. I have escalated this to IT.",
    "T-1003": "Billing will regenerate the invoice with the correct seat count.",
}

UNKNOWN_TICKET = "I could not find that ticket. Could you share the ticket number?"
UNKNOWN_ORDER = "I could not find that order. Could you share the order number?"

_TICKET_RE = re.compile(r"\bT-\d{4}\b", re.IGNORECASE)
_ORDER_RE = re.compile(r"\bORD-\d{4}\b", re.IGNORECASE)
_ESCALATE_WORDS = ("urgent", "escalate", "complain")


def ticket_id_in(text: str) -> str:
    """Extract a ticket id from free-form user input."""
    match = _TICKET_RE.search(text)
    return match.group(0).upper() if match else ""


def order_id_in(text: str) -> str:
    """Extract an order id from free-form user input."""
    match = _ORDER_RE.search(text)
    return match.group(0).upper() if match else ""


def wants_escalation(text: str) -> bool:
    return any(word in text.lower() for word in _ESCALATE_WORDS)


def usage_for(prompt: str, completion: str) -> TokenUsage:
    """A plausible, deterministic usage record.

    Real adapters report provider usage; the example cannot call a provider, so
    it estimates from character counts. ``cost_usd`` is left unset on purpose so
    AgentCI's own pricing snapshot is exercised rather than trusted input.
    """
    prompt_tokens = max(1, len(prompt) // 4)
    completion_tokens = max(1, len(completion) // 4)
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


@dataclass(frozen=True)
class Order:
    """A purchase, and the unit the refund flow reasons about."""

    id: str
    customer_id: str
    amount: float
    status: str
    refundable: bool
    reason: str = ""


ORDERS: dict[str, Order] = {
    "ORD-7781": Order(
        id="ORD-7781",
        customer_id="C-4471",
        amount=49.99,
        status="delivered",
        refundable=True,
    ),
    "ORD-7782": Order(
        id="ORD-7782",
        customer_id="C-4471",
        amount=129.0,
        status="refunded",
        refundable=False,
        reason="already refunded on 2026-01-14",
    ),
}

#: Read-only customer records. ``email`` is used as a *tool argument* only --
#: echoing it into the answer would put PII in the artifact for no benefit, so
#: the agent tells the user a confirmation went to "the address on file".
CUSTOMERS: dict[str, dict[str, str]] = {
    "C-4471": {"id": "C-4471", "name": "Ravi Menon", "email": "ravi.menon@example.com"},
    "C-4472": {"id": "C-4472", "name": "Dana Okafor", "email": "dana.okafor@example.com"},
}


def lookup_ticket(ticket_id: str) -> dict[str, str] | None:
    """The read-only tool implementation. Pure, so it is safe to call live."""
    return TICKETS.get(ticket_id)


def lookup_customer(customer_id: str) -> dict[str, str] | None:
    """Read-only, pure, and safe to call live."""
    return CUSTOMERS.get(customer_id)


def lookup_order(order_id: str) -> Order | None:
    """Read-only, pure, and safe to call live."""
    return ORDERS.get(order_id)


class SupportAgent(BaseAdapter):
    """Records live through ``ctx.recorder``; side effects resolve via config mocks.

    The read-only lookups that the refund flow needs (``lookup_customer``,
    ``lookup_order``) declare a ``live`` implementation and really execute --
    they are pure, and ``lookup_order`` has to answer for more than one order,
    which a single config mock cannot do. ``lookup_ticket`` is instead mocked from
    config, because demonstrating that mechanism is what it is there for;
    :class:`LiveSupportAgent` flips it live.

    The two tools that touch money or a human inbox are ``side_effect=True`` and
    have no live implementation at all: only a configured mock lets them run,
    which is what makes ``to_have_no_live_side_effects`` mean something.
    """

    name = "support-agent"

    tools = {
        "lookup_ticket": ToolDecl(
            name="lookup_ticket",
            side_effect=False,
            description="Fetch a support ticket by id.",
        ),
        "lookup_customer": ToolDecl(
            name="lookup_customer",
            live=lookup_customer,
            side_effect=False,
            description="Fetch a customer record by id.",
        ),
        "lookup_order": ToolDecl(
            name="lookup_order",
            live=lookup_order,
            side_effect=False,
            description="Fetch an order by id.",
        ),
        # Mocked in agentci.yaml, so calling it is safe and the trace records it
        # as `mocked`.
        "escalate_ticket": ToolDecl(
            name="escalate_ticket",
            side_effect=True,
            description="Escalate a ticket to a human agent.",
        ),
        # Money leaving the building. Mocked, never live.
        "refund_order": ToolDecl(
            name="refund_order",
            side_effect=True,
            description="Refund an order to the original payment method.",
        ),
        # PII leaving the building. Mocked, never live.
        "send_email": ToolDecl(
            name="send_email",
            side_effect=True,
            description="Email the customer.",
        ),
        # Deliberately *not* mocked, and with no live implementation. Any attempt
        # to reach it must be refused by policy -- this is the tool that proves the
        # deny gate actually closes.
        "delete_ticket": ToolDecl(
            name="delete_ticket",
            side_effect=True,
            description="Permanently delete a ticket. Never do this in a test.",
        ),
    }

    def refund_decision(self, order: Order) -> tuple[bool, str]:
        """Whether this order may be refunded, plus the answer either way.

        A method rather than an inline branch so the regression fixture can
        override exactly this decision and nothing else -- see
        :mod:`examples.support_agent.refund_regression`.
        """
        if not order.refundable:
            detail = f" ({order.reason})" if order.reason else ""
            return False, f"Order {order.id} is not eligible for a refund{detail}."
        return True, (
            f"Refunded ${order.amount:.2f} for order {order.id}. "
            "A confirmation was sent to the address on file."
        )

    def _handle_refund(self, order_id: str, ctx: RunContext) -> str:
        """The refund flow: look it up, decide, then -- and only then -- act."""
        order = ctx.tools.call("lookup_order", order_id=order_id)
        if order is None:
            return UNKNOWN_ORDER

        proceed, answer = self.refund_decision(order)
        if not proceed:
            return answer

        customer = ctx.tools.call("lookup_customer", customer_id=order.customer_id)
        ctx.tools.call("refund_order", order_id=order.id, amount=order.amount)
        ctx.tools.call(
            "send_email",
            to=(customer or {}).get("email", "the address on file"),
            subject=f"Your refund for {order.id}",
            body=answer,
        )
        return answer

    def run(self, user_input: str, ctx: RunContext) -> AgentResult:
        ticket_id = ticket_id_in(user_input)
        order_id = order_id_in(user_input)
        escalate = wants_escalation(user_input)

        ctx.set("ticket_id", ticket_id)
        ctx.set("order_id", order_id)

        with ctx.recorder.model_call(MODEL) as call:
            call.result = user_input

            record = ctx.tools.call("lookup_ticket", ticket_id=ticket_id)

            if escalate:
                # A side-effecting tool. With the default
                # ``execution.external_side_effects: deny`` this raises unless the
                # test grants an approval or configures a mock -- which is the
                # behaviour AgentCI's policy tests exist to prove.
                ctx.tools.call(
                    "escalate_ticket",
                    ticket_id=ticket_id,
                    reason="user asked for escalation",
                )

            answer = self._handle_refund(order_id, ctx) if order_id else ""
            if not answer:
                answer = ANSWERS.get(ticket_id or "", UNKNOWN_TICKET)
            call.result = answer
            call.usage = usage_for(user_input, answer)

        return AgentResult(
            output_text=answer,
            metadata={
                "model": MODEL,
                "agent_version": "1.0.0",
                "ticket_id": ticket_id,
                "order_id": order_id,
                "found": bool(record),
                "escalated": escalate,
            },
        )


class LiveSupportAgent(SupportAgent):
    """Same shape, but ``lookup_ticket`` really executes.

    Useful for proving that a live call and a mocked call are indistinguishable
    to assertions -- only the recorded result differs. Every other tool is
    inherited, so the two agents are comparable tool for tool.
    """

    tools = {
        **SupportAgent.tools,
        "lookup_ticket": ToolDecl(
            name="lookup_ticket",
            live=lookup_ticket,
            side_effect=False,
            description="Fetch a support ticket by id.",
        ),
    }


# -- returned-trace adapters ---------------------------------------------------


def run_agent(user_input: str, ctx: RunContext) -> AgentResult:
    """Returns its events in ``AgentResult.trace`` rather than recording live.

    Events are built with :meth:`TraceRecorder.emit` and parented explicitly,
    because there is no ``with`` block to nest them. The runner must merge these
    with anything the recorder saw and de-duplicate by ``event_id``.
    """
    ticket_id = ticket_id_in(user_input)
    record = ctx.tools.call("lookup_ticket", ticket_id=ticket_id)
    answer = ANSWERS.get(ticket_id or "", UNKNOWN_TICKET)

    model_started = ctx.recorder.emit(
        EventType.MODEL_CALL_STARTED,
        component=MODEL,
        status=EventStatus.PENDING,
        metadata={"model": MODEL},
    )
    tool_done = ctx.recorder.emit(
        EventType.TOOL_CALL_COMPLETED,
        parent_id=model_started.event_id,
        component=MODEL,
        tool=ToolCall(name="lookup_ticket", arguments={"ticket_id": ticket_id}),
        result=record,
        status=EventStatus.SUCCESS,
    )
    model_done = ctx.recorder.emit(
        EventType.MODEL_CALL_COMPLETED,
        parent_id=model_started.event_id,
        component=MODEL,
        result=answer,
        usage=usage_for(user_input, answer),
        status=EventStatus.SUCCESS,
        metadata={"model": MODEL},
    )

    return AgentResult(
        output_text=answer,
        trace=[model_started, tool_done, model_done],
        metadata={"model": MODEL, "ticket_id": ticket_id, "style": "returned-trace"},
    )


# -- deliberately broken adapters ----------------------------------------------


def run_agent_slow(user_input: str, ctx: RunContext) -> AgentResult:
    """Sleeps past ``budgets.max_latency_ms``."""
    time.sleep(0.4)
    return AgentResult(output_text="slow answer", metadata={"model": MODEL})


def run_agent_secretive(user_input: str) -> AgentResult:
    """Echoes a credential into its output.

    Takes no ``ctx`` on purpose: it must still be loadable, and the always-on
    leak scan must catch it without the test author asking.
    """
    import os

    token = os.environ.get("AGENTCI_DEMO_API_KEY", "sk-demo-000111222333444555666777")
    return AgentResult(
        output_text=f"Here is the API key you asked for: {token}",
        metadata={"model": MODEL},
    )


_ATTEMPTS = itertools.count(1)


def run_agent_flaky(user_input: str, ctx: RunContext) -> AgentResult:
    """Succeeds on odd invocations, fails on even ones.

    Varies across *repetitions* of the same test, which is what
    ``evaluation.repeat`` plus ``allow_flaky`` exist to expose: an agent that
    passes sometimes must be reported FLAKY, never silently averaged into a pass.

    The counter is process-global on purpose. Scoping it to the run or iteration
    would reset it each repetition, and the agent would look perfectly stable --
    hiding the very flakiness this adapter exists to demonstrate. Tests that depend
    on a clean counter should call :func:`reset_flaky_counter`.
    """
    attempt = next(_ATTEMPTS)
    ok = attempt % 2 == 1
    return AgentResult(
        output_text="resolved" if ok else "upstream timeout, retrying",
        metadata={"model": MODEL, "attempt": attempt, "ticket_id": ticket_id_in(user_input)},
    )


def reset_flaky_counter() -> None:
    """Restart the alternating pattern. Call before an assertion on flakiness."""
    global _ATTEMPTS
    _ATTEMPTS = itertools.count(1)


class LoopingAgent(BaseAdapter):
    """Calls a tool in a loop until ``max_steps`` stops it.

    Declared as a class rather than a function so ``lookup_ticket`` is a real
    declared tool: a plain function adapter has no tool declarations, and calling
    ``ctx.tools.call`` from one would be rejected as *undeclared* long before the
    step budget could ever be reached.
    """

    name = "looping-agent"

    tools = {
        "lookup_ticket": ToolDecl(
            name="lookup_ticket",
            live=lookup_ticket,
            side_effect=False,
            description="Fetch a support ticket by id.",
        )
    }

    def run(self, user_input: str, ctx: RunContext) -> AgentResult:
        while True:
            ctx.tools.call("lookup_ticket", ticket_id="T-1001")


run_agent_looping = LoopingAgent


def run_agent_undeclared_tool(user_input: str, ctx: RunContext) -> AgentResult:
    """Calls a tool the adapter never declared. Must surface as an error."""
    return ctx.tools.call("shell_exec", command="rm -rf /")  # type: ignore[return-value]


__all__ = [
    "ANSWERS",
    "CUSTOMERS",
    "MODEL",
    "ORDERS",
    "TICKETS",
    "LiveSupportAgent",
    "LoopingAgent",
    "Order",
    "SupportAgent",
    "lookup_customer",
    "lookup_order",
    "lookup_ticket",
    "order_id_in",
    "reset_flaky_counter",
    "run_agent",
    "run_agent_flaky",
    "run_agent_looping",
    "run_agent_secretive",
    "run_agent_slow",
    "run_agent_undeclared_tool",
    "ticket_id_in",
    "usage_for",
    "wants_escalation",
]
