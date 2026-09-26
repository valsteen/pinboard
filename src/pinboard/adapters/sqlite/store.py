"""Compose the public SQLite store's read operations and write entry points.

Read methods own only their read connection. Write methods delegate to the
SQLite persistence owner, which owns transaction lifetime and effects. This
module never obtains time, reads artifact bytes directly, or invokes callbacks.
"""

import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard.adapters.sqlite import state as sqlite_state
from pinboard.adapters.sqlite.artifacts import (
    read_artifact_reference,
    read_artifact_reference_by_id,
    read_brief_artifact_reference,
    read_latest_artifact_reference,
)
from pinboard.adapters.sqlite.authority import (
    read_attempt_authority_status,
    read_preparation_authority_status,
)
from pinboard.adapters.sqlite.database import (
    decode_row,
    open_database,
    read_operation,
    verify_database_integrity,
)
from pinboard.adapters.sqlite.decision_reads import (
    read_current_replacements,
    read_current_snapshot,
    read_selected_decision_facts,
)
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import (
    NonterminalAttemptContextSelection,
    TerminalAttemptContextSelection,
    decode_definition_revision,
    read_attempt_context,
    read_item_status,
    read_parallel_preview_lifecycle,
)
from pinboard.adapters.sqlite.lifecycle import (
    read_item_definition as select_item_definition,
)
from pinboard.adapters.sqlite.lifecycle import (
    read_item_definition_history as select_item_definition_history,
)
from pinboard.adapters.sqlite.models import (
    AttemptIdRow,
    CandidateSnapshotAttemptRow,
    DependencyViewRow,
    HistoryIdRow,
    ItemIdRow,
    OpenMode,
    ProjectRevisionRow,
    StateCountRow,
)
from pinboard.adapters.sqlite.persistence import SQLiteWorkTransaction
from pinboard.adapters.sqlite.persistence import accept_artifact_reference as persist_artifact_reference
from pinboard.adapters.sqlite.proposals import (
    read_proposal,
)
from pinboard.application import candidate_snapshots, queries, query_models, stored_state, work_briefs
from pinboard.application.artifacts import ArtifactRef
from pinboard.application.ports import ArtifactReferenceAcceptance
from pinboard.application.project_export import ProjectExportState
from pinboard.domain import decision_models, history, work_models
from pinboard.domain.errors import DecisionResult
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, HistoryId, ItemId, LeaseId, ProposalId
from pinboard.domain.ledger import LedgerSnapshot


def _read_generated_view_facts(
    connection: sqlite3.Connection,
    item_ids: tuple[ItemId, ...],
    attempt_ids: tuple[AttemptId, ...],
    history_ids: tuple[HistoryId, ...],
    now: datetime,
) -> query_models.GeneratedViewFacts:
    project_row = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
    if project_row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Project metadata is missing.")
    project_revision = decode_row(project_row, ProjectRevisionRow).revision
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
            decode_row(row, DependencyViewRow)
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
            replacements, dispositions = read_current_replacements(connection, (item_id,))
            attempt_row = connection.execute(
                "SELECT attempt_id FROM attempts WHERE item_id = ? AND state != 'done'",
                (item_id,),
            ).fetchone()
            attempt_id = None if attempt_row is None else decode_row(attempt_row, AttemptIdRow).attempt_id
            proposal_ids = tuple(dict.fromkeys((ProposalId(item_id), *(ProposalId(value) for value in dependencies))))
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
                    tuple((value.dependency_id, value.queue_position is not None) for value in dependency_rows),
                    work_models.DefinitionAnchor(
                        definition.item_id,
                        definition.revision,
                        definition.digest,
                        definition.definition,
                    ),
                    selected_proposals,
                    read_preparation_authority_status(connection, item_id),
                    replacements[0] if replacements else None,
                    dispositions[0] if dispositions else None,
                ),
                now,
            )
        items.append(query_models.ItemProjectionFacts(item, dependencies, projected, definition))
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
                None
                if attempt.state == work_models.AttemptState.DONE
                else read_brief_artifact_reference(connection, attempt.brief_artifact_ref_id),
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


def _read_overview_proposals(
    connection: sqlite3.Connection, snapshot: LedgerSnapshot
) -> tuple[stored_state.StoredProposal, ...]:
    proposal_ids = tuple(
        dict.fromkeys(ProposalId(item_id) for item in snapshot.items for item_id in (item.item, *item.depends_on))
    )
    return tuple(
        proposal for proposal_id in proposal_ids if (proposal := read_proposal(connection, proposal_id)) is not None
    )


