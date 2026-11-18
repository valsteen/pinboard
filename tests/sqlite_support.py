"""Test-only insertion of complete SQLite aggregate fixtures."""

import contextlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import msgspec

from pinboard.adapters.sqlite import database as sqlite_database
from pinboard.adapters.sqlite.artifact_ownership import accept_ownership
from pinboard.adapters.sqlite.database import APPLICATION, SCHEMA_VERSION
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import _definition_revision_values
from pinboard.adapters.sqlite.migration import database as migration_database
from pinboard.adapters.sqlite.migration.schema_amendments_v13 import upgrade_v13_connection
from pinboard.adapters.sqlite.migration.schema_holds_v14 import upgrade_v14_connection
from pinboard.adapters.sqlite.proposals import _encode_proposal_disposition_columns
from pinboard.adapters.sqlite.state import _validate_current_state, append_history
from pinboard.application import stored_state, unblockers
from pinboard.domain import work_models
from tests.domain_support import TEST_HOLD


def downgrade_to_v14(path: Path) -> None:
    """Rewrite a current v15 ledger without released-hold rows into the exact v14 predecessor schema."""

    statements = {name: sql for _, name, _, sql in sqlite_database._build_v14_schema_signature()}
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT count(*) FROM released_item_holds").fetchone()[0]:
            raise AssertionError("Only a ledger without released-hold rows can be rewritten as a v14 fixture.")
        connection.execute("DROP INDEX released_item_holds_by_item")
        connection.execute("DROP TABLE released_item_holds")
        connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v15")
        connection.execute(str(statements["project_meta"]))
        connection.execute(
            "INSERT INTO project_meta SELECT singleton, application, 14, revision, host_epoch, created_at, updated_at FROM project_meta_v15"
        )
        connection.execute("DROP TABLE project_meta_v15")
        sqlite_database._verify_v14_schema(connection)
        connection.execute("COMMIT")
        connection.execute("PRAGMA foreign_keys = ON")


def advanced_v14_copy(path: Path) -> Path:
    """Copy a v14 ledger beside itself and advance only the copy, so current readers can check v14 facts."""

    copy = path.with_name("state-v15-check.sqlite3")
    copy.write_bytes(path.read_bytes())
    with migration_database.migration_connection(copy) as connection:
        upgrade_v14_connection(connection, None)
    return copy


def downgrade_to_v13(path: Path) -> None:
    """Rewrite a current ledger without amendments or released holds into the exact v13 predecessor schema."""

    downgrade_to_v14(path)
    statements = {name: sql for _, name, _, sql in sqlite_database._build_v13_schema_signature()}
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT count(*) FROM attempt_amendments").fetchone()[0]:
            raise AssertionError("Only a ledger without amendments can be rewritten as a v13 fixture.")
        connection.execute("DROP TABLE attempt_amendments")
        connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v14")
        connection.execute(str(statements["project_meta"]))
        connection.execute(
            "INSERT INTO project_meta SELECT singleton, application, 13, revision, host_epoch, created_at, updated_at FROM project_meta_v14"
        )
        connection.execute("DROP TABLE project_meta_v14")
        sqlite_database._verify_v13_schema(connection)
        connection.execute("COMMIT")
        connection.execute("PRAGMA foreign_keys = ON")


def advanced_v13_copy(path: Path) -> Path:
    """Copy a v13 ledger beside itself and advance only the copy, so current readers can check v13 facts."""

    copy = path.with_name("state-v15-from-v13-check.sqlite3")
    copy.write_bytes(path.read_bytes())
    with migration_database.migration_connection(copy) as connection:
        upgrade_v13_connection(connection, None)
        upgrade_v14_connection(connection, None)
    return copy


def downgrade_to_v12(path: Path) -> None:
    """Rewrite an unheld current ledger into the exact v12 predecessor schema for migration journeys."""

    downgrade_to_v13(path)
    statements = {name: sql for _, name, _, sql in sqlite_database._build_v12_schema_signature()}
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT count(*) FROM work_item_unblockers").fetchone()[0]:
            raise AssertionError("Only a ledger without held items can be rewritten as a v12 fixture.")
        connection.execute("DROP TABLE work_item_unblockers")
        for table in ("work_items", "work_item_state_counts"):
            connection.execute(f"CREATE TEMP TABLE {table}_v13 AS SELECT * FROM {table}")
            connection.execute(f"DROP TABLE {table}")
            connection.execute(str(statements[table]))
            connection.execute(f"INSERT INTO {table} SELECT * FROM temp.{table}_v13")
            connection.execute(f"DROP TABLE temp.{table}_v13")
        connection.execute("INSERT INTO work_item_state_counts (state, item_count) VALUES ('deferred', 0)")
        connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v13")
        connection.execute(str(statements["project_meta"]))
        connection.execute(
            "INSERT INTO project_meta SELECT singleton, application, 12, revision, host_epoch, created_at, updated_at FROM project_meta_v13"
        )
        connection.execute("DROP TABLE project_meta_v13")
        sqlite_database._verify_v12_schema(connection)
        connection.execute("COMMIT")
        connection.execute("PRAGMA foreign_keys = ON")


