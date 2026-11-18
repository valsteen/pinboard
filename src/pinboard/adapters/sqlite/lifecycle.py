"""Read and change lifecycle records on a supplied connection.

This module never commits, rolls back, closes the connection, calls callbacks,
reads the filesystem, or obtains time. Expected stale CAS writes return a
``DecisionFailure``; SQLite and persisted-invariant failures remain exceptional.
"""

import hashlib
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Annotated, Literal, NoReturn, assert_never

import msgspec

from pinboard.adapters.sqlite.amendments import read_all_amendments, read_amendment_facts
from pinboard.adapters.sqlite.artifacts import read_artifact_references_by_ids
from pinboard.adapters.sqlite.database import (
    decode_row,
    require_one_changed_row,
    select_by_ids,
    stale_write,
)
from pinboard.adapters.sqlite.dependencies import DependencyRow, DependencyTargetRow, read_accepted_checkpoints
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.models import AttemptIdRow
from pinboard.application import queries, query_models, released_receipt_compatibility, stored_state, unblockers
from pinboard.application.ports import StaleWriteCode
from pinboard.domain import decision_models, history, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.history import (
    accepted_definition_bytes,
)
from pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    TaskId,
    WorkItemId,
)


@dataclass(frozen=True, slots=True)
class _DefinitionRevisionRow:
    item_id: WorkItemId
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


class _AttemptItemRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    state: stored_state.StoredWorkItemState
    subject_revision: int


class _AttemptContextRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    item_id: WorkItemId
    subject_revision: int
    state: work_models.AttemptState
    branch: str
    base_revision: str
    brief_artifact_ref_id: ArtifactRefId
    candidate_revision: str | None
    accepted_scope_revision: int
    accepted_scope_digest: str


class _DependencyStateRow(DependencyTargetRow, frozen=True, forbid_unknown_fields=True):
    state: stored_state.StoredWorkItemState


class _ParallelPreviewItemRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    state: stored_state.StoredWorkItemState


class _SelectedDependencyStateRow(DependencyRow, frozen=True, forbid_unknown_fields=True):
    state: stored_state.StoredWorkItemState


class _SelectedPreviewAttemptRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    attempt_id: AttemptId
    state: work_models.AttemptState


class _QueuePositionRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    queue_position: int


class TransitionHistoryRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One transition-history row with its stored JSON columns still as text."""

    history_id: HistoryId
    project_revision: int
    action_id: ActionId
    action_kind: str
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


class _DecisionBatchReferenceRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    pause_history_id: Annotated[HistoryId, msgspec.Meta(ge=1)]
    resume_history_id: Annotated[HistoryId, msgspec.Meta(ge=1)] | None

    def __post_init__(self) -> None:
        if self.resume_history_id is not None and self.resume_history_id <= self.pause_history_id:
            raise ValueError("A resolving resume must follow its decision-batch pause.")


def decode_decision_batch_reference(row: sqlite3.Row) -> stored_state.DecisionBatchReference:
    value = decode_row(row, _DecisionBatchReferenceRow)
    return stored_state.DecisionBatchReference(value.attempt_id, value.pause_history_id, value.resume_history_id)


_CONSUMED_RECEIPT_COLUMNS = """history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
       authorization_kind AS authorization, actor_task_id, actor_host_id, input_schema,
       input_json, outcome_schema, outcome_json, committed_at"""


def decode_history_action_kind(
    value: str,
) -> decision_models.ActionKind | released_receipt_compatibility.HistoricalActionKind:
    try:
        return released_receipt_compatibility.decode_released_action_kind(value)
    except ValueError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Stored history has an unknown action kind.") from error


def _stored_json(column: str, value: str) -> work_models.CanonicalJson:
    encoded = value.encode("utf-8")
    try:
        msgspec.json.decode(encoded, type=msgspec.Raw)
    except msgspec.DecodeError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {column!r} has invalid JSON.") from error
    return work_models.CanonicalJson(encoded)


def stored_receipt_from_row(value: TransitionHistoryRow) -> stored_state.StoredTransitionReceipt:
    return stored_state.StoredTransitionReceipt(
        value.history_id,
        value.project_revision,
        value.action_id,
        decode_history_action_kind(value.action_kind),
        value.subject_id,
        value.artifact_ref_id,
        value.authorization,
        value.actor_task_id,
        value.actor_host_id,
        value.input_schema,
        _stored_json("input_json", value.input_json),
        value.outcome_schema,
        _stored_json("outcome_json", value.outcome_json),
        value.committed_at,
    )


def read_history_receipts_by_ids(
    connection: sqlite3.Connection, history_ids: tuple[HistoryId, ...]
) -> dict[HistoryId, stored_state.StoredTransitionReceipt]:
    return {
        receipt.history_id: receipt
        for receipt in (
            stored_receipt_from_row(decode_row(row, TransitionHistoryRow))
            for row in select_by_ids(
                connection,
                """SELECT history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
                          authorization_kind AS authorization, actor_task_id, actor_host_id, input_schema,
                          input_json, outcome_schema, outcome_json, committed_at
                   FROM transition_history WHERE history_id IN ({ids})""",
                history_ids,
            )
        )
    }


def _consumed_receipt(row: sqlite3.Row) -> query_models.ConsumedTransitionReceipt:
    """Decode receipt columns while leaving stored JSON text for the consuming read to diagnose."""

    value = decode_row(row, TransitionHistoryRow)
    return query_models.ConsumedTransitionReceipt(
        value.history_id,
        value.project_revision,
        value.action_id,
        decode_history_action_kind(value.action_kind),
        value.subject_id,
        value.artifact_ref_id,
        value.authorization,
        value.actor_task_id,
        value.actor_host_id,
        value.input_schema,
        value.input_json,
        value.outcome_schema,
        value.outcome_json,
        value.committed_at,
    )


class OpenAttemptRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One open attempt's row, with the identity its current brief must match, for the board and item status."""

    attempt_id: AttemptId
    item_id: WorkItemId
    state: work_models.AttemptState
    branch: str
    base_revision: str
    accepted_scope_revision: int
    accepted_scope_digest: str
    candidate_revision: str | None
    candidate_recorded_at: datetime | None
    brief_artifact_ref_id: ArtifactRefId
    subject_revision: int


