"""Native behavior coverage for the integration-by-content item-status leaf.

Success paths run against real temporary Git repositories with fixed commit
dates; rejection paths prove every typed code, observed fact, effect, retry,
and next step; adapter paths prove the one new Git read stays read-only and
config-independent.
"""

import contextlib
import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from pinboard.adapters.files import root as files_root
from pinboard.adapters.files.errors import RootError, RootErrorCode
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.candidate_identity import working_tree_identity
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import AcceptedPackageFixture, CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool
from tests.support import JsonObject, JsonValue

BASE_STAMP = "2026-07-01T09:00:00 +0000"
CANDIDATE_STAMP = "2026-07-02T09:00:00 +0000"
MERGE_STAMP = "2026-07-03T09:00:00 +0000"
LATER_STAMP = "2026-07-04T09:00:00 +0000"
COMPLETED_AT = datetime(2030, 1, 3, 4, 5, 6, tzinfo=UTC)
EMPTY_DIFF_RECORDED_AT = datetime(2031, 2, 3, 4, 5, 6, tzinfo=UTC)


def run_git(project: Path, *arguments: str, stamp: str | None = None) -> str:
    environment = dict(os.environ)
    if stamp is not None:
        environment.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Pinboard Tests",
            "-c",
            "user.email=pinboard@example.invalid",
            *arguments,
        ],
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {arguments} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout


def write_file(project: Path, name: str, content: str) -> None:
    (project / name).write_text(content, encoding="utf-8")


def commit_all_dated(project: Path, message: str, stamp: str) -> str:
    run_git(project, "add", "--all")
    run_git(project, "commit", "-m", message, stamp=stamp)
    return run_git(project, "rev-parse", "HEAD").strip()


def committed_candidate_repository() -> tuple[Path, str, str, bytes]:
    project = Path(tempfile.mkdtemp()).resolve()
    run_git(project, "init", "-b", "target-branch")
    (project / ".git" / "info" / "exclude").write_text("/.pinboard/\n", encoding="utf-8")
    write_file(project, "tracked.txt", "base\n")
    base = commit_all_dated(project, "base", BASE_STAMP)
    write_file(project, "tracked.txt", "candidate\n")
    candidate = commit_all_dated(project, "candidate", CANDIDATE_STAMP)
    diff = subprocess.run(
        ["git", "diff", "--binary", base, candidate, "--"],
        cwd=project,
        check=True,
        capture_output=True,
    ).stdout
    return project, base, candidate, diff


def temporary_integration_directories() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("pinboard-integration-*"))


