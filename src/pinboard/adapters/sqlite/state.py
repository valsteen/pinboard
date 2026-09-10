"""Compose and validate complete state on a supplied connection.

This module reads and writes SQLite but never commits, rolls back, closes the
connection, calls callbacks, reads the filesystem, or obtains time. SQLite and
persisted-invariant failures remain exceptional; the transaction owner stays in
``store``.
"""

import sqlite3
from collections import Counter
from datetime import datetime
from itertools import pairwise

import msgspec

from pinboard.adapters.sqlite.artifacts import read_artifacts
from pinboard.adapters.sqlite.authority import read_authority, validate_attempt_authority
from pinboard.adapters.sqlite.database import decode_row
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import read_lifecycle
from pinboard.adapters.sqlite.proposals import read_pending_proposals, read_proposals
from pinboard.application import handover, stored_state
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    TaskId,
)


def _stored_json(column: str, value: str) -> work_models.CanonicalJson:
    encoded = value.encode("utf-8")
    try:
        msgspec.json.decode(encoded, type=msgspec.Raw)
    except msgspec.DecodeError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {column!r} has invalid JSON.") from error
    return work_models.CanonicalJson(encoded)


class _StoredTransitionRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: HistoryId
    project_revision: int
    action_id: ActionId
    action_kind: decision_models.ActionKind
    subject_id: HistorySubjectId
    artifact_ref_id: ArtifactRefId | None
    authorization: decision_models.AuthorizationKind
    actor_task_id: TaskId | None
    actor_host_id: HostId | None
    input_schema: str
    input_json: str
    outcome_schema: str
    outcome_json: str
    committed_at: datetime

    def receipt(self) -> stored_state.StoredTransitionReceipt:
        return stored_state.StoredTransitionReceipt(
            self.history_id,
            self.project_revision,
            self.action_id,
            self.action_kind,
            self.subject_id,
            self.artifact_ref_id,
            self.authorization,
            self.actor_task_id,
            self.actor_host_id,
            self.input_schema,
            _stored_json("input_json", self.input_json),
            self.outcome_schema,
            _stored_json("outcome_json", self.outcome_json),
            self.committed_at,
        )


class _StateCountRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    state: stored_state.StoredWorkItemState
    item_count: int


def _read_replacements(connection: sqlite3.Connection) -> stored_state.ReplacementRecords:
    planned = tuple(
        decode_row(row, stored_state.StoredPlannedReplacement)
        for row in connection.execute(
            """
            SELECT affected_item_id, relation_revision, replacement_item_id, replacement_cost,
                   status, recorded_by, recorded_at, accepted_project_revision
            FROM planned_replacements
            ORDER BY affected_item_id, relation_revision
            """
        ).fetchall()
    )
    dispositions = tuple(
        decode_row(row, stored_state.StoredReplacementDisposition)
        for row in connection.execute(
            """
            SELECT affected_item_id, relation_revision, rationale, accepted_cost,
                   recorded_by, recorded_at, accepted_project_revision
            FROM replacement_dispositions
            ORDER BY affected_item_id, relation_revision
            """
        ).fetchall()
    )
    return stored_state.ReplacementRecords(planned, dispositions)


def _read_project(connection: sqlite3.Connection) -> stored_state.ProjectRecord:
    rows = tuple(
        connection.execute(
            """
            SELECT application, schema_version, revision, host_epoch, created_at, updated_at
            FROM project_meta
            ORDER BY singleton
            """
        ).fetchall()
    )
    if len(rows) != 1:
        raise StorageError(StorageErrorCode.INVALID_STATE, "The database must contain one project record.")
    return decode_row(rows[0], stored_state.ProjectRecord)


def _read_history(connection: sqlite3.Connection) -> tuple[stored_state.StoredTransitionReceipt, ...]:
    return tuple(
        decode_row(row, _StoredTransitionRow).receipt()
        for row in connection.execute(
            """
            SELECT history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
                   authorization_kind AS authorization, actor_task_id, actor_host_id, input_schema,
                   input_json, outcome_schema, outcome_json, committed_at
            FROM transition_history
            ORDER BY history_id
            """
        ).fetchall()
    )


def read_history_receipt(
    connection: sqlite3.Connection, history_id: HistoryId
) -> stored_state.StoredTransitionReceipt | None:
    row = connection.execute(
        """
        SELECT history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
               authorization_kind AS authorization, actor_task_id, actor_host_id, input_schema,
               input_json, outcome_schema, outcome_json, committed_at
        FROM transition_history WHERE history_id = ?
        """,
        (history_id,),
    ).fetchone()
    return None if row is None else decode_row(row, _StoredTransitionRow).receipt()


