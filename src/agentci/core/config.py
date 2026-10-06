"""Versioned configuration schema for ``agentci.yaml``.

Config is the project's contract with CI, so parsing is strict: unknown keys are
rejected, numbers are coerced in one place, and every failure raises
:class:`~agentci.errors.ConfigError` with the offending path. A config error exits
``2`` and never degrades into a partially-applied run.

Versioning (ADR-003): ``version:`` is required at the top level and must equal
:data:`agentci.__about__.CONFIG_VERSION`. A breaking change increments it. Unknown
top-level keys are an error rather than a warning, so a typo in
``policies.denied_tool`` cannot silently disable a safety gate.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentci.__about__ import CONFIG_VERSION
from agentci.errors import ConfigError

#: Names searched, in order, when locating the config file.
CONFIG_FILENAMES = ("agentci.yaml", "agentci.yml", ".agentci.yaml")

_COMPARISON_RE = re.compile(r"^\s*(>=|<=|==|!=|>|<|=)?\s*(.+?)\s*$")


class _Strict(BaseModel):
    """Base for every config section: unknown keys are errors."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_assignment=True)


class Threshold(_Strict):
    """A release-gate threshold (PRD §18).

    Three equivalent spellings, because YAML users reach for all of them::

        task_success: 0.90                  # shorthand for min
        policy_compliance: "== 1.0"
        max_cost_usd: {lte: 0.05}
    """

    gt: float | None = None
    gte: float | None = None
    lt: float | None = None
    lte: float | None = None
    eq: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, value: object) -> object:
        if isinstance(value, Threshold):
            return value
        if isinstance(value, bool):
            raise ValueError("threshold must be a number, not a boolean")
        if isinstance(value, int | float):
            return {"gte": float(value)}
        if isinstance(value, str):
            match = _COMPARISON_RE.match(value)
            if not match:
                raise ValueError(f"cannot parse threshold from {value!r}")
            op, raw = match.group(1) or ">=", match.group(2)
            try:
                number = float(raw)
            except ValueError as exc:
                raise ValueError(f"threshold {value!r} is not numeric") from exc
            return {
                ">=": {"gte": number},
                ">": {"gt": number},
                "<=": {"lte": number},
                "<": {"lt": number},
                "=": {"eq": number},
                "==": {"eq": number},
                "!=": {"lte": number - 1e-12},
            }[op]
        if isinstance(value, Mapping):
            return value
        raise ValueError(f"cannot parse threshold from {value!s}")

    @property
    def target(self) -> str:
        for label, value in (
            ("gt", self.gt),
            ("gte", self.gte),
            ("lt", self.lt),
            ("lte", self.lte),
            ("eq", self.eq),
        ):
            if value is not None:
                op = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "=="}[label]
                return f"{op} {value}"
        return "any"

    def evaluate(self, actual: float) -> bool:
        """Whether ``actual`` satisfies every constraint this threshold declares.

        All declared constraints must hold. A threshold is an AND of its parts, so
        ``{"gte": 0.9, "lte": 1.0}`` is a band rather than a contradiction.
        """
        violations = (
            (self.gt is not None and not actual > self.gt)
            or (self.gte is not None and not actual >= self.gte)
            or (self.lt is not None and not actual < self.lt)
            or (self.lte is not None and not actual <= self.lte)
            or (self.eq is not None and not abs(actual - self.eq) <= 1e-9)
        )
        return not violations

    def describe(self, actual: float) -> str:
        return f"actual={actual:.4g}, required {self.target}"


class ProjectConfig(_Strict):
    name: str = "agent"
    version: str | None = None
    description: str | None = None


class AgentConfig(_Strict):
    """Adapter selection.

    ``adapter`` is ``module.path:attribute``. The attribute may be an
    :class:`~agentci.adapters.base.AgentAdapter` subclass/instance, a class with
    a ``run`` method, or a plain function ``(user_input) -> AgentResult``.
    """

    adapter: str = Field(description="module.path:attribute of the agent under test")
    name: str | None = Field(default=None, description="display name; defaults to project name")
    #: Model id used for cost estimation when the adapter does not report usage.
    model: str | None = None
    #: Extra keyword arguments injected into the adapter constructor.
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("adapter")
    @classmethod
    def _shape(cls, value: str) -> str:
        if ":" not in value:
            raise ValueError(
                f"agent.adapter must be 'module.path:attribute', got {value!r}"
            )
        module, _, attr = value.partition(":")
        if not module or not attr:
            raise ValueError(f"agent.adapter must be 'module.path:attribute', got {value!r}")
        return value

    @property
    def module_path(self) -> str:
        return self.adapter.partition(":")[0]

    @property
    def attribute(self) -> str:
        return self.adapter.partition(":")[2]


