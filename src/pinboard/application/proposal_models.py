from datetime import UTC, datetime
from datetime import date as HumanDate
from typing import Annotated, Literal

import msgspec

from pinboard.domain.history import DependencyPayload, OriginObligationPayload, RouteFactsPayload, WorkObligationPayload

type ProposalJsonValue = bool | int | float | str | list[ProposalJsonValue] | dict[str, ProposalJsonValue] | None

type ProposalIdentity = Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]
type ProposalText = Annotated[str, msgspec.Meta(pattern=r"\A[^\s|\n](?:[^|\n]*[^\s|\n])?\z")]


class ProposalObligation(WorkObligationPayload, frozen=True, forbid_unknown_fields=True):
    obligation_id: ProposalIdentity
    statement: ProposalText


def _proposal_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class IndependentProposalRelation(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="independent", tag_field="kind"
):
    item: None


class PrerequisiteProposalRelation(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="prerequisite", tag_field="kind"
):
    item: ProposalIdentity


class FollowUpProposalRelation(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="follow-up", tag_field="kind"
):
    item: DependencyPayload


class DuplicateProposalRelation(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="duplicate", tag_field="kind"
):
    item: ProposalIdentity


class ContradictionProposalRelation(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="contradiction", tag_field="kind"
):
    item: ProposalIdentity


class ClarificationProposalRelation(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="clarification", tag_field="kind"
):
    item: None


class PlannedReplacementProposalRelation(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="planned-replacement", tag_field="kind"
):
    item: ProposalIdentity
    replacement_cost: ProposalText


class HumanAttribution(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    human: ProposalText
    date: HumanDate
    reason: ProposalText


class InitiativeMembershipIntent(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="initiative", tag_field="kind"
):
    initiative_id: ProposalIdentity
    expected_revision: Annotated[int, msgspec.Meta(ge=1)]
    attribution: HumanAttribution


class OffRoadmapMembershipIntent(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="off-roadmap", tag_field="kind"
):
    attribution: HumanAttribution


class NoInitiativesMembershipIntent(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="no-initiatives", tag_field="kind"
):
    pass


type ProposalMembershipIntent = InitiativeMembershipIntent | OffRoadmapMembershipIntent | NoInitiativesMembershipIntent


class NoRelatedDisposition(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="none", tag_field="kind"):
    pass


class RelatedDisposition(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="related", tag_field="kind"):
    item_id: ProposalIdentity
    active_memberships: tuple[ProposalIdentity, ...]
    active_off_roadmap: bool

    def __post_init__(self) -> None:
        if len(self.active_memberships) != len(set(self.active_memberships)):
            raise ValueError("related active_memberships must be ordered and unique")
        if self.active_memberships and self.active_off_roadmap:
            raise ValueError("related disposition cannot be assigned and off-roadmap")


type ProposalRelatedDisposition = NoRelatedDisposition | RelatedDisposition


type ProposalRelation = (
    IndependentProposalRelation
    | PrerequisiteProposalRelation
    | FollowUpProposalRelation
    | DuplicateProposalRelation
    | ContradictionProposalRelation
    | ClarificationProposalRelation
    | PlannedReplacementProposalRelation
)


class _ProposalFields(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    proposal_id: ProposalIdentity
    created_at: ProposalText
    source_task_id: ProposalText
    user_label: ProposalText
    trigger: ProposalText
    evidence: tuple[ProposalText, ...]
    why_it_matters: ProposalText
    relation: ProposalRelation
    effect: ProposalText
    unlock: ProposalText
    urgency_evidence: ProposalText
    freshness_assumptions: tuple[ProposalText, ...]
    checkout_policy: Literal["main", "isolated", "coordinator-selected"]
    route_facts: RouteFactsPayload
    membership_intent: ProposalMembershipIntent
    related_disposition: ProposalRelatedDisposition

    def __post_init__(self) -> None:
        try:
            created_at = _proposal_datetime(self.created_at)
        except ValueError as error:
            raise ValueError("created_at must be an ISO timestamp.") from error
        if created_at.tzinfo is None and len(self.created_at) != 10:
            raise ValueError("created_at must include a timezone.")
        if len(self.evidence) != len(set(self.evidence)):
            raise ValueError("evidence entries must be ordered and unique.")
        if len(self.freshness_assumptions) != len(set(self.freshness_assumptions)):
            raise ValueError("freshness_assumptions entries must be ordered and unique.")

    def created_at_utc(self) -> datetime:
        created_at = _proposal_datetime(self.created_at)
        if created_at.tzinfo is None:
            return created_at.replace(tzinfo=UTC)
        return created_at.astimezone(UTC)


def _validate_obligations(
    values: tuple[WorkObligationPayload, ...], checkpoints: tuple[ProposalIdentity, ...], route: RouteFactsPayload
) -> None:
    obligation_ids = tuple(value.obligation_id for value in values)
    if len(obligation_ids) != len(set(obligation_ids)):
        raise ValueError("obligation identities must be ordered and unique.")
    if len(checkpoints) != len(set(checkpoints)):
        raise ValueError("checkpoints entries must be ordered and unique.")
    if route.declares_checkpoints != bool(checkpoints):
        raise ValueError("route_facts.declares_checkpoints must match the declared checkpoints.")


class Proposal(
    _ProposalFields, frozen=True, forbid_unknown_fields=True, tag="pinboard-proposal/v6", tag_field="schema"
):
    obligations: Annotated[tuple[ProposalObligation, ...], msgspec.Meta(min_length=1)]
    position: Annotated[int, msgspec.Meta(ge=1)] | None
    checkpoints: tuple[ProposalIdentity, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        _validate_obligations(self.obligations, self.checkpoints, self.route_facts)


class OriginProposal(
    _ProposalFields, frozen=True, forbid_unknown_fields=True, tag="pinboard-proposal/v7", tag_field="schema"
):
    position: Annotated[int, msgspec.Meta(ge=1)] | None
    obligations: Annotated[tuple[OriginObligationPayload, ...], msgspec.Meta(min_length=1)]
    checkpoints: tuple[ProposalIdentity, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        _validate_obligations(self.obligations, self.checkpoints, self.route_facts)


type ProposalInput = Proposal | OriginProposal
