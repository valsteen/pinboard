"""Typed Git configuration observations at the process boundary."""

import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from pinboard.adapters.files import git_config


class GitConfigTest(unittest.TestCase):
    def test_reads_preserve_missing_duplicates_and_invalid_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config"
            self.assertIsInstance(git_config.get_all(path, "mcp.mode", as_bool=False), git_config.Missing)
            path.touch()
            self.assertEqual(git_config.Entries(path, ()), git_config.list_entries(path))
            self.assertEqual(git_config.WriteAcknowledged(path, "mcp.mode"), git_config.add(path, "mcp.mode", "off"))
            self.assertEqual(git_config.WriteAcknowledged(path, "mcp.mode"), git_config.add(path, "mcp.mode", "on"))
            self.assertEqual(
                git_config.Values(path, "mcp.mode", ("off", "on")), git_config.get_all(path, "mcp.mode", as_bool=False)
            )
            self.assertEqual(
                git_config.Values(path, "mcp.mode", (False, True)), git_config.get_all(path, "mcp.mode", as_bool=True)
            )
            self.assertEqual(
                git_config.Entries(path, (git_config.Entry("mcp.mode", "off"), git_config.Entry("mcp.mode", "on"))),
                git_config.list_entries(path),
            )
            path.write_text("[mcp\n")
            self.assertIsInstance(git_config.get_all(path, "mcp.mode", as_bool=False), git_config.ReadFailed)
            self.assertIsInstance(git_config.list_entries(path), git_config.ReadFailed)

    def test_process_failure_never_looks_like_missing_or_an_acknowledged_write(self) -> None:
        with patch.object(git_config.subprocess, "run", side_effect=OSError("Git unavailable")):
            self.assertIsInstance(git_config.get_all(Path("config"), "mcp.mode", as_bool=False), git_config.ReadFailed)
            self.assertIsInstance(git_config.list_entries(Path("config")), git_config.ReadFailed)
            self.assertEqual(
                git_config.WriteUnconfirmed(Path("config"), "mcp.mode", git_config.LaunchFailed("Git unavailable")),
                git_config.add(Path("config"), "mcp.mode", "on"),
            )
        with patch.object(git_config.subprocess, "run", return_value=CompletedProcess([], 1, b"", b"read failed")):
            self.assertEqual(
                git_config.ReadFailed(
                    Path("config"), "get-all", "mcp.mode", git_config.ProcessFailed(1, "read failed")
                ),
                git_config.get_all(Path("config"), "mcp.mode", as_bool=False),
            )

    def test_process_stderr_does_not_prove_an_unavailable_working_directory(self) -> None:
        diagnostic = "fatal: Unable to read current working directory: No such file or directory"
        with patch.object(
            git_config.subprocess, "run", return_value=CompletedProcess([], 128, b"", diagnostic.encode())
        ):
            result = git_config.list_entries(Path("config"))
        assert isinstance(result, git_config.ReadFailed)
        self.assertEqual(git_config.ProcessFailed(128, diagnostic), result.cause)
        with (
            patch.object(
                git_config.subprocess, "run", return_value=CompletedProcess([], 128, b"", diagnostic.encode())
            ),
            patch.object(Path, "cwd", side_effect=FileNotFoundError("direct cwd observation")),
        ):
            unavailable = git_config.list_entries(Path("config"))
        assert isinstance(unavailable, git_config.ReadFailed)
        assert isinstance(unavailable.cause, git_config.WorkingDirectoryUnavailable)
        self.assertEqual(git_config.ProcessFailed(128, diagnostic), unavailable.cause.process_failure)
        self.assertIn("direct cwd observation", unavailable.cause.diagnostic)

    def test_successful_process_with_invalid_output_is_a_distinct_observation(self) -> None:
        for output in (b"unframed", b"key\n\xff\0", b"entry-without-value\0"):
            with (
                self.subTest(output=output),
                patch.object(git_config.subprocess, "run", return_value=CompletedProcess([], 0, output, b"")),
            ):
                result = git_config.list_entries(Path("config"))
            assert isinstance(result, git_config.ReadFailed)
            self.assertIsInstance(result.cause, git_config.InvalidOutput)


if __name__ == "__main__":
    unittest.main()
