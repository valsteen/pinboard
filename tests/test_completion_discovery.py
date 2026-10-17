import asyncio
import contextlib
import hashlib
import json
import sqlite3
import sys
from copy import deepcopy
from unittest.mock import patch

import msgspec
from mcp import Client
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp_types import CallToolResult

from pinboard.adapters.files.root import read_working_tree_candidate
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import actions, query_models, stored_state, work_brief_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import AttemptId, WorkItemId
from pinboard.mcp import contract_schemas, contracts
from pinboard.mcp import server as mcp_server
from pinboard.mcp.contracts import JsonValue
from tests.checkpoint_support import CheckpointPackageSupport
from tests.native_support import call_advertised_tool, native_tools
from tests.support import JsonObject


class CompletionDiscoveryTest(CheckpointPackageSupport):
    def assert_bounded_completion_warning(self, result: JsonObject) -> JsonObject:
        self.assertEqual("rejected", result["status"], result)
        self.assertEqual(
            (False, "unchanged", []), (result["state_changed"], result["effect"], result["changed_surfaces"])
        )
        observed = {
            str(self.json_object(row)["field"]): self.json_object(row)["value"]
            for row in self.json_array(result["observed"])
        }
        self.assertEqual({"attempt_id", "completion_inspection_tool", "completion_inspection_input"}, set(observed))
        self.assertEqual(mcp_server.ATTEMPT_INSPECT_TOOL, observed["completion_inspection_tool"])
        inspection = observed["completion_inspection_input"]
        assert isinstance(inspection, str)
        arguments = self.json_object(json.loads(inspection))
        self.assertEqual("work-a-1", arguments["attempt_id"])
        self.assertIsNone(arguments["reconciliation"])
        return call_advertised_tool(mcp_server.ATTEMPT_INSPECT_TOOL, arguments)

    def test_terminal_attempt_inspection_names_missing_candidate_then_independent_review(self) -> None:
        active = self.terminalize_brief(self.checkpoint_fixture())
        self.return_for_correction(active, "Protect the terminal candidate.", "terminal-routing")

        active_result = call_advertised_tool(
            mcp_server.ATTEMPT_INSPECT_TOOL,
            {
                "project_root": str(active.project),
                "work_root": str(active.work),
                "attempt_id": "work-a-1",
                "reconciliation": None,
            },
        )
        active_operation = self.json_object(self.json_object(active_result["continuation"])["next_operation"])
        self.assertEqual("action", active_operation["kind"])
        self.assertEqual({"target": "attempt", "action_kind": "complete"}, active_operation["action"])
        self.assertNotIn("submission", str(active_operation["condition"]))

        review = self.terminalize_brief(self.checkpoint_fixture())
        review_result = call_advertised_tool(
            mcp_server.ATTEMPT_INSPECT_TOOL,
            {
                "project_root": str(review.project),
                "work_root": str(review.work),
                "attempt_id": "work-a-1",
                "reconciliation": None,
            },
        )
        review_operation = self.json_object(self.json_object(review_result["continuation"])["next_operation"])
        self.assertEqual("review-subagent", review_operation["kind"])
        self.assertEqual(review.candidate_revision, review_operation["candidate_revision"])

    def test_active_terminal_completion_warns_without_relaunch_with_or_without_history(self) -> None:
        fixtures = (
            ("zero-history", self.terminalize_brief(self.checkpoint_fixture())),
            ("checkpointed", self.review_job_fixture(terminal=True)[0]),
        )
        for label, fixture in fixtures:
            with self.subTest(label=label):
                protected = fixture.store.read_attempt_context(AttemptId("work-a-1"))
                assert isinstance(protected, query_models.NonterminalAttemptContextFacts)
                continued = self.transition_result(
                    fixture,
                    self.project_action(fixture, "accept-review-and-continue:work-a-1"),
                    {"candidate": protected.candidate_revision, "evidence": "Accepted implementation still continues."},
                )
                self.assertEqual("committed", continued["status"], continued)
                before = fixture.store.validated_snapshot()
                context = fixture.store.read_completion_context(AttemptId("work-a-1"))
                assert context is not None
                self.assertEqual(
                    query_models.CompletionCandidateRequired(AttemptId("work-a-1")),
                    actions.completion_candidate_recovery(context),
                )

                rejected = self.actions_result(
                    fixture,
                    {
                        "role": "project",
                        "action_id": {"kind": "complete", "subject": "work-a-1"},
                    },
                )
                self.assertEqual("rejected", rejected["status"])
                self.assert_bounded_completion_warning(rejected)
                self.assertEqual(before, fixture.store.validated_snapshot())

    def test_original_briefs_reject_native_status_inspection_and_submission_without_recovery(self) -> None:
        for schema in ("pinboard-work-brief/v2", "pinboard-work-brief/v3"):
            with self.subTest(schema=schema):
                fixture = self.checkpoint_fixture()
                self.return_for_correction(fixture, "Check original brief rejection.", "v2-boundary")
                context = fixture.store.read_attempt_context(AttemptId("work-a-1"))
                assert isinstance(context, query_models.NonterminalAttemptContextFacts)
                reference = fixture.store.read_artifact_reference_by_id(context.brief_artifact_ref_id)
                assert reference is not None
                roots: JsonObject = {"project_root": str(fixture.project), "work_root": str(fixture.work)}
                assert isinstance(roots, dict)
                status_request: JsonObject = {
                    "request": {**roots, "operation": "status", "brief_artifact_ref_id": int(reference.artifact_ref_id)}
                }
                status = call_advertised_tool(mcp_server.BRIEF_REVIEW_TOOL, status_request)
                payload = msgspec.to_builtins(fixture.brief)
                assert isinstance(payload, dict)
                payload["schema"] = schema
                checkpoint = payload["checkpoint"]
                assert isinstance(checkpoint, dict)
                disposition = checkpoint.pop("disposition")
                assert isinstance(disposition, dict)
                payload["remaining_work"] = disposition["remaining_work"]
                if schema == "pinboard-work-brief/v2":
                    del payload["checkout_selection"]
                    del payload["obligation_correspondence"]
                status["brief"] = payload
                with self.assertRaises(msgspec.ValidationError):
                    contract_schemas.validate_result(mcp_server.BRIEF_REVIEW_TOOL, status)
                self.replace_artifact_bytes(fixture, reference, msgspec.json.encode(payload, order="sorted") + b"\n")
                lease = self.native_attempt_acquire(fixture, "original-v2-worker")
                selected = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
                before = fixture.store.validated_snapshot()
                artifacts = {
                    path: path.read_bytes() for path in (fixture.work / "artifacts").rglob("*") if path.is_file()
                }
                requests: tuple[tuple[str, JsonObject], ...] = (
                    (mcp_server.BRIEF_REVIEW_TOOL, status_request),
                    (mcp_server.ATTEMPT_INSPECT_TOOL, {**roots, "attempt_id": "work-a-1", "reconciliation": None}),
                )
                for tool, arguments in requests:
                    with self.subTest(tool=tool):
                        rejected = call_advertised_tool(tool, arguments)
                        self.assertEqual("rejected", rejected["status"], rejected)
                        self.assertFalse(rejected["state_changed"])
                completion = self.actions_result(
                    fixture, {"role": "project", "action_id": {"kind": "complete", "subject": "work-a-1"}}
                )
                self.assertEqual("rejected", completion["status"], completion)
                self.assertEqual("ACTION_NOT_AVAILABLE", completion["code"])
                self.assertFalse(completion["state_changed"])
                candidate = read_working_tree_candidate(fixture.project).identity
                rejected = self.transition_result(fixture, selected, {"candidate": candidate})
                self.assertEqual("rejected", rejected["status"], rejected)
                observations = {self.json_object(row)["field"] for row in self.json_array(rejected["observed"])}
                self.assertNotIn("brief_binding_action_input", observations)
                self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())
                self.assertEqual(
                    artifacts,
                    {path: path.read_bytes() for path in (fixture.work / "artifacts").rglob("*") if path.is_file()},
                )

    def test_direct_and_covered_discovery_select_exact_input_without_mutation(self) -> None:
        valid_results: list[JsonObject] = []
        invalid_results: list[tuple[str, JsonObject]] = []
        for covered in (False, True):
            with self.subTest(covered=covered):
                fixture = (
                    self.review_job_fixture(terminal=True)[0]
                    if covered
                    else self.terminalize_brief(self.checkpoint_fixture())
                )
                before = fixture.store.validated_snapshot()
                result = fixture.call_advertised_tool(
                    mcp_server.ACTIONS_TOOL,
                    {
                        "request": {
                            "project_root": str(fixture.project),
                            "work_root": str(fixture.work),
                            "role": "project",
                            "action_id": {"kind": "complete", "subject": "work-a-1"},
                        }
                    },
                )
                original = deepcopy(result)
                valid_results.append(result)
                msgspec.convert(result, type=contracts.CompletionActionsSuccess, strict=True)
                self.assertIs(result, contract_schemas.validate_result(mcp_server.ACTIONS_TOOL, result))
                self.assertEqual(original, result)
                action = self.json_object(self.json_array(result["actions"])[0])
                contract = self.json_object(action["input_contract"])
                schema = self.json_object(contract["payload_schema"])
                self.assertNotIn("oneOf", schema)
                definitions = self.json_object(schema["$defs"])
                self.assertIn("ReviewedCompleteInputPayload", definitions)
                required = self.json_array(self.json_object(definitions["ReviewedCompleteInputPayload"])["required"])
                self.assertIn("human_decision", required)
                self.assertNotIn("EvidenceInputPayload", definitions)
                self.assertNotIn("CoveredCompleteInputPayload", definitions)
                context = fixture.store.read_attempt_context(AttemptId("work-a-1"))
                assert isinstance(context, query_models.NonterminalAttemptContextFacts)
                self.assertEqual(context.candidate_revision, contract["candidate"])
                packages = self.json_array(contract["checkpoint_packages"])
                receipts = [
                    value for value in before.transition_receipts if value.outcome_schema == "checkpoint-acceptance/v2"
                ]
                self.assertEqual(
                    [int(value.history_id) for value in receipts],
                    [self.json_object(value)["history_id"] for value in packages],
                )
                for value in packages:
                    row = self.json_object(value)
                    reference = next(
                        value
                        for value in before.artifact_references
                        if int(value.artifact_ref_id) == row["artifact_ref_id"]
                    )
                    self.assertEqual(reference.content_sha256, row["package_sha256"])
                    self.assertEqual(reference.selector, row["selector"])
                invalid_contracts: tuple[tuple[str, JsonObject], ...] = (
                    ("absent", {key: value for key, value in contract.items() if key != "payload_schema"}),
                    ("null", {**contract, "payload_schema": None}),
                    ("wrong-schema", {**contract, "payload_schema": {"type": "object"}}),
                    ("unknown-field", {**contract, "unknown": True}),
                )
                for label, invalid_contract in invalid_contracts:
                    with self.subTest(covered=covered, rejection=label):
                        invalid: JsonObject = {**result, "actions": [{**action, "input_contract": invalid_contract}]}
                        invalid_results.append((f"covered={covered}:{label}", invalid))
                        unchanged = deepcopy(invalid)
                        with self.assertRaises((msgspec.ValidationError, ValueError)):
                            msgspec.convert(invalid, type=contracts.CompletionActionsSuccess, strict=True)
                        with self.assertRaises((msgspec.ValidationError, ValueError)):
                            contract_schemas.validate_result(mcp_server.ACTIONS_TOOL, invalid)
                        self.assertEqual(unchanged, invalid)
                self.assertEqual(before, fixture.store.validated_snapshot())

        async def advertised_schema_scenario() -> None:
            async with Client(native.server) as client:
                for valid in valid_results:
                    await client.session.validate_tool_result(
                        mcp_server.ACTIONS_TOOL, CallToolResult(content=[], structured_content=valid)
                    )
                for label, invalid in invalid_results:
                    with self.subTest(advertised_rejection=label), self.assertRaises(RuntimeError):
                        await client.session.validate_tool_result(
                            mcp_server.ACTIONS_TOOL, CallToolResult(content=[], structured_content=invalid)
                        )

        with native_tools() as native:
            asyncio.run(advertised_schema_scenario())

    def test_broad_native_discovery_omits_checkpoint_package_enumeration(self) -> None:

        fixture, _, _ = self.review_job_fixture(terminal=True)
        with (
            patch.object(
                SQLiteWorkStore, "read_completion_context", side_effect=AssertionError("broad checkpoint enumeration")
            ),
            patch(
                "pinboard.adapters.files.artifacts.ArtifactRepository.read",
                side_effect=AssertionError("broad brief read"),
            ),
        ):
            result = self.actions_result(fixture, {"role": "project"})
        self.assertFalse(
            any(
                self.json_object(row)["action_id"] == {"kind": "complete", "subject": "work-a-1"}
                for row in self.json_array(result["actions"])
            )
        )
        self.assertFalse(result["state_changed"])
        self.assertEqual("unchanged", result["effect"])

    def test_continuing_completion_reports_exact_remaining_work_without_relaunch(self) -> None:
        for active in (False, True):
            with self.subTest(active=active):
                fixture = self.checkpoint_fixture()
                if active:
                    self.return_for_correction(fixture, "Continue the accepted work.", "nonterminal-warning")
                before = fixture.store.validated_snapshot()
                result = self.actions_result(
                    fixture, {"role": "project", "action_id": {"kind": "complete", "subject": "work-a-1"}}
                )
                self.assert_bounded_completion_warning(result)
                disposition = fixture.brief.checkpoint.disposition
                assert isinstance(disposition, work_brief_models.ContinueCheckpointDisposition)
                for pending in disposition.remaining_work:
                    self.assertIn(pending, str(result["message"]))
                self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

    def test_unresolved_replacement_completion_reports_the_pending_decision(self) -> None:
        fixture = self.terminalize_brief(self.checkpoint_fixture())
        action = self.project_action(fixture, "record-replacement:work-a")
        recorded = self.transition_result(
            fixture,
            action,
            {
                "schema": "pinboard-planned-replacement/v1",
                "expected_relation_revision": 0,
                "replacement_item": "work-b",
                "replacement_cost": "One retained owner.",
                "status": "current",
                "recorded_by": "review-owner",
            },
        )
        self.assertEqual("committed", recorded["status"], recorded)

        before = fixture.store.validated_snapshot()
        result = self.actions_result(
            fixture,
            {"role": "project", "action_id": {"kind": "complete", "subject": "work-a-1"}},
        )
        self.assertEqual("rejected", result["status"])
        self.assert_bounded_completion_warning(result)
        self.assertIn("unresolved replacement", str(result["message"]))
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_stale_definition_completion_reports_changed_acceptance_without_relaunch(self) -> None:
        fixture = self.terminalize_brief(self.checkpoint_fixture())
        snapshot = fixture.store.validated_snapshot()
        definitions = tuple(
            value for value in snapshot.lifecycle.definition_revisions if value.item_id == WorkItemId("work-a")
        )
        current = definitions[-1]
        definition_bytes = stored_state.stored_definition_bytes(current.definition)
        self.assertNotIsInstance(definition_bytes, DecisionFailure)
        assert isinstance(definition_bytes, bytes)
        definition = self.json_object(json.loads(definition_bytes))
        definition["objective"] = "Exercise focused stale-definition completion recovery."
        action = self.project_action(fixture, "revise-item:work-a")
        revised = self.transition_result(
            fixture,
            action,
            {
                "schema": "pinboard-item-revision/v1",
                "expected_revision": current.revision,
                "expected_digest": current.digest,
                "source_task": "review-owner",
                "reason": "Exercise focused stale-definition completion recovery.",
                "definition": definition,
            },
        )
        self.assertEqual("committed", revised["status"], revised)

        before = fixture.store.validated_snapshot()
        result = self.actions_result(
            fixture,
            {"role": "project", "action_id": {"kind": "complete", "subject": "work-a-1"}},
        )
        self.assertEqual("rejected", result["status"])
        self.assert_bounded_completion_warning(result)
        self.assertIn("stale accepted attempt binding", str(result["message"]))
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_native_warning_inspects_missing_evidence_and_explicit_delivery_can_complete(self) -> None:  # noqa: PLR0915 - one explicit delivery and terminal client journey

        fixture, _, _ = self.review_job_fixture(terminal=True)
        self.return_for_correction(fixture, "Protect the final candidate again.", "recovery")
        current = self.project_action(fixture, "continue:work-a-1")
        current["action_id"] = {"kind": "complete", "subject": "work-a-1"}
        current["authorization"] = "project"
        rejected = self.transition_result(fixture, current, {"evidence": "cannot bypass checkpoints"})
        self.assertEqual("rejected", rejected["status"])
        rejected_observations = {
            str(self.json_object(row)["field"]): str(self.json_object(row)["value"])
            for row in self.json_array(rejected["observed"])
        }
        self.assertEqual("pinboard_actions", rejected_observations["completion_reinspection_tool"])
        self.assertIn('"kind":"complete"', rejected_observations["completion_reinspection_input"])
        discovered = self.actions_result(
            fixture,
            {
                "role": "project",
                "action_id": {"kind": "complete", "subject": "work-a-1"},
            },
        )
        self.assertFalse(rejected["state_changed"])
        self.assertEqual("unchanged", rejected["effect"])
        inspection = self.assert_bounded_completion_warning(discovered)
        self.assertEqual("active", self.json_object(inspection["continuation"])["state"])
        lease = self.native_attempt_acquire(fixture, "explicit-delivery-worker")
        selected = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        candidate = fixture.candidate_revision
        submitted = self.transition_result(fixture, selected, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)
        complete_action = self.project_action(fixture, "complete:work-a-1")
        invalid = self.transition_result(fixture, complete_action, {"unexpected": True})
        self.assertEqual("rejected", invalid["status"])
        self.assertEqual("TRANSITION_INPUT_INVALID", invalid["code"])
        self.assertFalse(invalid["state_changed"])
        self.assertEqual("unchanged", invalid["effect"])
        attempt_root = fixture.work / "attempts" / "work-a-1"
        result_bytes = b"Current terminal result\n"
        review_bytes = b"Current independent terminal review\n"
        (attempt_root / "result.md").write_bytes(result_bytes)
        (attempt_root / "review.md").write_bytes(review_bytes)
        self.record_commissioned_review(fixture, candidate, "terminal-reviewer")

        async def complete() -> None:
            parameters = StdioServerParameters(command=sys.executable, args=("-m", "pinboard.mcp"))
            common: dict[str, JsonValue] = {"project_root": str(fixture.project), "work_root": str(fixture.work)}
            async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                await session.initialize()
                discovery = await session.call_tool(
                    "pinboard_actions",
                    {
                        "request": common
                        | {
                            "role": "project",
                            "action_id": {"kind": "complete", "subject": "work-a-1"},
                        }
                    },
                )
                assert isinstance(discovery.structured_content, dict)
                actions = discovery.structured_content["actions"]
                assert isinstance(actions, list) and len(actions) == 1 and isinstance(actions[0], dict)
                selected = actions[0]
                contract = selected["input_contract"]
                assert isinstance(contract, dict)
                packages = contract["checkpoint_packages"]
                assert isinstance(packages, list)
                package_evidence: list[JsonValue] = []
                for package in packages:
                    assert isinstance(package, dict)
                    package_evidence.append(
                        {
                            "history_id": package["history_id"],
                            "package_sha256": package["package_sha256"],
                            "disposition": "revalidated",
                            "evidence": "Independent reviewer rechecked the current relationship.",
                        }
                    )
                self.assertEqual(
                    [int(fixture.package_reference.artifact_ref_id)],
                    [package["artifact_ref_id"] for package in packages if isinstance(package, dict)],
                )
                result = await session.call_tool(
                    "pinboard_transition",
                    {
                        "request": common
                        | {
                            "role": "project",
                            "actor_task_id": "coordinator",
                            "actor_host_id": "local",
                            "receipt": {
                                "action_id": selected["action_id"],
                                "subject_revision": selected["subject_revision"],
                            },
                            "payload": {
                                "schema": "pinboard-reviewed-completion/v3",
                                "human_decision": "Complete this item with its reviewed outcome.",
                                "candidate": contract["candidate"],
                                "evidence": "All returned checkpoint evidence is covered.",
                                "reviewer_task_id": "terminal-reviewer",
                                "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
                                "review_sha256": hashlib.sha256(review_bytes).hexdigest(),
                                "packages": package_evidence,
                            },
                        }
                    },
                )
                self.assertFalse(result.is_error)
                assert isinstance(result.structured_content, dict)
                self.assertIn(result.structured_content["status"], {"committed", "committed-with-warning"})

        asyncio.run(complete())
        terminal = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        item = next(row for row in terminal.lifecycle.work_items if str(row.item_id) == "work-a")
        self.assertEqual("done", item.state.value)
        receipt = next(row for row in terminal.transition_receipts if row.outcome_schema == "completion-acceptance/v2")
        self.assertIsNotNone(receipt.artifact_ref_id)

    def test_focused_completion_enumerates_only_ordered_same_attempt_packages(self) -> None:
        fixture, history_id, _ = self.review_job_fixture(terminal=True)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            next_id = connection.execute("SELECT max(history_id) + 1 FROM transition_history").fetchone()[0]
            next_revision = connection.execute("SELECT max(project_revision) + 1 FROM transition_history").fetchone()[0]
            for index in range(101):
                subject = "work-a-1" if index == 100 else f"unrelated-{index}"
                connection.execute(
                    """
                    INSERT INTO transition_history(
                        history_id, project_revision, action_id, action_kind, subject_id,
                        artifact_ref_id, artifact_kind, authorization_kind, actor_task_id, actor_host_id,
                        input_schema, input_json, outcome_schema, outcome_json, committed_at
                    )
                    SELECT ?, ?, action_id, action_kind, ?, artifact_ref_id, artifact_kind, authorization_kind,
                           actor_task_id, actor_host_id, input_schema, input_json, outcome_schema,
                           outcome_json, committed_at
                    FROM transition_history WHERE history_id = ?
                    """,
                    (next_id + index, next_revision + index, subject, history_id),
                )
        before = (fixture.work / "state.sqlite3").read_bytes()
        with (
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("full ledger read")),
            patch.object(
                SQLiteWorkStore, "read_project_export_batches", side_effect=AssertionError("project_export read")
            ),
        ):
            action = self.project_action(fixture, "complete:work-a-1")
        packages = self.json_array(self.json_object(action["input_contract"])["checkpoint_packages"])
        expected = [history_id, next_id + 100]
        self.assertEqual(expected, [self.json_object(row)["history_id"] for row in packages])
        self.assertEqual(before, (fixture.work / "state.sqlite3").read_bytes())
