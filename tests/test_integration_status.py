"""Exercise the local content integration leaf through the advertised native MCP boundary."""

import contextlib
import hashlib
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override

from pinboard.adapters.files.root import (
    ContentIntegrationPresence,
    ResolvedIntegrationTarget,
    UnresolvedIntegrationTarget,
    observe_content_integration,
)
from pinboard.application import query_models
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import server
from tests.checkpoint_support import AcceptedPackageFixture, CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool
from tests.support import JsonObject


def _path_depth(path: Path) -> int:
    return len(path.parts)


class IntegrationStatusTest(CheckpointPackageSupport):
    @override
    def commit_all(self, project: Path, message: str) -> str:
        environment = os.environ | {
            "GIT_AUTHOR_DATE": "2020-01-02T03:04:05+00:00",
            "GIT_COMMITTER_DATE": "2020-01-02T03:04:05+00:00",
        }
        subprocess.run(["git", "add", "--all"], cwd=project, env=environment, check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Pinboard Tests",
                "-c",
                "user.email=pinboard@example.invalid",
                "commit",
                "-m",
                message,
            ],
            cwd=project,
            env=environment,
            check=True,
            capture_output=True,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=project, env=environment, check=True, capture_output=True, text=True
        ).stdout.strip()

    def integration(self, fixture: CheckpointFixture, item_id: str, target: str) -> JsonObject:
        return call_advertised_tool(
            server.ITEM_STATUS_TOOL,
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

    def assert_rejected(
        self,
        result: JsonObject,
        code: str,
        retry: str,
        expected_observed_fields: set[str],
    ) -> None:
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual("rejected", result["status"], result)
        self.assertEqual(code, result["code"], result)
        self.assertFalse(result["state_changed"], result)
        self.assertEqual("unchanged", result["effect"], result)
        self.assertEqual(retry, result["retry"], result)
        self.assertEqual([], result["changed_surfaces"], result)
        observed = self.json_array(result["observed"])
        self.assertTrue(expected_observed_fields <= {self.json_object(fact)["field"] for fact in observed}, result)
        self.assertIsInstance(result.get("next_step"), str)
        self.assertTrue(result["next_step"])

    def git(self, repository: Path, *arguments: str) -> str:
        environment = os.environ | {
            "GIT_AUTHOR_DATE": "2020-01-02T03:04:05+00:00",
            "GIT_COMMITTER_DATE": "2020-01-02T03:04:05+00:00",
        }
        result = subprocess.run(
            ["git", *arguments], cwd=repository, env=environment, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    def test_protected_candidate_reports_base_later_and_overlapping_content(self) -> None:
        fixture = self.checkpoint_fixture(contextual_candidate=True)
        base = fixture.brief.base_revision
        ledger_before = (fixture.work / "state.sqlite3").read_bytes()
        work_files_before = {
            path.relative_to(fixture.work): path.read_bytes() for path in fixture.work.rglob("*") if path.is_file()
        }

        absent = self.integration(fixture, "work-a", base)
        self.assertEqual("pinboard-item-integration/v1", absent["schema"], absent)
        self.assertEqual("content-not-present", absent["presence"], absent)
        self.assertEqual(base, absent["resolved_target_revision"], absent)
        source = self.json_object(absent["source"])
        self.assertEqual(fixture.candidate_revision, source["candidate_revision"])
        self.assertEqual("protected-review", source["kind"])
        self.assertEqual("work-a-1", source["attempt_id"])
        self.assertEqual(base, source["compared_from_revision"])

        target = self.commit_all(fixture.project, "squash-equivalent target")
        present = self.integration(fixture, "work-a", target)
        self.assertEqual("content-present", present["presence"], present)

        tracked = fixture.project / "tracked.txt"
        tracked.write_text("one\ntwo\nthree\ncandidate\nfive\nsix\nseven\neight\nfollow-up\n", encoding="utf-8")
        later = self.commit_all(fixture.project, "later non-overlapping edit")
        later_present = self.integration(fixture, "work-a", later)
        self.assertEqual("content-present", later_present["presence"], later_present)

        tracked.write_text(
            "one\ntwo\nthree\noverlapping replacement\nfive\nsix\nseven\neight\nfollow-up\n", encoding="utf-8"
        )
        overlapping = self.commit_all(fixture.project, "later overlapping edit")
        later_absent = self.integration(fixture, "work-a", overlapping)
        self.assertEqual("content-not-present", later_absent["presence"], later_absent)
        self.assertEqual(ledger_before, (fixture.work / "state.sqlite3").read_bytes())
        self.assertEqual(
            work_files_before,
            {path.relative_to(fixture.work): path.read_bytes() for path in fixture.work.rglob("*") if path.is_file()},
        )

    def test_accepted_checkpoint_and_completion_select_their_owned_candidates(self) -> None:
        accepted: AcceptedPackageFixture = self.accepted_package_fixture()
        self.commit_all(accepted.project, "candidate snapshot")
        self.git(accepted.project, "branch", "main", accepted.brief.base_revision)
        self.git(accepted.project, "switch", "main")
        self.git(accepted.project, "merge", "--squash", "codex/work-a")
        self.commit_all(accepted.project, "squash candidate")
        self.git(accepted.project, "switch", "codex/work-a")
        checkpoint = self.integration(accepted, "work-a", "main")
        self.assertEqual("content-present", checkpoint["presence"], checkpoint)
        checkpoint_source = self.json_object(checkpoint["source"])
        self.assertEqual("accepted-checkpoint", checkpoint_source["kind"])
        self.assertEqual(accepted.candidate_revision, checkpoint_source["candidate_revision"])
        self.assertEqual("work-a-1", checkpoint_source["attempt_id"])
        self.assertEqual(accepted.brief.base_revision, checkpoint_source["compared_from_revision"])
        self.assertEqual(accepted.brief.checkpoint.checkpoint_id, checkpoint_source["checkpoint_id"])
        prerequisite = self.project_action(accepted, "close:work-c")
        closed = self.transition_result(
            accepted,
            prerequisite,
            {"outcome": "done", "reason": "The integration-read fixture prerequisite is satisfied."},
        )
        self.assertEqual("committed", closed["status"], closed)
        resume = self.project_action(accepted, "resume:work-a")
        self.assertEqual("committed", self.transition_result(accepted, resume, {})["status"])
        resumed = self.integration(accepted, "work-a", "main")
        self.assertEqual(checkpoint, resumed)

        terminal = self.terminalize_brief(self.checkpoint_fixture())
        self.record_ready_candidate(terminal)
        attempt_root = terminal.work / "attempts" / "work-a-1"
        complete = self.project_action(terminal, "complete:work-a-1")
        transition = self.transition_result(
            terminal,
            complete,
            {
                "schema": "pinboard-reviewed-completion/v2",
                "candidate": terminal.candidate_revision,
                "evidence": "The completed candidate is retained.",
                "reviewer_task_id": "independent-reviewer",
                "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
                "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
                "packages": [],
            },
        )
        self.assertEqual("committed", transition["status"], transition)
        completed = self.integration(terminal, "work-a", terminal.brief.base_revision)
        self.assertEqual("content-not-present", completed["presence"], completed)
        completion_source = self.json_object(completed["source"])
        self.assertEqual("completion", completion_source["kind"])
        self.assertEqual(terminal.candidate_revision, completion_source["candidate_revision"])
        self.assertEqual("work-a-1", completion_source["attempt_id"])
        self.assertEqual(terminal.brief.base_revision, completion_source["compared_from_revision"])

    def test_native_integration_leaf_recognizes_each_real_merge_shape(self) -> None:
        for merge_kind in ("fast-forward", "merge-commit", "rebase", "squash"):
            with self.subTest(merge_kind=merge_kind):
                candidate_form = "working-tree" if merge_kind == "squash" else "current-head"
                fixture = self.checkpoint_fixture(candidate_form=candidate_form)
                base = fixture.brief.base_revision
                candidate = fixture.candidate_revision
                if candidate_form == "working-tree":
                    candidate = self.commit_all(fixture.project, "candidate snapshot")
                self.git(fixture.project, "branch", "main", base)
                self.git(fixture.project, "switch", "main")
                if merge_kind in {"merge-commit", "rebase"}:
                    (fixture.project / "other.txt").write_text("target-only change\n", encoding="utf-8")
                    self.commit_all(fixture.project, "target-only change")
                if merge_kind == "fast-forward":
                    self.git(fixture.project, "merge", "--ff-only", "codex/work-a")
                elif merge_kind == "merge-commit":
                    self.git(fixture.project, "merge", "--no-ff", "codex/work-a", "-m", "merge candidate")
                elif merge_kind == "rebase":
                    self.git(fixture.project, "switch", "codex/work-a")
                    self.git(fixture.project, "rebase", "main")
                    self.assertNotEqual(candidate, self.git(fixture.project, "rev-parse", "HEAD"))
                    self.assertNotEqual(
                        0,
                        subprocess.run(
                            ["git", "merge-base", "--is-ancestor", candidate, "main"],
                            cwd=fixture.project,
                            check=False,
                            capture_output=True,
                        ).returncode,
                    )
                    self.git(fixture.project, "switch", "main")
                    self.git(fixture.project, "merge", "--ff-only", "codex/work-a")
                else:
                    self.git(fixture.project, "merge", "--squash", "codex/work-a")
                    self.commit_all(fixture.project, "squash candidate")
                result = self.integration(fixture, "work-a", "main")
                self.assertEqual("content-present", result["presence"], result)
                self.assertEqual("main", result["target"], result)
                self.assertEqual(self.git(fixture.project, "rev-parse", "main"), result["resolved_target_revision"])

    def test_no_change_target_resolution_and_all_rejections_use_v3(self) -> None:
        fixture = self.checkpoint_fixture()
        missing = self.integration(fixture, "work-a", "missing-local-target")
        self.assert_rejected(missing, "INTEGRATION_TARGET_UNRESOLVED", "correct-input", {"target", "project_root"})

        for invalid_target in ("-invalid", "bad\0target"):
            with self.subTest(invalid_target=invalid_target):
                invalid = self.integration(fixture, "work-a", invalid_target)
                self.assert_rejected(invalid, "ITEM_STATUS_INVALID", "correct-input", {"item_id", "target"})

        ready = self.integration(fixture, "work-c", fixture.brief.base_revision)
        self.assert_rejected(
            ready, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input", {"item_id", "item_state", "reason"}
        )

        unknown = self.integration(fixture, "missing-item", fixture.brief.base_revision)
        self.assert_rejected(unknown, "ITEM_NOT_FOUND", "correct-input", set())

        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary).resolve()
            non_git = call_advertised_tool(
                server.ITEM_STATUS_TOOL,
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
            self.assert_rejected(
                non_git, "PROJECT_GIT_ROOT_UNAVAILABLE", "correct-input", {"project_root", "git_diagnostic"}
            )

    def test_active_without_checkpoint_and_direct_close_have_no_candidate(self) -> None:
        active = self.checkpoint_fixture()
        returned = self.project_action(active, "return-for-correction:work-a-1")
        self.assertEqual(
            "committed",
            self.transition_result(active, returned, {"reason": "Return for the source-selection fixture."})["status"],
        )
        active_result = self.integration(active, "work-a", active.brief.base_revision)
        self.assert_rejected(
            active_result,
            "INTEGRATION_CANDIDATE_UNAVAILABLE",
            "correct-input",
            {"item_id", "item_state", "reason"},
        )
        active_facts = self.json_array(active_result["observed"])
        active_reason = next(
            self.json_object(fact)["value"] for fact in active_facts if self.json_object(fact)["field"] == "reason"
        )
        self.assertIn("no protected candidate or checkpoint acceptance", str(active_reason))

        closed = self.checkpoint_fixture()
        close = self.project_action(closed, "close:work-c")
        close_transition = self.transition_result(
            closed, close, {"outcome": "done", "reason": "Direct close source fixture."}
        )
        self.assertEqual("committed", close_transition["status"], close_transition)
        close_result = self.integration(closed, "work-c", closed.brief.base_revision)
        self.assert_rejected(
            close_result,
            "INTEGRATION_CANDIDATE_UNAVAILABLE",
            "correct-input",
            {"item_id", "item_state", "reason"},
        )
        close_facts = self.json_array(close_result["observed"])
        close_reason = next(
            self.json_object(fact)["value"] for fact in close_facts if self.json_object(fact)["field"] == "reason"
        )
        self.assertIn("closed directly", str(close_reason))

    def test_rename_and_binary_candidate_diff_is_present_after_squash(self) -> None:
        fixture = self.checkpoint_fixture(
            candidate_form="current-head", contextual_candidate=True, rename_binary_candidate=True
        )
        base = fixture.brief.base_revision
        candidate_diff = fixture.candidate_bytes
        self.assertIn(b"rename from rename-before.txt", candidate_diff)
        self.assertIn(b"rename to rename-after.txt", candidate_diff)
        self.assertIn(b"GIT binary patch", candidate_diff)
        self.git(fixture.project, "branch", "main", base)
        self.git(fixture.project, "switch", "main")
        self.git(fixture.project, "merge", "--squash", "codex/work-a")
        self.commit_all(fixture.project, "squash renamed binary candidate")

        result = self.integration(fixture, "work-a", "main")

        self.assertEqual("content-present", result["presence"], result)
        source = self.json_object(result["source"])
        self.assertEqual("protected-review", source["kind"])
        self.assertEqual(fixture.candidate_revision, source["candidate_revision"])

    def test_empty_candidate_returns_no_change_and_remote_tracking_ref_is_local(self) -> None:
        unchanged = self.checkpoint_fixture(empty_candidate=True)
        result = self.integration(unchanged, "work-a", unchanged.brief.base_revision)
        self.assertEqual("no-change", result["presence"], result)
        self.assertEqual(unchanged.brief.base_revision, result["resolved_target_revision"])

        fixture = self.checkpoint_fixture(candidate_form="current-head")
        target = fixture.brief.base_revision
        self.git(fixture.project, "update-ref", "refs/remotes/origin/review-target", target)
        result = self.integration(fixture, "work-a", "origin/review-target")
        self.assertEqual("content-not-present", result["presence"], result)
        self.assertEqual(target, result["resolved_target_revision"])

    def test_checkpoint_snapshot_tampering_is_diagnosed_without_repair(self) -> None:
        fixture = self.checkpoint_fixture()
        snapshot = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        path = fixture.work / snapshot.reference.selector
        accepted = path.read_bytes()
        path.write_bytes(accepted + b"tampered")
        result = self.integration(fixture, "work-a", fixture.brief.base_revision)
        self.assert_rejected(
            result,
            "INTEGRATION_CANDIDATE_EVIDENCE_INVALID",
            "do-not-retry",
            {"attempt_id", "accepted_reference", "artifact_ref_id", "defect"},
        )
        self.assertEqual(accepted + b"tampered", path.read_bytes())

    def test_damaged_checkpoint_acceptance_receipt_keeps_its_status_failure(self) -> None:
        fixture = self.accepted_package_fixture()
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                """UPDATE transition_history SET outcome_json = '{}'
                   WHERE subject_id = 'work-a-1' AND outcome_schema = 'checkpoint-acceptance/v2'"""
            )
            connection.commit()
        result = self.integration(fixture, "work-a", fixture.brief.base_revision)
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual("TRANSITION_RECEIPT_DAMAGED", result["code"], result)
        self.assertEqual("do-not-retry", result["retry"], result)
        self.assertEqual("unchanged", result["effect"], result)
        self.assertFalse(result["state_changed"], result)
        self.assertEqual([], result["changed_surfaces"], result)
        self.assertTrue(result.get("recovery"), result)

    def record_ready_candidate(self, fixture: CheckpointFixture) -> None:
        snapshot = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        attempt = fixture.store.read_attempt_context(AttemptId("work-a-1"))
        self.assertIsNotNone(snapshot)
        self.assertIsNotNone(attempt)
        assert snapshot is not None and isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        recorded = call_advertised_tool(
            server.REVIEW_JOB_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "review": {
                    "kind": "record-ready",
                    "attempt_id": "work-a-1",
                    "candidate_revision": fixture.candidate_revision,
                    "candidate_snapshot_sha256": snapshot.reference.content_sha256,
                    "accepted_brief_sha256": attempt.brief_reference.content_sha256,
                    "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
                    "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
                    "reviewer_task_id": "independent-reviewer",
                    "verdict": "ready",
                    "acceptance_evidence": "The protected candidate satisfies its accepted brief.",
                },
            },
        )
        self.assertEqual("recorded", recorded["status"], recorded)


