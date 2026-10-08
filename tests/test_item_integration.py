"""The item-status integration leaf reports whether a reviewed candidate's recorded change is in a named target."""

import hashlib
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.files.artifacts import read_reference
from pinboard.application import candidate_snapshots
from pinboard.mcp import common as mcp_common
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool
from tests.support import JsonObject

COMMIT_DATE = "2030-01-02T03:04:05Z"
COMMIT_DATETIME = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)


class ItemIntegrationTest(CheckpointPackageSupport):
    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def integration(self, fixture: CheckpointFixture, target: str, item_id: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": item_id, "target": target}},
        )

    def source(self, result: JsonObject) -> JsonObject:
        return self.json_object(result["source"])

    def snapshot_diff(self, fixture: CheckpointFixture) -> bytes:
        """Read the immutable candidate snapshot that the attempt accepted for its review or checkpoint."""

        reference = next(
            value
            for value in fixture.store.validated_snapshot().artifact_references
            if "-candidate-snapshot-" in value.key
        )
        return candidate_snapshots.decode_candidate_snapshot(read_reference(fixture.work, reference)).diff

    def git(self, project: Path, *arguments: str, environment: dict[str, str] | None = None) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=project,
            env={**os.environ, **(environment or {})},
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def land(self, project: Path, name: str, base: str, diff: bytes) -> str:
        """Commit the recorded change onto base in a private index, leaving the checkout untouched."""

        with tempfile.TemporaryDirectory() as directory:
            environment = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
            self.git(project, "read-tree", base, environment=environment)
            subprocess.run(
                ["git", "apply", "--cached", "-"],
                cwd=project,
                env={**os.environ, **environment},
                input=diff,
                check=True,
                capture_output=True,
            )
            tree = self.git(project, "write-tree", environment=environment)
        commit = self.git(
            project,
            "-c",
            "user.name=Pinboard Tests",
            "-c",
            "user.email=pinboard@example.invalid",
            "commit-tree",
            tree,
            "-p",
            base,
            "-m",
            f"Integrate {name}",
            environment={"GIT_AUTHOR_DATE": COMMIT_DATE, "GIT_COMMITTER_DATE": COMMIT_DATE},
        )
        self.git(project, "update-ref", f"refs/heads/{name}", commit)
        return commit

    def test_protected_working_tree_candidate_follows_its_target_by_content(self) -> None:
        fixture = self.checkpoint_fixture()
        base = fixture.brief.base_revision
        diff = self.snapshot_diff(fixture)
        self.git(fixture.project, "update-ref", "refs/heads/at-base", base)
        absent = self.integration(fixture, "at-base")
        self.assertEqual("pinboard-item-integration/v1", absent["schema"], absent)
        self.assertEqual("content-not-present", absent["presence"])
        self.assertEqual(base, absent["resolved_revision"])
        self.assertEqual("protected-review", self.source(absent)["kind"])
        self.assertEqual("work-a-1", self.source(absent)["attempt_id"])
        self.assertEqual(base, self.source(absent)["compared_from_revision"])

        landed = self.land(fixture.project, "squashed", base, diff)
        present = self.integration(fixture, "squashed")
        self.assertEqual("content-present", present["presence"], present)
        self.assertEqual(landed, present["resolved_revision"])
        self.assertEqual("squashed", present["target"])

    def test_committed_candidate_is_present_when_the_target_fast_forwards_to_it(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        candidate = fixture.candidate_revision
        self.git(fixture.project, "update-ref", "refs/heads/at-base", fixture.brief.base_revision)
        self.assertEqual("content-not-present", self.integration(fixture, "at-base")["presence"])
        self.git(fixture.project, "update-ref", "refs/heads/fast-forward", candidate)
        present = self.integration(fixture, "fast-forward")
        self.assertEqual("content-present", present["presence"], present)
        self.assertEqual(candidate, self.source(present)["candidate_revision"])
        self.assertEqual(candidate, present["resolved_revision"])

    def test_accepted_checkpoint_candidate_is_checked_after_checkpoint_acceptance(self) -> None:
        fixture = self.accepted_package_fixture()
        base = fixture.brief.base_revision
        self.git(fixture.project, "update-ref", "refs/heads/at-base", base)
        self.assertEqual("content-not-present", self.integration(fixture, "at-base")["presence"])
        self.land(fixture.project, "checkpoint-squashed", base, self.snapshot_diff(fixture))
        present = self.integration(fixture, "checkpoint-squashed")
        self.assertEqual("content-present", present["presence"], present)
        self.assertEqual("accepted-checkpoint", self.source(present)["kind"])
        self.assertEqual(fixture.brief.checkpoint.checkpoint_id, self.source(present)["checkpoint_id"])

    def test_remote_tracking_ref_is_read_locally_without_fetching(self) -> None:
        fixture = self.checkpoint_fixture()
        base = fixture.brief.base_revision
        landed = self.land(fixture.project, "remote-squashed", base, self.snapshot_diff(fixture))
        self.git(fixture.project, "update-ref", "refs/remotes/origin/main", landed)
        present = self.integration(fixture, "origin/main")
        self.assertEqual("content-present", present["presence"], present)
        self.assertEqual("origin/main", present["target"])
        self.assertEqual(landed, present["resolved_revision"])

    def test_unresolved_target_and_flag_like_target_are_typed_rejections(self) -> None:
        fixture = self.checkpoint_fixture()
        unresolved = self.integration(fixture, "no-such-target")
        self.assertEqual("pinboard-mcp-item-status-result/v3", unresolved["schema"], unresolved)
        self.assertEqual("INTEGRATION_TARGET_UNRESOLVED", unresolved["code"])
        self.assertEqual("unchanged", unresolved["effect"])
        self.assertEqual("correct-input", unresolved["retry"])
        self.assertIn("fetch it outside Pinboard", str(unresolved["recovery"]))
        self.assertFalse(unresolved["state_changed"])
        flagged = call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    **self.roots(fixture),
                    "operation": "integration",
                    "item_id": "work-a",
                    "target": "--output=/tmp/x",
                }
            },
        )
        self.assertEqual("ITEM_STATUS_INVALID", flagged["code"], flagged)
        self.assertEqual("pinboard-mcp-item-status-result/v3", flagged["schema"])

    def test_item_without_a_reviewed_candidate_names_the_missing_source(self) -> None:
        fixture = self.checkpoint_fixture()
        missing = call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": "unknown", "target": "main"}},
        )
        self.assertEqual("ITEM_NOT_FOUND", missing["code"], missing)
        self.assertEqual("pinboard-mcp-item-status-result/v3", missing["schema"])

    def commit_tree(self, project: Path, tree: str, parents: tuple[str, ...], message: str) -> str:
        arguments = ["commit-tree", tree]
        for parent in parents:
            arguments.extend(["-p", parent])
        return self.git(
            project,
            "-c",
            "user.name=Pinboard Tests",
            "-c",
            "user.email=pinboard@example.invalid",
            *arguments,
            "-m",
            message,
            environment={"GIT_AUTHOR_DATE": COMMIT_DATE, "GIT_COMMITTER_DATE": COMMIT_DATE},
        )

    def commit_with_file(self, project: Path, parent: str, path: str, content: bytes) -> str:
        """Commit one path onto parent in a private index, leaving the checkout untouched."""

        blob = (
            subprocess.run(
                ["git", "hash-object", "-w", "--stdin"], cwd=project, input=content, check=True, capture_output=True
            )
            .stdout.decode()
            .strip()
        )
        with tempfile.TemporaryDirectory() as directory:
            environment = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
            self.git(project, "read-tree", parent, environment=environment)
            self.git(project, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", environment=environment)
            tree = self.git(project, "write-tree", environment=environment)
        return self.commit_tree(project, tree, (parent,), f"Set {path}")

    def changed_path(self, fixture: CheckpointFixture) -> str:
        for line in self.snapshot_diff(fixture).decode(errors="replace").splitlines():
            if line.startswith("diff --git a/"):
                return line.split(" b/", 1)[1]
        raise AssertionError("The recorded diff names no path.")

    def transition(self, fixture: CheckpointFixture, action_id: str, payload: JsonObject) -> JsonObject:
        result = self.transition_result(fixture, self.project_action(fixture, action_id), payload)
        self.assertIn(result["status"], ("committed", "committed-with-warning"), result)
        return result

    def complete(self, fixture: CheckpointFixture, evidence: str) -> None:
        fixture = self.terminalize_brief(fixture)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        with patch("pinboard.mcp.mutation_operations.datetime") as clock:
            clock.now.return_value = COMMIT_DATETIME
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

    def test_merge_commit_containing_the_change_is_present(self) -> None:
        fixture = self.checkpoint_fixture()
        base = fixture.brief.base_revision
        landed = self.land(fixture.project, "landed", base, self.snapshot_diff(fixture))
        unrelated = self.commit_with_file(fixture.project, base, "unrelated.txt", b"other\n")
        tree = self.git(fixture.project, "rev-parse", f"{landed}^{{tree}}")
        merged = self.commit_tree(fixture.project, tree, (unrelated, landed), "Merge the change")
        self.git(fixture.project, "update-ref", "refs/heads/merged", merged)
        present = self.integration(fixture, "merged")
        self.assertEqual("content-present", present["presence"], present)
        self.assertEqual(merged, present["resolved_revision"])

    def test_rebased_change_on_a_newer_base_is_present(self) -> None:
        fixture = self.checkpoint_fixture()
        base = fixture.brief.base_revision
        newer = self.commit_with_file(fixture.project, base, "unrelated.txt", b"other\n")
        self.land(fixture.project, "rebased", newer, self.snapshot_diff(fixture))
        present = self.integration(fixture, "rebased")
        self.assertEqual("content-present", present["presence"], present)

    def test_later_non_overlapping_edit_keeps_the_change_present(self) -> None:
        fixture = self.checkpoint_fixture()
        landed = self.land(fixture.project, "landed-edit", fixture.brief.base_revision, self.snapshot_diff(fixture))
        later = self.commit_with_file(fixture.project, landed, "unrelated.txt", b"other\n")
        self.git(fixture.project, "update-ref", "refs/heads/later-edit", later)
        self.assertEqual("content-present", self.integration(fixture, "later-edit")["presence"])

    def test_later_overlapping_edit_reports_content_not_present(self) -> None:
        fixture = self.checkpoint_fixture()
        landed = self.land(fixture.project, "landed-overlap", fixture.brief.base_revision, self.snapshot_diff(fixture))
        overwritten = self.commit_with_file(fixture.project, landed, self.changed_path(fixture), b"overwritten\n")
        self.git(fixture.project, "update-ref", "refs/heads/overlapping", overwritten)
        absent = self.integration(fixture, "overlapping")
        self.assertEqual("content-not-present", absent["presence"], absent)
        self.assertEqual(overwritten, absent["resolved_revision"])

    def test_completed_item_checks_its_closing_candidate(self) -> None:
        fixture = self.checkpoint_fixture()
        self.complete(fixture, "Accepted and integrated by the maintainer.")
        self.land(fixture.project, "completed", fixture.brief.base_revision, self.snapshot_diff(fixture))
        present = self.integration(fixture, "completed")
        self.assertEqual("content-present", present["presence"], present)
        self.assertEqual("completion", self.source(present)["kind"])
        self.assertEqual("work-a-1", self.source(present)["attempt_id"])

    def test_returned_attempt_without_checkpoint_acceptance_names_its_state(self) -> None:
        fixture = self.checkpoint_fixture()
        self.transition(fixture, "return-for-correction:work-a-1", {"reason": "Revise the candidate."})
        unavailable = self.integration(fixture, "main")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", unavailable["code"], unavailable)
        self.assertEqual("correct-input", unavailable["retry"])
        self.assertIn("state 'active'", str(unavailable["message"]))

    def test_altered_snapshot_bytes_are_invalid_evidence_not_a_generic_error(self) -> None:
        fixture = self.checkpoint_fixture()
        reference = next(
            value
            for value in fixture.store.validated_snapshot().artifact_references
            if "-candidate-snapshot-" in value.key
        )
        (fixture.work / reference.selector).write_bytes(b"altered\n")
        invalid = self.integration(fixture, "main")
        self.assertEqual("INTEGRATION_CANDIDATE_EVIDENCE_INVALID", invalid["code"], invalid)
        self.assertEqual("do-not-retry", invalid["retry"])
        self.assertIn("pinboard validate", str(invalid["recovery"]))

    def test_directly_closed_item_names_the_missing_closing_candidate(self) -> None:
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
        unavailable = self.integration(fixture, "main", item_id="work-c")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", unavailable["code"], unavailable)
        self.assertIn("state 'done'", str(unavailable["message"]))
        self.assertIn("closed without a completion", str(unavailable["message"]))

    def test_integration_envelope_selects_its_item_for_trace_capture(self) -> None:
        """Automatic trace capture applies the named item's override, as it does for the item leaf."""

        selected = mcp_common.select_capture_item(
            Path(),
            None,
            {
                "request": {
                    "project_root": "/project",
                    "work_root": "/project/.pinboard",
                    "operation": "integration",
                    "item_id": "work-a",
                    "target": "main",
                }
            },
        )
        self.assertEqual("work-a", selected)
