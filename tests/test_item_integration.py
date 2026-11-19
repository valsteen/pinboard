"""Reviewed candidates record their diff from the accepted base, and integration finds that diff at a named target."""

import contextlib
import dataclasses
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
from pinboard.application import action_models, candidate_snapshots, work_brief_models
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import common as mcp_common
from pinboard.mcp import execution as mcp_execution
from pinboard.mcp import server as mcp_server
from tests.candidate_diff_support import historical_candidate_diffs
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, native_tools
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
    def native_fixture[T: CheckpointFixture](self, fixture: T) -> T:
        native = self.enterContext(native_tools())
        return dataclasses.replace(
            fixture,
            call_native_tool=native.call_native_tool,
            call_advertised_tool=native.call_advertised_tool,
        )

    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def integration(self, fixture: CheckpointFixture, target: str, item_id: str = "work-a") -> JsonObject:
        return fixture.call_advertised_tool(
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
        observed = fixture.call_native_tool(
            mcp_server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"}
        )
        candidate = observed["candidate"]
        assert isinstance(candidate, str), observed
        lease = self.native_attempt_acquire(fixture, worker)
        submission = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        submitted = self.transition_result(fixture, submission, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)
        return candidate

    def observe(self, fixture: CheckpointFixture) -> JsonObject:
        observed = fixture.call_advertised_tool(
            mcp_server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"}
        )
        self.assertEqual(
            ("pinboard-mcp-candidate-observation-result/v2", "observed"), (observed["schema"], observed["status"])
        )
        return observed

    def submit(self, fixture: CheckpointFixture, lease: JsonObject, candidate: str) -> JsonObject:
        submission = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        return self.transition_result(fixture, submission, {"candidate": candidate})

    def protected_snapshot(self, fixture: CheckpointFixture) -> candidate_snapshots.CandidateSnapshot:
        context = SQLiteWorkStore(fixture.work / "state.sqlite3").read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        return candidate_snapshots.decode_candidate_snapshot((fixture.work / context.reference.selector).read_bytes())

    def multiline_candidate(self) -> tuple[CheckpointFixture, str, str]:
        """Retain a working-tree snapshot that edits line 2 of a twelve-line file on a committed preimage."""

        fixture = self.native_fixture(self.checkpoint_fixture())
        self.return_for_review(fixture)
        (fixture.project / "tracked.txt").write_text("base\n", encoding="utf-8")
        (fixture.project / "notes.txt").write_text(NOTES, encoding="utf-8")
        preimage = commit(fixture.project, "reviewed context")
        replace_line(fixture.project / "notes.txt", 2, "line 2 reviewed")
        candidate = self.retain_working_tree_review(fixture)
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
        published = fixture.call_native_tool(
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

    def prepare_completion(self, fixture: CheckpointFixture) -> tuple[CheckpointFixture, JsonObject]:
        fixture = self.terminalize_brief(fixture)
        self.record_ready(fixture)
        return fixture, self.completion_payload(fixture)

    def completion_payload(self, fixture: CheckpointFixture) -> JsonObject:
        attempt_root = fixture.work / "attempts" / "work-a-1"
        return {
            "schema": "pinboard-reviewed-completion/v3",
            "human_decision": "Complete this item with its reviewed outcome.",
            "candidate": fixture.candidate_revision,
            "evidence": "Accepted by the maintainer.",
            "reviewer_task_id": "independent-reviewer",
            "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
            "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
            "packages": [],
        }

    def complete(self, fixture: CheckpointFixture, payload: JsonObject) -> JsonObject:
        action = self.project_action(fixture, "complete:work-a-1")
        with patch("pinboard.mcp.mutation_operations.datetime") as clock:
            clock.now.return_value = COMPLETED_AT
            return fixture.call_advertised_tool(
                mcp_server.TRANSITION_TOOL, self.native_transition_request(fixture, action, payload)
            )

    def landing_review(self, fixture: CheckpointFixture, choice: JsonObject) -> JsonObject:
        return fixture.call_advertised_tool(mcp_server.REVIEW_JOB_TOOL, {**self.roots(fixture), "review": choice})

    def external_landing_review(self, subject: JsonObject, commission: str, coverage: str) -> JsonObject:
        return {
            "schema": "pinboard-landing-difference-review/v1",
            "subject": subject,
            "commission": {
                "kind": "external",
                "commissioning_task_id": "human-coordinator",
                "human_commission": commission,
            },
            "reviewer_task_id": "independent-landing-reviewer",
            "verdict": "ready",
            "verified_target_baseline": True,
            "covered_complete_difference": True,
            "coverage": coverage,
        }

    def assert_side_parent_refused(
        self, fixture: CheckpointFixture, wrong_selection: JsonObject, narrow_review: JsonObject
    ) -> None:
        before = fixture.store.validated_snapshot()
        leaves: tuple[JsonObject, ...] = (
            {"kind": "observe-landing-difference"},
            {"kind": "commission-landing-difference", "runtime": "codex", "background": False},
            {"kind": "record-landing-difference", "review": narrow_review},
        )
        for leaf in leaves:
            wrong = self.landing_review(fixture, wrong_selection | leaf)
            self.assertEqual(("rejected", "unchanged"), (wrong["status"], wrong["effect"]), wrong)
            self.assertIn("First-parent depths", str(wrong["message"]))
        self.assertEqual(before, fixture.store.validated_snapshot())

    def commission_and_record_landing_review(
        self, fixture: CheckpointFixture, selection: JsonObject, review: JsonObject, decision: str
    ) -> JsonObject:
        job = self.landing_review(
            fixture, selection | {"kind": "commission-landing-difference", "runtime": "codex", "background": False}
        )
        self.assertEqual("landing-difference-commissioned", job["status"], job)
        self.assertEqual(review["subject"], job["subject"])
        prompt = self.json_object(job["prompt_reference"])
        prompt_text = (fixture.work / str(prompt["selector"])).read_text(encoding="utf-8")
        self.assertIn(decision, prompt_text)
        accepted = self.landing_review(
            fixture,
            selection
            | {
                "kind": "record-landing-difference",
                "review": review
                | {
                    "commission": {
                        "kind": "pinboard",
                        "prompt": {
                            "artifact_ref_id": prompt["accepted_artifact_reference_id"],
                            "sha256": prompt["sha256"],
                        },
                    }
                },
            },
        )
        self.assertEqual("landing-difference-recorded", accepted["status"], accepted)
        return accepted

    def test_protected_working_tree_candidate_is_found_after_squash_merge(self) -> None:
        with historical_candidate_diffs("7"):
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
        fixture = self.native_fixture(self.checkpoint_fixture(candidate_form="current-head"))
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
        fixture = self.native_fixture(self.accepted_package_fixture())
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
        fixture, payload = self.prepare_completion(self.native_fixture(self.checkpoint_fixture()))
        result = self.complete(fixture, payload)
        self.assertEqual("committed", result["status"], result)
        self.assertNotIn("local_cleanup", result)
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
        item = fixture.call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "item", "item_id": "work-a"}},
        )
        self.assertEqual("pinboard-item-status/v7", item["schema"])
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

    def test_landed_completion_returns_local_disposition_after_squash_and_fresh_reload(self) -> None:
        for retain in (False, True):
            with self.subTest(retain=retain):
                fixture, payload = self.prepare_completion(
                    self.native_fixture(self.checkpoint_fixture(candidate_form="current-head"))
                )
                target = self.target_worktree(fixture, fixture.brief.base_revision)
                (target / "baseline.txt").write_text("already on the landing target\n", encoding="utf-8")
                pre_landing = commit(target, "target-only work before landing")
                git(target, "merge", "--quiet", "--squash", fixture.brief.branch)
                landed_revision = commit(target, "squash the reviewed commit")
                self.assertNotEqual(fixture.candidate_revision, landed_revision)
                (target / "later.txt").write_text("later unrelated work\n", encoding="utf-8")
                commit(target, "later target change")
                landed: JsonObject = {
                    "target": "main",
                    "pre_landing_revision": pre_landing,
                    "landed_revision": landed_revision,
                    "target_depths": {"pre_landing": 2, "landed": 1},
                }
                if retain:
                    landed["retain_local_resources"] = True
                payload.update(human_decision="It's merged. Close it.", landed=landed)

                result = self.complete(fixture, payload)
                self.assertEqual("committed", result["status"], result)
                self.assertEqual(
                    {"branch": fixture.brief.branch, "disposition": "retain" if retain else "remove"},
                    result["local_cleanup"],
                )
                self.assertEqual(fixture.candidate_revision, git(fixture.project, "rev-parse", fixture.brief.branch))
                fresh = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
                item = next(item for item in fresh.lifecycle.work_items if item.item_id == "work-a")
                self.assertEqual("done", item.state.value)
                receipt = fresh.transition_receipts[-1]
                self.assertEqual(result["history_id"], int(receipt.history_id))
                self.assertEqual("pinboard-reviewed-completion/v3", receipt.input_schema)
                receipt_bytes = bytes(receipt.input_payload)
                plain_payload = {key: value for key, value in payload.items() if key != "landed"}
                self.assertEqual(msgspec.json.encode(plain_payload, order="sorted"), receipt_bytes)
                decoded = msgspec.json.decode(receipt_bytes, type=action_models.ReviewedCompletionReceiptPayload)
                self.assertEqual(receipt_bytes, msgspec.json.encode(decoded, order="sorted"))
                self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])

    def test_landed_completion_verifies_work_committed_on_the_attempt_branch(self) -> None:
        fixture = self.native_fixture(self.checkpoint_fixture())
        base = fixture.brief.base_revision
        self.return_for_review(fixture)
        (fixture.project / "feature.txt").write_text("committed feature\n", encoding="utf-8")
        head = commit(fixture.project, "commit the reviewed work")
        observed = self.observe(fixture)
        self.assertEqual((0, head), (observed["diff_size_bytes"], observed["preimage_revision"]))
        lease = self.native_attempt_acquire(fixture, "worker-committed")
        submitted = self.submit(fixture, lease, head)
        self.assertEqual("committed", submitted["status"], submitted)
        snapshot = self.protected_snapshot(fixture)
        self.assertEqual(("commit", head), (candidate_snapshots.candidate_kind(snapshot), snapshot.candidate))
        self.assertEqual(
            git_bytes(fixture.project, "diff", "--binary", "--full-index", base, head, "--"), snapshot.diff
        )
        self.assertIn(b"feature.txt", snapshot.diff)

        fixture, payload = self.prepare_completion(dataclasses.replace(fixture, candidate_revision=head))
        target = self.target_worktree(fixture, base)
        git(target, "merge", "--quiet", "--no-ff", "-m", "merge the reviewed branch", fixture.brief.branch)
        payload.update(
            human_decision="It's merged. Close it.",
            landed={
                "target": "main",
                "pre_landing_revision": base,
                "landed_revision": git(target, "rev-parse", "HEAD"),
            },
        )
        result = self.complete(fixture, payload)
        self.assertEqual("committed", result["status"], result)
        self.assertEqual({"branch": fixture.brief.branch, "disposition": "remove"}, result["local_cleanup"])
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        item = next(item for item in fresh.lifecycle.work_items if item.item_id == "work-a")
        attempt = next(value for value in fresh.lifecycle.attempts if value.attempt_id == "work-a-1")
        self.assertEqual(("done", "done", head), (item.state.value, attempt.state.value, attempt.candidate_revision))
        self.assertEqual(result["history_id"], int(fresh.transition_receipts[-1].history_id))

    def test_working_tree_identity_past_the_accepted_base_is_flagged_and_refused(self) -> None:
        fixture = self.native_fixture(self.checkpoint_fixture())
        base = fixture.brief.base_revision
        self.return_for_review(fixture)
        head = commit(fixture.project, "commit the reviewed work")
        lease = self.native_attempt_acquire(fixture, "worker-past-base")
        cases = (
            ("zero-byte", None, "submit-head-commit", f"submit HEAD {head} as a commit candidate"),
            ("non-empty", "uncommitted edit\n", "commit-tracked-changes", "Commit the tracked changes"),
        )
        for name, edit, step, instruction in cases:
            with self.subTest(diff=name):
                if edit is not None:
                    (fixture.project / "tracked.txt").write_text(edit, encoding="utf-8")
                observed = self.observe(fixture)
                self.assertEqual((head, base), (observed["preimage_revision"], observed["accepted_base_revision"]))
                self.assertEqual(edit is None, observed["diff_size_bytes"] == 0)
                submission = self.json_object(observed["submission"])
                self.assertEqual(
                    ("commit-required", head, step),
                    (submission["kind"], submission["head_revision"], submission["next_step"]),
                )
                self.assertIn(instruction, str(submission["message"]))
                before = fixture.store.validated_snapshot()
                artifacts_before = sorted((fixture.work / "artifacts").rglob("*"))
                rejected = self.submit(fixture, lease, str(observed["candidate"]))
                self.assertEqual(
                    ("rejected", "unchanged", "correct-input", []),
                    (rejected["status"], rejected["effect"], rejected["retry"], rejected["changed_surfaces"]),
                    rejected,
                )
                self.assertEqual(submission["message"], rejected["message"])
                self.assertIn(head, str(rejected["message"]))
                self.assertIn(instruction, str(rejected["message"]))
                self.assertIn({"field": "current_head_revision", "value": head}, self.json_array(rejected["observed"]))
                self.assertEqual(
                    [{"field": "preimage_revision", "expected": base, "observed": head}], rejected["mismatches"]
                )
                self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())
                self.assertEqual(artifacts_before, sorted((fixture.work / "artifacts").rglob("*")))
        git(fixture.project, "checkout", "--", "tracked.txt")
        submitted = self.submit(fixture, lease, head)
        self.assertEqual("committed", submitted["status"], submitted)
        self.assertEqual(
            git_bytes(fixture.project, "diff", "--binary", "--full-index", base, head, "--"),
            self.protected_snapshot(fixture).diff,
        )

    def test_working_tree_identity_on_the_accepted_base_stays_submittable(self) -> None:
        for name, contents in (("non-empty", None), ("zero-byte", "base\n")):
            with self.subTest(diff=name):
                fixture = self.native_fixture(self.checkpoint_fixture())
                self.return_for_review(fixture)
                if contents is not None:
                    (fixture.project / "tracked.txt").write_text(contents, encoding="utf-8")
                observed = self.observe(fixture)
                self.assertEqual(fixture.brief.base_revision, observed["preimage_revision"])
                self.assertEqual(contents is not None, observed["diff_size_bytes"] == 0)
                self.assertEqual({"kind": "submittable"}, observed["submission"])
                lease = self.native_attempt_acquire(fixture, f"worker-at-base-{name}")
                submitted = self.submit(fixture, lease, str(observed["candidate"]))
                self.assertEqual("committed", submitted["status"], submitted)
                snapshot = self.protected_snapshot(fixture)
                self.assertEqual(
                    ("working-tree", observed["candidate"], fixture.brief.base_revision),
                    (candidate_snapshots.candidate_kind(snapshot), snapshot.candidate, snapshot.preimage_revision),
                )

    def test_no_change_checkpoint_after_an_accepted_commit_submits_that_head_commit(self) -> None:
        fixture = self.native_fixture(self.accepted_package_fixture(candidate_form="current-head"))
        base = fixture.brief.base_revision
        accepted = fixture.candidate_revision
        self.close_prerequisite(fixture)
        self.transition(fixture, "resume:work-a", {})
        self.rebind(fixture, fixture.brief.artifact_revision + 1)
        self.assertEqual(accepted, git(fixture.project, "rev-parse", "HEAD"))
        observed = self.observe(fixture)
        self.assertEqual((0, accepted), (observed["diff_size_bytes"], observed["preimage_revision"]))
        self.assertEqual("submit-head-commit", self.json_object(observed["submission"])["next_step"])
        lease = self.native_attempt_acquire(fixture, "worker-no-change")
        submitted = self.submit(fixture, lease, accepted)
        self.assertEqual("committed", submitted["status"], submitted)
        snapshot = self.protected_snapshot(fixture)
        self.assertEqual(
            ("commit", accepted, base),
            (candidate_snapshots.candidate_kind(snapshot), snapshot.candidate, snapshot.accepted_base_revision),
        )
        self.assertEqual(
            git_bytes(fixture.project, "diff", "--binary", "--full-index", base, accepted, "--"), snapshot.diff
        )
        self.assertNotEqual(b"", snapshot.diff)

    def test_landed_completion_refuses_wrong_candidate_or_target_and_reports_recovery(self) -> None:
        fixture, payload = self.prepare_completion(self.native_fixture(self.checkpoint_fixture()))
        target = self.target_worktree(fixture, fixture.brief.base_revision)
        (target / "target.txt").write_text("Unrelated target change.\n", encoding="utf-8")
        unrelated = commit(target, "unrelated target change")
        landing: JsonObject = {
            "target": "main",
            "pre_landing_revision": fixture.brief.base_revision,
            "landed_revision": unrelated,
        }
        before = fixture.store.validated_snapshot()
        cases: tuple[tuple[JsonObject, str, str, str], ...] = (
            ({"landed": landing}, "no matching independent difference review", "correct-input", "pinboard_review_job"),
            (
                {"landed": landing | {"target": "missing-target"}},
                "Cannot resolve local landing target",
                "correct-input",
                "pinboard_actions",
            ),
            (
                {"landed": landing, "candidate": fixture.brief.base_revision},
                "protected candidate",
                "refresh-action",
                "pinboard_actions",
            ),
        )
        for changed, reason, retry, continuation in cases:
            with self.subTest(changed=changed):
                result = self.complete(fixture, payload | changed)
                self.assertEqual(("rejected", "unchanged"), (result["status"], result["effect"]), result)
                self.assertIn(reason, str(result["message"]))
                self.assertEqual(retry, result["retry"], result)
                self.assertIn(continuation, str(result["continuation"]))
                self.assertNotIn("local_cleanup", result)
                self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

        (target / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        landed_revision = commit(target, "squash the reviewed change")
        recovered = self.presence(fixture, "main", "content-present")
        self.assertEqual(fixture.candidate_revision, self.source(recovered)["candidate_revision"])
        result = self.complete(
            fixture,
            payload
            | {"landed": {"target": "main", "pre_landing_revision": unrelated, "landed_revision": landed_revision}},
        )
        self.assertEqual("committed", result["status"], result)
        self.assertEqual({"branch": fixture.brief.branch, "disposition": "remove"}, result["local_cleanup"])

    def test_landed_completion_requires_a_nonempty_reviewed_diff(self) -> None:
        fixture = self.native_fixture(self.checkpoint_fixture())
        self.return_for_review(fixture)
        (fixture.project / "tracked.txt").write_text("base\n", encoding="utf-8")
        candidate = self.submit_working_tree(fixture, "worker-empty")
        fixture, payload = self.prepare_completion(dataclasses.replace(fixture, candidate_revision=candidate))
        target = self.target_worktree(fixture, fixture.brief.base_revision)
        (target / "other.txt").write_text("Unreviewed work.\n", encoding="utf-8")
        landed_revision = commit(target, "land unreviewed work")
        before = fixture.store.validated_snapshot()
        result = self.complete(
            fixture,
            payload
            | {
                "landed": {
                    "target": "main",
                    "pre_landing_revision": fixture.brief.base_revision,
                    "landed_revision": landed_revision,
                }
            },
        )
        self.assertEqual(("rejected", "unchanged"), (result["status"], result["effect"]), result)
        self.assertIn("no matching independent difference review", str(result["message"]))
        self.assertEqual("correct-input", result["retry"], result)
        self.assertIn("observe-landing-difference", str(result["continuation"]))
        self.assertNotIn("local_cleanup", result)
        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

    def test_landing_with_extra_change_requires_exact_independent_range_review(self) -> None:  # noqa: PLR0915 - one range through all native leaves, completion and reload
        fixture, payload = self.prepare_completion(self.native_fixture(self.checkpoint_fixture()))
        base = fixture.brief.base_revision
        target = self.target_worktree(fixture, base)
        git(target, "switch", "--quiet", "-c", "unreviewed-side")
        (target / "extra.txt").write_text("Not part of the reviewed candidate.\n", encoding="utf-8")
        side_parent = commit(target, "unreviewed side-branch change")
        git(target, "switch", "--quiet", "main")
        git(target, "merge", "--quiet", "--no-ff", "--no-commit", "unreviewed-side")
        (target / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        landed = commit(target, "land reviewed work and an extra change")
        self.presence(fixture, "main", "content-present")
        depths: JsonObject = {"pre_landing": 2, "landed": 1}
        landing: JsonObject = {
            "target": "main",
            "pre_landing_revision": base,
            "landed_revision": landed,
            "target_depths": depths,
        }
        (target / "extra.txt").write_text("A later change on the target.\n", encoding="utf-8")
        newer = commit(target, "advance the target after landing")
        before = fixture.store.validated_snapshot()
        refused = self.complete(fixture, payload | {"landed": landing})
        self.assertEqual(("rejected", "unchanged"), (refused["status"], refused["effect"]))
        self.assertEqual(before, fixture.store.validated_snapshot())
        observations = {
            str(self.json_object(row)["field"]): self.json_object(row)["value"]
            for row in self.json_array(refused["observed"])
        }
        selection = {
            "attempt_id": observations["attempt_id"],
            "candidate_review_ref_id": observations["candidate_review_ref_id"],
            **landing,
        }
        self.assertEqual((2, 1), (observations["pre_landing_depth"], observations["landed_depth"]))
        observed = self.landing_review(fixture, {"kind": "observe-landing-difference", **selection})
        self.assertEqual("landing-difference-observed", observed["status"], observed)
        self.assertEqual((newer, depths), (observed["target_revision"], observed["target_depths"]))
        self.assertIs(observed["equivalent"], False)
        subject = self.json_object(observed["subject"])
        supplied = self.external_landing_review(
            subject,
            "Check independently what was added at landing.",
            "Verified baseline and exact commits. The reviewed patch is present. Reviewed extra.txt independently.",
        )
        omitted_anchors = {"pre_landing_revision": side_parent}
        narrow_diff = git_bytes(target, "diff", "--binary", side_parent, landed)
        self.assertIn(b"tracked.txt", narrow_diff)
        self.assertNotIn(b"extra.txt", narrow_diff)
        narrow_review = supplied | {
            "subject": subject | omitted_anchors | {"diff_sha256": hashlib.sha256(narrow_diff).hexdigest()},
            "coverage": "Reviewed the tracked.txt difference between the selected commits.",
        }
        narrow_selection = {key: value for key, value in selection.items() if key != "target_depths"} | omitted_anchors
        self.assert_side_parent_refused(fixture, narrow_selection, narrow_review)
        record: JsonObject = {"kind": "record-landing-difference", **selection}
        invalid_reviews: tuple[JsonObject, ...] = (
            {"subject": subject | {"diff_sha256": "f" * 64}},
            {"commission": {"kind": "pinboard", "prompt": {"artifact_ref_id": 1, "sha256": "f" * 64}}},
        )
        for bad in invalid_reviews:
            wrong = fixture.call_advertised_tool(
                mcp_server.REVIEW_JOB_TOOL, {**self.roots(fixture), "review": record | {"review": supplied | bad}}
            )
            self.assertEqual(("rejected", "unchanged"), (wrong["status"], wrong["effect"]), wrong)
            self.assertEqual(before, fixture.store.validated_snapshot())
        commission = fixture.call_advertised_tool(
            mcp_server.REVIEW_JOB_TOOL,
            {
                **self.roots(fixture),
                "review": {
                    "kind": "commission-landing-difference",
                    **selection,
                    "runtime": "codex",
                    "background": False,
                },
            },
        )
        self.assertEqual("landing-difference-commissioned", commission["status"], commission)
        self.assertEqual(subject, commission["subject"])
        self.assertEqual((newer, depths), (commission["target_revision"], commission["target_depths"]))
        accepted = fixture.call_advertised_tool(
            mcp_server.REVIEW_JOB_TOOL, {**self.roots(fixture), "review": record | {"review": supplied}}
        )
        self.assertEqual("landing-difference-recorded", accepted["status"], accepted)
        self.assertEqual((newer, depths), (accepted["target_revision"], accepted["target_depths"]))
        reviewed_landing = landing | {"difference_review_ref_id": accepted["artifact_ref_id"]}
        wrong_landing = {
            key: value for key, value in reviewed_landing.items() if key != "target_depths"
        } | omitted_anchors
        omitted_side = self.complete(fixture, payload | {"landed": wrong_landing})
        self.assertEqual(("rejected", "unchanged"), (omitted_side["status"], omitted_side["effect"]))
        self.assertIn("First-parent depths", str(omitted_side["message"]))
        stale = self.complete(
            fixture,
            payload
            | {
                "landed": reviewed_landing
                | {"landed_revision": newer, "target_depths": {"pre_landing": 2, "landed": 0}}
            },
        )
        self.assertEqual(("rejected", "unchanged"), (stale["status"], stale["effect"]))
        self.assertIn("no matching independent difference review", str(stale["message"]))
        completed = self.complete(fixture, payload | {"landed": reviewed_landing})
        self.assertEqual("committed", completed["status"], completed)
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])
        exported = self.run_json_cli(*fixture.common, "export")
        package = self.json_object(self.json_array(exported["completion_packages"])[0])
        evidence = self.json_object(package["landing"])
        self.assertEqual(subject["diff_sha256"], evidence["diff_sha256"])
        self.assertEqual((newer, depths), (evidence["target_revision"], evidence["target_depths"]))

    def resolve_input_policy(self, fixture: CheckpointFixture, human_decision: str) -> None:
        self.transition(
            fixture,
            "pause:work-a-1",
            {
                "schema": "pinboard-pause-decisions/v1",
                "reason": "Choose how the landed implementation should handle invalid input.",
                "decisions": [
                    {
                        "decision_id": "input-policy",
                        "question": "How should the implementation handle invalid input?",
                        "options": ["Replace invalid input.", "Reject invalid input."],
                        "recommendation": "Reject invalid input.",
                        "unblocks": "Review the changed input handling.",
                        "deferral_cost": "Landing review waits.",
                    }
                ],
            },
        )
        self.transition(
            fixture,
            "resume:work-a",
            {
                "schema": "pinboard-resolve-pause-decisions/v1",
                "brief_artifact_ref_id": None,
                "resolutions": [{"kind": "answer", "decision_id": "input-policy", "human_decision": human_decision}],
            },
        )

    def test_landing_review_qualifies_the_exact_same_brief_decisions_after_resume(self) -> None:
        fixture = self.native_fixture(self.checkpoint_fixture())
        self.close_prerequisite(fixture)
        self.record_ready(fixture)
        payload = self.completion_payload(fixture)
        self.transition(
            fixture,
            "accept-review-and-continue:work-a-1",
            {"candidate": fixture.candidate_revision, "evidence": "Accepted code. Continue with the owner checks."},
        )
        self.resolve_input_policy(fixture, "Replace invalid input.")
        target = self.target_worktree(fixture, fixture.brief.base_revision)
        (target / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        (target / "input-policy.txt").write_text("Reject invalid input.\n", encoding="utf-8")
        landing: JsonObject = {
            "target": "main",
            "pre_landing_revision": fixture.brief.base_revision,
            "landed_revision": commit(target, "land the reviewed patch and input-policy check"),
        }
        action = self.project_action(fixture, "complete:work-a-1")
        contract = self.json_object(action["input_contract"])
        historic = self.json_object(contract["continued_review"])
        accounting: JsonObject = {
            "kind": "continue",
            "brief_sha256": contract["accepted_brief_sha256"],
            "remaining_work": contract["remaining_work"],
            "required_step_ids": ["owner-checks"],
            "steps": [{"kind": "done", "step_id": "owner-checks", "evidence": "The owner checks are recorded."}],
            "obligations": [
                {"kind": "done", "obligation_id": "next-decision", "evidence": "The input decision is recorded."}
            ],
        }
        complete_payload = payload | {"accounting": accounting, "landed": landing}
        missing = self.complete(fixture, complete_payload)
        self.assertIn("no matching independent difference review", str(missing["message"]), missing)
        selection = {
            str(self.json_object(row)["field"]): self.json_object(row)["value"]
            for row in self.json_array(missing["observed"])
        }
        observe: JsonObject = {
            "kind": "observe-landing-difference",
            "attempt_id": "work-a-1",
            "candidate_review_ref_id": selection["candidate_review_ref_id"],
            **landing,
        }
        old = self.landing_review(fixture, observe)
        old_subject = self.json_object(old["subject"])
        self.assertEqual("landing-difference-observed", old["status"])
        old_review = self.external_landing_review(
            old_subject,
            "Check the landed difference against the recorded replacement policy.",
            "Checked the exact landed difference against the decision in effect when commissioned.",
        )
        record = observe | {"kind": "record-landing-difference"}
        accepted = self.landing_review(fixture, record | {"review": old_review})
        self.assertEqual("landing-difference-recorded", accepted["status"], accepted)
        self.resolve_input_policy(fixture, "Reject invalid input. Do not replace it.")

        before = fixture.store.validated_snapshot()
        refused_record = self.landing_review(fixture, record | {"review": old_review})
        self.assertEqual(("rejected", "unchanged"), (refused_record["status"], refused_record["effect"]))
        refused_completion = self.complete(
            fixture, complete_payload | {"landed": landing | {"difference_review_ref_id": accepted["artifact_ref_id"]}}
        )
        self.assertEqual(("rejected", "unchanged"), (refused_completion["status"], refused_completion["effect"]))
        self.assertIn("current recorded decisions", str(refused_completion["message"]))
        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

        current = self.landing_review(fixture, observe)
        current_subject = self.json_object(current["subject"])
        self.assertEqual(old_subject["accepted_brief_sha256"], current_subject["accepted_brief_sha256"])
        self.assertEqual(old_subject["diff_sha256"], current_subject["diff_sha256"])
        decision_basis = self.json_object(current_subject["decisions"])
        batches = [self.json_object(row) for row in self.json_array(decision_basis["resolutions"])]
        self.assertEqual(
            ["Replace invalid input.", "Reject invalid input. Do not replace it."],
            [
                self.json_object(self.json_array(self.json_object(batch["resolution"])["resolutions"])[0])[
                    "human_decision"
                ]
                for batch in batches
            ],
        )
        fresh = self.commission_and_record_landing_review(
            fixture,
            observe,
            old_review
            | {
                "subject": current_subject,
                "coverage": "The independent review verified the exact input rejection and all other landings in the range.",
            },
            "Reject invalid input. Do not replace it.",
        )
        completed = self.complete(
            fixture, complete_payload | {"landed": landing | {"difference_review_ref_id": fresh["artifact_ref_id"]}}
        )
        self.assertEqual("committed", completed["status"], completed)
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])
        exported = self.run_json_cli(*fixture.common, "export")
        package = self.json_object(self.json_array(exported["completion_packages"])[0])
        self.assertEqual(historic["acceptance_history_id"], package["continued_acceptance_history_id"])
        self.assertEqual(decision_basis, self.json_object(package["landing"])["decisions"])

    def test_empty_recorded_diff_is_no_change_without_a_content_check(self) -> None:
        fixture = self.native_fixture(self.checkpoint_fixture())
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
        fixture = self.native_fixture(self.checkpoint_fixture())
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

        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute("DELETE FROM transition_history WHERE action_id = 'close:work-c'")
        unknown = self.integration(fixture, "main", "work-c")
        self.assertEqual("closure-unknown", self.rejection(unknown, "INTEGRATION_CANDIDATE_UNAVAILABLE")["reason"])

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

        returned = self.native_fixture(self.checkpoint_fixture())
        self.return_for_review(returned)
        git(returned.project, "branch", "main", returned.brief.base_revision)
        self.assertEqual(
            "no-reviewed-candidate",
            self.rejection(self.integration(returned, "main"), "INTEGRATION_CANDIDATE_UNAVAILABLE")["reason"],
        )

    def test_retired_v2_package_rejects_before_git_comparison(self) -> None:
        fixture = self.accepted_package_fixture(local=True, candidate_form="current-head")
        git(fixture.project, "branch", "main", fixture.brief.base_revision)
        self.retain_v2_checkpoint(fixture)
        before = fixture.store.validated_snapshot()
        with patch.object(
            root, "observe_reviewed_diff_at_target", side_effect=AssertionError("Git comparison must not run")
        ):
            retained = self.integration(fixture, "main")
        self.rejection(retained, "INTEGRATION_CANDIDATE_EVIDENCE_INVALID")
        self.assertEqual(("unchanged", "do-not-retry"), (retained["effect"], retained["retry"]))
        self.assertEqual([], retained["changed_surfaces"])
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_current_checkpoint_missing_package_keeps_unavailable_reason_before_git(self) -> None:
        fixture = self.accepted_package_fixture(local=True, candidate_form="current-head")
        git(fixture.project, "branch", "main", fixture.brief.base_revision)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE transition_history SET artifact_ref_id = NULL, artifact_kind = NULL WHERE outcome_schema = 'checkpoint-acceptance/v2'"
            )
        before = fixture.store.validated_snapshot()
        with patch.object(
            root, "observe_reviewed_diff_at_target", side_effect=AssertionError("Git comparison must not run")
        ):
            unavailable = self.integration(fixture, "main")
        self.assertEqual(
            "checkpoint-without-candidate-snapshot",
            self.rejection(unavailable, "INTEGRATION_CANDIDATE_UNAVAILABLE")["reason"],
        )
        self.assertIn("no accepted package reference", str(unavailable["message"]))
        self.assertEqual(("unchanged", "correct-input"), (unavailable["effect"], unavailable["retry"]))
        self.assertEqual([], unavailable["changed_surfaces"])
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_target_and_request_rejections_are_typed(self) -> None:
        fixture = self.native_fixture(self.checkpoint_fixture())
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

    def test_integration_resolves_source_checkout_once(self) -> None:
        fixture = self.checkpoint_fixture()
        git(fixture.project, "branch", "main", fixture.brief.base_revision)
        with (
            patch.object(
                mcp_common, "resolve_source_checkout_root", wraps=root.resolve_source_checkout_root
            ) as resolve,
            patch("pinboard.mcp.read_operations.resolve_source_checkout_root", create=True) as second_resolve,
        ):
            self.presence(fixture, "main", "content-not-present")
        self.assertEqual(1, resolve.call_count)
        second_resolve.assert_not_called()

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

        accepted = self.native_fixture(self.accepted_package_fixture())
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
        fixture = self.native_fixture(self.checkpoint_fixture(candidate_form="current-head"))
        self.record_ready(fixture)
        git(fixture.project, "branch", "main", fixture.candidate_revision)
        integrated = self.presence(fixture, "main", "content-present")

        def inspect(phase: str, effects: tuple[str, str, str]) -> JsonObject:
            inspected = fixture.call_advertised_tool(
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
            overview = fixture.call_advertised_tool(
                mcp_server.OVERVIEW_TOOL, self.roots(fixture) | {"max_items": 25, "continuation": None}
            )
            item = fixture.call_advertised_tool(
                mcp_server.ITEM_STATUS_TOOL,
                {"request": {**self.roots(fixture), "operation": "item", "item_id": "work-a"}},
            )
        self.assertEqual({"kind": "repository-cleanup", "target_revision": integrated["target_revision"]}, cleanup)
        self.assertEqual({"target": "attempt", "action_kind": "complete"}, terminal["action"])
        self.assertEqual("pinboard-item-status/v7", item["schema"])
        self.assertNotIn("presence", json.dumps(overview))

    def test_integration_reads_stay_keyed_as_retained_history_grows(self) -> None:
        fixture = self.native_fixture(self.accepted_package_fixture())
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
                assert isinstance(selected, mcp_execution.AutomaticCaptureResolution)
                self.assertEqual(captured, selected.capture is not None)
