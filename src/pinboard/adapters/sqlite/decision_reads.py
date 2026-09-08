"""Read current decision facts without assembling retained SQLite history."""

import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime

import msgspec

from pinboard.adapters.sqlite.database import decode_row
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import read_current_definition
from pinboard.adapters.sqlite.proposals import decode_proposal_relation
from pinboard.application import query_models, stored_state
from pinboard.domain import authority_models, work_models
from pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    HostId,
    ItemId,
    LeaseId,
    ProposalId,
    TaskId,
)
from pinboard.domain.ledger import LedgerSnapshot


class _ProjectRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: int
    host_epoch: int


class _ItemRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: ItemId
    state: stored_state.StoredWorkItemState
    timing: work_models.Timing | None
    source: str | None
    outcome_evidence: str | None
    next_action: str | None
    notes: str | None
    subject_revision: int
    queue_position: int | None


class _AttemptRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    item_id: ItemId
    state: work_models.AttemptState
    accepted_scope_revision: int
    accepted_scope_digest: str
    candidate_revision: str | None
    brief_artifact_ref_id: ArtifactRefId
    result_artifact_ref_id: ArtifactRefId | None
    subject_revision: int


class _AttemptLineageRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    item_id: ItemId
    state: work_models.AttemptState
    branch: str
    base_revision: str
    accepted_scope_revision: int
    accepted_scope_digest: str
    candidate_revision: str | None
    brief_artifact_ref_id: ArtifactRefId
    result_artifact_ref_id: ArtifactRefId | None
    subject_revision: int


class _DependencyRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: ItemId
    dependency_id: ItemId


class _ItemIdRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: ItemId


class _ArtifactRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    artifact_ref_id: ArtifactRefId
    kind: work_models.ArtifactKind


class _ProposalRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    proposal_id: ProposalId
    created_at: datetime
    source_task_id: TaskId
    user_label: str
    trigger: str
    why_it_matters: str
    relation_kind: work_models.ProposalRelationKind
    relation_item_id: ItemId | None
    effect: str
    unlock: str
    urgency_evidence: str
    subject_revision: int


class _ProposalTextRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    proposal_id: ProposalId
    value: str


class _AttemptLeaseRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    item_id: ItemId
    item_subject_revision: int
    attempt_subject_revision: int
    generation: int
    lease_id: LeaseId
    task_id: TaskId
    host_id: HostId
    expires_at: datetime
    status: authority_models.AttemptLeaseStatus


class _PreparationLeaseRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: ItemId
    definition_revision: int
    definition_digest: str
    generation: int
    lease_id: LeaseId
    task_id: TaskId
    host_id: HostId
    acquired_at: datetime
    expires_at: datetime
    status: authority_models.PreparationLeaseStatus


def _attempt_row_key(value: _AttemptRow) -> str:
    return str(value.attempt_id)


def _item_row_identity_key(value: _ItemRow) -> str:
    return str(value.item_id)


def _project(connection: sqlite3.Connection) -> _ProjectRow:
    row = connection.execute("SELECT revision, host_epoch FROM project_meta WHERE singleton = 1").fetchone()
    if row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Project metadata is missing.")
    return decode_row(row, _ProjectRow)


def _read_attempt_lease(connection: sqlite3.Connection, attempt_id: AttemptId) -> _AttemptLeaseRow | None:
    row = connection.execute(
        """
        SELECT lease.attempt_id, attempt.item_id, item.subject_revision AS item_subject_revision,
               attempt.subject_revision AS attempt_subject_revision, lease.generation,
               anchor.lease_id, anchor.task_id, anchor.host_id, lease.expires_at, lease.status
        FROM attempt_leases AS lease
        JOIN attempt_lease_generations AS anchor
          ON anchor.attempt_id = lease.attempt_id AND anchor.generation = lease.generation
        JOIN attempts AS attempt ON attempt.attempt_id = lease.attempt_id
        JOIN work_items AS item ON item.item_id = attempt.item_id
        WHERE lease.attempt_id = ?
        """,
        (attempt_id,),
    ).fetchone()
    return None if row is None else decode_row(row, _AttemptLeaseRow)


