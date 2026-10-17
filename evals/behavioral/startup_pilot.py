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
    Completed,
    Record,
    RegisteredScenario,
    Scored,
    write_new,
)
from evals.behavioral.scenarios import RegisteredSet, load_set

MODEL = "gpt-5.6-terra"
EFFORT = "high"


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
    status: Literal["completed", "incomplete"] = "incomplete"
    reason = "interrupted before completion"
    try:
        arms = [("baseline", baseline), ("candidate", candidate)]
        exports = []
        for arm, revision in arms:
            evaluated = export.export_revision(source, revision, out / "exports" / arm, window)
            observed = probe.probe_codex(layout, budget, evaluated, arm, MODEL, EFFORT, worlds, default_source())
            if observed is None or not observed.passed:
                raise ValueError(f"{arm} isolation probe unavailable or failed; inspect retained evidence")
            exports.append(evaluated)
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
                if runner.run_codex(runner.CodexPlan(plan, EFFORT, default_source()), budget):
                    raise ValueError("session admission refused by cap or incomplete accounting")
                key = plan.planned()[0][1]
                directory = layout.run_directory(key)
                observation = observe((directory / "rollout.jsonl").read_text())
                write_new(directory / "workflow.json", observation)
                if not observation.configurations or any(
                    c.model != MODEL or c.effort != EFFORT for c in observation.configurations
                ):
                    raise ValueError("native actor configuration differs from the required Terra/high configuration")
                record = next(r for r in layout.run_records() if r.run == key)
                if not isinstance(record.outcome, Completed):
                    raise ValueError(f"{key.display()} did not complete; inspect retained evidence")
                for pending in layout.scorer_inputs():
                    if pending.run.display() not in scoring.valid_score_counts(layout) and not isinstance(
                        scoring.ScoringRun(layout, budget).score(pending), Scored
                    ):
                        raise ValueError("scoring unavailable or incomplete")
                if substance.assess(layout, budget) or any(
                    isinstance(a.outcome, AssessmentFailure) for a in layout.assessments()
                ):
                    raise ValueError("assessment unavailable or incomplete")
                print(f"recorded {key.display()}; known USD {spend.total(spend.items(layout)):.4f}", flush=True)
        status, reason = "completed", "descriptive paired observations recorded; formal counts not met"
    except (
        ValueError,
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
            out / "pilot.json",
            PilotResult(
                "pinboard-startup-pilot-result/v1",
                status,
                reason,
                spend.total(spend.items(layout)),
                spend.main_usage_unknown(layout) or bool(spend.comparison_reasons(layout)),
                any(a.reviewer_usage for _, a in layout.codex_accounting()),
                [r.run.display() for r in layout.run_records() if isinstance(r.outcome, Completed)],
                "inconclusive",
            ),
        )
        print(reason, flush=True)
        print(spend.report(layout), end="", flush=True)
    return 0 if status == "completed" else 2
