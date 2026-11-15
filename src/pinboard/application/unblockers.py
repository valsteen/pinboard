"""What held work waits for: its strict Pinboard-owned records and the one plain hold sentence.

Every holding transition records exactly one unblocker. The records here are the request, receipt, stored-state
and item-status shape of that unblocker; ``describe_hold`` is the only owner of the sentence every surface shows
for held work. The sentence is derived from snapshot facts alone, so generated views stay re-derivable: a held
attempt reads as started with nobody working on it now because it is held, never from lease liveness or a clock.
These functions perform no reads or writes.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Annotated, Literal, assert_never

import msgspec

from pinboard.application import stored_state
from pinboard.domain import work_models
from pinboard.domain.history import CanonicalLine
from pinboard.domain.identifiers import WorkItemId

type Identity = Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]


class ItemsToFinishPayload(msgspec.Struct, tag="items", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    """Named accepted dependencies whose completion Pinboard detects."""

    items: Annotated[tuple[Identity, ...], msgspec.Meta(min_length=1)]

    def __post_init__(self) -> None:
        if len(set(self.items)) != len(self.items):
            raise ValueError("Waited-for items must be unique.")


class HumanDecisionPayload(
    msgspec.Struct, tag="human-decision", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    """The question a human must answer before the work continues."""

    question: CanonicalLine


class OutsideConditionPayload(
    msgspec.Struct, tag="outside-condition", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    """One sentence saying what must become true outside Pinboard; Pinboard does not check it."""

    condition: CanonicalLine


type UnblockerPayload = ItemsToFinishPayload | HumanDecisionPayload | OutsideConditionPayload


class HoldReceiptInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Receipt input of block and block-item, stored as pinboard-hold/v1."""

    schema: Literal["pinboard-hold/v1"]
    unblocker: UnblockerPayload


class PlainPauseReceiptInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Receipt input of a plain pause, stored as pinboard-plain-pause/v1."""

    schema: Literal["pinboard-plain-pause/v1"]
    reason: Annotated[str, msgspec.Meta(min_length=1)]
    unblocker: UnblockerPayload


HOLD_RECEIPT_SCHEMA = "pinboard-hold/v1"
PLAIN_PAUSE_RECEIPT_SCHEMA = "pinboard-plain-pause/v1"


def unblocker_from_payload(payload: UnblockerPayload) -> work_models.Unblocker:
    match payload:
        case ItemsToFinishPayload(items=items):
            return work_models.WaitingForItemsToFinish(tuple(WorkItemId(value) for value in items))
        case HumanDecisionPayload(question=question):
            return work_models.WaitingForHumanDecision(question)
        case OutsideConditionPayload(condition=condition):
            return work_models.WaitingForOutsideCondition(condition)
        case _ as unreachable:
            assert_never(unreachable)


def unblocker_payload(unblocker: work_models.Unblocker) -> UnblockerPayload:
    match unblocker:
        case work_models.WaitingForItemsToFinish(items=items):
            return ItemsToFinishPayload(tuple(str(value) for value in items))
        case work_models.WaitingForHumanDecision(question=question):
            return HumanDecisionPayload(question)
        case work_models.WaitingForOutsideCondition(condition=condition):
            return OutsideConditionPayload(condition)
        case _ as unreachable:
            assert_never(unreachable)


def encode_unblocker(unblocker: work_models.Unblocker) -> bytes:
    """Encode the canonical stored form of one current unblocker."""

    return msgspec.json.encode(unblocker_payload(unblocker), order="sorted")


def decode_unblocker(value: bytes | str) -> work_models.Unblocker:
    """Decode one stored unblocker once into the domain union; malformed bytes raise msgspec errors."""

    return unblocker_from_payload(msgspec.json.decode(value, type=UnblockerPayload, strict=True))


def waited_items(unblocker: work_models.Unblocker) -> tuple[WorkItemId, ...]:
    match unblocker:
        case work_models.WaitingForItemsToFinish(items=items):
            return items
        case work_models.WaitingForHumanDecision() | work_models.WaitingForOutsideCondition():
            return ()
        case _ as unreachable:
            assert_never(unreachable)


def unresolved_replacement_item(
    replacement: work_models.PlannedReplacement | None,
    disposition: work_models.ReplacementDisposition | None,
) -> WorkItemId | None:
    """Name the replacement item while a current planned replacement still withholds resume."""

    if replacement is None or not work_models.replacement_withholds_resume(
        replacement.status, replacement.replacement_cost, None if disposition is None else disposition.accepted_cost
    ):
        return None
    return replacement.replacement_item


def stored_unresolved_replacements(records: stored_state.ReplacementRecords) -> dict[WorkItemId, WorkItemId]:
    """Map each item to its replacement while its latest planned replacement still withholds resume."""

    current: dict[WorkItemId, stored_state.StoredPlannedReplacement] = {}
    for relation in records.planned_replacements:
        latest = current.get(relation.affected_item_id)
        if latest is None or latest.relation_revision < relation.relation_revision:
            current[relation.affected_item_id] = relation
    accepted = {
        (value.affected_item_id, value.relation_revision): value.accepted_cost for value in records.dispositions
    }
    return {
        item_id: relation.replacement_item_id
        for item_id, relation in current.items()
        if work_models.replacement_withholds_resume(
            relation.status, relation.replacement_cost, accepted.get((item_id, relation.relation_revision))
        )
    }


@dataclass(frozen=True, slots=True)
class DependencyCondition:
    """One accepted dependency of the held item and what it is now."""

    target: work_models.Dependency
    fate: work_models.DependencyFate


@dataclass(frozen=True, slots=True)
class NoLongerADependency:
    """An item the hold named that the item's current definition no longer depends on, whatever its state."""

    item_id: WorkItemId


