"""Integration leaf behavior over real temporary Git repositories and real transitions."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from pinboard.adapters import candidate_evidence
from pinboard.adapters.files.errors import RootError
from pinboard.adapters.files.root import (
    ResolvedIntegrationPresence,
    UnresolvedIntegrationTarget,
    read_integration_presence,
)
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import query_models
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import JsonObject

FIXED_DATES = {
    "GIT_AUTHOR_DATE": "2020-01-01T00:00:00 +0000",
    "GIT_COMMITTER_DATE": "2020-01-01T00:00:00 +0000",
}


def _commit(repo: Path, message: str, day: int) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": f"2020-01-{day:02d}T00:00:00 +0000",
        "GIT_COMMITTER_DATE": f"2020-01-{day:02d}T00:00:00 +0000",
    }
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-q", "-m", message],
        cwd=repo,
        check=True,
        capture_output=True,
        env=env,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


class ItemIntegrationTest(CheckpointPackageSupport):
    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def integration_leaf(self, fixture: CheckpointFixture, target: str, item_id: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "operation": "integration",
                    "item_id": item_id,
                    "target": target,
                }
            },
        )

    def integrate_squash(
        self, repo: Path, candidate_branch: str, target_branch: str, message: str, base: str | None = None
    ) -> str:
        start = base or "HEAD"
        subprocess.run(["git", "checkout", "-q", "-b", target_branch, start], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "merge", "-q", "--squash", candidate_branch], cwd=repo, check=True, capture_output=True)
        return _commit(repo, message, 3)

    def transition(self, fixture: CheckpointFixture, action_id: str, payload: JsonObject) -> JsonObject:
        result = self.transition_result(fixture, self.project_action(fixture, action_id), payload)
        self.assertIn(result["status"], ("committed", "committed-with-warning"), result)
        return result

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

    def test_protected_working_tree_candidate_squash_merged_reports_content_present(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="working-tree")
        target = self.integrate_squash(fixture.project, fixture.brief.branch, "target", "squashed")
        result = self.integration_leaf(fixture, "target")
        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual("work-a", result["item_id"])
        self.assertEqual("target", result["target"])
        self.assertEqual(target, result["target_revision"])
        source = self.json_object(result["source"])
        self.assertEqual("protected-review", source["kind"])
        self.assertEqual("work-a-1", source["attempt_id"])
        self.assertTrue(str(source["candidate_revision"]))
        self.assertTrue(str(source["compared_from_revision"]))
        self.assertEqual("content-present", result["presence"], result)

    def test_protected_commit_candidate_fast_forward_reports_content_present(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        repo = fixture.project
        subprocess.run(
            ["git", "branch", "target", fixture.candidate_revision], cwd=repo, check=True, capture_output=True
        )
        result = self.integration_leaf(fixture, "target")
        self.assertEqual("content-present", result["presence"], result)
        self.assertEqual(fixture.candidate_revision, result["target_revision"])
        source = self.json_object(result["source"])
        self.assertEqual("protected-review", source["kind"])

    def test_protected_commit_candidate_merge_commit_reports_content_present(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        repo = fixture.project
        base = subprocess.run(
            ["git", "rev-parse", f"{fixture.candidate_revision}^{{}}"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(["git", "checkout", "-q", "-b", "target", base], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "merge", "-q", "-m", "merge", fixture.candidate_revision], cwd=repo, check=True, capture_output=True
        )
        result = self.integration_leaf(fixture, "target")
        self.assertEqual("content-present", result["presence"], result)

    def test_protected_commit_candidate_rebase_merge_reports_content_present(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        repo = fixture.project
        base = subprocess.run(
            ["git", "rev-parse", f"{fixture.candidate_revision}^"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(["git", "checkout", "-q", "-b", "target", base], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "cherry-pick", fixture.candidate_revision], cwd=repo, check=True, capture_output=True)
        result = self.integration_leaf(fixture, "target")
        self.assertEqual("content-present", result["presence"], result)
        target_revision = result["target_revision"]
        self.assertNotEqual(fixture.candidate_revision, target_revision, result)
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", fixture.candidate_revision, str(target_revision)],
            cwd=repo,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(0, ancestor.returncode, "the rebased commit must not be an ancestor of the target")

    def test_accepted_checkpoint_candidate_squash_merged_reports_content_present(self) -> None:
        fixture = self.accepted_package_fixture(candidate_form="current-head")
        target = self.integrate_squash(
            fixture.project, fixture.brief.branch, "target", "checkpoint-squash", base=fixture.brief.base_revision
        )
        result = self.integration_leaf(fixture, "target")
        self.assertEqual("content-present", result["presence"], result)
        source = self.json_object(result["source"])
        self.assertEqual("accepted-checkpoint", source["kind"], result)
        self.assertEqual(fixture.brief.checkpoint.checkpoint_id, source["checkpoint_id"], source)
        self.assertEqual(target, result["target_revision"])

    def test_completion_candidate_reports_content_present(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        self.close_prerequisite(fixture)
        self.record_ready(fixture, fixture.candidate_revision)
        fixture = self.terminalize_brief(fixture)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        completed = self.transition(
            fixture,
            "complete:work-a-1",
            {
                "schema": "pinboard-reviewed-completion/v2",
                "candidate": fixture.candidate_revision,
                "evidence": "Reviewed and complete.",
                "reviewer_task_id": "independent-reviewer",
                "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
                "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
                "packages": [],
            },
        )
        self.assertIn(completed["status"], ("committed", "committed-with-warning"), completed)
        self.integrate_squash(
            fixture.project, fixture.brief.branch, "target", "completion-squash", base=fixture.brief.base_revision
        )
        result = call_native_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "operation": "integration",
                    "item_id": "work-a",
                    "target": "target",
                }
            },
        )
        self.assertEqual("content-present", result["presence"], result)
        source = self.json_object(result["source"])
        self.assertEqual("completion", source["kind"], source)
        self.assertEqual("work-a-1", source["attempt_id"])

    def test_later_non_overlapping_edit_still_reports_content_present(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="working-tree")
        repo = fixture.project
        self.integrate_squash(repo, fixture.brief.branch, "target", "squashed")
        (repo / "unrelated.txt").write_text("new\n", encoding="utf-8")
        _commit(repo, "unrelated", 4)
        result = self.integration_leaf(fixture, "target")
        self.assertEqual("content-present", result["presence"], result)

    def test_empty_recorded_diff_reports_no_change(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="working-tree")
        store = SQLiteWorkStore(fixture.work / "state.sqlite3")
        selection = query_models.IntegrationSourceSelection(
            "protected-review", "work-a-1", fixture.candidate_revision, None, None
        )
        evidence = candidate_evidence.read_integration_presence(fixture.project, fixture.work, store, selection, "HEAD")
        self.assertIsInstance(evidence, candidate_evidence.IntegrationPresenceEvidence)
        assert isinstance(evidence, candidate_evidence.IntegrationPresenceEvidence)
        self.assertTrue(
            evidence.presence in ("no-change", "content-present", "content-not-present"),
            evidence.presence,
        )

    def test_target_at_accepted_base_reports_content_not_present(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="working-tree")
        repo = fixture.project
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        result = self.integration_leaf(fixture, base)
        self.assertEqual("content-not-present", result["presence"], result)
        self.assertEqual(base, result["target_revision"])

    def test_later_overlapping_edit_reports_content_not_present(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="working-tree")
        repo = fixture.project
        self.integrate_squash(repo, fixture.brief.branch, "target", "squashed")
        (repo / "tracked.txt").write_text("overlapping\n", encoding="utf-8")
        _commit(repo, "overlap", 4)
        result = self.integration_leaf(fixture, "target")
        self.assertEqual("content-not-present", result["presence"], result)

    def test_remote_tracking_ref_read_locally_reports_content_present(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        repo = fixture.project
        subprocess.run(
            ["git", "branch", "target", fixture.candidate_revision], cwd=repo, check=True, capture_output=True
        )
        target_revision = fixture.candidate_revision
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", target_revision],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        result = self.integration_leaf(fixture, "origin/main")
        self.assertEqual("content-present", result["presence"], result)
        self.assertEqual("origin/main", result["target"])
        self.assertEqual(target_revision, result["target_revision"])

    def test_unresolved_target_rejection(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="working-tree")
        result = self.integration_leaf(fixture, "nosuchref")
        self.assertEqual("rejected", result["status"], result)
        self.assertEqual("INTEGRATION_TARGET_UNRESOLVED", result["code"], result)
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual("unchanged", result["effect"])
        self.assertEqual("correct-input", result["retry"])
        self.assertFalse(result["state_changed"])
        observed_entries = self.json_array(result["observed"])
        observed = {
            str(self.json_object(entry)["field"]): self.json_object(entry)["value"] for entry in observed_entries
        }
        self.assertEqual("nosuchref", observed["target"])
        self.assertIn("project_root", observed)
        self.assertIn("fetch", str(result["recovery"]).lower())

    def test_candidate_unavailable_for_active_item_without_checkpoint(self) -> None:
        fixture = self.checkpoint_fixture()
        self.transition(fixture, "return-for-correction:work-a-1", {"reason": "Needs a fix."})
        result = self.integration_leaf(fixture, "target")
        self.assertEqual("rejected", result["status"], result)
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", result["code"], result)
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual("unchanged", result["effect"])
        self.assertEqual("correct-input", result["retry"])
        observed_entries = self.json_array(result["observed"])
        observed = {
            str(self.json_object(entry)["field"]): self.json_object(entry)["value"] for entry in observed_entries
        }
        self.assertEqual("work-a", observed["item_id"])
        self.assertTrue("no protected candidate" in str(observed["reason"]), observed["reason"])
        self.assertIn("item leaf", str(result["recovery"]).lower())

    def test_candidate_unavailable_for_direct_close(self) -> None:
        fixture = self.checkpoint_fixture()
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
        result = call_native_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "operation": "integration",
                    "item_id": "work-c",
                    "target": "target",
                }
            },
        )
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", result["code"], result)
        observed_entries = self.json_array(result["observed"])
        observed = {
            str(self.json_object(entry)["field"]): self.json_object(entry)["value"] for entry in observed_entries
        }
        self.assertEqual("a direct close", observed["reason"])

    def test_candidate_evidence_invalid_for_altered_snapshot(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="working-tree")
        self.integrate_squash(fixture.project, fixture.brief.branch, "target", "squashed")
        evidence_root = fixture.work / "artifacts"
        for path in evidence_root.rglob("*.json"):
            data = path.read_bytes()
            if b"candidate_kind" in data:
                path.write_bytes(data + b" ")
                break
        result = self.integration_leaf(fixture, "target")
        self.assertEqual("rejected", result["status"], result)
        self.assertEqual("INTEGRATION_CANDIDATE_EVIDENCE_INVALID", result["code"], result)
        self.assertEqual("do-not-retry", result["retry"])
        self.assertIn("validate", str(result["recovery"]).lower())

    def test_unknown_item_rejection(self) -> None:
        fixture = self.checkpoint_fixture()
        result = self.integration_leaf(fixture, "target", item_id="missing-item")
        self.assertEqual("ITEM_NOT_FOUND", result["code"], result)
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"])

    def test_git_failure_for_non_git_project_root(self) -> None:
        fixture = self.checkpoint_fixture()
        nongit = Path(tempfile.mkdtemp())
        result = call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "project_root": str(nongit),
                    "work_root": str(fixture.work),
                    "operation": "integration",
                    "item_id": "work-a",
                    "target": "target",
                }
            },
        )
        self.assertEqual("rejected", result["status"], result)
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertIn(
            str(result["code"]),
            {
                "PROJECT_GIT_ROOT_UNAVAILABLE",
                "PROJECT_GIT_CHECKOUT_UNAVAILABLE",
                "ITEM_STATUS_INVALID",
            },
            result,
        )
        self.assertEqual("unchanged", result["effect"])

    def test_invalid_target_beginning_with_dash_is_rejected(self) -> None:
        fixture = self.checkpoint_fixture()
        result = self.integration_leaf(fixture, "-not-a-ref")
        self.assertEqual("rejected", result["status"], result)
        code_value = str(result["code"])
        self.assertTrue(
            code_value in ("ITEM_STATUS_INVALID", "PROJECT_GIT_CHECKOUT_UNAVAILABLE"),
            result,
        )

    def test_item_and_branch_leaves_still_use_v3_rejections(self) -> None:
        fixture = self.checkpoint_fixture()
        missing = call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "operation": "item",
                    "item_id": "missing-item",
                }
            },
        )
        self.assertEqual("pinboard-mcp-item-status-result/v3", missing["schema"], missing)
        self.assertEqual("ITEM_NOT_FOUND", missing["code"], missing)
        branch = call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "operation": "branch",
                    "branch": "nosuch-branch",
                }
            },
        )
        self.assertEqual("pinboard-mcp-item-status-result/v3", branch["schema"], branch)
        self.assertEqual("BRANCH_OWNER_NOT_FOUND", branch["code"], branch)


class IntegrationGitReadTest(CheckpointPackageSupport):
    def run_git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout

    def build_repo(self) -> tuple[Path, bytes, str]:
        repo = Path(tempfile.mkdtemp()).resolve()
        self.run_git(repo, "init", "-b", "work")
        (repo / "file.txt").write_text("".join(f"line{i}\n" for i in range(1, 51)), encoding="utf-8")
        (repo / "bin.dat").write_bytes(b"\x00\x01\x02")
        base = _commit(repo, "base", 1)
        (repo / "file.txt").write_text(
            "".join("CHANGED\n" if i == 9 else f"line{i}\n" for i in range(1, 51)), encoding="utf-8"
        )
        (repo / "bin.dat").write_bytes(b"\x00\x01\x03")
        candidate = _commit(repo, "candidate", 2)
        diff = subprocess.run(
            ["git", "diff", "--binary", base, candidate, "--"],
            cwd=repo,
            check=True,
            capture_output=True,
        ).stdout
        return repo, diff, base

    def test_present_not_present_unresolved_and_empty(self) -> None:
        repo, diff, base = self.build_repo()
        subprocess.run(["git", "checkout", "-q", "-b", "target", base], cwd=repo, check=True, capture_output=True)
        (repo / "file.txt").write_text(
            "".join("CHANGED\n" if i == 9 else f"line{i}\n" for i in range(1, 51)), encoding="utf-8"
        )
        (repo / "bin.dat").write_bytes(b"\x00\x01\x03")
        _commit(repo, "squash", 3)
        self.assertIsInstance(read_integration_presence(repo, "target", diff), ResolvedIntegrationPresence)
        present = read_integration_presence(repo, "target", diff)
        assert isinstance(present, ResolvedIntegrationPresence)
        self.assertTrue(present.present)
        not_present = read_integration_presence(repo, base, diff)
        assert isinstance(not_present, ResolvedIntegrationPresence)
        self.assertFalse(not_present.present)
        unresolved = read_integration_presence(repo, "nosuchref", diff)
        self.assertIsInstance(unresolved, UnresolvedIntegrationTarget)

    def test_read_only_git_leaves_repository_unchanged(self) -> None:
        repo, diff, base = self.build_repo()
        subprocess.run(["git", "checkout", "-q", "-b", "target", base], cwd=repo, check=True, capture_output=True)
        (repo / "file.txt").write_text(
            "".join("CHANGED\n" if i == 9 else f"line{i}\n" for i in range(1, 51)), encoding="utf-8"
        )
        (repo / "bin.dat").write_bytes(b"\x00\x01\x03")
        _commit(repo, "squash", 3)

        def snapshot() -> dict[str, object]:
            git_dir = repo / ".git"
            files = {
                str(path.relative_to(git_dir)): path.read_bytes()
                for path in sorted(git_dir.rglob("*"))
                if path.is_file()
            }
            head = self.run_git(repo, "rev-parse", "HEAD").strip()
            index = (git_dir / "index").read_bytes() if (git_dir / "index").exists() else None
            status = self.run_git(repo, "status", "--porcelain")
            refs = self.run_git(repo, "show-ref")
            return {"files": files, "head": head, "index": index, "status": status, "refs": refs}

        before = snapshot()
        git_dir = repo / ".git"
        for path in sorted(git_dir.rglob("*"), reverse=True):
            if path.is_file():
                path.chmod(0o444)
        for path in sorted((p for p in git_dir.rglob("*") if p.is_dir()), reverse=True):
            path.chmod(0o555)
        git_dir.chmod(0o555)
        try:
            observation = read_integration_presence(repo, "target", diff)
            self.assertIsInstance(observation, ResolvedIntegrationPresence)
        finally:
            git_dir.chmod(0o755)
            for path in git_dir.rglob("*"):
                path.chmod(0o755 if path.is_dir() else 0o644)
        after = snapshot()
        self.assertEqual(before, after)
        leftovers = [
            entry.name
            for entry in Path(tempfile.gettempdir()).iterdir()
            if entry.name.startswith("pinboard-integration-")
        ]
        self.assertEqual([], leftovers)

    def test_non_git_directory_raises_root_error(self) -> None:
        directory = Path(tempfile.mkdtemp())
        with self.assertRaises(RootError) as raised:
            read_integration_presence(directory, "target", b"diff --git a/x b/x\n")
        self.assertIn("PROJECT_GIT", str(raised.exception))

    def test_whitespace_error_config_still_reports_present(self) -> None:
        repo, diff, base = self.build_repo()
        subprocess.run(["git", "checkout", "-q", "-b", "target", base], cwd=repo, check=True, capture_output=True)
        (repo / "file.txt").write_text(
            "".join("CHANGED\n" if i == 9 else f"line{i}\n" for i in range(1, 51)), encoding="utf-8"
        )
        (repo / "bin.dat").write_bytes(b"\x00\x01\x03")
        _commit(repo, "squash", 3)
        self.run_git(repo, "config", "apply.whitespace", "error")
        try:
            observation = read_integration_presence(repo, "target", diff)
            assert isinstance(observation, ResolvedIntegrationPresence)
            self.assertTrue(observation.present, observation)
        finally:
            self.run_git(repo, "config", "--unset", "apply.whitespace")
