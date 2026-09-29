"""Read current decision facts without assembling retained SQLite history."""

import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime

import msgspec

from pinboard.adapters.sqlite.database import decode_row, select_by_ids
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import (
    read_current_definitions,
    read_pause_reasons,
    validate_current_attempt_relation,
)
from pinboard.adapters.sqlite.proposals import decode_proposal_relation
from pinboard.application import query_models, stored_state
from pinboard.domain import authority_models, work_models
from pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    HistoryId,
    HostId,
    LeaseId,
    ProposalId,
    TaskId,
    WorkItemId,
)
from pinboard.domain.ledger import LedgerSnapshot


class _ProjectRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: int
    host_epoch: int


class _ItemRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
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
    item_id: WorkItemId
    state: work_models.AttemptState
    accepted_scope_revision: int
    accepted_scope_digest: str
    candidate_revision: str | None
    brief_artifact_ref_id: ArtifactRefId
    result_artifact_ref_id: ArtifactRefId | None
    subject_revision: int


class _AttemptLineageRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    item_id: WorkItemId
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
    item_id: WorkItemId
    dependency_id: WorkItemId


class _ItemIdRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId


class _HistoryIdRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: HistoryId


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
    relation_item_id: WorkItemId | None
    relation_replacement_cost: str | None
    effect: str
    unlock: str
    urgency_evidence: str
    subject_revision: int


class _ProposalTextRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    proposal_id: ProposalId
    value: str


class _AttemptLeaseRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    item_id: WorkItemId
    item_subject_revision: int
    attempt_subject_revision: int
    generation: int
    lease_id: LeaseId
    task_id: TaskId
    host_id: HostId
    expires_at: datetime
    status: authority_models.AttemptLeaseStatus


class _PreparationLeaseRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    definition_revision: int
    definition_digest: str
    generation: int
    lease_id: LeaseId
    task_id: TaskId
    host_id: HostId
    acquired_at: datetime
    expires_at: datetime
    status: authority_models.PreparationLeaseStatus


class _PlannedReplacementRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    affected_item_id: WorkItemId
    relation_revision: int
    replacement_item_id: WorkItemId
    replacement_cost: str
    status: work_models.PlannedReplacementStatus
    recorded_by: TaskId
    recorded_at: datetime


class _ReplacementDispositionRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    affected_item_id: WorkItemId
    relation_revision: int
    rationale: str
    accepted_cost: str
    recorded_by: TaskId
    recorded_at: datetime


def _replacement_item_key(value: _PlannedReplacementRow) -> WorkItemId:
    return value.affected_item_id


def _attempt_row_key(value: _AttemptRow) -> str:
    return str(value.attempt_id)


def _item_row_identity_key(value: _ItemRow) -> str:
    return str(value.item_id)


def _project(connection: sqlite3.Connection) -> _ProjectRow:
    row = connection.execute("SELECT revision, host_epoch FROM project_meta WHERE singleton = 1").fetchone()
    if row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Project metadata is missing.")
    return decode_row(row, _ProjectRow)


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


