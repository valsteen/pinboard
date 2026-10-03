"""Item-status leaves report review verdicts, branch owners, closure facts, and damaged receipts."""

import contextlib
import hashlib
import json
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
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import checkpoint_compatibility_models, queries, query_models, work_briefs
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

    def integration_leaf(self, fixture: CheckpointFixture, target: str, item_id: str = "work-a") -> JsonObject:
        database = fixture.work / "state.sqlite3"
        before = database.read_bytes()
        result = call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": item_id, "target": target}},
        )
        self.assertEqual(before, database.read_bytes())
        return result

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

    def assert_integration_rejection(
        self,
        result: JsonObject,
        code: str,
        retry: str,
        expected_observed: dict[str, str],
        next_step_fragment: str,
    ) -> None:
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual(("rejected", code), (result["status"], result["code"]))
        self.assertFalse(result["state_changed"])
        self.assertEqual("unchanged", result["effect"])
        self.assertEqual(retry, result["retry"])
        self.assertEqual([], result["changed_surfaces"])
        observed = {
            self.json_object(value)["field"]: self.json_object(value)["value"]
            for value in self.json_array(result["observed"])
        }
        for field, value in expected_observed.items():
            self.assertEqual(value, observed[field], result)
        next_step = result["next_step"]
        assert isinstance(next_step, str)
        self.assertIn(next_step_fragment, next_step)
        self.assertIsInstance(result["mismatches"], list)

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

    def test_integration_leaf_observes_candidate_content_without_ancestry(self) -> None:  # noqa: PLR0915 - one native candidate and integration-relation journey
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Prepare a multi-line candidate.")
        tracked = fixture.project / "tracked.txt"
        tracked.write_text("base\n" + "\n".join(f"stable-{line}" for line in range(1, 13)) + "\n", encoding="utf-8")
        self.commit_all(fixture.project, "expand reviewed file")
        compared_from = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=fixture.project, check=True, capture_output=True, text=True
        ).stdout.strip()
        tracked.write_text(
            "candidate\n" + "\n".join(f"stable-{line}" for line in range(1, 13)) + "\n", encoding="utf-8"
        )
        observed = call_native_tool(
            mcp_server.CANDIDATE_OBSERVE_TOOL,
            {**self.roots(fixture), "attempt_id": "work-a-1"},
        )
        candidate = observed["candidate"]
        assert isinstance(candidate, str)
        lease = self.native_attempt_acquire(fixture, "integration-worker")
        selected = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        submitted = self.transition_result(fixture, selected, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)

        absent = self.integration_leaf(fixture, fixture.brief.branch)
        self.assertEqual("ok", absent["status"], absent)
        self.assertEqual("content-not-present", absent.get("presence"))
        self.assertEqual(compared_from, self.json_object(absent["source"])["compared_from_revision"])

        candidate_commit = self.commit_all(fixture.project, "commit reviewed candidate")
        present = self.integration_leaf(fixture, fixture.brief.branch)
        self.assertEqual("ok", present["status"], present)
        self.assertEqual("content-present", present["presence"])
        self.assertEqual(candidate_commit, present["target_revision"])
        self.assertEqual(candidate, self.json_object(present["source"])["candidate_revision"])
        self.assertEqual("protected-review", self.json_object(present["source"])["kind"])

        def git(*arguments: str) -> str:
            return subprocess.run(
                ["git", *arguments],
                cwd=fixture.project,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        git("branch", "ff-target", compared_from)
        git("switch", "ff-target")
        git("merge", "--ff-only", fixture.brief.branch)
        fast_forward = self.integration_leaf(fixture, "ff-target")
        self.assertEqual("content-present", fast_forward.get("presence"), fast_forward)

        git("branch", "merge-target", compared_from)
        git("switch", "merge-target")
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Pinboard Tests",
                "-c",
                "user.email=pinboard@example.invalid",
                "merge",
                "--no-ff",
                fixture.brief.branch,
                "-m",
                "merge reviewed candidate",
            ],
            cwd=fixture.project,
            check=True,
            capture_output=True,
        )
        merge_commit = self.integration_leaf(fixture, "merge-target")
        self.assertEqual("content-present", merge_commit.get("presence"), merge_commit)

        git("branch", "rebase-target", compared_from)
        git("switch", "rebase-target")
        (fixture.project / "surrounding.txt").write_text("target context\n", encoding="utf-8")
        self.commit_all(fixture.project, "target context before rebase")
        git("switch", fixture.brief.branch)
        git("rebase", "rebase-target")
        rebased_candidate = git("rev-parse", "HEAD")
        self.assertNotEqual(
            0,
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", candidate_commit, rebased_candidate],
                cwd=fixture.project,
                capture_output=True,
            ).returncode,
        )
        git("switch", "rebase-target")
        git("merge", "--ff-only", fixture.brief.branch)
        rebase_merge = self.integration_leaf(fixture, "rebase-target")
        self.assertEqual("content-present", rebase_merge.get("presence"), rebase_merge)

        git("branch", "squash-target", compared_from)
        git("switch", "squash-target")
        subprocess.run(
            ["git", "merge", "--squash", fixture.brief.branch],
            cwd=fixture.project,
            check=True,
            capture_output=True,
        )
        self.commit_all(fixture.project, "squash reviewed candidate")
        squash_merge = self.integration_leaf(fixture, "squash-target")
        self.assertEqual("content-present", squash_merge.get("presence"), squash_merge)

        git("switch", fixture.brief.branch)

        tracked.write_text(
            "candidate\n" + "\n".join(f"stable-{line}" for line in range(1, 12)) + "\nchanged-12\n",
            encoding="utf-8",
        )
        nonoverlap_commit = self.commit_all(fixture.project, "edit an unrelated reviewed-file line")
        nonoverlap = self.integration_leaf(fixture, fixture.brief.branch)
        self.assertEqual("content-present", nonoverlap.get("presence"), nonoverlap)
        self.assertEqual(nonoverlap_commit, nonoverlap["target_revision"])

        tracked.write_text(
            "overlapping\n" + "\n".join(f"stable-{line}" for line in range(1, 12)) + "\nchanged-12\n",
            encoding="utf-8",
        )
        overlap_commit = self.commit_all(fixture.project, "edit the reviewed line")
        overlap = self.integration_leaf(fixture, fixture.brief.branch)
        self.assertEqual("content-not-present", overlap.get("presence"), overlap)
        self.assertEqual(overlap_commit, overlap["target_revision"])

        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", overlap_commit],
            cwd=fixture.project,
            check=True,
            capture_output=True,
        )
        remote_name = self.integration_leaf(fixture, "origin/main")
        self.assertEqual("content-not-present", remote_name.get("presence"), remote_name)
        self.assertEqual("origin/main", remote_name["target"])

    def test_integration_leaf_selects_latest_checkpoint_after_resume(self) -> None:
        fixture = self.accepted_package_fixture()
        self.close_prerequisite(fixture)
        target = "integration-target"
        self.git_at_fixed_date(fixture.project, "branch", target, fixture.brief.base_revision)
        candidate_commit = self.commit_all(fixture.project, "integrate accepted checkpoint")
        self.git_at_fixed_date(fixture.project, "switch", target)
        self.git_at_fixed_date(fixture.project, "merge", "--squash", candidate_commit)
        self.git_at_fixed_date(fixture.project, "commit", "-m", "squash accepted checkpoint")
        target_commit = self.git_at_fixed_date(fixture.project, "rev-parse", "HEAD")
        before_resume = self.integration_leaf(fixture, target)
        self.assertEqual("ok", before_resume["status"], before_resume)
        self.assertEqual("content-present", before_resume["presence"])
        self.transition(fixture, "resume:work-a", {})
        result = self.integration_leaf(fixture, target)

        self.assertEqual("ok", result["status"], result)
        self.assertEqual("content-present", result["presence"])
        self.assertEqual(target_commit, result["target_revision"])
        source = self.json_object(result["source"])
        self.assertEqual("accepted-checkpoint", source["kind"])
        self.assertEqual("work-a-1", source["attempt_id"])
        self.assertEqual(fixture.candidate_revision, source["candidate_revision"])
        self.assertEqual(fixture.brief.checkpoint.checkpoint_id, source["checkpoint_id"])
        with sqlite3.connect(fixture.work / "state.sqlite3") as connection:
            checkpoint_plan = " ".join(
                str(row[3]).upper()
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN SELECT history_id FROM transition_history "
                    "WHERE subject_id = ? AND outcome_schema = 'checkpoint-acceptance/v2' "
                    "ORDER BY history_id DESC LIMIT 1",
                    ("work-a-1",),
                )
            )
            artifact_plan = " ".join(
                str(row[3]).upper()
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN SELECT artifact_ref_id FROM artifact_refs "
                    "WHERE kind = 'evidence' AND artifact_key = ? AND artifact_revision = ?",
                    ("candidate", 1),
                )
            )
        self.assertIn("CHECKPOINT_HISTORY_BY_SUBJECT", checkpoint_plan)
        self.assertIn("SEARCH ARTIFACT_REFS", artifact_plan)

    def test_integration_leaf_rejects_checkpoint_packages_without_snapshot_identity(self) -> None:
        fixture = self.accepted_package_fixture()
        package_path = fixture.work / fixture.package_reference.selector
        package_json = json.loads(package_path.read_bytes())
        assert isinstance(package_json, dict)
        package_json["schema"] = "pinboard-checkpoint-review-package/v1"
        package_json.pop("candidate_snapshot")
        package = msgspec.convert(
            package_json, type=checkpoint_compatibility_models.CheckpointReviewPackage, strict=True
        )
        package_bytes = work_briefs.canonical_checkpoint_review_package_bytes(package)
        package_path.write_bytes(package_bytes)
        with sqlite3.connect(fixture.work / "state.sqlite3") as connection:
            connection.execute(
                "UPDATE artifact_refs SET content_sha256 = ?, size_bytes = ? WHERE artifact_ref_id = ?",
                (
                    hashlib.sha256(package_bytes).hexdigest(),
                    len(package_bytes),
                    fixture.package_reference.artifact_ref_id,
                ),
            )

        result = self.integration_leaf(fixture, fixture.brief.branch)

        self.assert_integration_rejection(
            result,
            "INTEGRATION_CANDIDATE_UNAVAILABLE",
            "correct-input",
            {"item_id": "work-a", "item_state": "paused"},
            "Read the item",
        )
        message = result["message"]
        assert isinstance(message, str)
        self.assertIn("no candidate snapshot reference", message)

    def test_integration_leaf_does_not_read_unrelated_retained_attempts_receipts_or_artifacts(self) -> None:
        fixture = self.accepted_package_fixture()
        database = fixture.work / "state.sqlite3"
        with sqlite3.connect(database) as connection:
            attempt = connection.execute(
                "SELECT brief_artifact_ref_id, accepted_scope_revision, accepted_scope_digest FROM attempts "
                "WHERE attempt_id = 'work-a-1'"
            ).fetchone()
            project_revision = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()[0]
            assert attempt is not None and isinstance(project_revision, int)
            for number in range(24):
                connection.execute(
                    "INSERT INTO attempts (attempt_id, item_id, state, branch, base_revision, provenance, "
                    "brief_artifact_ref_id, brief_artifact_kind, candidate_revision, candidate_recorded_at, accepted_scope_revision, "
                    "accepted_scope_digest, subject_revision, recorded_at, updated_at) "
                    "VALUES (?, 'work-c', 'done', ?, 'base', 'dispatched', ?, 'brief', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"retained-attempt-{number}",
                        f"retained-branch-{number}",
                        attempt[0],
                        f"retained-candidate-{number}",
                        "2000-01-01T00:00:00+00:00",
                        attempt[1],
                        attempt[2],
                        project_revision + number + 1,
                        "2000-01-01T00:00:00+00:00",
                        "2000-01-01T00:00:00+00:00",
                    ),
                )
                connection.execute(
                    "INSERT INTO transition_history (project_revision, action_id, action_kind, subject_id, "
                    "authorization_kind, input_schema, input_json, outcome_schema, outcome_json, committed_at) "
                    "VALUES (?, ?, 'submit-review', ?, 'project', 'decision/v1', '{}', "
                    "'transition-receipt/v1', '{}', '2000-01-01T00:00:00+00:00')",
                    (
                        project_revision + number + 1,
                        f"retained-submit-{number}",
                        f"retained-attempt-{number}",
                    ),
                )
                connection.execute(
                    "INSERT INTO artifact_refs (artifact_key, artifact_revision, kind, relative_path, "
                    "content_sha256, size_bytes, accepted_revision, created_at) "
                    "VALUES (?, 1, 'evidence', ?, ?, 0, ?, '2000-01-01T00:00:00+00:00')",
                    (
                        f"retained-artifact-{number}",
                        f"artifacts/evidence/retained-{number}.json",
                        "0" * 64,
                        project_revision + number + 1,
                    ),
                )

        statements: list[str] = []
        open_database = sqlite_store.open_database

        def traced_open(path: Path, mode: sqlite_store.OpenMode) -> sqlite3.Connection:
            connection = open_database(path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(sqlite_store, "open_database", side_effect=traced_open):
            result = self.integration_leaf(fixture, fixture.brief.branch)

        self.assertEqual("ok", result["status"], result)
        executed = "\n".join(statements).lower()
        self.assertNotIn("retained-attempt", executed)
        self.assertNotIn("retained-submit", executed)
        self.assertNotIn("retained-artifact", executed)
        for table in ("attempts", "transition_history", "artifact_refs"):
            selects = [statement for statement in statements if f"from {table}" in statement.lower()]
            self.assertTrue(selects, table)
            self.assertTrue(all(" where " in statement.lower() for statement in selects), selects)

    def test_integration_leaf_accepts_commit_candidates_after_fast_forward_merge_and_rebase(self) -> None:
        for strategy in ("fast-forward", "merge-commit", "rebase"):
            with self.subTest(strategy=strategy):
                fixture = self.checkpoint_fixture()
                self.return_for_review(fixture, f"Submit a commit candidate for {strategy}.")
                target = "integration-target"
                self.git_at_fixed_date(fixture.project, "branch", target, fixture.brief.base_revision)
                if strategy == "rebase":
                    self.git_at_fixed_date(fixture.project, "switch", target)
                    (fixture.project / "target-only.txt").write_text("target change\n", encoding="utf-8")
                    self.git_at_fixed_date(fixture.project, "add", "target-only.txt")
                    self.git_at_fixed_date(
                        fixture.project,
                        "-c",
                        "user.name=Pinboard Tests",
                        "-c",
                        "user.email=pinboard@example.invalid",
                        "commit",
                        "-m",
                        "advance target",
                    )
                    self.git_at_fixed_date(fixture.project, "switch", fixture.brief.branch)
                candidate = self.commit_all(fixture.project, f"commit candidate for {strategy}")
                lease = self.native_attempt_acquire(fixture, f"commit-candidate-{strategy}")
                selected = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
                submitted = self.transition_result(fixture, selected, {"candidate": candidate})
                self.assertEqual("committed", submitted["status"], submitted)

                if strategy == "fast-forward":
                    self.git_at_fixed_date(fixture.project, "update-ref", f"refs/heads/{target}", candidate)
                elif strategy == "merge-commit":
                    self.git_at_fixed_date(fixture.project, "switch", target)
                    self.git_at_fixed_date(fixture.project, "merge", "--no-ff", "--no-edit", candidate)
                else:
                    self.git_at_fixed_date(fixture.project, "rebase", target)
                    self.git_at_fixed_date(fixture.project, "switch", target)
                    self.git_at_fixed_date(fixture.project, "merge", "--ff-only", fixture.brief.branch)

                result = self.integration_leaf(fixture, target)
                self.assertEqual("ok", result["status"], result)
                self.assertEqual("content-present", result["presence"], result)
                source = self.json_object(result["source"])
                self.assertEqual(candidate, source["candidate_revision"])

    def test_integration_leaf_recognizes_rename_and_binary_content(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Review a rename and binary addition.")
        self.git_at_fixed_date(fixture.project, "branch", "integration-target", fixture.brief.base_revision)
        (fixture.project / "tracked.txt").rename(fixture.project / "renamed.txt")
        (fixture.project / "binary.dat").write_bytes(b"\x00\x01reviewed\xff\n")
        observed = call_native_tool(
            mcp_server.CANDIDATE_OBSERVE_TOOL,
            {**self.roots(fixture), "attempt_id": "work-a-1"},
        )
        candidate = observed["candidate"]
        assert isinstance(candidate, str)
        lease = self.native_attempt_acquire(fixture, "rename-binary-worker")
        selected = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        submitted = self.transition_result(fixture, selected, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)
        candidate_commit = self.commit_all(fixture.project, "commit rename and binary candidate")
        self.git_at_fixed_date(fixture.project, "switch", "integration-target")
        self.git_at_fixed_date(fixture.project, "merge", "--squash", candidate_commit)
        self.git_at_fixed_date(fixture.project, "commit", "-m", "squash rename and binary candidate")

        result = self.integration_leaf(fixture, "integration-target")

        self.assertEqual("ok", result["status"], result)
        self.assertEqual("content-present", result["presence"], result)

    def test_integration_leaf_accepts_a_protected_commit_candidate(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Submit an exact commit candidate.")
        candidate = self.commit_all(fixture.project, "commit candidate before review")
        lease = self.native_attempt_acquire(fixture, "commit-candidate-worker")
        selected = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        submitted = self.transition_result(fixture, selected, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)

        result = self.integration_leaf(fixture, fixture.brief.branch)
        self.assertEqual("ok", result["status"], result)
        self.assertEqual("content-present", result["presence"])
        source = self.json_object(result["source"])
        self.assertEqual(candidate, source["candidate_revision"])
        self.assertEqual(fixture.brief.base_revision, source["compared_from_revision"])

    def test_integration_leaf_selects_a_completed_items_closing_candidate(self) -> None:
        fixture = self.checkpoint_fixture()
        self.complete(fixture, "Completed with the reviewed candidate.")
        candidate_commit = self.commit_all(fixture.project, "integrate closing candidate")
        result = self.integration_leaf(fixture, fixture.brief.branch)

        self.assertEqual("ok", result["status"], result)
        self.assertEqual("content-present", result["presence"])
        self.assertEqual(candidate_commit, result["target_revision"])
        source = self.json_object(result["source"])
        self.assertEqual("completion", source["kind"])
        self.assertEqual("work-a-1", source["attempt_id"])
        self.assertEqual(fixture.candidate_revision, source["candidate_revision"])

    def test_integration_leaf_recognizes_only_canonical_legacy_completion_candidates(self) -> None:
        fixture = self.checkpoint_fixture()
        self.complete(fixture, "Completed with a retained pre-snapshot candidate.")
        store = SQLiteWorkStore(fixture.work / "state.sqlite3")
        context = store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        self.assertIsNotNone(context)
        assert context is not None
        receipt = context.receipt
        reference_id = context.reference.artifact_ref_id
        legacy_outcome = json.dumps(
            {"candidate": fixture.candidate_revision, "evidence": None, "outcome": "submit-review"},
            separators=(",", ":"),
            sort_keys=True,
        )
        with sqlite3.connect(fixture.work / "state.sqlite3") as connection:
            connection.execute(
                "UPDATE transition_history SET artifact_ref_id = NULL, artifact_kind = NULL, "
                "input_schema = 'decision/v1', input_json = '{}', outcome_schema = 'transition-receipt/v1', "
                "outcome_json = ? WHERE history_id = ?",
                (legacy_outcome, receipt.history_id),
            )
            connection.execute("DELETE FROM artifact_refs WHERE artifact_ref_id = ?", (reference_id,))
        (fixture.work / context.reference.selector).unlink()

        unavailable = self.integration_leaf(fixture, fixture.brief.branch)
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", unavailable["code"], unavailable)
        message = unavailable["message"]
        assert isinstance(message, str)
        self.assertIn("retained pre-snapshot", message)
        self.assertIn("accepted snapshot bytes", message)
        self.assertEqual(("unchanged", "correct-input"), (unavailable["effect"], unavailable["retry"]))

        damaged_fixture = self.checkpoint_fixture()
        self.complete(damaged_fixture, "Current candidate evidence must remain distinguishable.")
        damaged_store = SQLiteWorkStore(damaged_fixture.work / "state.sqlite3")
        damaged_context = damaged_store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        self.assertIsNotNone(damaged_context)
        assert damaged_context is not None
        with sqlite3.connect(damaged_fixture.work / "state.sqlite3") as connection:
            connection.execute(
                "UPDATE transition_history SET artifact_ref_id = NULL, artifact_kind = NULL WHERE history_id = ?",
                (damaged_context.receipt.history_id,),
            )
            connection.execute(
                "DELETE FROM artifact_refs WHERE artifact_ref_id = ?", (damaged_context.reference.artifact_ref_id,)
            )
        (damaged_fixture.work / damaged_context.reference.selector).unlink()
        with self.assertRaises(StorageError):
            damaged_store.read_completed_candidate_snapshot_context(AttemptId("work-a-1"))

    def test_integration_leaf_reports_no_change_and_typed_rejections(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Submit the unchanged candidate.")
        subprocess.run(["git", "checkout", "--", "tracked.txt"], cwd=fixture.project, check=True, capture_output=True)
        observed = call_native_tool(
            mcp_server.CANDIDATE_OBSERVE_TOOL,
            {**self.roots(fixture), "attempt_id": "work-a-1"},
        )
        candidate = observed["candidate"]
        assert isinstance(candidate, str)
        lease = self.native_attempt_acquire(fixture, "empty-integration-worker")
        selected = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        submitted = self.transition_result(fixture, selected, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)

        no_change = self.integration_leaf(fixture, fixture.brief.branch)
        self.assertEqual("ok", no_change["status"], no_change)
        self.assertEqual("no-change", no_change["presence"])

        unresolved = self.integration_leaf(fixture, "missing-target")
        self.assert_integration_rejection(
            unresolved,
            "INTEGRATION_TARGET_UNRESOLVED",
            "correct-input",
            {"target": "missing-target"},
            "local branch",
        )
        invalid_target = self.integration_leaf(fixture, "-option")
        self.assertEqual("ITEM_STATUS_INVALID", invalid_target["code"])
        self.assertEqual("pinboard-mcp-item-status-result/v3", invalid_target["schema"])
        nul_target = self.integration_leaf(fixture, "main\x00suffix")
        self.assertEqual("ITEM_STATUS_INVALID", nul_target["code"], nul_target)
        self.assertEqual("pinboard-mcp-item-status-result/v3", nul_target["schema"])

        ready_item = self.integration_leaf(fixture, fixture.brief.branch, item_id="work-c")
        self.assert_integration_rejection(
            ready_item,
            "INTEGRATION_CANDIDATE_UNAVAILABLE",
            "correct-input",
            {"item_id": "work-c", "item_state": "ready"},
            "Read the item",
        )

        unavailable_fixture = self.checkpoint_fixture()
        self.return_for_review(unavailable_fixture, "No current candidate remains.")
        unavailable = self.integration_leaf(unavailable_fixture, unavailable_fixture.brief.branch)
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", unavailable["code"])
        self.assertEqual("correct-input", unavailable["retry"])
        self.assert_integration_rejection(
            unavailable,
            "INTEGRATION_CANDIDATE_UNAVAILABLE",
            "correct-input",
            {"item_id": "work-a", "item_state": "active"},
            "Read the item",
        )
        unavailable_facts = {
            self.json_object(value)["field"]: self.json_object(value)["value"]
            for value in self.json_array(unavailable["observed"])
        }
        self.assertEqual("active", unavailable_facts["item_state"])

        self.close_prerequisite(fixture)
        direct_close = self.integration_leaf(fixture, fixture.brief.branch, item_id="work-c")
        self.assert_integration_rejection(
            direct_close,
            "INTEGRATION_CANDIDATE_UNAVAILABLE",
            "correct-input",
            {"item_id": "work-c", "item_state": "done"},
            "Read the item",
        )

        unknown = self.integration_leaf(fixture, fixture.brief.branch, item_id="missing-item")
        self.assert_integration_rejection(
            unknown,
            "ITEM_NOT_FOUND",
            "correct-input",
            {"item_id": "missing-item"},
            "Check the item id",
        )

    def test_integration_leaf_rejects_invalid_snapshot_and_non_git_root(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Check the accepted snapshot bytes.")
        lease = self.native_attempt_acquire(fixture, "evidence-corruption-worker")
        selected = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        observed = call_native_tool(
            mcp_server.CANDIDATE_OBSERVE_TOOL,
            {**self.roots(fixture), "attempt_id": "work-a-1"},
        )
        candidate = observed["candidate"]
        assert isinstance(candidate, str)
        submitted = self.transition_result(fixture, selected, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)

        with tempfile.TemporaryDirectory() as unrelated:
            non_git = Path(unrelated) / "not-a-git-checkout"
            non_git.mkdir()
            result = call_advertised_tool(
                mcp_server.ITEM_STATUS_TOOL,
                {
                    "request": {
                        "project_root": str(non_git),
                        "work_root": str(fixture.work),
                        "operation": "integration",
                        "item_id": "work-a",
                        "target": "main",
                    }
                },
            )
            self.assertEqual("PROJECT_GIT_ROOT_UNAVAILABLE", result["code"], result)
            self.assert_integration_rejection(
                result,
                "PROJECT_GIT_ROOT_UNAVAILABLE",
                "correct-input",
                {"project_root": str(non_git)},
                "Correct the selected checkout",
            )
            observed_root = {
                self.json_object(value)["field"]: self.json_object(value)["value"]
                for value in self.json_array(result["observed"])
            }
            self.assertEqual(str(non_git), observed_root["project_root"])

        context = SQLiteWorkStore(fixture.work / "state.sqlite3").read_candidate_snapshot_context(AttemptId("work-a-1"))
        self.assertIsNotNone(context)
        assert context is not None
        (fixture.work / context.reference.selector).write_bytes(b"altered accepted bytes")
        invalid = self.integration_leaf(fixture, fixture.brief.branch)
        self.assert_integration_rejection(
            invalid,
            "INTEGRATION_CANDIDATE_EVIDENCE_INVALID",
            "do-not-retry",
            {"attempt_id": "work-a-1"},
            "pinboard validate",
        )
        self.assertNotEqual([], invalid["mismatches"])

    def test_integration_leaf_names_a_damaged_checkpoint_receipt(self) -> None:
        fixture = self.accepted_package_fixture()
        history_id = self.latest_history_id(fixture)
        self.update_receipt(fixture, history_id, outcome_json='{"unexpected":true}')

        result = self.integration_leaf(fixture, fixture.brief.branch)

        self.assertEqual("TRANSITION_RECEIPT_DAMAGED", result["code"], result)
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"])
        self.assertEqual(("unchanged", "do-not-retry"), (result["effect"], result["retry"]))
        self.assertIn(str(history_id), str(result["recovery"]))

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
