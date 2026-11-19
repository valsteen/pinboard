"""Advance exact v14 ledgers to v15 by releasing releasable unstarted items holds.

v15 guarantees that no live unstarted item stays held on an items hold once nothing
it waits for remains unsatisfied and no planned replacement withholds resume, and adds
the normalized ``released_item_holds`` provenance relation for commit-time releases.
The v14-to-v15 schema step writes no synthetic ``transition_history`` rows and leaves
``project_revision`` unchanged; its retained ``pinboard-schema-plan/v9`` plan and exact
``.v14`` backup preserve migration provenance.
"""

import sqlite3
from collections import defaultdict
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

import msgspec

from pinboard.adapters.sqlite import database
from pinboard.adapters.sqlite.dependencies import DependencyRow, read_accepted_checkpoints
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import move_item_state_count, read_all_item_unblockers
from pinboard.adapters.sqlite.migration import database as migration_database
from pinboard.adapters.sqlite.migration.schema_amendment_lifetime_v15 import upgrade_v15_connection
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.replacements import read_current_replacements
from pinboard.application import stored_state, unblockers
from pinboard.domain import work_models
from pinboard.domain.identifiers import AttemptId, WorkItemId
from pinboard.domain.ledger import LedgerSnapshot


class PlannedHoldRelease(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One unstarted items hold released to ready by the v14-to-v15 schema step."""

    item_id: WorkItemId
    previous_state: Literal["blocked"]
    previous_subject_revision: Annotated[int, msgspec.Meta(ge=0)]
    unblocker: unblockers.ItemsToFinishPayload


class _V14ItemRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    state: stored_state.StoredWorkItemState
    timing: work_models.Timing | None
    source: str | None
    outcome_evidence: str | None
    next_action: str | None
    notes: str | None
    subject_revision: int
    queue_position: int | None


class _V14OpenAttemptRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    attempt_id: AttemptId


class _V14RevisionRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: int


def select_v14_releasable_holds(connection: sqlite3.Connection) -> tuple[PlannedHoldRelease, ...]:
    """Identify every releasable unstarted items hold in a verified v14 ledger."""

    connection.row_factory = sqlite3.Row
    revision = database.decode_row(
        connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone(),
        _V14RevisionRow,
    ).revision
    all_items = tuple(
        database.decode_row(row, _V14ItemRow)
        for row in connection.execute(
            """SELECT item_id, state, timing, source, outcome_evidence, next_action, notes,
                      subject_revision, queue_position
               FROM work_items ORDER BY item_id"""
        ).fetchall()
    )
    live_rows = tuple(row for row in all_items if stored_state.live_work_state(row.state) is not None)
    live_ids = tuple(row.item_id for row in live_rows)
    dependencies_by_item: dict[WorkItemId, list[work_models.Dependency]] = defaultdict(list)
    for row in database.select_by_ids(
        connection,
        "SELECT item_id, dependency_id, checkpoint_id FROM item_dependencies WHERE item_id IN ({ids}) ORDER BY item_id, position",
        live_ids,
    ):
        dependency = database.decode_row(row, DependencyRow)
        dependencies_by_item[dependency.item_id].append(dependency.target)
    open_attempts = {
        decoded.item_id: decoded.attempt_id
        for row in connection.execute(
            "SELECT item_id, attempt_id FROM attempts INDEXED BY one_live_attempt_per_item WHERE state != 'done'"
        ).fetchall()
        for decoded in (database.decode_row(row, _V14OpenAttemptRow),)
    }
    unblockers_by_item = read_all_item_unblockers(connection)
    replacements, dispositions = read_current_replacements(connection, live_ids)
    accepted_checkpoints = read_accepted_checkpoints(
        connection, (target for targets in dependencies_by_item.values() for target in targets)
    )
    snapshot = LedgerSnapshot(
        revision=str(revision),
        items=tuple(
            work_models.WorkItem(
                row.item_id,
                live_state,
                None if row.timing is None else row.timing.value,
                tuple(dependencies_by_item[row.item_id]),
                open_attempts.get(row.item_id),
                row.source,
                row.next_action,
                row.notes,
                row.queue_position,
                row.outcome_evidence,
                unblocker=unblockers_by_item.get(row.item_id),
            )
            for row in live_rows
            if (live_state := stored_state.live_work_state(row.state)) is not None
        ),
        dropped_item_ids=frozenset(
            row.item_id for row in all_items if row.state == stored_state.StoredWorkItemState.DROPPED
        ),
        dependency_facts=(),
        accepted_checkpoints=accepted_checkpoints,
        planned_replacements=replacements,
        replacement_dispositions=dispositions,
    )
    subject_revisions = {row.item_id: row.subject_revision for row in live_rows}
    released: list[PlannedHoldRelease] = []
    for item in sorted(snapshot.items, key=lambda value: value.work_item_id):
        unblocker = snapshot.releasable_unstarted_hold(item)
        if unblocker is None:
            continue
        if item.state != work_models.WorkState.BLOCKED:
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                f"Unstarted held item '{item.work_item_id}' has unsupported state '{item.state.value}'.",
            )
        released.append(
            PlannedHoldRelease(
                item.work_item_id,
                "blocked",
                subject_revisions[item.work_item_id],
                unblockers.ItemsToFinishPayload(unblocker.items),
            )
        )
    return tuple(released)


def _create_statement(name: str) -> str:
    statements = {object_name: statement for _, object_name, _, statement in database._build_v15_schema_signature()}
    statement = statements[name]
    assert statement is not None
    return statement


def _write_v15(connection: sqlite3.Connection, released: tuple[PlannedHoldRelease, ...]) -> None:
    connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v14")
    connection.execute(_create_statement("project_meta"))
    connection.execute(
        "INSERT INTO project_meta SELECT singleton, application, 15, revision, host_epoch, created_at, updated_at FROM project_meta_v14"
    )
    connection.execute("DROP TABLE project_meta_v14")
    connection.execute(_create_statement("released_item_holds"))
    connection.execute(_create_statement("released_item_holds_by_item"))
    for hold in released:
        cursor = connection.execute(
            "UPDATE work_items SET state = 'ready' WHERE item_id = ? AND state = 'blocked' AND queue_position IS NOT NULL",
            (hold.item_id,),
        )
        if cursor.rowcount != 1:
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                f"v14 hold release could not move unstarted item '{hold.item_id}' to ready.",
            )
        connection.execute("DELETE FROM work_item_unblockers WHERE item_id = ?", (hold.item_id,))
        move_item_state_count(
            connection,
            stored_state.StoredWorkItemState.BLOCKED,
            stored_state.StoredWorkItemState.READY,
        )


def project_v14_for_current_readers(connection: sqlite3.Connection) -> None:
    """Advance an in-memory v14 projection so predecessor checks can use current readers."""

    connection.row_factory = sqlite3.Row
    with database.write_transaction(connection):
        database._verify_v14_schema(connection)
        released = select_v14_releasable_holds(connection)
        _write_v15(connection, released)
        database._verify_v15_schema(connection)
    upgrade_v15_connection(connection, None)


def upgrade_v14_connection(connection: sqlite3.Connection, expected_postimage: str | None) -> bool:
    """Release releasable unstarted items holds and switch authority to v15 atomically."""

    connection.row_factory = sqlite3.Row
    with database.write_transaction(connection):
        if database._read_pinboard_schema_version(connection) == 15:
            database._verify_v15_schema(connection)
            return False
        database._verify_v14_schema(connection)
        database.verify_database_integrity(connection)
        released = select_v14_releasable_holds(connection)
        _write_v15(connection, released)
        database._verify_v15_schema(connection)
        database.verify_database_integrity(connection)
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as projection:
            projection.executescript("\n".join(connection.iterdump()))
            database._configure_connection(projection)
            upgrade_v15_connection(projection, None)
        if (
            expected_postimage is not None
            and migration_database.schema_logical_digest(connection) != expected_postimage
        ):
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                "Hold-release migration differs from the previewed postimage; the predecessor remains authoritative.",
            )
    return True


def inspect_v14_migration(path: Path) -> tuple[str, tuple[PlannedHoldRelease, ...], str]:
    """Predict the exact v15 postimage and released holds of one verified v14 ledger without effects."""

    try:
        source = sha256(path.read_bytes()).hexdigest()
        with closing(
            sqlite3.connect(database._build_database_uri(path, OpenMode.READ_ONLY), uri=True, isolation_level=None)
        ) as connection:
            database._configure_connection(connection)
            database._verify_v14_schema(connection)
            database.verify_database_integrity(connection)
            with closing(sqlite3.connect(":memory:", isolation_level=None)) as simulation:
                connection.backup(simulation)
                database._configure_connection(simulation)
                released = select_v14_releasable_holds(simulation)
                upgrade_v14_connection(simulation, None)
                return source, released, migration_database.schema_logical_digest(simulation)
    except OSError as error:
        raise StorageError(StorageErrorCode.IO_ERROR, "The v14 migration could not read the database.") from error
    except sqlite3.Error as error:
        raise database.translate_database_error(error, opening=True).with_database_path(path) from error


def migrate_v14_database(path: Path, expected_postimage: str) -> bool:
    with migration_database.migration_connection(path) as connection:
        return upgrade_v14_connection(connection, expected_postimage)
