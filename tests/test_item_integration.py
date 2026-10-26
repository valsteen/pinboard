"""Integration status compares accepted candidate bytes with locally named target trees."""

import hashlib
import os
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path
from typing import override
from unittest.mock import patch

from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.application import query_models, work_brief_models
from pinboard.domain.identifiers import AttemptId, WorkItemId
from pinboard.mcp import server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import JsonObject


class ItemIntegrationTest(CheckpointPackageSupport):
    @override
    def setUp(self) -> None:
        dates = patch.dict(
            os.environ,
            {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00+0000"},
        )
        dates.start()
        self.addCleanup(dates.stop)

    def git(self, fixture: CheckpointFixture, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=fixture.project, check=True, capture_output=True, text=True
        ).stdout.strip()

    def integration(self, fixture: CheckpointFixture, target: str, item_id: str = "work-a") -> JsonObject:
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

    def assert_rejection(self, result: JsonObject, code: str, retry: str) -> None:
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual("rejected", result["status"], result)
        self.assertEqual(code, result["code"], result)
        self.assertFalse(result["state_changed"], result)
        self.assertEqual("unchanged", result["effect"], result)
        self.assertEqual(retry, result["retry"], result)
        self.assertEqual([], result["changed_surfaces"], result)

    def test_protected_working_tree_content_against_base_and_committed_target(self) -> None:
        with patch.dict(
            os.environ,
            {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00+0000"},
        ):
            fixture = self.checkpoint_fixture()
            base = self.integration(fixture, "HEAD")
            self.assertEqual("content-not-present", base["presence"], base)
            candidate = self.commit_all(fixture.project, "candidate")
            self.git(fixture, "switch", "-c", "target", fixture.brief.base_revision)
            self.git(fixture, "merge", "--squash", candidate)
            revision = self.commit_all(fixture.project, "squash candidate")
            result = self.integration(fixture, "HEAD")
        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual("content-present", result["presence"])
        self.assertEqual("HEAD", result["target"])
        self.assertEqual(revision, result["target_revision"])
        source = self.json_object(result["source"])
        self.assertEqual("protected-review", source["kind"])
        self.assertEqual("work-a-1", source["attempt_id"])
        self.assertEqual(fixture.candidate_revision, source["candidate_revision"])
        self.assertEqual(fixture.brief.base_revision, source["compared_from_revision"])

    def test_checkpoint_candidate_remains_available_after_acceptance(self) -> None:
        with patch.dict(
            os.environ,
            {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00+0000"},
        ):
            fixture = self.accepted_package_fixture()
            candidate = self.commit_all(fixture.project, "candidate")
            self.git(fixture, "switch", "-c", "target", fixture.brief.base_revision)
            self.git(fixture, "merge", "--squash", candidate)
            revision = self.commit_all(fixture.project, "squash candidate")
            result = self.integration(fixture, "HEAD")
        self.assertEqual("content-present", result["presence"], result)
        self.assertEqual(revision, result["target_revision"])
        source = self.json_object(result["source"])
        self.assertEqual("accepted-checkpoint", source["kind"])
        self.assertEqual("work-a-1", source["attempt_id"])
        self.assertEqual(fixture.candidate_revision, source["candidate_revision"])
        self.assertEqual(fixture.brief.base_revision, source["compared_from_revision"])
        self.assertEqual(fixture.brief.checkpoint.checkpoint_id, source["checkpoint_id"])
        closed, _, error = self.run_cli(
            *fixture.common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "Satisfied",
            "--task-id",
            "owner",
            "--host-id",
            "local",
        )
        self.assertEqual(0, closed, error)
        resumed = self.transition_result(fixture, self.project_action(fixture, "resume:work-a"), {})
        self.assertEqual("committed", resumed["status"], resumed)
        after_resume = self.integration(fixture, "HEAD")
        self.assertEqual(source, after_resume["source"])
        attempt = fixture.store.read_attempt_context(AttemptId("work-a-1"))
        assert isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        rebound = self.transition_result(
            fixture,
            self.project_action(fixture, "rebind-attempt:work-a-1"),
            {
                "branch": fixture.brief.branch,
                "base_revision": fixture.brief.base_revision,
                "brief_artifact_ref_id": int(attempt.brief_artifact_ref_id),
            },
        )
        self.assertEqual("committed", rebound["status"], rebound)
        self.assertEqual(source, self.integration(fixture, "HEAD")["source"])

    def test_completion_uses_the_closing_candidate(self) -> None:
        dates = {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00+0000"}
        with patch.dict(os.environ, dates):
            fixture = self.terminalize_brief(self.checkpoint_fixture())
            self.commit_all(fixture.project, "candidate")
            attempt_root = fixture.work / "attempts" / "work-a-1"
            result = self.transition_result(
                fixture,
                self.project_action(fixture, "complete:work-a-1"),
                {
                    "schema": "pinboard-reviewed-completion/v2",
                    "candidate": fixture.candidate_revision,
                    "evidence": "Reviewed and retained.",
                    "reviewer_task_id": "independent-reviewer",
                    "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
                    "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
                    "packages": [],
                },
            )
            self.assertEqual("committed", result["status"], result)
            observed = self.integration(fixture, "HEAD")
        self.assertEqual("content-present", observed["presence"], observed)
        source = self.json_object(observed["source"])
        self.assertEqual("completion", source["kind"])
        self.assertEqual("work-a-1", source["attempt_id"])
        self.assertEqual(fixture.candidate_revision, source["candidate_revision"])
        self.assertEqual(fixture.brief.base_revision, source["compared_from_revision"])

    def test_commit_candidates_reach_target_by_common_merge_strategies(self) -> None:
        dates = {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00+0000"}
        for strategy in ("fast-forward", "merge", "rebase", "squash"):
            with self.subTest(strategy=strategy), patch.dict(os.environ, dates):
                fixture = self.checkpoint_fixture()
                returned = self.transition_result(
                    fixture,
                    self.project_action(fixture, "return-for-correction:work-a-1"),
                    {"reason": "Submit a committed candidate."},
                )
                self.assertEqual("committed", returned["status"], returned)
                candidate = self.commit_all(fixture.project, "committed candidate")
                lease = self.native_attempt_acquire(fixture, f"worker-{strategy}")
                action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
                submitted = self.transition_result(fixture, action, {"candidate": candidate})
                self.assertEqual("committed", submitted["status"], submitted)
                self.git(fixture, "switch", "-c", "target", fixture.brief.base_revision)
                if strategy == "rebase":
                    (fixture.project / "other.txt").write_text("target change\n", encoding="utf-8")
                    self.commit_all(fixture.project, "target change")
                    self.git(fixture, "switch", "codex/work-a")
                    self.git(fixture, "-c", "user.name=Test", "-c", "user.email=test@example.com", "rebase", "target")
                    self.git(fixture, "switch", "target")
                    self.git(fixture, "merge", "--ff-only", "codex/work-a")
                elif strategy == "merge":
                    self.git(
                        fixture,
                        "-c",
                        "user.name=Test",
                        "-c",
                        "user.email=test@example.com",
                        "merge",
                        "--no-ff",
                        candidate,
                        "-m",
                        "merge",
                    )
                elif strategy == "squash":
                    self.git(fixture, "merge", "--squash", candidate)
                    self.commit_all(fixture.project, "squash")
                else:
                    self.git(fixture, "merge", "--ff-only", candidate)
                result = self.integration(fixture, "target")
                self.assertEqual("content-present", result["presence"], result)
                self.assertEqual("target", result["target"])
                self.assertEqual(self.git(fixture, "rev-parse", "HEAD"), result["target_revision"])
                source = self.json_object(result["source"])
                self.assertEqual("protected-review", source["kind"])
                self.assertEqual("work-a-1", source["attempt_id"])
                self.assertEqual(candidate, source["candidate_revision"])
                self.assertEqual(fixture.brief.base_revision, source["compared_from_revision"])

    def test_later_edits_and_local_remote_tracking_ref(self) -> None:
        dates = {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00+0000"}
        with patch.dict(os.environ, dates):
            fixture = self.checkpoint_fixture()
            action = self.project_action(fixture, "return-for-correction:work-a-1")
            self.transition_result(fixture, action, {"reason": "Prepare a longer reviewed file."})
            tracked = fixture.project / "tracked.txt"
            lines = [f"line {number}\n" for number in range(20)]
            tracked.write_text("".join(lines), encoding="utf-8")
            self.commit_all(fixture.project, "long base")
            lines[2] = "candidate\n"
            tracked.write_text("".join(lines), encoding="utf-8")
            observed = call_native_tool(
                server.CANDIDATE_OBSERVE_TOOL,
                {"project_root": str(fixture.project), "work_root": str(fixture.work), "attempt_id": "work-a-1"},
            )
            lease = self.native_attempt_acquire(fixture, "worker-later-edits")
            action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
            submitted = self.transition_result(fixture, action, {"candidate": observed["candidate"]})
            self.assertEqual("committed", submitted["status"], submitted)
            self.commit_all(fixture.project, "candidate")
            lines[-1] = "unrelated\n"
            tracked.write_text("".join(lines), encoding="utf-8")
            non_overlapping = self.commit_all(fixture.project, "later separate edit")
            self.git(fixture, "update-ref", "refs/remotes/origin/main", non_overlapping)
            self.assertEqual("content-present", self.integration(fixture, "origin/main")["presence"])
            lines[2] = "overwritten\n"
            tracked.write_text("".join(lines), encoding="utf-8")
            self.commit_all(fixture.project, "later overlapping edit")
            self.assertEqual("content-not-present", self.integration(fixture, "HEAD")["presence"])

    def test_rejections_name_conditions_and_safe_recovery(self) -> None:
        fixture = self.checkpoint_fixture()
        unresolved = self.integration(fixture, "not-a-ref")
        self.assert_rejection(unresolved, "INTEGRATION_TARGET_UNRESOLVED", "correct-input")
        self.assertIn("local branch", str(unresolved["recovery"]))
        self.assertIn("fetch outside Pinboard", str(unresolved["recovery"]))
        self.assertIn({"field": "target", "value": "not-a-ref"}, self.json_array(unresolved["observed"]))
        self.assertIn({"field": "project_root", "value": str(fixture.project)}, self.json_array(unresolved["observed"]))
        missing = self.integration(fixture, "HEAD", "missing")
        self.assert_rejection(missing, "ITEM_NOT_FOUND", "correct-input")
        self.assertIn("missing", str(missing["message"]))
        self.assertEqual([], missing["observed"])
        ready = self.integration(fixture, "HEAD", "work-c")
        self.assert_rejection(ready, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.assertTrue(ready["message"])
        self.assertIn("operation item", str(ready["recovery"]))
        self.assertIn({"field": "item_id", "value": "work-c"}, self.json_array(ready["observed"]))
        self.assertIn({"field": "state", "value": "ready"}, self.json_array(ready["observed"]))
        closed, _, error = self.run_cli(
            *fixture.common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "Satisfied",
            "--task-id",
            "owner",
            "--host-id",
            "local",
        )
        self.assertEqual(0, closed, error)
        direct = self.integration(fixture, "HEAD", "work-c")
        self.assert_rejection(direct, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.assertIn("closed directly", str(direct["message"]))
        self.assertIn("operation item", str(direct["recovery"]))
        self.assertIn({"field": "item_id", "value": "work-c"}, self.json_array(direct["observed"]))
        self.assertIn({"field": "state", "value": "done"}, self.json_array(direct["observed"]))
        invalid = call_native_tool(
            server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "operation": "integration",
                    "item_id": "work-a",
                    "target": "-bad",
                }
            },
        )
        self.assert_rejection(invalid, "ITEM_STATUS_INVALID", "correct-input")
        self.assertIn("target", str(invalid["message"]))
        self.assertEqual([], invalid["observed"])
        snapshot = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert snapshot is not None
        (fixture.work / snapshot.reference.selector).write_bytes(b"damaged")
        damaged = self.integration(fixture, "HEAD")
        self.assert_rejection(damaged, "INTEGRATION_CANDIDATE_EVIDENCE_INVALID", "do-not-retry")
        self.assertIn("pinboard validate", str(damaged["recovery"]))
        self.assertIn({"field": "attempt_id", "value": "work-a-1"}, self.json_array(damaged["observed"]))
        self.assertIn(
            {"field": "reference", "value": snapshot.reference.selector}, self.json_array(damaged["observed"])
        )

    def test_non_git_project_is_a_typed_failure(self) -> None:
        fixture = self.checkpoint_fixture()
        with tempfile.TemporaryDirectory() as directory:
            result = call_native_tool(
                server.ITEM_STATUS_TOOL,
                {
                    "request": {
                        "project_root": directory,
                        "work_root": str(fixture.work),
                        "operation": "integration",
                        "item_id": "work-a",
                        "target": "HEAD",
                    }
                },
            )
        self.assert_rejection(result, "PROJECT_GIT_ROOT_UNAVAILABLE", "correct-input")
        self.assertIn({"field": "project_root", "value": directory}, self.json_array(result["observed"]))
        self.assertTrue(result["message"])
        self.assertIn("checkout", str(result["recovery"]))

    def test_unreadable_target_tree_is_a_typed_git_failure(self) -> None:
        fixture = self.checkpoint_fixture()
        self.commit_all(fixture.project, "candidate")
        tree = self.git(fixture, "rev-parse", "HEAD^{tree}")
        (fixture.project / ".git" / "objects" / tree[:2] / tree[2:]).unlink()
        result = self.integration(fixture, "HEAD")
        self.assert_rejection(result, "PROJECT_GIT_CHECKOUT_UNAVAILABLE", "correct-input")
        self.assertIn({"field": "project_root", "value": str(fixture.project)}, self.json_array(result["observed"]))
        self.assertIn({"field": "diagnostic", "value": result["message"]}, self.json_array(result["observed"]))
        self.assertIn("checkout", str(result["recovery"]))

    def test_damaged_checkpoint_receipt_and_legacy_package_are_diagnosed(self) -> None:
        legacy = self.accepted_package_fixture(local=True, candidate_form="current-head")
        self.retain_v2_checkpoint(legacy)
        unavailable = self.integration(legacy, "HEAD")
        self.assert_rejection(unavailable, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.assertIn("snapshot reference", str(unavailable["message"]))
        self.assertIn("operation item", str(unavailable["recovery"]))
        self.assertIn({"field": "item_id", "value": "work-a"}, self.json_array(unavailable["observed"]))
        self.assertIn({"field": "state", "value": "paused"}, self.json_array(unavailable["observed"]))
        fixture = self.accepted_package_fixture()
        with closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE transition_history SET outcome_json = ? WHERE outcome_schema = 'checkpoint-acceptance/v2'",
                ("{",),
            )
        damaged = self.integration(fixture, "HEAD")
        self.assert_rejection(damaged, "TRANSITION_RECEIPT_DAMAGED", "do-not-retry")
        self.assertIn("Report to the human", str(damaged["recovery"]))
        self.assertIn({"field": "attempt_id", "value": "work-a-1"}, self.json_array(damaged["observed"]))

    def test_damaged_checkpoint_candidate_names_the_failed_reference(self) -> None:
        fixture = self.accepted_package_fixture()
        package = self.package(fixture)
        assert isinstance(package, work_brief_models.CheckpointReviewPackageV3)
        selector = package.candidate_snapshot.selector
        (fixture.work / selector).write_bytes(b"damaged")
        result = self.integration(fixture, "HEAD")
        self.assertEqual("INTEGRATION_CANDIDATE_EVIDENCE_INVALID", result["code"], result)
        self.assertIn({"field": "reference", "value": selector}, self.json_array(result["observed"]))

    def test_damaged_submission_receipt_is_diagnosed(self) -> None:
        for payload in ("{", "{}"):
            with self.subTest(payload=payload):
                fixture = self.checkpoint_fixture()
                context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
                assert context is not None
                with closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
                    connection.execute(
                        "UPDATE transition_history SET input_json = ? WHERE history_id = ?",
                        (payload, int(context.receipt.history_id)),
                    )
                result = self.integration(fixture, "HEAD")
                self.assertEqual("TRANSITION_RECEIPT_DAMAGED", result["code"], result)
                self.assertEqual("do-not-retry", result["retry"])
                self.assertIn(
                    {"field": "history_id", "value": int(context.receipt.history_id)},
                    self.json_array(result["observed"]),
                )

    def test_unavailable_or_damaged_checkpoint_evidence_has_a_safe_diagnosis(self) -> None:
        for defect in (
            "missing-package",
            "altered-package",
            "invalid-package",
            "missing-candidate",
            "mismatched-candidate",
        ):
            with self.subTest(defect=defect):
                fixture = self.accepted_package_fixture()
                package = self.package(fixture)
                assert isinstance(package, work_brief_models.CheckpointReviewPackageV3)
                if defect == "altered-package":
                    (fixture.work / fixture.package_reference.selector).write_bytes(b"damaged")
                elif defect == "invalid-package":
                    self.replace_artifact_bytes(fixture, fixture.package_reference, b"{}")
                else:
                    with closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
                        if defect == "missing-package":
                            connection.execute(
                                "UPDATE transition_history SET artifact_ref_id = NULL, artifact_kind = NULL WHERE outcome_schema = 'checkpoint-acceptance/v2'"
                            )
                        elif defect == "missing-candidate":
                            connection.execute(
                                "DELETE FROM artifact_refs WHERE relative_path = ?",
                                (package.candidate_snapshot.selector,),
                            )
                        else:
                            connection.execute(
                                "UPDATE artifact_refs SET content_sha256 = ? WHERE relative_path = ?",
                                ("0" * 64, package.candidate_snapshot.selector),
                            )
                result = self.integration(fixture, "HEAD")
                self.assertEqual("INTEGRATION_CANDIDATE_EVIDENCE_INVALID", result["code"], result)
                self.assertEqual("unchanged", result["effect"])
                self.assertEqual("do-not-retry", result["retry"])
                self.assertIn("pinboard validate", str(result["recovery"]))

    def test_retained_pre_snapshot_review_is_unavailable(self) -> None:
        fixture = self.checkpoint_fixture()
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        with closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE transition_history SET artifact_ref_id = NULL, artifact_kind = NULL, input_schema = 'decision/v1', input_json = '{}' WHERE history_id = ?",
                (int(context.receipt.history_id),),
            )
            connection.execute(
                "DELETE FROM artifact_refs WHERE artifact_ref_id = ?", (int(context.reference.artifact_ref_id),)
            )
            connection.execute(
                "UPDATE attempts SET subject_revision = ? WHERE attempt_id = 'work-a-1'",
                (context.receipt.project_revision,),
            )
        result = self.integration(fixture, "HEAD")
        self.assert_rejection(result, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.assertIn("no accepted snapshot", str(result["message"]))
        self.assertIn("operation item", str(result["recovery"]))
        self.assertIn({"field": "item_id", "value": "work-a"}, self.json_array(result["observed"]))
        self.assertIn({"field": "state", "value": "review"}, self.json_array(result["observed"]))

    def test_status_read_leaves_repository_and_ledger_unchanged_with_read_only_git(self) -> None:
        fixture = self.checkpoint_fixture()
        self.commit_all(fixture.project, "candidate")
        git_dir = fixture.project / ".git"
        before_git = {p.relative_to(git_dir): p.read_bytes() for p in git_dir.rglob("*") if p.is_file()}
        before_work = {p.relative_to(fixture.work): p.read_bytes() for p in fixture.work.rglob("*") if p.is_file()}
        before_status = self.git(fixture, "status", "--porcelain=v1", "--untracked-files=all")
        modes = {p: p.stat().st_mode for p in git_dir.rglob("*")}
        with tempfile.TemporaryDirectory() as temporary:
            try:
                for path in modes:
                    path.chmod(0o500 if path.is_dir() else 0o400)
                git_dir.chmod(0o500)
                with patch.object(tempfile, "tempdir", temporary):
                    result = self.integration(fixture, "HEAD")
                self.assertEqual("content-present", result["presence"], result)
                self.assertEqual([], list(Path(temporary).iterdir()))
                self.assertEqual(
                    before_git, {p.relative_to(git_dir): p.read_bytes() for p in git_dir.rglob("*") if p.is_file()}
                )
                self.assertEqual(
                    before_work,
                    {p.relative_to(fixture.work): p.read_bytes() for p in fixture.work.rglob("*") if p.is_file()},
                )
                self.assertEqual(before_status, self.git(fixture, "status", "--porcelain=v1", "--untracked-files=all"))
            finally:
                git_dir.chmod(0o700)
                for path, mode in modes.items():
                    path.chmod(mode)

    def test_return_and_accepted_continuation_do_not_keep_the_review_candidate(self) -> None:
        for kind in ("return-for-correction", "accept-review-and-continue"):
            with self.subTest(kind=kind):
                fixture = self.checkpoint_fixture()
                payload: JsonObject = (
                    {"reason": "Correction requested."}
                    if kind == "return-for-correction"
                    else {"candidate": fixture.candidate_revision, "evidence": "Continue after review."}
                )
                changed = self.transition_result(fixture, self.project_action(fixture, f"{kind}:work-a-1"), payload)
                self.assertEqual("committed", changed["status"], changed)
                result = self.integration(fixture, "HEAD")
                self.assert_rejection(result, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
                self.assertIn("no checkpoint acceptance", str(result["message"]))
                self.assertIn("operation item", str(result["recovery"]))
                self.assertIn({"field": "item_id", "value": "work-a"}, self.json_array(result["observed"]))
                self.assertIn({"field": "state", "value": "active"}, self.json_array(result["observed"]))

    def test_empty_recorded_diff_reports_no_change(self) -> None:
        fixture = self.checkpoint_fixture()
        returned = self.transition_result(
            fixture, self.project_action(fixture, "return-for-correction:work-a-1"), {"reason": "Review no changes."}
        )
        self.assertEqual("committed", returned["status"], returned)
        self.git(fixture, "restore", "tracked.txt")
        observed = call_native_tool(
            server.CANDIDATE_OBSERVE_TOOL,
            {"project_root": str(fixture.project), "work_root": str(fixture.work), "attempt_id": "work-a-1"},
        )
        lease = self.native_attempt_acquire(fixture, "empty-worker")
        action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        submitted = self.transition_result(fixture, action, {"candidate": observed["candidate"]})
        self.assertEqual("committed", submitted["status"], submitted)
        result = self.integration(fixture, "HEAD")
        self.assertEqual("no-change", result["presence"], result)

    def test_squash_of_working_tree_candidate_with_rename_and_binary_change(self) -> None:
        fixture = self.checkpoint_fixture()
        returned = self.transition_result(
            fixture,
            self.project_action(fixture, "return-for-correction:work-a-1"),
            {"reason": "Review binary and rename."},
        )
        self.assertEqual("committed", returned["status"], returned)
        self.git(fixture, "restore", "tracked.txt")
        (fixture.project / "old-name.txt").write_text("same content\n", encoding="utf-8")
        (fixture.project / "binary.dat").write_bytes(b"\x00base\x00")
        preimage = self.commit_all(fixture.project, "binary base")
        self.git(fixture, "mv", "old-name.txt", "new-name.txt")
        (fixture.project / "binary.dat").write_bytes(b"\x00candidate\x00")
        self.git(fixture, "add", "binary.dat")
        observed = call_native_tool(
            server.CANDIDATE_OBSERVE_TOOL,
            {"project_root": str(fixture.project), "work_root": str(fixture.work), "attempt_id": "work-a-1"},
        )
        lease = self.native_attempt_acquire(fixture, "binary-worker")
        action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        submitted = self.transition_result(fixture, action, {"candidate": observed["candidate"]})
        self.assertEqual("committed", submitted["status"], submitted)
        candidate = self.commit_all(fixture.project, "binary candidate")
        self.git(fixture, "switch", "-c", "target", preimage)
        self.git(fixture, "merge", "--squash", candidate)
        self.commit_all(fixture.project, "squash")
        result = self.integration(fixture, "target")
        self.assertEqual("content-present", result["presence"], result)
        self.assertEqual(preimage, self.json_object(result["source"])["compared_from_revision"])

    def test_inspection_keeps_caller_owned_integration_relation(self) -> None:
        fixture = self.checkpoint_fixture()
        snapshot = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        attempt = fixture.store.read_attempt_context(AttemptId("work-a-1"))
        assert snapshot is not None and isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        roots: JsonObject = {"project_root": str(fixture.project), "work_root": str(fixture.work)}
        ready = call_native_tool(
            server.REVIEW_JOB_TOOL,
            {
                **roots,
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
                    "acceptance_evidence": "Reviewed.",
                },
            },
        )
        self.assertEqual("recorded", ready["status"], ready)
        reconciliation: JsonObject = {
            "target_revision": "observed-target",
            "relation": "candidate-integrated",
            "phase": "cleanup",
            "effects": [
                {"effect": "source-checkout", "status": "allowed"},
                {"effect": "shared-work-root", "status": "not-required"},
                {"effect": "git-metadata", "status": "allowed"},
            ],
        }
        with (
            patch(
                "pinboard.adapters.files.root.observe_content_at_target",
                side_effect=AssertionError("unexpected content read"),
            ),
            patch(
                "pinboard.adapters.candidate_evidence.observe_candidate_lineage",
                side_effect=AssertionError("unexpected lineage read"),
            ),
        ):
            result = call_advertised_tool(
                server.ATTEMPT_INSPECT_TOOL, {**roots, "attempt_id": "work-a-1", "reconciliation": reconciliation}
            )
        self.assertEqual("ok", result["status"], result)
        continuation = self.json_object(result["continuation"])
        self.assertEqual("repository-cleanup", self.json_object(continuation["next_operation"])["kind"], result)

    def test_checkpoint_lookup_stays_indexed_as_unrelated_history_grows(self) -> None:
        fixture = self.accepted_package_fixture()
        statements: list[str] = []
        original_open = sqlite_store.open_database

        def traced_open(path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open(path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(sqlite_store, "open_database", traced_open):
            before = fixture.store.read_integration_context(WorkItemId("work-a"))
            count = len([statement for statement in statements if statement.lstrip().upper().startswith("SELECT")])
            statements.clear()
            with closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
                for number in range(20):
                    connection.execute(
                        """INSERT INTO attempts
                           (attempt_id, item_id, state, branch, base_revision, provenance, brief_artifact_ref_id,
                            brief_artifact_kind, result_artifact_ref_id, result_artifact_kind, candidate_revision,
                            candidate_recorded_at, accepted_scope_revision, accepted_scope_digest, subject_revision,
                            recorded_at, updated_at)
                           SELECT ?, item_id, 'done', branch, base_revision, provenance, brief_artifact_ref_id,
                                  brief_artifact_kind, result_artifact_ref_id, result_artifact_kind, candidate_revision,
                                  candidate_recorded_at, accepted_scope_revision, accepted_scope_digest, subject_revision,
                                  recorded_at, updated_at FROM attempts WHERE attempt_id = 'work-a-1'""",
                        (f"unrelated-attempt-{number}",),
                    )
                    connection.execute(
                        """INSERT INTO artifact_refs
                           (artifact_key, artifact_revision, kind, relative_path, content_sha256, size_bytes,
                            accepted_revision, created_at)
                           SELECT ?, artifact_revision, kind, ?, content_sha256, size_bytes, accepted_revision,
                                  created_at FROM artifact_refs WHERE artifact_ref_id = ?""",
                        (
                            f"unrelated-package-{number}",
                            f"artifacts/evidence/unrelated-{number}.json",
                            int(fixture.package_reference.artifact_ref_id),
                        ),
                    )
                    connection.execute(
                        """INSERT INTO transition_history
                           (project_revision, action_id, action_kind, subject_id, artifact_ref_id, artifact_kind,
                            authorization_kind, actor_task_id, actor_host_id, input_schema, input_json,
                            outcome_schema, outcome_json, committed_at)
                           SELECT ?, action_id, action_kind, ?, artifact_ref_id, artifact_kind,
                                  authorization_kind, actor_task_id, actor_host_id, input_schema, input_json,
                                  outcome_schema, outcome_json, committed_at
                           FROM transition_history WHERE outcome_schema = 'checkpoint-acceptance/v2' LIMIT 1""",
                        (1000 + number, f"unrelated-{number}"),
                    )
            after = fixture.store.read_integration_context(WorkItemId("work-a"))
        selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
        self.assertEqual(before, after)
        self.assertEqual(count, len(selects))
        checkpoint_query = next(value for value in selects if "checkpoint-acceptance/v2" in value)
        with closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            for statement in selects:
                plan = " ".join(str(row[3]).upper() for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}"))
                self.assertNotIn("SCAN ", plan, (statement, plan))
                if statement == checkpoint_query:
                    self.assertIn("CHECKPOINT_HISTORY_BY_SUBJECT", plan)
