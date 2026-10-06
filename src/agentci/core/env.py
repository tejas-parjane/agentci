"""Execution-environment introspection.

Two jobs: stamp runs with where they happened, and read which files a pull request
touched. Both shell out to ``git`` and both degrade to ``None`` rather than
raising — AgentCI must still work in a directory that is not a repository, or in a
tarball.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

GIT_TIMEOUT_S = 10.0


def _run_git(*args: str, cwd: Path | None = None) -> tuple[bool, str]:
    """Run a git command. ``(False, "")`` on failure, ``(True, stdout)`` on success.

    Split from :func:`_git` because a *successful* command with empty output and a
    command that failed are indistinguishable once collapsed to ``None``, and
    change-aware selection has to tell "no files changed" from "git did not run".
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            cwd=str(cwd) if cwd else None,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    if completed.returncode != 0:
        return False, ""
    return True, completed.stdout.strip()


def _git(*args: str, cwd: Path | None = None) -> str | None:
    """Run a git command, returning stripped stdout or ``None`` on any failure."""
    ok, value = _run_git(*args, cwd=cwd)
    return value if ok and value else None


@dataclass(frozen=True, slots=True)
class GitContext:
    commit: str | None = None
    branch: str | None = None
    dirty: bool | None = None
    is_repo: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "git_commit": self.commit,
            "git_branch": self.branch,
            "git_dirty": self.dirty,
        }


@lru_cache(maxsize=1)
def git_context(root: Path | None = None) -> GitContext:
    """Commit, branch, and dirty state. Cached: it cannot change mid-run."""
    root_path = root or Path.cwd()
    inside = _git("rev-parse", "--is-inside-work-tree", cwd=root_path)
    if inside != "true":
        return GitContext(is_repo=False)
    return GitContext(
        commit=_git("rev-parse", "HEAD", cwd=root_path),
        branch=_git("rev-parse", "--abbrev-ref", "HEAD", cwd=root_path),
        dirty=bool(_git("status", "--porcelain", cwd=root_path)),
        is_repo=True,
    )


def default_base_ref(cwd: Path | None = None) -> str | None:
    """The ref to diff against, inferred from the CI environment.

    On GitHub Actions the merge-base ref is authoritative; locally ``origin/main``
    is a reasonable guess. An explicit ``--base`` always wins.

    ``cwd`` selects the repository to probe for ``origin/<branch>``; without it the
    search runs against the process working directory, which is not necessarily the
    project being tested.
    """
    for env_var in ("AGENTCI_BASE", "GITHUB_BASE_REF"):
        value = os.environ.get(env_var)
        if value:
            return f"origin/{value}" if not value.startswith(("origin/", "refs/")) else value
    for branch in ("main", "master"):
        if _git("rev-parse", "--verify", f"origin/{branch}", cwd=cwd):
            return f"origin/{branch}"
    return None


def changed_files(base: str | None = None, *, cwd: Path | None = None, head: str = "HEAD") -> list[str] | None:
    """Files changed since ``base``, as repo-relative POSIX paths.

    The left-hand side is the merge base of ``base`` and ``head``, so a feature
    branch's changes are measured from where it diverged rather than from a
    ``main`` that has moved on. The right-hand side is the working tree, so
    uncommitted edits count as changes too — on a CI checkout the two are
    identical, but locally ``--changed`` must not report "nothing changed" while
    the developer is mid-edit. Untracked, non-ignored files are included for the
    same reason.

    ``None`` means *the diff could not be determined*: no base ref, or git failed.
    ``[]`` means the diff succeeded and nothing changed. Callers that decide which
    tests to skip must not treat ``None`` as ``[]`` — that would silently reduce
    coverage to zero.
    """
    root = cwd or Path.cwd()
    target = base or default_base_ref(cwd=root)
    if target is None:
        return None

    ok, merge_base = _run_git("merge-base", target, head, cwd=root)
    ref = merge_base if ok and merge_base else target

    ok, output = _run_git("diff", "--name-only", ref, cwd=root)
    if not ok:
        return None
    ok, untracked = _run_git("ls-files", "--others", "--exclude-standard", cwd=root)
    if not ok:
        return None

    seen: dict[str, None] = {}
    for line in (*output.splitlines(), *untracked.splitlines()):
        if line.strip():
            seen[line.strip()] = None
    return list(seen)


def is_ci() -> bool:
    return os.environ.get("CI", "").lower() in ("true", "1") or os.environ.get(
        "GITHUB_ACTIONS"
    ) == "true"


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


def git_safe_env() -> dict[str, str]:
    """Environment for child git processes, without credential helpers prompting."""
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GCM_INTERACTIVE", "never")
    return env


__all__ = [
    "GitContext",
    "changed_files",
    "ci_provider",
    "default_base_ref",
    "git_context",
    "is_ci",
]
