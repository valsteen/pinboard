"""Plan one campaign, its direct origins, surviving waits and superseded sources without effects."""

from dataclasses import dataclass, replace
from datetime import datetime

from pinboard.domain import decision_models, decisions, history, proposal_models, work_models
from pinboard.domain.definition_decisions import (
    DefinitionRevisionDecision,
    decide_definition_revision,
    introduces_dependency_cycle,
)
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import CheckpointId, ProposalId, TaskId, WorkItemId
from pinboard.domain.ledger import LedgerSnapshot


@dataclass(frozen=True, slots=True)
class Source:
    item_id: WorkItemId
    replacement_cost: str


@dataclass(frozen=True, slots=True)
class CheckpointTarget:
    consumer: WorkItemId
    checkpoint: CheckpointId
    covers_carried_work: str


@dataclass(frozen=True, slots=True)
class Plan:
    sources: tuple[Source, ...]
    campaign_id: WorkItemId
    definition: work_models.WorkItemDefinition
    create: bool
    checkpoint_targets: tuple[CheckpointTarget, ...]


@dataclass(frozen=True, slots=True)
class Change:
    campaign: proposal_models.ProposalCreationDecision | DefinitionRevisionDecision
    dependent_revisions: tuple[DefinitionRevisionDecision, ...]
    replacements: tuple[work_models.PlannedReplacement, ...]
    source_closures: tuple[decision_models.WorkItemClosureChange, ...]


def _merge_waits(values: tuple[work_models.Dependency, ...]) -> tuple[work_models.Dependency, ...]:
    waits: dict[WorkItemId, work_models.Dependency] = {}
    for value in values:
        producer = work_models.dependency_item(value)
        if producer not in waits:
            waits[producer] = value
        elif waits[producer] != value:
            waits[producer] = producer
    return tuple(waits.values())


type SelectedSource = tuple[Source, work_models.WorkItem, work_models.DefinitionAnchor]


def _select_sources(snapshot: LedgerSnapshot, plan: Plan) -> DecisionResult[tuple[SelectedSource, ...]]:
    source_ids = frozenset(value.item_id for value in plan.sources)
    cohort = source_ids | {plan.campaign_id}
    owner = snapshot.work_item(plan.campaign_id)
    if plan.create:
        if owner is not None or plan.campaign_id in snapshot.history_items:
            return DecisionFailure(DecisionFailureCode.ITEM_ALREADY_EXISTS, "The new campaign identity exists.", None)
    elif owner is None:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_LIFECYCLE_INVALID,
            "The existing campaign must be live; terminal definitions are immutable.",
            None,
        )
    selected = []
    for source in plan.sources:
        item = snapshot.work_item(source.item_id)
        definition = snapshot.definition(source.item_id)
        if item is None or definition is None:
            return DecisionFailure(
                DecisionFailureCode.ITEM_NOT_FOUND, f"Consolidation source '{source.item_id}' must be live.", None
            )
        if item.state in {work_models.WorkState.ACTIVE, work_models.WorkState.REVIEW}:
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_AVAILABLE,
                f"Source '{source.item_id}' is active or in review; its acceptance path remains required.",
                None,
            )
        selected.append((source, item, definition))
    affected = cohort | {
        item.work_item_id
        for item in snapshot.items
        if item.work_item_id not in cohort
        and any(work_models.dependency_item(wait) in source_ids for wait in item.depends_on)
    }
    for item_id in affected:
        if snapshot.command_preparation_authority(item_id) is not None:
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_AVAILABLE,
                f"A live preparation claim prevents consolidation changes to '{item_id}'.",
                None,
            )
    if any(work_models.dependency_item(wait) == plan.campaign_id for wait in plan.definition.dependencies):
        return DecisionFailure(DecisionFailureCode.ITEM_DEPENDENCY_CYCLE, "A campaign cannot depend on itself.", None)
    return tuple(selected)


