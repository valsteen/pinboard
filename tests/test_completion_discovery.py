import asyncio
import contextlib
import hashlib
import json
import sqlite3
import sys
from unittest.mock import patch

import msgspec
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from pinboard.adapters.files.root import read_working_tree_candidate
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import actions, query_models, stored_state
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import AttemptId, WorkItemId
from pinboard.mcp import contract_schemas
from pinboard.mcp import server as mcp_server
from pinboard.mcp.contracts import JsonValue
from tests.checkpoint_support import CheckpointPackageSupport
from tests.native_support import call_advertised_tool
from tests.support import JsonObject


class CompletionDiscoveryTest(CheckpointPackageSupport):
    def test_terminal_attempt_inspection_routes_candidate_protection_then_independent_review(self) -> None:
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

    def test_active_terminal_completion_returns_candidate_recovery_with_or_without_history(self) -> None:
        fixtures = (
            ("zero-history", self.terminalize_brief(self.checkpoint_fixture())),
            ("checkpointed", self.review_job_fixture(terminal=True)[0]),
        )
        for label, fixture in fixtures:
            with self.subTest(label=label):
                self.return_for_correction(fixture, "Protect the final candidate again.", f"completion-{label}")
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
                self.assertFalse(rejected["state_changed"])
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
        for covered in (False, True):
            with self.subTest(covered=covered):
                fixture = (
                    self.review_job_fixture(terminal=True)[0]
                    if covered
                    else self.terminalize_brief(self.checkpoint_fixture())
                )
                before = fixture.store.validated_snapshot()
                action = self.project_action(fixture, "complete:work-a-1")
                contract = self.json_object(action["input_contract"])
                schema = self.json_object(contract["payload_schema"])
                self.assertNotIn("oneOf", schema)
                definitions = self.json_object(schema["$defs"])
                self.assertIn("ReviewedCompleteInputPayload", definitions)
                self.assertNotIn("EvidenceInputPayload", definitions)
                self.assertNotIn("CoveredCompleteInputPayload", definitions)
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
                self.assertEqual(before, fixture.store.validated_snapshot())

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

    def test_nonterminal_completion_returns_checkpoint_or_dispatch_recovery(self) -> None:
        fixture = self.checkpoint_fixture()
        reviewed = self.actions_result(
            fixture,
            {"role": "project", "action_id": {"kind": "complete", "subject": "work-a-1"}},
        )
        reviewed_observed = {
            str(self.json_object(row)["field"]): str(self.json_object(row)["value"])
            for row in self.json_array(reviewed["observed"])
        }
        self.assertIn('"kind":"accept-checkpoint"', reviewed_observed["recovery_action_input"])

        accepted, _, _ = self.review_job_fixture()
        self.return_for_correction(accepted, "Continue the accepted work.", "nonterminal-recovery")
        active = self.actions_result(
            accepted,
            {"role": "project", "action_id": {"kind": "complete", "subject": "work-a-1"}},
        )
        active_observed = {
            str(self.json_object(row)["field"]): str(self.json_object(row)["value"])
            for row in self.json_array(active["observed"])
        }
        self.assertIn('"kind":"dispatch"', active_observed["recovery_action_input"])
        self.assertIn('"kind":"revise-item"', active_observed["exceptional_revision_action_input"])
        self.assertIn('"kind":"rebind-attempt"', active_observed["exceptional_rebind_action_input"])

    def test_unresolved_replacement_completion_returns_both_exact_disposition_routes(self) -> None:
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

        result = self.actions_result(
            fixture,
            {"role": "project", "action_id": {"kind": "complete", "subject": "work-a-1"}},
        )
        self.assertEqual("rejected", result["status"])
        observations = {
            str(self.json_object(row)["field"]): str(self.json_object(row)["value"])
            for row in self.json_array(result["observed"])
        }
        self.assertIn('"kind":"record-replacement"', observations["recovery_action_input"])
        self.assertIn('"kind":"retain-temporarily"', observations["alternative_recovery_action_input"])
        self.assertIn('"kind":"revise-item"', observations["exceptional_revision_action_input"])

    def test_stale_definition_review_completion_returns_correction_before_rebind(self) -> None:
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

        result = self.actions_result(
            fixture,
            {"role": "project", "action_id": {"kind": "complete", "subject": "work-a-1"}},
        )
        self.assertEqual("rejected", result["status"])
        observations = {
            str(self.json_object(row)["field"]): str(self.json_object(row)["value"])
            for row in self.json_array(result["observed"])
        }
        self.assertIn('"kind":"return-for-correction"', observations["recovery_action_input"])
        self.assertIn('"kind":"revise-item"', observations["exceptional_revision_action_input"])
        self.assertIn("First execute return-for-correction", observations["exceptional_recovery_human_decision"])
        self.assertIn("submit a new candidate", observations["exceptional_recovery_after_revision"])

    def test_native_discovery_executes_recovery_and_terminal_transition(self) -> None:  # noqa: PLR0915 - one recovery and terminal client journey

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
        observations = {
            str(self.json_object(row)["field"]): str(self.json_object(row)["value"])
            for row in self.json_array(discovered["observed"])
        }
        candidate = fixture.candidate_revision
        before_recovery = fixture.store.validated_snapshot()

        async def recover() -> dict[str, JsonValue]:
            parameters = StdioServerParameters(command=sys.executable, args=("-m", "pinboard.mcp"))
            async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                await session.initialize()

                focused = await session.call_tool(
                    observations["completion_reinspection_tool"],
                    self.json_object(json.loads(observations["completion_reinspection_input"])),
                )
                self.assertFalse(focused.is_error)
                assert isinstance(focused.structured_content, dict)
                self.assertEqual("rejected", focused.structured_content["status"])
                self.assertFalse(focused.structured_content["state_changed"])
                self.assertEqual(before_recovery, fixture.store.validated_snapshot())
                recipe = {
                    str(self.json_object(row)["field"]): str(self.json_object(row)["value"])
                    for row in self.json_array(focused.structured_content["observed"])
                }

                async def call(prefix: str, replacements: dict[str, str]) -> dict[str, JsonValue]:
                    encoded = recipe[prefix + "_input"]
                    for old, new in replacements.items():
                        encoded = encoded.replace(old, new)
                    arguments = self.json_object(json.loads(encoded))
                    result = await session.call_tool(
                        recipe[prefix + "_tool"],
                        arguments,
                    )
                    self.assertFalse(result.is_error)
                    content = result.structured_content
                    assert isinstance(content, dict)
                    self.assertNotEqual("rejected", content["status"], content)
                    return content

                await call("authority_status", {})
                lease = await call(
                    "authority_acquisition", {"<worker-task-id>": "recovery-worker", "<host-id>": "local"}
                )
                replacements = {
                    "<current-lease-id>": str(lease["lease_id"]),
                    '"<current-generation>"': str(lease["generation"]),
                }
                selected = await call("candidate_submission_action", replacements)
                actions = selected["actions"]
                assert isinstance(actions, list) and isinstance(actions[0], dict)
                replacements["<current-subject-revision>"] = str(actions[0]["subject_revision"])
                replacements["<exact-candidate-revision>"] = candidate
                await call("candidate_submission", replacements)
                return await call("completion_reinspection", {})

        reinspected = asyncio.run(recover())
        contract = self.json_object(self.json_object(self.json_array(reinspected["actions"])[0])["input_contract"])
        self.assertEqual(candidate, contract["candidate"])
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
                                "schema": "pinboard-reviewed-completion/v2",
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
