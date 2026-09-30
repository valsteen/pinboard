"""Run a declared scenario set against one runtime: seed each world, drive every scripted turn, record the evidence.

Each run seeds a fresh world, snapshots its observed state before the first turn and after every turn, runs a
turn's declared hook before sending it, and writes ``run.json`` for every run whose session started (completed,
stopped or failed) so its spend is always recorded. Only completed runs get a ``scorer-input.json``. Claude Code
runs may run in parallel; Codex runs are always sequential, each in its own isolated home. An isolation breach in
either runtime, an interrupt or a harness defect stops every run of the batch that has not started yet.
"""

import subprocess
import tempfile
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import msgspec

from evals.behavioral import claude_driver, codex_driver, credentials, processes, world
from evals.behavioral.board import SEEDED_HOST_ID
from evals.behavioral.claude_driver import ClaudeSession, now
from evals.behavioral.export import SeedFailure
from evals.behavioral.layout import RUN_RECORD, SCORER_INPUT, Layout
from evals.behavioral.processes import GitError
from evals.behavioral.records import (
    Completed,
    ExportRecord,
    Failed,
    InventoryEntry,
    ObservedState,
    PermissionDenial,
    Redaction,
    RunDetails,
    RunKey,
    RunOutcome,
    RunRecord,
    Runtime,
    Scenario,
    ScorerInput,
    SeededItem,
    Stopped,
    TurnEvidence,
    write_new,
)
from evals.behavioral.scenarios import RegisteredSet
from evals.behavioral.spend import Budget, Category


@dataclass(frozen=True)
class RunPlan:
    layout: Layout
    worlds: Path
    export: ExportRecord
    variant: str
    scenarios: RegisteredSet
    first_index: int
    runs: int
    model: str
    window: processes.Window

    def planned(self) -> list[tuple[Scenario, RunKey]]:
        return [
            (scenario, RunKey(scenario_id=scenario.id, variant=self.variant, index=index))
            for index in range(self.first_index, self.first_index + self.runs)
            for scenario in self.scenarios.scenarios
        ]


@dataclass
class RunState:
    """Mutable evidence of one run while it is in progress."""

    plan: RunPlan
    scenario: Scenario
    key: RunKey
    directory: Path
    started_at: str
    states: list[ObservedState]
    turns: list[TurnEvidence]
    hooks_log: list[str]
    seeded: list[SeededItem]

    def world_root(self) -> Path:
        return self.plan.worlds / f"{self.key.scenario_id}-{self.key.variant}-{self.key.index}"

    def snapshot(self, built: world.World) -> None:
        state = world.snapshot(built, f"state-{len(self.states)}")
        (self.directory / f"{state.name}.txt").write_text(state.text)
        self.states.append(state)

    def finish(
        self,
        runtime: Runtime,
        cli_version: str,
        details: RunDetails,
        context: list[InventoryEntry],
        hosts: list[str],
        outcome: RunOutcome,
    ) -> RunRecord:
        record = RunRecord(
            schema="pinboard-behavioral-run/v2",
            run=self.key,
            runtime=runtime,
            cli_version=cli_version,
            model=self.plan.model,
            details=details,
            evaluated=self.plan.export,
            fixture_difference=world.fixture_difference(runtime),
            seeded_host_id=SEEDED_HOST_ID,
            seeded_items=self.seeded,
            observed_host_ids=hosts,
            inventory=context,
            turns=self.turns,
            started_at=self.started_at,
            finished_at=now(),
            outcome=outcome,
        )
        write_new(self.directory / RUN_RECORD, record)
        if self.hooks_log:
            (self.directory / "hooks.log").write_text("".join(self.hooks_log))
        if isinstance(outcome, Completed):
            write_new(
                self.directory / SCORER_INPUT,
                ScorerInput(
                    schema="pinboard-behavioral-scorer-input/v1",
                    run=self.key,
                    replies=[turn.final_reply for turn in self.turns],
                    states=self.states,
                    hooks_log="".join(self.hooks_log) if self.hooks_log else None,
                    redactions=self.redactions(),
                ),
            )
        return record

    def redactions(self) -> list[Redaction]:
        return [
            Redaction(value=str(self.world_root()), placeholder="<world>"),
            Redaction(value=self.plan.export.plugin_root, placeholder="<plugin>"),
            Redaction(value=str(Path.home()), placeholder="<home>"),
        ]

    def run_hook(self, built: world.World, index: int) -> None:
        hook = self.scenario.turns[index - 1].before
        if hook is not None:
            self.hooks_log.append(world.run_hook(hook, built))