def _project_attempt_authority(
    selected: _AttemptLeaseRow, host_epoch: int, now: datetime
) -> tuple[work_models.AttemptAuthority, work_models.CommandAttemptAuthority | None]:
    active = selected.status == authority_models.AttemptLeaseStatus.ACTIVE
    retained = work_models.AttemptAuthority(
        selected.attempt_id,
        selected.item_id,
        selected.lease_id if active else None,
        selected.generation,
    )
    if not active or selected.expires_at <= now:
        return retained, None
    return retained, work_models.CommandAttemptAuthority(
        host_epoch,
        selected.item_id,
        str(selected.item_subject_revision),
        selected.attempt_id,
        str(selected.attempt_subject_revision),
        selected.task_id,
        selected.host_id,
        selected.lease_id,
        selected.generation,
        selected.expires_at,
    )


def _read_preparation_lease(connection: sqlite3.Connection, item_id: ItemId) -> _PreparationLeaseRow | None:
    row = connection.execute(
        """
        SELECT lease.item_id, lease.definition_revision, lease.definition_digest,
               lease.generation, anchor.lease_id, anchor.task_id, anchor.host_id,
               lease.acquired_at, lease.expires_at, lease.status
        FROM preparation_leases AS lease
        JOIN preparation_lease_generations AS anchor
          ON anchor.item_id = lease.item_id AND anchor.generation = lease.generation
        WHERE lease.item_id = ?
        """,
        (item_id,),
    ).fetchone()
    return None if row is None else decode_row(row, _PreparationLeaseRow)


def _project_preparation_authority(
    selected: _PreparationLeaseRow, host_epoch: int, now: datetime
) -> tuple[work_models.PreparationAuthority, work_models.PreparationCommandAuthority | None]:
    active = selected.status == authority_models.PreparationLeaseStatus.ACTIVE
    current = active and selected.expires_at > now
    retained = work_models.PreparationAuthority(
        selected.item_id,
        selected.definition_revision,
        selected.definition_digest,
        selected.lease_id if current else None,
        selected.generation,
    )
    if not current:
        return retained, None
    return retained, work_models.PreparationCommandAuthority(
        host_epoch,
        selected.item_id,
        selected.definition_revision,
        selected.definition_digest,
        selected.task_id,
        selected.host_id,
        selected.lease_id,
        selected.generation,
        selected.expires_at,
    )


def _read_attempt_authorities(
    connection: sqlite3.Connection,
    attempt_ids: Iterable[AttemptId],
    host_epoch: int,
    now: datetime,
) -> tuple[tuple[work_models.AttemptAuthority, ...], tuple[work_models.CommandAttemptAuthority, ...]]:
    retained_authorities: list[work_models.AttemptAuthority] = []
    command_authorities: list[work_models.CommandAttemptAuthority] = []
    for attempt_id in attempt_ids:
        selected = _read_attempt_lease(connection, attempt_id)
        if selected is None:
            continue
        retained, command = _project_attempt_authority(selected, host_epoch, now)
        retained_authorities.append(retained)
        if command is not None:
            command_authorities.append(command)
    return tuple(retained_authorities), tuple(command_authorities)


def _read_preparation_authorities(
    connection: sqlite3.Connection,
    item_ids: Iterable[ItemId],
    host_epoch: int,
    now: datetime,
) -> tuple[tuple[work_models.PreparationAuthority, ...], tuple[work_models.PreparationCommandAuthority, ...]]:
    retained_authorities: list[work_models.PreparationAuthority] = []
    command_authorities: list[work_models.PreparationCommandAuthority] = []
    for item_id in item_ids:
        selected = _read_preparation_lease(connection, item_id)
        if selected is None:
            continue
        retained, command = _project_preparation_authority(selected, host_epoch, now)
        retained_authorities.append(retained)
        if command is not None:
            command_authorities.append(command)
    return tuple(retained_authorities), tuple(command_authorities)


def _work_item_record(
    item: _ItemRow,
    dependencies: tuple[ItemId, ...],
    attempt_id: AttemptId | None,
) -> work_models.WorkItem:
    return work_models.WorkItem(
        item.item_id,
        work_models.WorkState(item.state.value),
        None if item.timing is None else item.timing.value,
        dependencies,
        attempt_id,
        item.source,
        item.next_action,
        item.notes,
        item.queue_position,
        item.outcome_evidence,
    )


