"""Item-status leaves report review verdicts, branch owners, closure facts, and damaged receipts."""

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
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
from pinboard.application import checkpoint_compatibility_models, queries, query_models, work_brief_models
from pinboard.application.ports import GeneratedViewReader
from pinboard.domain import decision_models, history
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
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": item_id, "target": target}},
        )

    @patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"})
    def test_integration_leaf_checks_the_submitted_working_tree_after_commit(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Replace the candidate.")
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=fixture.project, capture_output=True, text=True, check=True
        ).stdout.strip()
        candidate = self.submit_candidate(fixture, "integration-content")
        missing = self.integration_leaf(fixture, base)
        self.assertEqual("content-not-present", missing["presence"])
        subprocess.run(["git", "add", "tracked.txt"], cwd=fixture.project, check=True)
        with patch.dict(
            os.environ, {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"}
        ):
            subprocess.run(["git", "commit", "-m", "integrate"], cwd=fixture.project, capture_output=True, check=True)
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=fixture.project, capture_output=True, text=True, check=True
        ).stdout.strip()
        present = self.integration_leaf(fixture, "HEAD")
        self.assertEqual("pinboard-item-integration/v1", present["schema"])
        self.assertEqual("content-present", present["presence"])
        self.assertEqual("HEAD", present["target"])
        self.assertEqual(revision, present["target_revision"])
        self.assertEqual(
            {
                "kind": "protected-review",
                "attempt_id": "work-a-1",
                "candidate_revision": candidate,
                "compared_from_revision": base,
            },
            present["source"],
        )
        subprocess.run(
            ["git", "switch", "-c", "working-squash", base], cwd=fixture.project, capture_output=True, check=True
        )
        subprocess.run(["git", "merge", "--squash", revision], cwd=fixture.project, capture_output=True, check=True)
        self.commit_all(fixture.project, "squash working-tree candidate")
        self.assertEqual("content-present", self.integration_leaf(fixture, "working-squash")["presence"])
        unknown = self.integration_leaf(fixture, "missing-ref")
        self.assertEqual("INTEGRATION_TARGET_UNRESOLVED", unknown["code"])
        self.assertEqual(("unchanged", "correct-input"), (unknown["effect"], unknown["retry"]))
        self.assertIn("fetch", str(unknown["recovery"]))
        self.assertEqual(
            {"target", "project_root"},
            {str(self.json_object(fact)["field"]) for fact in self.json_array(unknown["observed"])},
        )
        invalid = call_native_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": "work-a", "target": "--help"}},
        )
        self.assertEqual("ITEM_STATUS_INVALID", invalid["code"])
        self.assertEqual("pinboard-mcp-item-status-result/v3", invalid["schema"])
        self.assertIn("Correct the request", str(invalid["message"]))

    @patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"})
    def test_integration_leaf_selects_checkpoint_and_completion(self) -> None:
        checkpoint = self.checkpoint_fixture()
        self.return_for_review(checkpoint, "Prepare a new checkpoint candidate.")
        checkpoint_candidate = self.submit_candidate(checkpoint, "checkpoint candidate")
        self.transition(
            checkpoint,
            "accept-checkpoint:work-a-1",
            {
                "checkpoint": checkpoint.brief.checkpoint.checkpoint_id,
                "candidate": checkpoint_candidate,
                "evidence": "Accepted evidence.",
            },
        )
        absent = self.integration_leaf(checkpoint, "HEAD")
        self.assertEqual("content-not-present", absent["presence"])
        self.assertEqual("accepted-checkpoint", self.json_object(absent["source"])["kind"])
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=checkpoint.project, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(
            {
                "kind": "accepted-checkpoint",
                "attempt_id": "work-a-1",
                "candidate_revision": checkpoint_candidate,
                "compared_from_revision": base,
                "checkpoint_id": checkpoint.brief.checkpoint.checkpoint_id,
            },
            absent["source"],
        )
        candidate_commit = self.commit_all(checkpoint.project, "checkpoint candidate")
        subprocess.run(
            ["git", "switch", "-c", "checkpoint-target", base], cwd=checkpoint.project, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "merge", "--squash", candidate_commit], cwd=checkpoint.project, capture_output=True, check=True
        )
        self.commit_all(checkpoint.project, "squash checkpoint")
        present = self.integration_leaf(checkpoint, "checkpoint-target")
        self.assertEqual("content-present", present["presence"])
        self.assertEqual(absent["source"], present["source"])
        subprocess.run(
            ["git", "switch", checkpoint.brief.branch], cwd=checkpoint.project, capture_output=True, check=True
        )
        self.close_prerequisite(checkpoint)
        self.transition(checkpoint, "resume:work-a", {})
        self.assertEqual(present["source"], self.integration_leaf(checkpoint, "checkpoint-target")["source"])
        self.rebind(checkpoint, checkpoint.brief.branch, 2)
        self.assertEqual(present["source"], self.integration_leaf(checkpoint, "checkpoint-target")["source"])

        completed = self.checkpoint_fixture()
        self.return_for_review(completed, "Prepare a completion candidate.")
        (completed.project / "tracked.txt").write_text("completion candidate\n", encoding="utf-8")
        completed_candidate = self.commit_all(completed.project, "completion candidate")
        lease = self.native_attempt_acquire(completed, "completion-worker")
        action = self.native_actions(completed, "submit-review", "work-a-1", role="worker", lease=lease)
        self.assertEqual(
            "committed", self.transition_result(completed, action, {"candidate": completed_candidate})["status"]
        )
        completed = replace(completed, candidate_revision=completed_candidate)
        self.complete(completed, "Reviewed and complete.")
        result = self.integration_leaf(completed, "HEAD")
        self.assertEqual("content-present", result["presence"])
        self.assertEqual(
            {
                "kind": "completion",
                "attempt_id": "work-a-1",
                "candidate_revision": completed_candidate,
                "compared_from_revision": completed.brief.base_revision,
            },
            result["source"],
        )

    @patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"})
    def test_integration_leaf_rejects_missing_candidate_and_invalid_evidence(self) -> None:
        fixture = self.checkpoint_fixture()
        unknown_item = self.integration_leaf(fixture, "HEAD", "missing-item")
        self.assertEqual("ITEM_NOT_FOUND", unknown_item["code"])
        self.assertEqual("pinboard-mcp-item-status-result/v3", unknown_item["schema"])
        ready = self.integration_leaf(fixture, "HEAD", "work-c")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", ready["code"])
        self.close_prerequisite(fixture)
        closed = self.integration_leaf(fixture, "HEAD", "work-c")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", closed["code"])
        self.assertIn("closed directly", str(closed["message"]))
        self.return_for_review(fixture, "The candidate needs correction.")
        missing = self.integration_leaf(fixture, "HEAD")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", missing["code"])
        self.assertEqual(("unchanged", "correct-input"), (missing["effect"], missing["retry"]))
        self.assertIn("operation item", str(missing["recovery"]))
        self.assertEqual(
            {"item_id", "state", "reason"},
            {str(self.json_object(fact)["field"]) for fact in self.json_array(missing["observed"])},
        )
        self.submit_candidate(fixture, "damaged-evidence")
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        (fixture.work / context.reference.selector).write_bytes(b"damaged")
        damaged = self.integration_leaf(fixture, "HEAD")
        self.assertEqual("INTEGRATION_CANDIDATE_EVIDENCE_INVALID", damaged["code"])
        self.assertEqual(("unchanged", "do-not-retry"), (damaged["effect"], damaged["retry"]))
        self.assertIn("pinboard validate", str(damaged["recovery"]))
        self.assertEqual(
            {"attempt_id", "reference"},
            {str(self.json_object(fact)["field"]) for fact in self.json_array(damaged["observed"])},
        )

    @patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"})
    def test_integration_reports_non_git_checkout(self) -> None:
        fixture = self.checkpoint_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            result = call_advertised_tool(
                mcp_server.ITEM_STATUS_TOOL,
                {
                    "request": {
                        "project_root": temporary,
                        "work_root": str(fixture.work),
                        "operation": "integration",
                        "item_id": "work-a",
                        "target": "HEAD",
                    }
                },
            )
        self.assertEqual("PROJECT_GIT_ROOT_UNAVAILABLE", result["code"])
        self.assertEqual(("unchanged", "correct-input"), (result["effect"], result["retry"]))
        self.assertIn("checkout", str(result["recovery"]))
        self.assertEqual(
            {"project_root", "diagnostic"},
            {str(self.json_object(fact)["field"]) for fact in self.json_array(result["observed"])},
        )
        with patch(
            "pinboard.adapters.files.root.observe_integration_content",
            side_effect=RootError(RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE, "Git read failed"),
        ):
            failed = self.integration_leaf(fixture, "HEAD")
        self.assertEqual("PROJECT_GIT_CHECKOUT_UNAVAILABLE", failed["code"])
        self.assertEqual(("unchanged", "correct-input"), (failed["effect"], failed["retry"]))
        self.assertIn("Git read failed", str(failed["message"]))
        self.assertIn("checkout", str(failed["recovery"]))

    @patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"})
    def test_pre_snapshot_review_candidate_is_unavailable(self) -> None:
        fixture = self.checkpoint_fixture()
        candidate = "working-tree-sha256:" + "0" * 64
        outcome = history.encode_transition_receipt_outcome(evidence=None, outcome="submit-review", candidate=candidate)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "update attempts set candidate_revision = ?, subject_revision = "
                "(select max(project_revision) from transition_history where action_kind = 'submit-review' and subject_id = 'work-a-1') "
                "where attempt_id = ?",
                (candidate, "work-a-1"),
            )
            connection.execute(
                "update transition_history set artifact_ref_id = null, artifact_kind = null, input_schema = 'decision/v1', input_json = '{}', "
                "outcome_schema = 'transition-receipt/v1', outcome_json = ? where project_revision = "
                "(select subject_revision from attempts where attempt_id = 'work-a-1')",
                (outcome.decode(),),
            )
        result = self.integration_leaf(fixture, "HEAD")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", result["code"], result)
        self.assertIn("predates", str(result["message"]))

    @patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"})
    def test_checkpoint_without_snapshot_reference_is_unavailable(self) -> None:
        fixture = self.accepted_package_fixture()
        package = self.package(fixture)
        assert isinstance(package, work_brief_models.CheckpointReviewPackageV3)
        legacy = checkpoint_compatibility_models.CheckpointReviewPackage(
            package.attempt_id,
            package.item_id,
            package.candidate,
            package.acceptance_evidence,
            package.accepted_scope,
            package.checkpoint,
            package.accepted_brief,
            package.result,
            package.implementation_review,
            package.verdict,
            package.review_basis,
        )
        self.replace_package(fixture, legacy)
        result = self.integration_leaf(fixture, "HEAD")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", result["code"])
        self.assertIn("no candidate snapshot", str(result["message"]))

    def test_commit_candidate_content_survives_merge_forms_and_later_edits(self) -> None:
        dates = {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"}
        with patch.dict(os.environ, dates):
            fixture = self.checkpoint_fixture()
            self.return_for_review(fixture, "Replace the candidate.")
            subprocess.run(["git", "restore", "tracked.txt"], cwd=fixture.project, check=True)
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=fixture.project, capture_output=True, text=True, check=True
            ).stdout.strip()
            (fixture.project / "tracked.txt").write_text("reviewed\n", encoding="utf-8")
            commit = self.commit_all(fixture.project, "reviewed")
            lease = self.native_attempt_acquire(fixture, "commit-worker")
            action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
            submitted = self.transition_result(fixture, action, {"candidate": commit})
            self.assertEqual("committed", submitted["status"], submitted)

            def git(*arguments: str) -> str:
                return subprocess.run(
                    ["git", *arguments], cwd=fixture.project, capture_output=True, text=True, check=True
                ).stdout.strip()

            git("branch", "fast-forward", base)
            git("switch", "fast-forward")
            git("merge", "--ff-only", commit)
            fast_forward = self.integration_leaf(fixture, "fast-forward")
            self.assertEqual("content-present", fast_forward["presence"])
            self.assertEqual(
                {
                    "kind": "protected-review",
                    "attempt_id": "work-a-1",
                    "candidate_revision": commit,
                    "compared_from_revision": base,
                },
                fast_forward["source"],
            )
            git("switch", "-c", "merge-target", base)
            git("merge", "--no-ff", "-m", "merge", commit)
            self.assertEqual("content-present", self.integration_leaf(fixture, "merge-target")["presence"])
            git("switch", "-c", "squash-target", base)
            git("merge", "--squash", commit)
            git("commit", "-m", "squash")
            self.assertEqual("content-present", self.integration_leaf(fixture, "squash-target")["presence"])
            git("update-ref", "refs/remotes/origin/main", "HEAD")
            remote = self.integration_leaf(fixture, "origin/main")
            self.assertEqual("content-present", remote["presence"])
            self.assertEqual("origin/main", remote["target"])

            git("switch", "-c", "rebase-target", base)
            (fixture.project / "other.txt").write_text("unrelated\n", encoding="utf-8")
            self.commit_all(fixture.project, "diverge")
            git("branch", "rebase-candidate", commit)
            git("rebase", "--onto", "rebase-target", base, "rebase-candidate")
            git("switch", "rebase-target")
            git("merge", "--ff-only", "rebase-candidate")
            self.assertNotEqual(
                0,
                subprocess.run(
                    ["git", "merge-base", "--is-ancestor", commit, "HEAD"], cwd=fixture.project, check=False
                ).returncode,
            )
            self.assertEqual("content-present", self.integration_leaf(fixture, "rebase-target")["presence"])
            (fixture.project / "tracked.txt").write_text("changed\n", encoding="utf-8")
            self.commit_all(fixture.project, "overlapping")
            self.assertEqual("content-not-present", self.integration_leaf(fixture, "rebase-target")["presence"])

    def test_integration_content_remains_after_distant_edit_to_reviewed_file(self) -> None:
        with patch.dict(
            os.environ, {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"}
        ):
            fixture = self.checkpoint_fixture()
            self.return_for_review(fixture, "Replace the candidate.")
            tracked = fixture.project / "tracked.txt"
            lines = [f"line {number}\n" for number in range(20)]
            tracked.write_text("".join(lines), encoding="utf-8")
            (fixture.project / "old-name.txt").write_text("rename content\n", encoding="utf-8")
            (fixture.project / "binary.dat").write_bytes(b"\x00before\xff")
            preimage = self.commit_all(fixture.project, "working-tree preimage")
            lines[2] = "reviewed line\n"
            tracked.write_text("".join(lines), encoding="utf-8")
            subprocess.run(["git", "mv", "old-name.txt", "new-name.txt"], cwd=fixture.project, check=True)
            (fixture.project / "binary.dat").write_bytes(b"\x00after\xff")
            observed = call_native_tool(
                mcp_server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"}
            )
            lease = self.native_attempt_acquire(fixture, "working-worker")
            action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
            self.assertEqual(
                "committed", self.transition_result(fixture, action, {"candidate": observed["candidate"]})["status"]
            )
            absent = self.integration_leaf(fixture, "HEAD")
            self.assertIn("presence", absent, absent)
            self.assertEqual("content-not-present", absent["presence"])
            self.assertEqual(preimage, self.json_object(absent["source"])["compared_from_revision"])
            self.commit_all(fixture.project, "integrate")
            lines[18] = "later line\n"
            tracked.write_text("".join(lines), encoding="utf-8")
            self.commit_all(fixture.project, "later edit")
            self.assertEqual("content-present", self.integration_leaf(fixture, "HEAD")["presence"])

    @patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"})
    def test_empty_snapshot_reports_no_change(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Submit an empty candidate.")
        subprocess.run(["git", "restore", "tracked.txt"], cwd=fixture.project, check=True)
        observed = call_native_tool(
            mcp_server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"}
        )
        lease = self.native_attempt_acquire(fixture, "empty-worker")
        action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        self.assertEqual(
            "committed", self.transition_result(fixture, action, {"candidate": observed["candidate"]})["status"]
        )
        with patch("pinboard.adapters.files.root.subprocess.run", wraps=subprocess.run) as git_calls:
            self.assertEqual("no-change", self.integration_leaf(fixture, "HEAD")["presence"])
        self.assertFalse(
            any("read-tree" in call.args[0] or "apply" in call.args[0] for call in git_calls.call_args_list)
        )

    @patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"})
    def test_integration_reports_damaged_checkpoint_receipt(self) -> None:
        fixture = self.accepted_package_fixture()
        history_id = self.latest_history_id(fixture)
        self.update_receipt(fixture, history_id, outcome_json="{}")
        result = self.integration_leaf(fixture, "HEAD")
        self.assertEqual("TRANSITION_RECEIPT_DAMAGED", result["code"])
        self.assertEqual(("unchanged", "do-not-retry"), (result["effect"], result["retry"]))

    @patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"})
    def test_integration_names_damaged_checkpoint_snapshot(self) -> None:
        fixture = self.accepted_package_fixture()
        package = self.package(fixture)
        assert isinstance(package, work_brief_models.CheckpointReviewPackageV3)
        identity = package.candidate_snapshot
        (fixture.work / identity.selector).write_bytes(b"damaged")
        result = self.integration_leaf(fixture, "HEAD")
        self.assertEqual("INTEGRATION_CANDIDATE_EVIDENCE_INVALID", result["code"])
        observed = {
            str(self.json_object(fact)["field"]): self.json_object(fact)["value"]
            for fact in self.json_array(result["observed"])
        }
        self.assertEqual(identity.selector, observed["reference"])

    @patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"})
    def test_accepted_and_continued_candidate_is_not_an_integration_source(self) -> None:
        fixture = self.checkpoint_fixture()
        self.transition(
            fixture,
            "accept-review-and-continue:work-a-1",
            {"candidate": fixture.candidate_revision, "evidence": "Accepted; continue work."},
        )
        result = self.integration_leaf(fixture, "HEAD")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", result["code"])

    @patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"})
    def test_integration_uses_focused_checkpoint_lookup_without_review_walk(self) -> None:
        fixture = self.accepted_package_fixture()
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            for number in range(4):
                attempt_id = f"unrelated-{number}"
                connection.execute(
                    "insert into attempts select ?, 'work-b', 'done', branch, base_revision, provenance, "
                    "brief_artifact_ref_id, brief_artifact_kind, null, null, null, null, accepted_scope_revision, "
                    "accepted_scope_digest, subject_revision, recorded_at, updated_at from attempts where attempt_id = 'work-a-1'",
                    (attempt_id,),
                )
                connection.execute(
                    "insert into artifact_refs (artifact_key, artifact_revision, kind, relative_path, content_sha256, size_bytes, accepted_revision, created_at) "
                    "select ?, 1, 'evidence', ?, content_sha256, size_bytes, accepted_revision, created_at "
                    "from artifact_refs where artifact_ref_id = ?",
                    (
                        f"unrelated-package-{number}",
                        f"artifacts/evidence/unrelated-package-{number}/1.json",
                        int(fixture.package_reference.artifact_ref_id),
                    ),
                )
                reference_id = connection.execute("select last_insert_rowid()").fetchone()[0]
                connection.execute(
                    "insert into transition_history (project_revision, action_id, action_kind, subject_id, artifact_ref_id, artifact_kind, "
                    "authorization_kind, actor_task_id, actor_host_id, input_schema, input_json, outcome_schema, outcome_json, committed_at) "
                    "select ?, ?, action_kind, ?, ?, artifact_kind, authorization_kind, actor_task_id, actor_host_id, "
                    "input_schema, input_json, outcome_schema, outcome_json, committed_at from transition_history "
                    "where artifact_ref_id = ?",
                    (
                        1000 + number,
                        f"accept-checkpoint:{attempt_id}",
                        attempt_id,
                        reference_id,
                        int(fixture.package_reference.artifact_ref_id),
                    ),
                )
        statements: list[str] = []
        open_database = sqlite_store.open_database

        def traced(path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = open_database(path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(sqlite_store, "open_database", side_effect=traced):
            result = self.integration_leaf(fixture, "HEAD")
        self.assertEqual("accepted-checkpoint", self.json_object(result["source"])["kind"])
        selected = "\n".join(statements).lower()
        self.assertNotIn("action_kind in", selected)
        self.assertNotIn("order by project_revision desc", selected)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            plan = connection.execute(
                "explain query plan select history_id from transition_history where subject_id = ? "
                "and outcome_schema = 'checkpoint-acceptance/v2' order by history_id desc limit 1",
                ("work-a-1",),
            ).fetchall()
        self.assertTrue(any("checkpoint_history_by_subject" in str(row) for row in plan), plan)

    @patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"})
    def test_integration_leaves_git_worktree_and_ledger_unchanged(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        git_dir = fixture.project / ".git"

        def contents(directory: Path) -> dict[Path, bytes]:
            return {path.relative_to(directory): path.read_bytes() for path in directory.rglob("*") if path.is_file()}

        git_before = contents(git_dir)
        ledger_before = contents(fixture.work)
        tracked_before = (fixture.project / "tracked.txt").read_bytes()
        with tempfile.TemporaryDirectory() as temporary, patch.object(tempfile, "tempdir", temporary):
            for path in git_dir.rglob("*"):
                path.chmod(path.stat().st_mode & ~0o222)
            try:
                result = self.integration_leaf(fixture, "HEAD")
                self.assertEqual("content-present", result["presence"])
                self.assertEqual(git_before, contents(git_dir))
                self.assertEqual(ledger_before, contents(fixture.work))
                self.assertEqual(tracked_before, (fixture.project / "tracked.txt").read_bytes())
                self.assertEqual([], list(Path(temporary).iterdir()))
            finally:
                for path in git_dir.rglob("*"):
                    path.chmod(path.stat().st_mode | 0o700)

    @patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"})
    def test_attempt_inspection_keeps_caller_owned_integration_relation(self) -> None:
        fixture = self.checkpoint_fixture()
        self.record_ready(fixture, fixture.candidate_revision)
        reconciliation: JsonObject = {
            "target_revision": "caller-observed-target",
            "relation": "candidate-integrated",
            "phase": "cleanup",
            "effects": [
                {"effect": "source-checkout", "status": "allowed"},
                {"effect": "shared-work-root", "status": "not-required"},
                {"effect": "git-metadata", "status": "allowed"},
            ],
        }
        with patch(
            "pinboard.adapters.files.root.observe_integration_content",
            side_effect=AssertionError("unexpected Git read"),
        ):
            inspected = call_advertised_tool(
                mcp_server.ATTEMPT_INSPECT_TOOL,
                {**self.roots(fixture), "attempt_id": "work-a-1", "reconciliation": reconciliation},
            )
        self.assertEqual("ok", inspected["status"], inspected)
        operation = self.json_object(self.json_object(inspected["continuation"])["next_operation"])
        self.assertEqual("repository-cleanup", operation["kind"])

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
