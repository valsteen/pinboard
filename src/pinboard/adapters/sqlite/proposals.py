"""Read and change proposal records on a supplied connection.

This module never commits, rolls back, closes the connection, calls callbacks,
reads the filesystem, or obtains time. Expected stale CAS writes return a
``DecisionFailure``; SQLite and persisted-invariant failures remain exceptional.
"""

import sqlite3
from datetime import datetime
from typing import assert_never

import msgspec

from pinboard.adapters.sqlite.database import decode_row, require_one_changed_row
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import (
    append_definition_revision,
    increment_item_state_count,
    make_queue_space,
    move_item_state_count,
    replace_dependencies,
)
from pinboard.application import stored_state
from pinboard.application.mutation_models import ProposalCreationMutation
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import ItemId, ProposalId, TaskId


class _StoredProposalRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    proposal_id: ProposalId
    created_at: datetime
    recorded_at: datetime
    source_task_id: TaskId
    user_label: str
    trigger: str
    why_it_matters: str
    relation_kind: work_models.ProposalRelationKind
    relation_item_id: ItemId | None
    relation_replacement_cost: str | None
    effect: str
    unlock: str
    urgency_evidence: str
    disposition: work_models.ProposalDispositionKind | None
    disposition_target_item_id: ItemId | None
    disposition_reason: str | None
    subject_revision: int
    disposition_recorded_at: datetime | None

    def proposal(self) -> stored_state.StoredProposal:
        return stored_state.StoredProposal(
            self.proposal_id,
            self.created_at,
            self.recorded_at,
            self.source_task_id,
            self.user_label,
            self.trigger,
            self.why_it_matters,
            decode_proposal_relation(self.relation_kind, self.relation_item_id, self.relation_replacement_cost),
            self.effect,
            self.unlock,
            self.urgency_evidence,
            _decode_stored_proposal_disposition(
                self.disposition,
                self.disposition_target_item_id,
                self.disposition_reason,
                self.disposition_recorded_at,
            ),
            self.subject_revision,
        )


class _SubjectRevisionRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    subject_revision: int


def _require_relation_item(kind: work_models.ProposalRelationKind, value: ItemId | None) -> ItemId:
    if value is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"{kind.value} proposal has no related item.")
    return value


def _reject_relation_item(kind: work_models.ProposalRelationKind, value: ItemId | None) -> None:
    if value is not None:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"{kind.value} proposal has a related item.")


def decode_proposal_relation(
    kind: work_models.ProposalRelationKind,
    value: ItemId | None,
    replacement_cost: str | None,
) -> work_models.ProposalRelation:
    if kind != work_models.ProposalRelationKind.PLANNED_REPLACEMENT and replacement_cost is not None:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"{kind.value} proposal has replacement cost.")
    match kind:
        case work_models.ProposalRelationKind.INDEPENDENT:
            _reject_relation_item(kind, value)
            return work_models.IndependentProposalRelation()
        case work_models.ProposalRelationKind.PREREQUISITE:
            return work_models.PrerequisiteProposalRelation(_require_relation_item(kind, value))
        case work_models.ProposalRelationKind.FOLLOW_UP:
            return work_models.FollowUpProposalRelation(_require_relation_item(kind, value))
        case work_models.ProposalRelationKind.DUPLICATE:
            return work_models.DuplicateProposalRelation(_require_relation_item(kind, value))
        case work_models.ProposalRelationKind.CONTRADICTION:
            return work_models.ContradictionProposalRelation(_require_relation_item(kind, value))
        case work_models.ProposalRelationKind.CLARIFICATION:
            _reject_relation_item(kind, value)
            return work_models.ClarificationProposalRelation()
        case work_models.ProposalRelationKind.PLANNED_REPLACEMENT:
            if replacement_cost is None:
                raise StorageError(StorageErrorCode.INVALID_STATE, "Planned replacement proposal has no cost.")
            return work_models.PlannedReplacementProposalRelation(_require_relation_item(kind, value), replacement_cost)
        case _ as unreachable:
            assert_never(unreachable)


def _require_disposition_target(
    kind: work_models.ProposalDispositionKind,
    target: ItemId | None,
) -> ItemId:
    if target is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"{kind.value} proposal disposition has no target item.")
    return target


def _require_disposition_reason(kind: work_models.ProposalDispositionKind, reason: str | None) -> str:
    if reason is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"{kind.value} proposal disposition has no reason.")
    return reason


def _require_disposition_time(kind: work_models.ProposalDispositionKind, value: datetime | None) -> datetime:
    if value is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"{kind.value} proposal disposition has no timestamp.")
    return value


