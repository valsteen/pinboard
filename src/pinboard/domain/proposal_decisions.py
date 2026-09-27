from typing import assert_never

from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import WorkItemId
from pinboard.domain.ledger import LedgerSnapshot
from pinboard.domain.proposal_models import (
    CreateProposalOperation,
    PrerequisiteDependencyChange,
    ProposalCreationDecision,
    ReadyProposalWorkItem,
)


def decide_proposal_creation(  # noqa: C901, PLR0912 - exhaustive proposal relation decisions
    snapshot: LedgerSnapshot,
    operation: CreateProposalOperation,
    live_item_count: int,
) -> DecisionResult[ProposalCreationDecision]:
    intake = operation.intake
    if snapshot.proposal(intake.proposal_id) is not None:
        return DecisionFailure(DecisionFailureCode.PROPOSAL_ALREADY_EXISTS, "Proposal identity already exists.", None)
    item_id = WorkItemId(intake.proposal_id)
    if snapshot.work_item(item_id) is not None or item_id in snapshot.history_items:
        return DecisionFailure(
            DecisionFailureCode.ITEM_ALREADY_EXISTS, "Proposal identity already names a work item.", None
        )
    if (
        intake.relation.work_item_id is not None
        and snapshot.work_item(intake.relation.work_item_id) is None
        and intake.relation.work_item_id not in snapshot.history_items
    ):
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, "The related work item does not exist.", None)
    position = intake.position if intake.position is not None else live_item_count + 1
    if position > live_item_count + 1:
        return DecisionFailure(
            DecisionFailureCode.PROPOSAL_INVALID,
            f"Proposal position must be between 1 and {live_item_count + 1}.",
            None,
        )
    match intake.relation:
        case work_models.FollowUpProposalRelation(work_item_id=dependency):
            dependencies = (dependency,)
        case (
            work_models.IndependentProposalRelation()
            | work_models.PrerequisiteProposalRelation()
            | work_models.DuplicateProposalRelation()
            | work_models.ContradictionProposalRelation()
            | work_models.ClarificationProposalRelation()
            | work_models.PlannedReplacementProposalRelation()
        ):
            dependencies = ()
        case _ as unreachable:
            assert_never(unreachable)
    definition = work_models.WorkItemDefinition(
        intake.user_label,
        intake.effect,
        intake.why_it_matters,
        intake.evidence,
        (intake.effect,),
        (),
        (intake.unlock,),
        dependencies,
        intake.effect,
        intake.unlock,
        intake.checkout_policy,
        intake.obligations,
    )
    digest = work_item_definition_digest(definition)
    if isinstance(digest, DecisionFailure):
        return digest
    prerequisite_change: PrerequisiteDependencyChange | None = None
    planned_replacement: work_models.PlannedReplacement | None = None
    match intake.relation:
        case work_models.PrerequisiteProposalRelation(work_item_id=related_item):
            if any(authority.work_item_id == related_item for authority in snapshot.command_preparation_authorities):
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    "A live preparation claim prevents prerequisite changes to its ready item.",
                    None,
                )
            target = snapshot.work_item(related_item)
            anchor = snapshot.definition(related_item)
            if target is not None and anchor is not None:
                dependency_position = len(anchor.definition.dependencies)
                changed_definition = work_models.WorkItemDefinition(
                    anchor.definition.title,
                    anchor.definition.objective,
                    anchor.definition.hypothesis,
                    anchor.definition.evidence,
                    anchor.definition.scope,
                    anchor.definition.non_scope,
                    anchor.definition.acceptance_criteria,
                    (*anchor.definition.dependencies, item_id),
                    anchor.definition.effect,
                    anchor.definition.unlock,
                    anchor.definition.checkout_policy,
                    anchor.definition.obligations,
                )
                changed_digest = work_item_definition_digest(changed_definition)
                if isinstance(changed_digest, DecisionFailure):
                    return changed_digest
                prerequisite_change = PrerequisiteDependencyChange(
                    related_item,
                    item_id,
                    dependency_position,
                    anchor.revision,
                    anchor.digest,
                    changed_digest,
                    changed_definition,
                )
        case work_models.PlannedReplacementProposalRelation(work_item_id=related_item, replacement_cost=cost):
            if not cost.strip():
                return DecisionFailure(
                    DecisionFailureCode.PROPOSAL_INVALID,
                    "A planned replacement proposal requires a concrete nonempty replacement cost.",
                    None,
                )
            matching_relations: tuple[work_models.PlannedReplacement, ...] = tuple(
                relation for relation in snapshot.planned_replacements if relation.affected_item == related_item
            )
            latest = max(matching_relations, key=work_models.planned_replacement_revision, default=None)
            revision = 1 if latest is None else latest.relation_revision + 1
            planned_replacement = work_models.PlannedReplacement(
                related_item,
                revision,
                item_id,
                cost,
                work_models.PlannedReplacementStatus.CURRENT,
                intake.source_task_id,
                operation.intake.created_at,
            )
        case (
            work_models.IndependentProposalRelation()
            | work_models.FollowUpProposalRelation()
            | work_models.DuplicateProposalRelation()
            | work_models.ContradictionProposalRelation()
            | work_models.ClarificationProposalRelation()
        ):
            pass
        case _ as unreachable:
            assert_never(unreachable)
    return ProposalCreationDecision(
        intake,
        ReadyProposalWorkItem(item_id, position, dependencies, digest, definition),
        prerequisite_change,
        planned_replacement,
        intake.evidence,
        intake.freshness_assumptions,
    )
