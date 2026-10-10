"""Human-confirmed close and commissioned-review completion through installed MCP, CLI and persistence."""

import asyncio
import contextlib
import hashlib
import io
import json
import sqlite3
from datetime import UTC, datetime
from unittest.mock import patch

import msgspec

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import action_models, dispatch_models, query_models, work_brief_models, work_briefs
from pinboard.application.artifact_publication import AcceptedArtifactPublication, publish_accepted_artifact
from pinboard.application.artifacts import NewArtifact
from pinboard.application.project_export import ProjectExport
from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import AttemptId, WorkItemId
from pinboard.mcp import execution as mcp_execution
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import AcceptedPackageFixture, CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import JsonObject, JsonValue

HUMAN_WORDS = "Yes, drop work-c: the parser rewrite already covers it."


class HumanConfirmedCloseTest(CheckpointPackageSupport):
    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def close_arguments(self, fixture: CheckpointFixture, item_id: str, payload: JsonObject) -> JsonObject:
        action = self.project_action(fixture, f"close:{item_id}")
        return {
            **self.roots(fixture),
            "receipt": {"action_id": action["action_id"], "subject_revision": action["subject_revision"]},
            "payload": payload,
            "actor_task_id": "coordinator",
            "actor_host_id": "local",
        }

    def close_receipts(self, fixture: CheckpointFixture) -> tuple[tuple[str, bytes], ...]:
        state = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        return tuple(
            (value.input_schema, bytes(value.input_payload))
            for value in state.transition_receipts
            if value.action_kind == decision_models.ActionKind.CLOSE
        )

    def observed(self, result: JsonObject) -> dict[str, JsonValue]:
        return {
            str(self.json_object(row)["field"]): self.json_object(row)["value"]
            for row in self.json_array(result["observed"])
        }

    def test_close_without_the_human_decision_is_refused_unchanged_and_with_it_persists_the_exact_words(self) -> None:
        fixture = self.checkpoint_fixture()
        before = fixture.store.validated_snapshot()
        refusals: tuple[tuple[str, JsonObject], ...] = (
            ("missing", {"outcome": "done", "reason": "Covered elsewhere."}),
            ("empty", {"outcome": "done", "reason": "Covered elsewhere.", "human_decision": ""}),
            ("multiline", {"outcome": "done", "reason": "Covered elsewhere.", "human_decision": "Yes\nclose"}),
        )
        for label, payload in refusals:
            with self.subTest(human_decision=label):
                refused = call_advertised_tool(mcp_server.CLOSE_TOOL, self.close_arguments(fixture, "work-c", payload))
                self.assertEqual("rejected", refused["status"], refused)
                self.assertEqual("TRANSITION_INPUT_INVALID", refused["code"])
                self.assertEqual({"kind": "close", "subject": "work-c"}, refused["action_id"])
                self.assertEqual(
                    ("unchanged", "correct-input", []),
                    (refused["effect"], refused["retry"], refused["changed_surfaces"]),
                )
                self.assertIs(False, self.observed(refused)["human_decision_valid"])
                self.assertEqual(before, fixture.store.validated_snapshot())

        committed = call_advertised_tool(
            mcp_server.CLOSE_TOOL,
            self.close_arguments(
                fixture, "work-c", {"outcome": "done", "reason": "Covered elsewhere.", "human_decision": HUMAN_WORDS}
            ),
        )
        self.assertEqual("committed", committed["status"], committed)
        self.assertEqual({"kind": "close", "subject": "work-c"}, committed["action_id"])
        self.assertEqual(["ledger"], committed["changed_surfaces"])

        ((schema, stored),) = self.close_receipts(fixture)
        self.assertEqual("pinboard-close-decision/v1", schema)
        decoded = msgspec.json.decode(stored, type=action_models.CloseInputPayload)
        self.assertEqual(
            (work_models.CloseOutcome.DONE, "Covered elsewhere.", HUMAN_WORDS),
            (decoded.outcome, decoded.reason, decoded.human_decision),
        )
        self.assertEqual(msgspec.json.encode(decoded, order="sorted"), stored)
        status = SQLiteWorkStore(fixture.work / "state.sqlite3").read_item_status(WorkItemId("work-c"))
        assert status is not None
        self.assertEqual("done", status.work_item.state.value)
        exit_code, stdout, stderr = self.run_cli(*fixture.common, "export", "--json")
        self.assertEqual(0, exit_code, stderr)
        exported = msgspec.json.decode(stdout, type=ProjectExport, strict=True)
        (exported_close,) = (value for value in exported.transitions if value.action_kind == "close")
        self.assertEqual("pinboard-close-decision/v1", exported_close.input_schema)
        self.assertEqual(stored, msgspec.json.encode(msgspec.json.decode(bytes(exported_close.input)), order="sorted"))

    def test_validate_accepts_new_and_retained_close_inputs_and_rejects_a_malformed_decision(self) -> None:
        fixture = self.checkpoint_fixture()
        for item_id, reason in (("work-c", "Covered elsewhere."), ("zz-proposal-a", "No longer needed.")):
            committed = call_native_tool(
                mcp_server.CLOSE_TOOL,
                self.close_arguments(
                    fixture, item_id, {"outcome": "done", "reason": reason, "human_decision": HUMAN_WORDS}
                ),
            )
            self.assertEqual("committed", committed["status"], committed)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE transition_history SET input_schema = 'decision/v1', input_json = '{}' "
                "WHERE action_id = 'close:zz-proposal-a'"
            )
        self.assertEqual(
            {"pinboard-close-decision/v1", "decision/v1"}, {schema for schema, _ in self.close_receipts(fixture)}
        )
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])
        self.run_json_cli(*fixture.common, "export")
        for label, schema, value in (
            ("missing-decision", "pinboard-close-decision/v1", '{"outcome":"done","reason":"Covered elsewhere."}'),
            (
                "unknown-field",
                "pinboard-close-decision/v1",
                '{"extra":1,"human_decision":"y","outcome":"done","reason":"r"}',
            ),
            ("noncanonical", "pinboard-close-decision/v1", '{"reason":"r","outcome":"done","human_decision":"y"}'),
            ("retained-nonempty", "decision/v1", '{"reason":"r"}'),
            ("unsupported", "pinboard-close-decision/v2", "{}"),
        ):
            with self.subTest(close_input=label):
                with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
                    original = connection.execute(
                        "SELECT input_schema, input_json FROM transition_history WHERE action_id = 'close:work-c'"
                    ).fetchone()
                    connection.execute(
                        "UPDATE transition_history SET input_schema = ?, input_json = ? WHERE action_id = 'close:work-c'",
                        (schema, value),
                    )
                try:
                    result, stdout, _stderr = self.run_cli(*fixture.common, "validate", "--json")
                    self.assertNotEqual(0, result)
                    codes = {
                        self.json_object(row)["code"]
                        for row in self.json_array(self.json_object(json.loads(stdout))["diagnostics"])
                    }
                    self.assertIn("CLOSE_DECISION_INVALID", codes)
                finally:
                    with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
                        connection.execute(
                            "UPDATE transition_history SET input_schema = ?, input_json = ? "
                            "WHERE action_id = 'close:work-c'",
                            original,
                        )

    def test_human_decision_relaxes_no_close_condition_for_a_paused_item_with_an_accepted_attempt(self) -> None:
        fixture = self.checkpoint_fixture()
        returned = self.transition_result(
            fixture, self.project_action(fixture, "return-for-correction:work-a-1"), {"reason": "Continue it."}
        )
        self.assertEqual("committed", returned["status"], returned)
        paused = self.transition_result(fixture, self.project_action(fixture, "pause:work-a-1"), {"reason": "Wait."})
        self.assertEqual("committed", paused["status"], paused)
        actions = self.actions_result(fixture, {"role": "project", "action_id": {"kind": "close", "subject": "work-a"}})
        self.assertEqual("rejected", actions["status"], actions)
        before = fixture.store.validated_snapshot()
        refused = call_advertised_tool(
            mcp_server.CLOSE_TOOL,
            {
                **self.roots(fixture),
                "receipt": {
                    "action_id": {"kind": "close", "subject": "work-a"},
                    "subject_revision": str(before.lifecycle.work_items[0].subject_revision),
                },
                "payload": {"outcome": "dropped", "reason": "Bypass review.", "human_decision": HUMAN_WORDS},
                "actor_task_id": "coordinator",
                "actor_host_id": "local",
            },
        )
        self.assertEqual("rejected", refused["status"], refused)
        self.assertEqual("ACTION_NOT_AVAILABLE", refused["code"])
        self.assertEqual("unchanged", refused["effect"])
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_transition_refuses_a_close_leaf_naming_the_close_tool_and_never_for_an_undecodable_identity(self) -> None:
        fixture = self.checkpoint_fixture()
        action = self.project_action(fixture, "close:work-c")
        before = fixture.store.validated_snapshot()
        refused = call_advertised_tool(
            mcp_server.TRANSITION_TOOL,
            self.native_transition_request(
                fixture, action, {"outcome": "done", "reason": "Covered.", "human_decision": HUMAN_WORDS}
            ),
        )
        self.assertEqual("rejected", refused["status"], refused)
        self.assertEqual("TRANSITION_INPUT_INVALID", refused["code"])
        self.assertEqual({"kind": "close", "subject": "work-c"}, refused["action_id"])
        self.assertEqual(mcp_server.CLOSE_TOOL, self.observed(refused)["close_tool"])
        self.assertEqual(before, fixture.store.validated_snapshot())
        undecodable: tuple[JsonValue, ...] = ({"action_id": {"kind": "close"}}, {"action_id": "close:work-c"}, None)
        for receipt in undecodable:
            request = self.native_transition_request(fixture, action, {"outcome": "done", "reason": "Covered."})
            inner = self.json_object(request["request"])
            inner["receipt"] = receipt
            with self.subTest(receipt=receipt):
                generic = call_advertised_tool(mcp_server.TRANSITION_TOOL, request)
                self.assertEqual("TRANSITION_INPUT_INVALID", generic["code"])
                self.assertNotEqual("close", self.json_object(generic["action_id"])["kind"])
                self.assertNotIn("close_tool", self.observed(generic))
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_discovered_close_contract_requires_the_decision_without_naming_a_tool(self) -> None:
        fixture = self.checkpoint_fixture()
        discovered = call_advertised_tool(
            mcp_server.ACTIONS_TOOL,
            {
                "request": {
                    **self.roots(fixture),
                    "role": "project",
                    "action_id": {"kind": "close", "subject": "work-c"},
                }
            },
        )
        self.assertEqual("pinboard-mcp-actions-result/v1", discovered["schema"])
        (action,) = self.json_array(discovered["actions"])
        contract = self.json_object(self.json_object(action)["input_contract"])
        schema = self.json_object(contract["payload_schema"])
        definition = self.json_object(self.json_object(schema["$defs"])["CloseInputPayload"])
        self.assertIn("human_decision", self.json_array(definition["required"]))
        semantics = self.json_object(self.json_object(action)["semantics"])
        self.assertFalse(any("pinboard_" in str(value) for value in semantics.values()))

    def test_retained_cli_close_records_its_reason_as_the_human_decision(self) -> None:
        fixture = self.checkpoint_fixture()
        closed = self.run_json_cli(
            *fixture.common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "The maintainer closed it by hand.",
            "--task-id",
            "human-owner",
            "--host-id",
            "human-host",
        )
        self.assertEqual("work-c", closed["item_id"])
        ((schema, stored),) = self.close_receipts(fixture)
        self.assertEqual("pinboard-close-decision/v1", schema)
        decoded = msgspec.json.decode(stored, type=action_models.CloseInputPayload)
        self.assertEqual(("The maintainer closed it by hand.",) * 2, (decoded.reason, decoded.human_decision))
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])