def sync_test_unblocker(connection: sqlite3.Connection, item_id: str) -> None:
    """Keep a fixture that forces an item's state by SQL consistent with its current-unblocker row."""

    state = connection.execute("SELECT state FROM work_items WHERE item_id = ?", (item_id,)).fetchone()[0]
    connection.execute("DELETE FROM work_item_unblockers WHERE item_id = ?", (item_id,))
    if state in ("paused", "blocked"):
        connection.execute(
            "INSERT INTO work_item_unblockers (item_id, unblocker_json) VALUES (?, ?)",
            (item_id, unblockers.encode_unblocker(TEST_HOLD).decode("utf-8")),
        )


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
    for value in records:
        for owner in value.owners:
            accept_ownership(connection, value.artifact_ref_id, owner)


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
        "INSERT INTO item_dependencies (item_id, dependency_id, position, checkpoint_id) VALUES (?, ?, ?, ?)",
        tuple(
            (value.item_id, value.dependency_id, value.position, value.checkpoint_id) for value in records.dependencies
        ),
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
    connection.executemany(
        "INSERT INTO work_item_unblockers (item_id, unblocker_json) VALUES (?, ?)",
        tuple(
            (value.item_id, unblockers.encode_unblocker(value.unblocker).decode("utf-8"))
            for value in records.unblockers
        ),
    )


def _insert_proposals(connection: sqlite3.Connection, records: stored_state.ProposalRecords) -> None:
    connection.executemany(
        """
        INSERT INTO proposals (
            proposal_id, created_at, recorded_at, source_task_id, user_label,
            trigger, why_it_matters, relation_kind, relation_item_id, relation_replacement_cost, relation_checkpoint_id, effect, unlock,
            urgency_evidence, disposition, disposition_target_item_id, disposition_reason,
            disposition_recorded_at, subject_revision
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                value.relation.work_item_id,
                value.relation.replacement_cost
                if isinstance(value.relation, work_models.PlannedReplacementProposalRelation)
                else None,
                value.relation.dependency.checkpoint_id
                if isinstance(value.relation, work_models.FollowUpProposalRelation)
                and isinstance(value.relation.dependency, work_models.CheckpointDependency)
                else None,
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
    recorded = {value.item_id for value in state.lifecycle.unblockers}
    # A fixture that holds an item without naming its unblocker waits for the shared test condition.
    state = replace(
        state,
        lifecycle=replace(
            state.lifecycle,
            unblockers=(
                *state.lifecycle.unblockers,
                *(
                    stored_state.StoredUnblocker(value.item_id, TEST_HOLD)
                    for value in state.lifecycle.work_items
                    if value.state
                    in {stored_state.StoredWorkItemState.PAUSED, stored_state.StoredWorkItemState.BLOCKED}
                    and value.item_id not in recorded
                ),
            ),
        ),
    )
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
            "planned_replacements",
            "replacement_dispositions",
            "attempt_lease_counters",
            "attempt_lease_generations",
            "attempt_leases",
            "preparation_lease_counters",
            "preparation_lease_generations",
            "preparation_leases",
            "transition_history",
            "attempt_amendments",
            "released_item_holds",
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
    for item_state in stored_state.StoredWorkItemState:
        connection.execute(
            "UPDATE work_item_state_counts SET item_count = ? WHERE state = ?",
            (
                sum(1 for item in state.lifecycle.work_items if item.state == item_state),
                item_state.value,
            ),
        )
    _insert_proposals(connection, state.proposals)
    connection.executemany(
        """
        INSERT INTO planned_replacements (
            affected_item_id, relation_revision, replacement_item_id, replacement_cost,
            status, recorded_by, recorded_at, accepted_project_revision
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(
            (
                value.affected_item_id,
                value.relation_revision,
                value.replacement_item_id,
                value.replacement_cost,
                value.status.value,
                value.recorded_by,
                value.recorded_at.isoformat(),
                value.accepted_project_revision,
            )
            for value in state.replacements.planned_replacements
        ),
    )
    connection.executemany(
        """
        INSERT INTO replacement_dispositions (
            affected_item_id, relation_revision, rationale, accepted_cost,
            recorded_by, recorded_at, accepted_project_revision
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(
            (
                value.affected_item_id,
                value.relation_revision,
                value.rationale,
                value.accepted_cost,
                value.recorded_by,
                value.recorded_at.isoformat(),
                value.accepted_project_revision,
            )
            for value in state.replacements.dispositions
        ),
    )
    _insert_authority(connection, state.authority)
    append_history(connection, state.transition_receipts)
    connection.executemany(
        """
        INSERT INTO released_item_holds (
            history_id, item_id, previous_state, previous_subject_revision, unblocker_json
        ) VALUES (?, ?, ?, ?, ?)
        """,
        tuple(
            (
                value.history_id,
                value.item_id,
                value.previous_state.value,
                value.previous_subject_revision,
                unblockers.encode_unblocker(value.unblocker).decode("utf-8"),
            )
            for value in state.lifecycle.released_holds
        ),
    )
    connection.execute(
        """
        UPDATE project_meta
        SET revision = ?, host_epoch = ?, created_at = ?, updated_at = ?
        WHERE singleton = 1
        """,
        (project.revision, project.host_epoch, project.created_at.isoformat(), project.updated_at.isoformat()),
    )
