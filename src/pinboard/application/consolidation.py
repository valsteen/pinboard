"""Display exact selected facts; reread their binding under lock and commit one closed consolidation."""

from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import msgspec

from pinboard.application import consolidation_models as models
from pinboard.application import obligation_coverage, stored_state
from pinboard.application.dependency_authoring import DependencyCheckpointPin, validate_dependency_brief_pins
from pinboard.application.mutation_models import CommittedEffect, ConsolidationMutation, MutationReceipt
from pinboard.application.ports import WorkStore
from pinboard.domain import consolidation, decision_models, history, work_models
from pinboard.domain.definition_decisions import DefinitionRevisionDecision
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import ActionId, HistorySubjectId, HostId, TaskId, WorkItemId
from pinboard.domain.proposal_models import ProposalCreationDecision


def binding(preview: models.Preview, work_root: Path) -> models.Binding:
    return models.Binding(
        str(work_root),
        "pinboard-consolidation-binding/v1",
        sha256(msgspec.json.encode(preview, order="sorted")).hexdigest(),
    )


def _definition_change(
    item_id: WorkItemId, revision: int, digest: str, definition: work_models.WorkItemDefinition
) -> models.DefinitionChange:
    payload = history.work_item_definition_payload(definition)
    assert not isinstance(payload, DecisionFailure)
    return models.DefinitionChange(item_id, revision, digest, payload)


def _selected_items(facts: models.Facts) -> tuple[models.SelectedItem, ...]:
    snapshot = facts.snapshot
    values = []
    for anchor in sorted(snapshot.definitions, key=lambda value: value.work_item_id):
        item = snapshot.work_item(anchor.work_item_id)
        assert item is not None
        attempt = None if item.attempt is None else snapshot.attempt(item.attempt)
        observed: models.NoAttempt | models.RetainedAttempt = models.NoAttempt()
        if attempt is not None:
            revision = snapshot.subject_revision(attempt.attempt)
            assert revision is not None
            observed = models.RetainedAttempt(
                attempt.attempt,
                attempt.state,
                revision,
                attempt.accepted_scope_revision,
                attempt.accepted_scope_digest,
                attempt.protected_candidate_revision,
                attempt.brief_artifact_ref_id,
            )
        payload = history.work_item_definition_payload(anchor.definition)
        assert not isinstance(payload, DecisionFailure)
        revision = snapshot.subject_revision(item.work_item_id)
        assert revision is not None
        values.append(
            models.SelectedItem(
                item.work_item_id, item.state, revision, anchor.revision, anchor.digest, payload, observed
            )
        )
    return tuple(values)


def _project(selection: models.Selection, facts: models.Facts, change: consolidation.Change) -> models.Preview:
    match change.campaign:
        case ProposalCreationDecision(ready_item=created):
            campaign = _definition_change(created.work_item_id, 1, created.definition_digest, created.definition)
        case DefinitionRevisionDecision() as revised:
            campaign = _definition_change(
                revised.work_item_id, revised.revision, revised.after_digest, revised.definition
            )
    before = facts.live_order
    source_ids = frozenset(value.item_id for value in selection.sources)
    after = tuple(value for value in before if value not in source_ids)
    if selection.campaign.create:
        after = (*after, WorkItemId(selection.campaign.item_id))
    positions_before = {item: index for index, item in enumerate(before, 1)}
    positions_after = {item: index for index, item in enumerate(after, 1)}
    changes = tuple(
        models.QueueChange(item, positions_before.get(item), positions_after.get(item))
        for item in dict.fromkeys((*before, *after))
        if positions_before.get(item) != positions_after.get(item)
    )
    snapshot = facts.snapshot
    return models.Preview(
        selection,
        _selected_items(facts),
        campaign,
        tuple(
            _definition_change(value.work_item_id, value.revision, value.after_digest, value.definition)
            for value in change.dependent_revisions
        ),
        tuple(
            models.Replacement(
                value.affected_item, value.relation_revision, value.replacement_item, value.replacement_cost
            )
            for value in change.replacements
        ),
        tuple(sorted(snapshot.attempt_authorities, key=lambda value: value.attempt)),
        tuple(sorted(snapshot.preparation_authorities, key=lambda value: value.work_item_id)),
        tuple(sorted(snapshot.command_attempt_authorities, key=lambda value: value.attempt)),
        tuple(sorted(snapshot.command_preparation_authorities, key=lambda value: value.work_item_id)),
        changes,
        "Sources end as superseded; paused/blocked attempts retain evidence and lose write authority. Campaign and surviving consumer lifecycles, candidates and authority are preserved; their accepted scopes may become stale. No process, launch or repository effect.",
        "Declared checkpoints do not enforce execution ordering. Outside consumers wait for the whole campaign unless the separately approved checkpoint mapping covers all their carried prerequisite work.",
    )


