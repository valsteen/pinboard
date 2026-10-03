"""Shared item-status leaf helpers for fixtures built through real native transitions."""

import contextlib
import hashlib
import sqlite3
from datetime import UTC, datetime
from unittest.mock import patch

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.files.models import AffectedViews, ViewRefreshResult
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import queries, query_models
from pinboard.application.ports import GeneratedViewReader
from pinboard.domain.identifiers import AttemptId, WorkItemId
from pinboard.mcp import common as mcp_common
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import JsonObject, NoReadyCandidateReviews

CLOSED_AT = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
COMPLETED_AT = datetime(2030, 1, 3, 4, 5, 6, tzinfo=UTC)


class ItemStatusSupport(CheckpointPackageSupport):
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
