"""One paired descriptive startup pilot using the maintained delivery drivers and spending owner."""

import datetime
import hashlib
import time
from pathlib import Path
from typing import Literal

import msgspec

from evals.behavioral import codex_driver, export, probe, processes, runner, scoring, spend, substance
from evals.behavioral.credentials import default_source
from evals.behavioral.layout import Layout
from evals.behavioral.records import (
    AssessmentFailure,
    ChecklistItem,
    CodexAccounting,
    CodexRunDetails,
    Completed,
    ExportRecord,
    ProbeRecord,
    Record,
    RegisteredScenario,
    Runtime,
    Scored,
    write_new,
)
from evals.behavioral.scenarios import RegisteredSet, load_set

MODEL = "gpt-5.6-terra"
EFFORT = "high"
DESCRIPTIVE_MODEL = "claude-sonnet-5-5"


class PilotWindow(Record, frozen=True):
    schema: Literal["pinboard-startup-pilot-window/v1"]
    started_at: str
    deadline_at: str
    maximum_seconds: int
    admission_cap_usd: float
    source: str
    baseline_revision: str
    candidate_revision: str
    scenarios: list[RegisteredScenario]
    targeted_rules: list[ChecklistItem]

    def __post_init__(self) -> None:
        started = datetime.datetime.fromisoformat(self.started_at)
        deadline = datetime.datetime.fromisoformat(self.deadline_at)
        if (
            started.tzinfo is None
            or deadline.tzinfo is None
            or deadline - started != datetime.timedelta(seconds=7200)
            or self.maximum_seconds != 7200
            or self.admission_cap_usd != 30.0
        ):
            raise ValueError("pilot window must retain a timezone-aware two-hour interval and the 30 USD admission cap")


class PilotResult(Record, frozen=True):
    schema: Literal["pinboard-startup-pilot-result/v1"]
    status: Literal["completed", "incomplete"]
    reason: str
    known_cost_usd: float
    unreported_usage: bool
    unknown_reviewer_cost: bool
    completed_runs: list[str]
    formal_verdict: Literal["inconclusive"]


class TurnConfiguration(msgspec.Struct, frozen=True):
    turn_id: str
    model: str
    effort: str


class NativeCall(msgspec.Struct, frozen=True):
    name: str


class WorkflowObservation(Record, frozen=True):
    schema: Literal["pinboard-native-workflow-observation/v1"]
    configurations: list[TurnConfiguration]
    native_call_names: list[str]
    evidence: Literal["observed-calls-only"]


def observe(rollout: str) -> WorkflowObservation:
    """Record actual model/effort contexts and native calls without treating a call as a committed effect."""
    configurations: dict[str, TurnConfiguration] = {}
    calls: list[str] = []
    for record in codex_driver.rollout_records(rollout):
        if record.type == "turn_context":
            configuration = msgspec.json.decode(record.payload, type=TurnConfiguration)
            configurations[configuration.turn_id] = configuration
        elif record.type == "response_item":
            item = msgspec.json.decode(record.payload, type=codex_driver.ItemKind)
            if item.type in {"function_call", "custom_tool_call"}:
                calls.append(msgspec.json.decode(record.payload, type=NativeCall).name)
    return WorkflowObservation(
        "pinboard-native-workflow-observation/v1", list(configurations.values()), calls, "observed-calls-only"
    )


def validate_accounting(layout: Layout, allow_unknown_approval_price: bool) -> None:
    reasons = spend.comparison_reasons(layout)
    if allow_unknown_approval_price and all(
        usage.model == "codex-auto-review"
        for _, accounting in layout.codex_accounting()
        for usage in accounting.reviewer_usage
    ):
        reasons = [reason for reason in reasons if reason != "comparison reviewer dollar cost is unknown"]
    if reasons:
        raise ValueError("; ".join(reasons))


def validate_workflow(layout: Layout, observation: WorkflowObservation, allow_unknown_approval_price: bool) -> None:
    validate_accounting(layout, allow_unknown_approval_price)
    contexts = [context for context in observation.configurations if context.model != "codex-auto-review"]
    if not contexts or any(context.model != MODEL or context.effort != EFFORT for context in contexts):
        raise ValueError("native task actor configuration differs from the required Terra/high configuration")


