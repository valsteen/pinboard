"""Exact origins through native application writes, fresh reads, ending previews and portable state."""

from dataclasses import replace
from unittest.mock import patch

import msgspec

from pinboard.adapters.sqlite import lifecycle
from pinboard.adapters.sqlite import obligation_coverage as coverage_storage
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import project_export, query_models
from pinboard.domain import work_models
from pinboard.domain.identifiers import WorkItemId
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import native_tools
from tests.support import JsonObject
from tests.test_proposals import proposal


class ObligationCoverageTest(CheckpointPackageSupport):
    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def create(
        self, fixture: CheckpointFixture, item_id: str, obligations: list[JsonObject], source: str | None
    ) -> JsonObject:
        value = proposal()
        value["proposal_id"] = item_id
        value["obligations"] = list(obligations)
        if source is not None:
            value.update(
                {
                    "schema": "pinboard-proposal/v3",
                    "position": None,
                    "relation": {
                        "kind": "planned-replacement",
                        "item": source,
                        "replacement_cost": "The source is superseded.",
                    },
                }
            )
        return fixture.call_advertised_tool(
            mcp_server.PROPOSAL_CREATE_TOOL,
            {**self.roots(fixture), "proposal": value, "actor_task_id": "human", "actor_host_id": "local"},
        )

    def own(self, name: str) -> JsonObject:
        return {"obligation_id": name, "statement": f"Deliver {name}.", "deferral_policy": "forbidden"}

    def carried(self, name: str, source: str, revision: int, obligation: str) -> JsonObject:
        return {
            **self.own(name),
            "origin": {"item_id": source, "definition_revision": revision, "obligation_id": obligation},
        }

    def current(self, fixture: CheckpointFixture, item: str) -> query_models.ItemDefinition:
        value = fixture.call_advertised_tool(
            mcp_server.ITEM_DEFINITION_TOOL,
            {"request": {**self.roots(fixture), "operation": "current", "item_id": item}},
        )
        return msgspec.convert(value, type=query_models.ItemDefinition)

    def revise(self, fixture: CheckpointFixture, item: str, obligations: list[JsonObject]) -> JsonObject:
        current = self.current(fixture, item)
        definition: JsonObject = msgspec.to_builtins(current.definition)
        definition["obligations"] = list(obligations)
        definition["schema"] = (
            "pinboard-work-item-definition/v3"
            if any("origin" in value for value in obligations)
            else "pinboard-work-item-definition/v2"
        )
        return self.transition_result(
            fixture,
            self.project_action(fixture, f"revise-item:{item}"),
            {
                "schema": "pinboard-item-revision/v1",
                "expected_revision": current.definition_revision,
                "expected_digest": current.definition_digest,
                "source_task": "human",
                "reason": "Keep the accepted scope current.",
                "definition": definition,
            },
        )

    def coverage(self, fixture: CheckpointFixture, source: str, replacement: str | None) -> JsonObject:
        request: JsonObject = {**self.roots(fixture), "operation": "planned-replacement", "item_id": source}
        if replacement is not None:
            request = {
                **self.roots(fixture),
                "operation": "pair",
                "source_item": source,
                "replacement_item": replacement,
            }
        return fixture.call_advertised_tool(mcp_server.OBLIGATION_COVERAGE_TOOL, {"request": request})

    def close(self, fixture: CheckpointFixture, item: str, decision: str) -> JsonObject:
        action = self.project_action(fixture, f"close:{item}")
        return fixture.call_advertised_tool(
            mcp_server.CLOSE_TOOL,
            {
                **self.roots(fixture),
                "receipt": {"action_id": action["action_id"], "subject_revision": action["subject_revision"]},
                "payload": {"outcome": "done", "reason": decision, "human_decision": decision},
                "actor_task_id": "human",
                "actor_host_id": "local",
            },
        )

    def test_origin_write_reload_export_views_and_ending_preview_agree(self) -> None:
        with native_tools() as native:
            fixture = replace(
                self.checkpoint_fixture(local=True),
                call_native_tool=native.call_native_tool,
                call_advertised_tool=native.call_advertised_tool,
            )
            self.assertEqual(
                "committed",
                self.create(fixture, "source-work", [self.own("first"), self.own("second")], None)["status"],
            )
            carries = [
                self.carried("owner-a", "source-work", 1, "first"),
                self.carried("owner-b", "source-work", 1, "first"),
                {**self.own("first"), "origin": None},
            ]
            self.assertEqual("committed", self.create(fixture, "replacement-work", carries, "source-work")["status"])
            fresh = SQLiteWorkStore(fixture.work / "state.sqlite3")
            selected = fresh.read_item_definition(WorkItemId("replacement-work")).definition
            assert selected is not None and isinstance(selected.definition, work_models.WorkItemDefinition)
            origin = selected.definition.obligations[0]
            assert isinstance(origin, work_models.CarriedWorkObligation)
            self.assertEqual(
                work_models.ObligationOrigin(WorkItemId("source-work"), 1, work_models.ObligationId("first")),
                origin.origin,
            )
            planned = self.coverage(fixture, "source-work", None)
            pair = self.coverage(fixture, "source-work", "replacement-work")
            self.assertEqual("ok", pair["status"], pair)
            report = pair["coverage"]
            assert isinstance(report, dict)
            rows = report["obligations"]
            assert isinstance(rows, list)
            self.assertEqual(["covered", "uncovered"], [row["status"] for row in rows if isinstance(row, dict)])
            self.assertEqual(planned["coverage"], report)
            first = rows[0]
            assert isinstance(first, dict) and isinstance(first["carrying"], list)
            self.assertEqual(
                ["owner-a", "owner-b"], [row["obligation_id"] for row in first["carrying"] if isinstance(row, dict)]
            )
            detail = self.project_action(fixture, "close:source-work")
            self.assertEqual(report, detail["replacement_coverage"])
            preview = fixture.call_advertised_tool(
                mcp_server.CANCEL_TOOL,
                {"request": {**self.roots(fixture), "operation": "preview", "item_id": "source-work"}},
            )
            displayed = preview["preview"]
            assert isinstance(displayed, dict)
            self.assertEqual(report, displayed["replacement_coverage"])
            self.assertEqual(
                "committed",
                self.revise(fixture, "replacement-work", [*carries, {**self.own("extra"), "origin": None}])["status"],
            )
            before = fixture.store.validated_snapshot()
            stale = fixture.call_advertised_tool(
                mcp_server.CANCEL_TOOL,
                {
                    "request": {
                        **self.roots(fixture),
                        "operation": "confirm",
                        "item_id": "source-work",
                        "binding": preview["binding"],
                        "human_confirmation": "Cancel source-work with its uncovered second obligation.",
                        "actor_task_id": "human",
                        "actor_host_id": "local",
                    }
                },
            )
            self.assertEqual("rejected", stale["status"], stale)
            self.assertEqual(before, fixture.store.validated_snapshot())
            self.assertIn("source-work@1/obligation/first", (fixture.work / "views" / "board.html").read_text())
            terminal_coverage = self.coverage(fixture, "source-work", None)["coverage"]
            self.assertEqual(
                "committed", self.close(fixture, "replacement-work", "Mark replacement-work done.")["status"]
            )
            self.assertEqual(terminal_coverage, self.coverage(fixture, "source-work", None)["coverage"])
            # Coverage informs an explicit otherwise-legal ending; it adds no veto.
            self.assertEqual(
                "committed",
                self.close(fixture, "source-work", "End source-work despite the uncovered second obligation.")[
                    "status"
                ],
            )
            self.assertIs(True, self.run_json_cli(*fixture.common, "validate")["valid"])
            exported_code, exported_text, exported_error = self.run_cli(*fixture.common, "export", "--json")
            self.assertEqual(0, exported_code, exported_error)
            exported = msgspec.json.decode(exported_text, type=project_export.ProjectExport)
            retained = next(
                value
                for value in exported.definition_revisions
                if value.item_id == "replacement-work" and value.revision == 1
            )
            assert isinstance(retained.definition, query_models.OriginWorkItemDefinitionView)
            self.assertEqual(carries[0], msgspec.to_builtins(retained.definition.obligations[0]))
            view = (fixture.work / "views" / "items" / "replacement-work.md").read_text()
            self.assertIn("source-work@1/obligation/first", view)

    def test_unresolvable_origins_reject_intake_and_revision_without_changing_state(self) -> None:
        with native_tools() as native:
            fixture = replace(
                self.checkpoint_fixture(local=True),
                call_native_tool=native.call_native_tool,
                call_advertised_tool=native.call_advertised_tool,
            )
            self.assertEqual("committed", self.create(fixture, "source-work", [self.own("first")], None)["status"])
            for item, revision, obligation in (
                ("missing-item", 1, "first"),
                ("source-work", 99, "first"),
                ("source-work", 1, "missing-obligation"),
            ):
                with self.subTest(item=item, revision=revision, obligation=obligation):
                    before = fixture.store.validated_snapshot()
                    values = [self.carried("carrying", item, revision, obligation)]
                    result = self.create(fixture, "replacement-work", values, "source-work")
                    self.assertEqual("rejected", result["status"], result)
                    self.assertEqual("ITEM_DEFINITION_INVALID", result["code"])
                    self.assertIn(item, str(result["message"]))
                    self.assertEqual(before, fixture.store.validated_snapshot())
                    revised = self.revise(fixture, "source-work", values)
                    self.assertEqual("rejected", revised["status"], revised)
                    self.assertEqual(before, fixture.store.validated_snapshot())

    def test_older_source_origin_is_provenance_and_never_current_coverage(self) -> None:
        with native_tools() as native:
            fixture = replace(
                self.checkpoint_fixture(local=True),
                call_native_tool=native.call_native_tool,
                call_advertised_tool=native.call_advertised_tool,
            )
            self.assertEqual("committed", self.create(fixture, "source-work", [self.own("first")], None)["status"])
            self.assertEqual(
                "committed",
                self.create(
                    fixture, "replacement-work", [self.carried("carrying", "source-work", 1, "first")], "source-work"
                )["status"],
            )
            self.assertEqual(
                "committed", self.revise(fixture, "source-work", [self.own("first"), self.own("second")])["status"]
            )
            current = self.coverage(fixture, "source-work", None)["coverage"]
            assert isinstance(current, dict) and isinstance(current["obligations"], list)
            self.assertEqual(
                ["uncovered", "uncovered"], [row["status"] for row in current["obligations"] if isinstance(row, dict)]
            )
            self.assertEqual(
                "committed",
                self.revise(fixture, "replacement-work", [self.carried("still-retained", "source-work", 1, "first")])[
                    "status"
                ],
            )
            for source, replacement, code in (
                ("absent", "replacement-work", "ITEM_NOT_FOUND"),
                ("source-work", "absent", "ITEM_NOT_FOUND"),
                ("replacement-work", None, "REPLACEMENT_INVALID"),
            ):
                result = self.coverage(fixture, source, replacement)
                self.assertEqual(
                    ("rejected", code, "unchanged"), (result["status"], result["code"], result["effect"]), result
                )

    def test_uncovered_cancellation_does_not_authorize_accepted_attempt_close(self) -> None:
        with native_tools() as native:
            fixture = replace(
                self.checkpoint_fixture(local=True),
                call_native_tool=native.call_native_tool,
                call_advertised_tool=native.call_advertised_tool,
            )
            # A valid origin on another source with the same obligation ID cannot cover work-a.
            self.assertEqual(
                "committed",
                self.create(
                    fixture, "replacement-work", [self.carried("next-decision", "work-c", 1, "next-decision")], "work-a"
                )["status"],
            )
            coverage = self.coverage(fixture, "work-a", None)["coverage"]
            assert isinstance(coverage, dict) and isinstance(coverage["obligations"], list)
            row = coverage["obligations"][0]
            assert isinstance(row, dict)
            self.assertEqual("uncovered", row["status"])
            before = fixture.store.validated_snapshot()
            close = fixture.call_advertised_tool(
                mcp_server.ACTIONS_TOOL,
                {
                    "request": {
                        **self.roots(fixture),
                        "role": "project",
                        "action_id": {"kind": "close", "subject": "work-a"},
                    }
                },
            )
            self.assertEqual("rejected", close["status"], close)
            self.assertEqual(before, fixture.store.validated_snapshot())
            preview = fixture.call_advertised_tool(
                mcp_server.CANCEL_TOOL,
                {"request": {**self.roots(fixture), "operation": "preview", "item_id": "work-a"}},
            )
            result = fixture.call_advertised_tool(
                mcp_server.CANCEL_TOOL,
                {
                    "request": {
                        **self.roots(fixture),
                        "operation": "confirm",
                        "item_id": "work-a",
                        "binding": preview["binding"],
                        "human_confirmation": "Cancel work-a despite its uncovered obligation and retain its evidence.",
                        "actor_task_id": "human",
                        "actor_host_id": "local",
                    }
                },
            )
            self.assertEqual("committed", result["status"], result)
            self.assertIs(True, self.run_json_cli(*fixture.common, "validate")["valid"])

    def test_selected_coverage_and_origins_do_not_read_unrelated_history_and_batch_selected_revisions(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        origins = (work_models.ObligationOrigin(WorkItemId("work-a"), 1, work_models.ObligationId("next-decision")),)
        with fixture.store.write() as transaction:
            connection = transaction.connection
            statements: list[str] = []
            connection.set_trace_callback(statements.append)
            with patch.object(
                coverage_storage, "decode_definition_revision", wraps=lifecycle.decode_definition_revision
            ) as decoded:
                self.assertEqual(frozenset(origins), coverage_storage.read_origins(connection, origins))
                self.assertEqual(1, decoded.call_count)
            initial_origin_statements = tuple(statements)
            statements.clear()
            initial = coverage_storage.read_facts(connection, WorkItemId("work-a"), WorkItemId("work-c"))
            initial_coverage_statements = tuple(statements)
            connection.set_trace_callback(None)
            unrelated = lifecycle.read_current_definition(connection, WorkItemId("zz-proposal-a"))
            selected = lifecycle.read_current_definition(connection, WorkItemId("work-a"))
            assert unrelated is not None and selected is not None
            for revision in range(2, 502):
                lifecycle.append_definition_revision(
                    connection, replace(unrelated, revision=revision, before_digest=unrelated.digest)
                )
            statements.clear()
            connection.set_trace_callback(statements.append)
            with patch.object(
                coverage_storage, "decode_definition_revision", wraps=lifecycle.decode_definition_revision
            ) as decoded:
                self.assertEqual(frozenset(origins), coverage_storage.read_origins(connection, origins))
                self.assertEqual(1, decoded.call_count)
            self.assertEqual(initial_origin_statements, tuple(statements))
            statements.clear()
            with patch.object(
                lifecycle, "decode_definition_revision", wraps=lifecycle.decode_definition_revision
            ) as decoded:
                self.assertEqual(
                    initial, coverage_storage.read_facts(connection, WorkItemId("work-a"), WorkItemId("work-c"))
                )
                self.assertEqual(
                    {WorkItemId("work-a"), WorkItemId("work-c")},
                    {call.args[0]["item_id"] for call in decoded.call_args_list},
                )
            self.assertEqual(initial_coverage_statements, tuple(statements))
            connection.set_trace_callback(None)
            for revision in range(2, 502):
                lifecycle.append_definition_revision(
                    connection, replace(selected, revision=revision, before_digest=selected.digest)
                )
            requested = tuple(replace(origins[0], definition_revision=revision) for revision in range(1, 502))
            statements.clear()
            connection.set_trace_callback(statements.append)
            self.assertEqual(frozenset(requested), coverage_storage.read_origins(connection, requested))
            self.assertTrue(1 < len(statements) < len(requested))
            connection.set_trace_callback(None)
            for statement in statements:
                plans = connection.execute("EXPLAIN QUERY PLAN " + statement).fetchall()
                self.assertTrue(any("SEARCH definition USING INDEX" in str(row[3]) for row in plans), plans)
