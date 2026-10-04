"""Real Git content checks through the schema-validating native item-status leaf."""

import contextlib
import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Literal, override
from unittest.mock import patch

from pinboard.adapters.files import root
from pinboard.adapters.files.errors import RootError, RootErrorCode
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import checkpoint_compatibility_models, item_integration, query_models, work_brief_models
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import common, execution, server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool
from tests.support import JsonObject


class ItemIntegrationTest(CheckpointPackageSupport):
    @override
    def setUp(self) -> None:
        environment = patch.dict(
            os.environ,
            {
                "GIT_AUTHOR_DATE": "2001-02-03T04:05:06+0000",
                "GIT_COMMITTER_DATE": "2001-02-03T04:05:06+0000",
                "GIT_AUTHOR_NAME": "Integration Test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Integration Test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def git(self, project: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments], cwd=project, check=True, capture_output=True, text=True
        ).stdout.strip()

    @override
    def commit_all(self, project: Path, message: str) -> str:
        if message == "base":
            (project / "tracked.txt").write_text("base\n" + "unchanged\n" * 15 + "tail\n")
        return super().commit_all(project, message)

    def fixture(self, form: Literal["working-tree", "current-head"]) -> CheckpointFixture:
        fixture = self.checkpoint_fixture(candidate_form=form)
        self.addCleanup(shutil.rmtree, fixture.project)
        # Submission through the production boundary replaces the prebuilt review fixture.
        self.change(fixture, "return-for-correction", {"reason": "Exercise real submission."})
        if form == "working-tree":
            (fixture.project / "tracked.txt").write_text("candidate\n" + "unchanged\n" * 15 + "tail\n")
        lease = self.native_attempt_acquire(fixture, "integration-worker")
        candidate = (
            root.read_working_tree_candidate(fixture.project).identity
            if form == "working-tree"
            else self.git(fixture.project, "rev-parse", "HEAD")
        )
        action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        submitted = self.transition_result(fixture, action, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)
        return replace(fixture, candidate_revision=candidate)

    def change(self, fixture: CheckpointFixture, kind: str, payload: JsonObject) -> JsonObject:
        subject = "work-a" if kind == "resume" else "work-a-1"
        result = self.transition_result(fixture, self.native_actions(fixture, kind, subject), payload)
        self.assertEqual("committed", result["status"], result)
        return result

    def integration(self, fixture: CheckpointFixture, target: str, item: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "operation": "integration",
                    "item_id": item,
                    "target": target,
                }
            },
        )

    def assert_presence(self, fixture: CheckpointFixture, target: str, presence: str, source: str) -> JsonObject:
        result = self.integration(fixture, target)
        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual(presence, result["presence"], result)
        self.assertEqual(target, result["target"])
        self.assertEqual(self.git(fixture.project, "rev-parse", f"{target}^{{commit}}"), result["target_revision"])
        selected = self.json_object(result["source"])
        self.assertEqual(source, selected["kind"])
        self.assertEqual("work-a-1", selected["attempt_id"])
        self.assertEqual(fixture.candidate_revision, selected["candidate_revision"])
        self.assertEqual(fixture.brief.base_revision, selected["compared_from_revision"])
        return result

    def assert_rejection(self, result: JsonObject, code: str, retry: str) -> None:
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual(code, result["code"], result)
        self.assertEqual("unchanged", result["effect"])
        self.assertEqual(retry, result["retry"])
        self.assertFalse(result["state_changed"])
        self.assertEqual([], result["changed_surfaces"])
        if code.startswith(("INTEGRATION_", "PROJECT_GIT_")):
            self.assertTrue(result["observed"])
            self.assertTrue(result["recovery"])

    def test_commit_sources_recognize_fast_forward_merge_and_rebase(self) -> None:
        for method in ("fast-forward", "merge", "rebase"):
            with self.subTest(method=method):
                fixture = self.fixture("current-head")
                project = fixture.project
                self.git(project, "branch", "target", fixture.brief.base_revision)
                self.assert_presence(fixture, "target", "content-not-present", "protected-review")
                self.git(project, "switch", "target")
                if method != "fast-forward":
                    (project / "other.txt").write_text("target-only\n")
                    self.commit_all(project, "target-only")
                if method == "rebase":
                    self.git(project, "switch", "-c", "rebased", fixture.candidate_revision)
                    self.git(project, "rebase", "target")
                    self.git(project, "switch", "target")
                    self.git(project, "merge", "--ff-only", "rebased")
                    ancestor = subprocess.run(
                        ["git", "merge-base", "--is-ancestor", fixture.candidate_revision, "target"],
                        cwd=project,
                        check=False,
                        capture_output=True,
                    )
                    self.assertEqual(1, ancestor.returncode)
                else:
                    self.git(project, "merge", "--no-ff" if method == "merge" else "--ff-only", fixture.brief.branch)
                self.assert_presence(fixture, "target", "content-present", "protected-review")
                self.git(project, "update-ref", "refs/remotes/origin/main", "HEAD")
                self.assert_presence(fixture, "origin/main", "content-present", "protected-review")

    def test_working_tree_squash_and_later_edits(self) -> None:
        fixture = self.fixture("working-tree")
        project = fixture.project
        self.commit_all(project, "candidate")
        self.git(project, "switch", "-c", "target", fixture.brief.base_revision)
        self.git(project, "merge", "--squash", fixture.brief.branch)
        self.commit_all(project, "squashed")
        self.assert_presence(fixture, "target", "content-present", "protected-review")
        (project / "tracked.txt").write_text("candidate\n" + "unchanged\n" * 15 + "later\n")
        self.commit_all(project, "non-overlapping")
        self.assert_presence(fixture, "target", "content-present", "protected-review")
        (project / "tracked.txt").write_text("overlapping\n")
        self.commit_all(project, "overlapping")
        self.assert_presence(fixture, "target", "content-not-present", "protected-review")

    def test_checkpoint_source_survives_resume_and_rebind_and_new_review_wins(self) -> None:
        fixture = self.fixture("working-tree")
        checkpoint = fixture.brief.checkpoint.checkpoint_id
        self.change(
            fixture,
            "accept-checkpoint",
            {"checkpoint": checkpoint, "candidate": fixture.candidate_revision, "evidence": "Reviewed candidate."},
        )
        self.commit_all(fixture.project, "candidate")
        self.git(fixture.project, "switch", "-c", "target", fixture.brief.base_revision)
        self.git(fixture.project, "merge", "--squash", fixture.brief.branch)
        self.commit_all(fixture.project, "squashed")
        result = self.assert_presence(fixture, "target", "content-present", "accepted-checkpoint")
        self.assertEqual(checkpoint, self.json_object(result["source"])["checkpoint_id"])
        self.git(fixture.project, "switch", fixture.brief.branch)
        closed, stdout, stderr = self.run_cli(
            *fixture.common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "Satisfied prerequisite.",
            "--task-id",
            "review-owner",
            "--host-id",
            "local",
        )
        self.assertEqual(0, closed, stdout + stderr)
        self.change(fixture, "resume", {})
        self.assert_presence(fixture, "target", "content-present", "accepted-checkpoint")
        context = fixture.store.read_attempt_context(AttemptId("work-a-1"))
        assert isinstance(context, query_models.NonterminalAttemptContextFacts)
        self.change(
            fixture,
            "rebind-attempt",
            {
                "branch": fixture.brief.branch,
                "base_revision": fixture.brief.base_revision,
                "brief_artifact_ref_id": int(context.brief_artifact_ref_id),
            },
        )
        self.assert_presence(fixture, "target", "content-present", "accepted-checkpoint")
        (fixture.project / "tracked.txt").write_text("next candidate\n")
        lease = self.native_attempt_acquire(fixture, "next-worker")
        candidate = root.read_working_tree_candidate(fixture.project).identity
        action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        self.assertEqual("committed", self.transition_result(fixture, action, {"candidate": candidate})["status"])
        next_result = self.integration(fixture, "target")
        self.assertEqual("protected-review", self.json_object(next_result["source"])["kind"])
        self.change(fixture, "return-for-correction", {"reason": "Retain checkpoint source."})
        self.assert_presence(fixture, "target", "content-present", "accepted-checkpoint")

    def test_empty_diff_resolves_target_but_skips_comparison(self) -> None:
        fixture = self.fixture("working-tree")
        self.change(fixture, "return-for-correction", {"reason": "Submit empty diff."})
        self.git(fixture.project, "restore", "tracked.txt")
        lease = self.native_attempt_acquire(fixture, "empty-worker")
        candidate = root.read_working_tree_candidate(fixture.project).identity
        action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        self.assertEqual("committed", self.transition_result(fixture, action, {"candidate": candidate})["status"])
        fixture = replace(fixture, candidate_revision=candidate)
        with patch.object(root, "TemporaryDirectory", side_effect=AssertionError("No comparison for empty diff")):
            self.assert_presence(fixture, "HEAD", "no-change", "protected-review")
            self.assert_rejection(
                self.integration(fixture, "missing"), "INTEGRATION_TARGET_UNRESOLVED", "correct-input"
            )

    def test_expected_rejections_are_unchanged_and_actionable(self) -> None:
        fixture = self.fixture("working-tree")
        self.assert_rejection(self.integration(fixture, "missing"), "INTEGRATION_TARGET_UNRESOLVED", "correct-input")
        for target in ("-bad", "", "main\nother"):
            self.assert_rejection(self.integration(fixture, target), "ITEM_STATUS_INVALID", "correct-input")
        self.assert_rejection(self.integration(fixture, "HEAD", "absent"), "ITEM_NOT_FOUND", "correct-input")
        self.assert_rejection(
            self.integration(fixture, "HEAD", "work-b"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"
        )
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        artifact = fixture.work / context.reference.selector
        original = artifact.read_bytes()
        artifact.write_bytes(original + b" ")
        invalid = self.integration(fixture, "HEAD")
        self.assert_rejection(invalid, "INTEGRATION_CANDIDATE_EVIDENCE_INVALID", "do-not-retry")
        self.assertIn("pinboard validate", str(invalid["recovery"]))
        artifact.write_bytes(original)
        self.change(fixture, "return-for-correction", {"reason": "Remove protection."})
        self.assert_rejection(self.integration(fixture, "HEAD"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        with tempfile.TemporaryDirectory() as outside:
            result = call_advertised_tool(
                server.ITEM_STATUS_TOOL,
                {
                    "request": {
                        "project_root": outside,
                        "work_root": str(fixture.work),
                        "operation": "integration",
                        "item_id": "work-a",
                        "target": "HEAD",
                    }
                },
            )
            self.assert_rejection(result, "PROJECT_GIT_ROOT_UNAVAILABLE", "correct-input")

    def test_git_read_is_read_only_with_whitespace_and_split_index_configuration(self) -> None:
        fixture = self.fixture("working-tree")
        project = fixture.project
        (project / "tracked.txt").write_text("candidate with trailing space \n")
        self.git(project, "config", "apply.whitespace", "error")
        self.git(project, "config", "core.splitIndex", "true")
        diff = root.read_working_tree_candidate(project).diff
        self.commit_all(project, "with whitespace")
        revision = self.git(project, "rev-parse", "HEAD")
        before = {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}
        directories = [project / ".git", *(p for p in (project / ".git").rglob("*") if p.is_dir())]
        files = [p for p in (project / ".git").rglob("*") if p.is_file()]
        modes = {p: p.stat().st_mode for p in [*directories, *files]}
        with tempfile.TemporaryDirectory() as temporary:
            try:
                for path in files:
                    path.chmod(0o444)
                for path in reversed(directories):
                    path.chmod(0o555)
                with patch("tempfile.tempdir", temporary):
                    observed = root.read_integration_content(project, "HEAD", diff)
                self.assertEqual(
                    root.IntegrationContentObservation(revision, item_integration.ContentPresence.PRESENT), observed
                )
                self.assertEqual([], list(Path(temporary).iterdir()))
                self.assertEqual(
                    before, {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}
                )
            finally:
                for path, mode in modes.items():
                    path.chmod(mode)
        self.assertIsInstance(root.read_integration_content(project, "absent", diff), root.IntegrationTargetUnresolved)
        missing = root.read_integration_content(project, fixture.brief.base_revision, diff)
        self.assertEqual(
            root.IntegrationContentObservation(
                fixture.brief.base_revision, item_integration.ContentPresence.NOT_PRESENT
            ),
            missing,
        )
        with tempfile.TemporaryDirectory() as outside, self.assertRaises(RootError):
            root.read_integration_content(Path(outside), "HEAD", diff)

    def record_ready(self, fixture: CheckpointFixture) -> None:
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        attempt = fixture.store.read_attempt_context(AttemptId("work-a-1"))
        assert context is not None and isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        evidence = fixture.work / "attempts" / "work-a-1"
        result = call_advertised_tool(
            server.REVIEW_JOB_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "review": {
                    "kind": "record-ready",
                    "attempt_id": "work-a-1",
                    "candidate_revision": fixture.candidate_revision,
                    "candidate_snapshot_sha256": context.reference.content_sha256,
                    "accepted_brief_sha256": attempt.brief_reference.content_sha256,
                    "result_sha256": hashlib.sha256((evidence / "result.md").read_bytes()).hexdigest(),
                    "review_sha256": hashlib.sha256((evidence / "review.md").read_bytes()).hexdigest(),
                    "reviewer_task_id": "independent-reviewer",
                    "verdict": "ready",
                    "acceptance_evidence": "Verified the accepted candidate.",
                },
            },
        )
        self.assertEqual("recorded", result["status"], result)

    def test_completion_source_and_inspection_relations_remain_caller_owned(self) -> None:
        fixture = self.terminalize_brief(self.fixture("current-head"))
        self.record_ready(fixture)
        with patch.object(
            root, "read_integration_content", side_effect=AssertionError("Inspection owns no content read")
        ):
            inspected = call_advertised_tool(
                server.ATTEMPT_INSPECT_TOOL,
                {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "attempt_id": "work-a-1",
                    "reconciliation": {
                        "target_revision": fixture.candidate_revision,
                        "relation": "candidate-integrated",
                        "phase": "terminal",
                        "effects": [
                            {"effect": "source-checkout", "status": "not-required"},
                            {"effect": "shared-work-root", "status": "allowed"},
                            {"effect": "git-metadata", "status": "not-required"},
                        ],
                    },
                },
            )
            operation = self.json_object(self.json_object(inspected["continuation"])["next_operation"])
            self.assertEqual("action", operation["kind"], inspected)
            self.assertEqual("complete", self.json_object(operation["action"])["action_kind"])
        evidence = fixture.work / "attempts" / "work-a-1"
        self.change(
            fixture,
            "complete",
            {
                "schema": "pinboard-reviewed-completion/v2",
                "candidate": fixture.candidate_revision,
                "evidence": "Accepted on the named target.",
                "reviewer_task_id": "independent-reviewer",
                "result_sha256": hashlib.sha256((evidence / "result.md").read_bytes()).hexdigest(),
                "review_sha256": hashlib.sha256((evidence / "review.md").read_bytes()).hexdigest(),
                "packages": [],
            },
        )
        self.assert_presence(fixture, "HEAD", "content-present", "completion")

    def test_continue_and_direct_close_do_not_supply_a_candidate(self) -> None:
        fixture = self.fixture("working-tree")
        self.record_ready(fixture)
        self.change(
            fixture,
            "accept-review-and-continue",
            {"candidate": fixture.candidate_revision, "evidence": "Continue without checkpoint acceptance."},
        )
        self.assert_rejection(self.integration(fixture, "HEAD"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
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
        self.assertEqual(0, closed, stdout + stderr)
        result = self.integration(fixture, "HEAD", "work-c")
        self.assert_rejection(result, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.assertIn("closed directly", str(result["message"]))

    def test_damaged_checkpoint_receipt_retains_diagnosis(self) -> None:
        fixture = self.fixture("working-tree")
        accepted = self.change(
            fixture,
            "accept-checkpoint",
            {
                "checkpoint": fixture.brief.checkpoint.checkpoint_id,
                "candidate": fixture.candidate_revision,
                "evidence": "Accepted checkpoint.",
            },
        )
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE transition_history SET outcome_json = '{}' WHERE history_id = ?", (accepted["history_id"],)
            )
        result = self.integration(fixture, "HEAD")
        self.assert_rejection(result, "TRANSITION_RECEIPT_DAMAGED", "do-not-retry")
        self.assertIn(str(accepted["history_id"]), str(result["recovery"]))

    def test_binary_and_rename_diff_and_actual_working_tree_preimage(self) -> None:
        fixture = self.fixture("working-tree")
        self.change(fixture, "return-for-correction", {"reason": "Exercise a later preimage."})
        self.git(fixture.project, "restore", "tracked.txt")
        (fixture.project / "binary.bin").write_bytes(bytes(range(256)))
        preimage = self.commit_all(fixture.project, "intervening commit")
        self.git(fixture.project, "mv", "tracked.txt", "renamed.txt")
        (fixture.project / "binary.bin").write_bytes(bytes(range(255, -1, -1)))
        observed = root.read_working_tree_candidate(fixture.project)
        lease = self.native_attempt_acquire(fixture, "binary-worker")
        action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        self.assertEqual(
            "committed", self.transition_result(fixture, action, {"candidate": observed.identity})["status"]
        )
        self.commit_all(fixture.project, "rename and binary")
        result = self.integration(fixture, "HEAD")
        self.assertEqual("content-present", result["presence"], result)
        self.assertEqual(preimage, self.json_object(result["source"])["compared_from_revision"])
        self.assertNotEqual(fixture.brief.base_revision, preimage)

    def test_native_read_leaves_all_repository_and_board_bytes_unchanged(self) -> None:
        fixture = self.fixture("current-head")
        before = {p.relative_to(fixture.project): p.read_bytes() for p in fixture.project.rglob("*") if p.is_file()}
        self.assert_presence(fixture, "HEAD", "content-present", "protected-review")
        after = {p.relative_to(fixture.project): p.read_bytes() for p in fixture.project.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_focus_ignores_unrelated_retained_evidence_and_uses_indexed_lookups(self) -> None:
        fixture = self.fixture("working-tree")
        self.change(
            fixture,
            "accept-checkpoint",
            {
                "checkpoint": fixture.brief.checkpoint.checkpoint_id,
                "candidate": fixture.candidate_revision,
                "evidence": "Accepted checkpoint.",
            },
        )
        before = self.integration(fixture, fixture.brief.base_revision)
        database = fixture.work / "state.sqlite3"
        with contextlib.closing(sqlite3.connect(database)) as connection, connection:
            for number in range(64):
                identity = f"unrelated-{number}"
                connection.execute(
                    """INSERT INTO attempts
                    SELECT ?, item_id, 'done', branch, base_revision, provenance, brief_artifact_ref_id,
                           brief_artifact_kind, result_artifact_ref_id, result_artifact_kind, NULL, NULL,
                           accepted_scope_revision, accepted_scope_digest, subject_revision, recorded_at, updated_at
                    FROM attempts WHERE attempt_id = 'work-a-1'""",
                    (identity,),
                )
                reference = 10000 + number
                connection.execute(
                    """INSERT INTO artifact_refs
                    SELECT ?, ?, 1, 'evidence', ?, content_sha256, size_bytes, accepted_revision, created_at
                    FROM artifact_refs LIMIT 1""",
                    (reference, identity, f"artifacts/{identity}.json"),
                )
                (fixture.work / "artifacts" / f"{identity}.json").write_text("unrelated damaged package")
                connection.execute(
                    """INSERT INTO transition_history
                    SELECT ?, ?, ?, 'accept-checkpoint', ?, ?, 'evidence', 'project', actor_task_id,
                           actor_host_id, input_schema, '{}', 'checkpoint-acceptance/v2', '{}', committed_at
                    FROM transition_history LIMIT 1""",
                    (reference, reference, f"accept-checkpoint:{identity}", identity, reference),
                )
        statements: list[str] = []
        original = sqlite_store.open_database

        def trace(path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original(path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(sqlite_store, "open_database", trace):
            after = self.integration(fixture, fixture.brief.base_revision)
        self.assertEqual(before, after)
        self.assertFalse(any("unrelated" in query for query in statements))
        selects = [query for query in statements if query.lstrip().upper().startswith("SELECT")]
        self.assertTrue(any("checkpoint_history_by_subject" in query for query in selects))
        with contextlib.closing(sqlite3.connect(database)) as connection:
            for query in selects:
                plans = [str(row[3]).upper() for row in connection.execute(f"EXPLAIN QUERY PLAN {query}")]
                self.assertTrue(any("SEARCH " in plan for plan in plans), (query, plans))
                self.assertFalse(any("SCAN " in plan for plan in plans), (query, plans))

    def test_other_reads_do_not_select_integration_facts_and_item_override_is_used(self) -> None:
        fixture = self.fixture("current-head")
        with patch.object(SQLiteWorkStore, "read_item_integration", side_effect=AssertionError("Integration leaked")):
            for operation in (
                {"operation": "item", "item_id": "work-a"},
                {"operation": "branch", "branch": fixture.brief.branch},
            ):
                result = call_advertised_tool(
                    server.ITEM_STATUS_TOOL,
                    {
                        "request": {
                            "project_root": str(fixture.project),
                            "work_root": str(fixture.work),
                            **operation,
                        }
                    },
                )
                self.assertNotIn("code", result)
            self.native_actions(fixture, "return-for-correction", "work-a-1")
            overview = call_advertised_tool(
                server.OVERVIEW_TOOL, {"project_root": str(fixture.project), "work_root": str(fixture.work)}
            )
            self.assertEqual("pinboard-overview/v6", overview["schema"])
        (fixture.work / "contributor-traces.config").write_text(
            '[pinboard "unsafe_persist_exact_pinboard_traces"]\nmode = off\n[item "work-a"]\nmode = on\n'
        )
        create = partial(server.create_server, capture=execution.AutomaticCapture(common.select_capture_item))
        with patch.object(server, "create_server", create):
            self.assert_presence(fixture, "HEAD", "content-present", "protected-review")
        traces = list((fixture.work / "invocation-traces").glob("*.json"))
        self.assertEqual(1, len(traces))
        self.assertIn('"integration"', traces[0].read_text())

    def test_retained_pre_snapshot_review_and_checkpoint_are_unavailable(self) -> None:
        fixture = self.fixture("current-head")
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "DELETE FROM artifact_refs WHERE artifact_ref_id = ?", (int(context.reference.artifact_ref_id),)
            )
            connection.execute(
                """UPDATE transition_history SET artifact_ref_id = NULL, artifact_kind = NULL,
                input_schema = 'decision/v1', input_json = '{}' WHERE history_id = ?""",
                (int(context.receipt.history_id),),
            )
        result = self.integration(fixture, "HEAD")
        self.assert_rejection(result, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.assertIn("pre-snapshot", str(result["message"]))
        checkpoint = self.accepted_package_fixture(local=True)
        self.addCleanup(shutil.rmtree, checkpoint.project)
        package = self.package(checkpoint)
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
        self.replace_package(checkpoint, legacy)
        result = self.integration(checkpoint, "HEAD")
        self.assert_rejection(result, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.assertIn("no candidate snapshot reference", str(result["message"]))

    def test_git_effect_failure_preserves_adapter_diagnostic_and_recovery(self) -> None:
        fixture = self.fixture("current-head")
        with patch.object(
            root,
            "read_integration_content",
            side_effect=RootError(RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE, "Cannot read the target tree."),
        ):
            result = self.integration(fixture, "HEAD")
        self.assert_rejection(result, "PROJECT_GIT_CHECKOUT_UNAVAILABLE", "correct-input")
        self.assertIn("Cannot read the target tree", str(result["message"]))
        self.assertIn(str(fixture.project), str(result["observed"]))
        self.assertIn("Correct", str(result["recovery"]))
        with self.assertRaises(RootError):
            root.read_integration_content(fixture.project, "HEAD", b"not a patch")
