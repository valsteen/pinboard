"""Bounded MCP execution and request completion; no Pinboard use-case policy."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import shlex
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
from pinboard.adapters.files.root import resolve_shared_repository_root, resolve_source_checkout_root
from pinboard.adapters.files.setting_resolution import SettingEffects, SettingResolutionError
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.domain.errors import (
    DescribedCode,
    EffectDisposition,
    RetryDisposition,
)
from pinboard.mcp import contract_schemas
from pinboard.mcp.contracts import JsonValue

THREAD_NAME_PREFIX = "pinboard-mcp-worker"


class TraceEvent(DescribedCode):
    STARTUP = ("startup", "The MCP server recorded its startup before handling requests.")
    RESULT = ("result", "An MCP request produced a correlated result record.")
    RESULT_VALIDATION_ERROR = (
        "result-validation-error",
        "The result failed its declared MCP output schema after execution.",
    )
    CAPTURE_UNAVAILABLE = ("capture-unavailable", "Exact invocation capture was unavailable for the request.")
    CAPTURE_COMMITTED_WITH_WARNING = (
        "capture-committed-with-warning",
        "Exact invocation capture published bytes but reported a later warning.",
    )


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

    def resolve(  # noqa: C901, PLR0912, PLR0915 - one preflight owns settings, destination and partial effects
        self, project_root: str, arguments: dict[str, JsonValue]
    ) -> SemanticCapture | OperationResult | None:
        request = arguments.get("request")
        selected = request if isinstance(request, dict) else arguments
        work_root = selected.get("work_root")
        if not isinstance(work_root, str):
            return None
        settings_path = Path(work_root) / contributor_traces.SETTINGS_NAME
        directory_path = Path(work_root) / contributor_traces.TRACE_DIRECTORY
        settings_effects = SettingEffects("none", "none", "none")
        directory_existed = directory_path.exists()
        resource = settings_path
        probe_effect: Literal["none", "unconfirmed"] = "none"
        try:
            state = contributor_traces.read_project_trace_settings(Path(project_root), Path(work_root))
            if state is None:
                return None
            data_root, resolution = state
            settings_effects = resolution.effects
            settings = resolution.value
            resource = Path(work_root) if settings.item_overrides else settings_path
            item_id = (
                self._select_item(
                    resolve_shared_repository_root(resolve_source_checkout_root(Path(project_root))),
                    work_root,
                    arguments,
                )
                if settings.item_overrides
                else None
            )
            resource = directory_path
            directory = contributor_traces.automatic_trace_directory(data_root, settings, item_id)
            if directory is None:
                return None
            try:
                return SemanticCapture(directory, automatic=True)
            except ValueError, OSError, FileIOError:
                probe_effect = "unconfirmed"
                raise
        except (ValueError, OSError, FileIOError, StorageError) as error:
            if isinstance(error, StorageError) and error.invariant_violation:
                raise
            if isinstance(error, SettingResolutionError):
                settings_effects = error.effects
                resource = error.path
            elif "work root" in str(error).lower() and resource == settings_path:
                resource = Path(work_root)
            directory_created = not directory_existed and directory_path.exists()
            confirmed = (
                settings_effects.file_creation == "confirmed"
                or settings_effects.key_write == "acknowledged"
                or directory_created
            )
            unconfirmed = (
                settings_effects.parent_creation == "unconfirmed"
                or settings_effects.file_creation == "unconfirmed"
                or settings_effects.key_write == "unconfirmed"
                or probe_effect == "unconfirmed"
            )
            effect = "unconfirmed" if unconfirmed else "committed" if confirmed else "unchanged"
            if isinstance(error, StorageError):
                repair = f"Inspect Pinboard state under {resource} and resolve this read error before retrying."
            elif "work root must be a real directory" in str(error):
                try:
                    real_root = Path(work_root).resolve()
                except OSError, RuntimeError:
                    real_root = None
                repair = (
                    f"Pass the real directory {real_root} as work_root, then retry."
                    if real_root is not None and real_root.is_dir()
                    else f"Pass an existing real directory instead of the symlink {work_root} as work_root, then retry."
                )
            elif "Git status could not be verified" in str(error):
                root = Path(work_root)
                metadata = next(
                    (
                        parent / ".git"
                        for parent in (root, *root.parents)
                        if (parent / ".git").exists(follow_symlinks=False)
                    ),
                    root,
                )
                checked_name = f"{resource.name}/" if resource == directory_path else resource.name
                command = f"git -C {shlex.quote(work_root)} check-ignore -q -- {shlex.quote(checked_name)}"
                repair = f"Restore valid Git metadata at {metadata} so `{command}` succeeds, then retry."
            elif "Git-ignored" in str(error):
                repair = f"Keep {resource} Git-ignored, then retry."
            elif resource == settings_path:
                repair = (
                    f"Grant write access to {resource} and retry."
                    if "write" in str(error).lower() or isinstance(error, PermissionError)
                    else f"Correct {resource} and retry."
                )
            elif "private directory" in str(error):
                repair = f"Make {resource} a real private directory with mode 0700, then retry."
            elif isinstance(error, PermissionError) or "writable" in str(error).lower():
                repair = f"Grant write access to {resource}, then retry."
            else:
                repair = f"Correct {resource}, then retry."
            content: dict[str, JsonValue] = {
                "schema": "pinboard-mcp-execution-result/v1",
                "status": "rejected",
                "code": "TRACE_PREFLIGHT_FAILED",
                "message": f"Automatic Pinboard trace preflight failed: {error}. The target callback did not run.",
                "resource": str(resource),
                "repair": repair,
                "target_ran": False,
                "state_changed": None if unconfirmed else confirmed,
                "effect": effect,
                "retry": "correct-input",
                "changed_surfaces": ["work-root"] if confirmed else [],
                "settings_parent_creation": settings_effects.parent_creation,
                "settings_file_creation": settings_effects.file_creation,
                "settings_mode_write": settings_effects.key_write,
                "trace_directory_creation": "confirmed" if directory_created else "none",
                "capture_probe_effect": probe_effect,
            }
            return OperationResult(content, "rejected", None)


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
        event: TraceEvent,
        request_id: int | None,
        operation: str | None,
        project_id: str | None,
        duration_ms: int | None,
        classification: str | None,
        commit_reference: str | None,
        capture_selector: str | None,
    ) -> None:
        fields = ["pinboard_mcp", f"version={__version__}", f"event={event.value}"]
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
            capture_failure = event in {TraceEvent.CAPTURE_UNAVAILABLE, TraceEvent.CAPTURE_COMMITTED_WITH_WARNING}
            event_count = self._capture_failure_events if capture_failure else self._events
            if event_count >= self._event_limit:
                return
            if capture_failure:
                self._capture_failure_events += 1
            else:
                self._events += 1
            self._stream.write(line[: self._line_limit - 1] + "\n")
            self._stream.flush()


async def _run_request(  # noqa: C901, PLR0915 - one execution boundary owns callback and capture aftermath
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
        selected_capture = capture.resolve(project_root, arguments)
        if isinstance(selected_capture, OperationResult):
            return contract_schemas.validate_result(operation, selected_capture.content)
        capture = selected_capture
    captured_arguments = deepcopy(arguments) if capture is not None else arguments
    project_id = hashlib.sha256(project_root.encode()).hexdigest()[:12]
    started = time.monotonic_ns()

    def emit_request_event(event: TraceEvent, classification: str, commit_reference: str | None) -> None:
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
                event=TraceEvent.CAPTURE_COMMITTED_WITH_WARNING,
                request_id=None,
                operation=None,
                project_id=None,
                duration_ms=None,
                classification=classification,
                commit_reference=commit_reference,
                capture_selector=error.path.name,
            )
        except FileIOError:
            emit_request_event(TraceEvent.CAPTURE_UNAVAILABLE, classification, commit_reference)

    try:
        execution = executor.submit(callback)
    except ExecutorBusy:
        emit_request_event(TraceEvent.RESULT, "busy", None)
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
        emit_request_event(TraceEvent.RESULT, "cancelled", None)
        raise
    except OperationCancelled as error:
        capture_effect(
            lambda selected: selected.unavailable(operation, captured_arguments, "interrupted", "cancelled", None),
            "cancelled",
            None,
        )
        emit_request_event(TraceEvent.RESULT, "cancelled", None)
        raise ToolError(
            "The request was cancelled at a cooperative checkpoint. Its effect is unknown; inspect current "
            "item or attempt state before deciding whether another call is safe."
        ) from error
    except ToolError:
        capture_effect(
            lambda selected: selected.unavailable(operation, captured_arguments, "callback-rejected", "rejected", None),
            "rejected",
            None,
        )
        emit_request_event(TraceEvent.RESULT, "rejected", None)
        raise
    except Exception:
        capture_effect(
            lambda selected: selected.unavailable(operation, captured_arguments, "callback-error", "error", None),
            "error",
            None,
        )
        emit_request_event(TraceEvent.RESULT, "error", None)
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
        emit_request_event(TraceEvent.RESULT_VALIDATION_ERROR, result.classification, result.commit_reference)
        raise
    capture_effect(
        lambda selected: selected.available(operation, captured_arguments, validated),
        result.classification,
        result.commit_reference,
    )
    emit_request_event(TraceEvent.RESULT, result.classification, result.commit_reference)
    return validated
