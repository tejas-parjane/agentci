"""Assertion implementations and the fluent ``expect()`` facade.

Submodules are pure and independently testable:

``base``
    Result construction, the collector, and shared formatting.
``output``
    Text and JSON structure of the final answer.
``tools``
    Tool identity, order, count, arguments, success, retries.
``execution``
    Cost, latency, tokens, steps, retries, loop detection.
``policy``
    Per-test policy expectations and approval evidence.
``leaks``
    Data-leakage scanning.
``fluent``
    The chainable surface users write.
"""

from agentci.assertions.base import (
    AssertionCollector,
    AssertionFailed,
    ExpectationError,
    Kind,
)
from agentci.assertions.fluent import Expectation, expect

__all__ = [
    "AssertionCollector",
    "AssertionFailed",
    "Expectation",
    "ExpectationError",
    "Kind",
    "expect",
]
