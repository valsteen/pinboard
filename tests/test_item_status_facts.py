"""Item-status leaves report review, branch ownership, closure, integration, and receipt facts."""

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import assert_never
from unittest.mock import patch

import msgspec
from mcp.server.mcpserver.exceptions import ToolError
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files.errors import RootError, RootErrorCode
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.files.models import AffectedViews, ViewRefreshResult
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import queries, query_models
from pinboard.application.ports import GeneratedViewReader
from pinboard.domain import decision_models
from pinboard.domain.identifiers import AttemptId, HistoryId, WorkItemId
from pinboard.mcp import common as mcp_common
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import SQLITE_NOW, JsonObject, NoReadyCandidateReviews

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

    def inspection(self, fixture: CheckpointFixture, reconciliation: JsonObject | None = None) -> JsonObject:
        return call_advertised_tool(
            mcp_server.ATTEMPT_INSPECT_TOOL,
            {**self.roots(fixture), "attempt_id": "work-a-1", "reconciliation": reconciliation},
        )

    def add_unrelated_integration_rows(self, fixture: CheckpointFixture, count: int) -> None:
        timestamp = SQLITE_NOW.isoformat()
        digest = "a" * 64
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            brief_ref = connection.execute(
                "SELECT artifact_ref_id FROM artifact_refs WHERE kind = 'brief' LIMIT 1"
            ).fetchone()
            assert brief_ref is not None
            for index in range(count):
                attempt_id = f"unrelated-attempt-{index}"
                connection.execute(
                    """INSERT INTO attempts (
                           attempt_id, item_id, state, branch, base_revision, provenance,
                           brief_artifact_ref_id, brief_artifact_kind, result_artifact_ref_id, result_artifact_kind,
                           candidate_revision, candidate_recorded_at, accepted_scope_revision, accepted_scope_digest,
                           subject_revision, recorded_at, updated_at
                       ) VALUES (?, 'work-b', 'done', ?, ?, 'integration-scope-fixture', ?, 'brief',
                                 NULL, NULL, NULL, NULL, 1, ?, 0, ?, ?)""",
                    (attempt_id, f"codex/unrelated-{index}", "b" * 40, brief_ref[0], digest, timestamp, timestamp),
                )
                connection.execute(
                    """INSERT INTO transition_history (
                           project_revision, action_id, action_kind, subject_id, artifact_ref_id, artifact_kind,
                           authorization_kind, actor_task_id, actor_host_id, input_schema, input_json,
                           outcome_schema, outcome_json, committed_at
                       ) VALUES (?, ?, 'pause', ?, NULL, NULL, 'project', 'fixture-task', 'fixture-host',
                                 'transition-receipt/v1', '{}', 'transition-receipt/v1', '{}', ?)""",
                    (10000 + index, f"pause:{attempt_id}", attempt_id, timestamp),
                )
                connection.execute(
                    """INSERT INTO artifact_refs (
                           artifact_key, artifact_revision, kind, relative_path, content_sha256, size_bytes,
                           accepted_revision, created_at
                       ) VALUES (?, 1, 'evidence', ?, ?, 0, ?, ?)""",
                    (
                        f"unrelated-evidence-{index}",
                        f"artifacts/evidence/unrelated-evidence-{index}/1.json",
                        digest,
                        10000 + index,
                        timestamp,
                    ),
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

    def submit_committed_candidate(self, fixture: CheckpointFixture, label: str) -> tuple[str, bytes]:
        candidate = self.commit_all(fixture.project, label)
        diff = subprocess.run(
            ["git", "diff", "--binary", fixture.brief.base_revision, candidate, "--"],
            cwd=fixture.project,
            check=True,
            capture_output=True,
        ).stdout
        lease = self.native_attempt_acquire(fixture, f"worker-{label}")
        submission = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        submitted = self.transition_result(fixture, submission, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)
        return candidate, diff

    def squash_diff_to_main(self, fixture: CheckpointFixture, diff: bytes, message: str) -> str:
        subprocess.run(
            ["git", "branch", "main", fixture.brief.base_revision],
            cwd=fixture.project,
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "switch", "main"], cwd=fixture.project, check=True, capture_output=True)
        existing_diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--"],
            cwd=fixture.project,
            check=True,
            capture_output=True,
        ).stdout
        if existing_diff != diff:
            subprocess.run(
                ["git", "apply", "--index", "-"],
                cwd=fixture.project,
                input=diff,
                check=True,
                capture_output=True,
            )
        return self.commit_all(fixture.project, message)

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

    def test_integration_leaf_reports_protected_candidate_content_without_ancestry_claims(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Submit the candidate through the review route.")
        candidate = self.submit_candidate(fixture, "protected integration")
        absent = self.integration_leaf(fixture, fixture.brief.base_revision)
        self.assertEqual("pinboard-item-integration/v1", absent["schema"])
        self.assertEqual(
            ("work-a", fixture.brief.base_revision, "content-not-present"),
            (absent["item_id"], absent["target"], absent["presence"]),
        )
        source = self.json_object(absent["source"])
        snapshot = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert snapshot is not None
        self.assertEqual(
            ("protected-review", "work-a-1", candidate),
            (source["kind"], source["attempt_id"], source["candidate_revision"]),
        )
        self.assertEqual(fixture.brief.base_revision, source["compared_from_revision"])

        subprocess.run(["git", "add", "tracked.txt"], cwd=fixture.project, check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Pinboard Tests",
                "-c",
                "user.email=pinboard@example.invalid",
                "commit",
                "-m",
                "candidate commit",
            ],
            cwd=fixture.project,
            env=os.environ
            | {
                "GIT_AUTHOR_DATE": "2001-02-03T04:05:06+00:00",
                "GIT_COMMITTER_DATE": "2001-02-03T04:05:06+00:00",
            },
            check=True,
            capture_output=True,
        )
        candidate_commit = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=fixture.project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "branch", "main", candidate_commit], cwd=fixture.project, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", candidate_commit],
            cwd=fixture.project,
            check=True,
            capture_output=True,
        )
        present = self.integration_leaf(fixture, "origin/main")
        self.assertEqual("content-present", present["presence"], present)
        self.assertEqual(candidate_commit, present["target_revision"])
        self.assertEqual("origin/main", present["target"])

        (fixture.project / "tracked.txt").write_text("overlapping later edit\n", encoding="utf-8")
        self.commit_all(fixture.project, "overlap the reviewed lines")
        overlapping_target = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=fixture.project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "branch", "-f", "main", overlapping_target],
            cwd=fixture.project,
            check=True,
            capture_output=True,
        )
        absent_after_overlap = self.integration_leaf(fixture, "main")
        self.assertEqual("content-not-present", absent_after_overlap["presence"], absent_after_overlap)

    def test_integration_leaf_recognizes_squash_and_nonoverlapping_later_content(self) -> None:
        fixture = self.checkpoint_fixture_with_nonoverlapping_candidate()
        self.return_for_review(fixture, "Submit a committed candidate for squash integration.")
        candidate, diff = self.submit_committed_candidate(fixture, "squash candidate")
        candidate_commit = candidate
        target = self.squash_diff_to_main(fixture, diff, "squash integration")
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", candidate_commit, target],
            cwd=fixture.project,
            check=False,
            capture_output=True,
        )
        self.assertEqual(1, ancestry.returncode)
        result = self.integration_leaf(fixture, "main")
        source = self.json_object(result["source"])
        self.assertEqual("content-present", result["presence"], result)
        self.assertEqual(target, result["target_revision"])
        self.assertEqual(
            ("protected-review", candidate, fixture.brief.base_revision),
            (source["kind"], source["candidate_revision"], source["compared_from_revision"]),
        )

        tracked = fixture.project / "tracked.txt"
        tracked.write_text(tracked.read_text(encoding="utf-8") + "later independent line\n", encoding="utf-8")
        self.commit_all(fixture.project, "later nonoverlapping edit")
        result_after_later_edit = self.integration_leaf(fixture, "main")
        self.assertEqual("content-present", result_after_later_edit["presence"], result_after_later_edit)

    def test_integration_leaf_recognizes_fast_forward_for_protected_commit_candidate(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Submit a protected committed candidate for fast-forward integration.")
        candidate, _diff = self.submit_committed_candidate(fixture, "fast-forward candidate")
        subprocess.run(
            ["git", "branch", "main", fixture.brief.base_revision],
            cwd=fixture.project,
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "switch", "main"], cwd=fixture.project, check=True, capture_output=True)
        subprocess.run(
            ["git", "merge", "--ff-only", "codex/work-a"],
            cwd=fixture.project,
            check=True,
            capture_output=True,
        )

        result = self.integration_leaf(fixture, "main")
        source = self.json_object(result["source"])
        self.assertEqual("content-present", result["presence"], result)
        self.assertEqual(candidate, result["target_revision"])
        self.assertEqual(("protected-review", candidate), (source["kind"], source["candidate_revision"]))

    def test_integration_leaf_reverse_applies_renames_and_binary_content(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Submit a renamed file and binary candidate.")
        (fixture.project / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(
            ["git", "mv", "tracked.txt", "renamed.txt"],
            cwd=fixture.project,
            check=True,
            capture_output=True,
        )
        (fixture.project / "asset.bin").write_bytes(b"\x00\xffreviewed binary content\n")
        candidate, diff = self.submit_committed_candidate(fixture, "rename and binary candidate")
        self.assertIn(b"rename from tracked.txt", diff)
        self.assertIn(b"GIT binary patch", diff)

        self.squash_diff_to_main(fixture, diff, "squash renamed and binary content")
        result = self.integration_leaf(fixture, "main")

        self.assertEqual("content-present", result["presence"], result)
        self.assertEqual(candidate, self.json_object(result["source"])["candidate_revision"])

    def test_integration_leaf_recognizes_merge_commit_and_rebase_merge_content(self) -> None:
        for integration in ("merge-commit", "rebase-merge"):
            with self.subTest(integration=integration):
                fixture = self.checkpoint_fixture()
                self.return_for_review(fixture, f"Submit the {integration} candidate.")
                candidate, _diff = self.submit_committed_candidate(fixture, integration)
                if integration == "merge-commit":
                    subprocess.run(
                        ["git", "branch", "main", fixture.brief.base_revision],
                        cwd=fixture.project,
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(["git", "switch", "main"], cwd=fixture.project, check=True, capture_output=True)
                    subprocess.run(
                        ["git", "merge", "--no-ff", "codex/work-a", "-m", "merge candidate"],
                        cwd=fixture.project,
                        env=os.environ
                        | {
                            "GIT_AUTHOR_DATE": "2001-02-03T04:05:06+00:00",
                            "GIT_COMMITTER_DATE": "2001-02-03T04:05:06+00:00",
                        },
                        check=True,
                        capture_output=True,
                    )
                    target = subprocess.run(
                        ["git", "rev-parse", "--verify", "HEAD"],
                        cwd=fixture.project,
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip()
                else:
                    subprocess.run(
                        ["git", "branch", "main", fixture.brief.base_revision],
                        cwd=fixture.project,
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(["git", "switch", "main"], cwd=fixture.project, check=True, capture_output=True)
                    (fixture.project / "unrelated.txt").write_text("target progress\n", encoding="utf-8")
                    self.commit_all(fixture.project, "target progress")
                    subprocess.run(
                        ["git", "switch", "codex/work-a"],
                        cwd=fixture.project,
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(["git", "rebase", "main"], cwd=fixture.project, check=True, capture_output=True)
                    target = subprocess.run(
                        ["git", "rev-parse", "--verify", "HEAD"],
                        cwd=fixture.project,
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip()
                    subprocess.run(
                        ["git", "branch", "-f", "main", target],
                        cwd=fixture.project,
                        check=True,
                        capture_output=True,
                    )
                result = self.integration_leaf(fixture, "main")
                self.assertEqual("content-present", result["presence"], result)
                self.assertEqual(target, result["target_revision"])
                self.assertNotEqual(candidate, target)

    def test_accepted_checkpoint_candidate_survives_resume_and_completion_uses_closing_candidate(self) -> None:
        fixture = self.accepted_package_fixture(candidate_form="current-head")
        target = self.squash_diff_to_main(fixture, fixture.candidate_bytes, "accepted checkpoint squash")
        checkpoint_result = self.integration_leaf(fixture, "main")
        checkpoint_source = self.json_object(checkpoint_result["source"])
        self.assertEqual("content-present", checkpoint_result["presence"], checkpoint_result)
        self.assertEqual("accepted-checkpoint", checkpoint_source["kind"])
        self.assertEqual(fixture.brief.checkpoint.checkpoint_id, checkpoint_source["checkpoint_id"])
        self.assertEqual(target, checkpoint_result["target_revision"])
        self.close_prerequisite(fixture)
        self.transition(fixture, "resume:work-a", {})
        resumed = self.integration_leaf(fixture, "main")
        self.assertEqual("accepted-checkpoint", self.json_object(resumed["source"])["kind"], resumed)

        completed = self.checkpoint_fixture()
        self.return_for_review(completed, "Submit a closing candidate.")
        candidate = self.submit_candidate(completed, "completion candidate")
        self.record_ready(completed, candidate)
        completed = replace(completed, candidate_revision=candidate)
        self.complete(completed, "The closing candidate is accepted.")
        self.commit_all(completed.project, "closing candidate commit")
        closing_revision = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=completed.project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "branch", "main", closing_revision],
            cwd=completed.project,
            check=True,
            capture_output=True,
        )
        completion_result = self.integration_leaf(completed, "main")
        completion_source = self.json_object(completion_result["source"])
        self.assertEqual("content-present", completion_result["presence"], completion_result)
        self.assertEqual("completion", completion_source["kind"])
        self.assertEqual("work-a-1", completion_source["attempt_id"])

    def test_integration_leaf_reports_no_change_for_empty_recorded_diff(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head", empty_recorded_diff=True)
        self.return_for_review(fixture, "Submit the exact current head with an empty recorded diff.")
        lease = self.native_attempt_acquire(fixture, "worker-empty-candidate")
        selected = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        self.transition_result(fixture, selected, {"candidate": fixture.candidate_revision})

        result = self.integration_leaf(fixture, fixture.candidate_revision)

        source = self.json_object(result["source"])
        self.assertEqual("no-change", result["presence"], result)
        self.assertEqual(fixture.candidate_revision, result["target_revision"])
        self.assertEqual("protected-review", source["kind"])
        self.assertEqual(fixture.candidate_revision, source["compared_from_revision"])

    def test_integration_leaf_rejections_name_effect_retry_and_recovery(self) -> None:
        protected = self.checkpoint_fixture()
        self.return_for_review(protected, "Submit evidence for typed integration rejection tests.")
        self.submit_candidate(protected, "rejection candidate")

        unresolved = self.integration_leaf(protected, "missing/local-target")
        self.assertEqual(("rejected", "INTEGRATION_TARGET_UNRESOLVED"), (unresolved["status"], unresolved["code"]))
        self.assertEqual("pinboard-mcp-item-status-result/v3", unresolved["schema"])
        self.assertEqual(False, unresolved["state_changed"])
        self.assertEqual(("unchanged", "correct-input"), (unresolved["effect"], unresolved["retry"]))
        unresolved_recovery = unresolved["recovery"]
        assert isinstance(unresolved_recovery, str)
        self.assertIn("existing local branch", unresolved_recovery)
        self.assertIn("Fetch outside Pinboard", unresolved_recovery)
        unresolved_observed = self.json_array(unresolved["observed"])
        self.assertIn({"field": "target", "value": "missing/local-target"}, unresolved_observed)
        self.assertTrue(self.json_array(unresolved["mismatches"]))

        malformed_target = self.integration_leaf(protected, "--upload-pack=evil")
        self.assertEqual(("rejected", "ITEM_STATUS_INVALID"), (malformed_target["status"], malformed_target["code"]))
        self.assertEqual("pinboard-mcp-item-status-result/v3", malformed_target["schema"])
        self.assertEqual(
            (False, "unchanged", "correct-input"),
            (malformed_target["state_changed"], malformed_target["effect"], malformed_target["retry"]),
        )
        nul_target = self.integration_leaf(protected, "main\x00")
        self.assertEqual(("rejected", "ITEM_STATUS_INVALID"), (nul_target["status"], nul_target["code"]))
        self.assertEqual("pinboard-mcp-item-status-result/v3", nul_target["schema"])
        self.assertEqual(
            (False, "unchanged", "correct-input"),
            (nul_target["state_changed"], nul_target["effect"], nul_target["retry"]),
        )
        unicode_line_target = self.integration_leaf(protected, "main\u2028unexpected")
        self.assertEqual(
            ("rejected", "ITEM_STATUS_INVALID"), (unicode_line_target["status"], unicode_line_target["code"])
        )

        unknown_item = self.integration_leaf(protected, "main", item_id="unknown-item")
        self.assertEqual(("rejected", "ITEM_NOT_FOUND"), (unknown_item["status"], unknown_item["code"]))
        self.assertEqual("pinboard-mcp-item-status-result/v3", unknown_item["schema"])
        self.assertEqual(
            (False, "unchanged", "correct-input"),
            (unknown_item["state_changed"], unknown_item["effect"], unknown_item["retry"]),
        )

        snapshot = protected.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert snapshot is not None
        (protected.work / snapshot.reference.selector).write_bytes(b"damaged accepted bytes")
        invalid_evidence = self.integration_leaf(protected, "main")
        self.assertEqual(
            ("rejected", "INTEGRATION_CANDIDATE_EVIDENCE_INVALID"),
            (invalid_evidence["status"], invalid_evidence["code"]),
        )
        self.assertEqual("pinboard-mcp-item-status-result/v3", invalid_evidence["schema"])
        self.assertEqual(
            (False, "unchanged", "do-not-retry"),
            (invalid_evidence["state_changed"], invalid_evidence["effect"], invalid_evidence["retry"]),
        )
        evidence_recovery = invalid_evidence["recovery"]
        evidence_message = invalid_evidence["message"]
        assert isinstance(evidence_recovery, str) and isinstance(evidence_message, str)
        self.assertIn("pinboard validate", evidence_recovery)
        self.assertIn({"field": "attempt_id", "value": "work-a-1"}, self.json_array(invalid_evidence["observed"]))
        self.assertIn("Accepted candidate evidence", evidence_message)

    def test_integration_leaf_reports_unavailable_and_damaged_checkpoint_receipt(self) -> None:
        unavailable = self.checkpoint_fixture()
        self.return_for_review(unavailable, "Return before another candidate is submitted.")
        no_candidate = self.integration_leaf(unavailable, "main")
        self.assertEqual(
            ("rejected", "INTEGRATION_CANDIDATE_UNAVAILABLE"), (no_candidate["status"], no_candidate["code"])
        )
        self.assertEqual("pinboard-mcp-item-status-result/v3", no_candidate["schema"])
        self.assertEqual(
            (False, "unchanged", "correct-input"),
            (no_candidate["state_changed"], no_candidate["effect"], no_candidate["retry"]),
        )
        no_candidate_recovery = no_candidate["recovery"]
        no_candidate_message = no_candidate["message"]
        assert isinstance(no_candidate_recovery, str) and isinstance(no_candidate_message, str)
        self.assertIn("operation item", no_candidate_recovery)
        self.assertIn({"field": "item_id", "value": "work-a"}, self.json_array(no_candidate["observed"]))
        self.assertIn("no protected candidate", no_candidate_message)

        self.close_prerequisite(unavailable)
        direct_close = self.integration_leaf(unavailable, "main", item_id="work-c")
        self.assertEqual(
            ("rejected", "INTEGRATION_CANDIDATE_UNAVAILABLE"), (direct_close["status"], direct_close["code"])
        )
        self.assertEqual(
            (False, "unchanged", "correct-input"),
            (direct_close["state_changed"], direct_close["effect"], direct_close["retry"]),
        )
        direct_close_message = direct_close["message"]
        assert isinstance(direct_close_message, str)
        self.assertIn("direct close", direct_close_message)
        self.assertIn({"field": "item_id", "value": "work-c"}, self.json_array(direct_close["observed"]))

        accepted = self.accepted_package_fixture(candidate_form="current-head")
        checkpoint_history_id = self.latest_history_id(accepted)
        self.update_receipt(accepted, checkpoint_history_id, outcome_json="{damaged")
        damaged_receipt = self.integration_leaf(accepted, "main")
        self.assertEqual(
            ("rejected", "TRANSITION_RECEIPT_DAMAGED"), (damaged_receipt["status"], damaged_receipt["code"])
        )
        self.assertEqual("pinboard-mcp-item-status-result/v3", damaged_receipt["schema"])
        self.assertEqual(
            (False, "unchanged", "do-not-retry"),
            (damaged_receipt["state_changed"], damaged_receipt["effect"], damaged_receipt["retry"]),
        )
        damaged_recovery = damaged_receipt["recovery"]
        assert isinstance(damaged_recovery, str)
        self.assertIn("Report to the human", damaged_recovery)
        self.assertIn("do not retry", damaged_recovery)

    def test_integration_leaf_returns_typed_git_adapter_failures(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Submit a candidate before observing a Git read failure.")
        self.submit_candidate(fixture, "git failure candidate")
        git_error = RootError(
            RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE, "read-tree could not read the target tree"
        )
        with patch("pinboard.adapters.candidate_evidence.root.observe_candidate_content", side_effect=git_error):
            failure = self.integration_leaf(fixture, "main")

        self.assertEqual(("rejected", "PROJECT_GIT_CHECKOUT_UNAVAILABLE"), (failure["status"], failure["code"]))
        self.assertEqual("pinboard-mcp-item-status-result/v3", failure["schema"])
        self.assertEqual(
            (False, "unchanged", "correct-input"), (failure["state_changed"], failure["effect"], failure["retry"])
        )
        failure_observed = self.json_array(failure["observed"])
        self.assertIn({"field": "project_root", "value": str(fixture.project)}, failure_observed)
        git_error_fact = self.json_object(failure_observed[1])
        git_error = git_error_fact["value"]
        recovery = failure["recovery"]
        assert isinstance(git_error, str) and isinstance(recovery, str)
        self.assertIn("read-tree could not read the target tree", git_error)
        self.assertIn("Correct the local project checkout", recovery)

    def test_integration_read_scope_stays_keyed_with_unrelated_history_and_artifacts(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Submit a candidate for focused status read scope.")
        self.submit_candidate(fixture, "focused status candidate")
        self.add_unrelated_integration_rows(fixture, 32)
        read_tables: set[str] = set()
        statements: list[str] = []
        original_open = sqlite_store.open_database

        def traced_open(database_path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open(database_path, mode)

            def authorize(
                action: int,
                argument: str | None,
                _secondary_argument: str | None,
                _database: str | None,
                _trigger: str | None,
            ) -> int:
                if action == sqlite3.SQLITE_READ and argument is not None:
                    read_tables.add(argument)
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(sqlite_store, "open_database", traced_open):
            result = self.integration_leaf(fixture, fixture.brief.base_revision)

        self.assertEqual("content-not-present", result["presence"], result)
        self.assertEqual({"artifact_refs", "attempts", "project_meta", "transition_history", "work_items"}, read_tables)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            self.assertEqual(
                32,
                connection.execute(
                    "SELECT COUNT(*) FROM attempts WHERE attempt_id LIKE 'unrelated-attempt-%'"
                ).fetchone()[0],
            )
        selects = tuple(statement for statement in statements if statement.lstrip().upper().startswith("SELECT"))
        self.assertTrue(selects)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            plans = tuple(
                tuple(str(row[3]).upper() for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}").fetchall())
                for statement in selects
            )
        self.assertTrue(all(any(detail.startswith("SEARCH ") for detail in plan) for plan in plans), plans)
        self.assertFalse(any(detail.startswith("SCAN ") for plan in plans for detail in plan), plans)
        self.assertTrue(any("checkpoint_history_by_subject" in statement for statement in selects), selects)

    def test_attempt_inspection_relations_and_other_status_reads_stay_outside_git_check(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Submit and review a candidate for caller reconciliation.")
        candidate = self.submit_candidate(fixture, "caller-owned integration relation")
        self.record_ready(fixture, candidate)
        item_before = self.item_leaf(fixture)
        branch_before = self.branch_leaf(fixture, fixture.brief.branch)
        overview_before = call_advertised_tool(mcp_server.OVERVIEW_TOOL, self.roots(fixture))
        reconciliation: JsonObject = {
            "target_revision": fixture.brief.base_revision,
            "relation": "candidate-integrated",
            "phase": "cleanup",
            "effects": [
                {"effect": "source-checkout", "status": "allowed"},
                {"effect": "shared-work-root", "status": "not-required"},
                {"effect": "git-metadata", "status": "allowed"},
            ],
        }
        with patch("pinboard.adapters.candidate_evidence.root.observe_candidate_content") as content_read:
            inspection = self.inspection(fixture, reconciliation)
            self.assertEqual(item_before, self.item_leaf(fixture))
            self.assertEqual(branch_before, self.branch_leaf(fixture, fixture.brief.branch))
            self.assertEqual(overview_before, call_advertised_tool(mcp_server.OVERVIEW_TOOL, self.roots(fixture)))
        content_read.assert_not_called()

        self.assertEqual("pinboard-item-status/v2", item_before["schema"])
        self.assertEqual("pinboard-branch-owners/v1", branch_before["schema"])
        self.assertEqual("ok", inspection["status"], inspection)
        continuation = self.json_object(inspection["continuation"])
        next_operation = self.json_object(continuation["next_operation"])
        self.assertEqual("repository-cleanup", next_operation["kind"])
        self.assertEqual(fixture.brief.base_revision, next_operation["target_revision"])

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
            case decision_models.ActionKind.COMPLETE:
                fixture = self.checkpoint_fixture()
                candidate = self.submit_candidate(fixture, "completed")
                self.record_ready(fixture, candidate)
                self.complete(fixture, "The accepted candidate completes this item.")
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
