"""Initiative history and pure change decisions; item lifecycle stays item-owned."""

from dataclasses import dataclass
from datetime import date as HumanDate
from datetime import datetime
from enum import Enum
from typing import assert_never

from pinboard.domain.errors import DescribedCode
from pinboard.domain.identifiers import HostId, InitiativeId, TaskId, WorkItemId


class RejectionCode(DescribedCode):
    INITIATIVE_EXISTS = ("INITIATIVE_EXISTS", "The initiative already has a record at the selected identity.")
    INITIATIVE_NOT_FOUND = ("INITIATIVE_NOT_FOUND", "The selected initiative has no record.")
    INITIATIVE_STALE = ("INITIATIVE_STALE", "The supplied initiative subject revision differs from current state.")
    ITEM_NOT_FOUND = ("ITEM_NOT_FOUND", "The work item has no record at the requested identity.")
    MEMBERSHIP_EXISTS = ("MEMBERSHIP_EXISTS", "The selected initiative and item already have an active membership.")
    MEMBERSHIP_ABSENT = ("MEMBERSHIP_ABSENT", "The selected initiative and item have no active membership to end.")
    ASSESSMENT_CONTEXT_STALE = (
        "ASSESSMENT_CONTEXT_STALE",
        "The supplied evaluation context differs from the current goal and success signal.",
    )
    INITIATIVE_INPUT_INVALID = (
        "INITIATIVE_INPUT_INVALID",
        "The initiative request does not match a supported strict operation leaf.",
    )
    INITIATIVE_ROOT_INVALID = (
        "INITIATIVE_ROOT_INVALID",
        "The selected initiative project or initialized work root is unavailable.",
    )


class Judgment(Enum):
    MET = "met"
    NOT_MET = "not-met"


@dataclass(frozen=True, slots=True)
class Definition:
    title: str
    goal: str
    rationale: str
    success_signal: str
    project_references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HumanReason:
    human: str
    date: HumanDate
    reason: str


@dataclass(frozen=True, slots=True)
class Provenance:
    reason: HumanReason
    task_id: TaskId
    host_id: HostId
    committed_at: datetime
    project_revision: int


@dataclass(frozen=True, slots=True)
class Revision:
    initiative_id: InitiativeId
    revision: int
    evaluation_context: int
    definition: Definition
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class Membership:
    initiative_id: InitiativeId
    item_id: WorkItemId
    episode: int
    added: Provenance
    removed: Provenance | None


@dataclass(frozen=True, slots=True)
class Decision:
    initiative_id: InitiativeId
    decision: str
    supporting_links: tuple[str, ...]
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class Assessment:
    initiative_id: InitiativeId
    evaluation_context: int
    evaluated_goal: str
    evaluated_signal: str
    judgment: Judgment
    supporting_links: tuple[str, ...]
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class Initiative:
    initiative_id: InitiativeId
    subject_revision: int
    revisions: tuple[Revision, *tuple[Revision, ...]]
    memberships: tuple[Membership, ...]
    decisions: tuple[Decision, ...]
    assessments: tuple[Assessment, ...]


@dataclass(frozen=True, slots=True)
class Create:
    definition: Definition


@dataclass(frozen=True, slots=True)
class Revise:
    expected_revision: int
    definition: Definition


@dataclass(frozen=True, slots=True)
class AddMember:
    expected_revision: int
    item_id: WorkItemId


@dataclass(frozen=True, slots=True)
class RemoveMember:
    expected_revision: int
    item_id: WorkItemId


@dataclass(frozen=True, slots=True)
class RecordDecision:
    expected_revision: int
    decision: str
    supporting_links: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Assess:
    expected_revision: int
    evaluation_context: int
    judgment: Judgment
    supporting_links: tuple[str, ...]


type Change = Create | Revise | AddMember | RemoveMember | RecordDecision | Assess


@dataclass(frozen=True, slots=True)
class CurrentFacts:
    subject_revision: int
    revision: Revision


@dataclass(frozen=True, slots=True)
class Rejected:
    code: RejectionCode
    message: str


@dataclass(frozen=True, slots=True)
class Accepted:
    subject_revision: int
    evaluation_context: int


def decide(  # noqa: C901, PLR0912 - one closed initiative legality decision
    initiative_id: InitiativeId,
    current: CurrentFacts | None,
    change: Change,
    item_exists: bool,
    active_membership: bool,
) -> Accepted | Rejected:
    """Decide from locked selected facts; no history reads, effects, or lifecycle authority."""
    if isinstance(change, Create):
        if current is not None:
            return Rejected(
                RejectionCode.INITIATIVE_EXISTS,
                f"Initiative '{initiative_id}' already exists; read it before revising.",
            )
        return Accepted(1, 1)
    if current is None:
        return Rejected(
            RejectionCode.INITIATIVE_NOT_FOUND,
            f"Initiative '{initiative_id}' is absent; read the roadmap to select an existing initiative.",
        )
    if change.expected_revision != current.subject_revision:
        return Rejected(
            RejectionCode.INITIATIVE_STALE,
            f"Initiative '{initiative_id}' changed; read its current subject revision before requesting another change.",
        )
    context = current.revision.evaluation_context
    match change:
        case Revise(definition=definition):
            previous = current.revision.definition
            if (definition.goal, definition.success_signal) != (previous.goal, previous.success_signal):
                context += 1
        case AddMember(item_id=item_id) | RemoveMember(item_id=item_id):
            if not item_exists:
                return Rejected(
                    RejectionCode.ITEM_NOT_FOUND,
                    f"Item '{item_id}' is absent; inspect item status before changing its membership.",
                )
            if isinstance(change, AddMember) and active_membership:
                return Rejected(
                    RejectionCode.MEMBERSHIP_EXISTS,
                    f"Item '{item_id}' already belongs to initiative '{initiative_id}'; read its current membership.",
                )
            if isinstance(change, RemoveMember) and not active_membership:
                return Rejected(
                    RejectionCode.MEMBERSHIP_ABSENT,
                    f"Item '{item_id}' has no active membership in initiative '{initiative_id}'; read membership history.",
                )
        case Assess(evaluation_context=evaluated):
            if evaluated != context:
                return Rejected(
                    RejectionCode.ASSESSMENT_CONTEXT_STALE,
                    f"Initiative '{initiative_id}' has another evaluation context; read its current goal and success signal before assessment.",
                )
        case RecordDecision():
            pass
        case _ as unreachable:
            assert_never(unreachable)
    return Accepted(current.subject_revision + 1, context)
