"""Select only the named cancellation item, its current authority and direct relationships."""

import sqlite3

from pinboard.adapters.sqlite.authority import (
    fence_attempt_authority,
    read_attempt_authority_status,
    read_preparation_authority_status,
)
from pinboard.adapters.sqlite.database import decode_row, select_by_ids
from pinboard.adapters.sqlite.lifecycle import read_current_definitions, set_attempt_state, set_item_state
from pinboard.adapters.sqlite.models import ItemIdRow
from pinboard.adapters.sqlite.obligation_coverage import planned_report
from pinboard.application import cancellation_models as models
from pinboard.application import stored_state
from pinboard.application.mutation_models import CancellationMutation
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import WorkItemId

ITEM_COLUMNS = "item_id, state, timing, source, outcome_evidence, next_action, notes, subject_revision, recorded_at, updated_at, queue_position"
ATTEMPT_COLUMNS = "attempt_id, item_id, state, branch, base_revision, provenance, brief_artifact_ref_id, result_artifact_ref_id, candidate_revision, candidate_recorded_at, accepted_scope_revision, accepted_scope_digest, subject_revision, recorded_at, updated_at"


def read_preview(connection: sqlite3.Connection, item_id: WorkItemId) -> models.CancellationPreview | None:
    row = connection.execute(f"SELECT {ITEM_COLUMNS} FROM work_items WHERE item_id = ?", (item_id,)).fetchone()
    if row is None:
        return None
    item = decode_row(row, stored_state.StoredWorkItem)
    if stored_state.live_work_state(item.state) is None:
        return None
    definition = read_current_definitions(connection, (item_id,))[item_id]
    prerequisite_ids = tuple(
        work_models.dependency_item(WorkItemId(value) if isinstance(value, str) else value)
        for value in definition.definition.dependencies
    )
    dependent_ids = tuple(
        decode_row(row, ItemIdRow).item_id
        for row in connection.execute(
            "SELECT dependency.item_id FROM item_dependencies AS dependency INDEXED BY item_dependencies_by_dependency JOIN work_items AS item ON item.item_id = dependency.item_id WHERE dependency.dependency_id = ? AND item.queue_position IS NOT NULL ORDER BY dependency.item_id",
            (item_id,),
        )
    )
    related_ids = tuple(dict.fromkeys((*prerequisite_ids, *dependent_ids)))
    definitions = read_current_definitions(connection, related_ids)
    related = {}
    for row in select_by_ids(
        connection, f"SELECT {ITEM_COLUMNS} FROM work_items WHERE item_id IN ({{ids}})", related_ids
    ):
        value = decode_row(row, stored_state.StoredWorkItem)
        current = definitions[value.item_id]
        related[value.item_id] = models.CancellationRelatedItem(
            value.item_id,
            current.definition.title,
            value.state,
            value.subject_revision,
            current.revision,
            current.digest,
        )
    attempt = None
    row = connection.execute(
        f"SELECT {ATTEMPT_COLUMNS} FROM attempts WHERE item_id = ? AND state != 'done'", (item_id,)
    ).fetchone()
    if row is not None:
        value = decode_row(row, stored_state.StoredAttempt)
        authority = read_attempt_authority_status(connection, value.attempt_id)
        attempt = models.CancellationAttempt(
            value.attempt_id,
            value.state,
            value.subject_revision,
            value.branch,
            value.base_revision,
            value.accepted_scope_revision,
            value.accepted_scope_digest,
            value.brief_artifact_ref_id,
            value.result_artifact_ref_id,
            value.candidate_revision,
            None
            if authority is None
            else models.CancellationAuthority(
                authority.task_id,
                authority.host_id,
                authority.lease_id,
                authority.generation,
                authority.status.value,
                authority.expires_at,
                authority.acquired_at,
            ),
        )
    preparation = read_preparation_authority_status(connection, item_id)
    return models.CancellationPreview(
        item_id,
        definition.definition.title,
        item.state,
        item.subject_revision,
        definition.revision,
        definition.digest,
        definition.definition.objective,
        tuple(definition.definition.scope),
        attempt,
        None
        if preparation is None
        else models.CancellationPreparation(
            preparation.task_id,
            preparation.host_id,
            preparation.lease_id,
            preparation.generation,
            preparation.status.value,
            preparation.expires_at,
            preparation.acquired_at,
            preparation.definition_revision,
            preparation.definition_digest,
        ),
        tuple(related[value] for value in prerequisite_ids),
        tuple(related[value] for value in dependent_ids),
        "dropped",
        "Definitions, attempts, history, reviews, candidate evidence, code, branches and worktrees are retained.",
        "Whole-item dependencies on dropped items remain unsatisfied; matching accepted checkpoints remain satisfied; dependent commitments are retained.",
        "Pinboard authority is fenced; local processes are not stopped. Stop or await your known worker before confirming.",
        planned_report(connection, item_id),
    )


def persist(
    connection: sqlite3.Connection,
    item: stored_state.StoredWorkItem,
    attempt: stored_state.StoredAttempt | None,
    mutation: CancellationMutation,
) -> DecisionFailure | None:
    now = mutation.receipt.transition.decided_at
    revision = mutation.receipt.project_revision
    before = stored_state.live_work_state(item.state)
    if before is None:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, "The cancellation item is no longer live.", None
        )
    if (
        failure := set_item_state(
            connection,
            item,
            before,
            stored_state.StoredWorkItemState.DROPPED,
            revision,
            now,
            mutation.receipt.transition.evidence,
        )
    ) is not None:
        return failure
    if attempt is not None:
        if (
            failure := set_attempt_state(
                connection, attempt, attempt.state, work_models.AttemptState.DONE, revision, now
            )
        ) is not None:
            return failure
        authority = mutation.preview.attempt
        assert authority is not None
        if authority.authority is not None:
            current = authority.authority
            if (
                failure := fence_attempt_authority(
                    connection,
                    decision_models.AttemptAuthorityChange(
                        work_models.AttemptAuthority(
                            attempt.attempt_id, item.item_id, current.lease_id, current.generation
                        ),
                        work_models.AttemptAuthority(attempt.attempt_id, item.item_id, None, current.generation + 1),
                    ),
                    now,
                )
            ) is not None:
                return failure
    preparation = mutation.preview.preparation
    if preparation is not None:
        connection.execute(
            "UPDATE preparation_lease_counters SET generation_high_water = ? WHERE item_id = ?",
            (preparation.generation + 1, item.item_id),
        )
        connection.execute(
            "INSERT INTO preparation_lease_generations (item_id, generation, lease_id, task_id, host_id) VALUES (?, ?, ?, ?, ?)",
            (item.item_id, preparation.generation + 1, preparation.lease_id, preparation.task_id, preparation.host_id),
        )
        connection.execute(
            "UPDATE preparation_leases SET generation = ?, expires_at = ?, status = 'revoked' WHERE item_id = ?",
            (preparation.generation + 1, now.isoformat(), item.item_id),
        )
    return None