class _ClosureReceiptRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_kind: str
    subject_id: HistorySubjectId
    committed_at: datetime


class _BranchOwnerRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    attempt_state: work_models.AttemptState
    item_id: WorkItemId
    item_state: stored_state.StoredWorkItemState


def read_receipts_at_revisions(
    connection: sqlite3.Connection, project_revisions: Iterable[int]
) -> dict[int, query_models.ConsumedTransitionReceipt]:
    """Read the receipts committed at exact project revisions through the unique revision index, in fixed batches.

    An attempt's subject revision names the project revision of its latest receipt, so callers read each open
    attempt's latest receipt without walking history. Stored JSON stays text for the consuming read to diagnose.
    """

    return {
        receipt.project_revision: receipt
        for receipt in (
            _consumed_receipt(row)
            for row in select_by_ids(
                connection,
                f"SELECT {_CONSUMED_RECEIPT_COLUMNS} FROM transition_history WHERE project_revision IN ({{ids}})",
                project_revisions,
            )
        )
    }


def read_recorded_pause_reasons(
    connection: sqlite3.Connection,
    attempts: Iterable[tuple[AttemptId, work_models.AttemptState, int]],
) -> dict[AttemptId, query_models.RecordedPauseReason]:
    """Read each paused attempt's recorded pause reason from exactly its latest receipt.

    Each reason comes from the receipt at the attempt's subject revision in one batched keyed read rather than a
    history scan. A receipt whose columns decode but whose outcome does not is returned as a damaged receipt for
    status reads and view generation to name.
    """

    paused = {
        subject_revision: attempt_id
        for attempt_id, state, subject_revision in attempts
        if state == work_models.AttemptState.PAUSED
    }
    reasons: dict[AttemptId, query_models.RecordedPauseReason] = {}
    for receipt in read_receipts_at_revisions(connection, paused).values():
        attempt_id = paused[receipt.project_revision]
        reasons[attempt_id] = queries.decode_recorded_pause_reason(
            attempt_id,
            work_models.AttemptState.PAUSED,
            receipt.history_id,
            receipt.committed_at,
            receipt.action_kind,
            receipt.outcome_schema,
            receipt.outcome_json.encode("utf-8"),
        )
    return reasons


def read_pause_reasons(
    connection: sqlite3.Connection,
    attempts: Iterable[tuple[AttemptId, work_models.AttemptState, int]],
) -> dict[AttemptId, str]:
    """Read pause reasons for decision reads, which reject a damaged receipt as invalid state."""

    reasons: dict[AttemptId, str] = {}
    for attempt_id, reason in read_recorded_pause_reasons(connection, attempts).items():
        if isinstance(reason, query_models.DamagedTransitionReceipt):
            reject_damaged_pause_receipt(reason)
        if reason is not None:
            reasons[attempt_id] = reason
    return reasons


def reject_damaged_pause_receipt(_damaged: query_models.DamagedTransitionReceipt) -> NoReturn:
    """Keep the generic invalid-state rejection for decision reads of a damaged pause receipt."""

    raise StorageError(StorageErrorCode.INVALID_STATE, "A paused attempt's latest receipt is not canonical.")


def _read_review_event(
    connection: sqlite3.Connection,
    item_id: WorkItemId,
    attempt_id: AttemptId,
    subject_revision: int,
) -> query_models.ReviewEventFacts | None:
    """Walk this attempt's receipts backward to its latest review-relevant receipt.

    The walk descends the unique project-revision index from the attempt's latest
    receipt and stops at the first review-relevant receipt or at the item's
    activation receipt, which starts the current attempt.
    """

    rebound = False
    cursor = connection.execute(
        f"""
        SELECT {_CONSUMED_RECEIPT_COLUMNS}
        FROM transition_history
        WHERE project_revision <= ?
          AND ((subject_id = ? AND action_kind IN (
                  'submit-review', 'return-for-correction', 'accept-review-and-continue',
                  'accept-checkpoint', 'rebind-attempt'))
               OR (subject_id = ? AND action_kind = 'activate'))
        ORDER BY project_revision DESC
        """,
        (subject_revision, attempt_id, item_id),
    )
    for row in cursor:
        receipt = _consumed_receipt(row)
        match receipt.action_kind:
            case decision_models.ActionKind.REBIND_ATTEMPT:
                rebound = True
            case (
                decision_models.ActionKind.SUBMIT_REVIEW
                | decision_models.ActionKind.RETURN_FOR_CORRECTION
                | decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE
                | decision_models.ActionKind.ACCEPT_CHECKPOINT
            ) as review_action:
                return query_models.ReviewEventFacts(review_action, receipt, rebound)
            case decision_models.ActionKind.ACTIVATE:
                return None
            case _:
                raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "The review walk selected another receipt.")
    return None


def read_item_closure(
    connection: sqlite3.Connection,
    item_id: WorkItemId,
    subject_revision: int,
) -> query_models.ItemClosureFacts | None:
    """Read closure facts from the row columns of the receipt at a terminal item's subject revision."""

    row = connection.execute(
        "SELECT action_kind, subject_id, committed_at FROM transition_history WHERE project_revision = ?",
        (subject_revision,),
    ).fetchone()
    if row is None:
        return None
    receipt = decode_row(row, _ClosureReceiptRow)
    action_kind = decode_history_action_kind(receipt.action_kind)
    closing_attempt = None
    if action_kind == decision_models.ActionKind.COMPLETE:
        attempt_row = connection.execute(
            "SELECT attempt_id, branch, candidate_revision FROM attempts WHERE attempt_id = ? AND item_id = ?",
            (receipt.subject_id, item_id),
        ).fetchone()
        closing_attempt = None if attempt_row is None else decode_row(attempt_row, query_models.ClosingAttemptFacts)
    return query_models.ItemClosureFacts(action_kind, receipt.committed_at, closing_attempt)