class ContentIntegrationAdapterTest(unittest.TestCase):
    def git(self, repository: Path, *arguments: str, expected: int = 0) -> str:
        environment = os.environ | {
            "GIT_AUTHOR_DATE": "2020-01-02T03:04:05+00:00",
            "GIT_COMMITTER_DATE": "2020-01-02T03:04:05+00:00",
        }
        result = subprocess.run(
            ["git", *arguments], cwd=repository, env=environment, check=False, capture_output=True, text=True
        )
        if result.returncode != expected:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def commit(self, repository: Path, message: str) -> str:
        self.git(repository, "add", "--all")
        self.git(
            repository,
            "-c",
            "user.name=Pinboard Tests",
            "-c",
            "user.email=pinboard@example.invalid",
            "commit",
            "-m",
            message,
        )
        return self.git(repository, "rev-parse", "HEAD")

    def test_real_git_merge_shapes_and_read_only_metadata(self) -> None:
        for merge_kind in ("fast-forward", "merge-commit", "rebase", "squash"):
            with self.subTest(merge_kind=merge_kind), tempfile.TemporaryDirectory() as temporary:
                repository = Path(temporary).resolve()
                self.git(repository, "init", "-b", "main")
                (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
                self.commit(repository, "base")
                self.git(repository, "switch", "-c", "candidate")
                (repository / "tracked.txt").write_text("reviewed\n", encoding="utf-8")
                diff = subprocess.run(
                    ["git", "diff", "--binary", "HEAD", "--"], cwd=repository, check=True, capture_output=True
                ).stdout
                self.commit(repository, "candidate")
                candidate = self.git(repository, "rev-parse", "HEAD")
                self.git(repository, "switch", "main")
                if merge_kind == "fast-forward":
                    self.git(repository, "merge", "--ff-only", "candidate")
                elif merge_kind == "merge-commit":
                    self.git(repository, "merge", "--no-ff", "candidate", "-m", "merge candidate")
                elif merge_kind == "rebase":
                    (repository / "other.txt").write_text("target change\n", encoding="utf-8")
                    self.commit(repository, "target change")
                    self.git(repository, "switch", "candidate")
                    self.git(repository, "rebase", "main")
                    rebased = self.git(repository, "rev-parse", "HEAD")
                    self.assertNotEqual(candidate, rebased)
                    self.git(repository, "switch", "main")
                    self.git(repository, "merge", "--ff-only", "candidate")
                else:
                    self.git(repository, "merge", "--squash", "candidate")
                    self.commit(repository, "squash candidate")
                    self.assertNotEqual(candidate, self.git(repository, "rev-parse", "HEAD"))
                target = self.git(repository, "rev-parse", "HEAD")
                metadata = {
                    str(path.relative_to(repository / ".git")): path.read_bytes()
                    for path in (repository / ".git").rglob("*")
                    if path.is_file()
                }
                tracked_before = {
                    str(path.relative_to(repository)): path.read_bytes()
                    for path in repository.rglob("*")
                    if path.is_file() and ".git" not in path.parts
                }
                modes = {path: path.stat().st_mode for path in (repository / ".git").rglob("*")}
                for path in sorted(modes, key=_path_depth, reverse=True):
                    path.chmod(modes[path] & ~0o222)
                git_directories = [repository / ".git", *(path for path in modes if path.is_dir())]
                directory_modes = {path: path.stat().st_mode for path in git_directories}
                for path in sorted(directory_modes, key=_path_depth, reverse=True):
                    path.chmod(directory_modes[path] & ~0o222)
                temporary_indexes = set(Path(tempfile.gettempdir()).glob("pinboard-integration-*"))
                try:
                    resolved = observe_content_integration(repository, "main", diff)
                finally:
                    for path in sorted(directory_modes, key=_path_depth):
                        path.chmod(directory_modes[path])
                    for path, mode in modes.items():
                        path.chmod(mode)
                self.assertIsInstance(resolved, ResolvedIntegrationTarget)
                assert isinstance(resolved, ResolvedIntegrationTarget)
                self.assertEqual(target, resolved.revision)
                self.assertEqual(ContentIntegrationPresence.PRESENT, resolved.presence)
                self.assertEqual(
                    metadata,
                    {
                        str(path.relative_to(repository / ".git")): path.read_bytes()
                        for path in (repository / ".git").rglob("*")
                        if path.is_file()
                    },
                )
                self.assertEqual(
                    tracked_before,
                    {
                        str(path.relative_to(repository)): path.read_bytes()
                        for path in repository.rglob("*")
                        if path.is_file() and ".git" not in path.parts
                    },
                )
                self.assertEqual(temporary_indexes, set(Path(tempfile.gettempdir()).glob("pinboard-integration-*")))

    def test_unresolved_target_and_whitespace_config_are_typed_and_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve()
            self.git(repository, "init", "-b", "main")
            (repository / "tracked.txt").write_text("line with spaces   \n", encoding="utf-8")
            self.commit(repository, "base")
            diff = subprocess.run(
                ["git", "diff", "--binary", "HEAD", "--"], cwd=repository, check=True, capture_output=True
            ).stdout
            self.assertIsInstance(
                observe_content_integration(repository, "unknown-ref", diff), UnresolvedIntegrationTarget
            )
            (repository / "tracked.txt").write_text("changed with spaces   \n", encoding="utf-8")
            diff = subprocess.run(
                ["git", "diff", "--binary", "HEAD", "--"], cwd=repository, check=True, capture_output=True
            ).stdout
            self.commit(repository, "content present")
            self.git(repository, "config", "apply.whitespace", "error")
            result = observe_content_integration(repository, "main", diff)
            self.assertIsInstance(result, ResolvedIntegrationTarget)
            assert isinstance(result, ResolvedIntegrationTarget)
            self.assertEqual(ContentIntegrationPresence.PRESENT, result.presence)
