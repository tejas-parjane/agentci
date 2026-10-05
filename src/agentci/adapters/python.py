"""In-process adapter loading and invocation.

Handles all three adapter shapes from :mod:`agentci.adapters.base`, including
async adapters and generators, and normalizes every outcome into
:class:`~agentci.core.result.AgentResult` plus the events collected during the
call.

Loading ``"tests.adapter:run_agent"`` deliberately imports the *target user's*
module by file path relative to their project root, not the AgentCI package. That
keeps the user's code in their own import namespace so their relative imports,
settings, and logging keep working — a detail that decides whether an agent
author has to restructure their code to adopt AgentCI.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import sys
import threading
from collections.abc import Callable
from collections.abc import Callable as CType
from pathlib import Path
from typing import Any

from agentci.core.context import RunContext
from agentci.core.result import AgentResult
from agentci.core.trace import TraceEvent, new_id
from agentci.errors import AdapterError, DeadlineExceeded, StepLimitExceeded


class LoadedAdapter:
    """A resolved adapter, plus enough metadata to explain a load failure."""

    __slots__ = ("is_async", "is_generator", "name", "options", "takes_ctx", "target", "tools")

    def __init__(
        self,
        target: CType[..., Any],
        *,
        name: str,
        options: dict[str, Any] | None = None,
        tools: dict[str, Any] | None = None,
    ) -> None:
        self.target = target
        self.name = name
        self.options = options or {}
        self.tools = tools or {}
        self.is_async = inspect.iscoroutinefunction(target) or inspect.isasyncgenfunction(target)
        self.is_generator = inspect.isgeneratorfunction(target) or inspect.isasyncgenfunction(target)
        self.takes_ctx = _accepts_ctx(target)

    def __call__(self, user_input: str, ctx: RunContext) -> Any:
        return self.target(user_input, ctx) if self.takes_ctx else self.target(user_input)

    def __repr__(self) -> str:
        kind = "async" if self.is_async else "sync"
        return f"LoadedAdapter({self.name!r}, {kind}, takes_ctx={self.takes_ctx})"


def _accepts_ctx(fn: Callable[..., Any]) -> bool:
    """True when the callable's second positional parameter is the RunContext."""
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return False
    positional = [
        p
        for p in params
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        and p.name != "self"
    ]
    if len(positional) >= 2:
        return True
    return any(
        p.kind is p.KEYWORD_ONLY and p.name in ("ctx", "context", "run_context")
        for p in params
    )


def import_target(reference: str, *, project_root: Path | None = None) -> Any:
    """Resolve ``module.path:attribute`` to an object.

    Module paths are resolved against ``project_root`` on ``sys.path`` and,
    failing that, by file location. This is what lets a user's adapter import its
    own sibling modules.
    """
    if ":" not in reference:
        raise AdapterError(
            f"adapter reference {reference!r} must be 'module.path:attribute'"
        )
    module_path, _, attribute = reference.partition(":")
    root = Path(project_root or Path.cwd())

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError:
        module = _import_from_path(module_path, root)

    target: Any = module
    for part in attribute.split("."):
        try:
            target = getattr(target, part)
        except AttributeError as exc:
            available = [n for n in dir(module) if not n.startswith("_")]
            raise AdapterError(
                f"{module_path} has no attribute {attribute!r}",
                hint=f"available names: {', '.join(sorted(available)[:20])}",
            ) from exc
    return target


def _import_from_path(module_path: str, root: Path) -> Any:
    """Load a module by walking the project tree for ``<module_path>.py``."""
    relative = Path(*module_path.split("."))
    candidates = [root / relative.with_suffix(".py"), root / relative / "__init__.py"]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        spec = importlib.util.spec_from_file_location(module_path, candidate)
        if spec is None or spec.loader is None:  # pragma: no cover
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_path] = module
        spec.loader.exec_module(module)
        return module
    raise AdapterError(
        f"could not import module {module_path!r} from {root}",
        hint="check the path in agentci.yaml, and that your tests/ directory is importable",
    )


