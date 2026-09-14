import contextlib
import io
import json
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
from pinboard.interfaces.cli import main
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store


class OrderingTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve()
        self.roots = resolve_durable_roots(self.project)
        initialize_database(self.roots, SQLITE_NOW)
        self.store = SQLiteWorkStore(self.roots.database_path)
        initialize_store(self.store, complete_sqlite_state())
        self.before = self.store.validated_snapshot()
        with self.store.write() as transaction:
            self.order = transaction.read_live_order()

    def reorder(self, expected: tuple[ItemId, ...], requested: tuple[ItemId, ...]) -> CommittedEffect | DecisionFailure:
        return service.reorder(self.store, expected, requested, TaskId("owner"), HostId("local"), SQLITE_NOW)

    def cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(("--project-root", str(self.project), *arguments))
        return result, stdout.getvalue(), stderr.getvalue()

    def test_installed_order_preserves_non_order_facts_and_refreshes_only_changed_views(self) -> None:
        views = self.roots.work_root / "views"
        (views / "items").mkdir(parents=True)
        for item in self.before.lifecycle.work_items:
            (views / "items" / f"{item.item_id}.md").write_text("unchanged projection sentinel")
        old_files = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in views.rglob("*.md")}
        requested = (self.order[1], self.order[0], *self.order[2:])
        payload = self.project / "order.json"
        payload.write_text(
            json.dumps({"schema": "pinboard-live-order/v1", "expected_order": self.order, "requested_order": requested})
        )
        result, stdout, stderr = self.cli(
            "order", "--file", str(payload), "--task-id", "owner", "--host-id", "local", "--json"
        )
        self.assertEqual(0, result, stderr)
        receipt = json.loads(stdout)
        self.assertEqual(list(requested), receipt["order"])
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
        self.assertEqual(list(requested), json.loads(after.transition_receipts[-1].input_payload)["requested_order"])
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
        code, stdout, stderr = self.cli("overview", "--json")
        self.assertEqual(0, code, stderr)
        overview = json.loads(stdout)
        self.assertEqual({"item_id": "zz-proposal-a", "live_dependencies": ["work-c"]}, overview["next_unstarted"])
        self.assertNotIn("zz-proposal-a", overview["immediate_options"])
        self.assertIn("work-c", overview["immediate_options"])
        self.assertIn("next_unstarted=zz-proposal-a live_dependencies=work-c", self.cli("overview")[1])
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

    def test_strict_discoverable_input_and_empty_order(self) -> None:
        contract = json.loads(self.cli("tool-contract", "--operation", "order", "--json")[1])
        self.assertEqual("current-project", contract["data_scope"])
        self.assertEqual("direct-project-operation-with-task-host-attribution", contract["required_authority"])
        payload = self.project / "invalid.json"
        for value in (
            {
                "schema": "pinboard-live-order/v1",
                "expected_order": self.order,
                "requested_order": self.order,
                "extra": True,
            },
            {"schema": "pinboard-live-order/v1", "expected_order": self.order, "requested_order": ["work-a", "work-a"]},
            {"schema": "wrong", "expected_order": (), "requested_order": ()},
        ):
            payload.write_text(json.dumps(value))
            self.assertNotEqual(
                0, self.cli("order", "--file", str(payload), "--task-id", "owner", "--host-id", "local", "--json")[0]
            )
            self.assertEqual(self.before, self.store.validated_snapshot())
        self.assertNotEqual(
            0, self.cli("order", "--file", str(self.project / "missing"), "--task-id", "owner", "--host-id", "local")[0]
        )

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
