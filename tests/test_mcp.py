import asyncio
import io
import subprocess
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
from mcp.server.mcpserver.exceptions import UnexpectedToolError
from mcp.shared.message import SessionMessage
from mcp_types import CallToolResult, TextContent, Tool

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from pinboard.adapters.files.models import ViewRefreshResult, ViewWarning
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import queries, stored_state
from pinboard.application.ports import WorkStoreError
from pinboard.application.work_briefs import canonical_work_brief_bytes
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import ArtifactRefId, ItemId
from pinboard.mcp import server as mcp_server
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store
from tests.test_proposals import proposal as proposal_input
from tests.work_brief_support import example_work_brief


def _run_async[Result](operation: Coroutine[None, None, Result]) -> Result:
    return asyncio.run(operation)


async def _wait_for(event: threading.Event) -> None:
    if not await asyncio.to_thread(event.wait, 2):
        raise AssertionError("A deterministic synchronization point did not complete.")


async def _await_after_ready[Result](
    execution: mcp_server.Execution[Result],
    ready: asyncio.Event,
) -> Result:
    ready.set()
    return await execution.result()


class BoundedExecutorTest(unittest.TestCase):
    def test_worker_and_admission_limits_reject_before_effect(self) -> None:
        async def scenario() -> None:
            executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=3)
            release = threading.Event()
            started = (threading.Event(), threading.Event())
            rejected_effect = threading.Event()

            def blocked(token: mcp_server.CancellationToken, index: int) -> int:
                started[index].set()
                if not release.wait(2):
                    raise AssertionError("The blocking operation was not released.")
                token.checkpoint()
                return index

            first = executor.submit(lambda token: blocked(token, 0))
            second = executor.submit(lambda token: blocked(token, 1))
            await asyncio.gather(*(_wait_for(event) for event in started))
            third = executor.submit(lambda _token: 3)
            with self.assertRaises(mcp_server.ExecutorBusy):
                executor.submit(lambda _token: rejected_effect.set())
            self.assertFalse(rejected_effect.is_set())

            release.set()
            self.assertEqual((0, 1, 3), tuple(await asyncio.gather(first.result(), second.result(), third.result())))
            executor.shutdown()

        _run_async(scenario())

    def test_queued_and_running_cancellation_release_admission(self) -> None:
        async def scenario() -> None:
            executor = mcp_server.BoundedExecutor(worker_count=1, unfinished_limit=2)
            running_started = threading.Event()
            running_release = threading.Event()
            queued_effect = threading.Event()
            cooperative_cancelled = threading.Event()

            def running(token: mcp_server.CancellationToken) -> str:
                running_started.set()
                if not running_release.wait(2):
                    raise AssertionError("The running operation was not released.")
                try:
                    token.checkpoint()
                except mcp_server.OperationCancelled:
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
            await asyncio.sleep(0)
            self.assertFalse(active_waiter.done())
            running_release.set()
            with self.assertRaises(mcp_server.OperationCancelled):
                await active_waiter
            await _wait_for(active.finished)
            await _wait_for(queued.finished)

            self.assertFalse(queued_effect.is_set())
            self.assertTrue(cooperative_cancelled.is_set())
            replacement = executor.submit(lambda token: token.cancelled)
            self.assertFalse(await replacement.result())
            executor.shutdown()

        _run_async(scenario())

    def test_shutdown_joins_workers_and_rejects_new_work(self) -> None:
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=2)
        self.assertEqual("done", _run_async(executor.submit(lambda _token: "done").result()))
        executor.shutdown()
        self.assertFalse(any(thread.name.startswith(mcp_server.THREAD_NAME_PREFIX) for thread in threading.enumerate()))
        with self.assertRaises(mcp_server.ExecutorClosed):
            executor.submit(lambda _token: None)


