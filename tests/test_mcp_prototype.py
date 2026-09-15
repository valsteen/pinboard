import asyncio
import io
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import anyio
import msgspec
from mcp.client.session import ClientSession, IncomingMessage
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.message import SessionMessage
from mcp_types import CallToolResult, TextContent

from pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import queries
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import ItemId
from tests.prototypes import mcp_agent_interface
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store


def _run_async[Result](operation: Coroutine[None, None, Result]) -> Result:
    return asyncio.run(operation)


async def _wait_for(event: threading.Event) -> None:
    if not await asyncio.to_thread(event.wait, 2):
        raise AssertionError("A deterministic synchronization point did not complete.")


async def _await_after_ready[Result](
    execution: mcp_agent_interface.Execution[Result],
    ready: asyncio.Event,
) -> Result:
    ready.set()
    return await execution.result()


class BoundedExecutorTest(unittest.TestCase):
    def test_worker_and_admission_limits_reject_before_effect(self) -> None:
        async def scenario() -> None:
            executor = mcp_agent_interface.BoundedExecutor(worker_count=2, unfinished_limit=3)
            release = threading.Event()
            started = (threading.Event(), threading.Event())
            rejected_effect = threading.Event()

            def blocked(token: mcp_agent_interface.CancellationToken, index: int) -> int:
                started[index].set()
                if not release.wait(2):
                    raise AssertionError("The blocking operation was not released.")
                token.checkpoint()
                return index

            first = executor.submit(lambda token: blocked(token, 0))
            second = executor.submit(lambda token: blocked(token, 1))
            await asyncio.gather(*(_wait_for(event) for event in started))
            third = executor.submit(lambda _token: 3)
            with self.assertRaises(mcp_agent_interface.ExecutorBusy):
                executor.submit(lambda _token: rejected_effect.set())
            self.assertFalse(rejected_effect.is_set())

            release.set()
            self.assertEqual((0, 1, 3), tuple(await asyncio.gather(first.result(), second.result(), third.result())))
            executor.shutdown()

        _run_async(scenario())

    def test_queued_and_running_cancellation_release_admission(self) -> None:
        async def scenario() -> None:
            executor = mcp_agent_interface.BoundedExecutor(worker_count=1, unfinished_limit=2)
            running_started = threading.Event()
            running_release = threading.Event()
            queued_effect = threading.Event()
            cooperative_cancelled = threading.Event()

            def running(token: mcp_agent_interface.CancellationToken) -> str:
                running_started.set()
                if not running_release.wait(2):
                    raise AssertionError("The running operation was not released.")
                try:
                    token.checkpoint()
                except mcp_agent_interface.OperationCancelled:
                    cooperative_cancelled.set()
                    raise
                return "unexpected"

            active = executor.submit(running)
            await _wait_for(running_started)
            queued = executor.submit(lambda _token: queued_effect.set())
            queued_ready = asyncio.Event()
            queued_waiter = asyncio.create_task(_await_after_ready(queued, queued_ready))
            await queued_ready.wait()
            queued_waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued_waiter

            active_ready = asyncio.Event()
            active_waiter = asyncio.create_task(_await_after_ready(active, active_ready))
            await active_ready.wait()
            active_waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await active_waiter
            running_release.set()
            await _wait_for(active.finished)
            await _wait_for(queued.finished)

            self.assertFalse(queued_effect.is_set())
            self.assertTrue(cooperative_cancelled.is_set())
            replacement = executor.submit(lambda token: token.cancelled)
            self.assertFalse(await replacement.result())
            executor.shutdown()

        _run_async(scenario())

    def test_shutdown_joins_workers_and_rejects_new_work(self) -> None:
        executor = mcp_agent_interface.BoundedExecutor(worker_count=2, unfinished_limit=2)
        self.assertEqual("done", _run_async(executor.submit(lambda _token: "done").result()))
        executor.shutdown()
        self.assertFalse(
            any(thread.name.startswith(mcp_agent_interface.THREAD_NAME_PREFIX) for thread in threading.enumerate())
        )
        with self.assertRaises(mcp_agent_interface.ExecutorClosed):
            executor.submit(lambda _token: None)


