"""HTTP adapter: test an agent that runs behind a network boundary (PRD §FR-1).

The contract is a ``POST`` with ``{"input": "<user text>"}`` returning::

    {
      "output_text": "...",
      "trace": [ ...AgentCI TraceEvent dicts... ],
      "metadata": { "model": "gpt-5-mini", "prompt_version": "3" }
    }

An agent that already returns that shape needs no SDK at all — which is the point:
AgentCI adapts to the agent rather than requiring the agent to adopt AgentCI
(PRD §4, pillar 6).

Implemented on :mod:`urllib.request` deliberately. An HTTP test adapter must not
drag a client library into everyone's environment, and the request/response
surface here is small enough that ``urllib`` is sufficient.

See :mod:`agentci.serve` for the matching minimal server, so the round trip is
testable without writing any server code.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from agentci.core.context import RunContext
from agentci.core.result import AgentResult
from agentci.core.trace import EventStatus, EventType, TraceEvent, new_id
from agentci.errors import AdapterError

DEFAULT_TIMEOUT_S = 30.0

#: Refuse to talk to these, so a misconfigured endpoint cannot be used to reach
#: cloud metadata services or reach back into the test host (SSRF / AC-10).
_BLOCKED_HOSTNAMES = frozenset(
    {
        "169.254.169.254",
        "metadata.google.internal",
        "metadata.goog",
        "localhost.localdomain",
    }
)


@dataclass
class HttpAgentAdapter:
    """Adapter that delegates a run to an HTTP endpoint.

    :param url: full endpoint URL
    :param timeout_s: request timeout; AgentCI also enforces ``budgets.max_latency_ms``
    :param headers: extra request headers. Use for auth in CI; never inline a
        literal secret — read it from the environment.
    :param input_field: request key carrying the user text
    :param output_field: response key carrying the answer text
    :param send_metadata: include AgentCI's run id so a server can correlate
    """

    url: str
    timeout_s: float = DEFAULT_TIMEOUT_S
    headers: dict[str, str] = field(default_factory=dict)
    method: str = "POST"
    input_field: str = "input"
    output_field: str = "output_text"
    trace_field: str = "trace"
    metadata_field: str = "metadata"
    send_metadata: bool = True
    name: str = "http-agent"

    def __post_init__(self) -> None:
        self._validate_url()

    def _validate_url(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in ("http", "https"):
            raise AdapterError(
                f"http adapter url must be http(s), got {self.url!r}",
                hint="AgentCI does not execute code received over the network",
            )
        host = (parsed.hostname or "").lower()
        if not host:
            raise AdapterError(f"http adapter url has no host: {self.url!r}")
        if host in _BLOCKED_HOSTNAMES:
            raise AdapterError(
                f"refusing to call metadata endpoint {host!r}",
                hint="this is almost always a configuration mistake",
            )

    # -- contract helpers -----------------------------------------------------

    def build_payload(self, user_input: str, ctx: RunContext) -> dict[str, Any]:
        payload: dict[str, Any] = {self.input_field: user_input}
        if self.send_metadata:
            payload["_agentci"] = {"run_id": ctx.run_id, "agent": self.name}
        return payload

    def parse_response(self, body: bytes) -> AgentResult:
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError(f"agent endpoint returned invalid JSON: {exc}") from exc
        if not isinstance(data, Mapping):
            raise AdapterError(
                f"agent endpoint returned {type(data).__name__}, expected a JSON object"
            )

        output = data.get(self.output_field, data.get("output", ""))
        raw_trace = data.get(self.trace_field) or []
        if not isinstance(raw_trace, list):
            raise AdapterError(
                f"agent endpoint field {self.trace_field!r} must be a list, "
                f"got {type(raw_trace).__name__}"
            )

        return AgentResult(
            output_text=str(output or ""),
            trace=[_coerce_remote_event(item) for item in raw_trace],
            metadata=dict(data.get(self.metadata_field) or {}),
        )

    # -- invocation -----------------------------------------------------------

    def run(self, user_input: str, ctx: RunContext) -> AgentResult:
        payload = json.dumps(self.build_payload(user_input, ctx)).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - scheme validated above
            self.url,
            data=payload if self.method != "GET" else None,
            method=self.method,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                **self.headers,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise AdapterError(
                f"agent endpoint returned HTTP {exc.code}",
                hint=f"response body: {detail}" if detail else None,
            ) from exc
        except urllib.error.URLError as exc:
            raise AdapterError(
                f"cannot reach agent endpoint: {exc.reason}",
                hint="is the agent server running? For a local demo try `agentci serve`",
            ) from exc
        except TimeoutError as exc:
            raise AdapterError(f"agent endpoint timed out after {self.timeout_s}s") from exc
        except OSError as exc:
            raise AdapterError(f"agent endpoint request failed: {exc}") from exc

        result = self.parse_response(body)
        ctx.set("adapter", "http")
        ctx.set("endpoint", self.url)
        if result.metadata.get("model"):
            ctx.set("model", result.metadata["model"])
        return result

    def __repr__(self) -> str:
        return f"HttpAgentAdapter(url={self.url!r}, name={self.name!r})"


def _coerce_remote_event(item: Any) -> TraceEvent:
    """Validate a trace event received over the wire.

    Remote payloads are untrusted input (PRD §34). Anything unrecognised becomes
    a generic event rather than being trusted as a model/tool call, and unknown
    fields are rejected outright instead of being silently retained.
    """
    if isinstance(item, TraceEvent):
        return item
    if not isinstance(item, Mapping):
        return TraceEvent(
            run_id="remote",
            event_id=new_id("evt"),
            type=EventType.RUN_COMPLETED,
            status=EventStatus.SUCCESS,
            metadata={"unparsed_event": str(item)[:200]},
        )

    data = dict(item)
    data.setdefault("run_id", "remote")
    data.setdefault("event_id", new_id("evt"))
    raw_type = data.get("type")
    try:
        EventType(raw_type)
    except ValueError:
        data["type"] = EventType.RUN_COMPLETED.value
        data.setdefault("metadata", {})
        if isinstance(data["metadata"], dict):
            data["metadata"] = {
                **data["metadata"],
                "unrecognised_event_type": str(raw_type)[:120],
            }
        data.pop("tool", None)
    try:
        return TraceEvent.model_validate(data)
    except Exception:
        return TraceEvent(
            run_id=str(data.get("run_id", "remote")),
            event_id=str(data.get("event_id", new_id("evt"))),
            type=EventType.RUN_COMPLETED,
            status=EventStatus.SUCCESS,
            metadata={"unparsable_event": str(item)[:200]},
        )


def is_loopback(host: str) -> bool:
    """True when ``host`` resolves to a loopback address."""
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return any(
            info[4][0] == "127" or info[4][0] == "::1"
            for info in socket.getaddrinfo(host, None)
        )
    except OSError:  # pragma: no cover - resolution failure
        return False


__all__ = ["DEFAULT_TIMEOUT_S", "HttpAgentAdapter", "is_loopback"]
