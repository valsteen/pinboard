import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite import lifecycle, proposals
from pinboard.adapters.sqlite import state as sqlite_state
from pinboard.adapters.sqlite.artifacts import accept_checkpoint_artifact
from pinboard.adapters.sqlite.authority import (
    validate_attempt_authority,
    write_attempt_authority,
    write_coordination_authority,
)
from pinboard.adapters.sqlite.database import initialize_database, open_database, write_transaction
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.artifacts import EvidenceArtifactRef
from pinboard.application.decision_projection import project_decision_snapshot
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, ItemId, ProposalId
from pinboard.interfaces.work_state import read_state_for_validation
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store


class SQLiteEffectContractTest(unittest.TestCase):
    def _store(self) -> tuple[Path, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, complete_sqlite_state())
        return roots.database_path, store

    def test_checkpoint_artifact_identity_is_exact(self) -> None:
        path, store = self._store()
        state = store.snapshot()
        existing = next(value for value in state.artifact_references if value.kind == work_models.ArtifactKind.EVIDENCE)
        published = EvidenceArtifactRef(
            existing.key,
            existing.revision,
            existing.selector,
            existing.content_sha256,
            existing.size_bytes,
        )
        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            self.assertEqual(
                existing.artifact_ref_id,
                accept_checkpoint_artifact(
                    connection,
                    state,
                    published,
                    existing.artifact_ref_id,
                    state.lifecycle.project.revision + 1,
                    SQLITE_NOW,
                ),
            )
            with self.assertRaises(StorageError) as conflicting:
                accept_checkpoint_artifact(
                    connection,
                    state,
                    replace(published, content_sha256="0" * 64),
                    ArtifactRefId(int(existing.artifact_ref_id) + 1),
                    state.lifecycle.project.revision + 1,
                    SQLITE_NOW,
                )
            self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, conflicting.exception.code)
        finally:
            connection.close()

    def test_integrity_scans_are_explicit_validation_work(self) -> None:
        path, _store = self._store()
        statements: list[str] = []
        connect = sqlite3.connect

        def traced_connect(
            database: str,
            timeout: float = 5.0,
            isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None = "DEFERRED",
            *,
            uri: bool = False,
        ) -> sqlite3.Connection:
            connection = connect(
                database,
                timeout=timeout,
                isolation_level=isolation_level,
                uri=uri,
            )
            connection.set_trace_callback(statements.append)
            return connection

        with patch("pinboard.adapters.sqlite.database.sqlite3.connect", side_effect=traced_connect):
            connection = open_database(path, OpenMode.READ_ONLY)
            connection.close()
        self.assertFalse(any("quick_check" in statement for statement in statements))
        self.assertFalse(any("foreign_key_check" in statement for statement in statements))

        statements.clear()
        with patch("pinboard.adapters.sqlite.database.sqlite3.connect", side_effect=traced_connect):
            validated = read_state_for_validation(path.parent)
        self.assertNotIsInstance(validated, StorageError)
        self.assertTrue(any("quick_check" in statement for statement in statements))
        self.assertTrue(any("foreign_key_check" in statement for statement in statements))

    def test_exact_definition_work_is_stable_as_unrelated_histories_grow(self) -> None:
        path, _store = self._store()

        def inspect() -> tuple[tuple[str, ...], int, int]:
            tables: set[str] = set()
            progress_steps = 0

            def authorize(
                action: int,
                table: str | None,
                _column: str | None,
                _database: str | None,
                _trigger: str | None,
            ) -> int:
                if action == sqlite3.SQLITE_READ and table is not None:
                    tables.add(table)
                return sqlite3.SQLITE_OK

            def count_progress() -> int:
                nonlocal progress_steps
                progress_steps += 1
                return 0

            connection = open_database(path, OpenMode.READ_ONLY)
            try:
                connection.set_authorizer(authorize)
                connection.set_progress_handler(count_progress, 1)
                selected = sqlite_state.read_item_definition_state(connection, ItemId("work-a"))
            finally:
                connection.close()
            return tuple(sorted(tables)), progress_steps, selected.lifecycle.definition_revisions[-1].revision

        baseline = inspect()
        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                definition_revision = int(
                    connection.execute(
                        "SELECT MAX(definition_revision) FROM work_item_definition_revisions WHERE item_id = 'work-c'"
                    ).fetchone()[0]
                )
                connection.execute(
                    """
                    WITH RECURSIVE growth(number) AS (
                        SELECT 1
                        UNION ALL
                        SELECT number + 1 FROM growth WHERE number < 400
                    ), template AS (
                        SELECT definition_digest, definition_json, reason, source_task_id,
                               after_digest, accepted_project_revision, accepted_at
                        FROM work_item_definition_revisions
                        WHERE item_id = 'work-c'
                        ORDER BY definition_revision DESC
                        LIMIT 1
                    )
                    INSERT INTO work_item_definition_revisions (
                        item_id, definition_revision, definition_digest, definition_json, reason,
                        source_task_id, before_digest, after_digest, accepted_project_revision, accepted_at
                    )
                    SELECT 'work-c', ? + number, definition_digest, definition_json, reason,
                           source_task_id, definition_digest, after_digest, accepted_project_revision, accepted_at
                    FROM growth CROSS JOIN template
                    """,
                    (definition_revision,),
                )
                history_id, project_revision = connection.execute(
                    "SELECT MAX(history_id), MAX(project_revision) FROM transition_history"
                ).fetchone()
                connection.execute(
                    """
                    WITH RECURSIVE growth(number) AS (
                        SELECT 1
                        UNION ALL
                        SELECT number + 1 FROM growth WHERE number < 400
                    ), template AS (
                        SELECT action_kind, subject_id, artifact_ref_id, artifact_kind,
                               authorization_kind, actor_task_id, actor_host_id, input_schema,
                               input_json, outcome_schema, outcome_json, committed_at
                        FROM transition_history
                        ORDER BY history_id
                        LIMIT 1
                    )
                    INSERT INTO transition_history (
                        history_id, project_revision, action_id, action_kind, subject_id,
                        artifact_ref_id, artifact_kind, authorization_kind, actor_task_id,
                        actor_host_id, input_schema, input_json, outcome_schema, outcome_json, committed_at
                    )
                    SELECT ? + number, ? + number, 'unrelated-growth-' || number, action_kind, subject_id,
                           artifact_ref_id, artifact_kind, authorization_kind, actor_task_id,
                           actor_host_id, input_schema, input_json, outcome_schema, outcome_json, committed_at
                    FROM growth CROSS JOIN template
                    """,
                    (history_id, project_revision),
                )
                connection.execute(
                    "UPDATE project_meta SET revision = ? WHERE singleton = 1",
                    (int(project_revision) + 400,),
                )
        finally:
            connection.close()

        grown = inspect()
        self.assertEqual(baseline, grown)
        self.assertEqual(
            ("project_meta", "work_item_definition_revisions", "work_items"),
            grown[0],
        )

    def test_live_decision_work_is_stable_as_terminal_items_grow(self) -> None:
        path, store = self._store()

        def inspect() -> tuple[int, int]:
            progress_steps = 0

            def count_progress() -> int:
                nonlocal progress_steps
                progress_steps += 1
                return 0

            connection = open_database(path, OpenMode.READ_ONLY)
            try:
                connection.set_progress_handler(count_progress, 1)
                selected = sqlite_state.read_decision_state(
                    connection,
                    subject_item_ids=(ItemId("work-b"),),
                )
            finally:
                connection.close()
            return progress_steps, len(selected.lifecycle.work_items)

        baseline = inspect()
        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                connection.execute(
                    """
                    WITH RECURSIVE growth(number) AS (
                        SELECT 1
                        UNION ALL
                        SELECT number + 1 FROM growth WHERE number < 400
                    )
                    INSERT INTO work_items (
                        item_id, state, timing, source, outcome_evidence, next_action, notes,
                        subject_revision, recorded_at, updated_at, queue_position
                    )
                    SELECT 'terminal-growth-' || number, 'done', NULL, NULL, 'complete', NULL, NULL,
                           1, ?, ?, NULL
                    FROM growth
                    """,
                    (SQLITE_NOW.isoformat(), SQLITE_NOW.isoformat()),
                )
                connection.execute(
                    """
                    WITH RECURSIVE growth(number) AS (
                        SELECT 1
                        UNION ALL
                        SELECT number + 1 FROM growth WHERE number < 400
                    ), template AS (
                        SELECT definition_digest, definition_json, reason, source_task_id,
                               after_digest, accepted_project_revision, accepted_at
                        FROM work_item_definition_revisions
                        WHERE item_id = 'work-c'
                        ORDER BY definition_revision DESC
                        LIMIT 1
                    )
                    INSERT INTO work_item_definition_revisions (
                        item_id, definition_revision, definition_digest, definition_json, reason,
                        source_task_id, before_digest, after_digest, accepted_project_revision, accepted_at
                    )
                    SELECT 'terminal-growth-' || number, 1, definition_digest, definition_json, reason,
                           source_task_id, NULL, after_digest, accepted_project_revision, accepted_at
                    FROM growth CROSS JOIN template
                    """
                )
        finally:
            connection.close()

        grown = inspect()
        self.assertEqual(baseline, grown)
        self.assertEqual(len(store.live_state().lifecycle.work_items) + 1, grown[1])

    def test_live_proposal_work_is_stable_as_disposed_proposals_grow(self) -> None:
        path, store = self._store()

        def inspect() -> tuple[int, int]:
            progress_steps = 0
            connect = sqlite3.connect

            def count_progress() -> int:
                nonlocal progress_steps
                progress_steps += 1
                return 0

            def traced_connect(
                database: str,
                timeout: float = 5.0,
                isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None = "DEFERRED",
                *,
                uri: bool = False,
            ) -> sqlite3.Connection:
                connection = connect(database, timeout=timeout, isolation_level=isolation_level, uri=uri)
                connection.set_progress_handler(count_progress, 1)
                return connection

            with patch("pinboard.adapters.sqlite.database.sqlite3.connect", side_effect=traced_connect):
                selected = store.live_state()
            return progress_steps, len(selected.proposals.proposals)

        baseline = inspect()
        self.assertEqual(1, baseline[1])
        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                connection.execute(
                    """
                    WITH RECURSIVE growth(number) AS (
                        SELECT 1
                        UNION ALL
                        SELECT number + 1 FROM growth WHERE number < 400
                    )
                    INSERT INTO proposals (
                        proposal_id, created_at, recorded_at, source_task_id, user_label, trigger,
                        why_it_matters, relation_kind, relation_item_id, effect, unlock,
                        urgency_evidence, disposition, disposition_target_item_id, disposition_reason,
                        subject_revision, disposition_recorded_at
                    )
                    SELECT 'disposed-growth-' || number, ?, ?, 'source-task', 'Disposed growth',
                           'Unrelated history', 'Unrelated history', 'independent', NULL,
                           'No current effect', 'No current unlock', 'Already disposed',
                           'rejected', NULL, 'No longer relevant', 2, ?
                    FROM growth
                    """,
                    (SQLITE_NOW.isoformat(), SQLITE_NOW.isoformat(), SQLITE_NOW.isoformat()),
                )
        finally:
            connection.close()

        grown = inspect()
        self.assertEqual(baseline, grown)

    def test_unchanged_dependencies_are_not_rewritten(self) -> None:
        path, store = self._store()
        state = store.snapshot()
        item_id = state.lifecycle.dependencies[0].item_id
        dependencies = tuple(link.dependency_id for link in state.lifecycle.dependencies if link.item_id == item_id)
        self.assertTrue(dependencies)

        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            connection.execute(
                """
                CREATE TEMP TRIGGER reject_dependency_rewrite
                BEFORE DELETE ON item_dependencies
                WHEN OLD.item_id = 'work-a'
                BEGIN
                    SELECT RAISE(ABORT, 'unchanged dependencies were rewritten');
                END
                """
            )
            lifecycle.replace_dependencies(connection, item_id, dependencies)
            with self.assertRaises(sqlite3.IntegrityError):
                lifecycle.replace_dependencies(connection, item_id, ())
        finally:
            connection.close()

    def test_complete_state_rejects_missing_project_invalid_queue_and_reinitialization(self) -> None:
        path, store = self._store()
        before = store.snapshot()

        raw = sqlite3.connect(path)
        raw.row_factory = sqlite3.Row
        try:
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute("BEGIN")
            raw.execute("DELETE FROM project_meta")
            with self.assertRaises(StorageError) as missing_project:
                sqlite_state.read_state(raw)
            self.assertEqual(StorageErrorCode.INVALID_STATE, missing_project.exception.code)
            raw.rollback()
        finally:
            raw.close()

        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                connection.execute("UPDATE work_items SET queue_position = 99 WHERE queue_position = 1")
                with self.assertRaises(StorageError) as invalid_queue:
                    sqlite_state.read_state(connection)
                self.assertEqual(StorageErrorCode.INVALID_STATE, invalid_queue.exception.code)
                connection.rollback()
        finally:
            connection.close()

        with self.assertRaises(StorageError) as occupied:
            initialize_store(store, before)
        self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, occupied.exception.code)
        self.assertEqual(before, store.snapshot())

    def test_lifecycle_lookup_and_cas_failures_preserve_the_ledger(self) -> None:
        path, store = self._store()
        before = store.snapshot()
        with self.assertRaises(StorageError) as missing_item:
            lifecycle.require_stored_item(before, ItemId("missing"))
        self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, missing_item.exception.code)
        with self.assertRaises(StorageError) as missing_attempt:
            lifecycle.require_stored_attempt(before, AttemptId("missing-1"))
        self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, missing_attempt.exception.code)

        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            stale_focus = lifecycle.update_focus(
                connection,
                replace(before.focus, subject_revision=-1),
                replace(before.focus, subject_revision=before.focus.subject_revision + 1),
            )
        finally:
            connection.close()
        self.assertIsInstance(stale_focus, DecisionFailure)
        self.assertEqual(before, store.snapshot())

        for effect, argument in ((lifecycle.compact_queue, 1), (lifecycle.make_queue_space, 1)):
            connection = open_database(path, OpenMode.READ_WRITE)
            try:
                connection.execute(
                    """
                    CREATE TEMP TRIGGER arrange_real_queue_staleness
                    BEFORE UPDATE OF queue_position ON work_items
                    BEGIN
                        SELECT RAISE(IGNORE);
                    END
                    """
                )
                with write_transaction(connection):
                    result = effect(connection, before, argument)
            finally:
                connection.close()
            self.assertIsInstance(result, DecisionFailure)
            self.assertEqual(before, store.snapshot())

    def test_proposal_decode_and_stale_disposition_contracts_are_explicit(self) -> None:
        path, store = self._store()
        before = store.snapshot()
        proposal_id = before.proposals.proposals[0].proposal_id
        for statement, parameters in (
            (
                "UPDATE proposals SET relation_kind = 'follow-up', relation_item_id = NULL WHERE proposal_id = ?",
                (proposal_id,),
            ),
            (
                """
                UPDATE proposals
                SET disposition = 'accepted', disposition_target_item_id = NULL,
                    disposition_recorded_at = ?
                WHERE proposal_id = ?
                """,
                (SQLITE_NOW.isoformat(), proposal_id),
            ),
        ):
            connection = open_database(path, OpenMode.READ_WRITE)
            try:
                connection.execute("PRAGMA ignore_check_constraints = ON")
                with write_transaction(connection):
                    connection.execute(statement, parameters)
                    with self.assertRaises(StorageError) as malformed:
                        sqlite_state.read_state(connection)
                    self.assertEqual(StorageErrorCode.INVALID_STATE, malformed.exception.code)
                    connection.rollback()
            finally:
                connection.close()

        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            stale = proposals.set_proposal_disposition(
                connection,
                ProposalId("missing-proposal"),
                work_models.ReturnedProposalDisposition("No longer relevant.", SQLITE_NOW),
                before.lifecycle.project.revision + 1,
            )
        finally:
            connection.close()
        self.assertIsInstance(stale, DecisionFailure)
        self.assertEqual(before, store.snapshot())

    def test_authority_validation_and_coordination_insert_staleness_are_distinct(self) -> None:
        path, store = self._store()
        before = store.snapshot()
        without_counter = replace(
            before,
            authority=replace(before.authority, attempt_counters=()),
        )
        with self.assertRaises(StorageError) as invalid_generation:
            validate_attempt_authority(without_counter, StorageErrorCode.INVALID_STATE)
        self.assertEqual(StorageErrorCode.INVALID_STATE, invalid_generation.exception.code)

        mismatched_lease = replace(
            before,
            authority=replace(
                before.authority,
                attempt_leases=(
                    replace(
                        before.authority.attempt_leases[0],
                        generation=before.authority.attempt_leases[0].generation - 1,
                    ),
                ),
            ),
        )
        with self.assertRaises(StorageError) as invalid_lease:
            validate_attempt_authority(mismatched_lease, StorageErrorCode.INVALID_STATE)
        self.assertEqual(StorageErrorCode.INVALID_STATE, invalid_lease.exception.code)

        coordination = project_decision_snapshot(before, SQLITE_NOW).coordination_lease
        assert coordination is not None
        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            stale = write_coordination_authority(
                connection,
                None,
                replace(coordination, expires_at=coordination.expires_at + timedelta(minutes=1)),
                "The coordination authority already exists.",
            )
        finally:
            connection.close()
        self.assertIsInstance(stale, DecisionFailure)
        self.assertEqual(before, store.snapshot())

    def test_expected_insert_key_conflicts_are_stale_but_other_constraints_are_exceptional(self) -> None:
        path, store = self._store()
        before = store.snapshot()
        attempt = before.lifecycle.attempts[0]
        duplicate_attempt = decision_models.ActivationChange(
            ItemId("work-c"),
            work_models.WorkState.READY,
            attempt.attempt_id,
            attempt.brief_artifact_ref_id,
            "duplicate-attempt",
            "base",
            "worker",
        )
        unrelated_live_attempt_conflict = replace(
            duplicate_attempt,
            item=attempt.item_id,
            attempt=AttemptId("other-live-attempt"),
        )
        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                stale_attempt = lifecycle.insert_attempt(
                    connection,
                    before,
                    duplicate_attempt,
                    before.lifecycle.project.revision + 1,
                    SQLITE_NOW,
                )
            self.assertIsInstance(stale_attempt, DecisionFailure)

            with self.assertRaises(StorageError) as unrelated_unique, write_transaction(connection):
                lifecycle.insert_attempt(
                    connection,
                    before,
                    unrelated_live_attempt_conflict,
                    before.lifecycle.project.revision + 1,
                    SQLITE_NOW,
                )
            self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, unrelated_unique.exception.code)

            with self.assertRaises(StorageError) as unrelated_foreign_key, write_transaction(connection):
                lifecycle.insert_attempt(
                    connection,
                    before,
                    replace(
                        duplicate_attempt,
                        item=ItemId("missing-item"),
                        attempt=AttemptId("missing-item-attempt"),
                    ),
                    before.lifecycle.project.revision + 1,
                    SQLITE_NOW,
                )
            self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, unrelated_foreign_key.exception.code)

            coordination = project_decision_snapshot(before, SQLITE_NOW).coordination_lease
            assert coordination is not None
            with self.assertRaises(StorageError) as unrelated_check, write_transaction(connection):
                connection.execute("DELETE FROM coordination_lease")
                write_coordination_authority(
                    connection,
                    None,
                    replace(coordination, generation=0),
                    "The coordination authority already exists.",
                )
            self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, unrelated_check.exception.code)

            retained = before.authority.attempt_leases[0]
            command = project_decision_snapshot(before, SQLITE_NOW).command_attempt_authorities[0]
            current = authority_models.AttemptLeaseAuthority(
                command.host_epoch,
                command.attempt,
                command.item,
                command.task_id,
                command.host_id,
                command.lease_id,
                command.generation,
                retained.acquired_at,
                command.expires_at,
                authority_models.AttemptLeaseStatus.ACTIVE,
            )
            duplicate_current = authority_models.AttemptAuthorityDecision(
                command.attempt,
                command.generation,
                command.generation,
                None,
                current,
            )
            with write_transaction(connection):
                stale_current = write_attempt_authority(connection, duplicate_current)
            self.assertIsInstance(stale_current, DecisionFailure)

            missing_attempt = AttemptId("missing-attempt")
            invalid_counter = authority_models.AttemptAuthorityDecision(
                missing_attempt,
                0,
                -1,
                None,
                replace(current, attempt=missing_attempt),
            )
            with self.assertRaises(StorageError) as unrelated_counter_check, write_transaction(connection):
                write_attempt_authority(connection, invalid_counter)
            self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, unrelated_counter_check.exception.code)
        finally:
            connection.close()
        self.assertEqual(before, store.snapshot())


if __name__ == "__main__":
    unittest.main()
