"""The item-status integration leaf reports whether a reviewed candidate's diff reached a named target."""

import asyncio
import contextlib
import hashlib
import io
import os
import sqlite3
import stat
import subprocess
import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

from mcp import Client

from pinboard.adapters.files import contributor_traces, root
from pinboard.adapters.files.errors import RootError
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.domain import decision_models
from pinboard.domain.identifiers import AttemptId, WorkItemId
from pinboard.mcp import common as mcp_common
from pinboard.mcp import execution as mcp_execution
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import JsonObject, JsonValue
from tests.test_item_status_facts import ItemStatusSupport

FIXED_DATE = "2030-01-05T00:00:00Z"
FIXED_GIT_ENVIRONMENT = {
    "GIT_AUTHOR_DATE": FIXED_DATE,
    "GIT_COMMITTER_DATE": FIXED_DATE,
    "GIT_AUTHOR_NAME": "Pinboard Tests",
    "GIT_AUTHOR_EMAIL": "pinboard@example.invalid",
    "GIT_COMMITTER_NAME": "Pinboard Tests",
    "GIT_COMMITTER_EMAIL": "pinboard@example.invalid",
}


def git(cwd: Path, *arguments: str, stdin: bytes | None = None) -> str:
    """Run one Git command with fixed author and committer identities and dates."""

    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        input=stdin,
        env={**os.environ, **FIXED_GIT_ENVIRONMENT},
    ).stdout.decode()


def commit_tree_with_diff(cwd: Path, parents: tuple[str, ...], diff: bytes, message: str) -> str:
    """Create a commit whose tree is the first parent's tree plus ``diff``, without touching the checkout.

    Working through a private index keeps the fixture's dirty working tree and real index intact,
    so a working-tree candidate stays protected while its target history is built around it.
    """

    with tempfile.TemporaryDirectory() as temporary:
        environment = {**os.environ, **FIXED_GIT_ENVIRONMENT, "GIT_INDEX_FILE": str(Path(temporary) / "index")}

        def run(*arguments: str, stdin: bytes | None = None) -> str:
            return subprocess.run(
                ["git", *arguments], cwd=cwd, check=True, capture_output=True, input=stdin, env=environment
            ).stdout.decode()

        run("read-tree", parents[0])
        if diff:
            run("apply", "--cached", "-", stdin=diff)
        tree = run("write-tree").strip()
        parent_arguments = [argument for parent in parents for argument in ("-p", parent)]
        return run("commit-tree", tree, *parent_arguments, "-m", message).strip()


