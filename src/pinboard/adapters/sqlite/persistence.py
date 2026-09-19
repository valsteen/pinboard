"""Persist one accepted application mutation inside one SQLite write transaction."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, Self, assert_never

from pinboard.adapters.files.errors import ArtifactError, ArtifactErrorCode
from pinboard.adapters.sqlite import state as sqlite_state
from pinboard.adapters.sqlite.artifacts import (
    accept_artifact_reference as write_artifact_reference,
)
from pinboard.adapters.sqlite.artifacts import accept_checkpoint_artifact, read_artifact_reference_by_id
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
    require_one_changed_row,
    translate_database_error,
)
from pinboard.adapters.sqlite.decision_reads import read_selected_decision_facts
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import (
    decode_definition_revision,
    insert_attempt,
    insert_definition_revision,
    rebind_attempt,
    replace_dependencies,
    set_attempt_state,
    set_item_state,
)
from pinboard.adapters.sqlite.models import (
    ArtifactIdAllocationRow,
    AttemptIdRow,
    GenerationRow,
    ItemIdRow,
    LiveItemCountRow,
    MutationAllocationRow,
    OpenMode,
    PersistedAllocationRow,
)
from pinboard.adapters.sqlite.proposals import (
    accept_proposal,
    create_proposal,
    insert_planned_replacement,
    set_proposal_disposition,
)
from pinboard.application import query_models, stored_state
from pinboard.application.artifacts import ArtifactRef, EvidenceArtifactRef, ResultArtifactRef
from pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    CheckpointAcceptanceMutation,
    CheckpointMutationAllocation,
    CommittedEffect,
    CompletionAcceptanceMutation,
    MutationAllocation,
    OrderMutation,
    PreparationAuthorityMutation,
    ProposalCreationMutation,
    ReviewSubmissionMutation,
    StoredStateMutation,
    TransitionMutation,
)
from pinboard.application.mutations import stored_transition_receipt
from pinboard.application.ports import ArtifactReferenceAcceptance
from pinboard.domain import decision_models, work_models
from pinboard.domain.definition_decisions import DefinitionRevisionDecision
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, HistoryId, ItemId


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


def _mutation_subjects(
    mutation: StoredStateMutation,
) -> tuple[tuple[ItemId, ...], tuple[AttemptId, ...]]:
    match mutation:
        case TransitionMutation(decision=decision) | ReviewSubmissionMutation(decision=decision):
            match decision.change:
                case (
                    decision_models.ItemStateChange(item=item)
                    | decision_models.BlockItemChange(item=item)
                    | decision_models.ActivationChange(item=item)
                    | decision_models.ItemClosureChange(item=item)
                    | DefinitionRevisionDecision(item=item)
                ):
                    return (item,), ()
                case decision_models.PlannedReplacementChange(relation=relation):
                    return (relation.affected_item,), ()
                case decision_models.ReplacementDispositionChange(disposition=disposition):
                    return (disposition.affected_item,), ()
                case (
                    decision_models.AttemptStateChange(item=item, attempt=attempt)
                    | decision_models.BlockAttemptChange(item=item, attempt=attempt)
                    | decision_models.ResumeAttemptChange(item=item, attempt=attempt)
                    | decision_models.ReviewSubmissionChange(item=item, attempt=attempt)
                    | decision_models.ReviewAcceptanceChange(item=item, attempt=attempt)
                    | decision_models.ReviewReturnChange(item=item, attempt=attempt)
                    | decision_models.CompletionChange(item=item, attempt=attempt)
                    | decision_models.AttemptClosureChange(item=item, attempt=attempt)
                    | decision_models.RebindAttemptChange(item=item, attempt=attempt)
                ):
                    return (item,), (attempt,)
                case decision_models.AcceptedProposalChange(accepted_item=accepted):
                    return (accepted.item,), ()
                case (
                    decision_models.MergedProposalChange(proposal=proposal)
                    | decision_models.RejectedProposalChange(proposal=proposal)
                    | decision_models.ReturnedProposalChange(proposal=proposal)
                ):
                    return (ItemId(proposal),), ()
                case _ as unreachable:
                    assert_never(unreachable)
        case CheckpointAcceptanceMutation(decision=decision) | CompletionAcceptanceMutation(decision=decision):
            return (decision.change.item,), (decision.change.attempt,)
        case ProposalCreationMutation() | AttemptAuthorityMutation() | PreparationAuthorityMutation() | OrderMutation():
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


def _committed_effect_ids(  # noqa: C901, PLR0912 - exhaustively projects every closed mutation effect
    connection: sqlite3.Connection, mutation: StoredStateMutation
) -> tuple[tuple[ItemId, ...], tuple[AttemptId, ...]]:
    item_ids, attempt_ids = _mutation_subjects(mutation)
    affected_items = list(item_ids)
    liveness_flip_roots: list[ItemId] = []
    match mutation:
        case OrderMutation(change=change):
            affected_items.extend(item for _position, item in change.changed_positions)
        case ProposalCreationMutation(decision=decision):
            affected_items.append(decision.intake_item.item_id)
            if decision.planned_replacement is not None:
                affected_items.append(decision.planned_replacement.affected_item)
            if decision.prerequisite_change is not None:
                affected_items.append(decision.prerequisite_change.item_id)
            affected_items.extend(
                decode_row(row, ItemIdRow).item_id
                for row in connection.execute(
                    "SELECT item_id FROM work_items WHERE queue_position >= ? ORDER BY queue_position",
                    (decision.intake_item.position,),
                ).fetchall()
            )
        case PreparationAuthorityMutation(decision=decision):
            affected_items.append(decision.proposed_replacement.item)
        case AttemptAuthorityMutation():
            pass
        case (
            TransitionMutation(decision=decision)
            | ReviewSubmissionMutation(decision=decision)
            | CheckpointAcceptanceMutation(decision=decision)
            | CompletionAcceptanceMutation(decision=decision)
        ):
            match decision.change:
                case decision_models.ActivationChange(attempt=attempt):
                    attempt_ids = (*attempt_ids, attempt)
                case (
                    decision_models.CompletionChange(item=item)
                    | decision_models.CoveredCompletionChange(item=item)
                    | decision_models.AttemptClosureChange(item=item)
                    | decision_models.ItemClosureChange(item=item)
                    | decision_models.MergedProposalChange(proposal=item)
                    | decision_models.RejectedProposalChange(proposal=item)
                ):
                    selected_item = ItemId(item)
                    liveness_flip_roots.append(selected_item)
                    selected = connection.execute(
                        "SELECT queue_position FROM work_items WHERE item_id = ?", (selected_item,)
                    ).fetchone()
                    if selected is not None and selected["queue_position"] is not None:
                        affected_items.extend(
                            decode_row(row, ItemIdRow).item_id
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
                    | decision_models.PlannedReplacementChange()
                    | decision_models.ReplacementDispositionChange()
                ):
                    pass
                case _ as unreachable:
                    assert_never(unreachable)
        case _ as unreachable:
            assert_never(unreachable)
    for item_id in dict.fromkeys(liveness_flip_roots):
        affected_items.extend(
            decode_row(row, ItemIdRow).item_id
            for row in connection.execute(
                """
                SELECT owner.item_id
                FROM item_dependencies AS dependency INDEXED BY item_dependencies_by_dependency
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

    def advance_item_revision(item: ItemId) -> DecisionFailure | None:
        current = facts.item(item)
        return require_one_changed_row(
            connection.execute(
                """
                UPDATE work_items
                SET subject_revision = ?, updated_at = ?
                WHERE item_id = ? AND subject_revision = ?
                """,
                (revision, now.isoformat(), item, current.subject_revision),
            ),
            "The replacement subject changed before targeted persistence.",
        )

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
        case decision_models.PlannedReplacementChange(relation=relation):
            if (failure := advance_item_revision(relation.affected_item)) is not None:
                return failure
            insert_planned_replacement(connection, relation, revision)
        case decision_models.ReplacementDispositionChange(disposition=disposition):
            if (failure := advance_item_revision(disposition.affected_item)) is not None:
                return failure
            connection.execute(
                """
                INSERT INTO replacement_dispositions (
                    affected_item_id, relation_revision, rationale, accepted_cost,
                    recorded_by, recorded_at, accepted_project_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    disposition.affected_item,
                    disposition.relation_revision,
                    disposition.rationale,
                    disposition.accepted_cost,
                    disposition.recorded_by,
                    disposition.recorded_at.isoformat(),
                    revision,
                ),
            )
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
        artifacts.candidate,
        artifacts.candidate_id,
        revision,
        now,
    )
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
    accept_checkpoint_artifact(
        connection,
        artifacts.package,
        artifacts.package_id,
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


def _persist_completion_acceptance(
    connection: sqlite3.Connection,
    facts: _PersistenceFacts,
    mutation: CompletionAcceptanceMutation,
) -> DecisionFailure | None:
    change = mutation.decision.change
    artifacts = mutation.completion_artifacts
    revision = mutation.receipt.project_revision
    now = mutation.decision.receipt.decided_at
    for artifact, artifact_id in (
        (artifacts.result, artifacts.result_id),
        (artifacts.review, artifacts.review_id),
        (artifacts.package, artifacts.package_id),
    ):
        accept_checkpoint_artifact(connection, artifact, artifact_id, revision, now)
    if (
        failure := set_item_state(
            connection,
            facts.item(change.item),
            work_models.WorkState.REVIEW,
            stored_state.StoredWorkItemState.DONE,
            revision,
            now,
            change.evidence,
        )
    ) is not None:
        return failure
    if (
        failure := set_attempt_state(
            connection,
            facts.attempt(change.attempt),
            work_models.AttemptState.REVIEW,
            work_models.AttemptState.DONE,
            revision,
            now,
            result_artifact_ref_id=artifacts.result_id,
        )
    ) is not None:
        return failure
    if (
        change.authority_change is not None
        and (failure := fence_attempt_authority(connection, change.authority_change, now)) is not None
    ):
        return failure
    return None


def _persist_state_change(  # noqa: C901, PLR0912 - exhaustive closed mutation persistence
    connection: sqlite3.Connection,
    facts: _PersistenceFacts,
    mutation: StoredStateMutation,
) -> DecisionFailure | None:
    match mutation:
        case OrderMutation(change=change):
            moved = change.changed_positions
            # Move changed entries above the live range before assigning unique final positions.
            for position, item in moved:
                if (
                    failure := require_one_changed_row(
                        connection.execute(
                            "UPDATE work_items SET queue_position = ? WHERE item_id = ? AND queue_position IS NOT NULL",
                            (len(change.before) + position, item),
                        ),
                        "A reordered live item disappeared.",
                    )
                ) is not None:
                    return failure
            for position, item in moved:
                if (
                    failure := require_one_changed_row(
                        connection.execute(
                            "UPDATE work_items SET queue_position = ? WHERE item_id = ? AND queue_position = ?",
                            (position, item, len(change.before) + position),
                        ),
                        "A reordered live item changed before final positioning.",
                    )
                ) is not None:
                    return failure
            return None
        case TransitionMutation():
            return _persist_transition(connection, facts, mutation)
        case ReviewSubmissionMutation():
            accept_checkpoint_artifact(
                connection,
                mutation.candidate_snapshot,
                mutation.candidate_snapshot_id,
                mutation.receipt.project_revision,
                mutation.decision.receipt.decided_at,
            )
            return _persist_transition(
                connection,
                facts,
                TransitionMutation(mutation.decision, mutation.receipt),
            )
        case CheckpointAcceptanceMutation():
            return _persist_checkpoint_acceptance(connection, facts, mutation)
        case CompletionAcceptanceMutation():
            return _persist_completion_acceptance(connection, facts, mutation)
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
    selected_allocation = decode_row(allocation, PersistedAllocationRow)
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


class SQLiteWorkTransaction:
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
            raise translate_database_error(error).with_database_path(self._path) from error
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
                    raise translate_database_error(error).with_database_path(self._path) from error
                return False
            if not self._rejected:
                try:
                    connection.commit()
                except sqlite3.Error as commit_error:
                    connection.rollback()
                    raise translate_database_error(commit_error).with_database_path(self._path) from commit_error
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
        selected = decode_row(allocation, MutationAllocationRow)
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
        selected_artifact_allocation = decode_row(artifact_allocation, ArtifactIdAllocationRow)
        return CheckpointMutationAllocation(
            allocation.project_revision,
            allocation.next_history_id,
            ArtifactRefId(selected_artifact_allocation.next_artifact_ref_id),
            tuple(accepted),
        )

    def read_live_order(self) -> tuple[ItemId, ...]:
        return tuple(
            decode_row(row, ItemIdRow).item_id
            for row in self.connection.execute(
                "SELECT item_id FROM work_items WHERE queue_position IS NOT NULL ORDER BY queue_position"
            ).fetchall()
        )

    def read_live_item_count(self) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(queue_position), 0) AS live_item_count FROM work_items"
        ).fetchone()
        if row is None:
            raise StorageError(StorageErrorCode.INVALID_STATE, "Live queue allocation is unavailable.")
        return decode_row(row, LiveItemCountRow).live_item_count

    def read_attempt_authority_status(self, attempt_id: AttemptId) -> query_models.AttemptAuthorityStatus | None:
        return read_attempt_authority_status(self.connection, attempt_id)

    def read_preparation_authority_status(self, item_id: ItemId) -> query_models.PreparationAuthorityStatus | None:
        return read_preparation_authority_status(self.connection, item_id)

    def read_attempt_generation(self, attempt_id: AttemptId) -> int:
        row = self.connection.execute(
            "SELECT generation_high_water FROM attempt_lease_counters WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        return 0 if row is None else decode_row(row, GenerationRow).generation_high_water

    def read_preparation_generation(self, item_id: ItemId) -> int:
        row = self.connection.execute(
            "SELECT generation_high_water FROM preparation_lease_counters WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        return 0 if row is None else decode_row(row, GenerationRow).generation_high_water

    def commit(self, mutation: StoredStateMutation) -> DecisionResult[CommittedEffect]:
        item_ids, attempt_ids = _committed_effect_ids(self.connection, mutation)
        continuation_attempt_id = attempt_ids[0] if attempt_ids else None
        if continuation_attempt_id is None and mutation.receipt.transition.item is not None:
            row = self.connection.execute(
                "SELECT attempt_id FROM attempts WHERE item_id = ? AND state != 'done'",
                (mutation.receipt.transition.item,),
            ).fetchone()
            if row is not None:
                continuation_attempt_id = decode_row(row, AttemptIdRow).attempt_id
        if (failure := _persist(self.connection, mutation)) is not None:
            return self._select(failure)
        if (
            continuation_attempt_id is None
            and isinstance(mutation, TransitionMutation)
            and isinstance(mutation.decision.change, decision_models.ActivationChange)
        ):
            continuation_attempt_id = mutation.decision.change.attempt
        return self._select(CommittedEffect(mutation.receipt, item_ids, attempt_ids, continuation_attempt_id))


def accept_artifact_reference(
    database_path: Path,
    work_root: Path,
    published: ArtifactRef,
    accepted_at: datetime,
) -> DecisionResult[ArtifactReferenceAcceptance]:
    """Accept and exactly reload one immutable artifact inside one write transaction."""
    with SQLiteWorkTransaction(database_path) as transaction:
        try:
            result = write_artifact_reference(transaction.connection, work_root, published, accepted_at)
        except ArtifactError as error:
            match error.code:
                case ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION:
                    code = StorageErrorCode.INVARIANT_VIOLATION
                case ArtifactErrorCode.STORAGE_IO_ERROR:
                    code = StorageErrorCode.IO_ERROR
                case _ as unreachable:
                    assert_never(unreachable)
            raise StorageError(code, str(error), retryable=False) from error
        if isinstance(result, DecisionFailure):
            return transaction._select(result)
        reloaded = read_artifact_reference_by_id(transaction.connection, result.reference.artifact_ref_id)
        if reloaded != result.reference:
            raise StorageError(
                StorageErrorCode.INVARIANT_VIOLATION,
                "The accepted artifact reference did not reload exactly.",
            )
        return transaction._select(result)