type NamedItemFate = DependencyCondition | NoLongerADependency


@dataclass(frozen=True, slots=True)
class HoldFacts:
    """Snapshot facts the hold sentence is derived from.

    ``started`` is true when the item has a live attempt. ``dependencies`` are the item's current accepted
    dependencies with their fates; ``unresolved_replacement`` names the replacement item of an unresolved planned
    replacement.
    """

    started: bool
    unblocker: work_models.Unblocker
    dependencies: tuple[DependencyCondition, ...]
    unresolved_replacement: WorkItemId | None


@dataclass(frozen=True, slots=True)
class SentenceText:
    text: str


@dataclass(frozen=True, slots=True)
class SentenceItem:
    """A waited-for Pinboard item that every surface links."""

    item_id: WorkItemId


type SentencePart = SentenceText | SentenceItem


@dataclass(frozen=True, slots=True)
class HoldDescription:
    """The plain hold sentence and the facts an agent needs beside it.

    ``resume_needed`` is true when nothing an items hold names is still awaited and resume would be offered: the
    work can continue once someone resumes it. Human-decision and outside-condition holds never resolve on their
    own, so they never set it.
    """

    unblocker: work_models.Unblocker
    resume_needed: bool
    parts: tuple[SentencePart, ...]


def _joined(phrases: tuple[tuple[SentencePart, ...], ...]) -> tuple[SentencePart, ...]:
    parts: list[SentencePart] = []
    for index, phrase in enumerate(phrases):
        if index:
            parts.append(SentenceText(" and " if index == len(phrases) - 1 else ", "))
        parts.extend(phrase)
    return tuple(parts)


def _item_list(items: tuple[WorkItemId, ...]) -> tuple[SentencePart, ...]:
    return _joined(tuple((SentenceItem(item),) for item in items))


def _awaiting(fate: work_models.DependencyFate) -> bool:
    match fate:
        case work_models.DependencyFate.AWAITING_ITEM | work_models.DependencyFate.AWAITING_CHECKPOINT:
            return True
        case (
            work_models.DependencyFate.ITEM_FINISHED
            | work_models.DependencyFate.CHECKPOINT_ACCEPTED
            | work_models.DependencyFate.ITEM_CANCELLED
        ):
            return False
        case _ as unreachable:
            assert_never(unreachable)


def _target(target: work_models.Dependency, *, capitalized: bool) -> tuple[SentencePart, ...]:
    match target:
        case str():
            return (SentenceItem(target),)
        case work_models.CheckpointDependency(item_id=item_id, checkpoint_id=checkpoint_id):
            return (SentenceText(f"{'C' if capitalized else 'c'}heckpoint {checkpoint_id} of "), SentenceItem(item_id))
        case _ as unreachable:
            assert_never(unreachable)


def _awaited(target: work_models.Dependency) -> tuple[SentencePart, ...]:
    """What remains for one unsatisfied live dependency: its item to finish or its checkpoint to be accepted."""

    match target:
        case str():
            return (*_target(target, capitalized=False), SentenceText(" to finish"))
        case work_models.CheckpointDependency():
            return (*_target(target, capitalized=False), SentenceText(" to be accepted"))
        case _ as unreachable:
            assert_never(unreachable)


