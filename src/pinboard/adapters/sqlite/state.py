"""Compose and validate complete state on a supplied connection.

This module reads and writes SQLite but never commits, rolls back, closes the
connection, calls callbacks, reads the filesystem, or obtains time. SQLite and
persisted-invariant failures remain exceptional; the transaction owner stays in
``store``.
"""

import sqlite3
from datetime import datetime
from itertools import pairwise

import msgspec

from pinboard.adapters.sqlite.artifacts import read_artifacts, read_latest_artifact, read_selected_artifacts
from pinboard.adapters.sqlite.authority import read_authority, read_live_authority, validate_attempt_authority
from pinboard.adapters.sqlite.database import decode_row
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import (
    read_attempt_lifecycle,
    read_focus,
    read_item_definition_lifecycle,
    read_item_status_lifecycle,
    read_lifecycle,
    read_live_lifecycle,
)
from pinboard.adapters.sqlite.proposals import read_live_proposals, read_proposals
from pinboard.application import stored_state
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    ProposalId,
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


class _AttemptIdentity(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId


def read_project(connection: sqlite3.Connection) -> stored_state.ProjectRecord:
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


def _read_latest_history(connection: sqlite3.Connection) -> tuple[stored_state.StoredTransitionReceipt, ...]:
    row = connection.execute(
        """
        SELECT history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
               authorization_kind AS authorization, actor_task_id, actor_host_id, input_schema,
               input_json, outcome_schema, outcome_json, committed_at
        FROM transition_history
        ORDER BY history_id DESC
        LIMIT 1
        """
    ).fetchone()
    return () if row is None else (decode_row(row, _StoredTransitionRow).receipt(),)


def read_selected_history(
    connection: sqlite3.Connection,
    history_ids: tuple[HistoryId, ...],
) -> tuple[stored_state.StoredTransitionReceipt, ...]:
    if not history_ids:
        return ()
    placeholders = ", ".join("?" for _value in history_ids)
    return tuple(
        decode_row(row, _StoredTransitionRow).receipt()
        for row in connection.execute(
            f"""
            SELECT history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
                   authorization_kind AS authorization, actor_task_id, actor_host_id, input_schema,
                   input_json, outcome_schema, outcome_json, committed_at
            FROM transition_history
            WHERE history_id IN ({placeholders})
            ORDER BY history_id
            """,
            history_ids,
        ).fetchall()
    )


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


def _validate_current_state(state: stored_state.StoredWorkState, error_code: StorageErrorCode) -> None:
    validate_attempt_authority(state, error_code)
    positions = sorted(value.queue_position for value in state.lifecycle.work_items if value.queue_position is not None)
    if positions != list(range(1, len(positions) + 1)):
        raise StorageError(error_code, "Live work-item queue positions must be contiguous and one-based.")
    item_ids = {value.item_id for value in state.lifecycle.work_items}
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


def read_state(connection: sqlite3.Connection) -> stored_state.StoredWorkState:
    project = read_project(connection)
    state = stored_state.StoredWorkState(
        read_lifecycle(connection, project),
        read_proposals(connection),
        read_artifacts(connection),
        read_authority(connection),
        _read_history(connection),
        read_focus(connection),
    )
    _validate_current_state(state, StorageErrorCode.INVALID_STATE)
    return state


def read_item_definition_state(
    connection: sqlite3.Connection,
    item_id: ItemId,
    *,
    history_limit: int = 1,
    before_revision: int | None = None,
) -> stored_state.StoredWorkState:
    """Read one item and only its requested bounded definition window."""

    project = read_project(connection)
    return stored_state.StoredWorkState(
        read_item_definition_lifecycle(
            connection,
            project,
            item_id,
            history_limit=history_limit,
            before_revision=before_revision,
        ),
        stored_state.ProposalRecords(),
        (),
        stored_state.AuthorityRecords(),
        (),
        stored_state.StoredFocus(None, None, "select", 0),
    )


def _read_current_state(
    connection: sqlite3.Connection,
    selected_artifact_ref_ids: tuple[ArtifactRefId, ...],
    subject_item_ids: tuple[ItemId, ...],
    subject_attempt_ids: tuple[AttemptId, ...],
    subject_proposal_ids: tuple[ProposalId, ...],
) -> stored_state.StoredWorkState:
    project = read_project(connection)
    lifecycle = read_live_lifecycle(
        connection,
        project,
        subject_item_ids=subject_item_ids,
        subject_attempt_ids=subject_attempt_ids,
    )
    item_ids = tuple(item.item_id for item in lifecycle.work_items)
    proposal_item_ids = tuple(
        dict.fromkeys((*item_ids, *(dependency.dependency_id for dependency in lifecycle.dependencies)))
    )
    attempt_ids = tuple(attempt.attempt_id for attempt in lifecycle.attempts)
    artifact_ref_ids = tuple(
        dict.fromkeys(
            (
                *selected_artifact_ref_ids,
                *(
                    reference
                    for attempt in lifecycle.attempts
                    for reference in (attempt.brief_artifact_ref_id, attempt.result_artifact_ref_id)
                    if reference is not None
                ),
            )
        )
    )
    selected_artifacts = read_selected_artifacts(connection, artifact_ref_ids)
    latest_artifact = read_latest_artifact(connection)
    artifact_references = tuple(
        dict.fromkeys((*selected_artifacts, *((latest_artifact,) if latest_artifact is not None else ())))
    )
    return stored_state.StoredWorkState(
        lifecycle,
        read_live_proposals(connection, proposal_item_ids, subject_proposal_ids),
        artifact_references,
        read_live_authority(connection, attempt_ids, item_ids),
        _read_latest_history(connection),
        read_focus(connection),
    )


def read_live_state(
    connection: sqlite3.Connection,
    selected_artifact_ref_ids: tuple[ArtifactRefId, ...] = (),
) -> stored_state.StoredWorkState:
    """Read the current live graph without append-only history or terminal rows."""

    return _read_current_state(
        connection,
        selected_artifact_ref_ids,
        (),
        (),
        (),
    )


def read_decision_state(
    connection: sqlite3.Connection,
    selected_artifact_ref_ids: tuple[ArtifactRefId, ...] = (),
    *,
    subject_item_ids: tuple[ItemId, ...] = (),
    subject_attempt_ids: tuple[AttemptId, ...] = (),
    subject_proposal_ids: tuple[ProposalId, ...] = (),
) -> stored_state.StoredWorkState:
    """Read live decision rows plus exact operation subjects."""

    return _read_current_state(
        connection,
        selected_artifact_ref_ids,
        subject_item_ids,
        subject_attempt_ids,
        subject_proposal_ids,
    )


def read_status_facts(connection: sqlite3.Connection) -> stored_state.StatusFacts:
    """Read compact status aggregates without materializing work or history rows."""

    project = read_project(connection)
    active_attempts = tuple(
        decode_row(row, _AttemptIdentity).attempt_id
        for row in connection.execute(
            "SELECT attempt_id FROM attempts WHERE state = 'active' ORDER BY attempt_id"
        ).fetchall()
    )
    counts = tuple(
        decode_row(row, stored_state.WorkStateCount)
        for row in connection.execute(
            "SELECT state, COUNT(*) AS count FROM work_items GROUP BY state ORDER BY state"
        ).fetchall()
    )
    authority = read_live_authority(connection, active_attempts, ())
    return stored_state.StatusFacts(project, read_focus(connection), active_attempts, counts, authority.coordination)


def read_item_status_state(connection: sqlite3.Connection, item_id: ItemId) -> stored_state.StoredWorkState:
    """Read one item's status facts without unrelated items or histories."""

    project = read_project(connection)
    lifecycle = read_item_status_lifecycle(connection, project, item_id)
    attempt_ids = tuple(attempt.attempt_id for attempt in lifecycle.attempts)
    return stored_state.StoredWorkState(
        lifecycle,
        stored_state.ProposalRecords(),
        (),
        read_live_authority(connection, attempt_ids, (item_id,)),
        (),
        stored_state.StoredFocus(None, None, "select", 0),
    )


def read_coordination_state(connection: sqlite3.Connection) -> stored_state.StoredWorkState:
    project = read_project(connection)
    return stored_state.StoredWorkState(
        stored_state.LifecycleRecords(project),
        stored_state.ProposalRecords(),
        (),
        read_live_authority(connection, (), ()),
        _read_latest_history(connection),
        stored_state.StoredFocus(None, None, "select", 0),
    )


def read_attempt_authority_state(
    connection: sqlite3.Connection,
    attempt_id: AttemptId,
) -> stored_state.StoredWorkState:
    project = read_project(connection)
    lifecycle = read_attempt_lifecycle(connection, project, attempt_id)
    item_ids = tuple(value.item_id for value in lifecycle.work_items)
    artifact_ids = tuple(value.brief_artifact_ref_id for value in lifecycle.attempts)
    return stored_state.StoredWorkState(
        lifecycle,
        stored_state.ProposalRecords(),
        read_selected_artifacts(connection, artifact_ids),
        read_live_authority(connection, (attempt_id,), item_ids),
        _read_latest_history(connection),
        stored_state.StoredFocus(None, None, "select", 0),
    )


def read_preparation_authority_state(
    connection: sqlite3.Connection,
    item_id: ItemId,
) -> stored_state.StoredWorkState:
    project = read_project(connection)
    return stored_state.StoredWorkState(
        read_item_definition_lifecycle(connection, project, item_id),
        stored_state.ProposalRecords(),
        (),
        read_live_authority(connection, (), (item_id,)),
        (),
        stored_state.StoredFocus(None, None, "select", 0),
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
