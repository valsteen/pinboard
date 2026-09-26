"""Visible user configuration for MCP schema advertisement."""

import asyncio
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp_types import Tool

from pinboard.adapters.files import git_config
from pinboard.adapters.files.setting_resolution import SettingResolutionError
from pinboard.adapters.files.user_config import read_mcp_omit_regex_lookarounds
from pinboard.mcp import contracts, server


class McpUserConfigTest(unittest.TestCase):
    def test_stored_choice_controls_all_stdio_tool_schemas(self) -> None:
        def patterns(value: contracts.JsonSchemaValue) -> list[str]:
            if isinstance(value, dict):
                found = [pattern] if isinstance(pattern := value.get("pattern"), str) else []
                return found + [pattern for child in value.values() for pattern in patterns(child)]
            if isinstance(value, list):
                return [pattern for child in value for pattern in patterns(child)]
            return []

        async def advertised() -> list[Tool]:
            parameters = StdioServerParameters(
                command=sys.executable, args=["-m", "pinboard.mcp"], env=os.environ.copy()
            )
            async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                await session.initialize()
                return (await session.list_tools()).tools

        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary}):
            path = Path(temporary) / "pinboard" / "config"
            enabled = asyncio.run(advertised())
            self.assertEqual("[mcp]\n\tomitRegexLookarounds = true\n", path.read_text())
            path.write_text("[mcp]\n\tomitRegexLookarounds = false\n")
            disabled = asyncio.run(advertised())
            self.assertEqual([tool.name for tool in enabled], [tool.name for tool in disabled])
            original_patterns: list[str] = []
            for projected, original in zip(enabled, disabled, strict=True):
                with self.subTest(tool=projected.name):
                    self.assertEqual("object", projected.input_schema["type"])
                    self.assertEqual(set(projected.input_schema), set(original.input_schema))
                    self.assertFalse(
                        any(
                            marker in pattern
                            for pattern in patterns(projected.input_schema)
                            for marker in ("(?=", "(?!", "(?<=", "(?<!")
                        )
                    )
                    original_patterns.extend(patterns(original.input_schema))
            self.assertTrue(any("(?!" in pattern for pattern in original_patterns))

    def test_first_use_materializes_and_preserves_explicit_choice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary}):
            path = Path(temporary) / "pinboard" / "config"
            first = read_mcp_omit_regex_lookarounds()
            self.assertTrue(first.value)
            self.assertEqual(path, first.path)
            self.assertEqual("unconfirmed", first.effects.parent_creation)
            self.assertEqual(("unconfirmed", "acknowledged"), (first.effects.file_creation, first.effects.key_write))
            first_use = path.read_text()
            self.assertEqual("[mcp]\n\tomitRegexLookarounds = true\n", first_use)
            path.write_text("[mcp]\n\tomitRegexLookarounds = false\n")
            stored = read_mcp_omit_regex_lookarounds()
            self.assertFalse(stored.value)
            self.assertEqual("none", stored.effects.parent_creation)
            self.assertEqual(("none", "none"), (stored.effects.file_creation, stored.effects.key_write))
            self.assertEqual("[mcp]\n\tomitRegexLookarounds = false\n", path.read_text())
            path.write_text("[other]\n\tvalue = kept\n")
            missing_key = read_mcp_omit_regex_lookarounds()
            self.assertTrue(missing_key.value)
            self.assertEqual(
                ("none", "acknowledged"), (missing_key.effects.file_creation, missing_key.effects.key_write)
            )
            self.assertEqual("[other]\n\tvalue = kept\n" + first_use, path.read_text())

    def test_invalid_or_unwritable_config_stops_mcp_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary}):
            path = Path(temporary) / "pinboard" / "config"
            path.parent.mkdir()
            path.write_text("[mcp]\n\tomitRegexLookarounds = perhaps\n")
            for expected in ("Cannot read Pinboard MCP config", "Cannot read or write Pinboard MCP config"):
                if expected.startswith("Cannot read or write"):
                    path.unlink()
                    path.parent.rmdir()
                    path.parent.write_text("not a directory")
                stderr = io.StringIO()
                with (
                    patch.object(server.sys, "argv", ["pinboard-mcp"]),
                    redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as stopped,
                ):
                    server.main()
                self.assertEqual(64, stopped.exception.code)
                self.assertIn(expected, stderr.getvalue())
                self.assertIn(str(path), stderr.getvalue())

    def test_duplicate_value_is_rejected_without_rewriting_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary}):
            path = Path(temporary) / "pinboard" / "config"
            path.parent.mkdir()
            content = "[mcp]\n\tomitRegexLookarounds = false\n\tomitRegexLookarounds = true\n"
            path.write_text(content)
            with self.assertRaisesRegex(ValueError, "Invalid Pinboard MCP config"):
                read_mcp_omit_regex_lookarounds()
            self.assertEqual(content, path.read_text())

    def test_failed_reads_and_writes_report_confirmed_and_uncertain_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary}):
            path = Path(temporary) / "pinboard" / "config"
            with (
                patch.object(
                    git_config,
                    "get_all",
                    return_value=git_config.ReadFailed(path, "get-all", "mcp.omitRegexLookarounds", "read failed"),
                ),
                self.assertRaises(SettingResolutionError) as failed_read,
            ):
                read_mcp_omit_regex_lookarounds()
            self.assertIn("read failed", str(failed_read.exception))
            self.assertEqual("none", failed_read.exception.effects.parent_creation)
            self.assertFalse(path.parent.exists())

            with (
                patch.object(
                    git_config,
                    "add",
                    return_value=git_config.WriteUnconfirmed(path, "mcp.omitRegexLookarounds", "write failed"),
                ),
                self.assertRaises(SettingResolutionError) as failed,
            ):
                read_mcp_omit_regex_lookarounds()
            self.assertEqual(path, failed.exception.path)
            self.assertEqual("unconfirmed", failed.exception.effects.parent_creation)
            self.assertEqual(
                ("unconfirmed", "unconfirmed"),
                (failed.exception.effects.file_creation, failed.exception.effects.key_write),
            )
            self.assertFalse(hasattr(failed.exception, "value"))

            with (
                patch.object(
                    git_config,
                    "get_all",
                    side_effect=[
                        git_config.Missing(path, "mcp.omitRegexLookarounds"),
                        git_config.ReadFailed(path, "get-all", "mcp.omitRegexLookarounds", "read failed"),
                    ],
                ),
                self.assertRaises(SettingResolutionError) as failed_reread,
            ):
                read_mcp_omit_regex_lookarounds()
            self.assertTrue(path.is_file())
            self.assertEqual("none", failed_reread.exception.effects.parent_creation)
            self.assertEqual(
                ("unconfirmed", "acknowledged"),
                (failed_reread.exception.effects.file_creation, failed_reread.exception.effects.key_write),
            )
            self.assertIn("read failed", str(failed_reread.exception))
