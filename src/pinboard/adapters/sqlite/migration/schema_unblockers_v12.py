"""Advance exact v12 ledgers to v13 named unblockers only after every live hold has one.

v13 records what each paused or blocked item waits for and retires the deferred state. A v12 ledger knows that
for only some holds: a still-pending structured pause waits for its decision batch and a checkpoint-accepted
pause waits for a human decision about the recorded remaining work. Every other live blocked item, blocked or
paused attempt and deferred item needs a label in a strict ``pinboard-hold-labels/v1`` file bound to the exact
source ledger digest; INSTALL.md documents the agent-driven walk that prepares it. A label with a null unblocker
releases unstarted work that waits for nothing to ready. Other deferred items become unstarted blocked items waiting
for their labelled outside condition. Keep this step, like its predecessors, until an authorized decision retires
v12 ledgers.
"""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

import msgspec

from pinboard.adapters.sqlite import database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.migration import database as migration_database
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.application import decision_batches, stored_state, unblockers
from pinboard.domain import decisions, work_models
from pinboard.domain.history import CheckpointAcceptanceOutcome, TransitionReceiptOutcome
from pinboard.domain.identifiers import AttemptId, CheckpointId, WorkItemId

type Identity = Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]
type Sha256 = Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{64}\z")]

HOLD_LABELS_SCHEMA = "pinboard-hold-labels/v1"


