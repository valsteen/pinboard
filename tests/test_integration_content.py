"""The integration-by-content Git read and the item-status integration leaf."""

import contextlib
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import override
from unittest.mock import patch

from pinboard.adapters.files import root
from pinboard.adapters.files.errors import RootError, RootErrorCode
from pinboard.application import query_models
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import common as mcp_common
from pinboard.mcp import execution as mcp_execution
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import JsonObject, JsonValue

CLOSED_AT = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
COMPLETED_AT = datetime(2030, 1, 3, 4, 5, 6, tzinfo=UTC)
TRACE_SETTINGS = '[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = off\n[item "work-a"]\n\tmode = on\n'


def git(project: Path, *arguments: str) -> bytes:
    return subprocess.run(["git", *arguments], cwd=project, capture_output=True, check=True).stdout


def commit(project: Path, message: str) -> str:
    git(project, "add", "--all")
    git(project, "-c", "user.name=Tests", "-c", "user.email=tests@example.invalid", "commit", "-m", message)
    return head(project)


def head(project: Path) -> str:
    return git(project, "rev-parse", "HEAD").decode().strip()


class IntegrationContentReadTest(unittest.TestCase):
    """The Git adapter decides presence by reverse-applying recorded bytes to a target tree."""

    @override
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def repository(self, *, line: str = "candidate\n") -> tuple[Path, str, bytes]:
        project = self.directory / "repo"
        project.mkdir()
        git(project, "init", "-b", "work")
        (project / "tracked.txt").write_text("base\n", encoding="utf-8")
        base = commit(project, "base")
        (project / "tracked.txt").write_text(line, encoding="utf-8")
        diff = git(project, "diff", "--binary", "--no-color", "HEAD", "--")
        return project, base, diff

    def test_reports_present_for_a_target_commit_and_for_a_branch_name(self) -> None:
        project, _base, diff = self.repository()
        target = commit(project, "candidate")
        self.assertEqual(
            root.IntegrationTargetObservation(target, "content-present"),
            root.observe_integration_content(project, target, diff),
        )
        self.assertEqual(
            root.IntegrationTargetObservation(target, "content-present"),
            root.observe_integration_content(project, "work", diff),
        )

    def test_reports_not_present_when_the_target_commit_lacks_the_change(self) -> None:
        project, base, diff = self.repository()
        self.assertEqual(
            root.IntegrationTargetObservation(base, "content-not-present"),
            root.observe_integration_content(project, base, diff),
        )

    def test_reports_no_change_without_reading_the_target_tree(self) -> None:
        project, base, _diff = self.repository()
        self.assertEqual(
            root.IntegrationTargetObservation(base, "no-change"),
            root.observe_integration_content(project, base, b""),
        )

    def test_reports_an_unresolved_target_for_a_name_that_is_no_commit(self) -> None:
        project, _base, diff = self.repository()
        self.assertEqual(
            root.IntegrationTargetUnresolved("no-such-ref"),
            root.observe_integration_content(project, "no-such-ref", diff),
        )

    def test_reports_the_adapters_checkout_failure_outside_a_git_checkout(self) -> None:
        plain = self.directory / "plain"
        plain.mkdir()
        with self.assertRaises(RootError) as raised:
            root.observe_integration_content(plain, "HEAD", b"diff")
        self.assertIs(RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE, raised.exception.code)

    def test_whitespace_configuration_does_not_change_the_verdict(self) -> None:
        project, _base, diff = self.repository(line="candidate  \n")
        target = commit(project, "candidate")
        git(project, "config", "apply.whitespace", "error")
        self.assertEqual(
            root.IntegrationTargetObservation(target, "content-present"),
            root.observe_integration_content(project, target, diff),
        )

    def test_a_rename_reverse_applies_against_the_renamed_target_tree(self) -> None:
        project = self.directory / "renamed"
        project.mkdir()
        git(project, "init", "-b", "work")
        (project / "tracked.txt").write_text("base\n", encoding="utf-8")
        commit(project, "base")
        git(project, "mv", "tracked.txt", "renamed.txt")
        diff = git(project, "diff", "--binary", "--no-color", "HEAD", "--")
        self.assertIn(b"rename from", diff)
        target = commit(project, "candidate")
        self.assertEqual(
            root.IntegrationTargetObservation(target, "content-present"),
            root.observe_integration_content(project, target, diff),
        )

    def test_a_binary_change_reverse_applies_against_the_target_tree(self) -> None:
        project = self.directory / "binary"
        project.mkdir()
        git(project, "init", "-b", "work")
        (project / "blob.bin").write_bytes(b"\x00\x01base")
        commit(project, "base")
        (project / "blob.bin").write_bytes(b"\x00\x02candidate\xff")
        diff = git(project, "diff", "--binary", "--no-color", "HEAD", "--")
        self.assertIn(b"GIT binary patch", diff)
        target = commit(project, "candidate")
        self.assertEqual(
            root.IntegrationTargetObservation(target, "content-present"),
            root.observe_integration_content(project, target, diff),
        )

    def test_read_only_metadata_leaves_every_repository_fact_unchanged(self) -> None:
        project, _base, diff = self.repository()
        target = commit(project, "candidate")
        before = self.repository_facts(project)
        temporary = self.directory / "temporary"
        temporary.mkdir()
        with self.read_only(project / ".git"), patch.object(tempfile, "tempdir", str(temporary)):
            observation = root.observe_integration_content(project, target, diff)
        self.assertEqual(root.IntegrationTargetObservation(target, "content-present"), observation)
        self.assertEqual(before, self.repository_facts(project))
        self.assertEqual([], list(temporary.iterdir()))

    @contextlib.contextmanager
    def read_only(self, directory: Path) -> Generator[None]:
        touched = [path for path in directory.rglob("*") if path.is_file() or path.is_dir()]
        for path in touched:
            path.chmod(0o500 if path.is_dir() else 0o400)
        try:
            yield
        finally:
            for path in touched:
                path.chmod(0o700 if path.is_dir() else 0o600)

    def repository_facts(self, project: Path) -> JsonObject:
        metadata: dict[str, JsonValue] = {
            str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((project / ".git").rglob("*"))
            if path.is_file()
        }
        working: dict[str, JsonValue] = {
            str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(project.rglob("*"))
            if path.is_file() and ".git" not in path.parts
        }
        return {"metadata": metadata, "working": working, "objects": git(project, "count-objects", "-v").decode()}


