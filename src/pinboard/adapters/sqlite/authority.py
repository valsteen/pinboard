"""Read and change authority records on a supplied connection.

This module never commits, rolls back, closes the connection, calls callbacks,
reads the filesystem, or obtains time. Expected stale CAS writes return a
``DecisionFailure``; SQLite and persisted-invariant failures remain exceptional.
"""

import sqlite3
from datetime import datetime

import msgspec

from pinboard.adapters.sqlite.database import decode_row, require_one_changed_row, select_by_ids, stale_write
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.models import AttemptIdRow, ItemIdRow
from pinboard.application import query_models, stored_state
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import AttemptId, HostId, LeaseId, TaskId, WorkItemId


class _PreparationItemFacts(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    state: stored_state.StoredWorkItemState


class _DefinitionIdentity(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    definition_revision: int
    definition_digest: str


class _ProjectUpdate(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    updated_at: datetime


class _AttemptStatusRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    generation: int
    acquired_at: datetime
    expires_at: datetime
    state: authority_models.AttemptLeaseStatus
    generation_high_water: int | None
    lease_id: LeaseId | None
    task_id: TaskId | None
    host_id: HostId | None
    selected_attempt_id: AttemptId | None


class _PreparationStatusRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    generation: int
    definition_revision: int
    definition_digest: str
    acquired_at: datetime
    expires_at: datetime
    state: authority_models.PreparationLeaseStatus
    generation_high_water: int | None
    lease_id: LeaseId | None
    task_id: TaskId | None
    host_id: HostId | None
    item_state: stored_state.StoredWorkItemState | None
    referenced_revision: int | None
    referenced_digest: str | None
    current_revision: int | None
    current_digest: str | None
    project_updated_at: datetime | None


def read_attempt_authority_statuses(
    connection: sqlite3.Connection, attempt_ids: tuple[AttemptId, ...]
) -> dict[AttemptId, query_models.AttemptAuthorityStatus]:
    selected_ids = tuple(
        decode_row(row, AttemptIdRow).attempt_id
        for row in select_by_ids(
            connection, "SELECT attempt_id FROM attempt_leases WHERE attempt_id IN ({ids})", attempt_ids
        )
    )
    statuses: dict[AttemptId, query_models.AttemptAuthorityStatus] = {}
    for row in select_by_ids(
        connection,
        """SELECT lease.attempt_id, lease.generation, lease.acquired_at, lease.expires_at,
                  lease.status AS state, counter.generation_high_water, anchor.lease_id,
                  anchor.task_id, anchor.host_id, attempt.attempt_id AS selected_attempt_id
           FROM attempt_leases AS lease
           LEFT JOIN attempt_lease_counters AS counter ON counter.attempt_id = lease.attempt_id
           LEFT JOIN attempt_lease_generations AS anchor
             ON anchor.attempt_id = lease.attempt_id AND anchor.generation = lease.generation
           LEFT JOIN attempts AS attempt ON attempt.attempt_id = lease.attempt_id
           WHERE lease.attempt_id IN ({ids})""",
        selected_ids,
    ):
        value = decode_row(row, _AttemptStatusRow)
        if (
            value.generation_high_water != value.generation
            or value.lease_id is None
            or value.task_id is None
            or value.host_id is None
            or value.selected_attempt_id is None
        ):
            raise StorageError(StorageErrorCode.INVALID_STATE, "Attempt authority has no exact identity anchor.")
        statuses[value.attempt_id] = query_models.AttemptAuthorityStatus(
            value.attempt_id,
            value.task_id,
            value.host_id,
            value.lease_id,
            value.generation,
            value.acquired_at,
            value.expires_at,
            value.state,
        )
    return statuses


def read_preparation_authority_statuses(
    connection: sqlite3.Connection, item_ids: tuple[WorkItemId, ...]
) -> dict[WorkItemId, query_models.PreparationAuthorityStatus]:
    selected_ids = tuple(
        decode_row(row, ItemIdRow).item_id
        for row in select_by_ids(
            connection, "SELECT item_id FROM preparation_leases WHERE item_id IN ({ids})", item_ids
        )
    )
    statuses: dict[WorkItemId, query_models.PreparationAuthorityStatus] = {}
    for row in select_by_ids(
        connection,
        """SELECT lease.item_id, lease.generation, lease.definition_revision, lease.definition_digest,
                  lease.acquired_at, lease.expires_at, lease.status AS state,
                  counter.generation_high_water, anchor.lease_id, anchor.task_id, anchor.host_id,
                  item.state AS item_state, reference.definition_revision AS referenced_revision,
                  reference.definition_digest AS referenced_digest,
                  current.definition_revision AS current_revision, current.definition_digest AS current_digest,
                  (SELECT updated_at FROM project_meta WHERE singleton = 1) AS project_updated_at
           FROM preparation_leases AS lease
           LEFT JOIN preparation_lease_counters AS counter ON counter.item_id = lease.item_id
           LEFT JOIN preparation_lease_generations AS anchor
             ON anchor.item_id = lease.item_id AND anchor.generation = lease.generation
           LEFT JOIN work_items AS item ON item.item_id = lease.item_id
           LEFT JOIN work_item_definition_revisions AS reference
             ON reference.item_id = lease.item_id AND reference.definition_revision = lease.definition_revision
            AND reference.definition_digest = lease.definition_digest
           LEFT JOIN work_item_definition_revisions AS current
             ON current.item_id = lease.item_id AND current.definition_revision = (
                 SELECT MAX(latest.definition_revision) FROM work_item_definition_revisions AS latest
                 WHERE latest.item_id = lease.item_id
             )
           WHERE lease.item_id IN ({ids})""",
        selected_ids,
    ):
        value = decode_row(row, _PreparationStatusRow)
        if (
            value.generation_high_water != value.generation
            or value.lease_id is None
            or value.task_id is None
            or value.host_id is None
            or value.item_state is None
            or value.referenced_revision is None
            or value.current_revision is None
            or value.project_updated_at is None
        ):
            raise StorageError(StorageErrorCode.INVALID_STATE, "Preparation authority has no exact identity anchor.")
        if (
            value.state == authority_models.PreparationLeaseStatus.ACTIVE
            and value.expires_at > value.project_updated_at
            and (
                value.item_state
                not in {stored_state.StoredWorkItemState.INTAKE, stored_state.StoredWorkItemState.READY}
                or (value.referenced_revision, value.referenced_digest)
                != (value.current_revision, value.current_digest)
            )
        ):
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                "An active preparation lease must name a ready item and its current definition.",
            )
        statuses[value.item_id] = query_models.PreparationAuthorityStatus(
            value.item_id,
            value.definition_revision,
            value.definition_digest,
            value.task_id,
            value.host_id,
            value.lease_id,
            value.generation,
            value.acquired_at,
            value.expires_at,
            value.state,
        )
    return statuses


def read_attempt_authority_status(
    connection: sqlite3.Connection, attempt_id: AttemptId
) -> query_models.AttemptAuthorityStatus | None:
    lease_row = connection.execute(
        """
        SELECT attempt_id, generation, acquired_at, expires_at, status AS state
        FROM attempt_leases
        WHERE attempt_id = ?
        """,
        (attempt_id,),
    ).fetchone()
    if lease_row is None:
        return None
    lease = decode_row(lease_row, stored_state.StoredAttemptLease)
    counter_row = connection.execute(
        "SELECT attempt_id, generation_high_water FROM attempt_lease_counters WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    anchor_row = connection.execute(
        """
        SELECT attempt_id, generation, lease_id, task_id, host_id
        FROM attempt_lease_generations
        WHERE attempt_id = ? AND generation = ?
        """,
        (attempt_id, lease.generation),
    ).fetchone()
    attempt_row = connection.execute(
        "SELECT attempt_id FROM attempts WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    if counter_row is None or anchor_row is None or attempt_row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Attempt authority has no exact identity anchor.")
    counter = decode_row(counter_row, stored_state.AttemptLeaseCounter)
    anchor = decode_row(anchor_row, stored_state.AttemptLeaseGeneration)
    if counter.generation_high_water != lease.generation or anchor.generation != lease.generation:
        raise StorageError(StorageErrorCode.INVALID_STATE, "The current attempt lease does not match its counter.")
    return query_models.AttemptAuthorityStatus(
        lease.attempt_id,
        anchor.task_id,
        anchor.host_id,
        anchor.lease_id,
        lease.generation,
        lease.acquired_at,
        lease.expires_at,
        lease.state,
    )


def read_preparation_authority_status(
    connection: sqlite3.Connection, item_id: WorkItemId
) -> query_models.PreparationAuthorityStatus | None:
    lease_row = connection.execute(
        """
        SELECT item_id, generation, definition_revision, definition_digest,
               acquired_at, expires_at, status AS state
        FROM preparation_leases
        WHERE item_id = ?
        """,
        (item_id,),
    ).fetchone()
    if lease_row is None:
        return None
    lease = decode_row(lease_row, stored_state.StoredPreparationLease)
    counter_row = connection.execute(
        "SELECT item_id, generation_high_water FROM preparation_lease_counters WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    anchor_row = connection.execute(
        """
        SELECT item_id, generation, lease_id, task_id, host_id
        FROM preparation_lease_generations
        WHERE item_id = ? AND generation = ?
        """,
        (item_id, lease.generation),
    ).fetchone()
    item_row = connection.execute(
        "SELECT state FROM work_items WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    referenced_definition_row = connection.execute(
        """
        SELECT definition_revision, definition_digest
        FROM work_item_definition_revisions
        WHERE item_id = ? AND definition_revision = ? AND definition_digest = ?
        """,
        (item_id, lease.definition_revision, lease.definition_digest),
    ).fetchone()
    current_definition_row = connection.execute(
        """
        SELECT definition_revision, definition_digest
        FROM work_item_definition_revisions
        WHERE item_id = ?
        ORDER BY definition_revision DESC
        LIMIT 1
        """,
        (item_id,),
    ).fetchone()
    project_row = connection.execute("SELECT updated_at FROM project_meta WHERE singleton = 1").fetchone()
    if (
        counter_row is None
        or anchor_row is None
        or item_row is None
        or referenced_definition_row is None
        or current_definition_row is None
        or project_row is None
    ):
        raise StorageError(StorageErrorCode.INVALID_STATE, "Preparation authority has no exact identity anchor.")
    counter = decode_row(counter_row, stored_state.PreparationLeaseCounter)
    anchor = decode_row(anchor_row, stored_state.PreparationLeaseGeneration)
    item = decode_row(item_row, _PreparationItemFacts)
    referenced_definition = decode_row(referenced_definition_row, _DefinitionIdentity)
    current_definition = decode_row(current_definition_row, _DefinitionIdentity)
    project = decode_row(project_row, _ProjectUpdate)
    if counter.generation_high_water != lease.generation or anchor.generation != lease.generation:
        raise StorageError(StorageErrorCode.INVALID_STATE, "The current preparation lease does not match its counter.")
    if (
        lease.state == authority_models.PreparationLeaseStatus.ACTIVE
        and lease.expires_at > project.updated_at
        and (
            item.state not in {stored_state.StoredWorkItemState.INTAKE, stored_state.StoredWorkItemState.READY}
            or referenced_definition != current_definition
        )
    ):
        raise StorageError(
            StorageErrorCode.INVALID_STATE,
            "An active preparation lease must name a ready item and its current definition.",
        )
    return query_models.PreparationAuthorityStatus(
        lease.item_id,
        lease.definition_revision,
        lease.definition_digest,
        anchor.task_id,
        anchor.host_id,
        anchor.lease_id,
        lease.generation,
        lease.acquired_at,
        lease.expires_at,
        lease.state,
    )


def validate_attempt_authority(state: stored_state.StoredWorkState, error_code: StorageErrorCode) -> None:
    attempt_counters = {value.attempt_id: value.generation_high_water for value in state.authority.attempt_counters}
    for anchor in state.authority.attempt_generations:
        high_water = attempt_counters.get(anchor.attempt_id)
        if high_water is None or anchor.generation > high_water:
            raise StorageError(error_code, "An attempt generation exceeds its retained counter.")
    attempt_anchors = {(anchor.attempt_id, anchor.generation) for anchor in state.authority.attempt_generations}
    for lease in state.authority.attempt_leases:
        high_water = attempt_counters.get(lease.attempt_id)
        if (
            high_water is None
            or lease.generation != high_water
            or (lease.attempt_id, lease.generation) not in attempt_anchors
        ):
            raise StorageError(error_code, "The current attempt lease does not match its retained counter.")
    preparation_counters = {
        value.item_id: value.generation_high_water for value in state.authority.preparation_counters
    }
    for anchor in state.authority.preparation_generations:
        high_water = preparation_counters.get(anchor.item_id)
        if high_water is None or anchor.generation > high_water:
            raise StorageError(error_code, "A preparation generation exceeds its retained counter.")
    preparation_anchors = {(anchor.item_id, anchor.generation) for anchor in state.authority.preparation_generations}
    for lease in state.authority.preparation_leases:
        high_water = preparation_counters.get(lease.item_id)
        if (
            high_water is None
            or lease.generation != high_water
            or (lease.item_id, lease.generation) not in preparation_anchors
        ):
            raise StorageError(error_code, "The current preparation lease does not match its retained counter.")


def read_authority(connection: sqlite3.Connection) -> stored_state.AuthorityRecords:
    counters = tuple(
        decode_row(row, stored_state.AttemptLeaseCounter)
        for row in connection.execute(
            "SELECT attempt_id, generation_high_water FROM attempt_lease_counters ORDER BY attempt_id"
        ).fetchall()
    )
    generations = tuple(
        decode_row(row, stored_state.AttemptLeaseGeneration)
        for row in connection.execute(
            """
            SELECT attempt_id, generation, lease_id, task_id, host_id
            FROM attempt_lease_generations
            ORDER BY attempt_id, generation
            """
        ).fetchall()
    )
    leases = tuple(
        decode_row(row, stored_state.StoredAttemptLease)
        for row in connection.execute(
            """
            SELECT attempt_id, generation, acquired_at, expires_at, status AS state
            FROM attempt_leases
            ORDER BY attempt_id
            """
        ).fetchall()
    )
    preparation_counters = tuple(
        decode_row(row, stored_state.PreparationLeaseCounter)
        for row in connection.execute(
            "SELECT item_id, generation_high_water FROM preparation_lease_counters ORDER BY item_id"
        ).fetchall()
    )
    preparation_generations = tuple(
        decode_row(row, stored_state.PreparationLeaseGeneration)
        for row in connection.execute(
            """
            SELECT item_id, generation, lease_id, task_id, host_id
            FROM preparation_lease_generations
            ORDER BY item_id, generation
            """
        ).fetchall()
    )
    preparation_leases = tuple(
        decode_row(row, stored_state.StoredPreparationLease)
        for row in connection.execute(
            """
            SELECT item_id, generation, definition_revision, definition_digest,
                   acquired_at, expires_at, status AS state
            FROM preparation_leases
            ORDER BY item_id
            """
        ).fetchall()
    )
    return stored_state.AuthorityRecords(
        counters,
        generations,
        leases,
        preparation_counters,
        preparation_generations,
        preparation_leases,
    )


def fence_attempt_authority(
    connection: sqlite3.Connection,
    change: decision_models.AttemptAuthorityChange,
    decided_at: datetime,
) -> DecisionFailure | None:
    before = change.before
    after = change.after
    if after.lease_id is not None or after.generation != before.generation + 1:
        raise StorageError(
            StorageErrorCode.INVARIANT_VIOLATION,
            "Attempt-authority fencing must allocate one revoked generation.",
        )
    anchor = connection.execute(
        """
        SELECT lease_id, task_id, host_id
        FROM attempt_lease_generations
        WHERE attempt_id = ? AND generation = ?
        """,
        (before.attempt, before.generation),
    ).fetchone()
    if anchor is None:
        return stale_write("The retained attempt generation is missing.")
    if (
        failure := require_one_changed_row(
            connection.execute(
                """
                UPDATE attempt_lease_counters
                SET generation_high_water = ?
                WHERE attempt_id = ? AND generation_high_water = ?
                """,
                (after.generation, before.attempt, before.generation),
            ),
            "The attempt-authority counter is stale.",
        )
    ) is not None:
        return failure
    if (
        failure := require_one_changed_row(
            connection.execute(
                """
                UPDATE attempt_leases
                SET generation = ?, expires_at = ?, status = 'revoked'
                WHERE attempt_id = ? AND generation = ?
                """,
                (after.generation, decided_at.isoformat(), before.attempt, before.generation),
            ),
            "The current attempt lease is stale.",
        )
    ) is not None:
        return failure
    connection.execute(
        """
        INSERT INTO attempt_lease_generations (attempt_id, generation, lease_id, task_id, host_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (before.attempt, after.generation, anchor["lease_id"], anchor["task_id"], anchor["host_id"]),
    )
    return None


def write_attempt_authority(
    connection: sqlite3.Connection, decision: authority_models.AttemptAuthorityDecision
) -> DecisionFailure | None:
    proposed_replacement = decision.proposed_replacement
    retained_counter = connection.execute(
        "SELECT generation_high_water FROM attempt_lease_counters WHERE attempt_id = ?",
        (decision.attempt,),
    ).fetchone()
    if retained_counter is None:
        if decision.counter_before != 0:
            return stale_write("The attempt counter is missing.")
        if (
            failure := require_one_changed_row(
                connection.execute(
                    """
                    INSERT INTO attempt_lease_counters (attempt_id, generation_high_water)
                    VALUES (?, ?)
                    ON CONFLICT(attempt_id) DO NOTHING
                    """,
                    (decision.attempt, decision.counter_after),
                ),
                "The attempt counter already exists.",
            )
        ) is not None:
            return failure
    else:
        if (
            failure := require_one_changed_row(
                connection.execute(
                    """
                UPDATE attempt_lease_counters
                SET generation_high_water = ?
                WHERE attempt_id = ? AND generation_high_water = ?
                """,
                    (decision.counter_after, decision.attempt, decision.counter_before),
                ),
                "The attempt-authority counter is stale.",
            )
        ) is not None:
            return failure
    connection.execute(
        """
        INSERT INTO attempt_lease_generations (attempt_id, generation, lease_id, task_id, host_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(attempt_id, generation) DO NOTHING
        """,
        (
            proposed_replacement.attempt,
            proposed_replacement.generation,
            proposed_replacement.lease_id,
            proposed_replacement.task_id,
            proposed_replacement.host_id,
        ),
    )
    anchor = connection.execute(
        """
        SELECT lease_id, task_id, host_id
        FROM attempt_lease_generations
        WHERE attempt_id = ? AND generation = ?
        """,
        (proposed_replacement.attempt, proposed_replacement.generation),
    ).fetchone()
    if anchor is None or tuple(anchor) != (
        proposed_replacement.lease_id,
        proposed_replacement.task_id,
        proposed_replacement.host_id,
    ):
        return stale_write("The retained attempt generation conflicts.")
    expected_retained = decision.expected_retained
    if expected_retained is None:
        return require_one_changed_row(
            connection.execute(
                """
                INSERT INTO attempt_leases (attempt_id, generation, acquired_at, expires_at, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO NOTHING
                """,
                (
                    proposed_replacement.attempt,
                    proposed_replacement.generation,
                    proposed_replacement.acquired_at.isoformat(),
                    proposed_replacement.expires_at.isoformat(),
                    proposed_replacement.state.value,
                ),
            ),
            "The current attempt lease already exists.",
        )
    return require_one_changed_row(
        connection.execute(
            """
            UPDATE attempt_leases
            SET generation = ?, acquired_at = ?, expires_at = ?, status = ?
            WHERE attempt_id = ? AND generation = ? AND acquired_at = ? AND expires_at = ? AND status = ?
            """,
            (
                proposed_replacement.generation,
                proposed_replacement.acquired_at.isoformat(),
                proposed_replacement.expires_at.isoformat(),
                proposed_replacement.state.value,
                expected_retained.attempt,
                expected_retained.generation,
                expected_retained.acquired_at.isoformat(),
                expected_retained.expires_at.isoformat(),
                expected_retained.state.value,
            ),
        ),
        "The current attempt lease changed before persistence.",
    )


def write_preparation_authority(
    connection: sqlite3.Connection, decision: authority_models.PreparationAuthorityDecision
) -> DecisionFailure | None:
    proposed_replacement = decision.proposed_replacement
    retained_counter = connection.execute(
        "SELECT generation_high_water FROM preparation_lease_counters WHERE item_id = ?",
        (decision.work_item_id,),
    ).fetchone()
    if retained_counter is None:
        if decision.counter_before != 0:
            return stale_write("The preparation counter is missing.")
        if (
            failure := require_one_changed_row(
                connection.execute(
                    """
                    INSERT INTO preparation_lease_counters (item_id, generation_high_water)
                    VALUES (?, ?)
                    ON CONFLICT(item_id) DO NOTHING
                    """,
                    (decision.work_item_id, decision.counter_after),
                ),
                "The preparation counter already exists.",
            )
        ) is not None:
            return failure
    elif (
        failure := require_one_changed_row(
            connection.execute(
                """
                UPDATE preparation_lease_counters
                SET generation_high_water = ?
                WHERE item_id = ? AND generation_high_water = ?
                """,
                (decision.counter_after, decision.work_item_id, decision.counter_before),
            ),
            "The preparation-authority counter is stale.",
        )
    ) is not None:
        return failure
    connection.execute(
        """
        INSERT INTO preparation_lease_generations (item_id, generation, lease_id, task_id, host_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(item_id, generation) DO NOTHING
        """,
        (
            proposed_replacement.work_item_id,
            proposed_replacement.generation,
            proposed_replacement.lease_id,
            proposed_replacement.task_id,
            proposed_replacement.host_id,
        ),
    )
    anchor = connection.execute(
        """
        SELECT lease_id, task_id, host_id
        FROM preparation_lease_generations
        WHERE item_id = ? AND generation = ?
        """,
        (proposed_replacement.work_item_id, proposed_replacement.generation),
    ).fetchone()
    if anchor is None or tuple(anchor) != (
        proposed_replacement.lease_id,
        proposed_replacement.task_id,
        proposed_replacement.host_id,
    ):
        return stale_write("The retained preparation generation conflicts.")
    expected_retained = decision.expected_retained
    if expected_retained is None:
        return require_one_changed_row(
            connection.execute(
                """
                INSERT INTO preparation_leases (
                    item_id, generation, definition_revision, definition_digest, acquired_at, expires_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO NOTHING
                """,
                (
                    proposed_replacement.work_item_id,
                    proposed_replacement.generation,
                    proposed_replacement.definition_revision,
                    proposed_replacement.definition_digest,
                    proposed_replacement.acquired_at.isoformat(),
                    proposed_replacement.expires_at.isoformat(),
                    proposed_replacement.state.value,
                ),
            ),
            "The current preparation lease already exists.",
        )
    return require_one_changed_row(
        connection.execute(
            """
            UPDATE preparation_leases
            SET generation = ?, definition_revision = ?, definition_digest = ?,
                acquired_at = ?, expires_at = ?, status = ?
            WHERE item_id = ? AND generation = ? AND definition_revision = ? AND definition_digest = ?
                AND acquired_at = ? AND expires_at = ? AND status = ?
            """,
            (
                proposed_replacement.generation,
                proposed_replacement.definition_revision,
                proposed_replacement.definition_digest,
                proposed_replacement.acquired_at.isoformat(),
                proposed_replacement.expires_at.isoformat(),
                proposed_replacement.state.value,
                expected_retained.work_item_id,
                expected_retained.generation,
                expected_retained.definition_revision,
                expected_retained.definition_digest,
                expected_retained.acquired_at.isoformat(),
                expected_retained.expires_at.isoformat(),
                expected_retained.state.value,
            ),
        ),
        "The current preparation lease changed before persistence.",
    )


def consume_preparation_authority(
    connection: sqlite3.Connection,
    authority: work_models.PreparationCommandAuthority,
    consumed_at: datetime,
) -> DecisionFailure | None:
    if (
        failure := require_one_changed_row(
            connection.execute(
                """
                UPDATE preparation_lease_counters
                SET generation_high_water = ?
                WHERE item_id = ? AND generation_high_water = ?
                """,
                (authority.generation + 1, authority.work_item_id, authority.generation),
            ),
            "The preparation-authority counter is stale.",
        )
    ) is not None:
        return failure
    connection.execute(
        """
        INSERT INTO preparation_lease_generations (item_id, generation, lease_id, task_id, host_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            authority.work_item_id,
            authority.generation + 1,
            authority.lease_id,
            authority.task_id,
            authority.host_id,
        ),
    )
    return require_one_changed_row(
        connection.execute(
            """
            UPDATE preparation_leases
            SET generation = ?, expires_at = ?, status = 'revoked'
            WHERE item_id = ? AND generation = ? AND definition_revision = ? AND definition_digest = ?
                AND expires_at = ? AND status = 'active'
            """,
            (
                authority.generation + 1,
                consumed_at.isoformat(),
                authority.work_item_id,
                authority.generation,
                authority.definition_revision,
                authority.definition_digest,
                authority.expires_at.isoformat(),
            ),
        ),
        "The preparation authority changed before activation.",
    )
