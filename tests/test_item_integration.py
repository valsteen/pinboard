"""Native content observations over real Git trees and candidate transitions."""

import contextlib
import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Literal, override
from unittest.mock import patch

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files import root
from pinboard.adapters.files.errors import RootError
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import candidate_snapshots, item_integration, query_models
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import common, execution, server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import JsonObject, JsonValue

FIXED_NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)


class ItemIntegrationTest(CheckpointPackageSupport):
    @override
    def setUp(self) -> None:
        dates = patch.dict(
            os.environ,
            {"GIT_AUTHOR_DATE": "2030-01-02T03:04:05+00:00", "GIT_COMMITTER_DATE": "2030-01-02T03:04:05+00:00"},
        )
        dates.start()
        self.addCleanup(dates.stop)
        clock = patch("tests.checkpoint_support.datetime")
        fixed = clock.start()
        fixed.now.return_value = FIXED_NOW
        self.addCleanup(clock.stop)

    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def git(self, fixture: CheckpointFixture, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=Pinboard Tests", "-c", "user.email=pinboard@example.invalid", *arguments],
            cwd=fixture.project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def transition(self, fixture: CheckpointFixture, action: str, payload: JsonObject) -> JsonObject:
        result = self.transition_result(fixture, self.project_action(fixture, action), payload)
        self.assertIn(result["status"], ("committed", "committed-with-warning"), result)
        return result

    def fixture(
        self,
        form: Literal["working-tree", "current-head"] = "working-tree",
        *,
        empty: bool = False,
        context: bool = False,
        multiline: bool = False,
        complex_diff: bool = False,
    ) -> CheckpointFixture:
        fixture = self.checkpoint_fixture(local=True, candidate_form=form, committed_context=context)
        self.addCleanup(shutil.rmtree, fixture.project)
        self.transition(
            fixture, "return-for-correction:work-a-1", {"reason": "Prepare the candidate through native submission."}
        )
        if form == "current-head":
            self.git(fixture, "reset", "--hard", fixture.brief.base_revision)
        if multiline:
            (fixture.project / "tracked.txt").write_text("base\n" + "context\n" * 20, encoding="utf-8")
            self.commit_all(fixture.project, "preimage context")
        (fixture.project / "tracked.txt").write_text(
            ("base\n" if empty else "reviewed content\n") + ("context\n" * 20 if multiline else ""), encoding="utf-8"
        )
        if complex_diff:
            self.git(fixture, "mv", "tracked.txt", "renamed.txt")
            (fixture.project / "renamed.txt").write_text("reviewed with whitespace  \n", encoding="utf-8")
            (fixture.project / "binary.bin").write_bytes(bytes(range(256)))
        if form == "current-head" and not empty:
            candidate = self.commit_all(fixture.project, "reviewed change")
        else:
            observed = call_native_tool(
                server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"}
            )
            candidate = observed["candidate"]
            assert isinstance(candidate, str), observed
        lease = self.native_attempt_acquire(fixture, "native-integration-worker")
        action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        submitted = self.transition_result(fixture, action, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)
        context_facts = SQLiteWorkStore(fixture.work / "state.sqlite3").read_candidate_snapshot_context(
            AttemptId("work-a-1")
        )
        assert context_facts is not None
        snapshot = candidate_snapshots.decode_candidate_snapshot(
            (fixture.work / context_facts.reference.selector).read_bytes()
        )
        return replace(fixture, candidate_revision=candidate, candidate_bytes=snapshot.diff)

    def integration(self, fixture: CheckpointFixture, target: str, item: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": item, "target": target}},
        )

    def assert_presence(
        self, fixture: CheckpointFixture, target: str, expected: str, source_kind: str = "protected-review"
    ) -> JsonObject:
        result = self.integration(fixture, target)
        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual(expected, result["presence"], result)
        self.assertEqual(target, result["target"])
        self.assertEqual(self.git(fixture, "rev-parse", f"{target}^{{commit}}"), result["target_revision"])
        source = self.json_object(result["source"])
        self.assertEqual(source_kind, source["kind"])
        self.assertEqual("work-a-1", source["attempt_id"])
        self.assertEqual(fixture.candidate_revision, source["candidate_revision"])
        self.assertIn("compared_from_revision", source)
        return result

    def assert_rejection(self, result: JsonObject, code: str, retry: str) -> None:
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual("rejected", result["status"])
        self.assertEqual(code, result["code"])
        self.assertFalse(result["state_changed"])
        self.assertEqual("unchanged", result["effect"])
        self.assertEqual(retry, result["retry"])
        self.assertEqual([], result["changed_surfaces"])
        self.assertIn("observed", result)

    def test_native_protected_commit_recognizes_fast_forward_merge_and_rebase(self) -> None:
        for merge in ("fast-forward", "merge-commit", "rebase"):
            with self.subTest(merge=merge):
                fixture = self.fixture("current-head")
                candidate = fixture.candidate_revision
                base = fixture.brief.base_revision
                self.git(fixture, "checkout", "-b", "target", base)
                if merge == "fast-forward":
                    self.git(fixture, "merge", "--ff-only", candidate)
                else:
                    (fixture.project / "unrelated.txt").write_text("target-only change\n", encoding="utf-8")
                    self.commit_all(fixture.project, "target context")
                    if merge == "merge-commit":
                        self.git(fixture, "merge", "--no-ff", "-m", "integrate candidate", candidate)
                        self.assertEqual(
                            3, len(self.git(fixture, "rev-list", "--parents", "-n", "1", "target").split())
                        )
                    else:
                        self.git(fixture, "checkout", fixture.brief.branch)
                        self.git(fixture, "rebase", "target")
                        self.git(fixture, "checkout", "target")
                        self.git(fixture, "merge", "--ff-only", fixture.brief.branch)
                        ancestor = subprocess.run(
                            ["git", "merge-base", "--is-ancestor", candidate, "target"],
                            cwd=fixture.project,
                            capture_output=True,
                            check=False,
                        )
                        self.assertEqual(1, ancestor.returncode)
                result = self.assert_presence(fixture, "target", "content-present")
                self.assertEqual(base, self.json_object(result["source"])["compared_from_revision"])
                self.assert_presence(fixture, base, "content-not-present")

    def test_working_tree_squash_uses_actual_preimage_and_later_edit_distinction(self) -> None:
        fixture = self.fixture(context=True, multiline=True)
        preimage = self.git(fixture, "rev-parse", "HEAD")
        committed = self.commit_all(fixture.project, "commit reviewed working tree")
        self.git(fixture, "checkout", "-b", "target", fixture.brief.base_revision)
        self.git(fixture, "merge", "--squash", committed)
        self.commit_all(fixture.project, "squash reviewed change")
        result = self.assert_presence(fixture, "target", "content-present")
        self.assertEqual(preimage, self.json_object(result["source"])["compared_from_revision"])
        self.git(fixture, "update-ref", "refs/remotes/origin/main", "target")
        self.assert_presence(fixture, "origin/main", "content-present")
        # The later edit is beyond the recorded hunk's context in the same file.
        (fixture.project / "tracked.txt").write_text(
            "reviewed content\n" + "context\n" * 19 + "later independent content\n", encoding="utf-8"
        )
        self.commit_all(fixture.project, "later non-overlapping edit")
        self.assert_presence(fixture, "target", "content-present")
        (fixture.project / "tracked.txt").write_text(
            "overlapping replacement\n" + "context\n" * 19 + "later independent content\n", encoding="utf-8"
        )
        self.commit_all(fixture.project, "later overlapping edit")
        self.assert_presence(fixture, "target", "content-not-present")

    def test_native_squash_compares_rename_and_binary_snapshot_bytes(self) -> None:
        fixture = self.fixture(complex_diff=True)
        committed = self.commit_all(fixture.project, "commit rename and binary")
        self.git(fixture, "checkout", "-b", "target", fixture.brief.base_revision)
        self.git(fixture, "merge", "--squash", committed)
        self.commit_all(fixture.project, "squash rename and binary")
        self.git(fixture, "config", "apply.whitespace", "error")
        self.assert_presence(fixture, "target", "content-present")
        self.assert_presence(fixture, fixture.brief.base_revision, "content-not-present")

    def test_accepted_and_continued_candidate_is_not_an_integration_source(self) -> None:
        fixture = self.fixture()
        self.transition(
            fixture,
            "accept-review-and-continue:work-a-1",
            {"candidate": fixture.candidate_revision, "evidence": "Accept and continue work."},
        )
        result = self.integration(fixture, "HEAD")
        self.assert_rejection(result, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.assertIn({"field": "state", "value": "active"}, self.json_array(result["observed"]))
        self.assertIn("no protected candidate and no checkpoint acceptance", str(result["message"]))
        self.assertIn("operation item", str(result["recovery"]))

    def test_retained_candidates_without_snapshot_bytes_are_unavailable(self) -> None:
        fixture = self.fixture()
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "DELETE FROM artifact_refs WHERE artifact_ref_id = ?", (int(context.reference.artifact_ref_id),)
            )
        result = self.integration(fixture, "HEAD")
        self.assert_rejection(result, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.assertIn("predates accepted snapshot", str(result["message"]))
        self.assertIn({"field": "item_id", "value": "work-a"}, self.json_array(result["observed"]))
        self.assertIn("operation item", str(result["recovery"]))
        checkpoint = self.accepted_package_fixture(local=True, candidate_form="current-head")
        self.addCleanup(shutil.rmtree, checkpoint.project)
        self.retain_v2_checkpoint(checkpoint)
        legacy = self.integration(checkpoint, "HEAD")
        self.assert_rejection(legacy, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.assertIn("no candidate snapshot reference", str(legacy["message"]))
        self.assertIn({"field": "state", "value": "paused"}, self.json_array(legacy["observed"]))
        self.assertIn("operation item", str(legacy["recovery"]))

    def test_integration_trace_uses_the_named_items_override(self) -> None:
        fixture = self.fixture()
        self.git(
            fixture,
            "config",
            "--file",
            str(fixture.work / "contributor-traces.config"),
            "pinboard.unsafe_persist_exact_pinboard_traces.mode",
            "off",
        )
        self.git(fixture, "config", "--file", str(fixture.work / "contributor-traces.config"), "item.work-a.mode", "on")
        with patch(
            "tests.native_support.mcp_server.create_server",
            partial(server.create_server, capture=execution.AutomaticCapture(common.select_capture_item)),
        ):
            expected = self.assert_presence(fixture, "HEAD", "content-not-present")
            self.integration(fixture, "HEAD", "work-c")
        traces = tuple((fixture.work / "invocation-traces").glob("pinboard-auto-mcp-*.json"))
        self.assertEqual(1, len(traces))
        captured = self.json_object(msgspec.json.decode(traces[0].read_bytes()))
        self.assertEqual(expected, self.json_object(captured["result"])["value"])
        self.assertEqual(traces, tuple((fixture.work / "invocation-traces").glob("pinboard-auto-mcp-*.json")))

    def test_git_effect_failure_returns_an_unchanged_checkout_diagnosis(self) -> None:
        fixture = self.fixture()
        with patch(
            "pinboard.adapters.files.root.TemporaryDirectory", side_effect=PermissionError("Temporary directory denied")
        ):
            result = self.integration(fixture, "HEAD")
        self.assert_rejection(result, "PROJECT_GIT_CHECKOUT_UNAVAILABLE", "correct-input")
        self.assertIn({"field": "project_root", "value": str(fixture.project)}, self.json_array(result["observed"]))
        self.assertIn(
            {"field": "diagnostic", "value": "PROJECT_GIT_CHECKOUT_UNAVAILABLE: Temporary directory denied"},
            self.json_array(result["observed"]),
        )
        self.assertIn("Correct the named Git checkout", str(result["recovery"]))

    def test_missing_target_blob_returns_native_checkout_diagnosis_without_writes(self) -> None:
        fixture = self.fixture("current-head")
        self.assert_presence(fixture, "HEAD", "content-present")
        blob = self.git(fixture, "rev-parse", "HEAD:tracked.txt")
        (fixture.project / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
        before = {
            str(path.relative_to(fixture.project)): path.read_bytes()
            for path in fixture.project.rglob("*")
            if path.is_file()
        }
        with tempfile.TemporaryDirectory() as temporary:
            with patch("tempfile.tempdir", temporary):
                result = self.integration(fixture, "HEAD")
            self.assertEqual([], list(Path(temporary).iterdir()))
        self.assert_rejection(result, "PROJECT_GIT_CHECKOUT_UNAVAILABLE", "correct-input")
        self.assertIn({"field": "project_root", "value": str(fixture.project)}, self.json_array(result["observed"]))
        diagnostic = str(result["message"])
        self.assertIn("failed to read tracked.txt", diagnostic)
        self.assertIn("tracked.txt: patch does not apply", diagnostic)
        self.assertIn({"field": "diagnostic", "value": diagnostic}, self.json_array(result["observed"]))
        self.assertIn("Correct the named Git checkout", str(result["recovery"]))
        after = {
            str(path.relative_to(fixture.project)): path.read_bytes()
            for path in fixture.project.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_later_binary_edit_remains_an_ordinary_content_mismatch(self) -> None:
        fixture = self.fixture("current-head", complex_diff=True)
        (fixture.project / "binary.bin").write_bytes(bytes(reversed(range(256))))
        self.commit_all(fixture.project, "later overlapping binary edit")
        self.assert_presence(fixture, "HEAD", "content-not-present")

    def test_later_content_and_mode_edit_remains_an_ordinary_content_mismatch(self) -> None:
        fixture = self.fixture("current-head")
        tracked = fixture.project / "tracked.txt"
        tracked.chmod(0o755)
        tracked.write_text("later overlapping content\n", encoding="utf-8")
        self.commit_all(fixture.project, "later content and mode edit")
        self.assert_presence(fixture, "HEAD", "content-not-present")

    def test_checkpoint_remains_source_after_resume_rebind_and_return_until_submission(self) -> None:
        fixture = self.fixture()
        accepted = self.transition(
            fixture,
            "accept-checkpoint:work-a-1",
            {
                "checkpoint": fixture.brief.checkpoint.checkpoint_id,
                "candidate": fixture.candidate_revision,
                "evidence": "Accept reviewed checkpoint.",
            },
        )
        self.assertEqual("committed", accepted["status"])
        committed = self.commit_all(fixture.project, "checkpoint candidate")
        self.git(fixture, "checkout", "-b", "target", fixture.brief.base_revision)
        self.git(fixture, "merge", "--squash", committed)
        self.commit_all(fixture.project, "squash checkpoint")
        source = self.json_object(
            self.assert_presence(fixture, "target", "content-present", "accepted-checkpoint")["source"]
        )
        self.assertEqual(fixture.brief.checkpoint.checkpoint_id, source["checkpoint_id"])
        self.git(fixture, "checkout", fixture.brief.branch)
        code, stdout, stderr = self.run_cli(
            *fixture.common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "Dependency satisfied.",
            "--task-id",
            "test-human",
            "--host-id",
            "local",
        )
        self.assertEqual(0, code, f"{stdout}\n{stderr}")
        self.transition(fixture, "resume:work-a", {})
        self.assert_presence(fixture, "target", "content-present", "accepted-checkpoint")
        published = call_native_tool(
            server.BRIEF_PUBLISH_TOOL,
            {
                **self.roots(fixture),
                "brief": self.json_object(msgspec.to_builtins(replace_struct(fixture.brief, artifact_revision=2))),
            },
        )
        reference = self.json_object(published["reference"])
        self.transition(
            fixture,
            "rebind-attempt:work-a-1",
            {
                "branch": fixture.brief.branch,
                "base_revision": fixture.brief.base_revision,
                "brief_artifact_ref_id": reference["artifact_ref_id"],
            },
        )
        self.assert_presence(fixture, "target", "content-present", "accepted-checkpoint")
        (fixture.project / "tracked.txt").write_text("new submission\n", encoding="utf-8")
        observed = call_native_tool(server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"})
        lease = self.native_attempt_acquire(fixture, "checkpoint-continuation-worker")
        self.assertEqual(
            "committed",
            self.transition_result(
                fixture,
                self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease),
                {"candidate": observed["candidate"]},
            )["status"],
        )
        fresh = replace(fixture, candidate_revision=str(observed["candidate"]))
        self.assert_presence(fresh, "target", "content-not-present")
        self.transition(fixture, "return-for-correction:work-a-1", {"reason": "Return the later candidate."})
        self.assert_presence(fixture, "target", "content-present", "accepted-checkpoint")

    def test_completion_selects_closing_candidate_and_inspection_runs_no_integration_read(self) -> None:
        fixture = self.terminalize_brief(self.fixture("current-head"))
        attempt_root = fixture.work / "attempts" / "work-a-1"
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        attempt = fixture.store.read_attempt_context(AttemptId("work-a-1"))
        assert context is not None and isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        ready = call_advertised_tool(
            server.REVIEW_JOB_TOOL,
            {
                **self.roots(fixture),
                "review": {
                    "kind": "record-ready",
                    "attempt_id": "work-a-1",
                    "candidate_revision": fixture.candidate_revision,
                    "candidate_snapshot_sha256": context.reference.content_sha256,
                    "accepted_brief_sha256": attempt.brief_reference.content_sha256,
                    "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
                    "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
                    "reviewer_task_id": "independent-reviewer",
                    "verdict": "ready",
                    "acceptance_evidence": "Reviewed exact candidate.",
                },
            },
        )
        self.assertEqual("recorded", ready["status"], ready)
        effects: list[JsonValue] = [
            {"effect": effect, "status": "allowed" if required else "not-required"}
            for effect, required in (("source-checkout", False), ("shared-work-root", True), ("git-metadata", False))
        ]
        with patch(
            "pinboard.adapters.files.root.read_target_content",
            side_effect=AssertionError("Inspection must not read integration content."),
        ):
            inspected = call_advertised_tool(
                server.ATTEMPT_INSPECT_TOOL,
                {
                    **self.roots(fixture),
                    "attempt_id": "work-a-1",
                    "reconciliation": {
                        "target_revision": fixture.candidate_revision,
                        "relation": "candidate-integrated",
                        "phase": "terminal",
                        "effects": effects,
                    },
                },
            )
        self.assertEqual("ok", inspected["status"], inspected)
        self.transition(
            fixture,
            "complete:work-a-1",
            {
                "schema": "pinboard-reviewed-completion/v2",
                "candidate": fixture.candidate_revision,
                "evidence": "Complete the reviewed outcome.",
                "reviewer_task_id": "independent-reviewer",
                "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
                "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
                "packages": [],
            },
        )
        self.assert_presence(fixture, fixture.candidate_revision, "content-present", "completion")
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE transition_history SET outcome_json = 'damaged completion' WHERE action_kind = 'complete'"
            )
        damaged = self.integration(fixture, fixture.candidate_revision)
        self.assert_rejection(damaged, "TRANSITION_RECEIPT_DAMAGED", "do-not-retry")
        self.assertIn({"field": "action_kind", "value": "complete"}, self.json_array(damaged["observed"]))
        self.assertIn("Report to the human", str(damaged["recovery"]))

    def test_empty_diff_resolves_target_and_skips_only_content_comparison(self) -> None:
        fixture = self.fixture(empty=True)
        with patch(
            "pinboard.adapters.files.root.TemporaryDirectory",
            side_effect=AssertionError("No comparison for empty bytes."),
        ):
            self.assert_presence(fixture, "HEAD", "no-change")
            rejected = self.integration(fixture, "missing-ref")
        self.assert_rejection(rejected, "INTEGRATION_TARGET_UNRESOLVED", "correct-input")

    def test_native_expected_rejections_preserve_observations_and_safe_next_steps(self) -> None:
        fixture = self.fixture()
        missing = self.integration(fixture, "missing-ref")
        self.assert_rejection(missing, "INTEGRATION_TARGET_UNRESOLVED", "correct-input")
        self.assertIn("Fetch it outside Pinboard", str(missing["recovery"]))
        self.assertIn({"field": "target", "value": "missing-ref"}, self.json_array(missing["observed"]))
        invalid = call_native_tool(
            server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": "work-a", "target": "-bad"}},
        )
        self.assert_rejection(invalid, "ITEM_STATUS_INVALID", "correct-input")
        for target in ("", "main\nother", "main\rother", "main\x00other"):
            with self.subTest(target=target):
                invalid = call_native_tool(
                    server.ITEM_STATUS_TOOL,
                    {
                        "request": {
                            **self.roots(fixture),
                            "operation": "integration",
                            "item_id": "work-a",
                            "target": target,
                        }
                    },
                )
                self.assert_rejection(invalid, "ITEM_STATUS_INVALID", "correct-input")
        self.assert_rejection(self.integration(fixture, "HEAD", "unknown"), "ITEM_NOT_FOUND", "correct-input")
        ready = self.integration(fixture, "HEAD", "work-c")
        self.assert_rejection(ready, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.assertIn("operation item", str(ready["recovery"]))
        self.transition(
            fixture, "return-for-correction:work-a-1", {"reason": "The protected candidate must cease to be a source."}
        )
        returned = self.integration(fixture, "HEAD")
        self.assert_rejection(returned, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        closed, stdout, stderr = self.run_cli(
            *fixture.common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "Direct human close.",
            "--task-id",
            "test-human",
            "--host-id",
            "local",
        )
        self.assertEqual(0, closed, f"{stdout}\n{stderr}")
        direct = self.integration(fixture, "HEAD", "work-c")
        self.assert_rejection(direct, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.assertIn("closed directly", str(direct["message"]))

    def test_damaged_snapshot_and_checkpoint_receipt_are_typed_diagnoses(self) -> None:
        fixture = self.fixture()
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        (fixture.work / context.reference.selector).write_bytes(b"damaged accepted bytes")
        invalid = self.integration(fixture, "HEAD")
        self.assert_rejection(invalid, "INTEGRATION_CANDIDATE_EVIDENCE_INVALID", "do-not-retry")
        self.assertIn("pinboard validate", str(invalid["recovery"]))
        self.assertIn({"field": "attempt_id", "value": "work-a-1"}, self.json_array(invalid["observed"]))
        other = self.fixture()
        receipt = self.transition(
            other,
            "accept-checkpoint:work-a-1",
            {
                "checkpoint": other.brief.checkpoint.checkpoint_id,
                "candidate": other.candidate_revision,
                "evidence": "Accepted checkpoint.",
            },
        )
        with contextlib.closing(sqlite3.connect(other.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE transition_history SET outcome_json = 'invalid' WHERE history_id = ?", (receipt["history_id"],)
            )
        damaged = self.integration(other, "HEAD")
        self.assert_rejection(damaged, "TRANSITION_RECEIPT_DAMAGED", "do-not-retry")
        self.assertIn(str(receipt["history_id"]), str(damaged["recovery"]))

    def test_non_git_project_returns_the_root_diagnostic(self) -> None:
        fixture = self.fixture()
        with tempfile.TemporaryDirectory() as nongit:
            result = call_advertised_tool(
                server.ITEM_STATUS_TOOL,
                {
                    "request": {
                        "project_root": nongit,
                        "work_root": str(fixture.work),
                        "operation": "integration",
                        "item_id": "work-a",
                        "target": "HEAD",
                    }
                },
            )
        self.assert_rejection(result, "PROJECT_GIT_ROOT_UNAVAILABLE", "correct-input")
        self.assertIn("Correct the named Git checkout", str(result["recovery"]))

    def test_git_read_is_read_only_with_whitespace_split_index_rename_and_binary_content(self) -> None:
        fixture = self.fixture("current-head")
        self.git(fixture, "mv", "tracked.txt", "renamed.txt")
        (fixture.project / "renamed.txt").write_text("reviewed content with whitespace  \n", encoding="utf-8")
        (fixture.project / "binary.bin").write_bytes(bytes(range(256)))
        commit = self.commit_all(fixture.project, "rename binary and whitespace")
        diff = subprocess.run(
            ["git", "diff", "--binary", fixture.brief.base_revision, commit],
            cwd=fixture.project,
            capture_output=True,
            check=True,
        ).stdout
        self.git(fixture, "config", "apply.whitespace", "error")
        self.git(fixture, "config", "core.splitIndex", "true")
        gitdir = fixture.project / ".git"
        before = {
            str(path.relative_to(fixture.project)): path.read_bytes()
            for path in fixture.project.rglob("*")
            if path.is_file()
        }
        modes = {path: path.stat().st_mode & 0o777 for path in gitdir.rglob("*")}
        modes[gitdir] = gitdir.stat().st_mode & 0o777
        with tempfile.TemporaryDirectory() as temporary:
            try:
                for path in modes:
                    path.chmod(0o555 if path.is_dir() else 0o444)
                with patch("tempfile.tempdir", temporary):
                    observed = root.read_target_content(fixture.project, "HEAD", diff)
                    self.assertIsInstance(observed, root.TargetContentObservation)
                    assert isinstance(observed, root.TargetContentObservation)
                    self.assertEqual(item_integration.IntegrationPresence.CONTENT_PRESENT, observed.presence)
                    absent = root.read_target_content(fixture.project, fixture.brief.base_revision, diff)
                    assert isinstance(absent, root.TargetContentObservation)
                    self.assertEqual(item_integration.IntegrationPresence.CONTENT_NOT_PRESENT, absent.presence)
                    self.assertIsInstance(
                        root.read_target_content(fixture.project, "absent", diff), root.UnresolvedIntegrationTarget
                    )
                self.assertEqual([], list(Path(temporary).iterdir()))
                after = {
                    str(path.relative_to(fixture.project)): path.read_bytes()
                    for path in fixture.project.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(before, after)
            finally:
                for path, mode in modes.items():
                    path.chmod(mode)
        with tempfile.TemporaryDirectory() as nongit, self.assertRaises(RootError):
            root.read_target_content(Path(nongit), "HEAD", diff)

    def test_user_ignore_whitespace_config_does_not_change_context_verdict(self) -> None:
        fixture = self.fixture("current-head", multiline=True)
        (fixture.project / "tracked.txt").write_text(
            "reviewed content\n indented context\n" + "context\n" * 19, encoding="utf-8"
        )
        self.commit_all(fixture.project, "change reviewed context")
        baseline = root.read_target_content(fixture.project, "HEAD", fixture.candidate_bytes)
        assert isinstance(baseline, root.TargetContentObservation)
        self.assertEqual(item_integration.IntegrationPresence.CONTENT_NOT_PRESENT, baseline.presence)
        self.git(fixture, "config", "apply.ignoreWhitespace", "change")
        self.assertEqual(baseline, root.read_target_content(fixture.project, "HEAD", fixture.candidate_bytes))

    def test_integration_uses_keyed_reads_without_status_verdict_or_retained_scan(self) -> None:
        fixture = self.fixture()
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            for number in range(25):
                unrelated = f"retained-{number}"
                connection.execute(
                    "INSERT INTO attempts SELECT ?, 'work-c', 'done', 'retained/' || ?, base_revision, provenance, brief_artifact_ref_id, brief_artifact_kind, result_artifact_ref_id, result_artifact_kind, candidate_revision, candidate_recorded_at, accepted_scope_revision, accepted_scope_digest, subject_revision, recorded_at, updated_at FROM attempts WHERE attempt_id = 'work-a-1'",
                    (unrelated, unrelated),
                )
                connection.execute(
                    "INSERT INTO artifact_refs SELECT (SELECT max(artifact_ref_id) + 1 FROM artifact_refs), ?, artifact_revision, kind, ?, content_sha256, size_bytes, accepted_revision, created_at FROM artifact_refs WHERE kind = 'evidence' LIMIT 1",
                    (unrelated + "-review-package", "artifacts/unrelated/" + unrelated + ".json"),
                )
                reference = connection.execute("SELECT max(artifact_ref_id) FROM artifact_refs").fetchone()[0]
                connection.execute(
                    "INSERT INTO transition_history(history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id, artifact_kind, authorization_kind, actor_task_id, actor_host_id, input_schema, input_json, outcome_schema, outcome_json, committed_at) VALUES ((SELECT max(history_id) + 1 FROM transition_history), (SELECT max(project_revision) + 1 FROM transition_history), ?, 'accept-checkpoint', ?, ?, 'evidence', 'project', 'test', 'local', 'decision/v2', '{}', 'checkpoint-acceptance/v2', 'unconsumed damaged bytes', ?)",
                    ("accept-checkpoint:" + unrelated, unrelated, reference, FIXED_NOW.isoformat()),
                )
                artifact = fixture.work / "artifacts" / "unrelated" / (unrelated + ".json")
                artifact.parent.mkdir(exist_ok=True)
                artifact.write_bytes(b"unconsumed damaged artifact")
        statements: list[str] = []

        original_open = sqlite_store.open_database

        def traced_open(path: Path, mode: sqlite_store.OpenMode) -> sqlite3.Connection:
            connection = original_open(path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with (
            patch("pinboard.adapters.sqlite.store.open_database", traced_open),
            patch(
                "pinboard.adapters.sqlite.lifecycle._read_review_event", side_effect=AssertionError("No verdict walk.")
            ),
            patch(
                "pinboard.adapters.sqlite.store._read_attempt_context_facts",
                side_effect=AssertionError("No broad attempt context."),
            ),
        ):
            self.assert_presence(fixture, "HEAD", "content-not-present")
        self.assertFalse(
            any(
                "work_item_definition_revisions" in statement or "FROM transition_history" in statement
                for statement in statements
            )
        )
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT history_id FROM transition_history INDEXED BY checkpoint_history_by_subject WHERE subject_id = ? AND outcome_schema = 'checkpoint-acceptance/v2' ORDER BY history_id DESC LIMIT 1",
                ("work-a-1",),
            ).fetchall()
            self.assertTrue(
                any("SEARCH" in str(row) and "checkpoint_history_by_subject" in str(row) for row in plan), plan
            )
            for statement in statements:
                if any(f"FROM {table}" in statement for table in ("work_items", "attempts", "artifact_refs")):
                    indexed = connection.execute("EXPLAIN QUERY PLAN " + statement).fetchall()
                    self.assertTrue(any("SEARCH" in str(row) for row in indexed), indexed)
                    self.assertFalse(any("SCAN" in str(row) for row in indexed), indexed)
        with patch(
            "pinboard.adapters.files.root.read_target_content",
            side_effect=AssertionError("Ordinary reads must stay Git-content-free."),
        ):
            ordinary_reads: tuple[tuple[str, JsonObject], ...] = (
                (
                    server.ITEM_STATUS_TOOL,
                    {"request": {**self.roots(fixture), "operation": "item", "item_id": "work-a"}},
                ),
                (
                    server.ITEM_STATUS_TOOL,
                    {"request": {**self.roots(fixture), "operation": "branch", "branch": fixture.brief.branch}},
                ),
                (server.OVERVIEW_TOOL, self.roots(fixture)),
            )
            for name, arguments in ordinary_reads:
                result = call_advertised_tool(name, arguments)
                self.assertNotEqual("rejected", result.get("status"), result)
