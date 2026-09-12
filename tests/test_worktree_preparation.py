import os
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
PREPARE_WORKTREE = PROJECT_ROOT / "scripts" / "prepare-worktree"


class WorktreePreparationTest(unittest.TestCase):
    def write_fake(self, directory: Path, name: str) -> None:
        executable = directory / name
        executable.write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$PWD" "$@" > "$TRACE_DIR/' + name + '"\n',
            encoding="utf-8",
        )
        executable.chmod(0o755)

    def test_preparation_runs_locked_package_managers_from_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            binary_directory = temporary / "bin"
            trace_directory = temporary / "trace"
            binary_directory.mkdir()
            trace_directory.mkdir()
            self.write_fake(binary_directory, "uv")
            self.write_fake(binary_directory, "npm")
            environment = {
                **os.environ,
                "PATH": f"{binary_directory}:/usr/bin:/bin",
                "TRACE_DIR": str(trace_directory),
            }

            result = subprocess.run(
                [PREPARE_WORKTREE],
                cwd=temporary,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual((0, "", ""), (result.returncode, result.stdout, result.stderr))
            self.assertEqual(
                [str(PROJECT_ROOT), "sync", "--locked"],
                (trace_directory / "uv").read_text(encoding="utf-8").splitlines(),
            )
            self.assertEqual(
                [str(PROJECT_ROOT), "ci", "--prefer-offline", "--no-audit", "--no-fund"],
                (trace_directory / "npm").read_text(encoding="utf-8").splitlines(),
            )

    def test_missing_prerequisite_fails_before_dependency_setup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            binary_directory = temporary / "bin"
            trace_directory = temporary / "trace"
            binary_directory.mkdir()
            trace_directory.mkdir()
            self.write_fake(binary_directory, "uv")
            environment = {
                **os.environ,
                "PATH": str(binary_directory),
                "TRACE_DIR": str(trace_directory),
            }

            result = subprocess.run(
                [PREPARE_WORKTREE],
                cwd=temporary,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual("prepare-worktree: missing prerequisite: npm\n", result.stderr)
            self.assertFalse((trace_directory / "uv").exists())


if __name__ == "__main__":
    unittest.main()
