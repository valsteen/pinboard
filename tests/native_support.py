"""Invoke the installed native SDK contract without a CLI compatibility bridge."""

import asyncio
import io
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult

from pinboard.mcp import execution as mcp_execution
from pinboard.mcp import server as mcp_server
from tests.support import JsonObject


@dataclass(frozen=True, slots=True)
class NativeTools:
    server: MCPServer

    def call_advertised_tool(self, tool: str, arguments: JsonObject) -> JsonObject:
        """Call through the SDK client, which checks the advertised output schema on every request."""

        async def call() -> CallToolResult:
            async with Client(self.server) as client:
                return await client.call_tool(tool, arguments)

        result = asyncio.run(call())
        assert isinstance(result.structured_content, dict)
        assert not result.is_error
        content: JsonObject = result.structured_content
        return content

    def call_native_tool(self, tool: str, arguments: JsonObject) -> JsonObject:
        result = asyncio.run(self.server.call_tool(tool, arguments))
        assert isinstance(result, CallToolResult) and isinstance(result.structured_content, dict)
        assert not result.is_error
        content: JsonObject = result.structured_content
        return content


@contextmanager
def native_tools() -> Generator[NativeTools]:
    """Own one fixture's server and executor; wait for its callbacks before disposal."""

    executor = mcp_execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
    try:
        server = mcp_server.create_server(
            executor, mcp_execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256)
        )
        yield NativeTools(server)
    finally:
        executor.shutdown()


def call_advertised_tool(tool: str, arguments: JsonObject) -> JsonObject:
    """Call one tool through a fresh SDK server, validating its advertised output schema."""

    with native_tools() as native:
        return native.call_advertised_tool(tool, arguments)


def call_native_tool(tool: str, arguments: JsonObject) -> JsonObject:
    with native_tools() as native:
        return native.call_native_tool(tool, arguments)
