"""The forbidden-data policy check must attribute a hit to the *right* pattern.

The regression this file exists to prevent: ``_check_data`` and
``to_not_contain_forbidden_data`` used ``contains_secret``, which is true when
*any* active pattern matches. A tool result carrying an email address then made
a ``credit_card`` policy fail with a message blaming a card that was never there.
"""

from __future__ import annotations

from agentci.assertions import policy
from agentci.core.recorder import TraceRecorder
from agentci.core.redaction import Redactor


def _recorded(**result: object):
    recorder = TraceRecorder("run_policy")
    with recorder.tool_call("charge_order", {"order_id": "ORD-7781"}) as call:
        call.result = result if result else {"queued": True}
    return recorder.trace


def test_other_patterns_do_not_trip_an_unrelated_forbidden_pattern() -> None:
    trace = _recorded(to="ravi.menon@example.com")
    result = policy.forbidden_data_absent(trace, Redactor(patterns=["credit_card"]), ["credit_card"])
    assert result.ok, "an email is not a credit card and must not be reported as one"


def test_the_given_pattern_is_still_caught() -> None:
    trace = _recorded(card="4111111111111111")
    result = policy.forbidden_data_absent(trace, Redactor(patterns=["credit_card"]), ["credit_card"])
    assert not result.ok
    assert result.actual and "credit_card" in result.actual[0]


def test_literal_substrings_still_match() -> None:
    trace = _recorded(note="reset ACCT-9931")
    result = policy.forbidden_data_absent(trace, Redactor(), ["ACCT-9931"])
    assert not result.ok, "a project identifier must still be catchable as a literal"
