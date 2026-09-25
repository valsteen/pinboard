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

from pinboard.adapters.files.user_config import read_mcp_omit_regex_lookarounds
from pinboard.mcp import contracts, server


class McpUserConfigTest(unittest.TestCase):
    def test_stored_choice_controls_all_twenty_stdio_tool_schemas(self) -> None:
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
            self.assertEqual(20, len(enabled))
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
            self.assertTrue(read_mcp_omit_regex_lookarounds())
            self.assertEqual("[mcp]\n\tomitRegexLookarounds = true\n", path.read_text())
            path.write_text("[mcp]\n\tomitRegexLookarounds = false\n")
            self.assertFalse(read_mcp_omit_regex_lookarounds())
            self.assertEqual("[mcp]\n\tomitRegexLookarounds = false\n", path.read_text())
            path.write_text("[other]\n\tvalue = kept\n")
            self.assertTrue(read_mcp_omit_regex_lookarounds())
            self.assertIn("value = kept", path.read_text())
            self.assertIn("omitRegexLookarounds = true", path.read_text())

    def test_invalid_or_unwritable_config_stops_mcp_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary}):
            path = Path(temporary) / "pinboard" / "config"
            path.parent.mkdir()
            path.write_text("[mcp]\n\tomitRegexLookarounds = perhaps\n")
            for expected in ("Invalid Pinboard MCP config", "Cannot read or write Pinboard MCP config"):
                if expected.startswith("Cannot"):
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
