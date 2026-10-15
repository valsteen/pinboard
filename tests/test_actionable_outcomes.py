"""Caller continuations and diagnoses at installed response boundaries."""

import json
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import patch

from pinboard.adapters.files import contributor_traces, git_config
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.files.models import ViewRefreshResult, ViewWarning
from pinboard.adapters.sqlite.schema_procedure import SchemaProcedureFailure
from pinboard.application import query_models
from pinboard.cli import entrypoint
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import contract_schemas, execution, read_operations, server
from tests.checkpoint_support import CheckpointPackageSupport
from tests.native_support import call_advertised_tool
from tests.support import JsonObject


class ActionableOutcomeTest(CheckpointPackageSupport):
    def test_changed_source_can_be_replanned_without_overwriting_the_old_plan(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        source = fixture.project / "SOURCE.md"
        source.write_text("old source\n")
        request: JsonObject = {
            "project_root": str(fixture.project),
            "work_root": str(fixture.work),
            "operation": "plan",
            "max_batch_bytes": 2000,
            "manifest": {
                "schema": "pinboard-brief-sources/v1",
                "sources": [{"authority_id": "example", "families": ["behavior"], "selector": "SOURCE.md"}],
            },
        }
        before = fixture.store.validated_snapshot()
        planned = read_operations._brief_sources({"request": request}, execution.CancellationToken())
        source.write_text("changed source\n")
        emitted: JsonObject = {
            "project_root": str(fixture.project),
            "work_root": str(fixture.work),
            "operation": "emit",
            "plan": planned.content,
            "batch_index": 0,
        }
        rejected = read_operations._brief_sources({"request": emitted}, execution.CancellationToken())
        contract_schemas.validate_result(server.BRIEF_SOURCES_TOOL, rejected.content)
        self.assertEqual("BRIEF_SOURCE_CHANGED", rejected.content["code"])
        self.assertEqual("unchanged", rejected.content["effect"])
        self.assertIn("pinboard_brief_sources", str(rejected.content["message"]))
        replanned = read_operations._brief_sources({"request": request}, execution.CancellationToken())
        recovered = read_operations._brief_sources(
            {"request": emitted | {"plan": replanned.content}}, execution.CancellationToken()
        )
        self.assertIn("changed source", str(recovered.content["text"]))
        self.assertNotEqual(planned.content, replanned.content)
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_corrupt_accepted_bytes_name_the_resource_and_remain_untouched(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        context = fixture.store.read_attempt_context(AttemptId("work-a-1"))
        assert isinstance(context, query_models.NonterminalAttemptContextFacts)
        reference = context.brief_reference
        resource = fixture.work / reference.selector
        resource.write_bytes(b"corrupt")
        before = fixture.store.validated_snapshot()
        reply = read_operations._verify_artifact(
            str(fixture.project),
            str(fixture.work),
            int(context.brief_artifact_ref_id),
            reference.selector,
            reference.content_sha256,
            reference.size_bytes,
            execution.CancellationToken(),
        )
        contract_schemas.validate_result(server.ARTIFACT_VERIFY_TOOL, reply.content)
        self.assertEqual("ARTIFACT_BYTES_INVALID", reply.content["code"])
        self.assertEqual("do-not-retry", reply.content["retry"])
        self.assertIn(str(resource), str(reply.content["message"]))
        self.assertEqual(b"corrupt", resource.read_bytes())
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_terminal_cancellation_refusal_supplies_an_item_read(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        roots: JsonObject = {"project_root": str(fixture.project), "work_root": str(fixture.work), "item_id": "work-b"}
        before = fixture.store.validated_snapshot()
        rejected = call_advertised_tool(server.CANCEL_TOOL, {"request": roots | {"operation": "preview"}})
        self.assertEqual("rejected", rejected["status"])
        diagnostic = str(rejected["recovery"])
        next_input, _ = json.JSONDecoder().raw_decode(diagnostic[diagnostic.index("{") :])
        current = call_advertised_tool(server.ITEM_STATUS_TOOL, next_input)
        self.assertEqual("work-b", current["item_id"])
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_cli_view_warning_keeps_repair_without_replaying_close(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        repair = ViewWarning("view publication failed", "Run 'pinboard views rebuild'.")
        warning = ViewRefreshResult(12, repair)
        with patch("pinboard.cli.transitions.work_views.refresh_effect", return_value=warning):
            status, output, error = self.run_cli(
                *fixture.common,
                "close",
                "work-c",
                "--outcome",
                "done",
                "--reason",
                "Human requested this close.",
                "--task-id",
                "human",
                "--host-id",
                "local",
                "--json",
            )
        self.assertEqual(0, status, output + error)
        self.assertIn(repair.repair, error)
        self.assertEqual("done", self.json_object(json.loads(output))["outcome"])
        with patch("pinboard.cli.work_state_commands.work_views.rebuild", return_value=warning):
            status, output, error = self.run_cli(*fixture.common, "views", "rebuild")
        self.assertNotEqual(0, status)
        self.assertIn(repair.repair, error)

    def test_failed_result_output_keeps_committed_close_uncertain_until_inspected(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        with patch("pinboard.cli.transitions.write_json", side_effect=OSError("output connection closed")):
            status, output, error = self.run_cli(
                *fixture.common,
                "close",
                "work-c",
                "--outcome",
                "done",
                "--reason",
                "Human requested this close.",
                "--task-id",
                "human",
                "--host-id",
                "local",
                "--json",
            )
        self.assertEqual(2, status)
        self.assertEqual("", output)
        self.assertIn("effects are unknown", error)
        self.assertIn(str(fixture.work), error)
        inspected = call_advertised_tool(
            server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "operation": "item",
                    "item_id": "work-c",
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                }
            },
        )
        self.assertEqual("done", inspected["state"])

    def test_migration_preview_and_reverse_do_not_advertise_an_apply_plan(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        before = fixture.store.validated_snapshot()
        failure = SchemaProcedureFailure("selected database cannot satisfy this preview", (), "correct-input")
        for arguments, expected, forbidden in (
            ((), "pinboard migrate-schema", "--apply"),
            (("--reverse", "forward-id"), "--reverse forward-id", "--apply"),
        ):
            with (
                self.subTest(arguments=arguments),
                patch("pinboard.cli.schema_migration.preview_schema", return_value=failure),
            ):
                status, output, _ = self.run_cli(*fixture.common, "migrate-schema", *arguments, "--json")
                self.assertEqual(11, status)
                reply = self.json_object(json.loads(output))
                observations = {
                    self.json_object(value)["field"]: self.json_object(value)["value"]
                    for value in self.json_array(reply["observed"])
                }
                self.assertIn(expected, str(observations["next_step"]))
                self.assertNotIn(forbidden, str(observations["next_step"]))
                self.assertEqual([], reply["changed_surfaces"])
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_withdrawn_replacement_revision_can_be_corrected_from_the_native_refusal(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        payload: JsonObject = {
            "schema": "pinboard-planned-replacement/v1",
            "expected_relation_revision": 0,
            "replacement_item": "work-b",
            "replacement_cost": "The later replacement removes this work.",
            "status": "withdrawn",
            "recorded_by": "planner",
        }
        recorded = self.transition_result(fixture, self.project_action(fixture, "record-replacement:work-c"), payload)
        self.assertEqual("committed", recorded["status"])
        before = fixture.store.validated_snapshot()
        refused = self.transition_result(
            fixture, self.project_action(fixture, "record-replacement:work-c"), payload | {"status": "current"}
        )
        self.assertEqual("REPLACEMENT_STALE", refused["code"])
        observations = {
            self.json_object(value)["field"]: self.json_object(value)["value"]
            for value in self.json_array(refused["observed"])
        }
        self.assertEqual(1, observations["current_relation_revision"])
        self.assertEqual(before, fixture.store.validated_snapshot())
        corrected = self.transition_result(
            fixture,
            self.project_action(fixture, "record-replacement:work-c"),
            payload | {"status": "current", "expected_relation_revision": observations["current_relation_revision"]},
        )
        self.assertEqual("committed", corrected["status"])
        self.assertEqual(2, fixture.store.validated_snapshot().replacements.planned_replacements[-1].relation_revision)

    def test_cli_trace_preflight_reports_confirmed_auxiliary_writes_before_target(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        roots = resolve_durable_roots(fixture.project, fixture.work)
        settings = roots.work_root / contributor_traces.SETTINGS_NAME
        settings.unlink(missing_ok=True)
        error = StringIO()
        with (
            patch.object(
                git_config,
                "add",
                return_value=git_config.WriteUnconfirmed(
                    settings,
                    "pinboard.unsafe_persist_exact_pinboard_traces.mode",
                    git_config.ProcessFailed(128, "mode write failed"),
                ),
            ),
            redirect_stderr(error),
        ):
            status = entrypoint.main(("--contributor-capture-select", *fixture.common, "status", "--json"))
        self.assertEqual(64, status)
        self.assertIn(str(settings), error.getvalue())
        self.assertIn("settings_file_creation=confirmed", error.getvalue())
        self.assertIn("settings_mode_write=none", error.getvalue())
        self.assertEqual(b"", settings.read_bytes())
