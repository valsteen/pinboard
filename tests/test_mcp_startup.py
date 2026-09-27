"""MCP startup advertises one schema independent of historical user settings."""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp_types import Tool


class McpStartupTest(unittest.TestCase):
    def test_historical_setting_has_no_effect_and_is_not_rewritten(self) -> None:
        async def advertised() -> list[Tool]:
            parameters = StdioServerParameters(
                command=sys.executable, args=["-m", "pinboard.mcp"], env=os.environ.copy()
            )
            async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                await session.initialize()
                return (await session.list_tools()).tools

        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary}):
            path = Path(temporary) / "pinboard" / "config"
            current = asyncio.run(advertised())
            self.assertFalse(path.exists())
            path.parent.mkdir()
            historical = "[mcp]\n\tomitRegexLookarounds = false\n"
            path.write_text(historical)
            legacy = asyncio.run(advertised())
            self.assertEqual(historical, path.read_text())
            self.assertEqual(21, len(current))
            self.assertEqual(
                [(tool.name, tool.input_schema, tool.output_schema) for tool in current],
                [(tool.name, tool.input_schema, tool.output_schema) for tool in legacy],
            )
