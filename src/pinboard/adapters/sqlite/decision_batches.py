"""Read current decision projections and the exact receipt history needed for landed review."""

import sqlite3
from collections.abc import Iterable

import msgspec

from pinboard.adapters.sqlite.database import decode_row, select_by_ids
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import (
    TransitionHistoryRow,
    decode_decision_batch_reference,
    read_history_receipts_by_ids,
    stored_receipt_from_row,
)
from pinboard.application import decision_batches, landing_decisions, query_models
from pinboard.domain.identifiers import AttemptId, HistoryId, WorkItemId


class _BatchOwner(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    item_id: WorkItemId


class _LandingDecisionBounds(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    accepted_revision: int
    subject_revision: int


def read_decision_batches(
    connection: sqlite3.Connection, attempt_ids: Iterable[AttemptId]
) -> dict[AttemptId, decision_batches.DecisionBatchView]:
    references = tuple(
        decode_decision_batch_reference(row)
        for row in select_by_ids(
            connection,
            "SELECT attempt_id, pause_history_id, resume_history_id FROM attempt_decision_batches WHERE attempt_id IN ({ids})",
            attempt_ids,
        )
    )
    owners = {
        owner.attempt_id: owner.item_id
        for owner in (
            decode_row(row, _BatchOwner)
            for row in select_by_ids(
                connection,
                "SELECT attempt_id, item_id FROM attempts WHERE attempt_id IN ({ids})",
                (ref.attempt_id for ref in references),
            )
        )
    }
    receipts = read_history_receipts_by_ids(
        connection,
        tuple(
            identity
            for ref in references
            for identity in (ref.pause_history_id, ref.resume_history_id)
            if identity is not None
        ),
    )
    try:
        return {
            ref.attempt_id: decision_batches.project_batch(
                ref,
                receipts[ref.pause_history_id],
                None if ref.resume_history_id is None else receipts[ref.resume_history_id],
                owners[ref.attempt_id],
            )
            for ref in references
        }
    except (ValueError, KeyError) as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Stored decision batch is invalid: {error}") from error


def read_landing_decisions(
    connection: sqlite3.Connection, attempt: query_models.NonterminalAttemptContextFacts
) -> landing_decisions.LandingDecisionBasis | None:
    bounds_row = connection.execute(
        """SELECT artifact_refs.accepted_revision, attempts.subject_revision
           FROM attempts JOIN artifact_refs ON artifact_refs.artifact_ref_id = attempts.brief_artifact_ref_id
           WHERE attempts.attempt_id = ? AND attempts.subject_revision = ? AND attempts.brief_artifact_ref_id = ?""",
        (attempt.attempt_id, attempt.subject_revision, attempt.brief_artifact_ref_id),
    ).fetchone()
    if bounds_row is None:
        return None
    bounds = decode_row(bounds_row, _LandingDecisionBounds)
    rows = connection.execute(
        """SELECT history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
                  authorization_kind AS authorization, actor_task_id, actor_host_id, input_schema,
                  input_json, outcome_schema, outcome_json, committed_at
           FROM transition_history
           WHERE project_revision > ? AND project_revision <= ? AND subject_id = ? AND action_kind = 'resume'
             AND input_schema IN (?, ?)
           ORDER BY project_revision""",
        (
            bounds.accepted_revision,
            bounds.subject_revision,
            attempt.work_item_id,
            decision_batches.SAME_BRIEF_RESUME_SCHEMA,
            decision_batches.REVISED_RESUME_SCHEMA,
        ),
    )
    try:
        resumes = landing_decisions.select_bound_resolutions(
            (stored_receipt_from_row(decode_row(row, TransitionHistoryRow)) for row in rows),
            attempt.brief_artifact_ref_id,
        )
        pauses = read_history_receipts_by_ids(
            connection, tuple(HistoryId(resolution.pause_history_id) for _, resolution in resumes)
        )
        return landing_decisions.decision_basis(
            attempt.attempt_id, attempt.work_item_id, resumes, pauses, attempt.amendments.current
        )
    except (ValueError, KeyError) as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Stored landing decisions are invalid: {error}") from error
