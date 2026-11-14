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
    OFF_ROADMAP_EXISTS = ("OFF_ROADMAP_EXISTS", "The selected item already has an active off-roadmap episode.")
    OFF_ROADMAP_ABSENT = ("OFF_ROADMAP_ABSENT", "The selected item has no active off-roadmap episode to clear.")
    ROADMAP_DISPOSITION_CONFLICT = (
        "ROADMAP_DISPOSITION_CONFLICT",
        "An item cannot be actively assigned to an initiative and off-roadmap at the same time.",
    )
    ROADMAP_DISPOSITION_STALE = (
        "ROADMAP_DISPOSITION_STALE",
        "The supplied roadmap disposition revision differs from current state.",
    )
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
class OffRoadmapEpisode:
    item_id: WorkItemId
    episode: int
    opened: Provenance
    closed: Provenance | None


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
class MarkOffRoadmap:
    expected_disposition_revision: int
    item_id: WorkItemId


@dataclass(frozen=True, slots=True)
class ClearOffRoadmap:
    expected_disposition_revision: int
    expected_episode: int
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
type DispositionChange = MarkOffRoadmap | ClearOffRoadmap


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


@dataclass(frozen=True, slots=True)
class DispositionFacts:
    item_exists: bool
    active_memberships: tuple[InitiativeId, ...]
    active_off_roadmap: OffRoadmapEpisode | None
    revision: int


@dataclass(frozen=True, slots=True)
class InitiativeIntent:
    initiative_id: InitiativeId
    expected_revision: int
    reason: HumanReason


@dataclass(frozen=True, slots=True)
class OffRoadmapIntent:
    reason: HumanReason


@dataclass(frozen=True, slots=True)
class NoInitiativesIntent:
    pass


type MembershipIntent = InitiativeIntent | OffRoadmapIntent | NoInitiativesIntent


@dataclass(frozen=True, slots=True)
class ExpectedRelatedDisposition:
    item_id: WorkItemId
    active_memberships: tuple[InitiativeId, ...]
    active_off_roadmap: bool


@dataclass(frozen=True, slots=True)
class NoRelatedDisposition:
    pass


type RelatedDisposition = ExpectedRelatedDisposition | NoRelatedDisposition


@dataclass(frozen=True, slots=True)
class MembershipIntentFacts:
    initiative_revisions: tuple[tuple[InitiativeId, int], ...]
    related_disposition: RelatedDisposition


@dataclass(frozen=True, slots=True)
class AssignMembership:
    initiative_id: InitiativeId
    expected_revision: int
    reason: HumanReason


@dataclass(frozen=True, slots=True)
class OpenOffRoadmap:
    reason: HumanReason


@dataclass(frozen=True, slots=True)
class LeaveUnassigned:
    pass


type MembershipEffect = AssignMembership | OpenOffRoadmap | LeaveUnassigned


def decide(  # noqa: C901, PLR0912 - one closed initiative legality decision
    initiative_id: InitiativeId,
    current: CurrentFacts | None,
    change: Change,
    item_exists: bool,
    active_membership: bool,
    active_off_roadmap: bool,
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
            if active_membership and active_off_roadmap:
                return Rejected(
                    RejectionCode.ROADMAP_DISPOSITION_CONFLICT,
                    f"Item '{item_id}' has conflicting active membership and off-roadmap facts; run validation before changing it.",
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


def decide_disposition(change: DispositionChange, facts: DispositionFacts) -> Rejected | None:
    """Decide one explicit item-level roadmap disposition from locked selected facts."""
    if not facts.item_exists:
        return Rejected(
            RejectionCode.ITEM_NOT_FOUND,
            f"Item '{change.item_id}' is absent; inspect item status before changing its roadmap disposition.",
        )
    if change.expected_disposition_revision != facts.revision:
        return Rejected(
            RejectionCode.ROADMAP_DISPOSITION_STALE,
            f"Item '{change.item_id}' roadmap disposition changed; read a fresh roadmap before another change.",
        )
    match change:
        case MarkOffRoadmap(item_id=item_id):
            if facts.active_memberships:
                return Rejected(
                    RejectionCode.ROADMAP_DISPOSITION_CONFLICT,
                    f"Item '{item_id}' belongs to {', '.join(facts.active_memberships)}; remove active memberships before marking it off-roadmap.",
                )
            if facts.active_off_roadmap is not None:
                return Rejected(
                    RejectionCode.OFF_ROADMAP_EXISTS,
                    f"Item '{item_id}' is already off-roadmap; read its active episode before changing it.",
                )
        case ClearOffRoadmap(expected_episode=episode, item_id=item_id):
            active = facts.active_off_roadmap
            if active is None:
                return Rejected(
                    RejectionCode.OFF_ROADMAP_ABSENT,
                    f"Item '{item_id}' has no active off-roadmap episode; read the roadmap before clearing it.",
                )
            if active.episode != episode:
                return Rejected(
                    RejectionCode.ROADMAP_DISPOSITION_STALE,
                    f"Item '{item_id}' active off-roadmap episode changed; read a fresh roadmap before clearing it.",
                )
        case _ as unreachable:
            assert_never(unreachable)
    return None


def decide_membership_intent(
    intent: MembershipIntent,
    initiative_revisions: tuple[tuple[InitiativeId, int], ...],
    related_expected: RelatedDisposition,
    related_current: RelatedDisposition,
) -> MembershipEffect | Rejected:
    """Resolve one confirmed intent without deriving authority from a relation."""
    if related_expected != related_current:
        return Rejected(
            RejectionCode.ROADMAP_DISPOSITION_STALE,
            "The related item's displayed initiative or off-roadmap facts changed; read them again before confirming intent.",
        )
    current = dict(initiative_revisions)
    if not current and not isinstance(intent, NoInitiativesIntent):
        return Rejected(
            RejectionCode.ROADMAP_DISPOSITION_STALE,
            "No initiatives exist; confirm the exact no-initiatives intent before creating work.",
        )
    match intent:
        case InitiativeIntent(initiative_id=initiative_id, expected_revision=expected, reason=reason):
            observed = current.get(initiative_id)
            if observed is None:
                return Rejected(
                    RejectionCode.INITIATIVE_NOT_FOUND,
                    f"Initiative '{initiative_id}' is absent; read the roadmap and confirm another intent.",
                )
            if observed != expected:
                return Rejected(
                    RejectionCode.INITIATIVE_STALE,
                    f"Initiative '{initiative_id}' changed; read its current subject revision before confirming intent.",
                )
            return AssignMembership(initiative_id, expected, reason)
        case OffRoadmapIntent(reason=reason):
            return OpenOffRoadmap(reason)
        case NoInitiativesIntent():
            if current:
                return Rejected(
                    RejectionCode.ROADMAP_DISPOSITION_STALE,
                    "Initiatives now exist; read the roadmap and explicitly choose one initiative or off-roadmap intent.",
                )
            return LeaveUnassigned()
        case _ as unreachable:
            assert_never(unreachable)
