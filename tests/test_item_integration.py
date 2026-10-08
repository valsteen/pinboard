"""The item-status integration leaf reports whether a reviewed candidate's recorded change is in a named target."""

import hashlib
import os
import sqlite3
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters import candidate_evidence
from pinboard.adapters.files import contributor_traces
from pinboard.adapters.files.artifacts import read_reference
from pinboard.adapters.files.errors import ArtifactError
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.application import candidate_snapshots, query_models
from pinboard.domain.identifiers import AttemptId, WorkItemId
from pinboard.mcp import common as mcp_common
from pinboard.mcp import execution, tool_names
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import JsonObject, JsonValue

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

    def test_integration_leaf_reads_no_review_walk_or_pause_projection(self) -> None:
        fixture = self.checkpoint_fixture()
        landed = self.land(fixture.project, "focused", fixture.brief.base_revision, self.snapshot_diff(fixture))
        self.assertEqual(landed, self.integration(fixture, "focused")["resolved_revision"])
        with (
            patch("pinboard.adapters.sqlite.lifecycle._read_review_event", side_effect=AssertionError("review walk")),
            patch(
                "pinboard.adapters.sqlite.lifecycle.read_recorded_pause_reasons",
                side_effect=AssertionError("pause projection"),
            ),
        ):
            present = self.integration(fixture, "focused")
        self.assertEqual("content-present", present["presence"], present)

    def test_attempt_inspection_runs_no_target_comparison(self) -> None:
        fixture = self.checkpoint_fixture()
        with patch("pinboard.adapters.files.root.observe_target_content", side_effect=AssertionError("git read")):
            inspected = call_advertised_tool(
                mcp_server.ATTEMPT_INSPECT_TOOL,
                {**self.roots(fixture), "attempt_id": "work-a-1", "reconciliation": None},
            )
        self.assertEqual("ok", inspected["status"], inspected)

    def test_damaged_checkpoint_outcome_is_named_as_a_damaged_receipt(self) -> None:
        fixture = self.accepted_package_fixture()
        connection = sqlite3.connect(fixture.work / "state.sqlite3")
        try:
            connection.execute(
                "UPDATE transition_history SET outcome_json = ? WHERE outcome_schema = 'checkpoint-acceptance/v2'",
                ('{"unexpected":true}',),
            )
            connection.commit()
        finally:
            connection.close()
        damaged = self.integration(fixture, "main")
        self.assertEqual("TRANSITION_RECEIPT_DAMAGED", damaged["code"], damaged)
        self.assertEqual("pinboard-mcp-item-status-result/v3", damaged["schema"])
        self.assertEqual("do-not-retry", damaged["retry"])

    def test_nul_byte_in_target_is_an_invalid_request(self) -> None:
        fixture = self.checkpoint_fixture()
        invalid = self.integration(fixture, "bad\x00name")
        self.assertEqual("ITEM_STATUS_INVALID", invalid["code"], invalid)

    def test_candidate_continued_after_review_is_not_a_source(self) -> None:
        fixture = self.checkpoint_fixture()
        self.transition(
            fixture,
            "accept-review-and-continue:work-a-1",
            {"candidate": fixture.candidate_revision, "evidence": "Accepted; continue the attempt."},
        )
        unavailable = self.integration(fixture, "main")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", unavailable["code"], unavailable)
        self.assertIn("state 'active'", str(unavailable["message"]))

    def test_ready_item_without_an_attempt_names_its_state(self) -> None:
        fixture = self.checkpoint_fixture()
        unavailable = self.integration(fixture, "main", item_id="work-c")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", unavailable["code"], unavailable)
        self.assertIn("state 'ready'", str(unavailable["message"]))
        self.assertIn("no current attempt", str(unavailable["message"]))

    def test_project_root_outside_a_git_checkout_is_a_typed_rejection(self) -> None:
        fixture = self.checkpoint_fixture()
        with tempfile.TemporaryDirectory() as directory:
            outside = call_advertised_tool(
                mcp_server.ITEM_STATUS_TOOL,
                {
                    "request": {
                        "project_root": directory,
                        "work_root": str(fixture.work),
                        "operation": "integration",
                        "item_id": "work-a",
                        "target": "main",
                    }
                },
            )
        self.assertEqual("PROJECT_GIT_ROOT_UNAVAILABLE", outside["code"], outside)
        self.assertEqual("correct-input", outside["retry"])

    def test_candidate_integrated_reconciliation_is_rejected_before_any_target_comparison(self) -> None:
        fixture = self.checkpoint_fixture()
        reconciliation: JsonObject = {
            "target_revision": fixture.brief.base_revision,
            "relation": "candidate-integrated",
            "phase": "cleanup",
            "effects": [
                {"effect": "source-checkout", "status": "not-required"},
                {"effect": "shared-work-root", "status": "not-required"},
                {"effect": "git-metadata", "status": "not-required"},
            ],
        }
        with patch("pinboard.adapters.files.root.observe_target_content", side_effect=AssertionError("git read")):
            inspected = call_advertised_tool(
                mcp_server.ATTEMPT_INSPECT_TOOL,
                {**self.roots(fixture), "attempt_id": "work-a-1", "reconciliation": reconciliation},
            )
        self.assertEqual("ATTEMPT_INSPECT_INVALID", inspected["code"], inspected)
        self.assertEqual([], inspected["changed_surfaces"])

    def test_superseded_item_names_its_terminal_state(self) -> None:
        fixture = self.checkpoint_fixture()
        unavailable = self.integration(fixture, "main", item_id="work-b")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", unavailable["code"], unavailable)
        self.assertIn("state 'superseded'", str(unavailable["message"]))

    def test_reflog_spelling_past_its_entries_is_an_unresolved_target(self) -> None:
        fixture = self.checkpoint_fixture()
        unresolved = self.integration(fixture, "HEAD@{99999}")
        self.assertEqual("INTEGRATION_TARGET_UNRESOLVED", unresolved["code"], unresolved)
        self.assertEqual("correct-input", unresolved["retry"])

    def protected_diff(self, fixture: CheckpointFixture) -> bytes:
        """Read the diff of the attempt's current protected candidate, after any resubmission."""

        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        return candidate_snapshots.decode_candidate_snapshot(read_reference(fixture.work, context.reference)).diff

    def return_for_review(self, fixture: CheckpointFixture, reason: str) -> None:
        self.transition(fixture, "return-for-correction:work-a-1", {"reason": reason})

    def submit_candidate(self, fixture: CheckpointFixture, label: str) -> str:
        (fixture.project / "tracked.txt").write_text(f"{label}\n", encoding="utf-8")
        observed = call_native_tool(
            tool_names.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"}
        )
        candidate = observed["candidate"]
        assert isinstance(candidate, str), observed
        lease = self.native_attempt_acquire(fixture, f"worker-{label}")
        submission = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        submitted = self.transition_result(fixture, submission, {"candidate": candidate})
        self.assertEqual("committed", submitted["status"], submitted)
        return candidate

    def record_ready(self, fixture: CheckpointFixture, candidate: str) -> None:
        store = fixture.store
        snapshot = store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        attempt = store.read_attempt_context(AttemptId("work-a-1"))
        assert snapshot is not None and isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        recorded = call_native_tool(
            tool_names.REVIEW_JOB_TOOL,
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

    def observed_fields(self, result: JsonObject) -> dict[str, JsonValue]:
        observed = (self.json_object(field) for field in self.json_array(result["observed"]))
        return {str(field["field"]): field["value"] for field in observed}

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
            tool_names.BRIEF_PUBLISH_TOOL,
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

    def commit_rename(self, project: Path, parent: str, old: str, new: str) -> str:
        """Commit a rename of one path onto parent in a private index, keeping its content."""

        content = subprocess.run(
            ["git", "show", f"{parent}:{old}"], cwd=project, check=True, capture_output=True
        ).stdout
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
            self.git(project, "update-index", "--force-remove", old, environment=environment)
            self.git(project, "update-index", "--add", "--cacheinfo", f"100644,{blob},{new}", environment=environment)
            tree = self.git(project, "write-tree", environment=environment)
        return self.commit_tree(project, tree, (parent,), f"Rename {old} to {new}")

    def test_accepted_checkpoint_candidate_is_checked_after_resume(self) -> None:
        fixture = self.accepted_package_fixture()
        self.close_prerequisite(fixture)
        self.transition(fixture, "resume:work-a", {})
        self.land(fixture.project, "resumed", fixture.brief.base_revision, self.snapshot_diff(fixture))
        present = self.integration(fixture, "resumed")
        self.assertEqual("content-present", present["presence"], present)
        self.assertEqual("accepted-checkpoint", self.source(present)["kind"])
        self.assertEqual(fixture.brief.checkpoint.checkpoint_id, self.source(present)["checkpoint_id"])

    def test_accepted_checkpoint_candidate_keeps_its_source_after_rebind(self) -> None:
        fixture = self.accepted_package_fixture()
        self.rebind(fixture, "codex/rebound-work-a", 2)
        self.land(fixture.project, "rebound", fixture.brief.base_revision, self.snapshot_diff(fixture))
        present = self.integration(fixture, "rebound")
        self.assertEqual("content-present", present["presence"], present)
        self.assertEqual("accepted-checkpoint", self.source(present)["kind"])

    def test_resubmitted_protected_candidate_follows_favorable_review_and_cleanup_selection(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Rework the candidate.")
        candidate = self.submit_candidate(fixture, "reviewed")
        self.record_ready(fixture, candidate)
        landed = self.land(fixture.project, "resubmitted", fixture.brief.base_revision, self.protected_diff(fixture))
        present = self.integration(fixture, "resubmitted")
        self.assertEqual("content-present", present["presence"], present)
        self.assertEqual("protected-review", self.source(present)["kind"])
        self.assertEqual(candidate, self.source(present)["candidate_revision"])
        self.assertEqual(landed, present["resolved_revision"])
        cleanup: JsonObject = {
            "target_revision": fixture.brief.base_revision,
            "relation": "candidate-integrated",
            "phase": "cleanup",
            "effects": [
                {"effect": "source-checkout", "status": "allowed"},
                {"effect": "shared-work-root", "status": "not-required"},
                {"effect": "git-metadata", "status": "allowed"},
            ],
        }
        with patch("pinboard.adapters.files.root.observe_target_content", side_effect=AssertionError("git read")):
            inspected = call_advertised_tool(
                mcp_server.ATTEMPT_INSPECT_TOOL,
                {**self.roots(fixture), "attempt_id": "work-a-1", "reconciliation": cleanup},
            )
        self.assertEqual("ok", inspected["status"], inspected)
        next_operation = self.json_object(self.json_object(inspected["continuation"])["next_operation"])
        self.assertEqual("repository-cleanup", next_operation["kind"], inspected)
        self.assertEqual(fixture.brief.base_revision, next_operation["target_revision"], inspected)

    def test_rejections_name_their_observed_facts_and_effect(self) -> None:
        fixture = self.checkpoint_fixture()
        unresolved = self.integration(fixture, "no-such-target")
        self.assertEqual(
            {"target": "no-such-target", "project_root": str(fixture.project)}, self.observed_fields(unresolved)
        )
        self.assertEqual("unchanged", unresolved["effect"])
        unavailable = self.integration(fixture, "main", item_id="work-c")
        self.assertEqual("ready", self.observed_fields(unavailable)["item_state"], unavailable)
        self.assertEqual("correct-input", unavailable["retry"])
        self.assertEqual("unchanged", unavailable["effect"])
        self.assertIn("Read this item", str(unavailable["recovery"]))
        self.assertIn("work-c", str(unavailable["message"]))
        self.assertFalse(unavailable["state_changed"])

    def test_altered_snapshot_names_the_attempt_and_its_accepted_reference(self) -> None:
        fixture = self.checkpoint_fixture()
        reference = next(
            value
            for value in fixture.store.validated_snapshot().artifact_references
            if "-candidate-snapshot-" in value.key
        )
        (fixture.work / reference.selector).write_bytes(b"altered\n")
        invalid = self.integration(fixture, "main")
        observed = self.observed_fields(invalid)
        self.assertEqual("work-a-1", observed["attempt_id"], invalid)
        self.assertEqual(reference.key, observed["accepted_reference"], invalid)
        self.assertEqual("unchanged", invalid["effect"])

    def test_git_read_failure_is_a_typed_rejection_with_git_diagnostic(self) -> None:
        fixture = self.checkpoint_fixture()
        landed = self.land(fixture.project, "broken", fixture.brief.base_revision, self.snapshot_diff(fixture))
        tree = self.git(fixture.project, "rev-parse", f"{landed}^{{tree}}")
        loose = fixture.project / ".git" / "objects" / tree[:2] / tree[2:]
        self.assertTrue(loose.exists(), "The fixture's tree object must be loose so it can be removed.")
        loose.unlink()
        rejected = self.integration(fixture, "broken")
        self.assertEqual("PROJECT_GIT_CHECKOUT_UNAVAILABLE", rejected["code"], rejected)
        self.assertEqual("correct-input", rejected["retry"])
        self.assertEqual("unchanged", rejected["effect"])
        self.assertEqual(str(fixture.project), self.observed_fields(rejected)["project_root"])
        self.assertIn("Correct the checkout", str(rejected["recovery"]))
        self.assertIn("tree object", str(rejected["message"]))

    def test_target_side_rename_and_binary_file_are_compared_by_content(self) -> None:
        fixture = self.checkpoint_fixture()
        base = fixture.brief.base_revision
        reviewed = self.changed_path(fixture)
        landed = self.land(fixture.project, "rename-landed", base, self.snapshot_diff(fixture))
        renamed = self.commit_rename(fixture.project, landed, reviewed, "moved-after-review.txt")
        self.git(fixture.project, "update-ref", "refs/heads/renamed-target", renamed)
        self.assertEqual("content-not-present", self.integration(fixture, "renamed-target")["presence"])
        binary = self.commit_with_file(fixture.project, landed, "blob.bin", b"\x00\x01binary\x02")
        self.git(fixture.project, "update-ref", "refs/heads/binary-target", binary)
        present = self.integration(fixture, "binary-target")
        self.assertEqual("content-present", present["presence"], present)

    def recorded_integration(self, fixture: CheckpointFixture, target: str) -> tuple[JsonObject, list[str], list[str]]:
        """Run the leaf while recording every statement SQLite executes for its store reads."""

        real_open = sqlite_store.open_database
        recorded: list[str] = []

        def recording_open(path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = real_open(path, mode)
            connection.set_trace_callback(recorded.append)
            return connection

        with patch.object(sqlite_store, "open_database", side_effect=recording_open):
            result = self.integration(fixture, target)
        selects = [sql for sql in recorded if sql.lstrip().upper().startswith("SELECT")]
        connection = sqlite3.connect(f"file:{fixture.work / 'state.sqlite3'}?mode=ro", uri=True)
        try:
            plans = [str(row[3]) for sql in selects for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}")]
        finally:
            connection.close()
        return result, selects, plans

    def test_integration_leaf_reads_only_its_named_item_through_keyed_indexes(self) -> None:
        fixture = self.accepted_package_fixture()
        self.close_prerequisite(fixture)
        self.transition(fixture, "resume:work-a", {})
        landed = self.land(fixture.project, "scoped", fixture.brief.base_revision, self.snapshot_diff(fixture))
        present, selects, plans = self.recorded_integration(fixture, "scoped")
        self.assertEqual(landed, present["resolved_revision"])
        self.assertTrue(selects, "The integration read must run store statements.")
        for sql in selects:
            self.assertNotIn("work-b", sql)
            self.assertNotIn("work-c", sql)
        self.assertFalse([detail for detail in plans if detail.startswith("SCAN")], plans)
        self.assertTrue(any("checkpoint_history_by_subject" in detail for detail in plans), plans)

    def test_unrelated_attempts_receipts_and_artifacts_are_neither_read_nor_returned(self) -> None:
        fixture = self.accepted_package_fixture()
        self.close_prerequisite(fixture)
        self.transition(fixture, "resume:work-a", {})
        landed = self.land(fixture.project, "unrelated-scope", fixture.brief.base_revision, self.snapshot_diff(fixture))
        connection = sqlite3.connect(fixture.work / "state.sqlite3")
        try:
            connection.execute(
                """
                INSERT INTO attempts (attempt_id, item_id, state, branch, base_revision, provenance,
                    brief_artifact_ref_id, brief_artifact_kind, result_artifact_ref_id, result_artifact_kind,
                    candidate_revision, candidate_recorded_at, accepted_scope_revision, accepted_scope_digest,
                    subject_revision, recorded_at, updated_at)
                SELECT 'work-b-1', 'work-b', 'done', branch, base_revision, provenance,
                    brief_artifact_ref_id, brief_artifact_kind, NULL, NULL, NULL, NULL,
                    accepted_scope_revision, accepted_scope_digest, subject_revision, recorded_at, updated_at
                FROM attempts WHERE attempt_id = 'work-a-1'
                """
            )
            connection.execute(
                """
                INSERT INTO transition_history (project_revision, action_id, action_kind, subject_id, artifact_ref_id,
                    artifact_kind, authorization_kind, actor_task_id, actor_host_id, input_schema, input_json,
                    outcome_schema, outcome_json, committed_at)
                SELECT (SELECT MAX(project_revision) + 1 FROM transition_history), 'accept-checkpoint:work-b-1',
                    action_kind, 'work-b-1', NULL, NULL, authorization_kind, actor_task_id, actor_host_id,
                    input_schema, input_json, outcome_schema, outcome_json, committed_at
                FROM transition_history WHERE outcome_schema = 'checkpoint-acceptance/v2' LIMIT 1
                """
            )
            connection.execute(
                """
                INSERT INTO artifact_refs (artifact_key, artifact_revision, kind, relative_path, content_sha256,
                    size_bytes, accepted_revision, created_at)
                VALUES ('work-b-1-unrelated-evidence', 1, 'evidence', 'artifacts/evidence/work-b-1-unrelated/1.txt',
                    ?, 1, 1, '2030-01-02T03:04:05+00:00')
                """,
                (hashlib.sha256(b"x").hexdigest(),),
            )
            connection.commit()
        finally:
            connection.close()
        present, selects, plans = self.recorded_integration(fixture, "unrelated-scope")
        self.assertEqual("content-present", present["presence"], present)
        self.assertEqual(landed, present["resolved_revision"])
        self.assertEqual("work-a-1", self.source(present)["attempt_id"])
        for sql in selects:
            self.assertNotIn("work-b", sql)
            self.assertNotIn("unrelated-evidence", sql)
        self.assertFalse([detail for detail in plans if detail.startswith("SCAN")], plans)

    def test_overview_item_leaf_and_actions_issue_no_integration_reads(self) -> None:
        fixture = self.checkpoint_fixture()
        with (
            patch("pinboard.adapters.files.root.observe_target_content", side_effect=AssertionError("git read")),
            patch.object(sqlite_store.SQLiteWorkStore, "read_integration_item", side_effect=AssertionError("read")),
        ):
            item = call_advertised_tool(
                mcp_server.ITEM_STATUS_TOOL,
                {"request": {**self.roots(fixture), "operation": "item", "item_id": "work-a"}},
            )
            overview = call_advertised_tool(mcp_server.OVERVIEW_TOOL, self.roots(fixture))
            actions = call_advertised_tool(
                tool_names.ACTIONS_TOOL,
                {"request": {**self.roots(fixture), "role": "project"}},
            )
        self.assertEqual("pinboard-item-status/v2", item["schema"], item)
        self.assertEqual("pinboard-overview/v6", overview["schema"], overview)
        self.assertEqual("pinboard-mcp-actions-result/v1", actions["schema"], actions)

    def test_trace_override_applies_the_named_items_mode_to_the_integration_leaf(self) -> None:
        fixture = self.checkpoint_fixture()
        arguments: JsonObject = {
            "request": {
                **self.roots(fixture),
                "operation": "integration",
                "item_id": "work-a",
                "target": "main",
            }
        }
        (fixture.work / contributor_traces.SETTINGS_NAME).write_text(
            '[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = off\n[item "work-a"]\n\tmode = on\n',
            encoding="utf-8",
        )
        selected = execution.AutomaticCapture(mcp_common.select_capture_item).resolve(str(fixture.project), arguments)
        self.assertIsNotNone(selected)
        (fixture.work / contributor_traces.SETTINGS_NAME).write_text(
            '[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = on\n[item "work-a"]\n\tmode = off\n',
            encoding="utf-8",
        )
        unselected = execution.AutomaticCapture(mcp_common.select_capture_item).resolve(str(fixture.project), arguments)
        self.assertIsNone(unselected)

    def test_empty_recorded_diff_reports_no_change_through_the_leaf(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Rework the candidate.")
        self.git(fixture.project, "checkout", "--", ".")
        observed = call_native_tool(
            tool_names.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"}
        )
        candidate = observed["candidate"]
        assert isinstance(candidate, str), observed
        lease = self.native_attempt_acquire(fixture, "worker-empty")
        submission = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        self.assertEqual("committed", self.transition_result(fixture, submission, {"candidate": candidate})["status"])
        base = fixture.brief.base_revision
        self.git(fixture.project, "update-ref", "refs/heads/empty-target", base)
        unchanged = self.integration(fixture, "empty-target")
        self.assertEqual("no-change", unchanged["presence"], unchanged)
        self.assertEqual(base, unchanged["resolved_revision"])
        self.assertEqual(candidate, self.source(unchanged)["candidate_revision"])
        self.assertEqual(base, self.source(unchanged)["compared_from_revision"])

    def test_unreadable_snapshot_file_propagates_instead_of_reading_as_damaged_evidence(self) -> None:
        fixture = self.checkpoint_fixture()
        reference = next(
            value
            for value in fixture.store.validated_snapshot().artifact_references
            if "-candidate-snapshot-" in value.key
        )
        path = fixture.work / reference.selector
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        facts = fixture.store.read_integration_item(WorkItemId("work-a"))
        assert facts is not None
        with self.assertRaises(ArtifactError) as raised:
            candidate_evidence.read_integration_candidate(fixture.work, fixture.store, facts)
        self.assertIsInstance(raised.exception.__cause__, OSError)