def read_latest_checkpoint_acceptance(
    connection: sqlite3.Connection, attempt_id: AttemptId
) -> query_models.ConsumedTransitionReceipt | None:
    """Read one attempt's latest checkpoint-acceptance receipt through the checkpoint partial index."""

    row = connection.execute(
        f"""
        SELECT {_CONSUMED_RECEIPT_COLUMNS}
        FROM transition_history INDEXED BY checkpoint_history_by_subject
        WHERE subject_id = ? AND outcome_schema = 'checkpoint-acceptance/v2'
        ORDER BY history_id DESC
        LIMIT 1
        """,
        (attempt_id,),
    ).fetchone()
    return None if row is None else _consumed_receipt(row)


def read_branch_owners(connection: sqlite3.Connection, branch: str) -> query_models.BranchOwnersFacts:
    """Scan retained attempts for an exact branch; read owning items by key and no history or artifacts."""

    project_revision_row = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
    if project_revision_row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Project metadata is missing.")
    rows = connection.execute(
        """
        SELECT attempt.attempt_id, attempt.state AS attempt_state, attempt.item_id, item.state AS item_state
        FROM attempts AS attempt
        JOIN work_items AS item ON item.item_id = attempt.item_id
        WHERE attempt.branch = ?
        ORDER BY attempt.item_id, attempt.attempt_id
        """,
        (branch,),
    ).fetchall()
    return query_models.BranchOwnersFacts(
        decode_row(project_revision_row, _ProjectRevisionRow).revision,
        tuple(
            query_models.BranchOwnerFacts(value.item_id, value.item_state, value.attempt_id, value.attempt_state)
            for value in (decode_row(row, _BranchOwnerRow) for row in rows)
        ),
    )


def increment_item_state_count(
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkItemState,
) -> None:
    cursor = connection.execute(
        "UPDATE work_item_state_counts SET item_count = item_count + 1 WHERE state = ?",
        (state.value,),
    )
    if cursor.rowcount != 1:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Work-item state counts are incomplete.")


def move_item_state_count(
    connection: sqlite3.Connection,
    before: stored_state.StoredWorkItemState,
    after: stored_state.StoredWorkItemState,
) -> None:
    if before == after:
        return
    decremented = connection.execute(
        """
        UPDATE work_item_state_counts
        SET item_count = item_count - 1
        WHERE state = ? AND item_count > 0
        """,
        (before.value,),
    )
    if decremented.rowcount != 1:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Work-item state counts do not match stored items.")
    increment_item_state_count(connection, after)


@dataclass(frozen=True, slots=True)
class TerminalAttemptContextSelection:
    project_revision: int
    attempt_id: AttemptId
    item_id: WorkItemId
    item_state: stored_state.StoredWorkItemState


@dataclass(frozen=True, slots=True)
class NonterminalAttemptContextSelection:
    project_revision: int
    attempt_id: AttemptId
    subject_revision: str
    item_id: WorkItemId
    state: query_models.NonterminalAttemptState
    branch: str
    base_revision: str
    accepted_scope_revision: int
    accepted_scope_digest: str
    candidate_revision: str | None
    brief_artifact_ref_id: ArtifactRefId
    work_item: query_models.AttemptContextItemFacts
    pause_reason: query_models.RecordedPauseReason


type AttemptContextSelection = TerminalAttemptContextSelection | NonterminalAttemptContextSelection


@dataclass(frozen=True, slots=True)
class ParallelPreviewLifecycleAttempt:
    attempt_id: AttemptId
    state: query_models.NonterminalAttemptState


@dataclass(frozen=True, slots=True)
class ParallelPreviewLifecycleItem:
    item_id: WorkItemId
    label: str
    state: work_models.WorkState
    unsatisfied_dependencies: tuple[work_models.Dependency, ...]
    attempt: ParallelPreviewLifecycleAttempt | None
    dependency_conditions: tuple[query_models.DependencyStatus, ...]


@dataclass(frozen=True, slots=True)
class ParallelPreviewLifecycleSelection:
    project_revision: int
    items: tuple[ParallelPreviewLifecycleItem, ...]


def validate_current_attempt_relation(
    operation: str,
    item_id: WorkItemId,
    state: stored_state.StoredWorkItemState,
    attempt_state: work_models.AttemptState | None,
    error_code: StorageErrorCode,
) -> None:
    allowed = stored_state.allowed_current_attempt_states(state)
    if attempt_state not in allowed:
        expected = " or ".join("none" if value is None else value.value for value in allowed)
        observed = "none" if attempt_state is None else attempt_state.value
        raise StorageError(
            error_code,
            f"{operation}: work item '{item_id}' state '{state.value}' requires current attempt state "
            f"'{expected}', observed '{observed}'; effect unchanged.",
        )


def decode_definition_revision(row: sqlite3.Row) -> stored_state.ItemDefinitionRevision:
    value = decode_row(row, _DefinitionRevisionRow)
    try:
        definition = stored_state.decode_stored_definition(value.definition_json)
    except (msgspec.DecodeError, ValueError) as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Definition history is invalid: {error}") from error
    if hashlib.sha256(value.definition_json).hexdigest() != value.digest or value.after_digest != value.digest:
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


def current_definition_anchor(value: stored_state.ItemDefinitionRevision) -> work_models.DefinitionAnchor:
    if not isinstance(value.definition, work_models.WorkItemDefinition):
        raise StorageError(
            StorageErrorCode.INVALID_STATE,
            "A live work item requires a genuine current supported definition: origin-free v2 or origin-bearing v3; "
            "historical v1 is not executable.",
        )
    return work_models.DefinitionAnchor(value.item_id, value.revision, value.digest, value.definition)