class IntegrationContentLeafTest(CheckpointPackageSupport):
    """The item-status integration leaf selects one closed source and reports content presence."""

    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def integration(self, fixture: CheckpointFixture, target: str, item_id: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": item_id, "target": target}},
        )

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

    def transition(self, fixture: CheckpointFixture, action_id: str, payload: JsonObject) -> JsonObject:
        result = self.transition_result(fixture, self.project_action(fixture, action_id), payload)
        self.assertIn(result["status"], ("committed", "committed-with-warning"), result)
        return result

    def return_for_review(self, fixture: CheckpointFixture, reason: str) -> None:
        self.transition(fixture, "return-for-correction:work-a-1", {"reason": reason})

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

    def source(self, result: JsonObject) -> JsonObject:
        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        return self.json_object(result["source"])

    def observed(self, result: JsonObject) -> JsonObject:
        return {
            str(self.json_object(fact)["field"]): self.json_object(fact)["value"]
            for fact in self.json_array(result["observed"])
        }

    def test_protected_working_tree_candidate_reports_present(self) -> None:
        fixture = self.checkpoint_fixture()
        target = commit(fixture.project, "squash")
        result = self.integration(fixture, target)
        source = self.source(result)
        self.assertEqual("work-a", result["item_id"])
        self.assertEqual(target, result["target"])
        self.assertEqual(target, result["target_revision"])
        self.assertEqual("content-present", result["presence"])
        self.assertEqual("protected-review", source["variant"])
        self.assertEqual("work-a-1", source["attempt_id"])
        self.assertEqual(fixture.candidate_revision, source["candidate_revision"])
        self.assertEqual(fixture.brief.base_revision, source["compared_from"])

    def test_protected_commit_candidate_is_compared_by_content_for_every_integration_form(self) -> None:
        for form in ("fast-forward", "merge-commit", "rebase-merge"):
            with self.subTest(form=form):
                fixture = self.checkpoint_fixture(candidate_form="current-head")
                base = git(fixture.project, "rev-parse", f"{fixture.candidate_revision}^").decode().strip()
                git(fixture.project, "switch", "--detach", base)
                if form == "fast-forward":
                    git(fixture.project, "merge", "--ff-only", fixture.candidate_revision)
                else:
                    (fixture.project / "sibling.txt").write_text("unrelated\n", encoding="utf-8")
                    commit(fixture.project, "sibling")
                    if form == "merge-commit":
                        git(
                            fixture.project,
                            "-c",
                            "user.name=Tests",
                            "-c",
                            "user.email=tests@example.invalid",
                            "merge",
                            "--no-ff",
                            "-m",
                            "merge",
                            fixture.candidate_revision,
                        )
                    else:
                        git(fixture.project, "cherry-pick", fixture.candidate_revision)
                target = head(fixture.project)
                result = self.integration(fixture, target)
                self.assertEqual("content-present", result["presence"], result)
                self.assertEqual(target, result["target_revision"])
                self.assertEqual("protected-review", self.source(result)["variant"])

    def test_content_not_present_for_a_later_overlapping_edit(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        (fixture.project / "tracked.txt").write_text("rewritten\n", encoding="utf-8")
        edited = commit(fixture.project, "rewrite")
        result = self.integration(fixture, edited)
        self.assertEqual("content-not-present", result["presence"])
        self.assertEqual(edited, result["target_revision"])

    def test_content_present_after_a_later_non_overlapping_edit(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        (fixture.project / "sibling.txt").write_text("unrelated\n", encoding="utf-8")
        edited = commit(fixture.project, "sibling")
        result = self.integration(fixture, edited)
        self.assertEqual("content-present", result["presence"])

    def test_content_not_present_before_integration(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        base = git(fixture.project, "rev-parse", f"{fixture.candidate_revision}^").decode().strip()
        result = self.integration(fixture, base)
        self.assertEqual("content-not-present", result["presence"])
        self.assertEqual(base, result["target_revision"])

    def test_comparison_covers_a_working_tree_preimage_beyond_the_accepted_base(self) -> None:
        fixture = self.checkpoint_fixture(committed_context=True)
        preimage = head(fixture.project)
        self.assertNotEqual(fixture.brief.base_revision, preimage)
        result = self.integration(fixture, preimage)
        self.assertEqual("content-not-present", result["presence"])
        self.assertEqual(preimage, self.source(result)["compared_from"])

    def test_no_change_for_an_empty_recorded_diff(self) -> None:
        fixture = self.checkpoint_fixture(unchanged=True)
        result = self.integration(fixture, head(fixture.project))
        self.assertEqual("no-change", result["presence"])
        self.assertEqual("protected-review", self.source(result)["variant"])

    def test_remote_tracking_ref_name_is_read_locally_without_fetching(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        target = fixture.candidate_revision
        git(fixture.project, "update-ref", "refs/remotes/origin/topic", target)
        result = self.integration(fixture, "origin/topic")
        self.assertEqual("origin/topic", result["target"])
        self.assertEqual(target, result["target_revision"])
        self.assertEqual("content-present", result["presence"])

    def test_accepted_checkpoint_candidate_is_the_source_for_a_nonterminal_item(self) -> None:
        fixture = self.accepted_package_fixture()
        target = commit(fixture.project, "candidate")
        result = self.integration(fixture, target)
        source = self.source(result)
        self.assertEqual("content-present", result["presence"])
        self.assertEqual("accepted-checkpoint", source["variant"])
        self.assertEqual("work-a-1", source["attempt_id"])
        self.assertEqual(fixture.brief.checkpoint.checkpoint_id, source["checkpoint"])

    def test_completion_candidate_is_the_source_after_a_completed_item(self) -> None:
        fixture = self.checkpoint_fixture()
        self.close_prerequisite(fixture)
        self.complete(fixture, "Accepted and integrated by the maintainer.")
        target = commit(fixture.project, "candidate")
        result = self.integration(fixture, target)
        source = self.source(result)
        self.assertEqual("content-present", result["presence"])
        self.assertEqual("completion", source["variant"])
        self.assertEqual("work-a-1", source["attempt_id"])

    def test_unknown_target_is_a_correct_input_rejection(self) -> None:
        fixture = self.checkpoint_fixture()
        result = self.integration(fixture, "no-such-ref")
        self.assertEqual("rejected", result["status"], result)
        self.assertEqual("INTEGRATION_TARGET_UNRESOLVED", result["code"], result)
        self.assertFalse(result["state_changed"])
        self.assertEqual("correct-input", result["retry"])
        self.assertEqual("unchanged", result["effect"])
        observed = self.observed(result)
        self.assertEqual("no-such-ref", observed["target"])
        self.assertEqual(str(fixture.project), observed["project_root"])
        self.assertIn("remote", str(result["recovery"]))

    def test_target_beginning_with_a_dash_is_rejected_by_the_closed_request(self) -> None:
        fixture = self.checkpoint_fixture()
        result = call_native_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    **self.roots(fixture),
                    "operation": "integration",
                    "item_id": "work-a",
                    "target": "--help",
                }
            },
        )
        self.assertEqual("ITEM_STATUS_INVALID", result["code"], result)
        self.assertEqual("correct-input", result["retry"])
        self.assertIn("target", str(result["message"]))

    def test_unknown_request_field_is_rejected(self) -> None:
        fixture = self.checkpoint_fixture()
        result = call_native_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    **self.roots(fixture),
                    "operation": "integration",
                    "item_id": "work-a",
                    "target": "HEAD",
                    "unexpected": True,
                }
            },
        )
        self.assertEqual("ITEM_STATUS_INVALID", result["code"], result)
        self.assertEqual("correct-input", result["retry"])
        self.assertIn("unexpected", str(result["message"]))

    def test_item_without_a_reviewed_candidate_is_unavailable(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Send it back for correction.")
        result = self.integration(fixture, head(fixture.project))
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", result["code"], result)
        self.assertEqual("correct-input", result["retry"])
        self.assertEqual("unchanged", result["effect"])
        self.assertEqual("work-a", self.observed(result)["item_id"])
        self.assertIn("operation item", str(result["recovery"]))

    def test_ready_item_is_unavailable(self) -> None:
        fixture = self.checkpoint_fixture()
        result = self.integration(fixture, "HEAD", item_id="work-c")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", result["code"], result)
        self.assertEqual("ready", self.observed(result)["item_state"])

    def test_directly_closed_item_is_unavailable(self) -> None:
        fixture = self.checkpoint_fixture()
        self.close_prerequisite(fixture)
        result = self.integration(fixture, "HEAD", item_id="work-c")
        self.assertEqual("INTEGRATION_CANDIDATE_UNAVAILABLE", result["code"], result)
        self.assertEqual("done", self.observed(result)["item_state"])
        self.assertIn("close", str(result["message"]))

    def test_altered_candidate_evidence_is_a_diagnosis_only_rejection(self) -> None:
        fixture = self.checkpoint_fixture()
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        self.assertIsNotNone(context)
        assert context is not None
        fixture.work.joinpath(context.reference.selector).write_bytes(b"altered\n")
        result = self.integration(fixture, "HEAD")
        self.assertEqual("INTEGRATION_CANDIDATE_EVIDENCE_INVALID", result["code"], result)
        self.assertEqual("do-not-retry", result["retry"])
        self.assertEqual("unchanged", result["effect"])
        self.assertFalse(result["state_changed"])
        self.assertEqual("work-a-1", self.observed(result)["attempt_id"])
        self.assertIn("validate", str(result["recovery"]))

    def test_project_root_outside_a_git_checkout_reports_the_adapters_root_error(self) -> None:
        fixture = self.checkpoint_fixture()
        outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        result = call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "project_root": str(outside),
                    "work_root": str(fixture.work),
                    "operation": "integration",
                    "item_id": "work-a",
                    "target": "HEAD",
                }
            },
        )
        self.assertEqual("PROJECT_GIT_ROOT_UNAVAILABLE", result["code"], result)
        self.assertEqual("correct-input", result["retry"])
        self.assertEqual("unchanged", result["effect"])
        self.assertEqual(str(outside), self.observed(result)["project_root"])

    def test_unknown_item_is_not_found(self) -> None:
        fixture = self.checkpoint_fixture()
        result = self.integration(fixture, "HEAD", item_id="no-such-item")
        self.assertEqual("ITEM_NOT_FOUND", result["code"], result)
        self.assertIsNone(result.get("recovery"))
        self.assertEqual("correct-input", result["retry"])
        self.assertEqual([], result["observed"])

    def test_the_integration_leaf_resolves_the_items_trace_override(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        fixture.work.joinpath("contributor-traces.config").write_text(TRACE_SETTINGS, encoding="utf-8")
        capture = mcp_execution.AutomaticCapture(mcp_common.select_capture_item)
        selected = capture.resolve(
            str(fixture.project),
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": "work-a", "target": "HEAD"}},
        )
        unowned = capture.resolve(
            str(fixture.project),
            {"request": {**self.roots(fixture), "operation": "branch", "branch": fixture.brief.branch}},
        )
        self.assertIsInstance(selected, mcp_execution.SemanticCapture)
        self.assertIsNone(unowned)

    def test_other_reads_never_reach_the_integration_git_read(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        with patch.object(root, "observe_integration_content") as observed:
            call_advertised_tool(
                mcp_server.OVERVIEW_TOOL,
                {"project_root": str(fixture.project), "work_root": str(fixture.work)},
            )
            call_advertised_tool(
                mcp_server.ACTIONS_TOOL,
                {"request": {**self.roots(fixture), "role": "observer"}},
            )
            self.item_leaf(fixture)
            self.branch_leaf(fixture, fixture.brief.branch)
            observed.assert_not_called()

    def test_attempt_inspection_runs_no_git_read_and_gains_no_integration_vocabulary(self) -> None:
        fixture = self.checkpoint_fixture()
        with patch.object(root, "observe_integration_content") as observed:
            inspected = call_advertised_tool(
                mcp_server.ATTEMPT_INSPECT_TOOL,
                {**self.roots(fixture), "attempt_id": "work-a-1", "reconciliation": None},
            )
        observed.assert_not_called()
        encoded = str(inspected)
        for term in (term.value for term in query_models.IntegrationPresence):
            self.assertNotIn(term, encoded)
        self.assertNotIn("integration", set(self.json_object(inspected)))

    def test_the_other_two_leaves_gain_no_integration_output(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        overview = call_advertised_tool(
            mcp_server.OVERVIEW_TOOL,
            {"project_root": str(fixture.project), "work_root": str(fixture.work)},
        )
        branch = self.branch_leaf(fixture, fixture.brief.branch)
        item = self.item_leaf(fixture)
        for payload in (overview, branch, item):
            self.assertNotIn("integration", set(self.json_object(payload)))
