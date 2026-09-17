"""Invoke the installed native SDK contract without a CLI compatibility bridge."""

import asyncio
import io

from mcp_types import CallToolResult

from pinboard.mcp import server as mcp_server
from tests.support import JsonObject


def call_native_tool(tool: str, arguments: JsonObject) -> JsonObject:
    executor = mcp_server.BoundedExecutor(worker_count=1, unfinished_limit=1)
    server = mcp_server.create_server(executor, mcp_server.Diagnostics(io.StringIO(), event_limit=4, line_limit=256))
    try:
        result = asyncio.run(server.call_tool(tool, arguments))
        assert isinstance(result, CallToolResult) and isinstance(result.structured_content, dict)
        assert not result.is_error
        content: JsonObject = result.structured_content
        return content
    finally:
        executor.shutdown()
