"""The item-status integration leaf reports whether a reviewed candidate's content reached a named target."""

import contextlib
import hashlib
import os
import sqlite3
import subprocess
import tempfile
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.application import checkpoint_compatibility_models, work_brief_models
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import common as mcp_common
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import JsonObject

FIXED_DATE = "2030-01-02T03:04:05+00:00"
COMPLETED_AT = datetime(2030, 1, 3, 4, 5, 6, tzinfo=UTC)
REVIEWED_LINES = tuple(f"line {number}" for number in range(1, 11))


def file_snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class ItemIntegrationTest(CheckpointPackageSupport):
    def git(self, fixture: CheckpointFixture, *arguments: str) -> str:
        environment = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Pinboard Tests",
            "GIT_AUTHOR_EMAIL": "pinboard@example.invalid",
            "GIT_COMMITTER_NAME": "Pinboard Tests",
            "GIT_COMMITTER_EMAIL": "pinboard@example.invalid",
            "GIT_AUTHOR_DATE": FIXED_DATE,
            "GIT_COMMITTER_DATE": FIXED_DATE,
        }
        return subprocess.run(
            ["git", *arguments], cwd=fixture.project, env=environment, check=True, capture_output=True, text=True
        ).stdout.strip()

    def is_ancestor(self, fixture: CheckpointFixture, ancestor: str, descendant: str) -> bool:
        return (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=fixture.project, check=False
            ).returncode
            == 0
        )

    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def integration(self, fixture: CheckpointFixture, target: str, item_id: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": item_id, "target": target}},
        )

    def transition(self, fixture: CheckpointFixture, action_id: str, payload: JsonObject) -> None:
        result = self.transition_result(fixture, self.project_action(fixture, action_id), payload)
        self.assertIn(result["status"], ("committed", "committed-with-warning"), result)

    def write_reviewed(self, fixture: CheckpointFixture, changes: dict[int, str]) -> None:
        lines = [changes.get(number, line) for number, line in enumerate(REVIEWED_LINES, start=1)]
        (fixture.project / "reviewed.txt").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")

    def returned_with_context(self, fixture: CheckpointFixture) -> str:
        """Return the fixture candidate, then commit a multi-line reviewed file as the next preimage."""

        self.transition(fixture, "return-for-correction:work-a-1", {"reason": "Rework the candidate."})
        self.git(fixture, "checkout", "--", "tracked.txt")
        self.write_reviewed(fixture, {})
        self.git(fixture, "add", "reviewed.txt")
        self.git(fixture, "commit", "-m", "context")
        return self.git(fixture, "rev-parse", "HEAD")

    def submit(self, fixture: CheckpointFixture, candidate: str, worker: str) -> None:
        lease = self.native_attempt_acquire(fixture, worker)
        submission = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        submitted = self.transition_result(fixture, submission, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)

    def submit_working_tree(self, fixture: CheckpointFixture, worker: str) -> str:
        observed = call_native_tool(
            mcp_server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"}
        )
        candidate = observed["candidate"]
        assert isinstance(candidate, str), observed
        self.submit(fixture, candidate, worker)
        return candidate

    def commit_candidate_and_squash(self, fixture: CheckpointFixture, target: str) -> str:
        """Commit the working tree on the attempt branch, then squash-merge it into a target from the base."""

        self.git(fixture, "commit", "-am", "candidate")
        self.git(fixture, "switch", "-c", target, fixture.brief.base_revision)
        self.git(fixture, "merge", "--squash", "codex/work-a")
        self.git(fixture, "commit", "-m", "squash")
        squashed = self.git(fixture, "rev-parse", "HEAD")
        self.assertFalse(self.is_ancestor(fixture, "codex/work-a", squashed))
        self.git(fixture, "switch", "codex/work-a")
        return squashed

    def assert_integration(
        self,
        result: JsonObject,
        target: str,
        target_revision: str,
        source: JsonObject,
        presence: str,
    ) -> None:
        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual(
            (target, target_revision, source, presence),
            (result["target"], result["target_revision"], result["source"], result["presence"]),
        )
        self.assertEqual("work-a", result["item_id"])

    def assert_rejection(self, result: JsonObject, code: str, retry: str) -> dict[str, object]:
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual(
            (code, "unchanged", retry, False),
            (result["code"], result["effect"], result["retry"], result["state_changed"]),
        )
        self.assertEqual([], result["changed_surfaces"])
        return {
            str(self.json_object(value)["field"]): self.json_object(value)["value"]
            for value in self.json_array(result["observed"])
        }

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

    @contextlib.contextmanager
    def recorded_reads(self) -> Generator[list[str]]:
        statements: list[str] = []
        original_open = sqlite_store.open_database

        def traced_open(path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open(path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(sqlite_store, "open_database", traced_open):
            yield statements

    def test_protected_working_tree_candidate_is_found_after_a_squash_merge(self) -> None:
        fixture = self.checkpoint_fixture()
        preimage = self.returned_with_context(fixture)
        self.write_reviewed(fixture, {3: "line 3 reviewed"})
        candidate = self.submit_working_tree(fixture, "worker-working-tree")
        source: JsonObject = {
            "kind": "protected-review",
            "attempt_id": "work-a-1",
            "candidate_revision": candidate,
            "compared_from_revision": preimage,
        }
        base = fixture.brief.base_revision
        self.git(fixture, "branch", "main", base)
        self.assert_integration(self.integration(fixture, "main"), "main", base, source, "content-not-present")

        squashed = self.commit_candidate_and_squash(fixture, "squashed")
        git_before = file_snapshot(fixture.project / ".git")
        work_before = file_snapshot(fixture.work)
        with self.recorded_reads() as statements:
            present = self.integration(fixture, "squashed")
        self.assert_integration(present, "squashed", squashed, source, "content-present")
        self.assertEqual(git_before, file_snapshot(fixture.project / ".git"))
        self.assertEqual(work_before, file_snapshot(fixture.work))
        self.assertEqual(1, sum(statement == "BEGIN" for statement in statements), statements)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            for statement in statements:
                if statement.lstrip().upper().startswith("SELECT"):
                    with self.subTest(statement=statement):
                        plan = [str(row[3]).upper() for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}")]
                        self.assertFalse(any("SCAN " in detail for detail in plan), plan)
            self.assertFalse(any("review_verdict" in statement for statement in statements))

        self.git(fixture, "update-ref", "refs/remotes/origin/main", squashed)
        self.assert_integration(
            self.integration(fixture, "origin/main"), "origin/main", squashed, source, "content-present"
        )

        self.git(fixture, "switch", "squashed")
        self.write_reviewed(fixture, {3: "line 3 reviewed", 10: "line 10 later"})
        self.git(fixture, "commit", "-am", "later non-overlapping edit")
        later = self.git(fixture, "rev-parse", "HEAD")
        self.assert_integration(self.integration(fixture, "squashed"), "squashed", later, source, "content-present")
        self.write_reviewed(fixture, {2: "line 2 later", 3: "line 3 reviewed", 10: "line 10 later"})
        self.git(fixture, "commit", "-am", "later overlapping edit")
        overlapping = self.git(fixture, "rev-parse", "HEAD")
        self.assert_integration(
            self.integration(fixture, overlapping), overlapping, overlapping, source, "content-not-present"
        )

    def test_protected_commit_candidate_is_found_by_fast_forward_merge_commit_and_rebase(self) -> None:
        fixture = self.checkpoint_fixture()
        self.returned_with_context(fixture)
        self.write_reviewed(fixture, {5: "line 5 reviewed"})
        self.git(fixture, "commit", "-am", "commit candidate")
        candidate = self.git(fixture, "rev-parse", "HEAD")
        self.submit(fixture, candidate, "worker-commit")
        base = fixture.brief.base_revision
        source: JsonObject = {
            "kind": "protected-review",
            "attempt_id": "work-a-1",
            "candidate_revision": candidate,
            "compared_from_revision": base,
        }
        self.git(fixture, "switch", "-c", "fast-forward", base)
        self.git(fixture, "merge", "--ff-only", "codex/work-a")
        self.git(fixture, "switch", "-c", "merged", base)
        (fixture.project / "unrelated.txt").write_text("target work\n", encoding="utf-8")
        self.git(fixture, "add", "unrelated.txt")
        self.git(fixture, "commit", "-m", "target work")
        self.git(fixture, "branch", "rebased")
        self.git(fixture, "merge", "--no-ff", "-m", "merge", "codex/work-a")
        self.git(fixture, "switch", "rebased")
        self.git(fixture, "cherry-pick", base + ".." + candidate)
        rebased = self.git(fixture, "rev-parse", "HEAD")
        self.assertFalse(self.is_ancestor(fixture, candidate, rebased))
        self.git(fixture, "switch", "codex/work-a")
        for target in ("fast-forward", "merged", "rebased"):
            with self.subTest(target=target):
                self.assert_integration(
                    self.integration(fixture, target),
                    target,
                    self.git(fixture, "rev-parse", target),
                    source,
                    "content-present",
                )
        self.assert_integration(self.integration(fixture, base), base, base, source, "content-not-present")

    def test_accepted_checkpoint_candidate_is_the_source_while_paused_resumed_and_rebound(self) -> None:
        fixture = self.checkpoint_fixture()
        self.transition(fixture, "return-for-correction:work-a-1", {"reason": "Rework the candidate."})
        (fixture.project / "tracked.txt").write_text("checkpoint candidate\n", encoding="utf-8")
        candidate = self.submit_working_tree(fixture, "worker-checkpoint")
        checkpoint_id = fixture.brief.checkpoint.checkpoint_id
        self.transition(
            fixture,
            "accept-checkpoint:work-a-1",
            {"checkpoint": checkpoint_id, "candidate": candidate, "evidence": "Accepted checkpoint."},
        )
        squashed = self.commit_candidate_and_squash(fixture, "main")
        source: JsonObject = {
            "kind": "accepted-checkpoint",
            "attempt_id": "work-a-1",
            "checkpoint_id": checkpoint_id,
            "candidate_revision": candidate,
            "compared_from_revision": fixture.brief.base_revision,
        }
        self.assertEqual(
            "paused",
            call_native_tool(
                mcp_server.ITEM_STATUS_TOOL,
                {"request": {**self.roots(fixture), "operation": "item", "item_id": "work-a"}},
            )["state"],
        )
        self.assert_integration(self.integration(fixture, "main"), "main", squashed, source, "content-present")
        self.close_prerequisite(fixture)
        self.transition(fixture, "resume:work-a", {})
        self.assert_integration(self.integration(fixture, "main"), "main", squashed, source, "content-present")
        self.rebind(fixture, 2)
        self.assert_integration(self.integration(fixture, "main"), "main", squashed, source, "content-present")

        (fixture.project / "tracked.txt").write_text("next candidate\n", encoding="utf-8")
        next_candidate = self.submit_working_tree(fixture, "worker-next")
        protected = self.integration(fixture, "main")
        self.assertEqual(
            ("protected-review", next_candidate),
            (
                self.json_object(protected["source"])["kind"],
                self.json_object(protected["source"])["candidate_revision"],
            ),
        )
        self.assertEqual("content-not-present", protected["presence"])
        self.transition(fixture, "return-for-correction:work-a-1", {"reason": "Rework again."})
        self.assert_integration(self.integration(fixture, "main"), "main", squashed, source, "content-present")
        (fixture.project / "tracked.txt").write_text("continued candidate\n", encoding="utf-8")
        continued = self.submit_working_tree(fixture, "worker-continued")
        self.transition(
            fixture,
            "accept-review-and-continue:work-a-1",
            {"candidate": continued, "evidence": "Accepted; continue the attempt."},
        )
        self.assert_integration(self.integration(fixture, "main"), "main", squashed, source, "content-present")

    def test_completion_candidate_is_the_source_after_completion(self) -> None:
        fixture = self.checkpoint_fixture()
        self.transition(fixture, "return-for-correction:work-a-1", {"reason": "Rework the candidate."})
        (fixture.project / "tracked.txt").write_text("completed candidate\n", encoding="utf-8")
        candidate = self.submit_working_tree(fixture, "worker-completion")
        fixture = self.terminalize_brief(fixture)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        with patch("pinboard.mcp.mutation_operations.datetime") as clock:
            clock.now.return_value = COMPLETED_AT
            self.transition(
                fixture,
                "complete:work-a-1",
                {
                    "schema": "pinboard-reviewed-completion/v2",
                    "candidate": candidate,
                    "evidence": "Accepted for integration.",
                    "reviewer_task_id": "independent-reviewer",
                    "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
                    "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
                    "packages": [],
                },
            )
        squashed = self.commit_candidate_and_squash(fixture, "main")
        source: JsonObject = {
            "kind": "completion",
            "attempt_id": "work-a-1",
            "candidate_revision": candidate,
            "compared_from_revision": fixture.brief.base_revision,
        }
        self.assert_integration(self.integration(fixture, "main"), "main", squashed, source, "content-present")
        self.assert_integration(
            self.integration(fixture, fixture.brief.base_revision),
            fixture.brief.base_revision,
            fixture.brief.base_revision,
            source,
            "content-not-present",
        )

    def test_empty_recorded_diff_reports_no_change_at_the_resolved_target(self) -> None:
        fixture = self.checkpoint_fixture()
        self.transition(fixture, "return-for-correction:work-a-1", {"reason": "Rework the candidate."})
        self.git(fixture, "checkout", "--", "tracked.txt")
        candidate = self.submit_working_tree(fixture, "worker-empty")
        base = fixture.brief.base_revision
        self.assert_integration(
            self.integration(fixture, "codex/work-a"),
            "codex/work-a",
            base,
            {
                "kind": "protected-review",
                "attempt_id": "work-a-1",
                "candidate_revision": candidate,
                "compared_from_revision": base,
            },
            "no-change",
        )
        unresolved = self.assert_rejection(
            self.integration(fixture, "missing"), "INTEGRATION_TARGET_UNRESOLVED", "correct-input"
        )
        self.assertEqual({"target": "missing", "project_root": str(fixture.project)}, unresolved)

    def test_integration_rejections_name_their_condition_and_next_step(self) -> None:
        fixture = self.checkpoint_fixture()
        unresolved = self.integration(fixture, "refs/heads/never-created")
        self.assertEqual(
            {"target": "refs/heads/never-created", "project_root": str(fixture.project)},
            self.assert_rejection(unresolved, "INTEGRATION_TARGET_UNRESOLVED", "correct-input"),
        )
        self.assertIn("fetch it outside Pinboard", str(unresolved["recovery"]))

        option = call_native_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": "work-a", "target": "--all"}},
        )
        self.assert_rejection(option, "ITEM_STATUS_INVALID", "correct-input")

        missing = self.integration(fixture, "codex/work-a", "missing-item")
        self.assert_rejection(missing, "ITEM_NOT_FOUND", "correct-input")

        ready = self.integration(fixture, "codex/work-a", "work-c")
        self.assertEqual(
            {"item_id": "work-c", "item_state": "ready", "reason": "no-reviewed-candidate"},
            self.assert_rejection(ready, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"),
        )
        self.assertIn("operation item", str(ready["recovery"]))

        self.close_prerequisite(fixture)
        self.assertEqual(
            {"item_id": "work-c", "item_state": "done", "reason": "direct-close"},
            self.assert_rejection(
                self.integration(fixture, "codex/work-a", "work-c"),
                "INTEGRATION_CANDIDATE_UNAVAILABLE",
                "correct-input",
            ),
        )

        snapshot = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert snapshot is not None
        snapshot_path = fixture.work / snapshot.reference.selector
        snapshot_path.chmod(0o644)
        snapshot_path.write_bytes(snapshot_path.read_bytes().replace(b"candidate", b"altered!!"))
        altered = self.integration(fixture, "codex/work-a")
        self.assertEqual(
            {"attempt_id": "work-a-1", "reference": snapshot.reference.selector},
            self.assert_rejection(altered, "INTEGRATION_CANDIDATE_EVIDENCE_INVALID", "do-not-retry"),
        )
        self.assertIn("pinboard validate", str(altered["recovery"]))

        self.transition(fixture, "return-for-correction:work-a-1", {"reason": "Rework the candidate."})
        self.assertEqual(
            {"item_id": "work-a", "item_state": "active", "reason": "no-reviewed-candidate"},
            self.assert_rejection(
                self.integration(fixture, "codex/work-a"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"
            ),
        )

        outside = Path(tempfile.mkdtemp())
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
        observed = self.assert_rejection(not_git, "PROJECT_GIT_ROOT_UNAVAILABLE", "correct-input")
        self.assertEqual(str(outside), observed["project_root"])
        self.assertIn("not a git repository", str(observed["diagnostic"]))

    def test_integration_leaf_selects_its_item_for_trace_capture(self) -> None:
        fixture = self.checkpoint_fixture()
        selected = mcp_common.select_capture_item(
            resolve_durable_roots(fixture.project).anchor,
            str(fixture.work),
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": "work-a", "target": "main"}},
        )
        self.assertEqual("work-a", selected)

    def test_attempt_inspection_keeps_caller_owned_reconciliation_without_a_git_read(self) -> None:
        fixture = self.checkpoint_fixture()
        snapshot = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert snapshot is not None
        attempt_root = fixture.work / "attempts" / "work-a-1"
        recorded = call_native_tool(
            mcp_server.REVIEW_JOB_TOOL,
            {
                **self.roots(fixture),
                "review": {
                    "kind": "record-ready",
                    "attempt_id": "work-a-1",
                    "candidate_revision": fixture.candidate_revision,
                    "candidate_snapshot_sha256": snapshot.reference.content_sha256,
                    "accepted_brief_sha256": hashlib.sha256(
                        (fixture.work / "artifacts" / "briefs" / "work-a-1" / "1.json").read_bytes()
                    ).hexdigest(),
                    "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
                    "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
                    "reviewer_task_id": "independent-reviewer",
                    "verdict": "ready",
                    "acceptance_evidence": "The protected candidate satisfies the accepted brief.",
                },
            },
        )
        self.assertEqual("recorded", recorded["status"], recorded)
        forbidden = AssertionError("attempt inspection must not read target content")
        with (
            patch("pinboard.adapters.files.root.observe_target_content", side_effect=forbidden),
            patch("pinboard.adapters.files.root.resolve_target_revision", side_effect=forbidden),
        ):
            inspected = call_advertised_tool(
                mcp_server.ATTEMPT_INSPECT_TOOL,
                {
                    **self.roots(fixture),
                    "attempt_id": "work-a-1",
                    "reconciliation": {
                        "target_revision": "caller-observed-target",
                        "relation": "candidate-integrated",
                        "phase": "cleanup",
                        "effects": [
                            {"effect": "source-checkout", "status": "allowed"},
                            {"effect": "shared-work-root", "status": "not-required"},
                            {"effect": "git-metadata", "status": "allowed"},
                        ],
                    },
                },
            )
        operation = self.json_object(self.json_object(inspected["continuation"])["next_operation"])
        self.assertEqual("repository-cleanup", operation["kind"], inspected)

    def test_checkpoint_source_rejects_altered_evidence_and_a_package_without_a_snapshot(self) -> None:
        fixture = self.accepted_package_fixture()
        package = self.package(fixture)
        assert isinstance(package, work_brief_models.CheckpointReviewPackageV3)
        self.assertEqual("content-not-present", self.integration(fixture, "codex/work-a")["presence"])
        for selector in (package.candidate_snapshot.selector, fixture.package_reference.selector):
            with self.subTest(altered=selector):
                path = fixture.work / selector
                original = path.read_bytes()
                path.chmod(0o644)
                path.write_bytes(original + b" ")
                altered = self.integration(fixture, "codex/work-a")
                self.assertEqual(
                    {"attempt_id": "work-a-1", "reference": selector},
                    self.assert_rejection(altered, "INTEGRATION_CANDIDATE_EVIDENCE_INVALID", "do-not-retry"),
                )
                path.write_bytes(original)
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
        (fixture.work / fixture.package_reference.selector).chmod(0o644)
        self.replace_package(fixture, legacy)
        self.assertEqual(
            {"item_id": "work-a", "item_state": "paused", "reason": "checkpoint-without-snapshot"},
            self.assert_rejection(
                self.integration(fixture, "codex/work-a"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"
            ),
        )