def load_adapter(
    reference: str,
    *,
    options: dict[str, Any] | None = None,
    default_name: str | None = None,
    project_root: Path | None = None,
) -> LoadedAdapter:
    """Load and wrap the adapter named by ``reference``."""
    target = import_target(reference, project_root=project_root)

    if inspect.isclass(target):
        instance = _instantiate(target, options or {})
        run = getattr(instance, "run", None)
        if run is None:
            raise AdapterError(f"{reference} is a class with no run() method")
        declared = (
            instance.declared_tools()
            if hasattr(instance, "declared_tools")
            else getattr(instance, "tools", {}) or {}
        )
        return LoadedAdapter(
            run,
            name=getattr(instance, "name", None) or default_name or target.__name__,
            options=options or {},
            tools=_normalize_tools(declared),
        )

    if not callable(target):
        raise AdapterError(f"{reference} resolved to {type(target).__name__}, not a callable")

    return LoadedAdapter(
        target,
        name=str(default_name or getattr(target, "__name__", "agent")),
        options=options or {},
        tools={},
    )


def _instantiate(cls: type[Any], options: dict[str, Any]) -> Any:
    try:
        return cls(**options) if options else cls()
    except TypeError as exc:
        raise AdapterError(
            f"could not construct adapter {cls.__name__}: {exc}",
            hint="check agent.options in agentci.yaml against the adapter's __init__",
        ) from exc


def _normalize_tools(raw: Any) -> dict[str, Any]:
    from agentci.core.tools import ToolDecl

    if not raw:
        return {}
    if isinstance(raw, dict):
        return {
            name: value if isinstance(value, ToolDecl) else ToolDecl(name=name, live=value)
            for name, value in raw.items()
        }
    if isinstance(raw, list):
        return {decl.name: decl for decl in raw}
    return {}


class InvocationResult:
    """Normalized outcome of one adapter call."""

    __slots__ = ("elapsed_ms", "error", "result", "streamed_events")

    def __init__(
        self,
        result: AgentResult,
        streamed_events: list[TraceEvent],
        elapsed_ms: float,
        error: BaseException | None = None,
    ) -> None:
        self.result = result
        self.streamed_events = streamed_events
        self.elapsed_ms = elapsed_ms
        self.error = error


def invoke(
    adapter: LoadedAdapter,
    user_input: str,
    ctx: RunContext,
    *,
    timeout_ms: float | None = None,
) -> InvocationResult:
    """Call the adapter and normalize the outcome.

    Handles four cases:

    * sync callable
    * async callable (run in a fresh event loop, so one test's loop never leaks
      into the next)
    * generator, whose yields are appended to the trace as they are produced
    * timeout / step-limit / policy exceptions, captured rather than propagated

    A sync call is executed on a worker thread so a timeout is observable. Python
    cannot kill a thread, so a wedged adapter keeps its thread as a daemon; the
    loop and step guards catch the common runaway cases cooperatively, and this
    covers the rest at the cost of a leaked thread. This limitation is documented
    rather than hidden.
    """
    loop_start = _now_ms()

    try:
        if adapter.is_async:
            outcome = asyncio.run(_call_async(adapter, user_input, ctx))
        elif adapter.is_generator:
            outcome = _consume_sync_generator(adapter, user_input, ctx)
        else:
            outcome = _call_sync(adapter, user_input, ctx, timeout_ms)
    except BaseException as exc:
        return InvocationResult(
            AgentResult(output_text=""), [], _now_ms() - loop_start, error=exc
        )

    result, streamed = _split(outcome)
    return InvocationResult(result, streamed, _now_ms() - loop_start)


async def _call_async(adapter: LoadedAdapter, user_input: str, ctx: RunContext) -> Any:
    raw = adapter(user_input, ctx)
    if hasattr(raw, "__aiter__"):
        events: list[TraceEvent] = []
        final: AgentResult | None = None
        async for item in raw:
            if isinstance(item, AgentResult):
                final = item
            else:
                events.append(_coerce_event(item, ctx.run_id))
        return (final or AgentResult(output_text=""), events)
    return (await raw, [])