class EvaluationConfig(_Strict):
    """Repeat policy for probabilistic tests (PRD §20)."""

    repeat: int = Field(default=1, ge=1, le=100)
    minimum_pass_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    #: When a test fails some iterations and passes others, mark it FLAKY instead of FAIL.
    allow_flaky: bool = True

    @model_validator(mode="after")
    def _sane(self) -> Self:
        if self.repeat > 1 and self.minimum_pass_rate == 1.0 and not self.allow_flaky:
            raise ValueError(
                "evaluation.repeat > 1 with minimum_pass_rate 1.0 will always fail; "
                "lower minimum_pass_rate or set allow_flaky: true"
            )
        return self


class Budgets(_Strict):
    """Hard per-invocation limits. Exceeding one fails the test deterministically."""

    max_cost_usd: float | None = Field(default=None, gt=0)
    max_latency_ms: float | None = Field(default=None, gt=0)
    max_tool_calls: int | None = Field(default=None, gt=0)
    max_steps: int | None = Field(default=None, gt=0)
    max_retries: int | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, gt=0)

    def describe(self) -> str:
        parts = [
            f"{name}={value}"
            for name, value in (
                ("max_cost_usd", self.max_cost_usd),
                ("max_latency_ms", self.max_latency_ms),
                ("max_tool_calls", self.max_tool_calls),
                ("max_steps", self.max_steps),
                ("max_retries", self.max_retries),
                ("max_tokens", self.max_tokens),
            )
            if value is not None
        ]
        return ", ".join(parts) if parts else "none"


class Policies(_Strict):
    """Policy as code (PRD §4, §22)."""

    allowed_tools: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    approval_required: list[str] = Field(default_factory=list)
    forbidden_data_patterns: list[str] = Field(default_factory=list)

    @field_validator("allowed_tools", "denied_tools", "approval_required", mode="after")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in value:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    @model_validator(mode="after")
    def _no_contradiction(self) -> Self:
        overlap = set(self.allowed_tools) & set(self.denied_tools)
        if overlap:
            raise ValueError(
                f"tools listed in both policies.allowed_tools and policies.denied_tools: "
                f"{sorted(overlap)}. Deny wins at runtime; remove the contradiction."
            )
        return self


class RedactionConfig(_Strict):
    """Serialization-boundary scrubbing (ADR-008)."""

    enabled: bool = True
    patterns: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    #: Also scrub the literal values of secret-looking environment variables.
    scrub_environment: bool = True


class RegressionConfig(_Strict):
    """Relative-regression gates (PRD §FR-5, §19)."""

    baseline: str = ".agentci/baseline.json"
    max_quality_drop: float | None = Field(default=None, ge=0.0, le=1.0)
    max_cost_increase_pct: float | None = Field(default=None, ge=0.0)
    max_latency_increase_pct: float | None = Field(default=None, ge=0.0)
    #: ``fail`` blocks the build on regression; ``warn`` only annotates.
    on_regression: Literal["fail", "warn"] = "fail"
    #: Fail when a test that passed at baseline now fails.
    fail_on_new_test_failures: bool = True
    #: Ignore regressions below this absolute magnitude, to avoid noise from
    #: near-zero baselines (e.g. cost going 0.0001 -> 0.0004).
    ignore_regression_below_pct: float = Field(default=5.0, ge=0.0)


class MockSpec(_Strict):
    """A configured stand-in for an external tool."""

    response: Any = None
    match_arguments: bool = False
    #: Optional per-tool delay, used to test latency budgets deterministically.
    delay_ms: float | None = Field(default=None, ge=0)

    @field_validator("response", mode="before")
    @classmethod
    def _default_response(cls, value: object) -> object:
        return {} if value is None else value


class ExecutionConfig(_Strict):
    """Side-effect safety and mocking (PRD §FR-8, AC-10, AC-12)."""

    external_side_effects: Literal["deny", "allow"] = "deny"
    side_effecting_tools: list[str] = Field(default_factory=list)
    mocks: dict[str, MockSpec] = Field(default_factory=dict)


class GateConfig(_Strict):
    """Dimension-level release gates (PRD §18).

    Absence of a threshold means "do not gate on this dimension".
    """

    task_success: Threshold | None = None
    tool_correctness: Threshold | None = None
    policy_compliance: Threshold | None = None
    reliability: Threshold | None = None
    max_cost_usd: Threshold | None = None
    max_latency_ms: Threshold | None = None
    max_tokens: Threshold | None = None

    def active(self) -> dict[str, Threshold]:
        """Thresholds that are actually configured, as ``{name: Threshold}``."""
        out: dict[str, Threshold] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, Threshold):
                out[name] = value
        return out


class TestFileConfig(_Strict):
    file: str | None = None
    dir: str | None = None
    pattern: str = "test_*.py"
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_of(self) -> Self:
        if bool(self.file) == bool(self.dir):
            raise ValueError("each tests[] entry must set exactly one of 'file' or 'dir'")
        return self


