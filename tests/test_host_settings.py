"""A recognized host's startup page size is the user's stored setting, seeded once and never silently replaced."""

import asyncio
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

import msgspec
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp_types import Implementation

from pinboard.adapters.files import git_config, host_settings
from pinboard.mcp import contract_schemas, contracts, execution, host_profiles, server, startup_text

CLAUDE = host_profiles.HostProfile.CLAUDE_CODE
CLAUDE_KEY = "host.claude-code.startup-page-bytes"


class HostSettingsTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        home = patch.dict(os.environ, {"HOME": str(self.root / "home")})
        home.start()
        self.addCleanup(home.stop)
        self.settings = self.root / "home" / ".config" / "pinboard" / "hosts.config"
        source = self.root / "selected.txt"
        source.write_text("Pinboard guidance line with ordinary prose.\n" * 2000)
        self.request: dict[str, contracts.JsonValue] = {
            "operation": "file-start",
            "project_root": str(self.root),
            "work_root": str(self.root / "absent"),
            "path": str(source),
            "max_batch_bytes": "host-profile",
        }

    def read(self, profile: host_profiles.HostProfile = CLAUDE) -> dict[str, contracts.JsonValue]:
        result = startup_text.read({"request": self.request}, profile, execution.CancellationToken())
        validated = contract_schemas.validate_result("pinboard_startup_text", result.content)
        wire: dict[str, contracts.JsonValue] = msgspec.json.decode(msgspec.json.encode(validated))
        return wire

    def store(self, value: str) -> None:
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(f'[host "claude-code"]\n\tstartup-page-bytes = {value}\n')

    def assert_failure(
        self, page: dict[str, contracts.JsonValue], retry: str, effect: str, key: str = CLAUDE_KEY
    ) -> dict[str, contracts.JsonValue]:
        self.assertEqual("HOST_PAGE_SETTING_UNAVAILABLE", page["code"], page)
        self.assertEqual(str(self.settings), page["setting_file"])
        self.assertEqual(key, page["setting_key"])
        self.assertIn(key, str(page["message"]))
        self.assertNotIn("..", str(page["message"]))
        self.assertEqual(retry, page["retry"])
        self.assertEqual(effect, page["effect"])
        self.assertNotIn("text", page)
        return page

    def test_first_read_seeds_the_measured_default_privately_and_reads_it_back(self) -> None:
        page = self.read()
        self.assertEqual("ok", page["status"], page)
        self.assertEqual(32000, page["max_batch_bytes"])
        self.assertEqual(0o700, stat.S_IMODE(self.settings.parent.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(self.settings.stat().st_mode))
        self.assertEqual(
            git_config.Entries(self.settings, (git_config.Entry(CLAUDE_KEY, "32000"),)),
            git_config.list_entries(self.settings),
        )
        self.assertEqual("ok", self.read(host_profiles.HostProfile.CODEX)["status"])
        listed = git_config.list_entries(self.settings)
        assert isinstance(listed, git_config.Entries)
        self.assertEqual(
            (git_config.Entry(CLAUDE_KEY, "32000"), git_config.Entry("host.codex.startup-page-bytes", "20000")),
            listed.entries,
        )
        self.assertFalse(self.settings.with_name("hosts.config.lock").exists())

    def test_unmeasured_clients_never_touch_host_settings(self) -> None:
        page = self.read(host_profiles.HostProfile.UNMEASURED)
        self.assertEqual(8000, page["max_batch_bytes"])
        self.assertFalse((self.root / "home").exists())

    def test_an_edited_value_is_the_budget_for_every_request_shape(self) -> None:
        self.store("9000")
        self.assertEqual(9000, self.read()["max_batch_bytes"])
        for requested, effective in ((50000, 9000), (5000, 5000)):
            with self.subTest(requested=requested):
                self.request["max_batch_bytes"] = requested
                page = self.read()
                self.assertEqual(effective, page["max_batch_bytes"])
                self.assertLessEqual(startup_text.envelope_size(page, host_profiles.representation(CLAUDE)), effective)
        self.assertIn("9000", self.settings.read_text())

    def test_malformed_and_out_of_bounds_values_fail_without_falling_back(self) -> None:
        self.settings.parent.mkdir(parents=True)
        for value in ("big", "3999", "200001"):
            with self.subTest(value=value):
                content = f'[host "claude-code"]\n\tstartup-page-bytes = {value}\n'
                self.settings.write_text(content)
                page = self.assert_failure(self.read(), "correct-input", "unchanged")
                self.assertEqual(str(self.settings), page["resource"])
                self.assertIn(f"Set {CLAUDE_KEY} in {self.settings}", str(page["recovery"]))
                self.assertIn("4000 to 200000", str(page["recovery"]))
                self.assertEqual(content, self.settings.read_text())

    def test_an_invalid_entry_names_its_own_key_and_a_repair_that_clears_it(self) -> None:
        self.settings.parent.mkdir(parents=True)
        codex = "host.codex.startup-page-bytes"
        unknown = "host.claude-code.page-size"
        claude = '[host "claude-code"]\n\tstartup-page-bytes = 9000\n'
        for content, key, repair, fixed in (
            (
                f'{claude}[host "codex"]\n\tstartup-page-bytes = 12k\n',
                codex,
                f"Set {codex} in {self.settings} to a whole number of bytes from 4000 to 200000",
                f'{claude}[host "codex"]\n\tstartup-page-bytes = 12000\n',
            ),
            (
                f'{claude}[host "codex"]\n\tstartup-page-bytes\n',
                codex,
                f"Set {codex} in {self.settings} to a whole number of bytes from 4000 to 200000",
                f'{claude}[host "codex"]\n\tstartup-page-bytes = 12000\n',
            ),
            (
                f'{claude}[host "codex"]\n\tstartup-page-bytes = 12000\n\tstartup-page-bytes = 12000\n',
                codex,
                f"Keep one {codex} entry in {self.settings}",
                f'{claude}[host "codex"]\n\tstartup-page-bytes = 12000\n',
            ),
            (
                f"{claude}\tpage-size = 9000\n",
                unknown,
                f"Remove {unknown} from {self.settings}",
                claude,
            ),
        ):
            with self.subTest(key=key, repair=repair):
                self.settings.write_text(content)
                page = self.assert_failure(self.read(), "correct-input", "unchanged", key)
                self.assertIn(repair, str(page["recovery"]))
                self.assertNotIn(CLAUDE_KEY + " ", str(page["recovery"]))
                self.assertEqual(content, self.settings.read_text())
                self.settings.write_text(fixed)
                self.assertEqual(9000, self.read()["max_batch_bytes"])
        self.settings.write_text(f"{claude}\tstartup-page-bytes = 9000\n")
        page = self.assert_failure(self.read(), "correct-input", "unchanged")
        self.assertIn(f"Keep one {CLAUDE_KEY} entry in {self.settings}", str(page["recovery"]))

    def test_a_non_regular_settings_path_names_the_file_and_a_repair_that_clears_it(self) -> None:
        self.settings.parent.mkdir(parents=True)
        elsewhere = self.root / "elsewhere.config"
        elsewhere.write_text('[host "claude-code"]\n\tstartup-page-bytes = 9000\n')
        for kind, occupy, apply_repair, budget in (
            ("directory", self.settings.mkdir, self.settings.rmdir, 32000),
            (
                "symlink",
                lambda: self.settings.symlink_to(elsewhere),
                lambda: (self.settings.unlink(), self.settings.write_text(elsewhere.read_text())),
                9000,
            ),
        ):
            with self.subTest(kind=kind):
                occupy()
                page = self.read()
                self.assertEqual("HOST_PAGE_SETTING_UNAVAILABLE", page["code"], page)
                self.assertEqual(str(self.settings), page["resource"])
                self.assertEqual(str(self.settings), page["setting_file"])
                self.assertIn(f"Host page-size settings file {self.settings} is unusable", str(page["message"]))
                self.assertIn("must be a regular file", str(page["message"]))
                self.assertEqual(
                    f"Replace {self.settings} with a regular file, or remove it so Pinboard creates it, then retry.",
                    page["recovery"],
                )
                self.assertEqual("correct-input", page["retry"])
                self.assertEqual("unchanged", page["effect"])
                self.assertEqual("none", page["settings_file_creation"])
                self.assertEqual("none", page["settings_key_write"])
                self.assertIn("9000", elsewhere.read_text())
                apply_repair()
                self.assertEqual(budget, self.read()["max_batch_bytes"])
                self.settings.unlink()

    def test_a_seed_missing_when_read_back_asks_for_a_retry_rather_than_a_key_edit(self) -> None:
        acknowledged = git_config.WriteAcknowledged(self.settings, CLAUDE_KEY)
        with patch.object(git_config, "add", return_value=acknowledged):
            page = self.assert_failure(self.read(), "retry-same-input", "committed")
        self.assertEqual(str(self.settings), page["resource"])
        self.assertEqual("acknowledged", page["settings_key_write"])
        self.assertIn(f"after this request wrote {CLAUDE_KEY}; retry the same request.", str(page["recovery"]))
        self.assertNotIn(f"Set {CLAUDE_KEY}", str(page["recovery"]))
        self.assertEqual(32000, self.read()["max_batch_bytes"])

    def test_a_file_git_cannot_parse_names_the_diagnostic_command_and_asks_for_a_correction(self) -> None:
        self.store("9000")
        self.settings.write_text('[host "claude-code"\n')
        page = self.assert_failure(self.read(), "correct-input", "unchanged")
        self.assertEqual(str(self.settings), page["resource"])
        self.assertIn(f"git config --file {self.settings} --null --list", str(page["recovery"]))
        self.assertIn("resolve the reported cause before retrying", str(page["recovery"]))
        self.settings.write_text('[host "claude-code"]\n\tstartup-page-bytes = 9000\n')
        self.assertEqual(9000, self.read()["max_batch_bytes"])

    def test_an_unwritable_settings_directory_reports_the_directory_and_creates_nothing(self) -> None:
        self.settings.parent.mkdir(parents=True)
        self.settings.parent.chmod(0o500)
        page = self.assert_failure(self.read(), "retry-same-input", "unchanged")
        self.assertEqual(str(self.settings), page["resource"])
        self.assertEqual(
            f"Grant write access to {self.settings.parent} to initialize Host page-size settings at "
            f"{self.settings}, then retry.",
            page["recovery"],
        )
        self.assertIn("Permission denied", str(page["message"]))
        self.assertEqual("none", page["settings_file_creation"])
        self.assertFalse(self.settings.exists())
        self.assertEqual([], [path.name for path in self.settings.parent.iterdir()])
        self.settings.parent.chmod(0o700)
        self.assertEqual(32000, self.read()["max_batch_bytes"])
        self.settings.unlink()
        self.settings.parent.rmdir()
        (self.root / "home" / ".config").chmod(0o500)
        self.addCleanup((self.root / "home" / ".config").chmod, 0o700)
        page = self.assert_failure(self.read(), "retry-same-input", "unchanged")
        self.assertEqual(str(self.settings.parent), page["resource"])
        self.assertIn(f"Grant write access to {self.settings.parent.parent}", str(page["recovery"]))

    def test_a_non_directory_where_the_settings_directory_belongs_is_named_with_its_repair(self) -> None:
        config = self.root / "home" / ".config"
        config.mkdir(parents=True)
        for blocking, remove in (
            (self.settings.parent, self.settings.parent.unlink),
            (config, lambda: (config.unlink(), config.mkdir())),
        ):
            with self.subTest(blocking=blocking.name):
                if blocking == config:
                    config.rmdir()
                blocking.write_text("not a directory\n")
                page = self.assert_failure(self.read(), "correct-input", "unchanged")
                self.assertEqual(str(blocking), page["resource"])
                self.assertIn(
                    f"Cannot create the host settings directory {self.settings.parent}: {blocking} exists and is "
                    "not a directory",
                    str(page["message"]),
                )
                self.assertEqual(
                    f"Replace {blocking} with a directory, or remove it so Pinboard creates it, then retry.",
                    page["recovery"],
                )
                self.assertEqual("none", page["settings_directory_creation"])
                self.assertEqual("none", page["settings_file_creation"])
                self.assertEqual("not a directory\n", blocking.read_text())
                remove()
                self.assertEqual(32000, self.read()["max_batch_bytes"])
                self.settings.unlink()
                self.settings.parent.rmdir()

    def test_a_failed_or_locked_seeding_write_reports_its_confirmed_effects(self) -> None:
        with patch.object(
            git_config,
            "add",
            return_value=git_config.WriteUnconfirmed(
                self.settings.with_name("hosts.config.lock"),
                CLAUDE_KEY,
                git_config.ProcessFailed(255, "could not lock config file"),
            ),
        ):
            page = self.assert_failure(self.read(), "retry-same-input", "committed")
        self.assertEqual("confirmed", page["settings_directory_creation"])
        self.assertEqual("confirmed", page["settings_file_creation"])
        self.assertEqual("none", page["settings_key_write"])
        self.assertEqual(["user-settings"], page["changed_surfaces"])
        self.assertIn("could not lock config file", str(page["message"]))
        self.assertEqual(b"", self.settings.read_bytes())
        lock = self.settings.with_name("hosts.config.lock")
        lock.touch()
        page = self.assert_failure(self.read(), "retry-same-input", "unchanged")
        self.assertEqual(f"Host page-size settings edit at {lock}", page["resource"])
        self.assertIn(
            f"If no edit is in progress, {lock} is a stale lock left by an interrupted process: "
            "report it to the human, who can remove it",
            str(page["recovery"]),
        )
        self.assertTrue(lock.exists())
        lock.unlink()
        self.assertEqual(32000, self.read()["max_batch_bytes"])

    def test_a_stale_git_lock_on_the_staged_copy_is_named_in_the_repair(self) -> None:
        self.settings.parent.mkdir(parents=True)
        self.settings.touch()
        git_lock = self.settings.with_name("hosts.config.lock.lock")
        git_lock.touch()
        page = self.assert_failure(self.read(), "retry-same-input", "unchanged")
        staged = self.settings.with_name("hosts.config.lock")
        self.assertEqual(str(staged), page["resource"])
        self.assertIn(f"while adding {CLAUDE_KEY} to the staged copy {staged}", str(page["recovery"]))
        self.assertIn(
            f"If Git's lock {git_lock} exists and no settings edit is in progress, it is a stale lock left by "
            "an interrupted process: report it to the human, who can remove it",
            str(page["recovery"]),
        )
        self.assertNotIn("--null --list", str(page["recovery"]))
        self.assertFalse(staged.exists())
        self.assertTrue(git_lock.exists())
        git_lock.unlink()
        self.assertEqual(32000, self.read()["max_batch_bytes"])

    def test_an_unreadable_settings_file_asks_for_read_access(self) -> None:
        self.store("9000")
        self.settings.chmod(0o200)
        self.addCleanup(self.settings.chmod, 0o600)
        page = self.assert_failure(self.read(), "retry-same-input", "unchanged")
        self.assertEqual(str(self.settings), page["resource"])
        self.assertEqual(
            f"Grant read access to {self.settings} for the Pinboard MCP service, then retry.", page["recovery"]
        )
        self.settings.chmod(0o600)
        self.assertEqual(9000, self.read()["max_batch_bytes"])

    def test_a_missing_working_directory_asks_for_a_service_restart(self) -> None:
        self.store("9000")
        original = Path.cwd()
        vanished = self.root / "vanished"
        vanished.mkdir()
        try:
            os.chdir(vanished)
            vanished.rmdir()
            page = self.read()
        finally:
            os.chdir(original)
        self.assert_failure(page, "retry-same-input", "unchanged")
        self.assertEqual("MCP service working directory", page["resource"])
        self.assertIn("Restart or reconnect", str(page["recovery"]))

    def test_an_undeterminable_home_directory_is_named(self) -> None:
        with patch.object(host_settings, "settings_path", side_effect=RuntimeError("Could not determine home")):
            page = self.read()
        self.assertEqual("HOST_PAGE_SETTING_UNAVAILABLE", page["code"])
        self.assertEqual("home directory", page["resource"])
        self.assertEqual("retry-same-input", page["retry"])

    def test_the_initialize_path_reads_the_stored_setting_and_returns_typed_failures(self) -> None:
        self.store("9000")

        async def scenario(selected: MCPServer) -> None:
            info = Implementation(name="claude-code", version="9.0.0")
            async with Client(selected, client_info=info, mode="legacy") as client:
                page = (await client.call_tool("pinboard_startup_text", {"request": self.request})).structured_content
                assert isinstance(page, dict)
                self.assertEqual(9000, page["max_batch_bytes"])
                self.settings.write_text('[host "claude-code"]\n\tstartup-page-bytes = 9 KB\n')
                failed = await client.call_tool("pinboard_startup_text", {"request": self.request})
                self.assertFalse(failed.is_error)
                rejected = failed.structured_content
                assert isinstance(rejected, dict)
                self.assertEqual("HOST_PAGE_SETTING_UNAVAILABLE", rejected["code"])
                self.assertEqual(CLAUDE_KEY, rejected["setting_key"])

        executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        try:
            asyncio.run(
                scenario(
                    server.create_server(executor, execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256))
                )
            )
        finally:
            executor.shutdown()


if __name__ == "__main__":
    unittest.main()