def start(plan: RunPlan, scenario: Scenario, key: RunKey) -> RunState:
    directory = plan.layout.run_directory(key)
    directory.mkdir(parents=True, exist_ok=False)
    return RunState(plan, scenario, key, directory, now(), [], [], [], [])


def run_claude(plan: RunPlan, budget: Budget, jobs: int) -> list[str]:
    """Run every planned Claude Code run; return the runs that did not start because the cap was reached.

    An isolation breach in any run stops every run that has not started yet and is raised once the running ones end.
    """
    return execute(plan, budget, jobs, Category.CLAUDE_AGENT_RUN, lambda scenario, key: claude_run(plan, scenario, key))


def execute(
    plan: RunPlan, budget: Budget, jobs: int, category: Category, one: Callable[[Scenario, RunKey], RunRecord]
) -> list[str]:
    skipped: list[str] = []
    halted = threading.Event()

    def guarded(scenario: Scenario, key: RunKey) -> None:
        if halted.is_set():
            return
        projected = budget.reserve(category)
        if projected is None:
            skipped.append(key.display())
            return
        try:
            one(scenario, key)
        except BaseException:
            halted.set()
            raise
        finally:
            budget.release(projected)

    pending = [(scenario, key) for scenario, key in plan.planned() if not (plan.layout.run_directory(key)).exists()]
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = [executor.submit(guarded, scenario, key) for scenario, key in pending]
    for future in futures:
        future.result()
    return sorted(skipped)


def claude_run(plan: RunPlan, scenario: Scenario, key: RunKey) -> RunRecord:
    state = start(plan, scenario, key)
    version = claude_driver.claude_version()
    session = ClaudeSession.start(Path(plan.export.plugin_root), plan.model, state.world_root() / "tally")
    outcome: RunOutcome = Completed()
    try:
        built, state.seeded = world.build_world(
            state.world_root(),
            scenario,
            Runtime.CLAUDE_CODE,
            Path(plan.export.plugin_root),
            str(uuid.uuid4()),
            plan.window,
        )
        state.snapshot(built)
        outcome = claude_turns(state, built, session)
    except (SeedFailure, GitError) as failure:
        outcome = Failed(stage="world" if not state.turns else f"after turn {len(state.turns)}", reason=str(failure))
    except (claude_driver.StreamError, msgspec.DecodeError, subprocess.SubprocessError, OSError) as failure:
        outcome = Failed(stage=f"turn {len(state.turns) + 1}", reason=str(failure) or type(failure).__name__)
    except BaseException as failure:
        state.finish(
            Runtime.CLAUDE_CODE,
            version,
            session.details(),
            session.loaded_context(),
            session.observed_host_ids,
            Failed(stage="harness", reason=repr(failure)),
        )
        raise
    record = state.finish(
        Runtime.CLAUDE_CODE, version, session.details(), session.loaded_context(), session.observed_host_ids, outcome
    )
    if isinstance(outcome, Stopped) and outcome.reason.startswith(ISOLATION_FAILED):
        raise IsolationBreachError(f"{key.display()}: {outcome.reason}; no further Claude Code run starts")
    return record


def claude_turns(state: RunState, built: world.World, session: ClaudeSession) -> RunOutcome:
    """Send every scripted turn; a turn Claude reports as failed, or an isolation finding, ends the run unscored."""
    for index, turn in enumerate(state.scenario.turns, start=1):
        state.run_hook(built, index)
        sent = session.turn(index, turn.human, turn.before, state.directory / f"turn-{index}.jsonl")
        state.turns.append(sent.evidence)
        state.snapshot(built)
        if index == 1 and (findings := session.isolation_findings()):
            return Stopped(reason=ISOLATION_FAILED + "; ".join(findings) + "; ask the human")
        if sent.problem is not None:
            return Failed(stage=f"turn {index}", reason=sent.problem)
    return Completed()


