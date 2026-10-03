"""Native integration leaf: exact source selection, Git content, rejection, and focused reads."""

import asyncio
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import override
from unittest.mock import patch

from mcp import Client
from mcp.server.mcpserver.exceptions import UnexpectedToolError

from pinboard.adapters.files import root
from pinboard.adapters.files.errors import RootError, RootErrorCode
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.database import OpenMode
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import query_models, work_brief_models
from pinboard.domain import history
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import common, execution, server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import JsonObject


class ItemIntegrationTest(CheckpointPackageSupport):
    @override
    def setUp(self) -> None:
        self.addCleanup(patch.stopall)
        patch.dict(
            os.environ,
            {
                "GIT_AUTHOR_DATE": "2030-01-01T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2030-01-01T00:00:00+00:00",
            },
        ).start()
        for owner in ("tests.checkpoint_support", "pinboard.mcp.mutation_operations", "pinboard.mcp.read_operations"):
            clock = patch(f"{owner}.datetime", wraps=datetime).start()
            clock.now.return_value = datetime(2030, 1, 2, tzinfo=UTC)

    def git(self, fixture: CheckpointFixture, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=Integration tests", "-c", "user.email=integration@example.invalid", *arguments],
            cwd=fixture.project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def integration(self, fixture: CheckpointFixture, target: str, item: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": item, "target": target}},
        )

    def transition(self, fixture: CheckpointFixture, kind: str, subject: str, payload: JsonObject) -> JsonObject:
        result = self.transition_result(fixture, self.native_actions(fixture, kind, subject), payload)
        self.assertEqual("committed", result["status"], result)
        return result

    def submit(self, fixture: CheckpointFixture, commit: bool) -> CheckpointFixture:
        if commit:
            candidate = self.commit_all(fixture.project, "reviewed change")
        else:
            candidate = call_native_tool(
                server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"}
            )["candidate"]
            assert isinstance(candidate, str)
        lease = self.native_attempt_acquire(fixture, "integration-worker")
        action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        result = self.transition_result(fixture, action, {"candidate": candidate})
        self.assertEqual("committed", result["status"], result)
        return replace(fixture, candidate_revision=candidate)

    def prepared(self, commit: bool = False) -> CheckpointFixture:
        fixture = self.checkpoint_fixture(local=True)
        self.transition(fixture, "return-for-correction", "work-a-1", {"reason": "Prepare a fresh reviewed change."})
        (fixture.project / "tracked.txt").write_text("reviewed\n")
        return self.submit(fixture, commit)

    def assert_source(
        self, result: JsonObject, fixture: CheckpointFixture, kind: str, target: str, presence: str
    ) -> None:
        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual("work-a", result["item_id"])
        self.assertEqual(target, result["target"])
        self.assertEqual(self.git(fixture, "rev-parse", f"{target}^{{commit}}"), result["target_revision"])
        self.assertEqual(presence, result["presence"])
        source = self.json_object(result["source"])
        self.assertEqual(kind, source["kind"])
        self.assertEqual("work-a-1", source["attempt_id"])
        self.assertEqual(fixture.candidate_revision, source["candidate_revision"])
        self.assertEqual(fixture.brief.base_revision, source["compared_from_revision"])

    def test_commit_candidate_fast_forward_merge_and_rebase(self) -> None:
        for mode in ("fast-forward", "merge", "rebase"):
            with self.subTest(mode=mode):
                fixture = self.prepared(commit=True)
                self.git(fixture, "switch", "-c", "target", fixture.brief.base_revision)
                if mode in ("merge", "rebase"):
                    (fixture.project / "unrelated.txt").write_text("target work\n")
                    self.commit_all(fixture.project, "target work")
                if mode == "rebase":
                    self.git(fixture, "switch", fixture.brief.branch)
                    self.git(fixture, "rebase", "target")
                    self.git(fixture, "switch", "target")
                    self.git(fixture, "merge", "--ff-only", fixture.brief.branch)
                    ancestor = subprocess.run(
                        ["git", "merge-base", "--is-ancestor", fixture.candidate_revision, "target"],
                        cwd=fixture.project,
                        check=False,
                        capture_output=True,
                    )
                    self.assertEqual(1, ancestor.returncode)
                else:
                    self.git(
                        fixture,
                        "merge",
                        "--ff-only" if mode == "fast-forward" else "--no-ff",
                        "-m",
                        "integrate",
                        fixture.candidate_revision,
                    )
                self.assert_source(
                    self.integration(fixture, "target"), fixture, "protected-review", "target", "content-present"
                )
                self.assert_source(
                    self.integration(fixture, fixture.brief.base_revision),
                    fixture,
                    "protected-review",
                    fixture.brief.base_revision,
                    "content-not-present",
                )

    def test_working_tree_candidate_squash_and_local_remote_tracking_name(self) -> None:
        fixture = self.prepared()
        self.commit_all(fixture.project, "reviewed working tree")
        self.git(fixture, "switch", "-c", "target", fixture.brief.base_revision)
        self.git(fixture, "merge", "--squash", fixture.brief.branch)
        target = self.commit_all(fixture.project, "squash")
        self.git(fixture, "update-ref", "refs/remotes/origin/main", target)
        self.assert_source(
            self.integration(fixture, "origin/main"), fixture, "protected-review", "origin/main", "content-present"
        )

    def test_empty_diff_and_rejections(self) -> None:
        fixture = self.prepared()
        for target, item, code in (
            ("missing", "work-a", "INTEGRATION_TARGET_UNRESOLVED"),
            ("-bad", "work-a", "ITEM_STATUS_INVALID"),
            ("HEAD", "work-c", "INTEGRATION_CANDIDATE_UNAVAILABLE"),
            ("HEAD", "absent", "ITEM_NOT_FOUND"),
        ):
            result = self.integration(fixture, target, item)
            self.assertEqual(code, result["code"], result)
            self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"])
            self.assertEqual("unchanged", result["effect"])
            self.assertEqual("correct-input", result["retry"])
            if code.startswith("INTEGRATION_"):
                observed = {
                    self.json_object(fact)["field"]: self.json_object(fact)["value"]
                    for fact in self.json_array(result["observed"])
                }
                if code == "INTEGRATION_TARGET_UNRESOLVED":
                    self.assertEqual({"target": target, "project_root": str(fixture.project)}, observed)
                    self.assertIn("fetch it outside Pinboard", str(result["recovery"]))
                else:
                    self.assertEqual(item, observed["item_id"])
                    self.assertEqual("ready", observed["item_state"])
                    self.assertTrue(observed["reason"])
                    self.assertIn("operation item", str(result["recovery"]))
        self.transition(fixture, "return-for-correction", "work-a-1", {"reason": "Remove the change."})
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", self.integration(fixture, "HEAD")["code"])
        (fixture.project / "tracked.txt").write_text("base\n")
        fixture = self.submit(fixture, False)
        with patch.object(root.tempfile, "TemporaryDirectory", side_effect=AssertionError("no comparison")):
            self.assert_source(self.integration(fixture, "HEAD"), fixture, "protected-review", "HEAD", "no-change")
            self.assertEqual("INTEGRATION_TARGET_UNRESOLVED", self.integration(fixture, "missing")["code"])

    def test_tampered_snapshot_and_non_git_root_reject_without_repair(self) -> None:
        fixture = self.prepared()
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        artifact = fixture.work / context.reference.selector
        artifact.write_bytes(b"altered")
        result = self.integration(fixture, "HEAD")
        self.assertEqual("INTEGRATION_CANDIDATE_EVIDENCE_INVALID", result["code"], result)
        self.assertEqual("do-not-retry", result["retry"])
        self.assertEqual("unchanged", result["effect"])
        observed = {
            self.json_object(fact)["field"]: self.json_object(fact)["value"]
            for fact in self.json_array(result["observed"])
        }
        self.assertEqual({"attempt_id": "work-a-1", "accepted_reference": context.reference.selector}, observed)
        self.assertIn("pinboard validate", str(result["recovery"]))
        self.assertEqual(b"altered", artifact.read_bytes())
        outside = fixture.project / ".." / "non-git-integration"
        outside.mkdir(exist_ok=True)
        result = self.integration(replace(fixture, project=outside.resolve()), "HEAD")
        self.assertEqual("PROJECT_GIT_ROOT_UNAVAILABLE", result["code"], result)
        self.assertEqual("unchanged", result["effect"])
        self.assertEqual("correct-input", result["retry"])
        self.assertIn("checkout", str(result["recovery"]))

    def test_checkpoint_source_survives_resume_and_rebind(self) -> None:
        fixture = self.prepared()
        self.transition(
            fixture,
            "accept-checkpoint",
            "work-a-1",
            {
                "checkpoint": fixture.brief.checkpoint.checkpoint_id,
                "candidate": fixture.candidate_revision,
                "evidence": "Accepted reviewed change.",
            },
        )
        self.commit_all(fixture.project, "checkpoint work")
        self.git(fixture, "switch", "-c", "target", fixture.brief.base_revision)
        self.git(fixture, "merge", "--squash", fixture.brief.branch)
        self.commit_all(fixture.project, "checkpoint squash")
        result = self.integration(fixture, "target")
        self.assert_source(result, fixture, "accepted-checkpoint", "target", "content-present")
        self.assertEqual(fixture.brief.checkpoint.checkpoint_id, self.json_object(result["source"])["checkpoint_id"])
        self.git(fixture, "switch", fixture.brief.branch)
        code, output, errors = self.run_cli(
            *fixture.common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "Satisfied",
            "--task-id",
            "review-owner",
            "--host-id",
            "local",
        )
        self.assertEqual(0, code, output + errors)
        self.transition(fixture, "resume", "work-a", {})
        self.assert_source(
            self.integration(fixture, "target"), fixture, "accepted-checkpoint", "target", "content-present"
        )
        context = fixture.store.read_attempt_context(AttemptId("work-a-1"))
        assert isinstance(context, query_models.NonterminalAttemptContextFacts)
        reference = fixture.store.read_artifact_reference(
            context.brief_reference.kind, context.brief_reference.key, context.brief_reference.revision
        )
        assert reference is not None
        self.transition(
            fixture,
            "rebind-attempt",
            "work-a-1",
            {
                "branch": fixture.brief.branch,
                "base_revision": fixture.brief.base_revision,
                "brief_artifact_ref_id": int(reference.artifact_ref_id),
            },
        )
        self.assert_source(
            self.integration(fixture, "target"), fixture, "accepted-checkpoint", "target", "content-present"
        )
        (fixture.project / "tracked.txt").write_text("second reviewed change\n")
        newer = self.submit(fixture, False)
        current = self.integration(newer, "target")
        self.assertEqual("content-not-present", current["presence"])
        self.assertEqual(
            {
                "kind": "protected-review",
                "attempt_id": "work-a-1",
                "candidate_revision": newer.candidate_revision,
                "compared_from_revision": self.git(fixture, "rev-parse", "HEAD"),
            },
            current["source"],
        )
        self.transition(newer, "return-for-correction", "work-a-1", {"reason": "Correct the newer change."})
        self.assert_source(
            self.integration(fixture, "target"), fixture, "accepted-checkpoint", "target", "content-present"
        )
        newer = self.submit(fixture, False)
        self.transition(
            newer,
            "accept-review-and-continue",
            "work-a-1",
            {"candidate": newer.candidate_revision, "evidence": "Continue after second review."},
        )
        self.assert_source(
            self.integration(fixture, "target"), fixture, "accepted-checkpoint", "target", "content-present"
        )

    def test_direct_close_has_no_integration_candidate(self) -> None:
        fixture = self.prepared()
        code, output, errors = self.run_cli(
            *fixture.common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "Done directly",
            "--task-id",
            "review-owner",
            "--host-id",
            "local",
        )
        self.assertEqual(0, code, output + errors)
        result = self.integration(fixture, "HEAD", "work-c")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", result["code"], result)
        self.assertIn("directly", str(result["message"]))

    def test_focused_reads_use_indexes_and_never_walk_unrelated_retained_data(self) -> None:
        fixture = self.prepared()
        self.transition(
            fixture,
            "accept-checkpoint",
            "work-a-1",
            {
                "checkpoint": fixture.brief.checkpoint.checkpoint_id,
                "candidate": fixture.candidate_revision,
                "evidence": "Accepted focused-read candidate.",
            },
        )
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            for index in range(32):
                unrelated = f"unrelated-{index}"
                connection.execute(
                    """INSERT INTO attempts
                    SELECT ?, 'work-b', 'done', branch, base_revision, provenance, brief_artifact_ref_id,
                    brief_artifact_kind, result_artifact_ref_id, result_artifact_kind, candidate_revision,
                    candidate_recorded_at, accepted_scope_revision, accepted_scope_digest, subject_revision,
                    recorded_at, updated_at FROM attempts WHERE attempt_id = 'work-a-1'""",
                    (unrelated,),
                )
                connection.execute(
                    """INSERT INTO artifact_refs
                    SELECT (SELECT max(artifact_ref_id) + 1 FROM artifact_refs), ?, 1, 'evidence', ?,
                    content_sha256, size_bytes, accepted_revision, created_at FROM artifact_refs LIMIT 1""",
                    (f"{unrelated}-review-package", f"artifacts/{unrelated}.json"),
                )
                (fixture.work / "artifacts" / f"{unrelated}.json").write_text("damaged unrelated package")
                connection.execute(
                    """INSERT INTO transition_history
                    SELECT (SELECT max(history_id) + 1 FROM transition_history),
                    (SELECT max(project_revision) + 1 FROM transition_history), ?, 'accept-checkpoint', ?,
                    (SELECT max(artifact_ref_id) FROM artifact_refs), 'evidence', authorization_kind,
                    actor_task_id, actor_host_id, input_schema, 'bad input', 'checkpoint-acceptance/v2',
                    'bad outcome', committed_at FROM transition_history LIMIT 1""",
                    (f"accept-checkpoint:{unrelated}", unrelated),
                )
        statements: list[str] = []
        original_open = sqlite_store.open_database

        def traced_open(path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open(path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with (
            patch.object(SQLiteWorkStore, "read_item_status", side_effect=AssertionError("no verdict walk")),
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("no full scan")),
            patch.object(sqlite_store, "open_database", traced_open),
        ):
            result = self.integration(fixture, fixture.brief.base_revision)
        self.assert_source(result, fixture, "accepted-checkpoint", fixture.brief.base_revision, "content-not-present")
        self.assertNotIn("unrelated", str(result))
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            for statement in statements:
                if statement.lstrip().upper().startswith("SELECT"):
                    with self.subTest(statement=statement):
                        plan = str(connection.execute(f"EXPLAIN QUERY PLAN {statement}").fetchall())
                        self.assertNotIn("SCAN", plan.upper())
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT history_id FROM transition_history WHERE subject_id = ? AND outcome_schema = 'checkpoint-acceptance/v2' ORDER BY history_id DESC LIMIT 1",
                ("work-a-1",),
            ).fetchall()
        self.assertIn("checkpoint_history_by_subject", str(plan))

    def remove_candidate_snapshot(self, fixture: CheckpointFixture, legacy: bool) -> None:
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            if legacy:
                # Represent retained history; no supported producer creates retired submissions.
                connection.execute(
                    """UPDATE transition_history SET input_schema = 'decision/v1', input_json = '{}',
                       outcome_schema = 'transition-receipt/v1', outcome_json = ?, artifact_ref_id = NULL,
                       artifact_kind = NULL WHERE project_revision = ?""",
                    (
                        history.encode_transition_receipt_outcome(
                            evidence=None, outcome="submit-review", candidate=fixture.candidate_revision
                        ).decode(),
                        context.reference.accepted_revision,
                    ),
                )
            connection.execute(
                "DELETE FROM artifact_refs WHERE artifact_ref_id = ?", (context.reference.artifact_ref_id,)
            )

    def assert_missing_current_snapshot(self, fixture: CheckpointFixture) -> None:
        before = (fixture.work / "state.sqlite3").read_bytes()
        with self.assertRaises(UnexpectedToolError) as raised:
            call_native_tool(
                server.ITEM_STATUS_TOOL,
                {"request": {**self.roots(fixture), "operation": "integration", "item_id": "work-a", "target": "HEAD"}},
            )
        cause = raised.exception.__cause__
        self.assertIsInstance(cause, StorageError)
        assert isinstance(cause, StorageError)
        self.assertEqual(StorageErrorCode.INVALID_STATE, cause.code)
        self.assertEqual(before, (fixture.work / "state.sqlite3").read_bytes())

    def test_current_missing_snapshot_is_an_invariant_failure_in_review_and_completion(self) -> None:
        for completed in (False, True):
            with self.subTest(completed=completed):
                fixture = self.prepared(commit=True)
                if completed:
                    fixture = self.terminalize_brief(fixture)
                    self.record_ready(fixture)
                    self.complete_candidate(fixture)
                self.remove_candidate_snapshot(fixture, legacy=False)
                self.assert_missing_current_snapshot(fixture)
                with self.assertRaises(StorageError):
                    fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))

    def test_canonical_legacy_absence_in_review_and_completion(self) -> None:
        for completed in (False, True):
            with self.subTest(completed=completed):
                fixture = self.prepared(commit=True)
                if completed:
                    fixture = self.terminalize_brief(fixture)
                    self.record_ready(fixture)
                    self.complete_candidate(fixture)
                self.remove_candidate_snapshot(fixture, legacy=True)
                result = self.integration(fixture, "HEAD")
                self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", result["code"], result)
                self.assertEqual("unchanged", result["effect"])
                self.assertEqual("correct-input", result["retry"])
                self.assertIn("canonical pre-snapshot", str(result["message"]))
                self.assertIn("operation item", str(result["recovery"]))
                # The approved completion fallback does not broaden attempt inspection.
                if completed:
                    with self.assertRaises(StorageError):
                        fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
                else:
                    self.assertIsNone(fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1")))
                with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
                    connection.execute(
                        "UPDATE transition_history SET input_json = '{ }' WHERE subject_id = 'work-a-1' AND action_kind = 'submit-review' AND input_schema = 'decision/v1'"
                    )
                self.assert_missing_current_snapshot(fixture)
                for candidate, committed_at in (
                    ("0" * 40, "2030-01-02T00:00:00+00:00"),
                    (fixture.candidate_revision, "2020-01-01T00:00:00+00:00"),
                ):
                    with self.subTest(candidate=candidate, committed_at=committed_at):
                        with (
                            contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection,
                            connection,
                        ):
                            connection.execute(
                                "UPDATE transition_history SET input_json = '{}', outcome_json = ?, committed_at = ? WHERE subject_id = 'work-a-1' AND action_kind = 'submit-review' AND input_schema = 'decision/v1'",
                                (
                                    history.encode_transition_receipt_outcome(
                                        evidence=None, outcome="submit-review", candidate=candidate
                                    ).decode(),
                                    committed_at,
                                ),
                            )
                        self.assert_missing_current_snapshot(fixture)

    def test_present_snapshot_keeps_protected_and_completion_reads_keyed(self) -> None:
        fixture = self.prepared(commit=True)
        original_open = sqlite_store.open_database
        statements: list[str] = []

        def traced_open(path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open(path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        for completed in (False, True):
            with self.subTest(completed=completed):
                if completed:
                    fixture = self.terminalize_brief(fixture)
                    self.record_ready(fixture)
                    self.complete_candidate(fixture)
                statements.clear()
                with patch.object(sqlite_store, "open_database", traced_open):
                    result = self.integration(fixture, "HEAD")
                self.assertEqual("content-present", result["presence"])
                with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
                    for statement in statements:
                        if statement.lstrip().upper().startswith("SELECT"):
                            plan = str(connection.execute(f"EXPLAIN QUERY PLAN {statement}").fetchall())
                            self.assertNotIn("SCAN", plan.upper(), statement)

    def test_retained_checkpoint_without_snapshot_reference_is_unavailable(self) -> None:
        checkpoint = self.accepted_package_fixture(local=True, candidate_form="current-head")
        self.retain_v2_checkpoint(checkpoint)
        result = self.integration(checkpoint, "HEAD")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", result["code"], result)
        self.assertIn("snapshot reference", str(result["message"]))
        self.assertEqual("unchanged", result["effect"])
        self.assertEqual("correct-input", result["retry"])
        self.assertTrue(result["observed"])
        self.assertIn("item", str(result["recovery"]))

    def test_working_tree_preimage_and_later_edits_in_reviewed_file(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        self.transition(fixture, "return-for-correction", "work-a-1", {"reason": "Prepare multiline change."})
        path = fixture.project / "tracked.txt"
        path.write_text("".join(f"line {i}\n" for i in range(30)))
        (fixture.project / "binary").write_bytes(b"\x00old\xff")
        preimage = self.commit_all(fixture.project, "context before reviewed diff")
        self.git(fixture, "mv", "binary", "renamed")
        (fixture.project / "renamed").write_bytes(b"\x00new\xff")
        path.write_text(path.read_text().replace("line 2\n", "reviewed line 2\n"))
        fixture = self.submit(fixture, False)
        self.commit_all(fixture.project, "candidate")
        path.write_text(path.read_text().replace("line 25\n", "later line 25\n"))
        self.commit_all(fixture.project, "later independent edit")
        result = self.integration(fixture, "HEAD")
        self.assertEqual("content-present", result["presence"], result)
        self.assertEqual(preimage, self.json_object(result["source"])["compared_from_revision"])
        self.assertNotEqual(fixture.brief.base_revision, preimage)
        path.write_text(path.read_text().replace("reviewed line 2\n", "later overlapping edit\n"))
        self.commit_all(fixture.project, "later overlapping edit")
        self.assertEqual("content-not-present", self.integration(fixture, "HEAD")["presence"])

    def test_other_status_and_portfolio_reads_do_not_observe_integration(self) -> None:
        fixture = self.prepared()
        with (
            patch.object(
                SQLiteWorkStore, "read_item_integration", side_effect=AssertionError("integration facts are isolated")
            ),
            patch.object(
                root, "read_integration_content", side_effect=AssertionError("integration Git read is isolated")
            ),
        ):
            item = call_advertised_tool(
                server.ITEM_STATUS_TOOL, {"request": {**self.roots(fixture), "operation": "item", "item_id": "work-a"}}
            )
            branch = call_advertised_tool(
                server.ITEM_STATUS_TOOL,
                {"request": {**self.roots(fixture), "operation": "branch", "branch": fixture.brief.branch}},
            )
            overview = call_advertised_tool(server.OVERVIEW_TOOL, self.roots(fixture))
            actions = call_advertised_tool(
                server.ACTIONS_TOOL, {"request": {**self.roots(fixture), "role": "project", "action_id": None}}
            )
        self.assertEqual("pinboard-item-status/v2", item["schema"])
        self.assertEqual("pinboard-branch-owners/v1", branch["schema"])
        self.assertEqual("pinboard-overview/v6", overview["schema"])
        self.assertEqual("ok", actions["status"])

    def test_missing_checkpoint_snapshot_reference_names_the_selected_resource(self) -> None:
        fixture = self.accepted_package_fixture(local=True, candidate_form="current-head")
        package = self.package(fixture)
        assert isinstance(package, work_brief_models.CheckpointReviewPackageV3)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "DELETE FROM artifact_refs WHERE relative_path = ?", (package.candidate_snapshot.selector,)
            )
        result = self.integration(fixture, "HEAD")
        self.assertEqual("INTEGRATION_CANDIDATE_EVIDENCE_INVALID", result["code"], result)
        self.assertEqual("unchanged", result["effect"])
        self.assertEqual("do-not-retry", result["retry"])
        self.assertIn(package.candidate_snapshot.selector, str(result["observed"]))
        self.assertIn("pinboard validate", str(result["recovery"]))

    def test_accepted_continuation_is_not_an_integration_source(self) -> None:
        fixture = self.prepared()
        self.transition(
            fixture,
            "accept-review-and-continue",
            "work-a-1",
            {"candidate": fixture.candidate_revision, "evidence": "Reviewed continuation."},
        )
        result = self.integration(fixture, "HEAD")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", result["code"], result)
        self.assertEqual("active", self.json_object(self.json_array(result["observed"])[1])["value"])

    def record_ready(self, fixture: CheckpointFixture) -> None:
        snapshot = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        attempt = fixture.store.read_attempt_context(AttemptId("work-a-1"))
        assert snapshot is not None and isinstance(attempt, query_models.NonterminalAttemptContextFacts)
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
                    "acceptance_evidence": "Verified reviewed change.",
                },
            },
        )
        self.assertEqual("recorded", recorded["status"], recorded)

    def complete_candidate(self, fixture: CheckpointFixture) -> None:
        attempt_root = fixture.work / "attempts" / "work-a-1"
        self.transition(
            fixture,
            "complete",
            "work-a-1",
            {
                "schema": "pinboard-reviewed-completion/v2",
                "candidate": fixture.candidate_revision,
                "evidence": "Reviewed completion.",
                "reviewer_task_id": "independent-reviewer",
                "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
                "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
                "packages": [],
            },
        )

    def test_completion_source_and_inspection_reconciliation_use_separate_observations(self) -> None:
        fixture = self.terminalize_brief(self.prepared(commit=True))
        self.record_ready(fixture)
        reconciliation: JsonObject = {
            "target_revision": fixture.candidate_revision,
            "relation": "candidate-integrated",
            "phase": "cleanup",
            "effects": [
                {"effect": effect, "status": "not-required" if effect == "shared-work-root" else "allowed"}
                for effect in ("source-checkout", "shared-work-root", "git-metadata")
            ],
        }
        with patch.object(
            root, "read_integration_content", side_effect=AssertionError("inspection does not derive relations")
        ):
            inspection = call_advertised_tool(
                server.ATTEMPT_INSPECT_TOOL,
                {**self.roots(fixture), "attempt_id": "work-a-1", "reconciliation": reconciliation},
            )
        self.assertEqual("ok", inspection["status"], inspection)
        self.assertEqual(
            "repository-cleanup",
            self.json_object(self.json_object(inspection["continuation"])["next_operation"])["kind"],
        )
        self.complete_candidate(fixture)
        self.assert_source(self.integration(fixture, "HEAD"), fixture, "completion", "HEAD", "content-present")

    def test_damaged_checkpoint_receipt_is_diagnosed_without_repair(self) -> None:
        fixture = self.prepared()
        receipt = self.transition(
            fixture,
            "accept-checkpoint",
            "work-a-1",
            {
                "checkpoint": fixture.brief.checkpoint.checkpoint_id,
                "candidate": fixture.candidate_revision,
                "evidence": "Accepted checkpoint.",
            },
        )
        for damaged in ("{}", "not JSON"):
            with self.subTest(damaged=damaged):
                with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
                    connection.execute(
                        "UPDATE transition_history SET outcome_json = ? WHERE history_id = ?",
                        (damaged, receipt["history_id"]),
                    )
                before = (fixture.work / "state.sqlite3").read_bytes()
                result = self.integration(fixture, "HEAD")
                self.assertEqual("TRANSITION_RECEIPT_DAMAGED", result["code"], result)
                self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"])
                self.assertEqual("unchanged", result["effect"])
                self.assertEqual("do-not-retry", result["retry"])
                self.assertTrue(result["observed"])
                self.assertIn("human", str(result["recovery"]))
                self.assertEqual(before, (fixture.work / "state.sqlite3").read_bytes())

    def test_trace_override_and_read_only_repository_and_ledger(self) -> None:
        fixture = self.prepared()
        config = fixture.work / "contributor-traces.config"
        config.write_text(
            '[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = off\n[item "work-a"]\n\tmode = on\n'
        )
        executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        application = server.create_server(
            executor,
            execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256),
            execution.AutomaticCapture(common.select_capture_item),
        )

        async def automatic_call() -> JsonObject:
            async with Client(application) as client:
                response = await client.call_tool(
                    server.ITEM_STATUS_TOOL,
                    {
                        "request": {
                            **self.roots(fixture),
                            "operation": "integration",
                            "item_id": "work-a",
                            "target": fixture.brief.base_revision,
                        }
                    },
                )
                assert isinstance(response.structured_content, dict) and not response.is_error
                return response.structured_content

        result = asyncio.run(automatic_call())
        traces = list((fixture.work / "invocation-traces").glob("pinboard-auto-mcp-*.json"))
        self.assertEqual(1, len(traces))
        captured = json.loads(traces[0].read_bytes())
        self.assertEqual(result, captured["result"]["value"])
        config.write_text(
            '[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = off\n[item "work-a"]\n\tmode = off\n'
        )
        before = {
            str(p.relative_to(fixture.project)): p.read_bytes() for p in fixture.project.rglob("*") if p.is_file()
        }
        asyncio.run(automatic_call())
        after = {str(p.relative_to(fixture.project)): p.read_bytes() for p in fixture.project.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_git_read_failure_keeps_diagnostic_and_unchanged_retry(self) -> None:
        fixture = self.prepared()
        with patch.object(
            root,
            "read_integration_content",
            side_effect=RootError(RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE, "cannot read target tree"),
        ):
            result = self.integration(fixture, "HEAD")
        self.assertEqual("PROJECT_GIT_CHECKOUT_UNAVAILABLE", result["code"], result)
        self.assertEqual("unchanged", result["effect"])
        self.assertEqual("correct-input", result["retry"])
        self.assertIn("cannot read target tree", str(result["observed"]))
        self.assertIn("checkout", str(result["recovery"]))