def _statement(value: NamedItemFate) -> tuple[SentencePart, ...]:
    """One sentence, led by a space, stating exactly what is true of one dependency or named item now."""

    match value:
        case NoLongerADependency(item_id=item_id):
            return (
                SentenceText(" "),
                SentenceItem(item_id),
                SentenceText(" is no longer one of this item's dependencies."),
            )
        case DependencyCondition(target=target, fate=fate):
            match fate:
                case work_models.DependencyFate.AWAITING_ITEM | work_models.DependencyFate.AWAITING_CHECKPOINT:
                    return (SentenceText(" It still waits for "), *_awaited(target), SentenceText("."))
                case work_models.DependencyFate.ITEM_FINISHED:
                    item = work_models.dependency_item(target)
                    return (SentenceText(" "), SentenceItem(item), SentenceText(" has finished."))
                case work_models.DependencyFate.CHECKPOINT_ACCEPTED:
                    return (SentenceText(" "), *_target(target, capitalized=True), SentenceText(" was accepted."))
                case work_models.DependencyFate.ITEM_CANCELLED:
                    item = work_models.dependency_item(target)
                    return (
                        SentenceText(" "),
                        SentenceItem(item),
                        SentenceText(
                            " was cancelled and will not finish, so this item's dependencies need revising before it can resume."
                        ),
                    )
                case _ as unreachable:
                    assert_never(unreachable)
        case _ as unreachable:
            assert_never(unreachable)


def _remaining(
    awaited: tuple[DependencyCondition, ...], replacement: WorkItemId | None
) -> tuple[tuple[SentencePart, ...], ...]:
    phrases = tuple(_awaited(condition.target) for condition in awaited)
    if replacement is None:
        return phrases
    return (
        *phrases,
        (SentenceText("its planned replacement by "), SentenceItem(replacement), SentenceText(" to be resolved")),
    )


def _withheld(
    unsatisfied: tuple[DependencyCondition, ...], replacement: WorkItemId | None, lead: str
) -> tuple[SentencePart, ...]:
    """Say what else withholds resume: dependencies still to finish or be accepted, cancelled ones, a replacement."""

    remaining = _remaining(tuple(value for value in unsatisfied if _awaiting(value.fate)), replacement)
    parts: list[SentencePart] = []
    if remaining:
        parts.extend((SentenceText(lead), *_joined(remaining), SentenceText(".")))
    for value in unsatisfied:
        if value.fate == work_models.DependencyFate.ITEM_CANCELLED:
            parts.extend(_statement(value))
    return tuple(parts)


def _named(items: tuple[WorkItemId, ...], dependencies: tuple[DependencyCondition, ...]) -> tuple[NamedItemFate, ...]:
    """Classify each named item: its accepted dependency (an unsatisfied one first) or no longer a dependency."""

    named: list[NamedItemFate] = []
    for item in items:
        matching = tuple(value for value in dependencies if work_models.dependency_item(value.target) == item)
        if not matching:
            named.append(NoLongerADependency(item))
        else:
            named.append(next((value for value in matching if not value.fate.satisfied), matching[0]))
    return tuple(named)


def _is_awaited(value: NamedItemFate) -> bool:
    match value:
        case DependencyCondition(fate=fate):
            return _awaiting(fate)
        case NoLongerADependency():
            return False
        case _ as unreachable:
            assert_never(unreachable)


def _finished(value: NamedItemFate) -> bool:
    return isinstance(value, DependencyCondition) and value.fate == work_models.DependencyFate.ITEM_FINISHED


def _punctuated(text: str) -> str:
    return text if text.endswith((".", "?", "!")) else f"{text}."


