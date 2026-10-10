"""The item-status integration leaf compares one reviewed candidate with a named target by content."""

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError

from pinboard.adapters import candidate_evidence
from pinboard.adapters.files import root
from pinboard.adapters.files.errors import RootError
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.application.candidate_identity import working_tree_identity
from pinboard.domain.identifiers import AttemptId, WorkItemId
from pinboard.mcp import common as mcp_common
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool
from tests.support import SQLITE_NOW, JsonObject

FIXED_DATE = "2030-01-01T00:00:00+00:00"
GIT_DATED_ENV = {
    "GIT_AUTHOR_DATE": FIXED_DATE,
    "GIT_COMMITTER_DATE": FIXED_DATE,
    "GIT_AUTHOR_NAME": "Pinboard Tests",
    "GIT_AUTHOR_EMAIL": "pinboard@example.invalid",
    "GIT_COMMITTER_NAME": "Pinboard Tests",
    "GIT_COMMITTER_EMAIL": "pinboard@example.invalid",
}
COMPLETED_AT = datetime(2030, 1, 3, 4, 5, 6, tzinfo=UTC)


class ItemIntegrationTest(CheckpointPackageSupport):
    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def integration_leaf(self, fixture: CheckpointFixture, target: str, item_id: str = "work-a") -> JsonObject:
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

    def observed(self, result: JsonObject) -> dict[str, object]:
        return {
            str(self.json_object(value)["field"]): self.json_object(value)["value"]
            for value in self.json_array(result["observed"])
        }

    def assert_unchanged_rejection(
        self, result: JsonObject, code: str, retry: str = "correct-input"
    ) -> dict[str, object]:
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual(code, result["code"], result)
        self.assertEqual(
            ("rejected", False, "unchanged", retry),
            (result["status"], result["state_changed"], result["effect"], result["retry"]),
            result,
        )
        return self.observed(result)

    def run_git(self, project: Path, *arguments: str, check: bool = True) -> str:
        result = subprocess.run(["git", *arguments], cwd=project, check=check, capture_output=True, text=True)
        return result.stdout.strip()

    def dated_commit(self, project: Path, message: str) -> str:
        subprocess.run(["git", "add", "--all"], cwd=project, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=project,
            check=True,
            capture_output=True,
            env={**os.environ, **GIT_DATED_ENV},
        )
        return self.run_git(project, "rev-parse", "HEAD")

    def squash_merged_main(self, fixture: CheckpointFixture) -> str:
        """Carry the candidate's working-tree change onto a new main branch as one squash commit."""

        self.run_git(fixture.project, "switch", "-c", "main")
        return self.dated_commit(fixture.project, "candidate")

    def assert_integration(
        self,
        result: JsonObject,
        *,
        target: str,
        source: str,
        target_revision: str,
        presence: str,
        compared_from: str,
        candidate: str,
        checkpoint: str | None = None,
    ) -> None:
        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual("sqlite-v7", result["authority"], result)
        self.assertEqual(
            ("work-a", target, source, "work-a-1", presence),
            (result["item_id"], result["target"], result["source"], result["attempt_id"], result["presence"]),
            result,
        )
        self.assertEqual(target_revision, result["target_revision"], result)
        self.assertEqual(candidate, result["candidate_revision"], result)
        self.assertEqual(compared_from, result["compared_from_revision"], result)
        self.assertEqual(checkpoint, result.get("checkpoint_id"), result)
        self.assertNotEqual("", result["revision"], result)

    def install_empty_diff_candidate(self, fixture: CheckpointFixture) -> str:
        """Record a fresh working-tree candidate whose accepted diff is empty."""

        preimage = self.run_git(fixture.project, "rev-parse", "HEAD")
        candidate = working_tree_identity(preimage, b"")
        state = fixture.store.validated_snapshot()
        attempt = next(value for value in state.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        self.accept_candidate_snapshot(
            resolve_durable_roots(fixture.project),
            fixture.store,
            attempt,
            "working-tree",
            candidate,
            b"",
            preimage,
            attempt.base_revision,
            SQLITE_NOW,
        )
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE attempts SET candidate_revision = ?, candidate_recorded_at = ? WHERE attempt_id = 'work-a-1'",
                (candidate, SQLITE_NOW.isoformat()),
            )
        return candidate

    def content_repo(self, tracked_final: bytes = b"candidate\n") -> tuple[Path, str, bytes]:
        """Create a plain Git repository with one committed base change and a candidate diff."""

        project = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, project, True)
        self.run_git(project, "init", "-b", "work")
        (project / "tracked.txt").write_bytes(b"base\n")
        base = self.dated_commit(project, "base")
        (project / "tracked.txt").write_bytes(tracked_final)
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--"], cwd=project, check=True, capture_output=True
        ).stdout
        return project, base, diff

    def transition(self, fixture: CheckpointFixture, action_id: str, payload: JsonObject) -> JsonObject:
        result = self.transition_result(fixture, self.project_action(fixture, action_id), payload)
        self.assertIn(result["status"], ("committed", "committed-with-warning"), result)
        return result

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

    def copy_row(self, connection: sqlite3.Connection, table: str, where: str, **overrides: object) -> None:
        """Insert one copy of an existing row, overriding the named columns and auto-assigning ids."""

        row = connection.execute(f"SELECT * FROM {table} WHERE {where} LIMIT 1")
        columns = [description[0] for description in row.description]
        values = dict(zip(columns, row.fetchone(), strict=True))
        values.pop("history_id", None)
        if "project_revision" in values and "project_revision" not in overrides:
            highest = connection.execute(f"SELECT MAX(project_revision) FROM {table}").fetchone()[0] or 0
            values["project_revision"] = highest + 1
        values.update(overrides)
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        connection.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(values.values()))

    def test_working_tree_candidate_is_present_after_a_squash_merge(self) -> None:
        fixture = self.checkpoint_fixture()
        main_head = self.squash_merged_main(fixture)
        self.assert_integration(
            self.integration_leaf(fixture, "main"),
            target="main",
            source="protected-review",
            target_revision=main_head,
            presence="content-present",
            compared_from=fixture.brief.base_revision,
            candidate=fixture.candidate_revision,
        )

    def test_commit_candidate_is_present_through_fast_forward_merge_and_cherry_pick(self) -> None:
        for route in ("fast-forward", "merge", "cherry-pick"):
            with self.subTest(route=route):
                fixture = self.checkpoint_fixture(candidate_form="current-head")
                base = fixture.brief.base_revision
                if route == "fast-forward":
                    self.run_git(fixture.project, "branch", "main", fixture.candidate_revision)
                else:
                    self.run_git(fixture.project, "branch", "main", base)
                    self.run_git(fixture.project, "switch", "main")
                    (fixture.project / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
                    self.dated_commit(fixture.project, "unrelated")
                    if route == "merge":
                        self.run_git(fixture.project, "merge", "codex/work-a", "-m", "merge candidate")
                    else:
                        subprocess.run(
                            ["git", "cherry-pick", fixture.candidate_revision],
                            cwd=fixture.project,
                            check=True,
                            capture_output=True,
                            env={**os.environ, **GIT_DATED_ENV},
                        )
                        not_ancestor = subprocess.run(
                            ["git", "merge-base", "--is-ancestor", fixture.candidate_revision, "main"],
                            cwd=fixture.project,
                            check=False,
                            capture_output=True,
                        )
                        self.assertNotEqual(0, not_ancestor.returncode)
                target_revision = self.run_git(fixture.project, "rev-parse", "main")
                self.assert_integration(
                    self.integration_leaf(fixture, "main"),
                    target="main",
                    source="protected-review",
                    target_revision=target_revision,
                    presence="content-present",
                    compared_from=base,
                    candidate=fixture.candidate_revision,
                )

    def test_checkpoint_accepted_candidate_is_present_and_names_its_checkpoint(self) -> None:
        fixture = self.accepted_package_fixture()
        main_head = self.squash_merged_main(fixture)
        self.assert_integration(
            self.integration_leaf(fixture, "main"),
            target="main",
            source="accepted-checkpoint",
            target_revision=main_head,
            presence="content-present",
            compared_from=fixture.brief.base_revision,
            candidate=fixture.candidate_revision,
            checkpoint="typed-json-cutover",
        )

    def test_completion_candidate_is_present_after_the_item_closes(self) -> None:
        fixture = self.checkpoint_fixture()
        self.complete(fixture, "Accepted and integrated by the maintainer.")
        main_head = self.squash_merged_main(fixture)
        self.assert_integration(
            self.integration_leaf(fixture, "main"),
            target="main",
            source="completion",
            target_revision=main_head,
            presence="content-present",
            compared_from=fixture.brief.base_revision,
            candidate=fixture.candidate_revision,
        )

    def test_presence_survives_a_later_non_overlapping_edit(self) -> None:
        fixture = self.checkpoint_fixture()
        self.squash_merged_main(fixture)
        (fixture.project / "unrelated.txt").write_text("later\n", encoding="utf-8")
        later_head = self.dated_commit(fixture.project, "unrelated")
        self.assert_integration(
            self.integration_leaf(fixture, "main"),
            target="main",
            source="protected-review",
            target_revision=later_head,
            presence="content-present",
            compared_from=fixture.brief.base_revision,
            candidate=fixture.candidate_revision,
        )

    def test_absence_at_the_accepted_base_and_after_an_overlapping_edit(self) -> None:
        fixture = self.checkpoint_fixture()
        self.assert_integration(
            self.integration_leaf(fixture, fixture.brief.base_revision),
            target=fixture.brief.base_revision,
            source="protected-review",
            target_revision=fixture.brief.base_revision,
            presence="content-not-present",
            compared_from=fixture.brief.base_revision,
            candidate=fixture.candidate_revision,
        )
        self.squash_merged_main(fixture)
        (fixture.project / "tracked.txt").write_text("different\n", encoding="utf-8")
        edited_head = self.dated_commit(fixture.project, "overlapping edit")
        self.assert_integration(
            self.integration_leaf(fixture, "main"),
            target="main",
            source="protected-review",
            target_revision=edited_head,
            presence="content-not-present",
            compared_from=fixture.brief.base_revision,
            candidate=fixture.candidate_revision,
        )

    def test_an_empty_accepted_diff_reports_no_change_at_a_resolved_target(self) -> None:
        fixture = self.checkpoint_fixture()
        candidate = self.install_empty_diff_candidate(fixture)
        head = self.run_git(fixture.project, "rev-parse", "HEAD")
        self.assert_integration(
            self.integration_leaf(fixture, "codex/work-a"),
            target="codex/work-a",
            source="protected-review",
            target_revision=head,
            presence="no-change",
            compared_from=head,
            candidate=candidate,
        )

    def test_a_remote_tracking_ref_is_a_supported_target(self) -> None:
        fixture = self.checkpoint_fixture()
        main_head = self.squash_merged_main(fixture)
        self.run_git(fixture.project, "update-ref", "refs/remotes/origin/main", main_head)
        self.assert_integration(
            self.integration_leaf(fixture, "origin/main"),
            target="origin/main",
            source="protected-review",
            target_revision=main_head,
            presence="content-present",
            compared_from=fixture.brief.base_revision,
            candidate=fixture.candidate_revision,
        )

    def test_target_rejections_are_typed_unchanged_reads(self) -> None:
        fixture = self.checkpoint_fixture()
        unresolved = self.integration_leaf(fixture, "no-such-branch")
        observed = self.assert_unchanged_rejection(unresolved, "INTEGRATION_TARGET_UNRESOLVED")
        self.assertEqual({"target": "no-such-branch", "project_root": str(fixture.project)}, observed)
        self.assertIn("Name an existing local branch", str(unresolved["recovery"]))
        try:
            dashed = self.integration_leaf(fixture, "-evil")
        except ToolError:
            pass
        else:
            self.assert_unchanged_rejection(dashed, "ITEM_STATUS_INVALID")

    def test_candidate_unavailable_rejections_cover_each_unprotected_shape(self) -> None:
        ready = self.checkpoint_fixture()
        observed = self.assert_unchanged_rejection(
            self.integration_leaf(ready, "main", "work-c"), "INTEGRATION_CANDIDATE_UNAVAILABLE"
        )
        self.assertEqual(("work-c", "ready", "no-protected-candidate"), self.unavailable_shape(observed))
        active = self.checkpoint_fixture()
        self.transition(
            active,
            "accept-review-and-continue:work-a-1",
            {"candidate": active.candidate_revision, "evidence": "Accepted; continue the attempt."},
        )
        observed = self.assert_unchanged_rejection(
            self.integration_leaf(active, "main"), "INTEGRATION_CANDIDATE_UNAVAILABLE"
        )
        self.assertEqual(("work-a", "active", "no-protected-candidate"), self.unavailable_shape(observed))
        closed = self.checkpoint_fixture()
        self.close_prerequisite(closed)
        observed = self.assert_unchanged_rejection(
            self.integration_leaf(closed, "main", "work-c"), "INTEGRATION_CANDIDATE_UNAVAILABLE"
        )
        self.assertEqual(("work-c", "done", "direct-close"), self.unavailable_shape(observed))

    def unavailable_shape(self, observed: dict[str, object]) -> tuple[object, ...]:
        return (observed["item_id"], observed["item_state"], observed["reason"])

    def test_altered_accepted_snapshot_bytes_reject_with_do_not_retry(self) -> None:
        fixture = self.checkpoint_fixture()
        snapshot = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert snapshot is not None
        self.replace_artifact_bytes(fixture, snapshot.reference, b"tampered\n")
        result = self.integration_leaf(fixture, "main")
        observed = self.assert_unchanged_rejection(result, "INTEGRATION_CANDIDATE_EVIDENCE_INVALID", "do-not-retry")
        self.assertEqual("work-a-1", observed["attempt_id"])
        self.assertIn(snapshot.reference.key, str(observed["accepted_reference"]))
        self.assertIn("pinboard validate", str(result["recovery"]))

    def test_an_unknown_item_and_a_non_git_project_root_reject(self) -> None:
        fixture = self.checkpoint_fixture()
        unknown = self.integration_leaf(fixture, "main", "work-z")
        observed = self.assert_unchanged_rejection(unknown, "ITEM_NOT_FOUND")
        self.assertEqual({}, observed)
        self.assertNotIn("recovery", unknown)
        outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, outside, True)
        failure = candidate_evidence.read_item_integration(
            outside, fixture.work, fixture.store, WorkItemId("work-a"), "main", str(outside)
        )
        assert isinstance(failure, candidate_evidence.ItemIntegrationFailure)
        self.assertEqual("PROJECT_GIT_ROOT_UNAVAILABLE", failure.code)
        with self.assertRaisesRegex(RootError, "PROJECT_GIT_ROOT_UNAVAILABLE"):
            root.read_integration_content_presence(outside, "main", b"diff\n")
        rejection = call_advertised_tool(
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
        observed = self.assert_unchanged_rejection(rejection, "PROJECT_GIT_ROOT_UNAVAILABLE")
        self.assertEqual(str(outside), observed["project_root"])

    def test_read_integration_content_presence_reports_presence_by_reverse_apply(self) -> None:
        project, base, diff = self.content_repo()
        self.run_git(project, "switch", "-c", "main")
        head = self.dated_commit(project, "candidate")
        presence = root.read_integration_content_presence(project, "main", diff)
        self.assertEqual(root.IntegrationTargetResolved(head, "content-present"), presence)
        absence = root.read_integration_content_presence(project, base, diff)
        self.assertEqual(root.IntegrationTargetResolved(base, "content-not-present"), absence)
        unresolved = root.read_integration_content_presence(project, "missing-ref", diff)
        self.assertEqual(root.IntegrationTargetUnresolved("missing-ref"), unresolved)

    def test_whitespace_configuration_cannot_change_the_comparison(self) -> None:
        project, _base, diff = self.content_repo(tracked_final=b"candidate   \n")
        self.run_git(project, "config", "apply.whitespace", "error")
        self.run_git(project, "switch", "-c", "main")
        head = self.dated_commit(project, "candidate")
        presence = root.read_integration_content_presence(project, "main", diff)
        self.assertEqual(root.IntegrationTargetResolved(head, "content-present"), presence)

    def test_the_comparison_reads_a_read_only_git_directory_without_side_effects(self) -> None:
        project, base, diff = self.content_repo()
        self.run_git(project, "switch", "-c", "main")
        head = self.dated_commit(project, "candidate")
        before = (self.run_git(project, "rev-parse", "HEAD"), self.run_git(project, "status", "--porcelain"))
        git_directory = project / ".git"
        git_directory.chmod(stat.S_IREAD | stat.S_IEXEC)
        try:
            presence = root.read_integration_content_presence(project, "main", diff)
            self.assertEqual(root.IntegrationTargetResolved(head, "content-present"), presence)
            absence = root.read_integration_content_presence(project, base, diff)
            self.assertEqual(root.IntegrationTargetResolved(base, "content-not-present"), absence)
        finally:
            git_directory.chmod(stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
        after = (self.run_git(project, "rev-parse", "HEAD"), self.run_git(project, "status", "--porcelain"))
        self.assertEqual(before, after)

    def test_the_comparison_creates_and_removes_exactly_one_private_index_directory(self) -> None:
        project, _base, diff = self.content_repo()
        self.run_git(project, "switch", "-c", "main")
        self.dated_commit(project, "candidate")
        created: list[Path] = []
        real_mktemp = root.tempfile.mkdtemp

        def spy(*arguments: object, **keyword: object) -> str:
            directory = real_mktemp(*arguments, **keyword)  # type: ignore[arg-type]
            created.append(Path(directory))
            return directory

        with patch.object(root.tempfile, "mkdtemp", spy):
            root.read_integration_content_presence(project, "main", diff)
        self.assertEqual(1, len(created))
        self.assertFalse(created[0].exists())
        with patch.object(root.tempfile, "mkdtemp", spy):
            empty = root.read_integration_content_presence(project, "main", b"")
        self.assertEqual(1, len(created))
        assert isinstance(empty, root.IntegrationTargetResolved)
        self.assertEqual("no-change", empty.presence)

    def test_integration_requests_capture_the_named_item_for_traces(self) -> None:
        fixture = self.checkpoint_fixture()
        request = {
            "request": {**self.roots(fixture), "operation": "integration", "item_id": "work-a", "target": "main"}
        }
        self.assertEqual("work-a", mcp_common.select_capture_item(fixture.project, str(fixture.work), request))

    def test_the_checkpoint_read_uses_its_partial_index_and_the_latest_acceptance(self) -> None:
        fixture = self.accepted_package_fixture()
        self.squash_merged_main(fixture)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            plan = connection.execute(
                """
                EXPLAIN QUERY PLAN SELECT history_id FROM transition_history
                INDEXED BY checkpoint_history_by_subject
                WHERE subject_id = 'work-a-1' AND outcome_schema = 'checkpoint-acceptance/v2'
                ORDER BY history_id DESC LIMIT 1
                """
            ).fetchall()
            self.assertIn("checkpoint_history_by_subject", json.dumps(plan))
        self.assertEqual("accepted-checkpoint", self.integration_leaf(fixture, "main")["source"])
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            self.copy_row(
                connection,
                "transition_history",
                "outcome_schema = 'checkpoint-acceptance/v2'",
                artifact_ref_id=None,
                artifact_kind=None,
            )
        observed = self.assert_unchanged_rejection(
            self.integration_leaf(fixture, "main"), "INTEGRATION_CANDIDATE_UNAVAILABLE"
        )
        self.assertEqual("checkpoint-package-without-candidate-snapshot", observed["reason"])

    def test_unrelated_retained_attempts_and_receipts_do_not_enter_the_comparison(self) -> None:
        fixture = self.accepted_package_fixture()
        self.squash_merged_main(fixture)
        expected = self.integration_leaf(fixture, "main")
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            self.copy_row(
                connection,
                "attempts",
                "attempt_id = 'work-a-1'",
                attempt_id="work-b-kept",
                item_id="work-b",
                state="done",
            )
            self.copy_row(
                connection,
                "transition_history",
                "outcome_schema = 'checkpoint-acceptance/v2'",
                subject_id="work-b-kept",
                action_id="accept-checkpoint:work-b-kept",
                artifact_ref_id=None,
                artifact_kind=None,
            )
        self.assertEqual(expected, self.integration_leaf(fixture, "main"))
