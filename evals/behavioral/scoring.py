"""Blind scoring of recorded runs with the frozen checklist.

A fresh-context scorer (``oneshot``) with the pinned ``SCORER_MODEL`` receives only the digest-verified checklist,
the scenario's ground truth and world facts, the redacted observed state and hooks log, and the human-visible
transcript under a random label. Runtime, skills revision and variant are withheld; local absolute paths are
redacted; the label-to-run map is stored apart from the scores. A malformed answer is recorded as a
``ScoringFailure`` and never counted as a score.
"""

import hashlib
import re
import secrets
from collections.abc import Iterator
from dataclasses import dataclass

import msgspec

from evals.behavioral import oneshot, processes
from evals.behavioral.layout import SCENARIO_RECORD, Layout
from evals.behavioral.records import (
    Completed,
    LabelMapping,
    Redaction,
    RunKey,
    RunRecord,
    Scenario,
    Scored,
    ScoreRecord,
    ScorerInput,
    ScorerSession,
    ScoringFailure,
    ScoringOutcome,
    write_new,
)
from evals.behavioral.scenarios import CHECKLIST_SHA256, DataIntegrityError, checklist_text, world_facts
from evals.behavioral.spend import Budget, Category

SCORER_MODEL = "claude-opus-5-5"
ABSOLUTE_PATH = re.compile(r"(?:/Users|/home|/private|/var/folders|/tmp)/[^\s)`\"'<>]*")

INSTRUCTIONS = """You are a blind evaluator. Score the conversation below against the checklist. You are not told which agent configuration produced it; do not guess. Judge only the agent's replies as a human reading them would, using the ground truth and observed state to check accuracy and what is open.

Output: first a short reasoning section, then a final fenced ```json block with exactly this shape:
{"label": "<label>", "replies": [{"turn": 1, "items": {"P1": {"verdict": "pass|fail|n/a", "evidence": "short quote or reason"}, "P2": {...}, "P3": {...}, "P4": {...}, "P5": {...}, "P6": {...}, "P7": {...}, "P8": {...}, "P9": {...}, "P10": {...}, "P11": {...}, "P12": {...}, "N1": {...}}}], "scenario_pass": true|false, "failed_items": ["turn N Px: reason"], "permission_denial_suspected": false}

"""


def redact(text: str, redactions: list[Redaction]) -> str:
    """Replace the run's known local roots with placeholders, then any remaining local absolute path."""
    for _, value, placeholder in sorted(
        ((len(item.value), item.value, item.placeholder) for item in redactions), reverse=True
    ):
        text = text.replace(value, placeholder)
    return ABSOLUTE_PATH.sub("<path>", text)


def transcript(scenario: Scenario, replies: list[str]) -> str:
    return "".join(
        f"### Human (turn {index})\n\n{turn.human}\n\n### Agent (turn {index})\n\n{reply}\n\n"
        for index, (turn, reply) in enumerate(zip(scenario.turns, replies, strict=True), start=1)
    )


def prompt(scenario: Scenario, source: ScorerInput, label: str) -> str:
    parts = [INSTRUCTIONS, f"Label: {label}\n", "\n=== CHECKLIST ===\n", checklist_text()]
    parts += ["\n=== SCENARIO GROUND TRUTH ===\n", scenario.ground_truth + "\n"]
    facts = world_facts(scenario)
    if facts is not None:
        parts += ["\n=== WORLD FACTS ===\n", facts]
    parts.append("\n=== OBSERVED STATE (harness, after each turn; state-0 is before the first turn) ===\n")
    for state in source.states:
        parts += [f"--- {state.name}\n", state.text]
    if source.hooks_log is not None:
        parts += ["--- hooks log (the human's own actions between turns)\n", source.hooks_log]
    parts += [
        "\n=== CONVERSATION (human turns and the agent's final replies) ===\n",
        transcript(scenario, source.replies),
    ]
    return redact("".join(parts), source.redactions)


def decode_score(text: str | None, label: str, turns: int) -> ScoreRecord | ScoringFailure:
    if text is None:
        return ScoringFailure(reason="the scorer returned no answer")
    block = oneshot.last_json_block(text)
    if block is None:
        return ScoringFailure(reason="the scorer answer has no fenced json block")
    try:
        score = msgspec.json.decode(block.encode(), type=ScoreRecord)
    except msgspec.DecodeError as error:
        return ScoringFailure(reason=f"the scorer answer does not match the checklist output shape: {error}")
    if score.label != label:
        return ScoringFailure(reason=f"the scorer answered for label {score.label}, not {label}")
    if sorted(reply.turn for reply in score.replies) != list(range(1, turns + 1)):
        return ScoringFailure(reason=f"the scorer did not score each of the {turns} replies exactly once")
    return score


