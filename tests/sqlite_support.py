"""Test-only insertion of complete SQLite aggregate fixtures."""

import sqlite3

import msgspec

from pinboard.adapters.sqlite.database import APPLICATION, SCHEMA_VERSION
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import _definition_revision_values
from pinboard.adapters.sqlite.proposals import _encode_proposal_disposition_columns
from pinboard.adapters.sqlite.state import _validate_current_state, append_history
from pinboard.application import stored_state


def _insert_artifacts(
    connection: sqlite3.Connection,
    records: tuple[stored_state.ArtifactReference, ...],
) -> None:
    connection.executemany(
        """
        INSERT INTO artifact_refs (
            artifact_ref_id, artifact_key, artifact_revision, kind, relative_path, content_sha256,
            size_bytes, accepted_revision, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(
            (
                value.artifact_ref_id,
                value.key,
                value.revision,
                value.kind.value,
                value.selector,
                value.content_sha256,
                value.size_bytes,
                value.accepted_revision,
                value.created_at.isoformat(),
            )
            for value in records
        ),
    )


def _insert_lifecycle(connection: sqlite3.Connection, records: stored_state.LifecycleRecords) -> None:
    connection.executemany(
        """
        INSERT INTO work_items (
            item_id, state, timing, source, outcome_evidence, next_action, notes, subject_revision,
            recorded_at, updated_at, queue_position
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(
            (
                value.item_id,
                value.state.value,
                None if value.timing is None else value.timing.value,
                value.source,
                value.outcome_evidence,
                value.next_action,
                value.notes,
                value.subject_revision,
                value.recorded_at.isoformat(),
                value.updated_at.isoformat(),
                value.queue_position,
            )
            for value in records.work_items
        ),
    )
    connection.executemany(
        "INSERT INTO item_dependencies (item_id, dependency_id, position) VALUES (?, ?, ?)",
        tuple((value.item_id, value.dependency_id, value.position) for value in records.dependencies),
    )
    connection.executemany(
        """
        INSERT INTO work_item_definition_revisions (
            item_id, definition_revision, definition_digest, definition_json, reason,
            source_task_id, before_digest, after_digest, accepted_project_revision, accepted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(_definition_revision_values(value) for value in records.definition_revisions),
    )
    connection.executemany(
        """
        INSERT INTO attempts (
            attempt_id, item_id, state, branch, base_revision, provenance,
            brief_artifact_ref_id, brief_artifact_kind, result_artifact_ref_id, result_artifact_kind,
            candidate_revision, candidate_recorded_at,
            accepted_scope_revision, accepted_scope_digest, subject_revision, recorded_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'brief', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(
            (
                value.attempt_id,
                value.item_id,
                value.state.value,
                value.branch,
                value.base_revision,
                value.provenance,
                value.brief_artifact_ref_id,
                value.result_artifact_ref_id,
                None if value.result_artifact_ref_id is None else "result",
                value.candidate_revision,
                None if value.candidate_recorded_at is None else value.candidate_recorded_at.isoformat(),
                value.accepted_scope_revision,
                value.accepted_scope_digest,
                value.subject_revision,
                value.recorded_at.isoformat(),
                value.updated_at.isoformat(),
            )
            for value in records.attempts
        ),
    )


def _insert_proposals(connection: sqlite3.Connection, records: stored_state.ProposalRecords) -> None:
    connection.executemany(
        """
        INSERT INTO proposals (
            proposal_id, created_at, recorded_at, source_task_id, user_label,
            trigger, why_it_matters, relation_kind, relation_item_id, effect, unlock,
            urgency_evidence, disposition, disposition_target_item_id, disposition_reason,
            disposition_recorded_at, subject_revision
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(
            (
                value.proposal_id,
                value.created_at.isoformat(),
                value.recorded_at.isoformat(),
                value.source_task_id,
                value.user_label,
                value.trigger,
                value.why_it_matters,
                value.relation.kind.value,
                value.relation.item,
                value.effect,
                value.unlock,
                value.urgency_evidence,
                *_encode_proposal_disposition_columns(value.disposition),
                value.subject_revision,
            )
            for value in records.proposals
        ),
    )
    connection.executemany(
        "INSERT INTO proposal_evidence (proposal_id, position, selector) VALUES (?, ?, ?)",
        tuple((value.proposal_id, value.position, value.selector) for value in records.evidence),
    )
    connection.executemany(
        "INSERT INTO proposal_freshness (proposal_id, position, assumption) VALUES (?, ?, ?)",
        tuple((value.proposal_id, value.position, value.assumption) for value in records.freshness),
    )


def _insert_authority(connection: sqlite3.Connection, records: stored_state.AuthorityRecords) -> None:
    connection.executemany(
        "INSERT INTO attempt_lease_counters (attempt_id, generation_high_water) VALUES (?, ?)",
        tuple((value.attempt_id, value.generation_high_water) for value in records.attempt_counters),
    )
    connection.executemany(
        """
        INSERT INTO attempt_lease_generations (attempt_id, generation, lease_id, task_id, host_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        tuple(
            (value.attempt_id, value.generation, value.lease_id, value.task_id, value.host_id)
            for value in records.attempt_generations
        ),
    )
    connection.executemany(
        "INSERT INTO preparation_lease_counters (item_id, generation_high_water) VALUES (?, ?)",
        tuple((value.item_id, value.generation_high_water) for value in records.preparation_counters),
    )
    connection.executemany(
        """
        INSERT INTO preparation_lease_generations (item_id, generation, lease_id, task_id, host_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        tuple(
            (value.item_id, value.generation, value.lease_id, value.task_id, value.host_id)
            for value in records.preparation_generations
        ),
    )
    connection.executemany(
        """
        INSERT INTO preparation_leases (
            item_id, generation, definition_revision, definition_digest, acquired_at, expires_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(
            (
                value.item_id,
                value.generation,
                value.definition_revision,
                value.definition_digest,
                value.acquired_at.isoformat(),
                value.expires_at.isoformat(),
                value.state.value,
            )
            for value in records.preparation_leases
        ),
    )
    connection.executemany(
        """
        INSERT INTO attempt_leases (attempt_id, generation, acquired_at, expires_at, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        tuple(
            (
                value.attempt_id,
                value.generation,
                value.acquired_at.isoformat(),
                value.expires_at.isoformat(),
                value.state.value,
            )
            for value in records.attempt_leases
        ),
    )


def insert_initial_state(connection: sqlite3.Connection, state: stored_state.StoredWorkState) -> None:
    project = state.lifecycle.project
    if project.application != APPLICATION or project.schema_version != SCHEMA_VERSION:
        raise StorageError(
            StorageErrorCode.INVALID_STATE, "Stored state does not match the current application schema."
        )
    _validate_current_state(state, StorageErrorCode.INVARIANT_VIOLATION)
    occupied_rows = (
        row["count"]
        for table in (
            "artifact_refs",
            "work_items",
            "item_dependencies",
            "attempts",
            "proposals",
            "proposal_evidence",
            "proposal_freshness",
            "attempt_lease_counters",
            "attempt_lease_generations",
            "attempt_leases",
            "preparation_lease_counters",
            "preparation_lease_generations",
            "preparation_leases",
            "transition_history",
        )
        for row in connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchall()
    )
    try:
        occupied = sum(msgspec.convert(tuple(occupied_rows), type=tuple[int, ...], strict=True))
    except msgspec.ValidationError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Stored row is invalid: {error}") from error
    current_revision = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
    if occupied != 0 or current_revision is None or current_revision[0] != 0:
        raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "Initial state requires a new empty database.")
    connection.execute("PRAGMA defer_foreign_keys = ON")
    _insert_artifacts(connection, state.artifact_references)
    _insert_lifecycle(connection, state.lifecycle)
    _insert_proposals(connection, state.proposals)
    _insert_authority(connection, state.authority)
    append_history(connection, state.transition_receipts)
    connection.execute(
        """
        UPDATE project_meta
        SET revision = ?, host_epoch = ?, created_at = ?, updated_at = ?
        WHERE singleton = 1
        """,
        (project.revision, project.host_epoch, project.created_at.isoformat(), project.updated_at.isoformat()),
    )