@dataclass(frozen=True)
class CodexPlan:
    run: RunPlan
    reasoning_effort: str
    credential_source: Path


ISOLATION_FAILED = "isolation check failed: "


class IsolationBreachError(Exception):
    """A run loaded a source beyond the exported plugin, its MCP server and the runtime's own bundled context; the human
    must decide before any further run of that runtime starts."""


class CredentialConflictError(Exception):
    """The human's Codex login changed during a run; its refreshed copy was not written back and runs must stop."""


class WorldLocationError(Exception):
    """Codex worlds inside a default writable temporary root would widen the evaluated sandbox."""


def require_codex_world_location(worlds: Path) -> None:
    resolved = worlds.resolve()
    for root in {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()}:
        if resolved == root or root in resolved.parents:
            raise WorldLocationError(
                f"{worlds} lies inside {root}, which Codex's :workspace profile makes writable; choose a world "
                "directory outside the system temporary directories so the sandbox matches a normal project"
            )


def run_codex(plan: CodexPlan, budget: Budget) -> list[str]:
    """Run every planned Codex run sequentially; a denied Git write stops only its run, while a login conflict or an
    isolation breach stops every later run."""
    codex_driver.price(plan.run.model)
    require_codex_world_location(plan.run.worlds)
    skipped: list[str] = []
    for scenario, key in plan.run.planned():
        if plan.run.layout.run_directory(key).exists():
            continue
        projected = budget.reserve(Category.CODEX_AGENT_RUN)
        if projected is None:
            skipped.append(key.display())
            continue
        try:
            record = codex_run(plan, scenario, key)
        finally:
            budget.release(projected)
        if isinstance(record.outcome, Stopped):
            print(f"stopped {key.display()}: {record.outcome.reason}")
    return skipped


def codex_run(plan: CodexPlan, scenario: Scenario, key: RunKey) -> RunRecord:
    run_plan = plan.run
    state = start(run_plan, scenario, key)
    version = codex_driver.codex_version(run_plan.window)
    project = state.world_root() / "tally"
    outcome: RunOutcome = Completed()
    interrupted: BaseException | None = None
    context = codex_driver.LoadedContext(
        entries=[], sandbox_mode="unreported", approval_policy="unreported", writable_roots=[], prompt_text=""
    )
    with (
        credentials.exclusive_codex_session(Path(tempfile.gettempdir()), run_plan.window),
        credentials.isolated_home(plan.credential_source, None, run_plan.window) as home,
    ):
        try:
            built, state.seeded = world.build_world(
                state.world_root(),
                scenario,
                Runtime.CODEX,
                Path(run_plan.export.plugin_root),
                str(uuid.uuid4()),
                run_plan.window,
            )
            codex_driver.write_config(
                home.path, Path(run_plan.export.plugin_root), run_plan.model, plan.reasoning_effort, run_plan.window
            )
            context = codex_driver.loaded_context(home.path, project, run_plan.window)
            (state.directory / "prompt-input.txt").write_text(context.prompt_text)
            findings = codex_driver.isolation_findings(context, home.path / "plugins" / "cache", project)
            if findings:
                outcome = Stopped(reason=ISOLATION_FAILED + "; ".join(findings))
            else:
                state.snapshot(built)
                outcome = codex_turns(state, built, home, plan)
        except processes.CleanupUnconfirmed:
            raise
        except (SeedFailure, GitError, codex_driver.CodexUnavailableError) as failure:
            outcome = Failed(
                stage="world" if not state.turns else f"after turn {len(state.turns)}", reason=str(failure)
            )
        except (codex_driver.CodexStreamError, msgspec.DecodeError, subprocess.SubprocessError, OSError) as failure:
            outcome = Failed(stage=f"turn {len(state.turns) + 1}", reason=str(failure) or type(failure).__name__)
        except BaseException as failure:
            outcome = Failed(stage="harness", reason=repr(failure))
            interrupted = failure
    settlement = home.settlement
    details = codex_driver.details(
        plan.reasoning_effort, context, settlement is credentials.CredentialSettlement.WRITTEN_BACK
    )
    record = state.finish(Runtime.CODEX, version, details, context.entries, [], outcome)
    if settlement is credentials.CredentialSettlement.REFUSED_SOURCE_CHANGED:
        raise CredentialConflictError(
            "~/.codex/auth.json changed while a Codex run held a refreshed copy; nothing was written back. "
            "Check your Codex login before running Codex evaluations again."
        ) from interrupted
    if interrupted is not None:
        raise interrupted
    if isinstance(outcome, Stopped) and outcome.reason.startswith(ISOLATION_FAILED):
        raise IsolationBreachError(f"{key.display()}: {outcome.reason}; no further Codex run starts")
    return record


