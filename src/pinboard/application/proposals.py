from typing import assert_never

from pinboard.application import proposal_models
from pinboard.application.proposal_models import Proposal
from pinboard.domain import proposal_models as domain_proposal_models
from pinboard.domain import work_models
from pinboard.domain.identifiers import ItemId, ProposalId, TaskId


def convert_proposal(value: Proposal) -> domain_proposal_models.CreateProposalOperation:
    match value.relation:
        case proposal_models.IndependentProposalRelation():
            relation = work_models.IndependentProposalRelation()
        case proposal_models.PrerequisiteProposalRelation(item=item):
            relation = work_models.PrerequisiteProposalRelation(ItemId(item))
        case proposal_models.FollowUpProposalRelation(item=item):
            relation = work_models.FollowUpProposalRelation(ItemId(item))
        case proposal_models.DuplicateProposalRelation(item=item):
            relation = work_models.DuplicateProposalRelation(ItemId(item))
        case proposal_models.ContradictionProposalRelation(item=item):
            relation = work_models.ContradictionProposalRelation(ItemId(item))
        case proposal_models.ClarificationProposalRelation():
            relation = work_models.ClarificationProposalRelation()
        case proposal_models.PlannedReplacementProposalRelation(item=item, replacement_cost=replacement_cost):
            relation = work_models.PlannedReplacementProposalRelation(ItemId(item), replacement_cost)
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
            tuple(
                work_models.WorkObligation(
                    work_models.ObligationId(obligation.obligation_id),
                    obligation.statement,
                    work_models.ObligationDeferralPolicy(obligation.deferral_policy),
                )
                for obligation in value.obligations
            ),
            value.position,
        )
    )
