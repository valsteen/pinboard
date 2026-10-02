"""The integration leaf reports whether a reviewed candidate's accepted diff reached a caller-named target."""

import contextlib
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import override
from unittest.mock import patch

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files import contributor_traces, root
from pinboard.adapters.files.errors import RootError, RootErrorCode
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import work_brief_models
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import common as mcp_common
from pinboard.mcp import execution as mcp_execution
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import JsonObject

FIXED_GIT_DATE = "2030-01-01T00:00:00+00:00"
COMPLETED_AT = datetime(2030, 1, 3, 4, 5, 6, tzinfo=UTC)
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Pinboard Tests",
    "GIT_AUTHOR_EMAIL": "pinboard@example.invalid",
    "GIT_COMMITTER_NAME": "Pinboard Tests",
    "GIT_COMMITTER_EMAIL": "pinboard@example.invalid",
    "GIT_AUTHOR_DATE": FIXED_GIT_DATE,
    "GIT_COMMITTER_DATE": FIXED_GIT_DATE,
}
NOTES = "".join(f"line {index}\n" for index in range(1, 13))


def git(cwd: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def git_bytes(cwd: Path, *arguments: str) -> bytes:
    return subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True).stdout


def commit(cwd: Path, message: str) -> str:
    git(cwd, "add", "--all")
    git(cwd, "commit", "--quiet", "-m", message)
    return git(cwd, "rev-parse", "HEAD")


