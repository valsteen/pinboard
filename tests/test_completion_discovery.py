import contextlib
import json
import shlex
import sqlite3
from unittest.mock import patch

from pinboard.adapters.sqlite import state as sqlite_state
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from tests.checkpoint_support import CheckpointPackageSupport


class CompletionDiscoveryTest(CheckpointPackageSupport):
    def test_active_checkpointed_completion_returns_candidate_recovery_unchanged(self) -> None:
        fixture, _, _ = self.review_job_fixture()
        self.return_for_correction(fixture, "Protect the final candidate again.", "completion")
        before = fixture.store.validated_snapshot()

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

    def test_discovery_and_transition_share_executable_recovery(self) -> None:

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
        self.run_json_cli(*fixture.common, *shlex.split(observations["authority_status_command"])[:-1])
        acquisition = (
            observations["authority_acquisition_command"]
            .replace("<worker-task-id>", "recovery-worker")
            .replace("<host-id>", "local")
        )
        lease = self.run_json_cli(*fixture.common, *shlex.split(acquisition)[:-1])
        replacements = {
            "<returned-lease-id>": str(lease["lease_id"]),
            "<returned-generation>": str(lease["generation"]),
        }
        selection = observations["candidate_submission_action_command"]
        for old, new in replacements.items():
            selection = selection.replace(old, new)
        selected = self.run_json_cli(*fixture.common, *shlex.split(selection)[:-1])
        action = self.json_object(self.json_array(selected["actions"])[0])
        payload.write_text(
            observations["candidate_payload"].replace("<exact-candidate-revision>", "recovered-candidate"),
            encoding="utf-8",
        )
        submission = observations["candidate_submission_command"]
        replacements["<returned-subject-revision>"] = str(action["subject_revision"])
        replacements["<candidate-payload-file>"] = str(payload)
        for old, new in replacements.items():
            submission = submission.replace(old, new)
        self.run_json_cli(*fixture.common, *shlex.split(submission)[:-1])
        reinspected = self.run_json_cli(
            *fixture.common, *shlex.split(observations["completion_reinspection_command"])[:-1]
        )
        contract = self.json_object(self.json_object(self.json_array(reinspected["actions"])[0])["input_contract"])
        self.assertEqual("recovered-candidate", contract["candidate"])
        complete_action = self.json_object(self.json_array(reinspected["actions"])[0])
        payload.write_text('{"unexpected":true}', encoding="utf-8")
        invalid, stdout, _ = self.run_cli(
            *self.project_transition_arguments(fixture, complete_action, payload), "--json"
        )
        self.assertEqual(11, invalid)
        self.assertIn(
            {"field": "completion_reinspection_command", "value": observations["completion_reinspection_command"]},
            self.json_array(self.json_object(json.loads(stdout))["observed"]),
        )

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
