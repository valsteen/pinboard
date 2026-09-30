"""Isolated Codex homes: private copy, conditional atomic write-back, refusal on conflict, removal on failure."""

import stat
import tempfile
import unittest
from pathlib import Path
from typing import override

from evals.behavioral import credentials
from evals.behavioral.credentials import CredentialSettlement


class IsolatedHomeTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source_directory = self.root / "codex"
        self.source_directory.mkdir()
        self.source = self.source_directory / "auth.json"
        self.source.write_bytes(b'{"tokens": "original"}')
        self.source.chmod(0o600)
        self.homes = self.root / "homes"
        self.homes.mkdir()

    def test_the_home_is_private_holds_a_private_copy_and_is_removed_after_an_unchanged_run(self) -> None:
        with credentials.isolated_home(self.source, self.homes) as home:
            copy = home.path / "auth.json"
            self.assertEqual(b'{"tokens": "original"}', copy.read_bytes())
            self.assertEqual(0o700, stat.S_IMODE(home.path.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(copy.stat().st_mode))
        self.assertIs(CredentialSettlement.UNCHANGED, home.settlement)
        self.assertFalse(home.path.exists())
        self.assertEqual(b'{"tokens": "original"}', self.source.read_bytes())

    def test_a_refreshed_copy_is_written_back_privately_when_the_source_is_unchanged(self) -> None:
        with credentials.isolated_home(self.source, self.homes) as home:
            (home.path / "auth.json").write_bytes(b'{"tokens": "refreshed"}')
        self.assertIs(CredentialSettlement.WRITTEN_BACK, home.settlement)
        self.assertEqual(b'{"tokens": "refreshed"}', self.source.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(self.source.stat().st_mode))
        self.assertEqual(["auth.json"], sorted(path.name for path in self.source_directory.iterdir()))

    def test_nothing_is_written_back_when_the_source_changed_during_the_run(self) -> None:
        with credentials.isolated_home(self.source, self.homes) as home:
            (home.path / "auth.json").write_bytes(b'{"tokens": "refreshed"}')
            self.source.write_bytes(b'{"tokens": "human login"}')
        self.assertIs(CredentialSettlement.REFUSED_SOURCE_CHANGED, home.settlement)
        self.assertEqual(b'{"tokens": "human login"}', self.source.read_bytes())
        self.assertFalse(home.path.exists())

    def test_the_home_is_removed_and_the_failure_propagates_when_a_run_raises(self) -> None:
        holder: list[Path] = []
        with self.assertRaises(RuntimeError), credentials.isolated_home(self.source, self.homes) as home:
            holder.append(home.path)
            raise RuntimeError("run failed")
        self.assertFalse(holder[0].exists())
        self.assertEqual([], list(self.homes.iterdir()))


class LoginRedactionTest(unittest.TestCase):
    def test_long_login_values_are_removed_from_kept_evidence(self) -> None:
        copied = b'{"auth_mode": "chatgpt", "tokens": {"account_id": "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"}}'
        text = '{"session_meta": {"account": "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b", "mode": "chatgpt"}}'
        cleaned = credentials.without_login(text, copied)
        self.assertNotIn("0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b", cleaned)
        self.assertIn(credentials.LOGIN_PLACEHOLDER, cleaned)
        self.assertIn('"mode": "chatgpt"', cleaned)


if __name__ == "__main__":
    unittest.main()