def _forbid_disposition_value(
    kind: work_models.ProposalDispositionKind,
    name: str,
    value: ItemId | str | None,
) -> None:
    if value is not None:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"{kind.value} proposal disposition has a {name}.")


def _decode_stored_proposal_disposition(
    kind: work_models.ProposalDispositionKind | None,
    target: ItemId | None,
    reason: str | None,
    disposed_at: datetime | None,
) -> work_models.ProposalDisposition | None:
    match kind:
        case None:
            if target is not None or reason is not None or disposed_at is not None:
                raise StorageError(StorageErrorCode.INVALID_STATE, "Open proposal has disposition details.")
            return None
        case work_models.ProposalDispositionKind.ACCEPTED:
            _forbid_disposition_value(kind, "reason", reason)
            return work_models.AcceptedProposalDisposition(
                _require_disposition_target(kind, target),
                _require_disposition_time(kind, disposed_at),
            )
        case work_models.ProposalDispositionKind.MERGED:
            _forbid_disposition_value(kind, "reason", reason)
            return work_models.MergedProposalDisposition(
                _require_disposition_target(kind, target),
                _require_disposition_time(kind, disposed_at),
            )
        case work_models.ProposalDispositionKind.RETURNED:
            _forbid_disposition_value(kind, "target item", target)
            return work_models.ReturnedProposalDisposition(
                _require_disposition_reason(kind, reason),
                _require_disposition_time(kind, disposed_at),
            )
        case work_models.ProposalDispositionKind.REJECTED:
            _forbid_disposition_value(kind, "target item", target)
            return work_models.RejectedProposalDisposition(
                _require_disposition_reason(kind, reason),
                _require_disposition_time(kind, disposed_at),
            )
        case _ as unreachable:
            assert_never(unreachable)


def _encode_proposal_disposition_columns(
    value: work_models.ProposalDisposition | None,
) -> tuple[str | None, ItemId | None, str | None, str | None]:
    match value:
        case None:
            return None, None, None, None
        case (
            work_models.AcceptedProposalDisposition(kind=kind, target=target, disposed_at=disposed_at)
            | work_models.MergedProposalDisposition(kind=kind, target=target, disposed_at=disposed_at)
        ):
            return kind.value, target, None, disposed_at.isoformat()
        case (
            work_models.ReturnedProposalDisposition(kind=kind, reason=reason, disposed_at=disposed_at)
            | work_models.RejectedProposalDisposition(kind=kind, reason=reason, disposed_at=disposed_at)
        ):
            return kind.value, None, reason, disposed_at.isoformat()
        case _ as unreachable:
            assert_never(unreachable)


def _read_proposal_records(
    connection: sqlite3.Connection,
    *,
    pending_only: bool,
) -> tuple[stored_state.StoredProposal, ...]:
    condition = "WHERE disposition IS NULL" if pending_only else ""
    return tuple(
        decode_row(row, _StoredProposalRow).proposal()
        for row in connection.execute(
            f"""
            SELECT proposal_id, created_at, recorded_at, source_task_id, user_label, trigger, why_it_matters,
                   relation_kind, relation_item_id, relation_replacement_cost, effect, unlock, urgency_evidence, disposition,
                   disposition_target_item_id, disposition_reason, subject_revision, disposition_recorded_at
            FROM proposals
            {condition}
            ORDER BY proposal_id
            """
        ).fetchall()
    )


def read_proposals(connection: sqlite3.Connection) -> stored_state.ProposalRecords:
    proposals = _read_proposal_records(connection, pending_only=False)
    evidence = tuple(
        decode_row(row, stored_state.ProposalEvidence)
        for row in connection.execute(
            "SELECT proposal_id, position, selector FROM proposal_evidence ORDER BY proposal_id, position"
        ).fetchall()
    )
    freshness = tuple(
        decode_row(row, stored_state.ProposalFreshness)
        for row in connection.execute(
            "SELECT proposal_id, position, assumption FROM proposal_freshness ORDER BY proposal_id, position"
        ).fetchall()
    )
    return stored_state.ProposalRecords(proposals, evidence, freshness)


def read_pending_proposals(connection: sqlite3.Connection) -> stored_state.ProposalRecords:
    """Read only proposals and child records included in project handover."""

    proposals = _read_proposal_records(connection, pending_only=True)
    evidence = tuple(
        decode_row(row, stored_state.ProposalEvidence)
        for proposal in proposals
        for row in connection.execute(
            "SELECT proposal_id, position, selector FROM proposal_evidence WHERE proposal_id = ? ORDER BY position",
            (proposal.proposal_id,),
        ).fetchall()
    )
    freshness = tuple(
        decode_row(row, stored_state.ProposalFreshness)
        for proposal in proposals
        for row in connection.execute(
            "SELECT proposal_id, position, assumption FROM proposal_freshness WHERE proposal_id = ? ORDER BY position",
            (proposal.proposal_id,),
        ).fetchall()
    )
    return stored_state.ProposalRecords(proposals, evidence, freshness)


