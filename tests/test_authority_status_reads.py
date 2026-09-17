import asyncio
import contextlib
import hashlib
import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from collections.abc import Generator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import override
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import UnexpectedToolError
from mcp_types import CallToolResult

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite import database as sqlite_database
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.database import initialize_database, open_database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.work_briefs import canonical_work_brief_bytes
from pinboard.cli.entrypoint import main
from pinboard.domain import authority_models, decision_models, history, work_models
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import ActionId, AttemptId, HostId, ItemId, LeaseId, TaskId
from pinboard.mcp import server as mcp_server
from tests.support import SQLITE_NOW, JsonObject, JsonValue, complete_sqlite_state, initialize_store
from tests.work_brief_support import work_a_brief


class AuthorityStatusReadTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        clock_patch = patch("pinboard.mcp.server.datetime")
        clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        clock.now.return_value = SQLITE_NOW

    def initialized_state(self, state: stored_state.StoredWorkState) -> tuple[Path, Path, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        subprocess.run(["git", "init", "-q"], cwd=project, check=True, capture_output=True)
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
        subprocess.run(["git", "init", "-q"], cwd=project, check=True, capture_output=True)
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

    def native(self, tool: str, project: str, work: str, request: dict[str, JsonValue]) -> JsonObject:
        executor = mcp_server.BoundedExecutor(worker_count=1, unfinished_limit=1)
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(io.StringIO(), event_limit=4, line_limit=256)
        )
        arguments = {"project_root": project, "work_root": work, **request}
        if tool in {
            mcp_server.ATTEMPT_AUTHORITY_TOOL,
            mcp_server.PREPARATION_AUTHORITY_TOOL,
            mcp_server.ITEM_DEFINITION_TOOL,
            mcp_server.ACTIONS_TOOL,
            mcp_server.PARALLEL_PREVIEW_TOOL,
        }:
            arguments = {"request": arguments}
        try:
            result = asyncio.run(server.call_tool(tool, arguments))
            assert isinstance(result, CallToolResult) and isinstance(result.structured_content, dict)
            self.assertFalse(result.is_error)
            content: JsonObject = result.structured_content
            return content
        finally:
            executor.shutdown()

    def json_object(self, value: JsonValue) -> JsonObject:
        if not isinstance(value, dict):
            self.fail("Expected a native JSON object")
        return value

    def json_array(self, value: JsonValue) -> list[JsonValue]:
        if not isinstance(value, list):
            self.fail("Expected a native JSON array")
        return value

    @contextlib.contextmanager
    def rejected_storage(self) -> Generator[None]:
        with self.assertRaises(UnexpectedToolError) as failure:
            yield
        cause = failure.exception.__cause__
        self.assertIsInstance(cause, StorageError)
        assert isinstance(cause, StorageError)
        self.assertEqual(StorageErrorCode.INVALID_STATE, cause.code)

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

    def assert_keyed_status_queries(
        self, database: Path, statements: list[str], *, expected_operations: int = 1
    ) -> None:
        self.assertEqual(expected_operations, sum(statement == "BEGIN" for statement in statements), statements)
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

        with (
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")),
            self.record_store_reads() as attempt_reads,
        ):
            stdout = self.native(
                mcp_server.ATTEMPT_AUTHORITY_TOOL,
                str(project),
                str(work),
                {"operation": "status", "attempt_id": "work-a-1"},
            )
        self.assertNotIn("code", stdout)
        self.assertEqual("active", stdout["authority_status"])
        attempt_tables, attempt_statements = attempt_reads
        self.assertEqual(
            {"attempts", "attempt_lease_counters", "attempt_lease_generations", "attempt_leases"},
            attempt_tables,
        )
        self.assert_keyed_status_queries(work / "state.sqlite3", attempt_statements)

        with (
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")),
            patch("pinboard.mcp.server.datetime") as clock,
            self.record_store_reads() as preparation_reads,
        ):
            clock.now.return_value = SQLITE_NOW + timedelta(minutes=1)
            stdout = self.native(
                mcp_server.PREPARATION_AUTHORITY_TOOL,
                str(project),
                str(work),
                {"operation": "status", "item_id": "work-c"},
            )
        self.assertNotIn("code", stdout)
        self.assertEqual("active", stdout["authority_status"])
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

        with (
            patch.object(
                SQLiteWorkStore,
                "read_current_action_snapshot",
                side_effect=AssertionError("current project read used"),
            ),
            self.record_store_reads() as observer_reads,
        ):
            stdout = self.native(mcp_server.ACTIONS_TOOL, str(project), str(work), {"role": "observer"})
        self.assertNotIn("code", stdout)
        self.assertEqual(
            [{"kind": "inspect", "subject": "ledger"}],
            [self.json_object(value)["action_id"] for value in self.json_array(stdout["actions"])],
        )
        self.assertEqual(set(), observer_reads[0])

        with (
            patch.object(
                SQLiteWorkStore,
                "read_current_action_snapshot",
                side_effect=AssertionError("current project read used"),
            ),
            self.record_store_reads() as unleased_reads,
        ):
            _stdout = self.native(mcp_server.ACTIONS_TOOL, str(project), str(work), {"role": "worker"})
        self.assertEqual("rejected", _stdout["status"])
        self.assertEqual("ACTIONS_INVALID", _stdout["code"])
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
                with (
                    patch.object(
                        SQLiteWorkStore,
                        "read_current_action_snapshot",
                        side_effect=AssertionError("current project read used"),
                    ),
                    patch("pinboard.mcp.server.datetime") as clock,
                    self.record_store_reads() as selected_reads,
                ):
                    clock.now.return_value = SQLITE_NOW + timedelta(minutes=1)
                    stdout = self.native(
                        mcp_server.ACTIONS_TOOL,
                        str(project),
                        str(work),
                        {"role": role, "lease_id": lease_id, "generation": int(generation)},
                    )
                self.assertNotIn("code", stdout)
                self.assertTrue(stdout["actions"])
                read_tables, statements = selected_reads
                self.assertNotIn("transition_history", read_tables)
                self.assertNotIn("artifact_refs", read_tables)
                self.assertNotIn("work_item_state_counts", read_tables)
                self.assert_keyed_status_queries(work / "state.sqlite3", statements)

    def test_exact_action_discovery_reads_only_its_named_subject(self) -> None:
        project, work, _store = self.initialized_state(self.state_with_unrelated_attempt_authority())
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
            patch("pinboard.mcp.server.datetime") as clock,
            self.record_store_reads() as selected_reads,
        ):
            clock.now.return_value = SQLITE_NOW + timedelta(minutes=1)
            stdout = self.native(
                mcp_server.ACTIONS_TOOL,
                str(project),
                str(work),
                {
                    "role": "worker",
                    "action_id": {"kind": "continue", "subject": "work-a-1"},
                    "lease_id": "attempt-lease-a",
                    "generation": int("3"),
                },
            )

        self.assertNotIn("code", stdout)
        self.assertEqual(
            [{"kind": "continue", "subject": "work-a-1"}],
            [self.json_object(value)["action_id"] for value in self.json_array(stdout["actions"])],
        )
        read_tables, statements = selected_reads
        self.assertNotIn("transition_history", read_tables)
        self.assertNotIn("proposals", read_tables)
        self.assertNotIn("artifact_refs", read_tables)
        self.assertNotIn("work_item_state_counts", read_tables)
        self.assert_keyed_status_queries(work / "state.sqlite3", statements)

    def test_installed_item_status_reads_only_selected_item_facts(self) -> None:
        project, work, _store = self.initialized_state(self.state_with_preparation(unrelated_count=64))

        with (
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")),
            self.record_store_reads() as item_reads,
        ):
            stdout = self.native(mcp_server.ITEM_STATUS_TOOL, str(project), str(work), {"item_id": "work-c"})

        self.assertNotIn("code", stdout)
        self.assertEqual("work-c", stdout["item_id"])
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

    def test_item_status_and_same_revision_overview_agree_for_every_legal_live_shape(self) -> None:
        base = complete_sqlite_state()
        selected_item = base.lifecycle.work_items[1]
        selected_attempt = base.lifecycle.attempts[0]
        legal_shapes = (
            (stored_state.StoredWorkItemState.INTAKE, None),
            (stored_state.StoredWorkItemState.READY, None),
            (stored_state.StoredWorkItemState.ACTIVE, work_models.AttemptState.ACTIVE),
            (stored_state.StoredWorkItemState.PAUSED, work_models.AttemptState.PAUSED),
            (stored_state.StoredWorkItemState.BLOCKED, None),
            (stored_state.StoredWorkItemState.BLOCKED, work_models.AttemptState.BLOCKED),
            (stored_state.StoredWorkItemState.DEFERRED, None),
            (stored_state.StoredWorkItemState.REVIEW, work_models.AttemptState.REVIEW),
        )
        for item_state, attempt_state in legal_shapes:
            item = replace(selected_item, state=item_state)
            attempts = (
                ()
                if attempt_state is None
                else (
                    replace(
                        selected_attempt,
                        state=attempt_state,
                        candidate_revision="candidate-a" if attempt_state == work_models.AttemptState.REVIEW else None,
                        candidate_recorded_at=SQLITE_NOW if attempt_state == work_models.AttemptState.REVIEW else None,
                    ),
                )
            )
            state = replace(
                base,
                lifecycle=replace(
                    base.lifecycle,
                    work_items=(base.lifecycle.work_items[0], item, *base.lifecycle.work_items[2:]),
                    attempts=attempts,
                ),
                authority=replace(
                    base.authority,
                    attempt_counters=() if attempt_state is None else base.authority.attempt_counters,
                    attempt_generations=() if attempt_state is None else base.authority.attempt_generations,
                    attempt_leases=() if attempt_state is None else base.authority.attempt_leases,
                ),
            )
            project, work, _store = self.initialized_state(state)

            with self.subTest(item_state=item_state.value, attempt_state=attempt_state):
                with self.record_store_reads() as selected_reads:
                    status_stdout = self.native(
                        mcp_server.ITEM_STATUS_TOOL, str(project), str(work), {"item_id": "work-a"}
                    )
                overview_stdout = self.native(mcp_server.OVERVIEW_TOOL, str(project), str(work), {})

            self.assertNotIn("code", status_stdout)
            self.assertNotIn("code", overview_stdout)
            status = status_stdout
            overview = overview_stdout
            overview_item = next(
                self.json_object(value)
                for value in self.json_array(overview["items"])
                if self.json_object(value)["item_id"] == "work-a"
            )
            self.assertEqual(status["revision"], overview["revision"])
            for status_field, overview_field in (
                ("item_id", "item_id"),
                ("label", "label"),
                ("state", "state"),
                ("timing", "timing"),
                ("next_action", "next_action"),
                ("source", "source"),
                ("notes", "notes"),
                ("queue_position", "position"),
                ("preparation", "preparation"),
            ):
                self.assertEqual(status[status_field], overview_item[overview_field], status_field)
            attempts = self.json_array(status["attempts"])
            current_attempt = None if not attempts else self.json_object(attempts[0])["attempt_id"]
            self.assertEqual(current_attempt, overview_item["attempt_id"])
            self.assert_keyed_status_queries(work / "state.sqlite3", selected_reads[1])

    def test_terminal_item_status_does_not_read_retained_attempt_history(self) -> None:
        base = self.state_with_unrelated_attempt_authority(count=64)
        for terminal_state in (
            stored_state.StoredWorkItemState.DONE,
            stored_state.StoredWorkItemState.SUPERSEDED,
            stored_state.StoredWorkItemState.DROPPED,
        ):
            terminal = replace(base.lifecycle.work_items[2], state=terminal_state)
            state = replace(
                base,
                lifecycle=replace(
                    base.lifecycle,
                    work_items=(*base.lifecycle.work_items[:2], terminal, *base.lifecycle.work_items[3:]),
                ),
            )
            project, work, _store = self.initialized_state(state)

            with self.subTest(state=terminal_state.value), self.record_store_reads() as item_reads:
                stdout = self.native(mcp_server.ITEM_STATUS_TOOL, str(project), str(work), {"item_id": "work-b"})

            self.assertNotIn("code", stdout)
            self.assertEqual(terminal_state.value, stdout["state"])
            self.assertEqual([], stdout["attempts"])
            _read_tables, statements = item_reads
            self.assert_keyed_status_queries(work / "state.sqlite3", statements)
            attempt_selects = tuple(
                statement.lower() for statement in statements if "from attempts" in statement.lower()
            )
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

        with (
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")),
            patch("pinboard.mcp.server.datetime") as clock,
            self.record_store_reads() as preview_reads,
        ):
            clock.now.return_value = SQLITE_NOW
            stdout = self.native(
                mcp_server.PARALLEL_PREVIEW_TOOL,
                str(project),
                str(work),
                {"selection": "selected", "item_ids": ["work-c", "work-a"]},
            )

        self.assertNotIn("code", stdout)
        payload = stdout
        self.assertEqual("12", payload["revision"])
        self.assertEqual("selected", payload["selection"])
        self.assertFalse(payload["safe"])
        self.assertEqual(
            ["work-c"], [self.json_object(value)["item_id"] for value in self.json_array(payload["launchable"])]
        )
        self.assertEqual(
            ["work-a"], [self.json_object(value)["item_id"] for value in self.json_array(payload["excluded"])]
        )
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
        project, work, store = self.initialized_attempt_context(state)
        database = work / "state.sqlite3"
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
            patch.object(
                SQLiteWorkStore, "read_attempt_context", autospec=True, side_effect=SQLiteWorkStore.read_attempt_context
            ) as contexts,
            patch.object(
                SQLiteWorkStore,
                "read_artifact_reference_by_id",
                autospec=True,
                side_effect=SQLiteWorkStore.read_artifact_reference_by_id,
            ) as references,
            patch.object(
                SQLiteWorkStore,
                "read_candidate_snapshot_context",
                autospec=True,
                side_effect=SQLiteWorkStore.read_candidate_snapshot_context,
            ) as snapshots,
            self.record_store_reads() as attempt_reads,
        ):
            stdout = self.native(mcp_server.ATTEMPT_INSPECT_TOOL, str(project), str(work), {"attempt_id": "work-a-1"})

        self.assertNotIn("code", stdout)
        self.assertEqual("work-a-1", self.json_object(stdout["continuation"])["attempt_id"])
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
                "planned_replacements",
                "replacement_dispositions",
                "work_item_definition_revisions",
                "work_items",
            },
            read_tables,
        )
        self.assertEqual(1, contexts.call_count)
        self.assertEqual(1, references.call_count)
        self.assertEqual(1, snapshots.call_count)
        self.assert_keyed_status_queries(database, statements, expected_operations=3)

        raw = sqlite3.connect(database)
        try:
            raw.execute("UPDATE work_items SET state = 'review' WHERE item_id = 'work-a'")
            raw.execute("UPDATE work_item_state_counts SET item_count = item_count - 1 WHERE state = 'active'")
            raw.execute("UPDATE work_item_state_counts SET item_count = item_count + 1 WHERE state = 'review'")
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
        before_review = store.validated_snapshot()
        unrelated_before = (unrelated_view.read_bytes(), unrelated_view.stat().st_mtime_ns)

        with (
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")),
            self.record_store_reads() as review_reads,
            self.rejected_storage(),
        ):
            self.native(
                mcp_server.REVIEW_JOB_TOOL,
                str(project),
                str(work),
                {"review": {"kind": "initial", "attempt_id": "work-a-1", "candidate_revision": "candidate-a"}},
            )

        after_review = store.validated_snapshot()
        self.assertEqual(before_review, after_review)
        self.assertEqual(unrelated_before, (unrelated_view.read_bytes(), unrelated_view.stat().st_mtime_ns))
        review_tables, review_statements = review_reads
        self.assertEqual(read_tables | {"transition_history"}, review_tables)
        self.assert_keyed_status_queries(database, review_statements)

    def test_attempt_inspection_preserves_live_legacy_review_without_snapshot_recovery(self) -> None:
        state = complete_sqlite_state()
        candidate = "working-tree-sha256:" + "0" * 64
        items = tuple(
            replace(item, state=stored_state.StoredWorkItemState.REVIEW) if item.item_id == ItemId("work-a") else item
            for item in state.lifecycle.work_items
        )
        attempt = replace(
            state.lifecycle.attempts[0],
            state=work_models.AttemptState.REVIEW,
            candidate_revision=candidate,
            candidate_recorded_at=SQLITE_NOW,
            subject_revision=state.transition_receipts[0].project_revision,
        )
        receipt = replace(
            state.transition_receipts[0],
            action_id=ActionId("submit-review:work-a-1"),
            action_kind=decision_models.ActionKind.SUBMIT_REVIEW,
            artifact_ref_id=None,
            input_schema="decision/v1",
            input_payload=work_models.CanonicalJson(b"{}"),
            outcome_schema="transition-receipt/v1",
            outcome_payload=work_models.CanonicalJson(
                history.encode_transition_receipt_outcome(
                    evidence=None,
                    outcome="submit-review",
                    candidate=candidate,
                )
            ),
        )
        legacy = replace(
            state,
            lifecycle=replace(state.lifecycle, work_items=items, attempts=(attempt,)),
            transition_receipts=(receipt,),
        )
        project, work, store = self.initialized_attempt_context(legacy)

        stdout = self.native(mcp_server.ATTEMPT_INSPECT_TOOL, str(project), str(work), {"attempt_id": "work-a-1"})

        self.assertNotIn("code", stdout)
        self.assertEqual("absent", self.json_object(stdout["candidate_recovery"])["kind"])
        self.assertIsNone(store.read_candidate_snapshot_context(AttemptId("work-a-1")))

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

        with (
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")),
            self.record_store_reads() as reads,
        ):
            stdout = self.native(mcp_server.ATTEMPT_INSPECT_TOOL, str(project), str(work), {"attempt_id": "work-a-1"})

        self.assertNotIn("code", stdout)
        self.assertTrue(self.json_object(stdout["continuation"])["terminal"])
        tables, statements = reads
        self.assertEqual({"attempts", "project_meta"}, tables)
        self.assert_keyed_status_queries(work / "state.sqlite3", statements, expected_operations=2)

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

        stdout = self.native(mcp_server.ATTEMPT_INSPECT_TOOL, str(project), str(work), {"attempt_id": "work-a-1"})
        self.assertNotIn("code", stdout)
        self.assertEqual("work-a-1", self.json_object(stdout["continuation"])["attempt_id"])
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
        with self.rejected_storage():
            self.native(
                mcp_server.ATTEMPT_INSPECT_TOOL, str(selected_project), str(selected_work), {"attempt_id": "work-a-1"}
            )

        missing_project, missing_work, _missing_store = self.initialized_attempt_context(state)
        raw = sqlite3.connect(missing_work / "state.sqlite3")
        try:
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute("DELETE FROM work_items WHERE item_id = ?", ("work-a",))
            raw.commit()
        finally:
            raw.close()
        with self.rejected_storage():
            self.native(
                mcp_server.ATTEMPT_INSPECT_TOOL, str(missing_project), str(missing_work), {"attempt_id": "work-a-1"}
            )

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

        stdout = self.native(mcp_server.ITEM_STATUS_TOOL, str(project), str(work), {"item_id": "work-c"})
        self.assertNotIn("code", stdout)
        self.assertEqual("work-c", stdout["item_id"])
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
        with self.rejected_storage():
            self.native(mcp_server.ITEM_STATUS_TOOL, str(selected_project), str(selected_work), {"item_id": "work-c"})

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
        with self.rejected_storage():
            self.native(mcp_server.ITEM_STATUS_TOOL, str(authority_project), str(authority_work), {"item_id": "work-c"})

    def test_item_status_explains_both_inconsistency_directions_and_validation_does_not_repair(self) -> None:
        for item_id, mutation, expected_state, observed_attempt in (
            ("work-a", "UPDATE work_items SET state = 'ready' WHERE item_id = 'work-a'", "none", "active"),
            ("work-c", "UPDATE work_items SET state = 'active' WHERE item_id = 'work-c'", "active", "none"),
        ):
            project, work, _store = self.initialized_state(complete_sqlite_state())
            database = work / "state.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute(mutation)
                connection.commit()
            finally:
                connection.close()
            before = (database.read_bytes(), database.stat().st_mtime_ns)
            common = ("--project-root", str(project), "--work-root", str(work))

            with self.subTest(item_id=item_id), self.record_store_reads() as selected_reads:
                stdout = self.native(mcp_server.ITEM_STATUS_TOOL, str(project), str(work), {"item_id": item_id})

            self.assertEqual("rejected", stdout["status"])

            rejection = stdout
            self.assertEqual("pinboard-mcp-item-status-result/v1", rejection["schema"])
            self.assertEqual("ITEM_STATUS_INCONSISTENT", rejection["code"])
            self.assertFalse(rejection["state_changed"])
            self.assertEqual([], rejection["changed_surfaces"])
            self.assertEqual("do-not-retry", rejection["retry"])
            self.assertNotIn("next_actions", rejection)
            observed = {
                str(self.json_object(value)["field"]): self.json_object(value)["value"]
                for value in self.json_array(rejection["observed"])
            }
            self.assertEqual(
                {
                    "item_id": item_id,
                    "item_state": "ready" if item_id == "work-a" else "active",
                    "item_timing": "must-now",
                    "item_outcome_evidence": None,
                    "item_next_action": "continue" if item_id == "work-a" else "activate",
                    "item_source": "accepted requirement",
                    "item_notes": "Current work remains bounded.",
                    "item_queue_position": 2 if item_id == "work-a" else 3,
                    "attempt_id": "work-a-1" if item_id == "work-a" else None,
                    "attempt_state": observed_attempt,
                    "attempt_candidate_revision": None,
                },
                observed,
            )
            self.assertEqual(
                [{"field": "current_attempt_state", "expected": expected_state, "observed": observed_attempt}],
                rejection["mismatches"],
            )
            self.assertEqual(before, (database.read_bytes(), database.stat().st_mtime_ns))
            self.assert_keyed_status_queries(database, selected_reads[1])

            validation_result, validation_stdout, validation_stderr = self.run_cli(*common, "validate", "--json")

            self.assertEqual(10, validation_result, validation_stderr)
            self.assertEqual("", validation_stderr)
            validation = json.loads(validation_stdout)
            self.assertFalse(validation["valid"])
            self.assertIn("WORK_STATE_INVALID", [value["code"] for value in validation["diagnostics"]])
            self.assertEqual(before, (database.read_bytes(), database.stat().st_mtime_ns))

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

        stdout = self.native(
            mcp_server.PARALLEL_PREVIEW_TOOL, str(project), str(work), {"selection": "selected", "item_ids": ["work-c"]}
        )
        self.assertNotIn("code", stdout)
        self.assertEqual("selected", stdout["selection"])
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
        with self.rejected_storage():
            self.native(
                mcp_server.PARALLEL_PREVIEW_TOOL,
                str(selected_project),
                str(selected_work),
                {"selection": "selected", "item_ids": ["work-c"]},
            )

    def test_selected_parallel_preview_rejects_inconsistent_open_attempt_relationships(self) -> None:
        state = complete_sqlite_state()
        blocked_without_attempt = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=tuple(
                    replace(value, state=stored_state.StoredWorkItemState.BLOCKED)
                    if value.item_id == ItemId("work-c")
                    else value
                    for value in state.lifecycle.work_items
                ),
            ),
        )
        blocked_project, blocked_work, _blocked_store = self.initialized_state(blocked_without_attempt)
        blocked_stdout = self.native(
            mcp_server.PARALLEL_PREVIEW_TOOL,
            str(blocked_project),
            str(blocked_work),
            {"selection": "selected", "item_ids": ["work-c"]},
        )
        self.assertNotIn("code", blocked_stdout)
        blocked_payload = blocked_stdout
        self.assertEqual([], blocked_payload["launchable"])
        self.assertEqual("blocked", self.json_object(self.json_array(blocked_payload["excluded"])[0])["state"])

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

            with self.subTest(item_id=item_id, state=state_after), self.rejected_storage():
                self.native(
                    mcp_server.PARALLEL_PREVIEW_TOOL,
                    str(project),
                    str(work),
                    {"selection": "selected", "item_ids": [item_id]},
                )

    def test_installed_definition_reads_are_keyed_and_bounded_by_the_requested_page(self) -> None:
        project, work, _store = self.initialized_state(
            self.state_with_preparation(
                status=authority_models.PreparationLeaseStatus.RELEASED,
                historical=True,
                unrelated_count=64,
            )
        )

        cases: tuple[tuple[dict[str, JsonValue], set[str]], ...] = (
            (
                {"operation": "current", "item_id": "work-c"},
                {"project_meta", "work_items", "work_item_definition_revisions", "item_dependencies"},
            ),
            (
                {"operation": "history", "item_id": "work-c", "limit": 1, "before_revision": None},
                {"project_meta", "work_items", "work_item_definition_revisions"},
            ),
        )
        for request, expected_tables in cases:
            with (
                self.subTest(operation=request["operation"]),
                patch.object(
                    SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")
                ),
                self.record_store_reads() as reads,
            ):
                payload = self.native(mcp_server.ITEM_DEFINITION_TOOL, str(project), str(work), request)
            self.assertNotIn("code", payload)
            if request["operation"] == "current":
                self.assertEqual(2, payload["definition_revision"])
            else:
                self.assertEqual(1, len(self.json_array(payload["revisions"])))
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

        selected_stdout = self.native(
            mcp_server.ITEM_DEFINITION_TOOL, str(project), str(work), {"operation": "current", "item_id": "work-c"}
        )
        self.assertNotIn("code", selected_stdout)
        self.assertEqual(2, selected_stdout["definition_revision"])
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
        with self.rejected_storage():
            self.native(
                mcp_server.ITEM_DEFINITION_TOOL,
                str(selected_project),
                str(selected_work),
                {"operation": "current", "item_id": "work-c"},
            )

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
        with self.rejected_storage():
            self.native(
                mcp_server.ITEM_DEFINITION_TOOL,
                str(history_project),
                str(history_work),
                {"operation": "history", "item_id": "work-c", "limit": 20, "before_revision": None},
            )

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
        with self.rejected_storage():
            self.native(
                mcp_server.ITEM_DEFINITION_TOOL,
                str(dependency_project),
                str(dependency_work),
                {"operation": "current", "item_id": "work-c"},
            )

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

        with self.rejected_storage():
            self.native(
                mcp_server.ITEM_DEFINITION_TOOL,
                str(project),
                str(work),
                {"operation": "history", "item_id": "work-c", "limit": 20, "before_revision": None},
            )

    def test_status_preserves_distinct_expiry_and_historical_pin_contracts(self) -> None:
        project, work, _store = self.initialized_state(self.state_with_preparation())
        expires_at = SQLITE_NOW + timedelta(minutes=5)
        for observed_at, expected in (
            (expires_at - timedelta(microseconds=1), "active"),
            (expires_at, "expired"),
            (expires_at + timedelta(microseconds=1), "expired"),
        ):
            with (
                self.subTest(preparation_observed_at=observed_at),
                patch("pinboard.mcp.server.datetime") as clock,
            ):
                clock.now.return_value = observed_at
                stdout = self.native(
                    mcp_server.PREPARATION_AUTHORITY_TOOL,
                    str(project),
                    str(work),
                    {"operation": "status", "item_id": "work-c"},
                )
            self.assertNotIn("code", stdout)
            self.assertEqual(expected, stdout["authority_status"])
            self.assertEqual(1, clock.now.call_count)

        state = self.state_with_preparation(
            status=authority_models.PreparationLeaseStatus.RELEASED,
            historical=True,
        )
        project, work, _store = self.initialized_state(state)

        with patch("pinboard.mcp.server.datetime") as clock:
            clock.now.return_value = SQLITE_NOW + timedelta(days=1)
            stdout = self.native(
                mcp_server.PREPARATION_AUTHORITY_TOOL,
                str(project),
                str(work),
                {"operation": "status", "item_id": "work-c"},
            )
        self.assertNotIn("code", stdout)
        self.assertEqual(1, stdout["definition_revision"])
        self.assertEqual("released", stdout["authority_status"])
        self.assertEqual(1, clock.now.call_count)

        stdout = self.native(
            mcp_server.ATTEMPT_AUTHORITY_TOOL,
            str(project),
            str(work),
            {"operation": "status", "attempt_id": "work-a-1"},
        )
        self.assertNotIn("code", stdout)
        self.assertEqual("active", stdout["authority_status"])

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
                stdout = self.native(
                    mcp_server.ATTEMPT_AUTHORITY_TOOL,
                    str(retained_project),
                    str(retained_work),
                    {"operation": "status", "attempt_id": "work-a-1"},
                )
            self.assertNotIn("code", stdout)
            self.assertEqual(
                "expired" if retained_status == authority_models.AttemptLeaseStatus.ACTIVE else retained_status.value,
                stdout["authority_status"],
            )

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

        with self.rejected_storage():
            self.native(
                mcp_server.ATTEMPT_AUTHORITY_TOOL,
                str(project),
                str(work),
                {"operation": "status", "attempt_id": "work-a-1"},
            )

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

        with self.rejected_storage():
            self.native(
                mcp_server.PREPARATION_AUTHORITY_TOOL,
                str(stale_project),
                str(stale_work),
                {"operation": "status", "item_id": "work-c"},
            )

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
            stdout = self.native(
                mcp_server.ATTEMPT_AUTHORITY_TOOL,
                str(project),
                str(work),
                {"operation": "status", "attempt_id": "work-a-1"},
            )
        self.assertNotIn("code", stdout)
        self.assertEqual("active", stdout["authority_status"])
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
