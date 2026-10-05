"""On-disk run artifacts (ADR-002: JSONL traces).

Layout, rooted at ``storage.dir`` (default ``.agentci``)::

    .agentci/
      runs/<run_id>/trace.jsonl     append-friendly, git-friendly, line-oriented
      runs/<run_id>/result.json     the AgentResult for that invocation
      reports/<run_id>/report.json  machine-readable report
      reports/<run_id>/report.md    human-readable report
      reports/latest/report.json    stable path for CI artifact globs
      baseline.json                 saved via `agentci baseline save`

Two properties are load-bearing:

* **JSONL for traces.** One event per line means a partially written file is
  still parseable up to the last complete line, so an interrupted CI run does not
  leave an unreadable trace.
* **Path safety.** Every write resolves and verifies containment inside the storage
  root, so a crafted ``run_id`` from a report or a ``--output`` flag cannot write
  outside it (PRD §34, path traversal).
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentci.core.config import StorageConfig
from agentci.core.redaction import Redactor
from agentci.core.result import AgentResult
from agentci.core.trace import TraceEvent
from agentci.errors import AgentCIError

TRACE_FILENAME = "trace.jsonl"
RESULT_FILENAME = "result.json"


class StorageError(AgentCIError):
    pass


def safe_join(root: Path, *parts: str) -> Path:
    """Join under ``root``, refusing anything that escapes it.

    Guards three things at once: absolute components, ``..`` traversal, and
    symlinks pointing outside the root (resolved after construction).
    """
    candidate = root.joinpath(*parts)
    resolved_root = root.resolve()
    try:
        resolved = candidate.resolve()
    except OSError as exc:  # pragma: no cover - unusual FS state
        raise StorageError(f"cannot resolve path {candidate}: {exc}") from exc

    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise StorageError(
            f"refusing to write outside the storage root: {candidate}",
            hint="run ids and output paths must stay inside .agentci/",
        )
    return candidate


@dataclass
class StoredRun:
    """Where a persisted run landed."""

    run_id: str
    directory: Path
    trace_path: Path | None = None
    result_path: Path | None = None


class RunStore:
    """Persists traces and results, with redaction applied on the way out."""

    __slots__ = ("_warned_over_limit", "config", "redactor", "root")

    def __init__(
        self,
        config: StorageConfig | None = None,
        *,
        root: Path | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.config = config or StorageConfig()
        self.root = Path(root or self.config.dir)
        self.redactor = redactor or Redactor()
        self._warned_over_limit = False

    # -- paths ----------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        _validate_run_id(run_id)
        return safe_join(self.root, "runs", run_id)

    def report_dir(self, run_id: str) -> Path:
        _validate_run_id(run_id)
        return safe_join(self.root, "reports", run_id)

    @property
    def latest_report_dir(self) -> Path:
        return self.root / "reports" / "latest"

    def ensure(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- traces ---------------------------------------------------------------

    def save_trace(self, run_id: str, events: list[TraceEvent]) -> StoredRun | None:
        """Write a trace as JSONL, redacted."""
        if not self.config.record_traces:
            return None
        directory = self.ensure(self.run_dir(run_id))
        trace_path = directory / TRACE_FILENAME
        limit = self.config.max_trace_events

        redacted = self.redactor.redact_events(events)
        with trace_path.open("w", encoding="utf-8", newline="\n") as handle:
            for event in redacted[:limit]:
                handle.write(json.dumps(event.to_dict(), ensure_ascii=False, default=str) + "\n")
        return StoredRun(run_id=run_id, directory=directory, trace_path=trace_path)

    def load_trace(self, run_id: str) -> list[TraceEvent]:
        """Read a trace back, tolerating a truncated final line."""
        path = self.run_dir(run_id) / TRACE_FILENAME
        if not path.is_file():
            return []
        events: list[TraceEvent] = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                    events.append(TraceEvent.model_validate(data))
                except (json.JSONDecodeError, ValueError):
                    # A partial last line means the run was interrupted; keep
                    # everything up to that point rather than failing the replay.
                    break
        return events

    def save_result(self, run_id: str, result: AgentResult) -> StoredRun | None:
        if not self.config.record_traces:
            return None
        directory = self.ensure(self.run_dir(run_id))
        path = directory / RESULT_FILENAME
        payload = {
            "run_id": run_id,
            "output_text": self.redactor.redact_text(result.output_text),
            "metadata": self.redactor.redact(result.metadata),
            "trace_ref": TRACE_FILENAME,
        }
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        return StoredRun(run_id=run_id, directory=directory, result_path=path)

    def load_result(self, run_id: str) -> dict[str, Any]:
        path = self.run_dir(run_id) / RESULT_FILENAME
        if not path.is_file():
            return {}
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data

    # -- run inventory --------------------------------------------------------

    def list_runs(self) -> list[str]:
        """Run ids on disk, newest first."""
        runs_root = self.root / "runs"
        if not runs_root.is_dir():
            return []
        ids: list[tuple[float, str]] = []
        for child in runs_root.iterdir():
            if not child.is_dir():
                continue
            try:
                ids.append((child.stat().st_mtime, child.name))
            except OSError:  # pragma: no cover
                continue
        return [run_id for _, run_id in sorted(ids, reverse=True)]

    def prune(self) -> list[str]:
        """Delete runs older than ``retention_days``. Returns what was removed."""
        days = self.config.retention_days
        if days <= 0:
            return []
        import time

        cutoff = time.time() - days * 86400
        removed: list[str] = []
        for run_id in self.list_runs():
            directory = self.run_dir(run_id)
            try:
                if directory.stat().st_mtime < cutoff:
                    shutil.rmtree(directory)
                    removed.append(run_id)
            except OSError:  # pragma: no cover
                continue
        return removed

    # -- reports --------------------------------------------------------------

    def save_report(self, run_id: str, report_json: str, report_md: str | None = None) -> list[Path]:
        """Write report artifacts, including the stable ``latest`` copy."""
        written: list[Path] = []
        directory = self.ensure(self.report_dir(run_id))
        json_path = directory / "report.json"
        json_path.write_text(report_json, encoding="utf-8")
        written.append(json_path)

        if report_md is not None:
            md_path = directory / "report.md"
            md_path.write_text(report_md, encoding="utf-8")
            written.append(md_path)

        latest = self.ensure(self.latest_report_dir)
        for name, content in (("report.json", report_json), ("report.md", report_md)):
            if content is None:
                continue
            (latest / name).write_text(content, encoding="utf-8")
            written.append(latest / name)
        return written


def _validate_run_id(run_id: str) -> None:
    """Run ids are used as path components, so they must be plain identifiers."""
    if not run_id:
        raise StorageError("run id must not be empty")
    if len(run_id) > 128:
        raise StorageError("run id is too long")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    if not set(run_id) <= allowed:
        raise StorageError(
            f"run id {run_id!r} contains characters that are not safe in a path",
            hint="run ids look like run_a1b2c3d4e5f6",
        )


def in_ci() -> bool:
    return os.environ.get("GITHUB_ACTIONS", "").lower() == "true"


def ci_provider() -> str | None:
    for name, env in (
        ("github", "GITHUB_ACTIONS"),
        ("gitlab", "GITLAB_CI"),
        ("circleci", "CIRCLECI"),
        ("azure", "TF_BUILD"),
        ("buildkite", "BUILDKITE"),
        ("jenkins", "JENKINS_URL"),
    ):
        if os.environ.get(env):
            return name
    return None


__all__ = [
    "RESULT_FILENAME",
    "TRACE_FILENAME",
    "RunStore",
    "StorageError",
    "StoredRun",
    "ci_provider",
    "in_ci",
    "safe_join",
]