def read_current_definition(
    connection: sqlite3.Connection, item_id: WorkItemId
) -> stored_state.ItemDefinitionRevision | None:
    row = connection.execute(
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
    return None if row is None else decode_definition_revision(row)


def read_current_definitions(
    connection: sqlite3.Connection, item_ids: tuple[WorkItemId, ...]
) -> dict[WorkItemId, stored_state.ItemDefinitionRevision]:
    rows = select_by_ids(
        connection,
        """
        SELECT item_id, definition_revision AS revision, definition_digest AS digest,
               definition_json, reason, source_task_id, before_digest, after_digest,
               accepted_project_revision, accepted_at
        FROM work_item_definition_revisions AS definition
        WHERE item_id IN ({ids}) AND definition_revision = (
            SELECT MAX(current.definition_revision)
            FROM work_item_definition_revisions AS current
            WHERE current.item_id = definition.item_id
        )
        """,
        item_ids,
    )
    return {value.item_id: value for value in map(decode_definition_revision, rows)}


def read_definition_histories(
    connection: sqlite3.Connection, item_ids: tuple[WorkItemId, ...]
) -> dict[WorkItemId, tuple[stored_state.ItemDefinitionRevision, ...]]:
    """Read every accepted definition revision of the selected items by primary key, in fixed-size batches.

    Each history must link contiguously from revision 1 by digest; a broken chain is invalid state rather than a
    partial history. No other item's revisions are read.
    """

    grouped: dict[WorkItemId, list[stored_state.ItemDefinitionRevision]] = {}
    for value in map(
        decode_definition_revision,
        select_by_ids(
            connection,
            """
            SELECT item_id, definition_revision AS revision, definition_digest AS digest,
                   definition_json, reason, source_task_id, before_digest, after_digest,
                   accepted_project_revision, accepted_at
            FROM work_item_definition_revisions
            WHERE item_id IN ({ids})
            ORDER BY item_id, definition_revision
            """,
            item_ids,
        ),
    ):
        grouped.setdefault(value.item_id, []).append(value)
    for revisions in grouped.values():
        if (
            revisions[0].revision != 1
            or revisions[0].before_digest is not None
            or any(
                newer.revision != older.revision + 1 or newer.before_digest != older.digest
                for older, newer in pairwise(revisions)
            )
        ):
            raise StorageError(StorageErrorCode.INVALID_STATE, "Definition history digest links are not contiguous.")
    return {item_id: tuple(revisions) for item_id, revisions in grouped.items()}


def _current_definition_with_dependency_states(
    connection: sqlite3.Connection,
    item_id: WorkItemId,
    *,
    missing_message: str,
) -> tuple[stored_state.ItemDefinitionRevision, tuple[_DependencyStateRow, ...]]:
    definition = read_current_definition(connection, item_id)
    if definition is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, missing_message)
    dependencies = tuple(
        decode_row(row, _DependencyStateRow)
        for row in connection.execute(
            """
            SELECT dependency.dependency_id, dependency.checkpoint_id, item.state
            FROM item_dependencies AS dependency
            JOIN work_items AS item ON item.item_id = dependency.dependency_id
            WHERE dependency.item_id = ?
            ORDER BY dependency.position
            """,
            (item_id,),
        ).fetchall()
    )
    if tuple(value.target for value in dependencies) != definition.definition.dependencies:
        raise StorageError(
            StorageErrorCode.INVALID_STATE,
            "Current definition dependencies do not match relational dependencies.",
        )
    return definition, dependencies


def read_item_definition(connection: sqlite3.Connection, item_id: WorkItemId) -> query_models.ItemDefinitionFacts:
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
    selected_definition = None if definition is None else decode_definition_revision(definition)
    if selected_definition is not None:
        dependency_rows = connection.execute(
            "SELECT dependency_id, checkpoint_id FROM item_dependencies WHERE item_id = ? ORDER BY position",
            (item_id,),
        ).fetchall()
        dependencies = tuple(decode_row(row, DependencyTargetRow).target for row in dependency_rows)
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


