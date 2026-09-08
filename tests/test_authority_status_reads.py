import contextlib
import hashlib
import io
import json
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
from pinboard.interfaces.work_briefs import canonical_work_brief_bytes
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store
from tests.work_brief_support import work_a_brief


class AuthorityStatusReadTest(unittest.TestCase):
    def initialized_state(self, state: stored_state.StoredWorkState) -> tuple[Path, Path, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, state)
        return project, roots.work_root, store

    def initialized_attempt_context(
        self,
        state: stored_state.StoredWorkState,
    ) -> tuple[Path, Path, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        brief_bytes = canonical_work_brief_bytes(work_a_brief(project))
        selected_reference = replace(
            state.artifact_references[0],
            content_sha256=hashlib.sha256(brief_bytes).hexdigest(),
            size_bytes=len(brief_bytes),
        )
        state = replace(state, artifact_references=(selected_reference, *state.artifact_references[1:]))
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, state)
        brief_path = roots.work_root / selected_reference.selector
        brief_path.parent.mkdir(parents=True)
        brief_path.write_bytes(brief_bytes)
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
        self.assertEqual(1, sum(statement == "BEGIN" for statement in statements), statements)
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
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")),
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
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")),
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

    def test_action_discovery_uses_no_state_without_a_selected_lease(self) -> None:
        project, work, _store = self.initialized_state(self.state_with_unrelated_attempt_authority())
        common = ("--project-root", str(project), "--work-root", str(work), "actions")

        with (
            patch.object(
                SQLiteWorkStore,
                "read_current_action_snapshot",
                side_effect=AssertionError("current project read used"),
            ),
            self.record_store_reads() as observer_reads,
        ):
            result, stdout, stderr = self.run_cli(*common, "--role", "observer", "--json")
        self.assertEqual(0, result, stderr)
        self.assertEqual(["inspect:ledger"], [value["action_id"] for value in json.loads(stdout)["actions"]])
        self.assertEqual(set(), observer_reads[0])

        with (
            patch.object(
                SQLiteWorkStore,
                "read_current_action_snapshot",
                side_effect=AssertionError("current project read used"),
            ),
            self.record_store_reads() as unleased_reads,
        ):
            result, _stdout, stderr = self.run_cli(*common, "--role", "worker")
        self.assertEqual(11, result)
        self.assertIn("ATTEMPT_LEASE_REQUIRED", stderr)
        self.assertEqual(set(), unleased_reads[0])

    def test_leased_action_discovery_uses_only_lease_selected_subjects(self) -> None:
        cases = (
            (
                "worker",
                self.state_with_unrelated_attempt_authority(),
                "attempt-lease-a",
                "3",
            ),
            (
                "preparer",
                self.state_with_preparation(unrelated_count=64),
                "preparation-lease",
                "2",
            ),
        )
        for role, state, lease_id, generation in cases:
            with self.subTest(role=role):
                project, work, _store = self.initialized_state(state)
                common = ("--project-root", str(project), "--work-root", str(work), "actions")
                with (
                    patch.object(
                        SQLiteWorkStore,
                        "read_current_action_snapshot",
                        side_effect=AssertionError("current project read used"),
                    ),
                    patch("pinboard.interfaces.work_inspection.datetime") as clock,
                    self.record_store_reads() as selected_reads,
                ):
                    clock.now.return_value = SQLITE_NOW + timedelta(minutes=1)
                    result, stdout, stderr = self.run_cli(
                        *common,
                        "--role",
                        role,
                        "--lease-id",
                        lease_id,
                        "--generation",
                        generation,
                        "--json",
                    )
                self.assertEqual(0, result, stderr)
                self.assertTrue(json.loads(stdout)["actions"])
                read_tables, statements = selected_reads
                self.assertNotIn("transition_history", read_tables)
                self.assertNotIn("artifact_refs", read_tables)
                self.assertNotIn("work_item_state_counts", read_tables)
                self.assert_keyed_status_queries(work / "state.sqlite3", statements)

    def test_exact_action_discovery_reads_only_its_named_subject(self) -> None:
        project, work, _store = self.initialized_state(self.state_with_unrelated_attempt_authority())
        common = ("--project-root", str(project), "--work-root", str(work), "actions")
        with (
            patch.object(
                SQLiteWorkStore,
                "read_current_action_snapshot",
                side_effect=AssertionError("current project read used"),
            ),
            patch.object(
                SQLiteWorkStore,
                "read_leased_action_snapshot",
                side_effect=AssertionError("lease-wide read used"),
            ),
            patch("pinboard.interfaces.work_inspection.datetime") as clock,
            self.record_store_reads() as selected_reads,
        ):
            clock.now.return_value = SQLITE_NOW + timedelta(minutes=1)
            result, stdout, stderr = self.run_cli(
                *common,
                "--role",
                "worker",
                "--lease-id",
                "attempt-lease-a",
                "--generation",
                "3",
                "--action-id",
                "continue:work-a-1",
                "--json",
            )

        self.assertEqual(0, result, stderr)
        self.assertEqual(["continue:work-a-1"], [value["action_id"] for value in json.loads(stdout)["actions"]])
        read_tables, statements = selected_reads
        self.assertNotIn("transition_history", read_tables)
        self.assertNotIn("proposals", read_tables)
        self.assertNotIn("artifact_refs", read_tables)
        self.assertNotIn("work_item_state_counts", read_tables)
        self.assert_keyed_status_queries(work / "state.sqlite3", statements)

    def test_installed_item_status_reads_only_selected_item_facts(self) -> None:
        project, work, _store = self.initialized_state(self.state_with_preparation(unrelated_count=64))
        common = ("--project-root", str(project), "--work-root", str(work))

        with (
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")),
            self.record_store_reads() as item_reads,
        ):
            result, stdout, stderr = self.run_cli(*common, "item", "status", "--item-id", "work-c")

        self.assertEqual(0, result, stderr)
        self.assertIn("OK ITEM_STATUS item=work-c", stdout)
        read_tables, statements = item_reads
        self.assertEqual(
            {
                "attempts",
                "preparation_lease_counters",
                "preparation_lease_generations",
                "preparation_leases",
                "project_meta",
                "work_item_definition_revisions",
                "work_items",
            },
            read_tables,
        )
        self.assert_keyed_status_queries(work / "state.sqlite3", statements)

    def test_terminal_item_status_does_not_read_retained_attempt_history(self) -> None:
        project, work, _store = self.initialized_state(self.state_with_unrelated_attempt_authority(count=64))
        common = ("--project-root", str(project), "--work-root", str(work))

        with self.record_store_reads() as item_reads:
            result, stdout, stderr = self.run_cli(*common, "item", "status", "--item-id", "work-b", "--json")

        self.assertEqual(0, result, stderr)
        self.assertEqual([], json.loads(stdout)["attempts"])
        _read_tables, statements = item_reads
        self.assert_keyed_status_queries(work / "state.sqlite3", statements)
        attempt_selects = tuple(statement.lower() for statement in statements if "from attempts" in statement.lower())
        self.assertEqual(1, len(attempt_selects))
        self.assertIn("state != 'done'", attempt_selects[0])

    def test_selected_parallel_preview_reads_only_selected_facts_and_preserves_metadata(self) -> None:
        project, work, _store = self.initialized_state(self.state_with_unrelated_attempt_authority())
        database = work / "state.sqlite3"
        unrelated_view = work / "views" / "unrelated.md"
        unrelated_view.parent.mkdir(parents=True, exist_ok=True)
        unrelated_view.write_text("Unrelated projection.\n", encoding="utf-8")
        before = (
            database.read_bytes(),
            database.stat().st_mtime_ns,
            unrelated_view.read_bytes(),
            unrelated_view.stat().st_ino,
            unrelated_view.stat().st_mtime_ns,
        )
        common = ("--project-root", str(project), "--work-root", str(work))

        with (
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")),
            patch("pinboard.interfaces.work_inspection.datetime") as clock,
            self.record_store_reads() as preview_reads,
        ):
            clock.now.return_value = SQLITE_NOW
            result, stdout, stderr = self.run_cli(
                *common,
                "parallel",
                "preview",
                "--item",
                "work-c",
                "--item",
                "work-a",
                "--json",
            )

        self.assertEqual(0, result, stderr)
        payload = json.loads(stdout)
        self.assertEqual("12", payload["revision"])
        self.assertEqual("selected", payload["selection"])
        self.assertFalse(payload["safe"])
        self.assertEqual(["work-c"], [value["item_id"] for value in payload["launchable"]])
        self.assertEqual(["work-a"], [value["item_id"] for value in payload["excluded"]])
        self.assertEqual(1, clock.now.call_count)
        self.assertEqual(
            before,
            (
                database.read_bytes(),
                database.stat().st_mtime_ns,
                unrelated_view.read_bytes(),
                unrelated_view.stat().st_ino,
                unrelated_view.stat().st_mtime_ns,
            ),
        )
        read_tables, statements = preview_reads
        self.assertEqual(
            {
                "attempt_lease_counters",
                "attempt_lease_generations",
                "attempt_leases",
                "attempts",
                "item_dependencies",
                "preparation_leases",
                "project_meta",
                "work_item_definition_revisions",
                "work_items",
            },
            read_tables,
        )
        self.assert_keyed_status_queries(database, statements)

    def test_installed_attempt_reads_only_selected_continuation_facts(self) -> None:
        state = self.state_with_unrelated_attempt_authority()
        project, work, _store = self.initialized_attempt_context(state)
        database = work / "state.sqlite3"
        common = ("--project-root", str(project), "--work-root", str(work))
        unrelated_view = work / "views" / "unrelated.md"
        unrelated_view.parent.mkdir(parents=True, exist_ok=True)
        unrelated_view.write_text("Unrelated projection.\n", encoding="utf-8")
        before_inspection = (
            database.read_bytes(),
            database.stat().st_mtime_ns,
            unrelated_view.read_bytes(),
            unrelated_view.stat().st_mtime_ns,
        )

        with (
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")),
            self.record_store_reads() as attempt_reads,
        ):
            result, stdout, stderr = self.run_cli(*common, "attempt", "inspect", "--attempt-id", "work-a-1", "--json")

        self.assertEqual(0, result, stderr)
        self.assertIn('"attempt_id": "work-a-1"', stdout)
        self.assertEqual(
            before_inspection,
            (
                database.read_bytes(),
                database.stat().st_mtime_ns,
                unrelated_view.read_bytes(),
                unrelated_view.stat().st_mtime_ns,
            ),
        )
        read_tables, statements = attempt_reads
        self.assertEqual(
            {
                "artifact_refs",
                "attempts",
                "item_dependencies",
                "project_meta",
                "work_item_definition_revisions",
                "work_items",
            },
            read_tables,
        )
        self.assert_keyed_status_queries(database, statements)

        raw = sqlite3.connect(database)
        try:
            raw.execute("UPDATE work_items SET state = 'review' WHERE item_id = 'work-a'")
            raw.execute(
                """
                UPDATE attempts
                SET state = 'review', candidate_revision = 'candidate-a', candidate_recorded_at = ?
                WHERE attempt_id = 'work-a-1'
                """,
                (SQLITE_NOW.isoformat(),),
            )
            raw.commit()
        finally:
            raw.close()
        result_path = work / "attempts" / "work-a-1" / "result.md"
        result_path.parent.mkdir(parents=True)
        result_path.write_text("Candidate evidence.\n", encoding="utf-8")
        before_review = (
            database.read_bytes(),
            database.stat().st_mtime_ns,
            unrelated_view.read_bytes(),
            unrelated_view.stat().st_mtime_ns,
        )

        with (
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")),
            self.record_store_reads() as review_reads,
        ):
            result, stdout, stderr = self.run_cli(
                *common,
                "review-job",
                "--attempt-id",
                "work-a-1",
                "--candidate-revision",
                "candidate-a",
                "--json",
            )

        self.assertEqual(0, result, stderr)
        self.assertIn('"candidate_revision": "candidate-a"', stdout)
        self.assertEqual(
            before_review,
            (
                database.read_bytes(),
                database.stat().st_mtime_ns,
                unrelated_view.read_bytes(),
                unrelated_view.stat().st_mtime_ns,
            ),
        )
        review_tables, review_statements = review_reads
        self.assertEqual(read_tables, review_tables)
        self.assert_keyed_status_queries(database, review_statements)

    def test_terminal_attempt_inspection_stops_before_related_rows_and_artifacts(self) -> None:
        state = self.state_with_unrelated_attempt_authority()
        selected_item = next(value for value in state.lifecycle.work_items if value.item_id == ItemId("work-a"))
        selected_attempt = next(
            value for value in state.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1")
        )
        assert selected_item.queue_position is not None
        items = tuple(
            replace(
                item,
                state=stored_state.StoredWorkItemState.DONE,
                outcome_evidence="accepted",
                next_action=None,
                queue_position=None,
            )
            if item == selected_item
            else replace(item, queue_position=item.queue_position - 1)
            if item.queue_position is not None and item.queue_position > selected_item.queue_position
            else item
            for item in state.lifecycle.work_items
        )
        attempts = tuple(
            replace(attempt, state=work_models.AttemptState.DONE) if attempt == selected_attempt else attempt
            for attempt in state.lifecycle.attempts
        )
        state = replace(state, lifecycle=replace(state.lifecycle, work_items=items, attempts=attempts))
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))

        with (
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")),
            self.record_store_reads() as reads,
        ):
            result, stdout, stderr = self.run_cli(*common, "attempt", "inspect", "--attempt-id", "work-a-1")

        self.assertEqual(0, result, stderr)
        self.assertIn('"terminal": true', stdout)
        tables, statements = reads
        self.assertEqual({"attempts", "project_meta"}, tables)
        self.assert_keyed_status_queries(work / "state.sqlite3", statements)

    def test_attempt_inspection_rejects_selected_corruption_and_ignores_unrelated_corruption(self) -> None:
        state = self.state_with_unrelated_attempt_authority()
        project, work, _store = self.initialized_attempt_context(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        raw = sqlite3.connect(work / "state.sqlite3")
        try:
            raw.execute(
                "UPDATE work_item_definition_revisions SET definition_json = ? WHERE item_id = ?",
                (b"{}", "work-b"),
            )
            raw.commit()
        finally:
            raw.close()

        result, stdout, stderr = self.run_cli(*common, "attempt", "inspect", "--attempt-id", "work-a-1")
        self.assertEqual(0, result, stderr)
        self.assertIn('"attempt_id": "work-a-1"', stdout)
        validation, validation_stdout, _validation_stderr = self.run_cli(*common, "validate")
        self.assertEqual(10, validation)
        self.assertIn("WORK_STATE_INVALID", validation_stdout)

        selected_project, selected_work, _selected_store = self.initialized_attempt_context(state)
        raw = sqlite3.connect(selected_work / "state.sqlite3")
        try:
            raw.execute(
                "UPDATE work_item_definition_revisions SET definition_json = ? WHERE item_id = ?",
                (b"{}", "work-a"),
            )
            raw.commit()
        finally:
            raw.close()
        selected_result, _selected_stdout, selected_stderr = self.run_cli(
            "--project-root",
            str(selected_project),
            "--work-root",
            str(selected_work),
            "attempt",
            "inspect",
            "--attempt-id",
            "work-a-1",
        )
        self.assertEqual(12, selected_result)
        self.assertIn("WORK_STATE_INVALID", selected_stderr)

        missing_project, missing_work, _missing_store = self.initialized_attempt_context(state)
        raw = sqlite3.connect(missing_work / "state.sqlite3")
        try:
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute("DELETE FROM work_items WHERE item_id = ?", ("work-a",))
            raw.commit()
        finally:
            raw.close()
        missing_result, _missing_stdout, missing_stderr = self.run_cli(
            "--project-root",
            str(missing_project),
            "--work-root",
            str(missing_work),
            "attempt",
            "inspect",
            "--attempt-id",
            "work-a-1",
        )
        self.assertEqual(12, missing_result)
        self.assertIn("WORK_STATE_INVALID", missing_stderr)

    def test_item_status_rejects_selected_corruption_and_ignores_unrelated_corruption(self) -> None:
        state = self.state_with_preparation(unrelated_count=1)
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        database = work / "state.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "UPDATE work_item_definition_revisions SET definition_json = ? WHERE item_id = ?",
                (b"{}", "unrelated-preparation-0"),
            )
            connection.commit()
        finally:
            connection.close()

        result, stdout, stderr = self.run_cli(*common, "item", "status", "--item-id", "work-c")
        self.assertEqual(0, result, stderr)
        self.assertIn("OK ITEM_STATUS item=work-c", stdout)
        validation, validation_stdout, _validation_stderr = self.run_cli(*common, "validate")
        self.assertEqual(10, validation)
        self.assertIn("WORK_STATE_INVALID", validation_stdout)

        selected_project, selected_work, _selected_store = self.initialized_state(state)
        selected_connection = sqlite3.connect(selected_work / "state.sqlite3")
        try:
            selected_connection.execute(
                "UPDATE work_item_definition_revisions SET definition_json = ? WHERE item_id = ?",
                (b"{}", "work-c"),
            )
            selected_connection.commit()
        finally:
            selected_connection.close()
        selected_result, _selected_stdout, selected_stderr = self.run_cli(
            "--project-root",
            str(selected_project),
            "--work-root",
            str(selected_work),
            "item",
            "status",
            "--item-id",
            "work-c",
        )
        self.assertEqual(12, selected_result)
        self.assertIn("WORK_STATE_INVALID", selected_stderr)

        authority_project, authority_work, _authority_store = self.initialized_state(state)
        authority_connection = sqlite3.connect(authority_work / "state.sqlite3")
        try:
            authority_connection.execute("PRAGMA foreign_keys = OFF")
            authority_connection.execute(
                "DELETE FROM preparation_lease_generations WHERE item_id = ?",
                ("work-c",),
            )
            authority_connection.commit()
        finally:
            authority_connection.close()
        authority_result, _authority_stdout, authority_stderr = self.run_cli(
            "--project-root",
            str(authority_project),
            "--work-root",
            str(authority_work),
            "item",
            "status",
            "--item-id",
            "work-c",
        )
        self.assertEqual(12, authority_result)
        self.assertIn("WORK_STATE_INVALID", authority_stderr)

    def test_item_status_rejects_selected_item_attempt_state_mismatch(self) -> None:
        lifecycle_project, lifecycle_work, _lifecycle_store = self.initialized_state(
            self.state_with_preparation(unrelated_count=1)
        )
        lifecycle_connection = sqlite3.connect(lifecycle_work / "state.sqlite3")
        try:
            lifecycle_connection.execute("UPDATE work_items SET state = 'ready' WHERE item_id = ?", ("work-a",))
            lifecycle_connection.commit()
        finally:
            lifecycle_connection.close()
        lifecycle_result, _lifecycle_stdout, lifecycle_stderr = self.run_cli(
            "--project-root",
            str(lifecycle_project),
            "--work-root",
            str(lifecycle_work),
            "item",
            "status",
            "--item-id",
            "work-a",
        )
        self.assertEqual(12, lifecycle_result)
        self.assertIn("WORK_STATE_INVALID", lifecycle_stderr)

    def test_selected_parallel_preview_rejects_selected_corruption_and_ignores_unrelated_corruption(self) -> None:
        state = complete_sqlite_state()
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        database = work / "state.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "UPDATE work_item_definition_revisions SET definition_json = ? WHERE item_id = ?",
                (b"{}", "work-b"),
            )
            connection.commit()
        finally:
            connection.close()

        result, stdout, stderr = self.run_cli(*common, "parallel", "preview", "--item", "work-c")
        self.assertEqual(0, result, stderr)
        self.assertIn("OK PARALLEL_PREVIEW", stdout)
        validation, validation_stdout, _validation_stderr = self.run_cli(*common, "validate")
        self.assertEqual(10, validation)
        self.assertIn("WORK_STATE_INVALID", validation_stdout)

        selected_project, selected_work, _selected_store = self.initialized_state(state)
        selected_connection = sqlite3.connect(selected_work / "state.sqlite3")
        try:
            selected_connection.execute(
                "UPDATE work_item_definition_revisions SET definition_json = ? WHERE item_id = ?",
                (b"{}", "work-c"),
            )
            selected_connection.commit()
        finally:
            selected_connection.close()
        selected_result, _selected_stdout, selected_stderr = self.run_cli(
            "--project-root",
            str(selected_project),
            "--work-root",
            str(selected_work),
            "parallel",
            "preview",
            "--item",
            "work-c",
        )
        self.assertEqual(12, selected_result)
        self.assertIn("WORK_STATE_INVALID", selected_stderr)

    def test_selected_parallel_preview_rejects_inconsistent_open_attempt_relationships(self) -> None:
        state = complete_sqlite_state()
        for item_id, state_after in (("work-a", "ready"), ("work-c", "active")):
            project, work, _store = self.initialized_state(state)
            connection = sqlite3.connect(work / "state.sqlite3")
            try:
                connection.execute(
                    "UPDATE work_items SET state = ? WHERE item_id = ?",
                    (state_after, item_id),
                )
                connection.commit()
            finally:
                connection.close()

            with self.subTest(item_id=item_id, state=state_after):
                result, _stdout, stderr = self.run_cli(
                    "--project-root",
                    str(project),
                    "--work-root",
                    str(work),
                    "parallel",
                    "preview",
                    "--item",
                    item_id,
                )
            self.assertEqual(12, result)
            self.assertIn("WORK_STATE_INVALID", stderr)

    def test_installed_definition_reads_are_keyed_and_bounded_by_the_requested_page(self) -> None:
        project, work, _store = self.initialized_state(
            self.state_with_preparation(
                status=authority_models.PreparationLeaseStatus.RELEASED,
                historical=True,
                unrelated_count=64,
            )
        )
        common = ("--project-root", str(project), "--work-root", str(work))

        for arguments, expected_revision, expected_tables in (
            (
                ("item", "definition", "--item-id", "work-c"),
                "definition_revision=2",
                {"project_meta", "work_items", "work_item_definition_revisions", "item_dependencies"},
            ),
            (
                (
                    "item",
                    "definition-history",
                    "--item-id",
                    "work-c",
                    "--limit",
                    "1",
                ),
                "revisions=1",
                {"project_meta", "work_items", "work_item_definition_revisions"},
            ),
        ):
            with (
                self.subTest(command=arguments[1]),
                patch.object(
                    SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")
                ),
                self.record_store_reads() as reads,
            ):
                result, stdout, stderr = self.run_cli(*common, *arguments)
            self.assertEqual(0, result, stderr)
            self.assertIn(expected_revision, stdout)
            tables, statements = reads
            self.assertEqual(expected_tables, tables)
            self.assert_keyed_status_queries(work / "state.sqlite3", statements)

    def test_definition_reads_reject_selected_corruption_and_ignore_unrelated_corruption(self) -> None:
        state = self.state_with_preparation(
            status=authority_models.PreparationLeaseStatus.RELEASED,
            historical=True,
            unrelated_count=1,
        )
        project, work, _store = self.initialized_state(state)
        database = work / "state.sqlite3"
        raw = sqlite3.connect(database)
        try:
            raw.execute(
                "UPDATE work_item_definition_revisions SET definition_json = ? WHERE item_id = ?",
                (b"{}", "unrelated-preparation-0"),
            )
            raw.commit()
        finally:
            raw.close()

        selected_result, selected_stdout, selected_stderr = self.run_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(work),
            "item",
            "definition",
            "--item-id",
            "work-c",
        )
        self.assertEqual(0, selected_result, selected_stderr)
        self.assertIn("definition_revision=2", selected_stdout)
        validation_result, validation_stdout, _validation_stderr = self.run_cli(
            "--project-root", str(project), "--work-root", str(work), "validate"
        )
        self.assertEqual(10, validation_result)
        self.assertIn("WORK_STATE_INVALID", validation_stdout)

        selected_project, selected_work, _selected_store = self.initialized_state(state)
        selected_database = selected_work / "state.sqlite3"
        raw = sqlite3.connect(selected_database)
        try:
            raw.execute(
                "UPDATE work_item_definition_revisions SET definition_json = ? WHERE item_id = ? AND definition_revision = ?",
                (b"{}", "work-c", 2),
            )
            raw.commit()
        finally:
            raw.close()
        corrupt_result, _corrupt_stdout, corrupt_stderr = self.run_cli(
            "--project-root",
            str(selected_project),
            "--work-root",
            str(selected_work),
            "item",
            "definition",
            "--item-id",
            "work-c",
        )
        self.assertEqual(12, corrupt_result)
        self.assertIn("WORK_STATE_INVALID", corrupt_stderr)

        history_project, history_work, _history_store = self.initialized_state(state)
        raw = sqlite3.connect(history_work / "state.sqlite3")
        try:
            raw.execute(
                "DELETE FROM work_item_definition_revisions WHERE item_id = ? AND definition_revision = ?",
                ("work-c", 1),
            )
            raw.commit()
        finally:
            raw.close()
        history_result, _history_stdout, history_stderr = self.run_cli(
            "--project-root",
            str(history_project),
            "--work-root",
            str(history_work),
            "item",
            "definition-history",
            "--item-id",
            "work-c",
        )
        self.assertEqual(12, history_result)
        self.assertIn("WORK_STATE_INVALID", history_stderr)

        dependency_project, dependency_work, _dependency_store = self.initialized_state(state)
        raw = sqlite3.connect(dependency_work / "state.sqlite3")
        try:
            raw.execute(
                "INSERT INTO item_dependencies (item_id, dependency_id, position) VALUES (?, ?, ?)",
                ("work-c", "work-b", 0),
            )
            raw.commit()
        finally:
            raw.close()
        dependency_result, _dependency_stdout, dependency_stderr = self.run_cli(
            "--project-root",
            str(dependency_project),
            "--work-root",
            str(dependency_work),
            "item",
            "definition",
            "--item-id",
            "work-c",
        )
        self.assertEqual(12, dependency_result)
        self.assertIn("WORK_STATE_INVALID", dependency_stderr)

    def test_definition_history_rejects_an_existing_item_without_definitions(self) -> None:
        project, work, _store = self.initialized_state(
            self.state_with_preparation(
                status=authority_models.PreparationLeaseStatus.RELEASED,
                historical=True,
            )
        )
        raw = sqlite3.connect(work / "state.sqlite3")
        try:
            raw.execute("DELETE FROM work_item_definition_revisions WHERE item_id = ?", ("work-c",))
            raw.commit()
        finally:
            raw.close()

        result, _stdout, stderr = self.run_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(work),
            "item",
            "definition-history",
            "--item-id",
            "work-c",
        )

        self.assertEqual(12, result)
        self.assertIn("WORK_STATE_INVALID", stderr)

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
