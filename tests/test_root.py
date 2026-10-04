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
from unittest.mock import patch

from pinboard.adapters.files.errors import RootError
from pinboard.adapters.files.root import (
    CandidateContentObservation,
    CandidateContentPresence,
    CurrentHeadCandidate,
    UnresolvedIntegrationTarget,
    classify_checkout,
    ensure_default_git_exclude,
    observe_checkout_identity,
    read_candidate_integration,
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

    def commit_fixed_date(self, cwd: Path, message: str) -> str:
        environment = os.environ.copy()
        environment["GIT_AUTHOR_DATE"] = "2001-02-03T04:05:06+00:00"
        environment["GIT_COMMITTER_DATE"] = "2001-02-03T04:05:06+00:00"
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", message],
            cwd=cwd,
            env=environment,
            check=True,
            capture_output=True,
        )
        return self.run_git(cwd, "rev-parse", "HEAD").strip()

    def test_candidate_integration_uses_only_a_private_index_and_leaves_git_read_only(self) -> None:  # noqa: PLR0915 - verify every protected Git surface and cleanup
        project = Path(tempfile.mkdtemp()).resolve()
        self.run_git(project, "init", "-b", "main")
        self.run_git(project, "config", "apply.whitespace", "error")
        tracked = project / "tracked.txt"
        tracked.write_text("base\n", encoding="utf-8")
        self.run_git(project, "add", "tracked.txt")
        base = self.commit_fixed_date(project, "base")
        tracked.write_text("candidate  \n", encoding="utf-8")
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--"], cwd=project, check=True, capture_output=True
        ).stdout
        self.run_git(project, "add", "tracked.txt")
        candidate = self.commit_fixed_date(project, "candidate")
        self.run_git(project, "branch", "base", base)
        self.run_git(project, "update-ref", "refs/remotes/origin/main", candidate)
        work_root = project / ".pinboard"
        work_root.mkdir()
        sentinel = work_root / "sentinel.txt"
        sentinel.write_text("work root stays unchanged\n", encoding="utf-8")
        git_directory = project / ".git"

        def contents(directory: Path) -> dict[str, bytes]:
            return {
                path.relative_to(directory).as_posix(): path.read_bytes()
                for path in directory.rglob("*")
                if path.is_file()
            }

        before_git = contents(git_directory)
        before_worktree = contents(project)
        before_status = self.run_git(project, "status", "--porcelain=v1", "--untracked-files=all")
        before_objects = self.run_git(project, "count-objects", "-v")
        index = git_directory / "index"
        real_index = index.read_bytes()
        temporary_directories: list[Path] = []
        original_temporary_directory = tempfile.TemporaryDirectory

        def track_temporary_directory(prefix: str | None = None) -> tempfile.TemporaryDirectory[str]:
            directory = original_temporary_directory(prefix=prefix)
            temporary_directories.append(Path(directory.name))
            return directory

        def path_depth(path: Path) -> int:
            return len(path.parts)

        readonly_paths: tuple[Path, ...] = tuple(git_directory.rglob("*"))
        readonly_paths = tuple(sorted(readonly_paths, key=path_depth, reverse=True))
        for path in readonly_paths:
            path.chmod(0o555 if path.is_dir() else 0o444)
        git_directory.chmod(0o555)
        try:
            with patch("pinboard.adapters.files.root.tempfile.TemporaryDirectory", track_temporary_directory):
                present = read_candidate_integration(project, "main", diff)
                self.assertIsInstance(present, CandidateContentObservation)
                assert isinstance(present, CandidateContentObservation)
                self.assertEqual(
                    (candidate, CandidateContentPresence.PRESENT), (present.target_revision, present.presence)
                )
                absent = read_candidate_integration(project, "base", diff)
                self.assertIsInstance(absent, CandidateContentObservation)
                assert isinstance(absent, CandidateContentObservation)
                self.assertEqual(
                    (base, CandidateContentPresence.NOT_PRESENT), (absent.target_revision, absent.presence)
                )
                remote_tracking = read_candidate_integration(project, "origin/main", diff)
                self.assertIsInstance(remote_tracking, CandidateContentObservation)
                assert isinstance(remote_tracking, CandidateContentObservation)
                self.assertEqual(
                    (candidate, CandidateContentPresence.PRESENT),
                    (remote_tracking.target_revision, remote_tracking.presence),
                )
                no_change = read_candidate_integration(project, "main", b"")
                self.assertIsInstance(no_change, CandidateContentObservation)
                assert isinstance(no_change, CandidateContentObservation)
                self.assertEqual(
                    (candidate, CandidateContentPresence.NO_CHANGE), (no_change.target_revision, no_change.presence)
                )
                unresolved = read_candidate_integration(project, "missing-target", diff)
                self.assertEqual(UnresolvedIntegrationTarget("missing-target"), unresolved)
        finally:
            git_directory.chmod(0o755)
            for path in readonly_paths:
                path.chmod(0o755 if path.is_dir() else 0o644)

        self.assertTrue(temporary_directories)
        self.assertTrue(all(not path.exists() for path in temporary_directories))
        self.assertEqual(before_git, contents(git_directory))
        self.assertEqual(before_worktree, contents(project))
        self.assertEqual(real_index, index.read_bytes())
        self.assertEqual(before_status, self.run_git(project, "status", "--porcelain=v1", "--untracked-files=all"))
        self.assertEqual(before_objects, self.run_git(project, "count-objects", "-v"))
        self.assertEqual("work root stays unchanged\n", sentinel.read_text(encoding="utf-8"))

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


if __name__ == "__main__":
    unittest.main()
