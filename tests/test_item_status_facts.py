"""Item-status leaves report review verdicts, branch owners, closure facts, and damaged receipts."""

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from dataclasses import replace as replace_record
from datetime import UTC, datetime
from pathlib import Path
from typing import assert_never
from unittest.mock import MagicMock, patch

import msgspec
from mcp.server.mcpserver.exceptions import ToolError
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.files.models import AffectedViews, ViewRefreshResult
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import candidate_snapshots, queries, query_models
from pinboard.application.ports import GeneratedViewReader
from pinboard.domain import decision_models
from pinboard.domain.identifiers import AttemptId, HistoryId, WorkItemId
from pinboard.mcp import common as mcp_common
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import JsonObject, JsonValue, NoReadyCandidateReviews

CLOSED_AT = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
COMPLETED_AT = datetime(2030, 1, 3, 4, 5, 6, tzinfo=UTC)


class ItemStatusFactsTest(CheckpointPackageSupport):
    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def item_leaf(self, fixture: CheckpointFixture, item_id: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "item", "item_id": item_id}},
        )

    def branch_leaf(self, fixture: CheckpointFixture, branch: str) -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "branch", "branch": branch}},
        )

    def inspection(self, fixture: CheckpointFixture) -> JsonObject:
        return call_advertised_tool(
            mcp_server.ATTEMPT_INSPECT_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1", "reconciliation": None}
        )

    def verdict(self, fixture: CheckpointFixture) -> JsonObject:
        """Read the verdict through the advertised item leaf and again from a fresh store."""

        status = self.item_leaf(fixture)
        self.assertEqual("pinboard-item-status/v2", status["schema"], status)
        for attempt in self.json_array(status["attempts"]):
            self.assertEqual(fixture.brief.branch, self.json_object(attempt)["branch"])
        verdict = self.json_object(status["review_verdict"])
        fresh = queries.project_item_status(
            SQLiteWorkStore(fixture.work / "state.sqlite3"),
            NoReadyCandidateReviews(),
            WorkItemId("work-a"),
            datetime.now(UTC),
        )
        assert isinstance(fresh, query_models.ItemStatus)
        if verdict["kind"] != "ready":
            self.assertEqual(verdict, msgspec.to_builtins(fresh.review_verdict))
        return verdict

    def assert_valid(self, fixture: CheckpointFixture) -> None:
        validated, stdout, stderr = self.run_cli(*fixture.common, "validate", "--json")
        self.assertEqual(0, validated, f"{stdout}\n{stderr}")

    def transition(self, fixture: CheckpointFixture, action_id: str, payload: JsonObject) -> JsonObject:
        result = self.transition_result(fixture, self.project_action(fixture, action_id), payload)
        self.assertIn(result["status"], ("committed", "committed-with-warning"), result)
        return result

    def return_for_review(self, fixture: CheckpointFixture, reason: str) -> int:
        history_id = self.transition(fixture, "return-for-correction:work-a-1", {"reason": reason})["history_id"]
        assert isinstance(history_id, int)
        return history_id

    def close_prerequisite(self, fixture: CheckpointFixture) -> None:
        closed, stdout, stderr = self.run_cli(
            *fixture.common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "The prerequisite is satisfied.",
            "--task-id",
            "review-owner",
            "--host-id",
            "local",
        )
        self.assertEqual(0, closed, f"{stdout}\n{stderr}")

    def rebind(self, fixture: CheckpointFixture, branch: str, revision: int) -> None:
        brief = replace_struct(fixture.brief, artifact_revision=revision, branch=branch)
        published = call_native_tool(
            mcp_server.BRIEF_PUBLISH_TOOL,
            {**self.roots(fixture), "brief": self.json_object(msgspec.json.decode(msgspec.json.encode(brief)))},
        )
        self.assertEqual("committed", published["status"], published)
        reference = self.json_object(published["reference"])
        self.transition(
            fixture,
            "rebind-attempt:work-a-1",
            {
                "branch": branch,
                "base_revision": brief.base_revision,
                "brief_artifact_ref_id": reference["artifact_ref_id"],
            },
        )

    def submit_candidate(self, fixture: CheckpointFixture, label: str, content: str | None = None) -> str:
        (fixture.project / "tracked.txt").write_text(f"{label}\n" if content is None else content, encoding="utf-8")
        observed = call_native_tool(
            mcp_server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"}
        )
        candidate = observed["candidate"]
        assert isinstance(candidate, str), observed
        lease = self.native_attempt_acquire(fixture, f"worker-{label}")
        submission = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        submitted = self.transition_result(fixture, submission, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)
        return candidate

    def record_ready(self, fixture: CheckpointFixture, candidate: str) -> JsonObject:
        store = SQLiteWorkStore(fixture.work / "state.sqlite3")
        snapshot = store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        attempt = store.read_attempt_context(AttemptId("work-a-1"))
        assert snapshot is not None and isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        recorded = call_native_tool(
            mcp_server.REVIEW_JOB_TOOL,
            {
                **self.roots(fixture),
                "review": {
                    "kind": "record-ready",
                    "attempt_id": "work-a-1",
                    "candidate_revision": candidate,
                    "candidate_snapshot_sha256": snapshot.reference.content_sha256,
                    "accepted_brief_sha256": attempt.brief_reference.content_sha256,
                    "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
                    "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
                    "reviewer_task_id": "independent-reviewer",
                    "verdict": "ready",
                    "acceptance_evidence": "The protected candidate satisfies the accepted brief.",
                },
            },
        )
        self.assertEqual("recorded", recorded["status"], recorded)
        return recorded

    def complete(self, fixture: CheckpointFixture, evidence: str, candidate: str | None = None) -> None:
        fixture = self.terminalize_brief(fixture)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        with patch("pinboard.mcp.mutation_operations.datetime") as clock:
            clock.now.return_value = COMPLETED_AT
            self.transition(
                fixture,
                "complete:work-a-1",
                {
                    "schema": "pinboard-reviewed-completion/v2",
                    "candidate": fixture.candidate_revision if candidate is None else candidate,
                    "evidence": evidence,
                    "reviewer_task_id": "independent-reviewer",
                    "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
                    "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
                    "packages": [],
                },
            )

    def revise_title(self, fixture: CheckpointFixture, title: str) -> JsonObject:
        definition = call_native_tool(
            mcp_server.ITEM_DEFINITION_TOOL,
            {"request": {**self.roots(fixture), "operation": "current", "item_id": "work-a"}},
        )
        return self.transition(
            fixture,
            "revise-item:work-a",
            {
                "schema": "pinboard-item-revision/v1",
                "expected_revision": definition["definition_revision"],
                "expected_digest": definition["definition_digest"],
                "source_task": "review-owner",
                "reason": "Clarify the outcome.",
                "definition": {**self.json_object(definition["definition"]), "title": title},
            },
        )

    def item_leaf_after_repair_title(self, fixture: CheckpointFixture) -> str:
        """Read the committed definition title, which a damaged pause receipt does not affect."""

        definition = call_native_tool(
            mcp_server.ITEM_DEFINITION_TOOL,
            {"request": {**self.roots(fixture), "operation": "current", "item_id": "work-a"}},
        )
        title = self.json_object(definition["definition"])["title"]
        assert isinstance(title, str)
        return title

    def revise_with_damage_before_refresh(
        self, fixture: CheckpointFixture, history_id: int, columns: dict[str, str]
    ) -> JsonObject:
        """Commit a revision, then damage the pause receipt before its post-commit view refresh reads it."""

        refresh = mcp_common._refresh_affected_views

        def damage_then_refresh(
            durable: DurableRoots, store: GeneratedViewReader, affected: AffectedViews, now: datetime
        ) -> ViewRefreshResult:
            self.update_receipt(fixture, history_id, **columns)
            return refresh(durable, store, affected, now)

        with patch.object(mcp_common, "_refresh_affected_views", damage_then_refresh):
            return self.revise_title(fixture, "Revised while paused")

    def update_receipt(self, fixture: CheckpointFixture, history_id: int, **columns: str) -> None:
        assignments = ", ".join(f"{column} = ?" for column in columns)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                f"UPDATE transition_history SET {assignments} WHERE history_id = ?",
                (*columns.values(), history_id),
            )

    def latest_history_id(self, fixture: CheckpointFixture) -> int:
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            value = connection.execute("SELECT MAX(history_id) FROM transition_history").fetchone()[0]
        assert isinstance(value, int)
        return value

    def test_returned_verdict_survives_pause_block_resume_and_rebind_until_resubmission(self) -> None:
        reason = "Criterion 2 lacks behavior evidence."
        for route in ("pause", "block", "rebind"):
            with self.subTest(route=route):
                fixture = self.checkpoint_fixture()
                history_id = self.return_for_review(fixture, reason)
                returned = {
                    "kind": "returned-for-correction",
                    "history_id": history_id,
                    "reason": reason,
                    "rebound_since_return": False,
                }
                self.assertEqual(returned, self.verdict(fixture))
                context = call_advertised_tool(
                    mcp_server.CORRECTION_CONTEXT_TOOL,
                    {**self.roots(fixture), "attempt_id": "work-a-1", "correction_history_id": history_id},
                )
                self.assertEqual(context["correction_reason"], returned["reason"])
                match route:
                    case "pause":
                        self.close_prerequisite(fixture)
                        self.transition(fixture, "pause:work-a-1", {"reason": "Wait for the maintainer."})
                        self.assertEqual(returned, self.verdict(fixture))
                        self.transition(fixture, "resume:work-a", {})
                    case "block":
                        self.transition(
                            fixture,
                            "block:work-a-1",
                            {"reason": "Wait for the prerequisite.", "depends_on": ["work-c"]},
                        )
                        self.assertEqual(returned, self.verdict(fixture))
                        self.close_prerequisite(fixture)
                        self.transition(fixture, "resume:work-a", {})
                    case _:
                        self.rebind(fixture, fixture.brief.branch, 2)
                        self.rebind(fixture, fixture.brief.branch, 3)
                        returned = {**returned, "rebound_since_return": True}
                self.assertEqual(returned, self.verdict(fixture))
                self.submit_candidate(fixture, f"corrected-{route}")
                self.assertEqual({"kind": "none"}, self.verdict(fixture))
                self.assert_valid(fixture)

    def test_ready_verdict_follows_the_current_record_ready_review(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Rework the candidate.")
        candidate = self.submit_candidate(fixture, "reviewed")
        self.assertEqual({"kind": "none"}, self.verdict(fixture))
        recorded = self.record_ready(fixture, candidate)
        verdict = self.verdict(fixture)
        reference = self.json_object(recorded["candidate_review"])
        self.assertEqual("ready", verdict["kind"])
        self.assertEqual(candidate, verdict["candidate_revision"])
        self.assertEqual(
            (reference["artifact_ref_id"], reference["selector"], reference["sha256"]),
            tuple(
                self.json_object(verdict["candidate_review"])[key] for key in ("artifact_ref_id", "selector", "sha256")
            ),
        )
        review = fixture.work / "attempts" / "work-a-1" / "review.md"
        review.write_text("A later review changed the evidence.\n", encoding="utf-8")
        self.assertEqual({"kind": "none"}, self.verdict(fixture))
        review.unlink()
        self.assertEqual({"kind": "none"}, self.verdict(fixture))

    def test_acceptance_verdicts_carry_their_evidence_and_checkpoint(self) -> None:
        fixture = self.checkpoint_fixture()
        self.transition(
            fixture,
            "accept-review-and-continue:work-a-1",
            {"candidate": fixture.candidate_revision, "evidence": "Accepted; continue the attempt."},
        )
        self.assertEqual(
            {"kind": "accepted-and-continued", "evidence": "Accepted; continue the attempt."}, self.verdict(fixture)
        )
        fixture = self.accepted_package_fixture()
        self.assertEqual(
            {"kind": "checkpoint-accepted", "checkpoint": fixture.brief.checkpoint.checkpoint_id},
            self.verdict(fixture),
        )
        self.assertIsNone(self.json_object(self.json_array(self.item_leaf(fixture)["attempts"])[0])["pause_reason"])
        self.assert_valid(fixture)

    def test_retained_decision_v1_return_reports_its_evidence_without_failure(self) -> None:
        for evidence in ("Retained reason.", None):
            with self.subTest(evidence=evidence):
                fixture = self.checkpoint_fixture()
                history_id = self.return_for_review(fixture, "Current reason.")
                outcome = {"evidence": evidence, "outcome": "return-for-correction"}
                self.update_receipt(
                    fixture,
                    history_id,
                    input_schema="decision/v1",
                    input_json="{}",
                    outcome_json=json.dumps({key: value for key, value in outcome.items() if value is not None}),
                )
                self.assertEqual(
                    {
                        "kind": "returned-for-correction",
                        "history_id": history_id,
                        "reason": evidence,
                        "rebound_since_return": False,
                    },
                    self.verdict(fixture),
                )
                self.rebind(fixture, fixture.brief.branch, 2)
                self.assertTrue(self.verdict(fixture)["rebound_since_return"])

    def damaged_receipt_fixture(self, receipt: query_models.DamagedReceiptActionKind) -> CheckpointFixture:
        """Build an attempt whose consumed receipt of the named kind is its latest receipt."""

        match receipt:
            case decision_models.ActionKind.ACCEPT_CHECKPOINT:
                return self.accepted_package_fixture()
            case decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE:
                fixture = self.checkpoint_fixture()
                self.transition(
                    fixture,
                    "accept-review-and-continue:work-a-1",
                    {"candidate": fixture.candidate_revision, "evidence": "Accepted; continue the attempt."},
                )
                return fixture
            case decision_models.ActionKind.RETURN_FOR_CORRECTION:
                fixture = self.checkpoint_fixture()
                self.return_for_review(fixture, "Rework the candidate.")
                return fixture
            case decision_models.ActionKind.PAUSE:
                fixture = self.damaged_receipt_fixture(decision_models.ActionKind.RETURN_FOR_CORRECTION)
                self.transition(fixture, "pause:work-a-1", {"reason": "Wait for the maintainer."})
                return fixture
            case decision_models.ActionKind.REBIND_ATTEMPT:
                fixture = self.damaged_receipt_fixture(decision_models.ActionKind.PAUSE)
                self.rebind(fixture, fixture.brief.branch, 2)
                return fixture
            case _ as unreachable:
                assert_never(unreachable)

    def named_receipt(
        self, result: JsonObject, action_kind: query_models.DamagedReceiptActionKind
    ) -> query_models.DamagedTransitionReceipt:
        """Rebuild the damaged receipt a rejected read names from its structured facts."""

        self.assertEqual("TRANSITION_RECEIPT_DAMAGED", result["code"], result)
        self.assertEqual(("unchanged", "do-not-retry"), (result["effect"], result["retry"]))
        observed = {
            self.json_object(value)["field"]: self.json_object(value)["value"]
            for value in self.json_array(result["observed"])
        }
        self.assertEqual(action_kind.value, observed["action_kind"])
        attempt_id, history_id, committed_at = observed["attempt_id"], observed["history_id"], observed["committed_at"]
        defect = self.json_object(self.json_array(result["mismatches"])[0])["observed"]
        assert isinstance(attempt_id, str) and isinstance(history_id, int)
        assert isinstance(committed_at, str) and isinstance(defect, str)
        return query_models.DamagedTransitionReceipt(
            AttemptId(attempt_id), HistoryId(history_id), datetime.fromisoformat(committed_at), action_kind, defect
        )

    def test_damaged_consumed_receipts_are_named_by_status_reads(self) -> None:
        damage = (
            ("outcome-schema", {"outcome_schema": "transition-receipt/v9"}),
            ("outcome-body", {"outcome_json": '{"unexpected":true}'}),
            ("outcome-json", {"outcome_json": "not json"}),
        )
        validation = query_models.DamagedReceiptDiagnosis.VALIDATION
        human = query_models.DamagedReceiptDiagnosis.HUMAN
        receipts: tuple[tuple[query_models.DamagedReceiptActionKind, query_models.DamagedReceiptDiagnosis], ...] = (
            (decision_models.ActionKind.PAUSE, validation),
            (decision_models.ActionKind.REBIND_ATTEMPT, validation),
            (decision_models.ActionKind.RETURN_FOR_CORRECTION, human),
            (decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE, human),
            (decision_models.ActionKind.ACCEPT_CHECKPOINT, human),
        )
        for receipt, diagnosis in receipts:
            for label, columns in damage:
                with self.subTest(receipt=receipt.value, damage=label):
                    fixture = self.damaged_receipt_fixture(receipt)
                    if receipt == decision_models.ActionKind.ACCEPT_CHECKPOINT and label == "outcome-schema":
                        columns = {"outcome_schema": "checkpoint-acceptance/v9"}
                    damaged_history = self.latest_history_id(fixture)
                    self.update_receipt(fixture, damaged_history, **columns)

                    status = self.item_leaf(fixture)
                    self.assertEqual("pinboard-mcp-item-status-result/v3", status["schema"])
                    named = self.named_receipt(status, receipt)
                    self.assertEqual((AttemptId("work-a-1"), damaged_history), (named.attempt_id, named.history_id))
                    self.assertEqual(diagnosis, queries.damaged_receipt_diagnosis(receipt))
                    self.assertEqual(queries.damaged_receipt_recovery(named), status["recovery"])
                    inspected = self.inspection(fixture)
                    validated, stdout, _stderr = self.run_cli(*fixture.common, "validate", "--json")
                    codes = {
                        self.json_object(value)["code"]
                        for value in self.json_array(self.json_object(json.loads(stdout))["diagnostics"])
                    }
                    match diagnosis:
                        case query_models.DamagedReceiptDiagnosis.VALIDATION:
                            self.assertEqual(named, self.named_receipt(inspected, receipt))
                            self.assertEqual(status["recovery"], inspected["recovery"])
                            self.assertEqual(10, validated, stdout)
                            self.assertIn(
                                "WORK_STATE_INVALID" if label == "outcome-json" else "TRANSITION_RECEIPT_DAMAGED", codes
                            )
                        case query_models.DamagedReceiptDiagnosis.HUMAN:
                            self.assertEqual("ok", inspected["status"], inspected)
                            self.assertNotIn("TRANSITION_RECEIPT_DAMAGED", codes)
                        case _ as unreachable:
                            assert_never(unreachable)

    def test_damaged_pause_receipt_is_named_by_view_generation(self) -> None:
        for label, columns in (
            ("outcome-schema", {"outcome_schema": "transition-receipt/v9"}),
            ("outcome-body", {"outcome_json": '{"unexpected":true}'}),
            ("outcome-json", {"outcome_json": "not json"}),
        ):
            with self.subTest(damage=label):
                fixture = self.checkpoint_fixture()
                self.return_for_review(fixture, "Rework the candidate.")
                self.transition(fixture, "pause:work-a-1", {"reason": "Wait for the maintainer."})
                history_id = self.latest_history_id(fixture)
                revised = self.revise_with_damage_before_refresh(fixture, history_id, columns)
                self.assertEqual("committed-with-warning", revised["status"], revised)
                self.assertEqual("Revised while paused", self.item_leaf_after_repair_title(fixture))
                warning = self.json_object(revised["warning"])
                self.assertIn(f"Transition receipt {history_id} ", str(warning["message"]))

                validated, validation, validation_error = self.run_cli(*fixture.common, "validate", "--json")
                self.assertEqual(10, validated, f"{validation}\n{validation_error}")
                diagnostics = self.json_array(self.json_object(json.loads(validation))["diagnostics"])
                codes = {self.json_object(value)["code"] for value in diagnostics}
                if label == "outcome-json":
                    self.assertEqual({"WORK_STATE_INVALID"}, codes)
                    with self.assertRaises(StorageError) as rejected:
                        self.run_cli(*fixture.common, "views", "rebuild")
                    self.assertEqual(StorageErrorCode.INVALID_STATE, rejected.exception.code)
                    continue
                named = next(
                    self.json_object(value)
                    for value in diagnostics
                    if self.json_object(value)["code"] == "TRANSITION_RECEIPT_DAMAGED"
                )
                self.assertIn(f"Transition receipt {history_id} ", str(named["message"]))
                rebuilt, stdout, stderr = self.run_cli(*fixture.common, "views", "rebuild")
                self.assertNotEqual(0, rebuilt)
                self.assertIn(f"Transition receipt {history_id} ", f"{stdout}{stderr}")

    def test_item_leaf_reports_closure_by_completion_and_direct_close(self) -> None:
        fixture = self.checkpoint_fixture()
        with patch("pinboard.cli.transitions.datetime") as clock:
            clock.now.return_value = CLOSED_AT
            self.close_prerequisite(fixture)
        closed = self.item_leaf(fixture, "work-c")
        self.assertEqual(
            {"action": "close", "committed_at": CLOSED_AT.isoformat(), "closing_attempt": None}, closed["closure"]
        )
        self.assertEqual("The prerequisite is satisfied.", closed["outcome_evidence"])
        self.complete(fixture, "Accepted and integrated by the maintainer.")
        completed = self.item_leaf(fixture)
        self.assertEqual(
            {
                "action": "complete",
                "committed_at": COMPLETED_AT.isoformat(),
                "closing_attempt": {
                    "attempt_id": "work-a-1",
                    "branch": fixture.brief.branch,
                    "candidate_revision": fixture.candidate_revision,
                },
            },
            completed["closure"],
        )
        self.assertEqual([], completed["attempts"])
        self.assertEqual({"kind": "none"}, completed["review_verdict"])
        receipts = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot().transition_receipts
        close_history, complete_history = (
            next(
                receipt.history_id
                for receipt in receipts
                if str(receipt.subject_id) == subject and receipt.action_kind.value == action
            )
            for subject, action in (("work-c", "close"), ("work-a-1", "complete"))
        )
        for item_id, history_id, outcome in (
            ("work-c", close_history, "done"),
            ("work-a", complete_history, "complete"),
        ):
            with self.subTest(retained_terminal_receipt=item_id):
                current = self.item_leaf(fixture, item_id)["closure"]
                self.update_receipt(
                    fixture,
                    int(history_id),
                    input_schema="decision/v1",
                    input_json="{}",
                    outcome_schema="transition-receipt/v1",
                    outcome_json=json.dumps({"evidence": "Retained terminal evidence.", "outcome": outcome}),
                )
                self.assertEqual(current, self.item_leaf(fixture, item_id)["closure"])
        self.update_receipt(fixture, int(close_history), outcome_json="not json")
        self.assertEqual("close", self.json_object(self.item_leaf(fixture, "work-c")["closure"])["action"])
        self.update_receipt(fixture, int(close_history), action_kind="mark-ready")
        self.assertIsNone(self.item_leaf(fixture, "work-c")["closure"])

    def test_branch_leaf_maps_live_kept_reused_and_rebound_branches(self) -> None:
        fixture = self.checkpoint_fixture()
        branch = fixture.brief.branch
        owners = self.branch_leaf(fixture, branch)
        self.assertEqual("pinboard-branch-owners/v1", owners["schema"], owners)
        self.assertEqual(
            [{"item_id": "work-a", "item_state": "review", "attempt_id": "work-a-1", "attempt_state": "review"}],
            owners["owners"],
        )
        unknown = self.branch_leaf(fixture, "codex/never-recorded")
        self.assertEqual("BRANCH_OWNER_NOT_FOUND", unknown["code"], unknown)
        self.assertEqual(("unchanged", "correct-input"), (unknown["effect"], unknown["retry"]))
        observed = {
            self.json_object(value)["field"]: self.json_object(value)["value"]
            for value in self.json_array(unknown["observed"])
        }
        self.assertEqual({"branch": "codex/never-recorded", "work_root": str(fixture.work)}, observed)

        rebound = "codex/work-a-rebound"
        self.return_for_review(fixture, "Rework on a new branch.")
        subprocess.run(["git", "switch", "-c", rebound], cwd=fixture.project, check=True, capture_output=True)
        self.rebind(fixture, rebound, 2)
        before_rebind = self.branch_leaf(fixture, branch)
        self.assertEqual("BRANCH_OWNER_NOT_FOUND", before_rebind["code"], before_rebind)
        self.assertEqual(unknown["recovery"], before_rebind["recovery"])
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(attempts)")]
            copied = ", ".join(
                {"attempt_id": "'work-b-kept'", "item_id": "'work-b'", "state": "'done'"}.get(column, column)
                for column in columns
            )
            connection.execute(
                f"INSERT INTO attempts ({', '.join(columns)}) "
                f"SELECT {copied} FROM attempts WHERE attempt_id = 'work-a-1'"
            )
        self.assertEqual(
            [
                {"item_id": "work-a", "item_state": "active", "attempt_id": "work-a-1", "attempt_state": "active"},
                {"item_id": "work-b", "item_state": "superseded", "attempt_id": "work-b-kept", "attempt_state": "done"},
            ],
            self.branch_leaf(fixture, rebound)["owners"],
        )

    def test_scratch_board_location_is_found_from_the_kept_branch(self) -> None:
        fixture = self.checkpoint_fixture()
        scratch = fixture.project.parent / "experiment-scratch" / ".pinboard"
        self.complete(fixture, f"Kept the experiment's changes; its scratch board is at {scratch}.")
        owners = self.json_array(self.branch_leaf(fixture, fixture.brief.branch)["owners"])
        owner = self.json_object(owners[0])
        self.assertEqual(("work-a", "done", "done"), (owner["item_id"], owner["item_state"], owner["attempt_state"]))
        status = self.item_leaf(fixture, str(owner["item_id"]))
        self.assertIn(str(scratch), str(status["outcome_evidence"]))
        self.assertEqual("complete", self.json_object(status["closure"])["action"])

    def test_intake_text_is_labeled_original_context_after_revision_and_start(self) -> None:
        fixture = self.checkpoint_fixture()
        self.revise_title(fixture, "Revised work")
        status = self.item_leaf(fixture)
        self.assertNotIn("next_action", status)
        self.assertNotIn("notes", status)
        intake = self.json_object(status["intake_context"])
        self.assertEqual("original-context", intake["label"])
        stored = next(
            value
            for value in SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot().lifecycle.work_items
            if str(value.item_id) == "work-a"
        )
        self.assertEqual((stored.next_action, stored.notes), (intake["next_action"], intake["notes"]))
        assert stored.next_action is not None
        view = (fixture.work / "views" / "items" / "work-a.md").read_text(encoding="utf-8")
        current, *sections = view.split("\n## ")
        self.assertNotIn(stored.next_action, current)
        intake_sections = [section for section in sections if stored.next_action in section]
        self.assertEqual(1, len(intake_sections))
        self.assertIn(stored.notes or "none", intake_sections[0])

    def test_old_flat_item_status_arguments_are_rejected(self) -> None:
        fixture = self.checkpoint_fixture()
        with self.assertRaises(ToolError):
            call_native_tool(mcp_server.ITEM_STATUS_TOOL, {**self.roots(fixture), "item_id": "work-a"})

    def integration_leaf(self, fixture: CheckpointFixture, target: str, item_id: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": item_id, "target": target}},
        )

    def head(self, project: Path, revision: str = "HEAD") -> str:
        return subprocess.run(
            ["git", "rev-parse", revision], cwd=project, check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit_tracked_file(self, project: Path, parent: str, content: str, message: str) -> str:
        """Commit one tracked-file content over a parent with fixed dates, leaving the checkout untouched."""

        dates = {
            "GIT_AUTHOR_NAME": "Pinboard Tests",
            "GIT_AUTHOR_EMAIL": "pinboard@example.invalid",
            "GIT_COMMITTER_NAME": "Pinboard Tests",
            "GIT_COMMITTER_EMAIL": "pinboard@example.invalid",
            "GIT_AUTHOR_DATE": "2030-01-05T00:00:00Z",
            "GIT_COMMITTER_DATE": "2030-01-05T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, **dates, "GIT_INDEX_FILE": str(Path(directory) / "index")}
            subprocess.run(["git", "read-tree", parent], cwd=project, env=environment, check=True, capture_output=True)
            blob = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=project,
                input=content.encode(),
                check=True,
                capture_output=True,
            ).stdout.decode()
            subprocess.run(
                ["git", "update-index", "--add", "--cacheinfo", f"100644,{blob.strip()},tracked.txt"],
                cwd=project,
                env=environment,
                check=True,
                capture_output=True,
            )
            tree = subprocess.run(
                ["git", "write-tree"], cwd=project, env=environment, check=True, capture_output=True, text=True
            ).stdout.strip()
        return subprocess.run(
            ["git", "commit-tree", tree, "-p", parent, "-m", message],
            cwd=project,
            env={**os.environ, **dates},
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def point(self, project: Path, ref: str, revision: str) -> None:
        subprocess.run(["git", "update-ref", ref, revision], cwd=project, check=True, capture_output=True)

    def commit_all_fixed(self, project: Path, message: str) -> str:
        """Commit every working-tree change with fixed dates, so the candidate does not depend on the wall clock."""

        subprocess.run(["git", "add", "--all"], cwd=project, check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Pinboard Tests",
                "-c",
                "user.email=pinboard@example.invalid",
                "commit",
                "-m",
                message,
            ],
            cwd=project,
            check=True,
            capture_output=True,
            env={**os.environ, "GIT_AUTHOR_DATE": "2030-01-05T00:00:00Z", "GIT_COMMITTER_DATE": "2030-01-05T00:00:00Z"},
        )
        return self.head(project)

    def test_integration_leaf_compares_a_protected_working_tree_candidate_by_content(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Rework the candidate.")
        candidate = self.submit_candidate(fixture, "reviewed")
        base = self.head(fixture.project)
        source = {
            "kind": "protected-review",
            "attempt_id": "work-a-1",
            "candidate_revision": candidate,
            "compared_from_revision": base,
        }
        unchanged = self.integration_leaf(fixture, "codex/work-a")
        self.assertEqual("pinboard-item-integration/v1", unchanged["schema"], unchanged)
        self.assertEqual(
            ("codex/work-a", base, "content-not-present", source),
            (unchanged["target"], unchanged["resolved_revision"], unchanged["presence"], unchanged["source"]),
        )

        squashed = self.commit_tracked_file(fixture.project, base, "reviewed\n", "Squash the reviewed change.")
        self.point(fixture.project, "refs/heads/release", squashed)
        present = self.integration_leaf(fixture, "release")
        self.assertEqual(
            ("release", squashed, "content-present"),
            (present["target"], present["resolved_revision"], present["presence"]),
        )
        self.assertEqual(source, present["source"])
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", squashed, "codex/work-a"], cwd=fixture.project, check=False
        )
        self.assertEqual(1, ancestry.returncode, "The squash commit must not be an ancestor of the target.")

        self.point(fixture.project, "refs/remotes/origin/release", squashed)
        remote = self.integration_leaf(fixture, "origin/release")
        self.assertEqual(("origin/release", "content-present"), (remote["target"], remote["presence"]))

        overlapping = self.commit_tracked_file(
            fixture.project, squashed, "someone else\n", "Overwrite the reviewed line."
        )
        self.point(fixture.project, "refs/heads/drifted", overlapping)
        drifted = self.integration_leaf(fixture, "drifted")
        self.assertEqual("content-not-present", drifted["presence"])

    def test_integration_leaf_names_each_rejection_with_its_facts_and_next_step(self) -> None:
        fixture = self.checkpoint_fixture()
        unresolved = self.integration_leaf(fixture, "no-such-ref")
        self.assertEqual(
            ("INTEGRATION_TARGET_UNRESOLVED", "correct-input", "unchanged", "pinboard-mcp-item-status-result/v3"),
            (unresolved["code"], unresolved["retry"], unresolved["effect"], unresolved["schema"]),
            unresolved,
        )
        self.assertEqual(
            {"target": "no-such-ref", "project_root": str(fixture.project)},
            {
                str(self.json_object(value)["field"]): self.json_object(value)["value"]
                for value in self.json_array(unresolved["observed"])
            },
        )
        self.assertIn("fetch", str(unresolved["recovery"]))

        unavailable = self.integration_leaf(fixture, "codex/work-a", item_id="work-c")
        self.assertEqual(
            ("INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input", "unchanged"),
            (unavailable["code"], unavailable["retry"], unavailable["effect"]),
            unavailable,
        )
        self.assertEqual("work-c", self.json_object(self.json_array(unavailable["observed"])[0])["value"])

        missing = self.integration_leaf(fixture, "codex/work-a", item_id="never-created")
        self.assertEqual("ITEM_NOT_FOUND", missing["code"], missing)
        self.assertEqual(("unchanged", "correct-input"), (missing["effect"], missing["retry"]))

        flag_like = call_native_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    **self.roots(fixture),
                    "operation": "integration",
                    "item_id": "work-a",
                    "target": "--output=x",
                }
            },
        )
        self.assertEqual("ITEM_STATUS_INVALID", flag_like["code"], flag_like)

    def test_integration_leaf_reports_altered_snapshot_bytes_as_invalid_evidence(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Rework the candidate.")
        self.submit_candidate(fixture, "reviewed")
        snapshot = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert snapshot is not None
        path = fixture.work / snapshot.reference.selector
        original = path.read_bytes()
        path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        invalid = self.integration_leaf(fixture, "codex/work-a")
        self.assertEqual(
            ("INTEGRATION_CANDIDATE_EVIDENCE_INVALID", "do-not-retry"), (invalid["code"], invalid["retry"])
        )
        self.assertEqual(
            "work-a-1",
            self.json_object(self.json_array(invalid["observed"])[0])["value"],
        )

    def test_integration_leaf_compares_an_accepted_checkpoint_candidate(self) -> None:
        fixture = self.accepted_package_fixture()
        base = self.head(fixture.project)
        checkpoint = fixture.brief.checkpoint.checkpoint_id
        status = self.integration_leaf(fixture, "codex/work-a")
        self.assertEqual(
            ("accepted-checkpoint", checkpoint, base),
            (
                self.json_object(status["source"])["kind"],
                self.json_object(status["source"])["checkpoint_id"],
                self.json_object(status["source"])["compared_from_revision"],
            ),
            status,
        )
        self.assertEqual("content-not-present", status["presence"])
        accepted = self.commit_tracked_file(fixture.project, base, "candidate\n", "Integrate the accepted checkpoint.")
        self.point(fixture.project, "refs/heads/integrated", accepted)
        self.assertEqual("content-present", self.integration_leaf(fixture, "integrated")["presence"])

    def test_integration_leaf_compares_a_protected_commit_candidate_through_each_integration_shape(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Rework the candidate.")
        base = self.head(fixture.project)
        (fixture.project / "tracked.txt").write_text("reviewed\n", encoding="utf-8")
        candidate = self.commit_all_fixed(fixture.project, "Reviewed commit.")
        lease = self.native_attempt_acquire(fixture, "worker-commit")
        submission = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        self.assertEqual("committed", self.transition_result(fixture, submission, {"candidate": candidate})["status"])
        source = self.json_object(self.integration_leaf(fixture, "codex/work-a")["source"])
        self.assertEqual(
            ("protected-review", candidate, base),
            (source["kind"], source["candidate_revision"], source["compared_from_revision"]),
        )

        self.assertEqual("content-present", self.integration_leaf(fixture, candidate)["presence"])
        merged = self.commit_tracked_file(fixture.project, base, "reviewed\n", "Unrelated merge subject.")
        merge = subprocess.run(
            [
                "git",
                "commit-tree",
                self.head(fixture.project, f"{merged}^{{tree}}"),
                "-p",
                candidate,
                "-p",
                merged,
                "-m",
                "Merge the review.",
            ],
            cwd=fixture.project,
            env={**os.environ, "GIT_AUTHOR_DATE": "2030-01-06T00:00:00Z", "GIT_COMMITTER_DATE": "2030-01-06T00:00:00Z"},
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.point(fixture.project, "refs/heads/merged", merge)
        self.assertEqual("content-present", self.integration_leaf(fixture, "merged")["presence"])
        rebased = self.commit_tracked_file(fixture.project, base, "reviewed\n", "Rebase the reviewed change.")
        self.point(fixture.project, "refs/heads/rebased", rebased)
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", candidate, "rebased"], cwd=fixture.project, check=False
        )
        self.assertEqual(1, ancestry.returncode, "A rebase merge must not contain the candidate as an ancestor.")
        self.assertEqual("content-present", self.integration_leaf(fixture, "rebased")["presence"])

    def test_integration_leaf_compares_a_completed_items_closing_candidate(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Rework the candidate.")
        base = self.head(fixture.project)
        (fixture.project / "tracked.txt").write_text("reviewed\n", encoding="utf-8")
        candidate = self.commit_all_fixed(fixture.project, "Reviewed commit.")
        lease = self.native_attempt_acquire(fixture, "worker-complete")
        submission = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        self.assertEqual("committed", self.transition_result(fixture, submission, {"candidate": candidate})["status"])
        self.complete(fixture, "Accepted and integrated by the maintainer.", candidate=candidate)
        status = self.item_leaf(fixture)
        self.assertEqual("complete", self.json_object(status["closure"])["action"])
        completed = self.integration_leaf(fixture, base)
        self.assertEqual(
            {
                "kind": "completion",
                "attempt_id": "work-a-1",
                "candidate_revision": candidate,
                "compared_from_revision": base,
            },
            completed["source"],
        )
        self.assertEqual("content-not-present", completed["presence"])
        self.point(
            fixture.project,
            "refs/heads/released",
            self.commit_tracked_file(fixture.project, base, "reviewed\n", "Release."),
        )
        self.assertEqual("content-present", self.integration_leaf(fixture, "released")["presence"])

    def test_integration_compares_a_non_overlapping_later_edit_through_mcp(self) -> None:
        fixture = self.checkpoint_fixture()
        lines = [f"line {number}\n" for number in range(1, 11)]
        (fixture.project / "tracked.txt").write_text("".join(lines), encoding="utf-8")
        base = self.commit_all_fixed(fixture.project, "Multi-line base.")
        self.return_for_review(fixture, "Rework the candidate.")
        self.submit_candidate(fixture, "edited", content="".join(["changed\n", *lines[1:]]))
        distant = self.commit_tracked_file(
            fixture.project,
            base,
            "".join(["changed\n", *lines[1:-1], "line ten, edited later\n"]),
            "Edit a distant line.",
        )
        self.point(fixture.project, "refs/heads/distant", distant)
        self.assertEqual("content-present", self.integration_leaf(fixture, "distant")["presence"])

    def test_an_unchanged_candidate_reports_no_change(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Rework the candidate.")
        self.submit_candidate(fixture, "unchanged", content="base\n")
        unchanged = self.integration_leaf(fixture, "codex/work-a")
        self.assertEqual(
            ("protected-review", "no-change"),
            (self.json_object(unchanged["source"])["kind"], unchanged["presence"]),
            unchanged,
        )

    def test_integration_follows_return_continue_resume_and_rebind(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Rework the candidate.")
        self.submit_candidate(fixture, "reviewed")
        self.return_for_review(fixture, "Rework again.")
        returned = self.integration_leaf(fixture, "codex/work-a")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", returned["code"], returned)
        continued_candidate = self.submit_candidate(fixture, "continued")
        self.transition(
            fixture,
            "accept-review-and-continue:work-a-1",
            {"candidate": continued_candidate, "evidence": "Accepted; continue the attempt."},
        )
        continued = self.integration_leaf(fixture, "codex/work-a")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", continued["code"], continued)

        accepted = self.accepted_package_fixture()
        checkpoint = accepted.brief.checkpoint.checkpoint_id

        def checkpoint_source() -> tuple[JsonValue, JsonValue]:
            source = self.json_object(self.integration_leaf(accepted, "codex/work-a")["source"])
            return source["kind"], source["checkpoint_id"]

        self.assertEqual(("accepted-checkpoint", checkpoint), checkpoint_source())
        self.rebind(accepted, accepted.brief.branch, 2)
        self.assertEqual(("accepted-checkpoint", checkpoint), checkpoint_source())

    def test_integration_neither_runs_the_verdict_walk_nor_reads_unrelated_attempts(self) -> None:
        fixture = self.accepted_package_fixture()
        before = self.integration_leaf(fixture, "codex/work-a")
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(attempts)")]
            copied = ", ".join(
                {"attempt_id": "'work-b-kept'", "item_id": "'work-b'", "state": "'done'"}.get(column, column)
                for column in columns
            )
            connection.execute(
                f"INSERT INTO attempts ({', '.join(columns)}) "
                f"SELECT {copied} FROM attempts WHERE attempt_id = 'work-a-1'"
            )
        with (
            patch("pinboard.adapters.sqlite.lifecycle._read_review_event", side_effect=AssertionError("review walk")),
            patch(
                "pinboard.adapters.sqlite.lifecycle.read_recorded_pause_reasons",
                side_effect=AssertionError("pause reasons"),
            ),
        ):
            after = self.integration_leaf(fixture, "codex/work-a")
        self.assertEqual(before, after)

    def test_integration_names_each_unavailable_and_invalid_snapshot_path(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Rework the candidate.")
        self.submit_candidate(fixture, "reviewed")
        real_context = SQLiteWorkStore.read_candidate_snapshot_context

        def changed_context(
            store: SQLiteWorkStore, attempt_id: AttemptId
        ) -> query_models.CandidateSnapshotContextFacts | None:
            context = real_context(store, attempt_id)
            return None if context is None else replace_record(context, candidate_revision="another-candidate")

        with patch.object(SQLiteWorkStore, "read_candidate_snapshot_context", return_value=None):
            predates = self.integration_leaf(fixture, "codex/work-a")
        self.assertEqual(
            ("INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input", "unchanged"),
            (predates["code"], predates["retry"], predates["effect"]),
            predates,
        )
        self.assertIn("predates", str(predates["message"]))

        with patch.object(SQLiteWorkStore, "read_candidate_snapshot_context", changed_context):
            changed = self.integration_leaf(fixture, "codex/work-a")
        self.assertEqual(
            ("INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"), (changed["code"], changed["retry"]), changed
        )
        self.assertIn("changed while", str(changed["message"]))

        accepted = self.accepted_package_fixture()
        with patch.object(SQLiteWorkStore, "read_artifact_reference", return_value=None):
            missing = self.integration_leaf(accepted, "codex/work-a")
        self.assertEqual(
            ("INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"), (missing["code"], missing["retry"]), missing
        )
        self.assertIn("no accepted candidate snapshot reference", str(missing["message"]))

        foreign = candidate_snapshots.CommitCandidateSnapshot(
            "pinboard-candidate-snapshot/v1",
            "work-a-1",
            "work-a",
            "f" * 40,
            "codex/work-a",
            "a" * 40,
            "a" * 40,
            "2030-01-05T00:00:00+00:00",
            b"",
        )
        with (
            patch.object(SQLiteWorkStore, "read_artifact_reference", return_value=MagicMock()),
            patch("pinboard.adapters.candidate_evidence.read_reference", return_value=b"{}"),
            patch("pinboard.application.candidate_snapshots.decode_candidate_snapshot", return_value=foreign),
        ):
            mismatched = self.integration_leaf(accepted, "codex/work-a")
        self.assertEqual(
            ("INTEGRATION_CANDIDATE_EVIDENCE_INVALID", "do-not-retry"),
            (mismatched["code"], mismatched["retry"]),
            mismatched,
        )
        self.assertIn("another candidate", str(mismatched["message"]))

    def test_integration_lookups_use_keyed_or_indexed_access(self) -> None:
        fixture = self.accepted_package_fixture()
        statements = {
            "item": ("SELECT item_id, state FROM work_items WHERE item_id = ?", ("work-a",)),
            "attempt": (
                "SELECT attempt_id, state FROM attempts INDEXED BY one_live_attempt_per_item "
                "WHERE item_id = ? AND state != 'done'",
                ("work-a",),
            ),
        }
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            for name, (statement, parameters) in statements.items():
                with self.subTest(lookup=name):
                    plan = connection.execute(f"EXPLAIN QUERY PLAN {statement}", parameters).fetchall()
                    self.assertTrue(all("USING" in str(row[-1]) for row in plan), plan)

    def test_integration_keeps_the_checkpoint_source_after_resume(self) -> None:
        accepted = self.accepted_package_fixture()
        checkpoint = accepted.brief.checkpoint.checkpoint_id
        self.close_prerequisite(accepted)
        self.transition(accepted, "resume:work-a", {})
        source = self.json_object(self.integration_leaf(accepted, "codex/work-a")["source"])
        self.assertEqual(("accepted-checkpoint", checkpoint), (source["kind"], source["checkpoint_id"]))

    def test_integration_keeps_the_checkpoint_source_after_rebind(self) -> None:
        accepted = self.accepted_package_fixture()
        checkpoint = accepted.brief.checkpoint.checkpoint_id
        self.rebind(accepted, accepted.brief.branch, 2)
        source = self.json_object(self.integration_leaf(accepted, "codex/work-a")["source"])
        self.assertEqual(("accepted-checkpoint", checkpoint), (source["kind"], source["checkpoint_id"]))

    def test_integration_compares_a_renamed_file_through_mcp(self) -> None:
        fixture = self.checkpoint_fixture()
        base = self.head(fixture.project)
        branch = fixture.brief.branch
        subprocess.run(
            ["git", "switch", "-q", "-c", "released", base], cwd=fixture.project, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "mv", "tracked.txt", "renamed.txt"], cwd=fixture.project, check=True, capture_output=True
        )
        (fixture.project / "renamed.txt").write_text("reviewed\n", encoding="utf-8")
        self.commit_all_fixed(fixture.project, "Rename the reviewed file.")
        subprocess.run(["git", "switch", "-q", branch], cwd=fixture.project, check=True, capture_output=True)
        self.return_for_review(fixture, "Rework the candidate.")
        subprocess.run(
            ["git", "mv", "tracked.txt", "renamed.txt"], cwd=fixture.project, check=True, capture_output=True
        )
        (fixture.project / "renamed.txt").write_text("reviewed\n", encoding="utf-8")
        observed = call_native_tool(
            mcp_server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"}
        )
        candidate = str(observed["candidate"])
        lease = self.native_attempt_acquire(fixture, "worker-rename")
        submission = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        self.assertEqual("committed", self.transition_result(fixture, submission, {"candidate": candidate})["status"])
        self.assertEqual("content-present", self.integration_leaf(fixture, "released")["presence"])
        self.assertEqual("content-not-present", self.integration_leaf(fixture, base)["presence"])

    def test_directly_closed_item_has_no_reviewed_candidate_to_integrate(self) -> None:
        fixture = self.checkpoint_fixture()
        self.close_prerequisite(fixture)
        closed = self.integration_leaf(fixture, "codex/work-a", item_id="work-c")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", closed["code"], closed)
        self.assertIn("'close'", str(closed["message"]))

    def test_integration_names_a_damaged_checkpoint_receipt(self) -> None:
        fixture = self.accepted_package_fixture()
        self.update_receipt(fixture, self.latest_history_id(fixture), outcome_json="not json")
        damaged = self.integration_leaf(fixture, "codex/work-a")
        self.assertEqual(
            ("TRANSITION_RECEIPT_DAMAGED", "do-not-retry", "unchanged", "pinboard-mcp-item-status-result/v3"),
            (damaged["code"], damaged["retry"], damaged["effect"], damaged["schema"]),
            damaged,
        )

    def test_integration_rejects_a_project_root_outside_any_git_checkout(self) -> None:
        fixture = self.checkpoint_fixture()
        with tempfile.TemporaryDirectory() as directory:
            outside = call_advertised_tool(
                mcp_server.ITEM_STATUS_TOOL,
                {
                    "request": {
                        "project_root": directory,
                        "work_root": str(fixture.work),
                        "operation": "integration",
                        "item_id": "work-a",
                        "target": "codex/work-a",
                    }
                },
            )
        self.assertEqual(
            ("PROJECT_GIT_ROOT_UNAVAILABLE", "correct-input", "unchanged"),
            (outside["code"], outside["retry"], outside["effect"]),
            outside,
        )
        self.assertEqual(directory, self.json_object(self.json_array(outside["observed"])[0])["value"])

    def test_integration_reads_the_checkpoint_index_for_the_latest_acceptance(self) -> None:
        fixture = self.accepted_package_fixture()
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            plan = connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT history_id FROM transition_history
                WHERE subject_id = ? AND outcome_schema = 'checkpoint-acceptance/v2'
                ORDER BY history_id DESC LIMIT 1
                """,
                ("work-a-1",),
            ).fetchall()
        self.assertIn("checkpoint_history_by_subject", " ".join(str(row[-1]) for row in plan), plan)

    def test_integration_leaf_names_its_item_for_the_trace_override(self) -> None:
        selected = mcp_common.select_capture_item(
            Path("/unused"),
            None,
            {"request": {"operation": "integration", "item_id": "work-a", "target": "codex/work-a"}},
        )
        self.assertEqual("work-a", selected)

    def test_attempt_inspection_runs_no_git_read_for_relations(self) -> None:
        fixture = self.checkpoint_fixture()
        with patch("pinboard.adapters.files.root.observe_target_presence", side_effect=AssertionError("no Git read")):
            inspected = self.inspection(fixture)
        self.assertEqual("ok", inspected["status"], inspected)