def read_current_replacements(
    connection: sqlite3.Connection, item_ids: Iterable[WorkItemId]
) -> tuple[tuple[work_models.PlannedReplacement, ...], tuple[work_models.ReplacementDisposition, ...]]:
    selected_ids = tuple(dict.fromkeys(item_ids))
    if not selected_ids:
        return (), ()
    rows = tuple(
        sorted(
            (
                decode_row(row, _PlannedReplacementRow)
                for row in select_by_ids(
                    connection,
                    """
            SELECT relation.affected_item_id, relation.relation_revision,
                   relation.replacement_item_id, relation.replacement_cost,
                   relation.status, relation.recorded_by, relation.recorded_at
            FROM planned_replacements AS relation
            WHERE relation.affected_item_id IN ({ids})
              AND relation.relation_revision = (
                  SELECT MAX(candidate.relation_revision)
                  FROM planned_replacements AS candidate
                  WHERE candidate.affected_item_id = relation.affected_item_id
              )
            ORDER BY relation.affected_item_id
            """,
                    selected_ids,
                )
            ),
            key=_replacement_item_key,
        )
    )
    dispositions_by_key = {
        (value.affected_item_id, value.relation_revision): value
        for value in (
            decode_row(row, _ReplacementDispositionRow)
            for row in select_by_ids(
                connection,
                """
                SELECT disposition.affected_item_id, disposition.relation_revision,
                       disposition.rationale, disposition.accepted_cost,
                       disposition.recorded_by, disposition.recorded_at
                FROM replacement_dispositions AS disposition
                JOIN planned_replacements AS relation
                  ON relation.affected_item_id = disposition.affected_item_id
                 AND relation.relation_revision = disposition.relation_revision
                WHERE disposition.affected_item_id IN ({ids})
                  AND relation.relation_revision = (
                      SELECT MAX(candidate.relation_revision)
                      FROM planned_replacements AS candidate
                      WHERE candidate.affected_item_id = relation.affected_item_id
                  )
                ORDER BY disposition.affected_item_id, disposition.relation_revision
                """,
                selected_ids,
            )
        )
    }
    replacements = tuple(
        work_models.PlannedReplacement(
            value.affected_item_id,
            value.relation_revision,
            value.replacement_item_id,
            value.replacement_cost,
            value.status,
            value.recorded_by,
            value.recorded_at,
        )
        for value in rows
    )
    dispositions: list[work_models.ReplacementDisposition] = []
    for relation in rows:
        value = dispositions_by_key.get((relation.affected_item_id, relation.relation_revision))
        if value is None:
            continue
        if value.accepted_cost != relation.replacement_cost:
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                "A selected temporary-retention disposition does not accept its exact replacement cost.",
            )
        dispositions.append(
            work_models.ReplacementDisposition(
                value.affected_item_id,
                value.relation_revision,
                value.rationale,
                value.accepted_cost,
                value.recorded_by,
                value.recorded_at,
            )
        )
    return replacements, tuple(dispositions)


def _read_attempt_authorities(
    connection: sqlite3.Connection,
    attempt_ids: Iterable[AttemptId],
    host_epoch: int,
    now: datetime,
) -> tuple[tuple[work_models.AttemptAuthority, ...], tuple[work_models.CommandAttemptAuthority, ...]]:
    selected_ids = tuple(dict.fromkeys(attempt_ids))
    leases = {
        lease.attempt_id: lease
        for lease in (
            decode_row(row, _AttemptLeaseRow)
            for row in select_by_ids(
                connection,
                """SELECT lease.attempt_id, attempt.item_id, item.subject_revision AS item_subject_revision,
                          attempt.subject_revision AS attempt_subject_revision, lease.generation,
                          anchor.lease_id, anchor.task_id, anchor.host_id, lease.expires_at, lease.status
                   FROM attempt_leases AS lease
                   JOIN attempt_lease_generations AS anchor
                     ON anchor.attempt_id = lease.attempt_id AND anchor.generation = lease.generation
                   JOIN attempts AS attempt ON attempt.attempt_id = lease.attempt_id
                   JOIN work_items AS item ON item.item_id = attempt.item_id
                   WHERE lease.attempt_id IN ({ids})""",
                selected_ids,
            )
        )
    }
    retained_authorities: list[work_models.AttemptAuthority] = []
    command_authorities: list[work_models.CommandAttemptAuthority] = []
    for attempt_id in selected_ids:
        selected = leases.get(attempt_id)
        if selected is None:
            continue
        retained, command = _project_attempt_authority(selected, host_epoch, now)
        retained_authorities.append(retained)
        if command is not None:
            command_authorities.append(command)
    return tuple(retained_authorities), tuple(command_authorities)


def _read_preparation_authorities(
    connection: sqlite3.Connection,
    item_ids: Iterable[WorkItemId],
    host_epoch: int,
    now: datetime,
) -> tuple[tuple[work_models.PreparationAuthority, ...], tuple[work_models.PreparationCommandAuthority, ...]]:
    selected_ids = tuple(dict.fromkeys(item_ids))
    leases = {
        lease.item_id: lease
        for lease in (
            decode_row(row, _PreparationLeaseRow)
            for row in select_by_ids(
                connection,
                """SELECT lease.item_id, lease.definition_revision, lease.definition_digest,
                          lease.generation, anchor.lease_id, anchor.task_id, anchor.host_id,
                          lease.acquired_at, lease.expires_at, lease.status
                   FROM preparation_leases AS lease
                   JOIN preparation_lease_generations AS anchor
                     ON anchor.item_id = lease.item_id AND anchor.generation = lease.generation
                   WHERE lease.item_id IN ({ids})""",
                selected_ids,
            )
        )
    }
    retained_authorities: list[work_models.PreparationAuthority] = []
    command_authorities: list[work_models.PreparationCommandAuthority] = []
    for item_id in selected_ids:
        selected = leases.get(item_id)
        if selected is None:
            continue
        retained, command = _project_preparation_authority(selected, host_epoch, now)
        retained_authorities.append(retained)
        if command is not None:
            command_authorities.append(command)
    return tuple(retained_authorities), tuple(command_authorities)