class SubmittedCandidateSupport(CheckpointPackageSupport):
    def observed(self, result: JsonObject) -> dict[str, JsonValue]:
        return {
            str(self.json_object(row)["field"]): self.json_object(row)["value"]
            for row in self.json_array(result["observed"])
        }

    def review_fixture(self) -> tuple[AcceptedPackageFixture, str]:
        fixture = self.accepted_package_fixture(local=True, candidate_form="current-head")
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute("UPDATE work_items SET state = 'active' WHERE item_id = 'work-a'")
            connection.execute("UPDATE attempts SET state = 'active' WHERE attempt_id = 'work-a-1'")
            connection.execute("UPDATE work_item_state_counts SET item_count = item_count - 1 WHERE state = 'paused'")
            connection.execute("UPDATE work_item_state_counts SET item_count = item_count + 1 WHERE state = 'active'")
        attempt_root = fixture.work / "attempts" / "work-a-1"
        (attempt_root / "result.md").write_bytes(b"terminal result\n")
        (attempt_root / "review.md").write_bytes(b"terminal independent review\n")
        candidate = self.submit_review(fixture, "commissioned candidate", "commissioned-worker")
        return fixture, candidate


class CommissionedReviewCompletionTest(SubmittedCandidateSupport):
    def completion_payload(self, fixture: AcceptedPackageFixture, candidate: str, reviewer: str) -> JsonObject:
        state = fixture.store.validated_snapshot()
        checkpoint = next(
            value for value in state.transition_receipts if value.outcome_schema == "checkpoint-acceptance/v2"
        )
        attempt_root = fixture.work / "attempts" / "work-a-1"
        return {
            "schema": "pinboard-reviewed-completion/v2",
            "candidate": candidate,
            "evidence": "The commissioned reviewer accepted the exact candidate.",
            "reviewer_task_id": reviewer,
            "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
            "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
            "packages": [
                {
                    "history_id": int(checkpoint.history_id),
                    "package_sha256": fixture.package_reference.content_sha256,
                    "disposition": "revalidated",
                    "evidence": "The final relationship was rechecked.",
                }
            ],
        }

    def assert_review_required(
        self, fixture: AcceptedPackageFixture, payload: JsonObject, recorded_reviewer: str | None
    ) -> JsonObject:
        before = fixture.store.validated_snapshot()
        with patch(
            "pinboard.adapters.lifecycle_artifacts.ArtifactRepository.publish",
            side_effect=AssertionError("completion published evidence before its review gate"),
        ):
            refused = call_advertised_tool(
                mcp_server.TRANSITION_TOOL,
                self.native_transition_request(fixture, self.project_action(fixture, "complete:work-a-1"), payload),
            )
        self.assertEqual("rejected", refused["status"], refused)
        self.assertEqual("CANDIDATE_REVIEW_REQUIRED", refused["code"])
        self.assertEqual(
            ("unchanged", "do-not-retry", []), (refused["effect"], refused["retry"], refused["changed_surfaces"])
        )
        observed = self.observed(refused)
        self.assertEqual("work-a-1", observed["attempt_id"])
        self.assertEqual(recorded_reviewer, observed["recorded_reviewer_task_id"])
        self.assertEqual(mcp_server.REVIEW_JOB_TOOL, observed["review_commission_tool"])
        self.assertEqual(mcp_server.REVIEW_JOB_TOOL, observed["review_record_tool"])
        self.assertEqual(mcp_server.ATTEMPT_INSPECT_TOOL, observed["review_round_tool"])
        self.assertEqual(mcp_server.ACTIONS_TOOL, observed["completion_reinspection_tool"])
        self.assertEqual(before, fixture.store.validated_snapshot())
        return refused

    def test_reviewed_completion_requires_a_commissioned_review_by_the_named_reviewer(self) -> None:
        fixture, candidate = self.review_fixture()
        fixture = self.terminalize_brief(fixture)
        payload = self.completion_payload(fixture, candidate, "commissioned-reviewer")
        self.assert_review_required(fixture, payload, None)

        self.record_commissioned_review(fixture, candidate, "commissioned-reviewer")
        payload = self.completion_payload(fixture, candidate, "invented-reviewer")
        refused = self.assert_review_required(fixture, payload, "commissioned-reviewer")
        self.assertEqual(
            [{"field": "reviewer_task_id", "expected": "commissioned-reviewer", "observed": "invented-reviewer"}],
            refused["mismatches"],
        )

        reviewed = self.completion_payload(fixture, candidate, "commissioned-reviewer")
        committed = call_advertised_tool(
            mcp_server.TRANSITION_TOOL,
            self.native_transition_request(fixture, self.project_action(fixture, "complete:work-a-1"), reviewed),
        )
        self.assertEqual("committed", committed["status"], committed)
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])

    def test_reviewed_leaf_commits_after_review_job_record_ready_and_matching_reviewer(self) -> None:
        fixture, candidate = self.review_fixture()
        fixture = self.terminalize_brief(fixture)
        self.record_commissioned_review(fixture, candidate, "commissioned-reviewer")
        reviewed = self.completion_payload(fixture, candidate, "commissioned-reviewer")
        committed = call_advertised_tool(
            mcp_server.TRANSITION_TOOL,
            self.native_transition_request(fixture, self.project_action(fixture, "complete:work-a-1"), reviewed),
        )
        self.assertEqual("committed", committed["status"], committed)
        receipt = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot().transition_receipts[-1]
        self.assertEqual("pinboard-reviewed-completion/v2", receipt.input_schema)

    def test_a_ready_review_without_a_published_reviewer_prompt_does_not_authorize_completion(self) -> None:
        fixture, candidate = self.review_fixture()
        fixture = self.terminalize_brief(fixture)
        store = SQLiteWorkStore(fixture.work / "state.sqlite3")
        snapshot = store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        attempt = store.read_attempt_context(AttemptId("work-a-1"))
        assert snapshot is not None and isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        result_sha256 = hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest()
        review_sha256 = hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest()

        review = work_brief_models.CandidateReview(
            "pinboard-candidate-review/v2",
            "work-a-1",
            "work-a",
            candidate,
            work_brief_models.PortableArtifactIdentity(
                "candidate",
                "evidence",
                snapshot.reference.key,
                snapshot.reference.revision,
                snapshot.reference.selector,
                snapshot.reference.content_sha256,
                snapshot.reference.size_bytes,
            ),
            work_brief_models.PortableArtifactIdentity(
                "accepted-brief",
                "brief",
                attempt.brief_reference.key,
                attempt.brief_reference.revision,
                attempt.brief_reference.selector,
                attempt.brief_reference.content_sha256,
                attempt.brief_reference.size_bytes,
            ),
            result_sha256,
            review_sha256,
            "0" * 64,
            "commissioned-reviewer",
            "ready",
            "Recorded without a commissioned reviewer prompt.",
        )
        key = work_briefs.candidate_review_key(
            "work-a-1",
            candidate,
            snapshot.reference.content_sha256,
            attempt.brief_reference.content_sha256,
            result_sha256,
            review_sha256,
        )
        published = publish_accepted_artifact(
            store,
            ArtifactRepository(resolve_durable_roots(fixture.project, fixture.work)),
            NewArtifact(
                work_models.ArtifactKind.EVIDENCE, key, 1, ".json", work_briefs.canonical_candidate_review_bytes(review)
            ),
            datetime.now(UTC),
        )
        self.assertIsInstance(published, AcceptedArtifactPublication)
        payload = self.completion_payload(fixture, candidate, "commissioned-reviewer")
        self.assert_review_required(fixture, payload, "commissioned-reviewer")

    def test_commissioned_review_relaxes_no_continue_disposition_refusal(self) -> None:
        fixture, candidate = self.review_fixture()
        self.record_commissioned_review(fixture, candidate, "commissioned-reviewer")
        state = fixture.store.validated_snapshot()
        self.assertIn("checkpoint-acceptance/v2", {value.outcome_schema for value in state.transition_receipts})
        payload = self.completion_payload(fixture, candidate, "commissioned-reviewer")
        before = fixture.store.validated_snapshot()
        # Continue-disposition discovery withholds complete, so build its exact receipt from a sibling action.
        action = self.project_action(fixture, "return-for-correction:work-a-1")
        action["action_id"] = {"kind": "complete", "subject": "work-a-1"}
        refused = call_advertised_tool(
            mcp_server.TRANSITION_TOOL, self.native_transition_request(fixture, action, payload)
        )
        self.assertEqual("TRANSITION_INPUT_INVALID", refused["code"], refused)
        self.assertEqual("Completion requires a terminal checkpoint disposition.", refused["message"])
        self.assertEqual(before, fixture.store.validated_snapshot())


