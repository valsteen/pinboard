from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import ItemId
from pinboard.domain.ledger import LedgerSnapshot
from pinboard.domain.proposal_models import (
    CreateProposalOperation,
    IntakeWorkItem,
    PrerequisiteDependencyChange,
    ProposalCreationDecision,
)


def _planned_replacement_revision(relation: work_models.PlannedReplacement) -> int:
    return relation.relation_revision


def decide_proposal_creation(
    snapshot: LedgerSnapshot,
    operation: CreateProposalOperation,
    live_item_count: int,
) -> DecisionResult[ProposalCreationDecision]:
    intake = operation.intake
    if snapshot.proposal(intake.proposal_id) is not None:
        return DecisionFailure(DecisionFailureCode.PROPOSAL_ALREADY_EXISTS, "Proposal identity already exists.", None)
    item_id = ItemId(intake.proposal_id)
    if snapshot.item(item_id) is not None or item_id in snapshot.history_items:
        return DecisionFailure(
            DecisionFailureCode.ITEM_ALREADY_EXISTS, "Proposal identity already names a work item.", None
        )
    if (
        intake.relation.item is not None
        and snapshot.item(intake.relation.item) is None
        and intake.relation.item not in snapshot.history_items
    ):
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, "The related work item does not exist.", None)
    position = intake.position if intake.position is not None else live_item_count + 1
    if position > live_item_count + 1:
        return DecisionFailure(
            DecisionFailureCode.PROPOSAL_INVALID,
            f"Proposal position must be between 1 and {live_item_count + 1}.",
            None,
        )
    dependencies = (intake.relation.item,) if isinstance(intake.relation, work_models.FollowUpProposalRelation) else ()
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
    )
    digest = work_item_definition_digest(definition)
    if isinstance(digest, DecisionFailure):
        return digest
    prerequisite_change: PrerequisiteDependencyChange | None = None
    planned_replacement: work_models.PlannedReplacement | None = None
    if isinstance(intake.relation, work_models.PrerequisiteProposalRelation):
        if any(authority.item == intake.relation.item for authority in snapshot.command_preparation_authorities):
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_AVAILABLE,
                "A live preparation claim prevents prerequisite changes to its ready item.",
                None,
            )
        target = snapshot.item(intake.relation.item)
        anchor = snapshot.definition(intake.relation.item)
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
            )
            changed_digest = work_item_definition_digest(changed_definition)
            if isinstance(changed_digest, DecisionFailure):
                return changed_digest
            prerequisite_change = PrerequisiteDependencyChange(
                intake.relation.item,
                item_id,
                dependency_position,
                anchor.revision,
                anchor.digest,
                changed_digest,
                changed_definition,
            )
    elif isinstance(intake.relation, work_models.PlannedReplacementProposalRelation):
        if not intake.relation.replacement_cost.strip():
            return DecisionFailure(
                DecisionFailureCode.PROPOSAL_INVALID,
                "A planned replacement proposal requires a concrete nonempty replacement cost.",
                None,
            )
        matching_relations: tuple[work_models.PlannedReplacement, ...] = tuple(
            relation for relation in snapshot.planned_replacements if relation.affected_item == intake.relation.item
        )
        latest = max(matching_relations, key=_planned_replacement_revision, default=None)
        revision = 1 if latest is None else latest.relation_revision + 1
        planned_replacement = work_models.PlannedReplacement(
            intake.relation.item,
            revision,
            item_id,
            intake.relation.replacement_cost,
            work_models.PlannedReplacementStatus.CURRENT,
            intake.source_task_id,
            operation.intake.created_at,
        )
    return ProposalCreationDecision(
        intake,
        IntakeWorkItem(item_id, position, dependencies, digest, definition),
        prerequisite_change,
        planned_replacement,
        intake.evidence,
        intake.freshness_assumptions,
    )