def commit_tree_with_file(cwd: Path, parent: str, path: str, content: str, message: str) -> str:
    """Create a commit whose tree is the parent's tree with one file replaced, without touching the checkout."""

    with tempfile.TemporaryDirectory() as temporary:
        environment = {**os.environ, **FIXED_GIT_ENVIRONMENT, "GIT_INDEX_FILE": str(Path(temporary) / "index")}

        def run(*arguments: str, stdin: bytes | None = None) -> str:
            return subprocess.run(
                ["git", *arguments], cwd=cwd, check=True, capture_output=True, input=stdin, env=environment
            ).stdout.decode()

        run("read-tree", parent)
        blob = run("hash-object", "-w", "--stdin", stdin=content.encode()).strip()
        run("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
        tree = run("write-tree").strip()
        return run("commit-tree", tree, "-p", parent, "-m", message).strip()


def snapshot_repository(project: Path) -> tuple[dict[str, bytes], str, str]:
    """Fingerprint every .git file, HEAD, the working tree status, and the object count."""

    git_directory = project / ".git"
    files = {
        str(path.relative_to(git_directory)): hashlib.sha256(path.read_bytes()).digest()
        for path in sorted(git_directory.rglob("*"))
        if path.is_file()
    }
    head = (git_directory / "HEAD").read_bytes().decode()
    status = subprocess.run(
        ["git", "--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=project,
        check=True,
        capture_output=True,
    ).stdout.decode()
    objects = subprocess.run(["git", "count-objects", "-v"], cwd=project, check=True, capture_output=True, text=True)
    return files, head, status + objects.stdout


class DiffPresenceReadTest(CheckpointPackageSupport):
    def repository(self) -> tuple[Path, str, bytes]:
        project = Path(tempfile.mkdtemp()).resolve()
        git(project, "init", "-b", "main")
        (project / "notes.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
        git(project, "add", "notes.txt")
        git(project, "commit", "-m", "base")
        base = git(project, "rev-parse", "HEAD").strip()
        (project / "notes.txt").write_text("one\nTWO\nthree\nfour\n", encoding="utf-8")
        diff = subprocess.run(["git", "diff", "--binary", "HEAD", "--"], cwd=project, check=True, capture_output=True)
        git(project, "checkout", "--", "notes.txt")
        return project, base, diff.stdout

    @contextlib.contextmanager
    def tracked_temporary_directories(self) -> Generator[list[Path]]:
        created: list[Path] = []
        original = tempfile.mkdtemp

        def record(*arguments: str, **keywords: str) -> str:
            path = original(*arguments, **keywords)
            created.append(Path(path))
            return path

        with patch.object(tempfile, "mkdtemp", record):
            yield created

    def test_presence_is_judged_against_the_resolved_target_tree(self) -> None:
        project, base, diff = self.repository()
        self.assertEqual(base, root.resolve_integration_target(project, "main"))
        self.assertFalse(root.observe_diff_presence(project, base, diff))
        merged = commit_tree_with_diff(project, (base,), diff, "squash")
        git(project, "update-ref", "refs/heads/main", merged)
        git(project, "update-ref", "refs/remotes/origin/main", merged)
        git(project, "tag", "release", merged)
        for target in ("main", "origin/main", "release", merged):
            with self.subTest(target=target):
                self.assertEqual(merged, root.resolve_integration_target(project, target))
        self.assertTrue(root.observe_diff_presence(project, merged, diff))
        self.assertEqual(
            root.UnresolvedIntegrationTarget("nowhere"), root.resolve_integration_target(project, "nowhere")
        )
        with self.assertRaises(RootError):
            root.resolve_integration_target(Path(tempfile.mkdtemp()), "main")

    def test_read_only_git_metadata_stays_unchanged_and_leaves_no_temporary_directory(self) -> None:
        project, base, diff = self.repository()
        (project / "notes.txt").write_text("one\ntwo\nthree\nfour\nlocal edit\n", encoding="utf-8")
        before = snapshot_repository(project)
        git_paths = [project / ".git", *(project / ".git").rglob("*")]
        modes = {path: path.stat().st_mode for path in git_paths}
        for path in git_paths:
            path.chmod(modes[path] & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        try:
            with self.tracked_temporary_directories() as created:
                self.assertFalse(root.observe_diff_presence(project, base, diff))
            self.assertEqual(1, len(created))
            self.assertFalse(created[0].exists())
        finally:
            for path in reversed(git_paths):
                path.chmod(modes[path])
        self.assertEqual(before, snapshot_repository(project))
        self.assertEqual("one\ntwo\nthree\nfour\nlocal edit\n", (project / "notes.txt").read_text(encoding="utf-8"))

    def test_verdict_ignores_user_whitespace_configuration(self) -> None:
        project, base, diff = self.repository()
        (project / "notes.txt").write_text("one\ntwo   \nthree\nfour\n", encoding="utf-8")
        trailing = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--"], cwd=project, check=True, capture_output=True
        ).stdout
        git(project, "checkout", "--", "notes.txt")
        merged = commit_tree_with_diff(project, (base,), trailing, "trailing whitespace")
        git(project, "config", "apply.whitespace", "error")
        git(project, "config", "core.splitIndex", "true")
        self.assertTrue(root.observe_diff_presence(project, merged, trailing))
        self.assertFalse(root.observe_diff_presence(project, base, trailing))
        self.assertFalse(root.observe_diff_presence(project, merged, diff))


class ItemIntegrationLeafTest(ItemStatusSupport):
    def integration(self, fixture: CheckpointFixture, target: str, item_id: str = "work-a") -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "integration", "item_id": item_id, "target": target}},
        )

    def base_revision(self, fixture: CheckpointFixture) -> str:
        return fixture.brief.base_revision

    def observed_facts(self, result: JsonObject) -> dict[str, JsonValue]:
        return {
            str(self.json_object(value)["field"]): self.json_object(value)["value"]
            for value in self.json_array(result["observed"])
        }

    def assert_rejected(
        self, result: JsonObject, code: str, retry: str, observed: dict[str, JsonValue], next_step: str
    ) -> dict[str, JsonValue]:
        self.assertEqual("pinboard-mcp-item-status-result/v3", result["schema"], result)
        self.assertEqual(("rejected", code, False), (result["status"], result["code"], result["state_changed"]))
        self.assertEqual(("unchanged", retry, []), (result["effect"], result["retry"], result["changed_surfaces"]))
        facts = self.observed_facts(result)
        for field, value in observed.items():
            self.assertEqual(value, facts[field], facts)
        if next_step:
            self.assertIn(next_step, str(result["recovery"]))
        return facts

    def assert_integration(
        self,
        result: JsonObject,
        *,
        target: str,
        target_revision: str,
        presence: str,
        source: dict[str, JsonValue],
    ) -> None:
        self.assertEqual("pinboard-item-integration/v1", result["schema"], result)
        self.assertEqual(("sqlite-v7", "work-a"), (result["authority"], result["item_id"]))
        self.assertEqual(
            (target, target_revision, presence), (result["target"], result["target_revision"], result["presence"])
        )
        self.assertEqual(source, result["source"])

    def protected_source(self, fixture: CheckpointFixture, compared_from: str) -> dict[str, JsonValue]:
        return {
            "kind": "protected-review",
            "attempt_id": "work-a-1",
            "candidate_revision": fixture.candidate_revision,
            "compared_from_revision": compared_from,
        }

    def test_working_tree_candidate_is_present_after_squash_and_later_non_overlapping_edit(self) -> None:
        fixture = self.checkpoint_fixture(
            tracked_base="one\ntwo\nthree\nfour\n", tracked_candidate="one\nTWO\nthree\nfour\n"
        )
        project, base = fixture.project, self.base_revision(fixture)
        ledger_before = hashlib.sha256((fixture.work / "state.sqlite3").read_bytes()).hexdigest()
        git(project, "update-ref", "refs/heads/main", base)
        self.assert_integration(
            self.integration(fixture, "main"),
            target="main",
            target_revision=base,
            presence="content-not-present",
            source=self.protected_source(fixture, base),
        )
        squashed = commit_tree_with_diff(project, (base,), fixture.candidate_bytes, "squash merge")
        git(project, "update-ref", "refs/heads/main", squashed)
        self.assert_integration(
            self.integration(fixture, "main"),
            target="main",
            target_revision=squashed,
            presence="content-present",
            source=self.protected_source(fixture, base),
        )
        non_overlapping = commit_tree_with_file(
            project, squashed, "tracked.txt", "one\nTWO\nthree\nfour\nfive\nsix\nseven\n", "later non-overlapping edit"
        )
        git(project, "update-ref", "refs/heads/main", non_overlapping)
        git(project, "update-ref", "refs/remotes/origin/main", non_overlapping)
        self.assertEqual("content-present", self.integration(fixture, "main")["presence"])
        remote = self.integration(fixture, "origin/main")
        self.assert_integration(
            remote,
            target="origin/main",
            target_revision=non_overlapping,
            presence="content-present",
            source=self.protected_source(fixture, base),
        )
        overlapping = commit_tree_with_file(
            project,
            non_overlapping,
            "tracked.txt",
            "one\nrewritten\nthree\nfour\nfive\nsix\nseven\n",
            "overlapping edit",
        )
        git(project, "update-ref", "refs/heads/main", overlapping)
        self.assertEqual("content-not-present", self.integration(fixture, "main")["presence"])
        self.assertEqual(ledger_before, hashlib.sha256((fixture.work / "state.sqlite3").read_bytes()).hexdigest())
        self.assertEqual("one\nTWO\nthree\nfour\n", (project / "tracked.txt").read_text(encoding="utf-8"))
        self.assert_valid(fixture)

    def test_commit_candidate_is_recognized_by_fast_forward_merge_commit_and_rebase(self) -> None:
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        project, base, candidate = fixture.project, self.base_revision(fixture), fixture.candidate_revision
        other = commit_tree_with_file(project, base, "other.txt", "unrelated main work\n", "unrelated main commit")
        rebased = commit_tree_with_diff(project, (other,), fixture.candidate_bytes, "rebased candidate")
        merge_commit = commit_tree_with_diff(project, (other, candidate), fixture.candidate_bytes, "merge candidate")
        self.assertNotEqual(candidate, rebased)
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", candidate, rebased], cwd=project, check=False, capture_output=True
        )
        self.assertEqual(1, ancestor.returncode)
        for label, target_revision in (
            ("fast-forward", candidate),
            ("merge-commit", merge_commit),
            ("rebase", rebased),
        ):
            with self.subTest(integration=label):
                git(project, "update-ref", "refs/heads/main", target_revision)
                self.assert_integration(
                    self.integration(fixture, "main"),
                    target="main",
                    target_revision=target_revision,
                    presence="content-present",
                    source=self.protected_source(fixture, base),
                )
        git(project, "update-ref", "refs/heads/main", other)
        self.assertEqual("content-not-present", self.integration(fixture, "main")["presence"])

    def test_renamed_and_binary_changes_are_recognized_after_squash(self) -> None:
        fixture = self.checkpoint_fixture()
        project, base = fixture.project, self.base_revision(fixture)
        self.return_for_review(fixture, "Add the binary asset.")
        git(project, "checkout", "--", "tracked.txt")
        git(project, "mv", "tracked.txt", "renamed.txt")
        (project / "renamed.txt").write_text("base\nrenamed content\n", encoding="utf-8")
        (project / "asset.bin").write_bytes(bytes(range(256)) * 4)
        git(project, "add", "renamed.txt", "asset.bin")
        observed = call_native_tool(
            mcp_server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"}
        )
        candidate = observed["candidate"]
        assert isinstance(candidate, str), observed
        lease = self.native_attempt_acquire(fixture, "worker-binary")
        submission = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        self.assertEqual("committed", self.transition_result(fixture, submission, {"candidate": candidate})["status"])
        diff = subprocess.run(["git", "diff", "--binary", "HEAD", "--"], cwd=project, check=True, capture_output=True)
        self.assertIn(b"GIT binary patch", diff.stdout)
        squashed = commit_tree_with_diff(project, (base,), diff.stdout, "squash rename and binary")
        git(project, "update-ref", "refs/heads/main", squashed)
        result = self.integration(fixture, "main")
        self.assertEqual("content-present", result["presence"], result)
        self.assertEqual(candidate, self.json_object(result["source"])["candidate_revision"])
        git(project, "update-ref", "refs/heads/main", base)
        self.assertEqual("content-not-present", self.integration(fixture, "main")["presence"])

    def test_empty_recorded_diff_is_no_change_without_the_presence_read(self) -> None:
        fixture = self.checkpoint_fixture()
        project, base = fixture.project, self.base_revision(fixture)
        self.return_for_review(fixture, "Nothing needed changing.")
        git(project, "checkout", "--", "tracked.txt")
        observed = call_native_tool(
            mcp_server.CANDIDATE_OBSERVE_TOOL, {**self.roots(fixture), "attempt_id": "work-a-1"}
        )
        candidate = observed["candidate"]
        assert isinstance(candidate, str), observed
        lease = self.native_attempt_acquire(fixture, "worker-empty")
        submission = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        self.assertEqual("committed", self.transition_result(fixture, submission, {"candidate": candidate})["status"])
        git(project, "update-ref", "refs/heads/main", base)
        with patch.object(root, "observe_diff_presence", side_effect=AssertionError("no presence read")):
            result = self.integration(fixture, "main")
        self.assert_integration(
            result,
            target="main",
            target_revision=base,
            presence="no-change",
            source={
                "kind": "protected-review",
                "attempt_id": "work-a-1",
                "candidate_revision": candidate,
                "compared_from_revision": base,
            },
        )

    def test_lifecycle_transitions_select_the_reviewed_candidate_source(self) -> None:
        fixture = self.accepted_package_fixture()
        project, base = fixture.project, self.base_revision(fixture)
        checkpoint_id = fixture.brief.checkpoint.checkpoint_id
        squashed = commit_tree_with_diff(project, (base,), fixture.candidate_bytes, "squash checkpoint")
        git(project, "update-ref", "refs/heads/main", squashed)
        accepted_checkpoint: dict[str, JsonValue] = {
            "kind": "accepted-checkpoint",
            "attempt_id": "work-a-1",
            "candidate_revision": fixture.candidate_revision,
            "compared_from_revision": base,
            "checkpoint_id": checkpoint_id,
        }
        with self.subTest(stage="paused after checkpoint acceptance"):
            self.assert_integration(
                self.integration(fixture, "main"),
                target="main",
                target_revision=squashed,
                presence="content-present",
                source=accepted_checkpoint,
            )
        with self.subTest(stage="resumed"):
            self.close_prerequisite(fixture)
            self.transition(fixture, "resume:work-a", {})
            self.assertEqual(accepted_checkpoint, self.integration(fixture, "main")["source"])
        with self.subTest(stage="rebound"):
            self.rebind(fixture, fixture.brief.branch, 2)
            self.assertEqual(accepted_checkpoint, self.integration(fixture, "main")["source"])
        with self.subTest(stage="resubmitted"):
            candidate = self.submit_candidate(fixture, "second")
            second = self.integration(fixture, "main")
            self.assertEqual("protected-review", self.json_object(second["source"])["kind"])
            self.assertEqual(candidate, self.json_object(second["source"])["candidate_revision"])
            self.assertEqual("content-not-present", second["presence"])
        with self.subTest(stage="returned for correction"):
            self.return_for_review(fixture, "Rework the second candidate.")
            self.assertEqual(accepted_checkpoint, self.integration(fixture, "main")["source"])
        with self.subTest(stage="accepted and continued"):
            candidate = self.submit_candidate(fixture, "third")
            self.transition(
                fixture, "accept-review-and-continue:work-a-1", {"candidate": candidate, "evidence": "Continue."}
            )
            self.assertEqual(accepted_checkpoint, self.integration(fixture, "main")["source"])
        self.assert_valid(fixture)

    def test_completion_selects_the_closing_candidate(self) -> None:
        fixture = self.checkpoint_fixture()
        project, base = fixture.project, self.base_revision(fixture)
        git(project, "update-ref", "refs/heads/main", base)
        self.complete(fixture, "Accepted and integrated by the maintainer.")
        completed = self.integration(fixture, "main")
        self.assert_integration(
            completed,
            target="main",
            target_revision=base,
            presence="content-not-present",
            source={
                "kind": "completion",
                "attempt_id": "work-a-1",
                "candidate_revision": fixture.candidate_revision,
                "compared_from_revision": base,
            },
        )
        squashed = commit_tree_with_diff(project, (base,), fixture.candidate_bytes, "squash final")
        git(project, "update-ref", "refs/heads/main", squashed)
        self.assertEqual("content-present", self.integration(fixture, "main")["presence"])
        self.assertEqual([], self.item_leaf(fixture)["attempts"])

    def test_every_integration_rejection_names_its_facts_and_next_step(self) -> None:
        fixture = self.checkpoint_fixture()
        project, base = fixture.project, self.base_revision(fixture)
        with self.subTest(rejection="unknown ref"):
            self.assert_rejected(
                self.integration(fixture, "nowhere"),
                "INTEGRATION_TARGET_UNRESOLVED",
                "correct-input",
                {"target": "nowhere", "project_root": str(project)},
                "fetch it outside Pinboard first",
            )
        with self.subTest(rejection="target beginning with a dash"):
            rejected = self.integration(fixture, "--output=/tmp/x")
            self.assertEqual(
                ("pinboard-mcp-item-status-result/v3", "ITEM_STATUS_INVALID"), (rejected["schema"], rejected["code"])
            )
        with self.subTest(rejection="unknown item"):
            self.assert_rejected(
                self.integration(fixture, "codex/work-a", item_id="missing"), "ITEM_NOT_FOUND", "correct-input", {}, ""
            )
        with self.subTest(rejection="ready item"):
            self.assert_rejected(
                self.integration(fixture, "codex/work-a", item_id="work-c"),
                "INTEGRATION_CANDIDATE_UNAVAILABLE",
                "correct-input",
                {
                    "item_id": "work-c",
                    "item_state": "ready",
                    "reason": "no-protected-candidate-and-no-checkpoint-acceptance",
                },
                "operation item",
            )
        with self.subTest(rejection="directly closed item"):
            self.close_prerequisite(fixture)
            self.assert_rejected(
                self.integration(fixture, "codex/work-a", item_id="work-c"),
                "INTEGRATION_CANDIDATE_UNAVAILABLE",
                "correct-input",
                {"item_id": "work-c", "item_state": "done", "reason": "closed-without-completion"},
                "operation item",
            )
        with self.subTest(rejection="altered accepted snapshot bytes"):
            snapshot = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
            assert snapshot is not None
            path = fixture.work / snapshot.reference.selector
            original = path.read_bytes()
            path.write_bytes(original + b"\n")
            try:
                facts = self.assert_rejected(
                    self.integration(fixture, "codex/work-a"),
                    "INTEGRATION_CANDIDATE_EVIDENCE_INVALID",
                    "do-not-retry",
                    {"attempt_id": "work-a-1", "selector": snapshot.reference.selector},
                    "pinboard validate",
                )
                self.assertEqual(int(snapshot.reference.artifact_ref_id), facts["artifact_ref_id"])
            finally:
                path.write_bytes(original)
        with self.subTest(rejection="active item without checkpoint acceptance"):
            self.return_for_review(fixture, "Rework the candidate.")
            self.assert_rejected(
                self.integration(fixture, "codex/work-a"),
                "INTEGRATION_CANDIDATE_UNAVAILABLE",
                "correct-input",
                {
                    "item_id": "work-a",
                    "item_state": "active",
                    "reason": "no-protected-candidate-and-no-checkpoint-acceptance",
                },
                "operation item",
            )
        with self.subTest(rejection="project root outside any Git checkout"):
            outside = Path(tempfile.mkdtemp()).resolve()
            rejected = call_advertised_tool(
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
            self.assert_rejected(
                rejected,
                "PROJECT_GIT_ROOT_UNAVAILABLE",
                "correct-input",
                {"project_root": str(outside)},
                "Correct the checkout",
            )
            self.assertIn("not a git repository", str(self.observed_facts(rejected)["git_diagnostic"]))
        with self.subTest(rejection="damaged checkpoint receipt"):
            accepted = self.accepted_package_fixture()
            history_id = self.latest_history_id(accepted)
            self.update_receipt(accepted, history_id, outcome_json='{"unexpected":true}')
            damaged = self.integration(accepted, "codex/work-a")
            self.assertEqual("pinboard-mcp-item-status-result/v3", damaged["schema"])
            named = self.named_receipt(damaged, decision_models.ActionKind.ACCEPT_CHECKPOINT)
            self.assertEqual(history_id, int(named.history_id))
        self.assertEqual(base, git(project, "rev-parse", "HEAD").strip())

    def test_retained_evidence_without_a_candidate_snapshot_is_unavailable(self) -> None:
        with self.subTest(rejection="retained checkpoint package without a candidate snapshot"):
            retained = self.accepted_package_fixture(candidate_form="current-head", local=True)
            self.retain_v2_checkpoint(retained)
            self.assert_rejected(
                self.integration(retained, "codex/work-a"),
                "INTEGRATION_CANDIDATE_UNAVAILABLE",
                "correct-input",
                {
                    "item_id": "work-a",
                    "item_state": "paused",
                    "reason": "checkpoint-package-without-candidate-snapshot",
                },
                "operation item",
            )
        with self.subTest(rejection="retained pre-snapshot review candidate"):
            legacy = self.checkpoint_fixture()
            with contextlib.closing(sqlite3.connect(legacy.work / "state.sqlite3")) as connection, connection:
                connection.execute(
                    "UPDATE transition_history SET artifact_ref_id = NULL, artifact_kind = NULL, "
                    "input_schema = 'decision/v1', input_json = '{}' WHERE action_kind = 'submit-review'"
                )
                connection.execute("DELETE FROM artifact_refs WHERE artifact_key LIKE '%-candidate-snapshot-%'")
                connection.execute(
                    "UPDATE attempts SET subject_revision = (SELECT project_revision FROM transition_history "
                    "WHERE action_kind = 'submit-review') WHERE attempt_id = 'work-a-1'"
                )
            self.assert_rejected(
                self.integration(legacy, "codex/work-a"),
                "INTEGRATION_CANDIDATE_UNAVAILABLE",
                "correct-input",
                {"item_id": "work-a", "item_state": "review", "reason": "retained-pre-snapshot-review-candidate"},
                "operation item",
            )

    @contextlib.contextmanager
    def record_store_reads(self) -> Generator[tuple[set[str], list[str]]]:
        read_tables: set[str] = set()
        statements: list[str] = []
        original_open = sqlite_store.open_database

        def traced_open(path: Path, mode: sqlite_store.OpenMode) -> sqlite3.Connection:
            connection = original_open(path, mode)

            def authorize(
                action: int, argument: str | None, _secondary: str | None, _database: str | None, _trigger: str | None
            ) -> int:
                if action == sqlite3.SQLITE_READ and argument is not None:
                    read_tables.add(argument)
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(sqlite_store, "open_database", traced_open):
            yield read_tables, statements

    def test_integration_reads_stay_keyed_or_indexed_and_ignore_unrelated_retained_data(self) -> None:
        fixture = self.accepted_package_fixture()
        base = self.base_revision(fixture)
        git(fixture.project, "update-ref", "refs/heads/main", base)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(attempts)")]
            for index in range(24):
                copied = ", ".join(
                    {
                        "attempt_id": f"'unrelated-{index}'",
                        "item_id": "'work-b'",
                        "state": "'done'",
                        "branch": f"'codex/unrelated-{index}'",
                    }.get(column, column)
                    for column in columns
                )
                connection.execute(
                    f"INSERT INTO attempts ({', '.join(columns)}) "
                    f"SELECT {copied} FROM attempts WHERE attempt_id = 'work-a-1'"
                )
            history_columns = [row[1] for row in connection.execute("PRAGMA table_info(transition_history)")]
            for index in range(24):
                copied = ", ".join(
                    {
                        "history_id": "(SELECT max(history_id) + 1 FROM transition_history)",
                        "project_revision": "(SELECT max(project_revision) + 1 FROM transition_history)",
                        "subject_id": f"'unrelated-{index}'",
                        "action_id": f"'pause:unrelated-{index}'",
                        "action_kind": "'pause'",
                        "artifact_ref_id": "NULL",
                        "artifact_kind": "NULL",
                        "input_schema": "'pause/v1'",
                        "input_json": "'{}'",
                        "outcome_schema": "'transition-receipt/v1'",
                        "outcome_json": """'{"outcome":"pause"}'""",
                    }.get(column, column)
                    for column in history_columns
                )
                connection.execute(
                    f"INSERT INTO transition_history ({', '.join(history_columns)}) "
                    f"SELECT {copied} FROM transition_history WHERE action_kind = 'accept-checkpoint' LIMIT 1"
                )
        with self.record_store_reads() as (tables, statements):
            result = self.integration(fixture, "main")
        self.assertEqual("content-not-present", result["presence"], result)
        self.assertEqual("accepted-checkpoint", self.json_object(result["source"])["kind"])
        self.assertEqual({"project_meta", "work_items", "attempts", "transition_history", "artifact_refs"}, tables)
        selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
        self.assertTrue(selects)
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            for statement in selects:
                with self.subTest(statement=statement):
                    plan = [str(row[3]).upper() for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}")]
                    self.assertTrue(any("SEARCH " in detail for detail in plan), plan)
                    self.assertFalse(any("SCAN " in detail for detail in plan), plan)
        self.assertTrue(any("checkpoint_history_by_subject" in statement for statement in statements), statements)
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3").read_item_integration(WorkItemId("work-a"))
        assert fresh is not None and fresh.latest_checkpoint is not None
        self.assertEqual("work-a-1", str(fresh.latest_checkpoint.receipt.subject_id))
        with self.record_store_reads() as (_tables, other_statements):
            self.item_leaf(fixture)
            self.branch_leaf(fixture, fixture.brief.branch)
            call_advertised_tool(mcp_server.OVERVIEW_TOOL, self.roots(fixture))
        self.assertFalse(any("checkpoint_history_by_subject" in statement for statement in other_statements))

    def test_item_and_branch_leaves_and_inspection_run_no_presence_read(self) -> None:
        fixture = self.accepted_package_fixture()
        with (
            patch.object(root, "observe_diff_presence", side_effect=AssertionError("presence read")),
            patch.object(root, "resolve_integration_target", side_effect=AssertionError("target resolution")),
        ):
            self.assertEqual("pinboard-item-status/v2", self.item_leaf(fixture)["schema"])
            self.assertEqual("pinboard-branch-owners/v1", self.branch_leaf(fixture, fixture.brief.branch)["schema"])
            self.assertEqual("ok", self.inspection(fixture)["status"])
            call_advertised_tool(mcp_server.OVERVIEW_TOOL, self.roots(fixture))
            reviewed = self.checkpoint_fixture()
            self.record_ready(reviewed, reviewed.candidate_revision)
            inspected = call_advertised_tool(
                mcp_server.ATTEMPT_INSPECT_TOOL,
                {
                    **self.roots(reviewed),
                    "attempt_id": "work-a-1",
                    "reconciliation": {
                        "target_revision": reviewed.brief.base_revision,
                        "relation": "candidate-integrated",
                        "phase": "cleanup",
                        "effects": [
                            {"effect": "source-checkout", "status": "allowed"},
                            {"effect": "shared-work-root", "status": "not-required"},
                            {"effect": "git-metadata", "status": "allowed"},
                        ],
                    },
                },
            )
        self.assertEqual("ok", inspected["status"], inspected)
        continuation = self.json_object(inspected["continuation"])
        self.assertIn("cleanup", str(continuation["next_operation"]).lower(), continuation)

    def test_integration_leaf_uses_the_item_trace_override(self) -> None:
        fixture = self.checkpoint_fixture()
        git(fixture.project, "update-ref", "refs/heads/main", self.base_revision(fixture))
        (fixture.work / contributor_traces.SETTINGS_NAME).write_text(
            '[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = off\n[item "work-a"]\n\tmode = on\n',
            encoding="utf-8",
        )
        arguments: JsonObject = {
            "request": {**self.roots(fixture), "operation": "integration", "item_id": "work-a", "target": "main"}
        }
        self.assertEqual("work-a", mcp_common.select_capture_item(fixture.project, str(fixture.work), arguments))
        executor = mcp_execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        server = mcp_server.create_server(
            executor,
            mcp_execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256),
            mcp_execution.AutomaticCapture(mcp_common.select_capture_item),
        )
        traces = fixture.work / contributor_traces.TRACE_DIRECTORY

        async def scenario() -> None:
            async with Client(server) as client:
                integration = await client.call_tool(mcp_server.ITEM_STATUS_TOOL, arguments)
                assert isinstance(integration.structured_content, dict)
                self.assertEqual("pinboard-item-integration/v1", integration.structured_content["schema"])
                self.assertEqual(1, len(tuple(traces.glob("pinboard-auto-mcp-*.json"))))
                await client.call_tool(
                    mcp_server.ITEM_STATUS_TOOL,
                    {"request": {**self.roots(fixture), "operation": "branch", "branch": fixture.brief.branch}},
                )
                self.assertEqual(1, len(tuple(traces.glob("pinboard-auto-mcp-*.json"))))

        asyncio.run(scenario())