def run(source: Path, baseline: str, candidate: str, scenario_set: Path, out: Path, worlds: Path) -> int:
    registered = load_set(scenario_set)
    runner.require_codex_world_location(worlds)
    out.mkdir(parents=True, exist_ok=False)
    started = datetime.datetime.now(datetime.UTC)
    window = processes.Window(time.monotonic() + 7200)
    write_new(
        out / "window.json",
        PilotWindow(
            "pinboard-startup-pilot-window/v1",
            started.isoformat(),
            (started + datetime.timedelta(seconds=7200)).isoformat(),
            7200,
            30.0,
            str(source),
            baseline,
            candidate,
            [RegisteredScenario(s.id, hashlib.sha256(registered.content(s)).hexdigest()) for s in registered.scenarios],
            registered.registration.targeted_rules,
        ),
    )
    layout = Layout(out)
    budget = spend.Budget(layout, 30.0, window, False)
    return execute(source, baseline, candidate, registered, layout, worlds, budget, out / "pilot.json", False)


def continue_run(
    source: Path, baseline: str, candidate: str, scenario_set: Path, out: Path, worlds: Path, window_sha256: str
) -> int:
    """Reuse a retained pilot under explicit authority for unknown auxiliary approval-review dollars.

    Validation is read-only and precedes all further paid work. Original records are never replaced.
    """
    registered = load_set(scenario_set)
    runner.require_codex_world_location(worlds)
    window_bytes = (out / "window.json").read_bytes()
    if hashlib.sha256(window_bytes).hexdigest() != window_sha256:
        raise ValueError("retained window digest differs from the selected original; no session started")
    saved = msgspec.json.decode(window_bytes, type=PilotWindow)
    deadline = datetime.datetime.fromisoformat(saved.deadline_at)
    if (
        Path(saved.source).resolve() != source.resolve()
        or saved.baseline_revision != baseline
        or saved.candidate_revision != candidate
        or saved.scenarios != registered.registration.scenarios
        or saved.targeted_rules != registered.registration.targeted_rules
    ):
        raise ValueError("retained pilot window, source, revisions or registered scenarios differ; no session started")
    original = msgspec.json.decode((out / "pilot.json").read_bytes(), type=PilotResult)
    layout = Layout(out)
    completed = {record.run.display() for record in layout.run_records() if isinstance(record.outcome, Completed)}
    if not set(original.completed_runs) <= completed:
        raise ValueError("original completed task evidence is missing; no session started")
    # UTC is persisted across processes; the executor uses one monotonic deadline for the remaining interval.
    remaining = (deadline - datetime.datetime.now(datetime.UTC)).total_seconds()
    window = processes.Window(time.monotonic() + remaining)
    result_path = out / "continuation.json"
    if result_path.exists():
        raise ValueError(f"continuation result already exists at {result_path}; inspect it before further work")
    budget = spend.Budget(layout, saved.admission_cap_usd, window, True)
    return execute(source, baseline, candidate, registered, layout, worlds, budget, result_path, True)


def retained_exports(layout: Layout, baseline: str, candidate: str, registered: RegisteredSet) -> list[ExportRecord]:
    for assessment in layout.assessments():
        if assessment.assessor_model != DESCRIPTIVE_MODEL:
            path = layout.assessment_directory(assessment.run) / "assessment.json"
            raise ValueError(f"retained assessment model differs at {path}; inspect its provenance; no session started")
    exports: list[ExportRecord] = []
    for arm, revision in (("baseline", baseline), ("candidate", candidate)):
        evaluated = export.load_export(layout.root / "exports" / arm)
        if (
            evaluated.commit != revision
            or Path(evaluated.plugin_root).resolve() != (layout.root / "exports" / arm / "plugin").resolve()
        ):
            raise ValueError(f"retained {arm} export identity differs; no session started")
        observed = msgspec.json.decode(layout.probe_file(arm).read_bytes(), type=ProbeRecord)
        required = runner.codex_probe_inventory("unused", evaluated, MODEL, EFFORT)[1:]
        if (
            observed.name != arm
            or observed.runtime is not Runtime.CODEX
            or not observed.passed
            or observed.findings
            or not all(entry in observed.inventory for entry in required)
        ):
            raise ValueError(f"retained {arm} isolation probe differs or failed; no session started")
        exports.append(evaluated)
    expected = {scenario.id: scenario.sha256 for scenario in registered.registration.scenarios}
    for record in layout.run_records():
        if (
            record.run.variant not in ("baseline", "candidate")
            or record.run.index != 1
            or record.run.scenario_id not in expected
            or record.registration != registered.registration
            or record.scenario_sha256 != expected[record.run.scenario_id]
            or record.evaluated != exports[0 if record.run.variant == "baseline" else 1]
            or record.runtime is not Runtime.CODEX
            or record.model != MODEL
            or not isinstance(record.details, CodexRunDetails)
            or record.details.reasoning_effort != EFFORT
            or not isinstance(record.outcome, Completed)
        ):
            raise ValueError(
                f"retained task identity or completion differs for {record.run.display()}; no session started"
            )
        directory = layout.run_directory(record.run)
        rollout = (directory / "rollout.jsonl").read_text()
        observation = observe(rollout)
        accounting = msgspec.json.decode((directory / "accounting.json").read_bytes(), type=CodexAccounting)
        if accounting != codex_driver.rollout_accounting(rollout, MODEL, True):
            raise ValueError(f"retained accounting differs for {record.run.display()}; no session started")
        if msgspec.json.decode((directory / "workflow.json").read_bytes(), type=WorkflowObservation) != observation:
            raise ValueError(f"retained workflow differs for {record.run.display()}; no session started")
        validate_workflow(layout, observation, True)
        if not (directory / "scorer-input.json").is_file():
            raise ValueError(f"retained scorer input missing for {record.run.display()}; no session started")
    return exports


