"""Test authoring: the ``@agent_test`` decorator, the registry, and discovery.

The authoring API (PRD §13) is deliberately small::

    from agentci import agent_test, expect

    @agent_test(name="refund request", tags=["refund", "critical"],
                dependencies=["prompts/refund.txt", "tools/refund.py"])
    def test_refund(agent):
        result = agent.run("I want a refund for order 123")
        expect(result).to_use_tool("get_order")
        expect(result).to_have_max_cost(0.05)

Parameters are injected **by name**, so a test body reads as prose and needs no
fixture plumbing:

``agent``
    The :class:`AgentHandle` for this invocation.
``ctx``
    The raw :class:`~agentci.core.context.RunContext`, for tests that want to
    assert on recorder state directly.
``config``
    The loaded project configuration.

Discovery is import-based, not AST-based. Test modules are executed and the
module-level registry is read, which means a test file can build its cases
dynamically from a fixture file and AgentCI still finds them. The cost is that
importing a test file runs its module-level code; that is the same contract
pytest users already accept.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentci.errors import DiscoveryError

#: module name -> registered cases. Keyed by module so two test files that both
#: define ``test_refund`` never collide.
_REGISTRY: dict[str, list[AgentTestCase]] = {}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class AgentTestCase:
    """A discovered test."""

    fn: Callable[..., Any]
    name: str
    test_id: str
    tags: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    repeat: int | None = None
    minimum_pass_rate: float | None = None
    file: str = ""
    line: int = 0
    module: str = ""
    is_async: bool = False

    @property
    def declared_dependencies(self) -> bool:
        """Whether the author declared dependencies (drives change-aware selection)."""
        return bool(self.dependencies)

    def injected_params(self) -> set[str]:
        try:
            params = inspect.signature(self.fn).parameters
        except (TypeError, ValueError):  # pragma: no cover
            return set()
        return {
            name
            for name in params
            if name in ("agent", "ctx", "context", "run_context", "config")
        }

    def __call__(self, **injected: Any) -> Any:
        """Invoke with only the parameters the function actually declares."""
        wanted = self.injected_params()
        kwargs = {k: v for k, v in injected.items() if k in wanted}
        return self.fn(**kwargs)


def _slugify(value: str) -> str:
    return _SLUG_RE.sub("_", value.strip().lower()).strip("_") or "test"


def agent_test(
    name: str | None = None,
    *,
    tags: Sequence[str] = (),
    dependencies: Sequence[str] = (),
    repeat: int | None = None,
    minimum_pass_rate: float | None = None,
    test_id: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a function as an AgentCI test.

    :param name: human-readable test name; defaults to ``test_<x>`` -> ``<x>``
    :param tags: labels for ``--tag`` filtering
    :param dependencies: file paths or globs whose change should trigger this test
        (PRD FR-6). A test with no dependencies is treated as affected by any
        change, unless ``selection.unmatched: skip`` is configured.
    :param repeat: override ``evaluation.repeat`` for this test
    :param minimum_pass_rate: override ``evaluation.minimum_pass_rate``
    :param test_id: stable identifier for ``--test`` and baseline matching. Named
        ``test_id`` rather than ``id`` to avoid shadowing the builtin.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        module = getattr(fn, "__module__", "__main__")
        resolved_name = name or _humanize(getattr(fn, "__name__", "test"))
        case = AgentTestCase(
            fn=fn,
            name=resolved_name,
            test_id=test_id or _slugify(f"{module}.{fn.__name__}"),
            tags=list(tags),
            dependencies=list(dependencies),
            repeat=repeat,
            minimum_pass_rate=minimum_pass_rate,
            file=getattr(getattr(fn, "__code__", None), "co_filename", ""),
            line=(
                fn.__code__.co_firstlineno
                if hasattr(fn, "__code__")
                else inspect.getsourcelines(fn)[1]
            ),
            module=module,
            is_async=inspect.iscoroutinefunction(fn),
        )

        _REGISTRY.setdefault(module, []).append(case)
        # Attach for introspection without changing the call signature.
        fn.__agentci_test__ = case  # type: ignore[attr-defined]
        return fn

    return decorator


def _humanize(function_name: str) -> str:
    stem = function_name[5:] if function_name.startswith("test_") else function_name
    return stem.replace("_", " ").strip() or function_name


def registered(module: str) -> list[AgentTestCase]:
    """Cases registered by one module."""
    return list(_REGISTRY.get(module, ()))


def all_registered() -> list[AgentTestCase]:
    return [case for cases in _REGISTRY.values() for case in cases]


def clear_registry() -> None:
    """Drop every registered case. Used by tests to avoid cross-test leakage."""
    _REGISTRY.clear()


def discover(
    files: Sequence[Path | str],
    *,
    project_root: Path | None = None,
) -> list[AgentTestCase]:
    """Import each test file and return the cases it registered.

    Files are imported under a unique synthetic module name derived from their
    path, so two files that both contain ``test_refund`` are independent and a
    re-import (across repeated runs in one process) does not silently reuse a
    stale module.
    """
    root = Path(project_root or Path.cwd()).resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    cases: list[AgentTestCase] = []
    for raw in files:
        path = Path(raw)
        full = path if path.is_absolute() else root / path
        full = full.resolve()
        if not full.is_file():
            raise DiscoveryError(
                f"test file not found: {raw}",
                hint="check the `tests:` list in agentci.yaml",
            )
        module_name = _module_name_for(full, root)
        _import_file(module_name, full, root)
        found = registered(module_name)
        if not found:
            raise DiscoveryError(
                f"{full.name} contains no @agent_test",
                hint=(
                    "decorate at least one function with @agent_test(...) and make sure the "
                    "module is importable (no relative imports that fail standalone)"
                ),
            )
        for case in found:
            case.file = _display_path(full, root)
        cases.extend(found)
    return cases


def _module_name_for(path: Path, root: Path) -> str:
    """Derive a stable, unique module name from a file path."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    parts = list(relative.parts)
    parts[-1] = parts[-1].removesuffix(".py")
    # Insert a disambiguating token so re-imports do not collide in sys.modules.
    return "_agentci_" + "_".join(parts)


def _import_file(module_name: str, path: Path, root: Path) -> Any:
    existing = sys.modules.get(module_name)
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise DiscoveryError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    # Give the module a usable __package__ so its own relative imports resolve.
    module.__package__ = root.name if str(root) in sys.path else ""
    try:
        spec.loader.exec_module(module)
    except DiscoveryError:
        raise
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise DiscoveryError(
            f"importing {path.name} failed: {type(exc).__name__}: {exc}",
            hint="test modules are executed at discovery time; check for import-time errors",
        ) from exc
    return module


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover
        return path.as_posix()


def iter_cases(cases: Sequence[AgentTestCase]) -> Iterator[AgentTestCase]:  # pragma: no cover
    yield from cases


__all__ = [
    "AgentTestCase",
    "agent_test",
    "all_registered",
    "clear_registry",
    "discover",
    "registered",
]