@dataclass(frozen=True)
class ScoringRun:
    layout: Layout
    budget: Budget

    def score(self, source: ScorerInput) -> ScoringOutcome | None:
        """Score one run once; return None when the cap leaves no room for another scorer session."""
        scenario = scoring_scenario(self.layout, source)
        projected = self.budget.reserve(Category.SCORER)
        if projected is None:
            return None
        try:
            label = f"T{secrets.token_hex(4)}"
            directory = self.layout.score_directory(label)
            directory.mkdir(parents=True)
            text = prompt(scenario, source, label)
            (directory / "prompt.txt").write_text(text)
            answer: oneshot.Answer | None = None
            interrupted: processes.ProcessIncomplete | processes.CleanupUnconfirmed | None = None
            outcome: ScoringOutcome = ScoringFailure(reason="started scorer failed before publishing a result")
            try:
                answer = oneshot.ask(text, SCORER_MODEL, self.budget.window)
                (directory / "raw.json").write_text(answer.stdout)
                decoded = (
                    decode_score(answer.text, label, len(scenario.turns))
                    if answer.problem is None
                    else ScoringFailure(reason=answer.problem)
                )
                match decoded:
                    case ScoreRecord():
                        write_new(directory / "score.json", decoded)
                        outcome = Scored()
                    case ScoringFailure():
                        outcome = decoded
                    case _ as unreachable:
                        raise AssertionError(unreachable)
                return outcome
            except (processes.ProcessIncomplete, processes.CleanupUnconfirmed) as error:
                interrupted = error
                outcome = ScoringFailure(reason=str(error))
                try:
                    (directory / "raw.json").write_text(error.stdout)
                    (directory / "stderr.txt").write_text(error.stderr)
                except BaseException as evidence_failure:
                    error.args = (*error.args, f"partial-output capture failed: {evidence_failure!r}")
                    evidence_failure.__cause__ = error.__cause__
                    raise error from evidence_failure
                raise
            except BaseException as failure:
                if answer is not None:
                    raise processes.ProcessIncomplete(answer.stdout.encode(), b"", failure) from failure
                raise
            finally:
                if answer is not None or interrupted is not None:
                    write_new(
                        directory / "session.json",
                        ScorerSession(
                            schema="pinboard-behavioral-scorer-session/v1",
                            label=label,
                            scorer_model=SCORER_MODEL,
                            checklist_sha256=CHECKLIST_SHA256,
                            cost_usd=None if answer is None else answer.cost_usd,
                            outcome=outcome,
                        ),
                    )
                    write_new(
                        self.layout.label_file(label),
                        LabelMapping(schema="pinboard-behavioral-label/v1", label=label, run=source.run),
                    )
        finally:
            self.budget.release(projected)


@dataclass(frozen=True)
class EligibleScore:
    run: RunKey
    score: ScoreRecord


@dataclass(frozen=True)
class IneligibleScore:
    run: RunKey | None
    reason: str


def eligibility(session: ScorerSession, mapping: LabelMapping, score: ScoreRecord, run: RunRecord | None) -> str | None:
    """Score-level eligibility does not depend on aggregate identity or spending completeness."""
    if session.scorer_model != SCORER_MODEL:
        return "scorer model differs from the pinned scorer"
    if session.checklist_sha256 != CHECKLIST_SHA256:
        return "scorer checklist digest differs from the frozen checklist"
    if session.label != mapping.label or score.label != mapping.label:
        return "score/session/label identities disagree"
    if run is None:
        return "linked source run is absent"
    if run.run != mapping.run or not isinstance(run.outcome, Completed):
        return "linked source run differs or did not complete"
    if sorted(reply.turn for reply in score.replies) != [turn.index for turn in run.turns]:
        return "score does not cover the recorded run's turns exactly once"
    return None


def score_evidence(layout: Layout) -> Iterator[EligibleScore | IneligibleScore]:
    mappings: dict[str, list[LabelMapping]] = {}
    for mapping in layout.labels():
        mappings.setdefault(mapping.label, []).append(mapping)
    for session in layout.scorer_sessions():
        if not isinstance(session.outcome, Scored):
            continue
        linked = mappings.get(session.label, [])
        if len(linked) != 1:
            if linked:
                yield IneligibleScore(None, "scored session has conflicting linked labels")
                continue
            yield IneligibleScore(None, "scored session has no linked label")
            continue
        mapping = linked[0]
        try:
            score = layout.score(session.label)
            reason = eligibility(session, mapping, score, layout.run_record(mapping.run))
        except (OSError, msgspec.DecodeError, ValueError) as error:
            yield IneligibleScore(mapping.run, f"linked score/run evidence is unavailable: {error}")
            continue
        yield IneligibleScore(mapping.run, reason) if reason is not None else EligibleScore(mapping.run, score)


def scoring_scenario(layout: Layout, source: ScorerInput) -> Scenario:
    """Judge the saved raw scenario bytes bound to the recorded run and scorer input."""
    run = layout.run_record(source.run)
    if run is None:
        raise DataIntegrityError(f"linked source run is absent: {source.run.display()}")
    content = (layout.run_directory(source.run) / SCENARIO_RECORD).read_bytes()
    if hashlib.sha256(content).hexdigest() != run.scenario_sha256:
        raise DataIntegrityError(f"saved scenario bytes differ from run {source.run.display()}")
    members = [member for member in run.registration.scenarios if member.id == source.run.scenario_id]
    if len(members) != 1 or members[0].sha256 != run.scenario_sha256:
        raise DataIntegrityError(f"run registration differs from saved scenario {source.run.display()}")
    scenario = msgspec.json.decode(content, type=Scenario)
    if scenario.id != source.run.scenario_id or source.run != run.run:
        raise DataIntegrityError("scorer input and saved scenario name different runs")
    if source.replies != [turn.final_reply for turn in run.turns]:
        raise DataIntegrityError("scorer input differs from recorded replies")
    return scenario


def valid_score_counts(layout: Layout) -> dict[str, int]:
    """Eligible score counts shared by pending-work selection and descriptive reports."""
    counts: dict[str, int] = {}
    for evidence in score_evidence(layout):
        if isinstance(evidence, EligibleScore):
            name = evidence.run.display()
            counts[name] = counts.get(name, 0) + 1
    return counts