def _definition_revision_number(value: stored_state.ItemDefinitionRevision) -> int:
    return value.revision


def _dependency_identity_position(value: stored_state.ItemDependency) -> tuple[str, int]:
    return str(value.item_id), value.position


def _current_definitions(
    state: stored_state.StoredWorkState,
    item_ids: set[ItemId],
    error_code: StorageErrorCode,
) -> dict[ItemId, stored_state.ItemDefinitionRevision]:
    definitions_by_item: dict[ItemId, list[stored_state.ItemDefinitionRevision]] = {item_id: [] for item_id in item_ids}
    for value in state.lifecycle.definition_revisions:
        if value.item_id not in definitions_by_item:
            raise StorageError(error_code, "Definition history names an unknown work item.")
        definitions_by_item[value.item_id].append(value)
        digest = work_item_definition_digest(value.definition)
        if not isinstance(digest, str) or digest != value.digest or value.after_digest != value.digest:
            raise StorageError(error_code, "Definition history digest does not match its canonical definition.")
    current_definitions: dict[ItemId, stored_state.ItemDefinitionRevision] = {}
    for item_id, revisions in definitions_by_item.items():
        ordered = sorted(revisions, key=_definition_revision_number)
        if [value.revision for value in ordered] != list(range(1, len(ordered) + 1)) or not ordered:
            raise StorageError(error_code, "Every work item must have contiguous definition history from revision 1.")
        if ordered[0].before_digest is not None or any(
            value.before_digest != previous.digest for previous, value in pairwise(ordered)
        ):
            raise StorageError(error_code, "Definition history digest links are not contiguous.")
        current_definitions[item_id] = ordered[-1]
    return current_definitions


def _validate_dependencies(
    state: stored_state.StoredWorkState,
    item_ids: set[ItemId],
    current_definitions: dict[ItemId, stored_state.ItemDefinitionRevision],
    error_code: StorageErrorCode,
) -> None:
    dependency_groups: dict[ItemId, list[ItemId]] = {item_id: [] for item_id in item_ids}
    for value in sorted(state.lifecycle.dependencies, key=_dependency_identity_position):
        if value.item_id not in item_ids or value.dependency_id not in item_ids:
            raise StorageError(error_code, "A dependency names an unknown work item.")
        dependency_groups[value.item_id].append(value.dependency_id)
    if any(
        tuple(dependency_groups[item_id]) != current.definition.dependencies
        for item_id, current in current_definitions.items()
    ):
        raise StorageError(error_code, "Current definition dependencies do not match relational dependencies.")
    for item_id in item_ids:
        pending = list(dependency_groups[item_id])
        visited: set[ItemId] = set()
        while pending:
            dependency = pending.pop()
            if dependency == item_id:
                raise StorageError(error_code, "Current definition dependencies contain a cycle.")
            if dependency not in visited:
                visited.add(dependency)
                pending.extend(dependency_groups[dependency])


def _replacement_revision(value: stored_state.StoredPlannedReplacement) -> int:
    return value.relation_revision


def _validate_replacements(
    records: stored_state.ReplacementRecords,
    item_ids: set[ItemId],
    error_code: StorageErrorCode,
) -> None:
    replacements_by_item: dict[ItemId, list[stored_state.StoredPlannedReplacement]] = {}
    for replacement in records.planned_replacements:
        if replacement.affected_item_id not in item_ids or replacement.replacement_item_id not in item_ids:
            raise StorageError(error_code, "A planned replacement names an unknown work item.")
        replacements_by_item.setdefault(replacement.affected_item_id, []).append(replacement)
    indexed_replacements: dict[tuple[ItemId, int], stored_state.StoredPlannedReplacement] = {}
    for affected_item, replacements in replacements_by_item.items():
        ordered = sorted(replacements, key=_replacement_revision)
        if [value.relation_revision for value in ordered] != list(range(1, len(ordered) + 1)):
            raise StorageError(error_code, "Planned replacement revisions must be contiguous from revision 1.")
        indexed_replacements.update(((affected_item, value.relation_revision), value) for value in ordered)
    disposition_keys: set[tuple[ItemId, int]] = set()
    for disposition in records.dispositions:
        key = (disposition.affected_item_id, disposition.relation_revision)
        replacement = indexed_replacements.get(key)
        if replacement is None:
            raise StorageError(error_code, "A temporary-retention disposition names an unknown replacement revision.")
        if disposition.accepted_cost != replacement.replacement_cost:
            raise StorageError(error_code, "A temporary-retention disposition must accept the exact replacement cost.")
        if key in disposition_keys:
            raise StorageError(error_code, "A replacement revision has duplicate temporary-retention dispositions.")
        disposition_keys.add(key)


