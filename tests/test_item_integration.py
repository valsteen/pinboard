"""Native item-status integration reads over real Git repositories and real transitions."""

import contextlib
import hashlib
import os
import sqlite3
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import override
from unittest.mock import patch

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters import candidate_evidence
from pinboard.adapters.sqlite import database as sqlite_database
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import JsonObject, JsonValue

FIXED_DATE = "2030-01-01T00:00:00+00:00"


class ItemIntegrationTest(CheckpointPackageSupport):
    @override
    def setUp(self) -> None:
        dates = patch.dict(os.environ, {"GIT_AUTHOR_DATE": FIXED_DATE, "GIT_COMMITTER_DATE": FIXED_DATE})
        dates.start()
        self.addCleanup(dates.stop)

    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def integration(self, fixture: CheckpointFixture, target: str, item_id: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": item_id, "target": target}},
        )

    def git(self, fixture: CheckpointFixture, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=Pinboard Tests", "-c", "user.email=pinboard@example.invalid", *arguments],
            cwd=fixture.project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(self, fixture: CheckpointFixture, message: str) -> str:
        self.git(fixture, "add", "--all")
        self.git(fixture, "commit", "-q", "-m", message)
        return self.git(fixture, "rev-parse", "HEAD")

    def base(self, fixture: CheckpointFixture) -> str:
        return fixture.brief.base_revision

    def target_with_unrelated_commit(self, fixture: CheckpointFixture, branch: str) -> None:
        self.git(fixture, "checkout", "-q", "-B", branch, self.base(fixture))
        (fixture.project / "unrelated.txt").write_text(f"{branch}\n", encoding="utf-8")
        self.commit(fixture, f"unrelated on {branch}")

    def squash_into(self, fixture: CheckpointFixture, branch: str, feature: str) -> str:
        self.target_with_unrelated_commit(fixture, branch)
        self.git(fixture, "merge", "-q", "--squash", feature)
        return self.commit(fixture, f"squash into {branch}")

    def commit_working_tree_candidate(self, fixture: CheckpointFixture) -> str:
        return self.commit(fixture, "reviewed working tree")

    def assert_integration(
        self, result: JsonObject, target: str, target_revision: str, source: JsonObject, presence: str
    ) -> None:
        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual(
            {"item_id": "work-a", "target": target, "target_revision": target_revision, "source": source},
            {key: result[key] for key in ("item_id", "target", "target_revision", "source")},
        )
        self.assertEqual(presence, result["presence"], result)

    def assert_rejected(self, result: JsonObject, code: str, retry: str) -> JsonObject:
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual(
            ("rejected", code, "unchanged", retry, False),
            tuple(result[key] for key in ("status", "code", "effect", "retry", "state_changed")),
            result,
        )
        self.assertEqual([], result["changed_surfaces"])
        return {
            str(self.json_object(fact)["field"]): self.json_object(fact)["value"]
            for fact in self.json_array(result["observed"])
        }

    def transition(self, fixture: CheckpointFixture, action_id: str, payload: JsonObject) -> None:
        result = self.transition_result(fixture, self.project_action(fixture, action_id), payload)
        self.assertIn(result["status"], ("committed", "committed-with-warning"), result)

    def resubmit(self, fixture: CheckpointFixture, worker: str) -> str:
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

    def test_protected_working_tree_candidate_against_base_squash_remote_and_overlap(self) -> None:
        fixture = self.checkpoint_fixture()
        base = self.base(fixture)
        source: JsonObject = {
            "kind": "protected-review",
            "attempt_id": "work-a-1",
            "candidate_revision": fixture.candidate_revision,
            "compared_from_revision": base,
        }
        self.git(fixture, "branch", "main", base)
        self.assert_integration(self.integration(fixture, "main"), "main", base, source, "content-not-present")
        feature = self.commit_working_tree_candidate(fixture)
        squashed = self.squash_into(fixture, "main", feature)
        self.assertNotEqual(
            0,
            subprocess.run(["git", "merge-base", "--is-ancestor", feature, squashed], cwd=fixture.project).returncode,
        )
        self.assert_integration(self.integration(fixture, "main"), "main", squashed, source, "content-present")
        self.git(fixture, "update-ref", "refs/remotes/origin/main", squashed)
        self.assert_integration(
            self.integration(fixture, "origin/main"), "origin/main", squashed, source, "content-present"
        )
        (fixture.project / "tracked.txt").write_text("later overlapping edit\n", encoding="utf-8")
        overlapping = self.commit(fixture, "overlapping edit")
        self.assert_integration(self.integration(fixture, "main"), "main", overlapping, source, "content-not-present")
        self.assert_integration(
            self.integration(fixture, "origin/main"), "origin/main", squashed, source, "content-present"
        )

    def test_protected_commit_candidate_by_fast_forward_merge_commit_and_rebase(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        candidate = fixture.candidate_revision
        source: JsonObject = {
            "kind": "protected-review",
            "attempt_id": "work-a-1",
            "candidate_revision": candidate,
            "compared_from_revision": self.base(fixture),
        }
        self.git(fixture, "branch", "fast-forward", candidate)
        self.assert_integration(
            self.integration(fixture, "fast-forward"), "fast-forward", candidate, source, "content-present"
        )
        self.assert_integration(self.integration(fixture, candidate), candidate, candidate, source, "content-present")
        self.target_with_unrelated_commit(fixture, "merged")
        self.git(fixture, "merge", "-q", "--no-ff", "-m", "merge candidate", candidate)
        merged = self.git(fixture, "rev-parse", "HEAD")
        self.assert_integration(self.integration(fixture, "merged"), "merged", merged, source, "content-present")
        self.target_with_unrelated_commit(fixture, "rebased")
        self.git(fixture, "cherry-pick", candidate)
        rebased = self.git(fixture, "rev-parse", "HEAD")
        self.assertNotEqual(
            0,
            subprocess.run(["git", "merge-base", "--is-ancestor", candidate, rebased], cwd=fixture.project).returncode,
        )
        self.assert_integration(self.integration(fixture, "rebased"), "rebased", rebased, source, "content-present")
        self.git(fixture, "tag", "released", rebased)
        self.assert_integration(self.integration(fixture, "released"), "released", rebased, source, "content-present")

    def test_corrected_candidate_compares_from_its_preimage_with_non_overlapping_and_overlapping_edits(self) -> None:
        fixture = self.checkpoint_fixture()
        self.transition(fixture, "return-for-correction:work-a-1", {"reason": "Add the notes."})
        unavailable = self.assert_rejected(
            self.integration(fixture, "codex/work-a"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"
        )
        self.assertEqual("no-protected-candidate-or-checkpoint-acceptance", unavailable["reason"])
        notes = fixture.project / "notes.txt"
        notes.write_text("".join(f"line {number}\n" for number in range(1, 21)), encoding="utf-8")
        preimage = self.commit(fixture, "notes and reviewed tracked change")
        lines = notes.read_text(encoding="utf-8").splitlines(keepends=True)
        lines[1] = "corrected line two\n"
        notes.write_text("".join(lines), encoding="utf-8")
        candidate = self.resubmit(fixture, "corrector")
        source: JsonObject = {
            "kind": "protected-review",
            "attempt_id": "work-a-1",
            "candidate_revision": candidate,
            "compared_from_revision": preimage,
        }
        corrected = self.commit(fixture, "corrected notes")
        self.git(fixture, "checkout", "-q", "-b", "integrated", corrected)
        lines[17] = "later edit far from the reviewed line\n"
        notes.write_text("".join(lines), encoding="utf-8")
        later = self.commit(fixture, "later non-overlapping edit")
        self.assert_integration(self.integration(fixture, "integrated"), "integrated", later, source, "content-present")
        lines[2] = "later edit next to the reviewed line\n"
        notes.write_text("".join(lines), encoding="utf-8")
        overlapping = self.commit(fixture, "later overlapping edit")
        self.assert_integration(
            self.integration(fixture, "integrated"), "integrated", overlapping, source, "content-not-present"
        )

    def test_empty_recorded_diff_is_no_change(self) -> None:
        fixture = self.checkpoint_fixture()
        self.transition(fixture, "return-for-correction:work-a-1", {"reason": "Commit the change first."})
        head = self.commit_working_tree_candidate(fixture)
        candidate = self.resubmit(fixture, "empty-diff")
        self.assert_integration(
            self.integration(fixture, "codex/work-a"),
            "codex/work-a",
            head,
            {
                "kind": "protected-review",
                "attempt_id": "work-a-1",
                "candidate_revision": candidate,
                "compared_from_revision": head,
            },
            "no-change",
        )
        with patch.object(candidate_evidence.root, "observe_target_content", side_effect=AssertionError):
            self.assertEqual("no-change", self.integration(fixture, head)["presence"])

    def test_accepted_checkpoint_squash_merged_before_resume_survives_resume_and_rebind(self) -> None:
        fixture = self.accepted_package_fixture()
        checkpoint_id = fixture.brief.checkpoint.checkpoint_id
        source: JsonObject = {
            "kind": "accepted-checkpoint",
            "attempt_id": "work-a-1",
            "candidate_revision": fixture.candidate_revision,
            "compared_from_revision": self.base(fixture),
            "checkpoint_id": checkpoint_id,
        }
        feature = self.commit_working_tree_candidate(fixture)
        squashed = self.squash_into(fixture, "main", feature)
        self.assert_integration(self.integration(fixture, "main"), "main", squashed, source, "content-present")
        self.close_work_c(fixture)
        self.transition(fixture, "resume:work-a", {})
        self.assert_integration(self.integration(fixture, "main"), "main", squashed, source, "content-present")
        brief = replace_struct(fixture.brief, artifact_revision=2, branch="codex/work-a-rebound")
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
        self.assert_integration(self.integration(fixture, "main"), "main", squashed, source, "content-present")
        self.assert_integration(
            self.integration(fixture, self.base(fixture)),
            self.base(fixture),
            self.base(fixture),
            source,
            "content-not-present",
        )

    def test_accept_review_and_continue_is_not_a_source(self) -> None:
        fixture = self.checkpoint_fixture()
        self.transition(
            fixture,
            "accept-review-and-continue:work-a-1",
            {"candidate": fixture.candidate_revision, "evidence": "Accepted; continue."},
        )
        observed = self.assert_rejected(
            self.integration(fixture, "codex/work-a"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"
        )
        self.assertEqual("no-protected-candidate-or-checkpoint-acceptance", observed["reason"])

    def test_completion_candidate_and_direct_close(self) -> None:
        fixture = self.checkpoint_fixture()
        self.close_work_c(fixture)
        closed = self.assert_rejected(
            self.integration(fixture, "codex/work-a", "work-c"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"
        )
        self.assertEqual(("work-c", "closed-without-completion"), (closed["item_id"], closed["reason"]))
        terminal = self.terminalize_brief(fixture)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        self.transition(
            terminal,
            "complete:work-a-1",
            {
                "schema": "pinboard-reviewed-completion/v2",
                "candidate": fixture.candidate_revision,
                "evidence": "Accepted.",
                "reviewer_task_id": "independent-reviewer",
                "result_sha256": self.sha256(attempt_root / "result.md"),
                "review_sha256": self.sha256(attempt_root / "review.md"),
                "packages": [],
            },
        )
        feature = self.commit_working_tree_candidate(fixture)
        squashed = self.squash_into(fixture, "main", feature)
        self.assert_integration(
            self.integration(fixture, "main"),
            "main",
            squashed,
            {
                "kind": "completion",
                "attempt_id": "work-a-1",
                "candidate_revision": fixture.candidate_revision,
                "compared_from_revision": self.base(fixture),
            },
            "content-present",
        )

    def close_work_c(self, fixture: CheckpointFixture) -> None:
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

    def sha256(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_rejections_name_their_facts_effect_retry_and_next_step(self) -> None:
        fixture = self.checkpoint_fixture()
        unresolved = self.integration(fixture, "no-such-branch")
        self.assertEqual(
            {"target": "no-such-branch", "project_root": str(fixture.project)},
            self.assert_rejected(unresolved, "INTEGRATION_TARGET_UNRESOLVED", "correct-input"),
        )
        self.assertIn("fetch", str(unresolved["recovery"]))
        dashed = call_native_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": "work-a", "target": "-x"}},
        )
        self.assert_rejected(dashed, "ITEM_STATUS_INVALID", "correct-input")
        self.assert_rejected(
            self.integration(fixture, "codex/work-a", "missing-item"), "ITEM_NOT_FOUND", "correct-input"
        )
        ready = self.assert_rejected(
            self.integration(fixture, "codex/work-a", "work-c"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"
        )
        self.assertEqual("no-current-attempt", ready["reason"])
        not_git = call_native_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "project_root": str(self.temporary_directory()),
                    "work_root": str(fixture.work),
                    "operation": "integration",
                    "item_id": "work-a",
                    "target": "main",
                }
            },
        )
        self.assertEqual("PROJECT_GIT_ROOT_UNAVAILABLE", not_git["code"], not_git)
        self.assert_rejected(not_git, "PROJECT_GIT_ROOT_UNAVAILABLE", "correct-input")
        snapshot = SQLiteWorkStore(fixture.work / "state.sqlite3").read_candidate_snapshot_context(
            AttemptId("work-a-1")
        )
        assert snapshot is not None
        artifact = fixture.work / snapshot.reference.selector
        artifact.chmod(stat.S_IRUSR | stat.S_IWUSR)
        artifact.write_bytes(artifact.read_bytes().replace(b"candidate", b"tampered!"))
        invalid = self.integration(fixture, "codex/work-a")
        observed = self.assert_rejected(invalid, "INTEGRATION_CANDIDATE_EVIDENCE_INVALID", "do-not-retry")
        self.assertEqual({"attempt_id": "work-a-1", "reference": snapshot.reference.selector}, observed)
        self.assertIn("pinboard validate", str(invalid["recovery"]))

    def temporary_directory(self) -> Path:
        return Path(tempfile.mkdtemp()).resolve()

    def test_reads_stay_keyed_and_other_reads_run_no_integration_read(self) -> None:
        fixture = self.accepted_package_fixture()
        statements: list[str] = []
        open_database = sqlite_database.open_database

        def traced(*arguments: object) -> sqlite3.Connection:
            connection = open_database(*arguments)  # type: ignore[arg-type]
            connection.set_trace_callback(statements.append)
            return connection

        with patch("pinboard.adapters.sqlite.store.open_database", traced):
            self.assertEqual("content-not-present", self.integration(fixture, self.base(fixture))["presence"])
        reads = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
        self.assertTrue(reads)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            for statement in reads:
                plan = " ".join(str(row[3]) for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}"))
                with self.subTest(statement=statement):
                    self.assertNotRegex(plan, r"\bSCAN (?!CONSTANT)")
        with (
            patch.object(SQLiteWorkStore, "read_item_integration", side_effect=AssertionError),
            patch.object(candidate_evidence, "observe_item_integration", side_effect=AssertionError),
            patch.object(candidate_evidence.root, "observe_target_content", side_effect=AssertionError),
        ):
            item = call_advertised_tool(
                mcp_server.ITEM_STATUS_TOOL,
                {"request": {**self.roots(fixture), "operation": "item", "item_id": "work-a"}},
            )
            self.assertEqual("pinboard-item-status/v2", item["schema"], item)
            overview = call_native_tool(mcp_server.OVERVIEW_TOOL, self.roots(fixture))
            self.assertNotIn("presence", str(overview))
            reconciliations: tuple[JsonValue, ...] = (
                None,
                {
                    "target_revision": self.base(fixture),
                    "relation": "candidate-integrated",
                    "phase": "cleanup",
                    "effects": [
                        {"effect": effect, "status": status}
                        for effect, status in (
                            ("source-checkout", "allowed"),
                            ("shared-work-root", "not-required"),
                            ("git-metadata", "allowed"),
                        )
                    ],
                },
            )
            for reconciliation in reconciliations:
                inspection = call_native_tool(
                    mcp_server.ATTEMPT_INSPECT_TOOL,
                    {**self.roots(fixture), "attempt_id": "work-a-1", "reconciliation": reconciliation},
                )
                self.assertEqual("ok", inspection["status"], inspection)