def read_item_status(
    connection: sqlite3.Connection, item_id: WorkItemId
) -> query_models.ItemStatusLifecycleFacts | None:
    project_revision_row = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
    if project_revision_row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Project metadata is missing.")
    project_revision = decode_row(project_revision_row, _ProjectRevisionRow).revision
    item_row = connection.execute(
        """
        SELECT item_id AS work_item_id, state, timing, outcome_evidence, next_action, source, notes, queue_position,
               subject_revision
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
    definition = None if definition_row is None else decode_definition_revision(definition_row)
    attempt_row = connection.execute(
        """
        SELECT attempt_id, item_id, state, branch, base_revision, accepted_scope_revision, accepted_scope_digest,
               candidate_revision, candidate_recorded_at, brief_artifact_ref_id, subject_revision
        FROM attempts INDEXED BY one_live_attempt_per_item
        WHERE item_id = ? AND state != 'done'
        """,
        (item_id,),
    ).fetchone()
    attempts: tuple[query_models.ItemStatusAttemptFacts, ...] = ()
    if attempt_row is not None:
        selected_attempt = decode_row(attempt_row, OpenAttemptRow)
        brief = read_artifact_references_by_ids(connection, (selected_attempt.brief_artifact_ref_id,)).get(
            selected_attempt.brief_artifact_ref_id
        )
        if brief is None or brief.kind != work_models.ArtifactKind.BRIEF:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The current attempt has no accepted brief reference.")
        pause_reasons = read_recorded_pause_reasons(
            connection,
            ((selected_attempt.attempt_id, selected_attempt.state, selected_attempt.subject_revision),),
        )
        attempts = (
            query_models.ItemStatusAttemptFacts(
                selected_attempt.attempt_id,
                selected_attempt.item_id,
                selected_attempt.state,
                selected_attempt.branch,
                selected_attempt.base_revision,
                selected_attempt.accepted_scope_revision,
                selected_attempt.accepted_scope_digest,
                selected_attempt.candidate_revision,
                brief,
                pause_reasons.get(selected_attempt.attempt_id),
                _read_review_event(
                    connection, item.work_item_id, selected_attempt.attempt_id, selected_attempt.subject_revision
                ),
                read_amendment_facts(
                    connection, selected_attempt.attempt_id, selected_attempt.brief_artifact_ref_id
                ).current,
            ),
        )
    closure = (
        read_item_closure(connection, item.work_item_id, item.subject_revision)
        if stored_state.live_work_state(item.state) is None
        else None
    )
    dependency_rows = read_selected_dependency_states(connection, (item_id,)).get(item_id, ())
    if (
        definition is not None
        and tuple(value.target for value in dependency_rows) != definition.definition.dependencies
    ):
        raise StorageError(
            StorageErrorCode.INVALID_STATE, "Current definition dependencies do not match relational dependencies."
        )
    accepted = read_accepted_checkpoints(connection, (value.target for value in dependency_rows))
    return query_models.ItemStatusLifecycleFacts(
        project_revision,
        item,
        None if definition is None else definition.definition.title,
        attempts,
        closure,
        tuple(
            query_models.DependencyStatus(
                history.dependency_payload(value.target),
                value.state in {stored_state.StoredWorkItemState.DONE, stored_state.StoredWorkItemState.SUPERSEDED}
                or value.target in accepted,
                value.target in accepted,
            )
            for value in dependency_rows
        ),
        tuple((value.target, stored_state.prerequisite_outcome(value.state)) for value in dependency_rows),
    )


def read_selected_dependency_states(
    connection: sqlite3.Connection,
    item_ids: tuple[WorkItemId, ...],
) -> dict[WorkItemId, tuple[_SelectedDependencyStateRow, ...]]:
    """Read selected items' ordered dependency states on the caller's connection."""

    dependencies_by_item: dict[WorkItemId, list[_SelectedDependencyStateRow]] = {}
    for row in select_by_ids(
        connection,
        """SELECT dependency.item_id, dependency.dependency_id, dependency.checkpoint_id, item.state
           FROM item_dependencies AS dependency
           JOIN work_items AS item ON item.item_id = dependency.dependency_id
           WHERE dependency.item_id IN ({ids}) ORDER BY dependency.item_id, dependency.position""",
        item_ids,
    ):
        dependency = decode_row(row, _SelectedDependencyStateRow)
        dependencies_by_item.setdefault(dependency.item_id, []).append(dependency)
    return {item_id: tuple(dependencies) for item_id, dependencies in dependencies_by_item.items()}


def read_parallel_preview_lifecycle(
    connection: sqlite3.Connection,
    item_ids: tuple[WorkItemId, ...],
) -> ParallelPreviewLifecycleSelection | None:
    project_revision_row = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
    if project_revision_row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Project metadata is missing.")
    project_revision = decode_row(project_revision_row, _ProjectRevisionRow).revision
    selected_items = {
        item.item_id: item
        for item in (
            decode_row(row, _ParallelPreviewItemRow)
            for row in select_by_ids(
                connection,
                "SELECT item_id, state FROM work_items WHERE item_id IN ({ids})",
                item_ids,
            )
        )
    }
    definitions = read_current_definitions(connection, item_ids)
    dependencies_by_item = read_selected_dependency_states(connection, item_ids)
    accepted = read_accepted_checkpoints(
        connection, (value.target for values in dependencies_by_item.values() for value in values)
    )
    attempts = {
        attempt.item_id: attempt
        for attempt in (
            decode_row(row, _SelectedPreviewAttemptRow)
            for row in select_by_ids(
                connection,
                """SELECT item_id, attempt_id, state FROM attempts INDEXED BY one_live_attempt_per_item
                   WHERE item_id IN ({ids}) AND state != 'done'""",
                item_ids,
            )
        )
    }
    selected: list[ParallelPreviewLifecycleItem] = []
    for item_id in item_ids:
        item = selected_items.get(item_id)
        if item is None:
            return None
        state = stored_state.live_work_state(item.state)
        if state is None:
            return None
        definition = definitions.get(item_id)
        if definition is None:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The selected work item has no definition.")
        dependencies = dependencies_by_item.get(item_id, ())
        if tuple(value.target for value in dependencies) != definition.definition.dependencies:
            raise StorageError(
                StorageErrorCode.INVALID_STATE, "Current definition dependencies do not match relational dependencies."
            )
        decoded_attempt = attempts.get(item_id)
        attempt = None
        if decoded_attempt is not None:
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
        validate_current_attempt_relation(
            "read_parallel_preview_lifecycle",
            item.item_id,
            item.state,
            None if attempt is None else attempt.state,
            StorageErrorCode.INVALID_STATE,
        )
        selected.append(
            ParallelPreviewLifecycleItem(
                item.item_id,
                definition.definition.title,
                state,
                tuple(
                    value.target
                    for value in dependencies
                    if value.state
                    not in {stored_state.StoredWorkItemState.DONE, stored_state.StoredWorkItemState.SUPERSEDED}
                    and value.target not in accepted
                ),
                attempt,
                tuple(
                    query_models.DependencyStatus(
                        history.dependency_payload(value.target),
                        work_models.dependency_satisfied(
                            value.target,
                            value.state
                            in {stored_state.StoredWorkItemState.DONE, stored_state.StoredWorkItemState.SUPERSEDED},
                            accepted,
                        ),
                        value.target in accepted,
                    )
                    for value in dependencies
                ),
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
        SELECT attempt_id, item_id, subject_revision, state, branch, base_revision, brief_artifact_ref_id,
               candidate_revision, accepted_scope_revision, accepted_scope_digest
        FROM attempts
        WHERE attempt_id = ?
        """,
        (attempt_id,),
    ).fetchone()
    if attempt_row is None:
        return None
    attempt = decode_row(attempt_row, _AttemptContextRow)
    match attempt.state:
        case work_models.AttemptState.DONE:
            row = connection.execute(
                "SELECT item_id, state, subject_revision FROM work_items WHERE item_id = ?", (attempt.item_id,)
            ).fetchone()
            if row is None:
                raise StorageError(StorageErrorCode.INVALID_STATE, "Terminal attempt has no parent item.")
            item = decode_row(row, _AttemptItemRow)
            return TerminalAttemptContextSelection(project_revision, attempt.attempt_id, attempt.item_id, item.state)
        case (
            work_models.AttemptState.ACTIVE
            | work_models.AttemptState.PAUSED
            | work_models.AttemptState.BLOCKED
            | work_models.AttemptState.REVIEW
        ) as attempt_state:
            pass
        case _ as unreachable:
            assert_never(unreachable)

    item_row = connection.execute(
        "SELECT item_id, state, subject_revision FROM work_items WHERE item_id = ?",
        (attempt.item_id,),
    ).fetchone()
    if item_row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "The selected nonterminal attempt has no work item.")
    item = decode_row(item_row, _AttemptItemRow)
    validate_current_attempt_relation(
        "read_attempt_context", attempt.item_id, item.state, attempt_state, StorageErrorCode.INVALID_STATE
    )
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
    accepted = read_accepted_checkpoints(connection, (value.target for value in dependencies))
    return NonterminalAttemptContextSelection(
        project_revision,
        attempt.attempt_id,
        str(attempt.subject_revision),
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
            str(item.subject_revision),
            item_state,
            definition.revision,
            definition.digest,
            definition.definition.route_facts
            if isinstance(definition.definition, work_models.WorkItemDefinition)
            else None,
            tuple(
                value.target
                for value in dependencies
                if value.state
                not in {stored_state.StoredWorkItemState.DONE, stored_state.StoredWorkItemState.SUPERSEDED}
                and value.target not in accepted
            ),
            None,
            True,
            read_item_unblockers(connection, (item.item_id,)).get(item.item_id),
        ),
        read_recorded_pause_reasons(connection, ((attempt.attempt_id, attempt_state, attempt.subject_revision),)).get(
            attempt.attempt_id
        ),
    )


