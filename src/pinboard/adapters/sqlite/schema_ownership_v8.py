"""Verify released v8 evidence and backfill ownership inside the migration transaction.

Only this predecessor boundary interprets the released free-name layout. It never
rewrites accepted bytes, references, receipts, definitions, or archive certificates.
"""

import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import msgspec

from pinboard.adapters.files.artifacts import read_reference
from pinboard.adapters.sqlite import database
from pinboard.adapters.sqlite.artifact_ownership import accept_ownership
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.schema_dependencies_v9 import _upgrade_v9_connection
from pinboard.adapters.sqlite.state import read_state
from pinboard.application import (
    artifact_identity,
    artifact_ownership_compatibility_v8,
    candidate_snapshots,
    history_archives,
    lifecycle_packages,
    stored_state,
    work_brief_models,
    work_briefs,
)
from pinboard.domain import history, work_models
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, CheckpointId


def _backfill_unbound_briefs(
    connection: sqlite3.Connection, state: stored_state.StoredWorkState, data: dict[ArtifactRefId, bytes]
) -> None:
    known_attempts = {value.attempt_id for value in state.lifecycle.attempts}
    for reference in state.artifact_references:
        if reference.kind != work_models.ArtifactKind.BRIEF or not reference.selector.endswith(".json"):
            continue
        brief = work_briefs.decode_canonical_historical_work_brief(data[reference.artifact_ref_id])
        if isinstance(brief, work_brief_models.WorkBriefFailure):
            continue
        if AttemptId(brief.attempt_id) in known_attempts:
            continue
        for source in artifact_ownership_compatibility_v8.select_v8_artifact_references(
            brief.attempt_id, state.artifact_references, frozenset((reference.artifact_ref_id,)), frozenset()
        ):
            accept_ownership(
                connection,
                source.artifact_ref_id,
                artifact_identity.AttemptMembership(AttemptId(brief.attempt_id), artifact_identity.AttemptRole.SOURCE),
            )


def _backfill_attempts(
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkState,
    references: dict[tuple[str, str, int], stored_state.ArtifactReference],
    data: dict[ArtifactRefId, bytes],
) -> tuple[dict[AttemptId, history_archives.HistoryArchive], set[ArtifactRefId]]:
    archives: dict[AttemptId, history_archives.HistoryArchive] = {}
    linked_archives: set[ArtifactRefId] = set()
    for attempt in state.lifecycle.attempts:
        facts = artifact_ownership_compatibility_v8.select_v8_archive_facts(
            attempt, state.artifact_references, state.transition_receipts, state.lifecycle.definition_revisions
        )
        for reference in facts.artifact_references:
            accept_ownership(
                connection,
                reference.artifact_ref_id,
                artifact_identity.AttemptMembership(attempt.attempt_id, artifact_identity.AttemptRole.SOURCE),
            )
        certificate = references.get(("evidence", history_archives.archive_key(str(attempt.attempt_id)), 1))
        if certificate is not None:
            archive = history_archives.decode_archive(data[certificate.artifact_ref_id])
            if isinstance(archive, work_brief_models.WorkBriefFailure):
                raise ValueError(archive.message)
            if (archive.attempt_id, archive.item_id) != (str(attempt.attempt_id), str(attempt.item_id)):
                raise ValueError("The v8 archive names another attempt or item.")
            archives[attempt.attempt_id] = archive
            linked_archives.add(certificate.artifact_ref_id)
            accept_ownership(
                connection,
                certificate.artifact_ref_id,
                artifact_identity.AttemptMembership(attempt.attempt_id, artifact_identity.AttemptRole.ARCHIVE),
            )

    return archives, linked_archives