class McpTransportTest(unittest.TestCase):
    def _project(self) -> tuple[tempfile.TemporaryDirectory[str], Path, DurableRoots]:
        temporary = tempfile.TemporaryDirectory()
        project = Path(temporary.name).resolve()
        subprocess.run(("git", "init", "--quiet", str(project)), check=True)
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
        executor = mcp_server.BoundedExecutor(worker_count=1, unfinished_limit=2)
        diagnostics_stream = io.StringIO()
        diagnostics = mcp_server.Diagnostics(diagnostics_stream, event_limit=16, line_limit=256)
        server = mcp_server.create_server(executor, diagnostics)
        original_emit = diagnostics.emit
        running_started = threading.Event()
        running_release = threading.Event()
        queued_effect = threading.Event()
        cooperative_cancelled = threading.Event()
        cancellation_events = (threading.Event(), threading.Event())
        token_cancellation_events = (threading.Event(), threading.Event())
        cancellations_lock = threading.Lock()
        cancellation_count = 0
        token_cancellation_count = 0
        calls_lock = threading.Lock()
        call_count = 0
        admitted = (threading.Event(), threading.Event())
        executions: list[mcp_server.Execution[mcp_server.OperationResult]] = []
        original_submit = executor.submit
        original_cancel = mcp_server.CancellationToken.cancel

        def controlled_read(
            _project_root: str,
            _work_root: str,
            _item_id: str,
            token: mcp_server.CancellationToken,
        ) -> mcp_server.OperationResult:
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
            except mcp_server.OperationCancelled:
                cooperative_cancelled.set()
                raise
            return mcp_server.OperationResult({}, "ok", None)

        def observed_submit(
            callback: Callable[[mcp_server.CancellationToken], mcp_server.OperationResult],
        ) -> mcp_server.Execution[mcp_server.OperationResult]:
            execution = original_submit(callback)
            executions.append(execution)
            admitted[len(executions) - 1].set()
            return execution

        def observed_emit(
            *,
            event: str,
            request_id: int | None,
            operation: str | None,
            project_id: str | None,
            duration_ms: int | None,
            classification: str | None,
            commit_reference: str | None,
        ) -> None:
            nonlocal cancellation_count
            original_emit(
                event=event,
                request_id=request_id,
                operation=operation,
                project_id=project_id,
                duration_ms=duration_ms,
                classification=classification,
                commit_reference=commit_reference,
            )
            if classification == "cancelled":
                with cancellations_lock:
                    cancellation_events[cancellation_count].set()
                    cancellation_count += 1

        def observed_cancel(token: mcp_server.CancellationToken) -> None:
            nonlocal token_cancellation_count
            original_cancel(token)
            with cancellations_lock:
                token_cancellation_events[token_cancellation_count].set()
                token_cancellation_count += 1

        async def scenario() -> CallToolResult:
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
                        patch.object(mcp_server, "_read_item_status", controlled_read),
                        patch.object(executor, "submit", observed_submit),
                        patch.object(diagnostics, "emit", observed_emit),
                        patch.object(mcp_server.CancellationToken, "cancel", observed_cancel),
                    ):
                        active = asyncio.create_task(session.call_tool(mcp_server.ITEM_STATUS_TOOL, arguments))
                        await _wait_for(admitted[0])
                        await _wait_for(running_started)
                        queued = asyncio.create_task(session.call_tool(mcp_server.ITEM_STATUS_TOOL, arguments))
                        await _wait_for(admitted[1])

                        busy = await session.call_tool(mcp_server.ITEM_STATUS_TOOL, arguments)
                        if not isinstance(busy, CallToolResult):
                            raise AssertionError("The saturated tool returned an unexpected MCP result.")

                        queued.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await queued
                        await _wait_for(cancellation_events[0])
                        await _wait_for(executions[1].finished)
                        self.assertFalse(queued_effect.is_set())

                        active.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await active
                        self.assertFalse(cancellation_events[1].is_set())
                        await _wait_for(token_cancellation_events[1])
                        running_release.set()
                        await _wait_for(cancellation_events[1])
                        await _wait_for(executions[0].finished)
                await client_send.aclose()

            self.assertTrue(cooperative_cancelled.is_set())
            replacement = executor.submit(lambda token: token.cancelled)
            self.assertFalse(await replacement.result())
            return busy

        try:
            busy = _run_async(scenario())
        finally:
            running_release.set()
            executor.shutdown()

        self.assertEqual(2, diagnostics_stream.getvalue().count("classification=cancelled"))
        busy_content = busy.structured_content
        self.assertIsInstance(busy_content, dict)
        self.assertEqual("busy", busy_content["status"])
        self.assertEqual("EXECUTOR_BUSY", busy_content["code"])
        self.assertFalse(busy_content["state_changed"])
        self.assertEqual("retry-same-input", busy_content["retry"])
        self.assertIn("classification=busy", diagnostics_stream.getvalue())

    def test_running_mutation_cancellation_waits_for_committed_result(self) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=4)
        diagnostics_stream = io.StringIO()
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(diagnostics_stream, event_limit=16, line_limit=256)
        )
        committed = threading.Event()
        release = threading.Event()
        original_refresh = mcp_server._refresh_affected_views

        def delayed_refresh(
            durable: DurableRoots,
            store: SQLiteWorkStore,
            affected: mcp_server.AffectedViews,
            now: datetime,
        ) -> ViewRefreshResult:
            result = original_refresh(durable, store, affected, now)
            committed.set()
            if not release.wait(2):
                raise AssertionError("The committed mutation was not released.")
            return result

        async def scenario() -> CallToolResult:
            with patch.object(mcp_server, "_refresh_affected_views", delayed_refresh):
                request = asyncio.create_task(
                    server.call_tool(
                        mcp_server.PROPOSAL_CREATE_TOOL,
                        {
                            "project_root": str(project),
                            "work_root": str(roots.work_root),
                            "proposal": proposal_input(),
                            "actor_task_id": "mcp-test-task",
                            "actor_host_id": "local",
                        },
                    )
                )
                await _wait_for(committed)
                request.cancel()
                await asyncio.sleep(0)
                self.assertFalse(request.done())
                release.set()
                result = await request
            if not isinstance(result, CallToolResult):
                raise AssertionError("The mutation tool returned an unexpected MCP result.")
            return result

        try:
            result = _run_async(scenario())
        finally:
            release.set()
            executor.shutdown()

        content = result.structured_content
        self.assertIsInstance(content, dict)
        self.assertEqual("committed", content["status"])
        self.assertTrue(content["state_changed"])
        self.assertIsNotNone(SQLiteWorkStore(roots.database_path).read_item_status(ItemId("proposal-1")))
        diagnostics = diagnostics_stream.getvalue()
        self.assertIn("classification=committed", diagnostics)
        self.assertIn("commit=", diagnostics)

    def test_concurrent_calls_keep_request_and_store_identity_isolated(self) -> None:
        first_temporary, first_project, first_roots = self._project()
        second_temporary, second_project, second_roots = self._project()
        self.addCleanup(first_temporary.cleanup)
        self.addCleanup(second_temporary.cleanup)
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=2)
        diagnostics = mcp_server.Diagnostics(io.StringIO(), event_limit=16, line_limit=256)
        server = mcp_server.create_server(executor, diagnostics)
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
            with patch.object(mcp_server, "compose_store", compose):
                first, second = await asyncio.gather(
                    *(server.call_tool(mcp_server.ITEM_STATUS_TOOL, value) for value in arguments)
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

    def test_structured_rejections_leave_ledger_unchanged_and_duplicate_is_recoverable(self) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=4)
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(io.StringIO(), event_limit=16, line_limit=256)
        )
        store = SQLiteWorkStore(roots.database_path)
        before = store.validated_snapshot()
        invalid = {**proposal_input(), "unexpected": True}
        arguments = {
            "project_root": str(project),
            "work_root": str(roots.work_root),
            "actor_task_id": "mcp-test-task",
            "actor_host_id": "local",
        }

        async def scenario() -> tuple[CallToolResult, stored_state.StoredWorkState, CallToolResult, CallToolResult]:
            rejected = await server.call_tool(mcp_server.PROPOSAL_CREATE_TOOL, {**arguments, "proposal": invalid})
            after_rejection = store.validated_snapshot()
            created = await server.call_tool(
                mcp_server.PROPOSAL_CREATE_TOOL,
                {**arguments, "proposal": proposal_input()},
            )
            duplicate = await server.call_tool(
                mcp_server.PROPOSAL_CREATE_TOOL,
                {**arguments, "proposal": proposal_input()},
            )
            if not isinstance(rejected, CallToolResult):
                raise AssertionError("The mutation tool returned an unexpected MCP result.")
            if not isinstance(created, CallToolResult):
                raise AssertionError("The mutation tool returned an unexpected MCP result.")
            if not isinstance(duplicate, CallToolResult):
                raise AssertionError("The mutation tool returned an unexpected MCP result.")
            return rejected, after_rejection, created, duplicate

        try:
            rejected, after_rejection, created, duplicate = _run_async(scenario())
        finally:
            executor.shutdown()

        rejected_content = rejected.structured_content
        created_content = created.structured_content
        duplicate_content = duplicate.structured_content
        self.assertIsInstance(rejected_content, dict)
        self.assertIsInstance(created_content, dict)
        self.assertIsInstance(duplicate_content, dict)
        self.assertEqual("rejected", rejected_content["status"])
        self.assertFalse(rejected_content["state_changed"])
        self.assertEqual(before, after_rejection)
        self.assertEqual("committed", created_content["status"])
        self.assertEqual("PROPOSAL_ALREADY_EXISTS", duplicate_content["code"])
        self.assertEqual("do-not-retry", duplicate_content["retry"])
        self.assertIn("Read item status", duplicate_content["recovery"])
        self.assertFalse(duplicate_content["state_changed"])

    def test_invalid_actor_identity_is_structured_and_leaves_fresh_store_unchanged(self) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=4)
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(io.StringIO(), event_limit=16, line_limit=256)
        )
        before = SQLiteWorkStore(roots.database_path).validated_snapshot()

        async def scenario() -> tuple[CallToolResult, CallToolResult]:
            common = {
                "project_root": str(project),
                "work_root": str(roots.work_root),
                "proposal": proposal_input(),
            }
            empty = await server.call_tool(
                mcp_server.PROPOSAL_CREATE_TOOL,
                {**common, "actor_task_id": "", "actor_host_id": "local"},
            )
            malformed = await server.call_tool(
                mcp_server.PROPOSAL_CREATE_TOOL,
                {**common, "actor_task_id": "mcp-test-task", "actor_host_id": "local/host"},
            )
            if not isinstance(empty, CallToolResult) or not isinstance(malformed, CallToolResult):
                raise AssertionError("The mutation tool returned an unexpected MCP result.")
            return empty, malformed

        try:
            results = _run_async(scenario())
        finally:
            executor.shutdown()

        for result in results:
            content = result.structured_content
            self.assertIsInstance(content, dict)
            self.assertEqual("rejected", content["status"])
            self.assertEqual("PROPOSAL_INVALID", content["code"])
            self.assertFalse(content["state_changed"])
            self.assertEqual("unchanged", content["effect"])
            self.assertEqual("correct-input", content["retry"])
            self.assertEqual([], content["changed_surfaces"])
        self.assertEqual(before, SQLiteWorkStore(roots.database_path).validated_snapshot())

    def test_invalid_roots_are_rejected_before_durable_state_resolution(self) -> None:
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=4)
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(io.StringIO(), event_limit=16, line_limit=256)
        )
        requests = (
            (
                mcp_server.ITEM_STATUS_TOOL,
                {"project_root": "/project", "work_root": "/work", "item_id": "work-a"},
                "ITEM_STATUS_INVALID",
            ),
            (
                mcp_server.PROPOSAL_CREATE_TOOL,
                {
                    "project_root": "/project",
                    "work_root": "/work",
                    "proposal": proposal_input(),
                    "actor_task_id": "mcp-test-task",
                    "actor_host_id": "local",
                },
                "PROPOSAL_INVALID",
            ),
            (
                mcp_server.BRIEF_PUBLISH_TOOL,
                {
                    "project_root": "/project",
                    "work_root": "/work",
                    "brief": msgspec.to_builtins(example_work_brief()),
                },
                "WORK_BRIEF_INVALID",
            ),
        )

        async def scenario() -> None:
            for tool_name, arguments, expected_code in requests:
                for field in ("project_root", "work_root"):
                    for invalid_root in ("", "\x00", "/project/\x00child"):
                        result = await server.call_tool(tool_name, {**arguments, field: invalid_root})
                        if not isinstance(result, CallToolResult):
                            raise AssertionError("The invalid request returned an unexpected MCP result.")
                        content = result.structured_content
                        self.assertIsInstance(content, dict)
                        self.assertEqual("rejected", content["status"])
                        self.assertEqual(expected_code, content["code"])
                        self.assertFalse(content["state_changed"])

        try:
            with patch.object(mcp_server, "_resolve_durable") as resolve_durable:
                _run_async(scenario())
            resolve_durable.assert_not_called()
        finally:
            executor.shutdown()

    def test_post_commit_reply_loss_preserves_proposal_and_brief_for_fresh_store_recovery(self) -> None:
        for operation in (mcp_server.PROPOSAL_CREATE_TOOL, mcp_server.BRIEF_PUBLISH_TOOL):
            with self.subTest(operation=operation):
                temporary, project, roots = self._project()
                self.addCleanup(temporary.cleanup)
                executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=4)
                server = mcp_server.create_server(
                    executor,
                    mcp_server.Diagnostics(io.StringIO(), event_limit=16, line_limit=256),
                )
                if operation == mcp_server.PROPOSAL_CREATE_TOOL:
                    arguments = {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "proposal": proposal_input(),
                        "actor_task_id": "mcp-test-task",
                        "actor_host_id": "local",
                    }
                else:
                    arguments = {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "brief": msgspec.to_builtins(example_work_brief()),
                    }
                with (
                    patch.object(mcp_server, "_refresh_affected_views", side_effect=RuntimeError("reply lost")),
                    self.assertRaises(UnexpectedToolError),
                ):
                    _run_async(server.call_tool(operation, arguments))
                executor.shutdown()

                reopened = SQLiteWorkStore(roots.database_path)
                if operation == mcp_server.PROPOSAL_CREATE_TOOL:
                    self.assertIsNotNone(reopened.read_item_status(ItemId("proposal-1")))
                else:
                    reference = reopened.read_artifact_reference(
                        kind=work_models.ArtifactKind.BRIEF,
                        key="make-canonical-briefs-typed-json-1",
                        revision=1,
                    )
                    self.assertIsNotNone(reference)

    def test_brief_acceptance_failure_reports_discoverable_published_artifact(self) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=4)
        diagnostics_stream = io.StringIO()
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(diagnostics_stream, event_limit=16, line_limit=256)
        )
        brief = example_work_brief()
        references_before = SQLiteWorkStore(roots.database_path).validated_snapshot().artifact_references

        with patch.object(
            SQLiteWorkStore,
            "accept_artifact_reference",
            side_effect=WorkStoreError("database unavailable"),
        ):
            result = _run_async(
                server.call_tool(
                    mcp_server.BRIEF_PUBLISH_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "brief": msgspec.to_builtins(brief),
                    },
                )
            )
        executor.shutdown()

        if not isinstance(result, CallToolResult):
            raise AssertionError("The mutation tool returned an unexpected MCP result.")
        content = result.structured_content
        self.assertIsInstance(content, dict)
        self.assertEqual("failed-after-publication", content["status"])
        self.assertEqual("ARTIFACT_ACCEPTANCE_FAILED", content["code"])
        self.assertTrue(content["state_changed"])
        self.assertEqual("committed", content["effect"])
        self.assertEqual("do-not-retry", content["retry"])
        self.assertEqual(["immutable-artifact"], content["changed_surfaces"])
        selector = content["published_selector"]
        self.assertIsInstance(selector, str)
        self.assertEqual(canonical_work_brief_bytes(brief), (roots.work_root / selector).read_bytes())
        self.assertEqual(
            references_before,
            SQLiteWorkStore(roots.database_path).validated_snapshot().artifact_references,
        )
        diagnostics = diagnostics_stream.getvalue()
        self.assertIn("classification=infrastructure-failure", diagnostics)
        self.assertIn(f"commit={selector}", diagnostics)

    def test_view_refresh_failure_reports_committed_warning_and_rebuild_recovery(self) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=4)
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(io.StringIO(), event_limit=16, line_limit=256)
        )
        warning = ViewRefreshResult(
            12,
            ViewWarning("Generated views need repair.", "Run 'pinboard views rebuild'."),
        )
        with patch.object(mcp_server, "_refresh_affected_views", return_value=warning):
            result = _run_async(
                server.call_tool(
                    mcp_server.PROPOSAL_CREATE_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "proposal": proposal_input(),
                        "actor_task_id": "mcp-test-task",
                        "actor_host_id": "local",
                    },
                )
            )
        executor.shutdown()
        if not isinstance(result, CallToolResult):
            raise AssertionError("The mutation tool returned an unexpected MCP result.")
        content = result.structured_content
        self.assertIsInstance(content, dict)
        self.assertEqual("committed-with-warning", content["status"])
        self.assertEqual("do-not-retry", content["retry"])
        warning_content = content["warning"]
        self.assertIsInstance(warning_content, dict)
        self.assertEqual("Run 'pinboard views rebuild'.", warning_content["recovery"])
        self.assertIsNotNone(SQLiteWorkStore(roots.database_path).read_item_status(ItemId("proposal-1")))

    def test_server_rejects_an_internal_result_that_violates_its_advertised_contract(self) -> None:
        executor = mcp_server.BoundedExecutor(worker_count=1, unfinished_limit=1)
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(io.StringIO(), event_limit=8, line_limit=256)
        )

        def contradictory_result(
            _project_root: str,
            _work_root: str,
            _item_id: str,
            _token: mcp_server.CancellationToken,
        ) -> mcp_server.OperationResult:
            return mcp_server.OperationResult(
                {
                    "schema": "pinboard-mcp-execution-result/v1",
                    "status": "busy",
                    "code": "EXECUTOR_BUSY",
                    "message": "Busy.",
                    "state_changed": True,
                    "effect": "unchanged",
                    "retry": "retry-same-input",
                    "changed_surfaces": [],
                    "observed": [],
                    "mismatches": [],
                },
                "busy",
                None,
            )

        try:
            with (
                patch.object(mcp_server, "_read_item_status", contradictory_result),
                self.assertRaises(UnexpectedToolError),
            ):
                _run_async(
                    server.call_tool(
                        mcp_server.ITEM_STATUS_TOOL,
                        {"project_root": "/project", "work_root": "/work", "item_id": "item"},
                    )
                )
        finally:
            executor.shutdown()

    def test_sdk_stdio_negotiates_discovers_reads_and_rejects_unknown_tool(self) -> None:  # noqa: PLR0915
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        transport_errors: list[Exception] = []

        async def record_transport_error(message: IncomingMessage) -> None:
            if isinstance(message, Exception):
                transport_errors.append(message)

        async def scenario(
            diagnostics: io.TextIOWrapper,
        ) -> tuple[
            CallToolResult,
            tuple[Tool, ...],
            CallToolResult,
            CallToolResult,
            CallToolResult,
            CallToolResult,
        ]:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "pinboard.mcp"],
                cwd=Path.cwd(),
            )
            async with (
                stdio_client(parameters, errlog=diagnostics) as streams,
                ClientSession(*streams, message_handler=record_transport_error) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool(
                    mcp_server.ITEM_STATUS_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "item_id": "work-a",
                    },
                )
                rejected = await session.call_tool("unsupported_tool", {})
                invalid = await session.call_tool(
                    mcp_server.ITEM_STATUS_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "item_id": "",
                    },
                )
                missing = await session.call_tool(
                    mcp_server.ITEM_STATUS_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "item_id": "missing-item",
                    },
                )
                unknown_field = await session.call_tool(
                    mcp_server.ITEM_STATUS_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "item_id": "work-a",
                        "unknown": True,
                    },
                )
                return result, tuple(tools.tools), rejected, invalid, missing, unknown_field

        with tempfile.TemporaryFile(mode="w+") as diagnostics:
            result, tools, rejected, invalid, missing, unknown_field = _run_async(scenario(diagnostics))
            diagnostics.seek(0)
            stderr = diagnostics.read()
        self.assertEqual(
            {mcp_server.ITEM_STATUS_TOOL, mcp_server.PROPOSAL_CREATE_TOOL, mcp_server.BRIEF_PUBLISH_TOOL},
            {tool.name for tool in tools},
        )
        tools_by_name = {tool.name: tool for tool in tools}
        expected_required = {
            mcp_server.ITEM_STATUS_TOOL: {"project_root", "work_root", "item_id"},
            mcp_server.PROPOSAL_CREATE_TOOL: {
                "project_root",
                "work_root",
                "proposal",
                "actor_task_id",
                "actor_host_id",
            },
            mcp_server.BRIEF_PUBLISH_TOOL: {"project_root", "work_root", "brief"},
        }
        for tool_name, required in expected_required.items():
            tool = tools_by_name[tool_name]
            self.assertFalse(tool.input_schema["additionalProperties"])
            self.assertEqual(required, set(tool.input_schema["required"]))
            self.assertEqual(1, tool.input_schema["properties"]["project_root"]["minLength"])
            self.assertEqual(1, tool.input_schema["properties"]["work_root"]["minLength"])
            self.assertEqual(r"\A[^\x00]+\z", tool.input_schema["properties"]["project_root"]["pattern"])
            self.assertEqual(r"\A[^\x00]+\z", tool.input_schema["properties"]["work_root"]["pattern"])
            self.assertIn("$defs", tool.input_schema)
            self.assertIsNotNone(tool.output_schema)
            assert tool.output_schema is not None
            self.assertIn("anyOf", tool.output_schema)
            self.assertIn("$defs", tool.output_schema)
        item_schema = tools_by_name[mcp_server.ITEM_STATUS_TOOL].input_schema
        self.assertEqual(r"\A(?!\.{1,2}\z)[^/\r\n\x00]+\z", item_schema["properties"]["item_id"]["pattern"])
        proposal_schema = tools_by_name[mcp_server.PROPOSAL_CREATE_TOOL].input_schema
        self.assertEqual("#/$defs/Proposal", proposal_schema["properties"]["proposal"]["$ref"])
        self.assertFalse(proposal_schema["$defs"]["Proposal"]["additionalProperties"])
        brief_schema = tools_by_name[mcp_server.BRIEF_PUBLISH_TOOL].input_schema
        self.assertEqual("#/$defs/WorkBrief", brief_schema["properties"]["brief"]["$ref"])
        self.assertFalse(brief_schema["$defs"]["WorkBrief"]["additionalProperties"])
        brief_output_schema = tools_by_name[mcp_server.BRIEF_PUBLISH_TOOL].output_schema
        assert brief_output_schema is not None
        reference_schema = brief_output_schema["$defs"]["ArtifactReferenceResult"]
        self.assertEqual(1, reference_schema["properties"]["artifact_ref_id"]["minimum"])
        self.assertEqual(1, reference_schema["properties"]["revision"]["minimum"])
        self.assertEqual(1, reference_schema["properties"]["size_bytes"]["minimum"])
        self.assertEqual(r"\A[0-9a-f]{64}\z", reference_schema["properties"]["sha256"]["pattern"])
        unchanged_schema = brief_output_schema["$defs"]["BriefUnchanged"]
        self.assertFalse(unchanged_schema["properties"]["state_changed"]["const"])
        self.assertEqual(["unchanged"], unchanged_schema["properties"]["effect"]["enum"])
        self.assertEqual(0, unchanged_schema["properties"]["changed_surfaces"]["maxItems"])
        committed_schema = brief_output_schema["$defs"]["BriefCommitted"]
        self.assertTrue(committed_schema["properties"]["state_changed"]["const"])
        self.assertEqual(3, committed_schema["properties"]["changed_surfaces"]["minItems"])
        self.assertEqual({"type": "null"}, committed_schema["properties"]["warning"])
        self.assertEqual(self._expected_bytes(roots, "work-a"), msgspec.json.encode(result.structured_content))
        self.assertTrue(rejected.is_error)
        self.assertEqual(1, len(rejected.content))
        self.assertIsInstance(rejected.content[0], TextContent)
        self.assertIn("Unknown tool", rejected.content[0].text)
        for failure, code in ((invalid, "ITEM_STATUS_INVALID"), (missing, "ITEM_NOT_FOUND")):
            self.assertFalse(failure.is_error)
            content = failure.structured_content
            self.assertIsInstance(content, dict)
            self.assertEqual("rejected", content["status"])
            self.assertEqual(code, content["code"])
            self.assertFalse(content["state_changed"])
            self.assertEqual([], content["changed_surfaces"])
        self.assertTrue(unknown_field.is_error)
        self.assertLessEqual(len(stderr.encode()), 2_048)
        self.assertNotIn(str(project), stderr)
        self.assertNotIn("work-a", stderr)
        self.assertIn("classification=ok", stderr)
        self.assertEqual([], transport_errors)

    def test_sdk_stdio_creates_proposal_and_publishes_idempotent_brief(self) -> None:  # noqa: PLR0915 - one installed transport journey
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        proposal = proposal_input()
        brief_value = example_work_brief()
        brief = msgspec.to_builtins(brief_value)
        self.assertIsInstance(brief, dict)

        async def scenario(diagnostics: io.TextIOWrapper) -> tuple[CallToolResult, CallToolResult, CallToolResult]:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "pinboard.mcp"],
                cwd=Path.cwd(),
            )
            async with (
                stdio_client(parameters, errlog=diagnostics) as streams,
                ClientSession(*streams) as session,
            ):
                await session.initialize()
                created = await session.call_tool(
                    mcp_server.PROPOSAL_CREATE_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "proposal": proposal,
                        "actor_task_id": "mcp-test-task",
                        "actor_host_id": "local",
                    },
                )
                published = await session.call_tool(
                    mcp_server.BRIEF_PUBLISH_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "brief": brief,
                    },
                )
                repeated = await session.call_tool(
                    mcp_server.BRIEF_PUBLISH_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "brief": brief,
                    },
                )
                repeated_content = repeated.structured_content
                if not isinstance(repeated_content, dict):
                    raise AssertionError("The repeated publication did not return structured content.")
                contradictory = CallToolResult(
                    content=[],
                    structured_content={**repeated_content, "effect": "committed"},
                )
                with self.assertRaisesRegex(RuntimeError, "Invalid structured content"):
                    await session.validate_tool_result(mcp_server.BRIEF_PUBLISH_TOOL, contradictory)
                return created, published, repeated

        with tempfile.TemporaryFile(mode="w+") as diagnostics:
            created, published, repeated = _run_async(scenario(diagnostics))
            diagnostics.seek(0)
            stderr = diagnostics.read()

        created_content = created.structured_content
        published_content = published.structured_content
        repeated_content = repeated.structured_content
        self.assertIsInstance(created_content, dict)
        self.assertIsInstance(published_content, dict)
        self.assertIsInstance(repeated_content, dict)
        self.assertEqual("committed", created_content["status"])
        self.assertEqual("proposal-1", created_content["proposal_id"])
        self.assertEqual("committed", published_content["status"])
        self.assertTrue(published_content["state_changed"])
        self.assertEqual("committed", published_content["effect"])
        self.assertNotEqual([], published_content["changed_surfaces"])
        self.assertEqual("do-not-retry", published_content["retry"])
        self.assertEqual("unchanged", repeated_content["status"])
        self.assertFalse(repeated_content["state_changed"])
        self.assertEqual("unchanged", repeated_content["effect"])
        self.assertEqual([], repeated_content["changed_surfaces"])
        self.assertEqual("retry-same-input", repeated_content["retry"])
        self.assertEqual(published_content["reference"], repeated_content["reference"])

        reopened = SQLiteWorkStore(roots.database_path)
        status = reopened.read_item_status(ItemId("proposal-1"))
        self.assertIsNotNone(status)
        reference_content = published_content["reference"]
        self.assertIsInstance(reference_content, dict)
        reference = reopened.read_artifact_reference_by_id(ArtifactRefId(reference_content["artifact_ref_id"]))
        self.assertIsNotNone(reference)
        assert reference is not None
        self.assertEqual(canonical_work_brief_bytes(brief_value), ArtifactRepository(roots).read(reference))
        self.assertIn(f"operation={mcp_server.PROPOSAL_CREATE_TOOL}", stderr)
        self.assertIn(f"operation={mcp_server.BRIEF_PUBLISH_TOOL}", stderr)
        self.assertIn("classification=unchanged", stderr)
        self.assertIn("duration_ms=", stderr)
        self.assertIn("commit=", stderr)
        self.assertNotIn("proposal-1", stderr)
        self.assertNotIn(brief_value.title, stderr)


if __name__ == "__main__":
    unittest.main()
