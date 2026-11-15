"""Read the current planned replacement of selected items and the dispositions that accept its exact cost."""

import sqlite3
from collections.abc import Iterable
from datetime import datetime

import msgspec

from pinboard.adapters.sqlite.database import decode_row, select_by_ids
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.domain import work_models
from pinboard.domain.identifiers import TaskId, WorkItemId


class _PlannedReplacementRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    affected_item_id: WorkItemId
    relation_revision: int
    replacement_item_id: WorkItemId
    replacement_cost: str
    status: work_models.PlannedReplacementStatus
    recorded_by: TaskId
    recorded_at: datetime


class _ReplacementDispositionRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    affected_item_id: WorkItemId
    relation_revision: int
    rationale: str
    accepted_cost: str
    recorded_by: TaskId
    recorded_at: datetime


def _replacement_item_key(value: _PlannedReplacementRow) -> WorkItemId:
    return value.affected_item_id


def read_current_replacements(
    connection: sqlite3.Connection, item_ids: Iterable[WorkItemId]
) -> tuple[tuple[work_models.PlannedReplacement, ...], tuple[work_models.ReplacementDisposition, ...]]:
    selected_ids = tuple(dict.fromkeys(item_ids))
    if not selected_ids:
        return (), ()
    rows = tuple(
        sorted(
            (
                decode_row(row, _PlannedReplacementRow)
                for row in select_by_ids(
                    connection,
                    """
            SELECT relation.affected_item_id, relation.relation_revision,
                   relation.replacement_item_id, relation.replacement_cost,
                   relation.status, relation.recorded_by, relation.recorded_at
            FROM planned_replacements AS relation
            WHERE relation.affected_item_id IN ({ids})
              AND relation.relation_revision = (
                  SELECT MAX(candidate.relation_revision)
                  FROM planned_replacements AS candidate
                  WHERE candidate.affected_item_id = relation.affected_item_id
              )
            ORDER BY relation.affected_item_id
            """,
                    selected_ids,
                )
            ),
            key=_replacement_item_key,
        )
    )
    dispositions_by_key = {
        (value.affected_item_id, value.relation_revision): value
        for value in (
            decode_row(row, _ReplacementDispositionRow)
            for row in select_by_ids(
                connection,
                """
                SELECT disposition.affected_item_id, disposition.relation_revision,
                       disposition.rationale, disposition.accepted_cost,
                       disposition.recorded_by, disposition.recorded_at
                FROM replacement_dispositions AS disposition
                JOIN planned_replacements AS relation
                  ON relation.affected_item_id = disposition.affected_item_id
                 AND relation.relation_revision = disposition.relation_revision
                WHERE disposition.affected_item_id IN ({ids})
                  AND relation.relation_revision = (
                      SELECT MAX(candidate.relation_revision)
                      FROM planned_replacements AS candidate
                      WHERE candidate.affected_item_id = relation.affected_item_id
                  )
                ORDER BY disposition.affected_item_id, disposition.relation_revision
                """,
                selected_ids,
            )
        )
    }
    replacements = tuple(
        work_models.PlannedReplacement(
            value.affected_item_id,
            value.relation_revision,
            value.replacement_item_id,
            value.replacement_cost,
            value.status,
            value.recorded_by,
            value.recorded_at,
        )
        for value in rows
    )
    dispositions: list[work_models.ReplacementDisposition] = []
    for relation in rows:
        value = dispositions_by_key.get((relation.affected_item_id, relation.relation_revision))
        if value is None:
            continue
        if value.accepted_cost != relation.replacement_cost:
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                "A selected temporary-retention disposition does not accept its exact replacement cost.",
            )
        dispositions.append(
            work_models.ReplacementDisposition(
                value.affected_item_id,
                value.relation_revision,
                value.rationale,
                value.accepted_cost,
                value.recorded_by,
                value.recorded_at,
            )
        )
    return replacements, tuple(dispositions)