def _call_sync(
    adapter: LoadedAdapter, user_input: str, ctx: RunContext, timeout_ms: float | None
) -> Any:
    """Run a sync adapter, enforcing ``timeout_ms`` if one is configured.

    Implemented with a **daemon** thread and an ``Event`` rather than
    ``ThreadPoolExecutor``. A pool's ``with`` block calls ``shutdown(wait=True)``,
    which blocks on the very worker that timed out -- so the timeout would not
    actually return until the adapter finished on its own, silently converting a
    hang into a hang-plus-a-message. A daemon thread can also be abandoned: if the
    adapter never returns, the interpreter still exits, and the abandoned thread's
    events land in a recorder this run no longer reads.
    """
    if timeout_ms is None:
        return (adapter(user_input, ctx), [])

    done = threading.Event()
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = adapter(user_input, ctx)
        except BaseException as exc:
            box["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=target, name="agentci-adapter", daemon=True)
    worker.start()
    if not done.wait(timeout=timeout_ms / 1000.0):
        raise DeadlineExceeded(timeout_ms)

    error = box.get("error")
    if error is not None:
        raise error
    return (box.get("value"), [])


def _consume_sync_generator(
    adapter: LoadedAdapter, user_input: str, ctx: RunContext
) -> tuple[AgentResult, list[TraceEvent]]:
    events: list[TraceEvent] = []
    final: AgentResult | None = None
    for item in adapter(user_input, ctx):
        if isinstance(item, AgentResult):
            final = item
        else:
            events.append(_coerce_event(item, ctx.run_id))
    return (final or AgentResult(output_text=""), events)


def _split(outcome: Any) -> tuple[AgentResult, list[TraceEvent]]:
    if isinstance(outcome, InvocationResult):  # pragma: no cover - defensive
        return outcome.result, outcome.streamed_events
    if isinstance(outcome, tuple) and len(outcome) == 2:
        result, events = outcome
        return _as_result(result), list(events)
    return _as_result(outcome), []


def _as_result(value: Any) -> AgentResult:
    if isinstance(value, AgentResult):
        return value
    if isinstance(value, str):
        return AgentResult(output_text=value)
    if isinstance(value, dict):
        try:
            return AgentResult.model_validate(value)
        except Exception:
            return AgentResult(output_text=str(value))
    if value is None:
        return AgentResult(output_text="")
    if hasattr(value, "output_text"):
        # Duck-typed result from a framework-neutral adapter.
        return AgentResult(
            output_text=str(getattr(value, "output_text", "")),
            trace=list(getattr(value, "trace", []) or []),
            metadata=dict(getattr(value, "metadata", {}) or {}),
        )
    return AgentResult(output_text=str(value))


def _coerce_event(item: Any, run_id: str) -> TraceEvent:
    if isinstance(item, TraceEvent):
        return item
    if isinstance(item, dict):
        data = dict(item)
        data.setdefault("run_id", run_id)
        data.setdefault("event_id", new_id("evt"))
        try:
            return TraceEvent.model_validate(data)
        except Exception as exc:
            raise AdapterError(
                f"adapter yielded a malformed trace event: {exc}",
                hint="use agentci.core.trace.TraceEvent to construct streamed events",
            ) from exc
    raise AdapterError(
        f"adapter yielded {type(item).__name__}, expected TraceEvent or AgentResult"
    )


def _now_ms() -> float:
    return _clock() * 1000.0


def _clock() -> float:
    import time

    return time.perf_counter()


def classify_error(exc: BaseException) -> str:
    """Map an exception to the failure category stored in reports (PRD §32)."""
    if isinstance(exc, StepLimitExceeded):
        return "step_limit"
    if isinstance(exc, DeadlineExceeded):
        return "timeout"
    if isinstance(exc, AdapterError):
        return "adapter_error"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(exc, AssertionError):
        # A failed ``assert`` in a test body: a quality finding, not a broken
        # harness. Reported as a category so ``Flakiness`` can distinguish a
        # genuinely flaky assertion from consistently failing behaviour.
        return "assertion_failed"
    if isinstance(exc, (ConnectionError, OSError)):
        return "infra_error"
    return "agent_error"


def interrupt_all() -> None:  # pragma: no cover - reserved for KeyboardInterrupt paths
    """Best-effort cleanup hook for shutdown."""
    threading.current_thread().is_alive()


__all__ = [
    "InvocationResult",
    "LoadedAdapter",
    "classify_error",
    "import_target",
    "invoke",
    "load_adapter",
]
