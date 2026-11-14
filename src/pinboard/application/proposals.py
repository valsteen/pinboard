from typing import assert_never

from pinboard.application import proposal_models
from pinboard.application.proposal_models import ProposalInput
from pinboard.domain import initiatives, work_models
from pinboard.domain import proposal_models as domain_proposal_models
from pinboard.domain.history import dependency_from_payload, obligation_from_payload, route_facts_from_payload
from pinboard.domain.identifiers import CheckpointId, ProposalId, TaskId, WorkItemId


def convert_membership_intent(value: proposal_models.ProposalMembershipIntent) -> initiatives.MembershipIntent:
    match value:
        case proposal_models.InitiativeMembershipIntent(
            initiative_id=initiative_id, expected_revision=expected_revision, attribution=attribution
        ):
            membership_intent: initiatives.MembershipIntent = initiatives.InitiativeIntent(
                initiatives.InitiativeId(initiative_id),
                expected_revision,
                initiatives.HumanReason(attribution.human, attribution.date, attribution.reason),
            )
        case proposal_models.OffRoadmapMembershipIntent(attribution=attribution):
            membership_intent = initiatives.OffRoadmapIntent(
                initiatives.HumanReason(attribution.human, attribution.date, attribution.reason)
            )
        case proposal_models.NoInitiativesMembershipIntent():
            membership_intent = initiatives.NoInitiativesIntent()
        case _ as unreachable:
            assert_never(unreachable)
    return membership_intent


def convert_proposal(value: ProposalInput) -> domain_proposal_models.CreateProposalOperation:
    match value.relation:
        case proposal_models.IndependentProposalRelation():
            relation = work_models.IndependentProposalRelation()
        case proposal_models.PrerequisiteProposalRelation(item=item):
            relation = work_models.PrerequisiteProposalRelation(WorkItemId(item))
        case proposal_models.FollowUpProposalRelation(item=item):
            relation = work_models.FollowUpProposalRelation(dependency_from_payload(item))
        case proposal_models.DuplicateProposalRelation(item=item):
            relation = work_models.DuplicateProposalRelation(WorkItemId(item))
        case proposal_models.ContradictionProposalRelation(item=item):
            relation = work_models.ContradictionProposalRelation(WorkItemId(item))
        case proposal_models.ClarificationProposalRelation():
            relation = work_models.ClarificationProposalRelation()
        case proposal_models.PlannedReplacementProposalRelation(item=item, replacement_cost=replacement_cost):
            relation = work_models.PlannedReplacementProposalRelation(WorkItemId(item), replacement_cost)
        case _ as unreachable:
            assert_never(unreachable)
    match value.related_disposition:
        case proposal_models.NoRelatedDisposition():
            related_disposition: initiatives.RelatedDisposition = initiatives.NoRelatedDisposition()
        case proposal_models.RelatedDisposition(
            item_id=item_id,
            active_memberships=active_memberships,
            active_off_roadmap=active_off_roadmap,
        ):
            related_disposition = initiatives.ExpectedRelatedDisposition(
                WorkItemId(item_id),
                tuple(initiatives.InitiativeId(initiative_id) for initiative_id in active_memberships),
                active_off_roadmap,
            )
        case _ as unreachable:
            assert_never(unreachable)
    return domain_proposal_models.CreateProposalOperation(
        domain_proposal_models.ProposalIntake(
            ProposalId(value.proposal_id),
            value.created_at_utc(),
            TaskId(value.source_task_id),
            value.user_label,
            value.trigger,
            value.why_it_matters,
            value.effect,
            value.unlock,
            relation,
            value.urgency_evidence,
            value.evidence,
            value.freshness_assumptions,
            work_models.CheckoutPolicy(value.checkout_policy),
            tuple(obligation_from_payload(obligation) for obligation in value.obligations),
            tuple(CheckpointId(checkpoint) for checkpoint in value.checkpoints),
            route_facts_from_payload(value.route_facts),
            convert_membership_intent(value.membership_intent),
            related_disposition,
            value.position,
        )
    )
