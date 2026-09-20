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

    def test_preparation_repairs_an_incomplete_checkout_environment_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            script = project / "scripts" / "prepare-worktree"
            script.parent.mkdir()
            script.write_bytes(PREPARE_WORKTREE.read_bytes())
            script.chmod(0o755)
            binary_directory = project / "bin"
            trace_directory = project / "trace"
            binary_directory.mkdir()
            trace_directory.mkdir()
            uv = binary_directory / "uv"
            uv.write_text(
                "#!/bin/sh\n"
                'printf \'%s\\n\' "$*" >> "$TRACE_DIR/uv"\n'
                'if [ "$*" = "sync --locked --reinstall" ]; then\n'
                "  mkdir -p .venv\n"
                "  : > .venv/pyvenv.cfg\n"
                "fi\n",
                encoding="utf-8",
            )
            uv.chmod(0o755)
            self.write_fake(binary_directory, "npm")

            result = subprocess.run(
                [script],
                cwd=project,
                env={
                    **os.environ,
                    "PATH": f"{binary_directory}:/usr/bin:/bin",
                    "TRACE_DIR": str(trace_directory),
                },
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual((0, "", ""), (result.returncode, result.stdout, result.stderr))
            self.assertEqual(
                ["sync --locked", "sync --locked --reinstall"],
                (trace_directory / "uv").read_text(encoding="utf-8").splitlines(),
            )
            self.assertTrue((project / ".venv" / "pyvenv.cfg").is_file())


if __name__ == "__main__":
    unittest.main()
