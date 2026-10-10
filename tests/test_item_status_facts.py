"""Item-status leaves report review verdicts, branch owners, closure facts, and damaged receipts."""

import contextlib
import hashlib
import json
import os
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

from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.files.models import AffectedViews, ViewRefreshResult
from pinboard.adapters.sqlite import database as sqlite_database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import queries, query_models
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

    def integration_leaf(self, fixture: CheckpointFixture, target: str = "HEAD", item_id: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    **self.roots(fixture),
                    "operation": "integration",
                    "item_id": item_id,
                    "target": target,
                }
            },
        )

    def fixed_commit(self, project: Path, message: str) -> str:
        date = "2001-02-03T04:05:06+00:00"
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        subprocess.run(["git", "add", "tracked.txt"], cwd=project, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", message],
            cwd=project,
            check=True,
            capture_output=True,
            env=env,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=project, check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_integration_leaf_checks_protected_content_after_nonoverlapping_and_overlapping_edits(self) -> None:
        fixture = self.checkpoint_fixture()
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=fixture.project, check=True, capture_output=True, text=True
        ).stdout.strip()
        absent = self.integration_leaf(fixture, "HEAD")
        self.assertEqual("pinboard-item-integration/v1", absent["schema"])
        self.assertEqual("content-not-present", absent["presence"])
        self.assertEqual("HEAD", absent["target"])
        self.assertEqual(base, absent["resolved_target_revision"])
        self.assertEqual("protected-review", self.json_object(absent["source"])["kind"])
        self.assertEqual(fixture.candidate_revision, self.json_object(absent["source"])["candidate_revision"])
        integrated = self.fixed_commit(fixture.project, "squash candidate")
        present = self.integration_leaf(fixture, "HEAD")
        self.assertEqual("content-present", present["presence"])
        self.assertEqual(integrated, present["resolved_target_revision"])
        with (fixture.project / "tracked.txt").open("a", encoding="utf-8") as stream:
            stream.write("later non-overlapping edit\n")
        self.fixed_commit(fixture.project, "later non-overlapping edit")
        self.assertEqual("content-present", self.integration_leaf(fixture)["presence"])
        (fixture.project / "tracked.txt").write_text("overlapping replacement\n", encoding="utf-8")
        self.fixed_commit(fixture.project, "later overlapping edit")
        self.assertEqual("content-not-present", self.integration_leaf(fixture)["presence"])

    def test_integration_leaf_recognizes_fast_forward_merge_rebase_and_squash_trees(self) -> None:
        for integration in ("fast-forward", "merge-commit", "rebase-merge", "squash"):
            with self.subTest(integration=integration):
                fixture = self.checkpoint_fixture(candidate_form="current-head")
                base = fixture.brief.base_revision
                candidate = fixture.candidate_revision
                self.assertEqual(
                    candidate,
                    subprocess.run(
                        ["git", "rev-parse", "HEAD"], cwd=fixture.project, check=True, capture_output=True, text=True
                    ).stdout.strip(),
                )
                subprocess.run(["git", "branch", "main", base], cwd=fixture.project, check=True, capture_output=True)
                if integration == "rebase-merge":
                    (fixture.project / "context.txt").write_text("unrelated target history\n", encoding="utf-8")
                    target_base = self.commit_all(fixture.project, "target context")
                    subprocess.run(
                        ["git", "switch", "codex/work-a"], cwd=fixture.project, check=True, capture_output=True
                    )
                    subprocess.run(
                        ["git", "rebase", "main"],
                        cwd=fixture.project,
                        check=True,
                        capture_output=True,
                        env={**os.environ, "GIT_COMMITTER_DATE": "2001-02-04T04:05:06+00:00"},
                    )
                    rebased = subprocess.run(
                        ["git", "rev-parse", "HEAD"], cwd=fixture.project, check=True, capture_output=True, text=True
                    ).stdout.strip()
                    self.assertNotEqual(candidate, rebased)
                    subprocess.run(["git", "switch", "main"], cwd=fixture.project, check=True, capture_output=True)
                    subprocess.run(
                        ["git", "merge", "--ff-only", "codex/work-a"],
                        cwd=fixture.project,
                        check=True,
                        capture_output=True,
                    )
                    self.assertNotEqual(base, target_base)
                else:
                    subprocess.run(["git", "switch", "main"], cwd=fixture.project, check=True, capture_output=True)
                    if integration == "merge-commit":
                        subprocess.run(
                            ["git", "merge", "--no-ff", "codex/work-a", "-m", "merge candidate"],
                            cwd=fixture.project,
                            check=True,
                            capture_output=True,
                            env={
                                **os.environ,
                                "GIT_AUTHOR_DATE": "2001-02-04T04:05:06+00:00",
                                "GIT_COMMITTER_DATE": "2001-02-04T04:05:06+00:00",
                            },
                        )
                    elif integration == "squash":
                        subprocess.run(
                            ["git", "merge", "--squash", "codex/work-a"],
                            cwd=fixture.project,
                            check=True,
                            capture_output=True,
                        )
                        self.fixed_commit(fixture.project, "squash candidate")
                    else:
                        subprocess.run(
                            ["git", "merge", "--ff-only", "codex/work-a"],
                            cwd=fixture.project,
                            check=True,
                            capture_output=True,
                        )
                target = self.integration_leaf(fixture, "main")
                self.assertEqual("content-present", target["presence"])
                self.assertEqual("main", target["target"])
                resolved = target["resolved_target_revision"]
                self.assertEqual(
                    resolved,
                    subprocess.run(
                        ["git", "rev-parse", "main"], cwd=fixture.project, check=True, capture_output=True, text=True
                    ).stdout.strip(),
                )

    def test_integration_leaf_reads_local_remote_tracking_refs_and_empty_diffs(self) -> None:
        fixture = self.checkpoint_fixture()
        current = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=fixture.project, check=True, capture_output=True, text=True
        ).stdout.strip()
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", current],
            cwd=fixture.project,
            check=True,
            capture_output=True,
        )
        remote_tracking = self.integration_leaf(fixture, "origin/main")
        self.assertEqual("origin/main", remote_tracking["target"])
        self.assertEqual(current, remote_tracking["resolved_target_revision"])
        empty = self.checkpoint_fixture(empty_diff=True)
        result = self.integration_leaf(empty)
        self.assertEqual("no-change", result["presence"])

    def test_integration_leaf_selects_accepted_checkpoint_and_completion_sources(self) -> None:
        fixture = self.accepted_package_fixture()
        checkpoint = self.integration_leaf(fixture)
        self.assertEqual("accepted-checkpoint", self.json_object(checkpoint["source"])["kind"])
        self.assertEqual(
            fixture.brief.checkpoint.checkpoint_id, self.json_object(checkpoint["source"])["checkpoint_id"]
        )
        self.close_prerequisite(fixture)
        resume = self.project_action(fixture, "resume:work-a")
        resumed = self.transition_result(fixture, resume, {})
        self.assertEqual("committed", resumed["status"], resumed)
        resumed_checkpoint = self.integration_leaf(fixture)
        self.assertEqual("accepted-checkpoint", self.json_object(resumed_checkpoint["source"])["kind"])
        completed_fixture = self.checkpoint_fixture(candidate_form="current-head")
        self.complete(completed_fixture, "Completed candidate retained for integration check.")
        completed = self.integration_leaf(completed_fixture)
        self.assertEqual("completion", self.json_object(completed["source"])["kind"])
        self.assertEqual("content-present", completed["presence"])

    def test_integration_candidate_read_uses_keyed_and_indexed_queries(self) -> None:
        fixture = self.accepted_package_fixture()
        statements: list[str] = []
        original_open = sqlite_database.open_database

        def traced_open(database_path: Path, mode: sqlite_database.OpenMode) -> sqlite3.Connection:
            connection = original_open(database_path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with patch("pinboard.adapters.sqlite.store.open_database", side_effect=traced_open):
            facts = fixture.store.read_integration_candidate_facts(WorkItemId("work-a"))
        self.assertIsNotNone(facts)
        assert facts is not None
        self.assertIsNotNone(facts.latest_checkpoint)
        queries = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
        with sqlite3.connect(fixture.work / "state.sqlite3") as connection:
            plans = [
                " ".join(str(cell) for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}") for cell in row)
                for statement in queries
            ]
        self.assertTrue(any("SEARCH work_items" in plan for plan in plans), plans)
        self.assertTrue(any("SEARCH attempts" in plan for plan in plans), plans)
        self.assertTrue(any("checkpoint_history_by_subject" in plan for plan in plans), plans)
        self.assertFalse(any("SCAN transition_history" in plan for plan in plans), plans)

    def test_integration_leaf_rejections_name_target_item_and_damaged_candidate(self) -> None:  # noqa: PLR0915 - one native outcome sequence covers the named rejection contracts
        fixture = self.checkpoint_fixture()
        ready_without_candidate = self.integration_leaf(fixture, "HEAD", "work-c")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", ready_without_candidate["code"])
        self.assertEqual("unchanged", ready_without_candidate["effect"])
        self.assertEqual("correct-input", ready_without_candidate["retry"])
        unavailable_step = ready_without_candidate["next_step"]
        self.assertIsInstance(unavailable_step, str)
        self.assertIn("operation item", unavailable_step)
        self.close_prerequisite(fixture)
        directly_closed = self.integration_leaf(fixture, "HEAD", "work-c")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", directly_closed["code"])
        direct_close_message = directly_closed["message"]
        self.assertIsInstance(direct_close_message, str)
        self.assertIn("directly", direct_close_message)
        active = self.checkpoint_fixture()
        self.return_for_review(active, "No protected candidate remains for this active attempt.")
        no_current_candidate = self.integration_leaf(active)
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", no_current_candidate["code"])
        self.assertEqual("unchanged", no_current_candidate["effect"])
        self.assertEqual("correct-input", no_current_candidate["retry"])
        invalid_target = call_native_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": "work-a", "target": "-bad"}},
        )
        self.assertEqual("ITEM_STATUS_INVALID", invalid_target["code"])
        with tempfile.TemporaryDirectory() as non_git_directory:
            non_git = call_native_tool(
                mcp_server.ITEM_STATUS_TOOL,
                {
                    "request": {
                        "project_root": non_git_directory,
                        "work_root": str(fixture.work),
                        "operation": "integration",
                        "item_id": "work-a",
                        "target": "HEAD",
                    }
                },
            )
        self.assertEqual("PROJECT_GIT_ROOT_UNAVAILABLE", non_git["code"])
        self.assertEqual("correct-input", non_git["retry"])
        observed = self.json_array(non_git["observed"])
        self.assertIn("git_diagnostic", [self.json_object(fact)["field"] for fact in observed])
        missing_target = self.integration_leaf(fixture, "missing-target")
        self.assertEqual("pinboard-mcp-item-status-result/v3", missing_target["schema"])
        self.assertEqual("INTEGRATION_TARGET_UNRESOLVED", missing_target["code"])
        self.assertEqual("unchanged", missing_target["effect"])
        self.assertEqual("correct-input", missing_target["retry"])
        self.assertEqual("missing-target", self.json_object(self.json_array(missing_target["observed"])[0])["value"])
        next_step = missing_target["next_step"]
        self.assertIsInstance(next_step, str)
        self.assertIn("fetch outside Pinboard", next_step)
        missing_item = self.integration_leaf(fixture, "HEAD", "missing-item")
        self.assertEqual("ITEM_NOT_FOUND", missing_item["code"])
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        self.assertIsNotNone(context)
        assert context is not None
        artifact_path = fixture.work / context.reference.selector
        accepted_bytes = artifact_path.read_bytes()
        artifact_path.write_bytes(accepted_bytes + b"damaged")
        damaged = self.integration_leaf(fixture)
        self.assertEqual("INTEGRATION_CANDIDATE_EVIDENCE_INVALID", damaged["code"])
        self.assertEqual("do-not-retry", damaged["retry"])
        next_step = damaged["next_step"]
        self.assertIsInstance(next_step, str)
        self.assertIn("pinboard validate", next_step)

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
