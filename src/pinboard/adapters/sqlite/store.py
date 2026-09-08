"""Compose public SQLite store operations and own runtime transaction effects.

Each runtime write scope visibly opens one verified connection, begins one
transaction, rolls back an expected ``DecisionFailure`` or any exception,
commits successful work, and closes the connection. The scope supplies that
existing connection to thematic effects, which never end its transaction.
Snapshots own only their read connection. This module never obtains time,
reads artifact bytes directly, or invokes callbacks.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, Self, assert_never

import msgspec

from pinboard.adapters.files.errors import ArtifactError, ArtifactErrorCode
from pinboard.adapters.sqlite import state as sqlite_state
from pinboard.adapters.sqlite.artifacts import (
    accept_artifact_reference as write_artifact_reference,
)
from pinboard.adapters.sqlite.artifacts import (
    accept_checkpoint_artifact,
    read_artifact_reference,
    read_artifact_reference_by_id,
    read_brief_artifact_reference,
)
from pinboard.adapters.sqlite.authority import (
    consume_preparation_authority,
    fence_attempt_authority,
    read_attempt_authority_status,
    read_preparation_authority_status,
    write_attempt_authority,
    write_preparation_authority,
)
from pinboard.adapters.sqlite.database import (
    decode_row,
    open_database,
    read_operation,
    require_one_changed_row,
    translate_database_error,
    verify_database_integrity,
)
from pinboard.adapters.sqlite.decision_reads import read_current_snapshot, read_selected_decision_facts
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import (
    NonterminalAttemptContextSelection,
    TerminalAttemptContextSelection,
    decode_definition_revision,
    insert_attempt,
    insert_definition_revision,
    read_attempt_context,
    read_item_status,
    read_parallel_preview_lifecycle,
    rebind_attempt,
    replace_dependencies,
    set_attempt_state,
    set_item_state,
)
from pinboard.adapters.sqlite.lifecycle import (
    read_item_definition as select_item_definition,
)
from pinboard.adapters.sqlite.lifecycle import (
    read_item_definition_history as select_item_definition_history,
)
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.proposals import accept_proposal, create_proposal, read_proposal, set_proposal_disposition
from pinboard.application import queries, query_models, stored_state
from pinboard.application.artifacts import ArtifactRef, EvidenceArtifactRef, ResultArtifactRef
from pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    CheckpointAcceptanceMutation,
    CheckpointMutationAllocation,
    CommittedEffect,
    MutationAllocation,
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
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, HistoryId, ItemId, ProposalId
from pinboard.domain.ledger import LedgerSnapshot


class _ItemIdRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: ItemId


class _AttemptIdRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId


class _DependencyIdRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    dependency_id: ItemId


class _DependencyViewRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    dependency_id: ItemId
    queue_position: int | None


class _MutationAllocationRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: int
    next_history_id: int


class _PersistedAllocationRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: int
    history_id: int


class _ArtifactIdAllocationRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    next_artifact_ref_id: int


class _LiveItemCountRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    live_item_count: int


class _GenerationRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    generation_high_water: int


class _ProjectRevisionRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: int


class _StateCountRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    state: str
    item_count: int


def _translate_artifact_verification_error(error: ArtifactError) -> StorageError:
    match error.code:
        case ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION:
            code = StorageErrorCode.INVARIANT_VIOLATION
        case ArtifactErrorCode.STORAGE_IO_ERROR:
            code = StorageErrorCode.IO_ERROR
        case _ as unreachable:
            assert_never(unreachable)
    return StorageError(code, str(error), retryable=False)


def _read_overview_proposals(
    connection: sqlite3.Connection, snapshot: LedgerSnapshot
) -> tuple[stored_state.StoredProposal, ...]:
    proposal_ids = tuple(
        dict.fromkeys(ProposalId(item_id) for item in snapshot.items for item_id in (item.item, *item.depends_on))
    )
    return tuple(
        proposal for proposal_id in proposal_ids if (proposal := read_proposal(connection, proposal_id)) is not None
    )


@dataclass(frozen=True, slots=True)
class _PersistenceFacts:
    items: dict[ItemId, stored_state.StoredWorkItem]
    attempts: dict[AttemptId, stored_state.StoredAttempt]
    definitions: dict[ItemId, stored_state.ItemDefinitionRevision]

    def item(self, item_id: ItemId) -> stored_state.StoredWorkItem:
        try:
            return self.items[item_id]
        except KeyError:
            raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "The targeted mutation item is missing.") from None

    def attempt(self, attempt_id: AttemptId) -> stored_state.StoredAttempt:
        try:
            return self.attempts[attempt_id]
        except KeyError:
            raise StorageError(
                StorageErrorCode.INVARIANT_VIOLATION, "The targeted mutation attempt is missing."
            ) from None

    def definition(self, item_id: ItemId) -> stored_state.ItemDefinitionRevision:
        try:
            return self.definitions[item_id]
        except KeyError:
            raise StorageError(
                StorageErrorCode.INVARIANT_VIOLATION, "The targeted mutation definition is missing."
            ) from None


def _mutation_subjects(  # noqa: PLR0912
    mutation: StoredStateMutation,
) -> tuple[tuple[ItemId, ...], tuple[AttemptId, ...]]:
    match mutation:
        case TransitionMutation(decision=decision):
            match decision.change:
                case decision_models.ItemStateChange(item=item) | decision_models.BlockItemChange(item=item):
                    return (item,), ()
                case decision_models.ActivationChange(item=item):
                    return (item,), ()
                case (
                    decision_models.AttemptStateChange(item=item, attempt=attempt)
                    | decision_models.BlockAttemptChange(item=item, attempt=attempt)
                    | decision_models.ResumeAttemptChange(item=item, attempt=attempt)
                    | decision_models.ReviewSubmissionChange(item=item, attempt=attempt)
                    | decision_models.ReviewAcceptanceChange(item=item, attempt=attempt)
                    | decision_models.ReviewReturnChange(item=item, attempt=attempt)
                    | decision_models.CompletionChange(item=item, attempt=attempt)
                    | decision_models.AttemptClosureChange(item=item, attempt=attempt)
                ):
                    return (item,), (attempt,)
                case decision_models.RebindAttemptChange(item=item, attempt=attempt):
                    return (item,), (attempt,)
                case decision_models.ItemClosureChange(item=item) | DefinitionRevisionDecision(item=item):
                    return (item,), ()
                case decision_models.AcceptedProposalChange(accepted_item=accepted):
                    return (accepted.item,), ()
                case (
                    decision_models.MergedProposalChange(proposal=proposal)
                    | decision_models.RejectedProposalChange(proposal=proposal)
                ):
                    return (ItemId(proposal),), ()
                case decision_models.ReturnedProposalChange(proposal=proposal):
                    return (ItemId(proposal),), ()
                case _ as unreachable:
                    assert_never(unreachable)
        case CheckpointAcceptanceMutation(decision=decision):
            return (decision.change.item,), (decision.change.attempt,)
        case ProposalCreationMutation() | AttemptAuthorityMutation() | PreparationAuthorityMutation():
            return (), ()
        case _ as unreachable:
            assert_never(unreachable)


def _read_persistence_facts(connection: sqlite3.Connection, mutation: StoredStateMutation) -> _PersistenceFacts:
    item_ids, attempt_ids = _mutation_subjects(mutation)
    items: dict[ItemId, stored_state.StoredWorkItem] = {}
    for item_id in item_ids:
        row = connection.execute(
            """
            SELECT item_id, state, timing, source, outcome_evidence, next_action, notes,
                   subject_revision, recorded_at, updated_at, queue_position
            FROM work_items WHERE item_id = ?
            """,
            (item_id,),
        ).fetchone()
        if row is not None:
            items[item_id] = decode_row(row, stored_state.StoredWorkItem)
    attempts: dict[AttemptId, stored_state.StoredAttempt] = {}
    for attempt_id in attempt_ids:
        row = connection.execute(
            """
            SELECT attempt_id, item_id, state, branch, base_revision, provenance,
                   brief_artifact_ref_id, result_artifact_ref_id, candidate_revision,
                   candidate_recorded_at, accepted_scope_revision, accepted_scope_digest,
                   subject_revision, recorded_at, updated_at
            FROM attempts WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if row is not None:
            attempts[attempt_id] = decode_row(row, stored_state.StoredAttempt)
    definitions: dict[ItemId, stored_state.ItemDefinitionRevision] = {}
    for item_id in item_ids:
        row = connection.execute(
            """
            SELECT item_id, definition_revision AS revision, definition_digest AS digest,
                   definition_json, reason, source_task_id, before_digest, after_digest,
                   accepted_project_revision, accepted_at
            FROM work_item_definition_revisions
            WHERE item_id = ? ORDER BY definition_revision DESC LIMIT 1
            """,
            (item_id,),
        ).fetchone()
        if row is not None:
            definitions[item_id] = decode_definition_revision(row)
    return _PersistenceFacts(items, attempts, definitions)