class SelectionConfig(_Strict):
    """Change-aware test selection (PRD §FR-6).

    ``unmatched`` is the safe default. A test that declares no ``dependencies``
    cannot be proven unaffected by a change, so it runs. Opting into
    ``unmatched: skip`` trades that safety for CI minutes, and AgentCI warns when
    it does.
    """

    enabled: bool = True
    unmatched: Literal["run", "skip"] = "run"
    default_base: str | None = None
    #: Treat any change to these paths as affecting every test.
    global_paths: list[str] = Field(
        default_factory=lambda: ["agentci.yaml", "agentci.yml", ".agentci.yaml"]
    )


class StorageConfig(_Strict):
    """Run artifact persistence (ADR-002)."""

    dir: str = ".agentci"
    record_traces: bool = True
    retention_days: int = Field(default=30, ge=0)
    #: Cap per-run trace size so a pathological agent cannot fill the disk.
    max_trace_events: int = Field(default=10_000, ge=100)


class ReportConfig(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    #: Named ``json_enabled`` rather than ``json`` because ``json`` is already a
    #: ``BaseModel`` method, and a property of that name would shadow it. The YAML
    #: key stays ``report.json`` via the alias, which is what users actually type.
    json_enabled: bool = Field(default=True, alias="json")
    markdown: bool = True
    html: bool = False
    #: Emit `::error::` / `::warning::` workflow annotations when in CI.
    ci_annotations: bool = True
    #: Write a Markdown block to $GITHUB_STEP_SUMMARY when present.
    ci_summary: bool = True
    #: Show the trace of every failed test in the Markdown report.
    trace_excerpt_events: int = Field(default=40, ge=0)


class Config(_Strict):
    """Root configuration model."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_assignment=True)

    version: int = Field(default=CONFIG_VERSION, ge=1)
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    agent: AgentConfig
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    budgets: Budgets = Field(default_factory=Budgets)
    policies: Policies = Field(default_factory=Policies)
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)
    regression: RegressionConfig = Field(default_factory=RegressionConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    gates: GateConfig = Field(default_factory=GateConfig)
    tests: list[TestFileConfig] = Field(default_factory=list)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    pricing: dict[str, Any] = Field(default_factory=dict)
    #: Reserved for Phase 2 (PRD §22). Accepted and preserved, not yet enforced.
    mcp: dict[str, Any] = Field(default_factory=dict)

    @field_validator("version")
    @classmethod
    def _supported(cls, value: int) -> int:
        if value != CONFIG_VERSION:
            raise ValueError(
                f"unsupported config version {value}; this AgentCI build supports "
                f"version {CONFIG_VERSION}. A breaking config change requires a "
                f"config version bump."
            )
        return value

    # -- derived paths -------------------------------------------------------

    @property
    def baseline_path(self) -> Path:
        return Path(self.regression.baseline)

    @property
    def storage_dir(self) -> Path:
        return Path(self.storage.dir)

    @property
    def agent_display_name(self) -> str:
        return self.agent.name or self.project.name

    def test_files(self, base: Path | None = None) -> list[str]:
        """Expand ``tests`` into concrete file paths, honouring ``dir`` globs.

        ``dir`` entries are globbed relative to ``base`` — the project root — and
        not the process working directory, so ``agentci run --root <dir>`` resolves
        the same files no matter where it is invoked from. Returned paths are
        relative to ``base`` when they live under it, keeping error messages short.
        """
        root = base or Path.cwd()
        expanded: list[str] = []
        for entry in self.tests:
            if entry.file:
                expanded.append(entry.file)
                continue
            assert entry.dir is not None  # narrowed by TestFileConfig validator
            directory = Path(entry.dir)
            if not directory.is_absolute():
                directory = root / directory
            if not directory.exists():
                continue
            for path in sorted(directory.rglob(entry.pattern)):
                if not path.is_file():
                    continue
                expanded.append(
                    str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
                )
        return expanded


def find_config(start: Path | None = None) -> Path | None:
    """Walk upward from ``start`` looking for a config file."""
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        for name in CONFIG_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def load_config(path: str | Path | None = None, *, search_from: Path | None = None) -> Config:
    """Load and validate ``agentci.yaml``.

    Raises :class:`ConfigError` with the file path and, where pydantic can
    supply it, the exact dotted location of the offending key.
    """
    target = Path(path) if path else find_config(search_from)
    if target is None:
        raise ConfigError(
            "no agentci.yaml found",
            hint="run `agentci init` to create one, or pass --config",
        )
    target = Path(target)
    if not target.is_file():
        raise ConfigError(f"config file not found: {target}")

    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {target}: {exc}") from exc

    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {target}: {exc}") from exc

    if not isinstance(data, Mapping):
        raise ConfigError(f"{target} must contain a YAML mapping at the top level")

    try:
        return Config.model_validate(dict(data))
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(_format_validation_error(exc, target)) from exc


def _format_validation_error(exc: Exception, target: Path) -> str:
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return f"invalid configuration in {target}: {exc}"
    lines = [f"invalid configuration in {target}:"]
    for err in errors():
        loc = ".".join(str(part) for part in err.get("loc", ())) or "<root>"
        message = err.get("msg", "invalid value")
        message = message.removeprefix("Value error, ")
        lines.append(f"  {loc}: {message}")
    return "\n".join(lines)


def dump_config(config: Config) -> str:
    """Serialize a config back to YAML, preserving key order for readability."""
    dumped: str = yaml.safe_dump(
        config.model_dump(mode="json", exclude_none=True, by_alias=True),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return dumped


def example_config(project_name: str = "my-agent", adapter: str = "tests.agent:run_agent") -> str:
    """The starter config written by ``agentci init``."""
    return f"""# AgentCI configuration
# Full reference: https://github.com/tejas-parjane/agentci/blob/main/docs/reference/configuration.md
version: {CONFIG_VERSION}

project:
  name: {project_name}

# The agent under test: "module.path:attribute"
agent:
  adapter: {adapter}
  # model: gpt-5-mini   # used for cost estimation when the adapter reports no usage

evaluation:
  repeat: 1
  minimum_pass_rate: 1.0

# Hard limits. Exceeding any of these fails the test deterministically.
budgets:
  max_cost_usd: 0.10
  max_latency_ms: 10000
  max_steps: 15

# Policy as code. Deny always wins over allow.
policies:
  allowed_tools: []
  denied_tools: []
  approval_required: []
  forbidden_data_patterns: []

# Secrets are scrubbed as data leaves the process, never in memory.
redaction:
  enabled: true
  patterns:
    - email
    - phone
    - ssn
  fields:
    - authorization
    - api_key
    - password

# Safety posture for the outside world. 'deny' is the default and should stay so.
execution:
  external_side_effects: deny
  side_effecting_tools: []
  mocks: {{}}

# Dimension-level release gates. Omit a dimension to stop gating on it.
gates:
  policy_compliance: "== 1.0"
  task_success: ">= 0.90"

# Compare against a stored baseline.
regression:
  baseline: .agentci/baseline.json
  max_quality_drop: 0.03
  max_cost_increase_pct: 25
  max_latency_increase_pct: 20
  on_regression: fail

selection:
  enabled: true
  unmatched: run

storage:
  dir: .agentci
  record_traces: true
  retention_days: 30

report:
  json: true
  markdown: true
  html: false

tests:
  - file: tests/agentci/test_{project_name.replace("-", "_")}.py
"""


def resolve_test_files(config: Config, root: Path | None = None) -> list[Path]:
    """Resolve config test entries to existing paths, erroring on missing files."""
    base = root or Path.cwd()
    resolved: list[Path] = []
    missing: list[str] = []
    for raw in config.test_files(base):
        candidate = Path(raw)
        full = candidate if candidate.is_absolute() else base / candidate
        if not full.exists():
            missing.append(raw)
            continue
        resolved.append(full)
    if missing:
        raise ConfigError(
            "test file(s) declared in agentci.yaml do not exist: " + ", ".join(missing),
            hint="run `agentci init` or fix the paths under `tests:`",
        )
    return resolved


def validate_paths(config: Config) -> Sequence[str]:
    """Return human-readable warnings about a config that will surprise someone."""
    warnings: list[str] = []
    if config.execution.external_side_effects == "allow":
        warnings.append(
            "execution.external_side_effects is 'allow': live destructive tool calls "
            "will execute during tests"
        )
    if config.redaction.enabled is False:
        warnings.append(
            "redaction.enabled is false: report and trace files may contain secrets"
        )
    if not config.tests:
        warnings.append("no tests are declared under `tests:`; `agentci test` will find nothing")
    if config.gates.active() and config.gates.policy_compliance is None:
        warnings.append(
            "gates.policy_compliance is not set; a policy violation could be hidden "
            "behind other dimensions (recommend '== 1.0')"
        )
    return warnings


__all__ = [
    "CONFIG_FILENAMES",
    "AgentConfig",
    "Budgets",
    "Config",
    "EvaluationConfig",
    "ExecutionConfig",
    "GateConfig",
    "MockSpec",
    "Policies",
    "RedactionConfig",
    "RegressionConfig",
    "ReportConfig",
    "SelectionConfig",
    "StorageConfig",
    "Threshold",
    "dump_config",
    "example_config",
    "find_config",
    "load_config",
    "resolve_test_files",
    "validate_paths",
]
