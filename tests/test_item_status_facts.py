"""Item-status leaves report review verdicts, branch owners, closure facts, and damaged receipts."""

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from collections.abc import Generator
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime
from pathlib import Path
from typing import assert_never
from unittest.mock import patch

import msgspec
from mcp.server.mcpserver.exceptions import ToolError
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from pinboard.adapters.files.models import AffectedViews, ViewRefreshResult
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import checkpoint_compatibility_models, queries, query_models, work_brief_models, work_briefs
from pinboard.application.candidate_identity import working_tree_identity
from pinboard.application.ports import GeneratedViewReader
from pinboard.domain import decision_models
from pinboard.domain.identifiers import AttemptId, HistoryId, WorkItemId
from pinboard.mcp import common as mcp_common
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import AcceptedPackageFixture, CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import SQLITE_NOW, JsonObject, NoReadyCandidateReviews

CLOSED_AT = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
COMPLETED_AT = datetime(2030, 1, 3, 4, 5, 6, tzinfo=UTC)


class ItemStatusFactsTest(CheckpointPackageSupport):
    @contextlib.contextmanager
    def record_item_status_reads(self) -> Generator[tuple[set[str], list[str]]]:
        read_tables: set[str] = set()
        statements: list[str] = []
        original_open = sqlite_store.open_database

        def traced_open(database_path: Path, mode: sqlite_store.OpenMode) -> sqlite3.Connection:
            connection = original_open(database_path, mode)

            def authorize(
                action: int,
                argument: str | None,
                _secondary_argument: str | None,
                _database: str | None,
                _trigger: str | None,
            ) -> int:
                if action == sqlite3.SQLITE_READ and argument is not None:
                    read_tables.add(argument)
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(sqlite_store, "open_database", traced_open):
            yield read_tables, statements

    def assert_keyed_item_status_reads(self, fixture: CheckpointFixture, statements: list[str]) -> tuple[str, ...]:
        selects = tuple(statement for statement in statements if statement.lstrip().upper().startswith("SELECT"))
        self.assertTrue(selects)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            plans = tuple(
                str(row[3]).upper()
                for statement in selects
                for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}").fetchall()
            )
        self.assertFalse(any(detail.startswith("SCAN ") for detail in plans), plans)
        self.assertTrue(any(detail.startswith("SEARCH ") for detail in plans), plans)
        return plans

    def run_git(self, project: Path, *arguments: str) -> str:
        environment = os.environ.copy()
        environment["GIT_AUTHOR_DATE"] = "2001-02-03T04:05:06+00:00"
        environment["GIT_COMMITTER_DATE"] = "2001-02-03T04:05:06+00:00"
        return subprocess.run(
            ["git", *arguments], cwd=project, env=environment, check=True, capture_output=True, text=True
        ).stdout.strip()

    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def item_leaf(self, fixture: CheckpointFixture, item_id: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "item", "item_id": item_id}},
        )

    def branch_leaf(self, fixture: CheckpointFixture, branch: str) -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "branch", "branch": branch}},
        )

    def integration_leaf(self, fixture: CheckpointFixture, target: str, item_id: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": item_id, "target": target}},
        )

    def submit_native_candidate(self, fixture: CheckpointFixture) -> str:
        if fixture.candidate_revision.startswith("working-tree-state-sha256:"):
            observed = call_native_tool(
                mcp_server.CANDIDATE_OBSERVE_TOOL,
                {**self.roots(fixture), "attempt_id": "work-a-1"},
            )
            candidate = observed["candidate"]
            assert isinstance(candidate, str)
            self.assertEqual(fixture.candidate_revision, candidate, observed)
        else:
            candidate = fixture.candidate_revision
        state = fixture.store.validated_snapshot()
        generation = next(
            value for value in state.authority.attempt_generations if value.attempt_id == AttemptId("work-a-1")
        )
        lease: JsonObject = {"lease_id": str(generation.lease_id), "generation": generation.generation}
        with (
            patch("pinboard.mcp.read_operations.datetime") as action_clock,
            patch("pinboard.mcp.mutation_operations.datetime") as mutation_clock,
        ):
            action_clock.now.return_value = SQLITE_NOW
            mutation_clock.now.return_value = SQLITE_NOW
            action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
            submitted = self.transition_result(fixture, action, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)
        return candidate

    def inspection(self, fixture: CheckpointFixture) -> JsonObject:
        return call_advertised_tool(
            mcp_server.ATTEMPT_INSPECT_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1", "reconciliation": None}
        )

    def verdict(self, fixture: CheckpointFixture) -> JsonObject:
        """Read the verdict through the advertised item leaf and again from a fresh store."""

        status = self.item_leaf(fixture)
        self.assertEqual("pinboard-item-status/v2", status["schema"], status)
        for attempt in self.json_array(status["attempts"]):
            self.assertEqual(fixture.brief.branch, self.json_object(attempt)["branch"])
        verdict = self.json_object(status["review_verdict"])
        fresh = queries.project_item_status(
            SQLiteWorkStore(fixture.work / "state.sqlite3"),
            NoReadyCandidateReviews(),
            WorkItemId("work-a"),
            datetime.now(UTC),
        )
        assert isinstance(fresh, query_models.ItemStatus)
        if verdict["kind"] != "ready":
            self.assertEqual(verdict, msgspec.to_builtins(fresh.review_verdict))
        return verdict

    def assert_valid(self, fixture: CheckpointFixture) -> None:
        validated, stdout, stderr = self.run_cli(*fixture.common, "validate", "--json")
        self.assertEqual(0, validated, f"{stdout}\n{stderr}")

    def test_integration_leaf_reports_content_before_and_after_fast_forward(self) -> None:
        fixture = self.checkpoint_fixture(native_submission=True, candidate_context=True)
        candidate = self.submit_native_candidate(fixture)
        base_revision = self.run_git(fixture.project, "rev-parse", "main")

        before_integration = self.integration_leaf(fixture, "main")
        self.assertEqual("pinboard-item-integration/v1", before_integration["schema"], before_integration)
        before_result = self.json_object(before_integration)
        self.assertEqual("content-not-present", before_result["presence"])
        self.assertEqual("main", before_result["target"])
        self.assertEqual(base_revision, before_result["target_revision"])
        self.assertEqual(
            {
                "kind": "protected-review",
                "attempt_id": "work-a-1",
                "candidate_revision": candidate,
                "compared_from_revision": base_revision,
            },
            self.json_object(before_result["source"]),
        )

        self.run_git(fixture.project, "add", "tracked.txt")
        self.run_git(
            fixture.project,
            "-c",
            "user.name=Pinboard Tests",
            "-c",
            "user.email=pinboard@example.invalid",
            "commit",
            "-m",
            "candidate",
        )
        self.run_git(fixture.project, "switch", "main")
        self.run_git(fixture.project, "merge", "--ff-only", "codex/work-a")
        present = self.integration_leaf(fixture, "main")
        self.assertEqual("pinboard-item-integration/v1", present["schema"], present)
        self.assertEqual("content-present", present["presence"])
        target_revision = self.run_git(fixture.project, "rev-parse", "HEAD")
        self.assertEqual(target_revision, present["target_revision"])
        self.assertEqual(
            {
                "kind": "protected-review",
                "attempt_id": "work-a-1",
                "candidate_revision": candidate,
                "compared_from_revision": base_revision,
            },
            self.json_object(present["source"]),
        )

        tracked_lines = (fixture.project / "tracked.txt").read_text(encoding="utf-8").splitlines()
        tracked_lines[0] = "later non-overlapping line"
        (fixture.project / "tracked.txt").write_text("\n".join(tracked_lines) + "\n", encoding="utf-8")
        self.commit_all(fixture.project, "non-overlapping edit")
        non_overlapping = self.integration_leaf(fixture, "main")
        self.assertEqual("content-present", non_overlapping["presence"], non_overlapping)

        tracked_lines[10] = "overlapping replacement"
        (fixture.project / "tracked.txt").write_text("\n".join(tracked_lines) + "\n", encoding="utf-8")
        self.commit_all(fixture.project, "overlapping edit")
        overlapping = self.integration_leaf(fixture, "main")
        self.assertEqual("content-not-present", overlapping["presence"], overlapping)

    def test_integration_leaf_reports_working_tree_rename_and_binary_after_squash(self) -> None:
        fixture = self.checkpoint_fixture(native_submission=True, candidate_context=True)
        tracked = fixture.project / "tracked.txt"
        renamed = fixture.project / "renamed.txt"
        tracked.replace(renamed)
        binary = fixture.project / "assets" / "sample.bin"
        binary.parent.mkdir()
        binary.write_bytes(bytes(range(256)))
        observed = call_native_tool(
            mcp_server.CANDIDATE_OBSERVE_TOOL,
            {**self.roots(fixture), "attempt_id": "work-a-1"},
        )
        candidate = observed["candidate"]
        assert isinstance(candidate, str)
        fixture = dataclass_replace(fixture, candidate_revision=candidate)
        self.assertEqual(candidate, self.submit_native_candidate(fixture))
        base_revision = self.run_git(fixture.project, "rev-parse", "main")

        self.run_git(fixture.project, "add", "--all")
        self.run_git(
            fixture.project,
            "-c",
            "user.name=Pinboard Tests",
            "-c",
            "user.email=pinboard@example.invalid",
            "commit",
            "-m",
            "renamed and binary candidate",
        )
        self.run_git(fixture.project, "switch", "main")
        self.run_git(fixture.project, "merge", "--squash", "codex/work-a")
        self.run_git(
            fixture.project,
            "-c",
            "user.name=Pinboard Tests",
            "-c",
            "user.email=pinboard@example.invalid",
            "commit",
            "-m",
            "squash renamed and binary candidate",
        )

        result = self.integration_leaf(fixture, "main")
        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual("content-present", result["presence"], result)
        self.assertEqual("main", result["target"])
        self.assertEqual(self.run_git(fixture.project, "rev-parse", "main"), result["target_revision"])
        self.assertEqual(
            {
                "kind": "protected-review",
                "attempt_id": "work-a-1",
                "candidate_revision": candidate,
                "compared_from_revision": base_revision,
            },
            self.json_object(result["source"]),
        )
        self.assertFalse((fixture.project / "tracked.txt").exists())
        self.assertEqual(renamed.read_bytes(), (fixture.project / "renamed.txt").read_bytes())
        self.assertEqual(bytes(range(256)), (fixture.project / "assets" / "sample.bin").read_bytes())

    def test_integration_leaf_recognizes_merge_commit_rebase_and_squash_content(self) -> None:
        for strategy in ("fast-forward", "merge-commit", "rebase", "squash"):
            with self.subTest(strategy=strategy):
                fixture = self.checkpoint_fixture(native_submission=True, candidate_form="current-head")
                candidate = self.submit_native_candidate(fixture)
                base_revision = self.run_git(fixture.project, "rev-parse", "main")
                if strategy == "fast-forward":
                    self.run_git(fixture.project, "switch", "main")
                    self.run_git(fixture.project, "merge", "--ff-only", "codex/work-a")
                elif strategy == "rebase":
                    self.run_git(fixture.project, "switch", "main")
                    (fixture.project / "independent.txt").write_text("target-only change\n", encoding="utf-8")
                    self.commit_all(fixture.project, "target change")
                    self.run_git(fixture.project, "switch", "codex/work-a")
                    self.run_git(fixture.project, "rebase", "main")
                    self.run_git(fixture.project, "switch", "main")
                    self.run_git(fixture.project, "merge", "--ff-only", "codex/work-a")
                    ancestry = subprocess.run(
                        ["git", "merge-base", "--is-ancestor", candidate, "main"],
                        cwd=fixture.project,
                        check=False,
                        capture_output=True,
                    )
                    self.assertEqual(1, ancestry.returncode)
                elif strategy == "merge-commit":
                    self.run_git(fixture.project, "switch", "main")
                    self.run_git(
                        fixture.project,
                        "-c",
                        "user.name=Pinboard Tests",
                        "-c",
                        "user.email=pinboard@example.invalid",
                        "merge",
                        "--no-ff",
                        "--no-edit",
                        "codex/work-a",
                    )
                else:
                    self.run_git(fixture.project, "switch", "main")
                    self.run_git(fixture.project, "merge", "--squash", "codex/work-a")
                    self.run_git(
                        fixture.project,
                        "-c",
                        "user.name=Pinboard Tests",
                        "-c",
                        "user.email=pinboard@example.invalid",
                        "commit",
                        "-m",
                        "squash candidate",
                    )
                    ancestry = subprocess.run(
                        ["git", "merge-base", "--is-ancestor", candidate, "main"],
                        cwd=fixture.project,
                        check=False,
                        capture_output=True,
                    )
                    self.assertEqual(1, ancestry.returncode)
                result = self.integration_leaf(fixture, "main")
                self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
                self.assertEqual("content-present", result["presence"], result)
                self.assertEqual(
                    {
                        "kind": "protected-review",
                        "attempt_id": "work-a-1",
                        "candidate_revision": candidate,
                        "compared_from_revision": base_revision,
                    },
                    self.json_object(result["source"]),
                )
                target_revision = self.run_git(fixture.project, "rev-parse", "main")
                self.assertEqual(target_revision, result["target_revision"])
                self.run_git(fixture.project, "update-ref", "refs/remotes/origin/main", target_revision)
                remote_tracking = self.integration_leaf(fixture, "origin/main")
                self.assertEqual("content-present", remote_tracking["presence"], remote_tracking)
                self.assertEqual("origin/main", remote_tracking["target"])

    def test_integration_leaf_retains_accepted_checkpoint_source_across_resume(self) -> None:
        fixture = self.checkpoint_fixture(native_submission=True)
        candidate = self.submit_native_candidate(fixture)
        with patch("pinboard.mcp.mutation_operations.datetime") as clock:
            clock.now.return_value = SQLITE_NOW
            self.accept_checkpoint(
                fixture,
                self.project_action(fixture, "accept-checkpoint:work-a-1"),
                fixture.payload,
            )
        self.run_git(fixture.project, "add", "tracked.txt")
        self.run_git(
            fixture.project,
            "-c",
            "user.name=Pinboard Tests",
            "-c",
            "user.email=pinboard@example.invalid",
            "commit",
            "-m",
            "candidate",
        )
        self.run_git(fixture.project, "switch", "main")
        self.run_git(fixture.project, "merge", "--squash", "codex/work-a")
        self.run_git(
            fixture.project,
            "-c",
            "user.name=Pinboard Tests",
            "-c",
            "user.email=pinboard@example.invalid",
            "commit",
            "-m",
            "checkpoint candidate",
        )

        paused = self.integration_leaf(fixture, "main")
        self.assertEqual("accepted-checkpoint", self.json_object(paused["source"])["kind"], paused)
        self.assertEqual(fixture.brief.checkpoint.checkpoint_id, self.json_object(paused["source"])["checkpoint_id"])
        self.assertEqual(candidate, self.json_object(paused["source"])["candidate_revision"])
        self.assertEqual("content-present", paused["presence"], paused)

        self.close_prerequisite(fixture)
        with patch("pinboard.mcp.mutation_operations.datetime") as clock:
            clock.now.return_value = SQLITE_NOW
            self.transition(fixture, "resume:work-a", {})
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            history_id, project_revision = connection.execute(
                "SELECT MAX(history_id), MAX(project_revision) FROM transition_history"
            ).fetchone()
            for index in range(24):
                connection.execute(
                    """
                    INSERT INTO transition_history(
                        history_id, project_revision, action_id, action_kind, subject_id,
                        artifact_ref_id, artifact_kind, authorization_kind, actor_task_id, actor_host_id,
                        input_schema, input_json, outcome_schema, outcome_json, committed_at
                    ) VALUES (?, ?, ?, 'accept-checkpoint', ?, NULL, NULL, 'project', NULL, NULL,
                              'checkpoint-acceptance/v2', '{}', 'checkpoint-acceptance/v2', '{}', ?)
                    """,
                    (
                        int(history_id) + index + 1,
                        int(project_revision) + index + 1,
                        f"accept-checkpoint:unrelated-{index}",
                        f"unrelated-attempt-{index}",
                        SQLITE_NOW.isoformat(),
                    ),
                )
        with self.record_item_status_reads() as (read_tables, statements):
            resumed = self.integration_leaf(fixture, "main")
        self.assertEqual("accepted-checkpoint", self.json_object(resumed["source"])["kind"], resumed)
        self.assertEqual(candidate, self.json_object(resumed["source"])["candidate_revision"])
        self.assertEqual("content-present", resumed["presence"], resumed)
        self.assertIn("transition_history", read_tables)
        plans = self.assert_keyed_item_status_reads(fixture, statements)
        self.assertTrue(any("CHECKPOINT_HISTORY_BY_SUBJECT" in detail for detail in plans), plans)

    def test_integration_leaf_reads_only_the_selected_candidate_context(self) -> None:
        fixture = self.checkpoint_fixture(native_submission=True)
        self.submit_native_candidate(fixture)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            current = connection.execute("SELECT * FROM attempts WHERE attempt_id = 'work-a-1'").fetchone()
            assert current is not None
            columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(attempts)"))
            placeholders = ", ".join("?" for _ in columns)
            for index in range(24):
                values = list(current)
                values[0] = f"unrelated-attempt-{index}"
                values[1] = "work-b"
                values[2] = "done"
                values[3] = f"codex/unrelated-{index}"
                connection.execute(f"INSERT INTO attempts ({', '.join(columns)}) VALUES ({placeholders})", values)
            history_id, project_revision = connection.execute(
                "SELECT MAX(history_id), MAX(project_revision) FROM transition_history"
            ).fetchone()
            for index in range(24):
                connection.execute(
                    """
                    INSERT INTO transition_history(
                        history_id, project_revision, action_id, action_kind, subject_id,
                        artifact_ref_id, artifact_kind, authorization_kind, actor_task_id, actor_host_id,
                        input_schema, input_json, outcome_schema, outcome_json, committed_at
                    ) VALUES (?, ?, ?, 'accept-checkpoint', ?, NULL, NULL, 'project', NULL, NULL,
                              'checkpoint-acceptance/v2', '{}', 'checkpoint-acceptance/v2', '{}', ?)
                    """,
                    (
                        int(history_id) + index + 1,
                        int(project_revision) + index + 1,
                        f"accept-checkpoint:unrelated-{index}",
                        f"unrelated-attempt-{index}",
                        SQLITE_NOW.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO artifact_refs(
                        artifact_key, artifact_revision, kind, relative_path,
                        content_sha256, size_bytes, accepted_revision, created_at
                    ) VALUES (?, 1, 'evidence', ?, ?, 0, 0, ?)
                    """,
                    (
                        f"unrelated-package-{index}",
                        f"artifacts/unrelated-package-{index}.json",
                        "f" * 64,
                        SQLITE_NOW.isoformat(),
                    ),
                )

        with self.record_item_status_reads() as (read_tables, statements):
            result = self.integration_leaf(fixture, "main")

        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual("content-not-present", result["presence"])
        self.assertEqual({"artifact_refs", "attempts", "transition_history", "work_items"}, read_tables)
        self.assertNotIn("unrelated-attempt", str(result))
        self.assertNotIn("unrelated-package", str(result))
        self.assert_keyed_item_status_reads(fixture, statements)

    def test_integration_leaf_rejects_a_checkpoint_package_without_candidate_snapshot_identity(self) -> None:
        fixture = self.checkpoint_fixture(native_submission=True)
        candidate = self.submit_native_candidate(fixture)
        with patch("pinboard.mcp.mutation_operations.datetime") as clock:
            clock.now.return_value = SQLITE_NOW
            self.accept_checkpoint(
                fixture,
                self.project_action(fixture, "accept-checkpoint:work-a-1"),
                fixture.payload,
            )
        state = fixture.store.validated_snapshot()
        package_reference = next(
            value
            for value in state.artifact_references
            if value.key == f"work-a-1-{fixture.brief.checkpoint.checkpoint_id}-review-package"
        )
        accepted = AcceptedPackageFixture(
            fixture.project,
            fixture.work,
            fixture.store,
            fixture.brief,
            candidate,
            fixture.candidate_bytes,
            fixture.payload,
            package_reference,
        )
        current = self.package(accepted)
        self.assertIsInstance(current, work_brief_models.CheckpointReviewPackageV3)
        assert isinstance(current, work_brief_models.CheckpointReviewPackageV3)
        retained = checkpoint_compatibility_models.CheckpointReviewPackage(
            attempt_id=current.attempt_id,
            item_id=current.item_id,
            candidate=current.candidate,
            acceptance_evidence=current.acceptance_evidence,
            accepted_scope=current.accepted_scope,
            checkpoint=current.checkpoint,
            accepted_brief=current.accepted_brief,
            result=current.result,
            implementation_review=current.implementation_review,
            verdict=current.verdict,
            review_basis=current.review_basis,
        )
        self.replace_artifact_bytes(
            fixture, package_reference, work_briefs.canonical_checkpoint_review_package_bytes(retained)
        )

        result = self.integration_leaf(fixture, "main")

        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", result["code"], result)
        self.assertEqual("unchanged", result["effect"])
        self.assertEqual("correct-input", result["retry"])
        recovery = result["recovery"]
        assert isinstance(recovery, str)
        self.assertIn("with operation item", recovery)

    def test_attempt_inspection_keeps_integration_reconciliation_caller_owned(self) -> None:
        fixture = self.checkpoint_fixture(native_submission=True, candidate_form="current-head")
        candidate = self.submit_native_candidate(fixture)
        self.record_ready(fixture, candidate)
        target_revision = self.run_git(fixture.project, "rev-parse", "main")
        reconciliation = msgspec.to_builtins(
            query_models.AttemptReconciliation(
                target_revision,
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
        )

        with patch(
            "pinboard.adapters.files.root.read_candidate_integration",
            side_effect=AssertionError("Attempt inspection must not perform the integration content read."),
        ):
            result = call_advertised_tool(
                mcp_server.ATTEMPT_INSPECT_TOOL,
                {
                    **self.roots(fixture),
                    "attempt_id": "work-a-1",
                    "reconciliation": reconciliation,
                },
            )

        self.assertNotIn("code", result, result)
        continuation = self.json_object(result["continuation"])
        next_operation = self.json_object(continuation["next_operation"])
        self.assertEqual("repository-cleanup", next_operation["kind"])
        self.assertEqual(target_revision, next_operation["target_revision"])

    def test_integration_leaf_uses_the_closing_candidate_after_completion(self) -> None:
        fixture = self.checkpoint_fixture(native_submission=True)
        candidate = self.submit_native_candidate(fixture)
        self.record_ready(fixture, candidate)
        self.complete(fixture, "The reviewed candidate satisfies the outcome.")
        self.run_git(fixture.project, "add", "tracked.txt")
        self.run_git(
            fixture.project,
            "-c",
            "user.name=Pinboard Tests",
            "-c",
            "user.email=pinboard@example.invalid",
            "commit",
            "-m",
            "candidate",
        )
        self.run_git(fixture.project, "switch", "main")
        self.run_git(fixture.project, "merge", "--ff-only", "codex/work-a")
        result = self.integration_leaf(fixture, "main")
        self.assertEqual("completion", self.json_object(result["source"])["kind"], result)
        self.assertEqual(candidate, self.json_object(result["source"])["candidate_revision"])
        self.assertEqual("content-present", result["presence"], result)

    def test_integration_leaf_reports_no_change_for_an_empty_accepted_diff(self) -> None:
        fixture = self.checkpoint_fixture()
        state = fixture.store.validated_snapshot()
        attempt = next(value for value in state.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        base_revision = self.run_git(fixture.project, "rev-parse", "HEAD")
        candidate = working_tree_identity(base_revision, b"")
        self.accept_candidate_snapshot(
            resolve_durable_roots(fixture.project),
            fixture.store,
            attempt,
            "working-tree",
            candidate,
            b"",
            base_revision,
            attempt.base_revision,
            SQLITE_NOW,
        )
        with sqlite3.connect(fixture.work / "state.sqlite3") as connection:
            connection.execute(
                "UPDATE attempts SET candidate_revision = ?, candidate_recorded_at = ? WHERE attempt_id = ?",
                (candidate, SQLITE_NOW.isoformat(), "work-a-1"),
            )
        result = self.integration_leaf(fixture, "main")
        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual("no-change", result["presence"], result)
        self.assertEqual(base_revision, result["target_revision"])

    def test_integration_leaf_returns_typed_target_candidate_and_evidence_rejections(self) -> None:
        fixture = self.checkpoint_fixture(native_submission=True)
        self.submit_native_candidate(fixture)
        unresolved = self.integration_leaf(fixture, "missing-local-target")
        self.assertEqual("pinboard-mcp-item-status-result/v3", unresolved["schema"])
        self.assertEqual("INTEGRATION_TARGET_UNRESOLVED", unresolved["code"])
        self.assertFalse(unresolved["state_changed"])
        self.assertEqual(
            ("unchanged", "correct-input", []),
            (unresolved["effect"], unresolved["retry"], unresolved["changed_surfaces"]),
        )
        unresolved_observed = {
            self.json_object(value)["field"]: self.json_object(value)["value"]
            for value in self.json_array(unresolved["observed"])
        }
        self.assertEqual("missing-local-target", unresolved_observed["target"])
        self.assertEqual(str(fixture.project), unresolved_observed["project_root"])
        recovery = unresolved["recovery"]
        assert isinstance(recovery, str)
        self.assertIn("fetch outside Pinboard", recovery)

        ready = self.integration_leaf(fixture, "main", item_id="work-c")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", ready["code"], ready)
        self.assertEqual("correct-input", ready["retry"])
        self.assertEqual(
            "work-c",
            next(
                self.json_object(value)["value"]
                for value in self.json_array(ready["observed"])
                if self.json_object(value)["field"] == "item_id"
            ),
        )
        direct_close_reason = next(
            self.json_object(value)["value"]
            for value in self.json_array(ready["observed"])
            if self.json_object(value)["field"] == "reason"
        )
        assert isinstance(direct_close_reason, str)
        self.assertIn("no current attempt", direct_close_reason)
        self.close_prerequisite(fixture)
        directly_closed = self.integration_leaf(fixture, "main", item_id="work-c")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", directly_closed["code"], directly_closed)
        direct_close_reason = next(
            self.json_object(value)["value"]
            for value in self.json_array(directly_closed["observed"])
            if self.json_object(value)["field"] == "reason"
        )
        assert isinstance(direct_close_reason, str)
        self.assertIn("closed directly", direct_close_reason)
        missing = self.integration_leaf(fixture, "main", item_id="unknown-item")
        self.assertEqual("ITEM_NOT_FOUND", missing["code"], missing)
        self.assertEqual("pinboard-mcp-item-status-result/v3", missing["schema"])

        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        snapshot_path = fixture.work / context.reference.selector
        snapshot_path.write_bytes(snapshot_path.read_bytes() + b"tampered")
        invalid = self.integration_leaf(fixture, "main")
        self.assertEqual("INTEGRATION_CANDIDATE_EVIDENCE_INVALID", invalid["code"], invalid)
        self.assertEqual(
            ("unchanged", "do-not-retry", []), (invalid["effect"], invalid["retry"], invalid["changed_surfaces"])
        )
        self.assertEqual(
            {"attempt_id", "accepted_reference"},
            {self.json_object(value)["field"] for value in self.json_array(invalid["observed"])},
        )
        self.assertEqual(1, len(self.json_array(invalid["mismatches"])))
        recovery = invalid["recovery"]
        assert isinstance(recovery, str)
        self.assertIn("pinboard validate", recovery)

    def test_integration_leaf_names_missing_source_direct_close_invalid_target_and_git_failure(self) -> None:
        fixture = self.checkpoint_fixture(native_submission=True)
        active = self.integration_leaf(fixture, "main")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", active["code"], active)
        reason = next(
            self.json_object(value)["value"]
            for value in self.json_array(active["observed"])
            if self.json_object(value)["field"] == "reason"
        )
        assert isinstance(reason, str)
        self.assertIn("no protected candidate", reason)

        direct_closed = self.checkpoint_fixture(native_submission=True)
        self.close_prerequisite(direct_closed)
        closed = self.integration_leaf(direct_closed, "main", item_id="work-c")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", closed["code"], closed)

        invalid_target = self.integration_leaf(direct_closed, "-main")
        self.assertEqual("ITEM_STATUS_INVALID", invalid_target["code"], invalid_target)
        self.assertEqual("pinboard-mcp-item-status-result/v3", invalid_target["schema"])
        self.assertFalse(invalid_target["state_changed"])
        self.assertEqual(
            ("unchanged", "correct-input", [], [], []),
            (
                invalid_target["effect"],
                invalid_target["retry"],
                invalid_target["changed_surfaces"],
                invalid_target["observed"],
                invalid_target["mismatches"],
            ),
        )
        invalid_target_message = invalid_target["message"]
        assert isinstance(invalid_target_message, str)
        self.assertIn("Cannot read item status", invalid_target_message)
        self.assertIn("target", invalid_target_message)

        outside_git = Path(tempfile.mkdtemp()).resolve()
        git_failure = call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "project_root": str(outside_git),
                    "work_root": str(direct_closed.work),
                    "operation": "integration",
                    "item_id": "work-a",
                    "target": "main",
                }
            },
        )
        self.assertEqual("PROJECT_GIT_ROOT_UNAVAILABLE", git_failure["code"], git_failure)
        self.assertEqual("pinboard-mcp-item-status-result/v3", git_failure["schema"])
        self.assertEqual(
            ("unchanged", "correct-input", []),
            (git_failure["effect"], git_failure["retry"], git_failure["changed_surfaces"]),
        )
        recovery = git_failure["recovery"]
        assert isinstance(recovery, str)
        self.assertIn("Correct the project checkout", recovery)

    def transition(self, fixture: CheckpointFixture, action_id: str, payload: JsonObject) -> JsonObject:
        result = self.transition_result(fixture, self.project_action(fixture, action_id), payload)
        self.assertIn(result["status"], ("committed", "committed-with-warning"), result)
        return result

    def return_for_review(self, fixture: CheckpointFixture, reason: str) -> int:
        history_id = self.transition(fixture, "return-for-correction:work-a-1", {"reason": reason})["history_id"]
        assert isinstance(history_id, int)
        return history_id

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

    def rebind(self, fixture: CheckpointFixture, branch: str, revision: int) -> None:
        brief = replace_struct(fixture.brief, artifact_revision=revision, branch=branch)
        published = call_native_tool(
            mcp_server.BRIEF_PUBLISH_TOOL,
            {**self.roots(fixture), "brief": self.json_object(msgspec.json.decode(msgspec.json.encode(brief)))},
        )
        self.assertEqual("committed", published["status"], published)
        reference = self.json_object(published["reference"])
        self.transition(
            fixture,
            "rebind-attempt:work-a-1",
            {
                "branch": branch,
                "base_revision": brief.base_revision,
                "brief_artifact_ref_id": reference["artifact_ref_id"],
            },
        )

    def submit_candidate(self, fixture: CheckpointFixture, label: str) -> str:
        (fixture.project / "tracked.txt").write_text(f"{label}\n", encoding="utf-8")
        observed = call_native_tool(
            mcp_server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"}
        )
        candidate = observed["candidate"]
        assert isinstance(candidate, str), observed
        lease = self.native_attempt_acquire(fixture, f"worker-{label}")
        submission = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        submitted = self.transition_result(fixture, submission, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)
        return candidate

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

    def complete(self, fixture: CheckpointFixture, evidence: str) -> None:
        fixture = self.terminalize_brief(fixture)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        with patch("pinboard.mcp.mutation_operations.datetime") as clock:
            clock.now.return_value = COMPLETED_AT
            self.transition(
                fixture,
                "complete:work-a-1",
                {
                    "schema": "pinboard-reviewed-completion/v2",
                    "candidate": fixture.candidate_revision,
                    "evidence": evidence,
                    "reviewer_task_id": "independent-reviewer",
                    "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
                    "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
                    "packages": [],
                },
            )

    def revise_title(self, fixture: CheckpointFixture, title: str) -> JsonObject:
        definition = call_native_tool(
            mcp_server.ITEM_DEFINITION_TOOL,
            {"request": {**self.roots(fixture), "operation": "current", "item_id": "work-a"}},
        )
        return self.transition(
            fixture,
            "revise-item:work-a",
            {
                "schema": "pinboard-item-revision/v1",
                "expected_revision": definition["definition_revision"],
                "expected_digest": definition["definition_digest"],
                "source_task": "review-owner",
                "reason": "Clarify the outcome.",
                "definition": {**self.json_object(definition["definition"]), "title": title},
            },
        )

    def item_leaf_after_repair_title(self, fixture: CheckpointFixture) -> str:
        """Read the committed definition title, which a damaged pause receipt does not affect."""

        definition = call_native_tool(
            mcp_server.ITEM_DEFINITION_TOOL,
            {"request": {**self.roots(fixture), "operation": "current", "item_id": "work-a"}},
        )
        title = self.json_object(definition["definition"])["title"]
        assert isinstance(title, str)
        return title

    def revise_with_damage_before_refresh(
        self, fixture: CheckpointFixture, history_id: int, columns: dict[str, str]
    ) -> JsonObject:
        """Commit a revision, then damage the pause receipt before its post-commit view refresh reads it."""

        refresh = mcp_common._refresh_affected_views

        def damage_then_refresh(
            durable: DurableRoots, store: GeneratedViewReader, affected: AffectedViews, now: datetime
        ) -> ViewRefreshResult:
            self.update_receipt(fixture, history_id, **columns)
            return refresh(durable, store, affected, now)

        with patch.object(mcp_common, "_refresh_affected_views", damage_then_refresh):
            return self.revise_title(fixture, "Revised while paused")

    def update_receipt(self, fixture: CheckpointFixture, history_id: int, **columns: str) -> None:
        assignments = ", ".join(f"{column} = ?" for column in columns)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                f"UPDATE transition_history SET {assignments} WHERE history_id = ?",
                (*columns.values(), history_id),
            )

    def latest_history_id(self, fixture: CheckpointFixture) -> int:
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            value = connection.execute("SELECT MAX(history_id) FROM transition_history").fetchone()[0]
        assert isinstance(value, int)
        return value

    def test_returned_verdict_survives_pause_block_resume_and_rebind_until_resubmission(self) -> None:
        reason = "Criterion 2 lacks behavior evidence."
        for route in ("pause", "block", "rebind"):
            with self.subTest(route=route):
                fixture = self.checkpoint_fixture()
                history_id = self.return_for_review(fixture, reason)
                returned = {
                    "kind": "returned-for-correction",
                    "history_id": history_id,
                    "reason": reason,
                    "rebound_since_return": False,
                }
                self.assertEqual(returned, self.verdict(fixture))
                context = call_advertised_tool(
                    mcp_server.CORRECTION_CONTEXT_TOOL,
                    {**self.roots(fixture), "attempt_id": "work-a-1", "correction_history_id": history_id},
                )
                self.assertEqual(context["correction_reason"], returned["reason"])
                match route:
                    case "pause":
                        self.close_prerequisite(fixture)
                        self.transition(fixture, "pause:work-a-1", {"reason": "Wait for the maintainer."})
                        self.assertEqual(returned, self.verdict(fixture))
                        self.transition(fixture, "resume:work-a", {})
                    case "block":
                        self.transition(
                            fixture,
                            "block:work-a-1",
                            {"reason": "Wait for the prerequisite.", "depends_on": ["work-c"]},
                        )
                        self.assertEqual(returned, self.verdict(fixture))
                        self.close_prerequisite(fixture)
                        self.transition(fixture, "resume:work-a", {})
                    case _:
                        self.rebind(fixture, fixture.brief.branch, 2)
                        self.rebind(fixture, fixture.brief.branch, 3)
                        returned = {**returned, "rebound_since_return": True}
                self.assertEqual(returned, self.verdict(fixture))
                self.submit_candidate(fixture, f"corrected-{route}")
                self.assertEqual({"kind": "none"}, self.verdict(fixture))
                self.assert_valid(fixture)

    def test_ready_verdict_follows_the_current_record_ready_review(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Rework the candidate.")
        candidate = self.submit_candidate(fixture, "reviewed")
        self.assertEqual({"kind": "none"}, self.verdict(fixture))
        recorded = self.record_ready(fixture, candidate)
        verdict = self.verdict(fixture)
        reference = self.json_object(recorded["candidate_review"])
        self.assertEqual("ready", verdict["kind"])
        self.assertEqual(candidate, verdict["candidate_revision"])
        self.assertEqual(
            (reference["artifact_ref_id"], reference["selector"], reference["sha256"]),
            tuple(
                self.json_object(verdict["candidate_review"])[key] for key in ("artifact_ref_id", "selector", "sha256")
            ),
        )
        review = fixture.work / "attempts" / "work-a-1" / "review.md"
        review.write_text("A later review changed the evidence.\n", encoding="utf-8")
        self.assertEqual({"kind": "none"}, self.verdict(fixture))
        review.unlink()
        self.assertEqual({"kind": "none"}, self.verdict(fixture))

    def test_acceptance_verdicts_carry_their_evidence_and_checkpoint(self) -> None:
        fixture = self.checkpoint_fixture()
        self.transition(
            fixture,
            "accept-review-and-continue:work-a-1",
            {"candidate": fixture.candidate_revision, "evidence": "Accepted; continue the attempt."},
        )
        self.assertEqual(
            {"kind": "accepted-and-continued", "evidence": "Accepted; continue the attempt."}, self.verdict(fixture)
        )
        fixture = self.accepted_package_fixture()
        self.assertEqual(
            {"kind": "checkpoint-accepted", "checkpoint": fixture.brief.checkpoint.checkpoint_id},
            self.verdict(fixture),
        )
        self.assertIsNone(self.json_object(self.json_array(self.item_leaf(fixture)["attempts"])[0])["pause_reason"])
        self.assert_valid(fixture)

    def test_retained_decision_v1_return_reports_its_evidence_without_failure(self) -> None:
        for evidence in ("Retained reason.", None):
            with self.subTest(evidence=evidence):
                fixture = self.checkpoint_fixture()
                history_id = self.return_for_review(fixture, "Current reason.")
                outcome = {"evidence": evidence, "outcome": "return-for-correction"}
                self.update_receipt(
                    fixture,
                    history_id,
                    input_schema="decision/v1",
                    input_json="{}",
                    outcome_json=json.dumps({key: value for key, value in outcome.items() if value is not None}),
                )
                self.assertEqual(
                    {
                        "kind": "returned-for-correction",
                        "history_id": history_id,
                        "reason": evidence,
                        "rebound_since_return": False,
                    },
                    self.verdict(fixture),
                )
                self.rebind(fixture, fixture.brief.branch, 2)
                self.assertTrue(self.verdict(fixture)["rebound_since_return"])

    def damaged_receipt_fixture(self, receipt: query_models.DamagedReceiptActionKind) -> CheckpointFixture:
        """Build an attempt whose consumed receipt of the named kind is its latest receipt."""

        match receipt:
            case decision_models.ActionKind.ACCEPT_CHECKPOINT:
                return self.accepted_package_fixture()
            case decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE:
                fixture = self.checkpoint_fixture()
                self.transition(
                    fixture,
                    "accept-review-and-continue:work-a-1",
                    {"candidate": fixture.candidate_revision, "evidence": "Accepted; continue the attempt."},
                )
                return fixture
            case decision_models.ActionKind.RETURN_FOR_CORRECTION:
                fixture = self.checkpoint_fixture()
                self.return_for_review(fixture, "Rework the candidate.")
                return fixture
            case decision_models.ActionKind.PAUSE:
                fixture = self.damaged_receipt_fixture(decision_models.ActionKind.RETURN_FOR_CORRECTION)
                self.transition(fixture, "pause:work-a-1", {"reason": "Wait for the maintainer."})
                return fixture
            case decision_models.ActionKind.REBIND_ATTEMPT:
                fixture = self.damaged_receipt_fixture(decision_models.ActionKind.PAUSE)
                self.rebind(fixture, fixture.brief.branch, 2)
                return fixture
            case decision_models.ActionKind.COMPLETE:
                fixture = self.checkpoint_fixture()
                self.record_ready(fixture, fixture.candidate_revision)
                self.complete(fixture, "The reviewed candidate is complete.")
                return fixture
            case _ as unreachable:
                assert_never(unreachable)

    def named_receipt(
        self, result: JsonObject, action_kind: query_models.DamagedReceiptActionKind
    ) -> query_models.DamagedTransitionReceipt:
        """Rebuild the damaged receipt a rejected read names from its structured facts."""

        self.assertEqual("TRANSITION_RECEIPT_DAMAGED", result["code"], result)
        self.assertEqual(("unchanged", "do-not-retry"), (result["effect"], result["retry"]))
        observed = {
            self.json_object(value)["field"]: self.json_object(value)["value"]
            for value in self.json_array(result["observed"])
        }
        self.assertEqual(action_kind.value, observed["action_kind"])
        attempt_id, history_id, committed_at = observed["attempt_id"], observed["history_id"], observed["committed_at"]
        defect = self.json_object(self.json_array(result["mismatches"])[0])["observed"]
        assert isinstance(attempt_id, str) and isinstance(history_id, int)
        assert isinstance(committed_at, str) and isinstance(defect, str)
        return query_models.DamagedTransitionReceipt(
            AttemptId(attempt_id), HistoryId(history_id), datetime.fromisoformat(committed_at), action_kind, defect
        )

    def test_integration_leaf_names_damaged_consumed_candidate_receipts(self) -> None:
        for action_kind in (decision_models.ActionKind.ACCEPT_CHECKPOINT, decision_models.ActionKind.COMPLETE):
            with self.subTest(action_kind=action_kind.value):
                fixture = self.damaged_receipt_fixture(action_kind)
                history_id = self.latest_history_id(fixture)
                self.update_receipt(fixture, history_id, outcome_json="not json")

                result = self.integration_leaf(fixture, "main")
                damaged = self.named_receipt(result, action_kind)

                self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"])
                self.assertEqual((AttemptId("work-a-1"), history_id), (damaged.attempt_id, damaged.history_id))
                self.assertEqual(("unchanged", "do-not-retry"), (result["effect"], result["retry"]))

    def test_damaged_consumed_receipts_are_named_by_status_reads(self) -> None:
        damage = (
            ("outcome-schema", {"outcome_schema": "transition-receipt/v9"}),
            ("outcome-body", {"outcome_json": '{"unexpected":true}'}),
            ("outcome-json", {"outcome_json": "not json"}),
        )
        validation = query_models.DamagedReceiptDiagnosis.VALIDATION
        human = query_models.DamagedReceiptDiagnosis.HUMAN
        receipts: tuple[tuple[query_models.DamagedReceiptActionKind, query_models.DamagedReceiptDiagnosis], ...] = (
            (decision_models.ActionKind.PAUSE, validation),
            (decision_models.ActionKind.REBIND_ATTEMPT, validation),
            (decision_models.ActionKind.RETURN_FOR_CORRECTION, human),
            (decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE, human),
            (decision_models.ActionKind.ACCEPT_CHECKPOINT, human),
        )
        for receipt, diagnosis in receipts:
            for label, columns in damage:
                with self.subTest(receipt=receipt.value, damage=label):
                    fixture = self.damaged_receipt_fixture(receipt)
                    if receipt == decision_models.ActionKind.ACCEPT_CHECKPOINT and label == "outcome-schema":
                        columns = {"outcome_schema": "checkpoint-acceptance/v9"}
                    damaged_history = self.latest_history_id(fixture)
                    self.update_receipt(fixture, damaged_history, **columns)

                    status = self.item_leaf(fixture)
                    self.assertEqual("pinboard-mcp-item-status-result/v3", status["schema"])
                    named = self.named_receipt(status, receipt)
                    self.assertEqual((AttemptId("work-a-1"), damaged_history), (named.attempt_id, named.history_id))
                    self.assertEqual(diagnosis, queries.damaged_receipt_diagnosis(receipt))
                    self.assertEqual(queries.damaged_receipt_recovery(named), status["recovery"])
                    inspected = self.inspection(fixture)
                    validated, stdout, _stderr = self.run_cli(*fixture.common, "validate", "--json")
                    codes = {
                        self.json_object(value)["code"]
                        for value in self.json_array(self.json_object(json.loads(stdout))["diagnostics"])
                    }
                    match diagnosis:
                        case query_models.DamagedReceiptDiagnosis.VALIDATION:
                            self.assertEqual(named, self.named_receipt(inspected, receipt))
                            self.assertEqual(status["recovery"], inspected["recovery"])
                            self.assertEqual(10, validated, stdout)
                            self.assertIn(
                                "WORK_STATE_INVALID" if label == "outcome-json" else "TRANSITION_RECEIPT_DAMAGED", codes
                            )
                        case query_models.DamagedReceiptDiagnosis.HUMAN:
                            self.assertEqual("ok", inspected["status"], inspected)
                            self.assertNotIn("TRANSITION_RECEIPT_DAMAGED", codes)
                        case _ as unreachable:
                            assert_never(unreachable)

    def test_damaged_pause_receipt_is_named_by_view_generation(self) -> None:
        for label, columns in (
            ("outcome-schema", {"outcome_schema": "transition-receipt/v9"}),
            ("outcome-body", {"outcome_json": '{"unexpected":true}'}),
            ("outcome-json", {"outcome_json": "not json"}),
        ):
            with self.subTest(damage=label):
                fixture = self.checkpoint_fixture()
                self.return_for_review(fixture, "Rework the candidate.")
                self.transition(fixture, "pause:work-a-1", {"reason": "Wait for the maintainer."})
                history_id = self.latest_history_id(fixture)
                revised = self.revise_with_damage_before_refresh(fixture, history_id, columns)
                self.assertEqual("committed-with-warning", revised["status"], revised)
                self.assertEqual("Revised while paused", self.item_leaf_after_repair_title(fixture))
                warning = self.json_object(revised["warning"])
                self.assertIn(f"Transition receipt {history_id} ", str(warning["message"]))

                validated, validation, validation_error = self.run_cli(*fixture.common, "validate", "--json")
                self.assertEqual(10, validated, f"{validation}\n{validation_error}")
                diagnostics = self.json_array(self.json_object(json.loads(validation))["diagnostics"])
                codes = {self.json_object(value)["code"] for value in diagnostics}
                if label == "outcome-json":
                    self.assertEqual({"WORK_STATE_INVALID"}, codes)
                    with self.assertRaises(StorageError) as rejected:
                        self.run_cli(*fixture.common, "views", "rebuild")
                    self.assertEqual(StorageErrorCode.INVALID_STATE, rejected.exception.code)
                    continue
                named = next(
                    self.json_object(value)
                    for value in diagnostics
                    if self.json_object(value)["code"] == "TRANSITION_RECEIPT_DAMAGED"
                )
                self.assertIn(f"Transition receipt {history_id} ", str(named["message"]))
                rebuilt, stdout, stderr = self.run_cli(*fixture.common, "views", "rebuild")
                self.assertNotEqual(0, rebuilt)
                self.assertIn(f"Transition receipt {history_id} ", f"{stdout}{stderr}")

    def test_item_leaf_reports_closure_by_completion_and_direct_close(self) -> None:
        fixture = self.checkpoint_fixture()
        with patch("pinboard.cli.transitions.datetime") as clock:
            clock.now.return_value = CLOSED_AT
            self.close_prerequisite(fixture)
        closed = self.item_leaf(fixture, "work-c")
        self.assertEqual(
            {"action": "close", "committed_at": CLOSED_AT.isoformat(), "closing_attempt": None}, closed["closure"]
        )
        self.assertEqual("The prerequisite is satisfied.", closed["outcome_evidence"])
        self.complete(fixture, "Accepted and integrated by the maintainer.")
        completed = self.item_leaf(fixture)
        self.assertEqual(
            {
                "action": "complete",
                "committed_at": COMPLETED_AT.isoformat(),
                "closing_attempt": {
                    "attempt_id": "work-a-1",
                    "branch": fixture.brief.branch,
                    "candidate_revision": fixture.candidate_revision,
                },
            },
            completed["closure"],
        )
        self.assertEqual([], completed["attempts"])
        self.assertEqual({"kind": "none"}, completed["review_verdict"])
        receipts = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot().transition_receipts
        close_history, complete_history = (
            next(
                receipt.history_id
                for receipt in receipts
                if str(receipt.subject_id) == subject and receipt.action_kind.value == action
            )
            for subject, action in (("work-c", "close"), ("work-a-1", "complete"))
        )
        for item_id, history_id, outcome in (
            ("work-c", close_history, "done"),
            ("work-a", complete_history, "complete"),
        ):
            with self.subTest(retained_terminal_receipt=item_id):
                current = self.item_leaf(fixture, item_id)["closure"]
                self.update_receipt(
                    fixture,
                    int(history_id),
                    input_schema="decision/v1",
                    input_json="{}",
                    outcome_schema="transition-receipt/v1",
                    outcome_json=json.dumps({"evidence": "Retained terminal evidence.", "outcome": outcome}),
                )
                self.assertEqual(current, self.item_leaf(fixture, item_id)["closure"])
        self.update_receipt(fixture, int(close_history), outcome_json="not json")
        self.assertEqual("close", self.json_object(self.item_leaf(fixture, "work-c")["closure"])["action"])
        self.update_receipt(fixture, int(close_history), action_kind="mark-ready")
        self.assertIsNone(self.item_leaf(fixture, "work-c")["closure"])

    def test_branch_leaf_maps_live_kept_reused_and_rebound_branches(self) -> None:
        fixture = self.checkpoint_fixture()
        branch = fixture.brief.branch
        owners = self.branch_leaf(fixture, branch)
        self.assertEqual("pinboard-branch-owners/v1", owners["schema"], owners)
        self.assertEqual(
            [{"item_id": "work-a", "item_state": "review", "attempt_id": "work-a-1", "attempt_state": "review"}],
            owners["owners"],
        )
        unknown = self.branch_leaf(fixture, "codex/never-recorded")
        self.assertEqual("BRANCH_OWNER_NOT_FOUND", unknown["code"], unknown)
        self.assertEqual(("unchanged", "correct-input"), (unknown["effect"], unknown["retry"]))
        observed = {
            self.json_object(value)["field"]: self.json_object(value)["value"]
            for value in self.json_array(unknown["observed"])
        }
        self.assertEqual({"branch": "codex/never-recorded", "work_root": str(fixture.work)}, observed)

        rebound = "codex/work-a-rebound"
        self.return_for_review(fixture, "Rework on a new branch.")
        subprocess.run(["git", "switch", "-c", rebound], cwd=fixture.project, check=True, capture_output=True)
        self.rebind(fixture, rebound, 2)
        before_rebind = self.branch_leaf(fixture, branch)
        self.assertEqual("BRANCH_OWNER_NOT_FOUND", before_rebind["code"], before_rebind)
        self.assertEqual(unknown["recovery"], before_rebind["recovery"])
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(attempts)")]
            copied = ", ".join(
                {"attempt_id": "'work-b-kept'", "item_id": "'work-b'", "state": "'done'"}.get(column, column)
                for column in columns
            )
            connection.execute(
                f"INSERT INTO attempts ({', '.join(columns)}) "
                f"SELECT {copied} FROM attempts WHERE attempt_id = 'work-a-1'"
            )
        self.assertEqual(
            [
                {"item_id": "work-a", "item_state": "active", "attempt_id": "work-a-1", "attempt_state": "active"},
                {"item_id": "work-b", "item_state": "superseded", "attempt_id": "work-b-kept", "attempt_state": "done"},
            ],
            self.branch_leaf(fixture, rebound)["owners"],
        )

    def test_scratch_board_location_is_found_from_the_kept_branch(self) -> None:
        fixture = self.checkpoint_fixture()
        scratch = fixture.project.parent / "experiment-scratch" / ".pinboard"
        self.complete(fixture, f"Kept the experiment's changes; its scratch board is at {scratch}.")
        owners = self.json_array(self.branch_leaf(fixture, fixture.brief.branch)["owners"])
        owner = self.json_object(owners[0])
        self.assertEqual(("work-a", "done", "done"), (owner["item_id"], owner["item_state"], owner["attempt_state"]))
        status = self.item_leaf(fixture, str(owner["item_id"]))
        self.assertIn(str(scratch), str(status["outcome_evidence"]))
        self.assertEqual("complete", self.json_object(status["closure"])["action"])

    def test_intake_text_is_labeled_original_context_after_revision_and_start(self) -> None:
        fixture = self.checkpoint_fixture()
        self.revise_title(fixture, "Revised work")
        status = self.item_leaf(fixture)
        self.assertNotIn("next_action", status)
        self.assertNotIn("notes", status)
        intake = self.json_object(status["intake_context"])
        self.assertEqual("original-context", intake["label"])
        stored = next(
            value
            for value in SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot().lifecycle.work_items
            if str(value.item_id) == "work-a"
        )
        self.assertEqual((stored.next_action, stored.notes), (intake["next_action"], intake["notes"]))
        assert stored.next_action is not None
        view = (fixture.work / "views" / "items" / "work-a.md").read_text(encoding="utf-8")
        current, *sections = view.split("\n## ")
        self.assertNotIn(stored.next_action, current)
        intake_sections = [section for section in sections if stored.next_action in section]
        self.assertEqual(1, len(intake_sections))
        self.assertIn(stored.notes or "none", intake_sections[0])

    def test_old_flat_item_status_arguments_are_rejected(self) -> None:
        fixture = self.checkpoint_fixture()
        with self.assertRaises(ToolError):
            call_native_tool(mcp_server.ITEM_STATUS_TOOL, {**self.roots(fixture), "item_id": "work-a"})