def _attempt_record(attempt: _AttemptRow | _AttemptLineageRow) -> work_models.AttemptRecord:
    return work_models.AttemptRecord(
        attempt.attempt_id,
        attempt.item_id,
        attempt.state,
        attempt.accepted_scope_revision,
        attempt.accepted_scope_digest,
        None if attempt.candidate_revision is None else CandidateId(attempt.candidate_revision),
        attempt.brief_artifact_ref_id,
    )


def _proposal_record(
    proposal: _ProposalRow,
    evidence: tuple[str, ...],
    freshness: tuple[str, ...],
) -> work_models.ProposalRecord:
    return work_models.ProposalRecord(
        proposal.proposal_id,
        str(proposal.subject_revision),
        proposal.created_at,
        proposal.source_task_id,
        proposal.user_label,
        proposal.trigger,
        proposal.why_it_matters,
        decode_proposal_relation(proposal.relation_kind, proposal.relation_item_id),
        proposal.effect,
        proposal.unlock,
        proposal.urgency_evidence,
        evidence,
        freshness,
    )


def read_current_snapshot(
    connection: sqlite3.Connection,
    now: datetime,
    *,
    include_proposals: bool,
    include_action_authorities: bool,
) -> LedgerSnapshot:
    """Return only facts whose current meaning can affect ordinary project decisions."""

    project = _project(connection)
    item_rows = tuple(
        sorted(
            (
                decode_row(row, _ItemRow)
                for row in connection.execute(
                    """
                    SELECT item_id, state, timing, source, outcome_evidence, next_action, notes,
                           subject_revision, queue_position
                    FROM work_items
                    WHERE queue_position IS NOT NULL
                    ORDER BY queue_position
                    """
                ).fetchall()
            ),
            key=_item_row_identity_key,
        )
    )
    live_item_ids = {row.item_id for row in item_rows}
    attempt_rows = tuple(
        sorted(
            (
                decode_row(row, _AttemptRow)
                for row in connection.execute(
                    """
                    SELECT attempt_id, item_id, state, accepted_scope_revision, accepted_scope_digest,
                           candidate_revision, brief_artifact_ref_id, result_artifact_ref_id, subject_revision
                    FROM attempts INDEXED BY one_live_attempt_per_item
                    WHERE state != 'done'
                    """
                ).fetchall()
            ),
            key=_attempt_row_key,
        )
    )
    attempts_by_item = {row.item_id: row.attempt_id for row in attempt_rows}
    dependency_rows = tuple(
        decode_row(row, _DependencyRow)
        for item in item_rows
        for row in connection.execute(
            """
            SELECT item_id, dependency_id FROM item_dependencies
            WHERE item_id = ? ORDER BY position
            """,
            (item.item_id,),
        ).fetchall()
    )
    dependencies: dict[ItemId, list[ItemId]] = defaultdict(list)
    for row in dependency_rows:
        dependencies[row.item_id].append(row.dependency_id)
    definitions = tuple(
        definition
        for item in item_rows
        if (definition := read_current_definition(connection, item.item_id)) is not None
    )
    if {value.item_id for value in definitions} != live_item_ids:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Every current work item must have a current definition.")

    proposal_rows = (
        tuple(
            proposal
            for item in item_rows
            for item_id in (item.item_id,)
            if (proposal := _read_selected_proposal(connection, ProposalId(item_id))) is not None
        )
        if include_proposals
        else ()
    )
    evidence: dict[ProposalId, list[str]] = defaultdict(list)
    freshness: dict[ProposalId, list[str]] = defaultdict(list)
    for proposal in proposal_rows:
        for row in connection.execute(
            "SELECT proposal_id, selector AS value FROM proposal_evidence WHERE proposal_id = ? ORDER BY position",
            (proposal.proposal_id,),
        ).fetchall():
            selected = decode_row(row, _ProposalTextRow)
            evidence[selected.proposal_id].append(selected.value)
        for row in connection.execute(
            "SELECT proposal_id, assumption AS value FROM proposal_freshness WHERE proposal_id = ? ORDER BY position",
            (proposal.proposal_id,),
        ).fetchall():
            selected = decode_row(row, _ProposalTextRow)
            freshness[selected.proposal_id].append(selected.value)

    attempt_authorities: tuple[work_models.AttemptAuthority, ...] = ()
    command_attempt_authorities: tuple[work_models.CommandAttemptAuthority, ...] = ()
    preparation_authorities: tuple[work_models.PreparationAuthority, ...] = ()
    command_preparation_authorities: tuple[work_models.PreparationCommandAuthority, ...] = ()
    if include_action_authorities:
        attempt_authorities, command_attempt_authorities = _read_attempt_authorities(
            connection, (attempt.attempt_id for attempt in attempt_rows), project.host_epoch, now
        )
        preparation_authorities, command_preparation_authorities = _read_preparation_authorities(
            connection, (item.item_id for item in item_rows), project.host_epoch, now
        )

    history_items = tuple(
        dict.fromkeys(
            item_id
            for item_id in (
                *(row.dependency_id for row in dependency_rows),
                *(proposal.relation_item_id for proposal in proposal_rows if proposal.relation_item_id is not None),
            )
            if item_id not in live_item_ids
        )
    )

    return LedgerSnapshot(
        revision=str(project.revision),
        items=tuple(
            _work_item_record(item, tuple(dependencies[item.item_id]), attempts_by_item.get(item.item_id))
            for item in item_rows
        ),
        attempts=tuple(_attempt_record(attempt) for attempt in attempt_rows),
        artifacts=(),
        proposals=tuple(
            _proposal_record(
                proposal,
                tuple(evidence[proposal.proposal_id]),
                tuple(freshness[proposal.proposal_id]),
            )
            for proposal in proposal_rows
        ),
        subject_revisions=tuple(
            work_models.SubjectRevision(item.item_id, str(item.subject_revision)) for item in item_rows
        )
        + tuple(
            work_models.SubjectRevision(attempt.attempt_id, str(attempt.subject_revision)) for attempt in attempt_rows
        )
        + tuple(
            work_models.SubjectRevision(proposal.proposal_id, str(proposal.subject_revision))
            for proposal in proposal_rows
        ),
        attempt_authorities=attempt_authorities,
        command_attempt_authorities=command_attempt_authorities,
        preparation_authorities=preparation_authorities,
        command_preparation_authorities=command_preparation_authorities,
        history_items=history_items,
        definitions=tuple(
            work_models.DefinitionAnchor(value.item_id, value.revision, value.digest, value.definition)
            for value in definitions
        ),
        host_epoch=project.host_epoch,
    )


