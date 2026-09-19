import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import override
from unittest.mock import patch

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import queries, service, stored_state
from pinboard.application.mutation_models import CommittedEffect
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import HostId, ItemId, LeaseId, TaskId
from pinboard.mcp import server as mcp_server
from tests.native_support import call_native_tool
from tests.support import SQLITE_NOW, JsonObject, JsonValue, complete_sqlite_state, initialize_store


class OrderingTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve()
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True, capture_output=True)
        self.roots = resolve_durable_roots(self.project)
        initialize_database(self.roots, SQLITE_NOW)
        self.store = SQLiteWorkStore(self.roots.database_path)
        initialize_store(self.store, complete_sqlite_state())
        self.before = self.store.validated_snapshot()
        with self.store.write() as transaction:
            self.order = transaction.read_live_order()

    def reorder(self, expected: tuple[ItemId, ...], requested: tuple[ItemId, ...]) -> CommittedEffect | DecisionFailure:
        return service.reorder(self.store, expected, requested, TaskId("owner"), HostId("local"), SQLITE_NOW)

    def native_order(self, order: JsonObject) -> JsonObject:
        with patch("pinboard.mcp.read_operations.datetime") as clock:
            clock.now.return_value = SQLITE_NOW
            return call_native_tool(
                mcp_server.ORDER_TOOL,
                {
                    "request": {
                        "project_root": str(self.project),
                        "work_root": str(self.roots.work_root),
                        "order": order,
                        "actor_task_id": "owner",
                        "actor_host_id": "local",
                    }
                },
            )

    def test_installed_order_preserves_non_order_facts_and_refreshes_only_changed_views(self) -> None:
        views = self.roots.work_root / "views"
        (views / "items").mkdir(parents=True)
        for item in self.before.lifecycle.work_items:
            (views / "items" / f"{item.item_id}.md").write_text("unchanged projection sentinel")
        old_files = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in views.rglob("*.md")}
        requested = (self.order[1], self.order[0], *self.order[2:])
        receipt = self.native_order(
            {
                "schema": "pinboard-live-order/v1",
                "expected_order": list[JsonValue](self.order),
                "requested_order": list[JsonValue](requested),
            }
        )
        self.assertIn(receipt["status"], ("committed", "committed-with-warning"))
        self.assertEqual(list[JsonValue](requested), receipt["order"])
        fresh = SQLiteWorkStore(self.roots.database_path)
        after = fresh.validated_snapshot()
        original_items = {item.item_id: item for item in self.before.lifecycle.work_items}
        self.assertEqual(
            self.before,
            replace(
                after,
                lifecycle=replace(
                    after.lifecycle,
                    project=self.before.lifecycle.project,
                    work_items=tuple(
                        replace(item, queue_position=original_items[item.item_id].queue_position)
                        for item in after.lifecycle.work_items
                    ),
                ),
                transition_receipts=after.transition_receipts[:-1],
            ),
        )
        self.assertEqual(receipt["history_id"], int(after.transition_receipts[-1].history_id))
        self.assertEqual("owner", after.transition_receipts[-1].actor_task_id)
        self.assertEqual("local", after.transition_receipts[-1].actor_host_id)
        self.assertEqual(
            list[JsonValue](requested), json.loads(after.transition_receipts[-1].input_payload)["requested_order"]
        )
        for path, previous in old_files.items():
            if path.name not in {f"{self.order[0]}.md", f"{self.order[1]}.md"}:
                self.assertEqual(previous, (path.read_bytes(), path.stat().st_mtime_ns))
        overview = queries.project_current_overview(fresh.read_project_overview(SQLITE_NOW), SQLITE_NOW)
        self.assertEqual(requested, tuple(item.item_id for item in overview.items))

    def test_invalid_requests_and_stale_competitors_leave_ledger_unchanged(self) -> None:
        invalid = (
            self.order[:-1],
            (*self.order, ItemId("unknown")),
            (self.order[0],) * len(self.order),
            (*self.order[:-1], ItemId("work-b")),
        )
        for requested in invalid:
            with self.subTest(requested=requested):
                self.assertIsInstance(self.reorder(self.order, requested), DecisionFailure)
                self.assertEqual(self.before, self.store.validated_snapshot())
        # Both callers captured the same expected sequence before either writes.
        self.assertNotIsInstance(self.reorder(self.order, self.order[::-1]), DecisionFailure)
        winner = self.store.validated_snapshot()
        self.assertIsInstance(self.reorder(self.order, self.order[1:] + self.order[:1]), DecisionFailure)
        self.assertEqual(winner, self.store.validated_snapshot())

    def test_effect_failure_rolls_back_partial_position_updates(self) -> None:
        with (
            patch(
                "pinboard.adapters.sqlite.state.append_history",
                side_effect=StorageError(StorageErrorCode.IO_ERROR, "injected receipt failure"),
            ),
            self.assertRaises(StorageError),
        ):
            self.reorder(self.order, self.order[::-1])
        self.assertEqual(self.before, self.store.validated_snapshot())

    def test_order_reads_no_retained_state_and_noop_changes_no_item_views(self) -> None:
        with patch("pinboard.adapters.sqlite.state.read_state", side_effect=AssertionError("complete state read")):
            effect = self.reorder(self.order, self.order)
        self.assertNotIsInstance(effect, DecisionFailure)
        assert not isinstance(effect, DecisionFailure)
        self.assertEqual((), effect.item_ids)
        self.assertEqual((), effect.attempt_ids)

    def test_blocked_first_unstarted_is_visible_without_granting_eligibility(self) -> None:
        requested = (ItemId("work-a"), ItemId("zz-proposal-a"), ItemId("work-c"), ItemId("intake-work"))
        self.assertNotIsInstance(self.reorder(self.order, requested), DecisionFailure)
        overview = call_native_tool(
            mcp_server.OVERVIEW_TOOL,
            {
                "project_root": str(self.project),
                "work_root": str(self.roots.work_root),
            },
        )
        next_unstarted = overview["next_unstarted"]
        assert isinstance(next_unstarted, dict)
        self.assertEqual("zz-proposal-a", next_unstarted["item_id"])
        dependencies = next_unstarted["live_dependencies"]
        assert isinstance(dependencies, list)
        self.assertEqual(["work-c"], dependencies)
        immediate_options = overview["immediate_options"]
        assert isinstance(immediate_options, list)
        self.assertNotIn("zz-proposal-a", immediate_options)
        self.assertIn("work-c", immediate_options)
        for state in (work_models.AttemptState.PAUSED, work_models.AttemptState.REVIEW):
            snapshot = self.store.read_project_overview(SQLITE_NOW)
            items = tuple(
                replace(item, state=work_models.WorkState(state.value)) if item.item == ItemId("work-a") else item
                for item in snapshot.snapshot.items
            )
            attempts = tuple(replace(attempt, state=state) for attempt in snapshot.snapshot.attempts)
            overview = queries.project_current_overview(
                replace(snapshot, snapshot=replace(snapshot.snapshot, items=items, attempts=attempts)), SQLITE_NOW
            )
            assert overview.next_unstarted is not None
            self.assertEqual("zz-proposal-a", overview.next_unstarted.item_id)

    def test_native_order_rejects_unknown_fields_duplicate_items_and_wrong_schema(self) -> None:
        invalid_values: tuple[JsonObject, ...] = (
            {
                "schema": "pinboard-live-order/v1",
                "expected_order": list[JsonValue](self.order),
                "requested_order": list[JsonValue](self.order),
                "extra": True,
            },
            {
                "schema": "pinboard-live-order/v1",
                "expected_order": list[JsonValue](self.order),
                "requested_order": ["work-a", "work-a"],
            },
            {"schema": "wrong", "expected_order": [], "requested_order": []},
        )
        for value in invalid_values:
            with self.subTest(value=value):
                rejected = self.native_order(value)
                self.assertEqual("rejected", rejected["status"])
                self.assertFalse(rejected["state_changed"])
                self.assertEqual(self.before, self.store.validated_snapshot())

    def test_priority_preserves_replacement_and_preparation_conditions(self) -> None:
        item = ItemId("work-c")
        requested = (item, *(value for value in self.order if value != item))
        for retained in (False, True):
            with self.subTest(temporarily_retained=retained):
                (self.project / str(retained)).mkdir()
                roots = resolve_durable_roots(self.project / str(retained))
                initialize_database(roots, SQLITE_NOW)
                store = SQLiteWorkStore(roots.database_path)
                relation = stored_state.StoredPlannedReplacement(
                    item,
                    1,
                    ItemId("work-a"),
                    "Duplicate work.",
                    work_models.PlannedReplacementStatus.CURRENT,
                    TaskId("owner"),
                    SQLITE_NOW,
                    12,
                )
                disposition = stored_state.StoredReplacementDisposition(
                    item,
                    1,
                    "Needed first.",
                    "Duplicate work.",
                    TaskId("owner"),
                    SQLITE_NOW,
                    12,
                )
                state = replace(
                    self.before,
                    replacements=stored_state.ReplacementRecords((relation,), (disposition,) if retained else ()),
                )
                initialize_store(store, state)
                result = service.reorder(store, self.order, requested, TaskId("owner"), HostId("local"), SQLITE_NOW)
                self.assertNotIsInstance(result, DecisionFailure)
                overview = queries.project_current_overview(store.read_project_overview(SQLITE_NOW), SQLITE_NOW)
                assert overview.next_unstarted is not None
                self.assertEqual(item, overview.next_unstarted.item_id)
                self.assertEqual(retained, item in overview.immediate_options)
                self.assertEqual(state.replacements, store.validated_snapshot().replacements)
        prepared = service.start_preparation(
            self.store,
            item_id=item,
            task_id=TaskId("preparer"),
            host_id=HostId("local"),
            lease_id=LeaseId("preparation-order"),
            acquired_at=SQLITE_NOW,
            expires_at=SQLITE_NOW + timedelta(hours=1),
        )
        self.assertNotIsInstance(prepared, DecisionFailure)
        before_authority = self.store.validated_snapshot().authority
        self.assertNotIsInstance(self.reorder(self.order, requested), DecisionFailure)
        overview = queries.project_current_overview(self.store.read_project_overview(SQLITE_NOW), SQLITE_NOW)
        assert overview.next_unstarted is not None
        self.assertEqual(item, overview.next_unstarted.item_id)
        self.assertNotIn(item, overview.immediate_options)
        self.assertEqual(before_authority, self.store.validated_snapshot().authority)

    def test_empty_project_has_no_unstarted_item_and_accepts_empty_order(self) -> None:
        (self.project / "empty").mkdir()
        roots = resolve_durable_roots(self.project / "empty")
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        result = service.reorder(store, (), (), TaskId("owner"), HostId("local"), SQLITE_NOW)
        self.assertNotIsInstance(result, DecisionFailure)
        overview = queries.project_current_overview(store.read_project_overview(SQLITE_NOW), SQLITE_NOW)
        self.assertIsNone(overview.next_unstarted)
        self.assertEqual((), overview.items)
