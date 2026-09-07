import contextlib
import io
import sqlite3
import tempfile
import unittest
from collections.abc import Generator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite import database as sqlite_database
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.database import initialize_database, open_database
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.domain import authority_models, work_models
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import AttemptId, HostId, ItemId, LeaseId, TaskId
from pinboard.interfaces.cli import main
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store


class AuthorityStatusReadTest(unittest.TestCase):
    def initialized_state(self, state: stored_state.StoredWorkState) -> tuple[Path, Path, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, state)
        return project, roots.work_root, store

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    @contextlib.contextmanager
    def record_store_reads(self) -> Generator[tuple[set[str], list[str]]]:
        read_tables: set[str] = set()
        statements: list[str] = []
        original_open = sqlite_store.open_database

        def traced_open(path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open(path, mode)

            def authorize(
                action: int,
                argument: str | None,
                _secondary_argument: str | None,
                _database: str | None,
                _trigger: str | None,
            ) -> int:
                if action == sqlite3.SQLITE_READ and argument is not None:
                    read_tables.add(argument)
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(sqlite_store, "open_database", traced_open):
            yield read_tables, statements

    def assert_keyed_status_queries(self, database: Path, statements: list[str]) -> None:
        selects = tuple(statement for statement in statements if statement.lstrip().upper().startswith("SELECT"))
        self.assertTrue(selects)
        connection = sqlite3.connect(database)
        try:
            for statement in selects:
                with self.subTest(statement=statement):
                    plan = tuple(
                        str(row[3]).upper() for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}").fetchall()
                    )
                    self.assertTrue(any("SEARCH " in detail for detail in plan), plan)
                    self.assertFalse(any("SCAN " in detail for detail in plan), plan)
        finally:
            connection.close()

    def state_with_unrelated_attempt_authority(self, count: int = 64) -> stored_state.StoredWorkState:
        state = complete_sqlite_state()
        template = state.lifecycle.attempts[0]
        attempts = tuple(
            replace(
                template,
                attempt_id=AttemptId(f"unrelated-{index}"),
                item_id=ItemId("work-b"),
                state=work_models.AttemptState.DONE,
                branch=f"codex/unrelated-{index}",
            )
            for index in range(count)
        )
        counters = tuple(stored_state.AttemptLeaseCounter(attempt.attempt_id, 1) for attempt in attempts)
        generations = tuple(
            stored_state.AttemptLeaseGeneration(
                attempt.attempt_id,
                1,
                LeaseId(f"lease-{index}"),
                TaskId(f"task-{index}"),
                HostId("host-a"),
            )
            for index, attempt in enumerate(attempts)
        )
        leases = tuple(
            stored_state.StoredAttemptLease(
                attempt.attempt_id,
                1,
                SQLITE_NOW,
                SQLITE_NOW + timedelta(minutes=1),
                authority_models.AttemptLeaseStatus.RELEASED,
            )
            for attempt in attempts
        )
        return replace(
            state,
            lifecycle=replace(state.lifecycle, attempts=(*state.lifecycle.attempts, *attempts)),
            authority=replace(
                state.authority,
                attempt_counters=(*state.authority.attempt_counters, *counters),
                attempt_generations=(*state.authority.attempt_generations, *generations),
                attempt_leases=(*state.authority.attempt_leases, *leases),
            ),
        )

    def state_with_preparation(
        self,
        *,
        status: authority_models.PreparationLeaseStatus = authority_models.PreparationLeaseStatus.ACTIVE,
        historical: bool = False,
        unrelated_count: int = 0,
    ) -> stored_state.StoredWorkState:
        state = self.state_with_unrelated_attempt_authority()
        item_id = ItemId("work-c")
        original = next(value for value in state.lifecycle.definition_revisions if value.item_id == item_id)
        definitions = state.lifecycle.definition_revisions
        if historical:
            revised_definition = replace(original.definition, objective="The current objective changed.")
            revised_digest = work_item_definition_digest(revised_definition)
            assert isinstance(revised_digest, str)
            definitions = (
                *definitions,
                stored_state.ItemDefinitionRevision(
                    item_id,
                    2,
                    revised_digest,
                    revised_definition,
                    "Accepted revised test definition.",
                    TaskId("test-source"),
                    original.digest,
                    revised_digest,
                    state.lifecycle.project.revision,
                    SQLITE_NOW,
                ),
            )
        selected_item = next(value for value in state.lifecycle.work_items if value.item_id == item_id)
        unrelated_items = tuple(
            replace(
                selected_item,
                item_id=ItemId(f"unrelated-preparation-{index}"),
                queue_position=5 + index,
            )
            for index in range(unrelated_count)
        )
        unrelated_definitions = []
        for index, item in enumerate(unrelated_items):
            definition = replace(original.definition, objective=f"Unrelated preparation objective {index}.")
            digest = work_item_definition_digest(definition)
            assert isinstance(digest, str)
            unrelated_definitions.append(
                replace(
                    original,
                    item_id=item.item_id,
                    digest=digest,
                    definition=definition,
                    after_digest=digest,
                )
            )
        unrelated_counters = tuple(stored_state.PreparationLeaseCounter(item.item_id, 1) for item in unrelated_items)
        unrelated_generations = tuple(
            stored_state.PreparationLeaseGeneration(
                item.item_id,
                1,
                LeaseId(f"unrelated-preparation-lease-{index}"),
                TaskId(f"unrelated-preparer-{index}"),
                HostId("host-a"),
            )
            for index, item in enumerate(unrelated_items)
        )
        unrelated_leases = tuple(
            stored_state.StoredPreparationLease(
                item.item_id,
                1,
                1,
                unrelated_definitions[index].digest,
                SQLITE_NOW,
                SQLITE_NOW + timedelta(minutes=5),
                authority_models.PreparationLeaseStatus.RELEASED,
            )
            for index, item in enumerate(unrelated_items)
        )
        authority = replace(
            state.authority,
            preparation_counters=(stored_state.PreparationLeaseCounter(item_id, 2), *unrelated_counters),
            preparation_generations=(
                stored_state.PreparationLeaseGeneration(
                    item_id, 2, LeaseId("preparation-lease"), TaskId("preparer"), HostId("host-a")
                ),
                *unrelated_generations,
            ),
            preparation_leases=(
                stored_state.StoredPreparationLease(
                    item_id,
                    2,
                    original.revision,
                    original.digest,
                    SQLITE_NOW,
                    SQLITE_NOW + timedelta(minutes=5),
                    status,
                ),
                *unrelated_leases,
            ),
        )
        return replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=(*state.lifecycle.work_items, *unrelated_items),
                definition_revisions=(*definitions, *unrelated_definitions),
            ),
            authority=authority,
        )

    def test_installed_status_reads_are_exact_with_unrelated_authority_growth(self) -> None:
        project, work, _store = self.initialized_state(self.state_with_preparation(unrelated_count=64))
        common = ("--project-root", str(project), "--work-root", str(work))

        with (
            patch.object(SQLiteWorkStore, "snapshot", side_effect=AssertionError("complete snapshot used")),
            self.record_store_reads() as attempt_reads,
        ):
            result, stdout, stderr = self.run_cli(*common, "attempt", "status", "--attempt-id", "work-a-1")
        self.assertEqual(0, result, stderr)
        self.assertIn("status=active", stdout)
        attempt_tables, attempt_statements = attempt_reads
        self.assertEqual(
            {"attempts", "attempt_lease_counters", "attempt_lease_generations", "attempt_leases"},
            attempt_tables,
        )
        self.assert_keyed_status_queries(work / "state.sqlite3", attempt_statements)

        with (
            patch.object(SQLiteWorkStore, "snapshot", side_effect=AssertionError("complete snapshot used")),
            patch("pinboard.interfaces.preparation_authority.datetime") as clock,
            self.record_store_reads() as preparation_reads,
        ):
            clock.now.return_value = SQLITE_NOW + timedelta(minutes=1)
            result, stdout, stderr = self.run_cli(*common, "preparation", "status", "--item-id", "work-c")
        self.assertEqual(0, result, stderr)
        self.assertIn("status=active", stdout)
        self.assertEqual(1, clock.now.call_count)
        preparation_tables, preparation_statements = preparation_reads
        self.assertEqual(
            {
                "project_meta",
                "work_items",
                "work_item_definition_revisions",
                "preparation_lease_counters",
                "preparation_lease_generations",
                "preparation_leases",
            },
            preparation_tables,
        )
        self.assert_keyed_status_queries(work / "state.sqlite3", preparation_statements)

    def test_status_preserves_distinct_expiry_and_historical_pin_contracts(self) -> None:
        project, work, _store = self.initialized_state(self.state_with_preparation())
        common = ("--project-root", str(project), "--work-root", str(work))
        expires_at = SQLITE_NOW + timedelta(minutes=5)
        for observed_at, expected in (
            (expires_at - timedelta(microseconds=1), "active"),
            (expires_at, "expired"),
            (expires_at + timedelta(microseconds=1), "expired"),
        ):
            with (
                self.subTest(preparation_observed_at=observed_at),
                patch("pinboard.interfaces.preparation_authority.datetime") as clock,
            ):
                clock.now.return_value = observed_at
                result, stdout, stderr = self.run_cli(*common, "preparation", "status", "--item-id", "work-c")
            self.assertEqual(0, result, stderr)
            self.assertIn(f"status={expected}", stdout)
            self.assertEqual(1, clock.now.call_count)

        state = self.state_with_preparation(
            status=authority_models.PreparationLeaseStatus.RELEASED,
            historical=True,
        )
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))

        with patch("pinboard.interfaces.preparation_authority.datetime") as clock:
            clock.now.return_value = SQLITE_NOW + timedelta(days=1)
            result, stdout, stderr = self.run_cli(*common, "preparation", "status", "--item-id", "work-c")
        self.assertEqual(0, result, stderr)
        self.assertIn("definition_revision=1", stdout)
        self.assertIn("status=released", stdout)
        self.assertEqual(1, clock.now.call_count)

        result, stdout, stderr = self.run_cli(*common, "attempt", "status", "--attempt-id", "work-a-1")
        self.assertEqual(0, result, stderr)
        self.assertIn("status=active", stdout)

        for retained_status in authority_models.AttemptLeaseStatus:
            retained = complete_sqlite_state()
            retained = replace(
                retained,
                authority=replace(
                    retained.authority,
                    attempt_leases=tuple(
                        replace(
                            lease,
                            expires_at=SQLITE_NOW - timedelta(days=1),
                            state=retained_status,
                        )
                        for lease in retained.authority.attempt_leases
                    ),
                ),
            )
            retained_project, retained_work, _retained_store = self.initialized_state(retained)
            with self.subTest(attempt_retained_status=retained_status):
                result, stdout, stderr = self.run_cli(
                    "--project-root",
                    str(retained_project),
                    "--work-root",
                    str(retained_work),
                    "attempt",
                    "status",
                    "--attempt-id",
                    "work-a-1",
                )
            self.assertEqual(0, result, stderr)
            self.assertIn(f"status={retained_status.value}", stdout)

    def test_selected_authority_corruption_remains_invalid(self) -> None:
        project, work, _store = self.initialized_state(self.state_with_preparation())
        database = work / "state.sqlite3"
        raw = sqlite3.connect(database)
        try:
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute(
                "DELETE FROM attempt_lease_generations WHERE attempt_id = ?",
                ("work-a-1",),
            )
            raw.commit()
        finally:
            raw.close()

        result, _stdout, stderr = self.run_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(work),
            "attempt",
            "status",
            "--attempt-id",
            "work-a-1",
        )
        self.assertEqual(12, result)
        self.assertIn("WORK_STATE_INVALID", stderr)

        stale_project, stale_work, _stale_store = self.initialized_state(
            self.state_with_preparation(
                status=authority_models.PreparationLeaseStatus.RELEASED,
                historical=True,
            )
        )
        stale_database = stale_work / "state.sqlite3"
        raw = sqlite3.connect(stale_database)
        try:
            raw.execute(
                "UPDATE preparation_leases SET status = 'active' WHERE item_id = ?",
                ("work-c",),
            )
            raw.commit()
        finally:
            raw.close()

        stale_result, _stale_stdout, stale_stderr = self.run_cli(
            "--project-root",
            str(stale_project),
            "--work-root",
            str(stale_work),
            "preparation",
            "status",
            "--item-id",
            "work-c",
        )
        self.assertEqual(12, stale_result)
        self.assertIn("WORK_STATE_INVALID", stale_stderr)

    def test_explicit_validation_owns_complete_integrity_checks(self) -> None:
        project, work, _store = self.initialized_state(self.state_with_unrelated_attempt_authority())
        database = work / "state.sqlite3"
        raw = sqlite3.connect(database)
        try:
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute(
                "DELETE FROM attempt_lease_generations WHERE attempt_id = ?",
                ("unrelated-0",),
            )
            raw.commit()
        finally:
            raw.close()

        routine_statements: list[str] = []
        original_connect = sqlite3.connect

        def traced_connect(
            database: str,
            *,
            uri: bool,
            timeout: float,
            isolation_level: None,
        ) -> sqlite3.Connection:
            connection = original_connect(
                database,
                uri=uri,
                timeout=timeout,
                isolation_level=isolation_level,
            )
            connection.set_trace_callback(routine_statements.append)
            return connection

        with patch.object(sqlite_database.sqlite3, "connect", traced_connect):
            connection = open_database(database, OpenMode.READ_ONLY)
            connection.close()
            result, stdout, stderr = self.run_cli(
                "--project-root",
                str(project),
                "--work-root",
                str(work),
                "attempt",
                "status",
                "--attempt-id",
                "work-a-1",
            )
        self.assertEqual(0, result, stderr)
        self.assertIn("status=active", stdout)
        normalized_routine = {statement.strip().lower() for statement in routine_statements}
        self.assertNotIn("pragma quick_check", normalized_routine)
        self.assertNotIn("pragma foreign_key_check", normalized_routine)

        traced_statements: list[str] = []

        def traced_validation_connect(
            database: str,
            *,
            uri: bool,
            timeout: float,
            isolation_level: None,
        ) -> sqlite3.Connection:
            connection = original_connect(
                database,
                uri=uri,
                timeout=timeout,
                isolation_level=isolation_level,
            )
            connection.set_trace_callback(traced_statements.append)
            return connection

        with patch.object(sqlite_database.sqlite3, "connect", traced_validation_connect):
            validation_result, validation_stdout, _validation_stderr = self.run_cli(
                "--project-root", str(project), "--work-root", str(work), "validate"
            )
        self.assertEqual(10, validation_result)
        self.assertIn("WORK_STATE_INVALID", validation_stdout)
        normalized = {statement.strip().lower() for statement in traced_statements}
        self.assertIn("pragma quick_check", normalized)
        self.assertIn("pragma foreign_key_check", normalized)


if __name__ == "__main__":
    unittest.main()