def _decide_change(
    facts: models.Facts,
    plan: consolidation.Plan,
    actor: TaskId,
    now: datetime,
    dependency_pins: tuple[DependencyCheckpointPin, ...],
) -> DecisionResult[consolidation.Change]:
    if (
        failure := validate_dependency_brief_pins(facts.snapshot, requested_dependencies(plan), dependency_pins)
    ) is not None:
        return failure
    return consolidation.decide(facts.snapshot, plan, len(facts.live_order), actor, now)


def preview(
    store: WorkStore,
    selection: models.Selection,
    actor: TaskId,
    now: datetime,
    dependency_pins: tuple[DependencyCheckpointPin, ...],
) -> DecisionResult[models.Preview]:
    plan = models.plan(selection)
    facts = store.read_consolidation_facts(plan, now)
    change = _decide_change(facts, plan, actor, now, dependency_pins)
    if isinstance(change, DecisionFailure):
        return change
    if (failure := obligation_coverage.validate_origins(store, plan.definition.obligations)) is not None:
        return failure
    return _project(selection, facts, change)


def confirm(
    store: WorkStore,
    selection: models.Selection,
    expected: models.Binding,
    human_confirmation: str,
    actor: TaskId,
    host: HostId,
    now: datetime,
    work_root: Path,
    dependency_pins: tuple[DependencyCheckpointPin, ...],
) -> DecisionResult[CommittedEffect]:
    plan = models.plan(selection)
    with store.write() as transaction:
        facts = transaction.read_consolidation_facts(plan, now)
        change = _decide_change(facts, plan, actor, now, dependency_pins)
        if isinstance(change, DecisionFailure):
            return change
        current = _project(selection, facts, change)
        if binding(current, work_root) != expected:
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_AVAILABLE,
                "Consolidation selection, definitions, relationships, queue or authority changed, or the binding names another board. Read a fresh preview and obtain separate human confirmation.",
                None,
            )
        if (failure := obligation_coverage.validate_origins(transaction, plan.definition.obligations)) is not None:
            return failure
        allocation = transaction.read_mutation_allocation()
        record = models.Record(expected, current, human_confirmation)
        closures = tuple(replace(value, human_decision=human_confirmation) for value in change.source_closures)
        receipt = MutationReceipt(
            decision_models.TransitionReceipt(
                ActionId(f"consolidate:{plan.campaign_id}"), plan.campaign_id, "consolidated", human_confirmation, now
            ),
            allocation.next_history_id,
            allocation.project_revision + 1,
            decision_models.ActionKind.CONSOLIDATE,
            HistorySubjectId(plan.campaign_id),
            None,
            decision_models.AuthorizationKind.PROJECT,
            actor,
            host,
            "pinboard-consolidation/v1",
            work_models.CanonicalJson(msgspec.json.encode(record, order="sorted")),
        )
        return transaction.commit(
            ConsolidationMutation(receipt, replace(change, source_closures=closures), current.queue_changes)
        )


def requested_dependencies(plan: consolidation.Plan) -> tuple[work_models.Dependency, ...]:
    cohort = {plan.campaign_id, *(value.item_id for value in plan.sources)}
    return tuple(value for value in plan.definition.dependencies if work_models.dependency_item(value) not in cohort)


def receipt_failure(receipt: stored_state.StoredTransitionReceipt) -> str | None:
    try:
        record = msgspec.json.decode(bytes(receipt.input_payload), type=models.Record)
        outcome = msgspec.json.decode(bytes(receipt.outcome_payload), type=history.TransitionReceiptOutcome)
    except (msgspec.DecodeError, ValueError) as error:
        return f"Consolidation receipt cannot be decoded: {error}"
    if receipt.input_schema != "pinboard-consolidation/v1" or msgspec.json.encode(record, order="sorted") != bytes(
        receipt.input_payload
    ):
        return "Consolidation input schema or canonical bytes differ."
    campaign = record.preview.campaign.item_id
    if receipt.subject_id != campaign or receipt.action_id != f"consolidate:{campaign}":
        return "Consolidation receipt does not name its campaign."
    if (
        receipt.authorization != decision_models.AuthorizationKind.PROJECT
        or receipt.actor_task_id is None
        or receipt.actor_host_id is None
    ):
        return "Consolidation receipt has no project attribution."
    if (
        receipt.outcome_schema != "transition-receipt/v1"
        or outcome.outcome != "consolidated"
        or outcome.evidence != record.human_confirmation
        or outcome.candidate is not None
        or outcome.checkpoint is not None
        or msgspec.json.encode(outcome, order="sorted") != bytes(receipt.outcome_payload)
    ):
        return "Consolidation outcome differs from the confirmed record."
    return None
