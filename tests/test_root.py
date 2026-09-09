import contextlib
import io
import json
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
    ensure_default_git_exclude,
    resolve_shared_repository_root,
    resolve_source_checkout_root,
)
from pinboard.interfaces.cli import main


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

        with chdir(linked):
            result, stdout, stderr = self.run_cli("root")
        self.assertEqual((0, ""), (result, stderr))
        self.assertEqual(
            {
                "source_checkout_root": str(linked.resolve()),
                "shared_repository_root": str(repository.resolve()),
                "work_root": str(repository.resolve() / ".codex" / "pinboard"),
            },
            json.loads(stdout),
        )

        original_exclude = (repository / ".git" / "info" / "exclude").read_bytes()
        with chdir(linked):
            self.assertEqual(0, main(("init",)))
        exclude = repository / ".git" / "info" / "exclude"
        exclude_mtime = exclude.stat().st_mtime_ns
        self.assertEqual(0, main(("--project-root", str(linked), "init")))
        self.assertTrue((repository / ".codex" / "pinboard" / "state.sqlite3").is_file())
        self.assertFalse((linked / ".codex" / "pinboard").exists())
        self.assertEqual(
            original_exclude + b"/.codex/pinboard/\n",
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

    def test_returning_initialization_reads_an_existing_exclusion_without_write_access(self) -> None:
        repository = Path(tempfile.mkdtemp()).resolve()
        self.run_git(repository, "init", "-b", "main")
        exclude = repository / ".git" / "info" / "exclude"
        exclude.write_bytes(b"/.codex/pinboard/\n")
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
        self.assertEqual(1, exclude.read_text(encoding="utf-8").splitlines().count("/.codex/pinboard/"))

    def test_store_free_routes_do_not_validate_an_unused_external_work_root(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        source = project / "source.txt"
        source.write_text("selected authority\n", encoding="utf-8")
        manifest = project / "sources.json"
        manifest.write_text(
            '{"schema":"pinboard-brief-sources/v1","sources":['
            '{"authority_id":"source","selector":"source.txt","families":["contract"]}]}\n',
            encoding="utf-8",
        )
        unused_work_root = project / "missing-parent" / "work"

        root_result, root_stdout, root_stderr = self.run_cli(
            "--project-root", str(project), "--work-root", str(unused_work_root), "root"
        )
        self.assertEqual(0, root_result, root_stderr)
        self.assertEqual(str(unused_work_root), json.loads(root_stdout)["work_root"])

        source_result, source_stdout, source_stderr = self.run_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(unused_work_root),
            "brief-sources",
            "--file",
            str(manifest),
            "--json",
        )
        self.assertEqual(0, source_result, source_stderr)
        self.assertIn('"schema": "pinboard-brief-source-plan/v1"', source_stdout)


if __name__ == "__main__":
    unittest.main()
