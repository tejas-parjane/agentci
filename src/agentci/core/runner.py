"""The test runner: turns discovered cases into a :class:`RunReport`.

Per-test sequence, for one repetition:

1. Build a recorder, tool registry, and :class:`RunContext` carrying budget limits.
2. Activate a fresh assertion collector so ``expect()`` inside the body is captured.
3. Call the test body, injecting ``agent`` / ``ctx`` / ``config`` by name.
4. Merge recorder events with anything the adapter returned, then derive metrics.
5. Evaluate policy over the completed trace.
6. Apply **project budgets** and the always-on credential-leak scan — regardless of
   what the test body asserted.
7. Persist the trace, then fold everything into a :class:`TestReport`.

Step 6 is the load-bearing one for release gating. ``budgets.max_cost_usd`` is a
project-level commitment; if it only applied when a test author remembered to call
``to_have_max_cost``, a forgotten assertion would silently disable the gate.

Event merging deserves a note, because it is easy to get subtly wrong. An adapter
may emit events through ``ctx.recorder`` *and* return them in
``AgentResult.trace``. :meth:`TestRunner.build_view` keeps both, de-duplicated by
``event_id``, and — critically — only considers events recorded *after* the call
started, so a test body that invokes the agent twice gets two disjoint views
rather than each seeing both invocations.

Status vocabulary, per PRD §19 and §32:

===================  ===========================================================
``PASS``             every evaluated assertion passed, no policy violation
``FAIL``             an assertion failed, a policy was violated, or a hard budget
                     was exceeded
``ERROR``            infrastructure fault: the adapter raised, timed out, or the
                     harness broke. Never silently reported as ``PASS``.
``SKIP``             not selected, or not measurable
``WARN``             passed, but something drifted (used by regression)
===================  ===========================================================
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentci.adapters.python import (
    LoadedAdapter,
    classify_error,
    invoke,
    load_adapter,
)
from agentci.assertions import execution as budget_assertions
from agentci.assertions import leaks as leak_assertions
from agentci.assertions.base import AssertionCollector, AssertionFailed, Kind
from agentci.assertions.fluent import activate, deactivate
from agentci.core.config import Config
from agentci.core.context import RunContext
from agentci.core.cost import CostEstimator, build_cost_model
from agentci.core.env import ci_provider, git_context, is_ci
from agentci.core.recorder import TraceRecorder
from agentci.core.redaction import DEFAULT_PATTERNS, Redactor
from agentci.core.result import (
    AgentResult,
    AssertionResult,
    PolicyViolation,
    ResultView,
    RunMetrics,
    Status,
    derive_metrics,
)
from agentci.core.storage import RunStore
from agentci.core.trace import EventType, Trace, TraceEvent, new_id, utc_now
from agentci.errors import AgentCIError, DeadlineExceeded, StepLimitExceeded
from agentci.policy.engine import PolicyEngine, compliance
from agentci.reporting.models import (
    AssertionReport,
    DimensionsReport,
    EnvironmentReport,
    Flakiness,
    GateResult,
    IterationReport,
    PlatformInfo,
    RunIdentity,
    RunReport,
    RunSummary,
    TestReport,
    TraceExcerpt,
)
from agentci.testing import AgentTestCase

#: Events describing the invocation rather than the agent's reasoning. Always
#: attributed to the agent for policy purposes -- see ``_agent_scope``.
LIFECYCLE_EVENTS = frozenset(
    {EventType.RUN_STARTED, EventType.RUN_COMPLETED, EventType.ERROR}
)


class AgentHandle:
    """The ``agent`` object injected into a test body.

    One handle per repetition, bound to that repetition's
    :class:`~agentci.core.context.RunContext`. :meth:`run` executes the agent once
    and returns a fully populated
    :class:`~agentci.core.result.ResultView`, ready for ``expect()``.
    """

    __slots__ = ("_adapter", "_ctx", "_runner", "_views")

    def __init__(self, adapter: LoadedAdapter, ctx: RunContext, runner: TestRunner) -> None:
        self._adapter = adapter
        self._ctx = ctx
        self._runner = runner
        self._views: list[ResultView] = []

    @property
    def ctx(self) -> RunContext:
        return self._ctx

    @property
    def name(self) -> str:
        return self._adapter.name

    def run(self, user_input: str, **attributes: Any) -> ResultView:
        """Execute the agent once and return the result view.

        A fault is recorded on the view and then re-raised. Propagating it means a
        test body that writes ``expect(agent.run(...))`` sees a real, attributed
        error instead of an empty result producing a baffling assertion failure —
        and it keeps "infrastructure error" distinguishable from "quality failure"
        all the way into the report (§32).
        """
        for key, value in attributes.items():
            self._ctx.set(key, value)

        start_index = len(self._ctx.recorder.events)
        invocation = invoke(
            self._adapter,
            user_input,
            self._ctx,
            timeout_ms=self._runner.timeout_ms,
        )
        view = self._runner.build_view(invocation, self._ctx.recorder, start_index)
        self._views.append(view)
        if invocation.error is not None:
            raise invocation.error
        return view

    @property
    def views(self) -> list[ResultView]:
        """Every result view produced by this handle, in call order."""
        return list(self._views)

    @property
    def last(self) -> ResultView | None:
        return self._views[-1] if self._views else None


@dataclass
class RunOptions:
    """Knobs the CLI passes to the runner."""

    repeat: int | None = None
    tags: list[str] = field(default_factory=list)
    selected_by: str = "all"
    selection_reason: str = ""
    record_traces: bool | None = None
    fail_on_warn: bool = False
    root: Path | None = None


@dataclass
class _IterationOutcome:
    iteration: IterationReport
    assertions: list[AssertionResult]
    violations: list[PolicyViolation]
    view: ResultView | None = None
    trace: Trace | None = None


class TestRunner:
    """Executes discovered test cases against one adapter."""

    def __init__(
        self,
        config: Config,
        *,
        adapter: LoadedAdapter | None = None,
        redactor: Redactor | None = None,
        store: RunStore | None = None,
        options: RunOptions | None = None,
    ) -> None:
        self.config = config
        self.options = options or RunOptions()
        self.redactor = redactor or self._build_redactor(config)
        self.root = Path(self.options.root) if self.options.root else Path.cwd()
        # `storage.dir` is resolved *under* the project root. Passing the root as
        # the store's root instead would scatter `runs/` directories into whatever
        # directory happened to be the project, ignoring the configured location.
        self.store = store or RunStore(
            config.storage,
            root=self.root / config.storage.dir,
            redactor=self.redactor,
        )
        self.adapter = adapter or load_adapter(
            config.agent.adapter,
            options=dict(config.agent.options),
            default_name=config.agent_display_name,
            project_root=self.root,
        )
        self.costs = CostEstimator(build_cost_model(config.pricing))
        self.timeout_ms = config.budgets.max_latency_ms
        self.warnings: list[str] = []

    # -- construction helpers -------------------------------------------------

    @staticmethod
    def _build_redactor(config: Config) -> Redactor:
        """Assemble the redactor implied by ``redaction:``.

        Configured patterns are *additive* to a credential set that is always
        scrubbed, so listing only ``email`` cannot accidentally allow a JWT into a
        report.
        """
        if not config.redaction.enabled:
            return Redactor(enabled=False)
        base = Redactor.from_env() if config.redaction.scrub_environment else Redactor()
        wanted = list(config.redaction.patterns) or list(DEFAULT_PATTERNS)
        return Redactor(
            fields=config.redaction.fields,
            patterns=wanted,
            secrets=base.secrets,
        )

    # -- public API -----------------------------------------------------------

    def run(self, cases: Sequence[AgentTestCase]) -> RunReport:
        """Execute every case and build the report."""
        repeat = self.options.repeat or self.config.evaluation.repeat
        report = RunReport(
            run=RunIdentity(
                project=self.config.project.name,
                selected_by=self.options.selected_by,
                selection_reason=self.options.selection_reason,
                repeat=repeat,
                minimum_pass_rate=self.config.evaluation.minimum_pass_rate,
            ),
            environment=self._environment(),
            warnings=[],
        )
        report.run.run_ids = []

        for case in cases:
            if not self._matches_tags(case):
                report.tests.append(self._skipped(case, f"tag filter {self.options.tags}"))
                continue
            report.tests.append(self._run_case(case, repeat))
            for iteration in report.tests[-1].iterations:
                report.run.run_ids.append(iteration.run_id)

        report.run.finished_at = utc_now()
        report.policy_violations = self._collect_violations(report)
        report.summary = self._summarize(report)
        report.dimensions = self._dimensions(report)
        report.gates = self._absolute_gates(report)
        report.warnings = list(self.warnings)
        report.run.status = self._overall_status(report)
        return report

    # -- one test -------------------------------------------------------------

    def _run_case(self, case: AgentTestCase, repeat: int) -> TestReport:
        # A case-level ``repeat=`` overrides the project default, so a single
        # flaky-prone test can be hammered without slowing the whole suite down.
        effective_repeat = case.repeat or repeat
        iterations: list[IterationReport] = []
        assertions: list[AssertionResult] = []
        violations: list[PolicyViolation] = []
        last_view: ResultView | None = None
        last_trace: Trace | None = None
        error: str | None = None
        error_category: str | None = None

        for index in range(1, effective_repeat + 1):
            outcome = self._run_iteration(case, index)
            iterations.append(outcome.iteration)
            assertions.extend(outcome.assertions)
            violations.extend(outcome.violations)
            if outcome.view is not None:
                last_view = outcome.view
            if outcome.trace is not None:
                last_trace = outcome.trace
            # An ERROR outranks a FAIL: when some repetitions hit an infrastructure
            # fault and others merely failed an assertion, the report should lead
            # with the fault, because that is the thing a maintainer must triage.
            if outcome.iteration.status is Status.ERROR:
                error = outcome.iteration.error
                error_category = outcome.iteration.error_category
            elif outcome.iteration.status is Status.FAIL and error_category is None:
                error = outcome.iteration.error
                error_category = "assertion_failed"

        test = TestReport(
            test_id=case.test_id,
            name=case.name,
            file=case.file,
            line=case.line,
            tags=list(case.tags),
            dependencies=list(case.dependencies),
            iterations=iterations,
            assertions=[AssertionReport.from_result(a) for a in assertions],
            policy_violations=_dedupe_violations(violations),
            output_excerpt=last_view.repr_excerpt() if last_view else "",
        )
        test.status, test.flakiness = self._test_status(
            iterations, len(_blocking(violations)), case.minimum_pass_rate
        )
        if error and test.status is not Status.PASS:
            test.error = error
            test.error_category = error_category
        test.metrics = budget_assertions.describe_metrics(self._aggregate_metrics(test))
        full_trace = last_trace or (last_view.trace if last_view is not None else None)
        if full_trace is not None and full_trace.events:
            test.trace = TraceExcerpt.build(
                full_trace.events, self.config.report.trace_excerpt_events
            )
        return test

    def _run_iteration(self, case: AgentTestCase, iteration: int) -> _IterationOutcome:
        run_id = new_id("run")
        recorder = TraceRecorder(
            run_id,
            max_steps=self.config.budgets.max_steps,
            max_duration_ms=self.timeout_ms,
            component=self.adapter.name,
        )
        recorder.start(test=case.test_id, iteration=iteration, agent=self.adapter.name)

        engine = PolicyEngine.from_config(
            self.config, redactor=self.redactor, metrics=RunMetrics()
        )
        registry = engine.build_registry(self.adapter.tools, recorder=recorder)
        ctx = RunContext(
            run_id=run_id,
            recorder=recorder,
            tools=registry,
            agent_name=self.adapter.name,
            max_steps=self.config.budgets.max_steps,
            max_duration_ms=self.timeout_ms,
            iteration=iteration,
        )
        handle = AgentHandle(self.adapter, ctx, self)

        collector = AssertionCollector(standalone=False)
        token = activate(collector, self.redactor)
        body_error: BaseException | None = None
        try:
            outcome = case(agent=handle, ctx=ctx, config=self.config)
            if inspect.isawaitable(outcome):
                asyncio.run(_await(outcome))
        except BaseException as exc:
            # Includes KeyboardInterrupt/SystemExit: re-raising happens below only
            # after `finally` has deactivated. Deactivating in both places would
            # reset the same ContextVar token twice, which raises RuntimeError.
            body_error = exc
        finally:
            deactivate(token)

        if isinstance(body_error, (KeyboardInterrupt, SystemExit)):
            raise body_error

        if body_error is not None:
            self._record_failure(recorder, body_error)

        # Union of every event produced during this invocation: recorded live by
        # the adapter, returned in AgentResult.trace, or synthesized here.
        merged = self._merge_events(run_id, recorder, handle)
        trace = Trace(run_id=run_id, events=merged)
        metrics = derive_metrics(trace, latency_ms=recorder.elapsed_ms)

        violations = engine.evaluate(trace, scope=self._agent_scope(recorder, handle))
        for view in handle.views:
            view.policy_violations = list(violations)

        assertions = list(collector.results)
        assertions.extend(self._budget_assertions(metrics))
        assertions.extend(self._leak_assertions(trace, handle))

        status, message, category = self._iteration_status(assertions, violations, body_error)
        trace_ref = self._persist(run_id, trace, handle)

        return _IterationOutcome(
            iteration=IterationReport.from_metrics(
                run_id=run_id,
                iteration=iteration,
                status=status,
                metrics=metrics,
                error=message,
                error_category=category,
                trace_ref=trace_ref,
            ),
            assertions=assertions,
            violations=violations,
            view=handle.last,
            trace=trace,
        )

    def _merge_events(
        self, run_id: str, recorder: TraceRecorder, handle: AgentHandle
    ) -> list[TraceEvent]:
        """Collect the full event set for this invocation, de-duplicated."""
        events: list[TraceEvent] = list(recorder.events)
        seen = {e.event_id for e in events}
        for view in handle.views:
            for event in view.result.trace:
                if isinstance(event, TraceEvent) and event.event_id not in seen:
                    events.append(_restamp(event, run_id))
                    seen.add(event.event_id)
        events.sort(key=lambda e: e.timestamp)
        return events

    def _record_failure(self, recorder: TraceRecorder, error: BaseException) -> None:
        """Note a failure in the trace, best-effort.

        Recording the failure is itself budget-checked, so a run that already
        exceeded ``max_duration_ms`` or ``max_steps`` would raise *again* from
        inside the error handler and escape the runner entirely. A timeout must be
        reported as a FAIL, not crash the harness that was supposed to report it.
        """
        try:
            recorder.error(f"{type(error).__name__}: {error}")
        except AgentCIError as exc:
            self.warnings.append(f"could not record failure in trace: {exc}")

    def _agent_scope(self, recorder: TraceRecorder, handle: AgentHandle) -> frozenset[str]:
        """Event ids attributable to the agent under test.

        Everything emitted *inside* ``agent.run(...)`` is the agent's doing. Events
        emitted by the test body outside those calls -- a deliberate
        ``ctx.tools.call`` probing a denial, say -- are not, and the policy engine
        demotes violations found there to ``WARN``.

        Run-lifecycle and error events are always in scope: they describe the
        invocation itself, so a body-level crash still counts against the run.
        """
        ids: set[str] = set()
        for view in handle.views:
            ids.update(event.event_id for event in view.trace.events)
        for event in recorder.events:
            if event.type in LIFECYCLE_EVENTS:
                ids.add(event.event_id)
        return frozenset(ids)

    @staticmethod
    def _elapsed(recorder: TraceRecorder) -> float:
        return recorder.elapsed_ms

    # -- per-iteration evaluation ---------------------------------------------

    def _budget_assertions(self, metrics: RunMetrics) -> list[AssertionResult]:
        """Project budgets, applied to every invocation.

        Independent of what the test body asserted, so a forgotten
        ``to_have_max_cost`` cannot disable the project's cost gate.
        """
        budgets = self.config.budgets
        return budget_assertions.evaluate_budgets(
            metrics,
            max_cost_usd=budgets.max_cost_usd,
            max_latency_ms=budgets.max_latency_ms,
            max_tool_calls=budgets.max_tool_calls,
            max_steps=budgets.max_steps,
            max_retries=budgets.max_retries,
            max_tokens=budgets.max_tokens,
        )

    def _leak_assertions(self, trace: Trace, handle: AgentHandle) -> list[AssertionResult]:
        """Always-on credential scan.

        Runs on every invocation without the test author asking: a suite that never
        thinks about leakage should still catch a JWT that ended up in a tool
        argument.
        """
        output = handle.last.output_text if handle.last is not None else ""
        return [leak_assertions.leak_scan(trace, output, self.redactor)]

    def _iteration_status(
        self,
        assertions: list[AssertionResult],
        violations: list[PolicyViolation],
        error: BaseException | None,
    ) -> tuple[Status, str | None, str | None]:
        """Classify one iteration.

        Note the ordering: budget exhaustion (step limit, timeout) is a *product*
        finding and reports ``FAIL``, while an unexpected exception is an
        *infrastructure* fault and reports ``ERROR``. Collapsing the two would
        either hide real regressions or turn flaky infrastructure into blocking
        quality failures.
        """
        failed = [a for a in assertions if a.status.value == "failed"]
        if failed:
            return Status.FAIL, failed[0].message, "assertion_failed"
        # Only agent-attributable violations block. A WARN violation came from the
        # test body exercising a denial on purpose; reporting it would make policy
        # behaviour untestable.
        blocking = [v for v in violations if v.severity is Status.FAIL]
        if blocking:
            return Status.FAIL, blocking[0].message, blocking[0].kind
        # Error handling comes *before* the WARN short-circuit. A non-blocking
        # violation is expected whenever a test probes policy on purpose, so it
        # must never outrank a real body exception and report the test as passing.
        if error is not None:
            # A bare ``assert`` in a test body is a quality finding, not a broken
            # harness, so it must land in the same bucket as a failed assertion
            # rather than being reported as an infrastructure ERROR.
            rendered = _render_error(error)
            if isinstance(
                error, (AssertionError, AssertionFailed, StepLimitExceeded, DeadlineExceeded)
            ):
                return Status.FAIL, rendered, classify_error(error)
            return Status.ERROR, rendered, classify_error(error)
        if any(v.severity is Status.WARN for v in violations):
            return Status.PASS, None, None
        return Status.PASS, None, None

    def _test_status(
        self,
        iterations: list[IterationReport],
        violation_count: int,
        case_minimum_pass_rate: float | None = None,
    ) -> tuple[Status, Flakiness]:
        """Fold repetition verdicts into one test verdict plus a flakiness reason.

        A test that never produced a ``PASS`` is a failure, full stop -- flakiness
        describes a *mixture*, so it is only ever attached once both outcomes are
        present. Which bucket an all-failing test lands in comes from the recorded
        ``error_category``, not from guessing.
        """
        if not iterations:  # pragma: no cover - defensive
            return Status.SKIP, Flakiness.NONE
        if all(i.status is Status.ERROR for i in iterations):
            return Status.ERROR, Flakiness.INFRA_ERROR

        passed = sum(1 for i in iterations if i.status is Status.PASS)
        if passed == len(iterations):
            return Status.PASS, Flakiness.NONE
        if passed == 0:
            category = next((i.error_category for i in iterations if i.error_category), None)
            if category == "timeout":
                # A wall-clock overrun can genuinely vary with machine load.
                flakiness = Flakiness.TIMEOUT
            elif category == "step_limit":
                # Deterministic by nature: the agent hit the ceiling every time.
                flakiness = Flakiness.CONSISTENTLY_FAILING
            elif category == "infra_error":
                flakiness = Flakiness.INFRA_ERROR
            elif violation_count:
                flakiness = Flakiness.POLICY_VIOLATION
            else:
                flakiness = Flakiness.CONSISTENTLY_FAILING
            return Status.FAIL, flakiness

        rate = passed / len(iterations)
        floor = case_minimum_pass_rate or self.config.evaluation.minimum_pass_rate
        if self.config.evaluation.allow_flaky and rate >= floor:
            return Status.PASS, Flakiness.FLAKY
        return Status.FAIL, Flakiness.FLAKY

    # -- view construction ----------------------------------------------------

    def build_view(
        self,
        invocation: Any,
        recorder: TraceRecorder,
        start_index: int,
    ) -> ResultView:
        """Normalize an invocation into a :class:`ResultView`.

        Three sources are merged: events recorded live during the call, events the
        adapter returned in ``AgentResult.trace``, and metrics derived from the
        union. Only recorder events from ``start_index`` onward are taken, which
        keeps repeated ``agent.run()`` calls in one test body disjoint.
        """
        error = invocation.error
        result: AgentResult = invocation.result if error is None else AgentResult(output_text="")

        live = list(recorder.events[start_index:])
        returned = [e for e in result.trace if isinstance(e, TraceEvent)]

        merged: list[TraceEvent] = []
        seen: set[str] = set()
        for event in [*live, *returned]:
            if event.event_id in seen:
                continue
            seen.add(event.event_id)
            merged.append(event)
        merged.sort(key=lambda e: e.timestamp)

        trace = Trace(run_id=recorder.run_id, events=merged)
        metrics = derive_metrics(trace, latency_ms=invocation.elapsed_ms)
        if metrics.cost_usd is None and self.config.agent.model:
            metrics = metrics.model_copy(update={"cost_usd": None})
        return ResultView(result=result, trace=trace, metrics=metrics)

    # -- persistence ----------------------------------------------------------

    def _persist(self, run_id: str, trace: Trace, handle: AgentHandle) -> str | None:
        record = (
            self.config.storage.record_traces
            if self.options.record_traces is None
            else self.options.record_traces
        )
        if not record:
            return None
        try:
            stored = self.store.save_trace(run_id, trace.events)
            if handle.last is not None:
                self.store.save_result(run_id, handle.last.result)
        except OSError as exc:
            self.warnings.append(f"could not persist run artifacts: {exc}")
            return None
        if stored is None or stored.trace_path is None:
            return None
        try:
            return stored.trace_path.relative_to(self.store.root).as_posix()
        except ValueError:  # pragma: no cover
            return stored.trace_path.as_posix()

    # -- aggregation ----------------------------------------------------------

    def _summarize(self, report: RunReport) -> RunSummary:
        tests = report.tests
        executed = [t for t in tests if t.status is not Status.SKIP]
        costs = [t.cost_usd for t in tests if t.cost_usd is not None]
        tokens = [t.total_tokens for t in tests if t.total_tokens is not None]

        return RunSummary(
            total=len(tests),
            passed=sum(1 for t in tests if t.status is Status.PASS),
            failed=sum(1 for t in tests if t.status is Status.FAIL),
            warned=sum(1 for t in tests if t.status is Status.WARN),
            errored=sum(1 for t in tests if t.status is Status.ERROR),
            skipped=sum(1 for t in tests if t.status is Status.SKIP),
            flaky=sum(1 for t in tests if t.flakiness is Flakiness.FLAKY),
            pass_rate=(
                sum(1 for t in executed if t.status is Status.PASS) / len(executed)
                if executed
                else 0.0
            ),
            duration_ms=sum(t.duration_ms for t in tests),
            cost_usd=sum(costs) if costs else None,
            total_tokens=sum(tokens) if tokens else None,
            assertions_passed=sum(1 for t in tests for a in t.assertions if a.ok),
            assertions_failed=sum(len(t.failures) for t in tests),
            assertions_skipped=sum(len(t.skipped_assertions) for t in tests),
            policy_violations=len(_blocking(report.policy_violations)),
        )

    def _dimensions(self, report: RunReport) -> DimensionsReport:
        """Dimension scores over every evaluated assertion (PRD §18).

        Deliberately not collapsed into a composite: each dimension is
        independently gateable, so a policy violation cannot be averaged away by a
        good task score.
        """
        executed = [t for t in report.tests if t.status is not Status.SKIP]
        blocking = _blocking(report.policy_violations)
        if not executed:
            return DimensionsReport(policy_compliance=compliance(blocking))

        behavioural = [a for t in executed for a in t.assertions]
        tool_assertions = [a for a in behavioural if a.kind == Kind.TOOL.value]

        def rate(assertions: list[AssertionReport]) -> float | None:
            evaluated = [a for a in assertions if a.status.value != "skipped"]
            if not evaluated:
                return None
            return sum(1 for a in evaluated if a.ok) / len(evaluated)

        latencies = [t.duration_ms for t in executed if t.duration_ms > 0]
        costs = [t.cost_usd for t in executed if t.cost_usd is not None]
        budget = self.config.budgets.max_cost_usd

        cost_efficiency: float | None = None
        if costs and budget:
            cost_efficiency = sum(min(1.0, budget / c) if c > 0 else 1.0 for c in costs) / len(
                costs
            )

        return DimensionsReport(
            task_success=rate(behavioural),
            tool_correctness=rate(tool_assertions),
            policy_compliance=compliance(blocking),
            reliability=sum(1 for t in executed if t.status is Status.PASS) / len(executed),
            cost_efficiency=cost_efficiency,
            latency_ms=(sum(latencies) / len(latencies)) if latencies else None,
        )

    def _absolute_gates(self, report: RunReport) -> list[GateResult]:
        """Evaluate configured dimension gates (§18) plus absolute blocks.

        Absolute blocks come second but matter equally: a failing test or an
        infrastructure error always blocks, whether or not a dimension threshold
        happened to notice it.
        """
        gates: list[GateResult] = []
        summary = report.summary
        dimensions = report.dimensions

        values: dict[str, float | None] = {
            "task_success": dimensions.task_success,
            "tool_correctness": dimensions.tool_correctness,
            "policy_compliance": dimensions.policy_compliance,
            "reliability": dimensions.reliability,
            "max_cost_usd": summary.cost_usd,
            "max_latency_ms": (
                max((t.duration_ms for t in report.tests), default=0.0) or None
            ),
            "max_tokens": float(summary.total_tokens) if summary.total_tokens else None,
        }

        for name, threshold in self.config.gates.active().items():
            actual = values.get(name)
            if actual is None:
                gates.append(
                    GateResult(
                        name=name,
                        status=Status.SKIP,
                        threshold=threshold.target,
                        message="no data to evaluate this gate",
                        source="gate",
                    )
                )
                continue
            ok = threshold.evaluate(actual)
            gates.append(
                GateResult(
                    name=name,
                    status=Status.PASS if ok else Status.FAIL,
                    actual=actual,
                    threshold=threshold.target,
                    message=""
                    if ok
                    else f"{name} gate failed ({threshold.describe(actual)})",
                    source="gate",
                )
            )

        if summary.failed:
            gates.append(
                GateResult(
                    name="tests",
                    status=Status.FAIL,
                    actual=float(summary.failed),
                    threshold="0 failing",
                    message=f"{summary.failed} test(s) failed",
                    source="absolute",
                )
            )
        if summary.errored:
            gates.append(
                GateResult(
                    name="errors",
                    status=Status.FAIL,
                    actual=float(summary.errored),
                    threshold="0 errors",
                    message=(
                        f"{summary.errored} test(s) ended in ERROR; an infrastructure fault "
                        f"is never reported as a pass"
                    ),
                    source="absolute",
                )
            )
        if summary.policy_violations:
            gates.append(
                GateResult(
                    name="policy",
                    status=Status.FAIL,
                    actual=float(summary.policy_violations),
                    threshold="0 violations",
                    message=f"{summary.policy_violations} policy violation(s)",
                    source="policy",
                )
            )
        return gates

    def _collect_violations(self, report: RunReport) -> list[PolicyViolation]:
        return _dedupe_violations([v for test in report.tests for v in test.policy_violations])

    def _overall_status(self, report: RunReport) -> Status:
        if report.summary.errored or any(g.status is Status.FAIL for g in report.gates):
            return Status.FAIL
        if report.regression.status is Status.FAIL:
            return Status.FAIL
        if report.regression.status is Status.WARN or report.summary.warned:
            return Status.WARN
        if not report.tests:
            return Status.SKIP
        return Status.PASS

    # -- misc -----------------------------------------------------------------

    def _matches_tags(self, case: AgentTestCase) -> bool:
        if not self.options.tags:
            return True
        return set(self.options.tags).issubset(set(case.tags))

    def _skipped(self, case: AgentTestCase, reason: str) -> TestReport:
        return TestReport(
            test_id=case.test_id,
            name=case.name,
            file=case.file,
            line=case.line,
            tags=list(case.tags),
            dependencies=list(case.dependencies),
            status=Status.SKIP,
            error=reason,
        )

    def _aggregate_metrics(self, test: TestReport) -> RunMetrics:
        """Worst-case metrics across repetitions; reporting only."""
        costs = [i.cost_usd for i in test.iterations if i.cost_usd is not None]
        return RunMetrics(
            latency_ms=max((i.latency_ms for i in test.iterations), default=0.0),
            cost_usd=sum(costs) if costs else None,
            total_tokens=next(
                (i.total_tokens for i in test.iterations if i.total_tokens is not None), None
            ),
            tool_calls=max((i.tool_calls for i in test.iterations), default=0),
            steps=max((i.steps for i in test.iterations), default=0),
            retries=max((i.retries for i in test.iterations), default=0),
            errors=sum(1 for i in test.iterations if i.status is Status.ERROR),
        )

    def _environment(self) -> EnvironmentReport:
        git = git_context(self.root)
        return EnvironmentReport(
            agent=self.adapter.name,
            agent_model=self.config.agent.model,
            config_version=self.config.version,
            ci=is_ci(),
            ci_provider=ci_provider(),
            pricing_as_of=self.costs.model.as_of,
            platform=PlatformInfo(),
            git_commit=git.commit,
            git_branch=git.branch,
            git_dirty=git.dirty,
        )


def _render_error(error: BaseException) -> str:
    """Render a test-body failure for the report, including its hint.

    ``AgentCIError`` carries a ``hint`` holding the actionable half of the
    diagnosis, and ``__str__`` returns only the message on purpose so structured
    ``detail`` never leaks into logs. That means the hint has to be appended
    explicitly, or the report would tell a user *that* something failed without
    telling them *what to do*, which is the whole point of carrying one.

    The ``\\n  hint: `` shape matches :mod:`agentci.assertions.base`, so assertion
    and error text read identically in the JSON and Markdown reports.
    """
    if isinstance(error, AgentCIError):
        text = error.message
        if error.hint:
            return f"{text}\n  hint: {error.hint}"
        return text
    text = str(error)
    return text or type(error).__name__


def _restamp(event: TraceEvent, run_id: str) -> TraceEvent:
    """Align a returned event's ``run_id`` with the invocation it belongs to."""
    return event if event.run_id == run_id else event.model_copy(update={"run_id": run_id})


def _blocking(violations: Sequence[PolicyViolation]) -> list[PolicyViolation]:
    """Only agent-attributable violations decide a verdict."""
    return [v for v in violations if v.severity is Status.FAIL]


def _dedupe_violations(violations: Sequence[PolicyViolation]) -> list[PolicyViolation]:
    """Collapse duplicates, keeping the most severe instance of each.

    Severity matters: a violation seen once in agent scope (FAIL) and once in test
    scope (WARN) must not be collapsed down to the WARN, which would silently stop
    it blocking.
    """
    seen: dict[tuple[str, str, str], PolicyViolation] = {}
    order: list[tuple[str, str, str]] = []
    for violation in violations:
        key = (violation.kind, violation.tool or "", violation.message)
        existing = seen.get(key)
        if existing is None:
            seen[key] = violation
            order.append(key)
        elif violation.severity is Status.FAIL and existing.severity is not Status.FAIL:
            seen[key] = violation
    return [seen[key] for key in order]


async def _await(awaitable: Any) -> Any:  # pragma: no cover - trivial wrapper
    return await awaitable


__all__ = ["AgentHandle", "RunOptions", "TestRunner"]
