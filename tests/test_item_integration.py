"""The integration leaf reports whether a reviewed candidate's content reached a named target."""

import contextlib
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import override
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError

from pinboard.adapters.files import root
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture
from tests.item_status_support import ItemStatusSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import JsonObject

FIXED_GIT_ENVIRONMENT = {
    "GIT_AUTHOR_NAME": "Pinboard Tests",
    "GIT_AUTHOR_EMAIL": "pinboard@example.invalid",
    "GIT_AUTHOR_DATE": "2030-02-03T04:05:06+0000",
    "GIT_COMMITTER_NAME": "Pinboard Tests",
    "GIT_COMMITTER_EMAIL": "pinboard@example.invalid",
    "GIT_COMMITTER_DATE": "2030-02-03T04:05:06+0000",
}
BASE_TEXT = "alpha\nbeta\ngamma\ndelta\nepsilon\nzeta\neta\ntheta\n"
REVIEWED_TEXT = BASE_TEXT.replace("beta", "BETA")
LATER_DISTANT_TEXT = REVIEWED_TEXT.replace("theta", "THETA")
LATER_OVERLAPPING_TEXT = REVIEWED_TEXT.replace("BETA", "Beta!")
PRESENT = "content-present"
NOT_PRESENT = "content-not-present"