def _read_selected_item(connection: sqlite3.Connection, item_id: ItemId) -> _ItemRow | None:
    row = connection.execute(
        """
        SELECT item_id, state, timing, source, outcome_evidence, next_action, notes,
               subject_revision, queue_position
        FROM work_items WHERE item_id = ?
        """,
        (item_id,),
    ).fetchone()
    return None if row is None else decode_row(row, _ItemRow)


def _read_selected_dependencies(connection: sqlite3.Connection, item_id: ItemId) -> tuple[ItemId, ...]:
    return tuple(
        decode_row(row, _DependencyRow).dependency_id
        for row in connection.execute(
            "SELECT item_id, dependency_id FROM item_dependencies WHERE item_id = ? ORDER BY position",
            (item_id,),
        ).fetchall()
    )


def _read_required_definition(connection: sqlite3.Connection, item_id: ItemId) -> stored_state.ItemDefinitionRevision:
    definition = read_current_definition(connection, item_id)
    if definition is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Every selected work item must have a current definition.")
    return definition


def _read_selected_attempt(connection: sqlite3.Connection, attempt_id: AttemptId) -> _AttemptLineageRow | None:
    row = connection.execute(
        """
        SELECT attempt_id, item_id, state, branch, base_revision, accepted_scope_revision,
               accepted_scope_digest, candidate_revision, brief_artifact_ref_id,
               result_artifact_ref_id, subject_revision
        FROM attempts WHERE attempt_id = ?
        """,
        (attempt_id,),
    ).fetchone()
    return None if row is None else decode_row(row, _AttemptLineageRow)