def read_proposal(connection: sqlite3.Connection, proposal_id: ProposalId) -> stored_state.StoredProposal | None:
    row = connection.execute(
        """
        SELECT proposal_id, created_at, recorded_at, source_task_id, user_label, trigger,
               why_it_matters, relation_kind, relation_item_id, relation_replacement_cost, effect, unlock, urgency_evidence,
               disposition, disposition_target_item_id, disposition_reason, subject_revision,
               disposition_recorded_at
        FROM proposals WHERE proposal_id = ?
        """,
        (proposal_id,),
    ).fetchone()
    return None if row is None else decode_row(row, _StoredProposalRow).proposal()


def set_proposal_disposition(
    connection: sqlite3.Connection,
    proposal_id: ProposalId,
    disposition: work_models.ProposalDisposition,
    revision: int,
) -> DecisionFailure | None:
    kind, target, reason, disposed_at = _encode_proposal_disposition_columns(disposition)
    return require_one_changed_row(
        connection.execute(
            """
            UPDATE proposals
            SET disposition = ?, disposition_target_item_id = ?, disposition_reason = ?,
                disposition_recorded_at = ?, subject_revision = ?
            WHERE proposal_id = ? AND disposition IS NULL
            """,
            (kind, target, reason, disposed_at, revision, proposal_id),
        ),
        "The proposal disposition is stale.",
    )


def accept_proposal(
    connection: sqlite3.Connection,
    current: stored_state.StoredWorkItem,
    change: decision_models.AcceptedProposalChange,
    revision: int,
    now: datetime,
) -> DecisionFailure | None:
    accepted = change.accepted_item
    if current.item_id != accepted.item:
        raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "The accepted proposal item is missing.")
    if accepted.definition_digest_after != accepted.definition_digest_before:
        append_definition_revision(
            connection,
            stored_state.ItemDefinitionRevision(
                accepted.item,
                accepted.definition_revision,
                accepted.definition_digest_after,
                accepted.definition,
                "Accepted explicit proposal dependencies.",
                accepted.definition_source_task,
                accepted.definition_digest_before,
                accepted.definition_digest_after,
                revision,
                now,
            ),
        )
    if (
        failure := require_one_changed_row(
            connection.execute(
                """
                UPDATE work_items
                SET state = ?, timing = ?, source = ?, next_action = ?, notes = ?, subject_revision = ?, updated_at = ?
                WHERE item_id = ? AND state = 'intake' AND subject_revision = ?
                """,
                (
                    accepted.state.value,
                    None if accepted.timing is None else accepted.timing.value,
                    accepted.source,
                    accepted.next_action,
                    accepted.notes,
                    revision,
                    now.isoformat(),
                    accepted.item,
                    current.subject_revision,
                ),
            ),
            "The accepted proposal item is stale.",
        )
    ) is not None:
        return failure
    move_item_state_count(
        connection,
        current.state,
        stored_state.stored_live_work_state(accepted.state),
    )
    replace_dependencies(connection, accepted.item, accepted.dependencies)
    return set_proposal_disposition(
        connection,
        change.proposal,
        work_models.AcceptedProposalDisposition(accepted.item, change.disposed_at),
        revision,
    )