def replace_line(path: Path, number: int, text: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[number - 1] = f"{text}\n"
    path.write_text("".join(lines), encoding="utf-8")


class FixedGitIdentity(unittest.TestCase):
    """Every commit in these tests uses a fixed identity and fixed author and committer dates."""

    @override
    def setUp(self) -> None:
        super().setUp()
        environment = patch.dict(os.environ, GIT_IDENTITY)
        environment.start()
        self.addCleanup(environment.stop)


class TargetContentReadTest(FixedGitIdentity):
    def repository(self) -> tuple[Path, str]:
        repository = Path(tempfile.mkdtemp()).resolve()
        git(repository, "init", "--quiet", "-b", "main")
        (repository / "notes.txt").write_text(NOTES, encoding="utf-8")
        (repository / "moved-from.txt").write_text("renamed content\n" * 4, encoding="utf-8")
        (repository / "image.bin").write_bytes(bytes(range(256)) * 4)
        base = commit(repository, "base")
        git(repository, "switch", "--quiet", "-c", "feature")
        replace_line(repository / "notes.txt", 2, "line 2 reviewed ")
        (repository / "moved-from.txt").rename(repository / "moved-to.txt")
        (repository / "image.bin").write_bytes(bytes(reversed(range(256))) * 4)
        commit(repository, "candidate")
        git(repository, "switch", "--quiet", "main")
        return repository, base

    def reviewed_diff(self, repository: Path, base: str) -> bytes:
        return git_bytes(repository, "diff", "--binary", base, "feature", "--")

    def squash_into_main(self, repository: Path) -> str:
        git(repository, "merge", "--quiet", "--squash", "feature")
        return commit(repository, "squash")

    def git_state(self, repository: Path) -> dict[str, bytes]:
        git_directory = repository / ".git"
        state = {
            str(path.relative_to(repository)): path.read_bytes()
            for path in sorted(repository.rglob("*"))
            if path.is_file()
        }
        state["<objects>"] = git_bytes(repository, "count-objects", "-v")
        state["<refs>"] = git_bytes(repository, "for-each-ref")
        state["<head>"] = (git_directory / "HEAD").read_bytes()
        return state

    def test_present_and_not_present_follow_the_target_tree(self) -> None:
        repository, base = self.repository()
        diff = self.reviewed_diff(repository, base)
        self.assertIn(b"rename from moved-from.txt", diff)
        self.assertIn(b"GIT binary patch", diff)
        self.assertEqual(root.ReviewedDiffAbsent(base), root.observe_reviewed_diff_at_target(repository, "main", diff))
        squashed = self.squash_into_main(repository)
        self.assertEqual(
            root.ReviewedDiffPresent(squashed), root.observe_reviewed_diff_at_target(repository, "main", diff)
        )
        self.assertEqual(root.ReviewedDiffAbsent(base), root.observe_reviewed_diff_at_target(repository, base, diff))
        self.assertEqual(squashed, root.resolve_target_commit(repository, "main"))

    def test_unresolved_target_and_non_git_directory_are_distinct(self) -> None:
        repository, base = self.repository()
        diff = self.reviewed_diff(repository, base)
        for target in ("never-created", "main:notes.txt", "0" * 40):
            with self.subTest(target=target):
                self.assertEqual(
                    root.TargetUnresolved(target), root.observe_reviewed_diff_at_target(repository, target, diff)
                )
        outside = Path(tempfile.mkdtemp()).resolve()
        with self.assertRaises(RootError) as raised:
            root.observe_reviewed_diff_at_target(outside, "main", diff)
        self.assertEqual(RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE, raised.exception.code)

    def test_other_git_failures_remain_root_errors(self) -> None:
        repository, base = self.repository()
        with self.assertRaises(RootError) as corrupt:
            root.observe_reviewed_diff_at_target(repository, "main", b"not a patch\n")
        self.assertEqual(RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE, corrupt.exception.code)
        missing_temporary = Path(tempfile.mkdtemp()).resolve() / "missing"
        with patch.object(tempfile, "tempdir", str(missing_temporary)), self.assertRaises(RootError) as denied:
            root.observe_reviewed_diff_at_target(repository, "main", self.reviewed_diff(repository, base))
        self.assertIn("private temporary index", str(denied.exception))

    def test_user_whitespace_configuration_does_not_change_the_verdict(self) -> None:
        repository, base = self.repository()
        git(repository, "config", "apply.whitespace", "error")
        git(repository, "config", "apply.ignoreWhitespace", "change")
        diff = self.reviewed_diff(repository, base)
        self.assertIn(b"+line 2 reviewed \n", diff)
        squashed = self.squash_into_main(repository)
        self.assertEqual(
            root.ReviewedDiffPresent(squashed), root.observe_reviewed_diff_at_target(repository, "main", diff)
        )
        replace_line(repository / "notes.txt", 2, "line 2 reviewed")
        whitespace_only = commit(repository, "whitespace-only change")
        self.assertEqual(
            root.ReviewedDiffAbsent(whitespace_only), root.observe_reviewed_diff_at_target(repository, "main", diff)
        )

    def test_read_only_git_metadata_stays_unchanged_and_no_temporary_directory_remains(self) -> None:
        repository, base = self.repository()
        diff = self.reviewed_diff(repository, base)
        squashed = self.squash_into_main(repository)
        git(repository, "config", "core.splitIndex", "true")
        git(repository, "update-index", "--split-index")
        before = self.git_state(repository)
        private_temporary = Path(tempfile.mkdtemp()).resolve()
        git_paths = [repository / ".git", *(repository / ".git").rglob("*")]
        modes = {path: path.stat().st_mode for path in git_paths}
        for path in git_paths:
            path.chmod(stat.S_IMODE(modes[path]) & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        try:
            with patch.object(tempfile, "tempdir", str(private_temporary)):
                self.assertEqual(
                    root.ReviewedDiffPresent(squashed),
                    root.observe_reviewed_diff_at_target(repository, "main", diff),
                )
                self.assertEqual(
                    root.ReviewedDiffAbsent(base), root.observe_reviewed_diff_at_target(repository, base, diff)
                )
        finally:
            for path in reversed(git_paths):
                path.chmod(stat.S_IMODE(modes[path]))
        self.assertEqual(before, self.git_state(repository))
        self.assertEqual([], list(private_temporary.iterdir()))


class ItemIntegrationLeafTest(FixedGitIdentity, CheckpointPackageSupport):
    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def integration(self, fixture: CheckpointFixture, target: str, item_id: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": item_id, "target": target}},
        )

    def transition(self, fixture: CheckpointFixture, action_id: str, payload: JsonObject) -> JsonObject:
        result = self.transition_result(fixture, self.project_action(fixture, action_id), payload)
        self.assertIn(result["status"], ("committed", "committed-with-warning"), result)
        return result

    def presence(self, fixture: CheckpointFixture, target: str, expected: str, item_id: str = "work-a") -> JsonObject:
        result = self.integration(fixture, target, item_id)
        self.assertEqual("pinboard-item-integration/v1", result.get("schema"), result)
        self.assertEqual((item_id, target, expected), (result["item_id"], result["target"], result["presence"]))
        self.assertEqual(git(fixture.project, "rev-parse", f"{target}^{{commit}}"), result["target_revision"])
        return result

    def source(self, result: JsonObject) -> JsonObject:
        return self.json_object(result["source"])

    def target_worktree(self, fixture: CheckpointFixture, start: str) -> Path:
        target = Path(tempfile.mkdtemp()).resolve() / "target"
        git(fixture.project, "worktree", "add", "--quiet", "-b", "main", str(target), start)
        return target

    def rejection(self, result: JsonObject, code: str) -> dict[str, object]:
        self.assertEqual(
            ("pinboard-mcp-item-status-result/v3", "rejected", code),
            (
                result["schema"],
                result["status"],
                result["code"],
            ),
            result,
        )
        self.assertFalse(result["state_changed"])
        self.assertEqual([], result["changed_surfaces"])
        return {
            str(self.json_object(value)["field"]): self.json_object(value)["value"]
            for value in self.json_array(result["observed"])
        }

    def return_for_review(self, fixture: CheckpointFixture) -> None:
        self.transition(fixture, "return-for-correction:work-a-1", {"reason": "Rework the candidate."})

    def submit_working_tree(self, fixture: CheckpointFixture, worker: str) -> str:
        observed = call_native_tool(
            mcp_server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"}
        )
        candidate = observed["candidate"]
        assert isinstance(candidate, str), observed
        lease = self.native_attempt_acquire(fixture, worker)
        submission = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        submitted = self.transition_result(fixture, submission, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)
        return candidate

    def multiline_candidate(self) -> tuple[CheckpointFixture, str, str]:
        """Resubmit a working-tree candidate that edits line 2 of a twelve-line file on a committed preimage."""

        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture)
        (fixture.project / "tracked.txt").write_text("base\n", encoding="utf-8")
        (fixture.project / "notes.txt").write_text(NOTES, encoding="utf-8")
        preimage = commit(fixture.project, "reviewed context")
        replace_line(fixture.project / "notes.txt", 2, "line 2 reviewed")
        candidate = self.submit_working_tree(fixture, "worker-multiline")
        return fixture, preimage, candidate

    def close_prerequisite(self, fixture: CheckpointFixture) -> None:
        closed, stdout, stderr = self.run_cli(
            *fixture.common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "Closed directly.",
            "--task-id",
            "review-owner",
            "--host-id",
            "local",
        )
        self.assertEqual(0, closed, f"{stdout}\n{stderr}")

    def rebind(self, fixture: CheckpointFixture, revision: int) -> None:
        brief = replace_struct(fixture.brief, artifact_revision=revision)
        published = call_native_tool(
            mcp_server.BRIEF_PUBLISH_TOOL,
            {**self.roots(fixture), "brief": self.json_object(msgspec.json.decode(msgspec.json.encode(brief)))},
        )
        self.assertEqual("committed", published["status"], published)
        self.transition(
            fixture,
            "rebind-attempt:work-a-1",
            {
                "branch": brief.branch,
                "base_revision": brief.base_revision,
                "brief_artifact_ref_id": self.json_object(published["reference"])["artifact_ref_id"],
            },
        )

    def complete(self, fixture: CheckpointFixture) -> None:
        fixture = self.terminalize_brief(fixture)
        self.record_ready(fixture)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        with patch("pinboard.mcp.mutation_operations.datetime") as clock:
            clock.now.return_value = COMPLETED_AT
            self.transition(
                fixture,
                "complete:work-a-1",
                {
                    "schema": "pinboard-reviewed-completion/v2",
                    "candidate": fixture.candidate_revision,
                    "evidence": "Accepted by the maintainer.",
                    "reviewer_task_id": "independent-reviewer",
                    "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
                    "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
                    "packages": [],
                },
            )

    def test_protected_working_tree_candidate_is_found_after_squash_merge(self) -> None:
        fixture, preimage, candidate = self.multiline_candidate()
        target = self.target_worktree(fixture, preimage)
        at_base = self.presence(fixture, "main", "content-not-present")
        self.assertEqual(
            {
                "kind": "protected-review",
                "attempt_id": "work-a-1",
                "candidate_revision": candidate,
                "compared_from_revision": preimage,
            },
            self.source(at_base),
        )
        commit(fixture.project, "commit the reviewed working tree")
        git(target, "merge", "--quiet", "--squash", fixture.brief.branch)
        squashed = commit(target, "squash the reviewed change")
        self.assertNotEqual(
            0,
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", fixture.brief.branch, "main"],
                cwd=fixture.project,
                check=False,
            ).returncode,
        )
        present = self.presence(fixture, "main", "content-present")
        self.assertEqual(squashed, present["target_revision"])
        self.assertEqual(self.source(at_base), self.source(present))

        git(fixture.project, "update-ref", "refs/remotes/origin/main", squashed)
        self.presence(fixture, "origin/main", "content-present")
        self.presence(fixture, squashed, "content-present")

        replace_line(target / "notes.txt", 11, "line 11 edited later")
        commit(target, "later non-overlapping edit")
        self.presence(fixture, "main", "content-present")
        replace_line(target / "notes.txt", 2, "line 2 edited again")
        commit(target, "later overlapping edit")
        self.presence(fixture, "main", "content-not-present")
        self.presence(fixture, "origin/main", "content-present")

    def test_protected_commit_candidate_is_found_by_every_integration_style(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        base = fixture.brief.base_revision
        candidate = fixture.candidate_revision
        target = self.target_worktree(fixture, base)
        at_base = self.presence(fixture, "main", "content-not-present")
        self.assertEqual(
            {
                "kind": "protected-review",
                "attempt_id": "work-a-1",
                "candidate_revision": candidate,
                "compared_from_revision": base,
            },
            self.source(at_base),
        )
        git(target, "merge", "--quiet", "--ff-only", candidate)
        self.presence(fixture, "main", "content-present")

        git(target, "reset", "--quiet", "--hard", base)
        (target / "other.txt").write_text("unrelated\n", encoding="utf-8")
        unrelated = commit(target, "unrelated")
        git(target, "merge", "--quiet", "--no-ff", "-m", "merge the reviewed change", candidate)
        self.presence(fixture, "main", "content-present")

        git(target, "reset", "--quiet", "--hard", unrelated)
        git(target, "cherry-pick", candidate)
        rebased = self.presence(fixture, "main", "content-present")
        self.assertNotEqual(candidate, rebased["target_revision"])
        self.assertNotEqual(
            0,
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", candidate, "main"], cwd=fixture.project, check=False
            ).returncode,
        )

    def test_accepted_checkpoint_source_survives_resume_rebind_and_return(self) -> None:
        fixture = self.accepted_package_fixture()
        checkpoint = fixture.brief.checkpoint.checkpoint_id
        target = self.target_worktree(fixture, fixture.brief.base_revision)
        (target / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        commit(target, "squash the accepted checkpoint")
        expected = {
            "kind": "accepted-checkpoint",
            "attempt_id": "work-a-1",
            "checkpoint": checkpoint,
            "candidate_revision": fixture.candidate_revision,
            "compared_from_revision": fixture.brief.base_revision,
        }
        self.assertEqual(expected, self.source(self.presence(fixture, "main", "content-present")))
        self.close_prerequisite(fixture)
        self.transition(fixture, "resume:work-a", {})
        self.assertEqual(expected, self.source(self.presence(fixture, "main", "content-present")))
        self.rebind(fixture, fixture.brief.artifact_revision + 1)
        self.assertEqual(expected, self.source(self.presence(fixture, "main", "content-present")))

        (fixture.project / "tracked.txt").write_text("second checkpoint\n", encoding="utf-8")
        second = self.submit_working_tree(fixture, "worker-second")
        submitted = self.presence(fixture, "main", "content-not-present")
        self.assertEqual(
            ("protected-review", second), (self.source(submitted)["kind"], self.source(submitted)["candidate_revision"])
        )
        self.return_for_review(fixture)
        self.assertEqual(expected, self.source(self.presence(fixture, "main", "content-present")))

    def test_completion_source_is_the_closing_candidate(self) -> None:
        fixture = self.checkpoint_fixture()
        self.complete(fixture)
        target = self.target_worktree(fixture, fixture.brief.base_revision)
        completed = self.presence(fixture, "main", "content-not-present")
        self.assertEqual(
            {
                "kind": "completion",
                "attempt_id": "work-a-1",
                "candidate_revision": fixture.candidate_revision,
                "compared_from_revision": fixture.brief.base_revision,
            },
            self.source(completed),
        )
        (target / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        commit(target, "squash the completed change")
        self.assertEqual(self.source(completed), self.source(self.presence(fixture, "main", "content-present")))
        item = call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "item", "item_id": "work-a"}},
        )
        self.assertEqual("pinboard-item-status/v3", item["schema"])
        self.assertNotIn("presence", self.json_object(item["closure"]))
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE attempts SET candidate_recorded_at = ? WHERE attempt_id = 'work-a-1'",
                ("2020-01-01T00:00:00+00:00",),
            )
        self.assertEqual(
            {"item_id": "work-a", "item_state": "done", "attempt_id": "work-a-1", "reason": "pre-snapshot-candidate"},
            self.rejection(self.integration(fixture, "main"), "INTEGRATION_CANDIDATE_UNAVAILABLE"),
        )

    def test_empty_recorded_diff_is_no_change_without_a_content_check(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture)
        (fixture.project / "tracked.txt").write_text("base\n", encoding="utf-8")
        candidate = self.submit_working_tree(fixture, "worker-empty")
        git(fixture.project, "branch", "main", fixture.brief.base_revision)
        with patch.object(root, "observe_reviewed_diff_at_target", side_effect=AssertionError("no content check")):
            result = self.presence(fixture, "main", "no-change")
        self.assertEqual(candidate, self.source(result)["candidate_revision"])
        self.assertEqual(
            "INTEGRATION_TARGET_UNRESOLVED",
            self.integration(fixture, "never-created")["code"],
        )

    def test_candidates_without_accepted_snapshot_bytes_are_unavailable(self) -> None:
        fixture = self.checkpoint_fixture()
        git(fixture.project, "branch", "main", fixture.brief.base_revision)
        ready = self.rejection(self.integration(fixture, "main", "work-c"), "INTEGRATION_CANDIDATE_UNAVAILABLE")
        self.assertEqual(
            {"item_id": "work-c", "item_state": "ready", "attempt_id": None, "reason": "no-reviewed-candidate"}, ready
        )
        self.close_prerequisite(fixture)
        direct = self.integration(fixture, "main", "work-c")
        self.assertEqual(
            "closed-without-completion", self.rejection(direct, "INTEGRATION_CANDIDATE_UNAVAILABLE")["reason"]
        )

        self.transition(
            fixture,
            "accept-review-and-continue:work-a-1",
            {"candidate": fixture.candidate_revision, "evidence": "Accepted; continue the attempt."},
        )
        continued = self.integration(fixture, "main")
        self.assertEqual(
            {"item_id": "work-a", "item_state": "active", "attempt_id": "work-a-1", "reason": "no-reviewed-candidate"},
            self.rejection(continued, "INTEGRATION_CANDIDATE_UNAVAILABLE"),
        )
        self.assertEqual(("unchanged", "correct-input"), (continued["effect"], continued["retry"]))
        self.assertIn("operation item", str(continued["recovery"]))

        returned = self.checkpoint_fixture()
        self.return_for_review(returned)
        git(returned.project, "branch", "main", returned.brief.base_revision)
        self.assertEqual(
            "no-reviewed-candidate",
            self.rejection(self.integration(returned, "main"), "INTEGRATION_CANDIDATE_UNAVAILABLE")["reason"],
        )

    def test_retained_patch_checkpoint_package_is_unavailable(self) -> None:
        fixture = self.accepted_package_fixture(local=True, candidate_form="current-head")
        git(fixture.project, "branch", "main", fixture.brief.base_revision)
        self.retain_v2_checkpoint(fixture)
        retained = self.integration(fixture, "main")
        self.assertEqual(
            {
                "item_id": "work-a",
                "item_state": "paused",
                "attempt_id": "work-a-1",
                "reason": "checkpoint-without-candidate-snapshot",
            },
            self.rejection(retained, "INTEGRATION_CANDIDATE_UNAVAILABLE"),
        )

    def test_target_and_request_rejections_are_typed(self) -> None:
        fixture = self.checkpoint_fixture()
        unresolved = self.integration(fixture, "never-created")
        self.assertEqual(
            {"target": "never-created", "project_root": str(fixture.project)},
            self.rejection(unresolved, "INTEGRATION_TARGET_UNRESOLVED"),
        )
        self.assertEqual(("unchanged", "correct-input"), (unresolved["effect"], unresolved["retry"]))
        self.assertIn("fetch", str(unresolved["recovery"]))

        option = self.integration(fixture, "--output=/tmp/x")
        self.assertEqual("ITEM_STATUS_INVALID", option["code"], option)
        self.assertEqual("pinboard-mcp-item-status-result/v3", option["schema"])

        missing = self.integration(fixture, "main", "never-proposed")
        self.assertEqual(("ITEM_NOT_FOUND", "pinboard-mcp-item-status-result/v3"), (missing["code"], missing["schema"]))

        outside = Path(tempfile.mkdtemp()).resolve()
        not_git = call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "project_root": str(outside),
                    "work_root": str(fixture.work),
                    "operation": "integration",
                    "item_id": "work-a",
                    "target": "main",
                }
            },
        )
        observed = self.rejection(not_git, "PROJECT_GIT_ROOT_UNAVAILABLE")
        self.assertEqual(str(outside), observed["project_root"])
        self.assertIn("not a git repository", str(observed["diagnostic"]).lower())
        self.assertEqual(("unchanged", "correct-input"), (not_git["effect"], not_git["retry"]))

    def test_unwritable_temporary_directory_is_a_typed_git_checkout_rejection(self) -> None:
        fixture = self.checkpoint_fixture()
        git(fixture.project, "branch", "main", fixture.brief.base_revision)
        missing_temporary = Path(tempfile.mkdtemp()).resolve() / "missing"
        with patch.object(tempfile, "tempdir", str(missing_temporary)):
            result = self.integration(fixture, "main")
        observed = self.rejection(result, "PROJECT_GIT_CHECKOUT_UNAVAILABLE")
        self.assertEqual(str(fixture.project), observed["project_root"])
        self.assertIn("private temporary index", str(observed["diagnostic"]))
        self.assertEqual(("unchanged", "correct-input"), (result["effect"], result["retry"]))

    def test_altered_snapshot_bytes_are_invalid_evidence(self) -> None:
        fixture = self.checkpoint_fixture()
        git(fixture.project, "branch", "main", fixture.brief.base_revision)
        context = SQLiteWorkStore(fixture.work / "state.sqlite3").read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        artifact = fixture.work / context.reference.selector
        artifact.chmod(0o644)
        artifact.write_bytes(artifact.read_bytes().replace(b"candidate", b"tampered!"))
        result = self.integration(fixture, "main")
        observed = self.rejection(result, "INTEGRATION_CANDIDATE_EVIDENCE_INVALID")
        self.assertEqual(
            {
                "attempt_id": "work-a-1",
                "artifact_ref_id": int(context.reference.artifact_ref_id),
                "selector": context.reference.selector,
                "sha256": context.reference.content_sha256,
            },
            observed,
        )
        self.assertEqual(("unchanged", "do-not-retry"), (result["effect"], result["retry"]))
        self.assertIn("pinboard validate", str(result["recovery"]))

        accepted = self.accepted_package_fixture()
        git(accepted.project, "branch", "main", accepted.brief.base_revision)
        package_artifact = accepted.work / accepted.package_reference.selector
        package_bytes = package_artifact.read_bytes()
        package_artifact.chmod(0o644)
        package_artifact.write_bytes(package_bytes + b" ")
        damaged_package = self.rejection(self.integration(accepted, "main"), "INTEGRATION_CANDIDATE_EVIDENCE_INVALID")
        self.assertEqual(accepted.package_reference.selector, damaged_package["selector"])
        package_artifact.write_bytes(package_bytes)
        package = self.package(accepted)
        assert isinstance(package, work_brief_models.CheckpointReviewPackageV3)
        candidate_artifact = accepted.work / package.candidate_snapshot.selector
        candidate_artifact.chmod(0o644)
        candidate_artifact.write_bytes(b"{}")
        checkpoint = self.rejection(self.integration(accepted, "main"), "INTEGRATION_CANDIDATE_EVIDENCE_INVALID")
        self.assertEqual(
            ("work-a-1", package.candidate_snapshot.selector), (checkpoint["attempt_id"], checkpoint["selector"])
        )

    def test_damaged_checkpoint_receipt_keeps_the_damaged_receipt_rejection(self) -> None:
        fixture = self.accepted_package_fixture()
        git(fixture.project, "branch", "main", fixture.brief.base_revision)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            history_id = connection.execute(
                "SELECT MAX(history_id) FROM transition_history WHERE action_kind = 'accept-checkpoint'"
            ).fetchone()[0]
            connection.execute(
                "UPDATE transition_history SET outcome_json = ? WHERE history_id = ?",
                ('{"unexpected":true}', history_id),
            )
        result = self.integration(fixture, "main")
        self.assertEqual("TRANSITION_RECEIPT_DAMAGED", result["code"], result)
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"])
        self.assertEqual(("unchanged", "do-not-retry"), (result["effect"], result["retry"]))

    def record_ready(self, fixture: CheckpointFixture) -> None:
        self.record_commissioned_review(fixture, fixture.candidate_revision, "independent-reviewer")

    def test_attempt_inspection_reconciliation_runs_no_integration_read(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        self.record_ready(fixture)
        git(fixture.project, "branch", "main", fixture.candidate_revision)
        integrated = self.presence(fixture, "main", "content-present")

        def inspect(phase: str, effects: tuple[str, str, str]) -> JsonObject:
            inspected = call_advertised_tool(
                mcp_server.ATTEMPT_INSPECT_TOOL,
                {
                    **self.roots(fixture),
                    "attempt_id": "work-a-1",
                    "reconciliation": {
                        "target_revision": integrated["target_revision"],
                        "relation": "candidate-integrated",
                        "phase": phase,
                        "effects": [
                            {"effect": effect, "status": status}
                            for effect, status in zip(
                                ("source-checkout", "shared-work-root", "git-metadata"), effects, strict=True
                            )
                        ],
                    },
                },
            )
            self.assertEqual("ok", inspected["status"], inspected)
            return self.json_object(self.json_object(inspected["continuation"])["next_operation"])

        with (
            patch.object(root, "observe_reviewed_diff_at_target", side_effect=AssertionError("no content check")),
            patch.object(root, "resolve_target_commit", side_effect=AssertionError("no target resolution")),
        ):
            cleanup = inspect("cleanup", ("allowed", "not-required", "allowed"))
            terminal = inspect("terminal", ("not-required", "allowed", "not-required"))
            overview = call_advertised_tool(mcp_server.OVERVIEW_TOOL, self.roots(fixture))
            item = call_advertised_tool(
                mcp_server.ITEM_STATUS_TOOL,
                {"request": {**self.roots(fixture), "operation": "item", "item_id": "work-a"}},
            )
        self.assertEqual({"kind": "repository-cleanup", "target_revision": integrated["target_revision"]}, cleanup)
        self.assertEqual({"target": "attempt", "action_kind": "complete"}, terminal["action"])
        self.assertEqual("pinboard-item-status/v3", item["schema"])
        self.assertNotIn("presence", json.dumps(overview))

    def test_integration_reads_stay_keyed_as_retained_history_grows(self) -> None:
        fixture = self.accepted_package_fixture()
        git(fixture.project, "branch", "main", fixture.brief.base_revision)
        statements: list[str] = []
        original_open = sqlite_store.open_database

        def traced_open(database_path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open(database_path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        def selects() -> list[str]:
            return [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]

        with patch.object(sqlite_store, "open_database", traced_open):
            self.presence(fixture, "main", "content-not-present")
        small = selects()
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            history = connection.execute("SELECT * FROM transition_history ORDER BY history_id LIMIT 1").fetchone()
            columns = [row[1] for row in connection.execute("PRAGMA table_info(transition_history)")]
            first = int(connection.execute("SELECT MAX(history_id) FROM transition_history").fetchone()[0]) + 1
            revision = int(connection.execute("SELECT MAX(project_revision) FROM transition_history").fetchone()[0])
            for offset in range(200):
                values = dict(zip(columns, history, strict=True))
                values.update(
                    history_id=first + offset,
                    project_revision=revision + 1000 + offset,
                    subject_id=f"retained-{offset}",
                    action_id=f"accept-checkpoint:retained-{offset}",
                    action_kind="accept-checkpoint",
                    outcome_schema="checkpoint-acceptance/v2",
                    artifact_ref_id=None,
                    artifact_kind=None,
                )
                connection.execute(
                    f"INSERT INTO transition_history ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                    tuple(values.values()),
                )
                connection.execute(
                    """
                    INSERT INTO artifact_refs (
                        artifact_ref_id, artifact_key, artifact_revision, kind, relative_path,
                        content_sha256, size_bytes, accepted_revision, created_at
                    )
                    SELECT artifact_ref_id + 10000 + ?, 'retained-' || ? || '-candidate', 1, 'evidence',
                           'retained/' || ? || '.json', content_sha256, size_bytes, accepted_revision, created_at
                    FROM artifact_refs ORDER BY artifact_ref_id LIMIT 1
                    """,
                    (offset, offset, offset),
                )
        statements.clear()
        with patch.object(sqlite_store, "open_database", traced_open):
            self.presence(fixture, "main", "content-not-present")
        self.assertEqual(len(small), len(selects()))
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            for statement in selects():
                if "sqlite_master" in statement or "PRAGMA" in statement.upper():
                    continue
                plan = " ".join(str(row[3]).upper() for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}"))
                for table in ("TRANSITION_HISTORY", "ATTEMPTS", "ARTIFACT_REFS", "WORK_ITEMS"):
                    self.assertNotIn(f"SCAN {table}", plan, statement)

    def test_integration_leaf_uses_the_item_trace_override(self) -> None:
        fixture = self.checkpoint_fixture()
        (fixture.work / contributor_traces.SETTINGS_NAME).write_text(
            '[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = off\n[item "work-a"]\n\tmode = on\n',
            encoding="utf-8",
        )
        capture = mcp_execution.AutomaticCapture(mcp_common.select_capture_item)
        for item_id, captured in (("work-a", True), ("work-c", False)):
            with self.subTest(item_id=item_id):
                selected = capture.resolve(
                    str(fixture.project),
                    {
                        "request": {
                            **self.roots(fixture),
                            "operation": "integration",
                            "item_id": item_id,
                            "target": "main",
                        }
                    },
                )
                self.assertEqual(captured, selected is not None)
