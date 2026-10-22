import contextlib
import io
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import TextIO, override
from unittest.mock import patch

from scripts import verify


class VerificationTest(unittest.TestCase):
    def test_entry_selects_focused_and_full_groups_and_preserves_test_workers(self) -> None:
        with patch.object(verify, "run", return_value=0) as run:
            self.assertEqual(0, verify.main(["verify-types"]))
            self.assertEqual((verify.Group.TYPES,), run.call_args.args[0])
            self.assertTrue(run.call_args.args[2])
            self.assertEqual(0, verify.main([]))
            self.assertEqual(tuple(verify.Group), run.call_args.args[0])
            self.assertFalse(run.call_args.args[2])
            self.assertEqual(0, verify.main(["verify-all", "--sequential", "--jobs", "3"]))
            self.assertTrue(run.call_args.args[2])
            self.assertEqual(3, run.call_args.args[3])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as invalid_jobs:
            verify.main(["verify-tests", "--jobs", "0"])
        self.assertEqual(2, invalid_jobs.exception.code)

    def test_parallel_groups_finish_independently_with_complete_separate_logs(self) -> None:
        barrier = threading.Barrier(len(verify.Group))

        def check(group: verify.Group, root: Path, log: TextIO, stream: bool, jobs: int | None) -> int:
            self.assertFalse(stream)
            self.assertIsNone(jobs)
            self.assertEqual(root, Path.cwd())
            log.write(f"begin {group.value}\n")
            barrier.wait(timeout=10)
            log.write(f"end {group.value}\n")
            return 7 if group == verify.Group.REPO else 0

        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with (
                patch.object(verify.tempfile, "mkdtemp", return_value=directory),
                patch.object(verify, "verify_group", side_effect=check),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(1, verify.run(tuple(verify.Group), Path.cwd(), False, None))
            for group in verify.Group:
                path = Path(directory) / f"{group.value}.log"
                self.assertEqual(f"begin {group.value}\nend {group.value}\n", path.read_text())
                self.assertIn(str(path), output.getvalue())
            self.assertIn("[FAIL] verify-repo (exit 7)", output.getvalue())
            for group in (verify.Group.TYPES, verify.Group.TESTS, verify.Group.PACKAGE):
                self.assertIn(f"[PASS] {group.value}", output.getvalue())
            self.assertIn("begin verify-repo\nend verify-repo\n", output.getvalue())

    def test_sequential_execution_streams_and_continues_after_group_failure(self) -> None:
        observed: list[verify.Group] = []
        output = io.StringIO()

        def check(group: verify.Group, root: Path, log: TextIO, stream: bool, jobs: int | None) -> int:
            self.assertTrue(stream)
            self.assertEqual(2, jobs)
            observed.append(group)
            return verify.run_command(
                (sys.executable, "-c", f"print('{group.value} output'); raise SystemExit(4)"), root, log, stream
            )

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(verify.tempfile, "mkdtemp", return_value=directory),
                patch.object(verify, "verify_group", side_effect=check),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(1, verify.run((verify.Group.REPO, verify.Group.TYPES), Path.cwd(), True, 2))
            self.assertEqual([verify.Group.REPO, verify.Group.TYPES], observed)
            captured = output.getvalue()
            self.assertLess(captured.index("verify-repo output\n"), captured.index("verify-types output\n"))
            self.assertLess(captured.index("verify-types output\n"), captured.index("Verification results:"))
            self.assertIn("verify-types output\n", (Path(directory) / "verify-types.log").read_text())

    def test_launch_and_log_failures_are_failures_with_resource_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "launch.log"
            output = io.StringIO()
            with patch.object(verify, "verify_group", side_effect=FileNotFoundError("missing-check-tool")):
                result = verify.run_group(verify.Group.REPO, root, log_path, False, None)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("missing-check-tool", result.diagnostic)
            result = verify.run_group(verify.Group.REPO, root, root, False, None)
            self.assertNotEqual(0, result.returncode)
            self.assertIn(str(root), result.diagnostic)
            with (
                patch.object(verify.tempfile, "mkdtemp", side_effect=PermissionError("temporary directory denied")),
                contextlib.redirect_stderr(output),
            ):
                self.assertEqual(1, verify.run(tuple(verify.Group), root, False, None))
            self.assertIn("temporary directory denied", output.getvalue())

    def test_process_output_and_failures_survive_logging_and_streaming(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = io.StringIO()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = verify.run_command(
                    (
                        sys.executable,
                        "-c",
                        "import sys; print('stdout'); print('stderr', file=sys.stderr); sys.exit(9)",
                    ),
                    Path(directory),
                    log,
                    True,
                )
            self.assertEqual(9, status)
            self.assertIn("stdout\n", log.getvalue())
            self.assertIn("stderr\n", log.getvalue())
            self.assertEqual(log.getvalue(), output.getvalue())
            with self.assertRaises(FileNotFoundError):
                verify.run_command((str(Path(directory) / "missing-tool"),), Path(directory), log, False)

    def test_failed_build_prevents_package_smoke_against_existing_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dist").mkdir()
            (root / "dist/stale.whl").touch()
            with patch.object(verify, "run_command", return_value=5) as command:
                self.assertEqual(5, verify.verify_group(verify.Group.PACKAGE, root, io.StringIO(), False, None))
                command.assert_called_once()
            with patch.object(verify, "run_command", side_effect=(6, 0)) as command:
                self.assertEqual(6, verify.run_commands((("first",), ("dependent",)), root, io.StringIO(), False))
                command.assert_called_once()

    def test_package_smokes_current_build_with_older_dist_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dist").mkdir()
            stale_wheel = root / "dist/pinboard-0.0.0-py3-none-any.whl"
            stale_wheel.touch()
            current_wheel: Path | None = None
            smoke_count = 0

            def command(arguments: list[str] | tuple[str, ...], _cwd: Path, _log: TextIO, _stream: bool) -> int:
                nonlocal current_wheel, smoke_count
                if arguments[:2] == ("uv", "build"):
                    build_directory = (
                        Path(arguments[arguments.index("--out-dir") + 1]) if "--out-dir" in arguments else root / "dist"
                    )
                    build_directory.mkdir(exist_ok=True)
                    current_wheel = build_directory / "pinboard-0.1.0-py3-none-any.whl"
                    current_wheel.touch()
                elif "--with" in arguments:
                    self.assertIsNotNone(current_wheel)
                    self.assertEqual(str(current_wheel), arguments[arguments.index("--with") + 1])
                    self.assertNotIn(str(stale_wheel), arguments)
                    smoke_count += 1
                    if "init" in arguments:
                        repository = Path(arguments[arguments.index("--project-root") + 1])
                        (repository / ".pinboard").mkdir()
                        (repository / ".pinboard/state.sqlite3").touch()
                return 0

            with patch.object(verify, "run_command", side_effect=command):
                self.assertEqual(0, verify.verify_group(verify.Group.PACKAGE, root, io.StringIO(), False, None))
            self.assertEqual(4, smoke_count)
            self.assertTrue(stale_wheel.is_file())

    def test_log_write_failure_stops_child_and_fails_group(self) -> None:
        class BrokenLog(io.StringIO):
            @override
            def write(self, text: str) -> int:
                if text.startswith("$ "):
                    return super().write(text)
                raise OSError("log disk full")

        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.Popen(
                (sys.executable, "-c", "print('child output', flush=True)"),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            with (
                patch.object(verify.subprocess, "Popen", return_value=process),
                self.assertRaisesRegex(OSError, "log disk full"),
            ):
                verify.run_command(("check",), Path(directory), BrokenLog(), False)
            self.assertIsNotNone(process.returncode)


if __name__ == "__main__":
    unittest.main()
