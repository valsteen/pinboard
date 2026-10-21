"""Exact indexed relationship reads and writes on the caller's transaction."""

import sqlite3
from collections.abc import Iterable
from dataclasses import replace

import msgspec

from pinboard.adapters.sqlite.database import decode_row, select_by_ids
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.application import artifact_identity, stored_state
from pinboard.domain.identifiers import ArtifactRefId, AttemptId


class OwnershipRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    artifact_ref_id: ArtifactRefId
    attempt_id: AttemptId
    ownership_json: bytes


def read_ownership(
    connection: sqlite3.Connection, artifact_ids: tuple[ArtifactRefId, ...]
) -> dict[ArtifactRefId, tuple[artifact_identity.Ownership, ...]]:
    selected: dict[ArtifactRefId, list[artifact_identity.Ownership]] = {}
    for row in select_by_ids(
        connection,
        "SELECT artifact_ref_id, attempt_id, ownership_json FROM artifact_ownership WHERE artifact_ref_id IN ({ids}) ORDER BY artifact_ref_id, ownership_json",
        artifact_ids,
    ):
        record = decode_row(row, OwnershipRow)
        try:
            owner = msgspec.json.decode(record.ownership_json, type=artifact_identity.Ownership)
        except msgspec.DecodeError as error:
            raise StorageError(StorageErrorCode.INVALID_STATE, f"Artifact ownership is invalid: {error}") from error
        if (
            owner.attempt_id != record.attempt_id
            or artifact_identity.canonical_ownership(owner) != record.ownership_json
        ):
            raise StorageError(StorageErrorCode.INVALID_STATE, "Artifact ownership differs from its indexed identity.")
        selected.setdefault(record.artifact_ref_id, []).append(owner)
    return {key: tuple(values) for key, values in selected.items()}


def attach_ownership(
    connection: sqlite3.Connection, references: Iterable[stored_state.ArtifactReference]
) -> tuple[stored_state.ArtifactReference, ...]:
    selected = tuple(references)
    owners = read_ownership(connection, tuple(value.artifact_ref_id for value in selected))
    return tuple(replace(value, owners=owners.get(value.artifact_ref_id, ())) for value in selected)


def accept_ownership(
    connection: sqlite3.Connection, artifact_ref_id: ArtifactRefId, ownership: artifact_identity.Ownership
) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO artifact_ownership (artifact_ref_id, attempt_id, ownership_json) VALUES (?, ?, ?)",
        (artifact_ref_id, ownership.attempt_id, artifact_identity.canonical_ownership(ownership)),
    )