def read_item_definition_history(
    connection: sqlite3.Connection,
    item_id: WorkItemId,
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
    definitions = tuple(decode_definition_revision(row) for row in rows)
    if before_revision is None and not definitions:
        raise StorageError(StorageErrorCode.INVALID_STATE, "An existing work item must have definition history.")
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
            anchor = decode_definition_revision(anchor_row)
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
    return (
        value.item_id,
        value.revision,
        value.digest,
        accepted_definition_bytes(current_definition_anchor(value).definition),
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
            "SELECT item_id, dependency_id, position, checkpoint_id FROM item_dependencies ORDER BY item_id, position"
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
        decode_definition_revision(row)
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
    acceptances = tuple(
        decode_row(row, stored_state.CheckpointAcceptance)
        for row in connection.execute(
            "SELECT history_id, item_id, checkpoint_id FROM checkpoint_acceptances ORDER BY history_id"
        )
    )
    return stored_state.LifecycleRecords(
        project,
        items,
        dependencies,
        attempts,
        definitions,
        checkpoint_acceptances=acceptances,
        decision_batch_references=tuple(
            decode_decision_batch_reference(row)
            for row in connection.execute(
                "SELECT attempt_id, pause_history_id, resume_history_id FROM attempt_decision_batches ORDER BY attempt_id"
            )
        ),
        unblockers=tuple(
            stored_state.StoredUnblocker(item_id, unblocker)
            for item_id, unblocker in sorted(read_all_item_unblockers(connection).items())
        ),
        amendments=read_all_amendments(connection),
        released_holds=read_all_released_holds(connection),
    )


def compact_queue(
    connection: sqlite3.Connection,
    removed_position: int,
) -> DecisionFailure[StaleWriteCode] | None:
    rows = connection.execute(
        "SELECT item_id, queue_position FROM work_items WHERE queue_position > ? ORDER BY queue_position",
        (removed_position,),
    ).fetchall()
    for row in rows:
        selected = decode_row(row, _QueuePositionRow)
        item_id = selected.item_id
        position = selected.queue_position
        if (
            failure := require_one_changed_row(
                connection.execute(
                    "UPDATE work_items SET queue_position = ? WHERE item_id = ? AND queue_position = ?",
                    (position - 1, item_id, position),
                ),
                "The live queue changed before terminal persistence.",
            )
        ) is not None:
            return failure
    return None


def make_queue_space(
    connection: sqlite3.Connection,
    position: int,
) -> DecisionFailure[StaleWriteCode] | None:
    rows = connection.execute(
        "SELECT item_id, queue_position FROM work_items WHERE queue_position >= ? ORDER BY queue_position DESC",
        (position,),
    ).fetchall()
    for row in rows:
        selected = decode_row(row, _QueuePositionRow)
        item_id = selected.item_id
        current = selected.queue_position
        if (
            failure := require_one_changed_row(
                connection.execute(
                    "UPDATE work_items SET queue_position = ? WHERE item_id = ? AND queue_position = ?",
                    (current + 1, item_id, current),
                ),
                "The live queue changed before proposal persistence.",
            )
        ) is not None:
            return failure
    return None


_HELD_STATES = frozenset({stored_state.StoredWorkItemState.PAUSED, stored_state.StoredWorkItemState.BLOCKED})


def write_item_unblocker(
    connection: sqlite3.Connection, item_id: WorkItemId, unblocker: work_models.Unblocker | None
) -> None:
    """Replace the item's current unblocker; none removes it when the item stops being held."""

    connection.execute("DELETE FROM work_item_unblockers WHERE item_id = ?", (item_id,))
    if unblocker is not None:
        connection.execute(
            "INSERT INTO work_item_unblockers (item_id, unblocker_json) VALUES (?, ?)",
            (item_id, unblockers.encode_unblocker(unblocker).decode("utf-8")),
        )


class _UnblockerRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    unblocker_json: str


def read_item_unblockers(
    connection: sqlite3.Connection, item_ids: Iterable[WorkItemId]
) -> dict[WorkItemId, work_models.Unblocker]:
    """Read the selected held items' current unblockers, decoding each stored value once."""

    selected: dict[WorkItemId, work_models.Unblocker] = {}
    for row in select_by_ids(
        connection,
        "SELECT item_id, unblocker_json FROM work_item_unblockers WHERE item_id IN ({ids})",
        item_ids,
    ):
        stored = decode_row(row, _UnblockerRow)
        try:
            selected[stored.item_id] = unblockers.decode_unblocker(stored.unblocker_json)
        except (msgspec.DecodeError, ValueError) as error:
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                f"Held item '{stored.item_id}' records an unreadable unblocker: {error}",
            ) from error
    return selected


def read_all_item_unblockers(connection: sqlite3.Connection) -> dict[WorkItemId, work_models.Unblocker]:
    return read_item_unblockers(
        connection,
        (
            decode_row(row, _UnblockerItemRow).item_id
            for row in connection.execute("SELECT item_id FROM work_item_unblockers")
        ),
    )


class _UnblockerItemRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId


class _ReleasedHoldRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: HistoryId
    item_id: WorkItemId
    previous_state: stored_state.StoredWorkItemState
    previous_subject_revision: int
    unblocker_json: str


class _ItemReleasedHoldViewRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: HistoryId
    project_revision: int
    action_id: ActionId
    committed_at: datetime
    item_id: WorkItemId
    previous_state: stored_state.StoredWorkItemState
    previous_subject_revision: int
    unblocker_json: str


def _decode_released_items_unblocker(
    history_id: HistoryId, item_id: WorkItemId, unblocker_json: str
) -> work_models.WaitingForItemsToFinish:
    try:
        decoded = unblockers.decode_unblocker(unblocker_json)
    except (msgspec.DecodeError, ValueError) as error:
        raise StorageError(
            StorageErrorCode.INVALID_STATE,
            f"Released hold for '{item_id}' at receipt {history_id} records an unreadable unblocker: {error}",
        ) from error
    if not isinstance(decoded, work_models.WaitingForItemsToFinish):
        raise StorageError(
            StorageErrorCode.INVALID_STATE,
            f"Released hold for '{item_id}' at receipt {history_id} must record an items unblocker.",
        )
    if unblockers.encode_unblocker(decoded).decode("utf-8") != unblocker_json:
        raise StorageError(
            StorageErrorCode.INVALID_STATE,
            f"Released hold for '{item_id}' at receipt {history_id} is not canonical.",
        )
    return decoded


def _stored_released_hold(row: sqlite3.Row) -> stored_state.StoredReleasedHold:
    value = decode_row(row, _ReleasedHoldRow)
    return stored_state.StoredReleasedHold(
        value.history_id,
        value.item_id,
        value.previous_state,
        value.previous_subject_revision,
        _decode_released_items_unblocker(value.history_id, value.item_id, value.unblocker_json),
    )


def read_all_released_holds(connection: sqlite3.Connection) -> tuple[stored_state.StoredReleasedHold, ...]:
    return tuple(
        _stored_released_hold(row)
        for row in connection.execute(
            """SELECT history_id, item_id, previous_state, previous_subject_revision, unblocker_json
               FROM released_item_holds
               ORDER BY history_id, item_id"""
        ).fetchall()
    )


def read_released_holds_by_history_ids(
    connection: sqlite3.Connection, history_ids: Iterable[HistoryId]
) -> dict[HistoryId, tuple[stored_state.StoredReleasedHold, ...]]:
    grouped: dict[HistoryId, list[stored_state.StoredReleasedHold]] = {}
    for row in select_by_ids(
        connection,
        """SELECT history_id, item_id, previous_state, previous_subject_revision, unblocker_json
           FROM released_item_holds
           WHERE history_id IN ({ids})
           ORDER BY history_id, item_id""",
        history_ids,
    ):
        hold = _stored_released_hold(row)
        grouped.setdefault(hold.history_id, []).append(hold)
    return {history_id: tuple(values) for history_id, values in grouped.items()}


def read_item_released_hold_views(
    connection: sqlite3.Connection, item_ids: Iterable[WorkItemId]
) -> dict[WorkItemId, tuple[query_models.ItemReleasedHoldView, ...]]:
    grouped: dict[WorkItemId, list[query_models.ItemReleasedHoldView]] = {}
    for row in select_by_ids(
        connection,
        """SELECT released.history_id, history.project_revision, history.action_id, history.committed_at,
                  released.item_id, released.previous_state, released.previous_subject_revision,
                  released.unblocker_json
           FROM released_item_holds AS released INDEXED BY released_item_holds_by_item
           JOIN transition_history AS history ON history.history_id = released.history_id
           WHERE released.item_id IN ({ids})
           ORDER BY released.item_id, released.history_id""",
        item_ids,
    ):
        value = decode_row(row, _ItemReleasedHoldViewRow)
        grouped.setdefault(value.item_id, []).append(
            query_models.ItemReleasedHoldView(
                value.history_id,
                value.project_revision,
                value.action_id,
                value.committed_at,
                value.previous_state,
                value.previous_subject_revision,
                _decode_released_items_unblocker(value.history_id, value.item_id, value.unblocker_json),
            )
        )
    return {item_id: tuple(values) for item_id, values in grouped.items()}


def read_held_attempt_ids(connection: sqlite3.Connection, item_ids: tuple[WorkItemId, ...]) -> tuple[AttemptId, ...]:
    """Read paused or blocked open attempt IDs for the selected items in attempt-ID order."""

    return tuple(
        decode_row(row, AttemptIdRow).attempt_id
        for row in select_by_ids(
            connection,
            """SELECT attempt_id FROM attempts INDEXED BY one_live_attempt_per_item
               WHERE item_id IN ({ids}) AND state != 'done' AND state IN ('paused', 'blocked')
               ORDER BY attempt_id""",
            item_ids,
        )
    )


def persist_released_holds(
    connection: sqlite3.Connection,
    before_items: Mapping[WorkItemId, stored_state.StoredWorkItem],
    released_holds: tuple[decision_models.ReleasedItemHold, ...],
    history_id: HistoryId,
    revision: int,
    now: datetime,
) -> DecisionFailure[StaleWriteCode] | None:
    """Release each newly releasable unstarted items hold to ready and record provenance at ``history_id``."""

    for hold in released_holds:
        before = before_items.get(hold.work_item_id)
        if (
            before is None
            or hold.previous_state != work_models.WorkState.BLOCKED
            or stored_state.live_work_state(before.state) != hold.previous_state
            or (
                hold.previous_subject_revision is not None
                and str(before.subject_revision) != hold.previous_subject_revision
            )
        ):
            return stale_write("A released unstarted hold changed before commit.")
        open_attempt = connection.execute(
            "SELECT 1 FROM attempts INDEXED BY one_live_attempt_per_item WHERE item_id = ? AND state != 'done'",
            (hold.work_item_id,),
        ).fetchone()
        if open_attempt is not None:
            return stale_write("A released unstarted hold changed before commit.")
        encoded = unblockers.encode_unblocker(hold.unblocker).decode("utf-8")
        current_unblocker = connection.execute(
            "SELECT unblocker_json FROM work_item_unblockers WHERE item_id = ?",
            (hold.work_item_id,),
        ).fetchone()
        if current_unblocker is None or str(current_unblocker[0]) != encoded:
            return stale_write("A released unstarted hold changed before commit.")
        if (
            failure := require_one_changed_row(
                connection.execute(
                    """
                    UPDATE work_items
                    SET state = 'ready', subject_revision = ?, updated_at = ?
                    WHERE item_id = ? AND state = ? AND subject_revision IN (?, ?) AND queue_position IS NOT NULL
                    """,
                    (
                        revision,
                        now.isoformat(),
                        hold.work_item_id,
                        before.state.value,
                        before.subject_revision,
                        revision,
                    ),
                ),
                "A released unstarted hold changed before commit.",
            )
        ) is not None:
            return failure
        write_item_unblocker(connection, hold.work_item_id, None)
        move_item_state_count(connection, before.state, stored_state.StoredWorkItemState.READY)
        connection.execute(
            """
            INSERT INTO released_item_holds (
                history_id, item_id, previous_state, previous_subject_revision, unblocker_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (history_id, hold.work_item_id, before.state.value, before.subject_revision, encoded),
        )
    return None


def set_item_state(
    connection: sqlite3.Connection,
    current: stored_state.StoredWorkItem,
    before_state: work_models.WorkState,
    after_state: stored_state.StoredWorkItemState,
    revision: int,
    now: datetime,
    outcome_evidence: str | None = None,
    *,
    unblocker: work_models.Unblocker | None,
) -> DecisionFailure[StaleWriteCode] | None:
    """Move one item between states, writing exactly the unblocker its held after-state requires."""

    item_id = current.item_id
    if stored_state.live_work_state(current.state) != before_state:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "The targeted item mutation is stale.", None)
    if (after_state in _HELD_STATES) != (unblocker is not None):
        raise StorageError(
            StorageErrorCode.INVARIANT_VIOLATION,
            f"Item '{item_id}' must record an unblocker exactly while it is paused or blocked.",
        )
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
                SET state = ?, outcome_evidence = ?, next_action = ?, subject_revision = ?, updated_at = ?,
                    queue_position = ?
                WHERE item_id = ? AND state = ? AND subject_revision = ?
                """,
                (
                    after_state.value,
                    outcome_evidence,
                    None if terminal else current.next_action,
                    revision,
                    now.isoformat(),
                    None if terminal else current.queue_position,
                    item_id,
                    current.state.value,
                    current.subject_revision,
                ),
            ),
            "The targeted item mutation is stale.",
        )
    ) is not None:
        return failure
    if unblocker is not None or current.state in _HELD_STATES:
        write_item_unblocker(connection, item_id, unblocker)
    move_item_state_count(connection, current.state, after_state)
    if terminal and current.queue_position is not None:
        return compact_queue(connection, current.queue_position)
    return None


