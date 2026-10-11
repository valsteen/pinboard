"""Content observations through native MCP and real Git repositories and transitions."""

import contextlib
import hashlib
import os
import shutil
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Literal, override
from unittest.mock import patch

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters.candidate_evidence import read_candidate_evidence_from_context
from pinboard.adapters.files import root
from pinboard.adapters.files.errors import RootError
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import checkpoint_compatibility_models, integration, query_models, work_briefs
from pinboard.application.candidate_snapshots import CandidateSnapshotEvidence
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import common, execution, server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import JsonObject

COMMIT_ENV = {"GIT_AUTHOR_DATE": "2030-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2030-01-01T00:00:00+00:00"}


class ItemIntegrationTest(CheckpointPackageSupport):
    @override
    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, COMMIT_ENV)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def git(self, project: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def leaf(self, fixture: CheckpointFixture, target: str, item: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": item, "target": target}},
        )

    def transition(self, fixture: CheckpointFixture, action: str, payload: JsonObject) -> JsonObject:
        result = self.transition_result(fixture, self.project_action(fixture, action), payload)
        self.assertIn(result["status"], ("committed", "committed-with-warning"), result)
        return result

    def submitted(
        self,
        form: Literal["working-tree", "current-head"],
        *,
        empty: bool = False,
        context: bool = False,
        binary: bool = False,
    ) -> CheckpointFixture:
        fixture = self.checkpoint_fixture(local=True, committed_context=context)
        self.addCleanup(shutil.rmtree, fixture.project)
        self.transition(fixture, "return-for-correction:work-a-1", {"reason": "Prepare a fresh native submission."})
        if context:
            (fixture.project / "tracked.txt").write_text("base\n" + "context\n" * 30)
            self.commit_all(fixture.project, "working-tree preimage")
            (fixture.project / "tracked.txt").write_text("candidate\n" + "context\n" * 30)
        if empty:
            (fixture.project / "tracked.txt").write_text("base\n")
        if binary:
            self.git(fixture.project, "mv", "tracked.txt", "renamed.txt")
            (fixture.project / "renamed.txt").write_text("base\n")
            (fixture.project / "binary.bin").write_bytes(bytes(range(256)))
            (fixture.project / "whitespace.txt").write_text("trailing  \n")
            self.git(fixture.project, "add", "binary.bin", "whitespace.txt")
        if form == "current-head":
            candidate = self.commit_all(fixture.project, "reviewed candidate")
        else:
            observed = call_native_tool(
                server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"}
            )
            candidate = observed["candidate"]
            assert isinstance(candidate, str)
        lease = self.native_attempt_acquire(fixture, "integration-worker")
        action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        result = self.transition_result(fixture, action, {"candidate": candidate})
        self.assertEqual("committed", result["status"], result)
        snapshot = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert snapshot is not None

        evidence = read_candidate_evidence_from_context(fixture.work, snapshot, candidate)
        assert isinstance(evidence, CandidateSnapshotEvidence)
        return replace(fixture, candidate_revision=candidate, candidate_bytes=evidence.snapshot.diff)

    def assert_presence(
        self, fixture: CheckpointFixture, target: str, presence: str, kind: str = "protected-review"
    ) -> JsonObject:
        result = self.leaf(fixture, target)
        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual(presence, result["presence"], result)
        self.assertEqual(target, result["target"])
        self.assertEqual(self.git(fixture.project, "rev-parse", f"{target}^{{commit}}"), result["target_revision"])
        source = self.json_object(result["source"])
        self.assertEqual(kind, source["kind"])
        self.assertEqual("work-a-1", source["attempt_id"])
        self.assertEqual(fixture.candidate_revision, source["candidate_revision"])
        return source

    def assert_rejection(self, result: JsonObject, code: str, retry: str) -> list[str]:
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual(code, result["code"], result)
        self.assertEqual("unchanged", result["effect"])
        self.assertIs(False, result["state_changed"])
        self.assertEqual([], result["changed_surfaces"])
        self.assertEqual(retry, result["retry"])
        if code not in ("ITEM_NOT_FOUND", "ITEM_STATUS_INVALID"):
            self.assertTrue(result["recovery"])
            self.assertTrue(result["observed"])
        return [str(self.json_object(value)["field"]) for value in self.json_array(result["observed"])]

    def integrate(self, fixture: CheckpointFixture, strategy: str) -> str:
        if fixture.candidate_revision.startswith("working-tree"):
            self.commit_all(fixture.project, "commit reviewed work")
        candidate = self.git(fixture.project, "rev-parse", "HEAD")
        self.git(fixture.project, "switch", "-c", "target", fixture.brief.base_revision)
        if strategy in ("rebase", "squash"):
            (fixture.project / "unrelated.txt").write_text("target context\n")
            self.commit_all(fixture.project, "target change")
        if strategy == "rebase":
            self.git(fixture.project, "cherry-pick", candidate)
            self.assertNotEqual(candidate, self.git(fixture.project, "rev-parse", "HEAD"))
            ancestor = subprocess.run(
                ["git", "merge-base", "--is-ancestor", candidate, "HEAD"], cwd=fixture.project, check=False
            )
            self.assertEqual(1, ancestor.returncode)
        elif strategy == "squash":
            self.git(fixture.project, "merge", "--squash", candidate)
            self.commit_all(fixture.project, "squash reviewed work")
        elif strategy == "merge":
            self.git(fixture.project, "merge", "--no-ff", candidate, "-m", "integrate reviewed work")
        elif strategy == "fast-forward":
            self.git(fixture.project, "merge", "--ff-only", candidate)
        else:
            self.fail(strategy)
        return candidate

    def test_commit_candidates_recognize_fast_forward_merge_and_rebase(self) -> None:
        for strategy in ("fast-forward", "merge", "rebase"):
            with self.subTest(strategy=strategy):
                fixture = self.submitted("current-head")
                self.assert_presence(fixture, fixture.brief.base_revision, "content-not-present")
                self.integrate(fixture, strategy)
                source = self.assert_presence(fixture, "target", "content-present")
                self.assertEqual(fixture.brief.base_revision, source["compared_from_revision"])
                self.git(fixture.project, "update-ref", "refs/remotes/origin/target", "HEAD")
                self.assert_presence(fixture, "origin/target", "content-present")

    def test_working_tree_squash_and_later_edits_use_recorded_preimage(self) -> None:
        fixture = self.submitted("working-tree", context=True)
        preimage = self.git(fixture.project, "rev-parse", "HEAD")
        self.integrate(fixture, "squash")
        source = self.assert_presence(fixture, "target", "content-present")
        self.assertEqual(preimage, source["compared_from_revision"])
        (fixture.project / "tracked.txt").write_text("candidate\n" + "context\n" * 20 + "later edit\n")
        self.commit_all(fixture.project, "later non-overlapping edit")
        self.assert_presence(fixture, "target", "content-present")
        (fixture.project / "tracked.txt").write_text("changed reviewed line\n" + "context\n" * 20 + "later edit\n")
        self.commit_all(fixture.project, "later overlapping edit")
        self.assert_presence(fixture, "target", "content-not-present")

    def test_empty_diff_resolves_target_but_never_builds_temporary_index(self) -> None:
        fixture = self.submitted("working-tree", empty=True)
        with patch(
            "pinboard.adapters.files.root.TemporaryDirectory", side_effect=AssertionError("comparison forbidden")
        ):
            self.assert_presence(fixture, "HEAD", "no-change")
            self.assert_rejection(self.leaf(fixture, "absent"), "INTEGRATION_TARGET_UNRESOLVED", "correct-input")

    def test_checkpoint_survives_resume_rebind_and_new_review_takes_precedence(self) -> None:
        fixture = self.submitted("working-tree")
        self.transition(
            fixture,
            "accept-checkpoint:work-a-1",
            {
                "checkpoint": fixture.brief.checkpoint.checkpoint_id,
                "candidate": fixture.candidate_revision,
                "evidence": "Accepted candidate.",
            },
        )
        self.integrate(fixture, "squash")
        source = self.assert_presence(fixture, "target", "content-present", "accepted-checkpoint")
        self.assertEqual(fixture.brief.checkpoint.checkpoint_id, source["checkpoint_id"])
        self.run_json_cli(
            *fixture.common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "Prerequisite complete.",
            "--task-id",
            "human",
            "--host-id",
            "local",
        )
        self.transition(fixture, "resume:work-a", {})
        self.assert_presence(fixture, "target", "content-present", "accepted-checkpoint")
        self.git(fixture.project, "switch", fixture.brief.branch)
        brief = replace_struct(fixture.brief, artifact_revision=2)
        published = call_native_tool(
            server.BRIEF_PUBLISH_TOOL, {**self.roots(fixture), "brief": self.json_object(msgspec.to_builtins(brief))}
        )
        reference = self.json_object(published["reference"])
        self.transition(
            fixture,
            "rebind-attempt:work-a-1",
            {
                "branch": brief.branch,
                "base_revision": brief.base_revision,
                "brief_artifact_ref_id": reference["artifact_ref_id"],
            },
        )
        self.assert_presence(fixture, "target", "content-present", "accepted-checkpoint")
        (fixture.project / "tracked.txt").write_text("new candidate\n")
        lease = self.native_attempt_acquire(fixture, "next-worker")
        candidate = call_native_tool(server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"})[
            "candidate"
        ]
        assert isinstance(candidate, str)
        self.transition_result(
            fixture,
            self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease),
            {"candidate": candidate},
        )
        current = replace(fixture, candidate_revision=candidate)
        self.assert_presence(current, "target", "content-not-present")
        self.transition(fixture, "return-for-correction:work-a-1", {"reason": "Restore the checkpoint source."})
        self.assert_presence(fixture, "target", "content-present", "accepted-checkpoint")

    def test_completion_and_damaged_closing_receipt(self) -> None:
        fixture = self.terminalize_brief(self.submitted("current-head"))
        attempt_root = fixture.work / "attempts" / "work-a-1"
        completed = self.transition(
            fixture,
            "complete:work-a-1",
            {
                "schema": "pinboard-reviewed-completion/v2",
                "candidate": fixture.candidate_revision,
                "evidence": "Accepted locally.",
                "reviewer_task_id": "independent-reviewer",
                "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
                "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
                "packages": [],
            },
        )
        self.integrate(fixture, "fast-forward")
        self.assert_presence(fixture, "target", "content-present", "completion")
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            connection.execute(
                "UPDATE transition_history SET outcome_json = '{}' WHERE history_id = ?", (completed["history_id"],)
            )
            connection.commit()
        self.assert_rejection(self.leaf(fixture, "target"), "TRANSITION_RECEIPT_DAMAGED", "do-not-retry")

    def test_unavailable_sources_unknown_item_target_and_invalid_evidence(self) -> None:
        fixture = self.submitted("working-tree")
        fields = self.assert_rejection(self.leaf(fixture, "missing"), "INTEGRATION_TARGET_UNRESOLVED", "correct-input")
        self.assertIn("target", fields)
        self.assertIn("project_root", fields)
        self.assert_rejection(self.leaf(fixture, "-HEAD"), "ITEM_STATUS_INVALID", "correct-input")
        self.assert_rejection(self.leaf(fixture, "HEAD", "unknown"), "ITEM_NOT_FOUND", "correct-input")
        self.assert_rejection(
            self.leaf(fixture, "HEAD", "work-c"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"
        )
        snapshot = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert snapshot is not None
        selected = fixture.work / snapshot.reference.selector
        original = selected.read_bytes()
        selected.write_bytes(original + b" ")
        fields = self.assert_rejection(
            self.leaf(fixture, "HEAD"), "INTEGRATION_CANDIDATE_EVIDENCE_INVALID", "do-not-retry"
        )
        self.assertIn("attempt_id", fields)
        self.assertIn("accepted_reference", fields)
        selected.write_bytes(original)
        self.transition(fixture, "return-for-correction:work-a-1", {"reason": "No checkpoint exists."})
        self.assert_rejection(self.leaf(fixture, "HEAD"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.transition(fixture, "pause:work-a-1", {"reason": "Ordinary pause is not acceptance."})
        self.assert_rejection(self.leaf(fixture, "HEAD"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.run_json_cli(
            *fixture.common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "Direct close.",
            "--task-id",
            "human",
            "--host-id",
            "local",
        )
        self.assert_rejection(
            self.leaf(fixture, "HEAD", "work-c"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input"
        )

    def test_continued_candidate_is_not_an_integration_source(self) -> None:
        fixture = self.submitted("working-tree")
        self.transition(
            fixture,
            "accept-review-and-continue:work-a-1",
            {"candidate": fixture.candidate_revision, "evidence": "Continue work."},
        )
        self.assert_rejection(self.leaf(fixture, "HEAD"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")

    def test_git_adapter_is_read_only_with_binary_rename_whitespace_and_split_index(self) -> None:
        fixture = self.submitted("current-head", binary=True)
        diff = fixture.candidate_bytes
        self.git(fixture.project, "config", "apply.whitespace", "error")
        self.git(fixture.project, "config", "core.splitIndex", "true")
        self.git(fixture.project, "update-index", "--split-index")
        before = {
            path.relative_to(fixture.project): path.read_bytes()
            for path in fixture.project.rglob("*")
            if path.is_file()
        }
        modes = {path: path.stat().st_mode for path in (fixture.project / ".git").rglob("*")}
        modes[fixture.project / ".git"] = (fixture.project / ".git").stat().st_mode
        for path, mode in modes.items():
            path.chmod(mode & ~0o222)
        scratch = fixture.project.parent / f"{fixture.project.name}-temporary"
        scratch.mkdir()
        self.addCleanup(shutil.rmtree, scratch)
        try:
            with patch("tempfile.tempdir", str(scratch)):
                self.assert_presence(fixture, "HEAD", "content-present")
                observed = root.read_integration_content(fixture.project, "HEAD", diff)
                self.assertIsInstance(observed, root.IntegrationObservation)
                assert isinstance(observed, root.IntegrationObservation)
                self.assertEqual(integration.Presence.CONTENT_PRESENT, observed.presence)
                absent = root.read_integration_content(fixture.project, fixture.brief.base_revision, diff)
                assert isinstance(absent, root.IntegrationObservation)
                self.assertEqual(integration.Presence.CONTENT_NOT_PRESENT, absent.presence)
                self.assertIsInstance(
                    root.read_integration_content(fixture.project, "unknown", diff), root.IntegrationTargetUnresolved
                )
            self.assertEqual([], list(scratch.iterdir()))
            after = {
                path.relative_to(fixture.project): path.read_bytes()
                for path in fixture.project.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)
        finally:
            for path, mode in modes.items():
                path.chmod(mode)
        with self.assertRaises(RootError):
            root.read_integration_content(scratch, "HEAD", diff)
        result = call_advertised_tool(
            server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "project_root": str(scratch),
                    "work_root": str(fixture.work),
                    "operation": "integration",
                    "item_id": "work-a",
                    "target": "HEAD",
                }
            },
        )
        self.assert_rejection(result, "PROJECT_GIT_ROOT_UNAVAILABLE", "correct-input")

    def test_native_read_preserves_repository_and_uses_item_trace_override(self) -> None:
        fixture = self.submitted("current-head")
        before = {
            path.relative_to(fixture.project): path.read_bytes()
            for path in fixture.project.rglob("*")
            if path.is_file()
        }
        self.assert_presence(fixture, "HEAD", "content-present")
        after = {
            path.relative_to(fixture.project): path.read_bytes()
            for path in fixture.project.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)
        settings = fixture.work / "contributor-traces.config"
        settings.write_text(
            '[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = off\n[item "work-a"]\n\tmode = on\n'
        )
        arguments: JsonObject = {
            "request": {**self.roots(fixture), "operation": "integration", "item_id": "work-a", "target": "HEAD"}
        }
        capture = execution.AutomaticCapture(common.select_capture_item)
        self.assertIsNotNone(capture.resolve(str(fixture.project), arguments))
        settings.write_text(
            '[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = on\n[item "work-a"]\n\tmode = off\n'
        )
        self.assertIsNone(capture.resolve(str(fixture.project), arguments))

    def test_inspection_and_other_status_reads_do_not_compare_integration_content(self) -> None:
        fixture = self.submitted("current-head")
        snapshot = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert snapshot is not None
        attempt = fixture.store.read_attempt_context(AttemptId("work-a-1"))
        assert isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        evidence = fixture.work / "attempts" / "work-a-1"
        recorded = call_native_tool(
            server.REVIEW_JOB_TOOL,
            {
                **self.roots(fixture),
                "review": {
                    "kind": "record-ready",
                    "attempt_id": "work-a-1",
                    "candidate_revision": fixture.candidate_revision,
                    "candidate_snapshot_sha256": snapshot.reference.content_sha256,
                    "accepted_brief_sha256": attempt.brief_reference.content_sha256,
                    "result_sha256": hashlib.sha256((evidence / "result.md").read_bytes()).hexdigest(),
                    "review_sha256": hashlib.sha256((evidence / "review.md").read_bytes()).hexdigest(),
                    "reviewer_task_id": "independent-reviewer",
                    "verdict": "ready",
                    "acceptance_evidence": "Reviewed exact content.",
                },
            },
        )
        self.assertEqual("recorded", recorded["status"], recorded)
        with patch.object(root, "read_integration_content", side_effect=AssertionError("unrequested integration read")):
            item = call_advertised_tool(
                server.ITEM_STATUS_TOOL, {"request": {**self.roots(fixture), "operation": "item", "item_id": "work-a"}}
            )
            self.assertEqual("pinboard-item-status/v2", item["schema"])
            call_advertised_tool(
                server.ITEM_STATUS_TOOL,
                {"request": {**self.roots(fixture), "operation": "branch", "branch": fixture.brief.branch}},
            )
            call_advertised_tool(server.OVERVIEW_TOOL, self.roots(fixture))
            self.native_actions(fixture, "return-for-correction", "work-a-1", role="project")
            inspected = call_advertised_tool(
                server.ATTEMPT_INSPECT_TOOL,
                {
                    **self.roots(fixture),
                    "attempt_id": "work-a-1",
                    "reconciliation": {
                        "target_revision": fixture.brief.base_revision,
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
            self.assertIn("continuation", inspected, inspected)
            operation = self.json_object(self.json_object(inspected["continuation"])["next_operation"])
            self.assertEqual("repository-cleanup", operation["kind"])

    def test_focused_reads_ignore_unrelated_history_and_use_existing_indexes(self) -> None:
        fixture = self.submitted("working-tree")
        self.transition(
            fixture,
            "accept-checkpoint:work-a-1",
            {
                "checkpoint": fixture.brief.checkpoint.checkpoint_id,
                "candidate": fixture.candidate_revision,
                "evidence": "Accepted candidate.",
            },
        )
        database = fixture.work / "state.sqlite3"
        with contextlib.closing(sqlite3.connect(database)) as connection:
            for number in range(30):
                connection.execute(
                    """INSERT INTO attempts
                    (attempt_id, item_id, state, branch, base_revision, provenance, brief_artifact_ref_id,
                    brief_artifact_kind, accepted_scope_revision, accepted_scope_digest, subject_revision,
                    recorded_at, updated_at)
                    SELECT 'unrelated-' || ?, item_id, 'done', branch, base_revision, provenance,
                    brief_artifact_ref_id, brief_artifact_kind, accepted_scope_revision, accepted_scope_digest,
                    subject_revision, recorded_at, updated_at FROM attempts WHERE attempt_id = 'work-a-1'""",
                    (number,),
                )
                connection.execute(
                    """INSERT INTO artifact_refs
                    (artifact_ref_id, artifact_key, artifact_revision, kind, relative_path, content_sha256,
                    size_bytes, accepted_revision, created_at)
                    SELECT 10000 + ?, 'unrelated-package-' || ?, 1, kind, 'missing-unrelated-' || ?,
                    content_sha256, size_bytes, accepted_revision, created_at FROM artifact_refs
                    WHERE artifact_ref_id = (SELECT artifact_ref_id FROM transition_history
                    WHERE outcome_schema = 'checkpoint-acceptance/v2' LIMIT 1)""",
                    (number, number, number),
                )
                connection.execute(
                    """INSERT INTO transition_history
                    (history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id, artifact_kind,
                    authorization_kind, actor_task_id, actor_host_id, input_schema, input_json,
                    outcome_schema, outcome_json, committed_at)
                    SELECT 10000 + ?, 10000 + ?, 'accept-checkpoint:unrelated-' || ?, 'accept-checkpoint',
                    'unrelated-' || ?, 10000 + ?, artifact_kind, authorization_kind, actor_task_id, actor_host_id,
                    input_schema, input_json, 'checkpoint-acceptance/v2', '{}', committed_at
                    FROM transition_history WHERE outcome_schema = 'checkpoint-acceptance/v2' LIMIT 1""",
                    (number, number, number, number, number),
                )
            connection.commit()
        statements: list[str] = []
        real_open = sqlite_store.open_database

        def traced_open(path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = real_open(path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with (
            patch.object(sqlite_store, "open_database", side_effect=traced_open),
            patch.object(SQLiteWorkStore, "read_item_status", side_effect=AssertionError("review walk forbidden")),
        ):
            self.assert_presence(fixture, fixture.brief.base_revision, "content-not-present", "accepted-checkpoint")
        with contextlib.closing(sqlite3.connect(database)) as connection:
            plans = [
                str(row[3])
                for statement in statements
                if statement.lstrip().upper().startswith("SELECT")
                and any(
                    f"FROM {table}" in statement
                    for table in ("work_items", "attempts", "transition_history", "artifact_refs")
                )
                for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}")
            ]
        self.assertTrue(plans)
        self.assertTrue(any("checkpoint_history_by_subject" in plan for plan in plans), plans)
        self.assertFalse(any("SCAN" in plan.upper() for plan in plans), plans)
        self.assertFalse(any("unrelated-" in statement for statement in statements))

    def test_damaged_checkpoint_and_retained_candidates_have_typed_diagnoses(self) -> None:
        fixture = self.submitted("working-tree")
        accepted = self.transition(
            fixture,
            "accept-checkpoint:work-a-1",
            {
                "checkpoint": fixture.brief.checkpoint.checkpoint_id,
                "candidate": fixture.candidate_revision,
                "evidence": "Accepted candidate.",
            },
        )
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            connection.execute(
                "UPDATE transition_history SET outcome_json = '{}' WHERE history_id = ?", (accepted["history_id"],)
            )
            connection.commit()
        self.assert_rejection(self.leaf(fixture, "HEAD"), "TRANSITION_RECEIPT_DAMAGED", "do-not-retry")
        retained = self.checkpoint_fixture(local=True)
        self.addCleanup(shutil.rmtree, retained.project)
        with contextlib.closing(sqlite3.connect(retained.work / "state.sqlite3")) as connection:
            connection.execute(
                "UPDATE attempts SET candidate_recorded_at = '2000-01-01T00:00:00+00:00' WHERE attempt_id = 'work-a-1'"
            )
            connection.commit()
        self.assert_rejection(self.leaf(retained, "HEAD"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        package_fixture = self.accepted_package_fixture(local=True)
        self.addCleanup(shutil.rmtree, package_fixture.project)
        package = self.package(package_fixture)
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
        self.replace_artifact_bytes(
            package_fixture,
            package_fixture.package_reference,
            work_briefs.canonical_checkpoint_review_package_bytes(legacy),
        )
        self.assert_rejection(self.leaf(package_fixture, "HEAD"), "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