def _backfill_checkpoint(
    connection: sqlite3.Connection,
    reference: stored_state.ArtifactReference,
    receipt: stored_state.StoredTransitionReceipt,
    references: dict[tuple[str, str, int], stored_state.ArtifactReference],
    data: dict[ArtifactRefId, bytes],
    archives: dict[AttemptId, history_archives.HistoryArchive],
) -> None:
    attempt_id = AttemptId(str(receipt.subject_id))
    outcome = msgspec.json.decode(bytes(receipt.outcome_payload), type=history.CheckpointAcceptanceOutcome)
    checkpoint_id = CheckpointId(outcome.checkpoint)
    prefix = f"{attempt_id}-{checkpoint_id}"
    if (reference.kind, reference.key, reference.revision) != (
        work_models.ArtifactKind.EVIDENCE,
        f"{prefix}-review-package",
        1,
    ):
        raise ValueError("The v8 checkpoint package differs from its receipt-selected identity.")
    accept_ownership(
        connection,
        reference.artifact_ref_id,
        artifact_identity.CheckpointAddress(attempt_id, checkpoint_id, artifact_identity.CheckpointRole.PACKAGE, 1),
    )
    if history_archives.retired_checkpoint_package(data[reference.artifact_ref_id]):
        if attempt_id not in archives:
            raise ValueError("A retired v8 checkpoint requires its original independently verified archive.")
        for role, kind, suffix in (
            (artifact_identity.CheckpointRole.CANDIDATE, "evidence", "candidate"),
            (artifact_identity.CheckpointRole.RESULT, "result", "result"),
            (artifact_identity.CheckpointRole.REVIEW, "evidence", "review"),
        ):
            original = references.get((kind, f"{prefix}-{suffix}", 1))
            if original is not None:
                accept_ownership(
                    connection,
                    original.artifact_ref_id,
                    artifact_identity.CheckpointAddress(attempt_id, checkpoint_id, role, original.revision),
                )
        return
    package = work_briefs.decode_canonical_checkpoint_review_package(data[reference.artifact_ref_id])
    if isinstance(package, work_brief_models.WorkBriefFailure):
        raise ValueError(package.message)
    if (package.attempt_id, package.checkpoint.id) != (str(attempt_id), str(checkpoint_id)):
        raise ValueError("The v8 checkpoint payload differs from its acceptance.")
    for role, identity, kind, suffix in (
        (artifact_identity.CheckpointRole.CANDIDATE, package.candidate_snapshot, "evidence", "candidate"),
        (artifact_identity.CheckpointRole.RESULT, package.result, "result", "result"),
        (artifact_identity.CheckpointRole.REVIEW, package.implementation_review, "evidence", "review"),
    ):
        if (identity.kind, identity.key, identity.revision) != (kind, f"{prefix}-{suffix}", 1):
            raise ValueError("A v8 checkpoint role names another original artifact.")
        original = references.get((identity.kind, identity.key, identity.revision))
        if original is None:
            raise ValueError("A v8 checkpoint artifact is unavailable.")
        accept_ownership(
            connection,
            original.artifact_ref_id,
            artifact_identity.CheckpointAddress(attempt_id, checkpoint_id, role, original.revision),
        )


def _backfill_completion(
    connection: sqlite3.Connection,
    reference: stored_state.ArtifactReference,
    receipt: stored_state.StoredTransitionReceipt,
    references: dict[tuple[str, str, int], stored_state.ArtifactReference],
    data: dict[ArtifactRefId, bytes],
) -> None:
    attempt_id = AttemptId(str(receipt.subject_id))
    package = work_briefs.decode_canonical_historical_completion_review_package(data[reference.artifact_ref_id])
    if isinstance(package, work_brief_models.WorkBriefFailure):
        raise ValueError(package.message)
    if package.attempt_id != str(attempt_id) or (reference.kind, reference.key, reference.revision) != (
        work_models.ArtifactKind.EVIDENCE,
        f"{attempt_id}-completion-review-package",
        1,
    ):
        raise ValueError("The v8 completion package differs from its receipt-selected identity.")
    accept_ownership(
        connection,
        reference.artifact_ref_id,
        artifact_identity.CompletionAddress(attempt_id, artifact_identity.CompletionRole.PACKAGE, 1),
    )
    for role, identity, kind, suffix in (
        (artifact_identity.CompletionRole.RESULT, package.terminal_result, "result", "result"),
        (artifact_identity.CompletionRole.REVIEW, package.final_review, "evidence", "review"),
    ):
        if (identity.kind, identity.key, identity.revision) != (kind, f"{attempt_id}-terminal-{suffix}", 1):
            raise ValueError("A v8 completion role names another original artifact.")
        original = references.get((identity.kind, identity.key, identity.revision))
        if original is None:
            raise ValueError("A v8 completion artifact is unavailable.")
        accept_ownership(
            connection,
            original.artifact_ref_id,
            artifact_identity.CompletionAddress(attempt_id, role, original.revision),
        )


def _read_compatibility_v9_state(connection: sqlite3.Connection) -> stored_state.StoredWorkState:
    """Project predecessor facts through current readers without changing its retained schema meaning."""
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as projection:
        projection.executescript("\n".join(connection.iterdump()))
        database._configure_connection(projection)
        _upgrade_v9_connection(projection, None)
        return read_state(projection)


def _verify_backfill(connection: sqlite3.Connection, data: dict[ArtifactRefId, bytes]) -> None:
    migrated = _read_compatibility_v9_state(connection)
    verified_archives = history_archives.verify_archives(migrated, data)
    if isinstance(verified_archives, work_brief_models.WorkBriefFailure):
        raise ValueError(verified_archives.message)
    candidate_snapshots.validate_candidate_snapshot_history(migrated, data, verified_archives)
    checkpoints = lifecycle_packages.validate_checkpoint_review_packages(
        migrated.lifecycle, migrated.artifact_references, migrated.transition_receipts, data, verified_archives
    )
    if isinstance(checkpoints, work_brief_models.WorkBriefFailure):
        raise ValueError(checkpoints.message)
    completions = lifecycle_packages.validate_completion_review_packages(
        migrated.lifecycle,
        migrated.artifact_references,
        migrated.transition_receipts,
        data,
        checkpoints,
        verified_archives,
    )
    if isinstance(completions, work_brief_models.WorkBriefFailure):
        raise ValueError(completions.message)