def _read_selected_proposal(connection: sqlite3.Connection, proposal_id: ProposalId) -> _ProposalRow | None:
    row = connection.execute(
        """
        SELECT proposal_id, created_at, source_task_id, user_label, trigger, why_it_matters,
               relation_kind, relation_item_id, effect, unlock, urgency_evidence, subject_revision
        FROM proposals WHERE proposal_id = ? AND disposition IS NULL
        """,
        (proposal_id,),
    ).fetchone()
    return None if row is None else decode_row(row, _ProposalRow)


def read_selected_decision_facts(  # noqa: C901, PLR0912, PLR0915
    connection: sqlite3.Connection,
    scope: query_models.DecisionScope,
    now: datetime,
) -> query_models.DecisionFacts:
    """Read only the explicitly selected relationships needed by one decision."""

    project = _project(connection)
    attempts: dict[AttemptId, _AttemptLineageRow] = {}
    primary_item_ids = list(scope.item_ids)
    for attempt_id in scope.attempt_ids:
        attempt = _read_selected_attempt(connection, attempt_id)
        if attempt is not None:
            attempts[attempt.attempt_id] = attempt
            primary_item_ids.append(attempt.item_id)

    proposals: dict[ProposalId, _ProposalRow] = {}
    for proposal_id in scope.proposal_ids:
        proposal = _read_selected_proposal(connection, proposal_id)
        if proposal is not None:
            proposals[proposal.proposal_id] = proposal
            primary_item_ids.append(ItemId(proposal.proposal_id))

    item_rows: dict[ItemId, _ItemRow] = {}
    history_items: list[ItemId] = []
    dependencies: dict[ItemId, list[ItemId]] = defaultdict(list)
    definitions: dict[ItemId, stored_state.ItemDefinitionRevision] = {}
    contextual_item_ids: set[ItemId] = set()
    dependency_item_ids: set[ItemId] = set()

    def read_item(
        item_id: ItemId,
        *,
        include_dependencies: bool,
        include_definition: bool,
        context: bool,
    ) -> _ItemRow | None:
        item = item_rows.get(item_id)
        if item is None:
            item = _read_selected_item(connection, item_id)
        if item is None:
            return None
        live = stored_state.live_work_state(item.state) is not None
        if live:
            item_rows[item_id] = item
        else:
            history_items.append(item_id)
        if include_dependencies and item_id not in dependency_item_ids:
            dependencies[item_id].extend(_read_selected_dependencies(connection, item_id))
            dependency_item_ids.add(item_id)
        if include_definition and item_id not in definitions:
            definitions[item_id] = _read_required_definition(connection, item_id)
        if (
            item_id in dependency_item_ids
            and item_id in definitions
            and tuple(dependencies[item_id]) != definitions[item_id].definition.dependencies
        ):
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                "Current definition dependencies do not match relational dependencies.",
            )
        if context and live:
            contextual_item_ids.add(item_id)
        return item

    for item_id in dict.fromkeys(primary_item_ids):
        read_item(item_id, include_dependencies=True, include_definition=True, context=True)

    pending_closure = list(scope.dependency_closure_roots)
    visited_closure: set[ItemId] = set()
    while pending_closure:
        item_id = pending_closure.pop()
        if item_id in visited_closure:
            continue
        visited_closure.add(item_id)
        selected = read_item(item_id, include_dependencies=True, include_definition=True, context=False)
        if selected is not None:
            pending_closure.extend(dependencies[item_id])

    related_item_ids = list(scope.related_item_ids)
    for item_id in contextual_item_ids:
        related_item_ids.extend(dependencies[item_id])
    for item_id in scope.live_dependent_roots:
        for row in connection.execute(
            """
            SELECT dependency.item_id
            FROM item_dependencies AS dependency INDEXED BY item_dependencies_by_dependency
            JOIN work_items AS item ON item.item_id = dependency.item_id
            WHERE dependency.dependency_id = ? AND item.queue_position IS NOT NULL
            ORDER BY dependency.item_id
            """,
            (item_id,),
        ).fetchall():
            dependent_id = decode_row(row, _ItemIdRow).item_id
            read_item(dependent_id, include_dependencies=True, include_definition=False, context=False)
    for item_id in dict.fromkeys(related_item_ids):
        read_item(item_id, include_dependencies=False, include_definition=False, context=False)

    for item_id in contextual_item_ids:
        linked_attempt = connection.execute(
            """
            SELECT attempt_id, item_id, state, branch, base_revision, accepted_scope_revision,
                   accepted_scope_digest, candidate_revision, brief_artifact_ref_id,
                   result_artifact_ref_id, subject_revision
            FROM attempts WHERE item_id = ? AND state != 'done'
            """,
            (item_id,),
        ).fetchone()
        if linked_attempt is not None:
            selected_attempt = decode_row(linked_attempt, _AttemptLineageRow)
            attempts[selected_attempt.attempt_id] = selected_attempt

    evidence: dict[ProposalId, list[str]] = defaultdict(list)
    freshness: dict[ProposalId, list[str]] = defaultdict(list)
    for proposal_id in proposals:
        for row in connection.execute(
            "SELECT proposal_id, selector AS value FROM proposal_evidence WHERE proposal_id = ? ORDER BY position",
            (proposal_id,),
        ).fetchall():
            selected = decode_row(row, _ProposalTextRow)
            evidence[proposal_id].append(selected.value)
        for row in connection.execute(
            "SELECT proposal_id, assumption AS value FROM proposal_freshness WHERE proposal_id = ? ORDER BY position",
            (proposal_id,),
        ).fetchall():
            selected = decode_row(row, _ProposalTextRow)
            freshness[proposal_id].append(selected.value)

    artifacts: list[work_models.ArtifactRecord] = []
    for artifact_id in scope.artifact_ref_ids:
        row = connection.execute(
            "SELECT artifact_ref_id, kind FROM artifact_refs WHERE artifact_ref_id = ?",
            (artifact_id,),
        ).fetchone()
        if row is not None:
            selected = decode_row(row, _ArtifactRow)
            artifacts.append(work_models.ArtifactRecord(selected.artifact_ref_id, selected.kind))

    attempt_authorities, command_attempt_authorities = _read_attempt_authorities(
        connection, (attempt.attempt_id for attempt in attempts.values()), project.host_epoch, now
    )
    preparation_authorities, command_preparation_authorities = _read_preparation_authorities(
        connection, contextual_item_ids, project.host_epoch, now
    )

    attempts_by_item = {
        attempt.item_id: attempt.attempt_id
        for attempt in attempts.values()
        if attempt.state != work_models.AttemptState.DONE
    }
    proposal_records = tuple(
        _proposal_record(
            proposal,
            tuple(evidence[proposal.proposal_id]),
            tuple(freshness[proposal.proposal_id]),
        )
        for proposal in proposals.values()
    )
    snapshot = LedgerSnapshot(
        revision=str(project.revision),
        items=tuple(
            _work_item_record(item, tuple(dependencies[item.item_id]), attempts_by_item.get(item.item_id))
            for item in item_rows.values()
        ),
        attempts=tuple(_attempt_record(attempt) for attempt in attempts.values()),
        artifacts=tuple(artifacts),
        proposals=proposal_records,
        subject_revisions=tuple(
            work_models.SubjectRevision(item.item_id, str(item.subject_revision)) for item in item_rows.values()
        )
        + tuple(
            work_models.SubjectRevision(attempt.attempt_id, str(attempt.subject_revision))
            for attempt in attempts.values()
        )
        + tuple(
            work_models.SubjectRevision(proposal.proposal_id, str(proposal.subject_revision))
            for proposal in proposals.values()
        ),
        attempt_authorities=attempt_authorities,
        command_attempt_authorities=command_attempt_authorities,
        preparation_authorities=preparation_authorities,
        command_preparation_authorities=command_preparation_authorities,
        history_items=tuple(dict.fromkeys(history_items)),
        definitions=tuple(
            work_models.DefinitionAnchor(value.item_id, value.revision, value.digest, value.definition)
            for value in definitions.values()
        ),
        host_epoch=project.host_epoch,
    )
    return query_models.DecisionFacts(
        snapshot,
        tuple(
            query_models.AttemptLineage(value.attempt_id, value.item_id, value.branch, value.base_revision)
            for value in attempts.values()
        ),
    )