def _carry_definition(
    snapshot: LedgerSnapshot, plan: Plan, selected: tuple[SelectedSource, ...]
) -> work_models.WorkItemDefinition:
    cohort = {plan.campaign_id, *(value.item_id for value in plan.sources)}
    current_owner = snapshot.definition(plan.campaign_id)
    original_waits = tuple(wait for _, _, anchor in selected for wait in anchor.definition.dependencies)
    if current_owner is not None:
        original_waits = (*current_owner.definition.dependencies, *original_waits)
    external_waits = _merge_waits(
        tuple(
            wait
            for wait in (*plan.definition.dependencies, *original_waits)
            if work_models.dependency_item(wait) not in cohort
        )
    )
    obligations = list(plan.definition.obligations)
    used = {value.obligation_id for value in obligations}
    context = list(plan.definition.evidence)
    for _source, _item, anchor in selected:
        waits = (
            ", ".join(
                str(wait) if isinstance(wait, str) else f"{wait.item_id} checkpoint {wait.checkpoint_id}"
                for wait in anchor.definition.dependencies
            )
            or "none"
        )
        context.append(
            f"Binding source context {anchor.work_item_id}@{anchor.revision}: original prerequisites {waits}."
        )
        for obligation in anchor.definition.obligations:
            number = len(obligations) + 1
            obligation_id = work_models.ObligationId(f"carried-{number}")
            while obligation_id in used:
                number += 1
                obligation_id = work_models.ObligationId(f"carried-{number}")
            used.add(obligation_id)
            obligations.append(
                work_models.CarriedWorkObligation(
                    obligation_id,
                    obligation.statement,
                    obligation.deferral_policy,
                    work_models.ObligationOrigin(anchor.work_item_id, anchor.revision, obligation.obligation_id),
                )
            )
    return replace(
        plan.definition,
        dependencies=external_waits,
        obligations=tuple(obligations),
        evidence=tuple(dict.fromkeys(context)),
    )


def _retarget_definitions(
    snapshot: LedgerSnapshot, plan: Plan, campaign_definition: work_models.WorkItemDefinition
) -> DecisionResult[dict[WorkItemId, work_models.WorkItemDefinition]]:
    source_ids = {value.item_id for value in plan.sources}
    cohort = source_ids | {plan.campaign_id}
    targets = {value.consumer: value for value in plan.checkpoint_targets}
    outside = tuple(
        item
        for item in snapshot.items
        if item.work_item_id not in cohort
        and any(work_models.dependency_item(wait) in source_ids for wait in item.depends_on)
    )
    if any(item_id not in {item.work_item_id for item in outside} for item_id in targets):
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            "Checkpoint mappings must name surviving outside consumers of the selected sources.",
            None,
        )
    if any(value.checkpoint not in campaign_definition.checkpoints for value in targets.values()):
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID, "A mapped campaign checkpoint is not declared.", None
        )
    revised_definitions = {plan.campaign_id: campaign_definition}
    for item in outside:
        anchor = snapshot.definition(item.work_item_id)
        assert anchor is not None
        mapped = targets.get(item.work_item_id)
        target = (
            plan.campaign_id
            if mapped is None
            else work_models.CheckpointDependency(plan.campaign_id, mapped.checkpoint)
        )
        # All converging source/owner edges form one wait at the first original position.
        waits = _merge_waits(
            tuple(target if work_models.dependency_item(wait) in cohort else wait for wait in item.depends_on)
        )
        revised_definitions[item.work_item_id] = replace(anchor.definition, dependencies=waits)
    return revised_definitions


def _revision_changes(
    snapshot: LedgerSnapshot,
    graph: LedgerSnapshot,
    plan: Plan,
    definitions: dict[WorkItemId, work_models.WorkItemDefinition],
    actor: TaskId,
    now: datetime,
) -> DecisionResult[tuple[DefinitionRevisionDecision, ...]]:
    revisions = []
    for item_id, definition in definitions.items():
        if item_id == plan.campaign_id and plan.create:
            continue
        anchor = snapshot.definition(item_id)
        assert anchor is not None
        revised = decide_definition_revision(
            graph,
            item_id,
            work_models.ReviseWorkItemDefinitionInput(
                anchor.revision, anchor.digest, actor, "Confirmed campaign consolidation.", definition
            ),
            now,
        )
        if isinstance(revised, DecisionFailure):
            return revised
        revisions.append(revised)
    return tuple(revisions)


def _new_campaign(
    plan: Plan, definition: work_models.WorkItemDefinition, digest: str, live_count: int, actor: TaskId, now: datetime
) -> proposal_models.ProposalCreationDecision:
    intake = proposal_models.ProposalIntake(
        ProposalId(plan.campaign_id),
        now,
        actor,
        definition.title,
        "Human-confirmed campaign consolidation.",
        definition.hypothesis,
        definition.effect,
        definition.unlock,
        work_models.IndependentProposalRelation(),
        "Selected work is carried into this campaign.",
        definition.evidence,
        (),
        definition.checkout_policy,
        definition.obligations,
        definition.checkpoints,
        live_count + 1,
    )
    return proposal_models.ProposalCreationDecision(
        intake,
        proposal_models.ReadyProposalWorkItem(
            plan.campaign_id, live_count + 1, definition.dependencies, digest, definition
        ),
        None,
        None,
        intake.evidence,
        (),
    )