def set_attempt_state(
    connection: sqlite3.Connection,
    current: stored_state.StoredAttempt,
    before_state: work_models.AttemptState,
    after_state: work_models.AttemptState,
    revision: int,
    now: datetime,
    *,
    revised_brief: decision_models.RevisedAttemptBrief | None = None,
    result_artifact_ref_id: ArtifactRefId | None = None,
    candidate_revision: str | None = None,
    candidate_recorded_at: datetime | None = None,
) -> DecisionFailure[StaleWriteCode] | None:
    attempt_id = current.attempt_id
    match after_state:
        case work_models.AttemptState.REVIEW:
            stored_candidate = candidate_revision
            stored_candidate_at = None if candidate_recorded_at is None else candidate_recorded_at.isoformat()
        case work_models.AttemptState.ACTIVE | work_models.AttemptState.PAUSED | work_models.AttemptState.BLOCKED:
            stored_candidate = None
            stored_candidate_at = None
        case work_models.AttemptState.DONE:
            stored_candidate = current.candidate_revision
            stored_candidate_at = (
                None if current.candidate_recorded_at is None else current.candidate_recorded_at.isoformat()
            )
        case _ as unreachable:
            assert_never(unreachable)
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
    current: stored_state.StoredAttempt,
    change: decision_models.RebindAttemptChange,
    revision: int,
    now: datetime,
) -> DecisionFailure[StaleWriteCode] | None:
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
                change.work_item_id,
                change.attempt_state.value,
                current.subject_revision,
                current.accepted_scope_revision,
                current.accepted_scope_digest,
            ),
        ),
        "The targeted attempt rebind is stale.",
    )


