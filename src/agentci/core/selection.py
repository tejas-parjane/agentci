"""Change-aware test selection (PRD §FR-6).

The question a release gate has to answer is not "do the tests pass?" but "do the
tests *that this change could affect* pass?". A test declares the files it depends
on::

    @agent_test(dependencies=["prompts/refund.txt", "tools/*.py"])
    def test_refund(agent): ...

and selection compares those globs against the files a branch actually touched.

Three rules make this safe rather than merely fast:

**A test with no declared dependency runs.** It cannot be proven unaffected, so it
is treated as affected by everything — unless ``selection.unmatched: skip`` is
explicitly opted into.

**A base that cannot be resolved runs everything.** ``None`` from
:func:`~agentci.core.env.changed_files` means *unknown*, not *unchanged*. Collapsing
the two would turn a broken git checkout into a green run over an empty suite, which
is the worst possible failure mode for a release gate.

**A change to a global path runs everything.** Editing ``agentci.yaml`` changes the
harness itself, so no test can claim independence from it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from agentci.core.config import Config
from agentci.core.env import changed_files, default_base_ref
from agentci.testing import AgentTestCase

_MAGIC = "*?"


@lru_cache(maxsize=256)
def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a POSIX glob into an anchored regex.

    Hand-written rather than borrowed: :mod:`fnmatch` lets ``*`` cross ``/``, so
    ``tools/*.py`` would match ``tools/a/b.py``, and :meth:`pathlib.PurePath.match`
    only gained real ``**`` semantics in Python 3.13 while AgentCI targets 3.11.

    ``**`` spans directories (including zero of them, so ``a/**/b`` matches
    ``a/b``); ``*`` and ``?`` stay within one path segment.
    """
    parts: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**", index):
                parts.append(".*")
                index += 2
                if index < length and pattern[index] == "/":
                    parts.append("/?")
                    index += 1
                continue
            parts.append("[^/]*")
            index += 1
        elif char == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(char))
            index += 1
    return re.compile("".join(parts))


def path_matches(path: str, pattern: str) -> bool:
    """Whether a repo-relative POSIX ``path`` satisfies ``pattern``.

    A pattern without glob characters also matches everything *underneath* it, so
    ``dependencies: [examples/support_agent]`` means what it reads as.
    """
    if _glob_to_regex(pattern).fullmatch(path):
        return True
    return not any(char in pattern for char in _MAGIC) and path.startswith(
        pattern.rstrip("/") + "/"
    )


def path_matches_any(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    """Whether ``path`` satisfies at least one of ``patterns``."""
    return any(path_matches(path, pattern) for pattern in patterns)


@dataclass(frozen=True, slots=True)
class SelectionPlan:
    """What the diff said, and what that implies for each test.

    ``unresolved`` is the field that keeps this honest: when it is set, no test may
    be skipped on its account. ``build_plan`` fills it in rather than returning
    fewer changed files, so the decision stays visible all the way into
    ``report.warnings``.
    """

    enabled: bool = False
    base: str | None = None
    changed: tuple[str, ...] = ()
    global_hit: bool = False
    unmatched: Literal["run", "skip"] = "run"
    unresolved: str = ""

    @property
    def active(self) -> bool:
        """True only when the diff is known well enough to skip anything."""
        return self.enabled and not self.unresolved

    def evaluate(self, case: AgentTestCase) -> tuple[bool, str]:
        """Whether ``case`` should run, plus the reason to record if it does not.

        The second element is the skip reason; it is only consulted when the first
        is ``False``.
        """
        if not self.active:
            return True, ""
        if self.global_hit:
            return True, ""
        if not case.dependencies:
            if self.unmatched == "run":
                return True, ""
            return False, (
                "change-aware selection: declares no dependencies, and "
                "selection.unmatched is 'skip'"
            )
        affected = [
            pattern
            for pattern in case.dependencies
            if any(path_matches(changed, pattern) for changed in self.changed)
        ]
        if affected:
            return True, ""
        if self.unmatched == "run":
            return True, ""
        return False, (
            f"change-aware selection: no change to "
            f"{', '.join(case.dependencies)} since {self.base}"
        )


def build_plan(
    config: Config,
    root: Path,
    *,
    base: str | None = None,
    head: str = "HEAD",
) -> SelectionPlan:
    """Decide which files count as changed, before any test is evaluated.

    Base precedence: an explicit ``base`` (the ``--base`` flag) wins, then whatever
    the environment says (``AGENTCI_BASE``, ``GITHUB_BASE_REF``, ``origin/main``),
    then ``selection.default_base``. If all three are empty the plan is left
    *unresolved* — every test runs, and a warning explains why.
    """
    selection = config.selection
    if not selection.enabled:
        return SelectionPlan(enabled=False, unmatched=selection.unmatched)

    resolved = base or default_base_ref(cwd=root) or selection.default_base
    if not resolved:
        return SelectionPlan(
            enabled=True,
            unmatched=selection.unmatched,
            unresolved=(
                "no base ref could be determined; pass --base, set AGENTCI_BASE, "
                "or configure selection.default_base"
            ),
        )

    changed = changed_files(resolved, cwd=root, head=head)
    if changed is None:
        return SelectionPlan(
            enabled=True,
            unmatched=selection.unmatched,
            base=resolved,
            unresolved=f"git could not diff {resolved}...{head}",
        )

    return SelectionPlan(
        enabled=True,
        base=resolved,
        changed=tuple(changed),
        global_hit=any(path_matches_any(path, selection.global_paths) for path in changed),
        unmatched=selection.unmatched,
    )


__all__ = [
    "SelectionPlan",
    "build_plan",
    "path_matches",
    "path_matches_any",
]