def backfill_ownership(connection: sqlite3.Connection, work_root: Path) -> None:
    """Reject inconsistent predecessor facts before the caller's authority commit."""
    state = _read_compatibility_v9_state(connection)
    references = {(value.kind.value, value.key, value.revision): value for value in state.artifact_references}
    by_id = {value.artifact_ref_id: value for value in state.artifact_references}
    data = {value.artifact_ref_id: read_reference(work_root, value) for value in state.artifact_references}
    _backfill_unbound_briefs(connection, state, data)
    archives, linked_archives = _backfill_attempts(connection, state, references, data)
    linked_packages: set[ArtifactRefId] = set()
    for receipt in state.transition_receipts:
        if receipt.outcome_schema not in ("checkpoint-acceptance/v2", "completion-acceptance/v2"):
            continue
        reference = by_id.get(receipt.artifact_ref_id) if receipt.artifact_ref_id is not None else None
        if reference is None:
            raise ValueError("A v8 acceptance is missing its original package reference.")
        linked_packages.add(reference.artifact_ref_id)
        if receipt.outcome_schema == "checkpoint-acceptance/v2":
            _backfill_checkpoint(connection, reference, receipt, references, data, archives)
        else:
            _backfill_completion(connection, reference, receipt, references, data)
    if any(
        value.kind == work_models.ArtifactKind.EVIDENCE
        and value.key.endswith("-review-package")
        and value.artifact_ref_id not in linked_packages
        for value in state.artifact_references
    ):
        raise ValueError("A v8 accepted review package has no authoritative acceptance receipt.")
    if any(
        value.kind == work_models.ArtifactKind.EVIDENCE
        and value.key.endswith("-history-archive")
        and value.artifact_ref_id not in linked_archives
        for value in state.artifact_references
    ):
        raise ValueError("A v8 archive certificate has no exact terminal attempt.")
    _verify_backfill(connection, data)


def _upgrade_v8_connection(connection: sqlite3.Connection, work_root: Path, expected_postimage: str | None) -> bool:
    """Install verified ownership with one authority commit; errors roll it all back."""
    connection.row_factory = sqlite3.Row
    with database.write_transaction(connection):
        version = database._read_pinboard_schema_version(connection)
        if version == 9:
            database._verify_v9_schema(connection)
            return False
        database._verify_v8_schema(connection)
        database.verify_database_integrity(connection)
        connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v8")
        for _, name, _, statement in sorted(
            database._build_v9_schema_signature(), key=lambda value: value[0] != "table"
        ):
            if name in (
                "project_meta",
                "artifact_ownership",
                "artifact_ownership_by_attempt",
                "artifact_ownership_by_identity",
            ):
                assert statement is not None
                connection.execute(statement)
        connection.execute(
            "INSERT INTO project_meta SELECT singleton, application, 9, revision, host_epoch, created_at, updated_at FROM project_meta_v8"
        )
        connection.execute("DROP TABLE project_meta_v8")
        backfill_ownership(connection, work_root)
        database._verify_v9_schema(connection)
        database.verify_database_integrity(connection)
        if expected_postimage is not None and database.schema_logical_digest(connection) != expected_postimage:
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                "Ownership migration differs from the previewed postimage; the predecessor remains authoritative.",
            )
    return True


def inspect_current_schema_migration(path: Path) -> tuple[int, str, str | None]:
    """Observe exact supported identities and predict the next explicit advancement without effects."""
    try:
        source = sha256(path.read_bytes()).hexdigest()
        with closing(
            sqlite3.connect(database._build_database_uri(path, OpenMode.READ_ONLY), uri=True, isolation_level=None)
        ) as connection:
            database._configure_connection(connection)
            version = database._read_pinboard_schema_version(connection)
            if version == 6:
                return database.inspect_schema_migration(path)
            if version == database.SCHEMA_VERSION:
                database._verify_current_schema(connection)
                database.verify_database_integrity(connection)
                return version, source, None
            if version == 9:
                database._verify_v9_schema(connection)
            elif version == 8:
                database._verify_v8_schema(connection)
            elif version == 7:
                database._verify_v7_schema(connection)
            else:
                raise StorageError(
                    StorageErrorCode.SCHEMA_UNSUPPORTED, f"Schema sqlite-v{version} has no supported migration step."
                )
            database.verify_database_integrity(connection)
            with closing(sqlite3.connect(":memory:", isolation_level=None)) as simulation:
                connection.backup(simulation)
                database._configure_connection(simulation)
                if version == 9:
                    _upgrade_v9_connection(simulation, None)
                elif version == 8:
                    _upgrade_v8_connection(simulation, path.parent, None)
                else:
                    database._upgrade_v7_connection(simulation)
                return version, source, database.schema_logical_digest(simulation)
    except OSError as error:
        raise StorageError(
            StorageErrorCode.IO_ERROR, "The current schema migration could not read the database."
        ) from error
    except sqlite3.Error as error:
        raise database.translate_database_error(error, opening=True).with_database_path(path) from error


def migrate_v8_database(path: Path, expected_postimage: str) -> bool:
    with database.migration_connection(path) as connection:
        return _upgrade_v8_connection(connection, path.parent, expected_postimage)
