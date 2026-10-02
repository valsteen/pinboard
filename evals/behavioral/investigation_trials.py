"""Sequential, private Codex trials of registered fictional investigations.

Each arm receives the same world and human turns. Only its inquiry-home guidance differs.
The output home retains raw turns and a strict run record; it is never exported to the agent.
"""

import hashlib
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Annotated, Literal

import msgspec

from evals.behavioral import codex_driver, credentials, investigation, oneshot, processes, runner
from evals.behavioral.claude_driver import now
from evals.behavioral.layout import Layout
from evals.behavioral.records import (
    ExportRecord,
    InvestigationAssessmentRecord,
    InvestigationRunRecord,
    InvestigationTurnRecord,
    Sha256,
    write_new,
)
from evals.behavioral.spend import Budget, Category

ARMS = Path(__file__).parent / "data" / "investigation" / "arms"
READ_COMMAND = re.compile(
    r"\b(cat|sed|rg|head|tail)\b.*(inquiry(?:-[\w-]+)?\.md|notes(?:-[\w-]+)?\.md|evidence\.json)",
    re.IGNORECASE,
)


class Batch(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-investigation-batch/v1"]
    start_usd: Annotated[float, msgspec.Meta(ge=0)]
    cap_usd: Annotated[float, msgspec.Meta(ge=0)]


class PrivateKeys(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-investigation-private-registry/v1"]
    keys: dict[str, Sha256]


class Finding(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    claim: str
    status: Literal["observed", "inferred", "contradicted", "unknown"]
    source: str
    window: str


class Note(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["investigation-evidence/v1"]
    goal: str
    findings: list[Finding]
    unknowns: list[str]
    human_decisions: list[str]
    next_question: str
    last_turn: Annotated[int, msgspec.Meta(ge=1)]


def arm_text(arm: str) -> str:
    if arm == "ordinary":
        return ""
    return (ARMS / f"{arm}.md").read_text()


def saved_evidence_read(raw: str) -> bool:
    for line in raw.splitlines():
        if not line.startswith("{"):
            continue
        event = msgspec.json.decode(line.encode(), type=codex_driver.Event)
        if event.type != "item.completed":
            continue
        item = msgspec.json.decode(line.encode(), type=codex_driver.ItemEvent).item
        if item.type == "command_execution":
            command = msgspec.json.decode(line.encode(), type=codex_driver.CommandEvent).item
            if READ_COMMAND.search(command.command) and command.exit_code == 0:
                return True
    return False


def run(
    layout: Layout,
    budget: Budget,
    exported: ExportRecord,
    scenario_set: Path,
    case_id: str,
    arm: str,
    index: int,
    worlds: Path,
) -> InvestigationRunRecord | None:
    """Run one whole scripted case after a reservation; caller stages separate 15 USD batches."""
    if arm not in {"ordinary", "guidance", "structured"}:
        raise ValueError(f"unsupported investigation arm: {arm}")
    codex_driver.price("gpt-6-luna")
    runner.require_codex_world_location(worlds)
    _, cases = investigation.load_set(scenario_set)
    case = next((member for member in cases if member.id == case_id), None)
    if case is None:
        raise ValueError(f"case {case_id} is not registered")
    # Hold the existing cross-process lock before reserving or creating a run. An occupied lock leaves no
    # misleading unknown-cost record, and the explicit deadline prevents an indefinite setup wait.
    with credentials.exclusive_codex_session(
        Path(tempfile.gettempdir()), processes.Window(time.monotonic() + 60)
    ):
        projected = budget.reserve(Category.CODEX_AGENT_RUN)
        if projected is None:
            return None
        try:
            return _run_reserved(layout, budget, exported, scenario_set, case, arm, index, worlds)
        finally:
            budget.release(projected)


def _run_reserved(  # noqa: C901, PLR0912, PLR0915
    layout: Layout,
    budget: Budget,
    exported: ExportRecord,
    scenario_set: Path,
    case: investigation.InvestigationScenario,
    arm: str,
    index: int,
    worlds: Path,
) -> InvestigationRunRecord:
    directory = layout.root / "investigations" / case.id / f"{arm}-{index}"
    directory.mkdir(parents=True, exist_ok=False)
    world_root = worlds / f"{case.id}-{arm}-{index}"
    case_bytes = (investigation.DATA / "scenarios" / f"{case.id}.json").read_bytes()
    case_sha = hashlib.sha256(case_bytes).hexdigest()
    seed_root = worlds / "source-seeds" / f"{case.id}-{case_sha[:12]}"
    if not seed_root.exists():
        investigation.build_world(seed_root, case, budget.window)
        (seed_root / ".ready").write_text(case_sha)
    if (seed_root / ".ready").read_text() != case_sha:
        raise ValueError("source seed is incomplete or names another scenario")
    shutil.copytree(seed_root, world_root)
    inquiry = world_root / "inquiry"
    guide = arm_text(arm)
    if guide:
        (inquiry / "ARM.md").write_text(guide)
    arm_sha = hashlib.sha256(guide.encode()).hexdigest()
    turns: list[InvestigationTurnRecord] = []
    problem: str | None = None
    accounting = None
    version = codex_driver.codex_version(budget.window)
    started_at = now()
    try:
        with credentials.isolated_home(credentials.default_source(), None, budget.window) as home:
            codex_driver.write_config(
                home.path, Path(exported.plugin_root), "gpt-6-luna", "high", budget.window
            )
            context = codex_driver.loaded_context(home.path, inquiry, budget.window)
            (directory / "prompt-input.txt").write_text(context.prompt_text)
            findings = codex_driver.isolation_findings(context, home.path / "plugins" / "cache", inquiry)
            if findings:
                problem = "isolation: " + "; ".join(findings)
            else:
                thread_id: str | None = None
                previous: codex_driver.Usage | None = None
                for number, turn in enumerate(case.turns, start=1):
                    if turn.mode is not investigation.SessionMode.CONTINUE:
                        thread_id, previous = None, None
                    human = turn.human
                    if number == 1:
                        human += "\nRead evidence under ../sources. Save your inquiry notes under this directory."
                        if guide:
                            human += " Read ARM.md and follow it for this trial."
                    before = time.monotonic()
                    completed, reading = codex_driver.run_turn(
                        home.path, inquiry, thread_id, human, directory / f"turn-{number}.jsonl", budget.window
                    )
                    identity = reading.thread_id or thread_id or ""
                    own_usage = (
                        codex_driver.usage_since(previous, reading.usage)
                        if reading.usage is not None
                        else None
                    )
                    turns.append(
                        InvestigationTurnRecord(
                            index=number,
                            human=turn.human,
                            mode=turn.mode.value,
                            runtime_identity=identity,
                            final_reply=reading.messages[-1] if reading.messages else "",
                            commentary=reading.messages[:-1],
                            cost_usd=(
                                codex_driver.turn_cost("gpt-6-luna", own_usage)
                                if own_usage is not None
                                else None
                            ),
                            input_tokens=own_usage.input_tokens if own_usage is not None else None,
                            output_tokens=own_usage.output_tokens if own_usage is not None else None,
                            duration_seconds=time.monotonic() - before,
                            saved_evidence_read=(
                                saved_evidence_read(completed.stdout)
                                if turn.mode is investigation.SessionMode.FRESH
                                else False
                            ),
                            compaction_event=None,
                            record_valid=(
                                valid_note(inquiry / "evidence.json") if arm == "structured" else None
                            ),
                        )
                    )
                    thread_id, previous = identity, reading.usage
                    if not identity or completed.returncode != 0 or completed.timed_out or reading.usage is None:
                        problem = f"turn {number} failed or has incomplete usage"
                        break
                    if reading.errors or reading.mcp_approval_denied:
                        problem = f"turn {number}: {'; '.join(reading.errors) or 'approval denied'}"
                        break
            rollout = credentials.without_login(codex_driver.rollout_text(home.path), home.copied)
            (directory / "rollout.jsonl").write_text(rollout)
            accounting = codex_driver.rollout_accounting(
                rollout, "gpt-6-luna", problem is None and len(turns) == len(case.turns)
            )
            if accounting.reviewer_usage:
                problem = "unknown-priced reviewer usage"
            if not accounting.main_usage_complete:
                problem = problem or "primary usage accounting incomplete"
        if home.settlement is credentials.CredentialSettlement.REFUSED_SOURCE_CHANGED:
            problem = "Codex login changed during trial; credential write-back refused"
    except Exception as error:
        problem = str(error)
    record = InvestigationRunRecord(
        schema="pinboard-investigation-run/v1",
        case_id=case.id,
        scenario_sha256=case_sha,
        set_sha256=hashlib.sha256(scenario_set.read_bytes()).hexdigest(),
        arm=arm,
        arm_sha256=arm_sha,
        export_commit=exported.commit,
        model="gpt-6-luna",
        reasoning_effort="high",
        cli_version=version,
        started_at=started_at,
        finished_at=now(),
        turns=turns,
        accounting=accounting,
        outcome="completed" if problem is None else "failed",
        problem=problem,
    )
    write_new(directory / "run.json", record)
    return record


def valid_note(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        note = msgspec.json.decode(path.read_bytes(), type=Note)
    except msgspec.DecodeError:
        return False
    return note.last_turn > 0


def assess(
    layout: Layout,
    budget: Budget,
    scenario_set: Path,
    key_directory: Path,
    case_id: str,
    arm: str,
    index: int,
) -> InvestigationAssessmentRecord | None:
    """Blindly assess one completed trial against its private key; keep malformed usage blocking."""
    run_file = layout.root / "investigations" / case_id / f"{arm}-{index}" / "run.json"
    run = msgspec.json.decode(run_file.read_bytes(), type=InvestigationRunRecord)
    if run.outcome != "completed" or run.accounting is None or not run.accounting.main_usage_complete:
        raise ValueError("investigation run is incomplete")
    _, cases = investigation.load_set(scenario_set)
    case = next((member for member in cases if member.id == case_id), None)
    if case is None:
        raise ValueError(f"case {case_id} is not registered")
    if run.set_sha256 != hashlib.sha256(scenario_set.read_bytes()).hexdigest():
        raise ValueError("investigation run used another scenario registration")
    registry = msgspec.json.decode((key_directory / "registry.json").read_bytes(), type=PrivateKeys)
    key = investigation.load_key(key_directory / f"{case_id}.json", case, registry.keys[case_id])
    projected = budget.reserve(Category.SUBSTANCE_ASSESSMENT)
    if projected is None:
        return None
    directory = layout.root / "investigation-assessments" / case_id / f"{arm}-{index}"
    directory.mkdir(parents=True, exist_ok=False)
    answer_text = "\n\n".join(f"Turn {turn.index}: {turn.final_reply}" for turn in run.turns)
    prompt = investigation.assessment_prompt(case, key, answer_text)
    (directory / "prompt.txt").write_text(prompt)
    answer: oneshot.Answer | None = None
    problem: str | None = None
    try:
        answer = oneshot.ask(prompt, "claude-opus-5-5", budget.window)
        (directory / "raw.json").write_text(answer.stdout)
        if answer.cost_usd is None:
            problem = answer.problem or "assessor cost unknown"
        elif answer.problem is not None or answer.text is None:
            problem = answer.problem or "assessor returned no answer"
        else:
            block = oneshot.last_json_block(answer.text) or answer.text
            try:
                result = investigation.decode_assessment(block.encode(), case)
                write_new(directory / "assessment.json", result)
            except (msgspec.DecodeError, ValueError) as error:
                problem = str(error)
    except Exception as error:
        problem = str(error)
    finally:
        budget.release(projected)
        session = InvestigationAssessmentRecord(
            schema="pinboard-investigation-assessment-session/v1",
            case_id=case_id,
            arm=arm,
            index=index,
            assessor_model="claude-opus-5-5",
            cost_usd=answer.cost_usd if answer is not None else None,
            problem=problem,
        )
        write_new(directory / "session.json", session)
    return session