def _source_endings(
    snapshot: LedgerSnapshot,
    surviving: LedgerSnapshot,
    plan: Plan,
    selected: tuple[SelectedSource, ...],
    actor: TaskId,
    now: datetime,
) -> DecisionResult[
    tuple[tuple[work_models.PlannedReplacement, ...], tuple[decision_models.WorkItemClosureChange, ...]]
]:
    replacements = []
    closures = []
    for source, item, _anchor in selected:
        latest = max(
            (
                value.relation_revision
                for value in snapshot.planned_replacements
                if value.affected_item == source.item_id
            ),
            default=0,
        )
        relation = work_models.PlannedReplacement(
            source.item_id,
            latest + 1,
            plan.campaign_id,
            source.replacement_cost,
            work_models.PlannedReplacementStatus.CURRENT,
            actor,
            now,
        )
        closure = decisions.superseded_attempt_closure(surviving, item, relation)
        if isinstance(closure, DecisionFailure):
            return closure
        replacements.append(relation)
        closures.append(
            decision_models.WorkItemClosureChange(
                source.item_id,
                item.state,
                work_models.CloseOutcome.SUPERSEDED,
                f"Consolidated into {plan.campaign_id}.",
                "Separate campaign confirmation.",
                closure,
            )
        )
    return tuple(replacements), tuple(closures)


def decide(
    snapshot: LedgerSnapshot, plan: Plan, live_count: int, actor: TaskId, now: datetime
) -> DecisionResult[Change]:
    selected = _select_sources(snapshot, plan)
    if isinstance(selected, DecisionFailure):
        return selected
    campaign_definition = _carry_definition(snapshot, plan, selected)
    digest = history.work_item_definition_digest(campaign_definition)
    if isinstance(digest, DecisionFailure):
        return digest
    revised_definitions = _retarget_definitions(snapshot, plan, campaign_definition)
    if isinstance(revised_definitions, DecisionFailure):
        return revised_definitions
    owner = snapshot.work_item(plan.campaign_id)
    current_owner = snapshot.definition(plan.campaign_id)
    external_waits = campaign_definition.dependencies
    owner_item = (
        owner
        if owner is not None
        else work_models.WorkItem(
            plan.campaign_id,
            work_models.WorkState.READY,
            None,
            external_waits,
            None,
            None,
            None,
            None,
            live_count + 1,
            None,
        )
    )
    graph = replace(
        snapshot,
        items=snapshot.items + (() if owner is not None else (owner_item,)),
        dependency_facts=tuple(
            work_models.DefinitionDependencies(value.work_item_id, revised_definitions[value.work_item_id].dependencies)
            if value.work_item_id in revised_definitions
            else value
            for value in snapshot.dependency_facts
        )
        + (
            () if current_owner is not None else (work_models.DefinitionDependencies(plan.campaign_id, external_waits),)
        ),
    )
    known = {*graph.work_items_by_id(), *graph.history_items}
    if any(work_models.dependency_item(wait) not in known for wait in external_waits):
        return DecisionFailure(
            DecisionFailureCode.ITEM_NOT_FOUND, "Campaign prerequisites must name existing items.", None
        )
    revisions = _revision_changes(snapshot, graph, plan, revised_definitions, actor, now)
    if isinstance(revisions, DecisionFailure):
        return revisions
    if plan.create:
        if introduces_dependency_cycle(graph, plan.campaign_id, external_waits):
            return DecisionFailure(
                DecisionFailureCode.ITEM_DEPENDENCY_CYCLE, "Campaign dependencies introduce a cycle.", None
            )
        campaign: proposal_models.ProposalCreationDecision | DefinitionRevisionDecision = _new_campaign(
            plan, campaign_definition, digest, live_count, actor, now
        )
        dependent_revisions = revisions
    else:
        campaign, *dependents = revisions
        dependent_revisions = tuple(dependents)
    source_ids = {value.item_id for value in plan.sources}
    surviving = replace(
        snapshot,
        items=tuple(
            replace(item, depends_on=revised_definitions[item.work_item_id].dependencies)
            if item.work_item_id in revised_definitions
            else item
            for item in snapshot.items
            if item.work_item_id not in source_ids
        )
        + (() if owner is not None else (owner_item,)),
    )
    endings = _source_endings(snapshot, surviving, plan, selected, actor, now)
    if isinstance(endings, DecisionFailure):
        return endings
    replacements, closures = endings
    return Change(campaign, dependent_revisions, replacements, closures)
