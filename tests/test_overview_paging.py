"""Bound complete discovery envelopes and selected storage work."""

import asyncio
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mcp import Client

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite import overview as overview_storage
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.domain import work_models
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import TaskId, WorkItemId
from pinboard.mcp import host_profiles
from tests.checkpoint_support import CheckpointPackageSupport
from tests.native_support import native_tools
from tests.support import (
    SQLITE_NOW,
    JsonObject,
    JsonValue,
    complete_sqlite_state,
    initialize_store,
    with_definition_dependencies,
)


class OverviewPagingTest(CheckpointPackageSupport):
    def test_sdk_traversal_bounds_unicode_growth_and_restarts_after_order_change(self) -> None:  # noqa: PLR0915 - one SDK traversal and stale-order journey
        fixture = self.checkpoint_fixture()
        with native_tools() as native:
            roots = resolve_durable_roots(fixture.project, fixture.project / "discovery")
            initialize_database(roots, SQLITE_NOW)
            store = SQLiteWorkStore(roots.database_path)
            state = complete_sqlite_state()
            original = next(item for item in state.lifecycle.work_items if item.item_id == WorkItemId("work-c"))
            definition = next(
                value for value in state.lifecycle.definition_revisions if value.item_id == original.item_id
            )
            added_ids = tuple(WorkItemId(f"extra-{index:03}") for index in range(70))
            title = '漢😀"\\' * 2000
            assert isinstance(definition.definition, work_models.WorkItemDefinition)
            large_definition = replace(definition.definition, title=title)
            digest = work_item_definition_digest(large_definition)
            assert isinstance(digest, str)
            grown = replace(
                state,
                lifecycle=replace(
                    state.lifecycle,
                    work_items=(
                        *state.lifecycle.work_items,
                        *(
                            replace(original, item_id=item, queue_position=5 + index, notes="large note" * 10000)
                            for index, item in enumerate(added_ids)
                        ),
                    ),
                    definition_revisions=(
                        *state.lifecycle.definition_revisions,
                        *(
                            replace(
                                definition,
                                item_id=item,
                                definition=large_definition,
                                digest=digest,
                                after_digest=digest,
                            )
                            for item in added_ids
                        ),
                    ),
                ),
            )
            initialize_store(store, with_definition_dependencies(grown, WorkItemId("work-c"), added_ids))
            arguments: JsonObject = {
                "project_root": str(fixture.project),
                "work_root": str(roots.work_root),
                "max_items": 25,
                "continuation": None,
            }
            identities: list[str] = []
            sizes: list[int] = []

            async def read_page(selected: JsonObject) -> JsonObject:
                async with Client(native.server, mode="legacy") as client:
                    result = await client.call_tool("pinboard_overview", selected)
                    self.assertFalse(result.is_error)
                    content = result.structured_content
                    assert isinstance(content, dict)
                    sent = len(result.model_dump_json(by_alias=True, exclude_none=True).encode())
                    sizes.append(sent)
                    budget = content["max_bytes"]
                    assert isinstance(budget, int)
                    self.assertLessEqual(sent, budget)
                    self.assertLessEqual(
                        sent, host_profiles.envelope_size(content, host_profiles.Representation.STRUCTURED_AND_TEXT)
                    )
                    return content

            first = page = asyncio.run(read_page(arguments))
            while True:
                rows = self.json_array(page["items"])
                self.assertLessEqual(len(rows), 25)
                self.assertEqual(first["revision"], page["revision"])
                self.assertEqual(first["next_unstarted"], page["next_unstarted"])
                for value in rows:
                    row = self.json_object(value)
                    identity = row["item_id"]
                    assert isinstance(identity, str)
                    identities.append(identity)
                    if identity.startswith("extra-"):
                        self.assertEqual(title[:160], row["label"])
                    elif identity == "work-c":
                        self.assertEqual(len(added_ids), row["dependency_count"])
                        self.assertEqual(len(added_ids), row["unsatisfied_dependency_count"])
                if page["continuation"] is None:
                    break
                page = asyncio.run(read_page(arguments | {"continuation": page["continuation"]}))
            self.assertEqual(["intake-work", "work-a", "work-c", "zz-proposal-a", *added_ids], identities)
            status = native.call_advertised_tool(
                "pinboard_item_status",
                {
                    "request": {
                        "project_root": str(fixture.project),
                        "work_root": str(roots.work_root),
                        "operation": "item",
                        "item_id": "work-c",
                    }
                },
            )
            self.assertEqual(len(added_ids), len(self.json_array(status["dependencies"])))
            detail = native.call_advertised_tool(
                "pinboard_item_definition",
                {
                    "request": {
                        "project_root": str(fixture.project),
                        "work_root": str(roots.work_root),
                        "operation": "current",
                        "item_id": added_ids[-1],
                    }
                },
            )
            self.assertEqual(title, self.json_object(detail["definition"])["title"])
            order: JsonObject = {
                "schema": "pinboard-live-order/v1",
                "expected_order": list[JsonValue](identities[:1]),
                "requested_order": list[JsonValue](identities[:1]),
            }
            mutation: JsonObject = {
                "project_root": str(fixture.project),
                "work_root": str(roots.work_root),
                "order": order,
                "actor_task_id": "test",
                "actor_host_id": "local",
            }
            rejected = native.call_advertised_tool("pinboard_order", {"request": mutation})
            self.assertEqual("unchanged", rejected["effect"])
            order.update(expected_order=list[JsonValue](identities), requested_order=list[JsonValue](identities[::-1]))
            changed = native.call_advertised_tool("pinboard_order", {"request": mutation})
            self.assertEqual("committed", changed["effect"])
            stale = native.call_advertised_tool(
                "pinboard_overview", arguments | {"continuation": first["continuation"]}
            )
            self.assertEqual("unchanged", stale["effect"])
            self.assertIn("Restart", str(stale["message"]))
            print(f"overview: {len(identities)} rows, {len(sizes)} pages, maximum complete envelope {max(sizes)} bytes")

    def test_page_storage_reads_only_selected_compact_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            roots = resolve_durable_roots(Path(temporary))
            initialize_database(roots, SQLITE_NOW)
            store = SQLiteWorkStore(roots.database_path)
            initialize_store(store, complete_sqlite_state())
            statements: list[str] = []
            decoded_columns: list[tuple[str, ...]] = []
            original_open = sqlite_store.open_database
            original_decode = overview_storage.decode_row

            def recorded_decode[Record](row: sqlite3.Row, record_type: type[Record]) -> Record:
                decoded_columns.append(tuple(row.keys()))
                return original_decode(row, record_type)

            def traced_open(path: Path, mode: OpenMode) -> sqlite3.Connection:
                connection = original_open(path, mode)
                connection.set_trace_callback(statements.append)
                return connection

            with (
                patch.object(sqlite_store, "open_database", traced_open),
                patch.object(overview_storage, "decode_row", recorded_decode),
                patch(
                    "pinboard.adapters.sqlite.store.read_current_snapshot", side_effect=AssertionError("full portfolio")
                ),
            ):
                page = store.read_overview_page(2, 1)
            self.assertEqual(["work-c"], [item.item_id for item in page.items])
            self.assertEqual("intake-work", page.next_unstarted.item_id if page.next_unstarted else None)
            self.assertFalse(any("from proposals" in query.lower() for query in statements))
            self.assertEqual(4, len(decoded_columns))
            self.assertFalse(any("definition_json" in columns or "notes" in columns for columns in decoded_columns))
            selection = next(query for query in statements if "WITH selected AS MATERIALIZED" in query)
            with closing(sqlite3.connect(roots.database_path)) as connection:
                plan = tuple(row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + selection))
            self.assertTrue(any("queue_position>?" in detail for detail in plan), plan)
            self.assertTrue(any("checkpoint_acceptances_by_dependency" in detail for detail in plan), plan)

    def test_focused_status_recovers_full_context_omitted_from_discovery(self) -> None:
        fixture = self.checkpoint_fixture()
        roots = resolve_durable_roots(fixture.project, fixture.project / "selected-detail")
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        state = complete_sqlite_state()
        cost = "Replacement cost " + "漢😀" * 5000
        relation = stored_state.StoredPlannedReplacement(
            WorkItemId("work-c"),
            1,
            WorkItemId("work-a"),
            cost,
            work_models.PlannedReplacementStatus.CURRENT,
            TaskId("owner"),
            SQLITE_NOW,
            12,
        )
        initialize_store(store, replace(state, replacements=stored_state.ReplacementRecords((relation,), ())))
        common: JsonObject = {"project_root": str(fixture.project), "work_root": str(roots.work_root)}
        with native_tools() as native:
            page = native.call_advertised_tool("pinboard_overview", common | {"max_items": 1, "continuation": None})
            self.assertNotIn(cost, str(page))
            replacement = native.call_advertised_tool(
                "pinboard_item_status",
                {
                    "request": common
                    | {
                        "operation": "item",
                        "item_id": "work-c",
                    }
                },
            )
            self.assertEqual(cost, self.json_object(replacement["planned_replacement"])["replacement_cost"])
            proposal = native.call_advertised_tool(
                "pinboard_item_status",
                {
                    "request": common
                    | {
                        "operation": "item",
                        "item_id": "zz-proposal-a",
                    }
                },
            )
            origin = self.json_object(proposal["proposal_origin"])
            self.assertEqual(state.proposals.proposals[0].why_it_matters, origin["why_it_matters"])
            reason = self.json_object(self.json_array(proposal["dependency_reasons"])[0])
            self.assertIn(state.proposals.proposals[0].why_it_matters, str(reason["reason"]))
            self.assertEqual("work-c", self.json_object(self.json_array(proposal["dependencies"])[0])["target"])
            self.assertEqual("pinboard-item-status/v5", proposal["schema"])

    def test_invalid_continuations_and_oversized_identity_reject_without_echoing_input(self) -> None:
        fixture = self.checkpoint_fixture()
        common: JsonObject = {
            "project_root": str(fixture.project),
            "work_root": str(fixture.work),
            "max_items": 1,
            "continuation": None,
        }
        with native_tools() as native:
            first = native.call_advertised_tool("pinboard_overview", common)
            continuation = self.json_object(first["continuation"])
            for invalid in (
                continuation | {"after_position": 999},
                continuation | {"work_root": "/another-board"},
                continuation | {"extra": True},
            ):
                with self.subTest(continuation=invalid):
                    rejected = native.call_advertised_tool("pinboard_overview", common | {"continuation": invalid})
                    self.assertEqual("unchanged", rejected["effect"])
            roots = resolve_durable_roots(fixture.project, fixture.project / "oversized")
            initialize_database(roots, SQLITE_NOW)
            store = SQLiteWorkStore(roots.database_path)
            state = complete_sqlite_state()
            original = next(value for value in state.lifecycle.work_items if value.item_id == WorkItemId("intake-work"))
            definition = next(
                value for value in state.lifecycle.definition_revisions if value.item_id == original.item_id
            )
            oversized = WorkItemId("x" * 10000)
            initialize_store(
                store,
                replace(
                    state,
                    lifecycle=replace(
                        state.lifecycle,
                        work_items=tuple(
                            replace(value, item_id=oversized) if value.item_id == original.item_id else value
                            for value in state.lifecycle.work_items
                        ),
                        definition_revisions=tuple(
                            replace(value, item_id=oversized) if value == definition else value
                            for value in state.lifecycle.definition_revisions
                        ),
                    ),
                ),
            )
            rejected = native.call_advertised_tool("pinboard_overview", common | {"work_root": str(roots.work_root)})
            self.assertEqual("unchanged", rejected["effect"])
            self.assertNotIn(str(oversized), str(rejected))
            self.assertIn("human", str(rejected["message"]))