class RecordReadyCommissionTest(SubmittedCandidateSupport):
    def record(self, fixture: CheckpointFixture, candidate: str, prompt: str) -> JsonObject:
        return call_advertised_tool(
            mcp_server.REVIEW_JOB_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "review": self.ready_review_request(fixture, candidate, "commissioned-reviewer", prompt),
            },
        )

    def publish_prompt(self, fixture: CheckpointFixture, subject: dispatch_models.AgentPromptSubject, text: str) -> str:
        published = dispatch_models.publish_agent_prompt(
            SQLiteWorkStore(fixture.work / "state.sqlite3"),
            ArtifactRepository(resolve_durable_roots(fixture.project, fixture.work)),
            subject=subject,
            prompt=text,
            accepted_at=datetime.now(UTC),
        )
        assert isinstance(published, dispatch_models.PublishedAgentPrompt)
        return published.reference.sha256

    def test_record_ready_requires_the_prompt_the_review_job_published_for_the_exact_subject(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        candidate = fixture.candidate_revision
        store = SQLiteWorkStore(fixture.work / "state.sqlite3")
        snapshot = store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        attempt = store.read_attempt_context(AttemptId("work-a-1"))
        assert snapshot is not None and isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        older = self.commission_review(fixture, candidate)
        (attempt_root / "result.md").write_text("result refreshed after the first commission\n", encoding="utf-8")
        result_sha256 = hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest()
        worker = self.publish_prompt(fixture, dispatch_models.WorkerPromptSubject("work-a-1"), "worker task\n")
        foreign = self.publish_prompt(
            fixture,
            dispatch_models.ReviewerPromptSubject(
                "other-attempt",
                candidate,
                snapshot.reference.content_sha256,
                attempt.brief_reference.content_sha256,
                result_sha256,
            ),
            "foreign reviewer task\n",
        )
        for label, prompt in (("missing", "f" * 64), ("worker", worker), ("foreign", foreign), ("older-result", older)):
            with self.subTest(prompt=label):
                before = fixture.store.validated_snapshot()
                refused = self.record(fixture, candidate, prompt)
                self.assertEqual("rejected", refused["status"], refused)
                self.assertEqual("REVIEWER_PROMPT_NOT_COMMISSIONED", refused["code"])
                self.assertEqual(
                    ("unchanged", "do-not-retry", []),
                    (refused["effect"], refused["retry"], refused["changed_surfaces"]),
                )
                observed = {
                    self.json_object(row)["field"]: self.json_object(row)["value"]
                    for row in self.json_array(refused["observed"])
                }
                self.assertEqual(prompt, observed["reviewer_prompt_sha256"])
                self.assertEqual(mcp_server.REVIEW_JOB_TOOL, observed["next_step_tool"])
                self.assertEqual(before, fixture.store.validated_snapshot())

        current = self.commission_review(fixture, candidate)
        recorded = self.record(fixture, candidate, current)
        self.assertEqual("recorded", recorded["status"], recorded)
        reference = self.json_object(recorded["candidate_review"])
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3")
        accepted = next(
            value
            for value in fresh.validated_snapshot().artifact_references
            if int(value.artifact_ref_id) == reference["artifact_ref_id"]
        )
        stored = work_briefs.decode_canonical_candidate_review(
            ArtifactRepository(resolve_durable_roots(fixture.project, fixture.work)).read(accepted)
        )
        assert isinstance(stored, work_brief_models.CandidateReview)
        self.assertEqual(
            ("pinboard-candidate-review/v2", current, "commissioned-reviewer", result_sha256),
            (stored.schema, stored.reviewer_prompt_sha256, stored.reviewer_task_id, stored.result_sha256),
        )
        repeated = self.record(fixture, candidate, current)
        self.assertEqual(("recorded", "unchanged"), (repeated["status"], repeated["effect"]))

    def test_a_retained_v1_review_is_opaque_history_that_neither_reads_as_ready_nor_blocks_v2(self) -> None:
        fixture, candidate = self.review_fixture()
        store = SQLiteWorkStore(fixture.work / "state.sqlite3")
        snapshot = store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        attempt = store.read_attempt_context(AttemptId("work-a-1"))
        assert snapshot is not None and isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        result_sha256 = hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest()
        review_sha256 = hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest()

        retained = {
            "schema": "pinboard-candidate-review/v1",
            "attempt_id": "work-a-1",
            "item_id": "work-a",
            "candidate": candidate,
            "candidate_snapshot": {
                "role": "candidate",
                "kind": "evidence",
                "key": snapshot.reference.key,
                "revision": snapshot.reference.revision,
                "selector": snapshot.reference.selector,
                "content_sha256": snapshot.reference.content_sha256,
                "size_bytes": snapshot.reference.size_bytes,
            },
            "accepted_brief": {
                "role": "accepted-brief",
                "kind": "brief",
                "key": attempt.brief_reference.key,
                "revision": attempt.brief_reference.revision,
                "selector": attempt.brief_reference.selector,
                "content_sha256": attempt.brief_reference.content_sha256,
                "size_bytes": attempt.brief_reference.size_bytes,
            },
            "result_sha256": result_sha256,
            "review_sha256": review_sha256,
            "reviewer_task_id": "retained-reviewer",
            "verdict": "ready",
            "acceptance_evidence": "Recorded before reviews were commissioned.",
        }
        retained_identity = msgspec.json.encode(
            (
                "work-a-1",
                candidate,
                snapshot.reference.content_sha256,
                attempt.brief_reference.content_sha256,
                result_sha256,
                review_sha256,
            ),
            order="sorted",
        )
        retained_key = f"candidate-review-{hashlib.sha256(retained_identity).hexdigest()}"
        published = publish_accepted_artifact(
            store,
            ArtifactRepository(resolve_durable_roots(fixture.project, fixture.work)),
            NewArtifact(
                work_models.ArtifactKind.EVIDENCE,
                retained_key,
                1,
                ".json",
                msgspec.json.encode(retained, order="sorted") + b"\n",
            ),
            datetime.now(UTC),
        )
        self.assertIsInstance(published, AcceptedArtifactPublication)
        roots = {"project_root": str(fixture.project), "work_root": str(fixture.work)}
        status = call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL, {"request": {**roots, "operation": "item", "item_id": "work-a"}}
        )
        self.assertEqual({"kind": "none"}, status["review_verdict"])
        prepared = self.review_result(fixture, {"kind": "initial", "candidate_revision": candidate})
        self.assertEqual("ready", prepared["status"], prepared)
        inspected = call_advertised_tool(
            mcp_server.ATTEMPT_INSPECT_TOOL, {**roots, "attempt_id": "work-a-1", "reconciliation": None}
        )
        self.assertEqual({"kind": "absent"}, inspected["candidate_review"])
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])
        self.run_json_cli(*fixture.common, "export")

        prompt = self.json_object(prepared["prompt_reference"])["sha256"]
        assert isinstance(prompt, str)
        recorded = self.record(fixture, candidate, prompt)
        self.assertEqual(("recorded", "committed"), (recorded["status"], recorded["effect"]), recorded)
        status = call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL, {"request": {**roots, "operation": "item", "item_id": "work-a"}}
        )
        self.assertEqual("ready", self.json_object(status["review_verdict"])["kind"])
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])
        exported = self.run_json_cli(*fixture.common, "export")
        exported_names = {
            self.json_object(row)["logical_name"] for row in self.json_array(exported["artifact_references"])
        }
        self.assertIn(retained_key, exported_names)

    def test_both_new_refusal_codes_are_advertised_by_the_transition_close_and_review_job_schemas(self) -> None:
        executor = mcp_execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        server = mcp_server.create_server(
            executor, mcp_execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256)
        )
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        schemas = {
            name: tools[name].output_schema
            for name in (mcp_server.TRANSITION_TOOL, mcp_server.CLOSE_TOOL, mcp_server.REVIEW_JOB_TOOL)
        }
        transition = schemas[mcp_server.TRANSITION_TOOL]
        assert transition is not None
        codes = self.json_array(self.json_object(self.json_object(transition["$defs"])["DecisionFailureCode"])["enum"])
        self.assertIn("CANDIDATE_REVIEW_REQUIRED", codes)
        self.assertIn("REVIEWER_PROMPT_NOT_COMMISSIONED", codes)
        self.assertEqual(transition, schemas[mcp_server.CLOSE_TOOL])
        self.assertIn("REVIEWER_PROMPT_NOT_COMMISSIONED", json.dumps(schemas[mcp_server.REVIEW_JOB_TOOL]))
