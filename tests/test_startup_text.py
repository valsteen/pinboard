"""Selected startup pages preserve exact bytes, identities and read-only native effects."""

import asyncio
import hashlib
import io
import os
import sys
import tempfile
from pathlib import Path
from typing import override

import msgspec
from mcp import Client
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver import MCPServer
from mcp_types import TextContent

from pinboard.domain import work_models
from pinboard.mcp import common, contract_schemas, contracts, execution, server, startup_text
from tests.checkpoint_support import CheckpointPackageSupport


class StartupTextTest(CheckpointPackageSupport):
    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "selected.txt"
        self.request: dict[str, contracts.JsonValue] = {
            "operation": "file-start",
            "project_root": str(self.root),
            "work_root": str(self.root / "absent"),
            "path": str(self.path),
            "max_batch_bytes": 4000,
        }

    def call(self, request: dict[str, contracts.JsonValue]) -> dict[str, contracts.JsonValue]:
        result = startup_text.read({"request": request}, execution.CancellationToken())
        return contract_schemas.validate_result("pinboard_startup_text", result.content)

    def test_complete_unicode_escaping_long_lines_and_empty_inputs(self) -> None:
        for text in ('{"long":"' + 'café 音\\"\n' * 4000 + '"}', "short\n", ""):
            with self.subTest(size=len(text)):
                self.path.write_bytes(text.encode())
                request = self.request
                parts: list[str] = []
                start = 0
                while True:
                    page = self.call(request)
                    self.assertEqual("ok", page["status"], page)
                    self.assertLessEqual(startup_text.envelope_size(page), 4000)
                    self.assertEqual(start, page["start_character"])
                    source = page["source"]
                    assert isinstance(source, dict)
                    self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), source["sha256"])
                    self.assertEqual(len(text.encode()), source["size_bytes"])
                    chunk = page["text"]
                    assert isinstance(chunk, str)
                    parts.append(chunk)
                    start += len(chunk)
                    self.assertEqual(start, page["end_character"])
                    continuation = page["continuation"]
                    if continuation is None:
                        break
                    assert isinstance(continuation, dict)
                    request = continuation
                self.assertEqual(text.encode(), "".join(parts).encode())
                self.assertFalse((self.root / "absent").exists())

    def test_changed_source_stale_continuation_and_invalid_input(self) -> None:
        self.path.write_text("abcdef" * 4000)
        first = self.call(self.request)
        continuation = first["continuation"]
        assert isinstance(continuation, dict)
        self.path.write_text("other" * 4000)
        self.assertEqual("rejected", self.call(continuation)["status"])
        self.path.write_text("abcdef" * 4000)
        for change in ({"start_character": 999999}, {"start_character": 0}, {"extra": True}):
            with self.subTest(change=change):
                self.assertEqual("rejected", self.call(continuation | change)["status"])
        for change in (
            {"path": "relative"},
            {"max_batch_bytes": 8001},
            {"max_batch_bytes": True},
            {"operation": "unknown"},
        ):
            with self.subTest(change=change):
                self.assertEqual("rejected", self.call(self.request | change)["status"])
        self.path.write_bytes(b"\xff")
        self.assertEqual("rejected", self.call(self.request)["status"])
        self.path.unlink()
        self.assertEqual("rejected", self.call(self.request)["status"])

    def test_budget_accounts_for_metadata_and_sdk_duplication(self) -> None:
        self.path.write_text("x" * 10000)
        rejected = self.call(self.request | {"max_batch_bytes": 1})
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual("max_batch_bytes", rejected["resource"])
        page = self.call(self.request)
        self.assertLess(len(msgspec.json.encode(page)), startup_text.envelope_size(page))
        self.assertGreater(len(msgspec.json.encode([page, page, page])), 4000)
        invalid = dict(page)
        invalid["end_character"] = 0
        with self.assertRaises(msgspec.ValidationError):
            contract_schemas.validate_result("pinboard_startup_text", invalid)

    def test_exact_accepted_artifact_tuple_and_bytes_are_required(self) -> None:
        fixture = self.checkpoint_fixture()
        store = fixture.store
        reference = store.read_latest_artifact_reference(work_models.ArtifactKind.BRIEF, fixture.brief.attempt_id)
        assert reference is not None
        source: dict[str, contracts.JsonValue] = {
            "kind": "artifact",
            "artifact_ref_id": int(reference.artifact_ref_id),
            "selector": reference.selector,
            "sha256": reference.content_sha256,
            "size_bytes": reference.size_bytes,
        }
        request: dict[str, contracts.JsonValue] = {
            "operation": "artifact-start",
            "project_root": str(fixture.project),
            "work_root": str(fixture.work),
            "source": source,
            "max_batch_bytes": 8000,
        }
        self.assertEqual("ok", self.call(request)["status"])
        for change in (
            {"artifact_ref_id": 999999},
            {"selector": "different"},
            {"sha256": "0" * 64},
            {"size_bytes": 1},
        ):
            with self.subTest(change=change):
                self.assertEqual("rejected", self.call(request | {"source": source | change})["status"])
        path = fixture.work / reference.selector
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        self.assertEqual("rejected", self.call(request)["status"])
        self.assertEqual(original + b" ", path.read_bytes())

    def test_enabled_capture_never_writes_or_resolves_startup_settings(self) -> None:
        self.path.write_text("exact selected text")
        directory = self.root / "traces"
        directory.mkdir()
        for capture in (execution.SemanticCapture(directory), execution.AutomaticCapture(common.select_capture_item)):
            executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
            transport = server.create_server(
                executor, execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256), capture
            )

            async def scenario(selected_transport: MCPServer) -> None:
                async with Client(selected_transport) as client:
                    result = await client.call_tool("pinboard_startup_text", {"request": self.request})
                    self.assertFalse(result.is_error)
                    self.assertEqual("ok", result.structured_content["status"])

            try:
                asyncio.run(scenario(transport))
            finally:
                executor.shutdown()
        self.assertEqual([], list(directory.iterdir()))
        self.assertFalse((self.root / "absent").exists())

    def test_high_level_sdk_server_metadata_fits_the_complete_page_budget(self) -> None:
        self.path.write_text("x" * 17000)
        executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        transport = server.create_server(executor, execution.Diagnostics(io.StringIO(), event_limit=2, line_limit=256))

        async def scenario() -> None:
            async with Client(transport) as client:
                result = await client.call_tool("pinboard_startup_text", {"request": self.request})
                self.assertLessEqual(len(result.model_dump_json(by_alias=True, exclude_none=True).encode()), 4000)
                self.assertEqual("ok", result.structured_content["status"])

        try:
            asyncio.run(scenario())
        finally:
            executor.shutdown()

    def test_native_sdk_pages_fit_budget_without_capture_settings(self) -> None:
        text = '"\\café 音\n' * 1000
        self.path.write_text(text)

        async def scenario() -> None:
            parameters = StdioServerParameters(
                command=sys.executable, args=["-m", "pinboard.mcp"], env=os.environ.copy()
            )
            async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                await session.initialize()
                tool = next(tool for tool in (await session.list_tools()).tools if tool.name == "pinboard_startup_text")
                assert tool.annotations is not None
                self.assertTrue(tool.annotations.read_only_hint)
                request = self.request
                parts: list[str] = []
                while True:
                    result = await session.call_tool("pinboard_startup_text", {"request": request})
                    self.assertFalse(result.is_error)
                    self.assertLessEqual(len(result.model_dump_json(by_alias=True, exclude_none=True).encode()), 4000)
                    page = result.structured_content
                    assert isinstance(page, dict)
                    self.assertEqual("ok", page["status"], page)
                    companion = result.content[0]
                    assert isinstance(companion, TextContent)
                    self.assertEqual(page, msgspec.json.decode(companion.text))
                    parts.append(page["text"])
                    if page["continuation"] is None:
                        break
                    request = page["continuation"]
                self.assertEqual(text.encode(), "".join(parts).encode())
                rejected = await session.call_tool(
                    "pinboard_startup_text", {"request": self.request | {"path": str(self.root / "missing")}}
                )
                self.assertLessEqual(len(rejected.model_dump_json(by_alias=True, exclude_none=True).encode()), 4000)
                self.assertEqual("rejected", rejected.structured_content["status"])

        asyncio.run(scenario())
        self.assertFalse((self.root / "absent").exists())
        self.assertEqual(text, self.path.read_text())