def _work_item_record(
    item: _ItemRow,
    dependencies: tuple[WorkItemId, ...],
    attempt_id: AttemptId | None,
) -> work_models.WorkItem:
    state = stored_state.live_work_state(item.state)
    if state is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "A current work item has a terminal stored state.")
    return work_models.WorkItem(
        item.item_id,
        state,
        None if item.timing is None else item.timing.value,
        dependencies,
        attempt_id,
        item.source,
        item.next_action,
        item.notes,
        item.queue_position,
        item.outcome_evidence,
    )


def _attempt_records(
    connection: sqlite3.Connection, attempts: Iterable[_AttemptRow | _AttemptLineageRow]
) -> tuple[work_models.AttemptRecord, ...]:
    selected = tuple(attempts)
    pause_reasons = read_pause_reasons(
        connection, ((attempt.attempt_id, attempt.state, attempt.subject_revision) for attempt in selected)
    )
    return tuple(
        work_models.AttemptRecord(
            attempt.attempt_id,
            attempt.item_id,
            attempt.state,
            attempt.accepted_scope_revision,
            attempt.accepted_scope_digest,
            None if attempt.candidate_revision is None else CandidateId(attempt.candidate_revision),
            attempt.brief_artifact_ref_id,
            pause_reason=pause_reasons.get(attempt.attempt_id),
        )
        for attempt in selected
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
        decode_proposal_relation(proposal.relation_kind, proposal.relation_item_id, proposal.relation_replacement_cost),
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
    attempt_states = {row.item_id: row.state for row in attempt_rows}
    for item in item_rows:
        validate_current_attempt_relation(
            "read_current_snapshot",
            item.item_id,
            item.state,
            attempt_states.get(item.item_id),
            StorageErrorCode.INVALID_STATE,
        )
    for attempt in attempt_rows:
        if attempt.item_id not in live_item_ids:
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                f"read_current_snapshot: open attempt '{attempt.attempt_id}' has no live work item "
                f"'{attempt.item_id}'; expected one matching live item; effect unchanged.",
            )
    dependency_rows = tuple(
        decode_row(row, _DependencyRow)
        for row in select_by_ids(
            connection,
            "SELECT item_id, dependency_id FROM item_dependencies WHERE item_id IN ({ids}) ORDER BY item_id, position",
            (item.item_id for item in item_rows),
        )
    )
    dependencies: dict[WorkItemId, list[WorkItemId]] = defaultdict(list)
    for row in dependency_rows:
        dependencies[row.item_id].append(row.dependency_id)
    definitions_by_item = read_current_definitions(connection, tuple(item.item_id for item in item_rows))
    definitions = tuple(definitions_by_item[item.item_id] for item in item_rows if item.item_id in definitions_by_item)
    if definitions_by_item.keys() != live_item_ids:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Every current work item must have a current definition.")
    if any(
        tuple(dependencies[item_id]) != definition.definition.dependencies
        for item_id, definition in definitions_by_item.items()
    ):
        raise StorageError(
            StorageErrorCode.INVALID_STATE,
            "Current definition dependencies do not match relational dependencies.",
        )

    selected_proposals = {
        proposal.proposal_id: proposal
        for proposal in (
            decode_row(row, _ProposalRow)
            for row in select_by_ids(
                connection,
                """SELECT proposal_id, created_at, source_task_id, user_label, trigger, why_it_matters,
                          relation_kind, relation_item_id, relation_replacement_cost, effect, unlock,
                          urgency_evidence, subject_revision
                   FROM proposals WHERE proposal_id IN ({ids}) AND disposition IS NULL""",
                (item.item_id for item in item_rows) if include_proposals else (),
            )
        )
    }
    proposal_rows = tuple(
        selected_proposals[ProposalId(item.item_id)]
        for item in item_rows
        if ProposalId(item.item_id) in selected_proposals
    )
    evidence: dict[ProposalId, list[str]] = defaultdict(list)
    freshness: dict[ProposalId, list[str]] = defaultdict(list)
    for row in select_by_ids(
        connection,
        "SELECT proposal_id, selector AS value FROM proposal_evidence WHERE proposal_id IN ({ids}) ORDER BY proposal_id, position",
        (proposal.proposal_id for proposal in proposal_rows),
    ):
        selected = decode_row(row, _ProposalTextRow)
        evidence[selected.proposal_id].append(selected.value)
    for row in select_by_ids(
        connection,
        "SELECT proposal_id, assumption AS value FROM proposal_freshness WHERE proposal_id IN ({ids}) ORDER BY proposal_id, position",
        (proposal.proposal_id for proposal in proposal_rows),
    ):
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

    planned_replacements, replacement_dispositions = read_current_replacements(
        connection, (item.item_id for item in item_rows)
    )
    history_items = tuple(
        dict.fromkeys(
            item_id
            for item_id in (
                *(row.dependency_id for row in dependency_rows),
                *(proposal.relation_item_id for proposal in proposal_rows if proposal.relation_item_id is not None),
                *(relation.replacement_item for relation in planned_replacements),
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
        attempts=_attempt_records(connection, attempt_rows),
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
        planned_replacements=planned_replacements,
        replacement_dispositions=replacement_dispositions,
    )


def read_selected_decision_facts(  # noqa: C901, PLR0912, PLR0915
    connection: sqlite3.Connection,
    scope: query_models.DecisionScope,
    now: datetime,
) -> query_models.DecisionFacts:
    """Read only the explicitly selected relationships needed by one decision."""

    project = _project(connection)
    attempts: dict[AttemptId, _AttemptLineageRow] = {}
    primary_item_ids = list(scope.work_item_ids)
    for row in select_by_ids(
        connection,
        """SELECT attempt_id, item_id, state, branch, base_revision, accepted_scope_revision,
                  accepted_scope_digest, candidate_revision, brief_artifact_ref_id,
                  result_artifact_ref_id, subject_revision
           FROM attempts WHERE attempt_id IN ({ids})""",
        scope.attempt_ids,
    ):
        attempt = decode_row(row, _AttemptLineageRow)
        attempts[attempt.attempt_id] = attempt
    attempts = {attempt_id: attempts[attempt_id] for attempt_id in scope.attempt_ids if attempt_id in attempts}

    proposals: dict[ProposalId, _ProposalRow] = {}
    for row in select_by_ids(
        connection,
        """SELECT proposal_id, created_at, source_task_id, user_label, trigger, why_it_matters,
                  relation_kind, relation_item_id, relation_replacement_cost,
                  effect, unlock, urgency_evidence, subject_revision
           FROM proposals WHERE proposal_id IN ({ids}) AND disposition IS NULL""",
        scope.proposal_ids,
    ):
        proposal = decode_row(row, _ProposalRow)
        proposals[proposal.proposal_id] = proposal
    proposals = {proposal_id: proposals[proposal_id] for proposal_id in scope.proposal_ids if proposal_id in proposals}
    primary_item_ids.extend(attempt.item_id for attempt in attempts.values())
    primary_item_ids.extend(WorkItemId(proposal.proposal_id) for proposal in proposals.values())

    closure_ids = tuple(
        decode_row(row, _ItemIdRow).item_id
        for row in select_by_ids(
            connection,
            """WITH RECURSIVE closure(item_id) AS (
                   SELECT item_id FROM work_items WHERE item_id IN ({ids})
                   UNION
                   SELECT dependency.dependency_id
                   FROM item_dependencies AS dependency
                   JOIN closure ON closure.item_id = dependency.item_id
               ) SELECT item_id FROM closure""",
            scope.dependency_closure_roots,
        )
    )
    definition_ids = tuple(dict.fromkeys((*primary_item_ids, *closure_ids)))
    dependency_ids = tuple(dict.fromkeys((*definition_ids, *scope.live_dependent_roots)))
    dependent_ids = tuple(
        decode_row(row, _ItemIdRow).item_id
        for row in select_by_ids(
            connection,
            """SELECT dependency.item_id
               FROM item_dependencies AS dependency INDEXED BY item_dependencies_by_dependency
               JOIN work_items AS item ON item.item_id = dependency.item_id
               WHERE dependency.dependency_id IN ({ids}) AND item.queue_position IS NOT NULL
               ORDER BY dependency.item_id""",
            scope.live_dependent_roots,
        )
    )
    dependency_ids = tuple(dict.fromkeys((*dependency_ids, *dependent_ids)))
    selected_dependencies: dict[WorkItemId, list[WorkItemId]] = defaultdict(list)
    for row in select_by_ids(
        connection,
        "SELECT item_id, dependency_id FROM item_dependencies WHERE item_id IN ({ids}) ORDER BY item_id, position",
        dependency_ids,
    ):
        dependency = decode_row(row, _DependencyRow)
        selected_dependencies[dependency.item_id].append(dependency.dependency_id)
    selected_definitions = read_current_definitions(connection, definition_ids)
    selected_items = {
        item.item_id: item
        for item in (
            decode_row(row, _ItemRow)
            for row in select_by_ids(
                connection,
                """SELECT item_id, state, timing, source, outcome_evidence, next_action, notes,
                          subject_revision, queue_position
                   FROM work_items WHERE item_id IN ({ids})""",
                (*definition_ids, *dependent_ids),
            )
        )
    }
    related_ids = tuple(
        dict.fromkeys(
            (
                *scope.related_work_item_ids,
                *(
                    dependency
                    for item_id in primary_item_ids
                    if (item := selected_items.get(item_id)) is not None
                    and stored_state.live_work_state(item.state) is not None
                    for dependency in selected_dependencies[item_id]
                ),
            )
        )
    )
    selected_items.update(
        (item.item_id, item)
        for item in (
            decode_row(row, _ItemRow)
            for row in select_by_ids(
                connection,
                """SELECT item_id, state, timing, source, outcome_evidence, next_action, notes,
                          subject_revision, queue_position
                   FROM work_items WHERE item_id IN ({ids})""",
                (item_id for item_id in related_ids if item_id not in selected_items),
            )
        )
    )

    item_rows: dict[WorkItemId, _ItemRow] = {}
    history_items: list[WorkItemId] = []
    dependencies: dict[WorkItemId, list[WorkItemId]] = defaultdict(list)
    definitions: dict[WorkItemId, stored_state.ItemDefinitionRevision] = {}
    contextual_item_ids: set[WorkItemId] = set()
    selected_replacement_item_ids: set[WorkItemId] = set()
    dependency_item_ids: set[WorkItemId] = set()

    def read_item(
        item_id: WorkItemId,
        *,
        include_dependencies: bool,
        include_definition: bool,
        context: bool,
    ) -> _ItemRow | None:
        item = item_rows.get(item_id)
        if item is None:
            item = selected_items.get(item_id)
        if item is None:
            return None
        live = stored_state.live_work_state(item.state) is not None
        if live:
            item_rows[item_id] = item
        else:
            history_items.append(item_id)
        if include_dependencies and item_id not in dependency_item_ids:
            dependencies[item_id].extend(selected_dependencies[item_id])
            dependency_item_ids.add(item_id)
        if include_definition and item_id not in definitions:
            definition = selected_definitions.get(item_id)
            if definition is None:
                raise StorageError(
                    StorageErrorCode.INVALID_STATE, "Every selected work item must have a current definition."
                )
            definitions[item_id] = definition
        if (
            item_id in dependency_item_ids
            and item_id in definitions
            and tuple(dependencies[item_id]) != definitions[item_id].definition.dependencies
        ):
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                "Current definition dependencies do not match relational dependencies.",
            )
        if context:
            selected_replacement_item_ids.add(item_id)
            if live:
                contextual_item_ids.add(item_id)
        return item

    for item_id in dict.fromkeys(primary_item_ids):
        read_item(item_id, include_dependencies=True, include_definition=True, context=True)

    pending_closure = list(scope.dependency_closure_roots)
    visited_closure: set[WorkItemId] = set()
    while pending_closure:
        item_id = pending_closure.pop()
        if item_id in visited_closure:
            continue
        visited_closure.add(item_id)
        selected = read_item(item_id, include_dependencies=True, include_definition=True, context=False)
        if selected is not None:
            pending_closure.extend(dependencies[item_id])

    related_item_ids = list(scope.related_work_item_ids)
    for item_id in contextual_item_ids:
        related_item_ids.extend(dependencies[item_id])
    for dependent_id in dependent_ids:
        read_item(dependent_id, include_dependencies=True, include_definition=False, context=False)
    for item_id in dict.fromkeys(related_item_ids):
        read_item(item_id, include_dependencies=False, include_definition=False, context=False)

    for row in select_by_ids(
        connection,
        """SELECT attempt_id, item_id, state, branch, base_revision, accepted_scope_revision,
                  accepted_scope_digest, candidate_revision, brief_artifact_ref_id,
                  result_artifact_ref_id, subject_revision
           FROM attempts INDEXED BY one_live_attempt_per_item
           WHERE item_id IN ({ids}) AND state != 'done'""",
        contextual_item_ids,
    ):
        selected_attempt = decode_row(row, _AttemptLineageRow)
        attempts[selected_attempt.attempt_id] = selected_attempt

    attempt_states = {
        attempt.item_id: attempt.state
        for attempt in attempts.values()
        if attempt.state != work_models.AttemptState.DONE
    }
    for item_id in contextual_item_ids:
        validate_current_attempt_relation(
            "read_selected_decision_facts",
            item_id,
            item_rows[item_id].state,
            attempt_states.get(item_id),
            StorageErrorCode.INVALID_STATE,
        )
    for attempt in attempts.values():
        if attempt.state != work_models.AttemptState.DONE and attempt.item_id not in item_rows:
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                f"read_selected_decision_facts: open attempt '{attempt.attempt_id}' has no live work item "
                f"'{attempt.item_id}'; expected one matching live item; effect unchanged.",
            )

    evidence: dict[ProposalId, list[str]] = defaultdict(list)
    freshness: dict[ProposalId, list[str]] = defaultdict(list)
    for row in select_by_ids(
        connection,
        "SELECT proposal_id, selector AS value FROM proposal_evidence WHERE proposal_id IN ({ids}) ORDER BY proposal_id, position",
        proposals,
    ):
        selected = decode_row(row, _ProposalTextRow)
        evidence[selected.proposal_id].append(selected.value)
    for row in select_by_ids(
        connection,
        "SELECT proposal_id, assumption AS value FROM proposal_freshness WHERE proposal_id IN ({ids}) ORDER BY proposal_id, position",
        proposals,
    ):
        selected = decode_row(row, _ProposalTextRow)
        freshness[selected.proposal_id].append(selected.value)

    selected_artifacts: dict[ArtifactRefId, work_models.ArtifactRecord] = {}
    for row in select_by_ids(
        connection,
        "SELECT artifact_ref_id, kind FROM artifact_refs WHERE artifact_ref_id IN ({ids})",
        scope.artifact_ref_ids,
    ):
        selected = decode_row(row, _ArtifactRow)
        selected_artifacts[selected.artifact_ref_id] = work_models.ArtifactRecord(
            selected.artifact_ref_id, selected.kind
        )
    artifacts = [
        selected_artifacts[artifact_id] for artifact_id in scope.artifact_ref_ids if artifact_id in selected_artifacts
    ]

    attempt_authorities, command_attempt_authorities = _read_attempt_authorities(
        connection, (attempt.attempt_id for attempt in attempts.values()), project.host_epoch, now
    )
    preparation_authorities, command_preparation_authorities = _read_preparation_authorities(
        connection, contextual_item_ids, project.host_epoch, now
    )

    checkpoint_history_ids = tuple(
        decode_row(row, _HistoryIdRow).history_id
        for row in select_by_ids(
            connection,
            """SELECT history_id FROM transition_history
               WHERE subject_id IN ({ids}) AND outcome_schema = 'checkpoint-acceptance/v2'
               ORDER BY history_id""",
            scope.completion_history_attempt_ids,
        )
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
    planned_replacements, replacement_dispositions = read_current_replacements(
        connection, selected_replacement_item_ids
    )
    snapshot = LedgerSnapshot(
        revision=str(project.revision),
        items=tuple(
            _work_item_record(item, tuple(dependencies[item.item_id]), attempts_by_item.get(item.item_id))
            for item in item_rows.values()
        ),
        attempts=_attempt_records(connection, attempts.values()),
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
        checkpoint_history_ids=checkpoint_history_ids,
        planned_replacements=planned_replacements,
        replacement_dispositions=replacement_dispositions,
    )
    return query_models.DecisionFacts(
        snapshot,
        tuple(
            query_models.AttemptLineage(value.attempt_id, value.item_id, value.branch, value.base_revision)
            for value in attempts.values()
        ),
    )