class ItemIntegrationTest(ItemStatusSupport):
    @override
    def setUp(self) -> None:
        environment = patch.dict(os.environ, FIXED_GIT_ENVIRONMENT)
        environment.start()
        self.addCleanup(environment.stop)

    def git(self, cwd: Path, *arguments: str) -> str:
        return subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def integration(self, fixture: CheckpointFixture, target: str, item_id: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": item_id, "target": target}},
        )

    def target_branch(self, fixture: CheckpointFixture, branch: str, start: str) -> Path:
        """Check a new target branch out in its own linked worktree, leaving the candidate's checkout untouched."""

        worktree = Path(tempfile.mkdtemp()).resolve() / branch
        self.git(fixture.project, "worktree", "add", "-b", branch, str(worktree), start)
        return worktree

    def commit_text(self, worktree: Path, text: str, message: str) -> str:
        (worktree / "tracked.txt").write_text(text, encoding="utf-8")
        self.git(worktree, "commit", "--all", "-m", message)
        return self.git(worktree, "rev-parse", "HEAD")

    def commit_other(self, worktree: Path, message: str) -> str:
        (worktree / "other.txt").write_text(f"{message}\n", encoding="utf-8")
        self.git(worktree, "add", "other.txt")
        self.git(worktree, "commit", "-m", message)
        return self.git(worktree, "rev-parse", "HEAD")

    def assert_result(
        self,
        result: JsonObject,
        fixture: CheckpointFixture,
        *,
        presence: str,
        target: str,
        target_revision: str,
        source: dict[str, str],
    ) -> None:
        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual(
            {
                "authority": "sqlite-v7",
                "item_id": "work-a",
                "target": target,
                "target_revision": target_revision,
                "source": source,
                "presence": presence,
            },
            {key: value for key, value in result.items() if key not in {"schema", "revision"}},
        )
        self.assertEqual(self.item_leaf(fixture)["revision"], result["revision"])

    def protected_source(self, fixture: CheckpointFixture, compared_from: str) -> dict[str, str]:
        return {
            "kind": "protected-review",
            "attempt_id": "work-a-1",
            "candidate_revision": fixture.candidate_revision,
            "compared_from_revision": compared_from,
        }

    def assert_rejected(self, result: JsonObject, code: str, retry: str) -> None:
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual(("rejected", code, False), (result["status"], result["code"], result["state_changed"]))
        self.assertEqual(
            ("unchanged", retry, [], []),
            (result["effect"], result["retry"], result["changed_surfaces"], result["mismatches"]),
        )

    def observed(self, result: JsonObject) -> dict[str, object]:
        return {
            str(self.json_object(value)["field"]): self.json_object(value)["value"]
            for value in self.json_array(result["observed"])
        }

    def test_working_tree_candidate_is_found_after_a_squash_merge_and_later_edits(self) -> None:
        fixture = self.checkpoint_fixture(base_text=BASE_TEXT, candidate_text=REVIEWED_TEXT)
        base = self.git(fixture.project, "rev-parse", "HEAD")
        target = self.target_branch(fixture, "main", base)
        source = self.protected_source(fixture, base)
        self.assert_result(
            self.integration(fixture, "main"),
            fixture,
            presence=NOT_PRESENT,
            target="main",
            target_revision=base,
            source=source,
        )
        self.git(fixture.project, "commit", "--all", "-m", "candidate")
        self.git(target, "merge", "--squash", "codex/work-a")
        self.git(target, "commit", "-m", "squash")
        squashed = self.git(target, "rev-parse", "HEAD")
        self.assertNotEqual(self.git(fixture.project, "rev-parse", "HEAD"), squashed)
        self.assert_result(
            self.integration(fixture, "main"),
            fixture,
            presence=PRESENT,
            target="main",
            target_revision=squashed,
            source=source,
        )
        distant = self.commit_text(target, LATER_DISTANT_TEXT, "edit a distant line")
        self.assert_result(
            self.integration(fixture, "main"),
            fixture,
            presence=PRESENT,
            target="main",
            target_revision=distant,
            source=source,
        )
        overlapping = self.commit_text(target, LATER_OVERLAPPING_TEXT, "edit a reviewed line")
        self.assert_result(
            self.integration(fixture, "main"),
            fixture,
            presence=NOT_PRESENT,
            target="main",
            target_revision=overlapping,
            source=source,
        )

    def test_remote_tracking_ref_is_read_locally_without_fetching(self) -> None:
        fixture = self.checkpoint_fixture(base_text=BASE_TEXT, candidate_text=REVIEWED_TEXT)
        base = self.git(fixture.project, "rev-parse", "HEAD")
        target = self.target_branch(fixture, "main", base)
        integrated = self.commit_text(target, REVIEWED_TEXT, "squash")
        self.git(fixture.project, "update-ref", "refs/remotes/origin/main", base)
        source = self.protected_source(fixture, base)
        self.assertEqual("", self.git(fixture.project, "remote"))
        self.assert_result(
            self.integration(fixture, "origin/main"),
            fixture,
            presence=NOT_PRESENT,
            target="origin/main",
            target_revision=base,
            source=source,
        )
        self.git(fixture.project, "update-ref", "refs/remotes/origin/main", integrated)
        self.assert_result(
            self.integration(fixture, "origin/main"),
            fixture,
            presence=PRESENT,
            target="origin/main",
            target_revision=integrated,
            source=source,
        )
        self.assertEqual("", self.git(fixture.project, "remote"))

    def test_commit_candidate_is_found_by_fast_forward_merge_commit_and_rebase_merge(self) -> None:
        fixture = self.checkpoint_fixture(
            candidate_form="current-head", base_text=BASE_TEXT, candidate_text=REVIEWED_TEXT
        )
        candidate = fixture.candidate_revision
        base = self.git(fixture.project, "rev-parse", f"{candidate}^")
        source = {
            "kind": "protected-review",
            "attempt_id": "work-a-1",
            "candidate_revision": candidate,
            "compared_from_revision": base,
        }
        self.git(fixture.project, "branch", "at-base", base)
        self.assert_result(
            self.integration(fixture, "at-base"),
            fixture,
            presence=NOT_PRESENT,
            target="at-base",
            target_revision=base,
            source=source,
        )
        self.git(fixture.project, "branch", "fast-forwarded", candidate)
        self.assert_result(
            self.integration(fixture, "fast-forwarded"),
            fixture,
            presence=PRESENT,
            target="fast-forwarded",
            target_revision=candidate,
            source=source,
        )
        merged = self.target_branch(fixture, "merged", base)
        self.commit_other(merged, "unrelated work")
        self.git(merged, "merge", "--no-ff", "-m", "merge", candidate)
        merge_commit = self.git(merged, "rev-parse", "HEAD")
        self.assert_result(
            self.integration(fixture, "merged"),
            fixture,
            presence=PRESENT,
            target="merged",
            target_revision=merge_commit,
            source=source,
        )
        rebased = self.target_branch(fixture, "rebased", base)
        self.commit_other(rebased, "other unrelated work")
        self.git(rebased, "cherry-pick", candidate)
        rebased_commit = self.git(rebased, "rev-parse", "HEAD")
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", candidate, rebased_commit], cwd=fixture.project, check=False
        )
        self.assertEqual(1, ancestry.returncode)
        self.assert_result(
            self.integration(fixture, "rebased"),
            fixture,
            presence=PRESENT,
            target="rebased",
            target_revision=rebased_commit,
            source=source,
        )
        self.assert_result(
            self.integration(fixture, rebased_commit),
            fixture,
            presence=PRESENT,
            target=rebased_commit,
            target_revision=rebased_commit,
            source=source,
        )

    def test_empty_recorded_diff_reports_no_change_at_the_resolved_target(self) -> None:
        fixture = self.checkpoint_fixture(base_text=BASE_TEXT, candidate_text=BASE_TEXT)
        base = self.git(fixture.project, "rev-parse", "HEAD")
        self.assert_result(
            self.integration(fixture, "codex/work-a"),
            fixture,
            presence="no-change",
            target="codex/work-a",
            target_revision=base,
            source=self.protected_source(fixture, base),
        )

    def accepted_checkpoint_source(self, fixture: CheckpointFixture, compared_from: str) -> dict[str, str]:
        return {
            "kind": "accepted-checkpoint",
            "attempt_id": "work-a-1",
            "candidate_revision": fixture.candidate_revision,
            "compared_from_revision": compared_from,
            "checkpoint_id": fixture.brief.checkpoint.checkpoint_id,
        }

    def test_accepted_checkpoint_candidate_stays_the_source_through_pause_resume_rebind_and_return(self) -> None:
        fixture = self.accepted_package_fixture(base_text=BASE_TEXT, candidate_text=REVIEWED_TEXT)
        base = self.git(fixture.project, "rev-parse", "HEAD")
        target = self.target_branch(fixture, "main", base)
        source = self.accepted_checkpoint_source(fixture, base)
        self.assertEqual("paused", self.json_object(self.json_array(self.item_leaf(fixture)["attempts"])[0])["state"])
        self.assert_result(
            self.integration(fixture, "main"),
            fixture,
            presence=NOT_PRESENT,
            target="main",
            target_revision=base,
            source=source,
        )
        squashed = self.commit_text(target, REVIEWED_TEXT, "squash before resume")
        self.assert_result(
            self.integration(fixture, "main"),
            fixture,
            presence=PRESENT,
            target="main",
            target_revision=squashed,
            source=source,
        )
        self.close_prerequisite(fixture)
        self.transition(fixture, "resume:work-a", {})
        self.assert_result(
            self.integration(fixture, "main"),
            fixture,
            presence=PRESENT,
            target="main",
            target_revision=squashed,
            source=source,
        )
        self.rebind(fixture, fixture.brief.branch, 2)
        self.assert_result(
            self.integration(fixture, "main"),
            fixture,
            presence=PRESENT,
            target="main",
            target_revision=squashed,
            source=source,
        )
        next_candidate = self.submit_candidate(fixture, "next")
        protected = self.integration(fixture, "main")
        self.assertEqual("protected-review", self.json_object(protected["source"])["kind"])
        self.assertEqual(next_candidate, self.json_object(protected["source"])["candidate_revision"])
        self.assertEqual(NOT_PRESENT, protected["presence"])
        self.return_for_review(fixture, "Rework the candidate.")
        self.assert_result(
            self.integration(fixture, "main"),
            fixture,
            presence=PRESENT,
            target="main",
            target_revision=squashed,
            source=source,
        )

    def test_completion_candidate_is_found_in_the_target(self) -> None:
        fixture = self.checkpoint_fixture(base_text=BASE_TEXT, candidate_text=REVIEWED_TEXT)
        base = self.git(fixture.project, "rev-parse", "HEAD")
        target = self.target_branch(fixture, "main", base)
        self.complete(fixture, "Accepted and integrated by the maintainer.")
        source = {
            "kind": "completion",
            "attempt_id": "work-a-1",
            "candidate_revision": fixture.candidate_revision,
            "compared_from_revision": base,
        }
        self.assert_result(
            self.integration(fixture, "main"),
            fixture,
            presence=NOT_PRESENT,
            target="main",
            target_revision=base,
            source=source,
        )
        squashed = self.commit_text(target, REVIEWED_TEXT, "squash")
        self.assert_result(
            self.integration(fixture, "main"),
            fixture,
            presence=PRESENT,
            target="main",
            target_revision=squashed,
            source=source,
        )

    def test_candidate_selection_follows_each_lifecycle_operation(self) -> None:
        fixture = self.checkpoint_fixture(base_text=BASE_TEXT, candidate_text=REVIEWED_TEXT)
        self.git(fixture.project, "branch", "main")
        self.assertEqual("protected-review", self.json_object(self.integration(fixture, "main")["source"])["kind"])
        self.return_for_review(fixture, "Rework the candidate.")
        returned = self.integration(fixture, "main")
        self.assert_rejected(returned, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.assertEqual(
            {"item_id": "work-a", "item_state": "active", "reason": "no-reviewed-candidate"}, self.observed(returned)
        )
        self.assertIsInstance(returned["recovery"], str)
        continued = self.checkpoint_fixture(base_text=BASE_TEXT, candidate_text=REVIEWED_TEXT)
        self.git(continued.project, "branch", "main")
        self.transition(
            continued,
            "accept-review-and-continue:work-a-1",
            {"candidate": continued.candidate_revision, "evidence": "Accepted; continue the attempt."},
        )
        after_continue = self.integration(continued, "main")
        self.assert_rejected(after_continue, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.assertEqual("no-reviewed-candidate", self.observed(after_continue)["reason"])

    def test_items_without_a_reviewed_candidate_name_their_state(self) -> None:
        fixture = self.checkpoint_fixture()
        self.git(fixture.project, "branch", "main")
        for item_id, reason in (("work-c", "no-reviewed-candidate"), ("work-b", "closed-without-completion")):
            with self.subTest(item=item_id):
                rejected = self.integration(fixture, "main", item_id)
                self.assert_rejected(rejected, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
                self.assertEqual(
                    {"item_id": item_id, "item_state": self.item_leaf(fixture, item_id)["state"], "reason": reason},
                    self.observed(rejected),
                )
        self.close_prerequisite(fixture)
        closed = self.integration(fixture, "main", "work-c")
        self.assert_rejected(closed, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.assertEqual(
            {"item_id": "work-c", "item_state": "done", "reason": "closed-without-completion"}, self.observed(closed)
        )

    def test_rejections_name_their_facts_effect_retry_and_next_step(self) -> None:
        fixture = self.checkpoint_fixture(base_text=BASE_TEXT, candidate_text=REVIEWED_TEXT)
        self.git(fixture.project, "branch", "main")
        unresolved = self.integration(fixture, "no-such-ref")
        self.assert_rejected(unresolved, "INTEGRATION_TARGET_UNRESOLVED", "correct-input")
        self.assertEqual({"target": "no-such-ref", "project_root": str(fixture.project)}, self.observed(unresolved))
        self.assertIn("fetch", str(unresolved["recovery"]).lower())
        leading_dash = self.integration(fixture, "--output=injected")
        self.assert_rejected(leading_dash, "ITEM_STATUS_INVALID", "correct-input")
        self.assertFalse((fixture.project / "injected").exists())
        unknown = self.integration(fixture, "main", "no-such-item")
        self.assert_rejected(unknown, "ITEM_NOT_FOUND", "correct-input")
        self.assertNotIn("recovery", unknown)
        outside = Path(tempfile.mkdtemp()).resolve()
        outside_checkout = call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
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
        self.assert_rejected(outside_checkout, "PROJECT_GIT_ROOT_UNAVAILABLE", "correct-input")
        observed = self.observed(outside_checkout)
        self.assertEqual(str(outside), observed["project_root"])
        self.assertIn("not a git repository", str(observed["diagnostic"]).lower())

    def test_altered_snapshot_bytes_are_invalid_evidence(self) -> None:
        fixture = self.checkpoint_fixture(base_text=BASE_TEXT, candidate_text=REVIEWED_TEXT)
        self.git(fixture.project, "branch", "main")
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        snapshot = fixture.work / context.reference.selector
        original = snapshot.read_bytes()
        snapshot.write_bytes(original + b"\n")
        rejected = self.integration(fixture, "main")
        self.assert_rejected(rejected, "INTEGRATION_CANDIDATE_EVIDENCE_INVALID", "do-not-retry")
        self.assertEqual(
            {
                "attempt_id": "work-a-1",
                "artifact_ref_id": int(context.reference.artifact_ref_id),
                "selector": context.reference.selector,
            },
            self.observed(rejected),
        )
        self.assertIn("pinboard validate", str(rejected["recovery"]))
        snapshot.write_bytes(original)
        self.assertEqual("protected-review", self.json_object(self.integration(fixture, "main")["source"])["kind"])

    def test_checkpoint_package_without_a_usable_candidate_snapshot_is_reported(self) -> None:
        fixture = self.accepted_package_fixture(base_text=BASE_TEXT, candidate_text=REVIEWED_TEXT)
        self.git(fixture.project, "branch", "main")
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE transition_history SET artifact_ref_id = NULL, artifact_kind = NULL WHERE outcome_schema = 'checkpoint-acceptance/v2'"
            )
        unavailable = self.integration(fixture, "main")
        self.assert_rejected(unavailable, "INTEGRATION_CANDIDATE_UNAVAILABLE", "correct-input")
        self.assertEqual("checkpoint-without-snapshot", self.observed(unavailable)["reason"])
        package = fixture.work / fixture.package_reference.selector
        damaged = self.accepted_package_fixture(base_text=BASE_TEXT, candidate_text=REVIEWED_TEXT)
        self.git(damaged.project, "branch", "main")
        (damaged.work / damaged.package_reference.selector).write_bytes(package.read_bytes() + b" ")
        invalid = self.integration(damaged, "main")
        self.assert_rejected(invalid, "INTEGRATION_CANDIDATE_EVIDENCE_INVALID", "do-not-retry")
        self.assertEqual("work-a-1", self.observed(invalid)["attempt_id"])

    def test_other_git_failures_carry_the_root_error_code_and_git_diagnostic(self) -> None:
        fixture = self.checkpoint_fixture(base_text=BASE_TEXT, candidate_text=REVIEWED_TEXT)
        base = self.git(fixture.project, "rev-parse", "HEAD")
        target = self.target_branch(fixture, "main", base)
        self.commit_text(target, "unique tree contents\n", "unique")
        tree = self.git(fixture.project, "rev-parse", "main^{tree}")
        (fixture.project / ".git" / "objects" / tree[:2] / tree[2:]).unlink()
        rejected = self.integration(fixture, "main")
        self.assert_rejected(rejected, "PROJECT_GIT_CHECKOUT_UNAVAILABLE", "correct-input")
        observed = self.observed(rejected)
        self.assertEqual(str(fixture.project), observed["project_root"])
        self.assertIn("failed to unpack tree object", str(observed["diagnostic"]))
        self.assertIn(str(observed["diagnostic"]), str(rejected["message"]))

    def test_the_read_changes_nothing_in_the_ledger_work_root_or_checkout(self) -> None:
        fixture = self.checkpoint_fixture(base_text=BASE_TEXT, candidate_text=REVIEWED_TEXT)
        self.git(fixture.project, "branch", "main")

        def snapshot() -> tuple[object, ...]:
            files = {
                str(path.relative_to(fixture.work)): (path.stat().st_size, path.stat().st_mtime_ns)
                for path in sorted(fixture.work.rglob("*"))
                if path.is_file() and path.name != "state.sqlite3-shm"
            }
            return (
                files,
                self.git(fixture.project, "--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all"),
                self.git(fixture.project, "for-each-ref"),
                self.git(fixture.project, "rev-parse", "HEAD"),
                (fixture.project / ".git" / "index").stat().st_mtime_ns,
            )

        before = snapshot()
        self.assertEqual(NOT_PRESENT, self.integration(fixture, "main")["presence"])
        self.assertEqual(before, snapshot())

    def test_reads_stay_keyed_and_independent_of_unrelated_retained_data(self) -> None:
        fixture = self.accepted_package_fixture(base_text=BASE_TEXT, candidate_text=REVIEWED_TEXT)
        self.git(fixture.project, "branch", "main")
        statements: list[str] = []
        open_database = sqlite_store.open_database

        def recording_open(*arguments: object, **options: object) -> sqlite3.Connection:
            connection = open_database(*arguments, **options)  # type: ignore[arg-type]
            connection.set_trace_callback(statements.append)
            return connection

        def traced_integration() -> tuple[JsonObject, list[str]]:
            statements.clear()
            with patch.object(sqlite_store, "open_database", recording_open):
                result = self.integration(fixture, "main")
            return result, list(statements)

        first, first_statements = traced_integration()
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(attempts)")]
            for index in range(20):
                copied = ", ".join(
                    {"attempt_id": f"'unrelated-{index}'", "item_id": "'work-b'", "state": "'done'"}.get(column, column)
                    for column in columns
                )
                connection.execute(
                    f"INSERT INTO attempts ({', '.join(columns)}) "
                    f"SELECT {copied} FROM attempts WHERE attempt_id = 'work-a-1'"
                )
        second, second_statements = traced_integration()
        self.assertEqual(first, second)
        self.assertEqual(len(first_statements), len(second_statements))
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            plans = [
                str(row[3])
                for statement in second_statements
                if statement.lstrip().upper().startswith("SELECT")
                for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}")
            ]
        self.assertTrue(plans)
        self.assertEqual([], [plan for plan in plans if plan.startswith("SCAN") and "CONSTANT" not in plan], plans)
        self.assertTrue(any("checkpoint_history_by_subject" in plan for plan in plans), plans)

    def test_overview_actions_item_leaf_and_inspection_run_no_integration_read(self) -> None:
        fixture = self.accepted_package_fixture(base_text=BASE_TEXT, candidate_text=REVIEWED_TEXT)

        def forbidden(*_arguments: object, **_options: object) -> None:
            raise AssertionError("an integration read ran")

        with (
            patch.object(root, "observe_target_content", forbidden),
            patch.object(SQLiteWorkStore, "read_integration_facts", forbidden),
        ):
            self.assertEqual("pinboard-item-status/v2", self.item_leaf(fixture)["schema"])
            self.assertEqual("ok", self.inspection(fixture)["status"])
            overview = call_native_tool(mcp_server.OVERVIEW_TOOL, self.roots(fixture))
            self.assertEqual("pinboard-overview/v6", overview["schema"])
            actions = self.actions_result(fixture, {"role": "project"})
            self.assertEqual("ok", actions["status"])

    def test_attempt_inspection_uses_the_callers_relation_without_a_target_content_read(self) -> None:
        fixture = self.checkpoint_fixture()
        base = self.git(fixture.project, "rev-parse", "HEAD")
        self.record_ready(fixture, fixture.candidate_revision)
        reconciliation: JsonObject = {
            "target_revision": base,
            "relation": "candidate-integrated",
            "phase": "cleanup",
            "effects": [
                {"effect": "source-checkout", "status": "allowed"},
                {"effect": "shared-work-root", "status": "not-required"},
                {"effect": "git-metadata", "status": "allowed"},
            ],
        }
        arguments: JsonObject = {**self.roots(fixture), "attempt_id": "work-a-1", "reconciliation": reconciliation}
        expected = call_advertised_tool(mcp_server.ATTEMPT_INSPECT_TOOL, arguments)

        def forbidden(*_arguments: object, **_options: object) -> None:
            raise AssertionError("inspection ran a target-content read")

        with patch.object(root, "observe_target_content", forbidden):
            inspected = call_advertised_tool(mcp_server.ATTEMPT_INSPECT_TOOL, arguments)
        self.assertEqual("ok", inspected["status"], inspected)
        self.assertEqual(expected, inspected)
        operation = self.json_object(self.json_object(inspected["continuation"])["next_operation"])
        self.assertEqual("repository-cleanup", operation["kind"])

    def test_flat_or_unknown_integration_fields_are_rejected(self) -> None:
        fixture = self.checkpoint_fixture()
        for request in (
            {**self.roots(fixture), "operation": "integration", "item_id": "work-a"},
            {**self.roots(fixture), "operation": "integration", "item_id": "work-a", "target": "main", "extra": 1},
            {**self.roots(fixture), "operation": "item", "item_id": "work-a", "target": "main"},
        ):
            with self.subTest(request=request):
                try:
                    rejected = call_native_tool(mcp_server.ITEM_STATUS_TOOL, {"request": request})
                except ToolError:
                    continue
                self.assert_rejected(rejected, "ITEM_STATUS_INVALID", "correct-input")
