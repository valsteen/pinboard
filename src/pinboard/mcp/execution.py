"""Bounded MCP execution and request completion; no Pinboard use-case policy."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO

import msgspec
from mcp.server.mcpserver.exceptions import ToolError

from pinboard import __version__
from pinboard.adapters.files import contributor_traces
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode, ImmutableFilePublishedError
from pinboard.adapters.files.file_io import create_immutable
from pinboard.adapters.sqlite.errors import StorageError
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


class CapturedValueIdentity(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    sha256: str
    size_bytes: int


class CapturedResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    availability: Literal["available"]
    value: dict[str, JsonValue]
    identity: CapturedValueIdentity


class UnavailableCapturedResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    availability: Literal["unavailable"]
    reason: Literal["interrupted", "callback-rejected", "callback-error", "result-validation-error"]
    classification: str
    commit_reference: str | None


type InvocationCaptureResult = CapturedResult | UnavailableCapturedResult


class McpInvocationCapture(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-invocation-capture/v1"]
    capture_id: str
    operation: str
    request: dict[str, JsonValue]
    request_identity: CapturedValueIdentity
    result: InvocationCaptureResult
    transport_bytes: Literal["unavailable"]
    pre_callback_events: Literal["unavailable"]
    accepted_evidence: Literal[False]


class SemanticCapture:
    """Publish exact decoded MCP request and validated result values to a selected private directory."""

    def __init__(self, directory: Path, *, automatic: bool = False) -> None:
        try:
            selected = directory.resolve(strict=True)
        except OSError as error:
            raise ValueError(f"Capture directory could not be verified: {directory}") from error
        if not selected.is_dir():
            raise ValueError(f"Capture destination must be an existing directory: {directory}")
        probe = selected / f".pinboard-capture-probe-{secrets.token_hex(16)}"
        try:
            create_immutable(probe, b"")
        except ImmutableFilePublishedError as error:
            with suppress(OSError):
                error.path.unlink()
            raise ValueError(f"Capture directory could not be synchronized: {directory}") from error
        except FileIOError as error:
            raise ValueError(f"Capture directory is not writable: {directory}") from error
        try:
            probe.unlink()
        except OSError as error:
            raise ValueError(f"Capture directory preflight could not be removed: {directory}") from error
        self._directory = selected
        self._automatic = automatic

    @staticmethod
    def _identity(content: bytes) -> CapturedValueIdentity:
        return CapturedValueIdentity(hashlib.sha256(content).hexdigest(), len(content))

    def _publish(
        self,
        operation: str,
        arguments: dict[str, JsonValue],
        result: InvocationCaptureResult,
    ) -> None:
        capture_id = secrets.token_hex(16)
        request_bytes = msgspec.json.encode(arguments)
        record = McpInvocationCapture(
            "pinboard-mcp-invocation-capture/v1",
            capture_id,
            operation,
            arguments,
            self._identity(request_bytes),
            result,
            "unavailable",
            "unavailable",
            False,
        )
        content = msgspec.json.encode(record) + b"\n"
        identity = self._identity(content)
        prefix = "pinboard-auto-mcp" if self._automatic else "pinboard-mcp"
        path = self._directory / f"{prefix}-{capture_id}-{identity.sha256}-{identity.size_bytes}.json"
        create_immutable(path, content)
        if self._automatic:
            try:
                contributor_traces.prune_traces(self._directory)
            except OSError as error:
                raise ImmutableFilePublishedError(
                    path,
                    FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, "Automatic trace retention cleanup failed."),
                ) from error

    def available(
        self,
        operation: str,
        arguments: dict[str, JsonValue],
        result: dict[str, JsonValue],
    ) -> None:
        content = msgspec.json.encode(result)
        self._publish(operation, arguments, CapturedResult("available", result, self._identity(content)))

    def unavailable(
        self,
        operation: str,
        arguments: dict[str, JsonValue],
        reason: Literal["interrupted", "callback-rejected", "callback-error", "result-validation-error"],
        classification: str,
        commit_reference: str | None,
    ) -> None:
        self._publish(
            operation,
            arguments,
            UnavailableCapturedResult("unavailable", reason, classification, commit_reference),
        )


class AutomaticCapture:
    """Resolve the current project and item mode before every MCP callback."""

    def __init__(self, select_item: Callable[[Path, str | None, dict[str, JsonValue]], str | None]) -> None:
        self._select_item = select_item

    def resolve(self, project_root: str, arguments: dict[str, JsonValue]) -> SemanticCapture | None:
        request = arguments.get("request")
        selected = request if isinstance(request, dict) else arguments
        work_root = selected.get("work_root")
        try:
            state = contributor_traces.read_project_trace_settings(Path(project_root))
            if state is None:
                return None
            data_root, resolution = state
            settings = resolution.value
            item_id = (
                self._select_item(data_root.parent, work_root if isinstance(work_root, str) else None, arguments)
                if settings.item_overrides
                else None
            )
            directory = contributor_traces.automatic_trace_directory(data_root, settings, item_id)
            return None if directory is None else SemanticCapture(directory, automatic=True)
        except (ValueError, OSError, FileIOError, StorageError) as error:
            raise ToolError(
                "Automatic Pinboard trace settings or destination are unavailable; the target did not run."
            ) from error


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
    """Emit fixed bounded channels of short, metadata-only stderr records."""

    def __init__(self, stream: TextIO, *, event_limit: int, line_limit: int) -> None:
        if event_limit < 1 or line_limit < 2:
            raise ValueError("Diagnostic bounds must be positive.")
        self._stream = stream
        self._event_limit = event_limit
        self._line_limit = line_limit
        self._events = 0
        self._capture_failure_events = 0
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
        capture_selector: str | None,
    ) -> None:
        fields = ["pinboard_mcp", f"version={__version__}", f"event={event}"]
        if capture_selector is not None:
            fields.append(f"capture_selector={capture_selector}")
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
            capture_failure = event in {"capture-unavailable", "capture-committed-with-warning"}
            event_count = self._capture_failure_events if capture_failure else self._events
            if event_count >= self._event_limit:
                return
            if capture_failure:
                self._capture_failure_events += 1
            else:
                self._events += 1
            self._stream.write(line[: self._line_limit - 1] + "\n")
            self._stream.flush()


async def _run_request(  # noqa: C901 - one execution boundary owns callback and capture aftermath
    executor: BoundedExecutor,
    diagnostics: Diagnostics,
    request_id: int,
    operation: str,
    project_root: str,
    callback: Callable[[CancellationToken], OperationResult],
    *,
    arguments: dict[str, JsonValue],
    capture: SemanticCapture | AutomaticCapture | None,
) -> dict[str, JsonValue]:
    if isinstance(capture, AutomaticCapture):
        capture = capture.resolve(project_root, arguments)
    captured_arguments = deepcopy(arguments) if capture is not None else arguments
    project_id = hashlib.sha256(project_root.encode()).hexdigest()[:12]
    started = time.monotonic_ns()

    def emit_request_event(event: str, classification: str, commit_reference: str | None) -> None:
        diagnostics.emit(
            event=event,
            request_id=request_id,
            operation=operation,
            project_id=project_id,
            duration_ms=(time.monotonic_ns() - started) // 1_000_000,
            classification=classification,
            commit_reference=commit_reference,
            capture_selector=None,
        )

    def capture_effect(
        effect: Callable[[SemanticCapture], None],
        classification: str,
        commit_reference: str | None,
    ) -> None:
        if capture is None:
            return
        try:
            effect(capture)
        except ImmutableFilePublishedError as error:
            diagnostics.emit(
                event="capture-committed-with-warning",
                request_id=None,
                operation=None,
                project_id=None,
                duration_ms=None,
                classification=classification,
                commit_reference=commit_reference,
                capture_selector=error.path.name,
            )
        except FileIOError:
            emit_request_event("capture-unavailable", classification, commit_reference)

    try:
        execution = executor.submit(callback)
    except ExecutorBusy:
        emit_request_event("result", "busy", None)
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
        validated_busy = contract_schemas.validate_result(operation, busy)
        capture_effect(
            lambda selected: selected.available(operation, captured_arguments, validated_busy),
            "busy",
            None,
        )
        return validated_busy
    try:
        result = await execution.result()
    except asyncio.CancelledError:
        capture_effect(
            lambda selected: selected.unavailable(operation, captured_arguments, "interrupted", "cancelled", None),
            "cancelled",
            None,
        )
        emit_request_event("result", "cancelled", None)
        raise
    except OperationCancelled as error:
        capture_effect(
            lambda selected: selected.unavailable(operation, captured_arguments, "interrupted", "cancelled", None),
            "cancelled",
            None,
        )
        emit_request_event("result", "cancelled", None)
        raise ToolError("The request was cancelled at a cooperative checkpoint.") from error
    except ToolError:
        capture_effect(
            lambda selected: selected.unavailable(operation, captured_arguments, "callback-rejected", "rejected", None),
            "rejected",
            None,
        )
        emit_request_event("result", "rejected", None)
        raise
    except Exception:
        capture_effect(
            lambda selected: selected.unavailable(operation, captured_arguments, "callback-error", "error", None),
            "error",
            None,
        )
        emit_request_event("result", "error", None)
        raise
    try:
        validated = contract_schemas.validate_result(operation, result.content)
    except Exception:
        capture_effect(
            lambda selected: selected.unavailable(
                operation,
                captured_arguments,
                "result-validation-error",
                result.classification,
                result.commit_reference,
            ),
            result.classification,
            result.commit_reference,
        )
        emit_request_event("result-validation-error", result.classification, result.commit_reference)
        raise
    capture_effect(
        lambda selected: selected.available(operation, captured_arguments, validated),
        result.classification,
        result.commit_reference,
    )
    emit_request_event("result", result.classification, result.commit_reference)
    return validated
