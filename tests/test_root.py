import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import chdir
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from pinboard.adapters.files.errors import RootError, RootErrorCode
from pinboard.adapters.files.root import (
    CandidateContentNoChange,
    CandidateContentNotPresent,
    CandidateContentPresent,
    CurrentHeadCandidate,
    IntegrationTargetUnresolved,
    classify_checkout,
    ensure_default_git_exclude,
    observe_candidate_content,
    observe_checkout_identity,
    read_current_head_candidate,
    resolve_shared_repository_root,
    resolve_source_checkout_root,
)
from pinboard.cli.entrypoint import main
from pinboard.domain import work_models
from pinboard.mcp import server
from tests.native_support import call_native_tool


@dataclass(frozen=True, slots=True)
class IntegrationRootFixture:
    repository: Path
    private_temporary: Path
    base_revision: str
    candidate_revision: str
    diff: bytes
    index: Path
    tracked: Path


def _integration_root_fixture(temporary: Path) -> IntegrationRootFixture:
    repository = temporary / "repository"
    private_temporary = temporary / "system-temp"
    repository.mkdir()
    private_temporary.mkdir()

    def git(*arguments: str) -> bytes:
        return subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True).stdout

    def commit(path: Path, content: str, message: str) -> str:
        path.write_text(content, encoding="utf-8")
        git("add", "tracked.txt")
        environment = os.environ | {
            "GIT_AUTHOR_DATE": "2001-02-03T04:05:06+00:00",
            "GIT_COMMITTER_DATE": "2001-02-03T04:05:06+00:00",
        }
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", message],
            cwd=repository,
            env=environment,
            check=True,
            capture_output=True,
        )
        return git("rev-parse", "--verify", "HEAD").decode().strip()

    git("init", "-b", "main")
    tracked = repository / "tracked.txt"
    base_revision = commit(tracked, "base\n", "base")
    candidate_revision = commit(tracked, "candidate \n", "candidate with trailing whitespace")
    diff = git("diff", "--binary", base_revision, candidate_revision)
    git("update-ref", "refs/remotes/origin/main", candidate_revision)
    git("config", "apply.whitespace", "error")
    return IntegrationRootFixture(
        repository,
        private_temporary,
        base_revision,
        candidate_revision,
        diff,
        repository / ".git" / "index",
        tracked,
    )


def _git_file_snapshot(repository: Path) -> dict[Path, bytes]:
    git_directory = repository / ".git"
    return {path.relative_to(git_directory): path.read_bytes() for path in git_directory.rglob("*") if path.is_file()}


def _set_git_metadata_readonly(repository: Path, readonly: bool) -> None:
    git_directory = repository / ".git"
    for path in (git_directory, *(item for item in git_directory.rglob("*") if item.is_dir())):
        path.chmod(0o555 if readonly else 0o755)
    for path in (item for item in git_directory.rglob("*") if item.is_file()):
        path.chmod(0o444 if readonly else 0o644)


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

    def test_integration_content_read_uses_only_a_private_temporary_index(self) -> None:
        fixture = _integration_root_fixture(Path(tempfile.mkdtemp()).resolve())
        repository = fixture.repository
        index_before = fixture.index.read_bytes()
        tree_before = fixture.tracked.read_bytes()
        metadata_before = _git_file_snapshot(repository)
        work_root = repository / ".pinboard"

        with patch("tempfile.tempdir", str(fixture.private_temporary)):
            present = observe_candidate_content(repository, "origin/main", fixture.diff)
            absent = observe_candidate_content(repository, fixture.base_revision, fixture.diff)
            unresolved = observe_candidate_content(repository, "missing-target", fixture.diff)
        original_run = subprocess.run
        commands: list[tuple[str, ...]] = []

        def record_git_command(
            arguments: list[str], *, cwd: Path, text: bool, capture_output: bool, check: bool
        ) -> subprocess.CompletedProcess[str]:
            commands.append(tuple(arguments))
            return original_run(arguments, cwd=cwd, text=text, capture_output=capture_output, check=check)

        with (
            patch("tempfile.tempdir", str(fixture.private_temporary)),
            patch("pinboard.adapters.files.root.subprocess.run", side_effect=record_git_command),
        ):
            no_change = observe_candidate_content(repository, "main", b"")
        self.assertEqual(CandidateContentPresent(fixture.candidate_revision), present)
        self.assertEqual(CandidateContentNotPresent(fixture.base_revision), absent)
        self.assertEqual(CandidateContentNoChange(fixture.candidate_revision), no_change)
        self.assertFalse(any("read-tree" in command or "apply" in command for command in commands), commands)
        self.assertEqual(IntegrationTargetUnresolved("missing-target"), unresolved)
        self.assertEqual(index_before, fixture.index.read_bytes())
        self.assertEqual(tree_before, fixture.tracked.read_bytes())
        self.assertFalse(work_root.exists())
        self.assertEqual(metadata_before, _git_file_snapshot(repository))
        self.assertEqual((), tuple(fixture.private_temporary.iterdir()))

        _set_git_metadata_readonly(repository, True)
        try:
            with patch("tempfile.tempdir", str(fixture.private_temporary)):
                readonly_result = observe_candidate_content(repository, "main", fixture.diff)
        finally:
            _set_git_metadata_readonly(repository, False)
        self.assertEqual(CandidateContentPresent(fixture.candidate_revision), readonly_result)
        self.assertEqual(metadata_before, _git_file_snapshot(repository))
        self.assertEqual(index_before, fixture.index.read_bytes())
        self.assertEqual(tree_before, fixture.tracked.read_bytes())
        self.assertFalse(work_root.exists())
        self.assertEqual((), tuple(fixture.private_temporary.iterdir()))

    def test_integration_content_read_reports_a_non_git_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(RootError) as rejected:
            observe_candidate_content(Path(temporary), "HEAD", b"candidate patch")

        self.assertEqual(RootErrorCode.PROJECT_GIT_ROOT_UNAVAILABLE, rejected.exception.code)

    def test_integration_content_read_rejects_invalid_revision_characters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _integration_root_fixture(Path(temporary).resolve())
            with self.assertRaises(ValueError):
                observe_candidate_content(fixture.repository, "main\x00", fixture.diff)

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


if __name__ == "__main__":
    unittest.main()
