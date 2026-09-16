import asyncio
import contextlib
import hashlib
import json
import sqlite3
import sys
from unittest.mock import patch

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from pinboard.adapters.sqlite import state as sqlite_state
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import actions, query_models
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp.contracts import JsonValue
from tests.checkpoint_support import CheckpointPackageSupport


class CompletionDiscoveryTest(CheckpointPackageSupport):
    def test_active_checkpointed_completion_returns_candidate_recovery_unchanged(self) -> None:
        fixture, _, _ = self.review_job_fixture()
        self.return_for_correction(fixture, "Protect the final candidate again.", "completion")
        before = fixture.store.validated_snapshot()
        context = fixture.store.read_completion_context(AttemptId("work-a-1"))
        assert context is not None
        self.assertEqual(
            query_models.CompletionCandidateRequired(AttemptId("work-a-1")),
            actions.completion_candidate_recovery(context),
        )

        result, stdout, stderr = self.run_cli(
            *fixture.common, "actions", "--role", "project", "--action-id", "complete:work-a-1", "--json"
        )

        self.assertEqual(11, result, stderr)
        rejected = self.json_object(json.loads(stdout))
        self.assertEqual("rejected", rejected["status"])
        self.assertFalse(rejected["state_changed"])
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_direct_and_covered_discovery_select_exact_input_without_mutation(self) -> None:
        for covered in (False, True):
            with self.subTest(covered=covered):
                fixture = self.review_job_fixture()[0] if covered else self.checkpoint_fixture()
                before = fixture.store.validated_snapshot()
                action = self.project_action(fixture.common, "complete:work-a-1")
                contract = self.json_object(action["input_contract"])
                schema = self.json_object(contract["payload_schema"])
                self.assertNotIn("oneOf", schema)
                definitions = self.json_object(schema["$defs"])
                expected = "CoveredCompleteInputPayload" if covered else "EvidenceInputPayload"
                self.assertIn(expected, definitions)
                self.assertNotIn("EvidenceInputPayload" if covered else "CoveredCompleteInputPayload", definitions)
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

    def test_broad_discovery_is_advisory_without_checkpoint_enumeration(self) -> None:

        fixture, _, _ = self.review_job_fixture()
        with patch.object(
            SQLiteWorkStore, "read_completion_context", side_effect=AssertionError("broad checkpoint enumeration")
        ):
            result = self.run_json_cli(*fixture.common, "actions", "--role", "project")
        completion = next(
            self.json_object(row)
            for row in self.json_array(result["actions"])
            if self.json_object(row)["action_id"] == "complete:work-a-1"
        )
        self.assertEqual("advisory", completion["effect"])
        self.assertNotIn("authorization", completion)
        self.assertNotIn("subject_revision", completion)
        self.assertEqual(
            ["actions", "--role", "project", "--action-id", "complete:work-a-1", "--json"],
            completion["inspection_arguments"],
        )

    def test_discovery_and_transition_share_executable_recovery(self) -> None:  # noqa: PLR0915 - one recovery and terminal client journey

        fixture, _, _ = self.review_job_fixture()
        self.return_for_correction(fixture, "Protect the final candidate again.", "recovery")
        current = self.project_action(fixture.common, "continue:work-a-1")
        current["action_id"] = "complete:work-a-1"
        payload = fixture.project / "completion.json"
        payload.write_text('{"evidence":"cannot bypass checkpoints"}', encoding="utf-8")
        result, stdout, _ = self.run_cli(*self.project_transition_arguments(fixture, current, payload), "--json")
        self.assertEqual(11, result)
        rejected = self.json_object(json.loads(stdout))
        discovered = self.run_cli(
            *fixture.common, "actions", "--role", "project", "--action-id", "complete:work-a-1", "--json"
        )
        self.assertEqual(rejected["observed"], self.json_object(json.loads(discovered[1]))["observed"])
        observations = {
            str(self.json_object(row)["field"]): str(self.json_object(row)["value"])
            for row in self.json_array(rejected["observed"])
        }
        candidate = fixture.candidate_revision
        before_recovery = fixture.store.validated_snapshot()

        async def recover() -> dict[str, JsonValue]:
            parameters = StdioServerParameters(command=sys.executable, args=("-m", "pinboard.mcp"))
            async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                await session.initialize()

                focused = await session.call_tool(
                    observations["completion_reinspection_tool"],
                    {
                        "request": {
                            "project_root": observations["completion_reinspection_project_root"],
                            "work_root": observations["completion_reinspection_work_root"],
                            "role": observations["completion_reinspection_role"],
                            "action_id": {
                                "kind": observations["completion_reinspection_action_kind"],
                                "subject": observations["completion_reinspection_subject"],
                            },
                        }
                    },
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
        complete_action = self.project_action(fixture.common, "complete:work-a-1")
        payload.write_text('{"unexpected":true}', encoding="utf-8")
        invalid, stdout, _ = self.run_cli(
            *self.project_transition_arguments(fixture, complete_action, payload), "--json"
        )
        self.assertEqual(11, invalid)
        self.assertIn(
            {"field": "completion_reinspection_tool", "value": observations["completion_reinspection_tool"]},
            self.json_array(self.json_object(json.loads(stdout))["observed"]),
        )
        attempt_root = fixture.work / "attempts" / "work-a-1"
        result_bytes = b"Current terminal result\n"
        review_bytes = b"Current independent terminal review\n"
        (attempt_root / "result.md").write_bytes(result_bytes)
        (attempt_root / "review.md").write_bytes(review_bytes)

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
                                "schema": "pinboard-covered-completion/v1",
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
        fixture, history_id, _ = self.review_job_fixture()
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
            patch.object(SQLiteWorkStore, "read_handover_batches", side_effect=AssertionError("handover read")),
            patch.object(sqlite_state, "read_history_receipt", wraps=sqlite_state.read_history_receipt) as reads,
        ):
            action = self.project_action(fixture.common, "complete:work-a-1")
        packages = self.json_array(self.json_object(action["input_contract"])["checkpoint_packages"])
        expected = [history_id, next_id + 100]
        self.assertEqual(expected, [self.json_object(row)["history_id"] for row in packages])
        self.assertEqual(expected, [int(call.args[1]) for call in reads.call_args_list])
        self.assertEqual(before, (fixture.work / "state.sqlite3").read_bytes())