def _read_attempt_context_facts(
    connection: sqlite3.Connection,
    attempt_id: AttemptId,
) -> query_models.AttemptContextFacts | None:
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
            replacements, dispositions = read_current_replacements(connection, (selected.item_id,))
            replacement = next(
                (value for value in replacements if value.status == work_models.PlannedReplacementStatus.CURRENT),
                None,
            )
            return query_models.NonterminalAttemptContextFacts(
                selected.project_revision,
                selected.attempt_id,
                selected.subject_revision,
                selected.item_id,
                selected.state,
                selected.branch,
                selected.base_revision,
                selected.accepted_scope_revision,
                selected.accepted_scope_digest,
                selected.candidate_revision,
                selected.brief_artifact_ref_id,
                replace(
                    selected.item,
                    current_replacement_revision=None if replacement is None else replacement.relation_revision,
                    replacement_resolved=replacement is None or bool(dispositions),
                ),
                reference,
            )
        case _ as unreachable:
            assert_never(unreachable)


def _read_candidate_snapshot_context_facts(
    connection: sqlite3.Connection,
    attempt_id: AttemptId,
) -> query_models.CandidateSnapshotContextFacts | None:
    attempt_row = connection.execute(
        """
        SELECT attempt_id, item_id, state, branch, base_revision,
               candidate_revision, candidate_recorded_at, subject_revision
        FROM attempts WHERE attempt_id = ?
        """,
        (attempt_id,),
    ).fetchone()
    if attempt_row is None:
        return None
    attempt = decode_row(attempt_row, CandidateSnapshotAttemptRow)
    if attempt.candidate_revision is None or attempt.candidate_recorded_at is None:
        return None
    artifact_key = candidate_snapshots.candidate_snapshot_artifact_key(
        str(attempt.attempt_id),
        attempt.candidate_revision,
        attempt.candidate_recorded_at.isoformat(),
    )
    reference = read_latest_artifact_reference(
        connection,
        work_models.ArtifactKind.EVIDENCE,
        artifact_key,
    )
    if reference is None:
        history_row = connection.execute(
            "SELECT history_id FROM transition_history WHERE project_revision = ?",
            (attempt.subject_revision,),
        ).fetchone()
        receipt = (
            None
            if history_row is None
            else sqlite_state.read_history_receipt(connection, decode_row(history_row, HistoryIdRow).history_id)
        )
        try:
            legacy_candidate = None if receipt is None else candidate_snapshots.legacy_review_candidate(receipt)
        except ValueError as error:
            raise StorageError(StorageErrorCode.INVALID_STATE, str(error)) from error
        if (
            receipt is not None
            and receipt.committed_at == attempt.candidate_recorded_at
            and legacy_candidate == attempt.candidate_revision
        ):
            return None
        raise StorageError(
            StorageErrorCode.INVALID_STATE,
            "The protected candidate has no accepted snapshot artifact.",
        )
    history_row = connection.execute(
        """
        SELECT history_id FROM transition_history
        WHERE project_revision = ?
        """,
        (reference.accepted_revision,),
    ).fetchone()
    if history_row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Candidate snapshot receipt is missing.")
    history_id = decode_row(history_row, HistoryIdRow).history_id
    receipt = sqlite_state.read_history_receipt(connection, history_id)
    if receipt is None or receipt.artifact_ref_id is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Candidate snapshot receipt is incomplete.")
    if receipt.artifact_ref_id != reference.artifact_ref_id:
        raise StorageError(
            StorageErrorCode.INVALID_STATE,
            "Candidate snapshot receipt names a different artifact reference.",
        )
    return query_models.CandidateSnapshotContextFacts(
        attempt.attempt_id,
        attempt.item_id,
        attempt.state,
        attempt.branch,
        attempt.base_revision,
        attempt.candidate_revision,
        attempt.candidate_recorded_at,
        receipt,
        reference,
    )


class SQLiteWorkStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def validated_snapshot(self) -> stored_state.StoredWorkState:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                verify_database_integrity(connection)
                return sqlite_state.read_state(connection)
        finally:
            connection.close()

    def read_project_export_batches(self) -> tuple[ProjectExportState, ...]:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                return (sqlite_state.read_project_export_state(connection),)
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
                            decode_row(row, AttemptIdRow).attempt_id
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
                count_rows = tuple(
                    decode_row(row, StateCountRow)
                    for row in connection.execute(
                        """
                        SELECT state, item_count
                        FROM work_item_state_counts
                        WHERE state IN (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ORDER BY state
                        """,
                        tuple(state.value for state in stored_state.StoredWorkItemState),
                    ).fetchall()
                )
                if len(count_rows) != len(stored_state.StoredWorkItemState):
                    raise StorageError(StorageErrorCode.INVALID_STATE, "Work-item state counts are incomplete.")
                visible_counts = {
                    selected.state: selected.item_count for selected in count_rows if selected.item_count > 0
                }
                released_intake = visible_counts.pop(stored_state.StoredWorkItemState.INTAKE.value, 0)
                if released_intake:
                    ready = stored_state.StoredWorkItemState.READY.value
                    visible_counts[ready] = visible_counts.get(ready, 0) + released_intake
                counts = tuple(sorted(visible_counts.items()))
                revision = decode_row(project_row, ProjectRevisionRow).revision
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

    def read_latest_artifact_reference(
        self, kind: work_models.ArtifactKind, key: str
    ) -> stored_state.ArtifactReference | None:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                return read_latest_artifact_reference(connection, kind, key)
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

    def read_leased_action_snapshot(
        self,
        role: decision_models.Role,
        lease_id: LeaseId,
        generation: int,
        now: datetime,
    ) -> LedgerSnapshot:
        """Read only current subjects reached by one supplied lease identity."""

        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                match role:
                    case decision_models.Role.WORKER:
                        attempt_ids = tuple(
                            decode_row(row, AttemptIdRow).attempt_id
                            for row in connection.execute(
                                """
                                SELECT anchor.attempt_id
                                FROM attempt_lease_generations AS anchor
                                JOIN attempt_leases AS lease
                                  ON lease.attempt_id = anchor.attempt_id
                                 AND lease.generation = anchor.generation
                                WHERE anchor.lease_id = ? AND anchor.generation = ?
                                ORDER BY anchor.attempt_id
                                """,
                                (lease_id, generation),
                            ).fetchall()
                        )
                        scope = query_models.DecisionScope((), (), (), (), attempt_ids, (), (), ())
                    case decision_models.Role.PREPARER:
                        item_ids = tuple(
                            decode_row(row, ItemIdRow).item_id
                            for row in connection.execute(
                                """
                                SELECT anchor.item_id
                                FROM preparation_lease_generations AS anchor
                                JOIN preparation_leases AS lease
                                  ON lease.item_id = anchor.item_id
                                 AND lease.generation = anchor.generation
                                WHERE anchor.lease_id = ? AND anchor.generation = ?
                                ORDER BY anchor.item_id
                                """,
                                (lease_id, generation),
                            ).fetchall()
                        )
                        scope = query_models.DecisionScope(item_ids, (), (), (), (), (), (), ())
                    case decision_models.Role.PROJECT | decision_models.Role.OBSERVER:
                        raise StorageError(
                            StorageErrorCode.INVARIANT_VIOLATION,
                            "Only worker and preparer action discovery accepts lease selection.",
                        )
                    case _ as unreachable:
                        assert_never(unreachable)
                return read_selected_decision_facts(connection, scope, now).snapshot
        finally:
            connection.close()

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
                return _read_generated_view_facts(connection, item_ids, attempt_ids, history_ids, now)
        finally:
            connection.close()

    def read_all_generated_view_facts(self, now: datetime) -> query_models.GeneratedViewFacts:
        """Read every declared generated projection, excluding unrelated stored state."""

        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                item_ids = tuple(
                    decode_row(row, ItemIdRow).item_id
                    for row in connection.execute("SELECT item_id FROM work_items ORDER BY item_id").fetchall()
                )
                attempt_ids = tuple(
                    decode_row(row, AttemptIdRow).attempt_id
                    for row in connection.execute("SELECT attempt_id FROM attempts ORDER BY attempt_id").fetchall()
                )
                history_ids = tuple(
                    decode_row(row, HistoryIdRow).history_id
                    for row in connection.execute(
                        "SELECT history_id FROM transition_history ORDER BY history_id"
                    ).fetchall()
                )
                return _read_generated_view_facts(connection, item_ids, attempt_ids, history_ids, now)
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
                return _read_attempt_context_facts(connection, attempt_id)
        finally:
            connection.close()

    def read_candidate_snapshot_context(
        self, attempt_id: AttemptId
    ) -> query_models.CandidateSnapshotContextFacts | None:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                return _read_candidate_snapshot_context_facts(connection, attempt_id)
        finally:
            connection.close()

    def read_review_job_context(
        self,
        attempt_id: AttemptId,
        checkpoint_history_id: HistoryId | None,
        correction_history_id: HistoryId | None,
        result_sha256: str | None,
        review_sha256: str | None,
    ) -> query_models.ReviewJobContextFacts | None:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                attempt = _read_attempt_context_facts(connection, attempt_id)
                if attempt is None:
                    return None
                candidate_snapshot = _read_candidate_snapshot_context_facts(connection, attempt_id)
                candidate_review_reference = None
                if (
                    isinstance(attempt, query_models.NonterminalAttemptContextFacts)
                    and attempt.candidate_revision is not None
                    and candidate_snapshot is not None
                    and result_sha256 is not None
                    and review_sha256 is not None
                ):
                    candidate_review_reference = read_artifact_reference(
                        connection,
                        work_models.ArtifactKind.EVIDENCE,
                        work_briefs.candidate_review_key(
                            str(attempt.attempt_id),
                            attempt.candidate_revision,
                            candidate_snapshot.reference.content_sha256,
                            attempt.brief_reference.content_sha256,
                            result_sha256,
                            review_sha256,
                        ),
                        1,
                    )
                checkpoint_receipt = (
                    None
                    if checkpoint_history_id is None
                    else sqlite_state.read_history_receipt(connection, checkpoint_history_id)
                )
                checkpoint_package_reference = (
                    None
                    if checkpoint_receipt is None or checkpoint_receipt.artifact_ref_id is None
                    else read_artifact_reference_by_id(connection, checkpoint_receipt.artifact_ref_id)
                )
                checkpoint_candidate_reference = None
                if checkpoint_receipt is not None and checkpoint_receipt.outcome_schema == "checkpoint-acceptance/v2":
                    try:
                        checkpoint_outcome = msgspec.json.decode(
                            bytes(checkpoint_receipt.outcome_payload),
                            type=history.CheckpointAcceptanceOutcome,
                            strict=True,
                        )
                    except msgspec.DecodeError:
                        pass
                    else:
                        checkpoint_candidate_reference = read_artifact_reference(
                            connection,
                            work_models.ArtifactKind.EVIDENCE,
                            f"{attempt_id}-{checkpoint_outcome.checkpoint}-candidate",
                            1,
                        )
                correction_receipt = (
                    None
                    if correction_history_id is None
                    else sqlite_state.read_history_receipt(connection, correction_history_id)
                )
                return query_models.ReviewJobContextFacts(
                    attempt,
                    candidate_snapshot,
                    candidate_review_reference,
                    checkpoint_receipt,
                    checkpoint_package_reference,
                    checkpoint_candidate_reference,
                    correction_receipt,
                )
        finally:
            connection.close()

    def read_completion_context(self, attempt_id: AttemptId) -> query_models.CompletionContextFacts | None:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                attempt = _read_attempt_context_facts(connection, attempt_id)
                if attempt is None:
                    return None
                rows = connection.execute(
                    """
                    SELECT history_id FROM transition_history
                    WHERE subject_id = ? AND outcome_schema = 'checkpoint-acceptance/v2'
                    ORDER BY history_id
                    """,
                    (attempt_id,),
                ).fetchall()
                checkpoints: list[query_models.CompletionCheckpointFacts] = []
                for row in rows:
                    history_id = decode_row(row, HistoryIdRow).history_id
                    receipt = sqlite_state.read_history_receipt(connection, history_id)
                    if receipt is None:
                        raise StorageError(StorageErrorCode.INVALID_STATE, "Completion history disappeared.")
                    reference = (
                        None
                        if receipt.artifact_ref_id is None
                        else read_artifact_reference_by_id(connection, receipt.artifact_ref_id)
                    )
                    checkpoints.append(query_models.CompletionCheckpointFacts(receipt, reference))
                return query_models.CompletionContextFacts(attempt, tuple(checkpoints))
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

    def write(self) -> SQLiteWorkTransaction:
        return SQLiteWorkTransaction(self._path)

    def accept_artifact_reference(
        self,
        work_root: Path,
        published: ArtifactRef,
        accepted_at: datetime,
    ) -> DecisionResult[ArtifactReferenceAcceptance]:
        return persist_artifact_reference(self._path, work_root, published, accepted_at)
