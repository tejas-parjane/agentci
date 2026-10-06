"""Report schema and its renderers.

``models`` is the contract every consumer reads; ``renderers`` turns it into the
four formats CI systems consume. Keeping them in one package is what stops the
formats from drifting: they all read the same :class:`RunReport`.
"""

from agentci.reporting.models import (
    AssertionReport,
    DimensionsReport,
    EnvironmentReport,
    Flakiness,
    GateResult,
    IterationReport,
    PlatformInfo,
    RegressionSection,
    RunIdentity,
    RunReport,
    RunSummary,
    TestReport,
    TraceExcerpt,
)
from agentci.reporting.renderers import (
    redact_report,
    render_annotations,
    render_json,
    render_markdown,
    render_step_summary,
    status_text,
)

__all__ = [
    "AssertionReport",
    "DimensionsReport",
    "EnvironmentReport",
    "Flakiness",
    "GateResult",
    "IterationReport",
    "PlatformInfo",
    "RegressionSection",
    "RunIdentity",
    "RunReport",
    "RunSummary",
    "TestReport",
    "TraceExcerpt",
    "redact_report",
    "render_annotations",
    "render_json",
    "render_markdown",
    "render_step_summary",
    "status_text",
]
