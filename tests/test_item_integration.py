"""The item-status integration leaf reports recorded-candidate content presence in a named target."""

import contextlib
import hashlib
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite import state as sqlite_state
from pinboard.adapters.sqlite.database import OpenMode, open_database
from pinboard.application import (
    candidate_snapshots,
    checkpoint_compatibility_models,
    stored_state,
    work_brief_models,
)
from pinboard.application.candidate_identity import working_tree_identity
from pinboard.domain import work_models
from pinboard.domain.identifiers import WorkItemId
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import SQLITE_NOW, JsonObject

FIXED_DATE = "2026-01-02T03:04:05+00:00"
COMPLETED_AT = datetime(2030, 1, 3, 4, 5, 6, tzinfo=UTC)


class ItemIntegrationTest(CheckpointPackageSupport):
    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def integration(self, fixture: CheckpointFixture, item_id: str, target: str) -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    **self.roots(fixture),
                    "operation": "integration",
                    "item_id": item_id,
                    "target": target,
                }
            },
        )

    def git_text(self, fixture: CheckpointFixture, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=fixture.project, check=True, text=True, capture_output=True).stdout

    def git_bytes(self, fixture: CheckpointFixture, *args: str) -> bytes:
        return subprocess.run(["git", *args], cwd=fixture.project, check=True, capture_output=True).stdout

    def commit(self, fixture: CheckpointFixture, message: str) -> str:
        environment = {
            **os.environ,
            "GIT_AUTHOR_DATE": FIXED_DATE,
            "GIT_COMMITTER_DATE": FIXED_DATE,
        }
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", message],
            cwd=fixture.project,
            check=True,
            capture_output=True,
            env=environment,
        )
        return self.git_text(fixture, "rev-parse", "HEAD").strip()

    def commit_candidate(self, fixture: CheckpointFixture) -> tuple[str, str]:
        """Commit the fixture's dirty candidate change; return (base, candidate) commits."""

        self.git_text(fixture, "add", "--all")
        candidate = self.commit(fixture, "candidate")
        base = self.git_text(fixture, "rev-parse", "HEAD^").strip()
        return base, candidate

    def integrated_branch(self, fixture: CheckpointFixture, shape: str, base: str, candidate: str) -> str:
        """Build a branch containing the candidate content through the named integration shape."""

        branch = f"integration-{shape}"
        match shape:
            case "fast-forward":
                self.git_text(fixture, "branch", branch, candidate)
            case "merge":
                self.git_text(fixture, "switch", "-c", branch, base)
                (fixture.project / "other.txt").write_text("unrelated\n", encoding="utf-8")
                self.git_text(fixture, "add", "other.txt")
                self.commit(fixture, "unrelated context")
                self.git_text(fixture, "merge", "--no-ff", candidate, "-m", "integrate the candidate")
            case "rebase-merge":
                self.git_text(fixture, "switch", "-c", branch, base)
                (fixture.project / "other.txt").write_text("unrelated\n", encoding="utf-8")
                self.git_text(fixture, "add", "other.txt")
                self.commit(fixture, "unrelated context")
                self.git_text(fixture, "cherry-pick", candidate)
            case "squash":
                self.git_text(fixture, "switch", "-c", branch, base)
                self.git_text(fixture, "merge", "--squash", candidate)
                self.commit(fixture, "integrate the candidate")
        return branch

    def assert_integration(
        self,
        result: JsonObject,
        _fixture: CheckpointFixture,
        *,
        target: str,
        target_revision: str | None,
        source: JsonObject,
        presence: str,
    ) -> None:
        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual("sqlite-v7", result["authority"], result)
        self.assertEqual("work-a", result["item_id"], result)
        self.assertEqual(target, result["target"], result)
        self.assertEqual(target_revision, result["target_revision"], result)
        self.assertEqual(source, result["source"], result)
        self.assertEqual(presence, result["presence"], result)

    def assert_rejection(
        self,
        result: JsonObject,
        code: str,
        *,
        retry: str,
        observed: JsonObject,
    ) -> None:
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual("rejected", result["status"], result)
        self.assertEqual(code, result["code"], result)
        self.assertEqual("unchanged", result["effect"], result)
        self.assertEqual(retry, result["retry"], result)
        facts = {
            self.json_object(value)["field"]: self.json_object(value)["value"]
            for value in self.json_array(result["observed"])
        }
        self.assertEqual(observed, facts, result)

    def current_attempt_recorded_at(self, fixture: CheckpointFixture) -> datetime:
        state = fixture.store.validated_snapshot()
        attempt = next(value for value in state.lifecycle.attempts if str(value.attempt_id) == "work-a-1")
        assert attempt.candidate_recorded_at is not None
        return attempt.candidate_recorded_at

    def snapshot_reference(self, fixture: CheckpointFixture, candidate: str) -> stored_state.ArtifactReference:
        """Return the candidate snapshot artifact reference for the current attempt."""

        recorded_at = self.current_attempt_recorded_at(fixture)
        key = candidate_snapshots.candidate_snapshot_artifact_key("work-a-1", candidate, recorded_at.isoformat())
        reference = fixture.store.read_latest_artifact_reference(work_models.ArtifactKind.EVIDENCE, key)
        assert reference is not None
        return reference

    def test_protected_review_reports_content_presence_across_integration_shapes(self) -> None:
        for shape in ("fast-forward", "merge", "rebase-merge", "squash"):
            with self.subTest(shape=shape):
                fixture = self.checkpoint_fixture()
                base, candidate = self.commit_candidate(fixture)
                branch = self.integrated_branch(fixture, shape, base, candidate)
                resolved = self.git_text(fixture, "rev-parse", f"{branch}^{{commit}}").strip()

                result = self.integration(fixture, "work-a", branch)

                self.assert_integration(
                    result,
                    fixture,
                    target=branch,
                    target_revision=resolved,
                    source={
                        "kind": "protected-review",
                        "attempt_id": "work-a-1",
                        "candidate_revision": fixture.candidate_revision,
                        "compared_from_revision": base,
                    },
                    presence="content-present",
                )

    def test_protected_commit_candidate_reports_content_presence_across_integration_shapes(self) -> None:
        for shape in ("fast-forward", "merge", "rebase-merge"):
            with self.subTest(shape=shape):
                fixture = self.checkpoint_fixture(candidate_form="current-head")
                branch = self.integrated_branch(fixture, shape, fixture.brief.base_revision, fixture.candidate_revision)
                resolved = self.git_text(fixture, "rev-parse", f"{branch}^{{commit}}").strip()

                result = self.integration(fixture, "work-a", branch)

                self.assert_integration(
                    result,
                    fixture,
                    target=branch,
                    target_revision=resolved,
                    source={
                        "kind": "protected-review",
                        "attempt_id": "work-a-1",
                        "candidate_revision": fixture.candidate_revision,
                        "compared_from_revision": fixture.brief.base_revision,
                    },
                    presence="content-present",
                )

    def test_protected_review_reports_absent_content(self) -> None:
        fixture = self.checkpoint_fixture()
        base, candidate = self.commit_candidate(fixture)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            revision = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()[0]

        scenarios: list[tuple[str, str, str]] = []
        scenarios.append(("accepted-base", base, base))
        self.git_text(fixture, "switch", "-c", "integration-absent", base)
        (fixture.project / "other.txt").write_text("unrelated\n", encoding="utf-8")
        self.git_text(fixture, "add", "other.txt")
        absent = self.commit(fixture, "unrelated context")
        scenarios.append(("absent-change", "integration-absent", absent))
        self.git_text(fixture, "switch", "-c", "integration-overlap", candidate)
        (fixture.project / "tracked.txt").write_text("third\n", encoding="utf-8")
        self.git_text(fixture, "add", "tracked.txt")
        overlap = self.commit(fixture, "overlapping later edit")
        scenarios.append(("overlapping-edit", "integration-overlap", overlap))
        self.git_text(fixture, "switch", "-c", "integration-adjacent", candidate)
        (fixture.project / "adjacent.txt").write_text("adjacent\n", encoding="utf-8")
        self.git_text(fixture, "add", "adjacent.txt")
        adjacent = self.commit(fixture, "non-overlapping later edit")

        for name, target, resolved in scenarios:
            with self.subTest(scenario=name):
                self.assert_integration(
                    self.integration(fixture, "work-a", target),
                    fixture,
                    target=target,
                    target_revision=resolved,
                    source={
                        "kind": "protected-review",
                        "attempt_id": "work-a-1",
                        "candidate_revision": fixture.candidate_revision,
                        "compared_from_revision": base,
                    },
                    presence="content-not-present",
                )

        present = self.integration(fixture, "work-a", "integration-adjacent")
        self.assert_integration(
            present,
            fixture,
            target="integration-adjacent",
            target_revision=adjacent,
            source={
                "kind": "protected-review",
                "attempt_id": "work-a-1",
                "candidate_revision": fixture.candidate_revision,
                "compared_from_revision": base,
            },
            presence="content-present",
        )
        self.assertEqual(str(revision), present["revision"], present)

    def test_accepted_checkpoint_selects_the_checkpoint_candidate(self) -> None:
        fixture = self.accepted_package_fixture()
        base, candidate = self.commit_candidate(fixture)
        branch = self.integrated_branch(fixture, "squash", base, candidate)
        resolved = self.git_text(fixture, "rev-parse", f"{branch}^{{commit}}").strip()
        source: JsonObject = {
            "kind": "accepted-checkpoint",
            "attempt_id": "work-a-1",
            "candidate_revision": fixture.candidate_revision,
            "compared_from_revision": base,
            "checkpoint": fixture.brief.checkpoint.checkpoint_id,
        }

        paused = self.integration(fixture, "work-a", branch)

        self.assert_integration(
            paused,
            fixture,
            target=branch,
            target_revision=resolved,
            source=source,
            presence="content-present",
        )
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
        resumed = self.transition_result(fixture, self.project_action(fixture, "resume:work-a"), {})
        self.assertEqual("committed", resumed["status"], resumed)

        result = self.integration(fixture, "work-a", branch)

        self.assert_integration(
            result,
            fixture,
            target=branch,
            target_revision=resolved,
            source=source,
            presence="content-present",
        )

    def test_returned_for_correction_leaves_no_integration_candidate(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_correction(fixture, "The review found a gap.", "integration")

        result = self.integration(fixture, "work-a", "unused-target")

        self.assert_rejection(
            result,
            "INTEGRATION_CANDIDATE_UNAVAILABLE",
            retry="correct-input",
            observed={
                "item_id": "work-a",
                "item_state": "active",
                "reason": "no-protected-or-checkpoint",
            },
        )

    def test_accepted_and_continued_candidate_is_not_an_integration_source(self) -> None:
        fixture = self.checkpoint_fixture()
        continued = self.transition_result(
            fixture,
            self.project_action(fixture, "accept-review-and-continue:work-a-1"),
            {"candidate": fixture.candidate_revision, "evidence": "The review is favorable; continue."},
        )
        self.assertEqual("committed", continued["status"], continued)

        result = self.integration(fixture, "work-a", "unused-target")

        self.assert_rejection(
            result,
            "INTEGRATION_CANDIDATE_UNAVAILABLE",
            retry="correct-input",
            observed={
                "item_id": "work-a",
                "item_state": "active",
                "reason": "no-protected-or-checkpoint",
            },
        )

    def test_rebind_keeps_the_accepted_checkpoint_candidate(self) -> None:
        fixture = self.accepted_package_fixture()
        base, candidate = self.commit_candidate(fixture)
        branch = self.integrated_branch(fixture, "fast-forward", base, candidate)
        resolved = self.git_text(fixture, "rev-parse", f"{branch}^{{commit}}").strip()
        brief = replace_struct(fixture.brief, artifact_revision=fixture.brief.artifact_revision + 1)
        published = call_native_tool(
            mcp_server.BRIEF_PUBLISH_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "brief": self.json_object(msgspec.json.decode(msgspec.json.encode(brief))),
            },
        )
        self.assertEqual("committed", published["status"], published)
        reference = self.json_object(published["reference"])
        rebound = self.transition_result(
            fixture,
            self.project_action(fixture, "rebind-attempt:work-a-1"),
            {
                "branch": brief.branch,
                "base_revision": brief.base_revision,
                "brief_artifact_ref_id": reference["artifact_ref_id"],
            },
        )
        self.assertEqual("committed", rebound["status"], rebound)

        result = self.integration(fixture, "work-a", branch)

        self.assert_integration(
            result,
            fixture,
            target=branch,
            target_revision=resolved,
            source={
                "kind": "accepted-checkpoint",
                "attempt_id": "work-a-1",
                "candidate_revision": fixture.candidate_revision,
                "compared_from_revision": base,
                "checkpoint": fixture.brief.checkpoint.checkpoint_id,
            },
            presence="content-present",
        )

    def test_completion_selects_the_closing_candidate(self) -> None:
        fixture = self.checkpoint_fixture()
        fixture = self.terminalize_brief(fixture)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        with patch("pinboard.mcp.mutation_operations.datetime") as clock:
            clock.now.return_value = COMPLETED_AT
            completed = self.transition_result(
                fixture,
                self.project_action(fixture, "complete:work-a-1"),
                {
                    "schema": "pinboard-reviewed-completion/v2",
                    "candidate": fixture.candidate_revision,
                    "evidence": "Accepted and integrated.",
                    "reviewer_task_id": "independent-reviewer",
                    "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
                    "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
                    "packages": [],
                },
            )
        self.assertIn(completed["status"], ("committed", "committed-with-warning"), completed)
        base, candidate = self.commit_candidate(fixture)
        branch = self.integrated_branch(fixture, "fast-forward", base, candidate)
        resolved = self.git_text(fixture, "rev-parse", f"{branch}^{{commit}}").strip()

        result = self.integration(fixture, "work-a", branch)

        self.assert_integration(
            result,
            fixture,
            target=branch,
            target_revision=resolved,
            source={
                "kind": "completion",
                "attempt_id": "work-a-1",
                "candidate_revision": fixture.candidate_revision,
                "compared_from_revision": base,
            },
            presence="content-present",
        )

    def test_local_remote_tracking_ref_names_an_integrated_target(self) -> None:
        fixture = self.checkpoint_fixture()
        base, candidate = self.commit_candidate(fixture)
        branch = self.integrated_branch(fixture, "fast-forward", base, candidate)
        resolved = self.git_text(fixture, "rev-parse", f"{branch}^{{commit}}").strip()
        self.git_text(fixture, "update-ref", "refs/remotes/origin/main", resolved)

        result = self.integration(fixture, "work-a", "origin/main")

        self.assert_integration(
            result,
            fixture,
            target="origin/main",
            target_revision=resolved,
            source={
                "kind": "protected-review",
                "attempt_id": "work-a-1",
                "candidate_revision": fixture.candidate_revision,
                "compared_from_revision": base,
            },
            presence="content-present",
        )

    def test_empty_recorded_diff_decides_no_change_without_a_target_read(self) -> None:
        fixture = self.checkpoint_fixture()
        recorded_at = self.current_attempt_recorded_at(fixture)
        empty_candidate = working_tree_identity(fixture.brief.base_revision, b"")
        attempt = next(
            value
            for value in fixture.store.validated_snapshot().lifecycle.attempts
            if str(value.attempt_id) == "work-a-1"
        )
        self.accept_candidate_snapshot(
            resolve_durable_roots(fixture.project),
            fixture.store,
            attempt,
            "working-tree",
            empty_candidate,
            b"",
            fixture.brief.base_revision,
            fixture.brief.base_revision,
            recorded_at,
        )
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE attempts SET candidate_revision = ? WHERE attempt_id = 'work-a-1'", (empty_candidate,)
            )

        result = self.integration(fixture, "work-a", "no-such-target-at-all")

        self.assert_integration(
            result,
            fixture,
            target="no-such-target-at-all",
            target_revision=None,
            source={
                "kind": "protected-review",
                "attempt_id": "work-a-1",
                "candidate_revision": empty_candidate,
                "compared_from_revision": fixture.brief.base_revision,
            },
            presence="no-change",
        )

    def test_rename_and_binary_candidate_content_is_observed(self) -> None:
        fixture = self.checkpoint_fixture()
        self.git_text(fixture, "restore", "--", "tracked.txt")
        self.git_text(fixture, "mv", "tracked.txt", "renamed.txt")
        (fixture.project / "binary.bin").write_bytes(bytes(range(256)))
        self.git_text(fixture, "add", "binary.bin")
        diff = self.git_bytes(fixture, "diff", "--binary", "HEAD", "--")
        self.assertIn(b"renamed.txt", diff)
        self.assertIn(b"binary.bin", diff)
        recorded_at = self.current_attempt_recorded_at(fixture)
        candidate = working_tree_identity(fixture.brief.base_revision, diff)
        attempt = next(
            value
            for value in fixture.store.validated_snapshot().lifecycle.attempts
            if str(value.attempt_id) == "work-a-1"
        )
        self.accept_candidate_snapshot(
            resolve_durable_roots(fixture.project),
            fixture.store,
            attempt,
            "working-tree",
            candidate,
            diff,
            fixture.brief.base_revision,
            fixture.brief.base_revision,
            recorded_at,
        )
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute("UPDATE attempts SET candidate_revision = ? WHERE attempt_id = 'work-a-1'", (candidate,))
        base, candidate_commit = self.commit_candidate(fixture)
        branch = self.integrated_branch(fixture, "fast-forward", base, candidate_commit)
        resolved = self.git_text(fixture, "rev-parse", f"{branch}^{{commit}}").strip()

        result = self.integration(fixture, "work-a", branch)

        self.assert_integration(
            result,
            fixture,
            target=branch,
            target_revision=resolved,
            source={
                "kind": "protected-review",
                "attempt_id": "work-a-1",
                "candidate_revision": candidate,
                "compared_from_revision": base,
            },
            presence="content-present",
        )

    def test_unresolved_target_is_a_correctable_rejection(self) -> None:
        fixture = self.checkpoint_fixture()

        result = self.integration(fixture, "work-a", "no-such-ref")

        self.assert_rejection(
            result,
            "INTEGRATION_TARGET_UNRESOLVED",
            retry="correct-input",
            observed={"target": "no-such-ref", "project_root": str(fixture.project)},
        )
        self.assertIn("fetch", str(result["recovery"]), result)

    def test_option_like_targets_are_rejected_before_any_read(self) -> None:
        fixture = self.checkpoint_fixture()

        result = self.integration(fixture, "work-a", "-HEAD")

        self.assert_rejection(result, "ITEM_STATUS_INVALID", retry="correct-input", observed={})
        self.assertNotIn("recovery", result)

    def test_a_project_root_outside_any_git_checkout_is_a_correctable_rejection(self) -> None:
        fixture = self.checkpoint_fixture()
        with tempfile.TemporaryDirectory() as outside:
            result = call_advertised_tool(
                mcp_server.ITEM_STATUS_TOOL,
                {
                    "request": {
                        "project_root": outside,
                        "work_root": str(fixture.work),
                        "operation": "integration",
                        "item_id": "work-a",
                        "target": "unused-target",
                    }
                },
            )

        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual("rejected", result["status"], result)
        self.assertEqual("PROJECT_GIT_ROOT_UNAVAILABLE", result["code"], result)
        self.assertEqual("unchanged", result["effect"], result)
        self.assertEqual("correct-input", result["retry"], result)
        facts = {
            self.json_object(value)["field"]: self.json_object(value)["value"]
            for value in self.json_array(result["observed"])
        }
        self.assertEqual(outside, facts["project_root"], result)
        self.assertTrue(facts["diagnostic"], result)
        self.assertIn("checkout", str(result["recovery"]), result)

    def test_unavailable_candidates_are_named_by_reason(self) -> None:
        with self.subTest(reason="no-protected-or-checkpoint"):
            fixture = self.checkpoint_fixture()
            self.assert_rejection(
                self.integration(fixture, "work-c", "unused-target"),
                "INTEGRATION_CANDIDATE_UNAVAILABLE",
                retry="correct-input",
                observed={
                    "item_id": "work-c",
                    "item_state": "ready",
                    "reason": "no-protected-or-checkpoint",
                },
            )
        with self.subTest(reason="direct-close"):
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
            self.assert_rejection(
                self.integration(fixture, "work-c", "unused-target"),
                "INTEGRATION_CANDIDATE_UNAVAILABLE",
                retry="correct-input",
                observed={"item_id": "work-c", "item_state": "done", "reason": "direct-close"},
            )
        with self.subTest(reason="package-without-snapshot"):
            fixture = self.accepted_package_fixture()
            current = self.package(fixture)
            assert isinstance(current, work_brief_models.CheckpointReviewPackageV3)
            legacy = checkpoint_compatibility_models.CheckpointReviewPackage(
                current.attempt_id,
                current.item_id,
                current.candidate,
                current.acceptance_evidence,
                current.accepted_scope,
                current.checkpoint,
                current.accepted_brief,
                current.result,
                current.implementation_review,
                current.verdict,
                current.review_basis,
            )
            self.replace_package(fixture, legacy)
            self.assert_rejection(
                self.integration(fixture, "work-a", "unused-target"),
                "INTEGRATION_CANDIDATE_UNAVAILABLE",
                retry="correct-input",
                observed={
                    "item_id": "work-a",
                    "item_state": "paused",
                    "reason": "package-without-snapshot",
                },
            )
        with self.subTest(reason="pre-snapshot-candidate"):
            fixture = self.checkpoint_fixture()
            with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
                connection.execute(
                    "UPDATE attempts SET candidate_recorded_at = ? WHERE attempt_id = 'work-a-1'",
                    (SQLITE_NOW.isoformat(),),
                )
            self.assert_rejection(
                self.integration(fixture, "work-a", "unused-target"),
                "INTEGRATION_CANDIDATE_UNAVAILABLE",
                retry="correct-input",
                observed={
                    "item_id": "work-a",
                    "item_state": "review",
                    "reason": "pre-snapshot-candidate",
                },
            )

    def test_invalid_candidate_evidence_is_a_diagnosis_only_rejection(self) -> None:
        with self.subTest(defect="corrupted-snapshot-bytes"):
            fixture = self.checkpoint_fixture()
            selector = self.snapshot_reference(fixture, fixture.candidate_revision).selector
            (fixture.work / selector).write_bytes(b"corrupted snapshot bytes\n")
            result = self.integration(fixture, "work-a", fixture.brief.base_revision)
            self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
            self.assertEqual("INTEGRATION_CANDIDATE_EVIDENCE_INVALID", result["code"], result)
            self.assertEqual("do-not-retry", result["retry"], result)
            self.assertEqual("unchanged", result["effect"], result)
            facts = {
                self.json_object(value)["field"]: self.json_object(value)["value"]
                for value in self.json_array(result["observed"])
            }
            self.assertEqual("work-a-1", facts["attempt_id"], result)
            self.assertEqual(selector, facts["selector"], result)
            self.assertIn("pinboard validate", str(result["recovery"]), result)
        with self.subTest(defect="retained-v2-package"):
            fixture = self.accepted_package_fixture(candidate_form="current-head", local=True)
            self.retain_v2_checkpoint(fixture)
            result = self.integration(fixture, "work-a", fixture.brief.base_revision)
            self.assertEqual("INTEGRATION_CANDIDATE_EVIDENCE_INVALID", result["code"], result)
            self.assertEqual("do-not-retry", result["retry"], result)
            facts = {
                self.json_object(value)["field"]: self.json_object(value)["value"]
                for value in self.json_array(result["observed"])
            }
            self.assertEqual("work-a-1", facts["attempt_id"], result)
            self.assertTrue(str(facts["selector"]).endswith(".patch"), result)

    def test_damaged_checkpoint_receipt_is_a_typed_rejection(self) -> None:
        fixture = self.accepted_package_fixture()
        receipt = next(
            value
            for value in fixture.store.validated_snapshot().transition_receipts
            if value.outcome_schema == "checkpoint-acceptance/v2"
        )
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE transition_history SET outcome_json = '{\"unexpected\": true}' WHERE history_id = ?",
                (int(receipt.history_id),),
            )

        result = self.integration(fixture, "work-a", "unused-target")

        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual("TRANSITION_RECEIPT_DAMAGED", result["code"], result)
        self.assertEqual("do-not-retry", result["retry"], result)
        self.assertEqual("unchanged", result["effect"], result)
        facts = {
            self.json_object(value)["field"]: self.json_object(value)["value"]
            for value in self.json_array(result["observed"])
        }
        self.assertEqual("accept-checkpoint", facts["action_kind"], result)
        self.assertEqual("work-a-1", facts["attempt_id"], result)

    def test_an_unknown_item_is_a_typed_rejection(self) -> None:
        fixture = self.checkpoint_fixture()

        result = self.integration(fixture, "work-unknown", "unused-target")

        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual("rejected", result["status"], result)
        self.assertEqual("ITEM_NOT_FOUND", result["code"], result)

    def test_git_failures_are_typed_rejections_naming_the_checkout(self) -> None:
        fixture = self.checkpoint_fixture()
        recorded_at = self.current_attempt_recorded_at(fixture)
        bogus_diff = b"not a patch\n"
        candidate = working_tree_identity(fixture.brief.base_revision, bogus_diff)
        attempt = next(
            value
            for value in fixture.store.validated_snapshot().lifecycle.attempts
            if str(value.attempt_id) == "work-a-1"
        )
        self.accept_candidate_snapshot(
            resolve_durable_roots(fixture.project),
            fixture.store,
            attempt,
            "working-tree",
            candidate,
            bogus_diff,
            fixture.brief.base_revision,
            fixture.brief.base_revision,
            recorded_at,
        )
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute("UPDATE attempts SET candidate_revision = ? WHERE attempt_id = 'work-a-1'", (candidate,))

        result = self.integration(fixture, "work-a", fixture.brief.base_revision)

        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual("PROJECT_GIT_CHECKOUT_UNAVAILABLE", result["code"], result)
        self.assertEqual("correct-input", result["retry"], result)
        self.assertEqual("unchanged", result["effect"], result)
        facts = {
            self.json_object(value)["field"]: self.json_object(value)["value"]
            for value in self.json_array(result["observed"])
        }
        self.assertEqual(str(fixture.project), facts["project_root"], result)
        self.assertTrue(str(facts["diagnostic"]).strip(), result)
        self.assertIn("checkout", str(result["recovery"]), result)

    def test_integration_reads_stay_within_the_focused_scope(self) -> None:
        fixture = self.accepted_package_fixture()
        connection = open_database(fixture.work / "state.sqlite3", OpenMode.READ_ONLY)
        reads: list[tuple[str, str]] = []
        statements: list[str] = []

        def authorizer(
            action: int,
            table: str | None,
            column: str | None,
            _database: str | None,
            _source: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_READ and table is not None:
                reads.append((table, column or ""))
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        connection.set_trace_callback(statements.append)
        try:
            facts = sqlite_state.read_item_integration(connection, WorkItemId("work-a"))
        finally:
            connection.close()

        self.assertIsNotNone(facts.current_attempt)
        self.assertIsNotNone(facts.latest_checkpoint)
        allowed_tables = {"project_meta", "work_items", "attempts", "transition_history", "artifact_refs"}
        self.assertLessEqual({table for table, _column in reads}, allowed_tables)
        identity_columns = {
            "artifact_ref_id",
            "artifact_key",
            "artifact_revision",
            "kind",
            "relative_path",
            "content_sha256",
            "size_bytes",
            "accepted_revision",
            "created_at",
        }
        self.assertLessEqual({column for table, column in reads if table == "artifact_refs"}, identity_columns)

        plan_connection = open_database(fixture.work / "state.sqlite3", OpenMode.READ_ONLY)
        try:
            for statement in statements:
                if not statement.lstrip().upper().startswith("SELECT"):
                    continue
                rows = plan_connection.execute(f"EXPLAIN QUERY PLAN {statement.replace('?', 'NULL')}").fetchall()
                for row in rows:
                    detail = str(row[3])
                    if re.match(r"SCAN (work_items|attempts|transition_history|artifact_refs)\b", detail):
                        self.assertIn("USING", detail, f"{detail} in {statement}")
        finally:
            plan_connection.close()

    def test_temp_directories_do_not_leak_into_the_project(self) -> None:
        fixture = self.checkpoint_fixture()
        base, candidate = self.commit_candidate(fixture)
        branch = self.integrated_branch(fixture, "fast-forward", base, candidate)
        temporary_parent = Path(tempfile.gettempdir())
        leftover = set(temporary_parent.glob("pinboard-integration-*"))

        result = self.integration(fixture, "work-a", branch)

        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual(leftover, set(temporary_parent.glob("pinboard-integration-*")))


if __name__ == "__main__":
    unittest.main()
