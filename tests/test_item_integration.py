"""Reviewed content is observed at a local target through the native MCP leaf."""

import asyncio
import contextlib
import hashlib
import io
import os
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import override
from unittest.mock import patch

import msgspec
from mcp import Client
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files import contributor_traces, root
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import query_models
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import common, execution, server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import JsonObject


class ItemIntegrationTest(CheckpointPackageSupport):
    @override
    def setUp(self) -> None:
        fixed_git = patch.dict(
            os.environ,
            {"GIT_AUTHOR_DATE": "2030-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2030-01-01T00:00:00+00:00"},
        )
        fixed_git.start()
        self.addCleanup(fixed_git.stop)

    def git(self, project: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=Integration Tests", "-c", "user.email=integration@example.invalid", *arguments],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def integration(self, fixture: CheckpointFixture, target: str = "main", item: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": item, "target": target}},
        )

    def transition(self, fixture: CheckpointFixture, action: str, payload: JsonObject) -> JsonObject:
        result = self.transition_result(fixture, self.project_action(fixture, action), payload)
        self.assertIn(result["status"], ("committed", "committed-with-warning"), result)
        return result

    def candidate(
        self,
        *,
        committed: bool = False,
        empty: bool = False,
        context: bool = False,
        multiline: bool = False,
        complex_change: bool = False,
    ) -> CheckpointFixture:
        fixture = self.checkpoint_fixture(local=True, review_condition="missing", committed_context=context)
        self.transition(
            fixture,
            "return-for-correction:work-a-1",
            {"reason": "Prepare the candidate through the installed submission."},
        )
        self.git(fixture.project, "branch", "main", fixture.brief.base_revision)
        (fixture.project / "tracked.txt").write_text("base\n" if empty else "reviewed\n", encoding="utf-8")
        if complex_change:
            (fixture.project / "tracked.txt").write_text("base\n", encoding="utf-8")
            (fixture.project / "tracked.txt").rename(fixture.project / "renamed.txt")
            (fixture.project / "binary.dat").write_bytes(b"\x00reviewed\xffbinary")
            self.git(fixture.project, "add", "--intent-to-add", "renamed.txt", "binary.dat")
        if multiline:
            (fixture.project / "tracked.txt").write_text("base\n" + "context\n" * 20, encoding="utf-8")
            base = self.commit_all(fixture.project, "long file base")
            brief = replace_struct(fixture.brief, artifact_revision=2, base_revision=base)
            published = call_native_tool(
                server.BRIEF_PUBLISH_TOOL,
                {**self.roots(fixture), "brief": self.json_object(msgspec.to_builtins(brief))},
            )
            self.assertEqual("committed", published["status"], published)
            self.transition(
                fixture,
                "rebind-attempt:work-a-1",
                {
                    "branch": brief.branch,
                    "base_revision": base,
                    "brief_artifact_ref_id": self.json_object(published["reference"])["artifact_ref_id"],
                },
            )
            fixture = replace(fixture, brief=brief)
            self.git(fixture.project, "branch", "-f", "main", base)
            (fixture.project / "tracked.txt").write_text("reviewed\n" + "context\n" * 20, encoding="utf-8")
        if committed and not empty:
            candidate = self.commit_all(fixture.project, "reviewed change")
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
        return replace(fixture, candidate_revision=candidate)

    def assert_presence(
        self, fixture: CheckpointFixture, expected: str, *, target: str = "main", kind: str = "protected-review"
    ) -> JsonObject:
        result = self.integration(fixture, target)
        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual(expected, result["presence"], result)
        self.assertEqual(target, result["target"])
        self.assertEqual(self.git(fixture.project, "rev-parse", f"{target}^{{commit}}"), result["target_revision"])
        source = self.json_object(result["source"])
        self.assertEqual(kind, source["kind"])
        self.assertEqual("work-a-1", source["attempt_id"])
        self.assertEqual(fixture.candidate_revision, source["candidate_revision"])
        context = SQLiteWorkStore(fixture.work / "state.sqlite3").read_candidate_snapshot_context(AttemptId("work-a-1"))
        if context is not None:
            self.assertEqual(context.base_revision, source["compared_from_revision"])
        return result

    def rejection(
        self, fixture: CheckpointFixture, code: str, retry: str, *, target: str = "main", item: str = "work-a"
    ) -> JsonObject:
        result = self.integration(fixture, target, item)
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual(code, result["code"], result)
        self.assertEqual("unchanged", result["effect"])
        self.assertEqual(retry, result["retry"])
        self.assertFalse(result["state_changed"])
        self.assertEqual([], result["changed_surfaces"])
        if code not in ("ITEM_NOT_FOUND", "ITEM_STATUS_INVALID"):
            self.assertTrue(result["observed"])
            self.assertTrue(result["recovery"])
        observations = result["observed"]
        assert isinstance(observations, list)
        fields = {self.json_object(value)["field"]: self.json_object(value)["value"] for value in observations}
        if code == "INTEGRATION_TARGET_UNRESOLVED":
            self.assertEqual(target, fields["target"])
            self.assertEqual(str(fixture.project), fields["project_root"])
            self.assertIn("fetch it outside Pinboard", str(result["recovery"]))
        elif code == "INTEGRATION_CANDIDATE_UNAVAILABLE":
            self.assertEqual(item, fields["item_id"])
            self.assertTrue(fields["state"])
            self.assertTrue(fields["reason"])
            self.assertIn("operation item", str(result["recovery"]))
        elif code == "INTEGRATION_CANDIDATE_EVIDENCE_INVALID":
            self.assertEqual("work-a-1", fields["attempt_id"])
            self.assertTrue(fields["accepted_reference"])
            self.assertIn("pinboard validate", str(result["recovery"]))
        elif code == "TRANSITION_RECEIPT_DAMAGED":
            self.assertEqual("work-a-1", fields["attempt_id"])
            self.assertTrue(fields["history_id"])
            self.assertTrue(fields["committed_at"])
        return result

    def test_commit_candidates_present_after_fast_forward_merge_and_rebase(self) -> None:
        for method in ("fast-forward", "merge", "rebase"):
            with self.subTest(method=method):
                fixture = self.candidate(committed=True)
                self.assert_presence(fixture, "content-not-present")
                self.git(fixture.project, "switch", "main")
                if method != "fast-forward":
                    (fixture.project / "other.txt").write_text("target change\n", encoding="utf-8")
                    self.commit_all(fixture.project, "target change")
                if method == "rebase":
                    self.git(fixture.project, "switch", fixture.brief.branch)
                    self.git(fixture.project, "rebase", "main")
                    self.git(fixture.project, "switch", "main")
                    self.git(fixture.project, "merge", "--ff-only", fixture.brief.branch)
                    ancestor = subprocess.run(
                        ["git", "merge-base", "--is-ancestor", fixture.candidate_revision, "main"],
                        cwd=fixture.project,
                        check=False,
                        capture_output=True,
                    )
                    self.assertEqual(1, ancestor.returncode)
                else:
                    self.git(
                        fixture.project,
                        "merge",
                        "--ff-only" if method == "fast-forward" else "--no-ff",
                        fixture.brief.branch,
                    )
                self.assert_presence(fixture, "content-present")

    def test_working_tree_squash_and_local_remote_tracking_target(self) -> None:
        fixture = self.candidate(context=True)
        original = root.read_working_tree_candidate(fixture.project)
        self.commit_all(fixture.project, "commit reviewed bytes")
        self.git(fixture.project, "switch", "main")
        self.git(fixture.project, "merge", "--squash", fixture.brief.branch)
        self.commit_all(fixture.project, "squash reviewed bytes")
        result = self.integration(fixture)
        self.assertEqual("content-present", result["presence"], result)
        self.assertEqual(original.preimage_revision, self.json_object(result["source"])["compared_from_revision"])
        self.git(fixture.project, "update-ref", "refs/remotes/origin/main", "main")
        self.assertEqual("content-present", self.integration(fixture, "origin/main")["presence"])

    def test_squash_preserves_rename_and_binary_content(self) -> None:
        fixture = self.candidate(complex_change=True)
        self.commit_all(fixture.project, "commit rename and binary")
        self.git(fixture.project, "switch", "main")
        self.git(fixture.project, "merge", "--squash", fixture.brief.branch)
        self.commit_all(fixture.project, "squash rename and binary")
        self.assert_presence(fixture, "content-present")
        (fixture.project / "binary.dat").write_bytes(b"\x00different\xffbinary")
        self.commit_all(fixture.project, "replace binary content")
        self.assert_presence(fixture, "content-not-present")

    def test_item_trace_override_applies_to_integration(self) -> None:
        fixture = self.candidate()
        (fixture.work / contributor_traces.SETTINGS_NAME).write_text(
            '[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = off\n[item "work-a"]\n\tmode = on\n',
            encoding="utf-8",
        )
        executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        sdk_server = server.create_server(
            executor,
            execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256),
            execution.AutomaticCapture(common.select_capture_item),
        )

        async def call() -> None:
            async with Client(sdk_server) as client:
                result = await client.call_tool(
                    server.ITEM_STATUS_TOOL,
                    {
                        "request": {
                            **self.roots(fixture),
                            "operation": "integration",
                            "item_id": "work-a",
                            "target": "main",
                        }
                    },
                )
                self.assertFalse(result.is_error)

        try:
            asyncio.run(call())
        finally:
            executor.shutdown()
        traces = tuple((fixture.work / contributor_traces.TRACE_DIRECTORY).glob("pinboard-auto-*.json"))
        self.assertEqual(1, len(traces))

    def test_inspection_uses_caller_relation_without_integration_read(self) -> None:
        fixture = self.candidate()
        self.record_ready(fixture)
        reconciliation = query_models.AttemptReconciliation(
            fixture.brief.base_revision,
            query_models.IntegrationRelation.CANDIDATE_INTEGRATED,
            query_models.RepositoryPhase.CLEANUP,
            (
                query_models.RuntimeEffectObservation(
                    query_models.RuntimeEffect.SOURCE_CHECKOUT, query_models.RuntimeEffectStatus.ALLOWED
                ),
                query_models.RuntimeEffectObservation(
                    query_models.RuntimeEffect.SHARED_WORK_ROOT, query_models.RuntimeEffectStatus.NOT_REQUIRED
                ),
                query_models.RuntimeEffectObservation(
                    query_models.RuntimeEffect.GIT_METADATA, query_models.RuntimeEffectStatus.ALLOWED
                ),
            ),
        )
        with patch.object(
            root, "read_integration_content", side_effect=AssertionError("inspection owns no content read")
        ):
            inspected = call_advertised_tool(
                server.ATTEMPT_INSPECT_TOOL,
                {
                    **self.roots(fixture),
                    "attempt_id": "work-a-1",
                    "reconciliation": self.json_object(msgspec.to_builtins(reconciliation)),
                },
            )
        continuation = self.json_object(inspected["continuation"])
        self.assertEqual("repository-cleanup", self.json_object(continuation["next_operation"])["kind"])

    def test_later_nonoverlapping_and_overlapping_edits(self) -> None:
        fixture = self.candidate(committed=True, multiline=True)
        self.git(fixture.project, "switch", "main")
        self.git(fixture.project, "merge", "--ff-only", fixture.brief.branch)
        (fixture.project / "tracked.txt").write_text("reviewed\n" + "context\n" * 20 + "later\n", encoding="utf-8")
        self.commit_all(fixture.project, "later nonoverlapping addition")
        self.assert_presence(fixture, "content-present")
        (fixture.project / "tracked.txt").write_text("changed after integration\n", encoding="utf-8")
        self.commit_all(fixture.project, "later overlapping edit")
        self.assert_presence(fixture, "content-not-present")

    def test_empty_diff_skips_index_comparison(self) -> None:
        fixture = self.candidate(empty=True)
        with patch(
            "pinboard.adapters.files.root.TemporaryDirectory",
            side_effect=AssertionError("empty diff needs no temporary index"),
        ):
            self.assert_presence(fixture, "no-change")

    def test_native_read_preserves_repository_work_root_and_ledger(self) -> None:
        fixture = self.candidate(committed=True)
        self.git(fixture.project, "switch", "main")
        self.git(fixture.project, "merge", "--ff-only", fixture.brief.branch)
        before = {
            str(path.relative_to(fixture.project)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in fixture.project.rglob("*")
            if path.is_file()
        }
        result = self.integration(fixture)
        self.assertEqual("content-present", result["presence"], result)
        after = {
            str(path.relative_to(fixture.project)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in fixture.project.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_git_read_failure_has_resource_diagnostic_and_read_recovery(self) -> None:
        fixture = self.candidate()
        with patch.object(
            root, "TemporaryDirectory", side_effect=PermissionError("temporary index directory is unwritable")
        ):
            result = self.integration(fixture)
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"])
        self.assertEqual("PROJECT_GIT_CHECKOUT_UNAVAILABLE", result["code"])
        self.assertEqual("unchanged", result["effect"])
        self.assertEqual("correct-input", result["retry"])
        observations = result["observed"]
        assert isinstance(observations, list)
        facts = {self.json_object(value)["field"]: self.json_object(value)["value"] for value in observations}
        self.assertEqual(str(fixture.project), facts["project_root"])
        self.assertIn("unwritable", str(facts["diagnostic"]))
        self.assertIn("repeat this read", str(result["recovery"]))

    def test_checkpoint_source_survives_resume_and_rebind(self) -> None:
        fixture = self.accepted_package_fixture(local=True)
        self.git(fixture.project, "branch", "main", fixture.brief.base_revision)
        self.commit_all(fixture.project, "commit checkpoint")
        self.git(fixture.project, "switch", "main")
        self.git(fixture.project, "merge", "--squash", fixture.brief.branch)
        self.commit_all(fixture.project, "squash checkpoint")
        result = self.assert_presence(fixture, "content-present", kind="accepted-checkpoint")
        self.assertEqual(fixture.brief.checkpoint.checkpoint_id, self.json_object(result["source"])["checkpoint_id"])
        closed, stdout, stderr = self.run_cli(
            *fixture.common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "Prerequisite done.",
            "--task-id",
            "human",
            "--host-id",
            "local",
        )
        self.assertEqual(0, closed, f"{stdout} {stderr}")
        self.transition(fixture, "resume:work-a", {})
        self.assert_presence(fixture, "content-present", kind="accepted-checkpoint")

        self.git(fixture.project, "switch", fixture.brief.branch)
        (fixture.project / "tracked.txt").write_text("new protected candidate\n", encoding="utf-8")
        observed = call_native_tool(server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"})
        candidate = observed["candidate"]
        assert isinstance(candidate, str)
        lease = self.native_attempt_acquire(fixture, "resumed-integration-worker")
        action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        submitted = self.transition_result(fixture, action, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)
        result = self.integration(fixture)
        self.assertEqual("protected-review", self.json_object(result["source"])["kind"])
        self.assertEqual(candidate, self.json_object(result["source"])["candidate_revision"])
        self.transition(fixture, "return-for-correction:work-a-1", {"reason": "Return to checkpoint source."})
        self.assert_presence(fixture, "content-present", kind="accepted-checkpoint")
        attempt = fixture.store.read_attempt_context(AttemptId("work-a-1"))
        assert isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        self.transition(
            fixture,
            "rebind-attempt:work-a-1",
            {
                "branch": fixture.brief.branch,
                "base_revision": fixture.brief.base_revision,
                "brief_artifact_ref_id": int(attempt.brief_artifact_ref_id),
            },
        )
        self.assert_presence(fixture, "content-present", kind="accepted-checkpoint")

    def test_continued_review_falls_back_to_latest_checkpoint(self) -> None:
        fixture = self.accepted_package_fixture(local=True)
        closed, stdout, stderr = self.run_cli(
            *fixture.common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "Prerequisite done.",
            "--task-id",
            "human",
            "--host-id",
            "local",
        )
        self.assertEqual(0, closed, f"{stdout} {stderr}")
        self.transition(fixture, "resume:work-a", {})
        observed = call_native_tool(server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"})
        candidate = observed["candidate"]
        assert isinstance(candidate, str)
        lease = self.native_attempt_acquire(fixture, "continued-integration-worker")
        action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        submitted = self.transition_result(fixture, action, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)
        self.record_ready(replace(fixture, candidate_revision=candidate))
        self.transition(
            fixture,
            "accept-review-and-continue:work-a-1",
            {"candidate": candidate, "evidence": "Continue after review."},
        )
        result = self.integration(fixture, fixture.brief.base_revision)
        self.assertEqual("accepted-checkpoint", self.json_object(result["source"])["kind"])
        self.assertEqual(fixture.candidate_revision, self.json_object(result["source"])["candidate_revision"])

    def test_unavailable_unknown_target_bad_input_and_damaged_evidence(self) -> None:
        fixture = self.candidate()
        self.rejection(fixture, "INTEGRATION_TARGET_UNRESOLVED", "correct-input", target="missing-ref")
        self.rejection(fixture, "ITEM_STATUS_INVALID", "correct-input", target="-main")
        self.rejection(fixture, "ITEM_NOT_FOUND", "correct-input", item="unknown")
        self.rejection(fixture, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input", item="work-c")
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        (fixture.work / context.reference.selector).write_bytes(b"changed accepted bytes")
        self.rejection(fixture, "INTEGRATION_CANDIDATE_EVIDENCE_INVALID", "do-not-retry")

    def test_return_and_continue_do_not_reuse_review_candidate(self) -> None:
        fixture = self.candidate()
        self.transition(fixture, "return-for-correction:work-a-1", {"reason": "Needs correction."})
        self.rejection(fixture, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")

    def test_non_git_project_has_typed_unchanged_diagnosis(self) -> None:
        fixture = self.candidate()
        # A sibling directory is outside this repository, with the same private ledger.
        outside = fixture.project.parent / (fixture.project.name + "-outside")
        outside.mkdir()
        result = call_advertised_tool(
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
        self.assertEqual("PROJECT_GIT_ROOT_UNAVAILABLE", result["code"], result)
        self.assertEqual("unchanged", result["effect"])
        self.assertEqual("correct-input", result["retry"])
        self.assertTrue(result["observed"])
        self.assertIn("checkout", str(result["recovery"]))

    def test_selected_reads_use_indexes_and_never_walk_review_history(self) -> None:
        fixture = self.accepted_package_fixture(local=True)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            revision = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()[0]
            for index in range(30):
                unrelated = f"unrelated-retained-{index}"
                path = f"artifacts/evidence/{unrelated}/1.json"
                file = fixture.work / path
                file.parent.mkdir(parents=True)
                file.write_bytes(b"damaged unrelated package and snapshot")
                connection.execute(
                    "INSERT INTO artifact_refs(artifact_key, artifact_revision, kind, relative_path, content_sha256, size_bytes, accepted_revision, created_at) SELECT ?, 1, 'evidence', ?, content_sha256, size_bytes, accepted_revision, created_at FROM artifact_refs LIMIT 1",
                    (unrelated, path),
                )
                connection.execute(
                    "INSERT INTO attempts SELECT ?, item_id, 'done', branch, base_revision, provenance, brief_artifact_ref_id, brief_artifact_kind, result_artifact_ref_id, result_artifact_kind, candidate_revision, candidate_recorded_at, accepted_scope_revision, accepted_scope_digest, subject_revision, recorded_at, updated_at FROM attempts WHERE attempt_id = 'work-a-1'",
                    (unrelated,),
                )
                connection.execute(
                    "INSERT INTO transition_history SELECT (SELECT max(history_id) + 1 FROM transition_history), ?, action_id, action_kind, ?, artifact_ref_id, artifact_kind, authorization_kind, actor_task_id, actor_host_id, input_schema, input_json, outcome_schema, '{}', committed_at FROM transition_history WHERE outcome_schema = 'checkpoint-acceptance/v2' LIMIT 1",
                    (revision + index + 1, unrelated),
                )
            connection.execute("UPDATE project_meta SET revision = ? WHERE singleton = 1", (revision + 30,))
        with (
            patch(
                "pinboard.adapters.sqlite.lifecycle._read_review_event",
                side_effect=AssertionError("integration does not walk review events"),
            ),
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("no retained ledger scan")),
        ):
            result = self.integration(fixture, fixture.brief.base_revision)
        self.assertEqual("content-not-present", result["presence"], result)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT history_id FROM transition_history WHERE subject_id = ? AND outcome_schema = 'checkpoint-acceptance/v2' ORDER BY history_id DESC LIMIT 1",
                ("work-a-1",),
            ).fetchall()
            keyed = connection.execute(
                "EXPLAIN QUERY PLAN SELECT state, subject_revision FROM work_items WHERE item_id = ?", ("work-a",)
            ).fetchall()
            attempts = connection.execute(
                "EXPLAIN QUERY PLAN SELECT attempt_id, state, candidate_revision FROM attempts INDEXED BY one_live_attempt_per_item WHERE item_id = ? AND state != 'done'",
                ("work-a",),
            ).fetchall()
        self.assertTrue(all("SEARCH" in row[3] for row in (*keyed, *attempts)), (keyed, attempts))
        self.assertTrue(any("checkpoint_history_by_subject" in row[3] for row in plan), plan)

    def record_ready(self, fixture: CheckpointFixture) -> tuple[str, str]:
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        attempt = fixture.store.read_attempt_context(AttemptId("work-a-1"))
        assert context is not None and isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        directory = fixture.work / "attempts" / "work-a-1"
        result_digest = hashlib.sha256((directory / "result.md").read_bytes()).hexdigest()
        review_digest = hashlib.sha256((directory / "review.md").read_bytes()).hexdigest()
        ready = call_native_tool(
            server.REVIEW_JOB_TOOL,
            {
                **self.roots(fixture),
                "review": {
                    "kind": "record-ready",
                    "attempt_id": "work-a-1",
                    "candidate_revision": fixture.candidate_revision,
                    "candidate_snapshot_sha256": context.reference.content_sha256,
                    "accepted_brief_sha256": attempt.brief_reference.content_sha256,
                    "result_sha256": result_digest,
                    "review_sha256": review_digest,
                    "reviewer_task_id": "independent-reviewer",
                    "verdict": "ready",
                    "acceptance_evidence": "Reviewed the exact candidate.",
                },
            },
        )
        self.assertEqual("recorded", ready["status"], ready)
        return result_digest, review_digest

    def test_completion_and_direct_close_sources(self) -> None:
        fixture = self.terminalize_brief(self.candidate(committed=True))
        result_digest, review_digest = self.record_ready(fixture)
        self.transition(
            fixture,
            "complete:work-a-1",
            {
                "schema": "pinboard-reviewed-completion/v2",
                "candidate": fixture.candidate_revision,
                "evidence": "Completed the reviewed change.",
                "reviewer_task_id": "independent-reviewer",
                "result_sha256": result_digest,
                "review_sha256": review_digest,
                "packages": [],
            },
        )
        self.git(fixture.project, "switch", "main")
        self.git(fixture.project, "merge", "--ff-only", fixture.brief.branch)
        self.assert_presence(fixture, "content-present", kind="completion")
        self.transition(fixture, "close:work-c", {"outcome": "done", "reason": "Direct human closure."})
        self.rejection(fixture, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input", item="work-c")
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE transition_history SET outcome_json = '{}' WHERE outcome_schema = 'completion-acceptance/v2'"
            )
        self.rejection(fixture, "TRANSITION_RECEIPT_DAMAGED", "do-not-retry")

    def test_retained_checkpoint_without_snapshot_is_unavailable(self) -> None:
        fixture = self.accepted_package_fixture(local=True, candidate_form="current-head")
        self.retain_v2_checkpoint(fixture)
        self.rejection(
            fixture, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input", target=fixture.brief.base_revision
        )

    def test_retained_pre_snapshot_review_is_unavailable(self) -> None:
        fixture = self.candidate()
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE transition_history SET input_schema = 'decision/v1', input_json = '{}', artifact_ref_id = NULL, artifact_kind = NULL WHERE history_id = ?",
                (context.receipt.history_id,),
            )
            connection.execute(
                "DELETE FROM artifact_refs WHERE artifact_ref_id = ?", (int(context.reference.artifact_ref_id),)
            )
        self.rejection(fixture, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")

    def test_accepted_and_continued_candidate_is_unavailable(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        self.transition(
            fixture,
            "accept-review-and-continue:work-a-1",
            {"candidate": fixture.candidate_revision, "evidence": "Accepted; continue."},
        )
        self.rejection(
            fixture, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input", target=fixture.brief.base_revision
        )

    def test_damaged_checkpoint_receipt_is_diagnosed(self) -> None:
        fixture = self.accepted_package_fixture(local=True)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE transition_history SET outcome_json = '{}' WHERE outcome_schema = 'checkpoint-acceptance/v2'"
            )
        self.rejection(fixture, "TRANSITION_RECEIPT_DAMAGED", "do-not-retry", target=fixture.brief.base_revision)