def _committed_effect_ids(
    connection: sqlite3.Connection, mutation: StoredStateMutation
) -> tuple[tuple[ItemId, ...], tuple[AttemptId, ...]]:
    item_ids, attempt_ids = _mutation_subjects(mutation)
    affected_items = list(item_ids)
    match mutation:
        case ProposalCreationMutation(decision=decision):
            affected_items.append(decision.intake_item.item_id)
            if decision.prerequisite_change is not None:
                affected_items.append(decision.prerequisite_change.item_id)
            affected_items.extend(
                decode_row(row, _ItemIdRow).item_id
                for row in connection.execute(
                    "SELECT item_id FROM work_items WHERE queue_position >= ? ORDER BY queue_position",
                    (decision.intake_item.position,),
                ).fetchall()
            )
        case PreparationAuthorityMutation(decision=decision):
            affected_items.append(decision.proposed_replacement.item)
        case AttemptAuthorityMutation():
            pass
        case TransitionMutation(decision=decision) | CheckpointAcceptanceMutation(decision=decision):
            match decision.change:
                case decision_models.ActivationChange(attempt=attempt):
                    attempt_ids = (*attempt_ids, attempt)
                case (
                    decision_models.CompletionChange(item=item)
                    | decision_models.AttemptClosureChange(item=item)
                    | decision_models.ItemClosureChange(item=item)
                    | decision_models.MergedProposalChange(proposal=item)
                    | decision_models.RejectedProposalChange(proposal=item)
                ):
                    selected_item = ItemId(item)
                    selected = connection.execute(
                        "SELECT queue_position FROM work_items WHERE item_id = ?", (selected_item,)
                    ).fetchone()
                    if selected is not None and selected["queue_position"] is not None:
                        affected_items.extend(
                            decode_row(row, _ItemIdRow).item_id
                            for row in connection.execute(
                                "SELECT item_id FROM work_items WHERE queue_position > ? ORDER BY queue_position",
                                (selected["queue_position"],),
                            ).fetchall()
                        )
                case (
                    decision_models.ItemStateChange()
                    | decision_models.AttemptStateChange()
                    | decision_models.BlockAttemptChange()
                    | decision_models.BlockItemChange()
                    | decision_models.RebindAttemptChange()
                    | decision_models.ResumeAttemptChange()
                    | decision_models.ReviewSubmissionChange()
                    | decision_models.ReviewReturnChange()
                    | decision_models.ReviewAcceptanceChange()
                    | decision_models.AcceptedProposalChange()
                    | decision_models.ReturnedProposalChange()
                    | DefinitionRevisionDecision()
                    | decision_models.CheckpointAcceptanceChange()
                ):
                    pass
                case _ as unreachable:
                    assert_never(unreachable)
        case _ as unreachable:
            assert_never(unreachable)
    direct_items = tuple(dict.fromkeys(affected_items))
    for item_id in direct_items:
        affected_items.extend(
            decode_row(row, _ItemIdRow).item_id
            for row in connection.execute(
                """
                SELECT owner.item_id
                FROM item_dependencies AS dependency
                JOIN work_items AS owner ON owner.item_id = dependency.item_id
                WHERE dependency.dependency_id = ?
                  AND owner.queue_position IS NOT NULL
                ORDER BY owner.queue_position, owner.item_id
                """,
                (item_id,),
            ).fetchall()
        )
    return tuple(dict.fromkeys(affected_items)), tuple(dict.fromkeys(attempt_ids))


