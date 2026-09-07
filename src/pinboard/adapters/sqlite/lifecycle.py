"""Read and change lifecycle records on a supplied connection.

This module never commits, rolls back, closes the connection, calls callbacks,
reads the filesystem, or obtains time. Expected stale CAS writes return a
``DecisionFailure``; SQLite and persisted-invariant failures remain exceptional.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import assert_never

import msgspec

from pinboard.adapters.sqlite.database import decode_row, require_one_changed_row
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.application import query_models, stored_state
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.history import (
    decode_work_item_definition,
    work_item_definition_bytes,
    work_item_definition_digest,
)
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, ItemId, TaskId


@dataclass(frozen=True, slots=True)
class _DefinitionRevisionRow:
    item_id: ItemId
    revision: int
    digest: str
    definition_json: work_models.CanonicalJson
    reason: str
    source_task_id: TaskId
    before_digest: str | None
    after_digest: str
    accepted_project_revision: int
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class _ProjectRevisionRow:
    revision: int


@dataclass(frozen=True, slots=True)
class _SubjectRevisionRow:
    subject_revision: int


@dataclass(frozen=True, slots=True)
class _DependencyRow:
    dependency_id: ItemId


class _AttemptItemRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: ItemId
    state: stored_state.StoredWorkItemState


class _AttemptContextRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    item_id: ItemId
    state: work_models.AttemptState
    branch: str
    base_revision: str
    brief_artifact_ref_id: ArtifactRefId
    candidate_revision: str | None
    accepted_scope_revision: int
    accepted_scope_digest: str


class _DependencyStateRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    dependency_id: ItemId
    state: stored_state.StoredWorkItemState


class _ParallelPreviewItemRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: ItemId
    state: stored_state.StoredWorkItemState


class _ParallelPreviewAttemptRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    state: work_models.AttemptState


@dataclass(frozen=True, slots=True)
class TerminalAttemptContextSelection:
    project_revision: int
    attempt_id: AttemptId
    item_id: ItemId


@dataclass(frozen=True, slots=True)
class NonterminalAttemptContextSelection:
    project_revision: int
    attempt_id: AttemptId
    item_id: ItemId
    state: query_models.NonterminalAttemptState
    branch: str
    base_revision: str
    accepted_scope_revision: int
    accepted_scope_digest: str
    candidate_revision: str | None
    brief_artifact_ref_id: ArtifactRefId
    item: query_models.AttemptContextItemFacts


type AttemptContextSelection = TerminalAttemptContextSelection | NonterminalAttemptContextSelection


@dataclass(frozen=True, slots=True)
class ParallelPreviewLifecycleAttempt:
    attempt_id: AttemptId
    state: query_models.NonterminalAttemptState


@dataclass(frozen=True, slots=True)
class ParallelPreviewLifecycleItem:
    item_id: ItemId
    label: str
    state: work_models.WorkState
    live_dependencies: tuple[ItemId, ...]
    attempt: ParallelPreviewLifecycleAttempt | None


@dataclass(frozen=True, slots=True)
class ParallelPreviewLifecycleSelection:
    project_revision: int
    items: tuple[ParallelPreviewLifecycleItem, ...]


def _validate_parallel_preview_attempt(
    state: work_models.WorkState,
    attempt: ParallelPreviewLifecycleAttempt | None,
) -> None:
    match state:
        case (
            work_models.WorkState.ACTIVE
            | work_models.WorkState.PAUSED
            | work_models.WorkState.BLOCKED
            | work_models.WorkState.REVIEW
        ) as attempted_state:
            valid = attempt is not None and attempt.state.value == attempted_state.value
        case work_models.WorkState.INTAKE | work_models.WorkState.READY | work_models.WorkState.DEFERRED:
            valid = attempt is None
        case _ as unreachable:
            assert_never(unreachable)
    if not valid:
        raise StorageError(
            StorageErrorCode.INVALID_STATE,
            "The selected work item and open attempt states do not match.",
        )


def _definition_revision(row: sqlite3.Row) -> stored_state.ItemDefinitionRevision:
    value = decode_row(row, _DefinitionRevisionRow)
    definition = decode_work_item_definition(value.definition_json)
    if isinstance(definition, DecisionFailure):
        raise StorageError(StorageErrorCode.INVALID_STATE, definition.message)
    digest = work_item_definition_digest(definition)
    if not isinstance(digest, str) or digest != value.digest or value.after_digest != value.digest:
        raise StorageError(
            StorageErrorCode.INVALID_STATE,
            "Definition history digest does not match its canonical definition.",
        )
    return stored_state.ItemDefinitionRevision(
        value.item_id,
        value.revision,
        value.digest,
        definition,
        value.reason,
        value.source_task_id,
        value.before_digest,
        value.after_digest,
        value.accepted_project_revision,
        value.accepted_at,
    )


def _current_definition_with_dependency_states(
    connection: sqlite3.Connection,
    item_id: ItemId,
    *,
    missing_message: str,
) -> tuple[stored_state.ItemDefinitionRevision, tuple[_DependencyStateRow, ...]]:
    definition_row = connection.execute(
        """
        SELECT item_id, definition_revision AS revision, definition_digest AS digest,
               definition_json, reason, source_task_id, before_digest, after_digest,
               accepted_project_revision, accepted_at
        FROM work_item_definition_revisions
        WHERE item_id = ?
        ORDER BY definition_revision DESC
        LIMIT 1
        """,
        (item_id,),
    ).fetchone()
    if definition_row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, missing_message)
    definition = _definition_revision(definition_row)
    dependencies = tuple(
        decode_row(row, _DependencyStateRow)
        for row in connection.execute(
            """
            SELECT dependency.dependency_id, item.state
            FROM item_dependencies AS dependency
            JOIN work_items AS item ON item.item_id = dependency.dependency_id
            WHERE dependency.item_id = ?
            ORDER BY dependency.position
            """,
            (item_id,),
        ).fetchall()
    )
    if tuple(value.dependency_id for value in dependencies) != definition.definition.dependencies:
        raise StorageError(
            StorageErrorCode.INVALID_STATE,
            "Current definition dependencies do not match relational dependencies.",
        )
    return definition, dependencies


def read_item_definition(connection: sqlite3.Connection, item_id: ItemId) -> query_models.ItemDefinitionFacts:
    project_revision_row = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
    item_row = connection.execute("SELECT subject_revision FROM work_items WHERE item_id = ?", (item_id,)).fetchone()
    if project_revision_row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Project metadata is missing.")
    project_revision = decode_row(project_revision_row, _ProjectRevisionRow).revision
    if item_row is None:
        return query_models.ItemDefinitionFacts(project_revision, None, None)
    subject_revision = decode_row(item_row, _SubjectRevisionRow).subject_revision
    definition = connection.execute(
        """
        SELECT item_id, definition_revision AS revision, definition_digest AS digest,
               definition_json, reason, source_task_id, before_digest, after_digest,
               accepted_project_revision, accepted_at
        FROM work_item_definition_revisions
        WHERE item_id = ?
        ORDER BY definition_revision DESC
        LIMIT 1
        """,
        (item_id,),
    ).fetchone()
    selected_definition = None if definition is None else _definition_revision(definition)
    if selected_definition is not None:
        dependency_rows = connection.execute(
            "SELECT dependency_id FROM item_dependencies WHERE item_id = ? ORDER BY position",
            (item_id,),
        ).fetchall()
        dependencies = tuple(decode_row(row, _DependencyRow).dependency_id for row in dependency_rows)
        if dependencies != selected_definition.definition.dependencies:
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                "Current definition dependencies do not match relational dependencies.",
            )
    return query_models.ItemDefinitionFacts(
        project_revision,
        subject_revision,
        selected_definition,
    )


def read_item_status(connection: sqlite3.Connection, item_id: ItemId) -> query_models.ItemStatusLifecycleFacts | None:
    project_revision_row = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
    if project_revision_row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Project metadata is missing.")
    project_revision = decode_row(project_revision_row, _ProjectRevisionRow).revision
    item_row = connection.execute(
        """
        SELECT item_id, state, timing, outcome_evidence, next_action, source, notes, queue_position
        FROM work_items
        WHERE item_id = ?
        """,
        (item_id,),
    ).fetchone()
    if item_row is None:
        return None
    item = decode_row(item_row, query_models.ItemStatusItemFacts)
    definition_row = connection.execute(
        """
        SELECT item_id, definition_revision AS revision, definition_digest AS digest,
               definition_json, reason, source_task_id, before_digest, after_digest,
               accepted_project_revision, accepted_at
        FROM work_item_definition_revisions
        WHERE item_id = ?
        ORDER BY definition_revision DESC
        LIMIT 1
        """,
        (item_id,),
    ).fetchone()
    definition = None if definition_row is None else _definition_revision(definition_row)
    attempts = tuple(
        decode_row(row, query_models.ItemStatusAttemptFacts)
        for row in connection.execute(
            """
            SELECT attempt_id, state, candidate_revision
            FROM attempts
            WHERE item_id = ?
            ORDER BY attempt_id
            """,
            (item_id,),
        ).fetchall()
    )
    return query_models.ItemStatusLifecycleFacts(
        project_revision,
        item,
        None if definition is None else definition.definition.title,
        attempts,
    )


def read_parallel_preview_lifecycle(
    connection: sqlite3.Connection,
    item_ids: tuple[ItemId, ...],
) -> ParallelPreviewLifecycleSelection | None:
    project_revision_row = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
    if project_revision_row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Project metadata is missing.")
    project_revision = decode_row(project_revision_row, _ProjectRevisionRow).revision
    selected: list[ParallelPreviewLifecycleItem] = []
    for item_id in item_ids:
        item_row = connection.execute(
            "SELECT item_id, state FROM work_items WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if item_row is None:
            return None
        item = decode_row(item_row, _ParallelPreviewItemRow)
        state = stored_state.live_work_state(item.state)
        if state is None:
            return None
        definition, dependencies = _current_definition_with_dependency_states(
            connection,
            item_id,
            missing_message="The selected work item has no definition.",
        )
        attempt_row = connection.execute(
            """
            SELECT attempt_id, state
            FROM attempts
            WHERE item_id = ? AND state != 'done'
            ORDER BY attempt_id
            LIMIT 1
            """,
            (item_id,),
        ).fetchone()
        attempt = None
        if attempt_row is not None:
            decoded_attempt = decode_row(attempt_row, _ParallelPreviewAttemptRow)
            match decoded_attempt.state:
                case (
                    work_models.AttemptState.ACTIVE
                    | work_models.AttemptState.PAUSED
                    | work_models.AttemptState.BLOCKED
                    | work_models.AttemptState.REVIEW
                ) as attempt_state:
                    attempt = ParallelPreviewLifecycleAttempt(decoded_attempt.attempt_id, attempt_state)
                case _:
                    raise StorageError(StorageErrorCode.INVALID_STATE, "The selected open attempt state is invalid.")
        _validate_parallel_preview_attempt(state, attempt)
        selected.append(
            ParallelPreviewLifecycleItem(
                item.item_id,
                definition.definition.title,
                state,
                tuple(
                    value.dependency_id
                    for value in dependencies
                    if stored_state.live_work_state(value.state) is not None
                ),
                attempt,
            )
        )
    return ParallelPreviewLifecycleSelection(project_revision, tuple(selected))


def read_attempt_context(
    connection: sqlite3.Connection,
    attempt_id: AttemptId,
) -> AttemptContextSelection | None:
    project_revision_row = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
    if project_revision_row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Project metadata is missing.")
    project_revision = decode_row(project_revision_row, _ProjectRevisionRow).revision
    attempt_row = connection.execute(
        """
        SELECT attempt_id, item_id, state, branch, base_revision, brief_artifact_ref_id,
               candidate_revision, accepted_scope_revision, accepted_scope_digest
        FROM attempts
        WHERE attempt_id = ?
        """,
        (attempt_id,),
    ).fetchone()
    if attempt_row is None:
        return None
    attempt = decode_row(attempt_row, _AttemptContextRow)
    if attempt.state == work_models.AttemptState.DONE:
        return TerminalAttemptContextSelection(project_revision, attempt.attempt_id, attempt.item_id)
    match attempt.state:
        case (
            work_models.AttemptState.ACTIVE
            | work_models.AttemptState.PAUSED
            | work_models.AttemptState.BLOCKED
            | work_models.AttemptState.REVIEW
        ) as attempt_state:
            pass
        case _:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The selected attempt state is unsupported.")

    item_row = connection.execute(
        "SELECT item_id, state FROM work_items WHERE item_id = ?",
        (attempt.item_id,),
    ).fetchone()
    if item_row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "The selected nonterminal attempt has no work item.")
    item = decode_row(item_row, _AttemptItemRow)
    match item.state:
        case (
            stored_state.StoredWorkItemState.ACTIVE
            | stored_state.StoredWorkItemState.PAUSED
            | stored_state.StoredWorkItemState.BLOCKED
            | stored_state.StoredWorkItemState.REVIEW
        ) as item_state:
            if item_state.value != attempt_state.value:
                raise StorageError(
                    StorageErrorCode.INVALID_STATE,
                    "The selected attempt and work item states do not match.",
                )
        case _:
            raise StorageError(
                StorageErrorCode.INVALID_STATE, "The selected attempt and work item states do not match."
            )

    definition, dependencies = _current_definition_with_dependency_states(
        connection,
        attempt.item_id,
        missing_message="The selected attempt item has no definition.",
    )
    return NonterminalAttemptContextSelection(
        project_revision,
        attempt.attempt_id,
        attempt.item_id,
        attempt_state,
        attempt.branch,
        attempt.base_revision,
        attempt.accepted_scope_revision,
        attempt.accepted_scope_digest,
        attempt.candidate_revision,
        attempt.brief_artifact_ref_id,
        query_models.AttemptContextItemFacts(
            item.item_id,
            item_state,
            definition.revision,
            definition.digest,
            tuple(
                value.dependency_id for value in dependencies if stored_state.live_work_state(value.state) is not None
            ),
        ),
    )


def read_item_definition_history(
    connection: sqlite3.Connection,
    item_id: ItemId,
    *,
    limit: int,
    before_revision: int | None,
) -> query_models.ItemDefinitionHistoryFacts:
    project_revision_row = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
    item = connection.execute("SELECT 1 FROM work_items WHERE item_id = ?", (item_id,)).fetchone()
    if project_revision_row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Project metadata is missing.")
    project_revision = decode_row(project_revision_row, _ProjectRevisionRow).revision
    if item is None:
        return query_models.ItemDefinitionHistoryFacts(project_revision, False, ())
    predicate = "item_id = ?" if before_revision is None else "item_id = ? AND definition_revision < ?"
    parameters = (item_id,) if before_revision is None else (item_id, before_revision)
    rows = connection.execute(
        f"""
        SELECT item_id, definition_revision AS revision, definition_digest AS digest,
               definition_json, reason, source_task_id, before_digest, after_digest,
               accepted_project_revision, accepted_at
        FROM work_item_definition_revisions
        WHERE {predicate}
        ORDER BY definition_revision DESC
        LIMIT ?
        """,
        (*parameters, limit + 1),
    ).fetchall()
    definitions = tuple(_definition_revision(row) for row in rows)
    if any(
        newer.revision != older.revision + 1 or newer.before_digest != older.digest
        for newer, older in pairwise(definitions)
    ):
        raise StorageError(StorageErrorCode.INVALID_STATE, "Definition history digest links are not contiguous.")
    if (
        definitions
        and len(definitions) <= limit
        and (definitions[-1].revision != 1 or definitions[-1].before_digest is not None)
    ):
        raise StorageError(StorageErrorCode.INVALID_STATE, "Definition history digest links are not contiguous.")
    if before_revision is not None:
        anchor_row = connection.execute(
            """
            SELECT item_id, definition_revision AS revision, definition_digest AS digest,
                   definition_json, reason, source_task_id, before_digest, after_digest,
                   accepted_project_revision, accepted_at
            FROM work_item_definition_revisions
            WHERE item_id = ? AND definition_revision = ?
            """,
            (item_id, before_revision),
        ).fetchone()
        if anchor_row is not None:
            anchor = _definition_revision(anchor_row)
            if definitions and (
                anchor.revision != definitions[0].revision + 1 or anchor.before_digest != definitions[0].digest
            ):
                raise StorageError(
                    StorageErrorCode.INVALID_STATE,
                    "Definition history digest links are not contiguous.",
                )
            if not definitions and (anchor.revision != 1 or anchor.before_digest is not None):
                raise StorageError(
                    StorageErrorCode.INVALID_STATE,
                    "Definition history digest links are not contiguous.",
                )
    return query_models.ItemDefinitionHistoryFacts(project_revision, True, definitions)


def _definition_revision_values(value: stored_state.ItemDefinitionRevision) -> tuple[str | int | bytes | None, ...]:
    payload = work_item_definition_bytes(value.definition)
    if isinstance(payload, DecisionFailure):
        raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, payload.message)
    return (
        value.item_id,
        value.revision,
        value.digest,
        payload,
        value.reason,
        value.source_task_id,
        value.before_digest,
        value.after_digest,
        value.accepted_project_revision,
        value.accepted_at.isoformat(),
    )


def read_lifecycle(
    connection: sqlite3.Connection,
    project: stored_state.ProjectRecord,
) -> stored_state.LifecycleRecords:
    items = tuple(
        decode_row(row, stored_state.StoredWorkItem)
        for row in connection.execute(
            """
            SELECT item_id, state, timing, source, outcome_evidence, next_action, notes, subject_revision,
                   recorded_at, updated_at, queue_position
            FROM work_items
            ORDER BY item_id
            """
        ).fetchall()
    )
    dependencies = tuple(
        decode_row(row, stored_state.ItemDependency)
        for row in connection.execute(
            "SELECT item_id, dependency_id, position FROM item_dependencies ORDER BY item_id, position"
        ).fetchall()
    )
    attempts = tuple(
        decode_row(row, stored_state.StoredAttempt)
        for row in connection.execute(
            """
            SELECT attempt_id, item_id, state, branch, base_revision, provenance, brief_artifact_ref_id,
                   result_artifact_ref_id, candidate_revision, candidate_recorded_at,
                   accepted_scope_revision, accepted_scope_digest, subject_revision, recorded_at, updated_at
            FROM attempts
            ORDER BY attempt_id
            """
        ).fetchall()
    )
    definitions = tuple(
        _definition_revision(row)
        for row in connection.execute(
            """
            SELECT item_id, definition_revision AS revision, definition_digest AS digest,
                   definition_json, reason, source_task_id, before_digest, after_digest,
                   accepted_project_revision, accepted_at
            FROM work_item_definition_revisions
            ORDER BY item_id, definition_revision
            """
        ).fetchall()
    )
    return stored_state.LifecycleRecords(project, items, dependencies, attempts, definitions)


def require_stored_item(state: stored_state.StoredWorkState, item_id: ItemId) -> stored_state.StoredWorkItem:
    value = next((candidate for candidate in state.lifecycle.work_items if candidate.item_id == item_id), None)
    if value is None:
        raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "The targeted mutation item is missing.")
    return value


def require_stored_attempt(state: stored_state.StoredWorkState, attempt_id: AttemptId) -> stored_state.StoredAttempt:
    value = next((candidate for candidate in state.lifecycle.attempts if candidate.attempt_id == attempt_id), None)
    if value is None:
        raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "The targeted mutation attempt is missing.")
    return value


def _queue_position(value: stored_state.StoredWorkItem) -> int:
    return value.queue_position or 0


def compact_queue(
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkState,
    removed_position: int,
) -> DecisionFailure | None:
    for value in sorted(
        (
            candidate
            for candidate in state.lifecycle.work_items
            if candidate.queue_position is not None and candidate.queue_position > removed_position
        ),
        key=_queue_position,
    ):
        position = value.queue_position
        if position is None:  # pragma: no cover - narrowed by the collection filter
            continue
        if (
            failure := require_one_changed_row(
                connection.execute(
                    "UPDATE work_items SET queue_position = ? WHERE item_id = ? AND queue_position = ?",
                    (position - 1, value.item_id, position),
                ),
                "The live queue changed before terminal persistence.",
            )
        ) is not None:
            return failure
    return None


def make_queue_space(
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkState,
    position: int,
) -> DecisionFailure | None:
    for value in sorted(
        (
            candidate
            for candidate in state.lifecycle.work_items
            if candidate.queue_position is not None and candidate.queue_position >= position
        ),
        key=_queue_position,
        reverse=True,
    ):
        current = value.queue_position
        if current is None:  # pragma: no cover - narrowed by the collection filter
            continue
        if (
            failure := require_one_changed_row(
                connection.execute(
                    "UPDATE work_items SET queue_position = ? WHERE item_id = ? AND queue_position = ?",
                    (current + 1, value.item_id, current),
                ),
                "The live queue changed before proposal persistence.",
            )
        ) is not None:
            return failure
    return None


def set_item_state(
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkState,
    item_id: ItemId,
    before_state: work_models.WorkState,
    after_state: stored_state.StoredWorkItemState,
    revision: int,
    now: datetime,
    outcome_evidence: str | None = None,
) -> DecisionFailure | None:
    current = require_stored_item(state, item_id)
    terminal = after_state in {
        stored_state.StoredWorkItemState.DONE,
        stored_state.StoredWorkItemState.SUPERSEDED,
        stored_state.StoredWorkItemState.DROPPED,
    }
    if (
        failure := require_one_changed_row(
            connection.execute(
                """
                UPDATE work_items
                SET state = ?, outcome_evidence = ?, subject_revision = ?, updated_at = ?, queue_position = ?
                WHERE item_id = ? AND state = ? AND subject_revision = ?
                """,
                (
                    after_state.value,
                    outcome_evidence,
                    revision,
                    now.isoformat(),
                    None if terminal else current.queue_position,
                    item_id,
                    before_state.value,
                    current.subject_revision,
                ),
            ),
            "The targeted item mutation is stale.",
        )
    ) is not None:
        return failure
    if terminal and current.queue_position is not None:
        return compact_queue(connection, state, current.queue_position)
    return None


def set_attempt_state(
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkState,
    attempt_id: AttemptId,
    before_state: work_models.AttemptState,
    after_state: work_models.AttemptState,
    revision: int,
    now: datetime,
    *,
    revised_brief: decision_models.RevisedAttemptBrief | None = None,
    result_artifact_ref_id: ArtifactRefId | None = None,
    candidate_revision: str | None = None,
    candidate_recorded_at: datetime | None = None,
) -> DecisionFailure | None:
    current = require_stored_attempt(state, attempt_id)
    if after_state == work_models.AttemptState.REVIEW:
        stored_candidate = candidate_revision
        stored_candidate_at = None if candidate_recorded_at is None else candidate_recorded_at.isoformat()
    elif after_state in {
        work_models.AttemptState.ACTIVE,
        work_models.AttemptState.PAUSED,
        work_models.AttemptState.BLOCKED,
    }:
        stored_candidate = None
        stored_candidate_at = None
    else:
        stored_candidate = current.candidate_revision
        stored_candidate_at = (
            None if current.candidate_recorded_at is None else current.candidate_recorded_at.isoformat()
        )
    return require_one_changed_row(
        connection.execute(
            """
            UPDATE attempts
            SET state = ?, brief_artifact_ref_id = ?, result_artifact_ref_id = ?,
                result_artifact_kind = ?, candidate_revision = ?, candidate_recorded_at = ?,
                accepted_scope_revision = ?, accepted_scope_digest = ?, subject_revision = ?, updated_at = ?
            WHERE attempt_id = ? AND state = ? AND subject_revision = ?
            """,
            (
                after_state.value,
                revised_brief.artifact_ref_id if revised_brief is not None else current.brief_artifact_ref_id,
                result_artifact_ref_id or current.result_artifact_ref_id,
                "result" if (result_artifact_ref_id or current.result_artifact_ref_id) is not None else None,
                stored_candidate,
                stored_candidate_at,
                revised_brief.accepted_scope_revision if revised_brief is not None else current.accepted_scope_revision,
                revised_brief.accepted_scope_digest if revised_brief is not None else current.accepted_scope_digest,
                revision,
                now.isoformat(),
                attempt_id,
                before_state.value,
                current.subject_revision,
            ),
        ),
        "The targeted attempt mutation is stale.",
    )


def rebind_attempt(
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkState,
    change: decision_models.RebindAttemptChange,
    revision: int,
    now: datetime,
) -> DecisionFailure | None:
    current = require_stored_attempt(state, change.attempt)
    return require_one_changed_row(
        connection.execute(
            """
            UPDATE attempts
            SET branch = ?, base_revision = ?, brief_artifact_ref_id = ?,
                accepted_scope_revision = ?, accepted_scope_digest = ?, subject_revision = ?, updated_at = ?
            WHERE attempt_id = ? AND item_id = ? AND state = ? AND subject_revision = ?
                AND accepted_scope_revision = ? AND accepted_scope_digest = ?
            """,
            (
                change.branch,
                change.base_revision,
                change.brief_artifact_ref_id,
                change.accepted_scope_revision,
                change.accepted_scope_digest,
                revision,
                now.isoformat(),
                change.attempt,
                change.item,
                change.attempt_state.value,
                current.subject_revision,
                current.accepted_scope_revision,
                current.accepted_scope_digest,
            ),
        ),
        "The targeted attempt rebind is stale.",
    )


def replace_dependencies(connection: sqlite3.Connection, item_id: ItemId, dependencies: tuple[ItemId, ...]) -> None:
    connection.execute("DELETE FROM item_dependencies WHERE item_id = ?", (item_id,))
    connection.executemany(
        "INSERT INTO item_dependencies (item_id, dependency_id, position) VALUES (?, ?, ?)",
        tuple((item_id, dependency, position) for position, dependency in enumerate(dependencies)),
    )


def insert_definition_revision(
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkState,
    revision: stored_state.ItemDefinitionRevision,
) -> DecisionFailure | None:
    current = next(
        (value for value in reversed(state.lifecycle.definition_revisions) if value.item_id == revision.item_id),
        None,
    )
    if current is None or revision.revision != current.revision + 1 or revision.before_digest != current.digest:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_STALE,
            "The current definition changed before persistence.",
            None,
        )
    append_definition_revision(connection, revision)
    current_item = require_stored_item(state, revision.item_id)
    return require_one_changed_row(
        connection.execute(
            """
            UPDATE work_items
            SET subject_revision = ?, updated_at = ?
            WHERE item_id = ? AND subject_revision = ?
            """,
            (
                revision.accepted_project_revision,
                revision.accepted_at.isoformat(),
                revision.item_id,
                current_item.subject_revision,
            ),
        ),
        "The work item changed before definition persistence.",
    )


def append_definition_revision(
    connection: sqlite3.Connection,
    revision: stored_state.ItemDefinitionRevision,
) -> None:
    values = _definition_revision_values(revision)
    connection.execute(
        """
        INSERT INTO work_item_definition_revisions (
            item_id, definition_revision, definition_digest, definition_json, reason,
            source_task_id, before_digest, after_digest, accepted_project_revision, accepted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )


def insert_attempt(
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkState,
    change: decision_models.ActivationChange,
    revision: int,
    now: datetime,
) -> DecisionFailure | None:
    require_stored_item(state, change.item)
    definition = next(
        (value for value in reversed(state.lifecycle.definition_revisions) if value.item_id == change.item),
        None,
    )
    if definition is None:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            "The activated work item has no current definition.",
            None,
        )
    return require_one_changed_row(
        connection.execute(
            """
            INSERT INTO attempts (
                attempt_id, item_id, state, branch, base_revision, provenance,
                brief_artifact_ref_id, brief_artifact_kind, result_artifact_ref_id, result_artifact_kind,
                candidate_revision, candidate_recorded_at, accepted_scope_revision, accepted_scope_digest,
                subject_revision, recorded_at, updated_at
            ) VALUES (?, ?, 'active', ?, ?, ?, ?, 'brief', NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?)
            ON CONFLICT(attempt_id) DO NOTHING
            """,
            (
                change.attempt,
                change.item,
                change.branch,
                change.base_revision,
                change.owner,
                change.brief_artifact_ref_id,
                definition.revision,
                definition.digest,
                revision,
                now.isoformat(),
                now.isoformat(),
            ),
        ),
        "The activation attempt already exists.",
    )
