"""Redaction applied at the serialization boundary.

**Why here and not in the recorder.** If redaction ran while events were
recorded, assertions could no longer see real values — ``to_not_leak`` would be
asserting against already-scrubbed text, and a test could not verify that the
agent correctly *handled* a customer's email address. By redacting only as data
leaves the process (reports, trace files, PR comments), assertions keep fidelity
and AC-11 ("configured secret values do not appear in reports or persisted trace
payloads") is satisfied structurally rather than by convention.

Three complementary layers:

1. **Named patterns** — PII and credential shapes (email, ssn, jwt, aws keys ...).
2. **Field-name matching** — configured keys such as ``authorization`` are
   replaced wholesale, regardless of value shape.
3. **Environment literal scrubbing** — the literal value of every ``*_KEY``,
   ``*_TOKEN``, ``*_SECRET``, ``*_PASSWORD`` variable is removed from all output.
   This catches secrets no pattern can predict, which is the failure mode that
   actually leaks credentials in CI logs.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

PLACEHOLDER: Final = "[REDACTED:{name}]"

#: Environment variable suffixes whose values are treated as literal secrets.
SECRET_ENV_SUFFIXES: Final = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH")

#: Shortest literal secret we will bother scrubbing, to avoid mangling output
#: on trivial values like ``1`` or ``true``.
MIN_SECRET_LENGTH: Final = 6

#: Maximum traversal depth for redaction, so a self-referential structure cannot
#: cause unbounded recursion when reporting hostile payloads.
MAX_DEPTH: Final = 32


@dataclass(frozen=True, slots=True)
class Pattern:
    """A named redaction pattern."""

    name: str
    regex: re.Pattern[str]

    def search(self, text: str) -> re.Match[str] | None:
        return self.regex.search(text)

    def finditer(self, text: str) -> Iterable[re.Match[str]]:
        return self.regex.finditer(text)


# Ordered most-specific first so that, for example, an AWS key is not first
# swallowed by a generic api_key rule.
_PATTERN_LIBRARY: Final[tuple[Pattern, ...]] = (
    Pattern(
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
    ),
    Pattern("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b")),
    Pattern("authorization", re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")),
    Pattern(
        "api_key",
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
            r"|xox[abpsr]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})\b"
        ),
    ),
    Pattern(
        "private_key",
        re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END [^-]+-----"),
    ),
    Pattern("ssn", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")),
    Pattern(
        "credit_card",
        re.compile(r"\b(?:4\d{12}(?:\d{3})?|5[1-5]\d{14}|3[47]\d{13}|6(?:011|5\d{2})\d{12})\b"),
    ),
    Pattern(
        "email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
    Pattern(
        "phone",
        re.compile(
            r"(?<![\w.])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?![\w.])"
        ),
    ),
)

_PATTERNS_BY_NAME: Final[dict[str, Pattern]] = {p.name: p for p in _PATTERN_LIBRARY}

#: Always-on patterns. Extendable via ``redaction.patterns`` in agentci.yaml.
DEFAULT_PATTERNS: Final = frozenset(p.name for p in _PATTERN_LIBRARY)

#: Always-on field names, applied even without configuration.
DEFAULT_FIELDS: Final = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "client_secret",
        "secret",
        "password",
        "passwd",
        "pwd",
        "private_key",
        "session_key",
        "cookie",
        "set-cookie",
        "ssn",
    }
)


class Redactor:
    """Removes secrets and PII from arbitrary structures.

    Instances are immutable after construction and safe to share between threads.
    """

    __slots__ = ("_enabled", "_fields", "_patterns", "_secrets")

    def __init__(
        self,
        *,
        fields: Iterable[str] = (),
        patterns: Iterable[str] = (),
        secrets: Iterable[str] = (),
        enabled: bool = True,
    ) -> None:
        wanted = {p.strip().lower() for p in patterns}
        unknown = wanted - set(_PATTERNS_BY_NAME)
        if unknown:
            raise ValueError(
                f"unknown redaction pattern(s): {sorted(unknown)}. "
                f"Available: {sorted(_PATTERNS_BY_NAME)}"
            )
        # Patterns named in config are additive to a minimal always-on credential
        # set, so a user who lists only 'email' still never leaks a JWT.
        self._patterns: tuple[Pattern, ...] = tuple(
            p for p in _PATTERN_LIBRARY if p.name in wanted or p.name in _ALWAYS_ON
        )
        self._fields = {f.strip().lower() for f in fields} | DEFAULT_FIELDS
        self._secrets = tuple(sorted({s for s in secrets if len(s) >= MIN_SECRET_LENGTH}, key=len, reverse=True))
        self._enabled = enabled

    # -- configuration helpers ------------------------------------------------

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Redactor:
        """Build a redactor that scrubs every secret-looking environment variable."""
        env = os.environ if environ is None else environ
        secrets = [
            value
            for name, value in env.items()
            if value and name.upper().endswith(SECRET_ENV_SUFFIXES) and len(value) >= MIN_SECRET_LENGTH
        ]
        return cls(secrets=secrets)

    def with_patterns(self, *names: str) -> Redactor:
        return Redactor(
            fields=self._fields,
            patterns={*names, *(p.name for p in self._patterns)},
            secrets=self._secrets,
            enabled=self._enabled,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def pattern_names(self) -> frozenset[str]:
        """Names of the pattern classes this redactor can detect and scrub."""
        return frozenset(p.name for p in self._patterns)

    @property
    def secrets(self) -> tuple[str, ...]:
        """Literal secret values this redactor scrubs. Never include in output."""
        return self._secrets

    # -- redaction ------------------------------------------------------------

    def redact_text(self, text: str) -> str:
        """Apply pattern and literal-secret scrubbing to a single string."""
        if not self._enabled or not text:
            return text
        for pattern in self._patterns:
            text = pattern.regex.sub(PLACEHOLDER.format(name=pattern.name), text)
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, PLACEHOLDER.format(name="secret"))
        return text

    def redact(self, value: Any, *, _depth: int = 0) -> Any:
        """Recursively redact a structure.

        Mapping keys are matched against the configured field names
        (case-insensitive substring, so ``user_api_key`` is caught by ``api_key``).
        A matched key's value is replaced entirely — no pattern guessing.
        """
        if not self._enabled or _depth > MAX_DEPTH:
            return value

        if isinstance(value, str):
            return self.redact_text(value)

        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                if self._is_sensitive_field(key_text):
                    out[key_text] = PLACEHOLDER.format(name="field")
                else:
                    out[key_text] = self.redact(item, _depth=_depth + 1)
            return out

        if isinstance(value, list):
            return [self.redact(item, _depth=_depth + 1) for item in value]

        if isinstance(value, tuple):
            return tuple(self.redact(item, _depth=_depth + 1) for item in value)

        if isinstance(value, set):
            return {self.redact(item, _depth=_depth + 1) for item in value}

        return value

    def _is_sensitive_field(self, key: str) -> bool:
        lowered = key.lower()
        return any(field_name in lowered for field_name in self._fields)

    def __repr__(self) -> str:
        return (
            f"Redactor(patterns={sorted(p.name for p in self._patterns)}, "
            f"fields={len(self._fields)}, secrets={len(self._secrets)}, enabled={self._enabled})"
        )

    def redact_events(self, events: Iterable[Any]) -> list[Any]:
        """Redact a list of :class:`TraceEvent` models in place-safe fashion."""
        out = []
        for event in events:
            if hasattr(event, "model_copy") and hasattr(event, "metadata"):
                out.append(
                    event.model_copy(
                        update={
                            "tool": (
                                event.tool.model_copy(
                                    update={"arguments": self.redact(event.tool.arguments)}
                                )
                                if event.tool
                                else None
                            ),
                            "result": self.redact(event.result),
                            "metadata": self.redact(event.metadata),
                            "component": self.redact(event.component)
                            if isinstance(event.component, str)
                            else event.component,
                        }
                    )
                )
            else:  # pragma: no cover - defensive
                out.append(self.redact(event))
        return out

    def contains_secret(self, text: str, *, extra_patterns: Iterable[str] = ()) -> bool:
        """True if ``text`` contains any literal secret or matching pattern."""
        if not self._enabled:
            return False
        if any(secret in text for secret in self._secrets):
            return True
        patterns = [*self._patterns, *(_PATTERNS_BY_NAME[n] for n in extra_patterns)]
        return any(pattern.search(text) for pattern in patterns)

    def find_leaks(self, text: str, *, extra_patterns: Iterable[str] = ()) -> list[str]:
        """Return the names of every pattern that matched ``text``."""
        if not self._enabled:
            return []
        patterns = [*self._patterns, *(_PATTERNS_BY_NAME[n] for n in extra_patterns)]
        found = [p.name for p in patterns if p.search(text)]
        if any(secret in text for secret in self._secrets):
            found.append("secret")
        return sorted(set(found))


# Patterns scrubbed regardless of configuration: leaking these from a test log
# is never acceptable and users never knowingly opt into it.
_ALWAYS_ON: Final = frozenset(
    {"aws_access_key", "jwt", "authorization", "api_key", "private_key", "ssn"}
)


def default_redactor() -> Redactor:
    """Redactor with all library patterns plus environment secret scrubbing."""
    return Redactor.from_env().with_patterns(*DEFAULT_PATTERNS)


__all__ = [
    "DEFAULT_FIELDS",
    "DEFAULT_PATTERNS",
    "PLACEHOLDER",
    "Pattern",
    "Redactor",
    "default_redactor",
]