def codex_turns(state: RunState, built: world.World, home: credentials.IsolatedHome, plan: CodexPlan) -> RunOutcome:
    unconfirmed: processes.CleanupUnconfirmed | None = None
    try:
        return codex_thread(state, built, home.path, plan)
    except processes.CleanupUnconfirmed as failure:
        unconfirmed = failure
        raise
    finally:
        try:
            rollout = credentials.without_login(codex_driver.rollout_text(home.path), home.copied)
            (state.directory / "rollout.jsonl").write_text(rollout)
            accounting = codex_driver.rollout_accounting(
                rollout,
                plan.run.model,
                len(state.turns) == len(state.scenario.turns) and all(t.cost_usd is not None for t in state.turns),
            )
            write_new(state.directory / "accounting.json", accounting)
        except BaseException as evidence_failure:
            if unconfirmed is not None:
                unconfirmed.args = (*unconfirmed.args, f"rollout capture failed: {evidence_failure!r}")
                raise unconfirmed from evidence_failure
            raise


def codex_thread(state: RunState, built: world.World, home: Path, plan: CodexPlan) -> RunOutcome:
    thread_id: str | None = None
    reported: codex_driver.Usage | None = None
    seen = codex_driver.RolloutRefusals(git_writes=[], approvals=[])
    for index, turn in enumerate(state.scenario.turns, start=1):
        state.run_hook(built, index)
        started = now()
        completed, reading = codex_driver.run_turn(
            home, built.project, thread_id, turn.human, state.directory / f"turn-{index}.jsonl", plan.run.window
        )
        thread_id = thread_id or reading.thread_id
        if thread_id is None:
            return Failed(stage=f"turn {index}", reason=f"codex reported no thread: {completed.stderr.strip()}"[:2000])
        refusals = codex_driver.rollout_refusals(codex_driver.rollout_text(home))
        git_refused = refusals.git_writes[len(seen.git_writes) :]
        approval_refused = refusals.approvals[len(seen.approvals) :]
        seen = refusals
        reading.denials.extend(PermissionDenial(tool="git (rollout)", detail=text) for text in git_refused)
        reading.denials.extend(PermissionDenial(tool="approval (rollout)", detail=text) for text in approval_refused)
        reading.git_write_denied |= bool(git_refused)
        reading.mcp_approval_denied |= bool(approval_refused)
        state.turns.append(
            codex_driver.turn_evidence(
                index,
                turn.human,
                turn.before,
                reading.thread_id or thread_id,
                reading,
                reported,
                plan.run.model,
                started,
            )
        )
        reported = reading.usage or reported
        state.snapshot(built)
        if reading.mcp_approval_denied:
            return Stopped(reason=f"runtime approval rejected a tool call in turn {index}; ask the human")
        if completed.timed_out or reading.usage is None:
            return Failed(stage=f"turn {index}", reason="session incomplete; unreported usage and cost unknown")
        if completed.returncode != 0 or reading.errors:
            return Failed(stage=f"turn {index}", reason="; ".join(reading.errors)[:2000] or completed.stderr[:2000])
    return Completed()
