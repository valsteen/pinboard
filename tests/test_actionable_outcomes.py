"""Caller continuations and diagnoses at installed response boundaries."""

import errno
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from typing import override
from unittest.mock import patch

import msgspec

from pinboard.adapters.files import contributor_traces, git_config
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.files.models import ViewRefreshResult, ViewWarning
from pinboard.adapters.sqlite.migration.schema_procedure import SchemaProcedureFailure
from pinboard.application import query_models
from pinboard.cli import cli_output, entrypoint, invocation_capture, work_state_commands
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import contracts, execution, read_operations, server
from tests.checkpoint_support import CheckpointPackageSupport
from tests.native_support import call_advertised_tool
from tests.support import JsonObject


class BrokenOutput(StringIO):
    """An output connection whose reader has gone away."""

    @override
    def write(self, text: str, /) -> int:
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")


class UnflushableOutput(StringIO):
    """An output connection that accepts writes but cannot deliver them."""

    @override
    def flush(self) -> None:
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")


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
            "plan": execution.wire_content(planned.record),
            "batch_index": 0,
        }
        rejected = read_operations._brief_sources({"request": emitted}, execution.CancellationToken()).record
        assert isinstance(rejected, contracts.BriefSourcesRejected)
        self.assertEqual("BRIEF_SOURCE_CHANGED", rejected.code)
        self.assertEqual("unchanged", rejected.effect)
        self.assertIn("pinboard_brief_sources", rejected.message)
        replanned = read_operations._brief_sources({"request": request}, execution.CancellationToken())
        recovered = read_operations._brief_sources(
            {"request": emitted | {"plan": execution.wire_content(replanned.record)}}, execution.CancellationToken()
        ).record
        assert isinstance(recovered, contracts.BriefSourceBatchResult)
        self.assertIn("changed source", recovered.text)
        self.assertNotEqual(planned.record, replanned.record)
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
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "artifact_ref_id": int(context.brief_artifact_ref_id),
                "selector": reference.selector,
                "sha256": reference.content_sha256,
                "size_bytes": reference.size_bytes,
            },
            execution.CancellationToken(),
        )
        assert isinstance(reply.record, contracts.ArtifactBytesInvalid)
        self.assertEqual("ARTIFACT_BYTES_INVALID", reply.record.code)
        self.assertEqual("do-not-retry", reply.record.retry)
        self.assertIn(str(resource), reply.record.message)
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
        stderr = StringIO()
        with redirect_stdout(BrokenOutput()), redirect_stderr(stderr):
            status = entrypoint.main(
                (
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
            )
        error = stderr.getvalue()
        self.assertEqual(2, status)
        self.assertTrue(error.startswith("CLI_IO_ERROR: "), error)
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

    def test_unclassified_operation_os_error_names_its_path_and_truthful_effect(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        failed = fixture.work / "state.sqlite3"
        denied = PermissionError(errno.EACCES, "Permission denied", str(failed))
        close = ("close", "work-c", "--outcome", "done", "--reason", "Human requested this close.")
        before = fixture.store.validated_snapshot()
        for arguments, status_value, retry, effect, next_actions in (
            (("status", "--json"), "rejected", "retry-same-input", "unchanged", ()),
            (
                (*close, "--task-id", "human", "--host-id", "local", "--json"),
                "effect-unknown",
                "do-not-retry",
                "unknown",
                (cli_output.RecoveryCommandView("pinboard diagnose --json"),),
            ),
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(work_state_commands, "resolve_durable_layout", side_effect=denied),
            ):
                status, output, error = self.run_cli(*fixture.common, *arguments)
                self.assertEqual((12, ""), (status, error))
                rejection = msgspec.json.decode(output, type=cli_output.RejectedOperationView)
                self.assertEqual("CLI_FILESYSTEM_ERROR", rejection.code)
                self.assertEqual(status_value, rejection.status)
                self.assertEqual(effect == "unknown", rejection.state_changed)
                self.assertEqual(retry, rejection.retry)
                self.assertEqual(next_actions, rejection.next_actions)
                observed = {value.field: value.value for value in rejection.observed}
                self.assertEqual(
                    (str(failed), "EACCES", effect), (observed["path"], observed["errno"], observed["effect"])
                )
                self.assertIn(str(failed), rejection.message)
                self.assertIn(
                    "safe to repeat" if effect == "unchanged" else "inspect current state", str(observed["next_step"])
                )
        for arguments, effect_text in (
            (("status",), "it changed nothing"),
            (("views", "rebuild"), "its effects are unknown"),
            ((*close, "--task-id", "human", "--host-id", "local"), "its effects are unknown"),
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(work_state_commands, "resolve_durable_layout", side_effect=denied),
            ):
                status, output, error = self.run_cli(*fixture.common, *arguments)
                self.assertEqual((12, ""), (status, output))
                self.assertTrue(error.startswith("CLI_FILESYSTEM_ERROR: "), error)
                self.assertIn(f"EACCES (Permission denied) on '{failed}'", error)
                self.assertIn(effect_text, error)
        self.assertEqual(before, fixture.store.validated_snapshot())
        catalog_status, catalog, _ = self.run_cli("code-catalog", "--code", "CLI_FILESYSTEM_ERROR", "--json")
        self.assertEqual(0, catalog_status)
        self.assertIn("effect is unknown until inspected", str(json.loads(catalog)["meaning"]))

    def test_a_result_stream_that_fails_to_flush_reports_the_output_failure(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        stderr = StringIO()
        with redirect_stdout(UnflushableOutput()), redirect_stderr(stderr):
            status = entrypoint.main((*fixture.common, "status", "--json"))
        self.assertEqual(2, status)
        self.assertTrue(stderr.getvalue().startswith("CLI_IO_ERROR: "), stderr.getvalue())
        self.assertNotIn("CLI_FILESYSTEM_ERROR", stderr.getvalue())

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
                patch("pinboard.cli.migration.schema_migration.preview_schema", return_value=failure),
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
        output = StringIO()
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
            patch.dict(os.environ, {invocation_capture.LAUNCHER_PATH_VARIABLE: "/plugin/scripts/pinboard"}),
            patch.object(
                entrypoint, "_run_cli", side_effect=AssertionError("The target ran after trace setup failed.")
            ),
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            status = entrypoint.main((*fixture.common, "status", "--json"))
        self.assertEqual(64, status)
        self.assertEqual("", output.getvalue())
        self.assertIn(str(settings), error.getvalue())
        self.assertIn("settings_parent_creation=none", error.getvalue())
        self.assertIn("settings_file_creation=confirmed", error.getvalue())
        self.assertIn("settings_mode_write=none", error.getvalue())
        self.assertIn("the target did not run", error.getvalue())
        self.assertEqual(b"", settings.read_bytes())
