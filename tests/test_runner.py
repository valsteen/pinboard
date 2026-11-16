import contextlib
import io
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tests import runner


class RunnerTest(unittest.TestCase):
    def test_success_failure_and_invalid_worker_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            test_root = Path(temporary_directory) / "tests"
            test_root.mkdir()
            (test_root / "__init__.py").touch()
            (test_root / "test_pass.py").write_text(
                "import unittest\n\nclass PassingTest(unittest.TestCase):\n    def test_passes(self):\n        pass\n",
                encoding="utf-8",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(0, runner.run(test_root, jobs=2, coverage=False, modules=None))
            self.assertIn("[PASS] test_pass.py", output.getvalue())

            (test_root / "test_fail.py").write_text(
                "import unittest\n\nclass FailingTest(unittest.TestCase):\n"
                "    def test_fails(self):\n        self.assertEqual(1, 2)\n",
                encoding="utf-8",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(1, runner.run(test_root, jobs=2, coverage=False, modules=None))
            captured = output.getvalue()
            self.assertLess(captured.index("[FAIL] test_fail.py"), captured.index("[PASS] test_pass.py"))
            self.assertIn("AssertionError: 1 != 2", captured)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(0, runner.run(test_root, jobs=2, coverage=False, modules=frozenset({"test_pass"})))
            self.assertIn("[PASS] test_pass.py", output.getvalue())
            self.assertNotIn("test_fail.py", output.getvalue())

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    1, runner.run(test_root, jobs=2, coverage=False, modules=frozenset({"test_pass", "test_renamed"}))
                )
            self.assertIn("missing", output.getvalue())
            self.assertIn("test_renamed", output.getvalue())
            self.assertNotIn("[PASS]", output.getvalue())

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as invalid_jobs:
            runner.main(["--jobs", "0"])
        self.assertEqual(2, invalid_jobs.exception.code)

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as partial_coverage:
            runner.main(["--platform-contract", "--coverage"])
        self.assertEqual(2, partial_coverage.exception.code)

    def test_large_module_splits_into_isolated_subprocesses_and_preserves_file_reporting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            test_root = root / "tests"
            test_root.mkdir()
            (test_root / "__init__.py").touch()
            pid_dir = root / "pids"
            pid_dir.mkdir()
            method_count = runner.MAX_METHODS_PER_TARGET + 2
            methods = "\n".join(
                f"    def test_{index}(self) -> None:\n"
                f"        (Path({str(pid_dir)!r}) / f'test_{index}_{{os.getpid()}}').write_text(str(Path.cwd()), encoding='utf-8')\n"
                for index in range(method_count)
            )
            (test_root / "test_large.py").write_text(
                f"import os\nfrom pathlib import Path\nimport unittest\n\nclass LargeTest(unittest.TestCase):\n{methods}\n",
                encoding="utf-8",
            )
            (test_root / "test_small.py").write_text(
                "import unittest\n\nclass SmallTest(unittest.TestCase):\n    def test_ok(self) -> None:\n        pass\n",
                encoding="utf-8",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(0, runner.run(test_root, jobs=2, coverage=False, modules=None))
            captured = output.getvalue()
            self.assertEqual(["[PASS] test_large.py", "[PASS] test_small.py"], captured.strip().splitlines())
            recorded_files = tuple(pid_dir.iterdir())
            self.assertEqual(method_count, len(recorded_files))
            self.assertGreaterEqual(len({path.name.rsplit("_", 1)[-1] for path in recorded_files}), 2)
            self.assertEqual({str(root.resolve())}, {path.read_text(encoding="utf-8") for path in recorded_files})

            (test_root / "test_large.py").write_text(
                f"import os\nfrom pathlib import Path\nimport unittest\n\nclass LargeTest(unittest.TestCase):\n{methods}\n"
                "    def test_late_failure(self) -> None:\n"
                "        self.fail('split shard failed')\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(1, runner.run(test_root, jobs=2, coverage=False, modules=None))
            failed_output = output.getvalue()
            self.assertEqual(1, failed_output.count("[FAIL] test_large.py"))
            self.assertIn("split shard failed", failed_output)
            self.assertLess(failed_output.index("[FAIL] test_large.py"), failed_output.index("[PASS] test_small.py"))

    def test_platform_contract_selection_names_existing_modules(self) -> None:
        def passing_target(path: Path, _targets: tuple[str, ...], _coverage: bool) -> runner.TestResult:
            return path, subprocess.CompletedProcess([], 0, "", "")

        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            unittest.mock.patch.object(runner, "_run_target", side_effect=passing_target) as run_target,
        ):
            self.assertEqual(
                0, runner.run(runner.TEST_ROOT, jobs=2, coverage=False, modules=runner.PLATFORM_CONTRACT_MODULES)
            )
        self.assertEqual(runner.PLATFORM_CONTRACT_MODULES, {call.args[0].stem for call in run_target.call_args_list})
        self.assertGreater(len(run_target.call_args_list), len(runner.PLATFORM_CONTRACT_MODULES))
        self.assertIn(
            "test_contributor_traces.py",
            {call.args[0].name for call in run_target.call_args_list if call.args[1]},
        )


if __name__ == "__main__":
    unittest.main()