def replace_dependencies(
    connection: sqlite3.Connection, item_id: WorkItemId, dependencies: tuple[work_models.Dependency, ...]
) -> None:
    connection.execute("DELETE FROM item_dependencies WHERE item_id = ?", (item_id,))
    connection.executemany(
        "INSERT INTO item_dependencies (item_id, dependency_id, position, checkpoint_id) VALUES (?, ?, ?, ?)",
        tuple(
            (
                item_id,
                work_models.dependency_item(dependency),
                position,
                None if isinstance(dependency, str) else dependency.checkpoint_id,
            )
            for position, dependency in enumerate(dependencies)
        ),
    )


def insert_definition_revision(
    connection: sqlite3.Connection,
    current_item: stored_state.StoredWorkItem,
    current: stored_state.ItemDefinitionRevision,
    revision: stored_state.ItemDefinitionRevision,
) -> (
    DecisionFailure[Literal[DecisionFailureCode.ACTION_NOT_AVAILABLE, DecisionFailureCode.ITEM_DEFINITION_STALE]] | None
):
    if revision.revision != current.revision + 1 or revision.before_digest != current.digest:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_STALE,
            "The current definition changed before persistence.",
            None,
        )
    append_definition_revision(connection, revision)
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
    current_item: stored_state.StoredWorkItem,
    definition: stored_state.ItemDefinitionRevision,
    change: decision_models.ActivationChange,
    revision: int,
    now: datetime,
) -> (
    DecisionFailure[Literal[DecisionFailureCode.ACTION_NOT_AVAILABLE, DecisionFailureCode.ITEM_DEFINITION_INVALID]]
    | None
):
    if current_item.item_id != change.work_item_id or definition.item_id != change.work_item_id:
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
                change.work_item_id,
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
