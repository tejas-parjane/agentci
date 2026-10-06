"""Leak assertions must scan the whole catalog, not just the active subset.

The regression this file exists to prevent: ``to_not_leak("email")`` used to
treat ``email`` as a literal substring because the redactor's default pattern
set does not include it. The assertion then searched for the word "email" in
the output and passed -- a security assertion that could not fail.
"""

from __future__ import annotations

import pytest

from agentci import expect
from agentci.assertions import leaks
from agentci.assertions.base import AssertionFailed
from agentci.core.recorder import TraceRecorder
from agentci.core.redaction import Redactor
from agentci.core.result import AgentResult, as_view
from agentci.core.trace import Trace

EMPTY_TRACE = Trace(run_id="run_empty", events=[])


def status_of(result) -> str:
    return result.status.value


def test_named_pattern_outside_the_active_set_is_scanned() -> None:
    result = leaks.not_leak(EMPTY_TRACE, "contact alice@example.com", Redactor(), ["email"])
    assert not result.ok, "email in output must fail an email leak assertion"


def test_credit_card_in_the_catalog_is_caught() -> None:
    result = leaks.not_leak(
        EMPTY_TRACE, "card 4111111111111111", Redactor(), ["credit_card"]
    )
    assert not result.ok, "a card number must fail a credit_card leak assertion"


def test_phone_in_the_catalog_is_caught() -> None:
    result = leaks.not_leak(EMPTY_TRACE, "reachable on 555-123-4567", Redactor(), ["phone"])
    assert not result.ok, "a phone number must fail a phone leak assertion"


def test_named_pattern_passes_when_the_data_is_absent() -> None:
    result = leaks.not_leak(EMPTY_TRACE, "the ticket is resolved", Redactor(), ["email"])
    assert result.ok


def test_literal_substring_is_still_matched() -> None:
    assert not leaks.not_leak(EMPTY_TRACE, "order ACCT-9931", Redactor(), ["ACCT-9931"]).ok
    assert leaks.not_leak(EMPTY_TRACE, "order ACCT-1", Redactor(), ["ACCT-9931"]).ok


def test_active_credential_patterns_are_caught() -> None:
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    result = leaks.not_leak(EMPTY_TRACE, f"token {jwt}", Redactor(), ["jwt"])
    assert not result.ok


def test_patterns_are_searched_in_tool_arguments_not_only_output() -> None:
    recorder = TraceRecorder("run_leak")
    with recorder.tool_call("charge_card", {"card": "4111111111111111"}):
        pass
    result = leaks.not_leak(recorder.trace, "", Redactor(), ["credit_card"])
    assert not result.ok, "a secret in a tool argument is still a leak"
    assert any("charge_card" in str(entry) for entry in (result.actual or []))


def test_no_patterns_given_skips_rather_than_passing() -> None:
    result = leaks.not_leak(EMPTY_TRACE, "anything", Redactor(), [])
    assert status_of(result) == "skipped"


def test_available_pattern_names_is_wider_than_the_active_set() -> None:
    redactor = Redactor()
    catalog = redactor.available_pattern_names()
    assert {"email", "credit_card", "phone"} <= catalog
    assert catalog - redactor.pattern_names, "catalog must not be just the active set"


def test_expect_not_leak_fails_end_to_end() -> None:
    result = AgentResult(
        output_text="write to alice@example.com",
        trace=list(EMPTY_TRACE),
        metadata={},
    )
    chain = expect(as_view(result))
    # Outside a test body expect() is standalone, so it fails fast.
    with pytest.raises(AssertionFailed):
        chain.to_not_leak("email")
    assert not chain.results[-1].ok


def test_expect_not_leak_passes_end_to_end() -> None:
    result = AgentResult(
        output_text="the ticket is resolved",
        trace=list(EMPTY_TRACE),
        metadata={},
    )
    chain = expect(as_view(result))
    chain.to_not_leak("email")
    assert chain.results[-1].ok
