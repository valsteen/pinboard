import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import chdir
from pathlib import Path
from threading import Barrier
from typing import ClassVar, override
from unittest.mock import patch

from pinboard.adapters.files.errors import RootError, RootErrorCode
from pinboard.adapters.files.root import (
    CurrentHeadCandidate,
    ResolvedTargetPresence,
    ResolvedTargetWithoutChange,
    UnresolvedTargetObservation,
    classify_checkout,
    ensure_default_git_exclude,
    observe_checkout_identity,
    observe_target_presence,
    read_current_head_candidate,
    resolve_shared_repository_root,
    resolve_source_checkout_root,
)
from pinboard.cli.entrypoint import main
from pinboard.domain import work_models
from pinboard.mcp import server
from tests.native_support import call_native_tool


class RootResolutionTest(unittest.TestCase):
    def run_git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            text=True,
            capture_output=True,
        ).stdout

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_linked_worktree_owns_sources_while_the_repository_owns_the_default_ledger(self) -> None:
        temporary = Path(tempfile.mkdtemp())
        repository = temporary / "repository"
        linked = temporary / "linked"
        repository.mkdir()
        self.run_git(repository, "init", "-b", "main")
        (repository / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self.run_git(repository, "add", "tracked.txt")
        self.run_git(
            repository,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial",
        )
        self.run_git(repository, "worktree", "add", "-b", "linked", str(linked))

        self.assertEqual(repository.resolve(), resolve_source_checkout_root(repository))
        self.assertEqual(linked.resolve(), resolve_source_checkout_root(linked))
        self.assertEqual(repository.resolve(), resolve_shared_repository_root(repository))
        self.assertEqual(repository.resolve(), resolve_shared_repository_root(linked))
        self.assertEqual(work_models.CheckoutSelection.MAIN, classify_checkout(repository))
        self.assertEqual(work_models.CheckoutSelection.ISOLATED, classify_checkout(linked))
        repository_revision = self.run_git(repository, "rev-parse", "HEAD").strip()
        self.assertEqual(("main", repository_revision), observe_checkout_identity(repository))
        self.assertEqual(("linked", repository_revision), observe_checkout_identity(linked))
        self.run_git(repository, "switch", "-c", "primary-feature")
        self.assertEqual(work_models.CheckoutSelection.MAIN, classify_checkout(repository))

        with chdir(linked):
            result, stdout, stderr = self.run_cli("root")
        self.assertEqual((0, ""), (result, stderr))
        self.assertEqual(
            {
                "source_checkout_root": str(linked.resolve()),
                "shared_repository_root": str(repository.resolve()),
                "work_root": str(repository.resolve() / ".pinboard"),
            },
            json.loads(stdout),
        )

        original_exclude = (repository / ".git" / "info" / "exclude").read_bytes()
        with chdir(linked):
            self.assertEqual(0, main(("init",)))
        exclude = repository / ".git" / "info" / "exclude"
        exclude_mtime = exclude.stat().st_mtime_ns
        self.assertEqual(0, main(("--project-root", str(linked), "init")))
        self.assertTrue((repository / ".pinboard" / "state.sqlite3").is_file())
        self.assertFalse((linked / ".pinboard").exists())
        self.assertEqual(
            original_exclude + b"/.pinboard/\n",
            exclude.read_bytes(),
        )
        self.assertEqual(exclude_mtime, exclude.stat().st_mtime_ns)
        (linked / ".codex").mkdir()
        (linked / ".codex" / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
        self.assertEqual("?? .codex/config.toml\n", self.run_git(linked, "status", "--short", "--untracked-files=all"))

    def test_rejects_non_git_directory(self) -> None:
        directory = Path(tempfile.mkdtemp())

        with self.assertRaisesRegex(RootError, "PROJECT_GIT_ROOT_UNAVAILABLE"):
            resolve_source_checkout_root(directory)
        with self.assertRaisesRegex(RootError, "PROJECT_GIT_ROOT_UNAVAILABLE"):
            resolve_shared_repository_root(directory)
        with self.assertRaisesRegex(RootError, "PROJECT_GIT_CHECKOUT_UNAVAILABLE"):
            observe_checkout_identity(directory)

    def test_current_head_candidate_reads_the_exact_base_diff(self) -> None:
        repository = Path(tempfile.mkdtemp()).resolve()
        self.run_git(repository, "init", "-b", "main")
        tracked = repository / "tracked.txt"
        tracked.write_text("base\n", encoding="utf-8")
        self.run_git(repository, "add", "tracked.txt")
        self.run_git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base")
        base_revision = self.run_git(repository, "rev-parse", "HEAD").strip()
        tracked.write_text("candidate\n", encoding="utf-8")
        self.run_git(repository, "add", "tracked.txt")
        self.run_git(
            repository, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "candidate"
        )
        candidate_revision = self.run_git(repository, "rev-parse", "HEAD").strip()
        index = repository / ".git" / "index"
        original_index = index.read_bytes()
        os.utime(tracked, (1, 1))
        observed = read_current_head_candidate(
            repository, candidate_revision, base_revision, excluded_untracked_paths=()
        )

        self.assertIsInstance(observed, CurrentHeadCandidate)
        assert isinstance(observed, CurrentHeadCandidate)
        self.assertEqual(candidate_revision, observed.identity)
        self.assertIn(b"-base\n+candidate", observed.diff)
        self.assertEqual(original_index, index.read_bytes())

    def test_returning_initialization_reads_an_existing_exclusion_without_write_access(self) -> None:
        repository = Path(tempfile.mkdtemp()).resolve()
        self.run_git(repository, "init", "-b", "main")
        exclude = repository / ".git" / "info" / "exclude"
        exclude.write_bytes(b"/.pinboard/\n")
        exclude.chmod(0o400)
        try:
            self.assertIsNone(ensure_default_git_exclude(repository))
        finally:
            exclude.chmod(0o600)

    def test_concurrent_initialization_appends_the_shared_exclusion_once(self) -> None:
        temporary = Path(tempfile.mkdtemp())
        repository = (temporary / "repository").resolve()
        linked = (temporary / "linked").resolve()
        repository.mkdir()
        self.run_git(repository, "init", "-b", "main")
        (repository / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self.run_git(repository, "add", "tracked.txt")
        self.run_git(
            repository,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial",
        )
        self.run_git(repository, "worktree", "add", "-b", "linked", str(linked))
        exclude = repository / ".git" / "info" / "exclude"
        original_open = Path.open
        readers_ready = Barrier(2)

        def synchronized_open(path: Path, mode: str = "r") -> io.BufferedIOBase:
            stream = original_open(path, mode)
            if not isinstance(stream, io.BufferedIOBase):
                raise AssertionError("Expected the Git exclude to be opened in binary mode.")
            if path == exclude and mode in {"rb", "a+b"}:
                readers_ready.wait(timeout=5)
            return stream

        with (
            patch.object(Path, "open", synchronized_open),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            results = tuple(executor.map(ensure_default_git_exclude, (repository, linked)))

        self.assertEqual(1, results.count(exclude))
        self.assertEqual(1, results.count(None))
        self.assertEqual(1, exclude.read_text(encoding="utf-8").splitlines().count("/.pinboard/"))

    def test_store_free_routes_do_not_validate_an_unused_external_work_root(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        source = project / "source.txt"
        source.write_text("selected authority\n", encoding="utf-8")
        unused_work_root = project / "missing-parent" / "work"

        root_result, root_stdout, root_stderr = self.run_cli(
            "--project-root", str(project), "--work-root", str(unused_work_root), "root"
        )
        self.assertEqual(0, root_result, root_stderr)
        self.assertEqual(str(unused_work_root), json.loads(root_stdout)["work_root"])

        self.run_git(project, "init", "--quiet")
        source_result = call_native_tool(
            server.BRIEF_SOURCES_TOOL,
            {
                "request": {
                    "project_root": str(project),
                    "work_root": str(unused_work_root),
                    "operation": "plan",
                    "max_batch_bytes": 24_000,
                    "manifest": {
                        "schema": "pinboard-brief-sources/v1",
                        "sources": [{"authority_id": "source", "selector": "source.txt", "families": ["contract"]}],
                    },
                }
            },
        )
        self.assertEqual("pinboard-brief-source-plan/v1", source_result["schema"])
        self.assertFalse(unused_work_root.exists())


class TargetPresenceTest(unittest.TestCase):
    """Reverse-check a reviewed diff against a named local target without writing Git state."""

    DATES: ClassVar[dict[str, str]] = {
        "GIT_AUTHOR_DATE": "2030-01-05T00:00:00Z",
        "GIT_COMMITTER_DATE": "2030-01-05T00:00:00Z",
    }
    BASE_LINES: ClassVar[tuple[str, ...]] = tuple(f"line {number}\n" for number in range(1, 11))

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repository = Path(directory.name) / "repository"
        self.repository.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Pinboard Tests")
        self.git("config", "user.email", "pinboard@example.invalid")
        self.base = self.commit("".join(self.BASE_LINES), "base")
        self.candidate = self.commit("".join(("changed\n", *self.BASE_LINES[1:])), "candidate")
        self.diff = self.git_bytes("diff", "--binary", self.base, self.candidate, "--")

    def git(self, *arguments: str) -> str:
        return self.git_bytes(*arguments).decode().strip()

    def git_bytes(self, *arguments: str) -> bytes:
        return subprocess.run(
            ["git", *arguments], cwd=self.repository, check=True, capture_output=True, env={**os.environ}
        ).stdout

    def commit(self, content: str, message: str, parent: str | None = None) -> str:
        """Commit content in the checkout, or over a parent through a temporary index with fixed dates."""

        if parent is None:
            (self.repository / "tracked.txt").write_text(content, encoding="utf-8")
            self.git("add", "tracked.txt")
            subprocess.run(
                ["git", "commit", "-q", "-m", message],
                cwd=self.repository,
                check=True,
                capture_output=True,
                env={**os.environ, **self.DATES},
            )
            return self.git("rev-parse", "HEAD")
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, **self.DATES, "GIT_INDEX_FILE": str(Path(directory) / "index")}
            subprocess.run(["git", "read-tree", parent], cwd=self.repository, env=environment, check=True)
            blob = (
                subprocess.run(
                    ["git", "hash-object", "-w", "--stdin"],
                    cwd=self.repository,
                    input=content.encode(),
                    check=True,
                    capture_output=True,
                )
                .stdout.decode()
                .strip()
            )
            subprocess.run(
                ["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},tracked.txt"],
                cwd=self.repository,
                env=environment,
                check=True,
            )
            tree = subprocess.run(
                ["git", "write-tree"], cwd=self.repository, env=environment, check=True, capture_output=True, text=True
            ).stdout.strip()
        return subprocess.run(
            ["git", "commit-tree", tree, "-p", parent, "-m", message],
            cwd=self.repository,
            env={**os.environ, **self.DATES},
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_reports_presence_by_reverse_applying_the_reviewed_diff(self) -> None:
        self.assertEqual(
            ResolvedTargetPresence(self.candidate, present=True),
            observe_target_presence(self.repository, self.candidate, self.diff),
        )
        self.assertEqual(
            ResolvedTargetPresence(self.base, present=False),
            observe_target_presence(self.repository, self.base, self.diff),
        )

    def test_later_non_overlapping_edit_keeps_presence_and_overlapping_edit_removes_it(self) -> None:
        non_overlapping = self.commit(
            "".join(("changed\n", *self.BASE_LINES[1:-1], "line ten\n")), "Edit a distant line.", self.candidate
        )
        overlapping = self.commit("".join(("other\n", *self.BASE_LINES[1:])), "Edit the reviewed line.", self.candidate)
        self.assertEqual(
            ResolvedTargetPresence(non_overlapping, present=True),
            observe_target_presence(self.repository, non_overlapping, self.diff),
        )
        self.assertEqual(
            ResolvedTargetPresence(overlapping, present=False),
            observe_target_presence(self.repository, overlapping, self.diff),
        )

    def test_empty_diff_reports_no_change_without_applying_anything(self) -> None:
        self.assertEqual(
            ResolvedTargetWithoutChange(self.candidate), observe_target_presence(self.repository, "HEAD", b"")
        )

    def test_unresolved_target_and_non_git_directory_are_distinct_observations(self) -> None:
        self.assertEqual(
            UnresolvedTargetObservation("no-such-ref"),
            observe_target_presence(self.repository, "no-such-ref", self.diff),
        )
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(RootError) as raised:
            observe_target_presence(Path(directory), "HEAD", self.diff)
        self.assertEqual(RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE, raised.exception.code)

    def test_repository_whitespace_and_split_index_configuration_do_not_change_the_verdict(self) -> None:
        self.git("config", "apply.whitespace", "error")
        self.git("config", "core.splitIndex", "true")
        trailing = self.commit("trailing \n", "Add trailing whitespace.", self.base)
        trailing_diff = self.git_bytes("diff", "--binary", self.base, trailing, "--")
        self.assertEqual(
            ResolvedTargetPresence(trailing, present=True),
            observe_target_presence(self.repository, trailing, trailing_diff),
        )

    def test_repository_ignore_whitespace_configuration_does_not_change_the_verdict(self) -> None:
        self.git("config", "apply.ignoreWhitespace", "change")
        indented = self.commit(
            "".join(("  changed\n", *self.BASE_LINES[1:])), "Indent the reviewed line.", self.candidate
        )
        self.assertEqual(
            ResolvedTargetPresence(indented, present=False),
            observe_target_presence(self.repository, indented, self.diff),
        )

    def test_read_only_git_directory_is_not_written_and_leaves_no_temporary_directory(self) -> None:
        git_directory = self.repository / ".git"
        before = self.git_state(git_directory)
        temporary_before = set(Path(tempfile.gettempdir()).glob("pinboard-integration-*"))
        for path in (git_directory, *git_directory.rglob("*")):
            if path.is_dir():
                path.chmod(0o555)
        self.addCleanup(self.restore_writable, git_directory)
        result = observe_target_presence(self.repository, self.candidate, self.diff)
        self.assertEqual(ResolvedTargetPresence(self.candidate, present=True), result)
        self.assertEqual(before, self.git_state(git_directory))
        self.assertEqual(temporary_before, set(Path(tempfile.gettempdir()).glob("pinboard-integration-*")))

    def test_renamed_file_and_added_binary_are_checked_by_content(self) -> None:
        self.git("mv", "tracked.txt", "renamed.txt")
        (self.repository / "blob.bin").write_bytes(b"\x00\x01binary\x02")
        self.git("add", "--all")
        subprocess.run(
            ["git", "commit", "-q", "-m", "Rename the reviewed file and add a binary."],
            cwd=self.repository,
            check=True,
            capture_output=True,
            env={**os.environ, **self.DATES},
        )
        renamed = self.git("rev-parse", "HEAD")
        diff = self.git_bytes("diff", "--binary", self.base, renamed, "--")
        self.assertEqual(
            ResolvedTargetPresence(renamed, present=True), observe_target_presence(self.repository, renamed, diff)
        )
        self.assertEqual(
            ResolvedTargetPresence(self.base, present=False), observe_target_presence(self.repository, self.base, diff)
        )

    def git_state(self, git_directory: Path) -> tuple[tuple[str, int, bytes], ...]:
        entries = [
            (str(path.relative_to(git_directory)), path.stat().st_size, path.read_bytes())
            for path in sorted(git_directory.rglob("*"))
            if path.is_file()
        ]
        return tuple(entries)

    def restore_writable(self, git_directory: Path) -> None:
        for path in (git_directory, *git_directory.rglob("*")):
            if path.is_dir():
                path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
