"""Original definition history stays factual while current revisions use genuine v2."""

import hashlib
import sqlite3
import unittest
from contextlib import closing
from typing import override

import msgspec

from pinboard.adapters.files.views import rebuild_facts
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import project_export, query_models, stored_state
from pinboard.domain import work_models
from pinboard.domain.identifiers import WorkItemId
from tests import test_mcp as mcp_support
from tests.native_support import call_advertised_tool
from tests.support import SQLITE_NOW, JsonObject, with_definition_dependencies


class HistoricalDefinitionTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        temporary, self.project, self.roots = mcp_support.McpTransportTest()._project()
        self.addCleanup(temporary.cleanup)
        self.store = SQLiteWorkStore(self.roots.database_path)
        self.explicit_roots: JsonObject = {
            "project_root": str(self.project),
            "work_root": str(self.roots.work_root),
        }

    def current(self, item_id: str) -> JsonObject:
        return call_advertised_tool(
            "pinboard_item_definition",
            {"request": {**self.explicit_roots, "operation": "current", "item_id": item_id}},
        )

    def historical_payload(self, item_id: str) -> bytes:
        value = self.current(item_id)["definition"]
        assert isinstance(value, dict)
        body = {key: field for key, field in value.items() if key not in {"checkout_policy", "obligations"}}
        body["schema"] = "pinboard-work-item-definition/v1"
        return msgspec.json.encode(body, order="sorted") + b"\n"

    def replace_original(self, item_id: str, payload: bytes) -> None:
        digest = hashlib.sha256(payload).hexdigest()
        with closing(sqlite3.connect(self.roots.database_path)) as connection, connection:
            connection.execute(
                "UPDATE work_item_definition_revisions SET definition_json = ?, definition_digest = ?, after_digest = ? "
                "WHERE item_id = ? AND definition_revision = 1",
                (payload, digest, digest, item_id),
            )
            connection.execute(
                "UPDATE work_item_definition_revisions SET before_digest = ? WHERE item_id = ? AND definition_revision = 2",
                (digest, item_id),
            )

    def revise(self, item_id: str, definition: JsonObject) -> JsonObject:
        current = self.current(item_id)
        return call_advertised_tool(
            "pinboard_transition",
            {
                "request": {
                    **self.explicit_roots,
                    "role": "project",
                    "actor_task_id": "definition-owner",
                    "actor_host_id": "local",
                    "receipt": {
                        "action_id": {"kind": "revise-item", "subject": item_id},
                        "subject_revision": str(current["item_subject_revision"]),
                    },
                    "payload": {
                        "schema": "pinboard-item-revision/v1",
                        "expected_revision": current["definition_revision"],
                        "expected_digest": current["definition_digest"],
                        "source_task": "definition-owner",
                        "reason": "Record the supported current objective.",
                        "definition": definition,
                    },
                }
            },
        )

    def test_native_append_reload_history_export_and_terminal_view_keep_original_bytes(self) -> None:
        original = self.historical_payload("work-c")
        current = self.current("work-c")["definition"]
        assert isinstance(current, dict)
        self.assertEqual("committed", self.revise("work-c", current)["status"])
        self.replace_original("work-c", original)
        terminal = self.historical_payload("work-b")
        self.replace_original("work-b", terminal)
        with closing(sqlite3.connect(self.roots.database_path)) as connection, connection:
            prior_rows = connection.execute(
                "SELECT * FROM work_item_definition_revisions ORDER BY item_id, definition_revision"
            ).fetchall()
            prior_items = connection.execute("SELECT * FROM work_items ORDER BY item_id").fetchall()
        self.assertEqual(
            "committed", self.revise("work-c", {**current, "objective": "A fresh stored current value."})["status"]
        )
        reopened = SQLiteWorkStore(self.roots.database_path)
        state = reopened.validated_snapshot()
        accepted = next(
            value for value in state.lifecycle.definition_revisions if value.item_id == "work-c" and value.revision == 3
        )
        assert isinstance(accepted.definition, work_models.WorkItemDefinition)
        self.assertEqual("A fresh stored current value.", accepted.definition.objective)
        with closing(sqlite3.connect(self.roots.database_path)) as connection, connection:
            after_rows = connection.execute(
                "SELECT * FROM work_item_definition_revisions WHERE NOT (item_id = 'work-c' AND definition_revision = 3) "
                "ORDER BY item_id, definition_revision"
            ).fetchall()
            after_items = connection.execute("SELECT * FROM work_items ORDER BY item_id").fetchall()
        self.assertEqual(prior_rows, after_rows)
        self.assertEqual(
            [row for row in prior_items if row[0] != "work-c"], [row for row in after_items if row[0] != "work-c"]
        )
        self.assertEqual(
            next(row for row in prior_items if row[0] == "work-c")[:7],
            next(row for row in after_items if row[0] == "work-c")[:7],
        )
        page = call_advertised_tool(
            "pinboard_item_definition",
            {
                "request": {
                    **self.explicit_roots,
                    "operation": "history",
                    "item_id": "work-c",
                    "limit": 100,
                    "before_revision": None,
                }
            },
        )
        history = msgspec.convert(page, type=query_models.ItemDefinitionHistory)
        self.assertEqual(original, msgspec.json.encode(history.revisions[-1].definition, order="sorted") + b"\n")
        self.assertEqual(terminal, msgspec.json.encode(self.current("work-b")["definition"], order="sorted") + b"\n")
        exported = project_export.project_export_from_state(reopened.read_project_export_batches()[0], (), (), (), ())
        decoded = msgspec.json.decode(msgspec.json.encode(exported), type=project_export.ProjectExport)
        row = next(value for value in decoded.definition_revisions if value.item_id == "work-b")
        self.assertEqual(terminal, msgspec.json.encode(row.definition, order="sorted") + b"\n")
        refreshed = rebuild_facts(
            reopened.read_all_generated_view_facts(SQLITE_NOW), self.roots.work_root, {}, reopened, SQLITE_NOW
        )
        self.assertIsNone(refreshed.warning)
        view = (self.roots.work_root / "views" / "items" / "work-b.md").read_text()
        self.assertIn("Historical definition schema: pinboard-work-item-definition/v1", view)
        self.assertNotIn("Checkout policy:", view)
        self.assertNotIn("### Obligations", view)
        status = call_advertised_tool(
            "pinboard_item_status", {"request": {**self.explicit_roots, "operation": "item", "item_id": "work-b"}}
        )
        self.assertEqual("superseded", status["state"])

    def test_strict_historical_corruption_is_rejected_independently_of_current_v2(self) -> None:
        original = self.historical_payload("work-b")
        malformed = (
            original[:-1],
            original.replace(b'"title":', b'"unknown":null,"title":'),
            original.replace(b"definition/v1", b"definition/v0"),
            original.replace(b'"dependencies":[]', b'"dependencies":["work-c","work-c"]'),
            original.replace(b'"title":"', b'"title":"\xff'),
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                self.replace_original("work-b", payload)
                with self.assertRaises(StorageError):
                    self.store.read_item_definition(WorkItemId("work-b"))
                with self.assertRaises(StorageError):
                    self.store.validated_snapshot()
        self.replace_original("work-b", original)
        with closing(sqlite3.connect(self.roots.database_path)) as connection, connection:
            connection.execute(
                "UPDATE work_item_definition_revisions SET definition_digest = ?, after_digest = ? WHERE item_id = 'work-b'",
                ("0" * 64, "0" * 64),
            )
        with self.assertRaises(StorageError):
            self.store.validated_snapshot()

    def test_live_historical_definition_cannot_authorize_current_execution(self) -> None:
        self.replace_original("work-c", self.historical_payload("work-c"))
        with self.assertRaises(StorageError):
            self.store.validated_snapshot()
        with self.assertRaises(StorageError):
            self.store.read_current_parallel_snapshot(SQLITE_NOW)

    def test_terminal_dependency_facts_preserve_mixed_cycle_and_lifecycle_rejections(self) -> None:
        state = with_definition_dependencies(
            self.store.validated_snapshot(), WorkItemId("work-b"), (WorkItemId("work-c"),)
        )
        current = next(value for value in state.lifecycle.definition_revisions if value.item_id == "work-b")
        payload = stored_state.stored_definition_bytes(current.definition)
        assert isinstance(payload, bytes)
        # Seed the independently recorded terminal edge with its original supported historical shape.
        historical = msgspec.json.decode(payload)
        assert isinstance(historical, dict)
        del historical["checkout_policy"]
        del historical["obligations"]
        historical["schema"] = "pinboard-work-item-definition/v1"
        self.replace_original("work-b", msgspec.json.encode(historical, order="sorted") + b"\n")
        with closing(sqlite3.connect(self.roots.database_path)) as connection, connection:
            connection.execute("INSERT INTO item_dependencies VALUES ('work-b', 'work-c', 0)")
        live = self.current("work-c")["definition"]
        assert isinstance(live, dict)
        rejected = self.revise("work-c", {**live, "dependencies": ["work-b"]})
        self.assertEqual("ITEM_DEPENDENCY_CYCLE", rejected["code"])
        terminal_rejection = self.revise("work-b", live)
        self.assertEqual("ACTION_NOT_AVAILABLE", terminal_rejection["code"])
        self.store.validated_snapshot()
        historical["dependencies"] = ["intake-work"]
        self.replace_original("work-b", msgspec.json.encode(historical, order="sorted") + b"\n")
        with closing(sqlite3.connect(self.roots.database_path)) as connection, connection:
            connection.execute("UPDATE item_dependencies SET dependency_id = 'intake-work' WHERE item_id = 'work-b'")
        self.assertEqual("committed", self.revise("work-c", {**live, "dependencies": ["work-b"]})["status"])
        self.store.validated_snapshot()
        with closing(sqlite3.connect(self.roots.database_path)) as connection, connection:
            connection.execute("DELETE FROM item_dependencies WHERE item_id = 'work-b'")
        with self.assertRaises(StorageError):
            self.store.validated_snapshot()
