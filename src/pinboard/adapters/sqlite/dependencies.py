"""Read selected receipt-backed dependency facts on the caller's connection.

No transaction, filesystem, clock or lifecycle effects are owned here. Corrupt
selected receipt relationships remain persisted-invariant failures.
"""

import sqlite3
from collections.abc import Iterable
from itertools import batched

import msgspec

from pinboard.adapters.sqlite.database import decode_row
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.domain import history, work_models
from pinboard.domain.identifiers import CheckpointId, HistoryId, WorkItemId


class DependencyTargetRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    dependency_id: WorkItemId
    checkpoint_id: CheckpointId | None

    @property
    def target(self) -> work_models.Dependency:
        if self.checkpoint_id is None:
            return self.dependency_id
        return work_models.CheckpointDependency(self.dependency_id, self.checkpoint_id)


class DependencyRow(DependencyTargetRow, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId


class _AcceptanceRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    checkpoint_id: CheckpointId
    history_id: HistoryId
    producer: WorkItemId | None
    action_kind: str | None
    outcome_schema: str | None
    outcome_json: str | None


def read_accepted_checkpoints(
    connection: sqlite3.Connection, dependencies: Iterable[work_models.Dependency]
) -> frozenset[work_models.CheckpointDependency]:
    requested = tuple(
        dict.fromkeys(value for value in dependencies if isinstance(value, work_models.CheckpointDependency))
    )
    accepted: set[work_models.CheckpointDependency] = set()
    for batch in batched(requested, 200, strict=False):
        placeholders = ",".join("(?, ?)" for _ in batch)
        rows = connection.execute(
            f"""WITH requested(item_id, checkpoint_id) AS (VALUES {placeholders})
                SELECT fact.item_id, fact.checkpoint_id, fact.history_id,
                       attempt.item_id AS producer, receipt.action_kind,
                       receipt.outcome_schema, receipt.outcome_json
                FROM requested
                JOIN checkpoint_acceptances AS fact ON fact.history_id = (
                    SELECT MIN(history_id) FROM checkpoint_acceptances INDEXED BY checkpoint_acceptances_by_dependency
                    WHERE item_id = requested.item_id AND checkpoint_id = requested.checkpoint_id
                )
                LEFT JOIN transition_history AS receipt ON receipt.history_id = fact.history_id
                LEFT JOIN attempts AS attempt ON attempt.attempt_id = receipt.subject_id""",
            tuple(part for value in batch for part in (value.item_id, value.checkpoint_id)),
        )
        for row in rows:
            fact = decode_row(row, _AcceptanceRow)
            if (
                fact.producer != fact.item_id
                or fact.action_kind != "accept-checkpoint"
                or fact.outcome_schema != "checkpoint-acceptance/v2"
                or fact.outcome_json is None
            ):
                raise StorageError(
                    StorageErrorCode.INVALID_STATE,
                    f"Checkpoint acceptance fact {fact.history_id} has no matching producer receipt.",
                )
            try:
                outcome = msgspec.json.decode(fact.outcome_json, type=history.CheckpointAcceptanceOutcome)
            except msgspec.DecodeError as error:
                raise StorageError(
                    StorageErrorCode.INVALID_STATE, f"Checkpoint acceptance receipt {fact.history_id} is invalid."
                ) from error
            if outcome.checkpoint != fact.checkpoint_id:
                raise StorageError(
                    StorageErrorCode.INVALID_STATE,
                    f"Checkpoint acceptance fact {fact.history_id} names a different checkpoint than its receipt.",
                )
            accepted.add(work_models.CheckpointDependency(fact.item_id, fact.checkpoint_id))
    return frozenset(accepted)
