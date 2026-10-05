"""Single source of truth for the distribution version.

Kept in its own module so that ``pyproject.toml`` can read it via
``[tool.hatch.version]`` without importing the package.
"""

from __future__ import annotations

__version__ = "0.1.0"

#: Version of the JSON report schema emitted by the reporting layer.
#: Bumped independently of ``__version__`` when the report shape changes.
REPORT_SCHEMA_VERSION = 1

#: Version of the on-disk trace (JSONL) format.
TRACE_SCHEMA_VERSION = 1

#: Version of the baseline file format.
BASELINE_SCHEMA_VERSION = 1

#: Highest ``version:`` value accepted in ``agentci.yaml``.
#: A breaking configuration change requires incrementing this.
CONFIG_VERSION = 1