def _validate_current_state(state: stored_state.StoredWorkState, error_code: StorageErrorCode) -> None:
    validate_attempt_authority(state, error_code)
    positions = sorted(value.queue_position for value in state.lifecycle.work_items if value.queue_position is not None)
    if positions != list(range(1, len(positions) + 1)):
        raise StorageError(error_code, "Live work-item queue positions must be contiguous and one-based.")
    item_ids = {value.item_id for value in state.lifecycle.work_items}
    _validate_replacements(state.replacements, item_ids, error_code)
    current_definitions = _current_definitions(state, item_ids, error_code)
    item_states = {value.item_id: value.state for value in state.lifecycle.work_items}
    for lease in state.authority.preparation_leases:
        if (
            lease.state != authority_models.PreparationLeaseStatus.ACTIVE
            or lease.expires_at <= state.lifecycle.project.updated_at
        ):
            continue
        current = current_definitions.get(lease.item_id)
        if item_states.get(lease.item_id) != stored_state.StoredWorkItemState.READY:
            raise StorageError(error_code, "An active preparation lease must name a ready work item.")
        if current is None or (lease.definition_revision, lease.definition_digest) != (
            current.revision,
            current.digest,
        ):
            raise StorageError(error_code, "An active preparation lease must pin the current item definition.")
    _validate_dependencies(state, item_ids, current_definitions, error_code)


def _validate_item_state_counts(
    connection: sqlite3.Connection,
    items: tuple[stored_state.StoredWorkItem, ...],
) -> None:
    rows = tuple(
        decode_row(row, _StateCountRow)
        for row in connection.execute("SELECT state, item_count FROM work_item_state_counts ORDER BY state").fetchall()
    )
    if tuple(sorted(value.state.value for value in rows)) != tuple(
        sorted(value.value for value in stored_state.StoredWorkItemState)
    ):
        raise StorageError(StorageErrorCode.INVALID_STATE, "Work-item state counts are incomplete.")
    expected = Counter(value.state for value in items)
    if any(value.item_count != expected[value.state] for value in rows):
        raise StorageError(StorageErrorCode.INVALID_STATE, "Work-item state counts do not match stored items.")


def read_state(connection: sqlite3.Connection) -> stored_state.StoredWorkState:
    project = _read_project(connection)
    state = stored_state.StoredWorkState(
        read_lifecycle(connection, project),
        read_proposals(connection),
        _read_replacements(connection),
        read_artifacts(connection),
        read_authority(connection),
        _read_history(connection),
    )
    _validate_current_state(state, StorageErrorCode.INVALID_STATE)
    _validate_item_state_counts(connection, state.lifecycle.work_items)
    return state


def read_handover_state(connection: sqlite3.Connection) -> handover.HandoverState:
    """Read every exported relation while excluding local-only authority history."""

    project = _read_project(connection)
    lifecycle = read_lifecycle(connection, project)
    replacements = _read_replacements(connection)
    _validate_replacements(
        replacements,
        {value.item_id for value in lifecycle.work_items},
        StorageErrorCode.INVALID_STATE,
    )
    return handover.HandoverState(
        lifecycle,
        read_pending_proposals(connection),
        replacements,
        read_artifacts(connection),
        _read_history(connection),
    )


def _json_text(value: work_models.CanonicalJson | None) -> str | None:
    return None if value is None else bytes(value).decode("utf-8")


def append_history(
    connection: sqlite3.Connection,
    records: tuple[stored_state.StoredTransitionReceipt, ...],
) -> None:
    connection.executemany(
        """
        INSERT INTO transition_history (
            history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
            artifact_kind, authorization_kind, actor_task_id, actor_host_id, input_schema,
            input_json, outcome_schema, outcome_json, committed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(
            (
                value.history_id,
                value.project_revision,
                value.action_id,
                value.action_kind.value,
                value.subject_id,
                value.artifact_ref_id,
                None if value.artifact_ref_id is None else "evidence",
                value.authorization.value,
                value.actor_task_id,
                value.actor_host_id,
                value.input_schema,
                _json_text(value.input_payload),
                value.outcome_schema,
                _json_text(value.outcome_payload),
                value.committed_at.isoformat(),
            )
            for value in records
        ),
    )
