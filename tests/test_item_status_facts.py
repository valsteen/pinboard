"""Item-status leaves report review verdicts, branch owners, closure facts, and damaged receipts."""

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import assert_never
from unittest.mock import patch

import msgspec
from mcp.server.mcpserver.exceptions import ToolError
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files import root as root_module
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.files.models import AffectedViews, ViewRefreshResult
from pinboard.adapters.sqlite import store as store_module
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import candidate_snapshots, queries, query_models
from pinboard.application.ports import GeneratedViewReader
from pinboard.domain import decision_models
from pinboard.domain.identifiers import AttemptId, HistoryId, WorkItemId
from pinboard.mcp import common as mcp_common
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import JsonObject, NoReadyCandidateReviews

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

    def submit_candidate(self, fixture: CheckpointFixture, label: str) -> str:
        (fixture.project / "tracked.txt").write_text(f"{label}\n", encoding="utf-8")
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

    def complete(self, fixture: CheckpointFixture, evidence: str) -> None:
        fixture = self.terminalize_brief(fixture)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        with patch("pinboard.mcp.mutation_operations.datetime") as clock:
            clock.now.return_value = COMPLETED_AT
            self.transition(
                fixture,
                "complete:work-a-1",
                {
                    "schema": "pinboard-reviewed-completion/v2",
                    "candidate": fixture.candidate_revision,
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

    def git_output(self, fixture: CheckpointFixture, *arguments: str, environment: dict[str, str] | None = None) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=fixture.project,
            check=True,
            text=True,
            capture_output=True,
            env={**os.environ, **(environment or {})},
        ).stdout.strip()

    def target_commit(
        self,
        fixture: CheckpointFixture,
        parents: tuple[str, ...],
        tree_from: str,
        files: dict[str, str],
        ref: str,
    ) -> str:
        """Create one dated commit without touching the checkout, naming it by a full ref."""

        with tempfile.TemporaryDirectory() as directory:
            fixed = {
                "GIT_INDEX_FILE": str(Path(directory) / "index"),
                "GIT_AUTHOR_NAME": "Pinboard Tests",
                "GIT_AUTHOR_EMAIL": "pinboard@example.invalid",
                "GIT_COMMITTER_NAME": "Pinboard Tests",
                "GIT_COMMITTER_EMAIL": "pinboard@example.invalid",
                "GIT_AUTHOR_DATE": "2030-01-02T03:04:05+00:00",
                "GIT_COMMITTER_DATE": "2030-01-02T03:04:05+00:00",
            }
            self.git_output(fixture, "read-tree", tree_from, environment=fixed)
            for path, content in files.items():
                blob = subprocess.run(
                    ["git", "hash-object", "-w", "--stdin"],
                    cwd=fixture.project,
                    input=content,
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout.strip()
                self.git_output(
                    fixture, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", environment=fixed
                )
            tree = self.git_output(fixture, "write-tree", environment=fixed)
            parent_arguments = [argument for parent in parents for argument in ("-p", parent)]
            commit = self.git_output(fixture, "commit-tree", tree, *parent_arguments, "-m", ref, environment=fixed)
        self.git_output(fixture, "update-ref", ref, commit)
        return commit

    def assert_integration(
        self,
        fixture: CheckpointFixture,
        target: str,
        resolved: str,
        presence: str,
        source: JsonObject,
        item_id: str = "work-a",
    ) -> None:
        result = self.integration_leaf(fixture, target, item_id)
        revision = result.pop("revision")
        self.assertIsInstance(revision, str)
        self.assertEqual(
            {
                "schema": "pinboard-item-integration/v1",
                "authority": "sqlite-v7",
                "item_id": item_id,
                "target": target,
                "resolved_revision": resolved,
                "source": source,
                "presence": presence,
            },
            result,
        )

    def assert_integration_rejection(self, result: JsonObject, code: str, retry: str) -> JsonObject:
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual(
            (code, "rejected", False, "unchanged", retry, []),
            tuple(result[key] for key in ("code", "status", "state_changed", "effect", "retry", "changed_surfaces")),
            result,
        )
        self.assertTrue(result["recovery"], result)
        return result

    def test_integration_leaf_reads_a_protected_working_tree_candidate_by_content(self) -> None:
        base_text = "".join(f"line {number}\n" for number in range(1, 11))
        candidate_text = base_text.replace("line 5\n", "line five\n")
        fixture = self.checkpoint_fixture(base_text=base_text, candidate_text=candidate_text)
        base = fixture.brief.base_revision
        base_tree = f"{base}^{{tree}}"
        squash = self.target_commit(fixture, (base,), base_tree, {"tracked.txt": candidate_text}, "refs/heads/squashed")
        later = candidate_text.replace("line 1\n", "first\n").replace("line 10\n", "last\n")
        self.target_commit(fixture, (squash,), squash, {"tracked.txt": later}, "refs/heads/later")
        overlap = candidate_text.replace("line 4\n", "rewritten\n")
        self.target_commit(fixture, (squash,), squash, {"tracked.txt": overlap}, "refs/heads/overlap")
        self.git_output(fixture, "update-ref", "refs/heads/at-base", base)
        self.git_output(fixture, "update-ref", "refs/remotes/origin/main", squash)
        self.git_output(fixture, "tag", "released", squash)
        source: JsonObject = {
            "kind": "protected-review",
            "attempt_id": "work-a-1",
            "candidate_revision": fixture.candidate_revision,
            "compared_from_revision": base,
        }
        before = self.git_output(fixture, "status", "--porcelain=v1", "--untracked-files=all")
        for target, resolved, presence in (
            ("squashed", squash, "content-present"),
            ("origin/main", squash, "content-present"),
            ("released", squash, "content-present"),
            (squash, squash, "content-present"),
            ("at-base", base, "content-not-present"),
            ("later", self.git_output(fixture, "rev-parse", "later"), "content-present"),
            ("overlap", self.git_output(fixture, "rev-parse", "overlap"), "content-not-present"),
        ):
            with self.subTest(target=target):
                self.assert_integration(fixture, target, resolved, presence, source)
        self.assertEqual(before, self.git_output(fixture, "status", "--porcelain=v1", "--untracked-files=all"))

    def test_integration_leaf_reads_a_protected_commit_candidate_across_merge_styles(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        base, candidate = fixture.brief.base_revision, fixture.candidate_revision
        fast_forward = self.target_commit(fixture, (candidate,), candidate, {}, "refs/heads/fast-forward")
        other = self.target_commit(fixture, (base,), base, {"other.txt": "other\n"}, "refs/heads/other")
        merged = self.target_commit(
            fixture, (other, candidate), candidate, {"other.txt": "other\n"}, "refs/heads/merge-commit"
        )
        rebased = self.target_commit(
            fixture, (other,), other, {"tracked.txt": "candidate\n"}, "refs/heads/rebase-merge"
        )
        squashed = self.target_commit(fixture, (base,), base, {"tracked.txt": "candidate\n"}, "refs/heads/squash")
        self.assertNotEqual(
            0,
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", candidate, rebased], cwd=fixture.project, check=False
            ).returncode,
        )
        source: JsonObject = {
            "kind": "protected-review",
            "attempt_id": "work-a-1",
            "candidate_revision": candidate,
            "compared_from_revision": base,
        }
        for target, resolved in (
            ("fast-forward", fast_forward),
            ("merge-commit", merged),
            ("rebase-merge", rebased),
            ("squash", squashed),
        ):
            with self.subTest(target=target):
                self.assert_integration(fixture, target, resolved, "content-present", source)
        self.assert_integration(fixture, "other", other, "content-not-present", source)

    def test_integration_leaf_reports_no_change_for_an_empty_recorded_diff(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        snapshot = candidate_snapshots.decode_candidate_snapshot(
            (fixture.work / context.reference.selector).read_bytes()
        )
        empty = candidate_snapshots.canonical_candidate_snapshot_bytes(replace_struct(snapshot, diff=b""))
        self.replace_artifact_bytes(fixture, context.reference, empty)
        base = fixture.brief.base_revision
        self.assert_integration(
            fixture,
            "HEAD",
            fixture.candidate_revision,
            "no-change",
            {
                "kind": "protected-review",
                "attempt_id": "work-a-1",
                "candidate_revision": fixture.candidate_revision,
                "compared_from_revision": base,
            },
        )

    def test_integration_leaf_follows_the_candidate_through_the_lifecycle(self) -> None:
        fixture = self.checkpoint_fixture()
        base = fixture.brief.base_revision
        self.target_commit(fixture, (base,), f"{base}^{{tree}}", {"tracked.txt": "candidate\n"}, "refs/heads/squashed")
        squash = self.git_output(fixture, "rev-parse", "squashed")
        protected: JsonObject = {
            "kind": "protected-review",
            "attempt_id": "work-a-1",
            "candidate_revision": fixture.candidate_revision,
            "compared_from_revision": base,
        }
        self.assert_integration(fixture, "squashed", squash, "content-present", protected)
        self.return_for_review(fixture, "Rework the candidate.")
        unavailable = self.assert_integration_rejection(
            self.integration_leaf(fixture, "squashed"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"
        )
        self.assertEqual(
            [("item_id", "work-a"), ("item_state", "active"), ("reason", "no-protected-candidate-or-checkpoint")],
            [
                (fact["field"], fact["value"])
                for fact in self.json_array(unavailable["observed"])
                if isinstance(fact, dict)
            ],
        )
        candidate = self.submit_candidate(fixture, "corrected")
        resubmitted = self.integration_leaf(fixture, "squashed")
        self.assertEqual("content-not-present", resubmitted["presence"])
        self.assertEqual(candidate, self.json_object(resubmitted["source"])["candidate_revision"])
        self.transition(
            fixture,
            "accept-review-and-continue:work-a-1",
            {"candidate": candidate, "evidence": "Accepted; continue the attempt."},
        )
        self.assert_integration_rejection(
            self.integration_leaf(fixture, "squashed"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"
        )

    def test_integration_leaf_reads_an_accepted_checkpoint_through_pause_resume_and_rebind(self) -> None:
        fixture = self.accepted_package_fixture()
        base = fixture.brief.base_revision
        squash = self.target_commit(
            fixture, (base,), f"{base}^{{tree}}", {"tracked.txt": "candidate\n"}, "refs/heads/squashed"
        )
        checkpoint: JsonObject = {
            "kind": "accepted-checkpoint",
            "attempt_id": "work-a-1",
            "candidate_revision": fixture.candidate_revision,
            "compared_from_revision": self.git_output(fixture, "rev-parse", "HEAD"),
            "checkpoint_id": fixture.brief.checkpoint.checkpoint_id,
        }
        self.assert_integration(fixture, "squashed", squash, "content-present", checkpoint)
        self.close_prerequisite(fixture)
        self.transition(fixture, "resume:work-a", {})
        self.assert_integration(fixture, "squashed", squash, "content-present", checkpoint)
        self.rebind(fixture, fixture.brief.branch, 3)
        self.assert_integration(fixture, "squashed", squash, "content-present", checkpoint)
        self.assert_valid(fixture)

    def test_integration_leaf_reads_a_completion_candidate_and_rejects_a_direct_close(self) -> None:
        fixture = self.checkpoint_fixture()
        base = fixture.brief.base_revision
        squash = self.target_commit(
            fixture, (base,), f"{base}^{{tree}}", {"tracked.txt": "candidate\n"}, "refs/heads/squashed"
        )
        self.close_prerequisite(fixture)
        self.complete(fixture, "Accepted and integrated by the maintainer.")
        self.assert_integration(
            fixture,
            "squashed",
            squash,
            "content-present",
            {
                "kind": "completion",
                "attempt_id": "work-a-1",
                "candidate_revision": fixture.candidate_revision,
                "compared_from_revision": base,
            },
        )
        closed = self.assert_integration_rejection(
            self.integration_leaf(fixture, "squashed", "work-c"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"
        )
        self.assertIn(
            ("reason", "not-completed"),
            [(fact["field"], fact["value"]) for fact in self.json_array(closed["observed"]) if isinstance(fact, dict)],
        )

    def test_integration_leaf_rejections_carry_their_facts_effect_retry_and_next_step(self) -> None:
        fixture = self.checkpoint_fixture()
        unresolved = self.assert_integration_rejection(
            self.integration_leaf(fixture, "no-such-ref"), "INTEGRATION_TARGET_UNRESOLVED", "correct-input"
        )
        self.assertEqual(
            [("target", "no-such-ref"), ("project_root", str(fixture.project))],
            [
                (fact["field"], fact["value"])
                for fact in self.json_array(unresolved["observed"])
                if isinstance(fact, dict)
            ],
        )
        self.assertIn("fetch it outside Pinboard", str(unresolved["recovery"]))
        option = self.integration_leaf(fixture, "--output=elsewhere")
        self.assertEqual(("ITEM_STATUS_INVALID", "correct-input"), (option["code"], option["retry"]))
        ready = self.assert_integration_rejection(
            self.integration_leaf(fixture, "HEAD", "work-c"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"
        )
        self.assertIn("item", str(ready["recovery"]))
        missing = self.integration_leaf(fixture, "HEAD", "no-such-item")
        self.assertEqual(("ITEM_NOT_FOUND", "pinboard-mcp-item-status-result/v3"), (missing["code"], missing["schema"]))
        outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        git_failure = self.assert_integration_rejection(
            call_advertised_tool(
                mcp_server.ITEM_STATUS_TOOL,
                {
                    "request": {
                        "project_root": str(outside),
                        "work_root": str(fixture.work),
                        "operation": "integration",
                        "item_id": "work-a",
                        "target": "HEAD",
                    }
                },
            ),
            "PROJECT_GIT_ROOT_UNAVAILABLE",
            "correct-input",
        )
        self.assertIn(
            ("project_root", str(outside)),
            [
                (fact["field"], fact["value"])
                for fact in self.json_array(git_failure["observed"])
                if isinstance(fact, dict)
            ],
        )
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        self.replace_artifact_bytes(fixture, context.reference, b"altered\n")
        invalid = self.assert_integration_rejection(
            self.integration_leaf(fixture, "HEAD"), "INTEGRATION_CANDIDATE_EVIDENCE_INVALID", "do-not-retry"
        )
        self.assertEqual(
            [("attempt_id", "work-a-1"), ("reference", context.reference.selector)],
            [(fact["field"], fact["value"]) for fact in self.json_array(invalid["observed"]) if isinstance(fact, dict)],
        )
        self.assertIn("pinboard validate", str(invalid["recovery"]))

    def test_integration_leaf_names_a_damaged_checkpoint_receipt_and_a_missing_candidate_snapshot(self) -> None:
        fixture = self.accepted_package_fixture()
        checkpoint = fixture.brief.checkpoint.checkpoint_id
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "DELETE FROM artifact_refs WHERE artifact_key = ?", (f"work-a-1-{checkpoint}-candidate",)
            )
        unavailable = self.assert_integration_rejection(
            self.integration_leaf(fixture, "HEAD"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"
        )
        self.assertIn(
            ("reason", "checkpoint-without-candidate-snapshot"),
            [
                (fact["field"], fact["value"])
                for fact in self.json_array(unavailable["observed"])
                if isinstance(fact, dict)
            ],
        )
        self.update_receipt(fixture, self.latest_history_id(fixture), outcome_json="not json")
        damaged = self.integration_leaf(fixture, "HEAD")
        self.assertEqual(("TRANSITION_RECEIPT_DAMAGED", "do-not-retry"), (damaged["code"], damaged["retry"]))

    def test_integration_leaf_stays_out_of_the_other_reads_and_inspection(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Rework the candidate.")
        candidate = self.submit_candidate(fixture, "reviewed")
        self.record_ready(fixture, candidate)
        statements: list[str] = []
        real_open = store_module.open_database

        def traced_open(path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = real_open(path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(store_module, "open_database", traced_open):
            self.integration_leaf(fixture, "HEAD")
        self.assertTrue(statements)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            for statement in statements:
                if statement.lstrip().upper().startswith("SELECT"):
                    plan = " ".join(str(row[3]) for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}"))
                    self.assertNotRegex(
                        plan, r"\bSCAN (attempts|transition_history|artifact_refs|work_items)\b", statement
                    )
        with patch.object(root_module, "read_target_content", side_effect=AssertionError("no Git read")):
            self.item_leaf(fixture)
            call_advertised_tool(mcp_server.OVERVIEW_TOOL, self.roots(fixture))
            reconciliation: JsonObject = {
                "target_revision": self.git_output(fixture, "rev-parse", "HEAD"),
                "relation": "candidate-integrated",
                "phase": "terminal",
                "effects": [
                    {"effect": "source-checkout", "status": "not-required"},
                    {"effect": "shared-work-root", "status": "allowed"},
                    {"effect": "git-metadata", "status": "not-required"},
                ],
            }
            inspected = call_advertised_tool(
                mcp_server.ATTEMPT_INSPECT_TOOL,
                {**self.roots(fixture), "attempt_id": "work-a-1", "reconciliation": reconciliation},
            )
        self.assertEqual("ok", inspected["status"], inspected)

    def test_trace_capture_selects_the_item_for_the_integration_leaf(self) -> None:
        fixture = self.checkpoint_fixture()
        arguments: JsonObject = {
            "request": {**self.roots(fixture), "operation": "integration", "item_id": "work-a", "target": "HEAD"}
        }
        item_arguments: JsonObject = {"request": {**self.roots(fixture), "operation": "item", "item_id": "work-a"}}
        selected = [
            mcp_common.select_capture_item(fixture.project, str(fixture.work), value)
            for value in (arguments, item_arguments)
        ]
        self.assertEqual(["work-a", "work-a"], selected)

    def test_integration_leaf_names_a_git_failure_while_reading_the_target_content(self) -> None:
        fixture = self.checkpoint_fixture()
        base = fixture.brief.base_revision
        broken = self.target_commit(
            fixture, (base,), f"{base}^{{tree}}", {"tracked.txt": "unique\n"}, "refs/heads/broken"
        )
        tree = self.git_output(fixture, "rev-parse", f"{broken}^{{tree}}")
        (fixture.project / ".git" / "objects" / tree[:2] / tree[2:]).unlink()
        failed = self.assert_integration_rejection(
            self.integration_leaf(fixture, "broken"), "PROJECT_GIT_CHECKOUT_UNAVAILABLE", "correct-input"
        )
        self.assertEqual(
            [("project_root", str(fixture.project)), ("target", "broken")],
            [(fact["field"], fact["value"]) for fact in self.json_array(failed["observed"]) if isinstance(fact, dict)],
        )
        self.assertIn(str(fixture.project), str(failed["recovery"]))

    def test_integration_leaf_reports_a_candidate_that_predates_snapshots_as_unavailable(self) -> None:
        fixture = self.checkpoint_fixture()
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE transition_history SET input_schema = 'decision/v1', input_json = '{}', "
                "artifact_ref_id = NULL, artifact_kind = NULL WHERE action_kind = 'submit-review'"
            )
            connection.execute("DELETE FROM artifact_refs WHERE artifact_key LIKE '%-candidate-snapshot-%'")
            connection.execute(
                "UPDATE attempts SET subject_revision = "
                "(SELECT project_revision FROM transition_history WHERE action_kind = 'submit-review') "
                "WHERE attempt_id = 'work-a-1'"
            )
        unavailable = self.assert_integration_rejection(
            self.integration_leaf(fixture, "HEAD"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"
        )
        self.assertIn(
            ("reason", "pre-snapshot-candidate"),
            [
                (fact["field"], fact["value"])
                for fact in self.json_array(unavailable["observed"])
                if isinstance(fact, dict)
            ],
        )
