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

    def test_platform_contract_selection_names_existing_modules(self) -> None:
        def passing_file(path: Path, _coverage: bool) -> runner.TestResult:
            return path, subprocess.CompletedProcess([], 0, "", "")

        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            unittest.mock.patch.object(runner, "_run_file", side_effect=passing_file) as run_file,
        ):
            self.assertEqual(
                0, runner.run(runner.TEST_ROOT, jobs=2, coverage=False, modules=runner.PLATFORM_CONTRACT_MODULES)
            )
        self.assertEqual(runner.PLATFORM_CONTRACT_MODULES, {call.args[0].stem for call in run_file.call_args_list})


if __name__ == "__main__":
    unittest.main()
