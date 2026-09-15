"""Test-only MCP stdio and bounded-executor proof.

This module is deliberately outside ``src`` and is not installed. It exercises a
possible agent transport without adding a production MCP boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TextIO

import anyio
import msgspec
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from pinboard.application import queries, query_models
from pinboard.cli import cli_commands
from pinboard.cli.work_state_commands import compose_store, resolve_durable_layout, resolve_roots
from pinboard.domain.errors import DecisionFailure

TOOL_NAME = "pinboard_item_status"
THREAD_NAME_PREFIX = "pinboard-mcp-worker"

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


class ExecutorBusy(RuntimeError):
    """The bounded executor rejected work before its callback began."""


class ExecutorClosed(RuntimeError):
    """The bounded executor no longer accepts work."""


class OperationCancelled(RuntimeError):
    """A running callback observed its request-local cancellation token."""


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._cancelled.set()

    def checkpoint(self) -> None:
        if self.cancelled:
            raise OperationCancelled("The request was cancelled at a cooperative checkpoint.")


class Execution[Result]:
    def __init__(
        self,
        future: Future[Result],
        token: CancellationToken,
        finished: threading.Event,
    ) -> None:
        self._future = future
        self._token = token
        self.finished = finished

    async def result(self) -> Result:
        try:
            return await asyncio.wrap_future(self._future)
        except asyncio.CancelledError:
            self._token.cancel()
            self._future.cancel()
            raise


class BoundedExecutor:
    """A fixed worker pool whose semaphore bounds every unfinished callback."""

    def __init__(self, *, worker_count: int, unfinished_limit: int) -> None:
        if worker_count < 1 or unfinished_limit < worker_count:
            raise ValueError("The unfinished-work limit must cover at least one positive worker set.")
        self._pool = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix=THREAD_NAME_PREFIX)
        self._admission = threading.BoundedSemaphore(unfinished_limit)
        self._lock = threading.Lock()
        self._closed = False

    def submit[Result](self, callback: Callable[[CancellationToken], Result]) -> Execution[Result]:
        with self._lock:
            if self._closed:
                raise ExecutorClosed("The executor is closed.")
            if not self._admission.acquire(blocking=False):
                raise ExecutorBusy("The executor has reached its unfinished-work limit.")
            token = CancellationToken()
            finished = threading.Event()
            try:
                future = self._pool.submit(callback, token)
            except BaseException:
                self._admission.release()
                raise

            def release(_future: Future[Result]) -> None:
                self._admission.release()
                finished.set()

            future.add_done_callback(release)
            return Execution(future, token, finished)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._pool.shutdown(wait=True, cancel_futures=True)


class Diagnostics:
    """Emit a fixed number of short, metadata-only stderr records."""

    def __init__(self, stream: TextIO, *, event_limit: int, line_limit: int) -> None:
        if event_limit < 1 or line_limit < 2:
            raise ValueError("Diagnostic bounds must be positive.")
        self._stream = stream
        self._event_limit = event_limit
        self._line_limit = line_limit
        self._events = 0
        self._lock = threading.Lock()

    def emit(
        self,
        *,
        event: str,
        request_id: int | None,
        project_id: str | None,
        classification: str | None,
    ) -> None:
        fields = ["pinboard_mcp", f"event={event}"]
        if request_id is not None:
            fields.append(f"request_id={request_id}")
        if project_id is not None:
            fields.append(f"project={project_id}")
        if classification is not None:
            fields.append(f"classification={classification}")
        line = " ".join(fields)
        with self._lock:
            if self._events >= self._event_limit:
                return
            self._events += 1
            self._stream.write(line[: self._line_limit - 1] + "\n")
            self._stream.flush()


def _item_status_json(status: query_models.ItemStatus) -> dict[str, JsonValue]:
    preparation = status.preparation
    return {
        "schema": status.schema,
        "authority": status.authority,
        "revision": status.revision,
        "item_id": status.item_id,
        "label": status.label,
        "state": status.state.value,
        "timing": None if status.timing is None else status.timing.value,
        "outcome_evidence": status.outcome_evidence,
        "next_action": status.next_action,
        "source": status.source,
        "notes": status.notes,
        "queue_position": status.queue_position,
        "attempts": [
            {
                "attempt_id": attempt.attempt_id,
                "state": attempt.state.value,
                "candidate_revision": attempt.candidate_revision,
            }
            for attempt in status.attempts
        ],
        "preparation": (
            None
            if preparation is None
            else {
                "definition_revision": preparation.definition_revision,
                "definition_digest": preparation.definition_digest,
                "task_id": preparation.task_id,
                "host_id": preparation.host_id,
                "lease_id": preparation.lease_id,
                "generation": preparation.generation,
                "expires_at": preparation.expires_at,
                "status": preparation.status.value,
            }
        ),
    }


def _read_item_status(
    project_root: str,
    work_root: str,
    item_id: str,
    token: CancellationToken,
) -> dict[str, JsonValue]:
    token.checkpoint()
    try:
        command = msgspec.convert({"item_id": item_id}, type=cli_commands.ItemStatusCommand, strict=True)
    except msgspec.ValidationError as error:
        raise ToolError(f"Invalid item status input: {error}") from error
    roots = resolve_roots(cli_commands.RootSelection(Path(project_root), Path(work_root)))
    durable = resolve_durable_layout(roots)
    token.checkpoint()
    projected = queries.project_item_status(compose_store(durable), command.item_id, datetime.now(UTC))
    if isinstance(projected, DecisionFailure):
        raise ToolError(projected.message)
    token.checkpoint()
    return _item_status_json(projected)


def create_server(executor: BoundedExecutor, diagnostics: Diagnostics) -> MCPServer:
    server = MCPServer("pinboard-mcp-prototype", version="0", log_level="ERROR")
    request_ids = itertools.count(1)

    @server.tool(
        name=TOOL_NAME,
        description="Read one current Pinboard item status from an explicit local project and work root.",
    )
    async def item_status(project_root: str, work_root: str, item_id: str) -> dict[str, JsonValue]:
        request_id = next(request_ids)
        project_id = hashlib.sha256(project_root.encode()).hexdigest()[:12]
        try:
            execution = executor.submit(partial(_read_item_status, project_root, work_root, item_id))
        except ExecutorBusy as error:
            diagnostics.emit(
                event="result",
                request_id=request_id,
                project_id=project_id,
                classification="busy",
            )
            raise ToolError("The prototype executor is busy; retry after current work finishes.") from error
        try:
            result = await execution.result()
        except asyncio.CancelledError:
            diagnostics.emit(
                event="result",
                request_id=request_id,
                project_id=project_id,
                classification="cancelled",
            )
            raise
        except OperationCancelled as error:
            diagnostics.emit(
                event="result",
                request_id=request_id,
                project_id=project_id,
                classification="cancelled",
            )
            raise ToolError("The request was cancelled at a cooperative checkpoint.") from error
        except ToolError:
            diagnostics.emit(
                event="result",
                request_id=request_id,
                project_id=project_id,
                classification="rejected",
            )
            raise
        diagnostics.emit(
            event="result",
            request_id=request_id,
            project_id=project_id,
            classification="ok",
        )
        return result

    return server


def main() -> None:
    executor = BoundedExecutor(worker_count=2, unfinished_limit=4)
    diagnostics = Diagnostics(sys.stderr, event_limit=32, line_limit=256)
    diagnostics.emit(event="startup", request_id=None, project_id=None, classification="ready")
    try:
        anyio.run(create_server(executor, diagnostics).run_stdio_async)
    finally:
        executor.shutdown()


if __name__ == "__main__":
    main()
