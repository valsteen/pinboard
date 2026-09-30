"""Blind assessment of whether each Codex turn's substance reaches its final reply or stays in progress commentary.

This assessment is separate from checklist scoring: it never reads the checklist and never changes a score. A
fresh-context assessor (``oneshot``) sees, under a random label, each human turn with the agent's progress
commentary and final reply, redacted of local paths, and answers per turn. Word counts are measured mechanically.
"""

import secrets
from collections import defaultdict
from pathlib import Path

import msgspec

from evals.behavioral import oneshot, processes
from evals.behavioral.layout import Layout
from evals.behavioral.records import (
    Assessed,
    AssessmentFailure,
    AssessmentOutcome,
    AssessmentRecord,
    Failed,
    RunRecord,
    Runtime,
    SubstanceAnswer,
    SubstanceVerdict,
    TurnWords,
    write_new,
)
from evals.behavioral.scoring import ABSOLUTE_PATH
from evals.behavioral.spend import Budget, Category

ASSESSOR_MODEL = "claude-opus-5-5"

INSTRUCTIONS = """You are a blind assessor. For each turn of the conversation below you see the human's message, the agent's progress commentary (messages it wrote while working, before its final reply) and the agent's final reply. A human often reads only the final reply.

For each turn decide whether the turn's substantive content - facts found, conclusions, answers, decisions or questions the human needs - reaches the final reply ("final-reply-carries-substance"), or whether some of it appears only in the progress commentary and is missing from the final reply ("substance-only-in-commentary"). Routine narration of what the agent is about to do is not substance. A turn with no commentary carries its substance in the final reply.

Output: first a short reasoning section, then a final fenced ```json block with exactly this shape:
{"label": "<label>", "turns": [{"turn": 1, "verdict": "final-reply-carries-substance|substance-only-in-commentary", "evidence": "short quote of the substance and where it appears"}]}

"""


def words(text: str) -> int:
    return len(text.split())


def prompt(record: RunRecord, label: str) -> str:
    parts = [INSTRUCTIONS, f"Label: {label}\n"]
    for turn in record.turns:
        commentary = "\n\n".join(f"[commentary {n}] {text}" for n, text in enumerate(turn.commentary, start=1))
        parts.append(
            f"\n=== TURN {turn.index} ===\n### Human\n\n{turn.human}\n\n### Progress commentary\n\n"
            f"{commentary or '(none)'}\n\n### Final reply\n\n{turn.final_reply}\n"
        )
    return ABSOLUTE_PATH.sub("<path>", "".join(parts))


def decode_answer(text: str | None, label: str, turns: int) -> SubstanceAnswer | AssessmentFailure:
    if text is None:
        return AssessmentFailure(reason="the assessor returned no answer")
    block = oneshot.last_json_block(text)
    if block is None:
        return AssessmentFailure(reason="the assessor answer has no fenced json block")
    try:
        answer = msgspec.json.decode(block.encode(), type=SubstanceAnswer)
    except msgspec.DecodeError as error:
        return AssessmentFailure(reason=f"the assessor answer does not match its output shape: {error}")
    if answer.label != label or sorted(turn.turn for turn in answer.turns) != list(range(1, turns + 1)):
        return AssessmentFailure(reason="the assessor answer does not cover each turn of this label exactly once")
    return answer


def assess(layout: Layout, budget: Budget) -> list[str]:
    """Assess every completed or stopped Codex run with at least one recorded turn and no assessment yet.

    A run stopped for a human decision is not scored, but its recorded turns still show where its substance went.
    Return the runs skipped by the cap.
    """
    skipped = []
    for record in layout.run_records():
        if record.runtime is not Runtime.CODEX or isinstance(record.outcome, Failed) or not record.turns:
            continue
        directory = layout.assessment_directory(record.run)
        if directory.exists():
            continue
        projected = budget.reserve(Category.SUBSTANCE_ASSESSMENT)
        if projected is None:
            skipped.append(record.run.display())
            continue
        try:
            assess_run(record, directory, budget.window)
        finally:
            budget.release(projected)
    return skipped


def assess_run(record: RunRecord, directory: Path, window: processes.Window) -> None:
    label = f"S{secrets.token_hex(4)}"
    directory.mkdir(parents=True)
    text = prompt(record, label)
    (directory / "prompt.txt").write_text(text)
    answer = oneshot.ask(text, ASSESSOR_MODEL, window)
    (directory / "raw.json").write_text(answer.stdout)
    decoded = (
        decode_answer(answer.text, label, len(record.turns))
        if answer.problem is None
        else AssessmentFailure(reason=answer.problem)
    )
    write_new(
        directory / "assessment.json",
        AssessmentRecord(
            schema="pinboard-behavioral-substance/v1",
            run=record.run,
            assessor_model=ASSESSOR_MODEL,
            cost_usd=answer.cost_usd,
            words=turn_words(record),
            outcome=outcome_of(decoded),
        ),
    )


def summarize(layout: Layout) -> str:
    by_scenario: dict[str, list[AssessmentRecord]] = defaultdict(list)
    for record in layout.assessments():
        by_scenario[record.run.scenario_id].append(record)
    lines = ["scenario\tverdict\truns\tturns only in commentary\tcommentary words\tfinal reply words\tevidence"]
    for scenario, records in sorted(by_scenario.items()):
        lines.append(scenario_line(scenario, records))
    return "\n".join(lines) + "\n"


def scenario_line(scenario: str, records: list[AssessmentRecord]) -> str:
    only, turns = 0, 0
    quotes: list[str] = []
    examples: list[str] = []
    for record in records:
        match record.outcome:
            case Assessed(answer=answer):
                for turn in answer.turns:
                    turns += 1
                    quote = f"{record.run.display()} turn {turn.turn}: {turn.evidence}"
                    if turn.verdict is SubstanceVerdict.SUBSTANCE_ONLY_IN_COMMENTARY:
                        only += 1
                        quotes.append(quote)
                    else:
                        examples.append(quote)
            case AssessmentFailure(reason=reason):
                quotes.append(f"{record.run.display()}: assessment failed: {reason}")
            case _ as unreachable:
                raise AssertionError(unreachable)
    commentary = sum(entry.commentary_words for record in records for entry in record.words)
    final = sum(entry.final_reply_words for record in records for entry in record.words)
    verdict = SubstanceVerdict.SUBSTANCE_ONLY_IN_COMMENTARY if only else SubstanceVerdict.FINAL_REPLY_CARRIES_SUBSTANCE
    evidence = quotes or examples[:1]
    return f"{scenario}\t{verdict.value}\t{len(records)}\t{only}/{turns}\t{commentary}\t{final}\t{' | '.join(evidence)}"


def outcome_of(answer: SubstanceAnswer | AssessmentFailure) -> AssessmentOutcome:
    match answer:
        case SubstanceAnswer():
            return Assessed(answer=answer)
        case AssessmentFailure():
            return answer
        case _ as unreachable:
            raise AssertionError(unreachable)


def turn_words(record: RunRecord) -> list[TurnWords]:
    return [
        TurnWords(
            turn=turn.index,
            commentary_words=sum(words(text) for text in turn.commentary),
            final_reply_words=words(turn.final_reply),
        )
        for turn in record.turns
    ]
