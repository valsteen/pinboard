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
    CurrentHeadCandidate,
    IntegrationTargetObserved,
    IntegrationTargetUnresolved,
    classify_checkout,
    ensure_default_git_exclude,
    observe_checkout_identity,
    read_current_head_candidate,
    read_integration_target,
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

    def test_integration_target_reverse_applies_without_writing_git_metadata(self) -> None:
        repository = Path(tempfile.mkdtemp()).resolve()
        self.run_git(repository, "init", "-b", "main")
        tracked = repository / "tracked.txt"
        tracked.write_text("base\n", encoding="utf-8")
        self.run_git(repository, "add", "tracked.txt")
        self.run_git(
            repository,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "author.date=2000-01-01T00:00:00+00:00",
            "-c",
            "committer.date=2000-01-01T00:00:00+00:00",
            "commit",
            "-m",
            "base",
        )
        base = self.run_git(repository, "rev-parse", "HEAD").strip()
        tracked.write_text("candidate \n", encoding="utf-8")
        self.run_git(repository, "add", "tracked.txt")
        self.run_git(
            repository,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "author.date=2000-01-02T00:00:00+00:00",
            "-c",
            "committer.date=2000-01-02T00:00:00+00:00",
            "commit",
            "-m",
            "candidate",
        )
        candidate = self.run_git(repository, "rev-parse", "HEAD").strip()
        diff = subprocess.run(
            ["git", "diff", "--binary", base, candidate], cwd=repository, check=True, capture_output=True
        ).stdout
        self.run_git(repository, "-c", "apply.whitespace=error", "config", "apply.whitespace", "error")
        git_directory = repository / ".git"
        before = {
            path.relative_to(git_directory): (path.read_bytes() if path.is_file() else None, path.stat().st_mode)
            for path in git_directory.rglob("*")
        }
        index = (git_directory / "index").read_bytes()
        head = (git_directory / "HEAD").read_bytes()
        worktree = tracked.read_bytes()
        temporary_directories: list[Path] = []
        create_temporary_directory = tempfile.TemporaryDirectory

        def record_temporary_directory(*, prefix: str) -> tempfile.TemporaryDirectory[str]:
            temporary_directory = create_temporary_directory(prefix=prefix)
            temporary_directories.append(Path(temporary_directory.name))
            return temporary_directory

        with patch("pinboard.adapters.files.root.tempfile.TemporaryDirectory", side_effect=record_temporary_directory):
            observed = read_integration_target(repository, "main", diff)

        self.assertIsInstance(observed, IntegrationTargetObserved)
        assert isinstance(observed, IntegrationTargetObserved)
        self.assertEqual(candidate, observed.target_revision)
        self.assertTrue(observed.content_present)
        self.assertEqual(index, (git_directory / "index").read_bytes())
        self.assertEqual(head, (git_directory / "HEAD").read_bytes())
        self.assertEqual(worktree, tracked.read_bytes())
        after = {
            path.relative_to(git_directory): (path.read_bytes() if path.is_file() else None, path.stat().st_mode)
            for path in git_directory.rglob("*")
        }
        self.assertEqual(before, after)
        self.assertEqual(1, len(temporary_directories))
        self.assertFalse(temporary_directories[0].exists())
        self.run_git(repository, "update-ref", "refs/heads/base-target", base)
        absent = read_integration_target(repository, "base-target", diff)
        self.assertIsInstance(absent, IntegrationTargetObserved)
        assert isinstance(absent, IntegrationTargetObserved)
        self.assertEqual(base, absent.target_revision)
        self.assertFalse(absent.content_present)
        self.assertEqual(IntegrationTargetUnresolved("missing"), read_integration_target(repository, "missing", diff))

    def test_integration_target_reads_with_a_read_only_git_directory(self) -> None:
        repository = Path(tempfile.mkdtemp()).resolve()
        self.run_git(repository, "init", "-b", "main")
        tracked = repository / "tracked.txt"
        tracked.write_text("value\n", encoding="utf-8")
        self.run_git(repository, "add", "tracked.txt")
        self.run_git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base")
        git_directory = repository / ".git"
        original_modes = {path: path.stat().st_mode for path in (git_directory, *git_directory.rglob("*"))}
        files_before = {path: path.read_bytes() for path in git_directory.rglob("*") if path.is_file()}
        head_before = (git_directory / "HEAD").read_bytes()
        index_before = (git_directory / "index").read_bytes()
        worktree_before = tracked.read_bytes()
        object_count_before = self.run_git(repository, "count-objects", "-v")
        temporary_directories: list[Path] = []
        create_temporary_directory = tempfile.TemporaryDirectory

        def record_temporary_directory(*, prefix: str) -> tempfile.TemporaryDirectory[str]:
            temporary_directory = create_temporary_directory(prefix=prefix)
            temporary_directories.append(Path(temporary_directory.name))
            return temporary_directory

        try:
            for path in original_modes:
                path.chmod(0o555 if path.is_dir() else 0o444)
            patch_bytes = (
                b"diff --git a/tracked.txt b/tracked.txt\n--- a/tracked.txt\n+++ b/tracked.txt\n"
                b"@@ -1 +1 @@\n-old\n+candidate\n"
            )
            with patch(
                "pinboard.adapters.files.root.tempfile.TemporaryDirectory", side_effect=record_temporary_directory
            ):
                observed = read_integration_target(repository, "main", patch_bytes)
            self.assertIsInstance(observed, IntegrationTargetObserved)
            self.assertFalse(observed.content_present)
            self.assertEqual(
                files_before, {path: path.read_bytes() for path in git_directory.rglob("*") if path.is_file()}
            )
            self.assertEqual(head_before, (git_directory / "HEAD").read_bytes())
            self.assertEqual(index_before, (git_directory / "index").read_bytes())
            self.assertEqual(worktree_before, tracked.read_bytes())
            self.assertEqual(object_count_before, self.run_git(repository, "count-objects", "-v"))
            self.assertEqual(1, len(temporary_directories))
            self.assertFalse(temporary_directories[0].exists())
        finally:
            for path, mode in original_modes.items():
                path.chmod(mode)

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
