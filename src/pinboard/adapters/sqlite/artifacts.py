"""Read and accept artifact records on a supplied connection.

Artifact acceptance verifies the supplied filesystem reference; no other
operation reads files. This module never commits, rolls back, closes the
connection, calls callbacks, or obtains time. Expected stale CAS writes return a
``DecisionFailure``; SQLite and persisted-invariant failures remain exceptional.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

import msgspec

from pinboard.adapters.files.artifacts import verify_reference
from pinboard.adapters.sqlite.artifact_ownership import accept_ownership, attach_ownership
from pinboard.adapters.sqlite.database import decode_row, require_one_changed_row, select_by_ids
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.application import artifact_identity, stored_state
from pinboard.application.artifacts import (
    ArtifactRef,
    BriefArtifactRef,
    EvidenceArtifactRef,
    ResultArtifactRef,
)
from pinboard.application.ports import ArtifactReferenceAcceptance
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionResult
from pinboard.domain.identifiers import ArtifactRefId


class _BriefArtifactReferenceRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    key: str
    revision: int
    selector: str
    content_sha256: str
    size_bytes: int


class _ArtifactAllocationRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: int
    artifact_ref_id: int


def decode_reference_row(row: sqlite3.Row) -> stored_state.ArtifactReference:
    try:
        return msgspec.convert({**dict(row), "owners": ()}, type=stored_state.ArtifactReference, strict=True)
    except msgspec.ValidationError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Stored row is invalid: {error}") from error


def decode_reference_rows(
    connection: sqlite3.Connection, rows: tuple[sqlite3.Row, ...] | list[sqlite3.Row]
) -> tuple[stored_state.ArtifactReference, ...]:
    return attach_ownership(connection, (decode_reference_row(row) for row in rows))


def _read_accepted_artifact(
    connection: sqlite3.Connection,
    published: ArtifactRef | ResultArtifactRef | EvidenceArtifactRef,
) -> stored_state.ArtifactReference | None:
    row = connection.execute(
        """
        SELECT artifact_ref_id, artifact_key AS key, artifact_revision AS revision, kind,
               relative_path AS selector, content_sha256, size_bytes, accepted_revision, created_at
        FROM artifact_refs
        WHERE kind = ? AND artifact_key = ? AND artifact_revision = ?
        """,
        (published.kind.value, published.key, published.revision),
    ).fetchone()
    return None if row is None else decode_reference_rows(connection, [row])[0]


def read_artifact_reference(
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
    return None if row is None else decode_reference_rows(connection, [row])[0]


def read_owned_reference(
    connection: sqlite3.Connection, ownership: artifact_identity.LifecycleAddress
) -> stored_state.ArtifactReference | None:
    rows = connection.execute(
        """SELECT artifact_refs.artifact_ref_id, artifact_key AS key, artifact_revision AS revision, kind,
                  relative_path AS selector, content_sha256, size_bytes, accepted_revision, created_at
           FROM artifact_ownership JOIN artifact_refs USING (artifact_ref_id)
           WHERE ownership_json = ?""",
        (artifact_identity.canonical_ownership(ownership),),
    ).fetchall()
    if len(rows) > 1:
        raise StorageError(StorageErrorCode.INVALID_STATE, "A lifecycle address names multiple accepted artifacts.")
    return None if not rows else decode_reference_rows(connection, rows)[0]


def read_latest_artifact_reference(
    connection: sqlite3.Connection,
    kind: work_models.ArtifactKind,
    key: str,
) -> stored_state.ArtifactReference | None:
    row = connection.execute(
        """
        SELECT artifact_ref_id, artifact_key AS key, artifact_revision AS revision, kind,
               relative_path AS selector, content_sha256, size_bytes, accepted_revision, created_at
        FROM artifact_refs
        WHERE kind = ? AND artifact_key = ?
        ORDER BY artifact_revision DESC
        LIMIT 1
        """,
        (kind.value, key),
    ).fetchone()
    return None if row is None else decode_reference_rows(connection, [row])[0]


def read_artifact_reference_by_id(
    connection: sqlite3.Connection, artifact_ref_id: ArtifactRefId
) -> stored_state.ArtifactReference | None:
    row = connection.execute(
        """
        SELECT artifact_ref_id, artifact_key AS key, artifact_revision AS revision, kind,
               relative_path AS selector, content_sha256, size_bytes, accepted_revision, created_at
        FROM artifact_refs WHERE artifact_ref_id = ?
        """,
        (artifact_ref_id,),
    ).fetchone()
    return None if row is None else decode_reference_rows(connection, [row])[0]


def read_artifact_references_by_ids(
    connection: sqlite3.Connection, artifact_ids: tuple[ArtifactRefId, ...]
) -> dict[ArtifactRefId, stored_state.ArtifactReference]:
    return {
        value.artifact_ref_id: value
        for value in decode_reference_rows(
            connection,
            select_by_ids(
                connection,
                """SELECT artifact_ref_id, artifact_key AS key, artifact_revision AS revision, kind,
                          relative_path AS selector, content_sha256, size_bytes, accepted_revision, created_at
                   FROM artifact_refs WHERE artifact_ref_id IN ({ids})""",
                artifact_ids,
            ),
        )
    }


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
    for ownership in reference.owners:
        accept_ownership(connection, reference.artifact_ref_id, ownership)


def read_artifacts(connection: sqlite3.Connection) -> tuple[stored_state.ArtifactReference, ...]:
    return decode_reference_rows(
        connection,
        connection.execute(
            """
            SELECT artifact_ref_id, artifact_key AS key, artifact_revision AS revision, kind,
                   relative_path AS selector, content_sha256, size_bytes, accepted_revision, created_at
            FROM artifact_refs
            ORDER BY artifact_ref_id
            """
        ).fetchall(),
    )


def read_brief_artifact_reference(
    connection: sqlite3.Connection,
    artifact_ref_id: ArtifactRefId,
) -> BriefArtifactRef | None:
    row = connection.execute(
        """
        SELECT artifact_key AS key, artifact_revision AS revision, relative_path AS selector,
               content_sha256, size_bytes
        FROM artifact_refs
        WHERE artifact_ref_id = ? AND kind = 'brief'
        """,
        (artifact_ref_id,),
    ).fetchone()
    if row is None:
        return None
    selected = decode_row(row, _BriefArtifactReferenceRow)
    return BriefArtifactRef(
        selected.key,
        selected.revision,
        selected.selector,
        selected.content_sha256,
        selected.size_bytes,
        work_models.ArtifactKind.BRIEF,
    )


def accept_checkpoint_artifact(
    connection: sqlite3.Connection,
    published: ResultArtifactRef | EvidenceArtifactRef,
    expected_id: ArtifactRefId,
    revision: int,
    now: datetime,
    ownership: artifact_identity.Ownership,
) -> ArtifactRefId:
    existing = _read_accepted_artifact(connection, published)
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
        if ownership not in existing.owners:
            raise StorageError(
                StorageErrorCode.INVARIANT_VIOLATION, "An accepted lifecycle artifact has another owner."
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
            (ownership,),
        ),
    )
    return expected_id


def accept_artifact_reference(
    connection: sqlite3.Connection,
    work_root: Path,
    published: ArtifactRef,
    accepted_at: datetime,
    ownership: artifact_identity.Ownership | None,
) -> DecisionResult[ArtifactReferenceAcceptance]:
    """Accept one verified reference; the caller owns transaction and readback."""

    verify_reference(work_root, published)
    existing = _read_accepted_artifact(connection, published)
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
        if ownership is not None and ownership not in existing.owners:
            raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "An accepted artifact has another owner.")
        return ArtifactReferenceAcceptance(existing, False)
    allocation = connection.execute(
        """
        SELECT project.revision,
               COALESCE((SELECT MAX(artifact_ref_id) FROM artifact_refs), 0) + 1 AS artifact_ref_id
        FROM project_meta AS project WHERE project.singleton = 1
        """
    ).fetchone()
    if allocation is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Project metadata is missing.")
    selected_allocation = decode_row(allocation, _ArtifactAllocationRow)
    current_revision = selected_allocation.revision
    reference = stored_state.ArtifactReference(
        ArtifactRefId(selected_allocation.artifact_ref_id),
        published.key,
        published.revision,
        published.kind,
        published.selector,
        published.content_sha256,
        published.size_bytes,
        current_revision + 1,
        accepted_at,
        () if ownership is None else (ownership,),
    )
    revision = current_revision + 1
    _insert_artifact(connection, reference)
    if (
        failure := require_one_changed_row(
            connection.execute(
                """
                UPDATE project_meta
                SET revision = ?, updated_at = ?
                WHERE singleton = 1 AND revision = ?
                """,
                (revision, accepted_at.isoformat(), current_revision),
            ),
            "The project revision changed before artifact acceptance.",
        )
    ) is not None:
        return failure
    return ArtifactReferenceAcceptance(reference, True)
