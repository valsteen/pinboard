"""Bounded MCP execution and request completion; no Pinboard use-case policy."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import shlex
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO, assert_never

import msgspec
from mcp.server.mcpserver.exceptions import ToolError
from typing_extensions import TypeForm

from pinboard import __version__
from pinboard.adapters.files import contributor_traces, git_config
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode, ImmutableFilePublishedError
from pinboard.adapters.files.file_io import create_immutable
from pinboard.adapters.files.root import resolve_shared_repository_root, resolve_source_checkout_root
from pinboard.adapters.files.setting_resolution import (
    DanglingLink,
    InvalidEntry,
    NotDirectory,
    NotRegularFile,
    SeedNotReadBack,
    SettingEffects,
    SettingResolutionError,
    UnusableLinkTarget,
)
from pinboard.adapters.sqlite.errors import SQLiteSchemaError, StorageError
from pinboard.diagnostic_codes import TraceEvent
from pinboard.mcp import contract_schemas, contracts
from pinboard.mcp.contracts import JsonValue

THREAD_NAME_PREFIX = "pinboard-mcp-worker"
_CANCELLED_REQUEST_MESSAGE = (
    "The request was cancelled at a cooperative checkpoint. Its effect is unknown. "
    "Before deciding whether another call is safe, use pinboard_attempt_inspect with the original exact roots, "
    "attempt_id and reconciliation:null, or pinboard_item_status with request containing the original roots, "
    "operation:'item' and item_id. For a request without an item or attempt, read pinboard_overview with the "
    "original project_root and work_root, max_items:25 and continuation:null, following continuation for complete "
    "coverage. Cancellation does not prove rollback; do not blindly replay a mutation."
)


@dataclass(frozen=True, slots=True)
class OperationResult:
    """An untyped operation result that validate_result selects by tool name and result strings."""

    content: dict[str, JsonValue]
    classification: str
    commit_reference: str | None


@dataclass(frozen=True, slots=True)
class TypedOperationResult[Result: msgspec.Struct]:
    """A declared result record emitted through wire_content without runtime revalidation.

    Construction runs the record's post-init invariants; static types and the advertised output schema own the rest.
    """

    record: Result
    classification: str
    commit_reference: str | None


@dataclass(frozen=True, slots=True)
class ToolContract[Request: msgspec.Struct, Result: msgspec.Struct]:
    """Bind one tool's name and description, request record, result union and operation for the type checker.

    `results` is the `Result` union as a type form, so a union or alias outside `Result` fails type checking, and
    the advertised output schema projects from it.
    """

    name: str
    description: str
    request: type[Request]
    results: TypeForm[Result]
    operate: Callable[[Mapping[str, JsonValue], CancellationToken], TypedOperationResult[Result]]


def wire_content(record: msgspec.Struct) -> dict[str, JsonValue]:
    """Convert one typed result record to the JSON object content the SDK serializes, without revalidating it."""
    content: dict[str, JsonValue] = msgspec.json.decode(msgspec.json.encode(record))
    return content


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


def settings_recovery(  # noqa: C901, PLR0912, PLR0915 - one boundary distinguishes exact settings failures and recovery
    error: SettingResolutionError, subject: str, correction: str
) -> tuple[str, str, Literal["correct-input", "retry-same-input"]]:
    """Assign recovery from the original settings observation, independently of its effect account.

    `subject` names the settings in resources and repairs; `correction` is the repair for invalid content.
    """
    resource = str(error.resource)
    retry: Literal["correct-input", "retry-same-input"] = "correct-input"
    cause = error.cause
    if isinstance(cause, git_config.ReadFailed | git_config.WriteUnconfirmed):
        command = f"git config --file {shlex.quote(str(error.path))} --null --list"
        match cause.cause:
            case git_config.WorkingDirectoryUnavailable():
                resource = "MCP service working directory"
                repair = "Restart or reconnect the Pinboard MCP service from an existing plugin directory, then retry the same request."
                retry = "retry-same-input"
            case git_config.LaunchFailed():
                resource = "Git executable"
                repair = (
                    "Restore access to the Git executable for the Pinboard MCP service, then retry the same request."
                )
                retry = "retry-same-input"
            case git_config.ProcessFailed(returncode=returncode) if isinstance(cause, git_config.WriteUnconfirmed):
                # Git locks the staged copy through its own `<staged>.lock`, which a killed write leaves behind.
                git_lock = cause.path.with_name(f"{cause.path.name}.lock")
                repair = (
                    f"Git exited with status {returncode} while adding {cause.key} to the staged copy {cause.path}. "
                    f"If Git's lock {git_lock} exists and no settings edit is in progress, it is a stale lock left by "
                    "an interrupted process: report it to the human, who can remove it; do not delete it while an "
                    "edit may be active. Otherwise resolve the reported cause, then retry the same request."
                )
                retry = "retry-same-input"
            case git_config.ProcessFailed(returncode=returncode):
                # A read that Git rejects, such as unparseable syntax, clears only after the file is corrected.
                repair = f"Run `{command}` to diagnose Git's exit status {returncode}; resolve the reported cause before retrying."
            case git_config.InvalidOutput():
                resource = f"Git configuration output for {error.path}"
                repair = f"Run `{command}` and resolve Git's invalid output before retrying the same request."
                retry = "retry-same-input"
            case _ as unreachable:
                assert_never(unreachable)
    else:
        match cause:
            case FileExistsError():
                resource = f"{subject} edit at {error.resource}"
                repair = (
                    f"Retry the same request after the settings edit at {error.resource} finishes. "
                    f"If no edit is in progress, {error.resource} is a stale lock left by an interrupted process: "
                    "report it to the human, who can remove it; do not delete it while an edit may be active."
                )
                retry = "retry-same-input"
            case PermissionError():
                match error.operation:
                    case "read":
                        repair = (
                            f"Grant read access to {error.resource} and access to its directory "
                            f"{error.resource.parent} for the Pinboard MCP service, then retry."
                        )
                    case "create" | "stage" | "publish":
                        repair = f"Grant write access to {error.resource.parent} to initialize {subject} at {error.resource}, then retry."
                    case _ as unreachable:
                        assert_never(unreachable)
                retry = "retry-same-input"
            case OSError() | FileIOError():
                repair = (
                    f"Inspect access and resolve the reported filesystem failure at {error.resource} before retrying."
                )
            case NotRegularFile(path=path):
                repair = f"Replace {path} with a regular file, or remove it so Pinboard creates it, then retry."
            case NotDirectory(path=path):
                repair = f"Replace {path} with a directory, or remove it so Pinboard creates it, then retry."
            case DanglingLink(link=link, target=target):
                repair = f"Create {target}, or fix or remove the link {link}, then retry."
            case UnusableLinkTarget(link=link):
                repair = f"Fix or remove the link {link}, then retry."
            case SeedNotReadBack(key=key):
                repair = (
                    f"Another process may have changed {error.path} after this request wrote {key}; "
                    "retry the same request."
                )
                retry = "retry-same-input"
            case InvalidEntry() | ValueError():
                repair = correction
            case _ as unreachable:
                assert_never(unreachable)
    if error.effects.key_write != "none" or error.effects.file_creation != "none":
        repair += f" Inspect the stored settings at {error.path} first; prior settings effects are reported separately."
    return resource, repair, retry


def _schema_mismatch(error: SQLiteSchemaError, project_root: str) -> tuple[contracts.SchemaMismatch, str]:
    """Present verified schema facts and a separately authorized, quiescent CLI preview."""
    arguments: contracts.MigrationPreviewArguments | None = None
    if error.migration_supported:
        arguments = (
            "--project-root",
            project_root,
            "--work-root",
            str(error.database_path.parent),
            "migrate-schema",
            "--json",
        )
        command = " ".join(shlex.quote(value) for value in arguments)
        repair = (
            "Obtain explicit human authorization for migration, then stop every Pinboard process using "
            f"{error.database_path} before preview. Using the same Pinboard launcher from the original caller "
            f"context, run `<launcher> {command}`. Preview is read-only; inspect its plan before any separately "
            "authorized apply. Do not replay the refused operation or edit the database to bypass this failure."
        )
    else:
        repair = (
            f"Preserve {error.database_path} and report its sqlite-v{error.observed} schema and this installation's "
            f"required sqlite-v{error.required} to the human. No supported migration preview exists for this schema; "
            "do not edit metadata, apply another schema's plan or replay the refused operation."
        )
    return contracts.SchemaMismatch(
        str(error.database_path),
        f"sqlite-v{error.observed}",
        f"sqlite-v{error.required}",
        arguments,
    ), repair


@dataclass(frozen=True, slots=True)
class AutomaticCaptureResolution:
    capture: SemanticCapture | None
    settings_effects: SettingEffects
    directory_created: bool


class AutomaticCapture:
    """Resolve the current project and item mode before every MCP callback."""

    def __init__(self, select_item: Callable[[Path, str | None, dict[str, JsonValue]], str | None]) -> None:
        self._select_item = select_item

    def resolve(  # noqa: C901, PLR0912, PLR0915 - one preflight owns settings, destination and partial effects
        self, project_root: str, arguments: dict[str, JsonValue]
    ) -> AutomaticCaptureResolution | contracts.TracePreflightResult:
        request = arguments.get("request")
        selected = request if isinstance(request, dict) else arguments
        work_root = selected.get("work_root")
        if not isinstance(work_root, str):
            return AutomaticCaptureResolution(None, SettingEffects("none", "none", "none"), False)
        settings_path = Path(work_root) / contributor_traces.SETTINGS_NAME
        directory_path = Path(work_root) / contributor_traces.TRACE_DIRECTORY
        settings_effects = SettingEffects("none", "none", "none")
        directory_existed = directory_path.exists()
        resource = settings_path
        probe_effect: Literal["none", "unconfirmed"] = "none"
        try:
            state = contributor_traces.read_project_trace_settings(Path(project_root), Path(work_root))
            if state is None:
                return AutomaticCaptureResolution(None, settings_effects, False)
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
                return AutomaticCaptureResolution(None, settings_effects, False)
            try:
                return AutomaticCaptureResolution(
                    SemanticCapture(directory, automatic=True), settings_effects, not directory_existed
                )
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
            effect: Literal["unchanged", "committed", "unconfirmed"] = (
                "unconfirmed" if unconfirmed else "committed" if confirmed else "unchanged"
            )
            resource_text = str(resource)
            retry: Literal["correct-input", "retry-same-input"] = "correct-input"
            schema_mismatch: contracts.SchemaMismatch | None = None
            if isinstance(error, SettingResolutionError):
                resource_text, repair, retry = settings_recovery(
                    error, "Contributor trace settings", f"Correct {error.path} and retry."
                )
            elif isinstance(error, OSError) and error.filename == "git":
                resource_text = "Git executable"
                repair = (
                    "Restore access to the Git executable for the Pinboard MCP service, then retry the same request."
                )
                retry = "retry-same-input"
            elif isinstance(error, SQLiteSchemaError):
                schema_mismatch, repair = _schema_mismatch(error, project_root)
                resource_text = str(error.database_path)
            elif isinstance(error, StorageError):
                repair = f"Inspect Pinboard state under {resource} and resolve this read error before retrying."
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
                repair = f"Make {resource} a private directory with mode 0700, then retry."
            elif isinstance(error, PermissionError) or "writable" in str(error).lower():
                repair = f"Grant write access to {resource}, then retry."
            else:
                repair = f"Correct {resource}, then retry."
            return contracts.TracePreflightResult(
                "pinboard-mcp-execution-result/v1",
                "rejected",
                "TRACE_PREFLIGHT_FAILED",
                f"Automatic Pinboard trace preflight failed: {error}. The target callback did not run.",
                resource_text,
                repair,
                schema_mismatch,
                False,
                None if unconfirmed else confirmed,
                effect,
                retry,
                ("work-root",) if confirmed else (),
                settings_effects.parent_creation,
                settings_effects.file_creation,
                settings_effects.key_write,
                "confirmed" if directory_created else "none",
                probe_effect,
            )


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


def _schema_unsupported(
    error: SQLiteSchemaError, project_root: str, settings_effects: SettingEffects, trace_directory_created: bool
) -> TypedOperationResult[contracts.SchemaUnsupportedResult]:
    """Refuse a callback whose ledger schema is unsupported, keeping trace preflight effects separate."""
    mismatch, repair = _schema_mismatch(error, project_root)
    return TypedOperationResult(
        contracts.SchemaUnsupportedResult(
            "pinboard-mcp-schema-result/v1",
            "rejected",
            "SCHEMA_UNSUPPORTED",
            str(error),
            str(error.database_path),
            repair,
            mismatch,
            contracts.TracePreflightEffects(
                settings_effects.parent_creation,
                settings_effects.file_creation,
                settings_effects.key_write,
                "confirmed" if trace_directory_created else "none",
            ),
            False,
            "unchanged",
            "do-not-retry",
            (),
        ),
        "rejected",
        None,
    )


async def _run_request[Result: msgspec.Struct](  # noqa: C901, PLR0915 - one execution boundary owns callback and capture aftermath
    executor: BoundedExecutor,
    diagnostics: Diagnostics,
    request_id: int,
    operation: str,
    project_root: str,
    callback: Callable[[CancellationToken], OperationResult | TypedOperationResult[Result]],
    *,
    arguments: dict[str, JsonValue],
    capture: SemanticCapture | AutomaticCapture | None,
) -> dict[str, JsonValue]:
    project_id = hashlib.sha256(project_root.encode()).hexdigest()[:12]
    started = time.monotonic_ns()
    cancellation_reported = False
    cancellation_reporting = threading.Lock()

    def emit_request_event(event: TraceEvent, classification: str, commit_reference: str | None) -> None:
        nonlocal cancellation_reported
        if classification == "cancelled":
            # Transport cancellation can overlap the worker's cooperative checkpoint.
            with cancellation_reporting:
                if cancellation_reported:
                    return
                cancellation_reported = True
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

    def complete(token: CancellationToken) -> dict[str, JsonValue]:  # noqa: C901, PLR0915 - one worker owns preflight, callback, result emission and capture aftermath
        selected_capture = capture
        settings_effects = SettingEffects("none", "none", "none")
        trace_directory_created = False
        token.checkpoint()
        if isinstance(selected_capture, AutomaticCapture):
            resolved_capture = selected_capture.resolve(project_root, arguments)
            if isinstance(resolved_capture, contracts.TracePreflightResult):
                return wire_content(resolved_capture)
            selected_capture = resolved_capture.capture
            settings_effects = resolved_capture.settings_effects
            trace_directory_created = resolved_capture.directory_created
        captured_arguments = deepcopy(arguments) if selected_capture is not None else arguments

        def capture_effect(
            effect: Callable[[SemanticCapture], None],
            classification: str,
            commit_reference: str | None,
        ) -> None:
            if selected_capture is None:
                return
            try:
                effect(selected_capture)
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

        def record_callback_error() -> None:
            capture_effect(
                lambda selected: selected.unavailable(operation, captured_arguments, "callback-error", "error", None),
                "error",
                None,
            )
            emit_request_event(TraceEvent.RESULT, "error", None)

        result: OperationResult | TypedOperationResult[Result] | TypedOperationResult[contracts.SchemaUnsupportedResult]
        try:
            token.checkpoint()
            result = callback(token)
        except SQLiteSchemaError as error:
            result = _schema_unsupported(error, project_root, settings_effects, trace_directory_created)
        except OperationCancelled as error:
            capture_effect(
                lambda selected: selected.unavailable(operation, captured_arguments, "interrupted", "cancelled", None),
                "cancelled",
                None,
            )
            emit_request_event(TraceEvent.RESULT, "cancelled", None)
            raise ToolError(_CANCELLED_REQUEST_MESSAGE) from error
        except ToolError:
            capture_effect(
                lambda selected: selected.unavailable(
                    operation, captured_arguments, "callback-rejected", "rejected", None
                ),
                "rejected",
                None,
            )
            emit_request_event(TraceEvent.RESULT, "rejected", None)
            raise
        except Exception:
            record_callback_error()
            raise
        match result:
            case TypedOperationResult():
                try:
                    validated = wire_content(result.record)
                except Exception:
                    # A typed record that cannot encode is a defect in the operation that built it.
                    record_callback_error()
                    raise
            case OperationResult():
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
                    emit_request_event(
                        TraceEvent.RESULT_VALIDATION_ERROR, result.classification, result.commit_reference
                    )
                    raise
            case _ as unreachable:
                assert_never(unreachable)
        capture_effect(
            lambda selected: selected.available(operation, captured_arguments, validated),
            result.classification,
            result.commit_reference,
        )
        emit_request_event(TraceEvent.RESULT, result.classification, result.commit_reference)
        return validated

    try:
        execution = executor.submit(complete)
    except ExecutorBusy:
        emit_request_event(TraceEvent.RESULT, "busy", None)
        return wire_content(
            contracts.ExecutorBusyResult(
                "pinboard-mcp-execution-result/v1",
                "busy",
                "EXECUTOR_BUSY",
                "The MCP executor is busy; retry after current work finishes.",
                False,
                "unchanged",
                "retry-same-input",
                (),
                (),
                (),
            )
        )
    try:
        return await execution.result()
    except asyncio.CancelledError:
        emit_request_event(TraceEvent.RESULT, "cancelled", None)
        raise
    except OperationCancelled as error:
        emit_request_event(TraceEvent.RESULT, "cancelled", None)
        raise ToolError(_CANCELLED_REQUEST_MESSAGE) from error