def _persist_definition_revision(
    connection: sqlite3.Connection,
    facts: _PersistenceFacts,
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
    if (
        failure := insert_definition_revision(
            connection, facts.item(decision.item), facts.definition(decision.item), stored
        )
    ) is not None:
        return failure
    replace_dependencies(connection, decision.item, decision.definition.dependencies)
    return None


def _persist_transition(  # noqa: C901, PLR0912, PLR0915
    connection: sqlite3.Connection,
    facts: _PersistenceFacts,
    mutation: TransitionMutation,
) -> DecisionFailure | None:
    change = mutation.decision.change
    revision = mutation.receipt.project_revision
    now = mutation.decision.receipt.decided_at
    match change:
        case decision_models.ItemStateChange(item=item, before=before, after=after):
            if (
                failure := set_item_state(
                    connection,
                    facts.item(item),
                    before,
                    stored_state.stored_live_work_state(after),
                    revision,
                    now,
                )
            ) is not None:
                return failure
        case decision_models.ActivationChange(item=item, item_before=before):
            if (
                failure := set_item_state(
                    connection,
                    facts.item(item),
                    before,
                    stored_state.StoredWorkItemState.ACTIVE,
                    revision,
                    now,
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
            if (
                failure := insert_attempt(connection, facts.item(item), facts.definition(item), change, revision, now)
            ) is not None:
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
                    facts.item(item),
                    item_before,
                    stored_state.stored_live_work_state(item_after),
                    revision,
                    now,
                )
            ) is not None:
                return failure
            if (
                failure := set_attempt_state(
                    connection, facts.attempt(attempt), attempt_before, attempt_after, revision, now
                )
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
                    facts.item(item),
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
                    facts.attempt(attempt),
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
                    facts.item(item),
                    item_before,
                    stored_state.StoredWorkItemState.BLOCKED,
                    revision,
                    now,
                )
            ) is not None:
                return failure
            replace_dependencies(connection, item, dependencies)
        case decision_models.RebindAttemptChange(authority_change=authority):
            if (
                failure := rebind_attempt(connection, facts.attempt(change.attempt), change, revision, now)
            ) is not None:
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
                    facts.item(item),
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
                    facts.attempt(attempt),
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
                    facts.item(item),
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
                    facts.attempt(attempt),
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
                    facts.item(item),
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
                    facts.attempt(attempt),
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
                    facts.item(item),
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
                    facts.attempt(attempt),
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
                    facts.item(item),
                    item_before,
                    stored_state.stored_close_outcome(terminal_state),
                    revision,
                    now,
                    evidence,
                )
            ) is not None:
                return failure
        case decision_models.AcceptedProposalChange():
            if (
                failure := accept_proposal(connection, facts.item(change.accepted_item.item), change, revision, now)
            ) is not None:
                return failure
        case DefinitionRevisionDecision():
            if (failure := _persist_definition_revision(connection, facts, change, revision)) is not None:
                return failure
        case decision_models.MergedProposalChange(proposal=proposal, target_item=target, disposed_at=disposed_at):
            if (
                failure := set_item_state(
                    connection,
                    facts.item(ItemId(proposal)),
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
                    facts.item(ItemId(proposal)),
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
    facts: _PersistenceFacts,
    mutation: CheckpointAcceptanceMutation,
) -> DecisionFailure | None:
    change = mutation.decision.change
    artifacts = mutation.checkpoint_artifacts
    revision = mutation.receipt.project_revision
    now = mutation.decision.receipt.decided_at
    accept_checkpoint_artifact(
        connection,
        artifacts.result,
        artifacts.result_id,
        revision,
        now,
    )
    accept_checkpoint_artifact(
        connection,
        artifacts.review,
        artifacts.review_id,
        revision,
        now,
    )
    if (
        failure := set_item_state(
            connection,
            facts.item(change.item),
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
            facts.attempt(change.attempt),
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
    facts: _PersistenceFacts,
    mutation: StoredStateMutation,
) -> DecisionFailure | None:
    match mutation:
        case TransitionMutation():
            return _persist_transition(connection, facts, mutation)
        case CheckpointAcceptanceMutation():
            return _persist_checkpoint_acceptance(connection, facts, mutation)
        case ProposalCreationMutation():
            return create_proposal(connection, mutation)
        case AttemptAuthorityMutation(decision=decision):
            return write_attempt_authority(connection, decision)
        case PreparationAuthorityMutation(decision=decision):
            return write_preparation_authority(connection, decision)
        case _ as unreachable:
            assert_never(unreachable)


def _persist(
    connection: sqlite3.Connection,
    mutation: StoredStateMutation,
) -> DecisionFailure | None:
    """Persist one targeted accepted mutation without rebuilding unrelated relations."""

    receipt = stored_transition_receipt(mutation)
    allocation = connection.execute(
        """
        SELECT project.revision,
               COALESCE((SELECT MAX(history_id) FROM transition_history), 0) AS history_id
        FROM project_meta AS project
        WHERE project.singleton = 1
        """
    ).fetchone()
    if allocation is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Project metadata is missing.")
    selected_allocation = decode_row(allocation, _PersistedAllocationRow)
    current_revision = selected_allocation.revision
    expected_history_id = selected_allocation.history_id + 1
    if int(receipt.history_id) != expected_history_id or receipt.project_revision != current_revision + 1:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "The targeted mutation receipt does not identify the next project revision exactly.",
            None,
        )
    facts = _read_persistence_facts(connection, mutation)
    connection.execute("PRAGMA defer_foreign_keys = ON")
    if (failure := _persist_state_change(connection, facts, mutation)) is not None:
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
                    current_revision,
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

    def read_decision_facts(self, scope: query_models.DecisionScope, now: datetime) -> query_models.DecisionFacts:
        return read_selected_decision_facts(self.connection, scope, now)

    def read_mutation_allocation(self) -> MutationAllocation:
        allocation = self.connection.execute(
            """
            SELECT project.revision,
                   COALESCE((SELECT MAX(history_id) FROM transition_history), 0) + 1 AS next_history_id
            FROM project_meta AS project
            WHERE project.singleton = 1
            """
        ).fetchone()
        if allocation is None:
            raise StorageError(StorageErrorCode.INVALID_STATE, "Project metadata is missing.")
        selected = decode_row(allocation, _MutationAllocationRow)
        return MutationAllocation(selected.revision, HistoryId(selected.next_history_id))

    def read_checkpoint_mutation_allocation(
        self, artifacts: tuple[ArtifactRef | ResultArtifactRef | EvidenceArtifactRef, ...]
    ) -> CheckpointMutationAllocation:
        allocation = self.read_mutation_allocation()
        artifact_allocation = self.connection.execute(
            "SELECT COALESCE(MAX(artifact_ref_id), 0) + 1 AS next_artifact_ref_id FROM artifact_refs"
        ).fetchone()
        if artifact_allocation is None:
            raise StorageError(StorageErrorCode.INVALID_STATE, "Artifact allocation is unavailable.")
        accepted: list[stored_state.ArtifactReference] = []
        for artifact in artifacts:
            row = self.connection.execute(
                """
                SELECT artifact_ref_id, artifact_key AS key, artifact_revision AS revision, kind,
                       relative_path AS selector, content_sha256, size_bytes, accepted_revision, created_at
                FROM artifact_refs
                WHERE kind = ? AND artifact_key = ? AND artifact_revision = ?
                """,
                (artifact.kind.value, artifact.key, artifact.revision),
            ).fetchone()
            if row is not None:
                accepted.append(decode_row(row, stored_state.ArtifactReference))
        selected_artifact_allocation = decode_row(artifact_allocation, _ArtifactIdAllocationRow)
        return CheckpointMutationAllocation(
            allocation.project_revision,
            allocation.next_history_id,
            ArtifactRefId(selected_artifact_allocation.next_artifact_ref_id),
            tuple(accepted),
        )

    def read_live_item_count(self) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(queue_position), 0) AS live_item_count FROM work_items"
        ).fetchone()
        if row is None:
            raise StorageError(StorageErrorCode.INVALID_STATE, "Live queue allocation is unavailable.")
        return decode_row(row, _LiveItemCountRow).live_item_count

    def read_attempt_authority_status(self, attempt_id: AttemptId) -> query_models.AttemptAuthorityStatus | None:
        return read_attempt_authority_status(self.connection, attempt_id)

    def read_preparation_authority_status(self, item_id: ItemId) -> query_models.PreparationAuthorityStatus | None:
        return read_preparation_authority_status(self.connection, item_id)

    def read_attempt_generation(self, attempt_id: AttemptId) -> int:
        row = self.connection.execute(
            "SELECT generation_high_water FROM attempt_lease_counters WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        return 0 if row is None else decode_row(row, _GenerationRow).generation_high_water

    def read_preparation_generation(self, item_id: ItemId) -> int:
        row = self.connection.execute(
            "SELECT generation_high_water FROM preparation_lease_counters WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        return 0 if row is None else decode_row(row, _GenerationRow).generation_high_water

    def commit(self, mutation: StoredStateMutation) -> DecisionResult[CommittedEffect]:
        item_ids, attempt_ids = _committed_effect_ids(self.connection, mutation)
        continuation_attempt_id = attempt_ids[0] if attempt_ids else None
        if continuation_attempt_id is None and mutation.receipt.transition.item is not None:
            row = self.connection.execute(
                "SELECT attempt_id FROM attempts WHERE item_id = ? AND state != 'done'",
                (mutation.receipt.transition.item,),
            ).fetchone()
            if row is not None:
                continuation_attempt_id = decode_row(row, _AttemptIdRow).attempt_id
        if (failure := _persist(self.connection, mutation)) is not None:
            return self._select(failure)
        if (
            continuation_attempt_id is None
            and isinstance(mutation, TransitionMutation)
            and isinstance(mutation.decision.change, decision_models.ActivationChange)
        ):
            continuation_attempt_id = mutation.decision.change.attempt
        return self._select(CommittedEffect(mutation.receipt, item_ids, attempt_ids, continuation_attempt_id))


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

    def validated_snapshot(self) -> stored_state.StoredWorkState:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                verify_database_integrity(connection)
                return sqlite_state.read_state(connection)
        finally:
            connection.close()

    def read_project_status(self) -> query_models.ProjectStatusFacts:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                project_row = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
                if project_row is None:
                    raise StorageError(StorageErrorCode.INVALID_STATE, "Project metadata is missing.")
                active_attempts = tuple(
                    sorted(
                        (
                            decode_row(row, _AttemptIdRow).attempt_id
                            for row in connection.execute(
                                """
                                SELECT attempt_id FROM attempts INDEXED BY one_live_attempt_per_item
                                WHERE state != 'done' AND state = 'active'
                                """
                            ).fetchall()
                        ),
                        key=str,
                    )
                )
                counts = tuple(
                    (selected.state, selected.item_count)
                    for row in connection.execute(
                        """
                        SELECT state, COUNT(*) AS item_count
                        FROM work_items
                        WHERE queue_position IS NOT NULL
                        GROUP BY state ORDER BY state
                        """
                    ).fetchall()
                    for selected in (decode_row(row, _StateCountRow),)
                )
                revision = decode_row(project_row, _ProjectRevisionRow).revision
                return query_models.ProjectStatusFacts(revision, active_attempts, counts)
        finally:
            connection.close()

    def read_artifact_reference(
        self, kind: work_models.ArtifactKind, key: str, revision: int
    ) -> stored_state.ArtifactReference | None:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                return read_artifact_reference(connection, kind, key, revision)
        finally:
            connection.close()

    def read_artifact_reference_by_id(self, artifact_ref_id: ArtifactRefId) -> stored_state.ArtifactReference | None:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                return read_artifact_reference_by_id(connection, artifact_ref_id)
        finally:
            connection.close()

    def _read_current_project_snapshot(self, now: datetime, *, include_proposals: bool) -> LedgerSnapshot:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                return read_current_snapshot(
                    connection,
                    now,
                    include_proposals=include_proposals,
                    include_action_authorities=True,
                )
        finally:
            connection.close()

    def read_current_action_snapshot(self, now: datetime) -> LedgerSnapshot:
        return self._read_current_project_snapshot(now, include_proposals=True)

    def read_current_parallel_snapshot(self, now: datetime) -> LedgerSnapshot:
        return self._read_current_project_snapshot(now, include_proposals=False)

    def read_decision_facts(self, scope: query_models.DecisionScope, now: datetime) -> query_models.DecisionFacts:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                return read_selected_decision_facts(connection, scope, now)
        finally:
            connection.close()

    def read_project_overview(self, now: datetime) -> query_models.ProjectOverviewFacts:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                snapshot = read_current_snapshot(
                    connection, now, include_proposals=False, include_action_authorities=False
                )
                return query_models.ProjectOverviewFacts(
                    snapshot,
                    _read_overview_proposals(connection, snapshot),
                    tuple(
                        status
                        for item in snapshot.items
                        if (status := read_preparation_authority_status(connection, item.item)) is not None
                    ),
                )
        finally:
            connection.close()

    def read_generated_view_facts(
        self,
        item_ids: tuple[ItemId, ...],
        attempt_ids: tuple[AttemptId, ...],
        history_ids: tuple[HistoryId, ...],
        now: datetime,
    ) -> query_models.GeneratedViewFacts:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                project_row = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
                if project_row is None:
                    raise StorageError(StorageErrorCode.INVALID_STATE, "Project metadata is missing.")
                project_revision = decode_row(project_row, _ProjectRevisionRow).revision
                items: list[query_models.ItemProjectionFacts] = []
                for item_id in item_ids:
                    item_row = connection.execute(
                        """
                        SELECT item_id, state, timing, source, outcome_evidence, next_action, notes,
                               subject_revision, recorded_at, updated_at, queue_position
                        FROM work_items WHERE item_id = ?
                        """,
                        (item_id,),
                    ).fetchone()
                    definition_row = connection.execute(
                        """
                        SELECT item_id, definition_revision AS revision, definition_digest AS digest,
                               definition_json, reason, source_task_id, before_digest, after_digest,
                               accepted_project_revision, accepted_at
                        FROM work_item_definition_revisions
                        WHERE item_id = ? ORDER BY definition_revision DESC LIMIT 1
                        """,
                        (item_id,),
                    ).fetchone()
                    if item_row is None or definition_row is None:
                        raise StorageError(StorageErrorCode.INVALID_STATE, "An affected item projection is missing.")
                    dependency_rows = tuple(
                        decode_row(row, _DependencyViewRow)
                        for row in connection.execute(
                            """
                            SELECT dependency.dependency_id, item.queue_position
                            FROM item_dependencies AS dependency
                            JOIN work_items AS item ON item.item_id = dependency.dependency_id
                            WHERE dependency.item_id = ? ORDER BY dependency.position
                            """,
                            (item_id,),
                        ).fetchall()
                    )
                    dependencies = tuple(value.dependency_id for value in dependency_rows)
                    item = decode_row(item_row, stored_state.StoredWorkItem)
                    definition = decode_definition_revision(definition_row)
                    live_state = stored_state.live_work_state(item.state)
                    projected: query_models.OverviewItem | None = None
                    if live_state is not None:
                        attempt_row = connection.execute(
                            "SELECT attempt_id FROM attempts WHERE item_id = ? AND state != 'done'",
                            (item_id,),
                        ).fetchone()
                        attempt_id = None if attempt_row is None else decode_row(attempt_row, _AttemptIdRow).attempt_id
                        proposal_ids = tuple(
                            dict.fromkeys((ProposalId(item_id), *(ProposalId(value) for value in dependencies)))
                        )
                        selected_proposals = tuple(
                            proposal
                            for proposal_id in proposal_ids
                            if (proposal := read_proposal(connection, proposal_id)) is not None
                        )
                        projected = queries.project_item_overview(
                            query_models.ItemOverviewFacts(
                                work_models.WorkItem(
                                    item.item_id,
                                    live_state,
                                    item.timing.value if item.timing is not None else None,
                                    dependencies,
                                    attempt_id,
                                    item.source,
                                    item.next_action,
                                    item.notes,
                                    item.queue_position,
                                    item.outcome_evidence,
                                ),
                                tuple(
                                    (value.dependency_id, value.queue_position is not None) for value in dependency_rows
                                ),
                                work_models.DefinitionAnchor(
                                    definition.item_id,
                                    definition.revision,
                                    definition.digest,
                                    definition.definition,
                                ),
                                selected_proposals,
                                read_preparation_authority_status(connection, item_id),
                            ),
                            now,
                        )
                    items.append(
                        query_models.ItemProjectionFacts(
                            item,
                            dependencies,
                            projected,
                            definition,
                        )
                    )
                attempts: list[query_models.AttemptProjectionFacts] = []
                for attempt_id in attempt_ids:
                    attempt_row = connection.execute(
                        """
                        SELECT attempt_id, item_id, state, branch, base_revision, provenance,
                               brief_artifact_ref_id, result_artifact_ref_id, candidate_revision,
                               candidate_recorded_at, accepted_scope_revision, accepted_scope_digest,
                               subject_revision, recorded_at, updated_at
                        FROM attempts WHERE attempt_id = ?
                        """,
                        (attempt_id,),
                    ).fetchone()
                    if attempt_row is None:
                        raise StorageError(StorageErrorCode.INVALID_STATE, "An affected attempt projection is missing.")
                    attempt = decode_row(attempt_row, stored_state.StoredAttempt)
                    attempts.append(
                        query_models.AttemptProjectionFacts(
                            attempt,
                            read_brief_artifact_reference(connection, attempt.brief_artifact_ref_id),
                        )
                    )
                receipts = tuple(
                    receipt
                    for history_id in history_ids
                    if (receipt := sqlite_state.read_history_receipt(connection, history_id)) is not None
                )
                if len(receipts) != len(history_ids):
                    raise StorageError(StorageErrorCode.INVALID_STATE, "An affected history projection is missing.")
                return query_models.GeneratedViewFacts(project_revision, tuple(items), tuple(attempts), receipts)
        finally:
            connection.close()

    def read_attempt_authority_status(self, attempt_id: AttemptId) -> query_models.AttemptAuthorityStatus | None:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                return read_attempt_authority_status(connection, attempt_id)
        finally:
            connection.close()

    def read_preparation_authority_status(self, item_id: ItemId) -> query_models.PreparationAuthorityStatus | None:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                return read_preparation_authority_status(connection, item_id)
        finally:
            connection.close()

    def read_item_definition(self, item_id: ItemId) -> query_models.ItemDefinitionFacts:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                return select_item_definition(connection, item_id)
        finally:
            connection.close()

    def read_item_status(self, item_id: ItemId) -> query_models.ItemStatusFacts | None:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                lifecycle = read_item_status(connection, item_id)
                if lifecycle is None:
                    return None
                preparation = read_preparation_authority_status(connection, item_id)
                return query_models.ItemStatusFacts(
                    lifecycle.project_revision,
                    lifecycle.item,
                    lifecycle.definition_title,
                    lifecycle.attempts,
                    preparation,
                )
        finally:
            connection.close()

    def read_attempt_context(self, attempt_id: AttemptId) -> query_models.AttemptContextFacts | None:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                selected = read_attempt_context(connection, attempt_id)
                if selected is None:
                    return None
                match selected:
                    case TerminalAttemptContextSelection():
                        return query_models.TerminalAttemptContextFacts(
                            selected.project_revision,
                            selected.attempt_id,
                            selected.item_id,
                        )
                    case NonterminalAttemptContextSelection():
                        reference = read_brief_artifact_reference(connection, selected.brief_artifact_ref_id)
                        if reference is None:
                            raise StorageError(
                                StorageErrorCode.INVALID_STATE,
                                "The selected nonterminal attempt has no accepted brief reference.",
                            )
                        return query_models.NonterminalAttemptContextFacts(
                            selected.project_revision,
                            selected.attempt_id,
                            selected.item_id,
                            selected.state,
                            selected.branch,
                            selected.base_revision,
                            selected.accepted_scope_revision,
                            selected.accepted_scope_digest,
                            selected.candidate_revision,
                            selected.brief_artifact_ref_id,
                            selected.item,
                            reference,
                        )
                    case _ as unreachable:
                        assert_never(unreachable)
        finally:
            connection.close()

    def read_parallel_preview(self, item_ids: tuple[ItemId, ...]) -> query_models.ParallelPreviewFacts | None:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                lifecycle = read_parallel_preview_lifecycle(connection, item_ids)
                if lifecycle is None:
                    return None
                items: list[query_models.ParallelPreviewItemFacts] = []
                for item in lifecycle.items:
                    preparation_status = read_preparation_authority_status(connection, item.item_id)
                    preparation = (
                        None
                        if preparation_status is None
                        else query_models.ParallelPreparationFacts(
                            preparation_status.status,
                            preparation_status.expires_at,
                        )
                    )
                    attempt = None
                    if item.attempt is not None:
                        authority = (
                            read_attempt_authority_status(connection, item.attempt.attempt_id)
                            if item.attempt.state == work_models.AttemptState.ACTIVE
                            else None
                        )
                        attempt = query_models.ParallelAttemptFacts(
                            item.attempt.attempt_id,
                            item.attempt.state,
                            None if authority is None else authority.status,
                            None if authority is None else authority.expires_at,
                        )
                    items.append(
                        query_models.ParallelPreviewItemFacts(
                            item.item_id,
                            item.label,
                            item.state,
                            item.live_dependencies,
                            preparation,
                            attempt,
                        )
                    )
                return query_models.ParallelPreviewFacts(lifecycle.project_revision, tuple(items))
        finally:
            connection.close()

    def read_item_definition_history(
        self, item_id: ItemId, *, limit: int, before_revision: int | None
    ) -> query_models.ItemDefinitionHistoryFacts:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                return select_item_definition_history(connection, item_id, limit=limit, before_revision=before_revision)
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
                    work_root,
                    published,
                    accepted_at,
                )
            except ArtifactError as error:
                raise _translate_artifact_verification_error(error) from error
            if isinstance(result, DecisionFailure):
                return transaction._select(result)
            reloaded = read_artifact_reference_by_id(connection, result.reference.artifact_ref_id)
            if reloaded != result.reference:
                raise StorageError(
                    StorageErrorCode.INVARIANT_VIOLATION,
                    "The accepted artifact reference did not reload exactly.",
                )
            return transaction._select(result)
