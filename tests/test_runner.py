import contextlib
import io
import tempfile
import unittest
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
                self.assertEqual(0, runner.run(test_root, jobs=2, coverage=False))
            self.assertIn("[PASS] test_pass.py", output.getvalue())

            (test_root / "test_fail.py").write_text(
                "import unittest\n\nclass FailingTest(unittest.TestCase):\n"
                "    def test_fails(self):\n        self.assertEqual(1, 2)\n",
                encoding="utf-8",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(1, runner.run(test_root, jobs=2, coverage=False))
            captured = output.getvalue()
            self.assertLess(captured.index("[FAIL] test_fail.py"), captured.index("[PASS] test_pass.py"))
            self.assertIn("AssertionError: 1 != 2", captured)

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as invalid_jobs:
            runner.main(["--jobs", "0"])
        self.assertEqual(2, invalid_jobs.exception.code)


if __name__ == "__main__":
    unittest.main()