def execute(
    source: Path,
    baseline: str,
    candidate: str,
    registered: RegisteredSet,
    layout: Layout,
    worlds: Path,
    budget: spend.Budget,
    result_path: Path,
    continuing: bool,
) -> int:
    window = budget.window
    status: Literal["completed", "incomplete"] = "incomplete"
    reason = "interrupted before completion"
    try:
        arms = [("baseline", baseline), ("candidate", candidate)]
        if continuing:
            exports = retained_exports(layout, baseline, candidate, registered)
            validate_accounting(layout, True)
        else:
            exports = []
            for arm, revision in arms:
                evaluated = export.export_revision(source, revision, layout.root / "exports" / arm, window)
                observed = probe.probe_codex(layout, budget, evaluated, arm, MODEL, EFFORT, worlds, default_source())
                if observed is None or not observed.passed:
                    raise ValueError(f"{arm} isolation probe unavailable or failed; inspect retained evidence")
                exports.append(evaluated)
        window.timeout(300)
        for scenario in registered.scenarios:
            for (arm, _), evaluated in zip(arms, exports, strict=True):
                plan = runner.RunPlan(
                    layout,
                    worlds,
                    evaluated,
                    arm,
                    RegisteredSet((scenario,), registered.registration, registered.scenario_sources),
                    1,
                    1,
                    MODEL,
                    window,
                )
                key = plan.planned()[0][1]
                run_trial(plan, budget, continuing)
                print(f"recorded {key.display()}; known USD {spend.total(spend.items(layout)):.4f}", flush=True)
        status, reason = "completed", "descriptive paired observations recorded; formal counts not met"
    except (
        ValueError,
        msgspec.DecodeError,
        TimeoutError,
        OSError,
        processes.ToolUnavailableError,
        processes.GitError,
        export.SeedFailure,
        codex_driver.CodexUnavailableError,
        runner.IsolationBreachError,
        runner.CredentialConflictError,
    ) as failure:
        reason = str(failure)
    finally:
        write_new(
            result_path,
            PilotResult(
                "pinboard-startup-pilot-result/v1",
                status,
                reason,
                spend.total(spend.items(layout)),
                spend.main_usage_unknown(layout)
                or any(
                    reason != "comparison reviewer dollar cost is unknown"
                    for reason in spend.comparison_reasons(layout)
                ),
                any(a.reviewer_usage for _, a in layout.codex_accounting()),
                [r.run.display() for r in layout.run_records() if isinstance(r.outcome, Completed)],
                "inconclusive",
            ),
        )
        print(reason, flush=True)
        print(spend.report(layout), end="", flush=True)
    return 0 if status == "completed" else 2


def run_trial(plan: runner.RunPlan, budget: spend.Budget, continuing: bool) -> None:
    layout = plan.layout
    key = plan.planned()[0][1]
    if layout.run_record(key) is None and runner.run_codex(runner.CodexPlan(plan, EFFORT, default_source()), budget):
        raise ValueError("session admission refused by cap or incomplete accounting")
    directory = layout.run_directory(key)
    observation = observe((directory / "rollout.jsonl").read_text())
    if not (directory / "workflow.json").exists():
        write_new(directory / "workflow.json", observation)
    validate_workflow(layout, observation, continuing)
    record = layout.run_record(key)
    if record is None or not isinstance(record.outcome, Completed):
        raise ValueError(f"{key.display()} did not complete; inspect retained evidence")
    for pending in layout.scorer_inputs():
        if pending.run.display() not in scoring.valid_score_counts(layout, DESCRIPTIVE_MODEL) and not isinstance(
            scoring.ScoringRun(layout, budget, DESCRIPTIVE_MODEL).score(pending), Scored
        ):
            raise ValueError("scoring unavailable or incomplete")
    if substance.assess(layout, budget, DESCRIPTIVE_MODEL) or any(
        isinstance(a.outcome, AssessmentFailure) for a in layout.assessments()
    ):
        raise ValueError("assessment unavailable or incomplete")
