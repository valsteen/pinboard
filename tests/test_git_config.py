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
                git_config.WriteUnconfirmed(Path("config"), "mcp.mode", "Git unavailable"),
                git_config.add(Path("config"), "mcp.mode", "on"),
            )
        with patch.object(git_config.subprocess, "run", return_value=CompletedProcess([], 1, b"", b"read failed")):
            self.assertEqual(
                git_config.ReadFailed(Path("config"), "get-all", "mcp.mode", "read failed"),
                git_config.get_all(Path("config"), "mcp.mode", as_bool=False),
            )


if __name__ == "__main__":
    unittest.main()
