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
    IntegrationContentObservation,
    IntegrationTargetUnresolved,
    classify_checkout,
    ensure_default_git_exclude,
    observe_checkout_identity,
    observe_integration_content,
    read_current_head_candidate,
    resolve_shared_repository_root,
    resolve_source_checkout_root,
)
from pinboard.cli.entrypoint import main
from pinboard.domain import work_models
from pinboard.mcp import server
from tests.native_support import call_native_tool


class RootResolutionTest(unittest.TestCase):
    def run_git(self, cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            text=True,
            capture_output=True,
            env=env,
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

    def test_integration_content_observation_reverse_applies_without_writing_git_metadata(self) -> None:  # noqa: PLR0915 - one end-to-end repository fixture proves the complete read-only contract
        repository = Path(tempfile.mkdtemp()).resolve()
        self.run_git(repository, "init", "-b", "main")
        tracked = repository / "tracked.txt"
        tracked.write_text("base\n", encoding="utf-8")
        self.run_git(repository, "add", "tracked.txt")
        fixed_date = "2001-02-03T04:05:06+00:00"
        git_env = {**os.environ, "GIT_AUTHOR_DATE": fixed_date, "GIT_COMMITTER_DATE": fixed_date}
        self.run_git(
            repository,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "base",
            env=git_env,
        )
        base = self.run_git(repository, "rev-parse", "HEAD").strip()
        tracked.write_text("candidate\n", encoding="utf-8")
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD"], cwd=repository, check=True, capture_output=True
        ).stdout
        absent = observe_integration_content(repository, base, diff)
        self.assertEqual(IntegrationContentObservation(base, False), absent)
        self.run_git(repository, "add", "tracked.txt")
        self.run_git(
            repository,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "integrated",
            env=git_env,
        )
        target = self.run_git(repository, "rev-parse", "HEAD").strip()
        self.run_git(repository, "config", "core.splitIndex", "true")
        self.run_git(repository, "config", "apply.whitespace", "error")
        whitespace = repository / "whitespace.txt"
        whitespace.write_text("base\n", encoding="utf-8")
        self.run_git(repository, "add", "whitespace.txt")
        self.run_git(
            repository,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "whitespace base",
            env=git_env,
        )
        whitespace_base = self.run_git(repository, "rev-parse", "HEAD").strip()
        whitespace.write_text("reviewed trailing space \n", encoding="utf-8")
        whitespace_diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD"], cwd=repository, check=True, capture_output=True
        ).stdout
        self.run_git(repository, "add", "whitespace.txt")
        self.run_git(
            repository,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "whitespace candidate",
            env=git_env,
        )
        target = self.run_git(repository, "rev-parse", "HEAD").strip()
        self.assertNotEqual(whitespace_base, target)
        self.assertEqual(
            IntegrationContentObservation(target, True),
            observe_integration_content(repository, target, whitespace_diff),
        )
        metadata = repository / ".git"
        before = {path.relative_to(metadata): path.read_bytes() for path in metadata.rglob("*") if path.is_file()}
        files = tuple(path for path in metadata.rglob("*") if path.is_file())
        directories = tuple(path for path in metadata.rglob("*") if path.is_dir())
        for path in files:
            path.chmod(0o444)
        for path in directories:
            path.chmod(0o555)
        metadata.chmod(0o555)
        temporary = Path(tempfile.gettempdir())
        temporary_before = {path for path in temporary.iterdir() if path.name.startswith("pinboard-integration-")}
        working_tree_before = {
            path.relative_to(repository): path.read_bytes()
            for path in repository.rglob("*")
            if path.is_file() and metadata not in path.parents
        }
        try:
            self.assertEqual(
                IntegrationContentObservation(target, True), observe_integration_content(repository, target, diff)
            )
        finally:
            metadata.chmod(0o755)
            for path in directories:
                path.chmod(0o755)
            for path in files:
                path.chmod(0o644)
        after = {path.relative_to(metadata): path.read_bytes() for path in metadata.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        working_tree_after = {
            path.relative_to(repository): path.read_bytes()
            for path in repository.rglob("*")
            if path.is_file() and metadata not in path.parents
        }
        self.assertEqual(working_tree_before, working_tree_after)
        self.assertEqual(
            temporary_before, {path for path in temporary.iterdir() if path.name.startswith("pinboard-integration-")}
        )
        self.assertEqual(
            IntegrationTargetUnresolved("missing-target"),
            observe_integration_content(repository, "missing-target", diff),
        )
        self.assertEqual(
            IntegrationContentObservation(target, False), observe_integration_content(repository, target, b"")
        )

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
