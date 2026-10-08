"""The item-status integration leaf reports whether a reviewed candidate's recorded change is in a named target."""

import os
import subprocess
import tempfile
from pathlib import Path

from pinboard.adapters.files.artifacts import read_reference
from pinboard.application import candidate_snapshots
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool
from tests.support import JsonObject

COMMIT_DATE = "2030-01-02T03:04:05Z"


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
