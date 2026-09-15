from datetime import UTC, datetime
from typing import Annotated, Literal

import msgspec

type ProposalSchema = Literal["pinboard-proposal/v1"]
type ProposalIdentity = Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]
type ProposalText = Annotated[str, msgspec.Meta(pattern=r"\A[^\s|\n](?:[^|\n]*[^\s|\n])?\z")]


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
    item: ProposalIdentity


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


type ProposalRelation = (
    IndependentProposalRelation
    | PrerequisiteProposalRelation
    | FollowUpProposalRelation
    | DuplicateProposalRelation
    | ContradictionProposalRelation
    | ClarificationProposalRelation
    | PlannedReplacementProposalRelation
)


class Proposal(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: ProposalSchema
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
    position: Annotated[int, msgspec.Meta(ge=1)] | None = None

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
