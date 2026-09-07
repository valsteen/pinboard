"""Compose public SQLite store operations and own runtime transaction effects.

Each runtime write scope visibly opens one verified connection, begins one
transaction, rolls back an expected ``DecisionFailure`` or any exception,
commits successful work, and closes the connection. The scope supplies that
existing connection to thematic effects, which never end its transaction.
Snapshots own only their read connection. This module never obtains time,
reads artifact bytes directly, or invokes callbacks.
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, Self, assert_never

from pinboard.adapters.files.errors import ArtifactError, ArtifactErrorCode
from pinboard.adapters.sqlite import state as sqlite_state
from pinboard.adapters.sqlite.artifacts import (
    accept_artifact_reference as write_artifact_reference,
)
from pinboard.adapters.sqlite.artifacts import (
    accept_checkpoint_artifact,
)
from pinboard.adapters.sqlite.authority import (
    consume_preparation_authority,
    fence_attempt_authority,
    write_attempt_authority,
    write_preparation_authority,
)
from pinboard.adapters.sqlite.database import (
    open_database,
    read_operation,
    require_one_changed_row,
    translate_database_error,
)
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import (
    insert_attempt,
    insert_definition_revision,
    rebind_attempt,
    replace_dependencies,
    set_attempt_state,
    set_item_state,
)
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.proposals import accept_proposal, create_proposal, set_proposal_disposition
from pinboard.application import stored_state
from pinboard.application.artifacts import ArtifactRef
from pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    CheckpointAcceptanceMutation,
    MutationReceipt,
    PreparationAuthorityMutation,
    ProposalCreationMutation,
    StoredStateMutation,
    TransitionMutation,
)
from pinboard.application.mutations import stored_transition_receipt
from pinboard.application.ports import ArtifactReferenceAcceptance
from pinboard.domain import decision_models, work_models
from pinboard.domain.definition_decisions import DefinitionRevisionDecision
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import ItemId


def _translate_artifact_verification_error(error: ArtifactError) -> StorageError:
    match error.code:
        case ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION:
            code = StorageErrorCode.INVARIANT_VIOLATION
        case ArtifactErrorCode.STORAGE_IO_ERROR:
            code = StorageErrorCode.IO_ERROR
        case _ as unreachable:
            assert_never(unreachable)
    return StorageError(code, str(error), retryable=False)


def _persist_definition_revision(
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkState,
    decision: DefinitionRevisionDecision,
    project_revision: int,
) -> DecisionFailure | None:
    stored = stored_state.ItemDefinitionRevision(
        decision.item,
        decision.revision,
        decision.after_digest,
        decision.definition,
        decision.reason,
        decision.source_task,
        decision.before_digest,
        decision.after_digest,
        project_revision,
        decision.decided_at,
    )
    if (failure := insert_definition_revision(connection, state, stored)) is not None:
        return failure
    replace_dependencies(connection, decision.item, decision.definition.dependencies)
    return None


def _persist_transition(  # noqa: C901, PLR0912, PLR0915
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkState,
    mutation: TransitionMutation,
) -> DecisionFailure | None:
    change = mutation.decision.change
    revision = mutation.receipt.project_revision
    now = mutation.decision.receipt.decided_at
    match change:
        case decision_models.ItemStateChange(item=item, before=before, after=after):
            if (
                failure := set_item_state(
                    connection, state, item, before, stored_state.stored_live_work_state(after), revision, now
                )
            ) is not None:
                return failure
        case decision_models.ActivationChange(item=item, item_before=before):
            if (
                failure := set_item_state(
                    connection, state, item, before, stored_state.StoredWorkItemState.ACTIVE, revision, now
                )
            ) is not None:
                return failure
            preparation = mutation.decision.action.capability.preparation_authority
            if preparation is None:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    "Activation requires exact preparation authority.",
                    None,
                )
            if (failure := consume_preparation_authority(connection, preparation, now)) is not None:
                return failure
            if (failure := insert_attempt(connection, state, change, revision, now)) is not None:
                return failure
        case decision_models.AttemptStateChange(
            item=item,
            item_before=item_before,
            item_after=item_after,
            attempt=attempt,
            attempt_before=attempt_before,
            attempt_after=attempt_after,
        ):
            if (
                failure := set_item_state(
                    connection,
                    state,
                    item,
                    item_before,
                    stored_state.stored_live_work_state(item_after),
                    revision,
                    now,
                )
            ) is not None:
                return failure
            if (
                failure := set_attempt_state(connection, state, attempt, attempt_before, attempt_after, revision, now)
            ) is not None:
                return failure
        case decision_models.BlockAttemptChange(
            item=item,
            item_before=item_before,
            attempt=attempt,
            attempt_before=attempt_before,
            dependencies_after=dependencies,
        ):
            if (
                failure := set_item_state(
                    connection,
                    state,
                    item,
                    item_before,
                    stored_state.StoredWorkItemState.BLOCKED,
                    revision,
                    now,
                )
            ) is not None:
                return failure
            if (
                failure := set_attempt_state(
                    connection,
                    state,
                    attempt,
                    attempt_before,
                    work_models.AttemptState.BLOCKED,
                    revision,
                    now,
                )
            ) is not None:
                return failure
            replace_dependencies(connection, item, dependencies)
        case decision_models.BlockItemChange(item=item, item_before=item_before, dependencies_after=dependencies):
            if (
                failure := set_item_state(
                    connection,
                    state,
                    item,
                    item_before,
                    stored_state.StoredWorkItemState.BLOCKED,
                    revision,
                    now,
                )
            ) is not None:
                return failure
            replace_dependencies(connection, item, dependencies)
        case decision_models.RebindAttemptChange(authority_change=authority):
            if (failure := rebind_attempt(connection, state, change, revision, now)) is not None:
                return failure
            if (failure := fence_attempt_authority(connection, authority, now)) is not None:
                return failure
        case decision_models.ResumeAttemptChange(
            item=item,
            item_before=item_before,
            attempt=attempt,
            attempt_before=attempt_before,
            revised_brief=revised_brief,
        ):
            if (
                failure := set_item_state(
                    connection,
                    state,
                    item,
                    item_before,
                    stored_state.StoredWorkItemState.ACTIVE,
                    revision,
                    now,
                )
            ) is not None:
                return failure
            if (
                failure := set_attempt_state(
                    connection,
                    state,
                    attempt,
                    attempt_before,
                    work_models.AttemptState.ACTIVE,
                    revision,
                    now,
                    revised_brief=revised_brief,
                )
            ) is not None:
                return failure
        case decision_models.ReviewSubmissionChange(
            item=item,
            attempt=attempt,
            protected_candidate_after=candidate,
            candidate_observed_at=observed_at,
        ):
            if (
                failure := set_item_state(
                    connection,
                    state,
                    item,
                    work_models.WorkState.ACTIVE,
                    stored_state.StoredWorkItemState.REVIEW,
                    revision,
                    now,
                )
            ) is not None:
                return failure
            if (
                failure := set_attempt_state(
                    connection,
                    state,
                    attempt,
                    work_models.AttemptState.ACTIVE,
                    work_models.AttemptState.REVIEW,
                    revision,
                    now,
                    candidate_revision=str(candidate),
                    candidate_recorded_at=observed_at,
                )
            ) is not None:
                return failure
        case (
            decision_models.ReviewAcceptanceChange(item=item, attempt=attempt, authority_change=authority)
            | decision_models.ReviewReturnChange(item=item, attempt=attempt, authority_change=authority)
        ):
            if (
                failure := set_item_state(
                    connection,
                    state,
                    item,
                    work_models.WorkState.REVIEW,
                    stored_state.StoredWorkItemState.ACTIVE,
                    revision,
                    now,
                )
            ) is not None:
                return failure
            if (
                failure := set_attempt_state(
                    connection,
                    state,
                    attempt,
                    work_models.AttemptState.REVIEW,
                    work_models.AttemptState.ACTIVE,
                    revision,
                    now,
                )
            ) is not None:
                return failure
            if (failure := fence_attempt_authority(connection, authority, now)) is not None:
                return failure
        case (
            decision_models.CompletionChange(
                item=item,
                item_before=item_before,
                attempt=attempt,
                attempt_before=attempt_before,
                evidence=evidence,
                authority_change=authority,
            )
            | decision_models.AttemptClosureChange(
                item=item,
                item_before=item_before,
                evidence=evidence,
                attempt=attempt,
                attempt_before=attempt_before,
                authority_change=authority,
            )
        ) as terminal_change:
            match terminal_change:
                case decision_models.CompletionChange():
                    terminal_item_state = stored_state.StoredWorkItemState.DONE
                case decision_models.AttemptClosureChange(terminal_state=terminal_state):
                    terminal_item_state = stored_state.stored_close_outcome(terminal_state)
                case _ as unreachable:
                    assert_never(unreachable)
            if (
                failure := set_item_state(
                    connection,
                    state,
                    item,
                    item_before,
                    terminal_item_state,
                    revision,
                    now,
                    evidence,
                )
            ) is not None:
                return failure
            if (
                failure := set_attempt_state(
                    connection,
                    state,
                    attempt,
                    attempt_before,
                    work_models.AttemptState.DONE,
                    revision,
                    now,
                )
            ) is not None:
                return failure
            if authority is not None and (failure := fence_attempt_authority(connection, authority, now)) is not None:
                return failure
        case decision_models.ItemClosureChange(
            item=item,
            item_before=item_before,
            terminal_state=terminal_state,
            evidence=evidence,
        ):
            if (
                failure := set_item_state(
                    connection,
                    state,
                    item,
                    item_before,
                    stored_state.stored_close_outcome(terminal_state),
                    revision,
                    now,
                    evidence,
                )
            ) is not None:
                return failure
        case decision_models.AcceptedProposalChange():
            if (failure := accept_proposal(connection, state, change, revision, now)) is not None:
                return failure
        case DefinitionRevisionDecision():
            if (failure := _persist_definition_revision(connection, state, change, revision)) is not None:
                return failure
        case decision_models.MergedProposalChange(proposal=proposal, target_item=target, disposed_at=disposed_at):
            if (
                failure := set_item_state(
                    connection,
                    state,
                    ItemId(proposal),
                    work_models.WorkState.INTAKE,
                    stored_state.StoredWorkItemState.SUPERSEDED,
                    revision,
                    now,
                    f"Merged into {target}.",
                )
            ) is not None:
                return failure
            if (
                failure := set_proposal_disposition(
                    connection,
                    proposal,
                    work_models.MergedProposalDisposition(target, disposed_at),
                    revision,
                )
            ) is not None:
                return failure
        case decision_models.ReturnedProposalChange(proposal=proposal, reason=reason, disposed_at=disposed_at):
            if (
                failure := set_proposal_disposition(
                    connection,
                    proposal,
                    work_models.ReturnedProposalDisposition(reason, disposed_at),
                    revision,
                )
            ) is not None:
                return failure
        case decision_models.RejectedProposalChange(proposal=proposal, reason=reason, disposed_at=disposed_at):
            if (
                failure := set_item_state(
                    connection,
                    state,
                    ItemId(proposal),
                    work_models.WorkState.INTAKE,
                    stored_state.StoredWorkItemState.DROPPED,
                    revision,
                    now,
                    reason,
                )
            ) is not None:
                return failure
            if (
                failure := set_proposal_disposition(
                    connection,
                    proposal,
                    work_models.RejectedProposalDisposition(reason, disposed_at),
                    revision,
                )
            ) is not None:
                return failure
        case _ as unreachable:
            assert_never(unreachable)
    return None


def _persist_checkpoint_acceptance(
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkState,
    mutation: CheckpointAcceptanceMutation,
) -> DecisionFailure | None:
    change = mutation.decision.change
    artifacts = mutation.checkpoint_artifacts
    revision = mutation.receipt.project_revision
    now = mutation.decision.receipt.decided_at
    accept_checkpoint_artifact(
        connection,
        state,
        artifacts.result,
        artifacts.result_id,
        revision,
        now,
    )
    accept_checkpoint_artifact(
        connection,
        state,
        artifacts.review,
        artifacts.review_id,
        revision,
        now,
    )
    if (
        failure := set_item_state(
            connection,
            state,
            change.item,
            work_models.WorkState.REVIEW,
            stored_state.StoredWorkItemState.PAUSED,
            revision,
            now,
        )
    ) is not None:
        return failure
    if (
        failure := set_attempt_state(
            connection,
            state,
            change.attempt,
            work_models.AttemptState.REVIEW,
            work_models.AttemptState.PAUSED,
            revision,
            now,
            result_artifact_ref_id=artifacts.result_id,
        )
    ) is not None:
        return failure
    if (failure := fence_attempt_authority(connection, change.authority_change, now)) is not None:
        return failure
    return None


def _persist_state_change(
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkState,
    mutation: StoredStateMutation,
) -> DecisionFailure | None:
    match mutation:
        case TransitionMutation():
            return _persist_transition(connection, state, mutation)
        case CheckpointAcceptanceMutation():
            return _persist_checkpoint_acceptance(connection, state, mutation)
        case ProposalCreationMutation():
            return create_proposal(connection, state, mutation)
        case AttemptAuthorityMutation(decision=decision):
            return write_attempt_authority(connection, decision)
        case PreparationAuthorityMutation(decision=decision):
            return write_preparation_authority(connection, decision)
        case _ as unreachable:
            assert_never(unreachable)


def _persist(
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkState,
    mutation: StoredStateMutation,
) -> DecisionFailure | None:
    """Persist one targeted accepted mutation without rebuilding unrelated relations."""

    receipt = stored_transition_receipt(mutation)
    expected_history_id = 1 + max((int(value.history_id) for value in state.transition_receipts), default=0)
    if (
        int(receipt.history_id) != expected_history_id
        or receipt.project_revision != state.lifecycle.project.revision + 1
    ):
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "The targeted mutation receipt does not identify the next project revision exactly.",
            None,
        )
    connection.execute("PRAGMA defer_foreign_keys = ON")
    if (failure := _persist_state_change(connection, state, mutation)) is not None:
        return failure
    if (
        failure := require_one_changed_row(
            connection.execute(
                """
                UPDATE project_meta
                SET revision = ?, updated_at = ?
                WHERE singleton = 1 AND revision = ?
                """,
                (
                    receipt.project_revision,
                    receipt.committed_at.isoformat(),
                    state.lifecycle.project.revision,
                ),
            ),
            "The project revision changed before targeted persistence.",
        )
    ) is not None:
        return failure
    sqlite_state.append_history(connection, (receipt,))
    return None


class _SQLiteWorkTransaction:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._connection: sqlite3.Connection | None = None
        self._rejected = False

    def __enter__(self) -> Self:
        connection = open_database(self._path, OpenMode.READ_WRITE)
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as error:
            connection.close()
            raise translate_database_error(error) from error
        self._connection = connection
        return self

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del error_type, traceback
        connection = self._active_connection()
        try:
            if error is not None:
                connection.rollback()
                if isinstance(error, sqlite3.Error):
                    raise translate_database_error(error) from error
                return False
            if not self._rejected:
                try:
                    connection.commit()
                except sqlite3.Error as commit_error:
                    connection.rollback()
                    raise translate_database_error(commit_error) from commit_error
            return False
        finally:
            connection.close()
            self._connection = None

    def _active_connection(self) -> sqlite3.Connection:
        assert self._connection is not None
        return self._connection

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the connection while this runtime transaction is active."""

        return self._active_connection()

    def _select[Value](self, result: DecisionResult[Value]) -> DecisionResult[Value]:
        if isinstance(result, DecisionFailure):
            self._active_connection().rollback()
            self._rejected = True
        return result

    def snapshot(self) -> stored_state.StoredWorkState:
        return sqlite_state.read_state(self.connection)

    def commit(self, mutation: StoredStateMutation) -> DecisionResult[MutationReceipt]:
        connection = self.connection
        current = sqlite_state.read_state(connection)
        if (failure := _persist(connection, current, mutation)) is not None:
            return self._select(failure)
        sqlite_state.read_state(connection)
        return self._select(mutation.receipt)


class SQLiteWorkStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def snapshot(self) -> stored_state.StoredWorkState:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                return sqlite_state.read_state(connection)
        finally:
            connection.close()

    def write(self) -> _SQLiteWorkTransaction:
        return _SQLiteWorkTransaction(self._path)

    def accept_artifact_reference(
        self,
        work_root: Path,
        published: ArtifactRef,
        accepted_at: datetime,
    ) -> DecisionResult[ArtifactReferenceAcceptance]:
        with _SQLiteWorkTransaction(self._path) as transaction:
            connection = transaction.connection
            try:
                result = write_artifact_reference(
                    connection,
                    sqlite_state.read_state(connection),
                    work_root,
                    published,
                    accepted_at,
                )
            except ArtifactError as error:
                raise _translate_artifact_verification_error(error) from error
            if isinstance(result, DecisionFailure):
                return transaction._select(result)
            sqlite_state.read_state(connection)
            return transaction._select(result)
