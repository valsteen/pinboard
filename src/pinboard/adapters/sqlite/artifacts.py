"""Read and accept artifact records on a supplied connection.

Artifact acceptance records the exact reference returned by the trusted immutable
publisher and never reads artifact files. Transaction control and time belong to
the caller. Expected stale writes return a ``DecisionFailure``; malformed
persisted state remains exceptional.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from pinboard.adapters.sqlite.database import decode_row, require_one_changed_row
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.application import stored_state
from pinboard.application.artifacts import (
    ArtifactRef,
    EvidenceArtifactRef,
    ResultArtifactRef,
)
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionResult
from pinboard.domain.identifiers import ArtifactRefId


@dataclass(frozen=True, slots=True)
class _ProjectRevision:
    revision: int


def _find_accepted_artifact(
    state: stored_state.StoredWorkState,
    published: ArtifactRef | ResultArtifactRef | EvidenceArtifactRef,
) -> stored_state.ArtifactReference | None:
    return next(
        (
            value
            for value in state.artifact_references
            if (value.kind, value.key, value.revision) == (published.kind, published.key, published.revision)
        ),
        None,
    )


def _insert_artifact(connection: sqlite3.Connection, reference: stored_state.ArtifactReference) -> None:
    connection.execute(
        """
        INSERT INTO artifact_refs (
            artifact_ref_id, artifact_key, artifact_revision, kind, relative_path,
            content_sha256, size_bytes, accepted_revision, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reference.artifact_ref_id,
            reference.key,
            reference.revision,
            reference.kind.value,
            reference.selector,
            reference.content_sha256,
            reference.size_bytes,
            reference.accepted_revision,
            reference.created_at.isoformat(),
        ),
    )


def read_artifacts(connection: sqlite3.Connection) -> tuple[stored_state.ArtifactReference, ...]:
    return tuple(
        decode_row(row, stored_state.ArtifactReference)
        for row in connection.execute(
            """
            SELECT artifact_ref_id, artifact_key AS key, artifact_revision AS revision, kind,
                   relative_path AS selector, content_sha256, size_bytes, accepted_revision, created_at
            FROM artifact_refs
            ORDER BY artifact_ref_id
            """
        ).fetchall()
    )


def read_selected_artifacts(
    connection: sqlite3.Connection,
    artifact_ref_ids: tuple[ArtifactRefId, ...],
) -> tuple[stored_state.ArtifactReference, ...]:
    """Read only accepted references named by the current operation facts."""

    if not artifact_ref_ids:
        return ()
    placeholders = ", ".join("?" for _value in artifact_ref_ids)
    return tuple(
        decode_row(row, stored_state.ArtifactReference)
        for row in connection.execute(
            f"""
            SELECT artifact_ref_id, artifact_key AS key, artifact_revision AS revision, kind,
                   relative_path AS selector, content_sha256, size_bytes, accepted_revision, created_at
            FROM artifact_refs
            WHERE artifact_ref_id IN ({placeholders})
            ORDER BY artifact_ref_id
            """,
            artifact_ref_ids,
        ).fetchall()
    )


def read_latest_artifact(connection: sqlite3.Connection) -> stored_state.ArtifactReference | None:
    row = connection.execute(
        """
        SELECT artifact_ref_id, artifact_key AS key, artifact_revision AS revision, kind,
               relative_path AS selector, content_sha256, size_bytes, accepted_revision, created_at
        FROM artifact_refs
        ORDER BY artifact_ref_id DESC
        LIMIT 1
        """
    ).fetchone()
    return None if row is None else decode_row(row, stored_state.ArtifactReference)


def read_artifact_by_identity(
    connection: sqlite3.Connection,
    kind: work_models.ArtifactKind,
    key: str,
    revision: int,
) -> stored_state.ArtifactReference | None:
    row = connection.execute(
        """
        SELECT artifact_ref_id, artifact_key AS key, artifact_revision AS revision, kind,
               relative_path AS selector, content_sha256, size_bytes, accepted_revision, created_at
        FROM artifact_refs
        WHERE kind = ? AND artifact_key = ? AND artifact_revision = ?
        """,
        (kind.value, key, revision),
    ).fetchone()
    return None if row is None else decode_row(row, stored_state.ArtifactReference)


def accept_checkpoint_artifact(
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkState,
    published: ResultArtifactRef | EvidenceArtifactRef,
    expected_id: ArtifactRefId,
    revision: int,
    now: datetime,
) -> ArtifactRefId:
    existing = _find_accepted_artifact(state, published)
    if existing is not None:
        if existing.artifact_ref_id != expected_id or (
            existing.selector,
            existing.content_sha256,
            existing.size_bytes,
        ) != (published.selector, published.content_sha256, published.size_bytes):
            raise StorageError(
                StorageErrorCode.INVARIANT_VIOLATION,
                "An accepted checkpoint artifact identity names different bytes.",
            )
        return existing.artifact_ref_id
    _insert_artifact(
        connection,
        stored_state.ArtifactReference(
            expected_id,
            published.key,
            published.revision,
            published.kind,
            published.selector,
            published.content_sha256,
            published.size_bytes,
            revision,
            now,
        ),
    )
    return expected_id


def accept_artifact_reference(
    connection: sqlite3.Connection,
    published: ArtifactRef,
    accepted_at: datetime,
) -> DecisionResult[stored_state.ArtifactReference]:
    """Accept a publisher-verified reference; the caller owns transaction and readback."""

    existing = read_artifact_by_identity(connection, published.kind, published.key, published.revision)
    if existing is not None:
        if (
            existing.selector,
            existing.content_sha256,
            existing.size_bytes,
        ) != (published.selector, published.content_sha256, published.size_bytes):
            raise StorageError(
                StorageErrorCode.INVARIANT_VIOLATION,
                "An accepted artifact identity already names different bytes.",
            )
        return existing
    latest = read_latest_artifact(connection)
    project_row = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
    if project_row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "The database has no project record.")
    project_revision = decode_row(project_row, _ProjectRevision).revision
    reference = stored_state.ArtifactReference(
        ArtifactRefId(1 if latest is None else int(latest.artifact_ref_id) + 1),
        published.key,
        published.revision,
        published.kind,
        published.selector,
        published.content_sha256,
        published.size_bytes,
        project_revision + 1,
        accepted_at,
    )
    revision = project_revision + 1
    _insert_artifact(connection, reference)
    if (
        failure := require_one_changed_row(
            connection.execute(
                """
                UPDATE project_meta
                SET revision = ?, updated_at = ?
                WHERE singleton = 1 AND revision = ?
                """,
                (revision, accepted_at.isoformat(), project_revision),
            ),
            "The project revision changed before artifact acceptance.",
        )
    ) is not None:
        return failure
    return reference
