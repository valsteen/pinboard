"""Read indexed latest-batch references and their exact immutable receipts on a supplied connection."""

import sqlite3
from collections.abc import Iterable

import msgspec

from pinboard.adapters.sqlite.database import decode_row, select_by_ids
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import decode_decision_batch_reference
from pinboard.adapters.sqlite.state import read_history_receipts_by_ids
from pinboard.application import decision_batches
from pinboard.domain.identifiers import AttemptId, WorkItemId


class _BatchOwner(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    item_id: WorkItemId


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