def describe_hold(facts: HoldFacts) -> HoldDescription:
    """Say in plain words whether work started, whether anyone works on it now, and what it waits for.

    Each item an items hold names is described by its actual fate: still to finish, a checkpoint still to be
    accepted, finished, its checkpoint accepted, cancelled, or no longer one of the item's dependencies. The
    sentence says someone can resume the work only when the shared resume predicate would offer it; otherwise it
    names what still withholds resume instead of presenting dependency order as the hold.
    """

    opening = SentenceText(
        "Work started, but nobody is working on it now because it is on hold. "
        if facts.started
        else "Work has not started. "
    )
    unsatisfied = tuple(value for value in facts.dependencies if not value.fate.satisfied)
    resume_available = work_models.resume_available(
        tuple(value.target for value in unsatisfied), facts.unresolved_replacement is None
    )
    match facts.unblocker:
        case work_models.WaitingForItemsToFinish(items=items):
            named = _named(items, facts.dependencies)
            others = tuple(value for value in unsatisfied if work_models.dependency_item(value.target) not in items)
            awaited = tuple(
                value for value in named if isinstance(value, DependencyCondition) and _awaiting(value.fate)
            )
            settled = tuple(value for value in named if not _is_awaited(value))
            statements = tuple(part for value in settled for part in _statement(value))
            if awaited:
                return HoldDescription(
                    facts.unblocker,
                    False,
                    (
                        opening,
                        SentenceText("It waits for "),
                        *_joined(tuple(_awaited(value.target) for value in awaited)),
                        SentenceText("."),
                        *statements,
                        *_withheld(others, facts.unresolved_replacement, " Resuming also waits for "),
                    ),
                )
            if all(_finished(value) for value in named):
                waited = (
                    opening,
                    SentenceText("It waited for "),
                    *_item_list(items),
                    SentenceText(", which has finished" if len(items) == 1 else ", which have all finished"),
                )
                if resume_available:
                    return HoldDescription(
                        facts.unblocker,
                        True,
                        (*waited, SentenceText(", so it can continue once someone resumes it.")),
                    )
                remaining = _remaining(
                    tuple(value for value in others if _awaiting(value.fate)), facts.unresolved_replacement
                )
                return HoldDescription(
                    facts.unblocker,
                    False,
                    (
                        *waited,
                        *((SentenceText(", but it still waits for "), *_joined(remaining)) if remaining else ()),
                        SentenceText("."),
                        *(
                            part
                            for value in others
                            if value.fate == work_models.DependencyFate.ITEM_CANCELLED
                            for part in _statement(value)
                        ),
                    ),
                )
            waited_parts = (opening, SentenceText("It waited for "), *_item_list(items), SentenceText("."), *statements)
            if resume_available:
                return HoldDescription(
                    facts.unblocker,
                    True,
                    (*waited_parts, SentenceText(" It can continue once someone resumes it.")),
                )
            return HoldDescription(
                facts.unblocker,
                False,
                (*waited_parts, *_withheld(others, facts.unresolved_replacement, " Resuming still waits for ")),
            )
        case work_models.WaitingForHumanDecision(question=question):
            body: tuple[SentencePart, ...] = (
                opening,
                SentenceText(
                    f"It waits for a human decision: {_punctuated(question)} Someone resumes it after that decision."
                ),
            )
        case work_models.WaitingForOutsideCondition(condition=condition):
            body = (
                opening,
                SentenceText(
                    f"It waits for this outside condition: {_punctuated(condition)} Pinboard does not check it; "
                    "someone resumes the work once it holds."
                ),
            )
        case _ as unreachable:
            assert_never(unreachable)
    if resume_available:
        return HoldDescription(facts.unblocker, False, body)
    return HoldDescription(
        facts.unblocker,
        False,
        (*body, *_withheld(unsatisfied, facts.unresolved_replacement, " Resuming also waits for ")),
    )


def render_sentence(
    parts: Iterable[SentencePart], text: Callable[[str], str], item: Callable[[WorkItemId], str]
) -> str:
    """Render the one hold sentence for a surface that supplies its own text escaping and item links."""

    rendered: list[str] = []
    for part in parts:
        match part:
            case SentenceText(text=value):
                rendered.append(text(value))
            case SentenceItem(item_id=item_id):
                rendered.append(item(item_id))
            case _ as unreachable:
                assert_never(unreachable)
    return "".join(rendered)


def sentence_text_identity(value: str) -> str:
    """Leave sentence text unchanged for surfaces that need no escaping."""

    return value


def sentence_text(parts: Iterable[SentencePart]) -> str:
    """Render the sentence as plain text, naming each waited-for item by its identity."""

    return render_sentence(parts, sentence_text_identity, str)


def sentence_items(parts: Iterable[SentencePart]) -> tuple[WorkItemId, ...]:
    return tuple(dict.fromkeys(part.item_id for part in parts if isinstance(part, SentenceItem)))


class HoldStatus(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Item-status facts for held work: the plain sentence, its structured unblocker and linked items."""

    sentence: str
    unblocker: UnblockerPayload
    linked_items: tuple[str, ...]
    resume_needed: bool
    """True when nothing the hold names is still awaited and resume would be offered: someone needs to resume it."""


def hold_status(description: HoldDescription) -> HoldStatus:
    return HoldStatus(
        sentence_text(description.parts),
        unblocker_payload(description.unblocker),
        tuple(str(item) for item in sentence_items(description.parts)),
        description.resume_needed,
    )