class McpTransportTest(unittest.TestCase):
    def _project(self) -> tuple[tempfile.TemporaryDirectory[str], Path, DurableRoots]:
        temporary = tempfile.TemporaryDirectory()
        project = Path(temporary.name).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        initialize_store(SQLiteWorkStore(roots.database_path), complete_sqlite_state())
        return temporary, project, roots

    def _expected_bytes(self, roots: DurableRoots, item_id: str) -> bytes:
        projected = queries.project_item_status(
            SQLiteWorkStore(roots.database_path), ItemId(item_id), datetime.now(UTC)
        )
        if isinstance(projected, DecisionFailure):
            raise AssertionError(projected.message)
        return msgspec.json.encode(projected)

    def test_mcp_cancellation_releases_running_and_queued_admission(self) -> None:  # noqa: PLR0915 - one MCP cancellation journey
        executor = mcp_agent_interface.BoundedExecutor(worker_count=1, unfinished_limit=2)
        diagnostics_stream = io.StringIO()
        diagnostics = mcp_agent_interface.Diagnostics(diagnostics_stream, event_limit=16, line_limit=256)
        server = mcp_agent_interface.create_server(executor, diagnostics)
        original_emit = diagnostics.emit
        running_started = threading.Event()
        running_release = threading.Event()
        queued_effect = threading.Event()
        cooperative_cancelled = threading.Event()
        cancellation_events = (threading.Event(), threading.Event())
        cancellations_lock = threading.Lock()
        cancellation_count = 0
        calls_lock = threading.Lock()
        call_count = 0
        admitted = (threading.Event(), threading.Event())
        executions: list[mcp_agent_interface.Execution[dict[str, mcp_agent_interface.JsonValue]]] = []
        original_submit = executor.submit

        def controlled_read(
            _project_root: str,
            _work_root: str,
            _item_id: str,
            token: mcp_agent_interface.CancellationToken,
        ) -> dict[str, mcp_agent_interface.JsonValue]:
            nonlocal call_count
            with calls_lock:
                call_count += 1
                if call_count > 1:
                    queued_effect.set()
            running_started.set()
            if not running_release.wait(2):
                raise AssertionError("The MCP operation was not released.")
            try:
                token.checkpoint()
            except mcp_agent_interface.OperationCancelled:
                cooperative_cancelled.set()
                raise
            return {}

        def observed_submit(
            callback: Callable[[mcp_agent_interface.CancellationToken], dict[str, mcp_agent_interface.JsonValue]],
        ) -> mcp_agent_interface.Execution[dict[str, mcp_agent_interface.JsonValue]]:
            execution = original_submit(callback)
            executions.append(execution)
            admitted[len(executions) - 1].set()
            return execution

        def observed_emit(
            *,
            event: str,
            request_id: int | None,
            project_id: str | None,
            classification: str | None,
        ) -> None:
            nonlocal cancellation_count
            original_emit(
                event=event,
                request_id=request_id,
                project_id=project_id,
                classification=classification,
            )
            if classification == "cancelled":
                with cancellations_lock:
                    cancellation_events[cancellation_count].set()
                    cancellation_count += 1

        async def scenario() -> None:
            client_send, server_receive = anyio.create_memory_object_stream[SessionMessage | Exception](0)
            server_send, client_receive = anyio.create_memory_object_stream[SessionMessage](0)
            lowlevel = server._lowlevel_server

            async def run_server() -> None:
                await lowlevel.run(
                    server_receive,
                    server_send,
                    lowlevel.create_initialization_options(),
                )

            async with server_receive, server_send, client_receive, anyio.create_task_group() as server_tasks:
                server_tasks.start_soon(run_server)
                async with client_send, ClientSession(client_receive, client_send) as session:
                    await session.initialize()
                    arguments = {"project_root": "/project", "work_root": "/work", "item_id": "item"}
                    with (
                        patch.object(mcp_agent_interface, "_read_item_status", controlled_read),
                        patch.object(executor, "submit", observed_submit),
                        patch.object(diagnostics, "emit", observed_emit),
                    ):
                        active = asyncio.create_task(session.call_tool(mcp_agent_interface.TOOL_NAME, arguments))
                        await _wait_for(admitted[0])
                        await _wait_for(running_started)
                        queued = asyncio.create_task(session.call_tool(mcp_agent_interface.TOOL_NAME, arguments))
                        await _wait_for(admitted[1])

                        queued.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await queued
                        await _wait_for(cancellation_events[0])
                        await _wait_for(executions[1].finished)
                        self.assertFalse(queued_effect.is_set())

                        active.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await active
                        await _wait_for(cancellation_events[1])
                        running_release.set()
                        await _wait_for(executions[0].finished)
                await client_send.aclose()

            self.assertTrue(cooperative_cancelled.is_set())
            replacement = executor.submit(lambda token: token.cancelled)
            self.assertFalse(await replacement.result())

        try:
            _run_async(scenario())
        finally:
            running_release.set()
            executor.shutdown()

        self.assertEqual(2, diagnostics_stream.getvalue().count("classification=cancelled"))

    def test_concurrent_calls_keep_request_and_store_identity_isolated(self) -> None:
        first_temporary, first_project, first_roots = self._project()
        second_temporary, second_project, second_roots = self._project()
        self.addCleanup(first_temporary.cleanup)
        self.addCleanup(second_temporary.cleanup)
        executor = mcp_agent_interface.BoundedExecutor(worker_count=2, unfinished_limit=2)
        diagnostics = mcp_agent_interface.Diagnostics(io.StringIO(), event_limit=16, line_limit=256)
        server = mcp_agent_interface.create_server(executor, diagnostics)
        barrier = threading.Barrier(2)
        stores: list[tuple[int, Path, SQLiteWorkStore]] = []
        stores_lock = threading.Lock()

        def compose(durable: DurableRoots) -> SQLiteWorkStore:
            store = SQLiteWorkStore(durable.database_path)
            with stores_lock:
                stores.append((threading.get_ident(), durable.database_path, store))
            barrier.wait(2)
            return store

        async def scenario() -> tuple[CallToolResult, CallToolResult]:
            arguments = (
                {
                    "project_root": str(first_project),
                    "work_root": str(first_roots.work_root),
                    "item_id": "work-a",
                },
                {
                    "project_root": str(second_project),
                    "work_root": str(second_roots.work_root),
                    "item_id": "work-c",
                },
            )
            with patch.object(mcp_agent_interface, "compose_store", compose):
                first, second = await asyncio.gather(
                    *(server.call_tool(mcp_agent_interface.TOOL_NAME, value) for value in arguments)
                )
            if not isinstance(first, CallToolResult) or not isinstance(second, CallToolResult):
                raise AssertionError("The representative tool returned an unexpected MCP result.")
            return first, second

        try:
            first, second = _run_async(scenario())
        finally:
            executor.shutdown()

        self.assertEqual(self._expected_bytes(first_roots, "work-a"), msgspec.json.encode(first.structured_content))
        self.assertEqual(self._expected_bytes(second_roots, "work-c"), msgspec.json.encode(second.structured_content))
        self.assertEqual(2, len({id(store) for _thread, _path, store in stores}))
        self.assertEqual(
            {first_roots.database_path, second_roots.database_path}, {path for _thread, path, _store in stores}
        )
        self.assertEqual(2, len({thread for thread, _path, _store in stores}))
        self.assertNotIn(threading.get_ident(), {thread for thread, _path, _store in stores})

    def test_sdk_stdio_negotiates_discovers_reads_and_rejects_unknown_tool(self) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        transport_errors: list[Exception] = []

        async def record_transport_error(message: IncomingMessage) -> None:
            if isinstance(message, Exception):
                transport_errors.append(message)

        async def scenario(diagnostics: io.TextIOWrapper) -> tuple[CallToolResult, tuple[str, ...], CallToolResult]:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "tests.prototypes.mcp_agent_interface"],
                cwd=Path.cwd(),
            )
            async with (
                stdio_client(parameters, errlog=diagnostics) as streams,
                ClientSession(*streams, message_handler=record_transport_error) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool(
                    mcp_agent_interface.TOOL_NAME,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "item_id": "work-a",
                    },
                )
                rejected = await session.call_tool("unsupported_tool", {})
                return result, tuple(tool.name for tool in tools.tools), rejected

        with tempfile.TemporaryFile(mode="w+") as diagnostics:
            result, tools, rejected = _run_async(scenario(diagnostics))
            diagnostics.seek(0)
            stderr = diagnostics.read()
        self.assertEqual((mcp_agent_interface.TOOL_NAME,), tools)
        self.assertEqual(self._expected_bytes(roots, "work-a"), msgspec.json.encode(result.structured_content))
        self.assertTrue(rejected.is_error)
        self.assertEqual(1, len(rejected.content))
        self.assertIsInstance(rejected.content[0], TextContent)
        self.assertIn("Unknown tool", rejected.content[0].text)
        self.assertLessEqual(len(stderr.encode()), 2_048)
        self.assertNotIn(str(project), stderr)
        self.assertNotIn("work-a", stderr)
        self.assertIn("classification=ok", stderr)
        self.assertEqual([], transport_errors)


if __name__ == "__main__":
    unittest.main()