class HoldLabel(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """What one live held subject waits for; attempt_id names its live attempt, or null for unstarted work.

    A null unblocker says unstarted work waits for nothing, so the step stores it as ready.
    """

    item_id: Identity
    attempt_id: Identity | None
    unblocker: unblockers.UnblockerPayload | None


class HoldLabels(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Labels for one exact v12 ledger, identified by the SHA-256 of its database bytes."""

    schema: Literal["pinboard-hold-labels/v1"]
    source_sha256: Sha256
    labels: tuple[HoldLabel, ...]

    def __post_init__(self) -> None:
        items = tuple(label.item_id for label in self.labels)
        if len(set(items)) != len(items):
            raise ValueError("Each held item has at most one label.")


@dataclass(frozen=True, slots=True)
class LabelInput:
    """One label file's exact bytes, their digest and the decoded labels."""

    content: bytes
    sha256: str
    labels: HoldLabels


@dataclass(frozen=True, slots=True)
class ResolvedHolds:
    """The v13 unblocker of every item that stays held, and the unstarted items released to ready."""

    held: dict[WorkItemId, work_models.Unblocker]
    released: frozenset[WorkItemId]


@dataclass(frozen=True, slots=True)
class LabelSubject:
    """One live held subject the v12 ledger cannot label by itself."""

    item_id: WorkItemId
    attempt_id: AttemptId | None
    state: str
    proposal: str


class _HeldRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    item_state: str
    attempt_id: AttemptId | None
    attempt_state: str | None
    attempt_revision: int | None


class _ReceiptRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_kind: str
    input_schema: str
    input_json: str
    outcome_schema: str
    outcome_json: str


class _PendingBatchRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    input_json: str


class _DependencyRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    dependency_id: WorkItemId
    satisfied: int


def read_labels(path: Path) -> LabelInput:
    """Read one label file exactly once; malformed or non-canonical content is rejected unchanged."""

    content = path.read_bytes()
    try:
        labels = msgspec.json.decode(content, type=HoldLabels, strict=True)
    except (msgspec.DecodeError, ValueError) as error:
        raise ValueError(f"The hold label file {path} is not a valid {HOLD_LABELS_SCHEMA} record: {error}") from error
    return LabelInput(content, sha256(content).hexdigest(), labels)


def _receipt_at(connection: sqlite3.Connection, project_revision: int) -> _ReceiptRow | None:
    row = connection.execute(
        """SELECT action_kind, input_schema, input_json, outcome_schema, outcome_json
           FROM transition_history WHERE project_revision = ?""",
        (project_revision,),
    ).fetchone()
    return None if row is None else database.decode_row(row, _ReceiptRow)


def _pending_batch_unblocker(
    connection: sqlite3.Connection, attempt_id: AttemptId
) -> work_models.WaitingForHumanDecision | None:
    row = connection.execute(
        """SELECT receipt.input_json FROM attempt_decision_batches AS batch
           JOIN transition_history AS receipt ON receipt.history_id = batch.pause_history_id
           WHERE batch.attempt_id = ? AND batch.resume_history_id IS NULL""",
        (attempt_id,),
    ).fetchone()
    if row is None:
        return None
    pause = msgspec.json.decode(
        database.decode_row(row, _PendingBatchRow).input_json, type=decision_batches.PauseDecisionsInput, strict=True
    )
    return decisions.decision_batch_unblocker(decision_batches.decisions_from_payload(pause).decisions)


def _unfinished_dependencies(connection: sqlite3.Connection, item_id: WorkItemId) -> tuple[WorkItemId, ...]:
    rows = connection.execute(
        """SELECT dependency.dependency_id,
                  producer.state IN ('done', 'superseded') OR EXISTS (
                      SELECT 1 FROM checkpoint_acceptances AS accepted
                      WHERE accepted.item_id = dependency.dependency_id
                        AND accepted.checkpoint_id = dependency.checkpoint_id
                  ) AS satisfied
           FROM item_dependencies AS dependency
           JOIN work_items AS producer ON producer.item_id = dependency.dependency_id
           WHERE dependency.item_id = ? ORDER BY dependency.position""",
        (item_id,),
    ).fetchall()
    return tuple(
        value.dependency_id
        for value in (database.decode_row(row, _DependencyRow) for row in rows)
        if not value.satisfied
    )


def _accepted_dependencies(connection: sqlite3.Connection, item_id: WorkItemId) -> frozenset[WorkItemId]:
    return frozenset(
        WorkItemId(str(row[0]))
        for row in connection.execute("SELECT dependency_id FROM item_dependencies WHERE item_id = ?", (item_id,))
    )


def _dependency_proposal(connection: sqlite3.Connection, item_id: WorkItemId) -> str:
    unfinished = _unfinished_dependencies(connection, item_id)
    if unfinished:
        return f"Unfinished accepted dependencies: {', '.join(unfinished)}; consider waiting for these items to finish."
    return "No unfinished accepted dependency; ask the human what it waits for."


def classify_holds(
    connection: sqlite3.Connection,
) -> tuple[dict[WorkItemId, work_models.Unblocker], tuple[LabelSubject, ...]]:
    """Split live held subjects into those the ledger labels itself and those that need a label.

    This is predecessor-schema decoding: the query selects only paused, blocked and deferred v12 rows, so any other
    stored held state, or a paused item without its attempt, is invalid stored state and raises `StorageError`.
    """

    automatic: dict[WorkItemId, work_models.Unblocker] = {}
    needed: list[LabelSubject] = []
    rows = connection.execute(
        """SELECT item.item_id, item.state AS item_state, attempt.attempt_id, attempt.state AS attempt_state,
                  attempt.subject_revision AS attempt_revision
           FROM work_items AS item
           LEFT JOIN attempts AS attempt ON attempt.item_id = item.item_id AND attempt.state != 'done'
           WHERE item.state IN ('paused', 'blocked', 'deferred')
           ORDER BY item.queue_position, item.item_id"""
    ).fetchall()
    for held in (database.decode_row(row, _HeldRow) for row in rows):
        match held.item_state:
            case "deferred":
                needed.append(
                    LabelSubject(
                        held.item_id,
                        None,
                        "deferred",
                        "Deferred work becomes unstarted work waiting for an outside condition; state its reopen "
                        "condition as that outside condition.",
                    )
                )
            case "blocked":
                needed.append(
                    LabelSubject(
                        held.item_id,
                        held.attempt_id,
                        "blocked",
                        "Its block recorded no reason. " + _dependency_proposal(connection, held.item_id),
                    )
                )
            case "paused":
                if held.attempt_id is None or held.attempt_revision is None:
                    raise StorageError(StorageErrorCode.INVALID_STATE, f"Paused item '{held.item_id}' has no attempt.")
                if (pending := _pending_batch_unblocker(connection, held.attempt_id)) is not None:
                    automatic[held.item_id] = pending
                    continue
                receipt = _receipt_at(connection, held.attempt_revision)
                if (
                    receipt is not None
                    and receipt.action_kind == "accept-checkpoint"
                    and receipt.outcome_schema == "checkpoint-acceptance/v2"
                ):
                    outcome = msgspec.json.decode(receipt.outcome_json, type=CheckpointAcceptanceOutcome, strict=True)
                    automatic[held.item_id] = decisions.checkpoint_unblocker(
                        held.item_id, CheckpointId(outcome.checkpoint)
                    )
                    continue
                reason = (
                    msgspec.json.decode(receipt.outcome_json, type=TransitionReceiptOutcome, strict=True).evidence
                    if receipt is not None and receipt.outcome_schema == "transition-receipt/v1"
                    else None
                )
                needed.append(
                    LabelSubject(
                        held.item_id,
                        held.attempt_id,
                        "paused",
                        "Recorded pause reason, whose kind is unknown: " + reason
                        if reason is not None
                        else "Its pause records no reason. " + _dependency_proposal(connection, held.item_id),
                    )
                )
            case _:
                raise StorageError(StorageErrorCode.INVALID_STATE, f"Unexpected held state '{held.item_state}'.")
    return automatic, tuple(needed)


def missing_labels_message(subjects: tuple[LabelSubject, ...]) -> str:
    listed = "; ".join(
        f"{subject.item_id} ({subject.state}"
        + ("" if subject.attempt_id is None else f", attempt {subject.attempt_id}")
        + f"): {subject.proposal}"
        for subject in subjects
    )
    return (
        f"The v12 ledger has {len(subjects)} live held subject(s) without a named unblocker: {listed}. "
        f"Write a {HOLD_LABELS_SCHEMA} file bound to this ledger's source_sha256 that labels each one as items "
        "to finish, a human decision or an outside condition, or with a null unblocker for unstarted work that "
        "waits for nothing, as INSTALL.md describes, then preview again with --labels."
    )


def resolve_unblockers(connection: sqlite3.Connection, source_sha256: str, labels: LabelInput | None) -> ResolvedHolds:
    """Return every live held item's v13 unblocker or release, or refuse with the exact missing subjects."""

    automatic, needed = classify_holds(connection)
    held_rows = {
        WorkItemId(str(row[0])): (str(row[1]), None if row[2] is None else AttemptId(str(row[2])))
        for row in connection.execute(
            """SELECT item.item_id, item.state, attempt.attempt_id FROM work_items AS item
               LEFT JOIN attempts AS attempt ON attempt.item_id = item.item_id AND attempt.state != 'done'
               WHERE item.state IN ('paused', 'blocked', 'deferred')"""
        )
    }
    resolved: dict[WorkItemId, work_models.Unblocker] = dict(automatic)
    released: set[WorkItemId] = set()
    if labels is not None:
        if labels.labels.source_sha256 != source_sha256:
            raise ValueError(
                "The hold label file is bound to another ledger state "
                f"({labels.labels.source_sha256}); this ledger is {source_sha256}. Relabel the current ledger."
            )
        for label in labels.labels.labels:
            item_id = WorkItemId(label.item_id)
            held = held_rows.get(item_id)
            if held is None:
                raise ValueError(f"Label for '{item_id}' names no live paused, blocked or deferred item.")
            state, attempt_id = held
            if (None if label.attempt_id is None else AttemptId(label.attempt_id)) != attempt_id:
                raise ValueError(
                    f"Label for '{item_id}' names attempt {label.attempt_id!r}; its live attempt is {attempt_id!r}."
                )
            if label.unblocker is None:
                if attempt_id is not None:
                    raise ValueError(
                        f"Label for '{item_id}' releases work with live attempt {attempt_id!r}; only unstarted "
                        "work can be released to ready."
                    )
                resolved.pop(item_id, None)
                released.add(item_id)
                continue
            unblocker = unblockers.unblocker_from_payload(label.unblocker)
            if state == "deferred" and not isinstance(unblocker, work_models.WaitingForOutsideCondition):
                raise ValueError(f"Deferred item '{item_id}' becomes unstarted work waiting for an outside condition.")
            if isinstance(unblocker, work_models.WaitingForItemsToFinish):
                outside = tuple(
                    item for item in unblocker.items if item not in _accepted_dependencies(connection, item_id)
                )
                if outside:
                    raise ValueError(
                        f"Label for '{item_id}' waits for items that are not its accepted dependencies: "
                        f"{', '.join(outside)}."
                    )
            resolved[item_id] = unblocker
    missing = tuple(
        subject for subject in needed if subject.item_id not in resolved and subject.item_id not in released
    )
    if missing:
        raise ValueError(missing_labels_message(missing))
    return ResolvedHolds(resolved, frozenset(released))


def _create_statement(name: str) -> str:
    statements = {table: statement for _, table, _, statement in database._build_v13_schema_signature()}
    statement = statements[name]
    assert statement is not None
    return statement


def _write_v13(connection: sqlite3.Connection, holds: ResolvedHolds) -> None:
    connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v12")
    connection.execute(_create_statement("project_meta"))
    connection.execute(
        "INSERT INTO project_meta SELECT singleton, application, 13, revision, host_epoch, created_at, updated_at FROM project_meta_v12"
    )
    connection.execute("DROP TABLE project_meta_v12")
    # Retiring 'deferred' changes CHECK constraints, so both tables are rebuilt from a temporary copy.
    connection.execute("CREATE TEMP TABLE work_items_v12 AS SELECT * FROM work_items")
    connection.executemany(
        "UPDATE temp.work_items_v12 SET state = 'ready' WHERE item_id = ?",
        tuple((item_id,) for item_id in sorted(holds.released)),
    )
    connection.execute("UPDATE temp.work_items_v12 SET state = 'blocked' WHERE state = 'deferred'")
    connection.execute("DROP TABLE work_items")
    connection.execute(_create_statement("work_items"))
    connection.execute("INSERT INTO work_items SELECT * FROM temp.work_items_v12")
    connection.execute("DROP TABLE temp.work_items_v12")
    connection.execute("DROP TABLE work_item_state_counts")
    connection.execute(_create_statement("work_item_state_counts"))
    connection.executemany(
        """INSERT INTO work_item_state_counts (state, item_count)
           VALUES (?, (SELECT count(*) FROM work_items WHERE state = ?))""",
        tuple((state.value, state.value) for state in stored_state.StoredWorkItemState),
    )
    connection.execute(_create_statement("work_item_unblockers"))
    connection.executemany(
        "INSERT INTO work_item_unblockers (item_id, unblocker_json) VALUES (?, ?)",
        tuple(
            (item_id, unblockers.encode_unblocker(unblocker).decode("utf-8"))
            for item_id, unblocker in sorted(holds.held.items())
        ),
    )


_PROJECTION_CONDITION = "This v12 hold is unlabelled in a verification-only projection."


def project_v12_for_current_readers(connection: sqlite3.Connection) -> None:
    """Advance an in-memory v12 projection so retained predecessor checks can use the current readers.

    Holds the ledger cannot label itself carry a fixed verification-only condition. The projection is discarded
    after its check and never becomes a ledger, so no label is ever invented for stored state.
    """

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        with database.write_transaction(connection):
            database._verify_v12_schema(connection)
            automatic, needed = classify_holds(connection)
            _write_v13(
                connection,
                ResolvedHolds(
                    {
                        **automatic,
                        **{
                            subject.item_id: work_models.WaitingForOutsideCondition(_PROJECTION_CONDITION)
                            for subject in needed
                        },
                    },
                    frozenset(),
                ),
            )
            database._verify_v13_schema(connection)
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def upgrade_v12_connection(
    connection: sqlite3.Connection,
    source_sha256: str,
    labels: LabelInput | None,
    expected_postimage: str | None,
) -> bool:
    """Label every live hold and switch authority atomically, rolling back any failed validation."""

    connection.row_factory = sqlite3.Row
    # Rebuilding work_items must not cascade its referencing rows, so foreign keys pause for this transaction.
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        with database.write_transaction(connection):
            if database._read_pinboard_schema_version(connection) == 13:
                database._verify_v13_schema(connection)
                return False
            database._verify_v12_schema(connection)
            database.verify_database_integrity(connection)
            _write_v13(connection, resolve_unblockers(connection, source_sha256, labels))
            database._verify_v13_schema(connection)
            database.verify_database_integrity(connection)
            if (
                expected_postimage is not None
                and migration_database.schema_logical_digest(connection) != expected_postimage
            ):
                raise StorageError(
                    StorageErrorCode.INVALID_STATE,
                    "Unblocker migration differs from the previewed postimage; the predecessor remains authoritative.",
                )
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
    return True


def inspect_v12_migration(path: Path, labels: LabelInput | None) -> tuple[str, str]:
    """Predict the exact v13 postimage of one verified v12 ledger and its labels without effects."""

    try:
        source = sha256(path.read_bytes()).hexdigest()
        with closing(
            sqlite3.connect(database._build_database_uri(path, OpenMode.READ_ONLY), uri=True, isolation_level=None)
        ) as connection:
            database._configure_connection(connection)
            database._verify_v12_schema(connection)
            database.verify_database_integrity(connection)
            with closing(sqlite3.connect(":memory:", isolation_level=None)) as simulation:
                connection.backup(simulation)
                database._configure_connection(simulation)
                upgrade_v12_connection(simulation, source, labels, None)
                return source, migration_database.schema_logical_digest(simulation)
    except OSError as error:
        raise StorageError(
            StorageErrorCode.IO_ERROR, "The v12 schema migration could not read the database."
        ) from error
    except sqlite3.Error as error:
        raise database.translate_database_error(error, opening=True).with_database_path(path) from error


def migrate_v12_database(path: Path, source_sha256: str, labels: LabelInput | None, expected_postimage: str) -> bool:
    with migration_database.migration_connection(path) as connection:
        return upgrade_v12_connection(connection, source_sha256, labels, expected_postimage)