def insert_planned_replacement(
    connection: sqlite3.Connection,
    relation: work_models.PlannedReplacement,
    recorded_at: datetime,
    accepted_project_revision: int,
) -> None:
    connection.execute(
        """
        INSERT INTO planned_replacements (
            affected_item_id, relation_revision, replacement_item_id, replacement_cost,
            status, recorded_by, recorded_at, accepted_project_revision
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            relation.affected_item,
            relation.relation_revision,
            relation.replacement_item,
            relation.replacement_cost,
            relation.status.value,
            relation.recorded_by,
            recorded_at.isoformat(),
            accepted_project_revision,
        ),
    )


def create_proposal(
    connection: sqlite3.Connection,
    mutation: ProposalCreationMutation,
) -> DecisionFailure | None:
    decision = mutation.decision
    intake = decision.proposal
    intake_item = decision.intake_item
    revision = mutation.receipt.project_revision
    now = mutation.receipt.transition.decided_at
    prerequisite = decision.prerequisite_change
    prerequisite_subject_revision: int | None = None
    replacement_subject_revision: int | None = None
    if prerequisite is not None:
        target = connection.execute(
            "SELECT subject_revision FROM work_items WHERE item_id = ?",
            (prerequisite.item_id,),
        ).fetchone()
        if target is None:
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_AVAILABLE,
                "The prerequisite target changed before persistence.",
                None,
            )
        prerequisite_subject_revision = decode_row(target, _SubjectRevisionRow).subject_revision
    if decision.planned_replacement is not None:
        target = connection.execute(
            "SELECT subject_revision FROM work_items WHERE item_id = ?",
            (decision.planned_replacement.affected_item,),
        ).fetchone()
        if target is None:
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_AVAILABLE,
                "The planned-replacement target changed before persistence.",
                None,
            )
        replacement_subject_revision = decode_row(target, _SubjectRevisionRow).subject_revision
    if (failure := make_queue_space(connection, intake_item.position)) is not None:
        return failure
    relation = intake.relation
    connection.execute(
        """
        INSERT INTO proposals (
            proposal_id, created_at, recorded_at, source_task_id, user_label, trigger, why_it_matters,
            relation_kind, relation_item_id, relation_replacement_cost, effect, unlock, urgency_evidence, disposition,
            disposition_target_item_id, disposition_reason, disposition_recorded_at, subject_revision
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)
        """,
        (
            intake.proposal_id,
            intake.created_at.isoformat(),
            now.isoformat(),
            intake.source_task_id,
            intake.user_label,
            intake.trigger,
            intake.why_it_matters,
            relation.kind.value,
            relation.item,
            relation.replacement_cost if isinstance(relation, work_models.PlannedReplacementProposalRelation) else None,
            intake.effect,
            intake.unlock,
            intake.urgency_evidence,
            revision,
        ),
    )
    connection.executemany(
        "INSERT INTO proposal_evidence (proposal_id, position, selector) VALUES (?, ?, ?)",
        tuple((intake.proposal_id, position, value) for position, value in enumerate(decision.evidence)),
    )
    connection.executemany(
        "INSERT INTO proposal_freshness (proposal_id, position, assumption) VALUES (?, ?, ?)",
        tuple((intake.proposal_id, position, value) for position, value in enumerate(decision.freshness)),
    )
    connection.execute(
        """
        INSERT INTO work_items (
            item_id, state, timing, source, outcome_evidence, next_action, notes, subject_revision,
            recorded_at, updated_at, queue_position
        ) VALUES (?, 'intake', NULL, ?, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (
            intake_item.item_id,
            f"proposal:{intake.proposal_id}",
            intake.unlock,
            intake.urgency_evidence,
            revision,
            now.isoformat(),
            now.isoformat(),
            intake_item.position,
        ),
    )
    increment_item_state_count(connection, stored_state.StoredWorkItemState.INTAKE)
    append_definition_revision(
        connection,
        stored_state.ItemDefinitionRevision(
            intake_item.item_id,
            1,
            intake_item.definition_digest,
            intake_item.definition,
            "Accepted proposal definition.",
            intake.source_task_id,
            None,
            intake_item.definition_digest,
            revision,
            now,
        ),
    )
    replace_dependencies(connection, intake_item.item_id, intake_item.dependencies)
    if decision.planned_replacement is not None:
        relation = decision.planned_replacement
        assert replacement_subject_revision is not None
        if (
            failure := require_one_changed_row(
                connection.execute(
                    """
                    UPDATE work_items
                    SET subject_revision = ?, updated_at = ?
                    WHERE item_id = ? AND subject_revision = ?
                    """,
                    (revision, now.isoformat(), relation.affected_item, replacement_subject_revision),
                ),
                "The planned-replacement target changed before persistence.",
            )
        ) is not None:
            return failure
        insert_planned_replacement(connection, relation, now, revision)
    if prerequisite is None:
        return None
    assert prerequisite_subject_revision is not None
    if (
        failure := require_one_changed_row(
            connection.execute(
                """
                UPDATE work_items
                SET subject_revision = ?, updated_at = ?
                WHERE item_id = ? AND subject_revision = ?
                """,
                (
                    revision,
                    now.isoformat(),
                    prerequisite.item_id,
                    prerequisite_subject_revision,
                ),
            ),
            "The prerequisite target changed before persistence.",
        )
    ) is not None:
        return failure
    connection.execute(
        "INSERT INTO item_dependencies (item_id, dependency_id, position) VALUES (?, ?, ?)",
        (prerequisite.item_id, prerequisite.dependency_id, prerequisite.position),
    )
    append_definition_revision(
        connection,
        stored_state.ItemDefinitionRevision(
            prerequisite.item_id,
            prerequisite.definition_revision + 1,
            prerequisite.definition_digest_after,
            prerequisite.definition_after,
            "Accepted prerequisite proposal dependency.",
            intake.source_task_id,
            prerequisite.definition_digest_before,
            prerequisite.definition_digest_after,
            revision,
            now,
        ),
    )
    return None
