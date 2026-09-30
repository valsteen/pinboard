"""Blind scoring of recorded runs with the frozen checklist.

A fresh-context scorer (``oneshot``) with the pinned ``SCORER_MODEL`` receives only the digest-verified checklist,
the scenario's ground truth and world facts, the redacted observed state and hooks log, and the human-visible
transcript under a random label. Runtime, skills revision and variant are withheld; local absolute paths are
redacted; the label-to-run map is stored apart from the scores. A malformed answer is recorded as a
``ScoringFailure`` and never counted as a score.
"""

import re
import secrets
from dataclasses import dataclass

import msgspec

from evals.behavioral import oneshot
from evals.behavioral.layout import Layout
from evals.behavioral.records import (
    LabelMapping,
    Redaction,
    Scenario,
    ScenarioId,
    Scored,
    ScoreRecord,
    ScorerInput,
    ScorerSession,
    ScoringFailure,
    ScoringOutcome,
    write_new,
)
from evals.behavioral.scenarios import CHECKLIST_SHA256, checklist_text, load_scenario, world_facts
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
        projected = self.budget.reserve(Category.SCORER)
        if projected is None:
            return None
        try:
            scenario = load_scenario(ScenarioId(source.run.scenario_id))
            label = f"T{secrets.token_hex(4)}"
            directory = self.layout.score_directory(label)
            directory.mkdir(parents=True)
            text = prompt(scenario, source, label)
            (directory / "prompt.txt").write_text(text)
            answer = oneshot.ask(text, SCORER_MODEL)
            (directory / "raw.json").write_text(answer.stdout)
            decoded = (
                decode_score(answer.text, label, len(scenario.turns))
                if answer.problem is None
                else ScoringFailure(reason=answer.problem)
            )
            outcome: ScoringOutcome
            match decoded:
                case ScoreRecord():
                    write_new(directory / "score.json", decoded)
                    outcome = Scored()
                case ScoringFailure():
                    outcome = decoded
                case _ as unreachable:
                    raise AssertionError(unreachable)
            write_new(
                directory / "session.json",
                ScorerSession(
                    schema="pinboard-behavioral-scorer-session/v1",
                    label=label,
                    scorer_model=SCORER_MODEL,
                    checklist_sha256=CHECKLIST_SHA256,
                    cost_usd=answer.cost_usd,
                    outcome=outcome,
                ),
            )
            write_new(
                self.layout.label_file(label),
                LabelMapping(schema="pinboard-behavioral-label/v1", label=label, run=source.run),
            )
            return outcome
        finally:
            self.budget.release(projected)


def valid_score_counts(layout: Layout) -> dict[str, int]:
    """Number of valid scores per run, keyed by the run's display name."""
    valid = {session.label for session in layout.scorer_sessions() if isinstance(session.outcome, Scored)}
    counts: dict[str, int] = {}
    for mapping in layout.labels():
        if mapping.label in valid:
            counts[mapping.run.display()] = counts.get(mapping.run.display(), 0) + 1
    return counts