class ItemIntegrationTest(CheckpointPackageSupport):
    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def item_leaf(self, fixture: CheckpointFixture, item_id: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "item", "item_id": item_id}},
        )

    def integration_leaf(self, fixture: CheckpointFixture, target: str, *, item_id: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": item_id, "target": target}},
        )

    @contextlib.contextmanager
    def fixed_dates(self, stamp: str = BASE_STAMP) -> Generator[None]:
        with patch.dict(os.environ, {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}):
            yield

    def integration_fixture(
        self, *, candidate_form: Literal["working-tree", "current-head"] = "working-tree"
    ) -> CheckpointFixture:
        with self.fixed_dates():
            return self.checkpoint_fixture(candidate_form=candidate_form)

    def package_fixture(self) -> AcceptedPackageFixture:
        with self.fixed_dates():
            return self.accepted_package_fixture()

    def git_head(self, project: Path) -> str:
        return run_git(project, "rev-parse", "HEAD").strip()

    def commit_dated(self, project: Path, message: str, stamp: str, *paths: str) -> str:
        if paths:
            run_git(project, "add", "--", *paths, stamp=stamp)
        run_git(project, "commit", "-m", message, stamp=stamp)
        return self.git_head(project)

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

    def execute_sql(self, fixture: CheckpointFixture, statement: str, parameters: tuple[object, ...] = ()) -> None:
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(statement, parameters)

    def project_revision(self, fixture: CheckpointFixture) -> int:
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            revision = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()[0]
        assert isinstance(revision, int)
        return revision

    def transition(self, fixture: CheckpointFixture, action_id: str, payload: JsonObject) -> JsonObject:
        result = self.transition_result(fixture, self.project_action(fixture, action_id), payload)
        self.assertIn(result["status"], ("committed", "committed-with-warning"), result)
        return result

    def assert_rejected(self, result: JsonObject, code: str) -> JsonObject:
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual("rejected", result["status"], result)
        self.assertEqual(code, result["code"], result)
        self.assertEqual(False, result["state_changed"], result)
        self.assertEqual("unchanged", result["effect"], result)
        self.assertEqual([], result["mismatches"], result)
        self.assertEqual([], result["changed_surfaces"], result)
        return result

    def observed_map(self, rejected: JsonObject) -> dict[str, JsonValue]:
        observed = self.json_array(rejected["observed"])
        return {str(self.json_object(fact)["field"]): self.json_object(fact)["value"] for fact in observed}

    @contextlib.contextmanager
    def record_store_reads(self) -> Generator[tuple[set[str], list[str]]]:
        read_tables: set[str] = set()
        statements: list[str] = []
        original_open = sqlite_store.open_database

        def traced_open(path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open(path, mode)

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

    def assert_keyed_status_queries(self, database: Path, statements: list[str]) -> None:
        self.assertEqual(1, sum(statement == "BEGIN" for statement in statements), statements)
        selects = tuple(statement for statement in statements if statement.lstrip().upper().startswith("SELECT"))
        self.assertTrue(selects)
        connection = sqlite3.connect(database)
        try:
            for statement in selects:
                with self.subTest(statement=statement):
                    plan = tuple(
                        str(row[3]).upper() for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}").fetchall()
                    )
                    self.assertTrue(any("SEARCH " in detail for detail in plan), plan)
                    self.assertFalse(any("SCAN " in detail for detail in plan), plan)
        finally:
            connection.close()

    def assert_result(
        self,
        fixture: CheckpointFixture,
        result: JsonObject,
        *,
        target: str,
        resolved_revision: str,
        source_kind: str,
        candidate_revision: str,
        compared_from_revision: str,
        presence: str,
        checkpoint_id: str | None = None,
    ) -> None:
        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual("sqlite-v7", result["authority"], result)
        self.assertEqual(str(self.project_revision(fixture)), result["revision"], result)
        self.assertEqual("work-a", result["item_id"], result)
        self.assertEqual(target, result["target"], result)
        self.assertEqual(resolved_revision, result["resolved_revision"], result)
        self.assertEqual(presence, result["presence"], result)
        source = self.json_object(result["source"])
        self.assertEqual(source_kind, source["kind"], result)
        self.assertEqual("work-a-1", source["attempt_id"], result)
        self.assertEqual(candidate_revision, source["candidate_revision"], result)
        self.assertEqual(compared_from_revision, source["compared_from_revision"], result)
        if checkpoint_id is None:
            self.assertNotIn("checkpoint_id", source, result)
        else:
            self.assertEqual(checkpoint_id, source["checkpoint_id"], result)

    def test_protected_working_tree_candidate_committed_then_squash_merged(self) -> None:
        fixture = self.integration_fixture()
        base = self.git_head(fixture.project)
        candidate = self.commit_dated(fixture.project, "candidate", CANDIDATE_STAMP, "tracked.txt")
        run_git(fixture.project, "branch", "main", base)
        run_git(fixture.project, "checkout", "-q", "main")
        run_git(fixture.project, "merge", "--squash", candidate, stamp=MERGE_STAMP)
        squash = self.commit_dated(fixture.project, "Squash candidate", MERGE_STAMP)
        result = self.integration_leaf(fixture, "main")
        self.assert_result(
            fixture,
            result,
            target="main",
            resolved_revision=squash,
            source_kind="protected-review",
            candidate_revision=fixture.candidate_revision,
            compared_from_revision=base,
            presence="content-present",
        )

    def test_protected_commit_candidate_fast_forwarded_into_main(self) -> None:
        fixture = self.integration_fixture(candidate_form="current-head")
        base = fixture.brief.base_revision
        candidate = self.git_head(fixture.project)
        run_git(fixture.project, "branch", "main", base)
        run_git(fixture.project, "checkout", "-q", "main")
        run_git(fixture.project, "merge", "--ff-only", candidate, stamp=MERGE_STAMP)
        self.assert_result(
            fixture,
            self.integration_leaf(fixture, "main"),
            target="main",
            resolved_revision=candidate,
            source_kind="protected-review",
            candidate_revision=fixture.candidate_revision,
            compared_from_revision=base,
            presence="content-present",
        )

    def test_protected_commit_candidate_merged_by_merge_commit(self) -> None:
        fixture = self.integration_fixture(candidate_form="current-head")
        base = fixture.brief.base_revision
        candidate = self.git_head(fixture.project)
        run_git(fixture.project, "branch", "main", base)
        run_git(fixture.project, "checkout", "-q", "main")
        write_file(fixture.project, "other.txt", "unrelated\n")
        self.commit_dated(fixture.project, "context", MERGE_STAMP, "other.txt")
        run_git(fixture.project, "merge", "--no-ff", "--no-edit", candidate, stamp=MERGE_STAMP)
        merge = self.git_head(fixture.project)
        self.assert_result(
            fixture,
            self.integration_leaf(fixture, "main"),
            target="main",
            resolved_revision=merge,
            source_kind="protected-review",
            candidate_revision=fixture.candidate_revision,
            compared_from_revision=base,
            presence="content-present",
        )

    def test_protected_commit_candidate_merged_by_rebase(self) -> None:
        fixture = self.integration_fixture(candidate_form="current-head")
        base = fixture.brief.base_revision
        candidate = self.git_head(fixture.project)
        run_git(fixture.project, "branch", "main", base)
        run_git(fixture.project, "checkout", "-q", "main")
        write_file(fixture.project, "other.txt", "unrelated\n")
        self.commit_dated(fixture.project, "context", MERGE_STAMP, "other.txt")
        run_git(fixture.project, "checkout", "-q", fixture.brief.branch)
        run_git(fixture.project, "rebase", "main", stamp=MERGE_STAMP)
        rebased = self.git_head(fixture.project)
        self.assertNotEqual(candidate, rebased)
        run_git(fixture.project, "checkout", "-q", "main")
        run_git(fixture.project, "merge", "--ff-only", rebased, stamp=MERGE_STAMP)
        self.assert_result(
            fixture,
            self.integration_leaf(fixture, "main"),
            target="main",
            resolved_revision=rebased,
            source_kind="protected-review",
            candidate_revision=fixture.candidate_revision,
            compared_from_revision=base,
            presence="content-present",
        )

    def test_accepted_checkpoint_candidate_squash_merged_before_attempt_resumed(self) -> None:
        fixture = self.package_fixture()
        attempts = self.json_array(self.item_leaf(fixture)["attempts"])
        self.assertEqual("paused", self.json_object(attempts[0])["state"])
        base = self.git_head(fixture.project)
        candidate = self.commit_dated(fixture.project, "candidate", CANDIDATE_STAMP, "tracked.txt")
        run_git(fixture.project, "branch", "main", base)
        run_git(fixture.project, "checkout", "-q", "main")
        run_git(fixture.project, "merge", "--squash", candidate, stamp=MERGE_STAMP)
        squash = self.commit_dated(fixture.project, "Squash candidate", MERGE_STAMP)
        self.assert_result(
            fixture,
            self.integration_leaf(fixture, "main"),
            target="main",
            resolved_revision=squash,
            source_kind="accepted-checkpoint",
            candidate_revision=fixture.candidate_revision,
            compared_from_revision=base,
            presence="content-present",
            checkpoint_id=fixture.brief.checkpoint.checkpoint_id,
        )

    def test_completion_candidate(self) -> None:
        fixture = self.integration_fixture()
        self.complete(fixture, "Accepted and integrated by the maintainer.")
        base = self.git_head(fixture.project)
        candidate = self.commit_dated(fixture.project, "candidate", CANDIDATE_STAMP, "tracked.txt")
        run_git(fixture.project, "branch", "main", base)
        run_git(fixture.project, "checkout", "-q", "main")
        run_git(fixture.project, "merge", "--squash", candidate, stamp=MERGE_STAMP)
        squash = self.commit_dated(fixture.project, "Squash candidate", MERGE_STAMP)
        self.assert_result(
            fixture,
            self.integration_leaf(fixture, "main"),
            target="main",
            resolved_revision=squash,
            source_kind="completion",
            candidate_revision=fixture.candidate_revision,
            compared_from_revision=base,
            presence="content-present",
        )

    def test_content_present_after_later_non_overlapping_edit(self) -> None:
        fixture = self.integration_fixture()
        base = self.git_head(fixture.project)
        candidate = self.commit_dated(fixture.project, "candidate", CANDIDATE_STAMP, "tracked.txt")
        run_git(fixture.project, "branch", "main", base)
        run_git(fixture.project, "checkout", "-q", "main")
        run_git(fixture.project, "merge", "--squash", candidate, stamp=MERGE_STAMP)
        self.commit_dated(fixture.project, "Squash candidate", MERGE_STAMP)
        write_file(fixture.project, "other.txt", "unrelated\n")
        later = self.commit_dated(fixture.project, "later edit", LATER_STAMP, "other.txt")
        self.assert_result(
            fixture,
            self.integration_leaf(fixture, "main"),
            target="main",
            resolved_revision=later,
            source_kind="protected-review",
            candidate_revision=fixture.candidate_revision,
            compared_from_revision=base,
            presence="content-present",
        )

    def test_content_not_present_after_later_overlapping_edit(self) -> None:
        fixture = self.integration_fixture()
        base = self.git_head(fixture.project)
        candidate = self.commit_dated(fixture.project, "candidate", CANDIDATE_STAMP, "tracked.txt")
        run_git(fixture.project, "branch", "main", base)
        run_git(fixture.project, "checkout", "-q", "main")
        run_git(fixture.project, "merge", "--squash", candidate, stamp=MERGE_STAMP)
        self.commit_dated(fixture.project, "Squash candidate", MERGE_STAMP)
        write_file(fixture.project, "tracked.txt", "overwritten\n")
        later = self.commit_dated(fixture.project, "overlapping edit", LATER_STAMP, "tracked.txt")
        self.assert_result(
            fixture,
            self.integration_leaf(fixture, "main"),
            target="main",
            resolved_revision=later,
            source_kind="protected-review",
            candidate_revision=fixture.candidate_revision,
            compared_from_revision=base,
            presence="content-not-present",
        )

    def test_content_not_present_at_accepted_base(self) -> None:
        fixture = self.integration_fixture()
        base = self.git_head(fixture.project)
        for target in ("codex/work-a", base):
            with self.subTest(target=target):
                self.assert_result(
                    fixture,
                    self.integration_leaf(fixture, target),
                    target=target,
                    resolved_revision=base,
                    source_kind="protected-review",
                    candidate_revision=fixture.candidate_revision,
                    compared_from_revision=base,
                    presence="content-not-present",
                )

    def test_no_change_for_empty_recorded_diff(self) -> None:
        fixture = self.integration_fixture()
        base = self.git_head(fixture.project)
        subprocess.run(["git", "checkout", "--", "tracked.txt"], cwd=fixture.project, check=True, capture_output=True)
        candidate = working_tree_identity(base, b"")
        state = fixture.store.validated_snapshot()
        attempt = next(value for value in state.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        self.accept_candidate_snapshot(
            resolve_durable_roots(fixture.project, fixture.work),
            fixture.store,
            attempt,
            "working-tree",
            candidate,
            b"",
            base,
            base,
            EMPTY_DIFF_RECORDED_AT,
        )
        self.execute_sql(
            fixture,
            "UPDATE attempts SET candidate_revision = ?, candidate_recorded_at = ? WHERE attempt_id = 'work-a-1'",
            (candidate, EMPTY_DIFF_RECORDED_AT.isoformat()),
        )
        self.assert_result(
            fixture,
            self.integration_leaf(fixture, "codex/work-a"),
            target="codex/work-a",
            resolved_revision=base,
            source_kind="protected-review",
            candidate_revision=candidate,
            compared_from_revision=base,
            presence="no-change",
        )

    def test_remote_tracking_ref_target_reads_local_ref(self) -> None:
        fixture = self.integration_fixture()
        base = self.git_head(fixture.project)
        run_git(fixture.project, "update-ref", "refs/remotes/origin/main", base)
        self.assert_result(
            fixture,
            self.integration_leaf(fixture, "origin/main"),
            target="origin/main",
            resolved_revision=base,
            source_kind="protected-review",
            candidate_revision=fixture.candidate_revision,
            compared_from_revision=base,
            presence="content-not-present",
        )

    def test_unresolved_target_rejection(self) -> None:
        fixture = self.integration_fixture()
        rejected = self.assert_rejected(
            self.integration_leaf(fixture, "does-not-exist"), "INTEGRATION_TARGET_UNRESOLVED"
        )
        self.assertEqual(
            f"Integration target 'does-not-exist' does not resolve to a commit in '{fixture.project}'.",
            rejected["message"],
            rejected,
        )
        self.assertEqual(
            {"target": "does-not-exist", "project_root": str(fixture.project)}, self.observed_map(rejected)
        )
        self.assertEqual("correct-input", rejected["retry"], rejected)
        self.assertTrue(str(rejected["recovery"]).startswith("Name an existing local branch"), rejected)
        self.assertIn("fetch", str(rejected["recovery"]), rejected)

    def test_unavailable_when_attempt_has_no_protected_candidate_or_checkpoint(self) -> None:
        fixture = self.package_fixture()
        self.execute_sql(
            fixture,
            "DELETE FROM transition_history WHERE subject_id = 'work-a-1' AND outcome_schema = 'checkpoint-acceptance/v2'",
        )
        rejected = self.assert_rejected(self.integration_leaf(fixture, "main"), "INTEGRATION_CANDIDATE_UNAVAILABLE")
        self.assertEqual(
            "Item 'work-a' in state 'paused' has no reviewed candidate with accepted snapshot bytes: "
            "the current attempt has no protected candidate and no checkpoint acceptance.",
            rejected["message"],
            rejected,
        )
        self.assertEqual({"item_id": "work-a", "item_state": "paused"}, self.observed_map(rejected))

    def test_unavailable_when_item_was_closed_directly(self) -> None:
        fixture = self.integration_fixture()
        self.close_prerequisite(fixture)
        rejected = self.assert_rejected(
            self.integration_leaf(fixture, "main", item_id="work-c"), "INTEGRATION_CANDIDATE_UNAVAILABLE"
        )
        self.assertEqual(
            "Item 'work-c' in state 'done' has no reviewed candidate with accepted snapshot bytes: "
            "it was closed directly without a completion.",
            rejected["message"],
            rejected,
        )
        self.assertEqual({"item_id": "work-c", "item_state": "done"}, self.observed_map(rejected))

    def test_unavailable_when_checkpoint_acceptance_has_no_snapshot_reference(self) -> None:
        fixture = self.package_fixture()
        checkpoint_id = fixture.brief.checkpoint.checkpoint_id
        self.execute_sql(
            fixture,
            "UPDATE artifact_refs SET artifact_key = artifact_key || '-orphaned' WHERE artifact_key = ?",
            (f"work-a-1-{checkpoint_id}-candidate",),
        )
        rejected = self.assert_rejected(self.integration_leaf(fixture, "main"), "INTEGRATION_CANDIDATE_UNAVAILABLE")
        self.assertEqual(
            "Item 'work-a' in state 'paused' has no reviewed candidate with accepted snapshot bytes: "
            "the latest checkpoint acceptance has no candidate snapshot reference.",
            rejected["message"],
            rejected,
        )
        self.assertEqual({"item_id": "work-a", "item_state": "paused"}, self.observed_map(rejected))

    def test_unavailable_when_live_attempt_retains_pre_snapshot_candidate(self) -> None:
        fixture = self.integration_fixture()
        self.execute_sql(
            fixture,
            "UPDATE artifact_refs SET artifact_key = artifact_key || '-orphaned'"
            " WHERE kind = 'evidence' AND artifact_key LIKE 'work-a-1-candidate-snapshot-%'",
        )
        rejected = self.assert_rejected(self.integration_leaf(fixture, "main"), "INTEGRATION_CANDIDATE_UNAVAILABLE")
        self.assertEqual(
            "Item 'work-a' in state 'review' has no reviewed candidate with accepted snapshot bytes: "
            "its current attempt retains a pre-snapshot review candidate.",
            rejected["message"],
            rejected,
        )
        self.assertEqual({"item_id": "work-a", "item_state": "review"}, self.observed_map(rejected))

    def test_unavailable_when_completing_attempt_retains_pre_snapshot_candidate(self) -> None:
        fixture = self.integration_fixture()
        self.complete(fixture, "Accepted and integrated by the maintainer.")
        self.execute_sql(
            fixture,
            "UPDATE artifact_refs SET artifact_key = artifact_key || '-orphaned'"
            " WHERE kind = 'evidence' AND artifact_key LIKE 'work-a-1-candidate-snapshot-%'",
        )
        rejected = self.assert_rejected(self.integration_leaf(fixture, "main"), "INTEGRATION_CANDIDATE_UNAVAILABLE")
        self.assertEqual(
            "Item 'work-a' in state 'done' has no reviewed candidate with accepted snapshot bytes: "
            "its completing attempt retains a pre-snapshot review candidate.",
            rejected["message"],
            rejected,
        )
        self.assertEqual({"item_id": "work-a", "item_state": "done"}, self.observed_map(rejected))

    def test_evidence_invalid_rejection(self) -> None:
        fixture = self.integration_fixture()
        context = SQLiteWorkStore(fixture.work / "state.sqlite3").read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        self.replace_artifact_bytes(fixture, context.reference, b"damaged\n")
        rejected = self.assert_rejected(
            self.integration_leaf(fixture, "codex/work-a"), "INTEGRATION_CANDIDATE_EVIDENCE_INVALID"
        )
        self.assertTrue(
            str(rejected["message"]).startswith(
                "The accepted candidate evidence for attempt 'work-a-1' did not verify:"
            ),
            rejected,
        )
        self.assertEqual(
            {
                "attempt_id": "work-a-1",
                "candidate_revision": fixture.candidate_revision,
                "artifact_key": context.reference.key,
                "artifact_revision": str(context.reference.revision),
            },
            self.observed_map(rejected),
        )
        self.assertEqual("do-not-retry", rejected["retry"], rejected)
        self.assertIn("pinboard validate", str(rejected["recovery"]), rejected)

    def test_git_unavailable_rejection_carries_root_error_code(self) -> None:
        fixture = self.integration_fixture()
        unavailable = RootError(
            RootErrorCode.PROJECT_GIT_ROOT_UNAVAILABLE,
            f"'{fixture.project}' is not inside a Git checkout.",
        )
        with patch("pinboard.mcp.read_operations.resolve_source_checkout_root", side_effect=unavailable):
            rejected = self.assert_rejected(self.integration_leaf(fixture, "main"), "PROJECT_GIT_ROOT_UNAVAILABLE")
        self.assertEqual(
            f"Cannot judge integration into target 'main': '{fixture.project}' is not inside a Git checkout.",
            rejected["message"],
            rejected,
        )
        self.assertEqual({"project_root": str(fixture.project), "target": "main"}, self.observed_map(rejected))
        self.assertEqual("do-not-retry", rejected["retry"], rejected)
        self.assertEqual(
            "Correct the Git checkout at the project root, then retry the integration read.", rejected["recovery"]
        )

    def test_unknown_item_rejection(self) -> None:
        fixture = self.integration_fixture()
        rejected = self.assert_rejected(
            self.integration_leaf(fixture, "main", item_id="missing-item"), "ITEM_NOT_FOUND"
        )
        self.assertEqual("Item 'missing-item' was not found.", rejected["message"], rejected)
        self.assertNotIn("recovery", rejected, rejected)
        self.assertEqual([], rejected["observed"], rejected)

    def test_invalid_target_rejection(self) -> None:
        fixture = self.integration_fixture()
        rejected = self.assert_rejected(self.integration_leaf(fixture, "-checkout"), "ITEM_STATUS_INVALID")
        self.assertTrue(str(rejected["message"]).startswith("Cannot read item status:"), rejected)
        self.assertNotIn("recovery", rejected, rejected)
        self.assertEqual("correct-input", rejected["retry"], rejected)

    def test_integration_reads_only_protected_candidate_rows(self) -> None:
        fixture = self.integration_fixture()
        with (
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")),
            self.record_store_reads() as reads,
        ):
            result = self.integration_leaf(fixture, "codex/work-a")
        self.assertEqual("content-not-present", result["presence"], result)
        read_tables, statements = reads
        self.assertEqual({"project_meta", "work_items", "attempts", "artifact_refs"}, read_tables)
        self.assertNotIn("transition_history", read_tables)
        self.assert_keyed_status_queries(fixture.work / "state.sqlite3", statements)

    def test_integration_reads_checkpoint_receipt_through_partial_index(self) -> None:
        fixture = self.package_fixture()
        with (
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")),
            self.record_store_reads() as reads,
        ):
            result = self.integration_leaf(fixture, "codex/work-a")
        self.assertEqual("content-not-present", result["presence"], result)
        self.assertEqual("accepted-checkpoint", self.json_object(result["source"])["kind"], result)
        read_tables, statements = reads
        self.assertEqual({"project_meta", "work_items", "attempts", "artifact_refs", "transition_history"}, read_tables)
        self.assert_keyed_status_queries(fixture.work / "state.sqlite3", statements)


class IntegrationTargetObservationTest(unittest.TestCase):
    def tracked_repository(self) -> tuple[Path, str, str, bytes]:
        project, base, candidate, diff = committed_candidate_repository()
        self.addCleanup(shutil.rmtree, project, True)
        return project, base, candidate, diff

    def working_status(self, project: Path) -> str:
        return subprocess.run(
            ["git", "--no-optional-locks", "status", "--porcelain"],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_reads_target_tree_through_a_private_index_only(self) -> None:
        project, _base, candidate, diff = self.tracked_repository()
        index = project / ".git" / "index"
        head = project / ".git" / "HEAD"
        before = (
            index.read_bytes(),
            head.read_bytes(),
            self.working_status(project),
            run_git(project, "for-each-ref"),
        )
        temporary = temporary_integration_directories()
        with patch("pinboard.adapters.files.root.subprocess.run", wraps=subprocess.run) as git_calls:
            observed = files_root.observe_integration_target(project, "target-branch", diff)
        commands = [tuple(call.args[0]) for call in git_calls.call_args_list]
        self.assertEqual(
            [
                ("git", "rev-parse", "--verify", "--quiet", "target-branch^{commit}"),
                ("git", "-c", "core.splitIndex=false", "read-tree", candidate),
                (
                    "git",
                    "-c",
                    "core.splitIndex=false",
                    "-c",
                    "apply.whitespace=nowarn",
                    "apply",
                    "--cached",
                    "--check",
                    "--reverse",
                    "--ignore-whitespace",
                    "-",
                ),
            ],
            commands,
        )
        self.assertFalse(any("fetch" in command for command in commands))
        private_index_files: list[Path] = []
        for call in git_calls.call_args_list:
            environment = call.kwargs.get("env")
            if isinstance(environment, dict):
                value = environment.get("GIT_INDEX_FILE")
                if isinstance(value, str):
                    private_index_files.append(Path(value))
        self.assertEqual(2, len(private_index_files))
        for value in private_index_files:
            self.assertEqual("index", value.name)
            self.assertNotEqual(index, value)
        assert isinstance(observed, files_root.IntegrationTargetPresence)
        self.assertTrue(observed.present)
        self.assertEqual(candidate, observed.resolved_revision)
        self.assertEqual(
            before,
            (
                index.read_bytes(),
                head.read_bytes(),
                self.working_status(project),
                run_git(project, "for-each-ref"),
            ),
        )
        self.assertEqual(temporary, temporary_integration_directories())

    def test_empty_diff_is_decided_without_reading_the_tree(self) -> None:
        project, _base, candidate, _diff = self.tracked_repository()
        with patch("pinboard.adapters.files.root.subprocess.run", wraps=subprocess.run) as git_calls:
            observed = files_root.observe_integration_target(project, "target-branch", b"")
        commands = [tuple(call.args[0]) for call in git_calls.call_args_list]
        self.assertEqual([("git", "rev-parse", "--verify", "--quiet", "target-branch^{commit}")], commands)
        assert isinstance(observed, files_root.IntegrationTargetPresence)
        self.assertTrue(observed.present)
        self.assertEqual(candidate, observed.resolved_revision)

    def test_unresolved_target_when_checkout_is_healthy(self) -> None:
        project, _base, _candidate, diff = self.tracked_repository()
        observed = files_root.observe_integration_target(project, "missing-target", diff)
        assert isinstance(observed, files_root.UnresolvedIntegrationTarget)
        self.assertEqual("missing-target", observed.target)

    def test_invalid_target_names_are_rejected_before_git(self) -> None:
        project, _base, _candidate, diff = self.tracked_repository()
        for target in ("", "-checkout", "a\nb", "a\x00b"):
            with self.subTest(target=target):
                with self.assertRaises(RootError) as raised:
                    files_root.observe_integration_target(project, target, diff)
                self.assertEqual(RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE, raised.exception.code)

    def test_read_only_git_directory_is_sufficient(self) -> None:
        project, _base, candidate, diff = self.tracked_repository()
        modes: list[tuple[Path, int]] = []
        for path in sorted((project / ".git").rglob("*")):
            modes.append((path, path.stat().st_mode))
            path.chmod(0o555 if path.is_dir() else 0o444)

        def restore() -> None:
            for path, mode in reversed(modes):
                path.chmod(mode)

        self.addCleanup(restore)
        observed = files_root.observe_integration_target(project, "target-branch", diff)
        assert isinstance(observed, files_root.IntegrationTargetPresence)
        self.assertTrue(observed.present)
        self.assertEqual(candidate, observed.resolved_revision)

    def test_whitespace_and_split_index_config_independence(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, project, True)
        run_git(project, "init", "-b", "target-branch")
        (project / ".git" / "info" / "exclude").write_text("/.pinboard/\n", encoding="utf-8")
        write_file(project, "tracked.txt", "a b \nb\n")
        base = commit_all_dated(project, "base", BASE_STAMP)
        write_file(project, "tracked.txt", "a b\nc\n")
        candidate = commit_all_dated(project, "candidate", CANDIDATE_STAMP)
        diff = subprocess.run(
            ["git", "diff", "--binary", base, candidate, "--"],
            cwd=project,
            check=True,
            capture_output=True,
        ).stdout
        run_git(project, "config", "core.splitIndex", "true")
        run_git(project, "config", "apply.whitespace", "error")
        run_git(project, "reset", "--hard", base)
        write_file(project, "tracked.txt", "a  b\nc\n")
        target_head = commit_all_dated(project, "same change with reflowed context whitespace", LATER_STAMP)
        observed = files_root.observe_integration_target(project, "target-branch", diff)
        assert isinstance(observed, files_root.IntegrationTargetPresence)
        self.assertTrue(observed.present)
        self.assertEqual(target_head, observed.resolved_revision)
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
            subprocess.run(
                ["git", "-c", "core.splitIndex=false", "read-tree", target_head],
                cwd=project,
                capture_output=True,
                check=True,
                env=environment,
            )
            without_whitespace_policy = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.splitIndex=false",
                    "apply",
                    "--cached",
                    "--check",
                    "--reverse",
                    "--ignore-whitespace",
                    "-",
                ],
                cwd=project,
                input=diff,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertNotEqual(0, without_whitespace_policy.returncode, without_whitespace_policy.stderr)
            without_ignore = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.splitIndex=false",
                    "-c",
                    "apply.whitespace=nowarn",
                    "apply",
                    "--cached",
                    "--check",
                    "--reverse",
                    "-",
                ],
                cwd=project,
                input=diff,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertNotEqual(0, without_ignore.returncode, without_ignore.stderr)

    def test_rename_and_binary_file_change_presence(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, project, True)
        run_git(project, "init", "-b", "target-branch")
        (project / ".git" / "info" / "exclude").write_text("/.pinboard/\n", encoding="utf-8")
        write_file(project, "old-name.txt", "one\nshared context\ntwo\n")
        (project / "blob.bin").write_bytes(b"\x00\x01binary base\xff\xfe")
        base = commit_all_dated(project, "base", BASE_STAMP)
        (project / "old-name.txt").rename(project / "new-name.txt")
        write_file(project, "new-name.txt", "one\nshared context\nchanged\n")
        (project / "blob.bin").write_bytes(b"\x00\x01binary candidate\xf0\x0f")
        candidate = commit_all_dated(project, "rename and binary change", CANDIDATE_STAMP)
        diff = subprocess.run(
            ["git", "diff", "--binary", "-M", base, candidate, "--"],
            cwd=project,
            check=True,
            capture_output=True,
        ).stdout
        self.assertIn(b"rename from", diff)
        self.assertIn(b"GIT binary patch", diff)
        run_git(project, "branch", "with-change", candidate)
        run_git(project, "reset", "--hard", base)
        present = files_root.observe_integration_target(project, "with-change", diff)
        assert isinstance(present, files_root.IntegrationTargetPresence)
        self.assertTrue(present.present)
        self.assertEqual(candidate, present.resolved_revision)
        absent = files_root.observe_integration_target(project, "target-branch", diff)
        assert isinstance(absent, files_root.IntegrationTargetPresence)
        self.assertFalse(absent.present)
        self.assertEqual(base, absent.resolved_revision)

    def test_broken_checkout_reports_root_error(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, project, True)
        with self.assertRaises(RootError) as raised:
            files_root.observe_integration_target(project, "main", b"diff\n")
        self.assertEqual(RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
