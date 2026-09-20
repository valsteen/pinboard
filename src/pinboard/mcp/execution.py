"""Bounded MCP execution and request completion; no Pinboard use-case policy."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TextIO

from mcp.server.mcpserver.exceptions import ToolError

from pinboard import __version__
from pinboard.domain.errors import (
    EffectDisposition,
    RetryDisposition,
)
from pinboard.mcp import contract_schemas
from pinboard.mcp.contracts import JsonValue

THREAD_NAME_PREFIX = "pinboard-mcp-worker"


@dataclass(frozen=True, slots=True)
class OperationResult:
    content: dict[str, JsonValue]
    classification: str
    commit_reference: str | None


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
        self.finished: threading.Event = finished

    async def result(self) -> Result:
        try:
            return await asyncio.wrap_future(self._future)
        except asyncio.CancelledError:
            self._token.cancel()
            if self._future.cancel():
                raise
            return await asyncio.wrap_future(self._future)


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
        operation: str | None,
        project_id: str | None,
        duration_ms: int | None,
        classification: str | None,
        commit_reference: str | None,
    ) -> None:
        fields = ["pinboard_mcp", f"version={__version__}", f"event={event}"]
        if request_id is not None:
            fields.append(f"request_id={request_id}")
        if operation is not None:
            fields.append(f"operation={operation}")
        if project_id is not None:
            fields.append(f"project={project_id}")
        if duration_ms is not None:
            fields.append(f"duration_ms={duration_ms}")
        if classification is not None:
            fields.append(f"classification={classification}")
        if commit_reference is not None:
            fields.append(f"commit={commit_reference}")
        line = " ".join(fields)
        with self._lock:
            if self._events >= self._event_limit:
                return
            self._events += 1
            self._stream.write(line[: self._line_limit - 1] + "\n")
            self._stream.flush()


async def _run_request(
    executor: BoundedExecutor,
    diagnostics: Diagnostics,
    request_id: int,
    operation: str,
    project_root: str,
    callback: Callable[[CancellationToken], OperationResult],
) -> dict[str, JsonValue]:
    project_id = hashlib.sha256(project_root.encode()).hexdigest()[:12]
    started = time.monotonic_ns()

    def emit(classification: str, commit_reference: str | None) -> None:
        diagnostics.emit(
            event="result",
            request_id=request_id,
            operation=operation,
            project_id=project_id,
            duration_ms=(time.monotonic_ns() - started) // 1_000_000,
            classification=classification,
            commit_reference=commit_reference,
        )

    try:
        execution = executor.submit(callback)
    except ExecutorBusy:
        emit("busy", None)
        busy: dict[str, JsonValue] = {
            "schema": "pinboard-mcp-execution-result/v1",
            "status": "busy",
            "code": "EXECUTOR_BUSY",
            "message": "The MCP executor is busy; retry after current work finishes.",
            "state_changed": False,
            "effect": EffectDisposition.UNCHANGED.value,
            "retry": RetryDisposition.RETRY_SAME_INPUT.value,
            "changed_surfaces": [],
            "observed": [],
            "mismatches": [],
        }
        return contract_schemas.validate_result(operation, busy)
    try:
        result = await execution.result()
    except asyncio.CancelledError:
        emit("cancelled", None)
        raise
    except OperationCancelled as error:
        emit("cancelled", None)
        raise ToolError("The request was cancelled at a cooperative checkpoint.") from error
    except ToolError:
        emit("rejected", None)
        raise
    except Exception:
        emit("error", None)
        raise
    emit(result.classification, result.commit_reference)
    return contract_schemas.validate_result(operation, result.content)
