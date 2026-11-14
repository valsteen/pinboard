"""Exact CLI invocation capture for calls that entered through `scripts/pinboard`.

The launcher exports its own invocation path in `PINBOARD_LAUNCHER_PATH` on the final CLI exec only.
`prepare` reads and removes that value once, before the target runs. Without it the call is uncaptured:
the manual form reaches the parser unchanged and automatic contributor tracing does not apply, so a
direct console-script or in-process `entrypoint.main` call neither seeds trace settings nor publishes
a trace.

With it, a leading `--capture-evidence <file> --safe-to-persist-exactly --` selects manual capture and
never reads trace settings; otherwise the selected work root's contributor setting may select an
automatic `pinboard-auto-cli-<hex>.json` trace. A `--project-root` that is not an existing directory
selects no automatic trace, so the target reports that project-root error itself instead of a trace
setup failure. Before the target runs, this owner validates the
destination name, resolves its parent, requires the destination to be absent and reserves an
`O_EXCL` mode-0600 stage file and unnamed output files in that parent, never under `TMPDIR`. Any
failure there ends the call before the target with a strict launcher result or, for automatic
settings and directories, the contributor-trace diagnosis on stderr.

`CaptureSession` then owns the target's file descriptors 1 and 2 and the `HUP`, `INT` and `TERM`
handlers of every such signal not already ignored at that point; an ignored signal stays ignored. A
signal during the target ends it through `CaptureInterrupted`, the one exception this
module declares: the signal arrives asynchronously inside command code that cannot return a typed
value for it, and only `entrypoint.main` catches it, at the capture boundary. A signal in the few
bytecodes between the target returning or raising and `disarm` may still raise it there; the call is
then finished as an interruption, published and replayed like any other. After `disarm`, later
signals are only remembered: they change neither the record nor a successful exit status. The session publishes the unchanged `pinboard-cli-invocation-capture/v1` record by hard
link without overwriting, removes its stage, applies automatic retention and replays the target's
exact stdout and stderr. A publication failure after the target ran is reported as
`capture-publication-failed` with `do-not-retry`; it never invites replay.
If replaying stdout fails, the session still attempts to deliver stderr and either names the published
record and target exit status or reports the publication failure. The original output error propagates.

This module adds no process: the target runs in this interpreter. It deliberately does not recover a
record when the target dies hard or the process fails before `CaptureSession.arm` runs, and it does
not clean stale stage files left by such a death.
"""

import hashlib
import os
import secrets
import signal
import sys
import tempfile
import types
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import IO, Annotated, Any, Literal, assert_never  # noqa: TID251 - typeshed signal handler return type

import msgspec

from pinboard.adapters.files import contributor_traces
from pinboard.adapters.files.errors import FileIOError, RootError
from pinboard.adapters.files.setting_resolution import DanglingLink, SettingResolutionError, UnusableLinkTarget
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.cli import work_state_commands

LAUNCHER_PATH_VARIABLE = "PINBOARD_LAUNCHER_PATH"
CAPTURED_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
_LAUNCHER_ONLY_TARGETS = frozenset(
    {
        "--prepare-runtime",
        "--mcp",
        "--claude-session-start",
        "--claude-subagent-start",
        "--claude-pre-tool-use",
        "--antigravity-session-start",
        "--antigravity-pre-tool-use",
    }
)
_CAPTURE_FLAGS = ("--capture-evidence", "--capture-evidence-dir", "--safe-to-persist-exactly")
_PUBLICATION_FAILED_EXIT = 74
_INVALID_CAPTURE_EXIT = 64


class RuntimeLocation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    base: Literal["launcher-root"]
    relative: Literal[".pinboard-runtime"]


class LauncherResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """The `pinboard-launcher-result/v1` shape `scripts/pinboard` also emits, for capture failures."""

    schema: Literal["pinboard-launcher-result/v1"]
    status: Literal["invalid-capture-arguments", "capture-publication-failed"]
    pinboard_started: bool
    runtime_location: RuntimeLocation
    observations: tuple[Annotated[str, msgspec.Meta(min_length=1)], ...]
    upstream_exit_code: int | None
    retry_disposition: Literal["correct-input", "do-not-retry"]
    effect_disposition: Literal["unchanged", "potentially-changed"]
    changed_surfaces: tuple[Literal["target"], ...]
    next_action: None


class CapturedBytes(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    encoding: Literal["hex"]
    sha256: Annotated[str, msgspec.Meta(pattern="^[0-9a-f]{64}$")]
    size_bytes: Annotated[int, msgspec.Meta(ge=0)]
    data: Annotated[str, msgspec.Meta(pattern="^(?:[0-9a-f]{2})*$")]


class CliInvocationCapture(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-cli-invocation-capture/v1"]
    selector: CapturedBytes
    argv: tuple[CapturedBytes, ...]
    outcome: Literal["completed", "interrupted"]
    exit_status: Annotated[int, msgspec.Meta(ge=0, le=255)]
    stdout: CapturedBytes
    stderr: CapturedBytes
    environment: Literal["not-captured"]
    presentation: Literal["outer-presentation-unavailable"]
    accepted_evidence: bool


class CaptureInterrupted(BaseException):
    """HUP, INT or TERM ended the captured target; `entrypoint.main` alone catches it."""

    def __init__(self, signal_number: int) -> None:
        super().__init__(signal_number)
        self.signal_number: int = signal_number


@dataclass(frozen=True)
class Uncaptured:
    target_arguments: tuple[str, ...]


@dataclass(frozen=True)
class RejectedBeforeTarget:
    exit_status: int


class _Phase(Enum):
    PREPARED = auto()
    TARGET = auto()
    FINISHING = auto()


type _SignalHandler = Callable[[int, types.FrameType | None], Any] | int | signal.Handlers | None


@dataclass(frozen=True)
class _Reservation:
    selector: str
    destination: Path
    stage: Path
    stage_descriptor: int
    stdout: IO[bytes]
    stderr: IO[bytes]


class CaptureSession:
    """One reserved capture: owns the stage, output files, descriptors 1 and 2 and signal handlers."""

    def __init__(
        self,
        argv: tuple[str, ...],
        target_arguments: tuple[str, ...],
        reservation: _Reservation,
        retained_directory: Path | None,
    ) -> None:
        self.target_arguments: tuple[str, ...] = target_arguments
        self._argv = argv
        self._reservation = reservation
        self._retained_directory = retained_directory
        self._phase = _Phase.PREPARED
        self._late_signal: int | None = None
        self._previous_handlers: dict[signal.Signals, _SignalHandler] = {}
        self._saved_descriptors: tuple[int, int] | None = None
        self._stage_open = True

    def arm(self) -> None:
        """Install the interrupting handlers, then route descriptors 1 and 2 into the reserved files.

        A signal ignored when capture starts, as under `nohup` or for a background job, stays ignored,
        so the captured target sees the same dispositions as an uncaptured one.
        """
        self._phase = _Phase.TARGET
        for captured in CAPTURED_SIGNALS:
            if signal.getsignal(captured) != signal.SIG_IGN:
                self._previous_handlers[captured] = signal.signal(captured, self._handle_signal)
        _flush_python_streams()
        saved = (os.dup(1), os.dup(2))
        self._saved_descriptors = saved
        os.dup2(self._reservation.stdout.fileno(), 1)
        os.dup2(self._reservation.stderr.fileno(), 2)

    def disarm(self) -> None:
        """Stop interrupting the target; a later signal is only remembered."""
        self._phase = _Phase.FINISHING

    def finish(self, outcome: int | BaseException) -> int:
        """Present the target's own exit, publish its record, replay its output and return the exit status."""
        self.disarm()
        try:
            status = _target_exit_status(outcome)
            _flush_python_streams()
            self._restore_descriptors()
            stdout = _read_all(self._reservation.stdout)
            stderr = _read_all(self._reservation.stderr)
            failure = self._publish(_record(self._reservation.selector, self._argv, status, stdout, stderr))
            diagnostic = (
                self._retain()
                if failure is None
                else _publication_failure(self._reservation.destination, failure, status)
            )
            self._replay(stdout, stderr, diagnostic, status, published=failure is None)
            if failure is None:
                return status
            if isinstance(outcome, CaptureInterrupted):
                return status
            if self._late_signal is not None:
                return 128 + self._late_signal
            return _PUBLICATION_FAILED_EXIT
        finally:
            self._restore_descriptors()
            self._discard_stage()
            self._reservation.stdout.close()
            self._reservation.stderr.close()
            for captured, handler in self._previous_handlers.items():
                signal.signal(captured, handler)

    def _replay(self, stdout: bytes, stderr: bytes, diagnostic: bytes | None, status: int, *, published: bool) -> None:
        stdout_error = None
        try:
            _write_all(1, stdout)
        except OSError as error:
            stdout_error = error

        try:
            _write_all(2, stderr)
            if diagnostic is not None:
                _write_all(2, diagnostic)
            if stdout_error is not None and published:
                _write_all(
                    2,
                    os.fsencode(
                        f"Pinboard could not deliver captured stdout. Read the exact capture at "
                        f"'{self._reservation.destination}' for the target's output and exit status {status}.\n"
                    ),
                )
        except OSError:
            if stdout_error is None:
                raise
        if stdout_error is not None:
            raise stdout_error

    def _handle_signal(self, signal_number: int, _frame: types.FrameType | None) -> None:
        if self._phase is _Phase.TARGET:
            self._phase = _Phase.FINISHING
            raise CaptureInterrupted(signal_number)
        if self._late_signal is None:
            self._late_signal = signal_number

    def _restore_descriptors(self) -> None:
        saved = self._saved_descriptors
        if saved is None:
            return
        self._saved_descriptors = None
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        os.close(saved[0])
        os.close(saved[1])

    def _publish(self, content: bytes) -> OSError | None:
        try:
            _write_all(self._reservation.stage_descriptor, content)
            os.link(self._reservation.stage, self._reservation.destination)
        except OSError as error:
            return error
        finally:
            self._discard_stage()
        return None

    def _discard_stage(self) -> None:
        if self._stage_open:
            self._stage_open = False
            with suppress(OSError):
                os.close(self._reservation.stage_descriptor)
        with suppress(OSError):
            self._reservation.stage.unlink(missing_ok=True)

    def _retain(self) -> bytes | None:
        directory = self._retained_directory
        if directory is None:
            return None
        try:
            contributor_traces.prune_traces(directory)
        except OSError as error:
            return (
                f"Pinboard automatic trace was published at {self._reservation.destination}, but retention cleanup "
                f"failed after Pinboard ran: {error}. Restore access to {directory} for cleanup only; do not "
                "repeat the target command.\n"
            ).encode()
        return None


type CapturePreparation = Uncaptured | CaptureSession | RejectedBeforeTarget


def prepare(arguments: tuple[str, ...]) -> CapturePreparation:
    """Select manual, automatic or no capture for one CLI call and reserve its publication."""
    launcher = os.environ.pop(LAUNCHER_PATH_VARIABLE, None)
    if launcher is None:
        return Uncaptured(arguments)
    argv = (launcher, *arguments)
    if arguments[:1] == ("--capture-evidence",):
        if len(arguments) < 5 or arguments[2] != "--safe-to-persist-exactly" or arguments[3] != "--":
            return _invalid_capture(
                "Use --capture-evidence <fresh-file> --safe-to-persist-exactly -- <normal CLI arguments>."
            )
        target = arguments[4:]
        if target[0] in _LAUNCHER_ONLY_TARGETS or any(_is_capture_flag(argument) for argument in target):
            return _invalid_capture(
                "CLI process capture wraps only an ordinary CLI call; run --prepare-runtime and hook selectors "
                "without capture, use the declared MCP capture form for --mcp, and do not nest capture flags."
            )
        return _reserve(argv, target, arguments[1], None)
    selection = contributor_traces.cli_trace_selection(arguments)
    if selection.project_root is not None:
        try:
            work_state_commands.require_project_root_directory(selection.project_root)
        except RootError:
            # The target reports this project-root error itself; a call that cannot run is not traced.
            return Uncaptured(arguments)
    try:
        selected = contributor_traces.select_cli_trace(selection)
    except (ValueError, OSError, FileIOError, StorageError) as error:
        if isinstance(error, StorageError) and error.invariant_violation:
            raise
        _report_trace_setup_failure(error)
        return RejectedBeforeTarget(_INVALID_CAPTURE_EXIT)
    if selected is None:
        return Uncaptured(arguments)
    return _reserve(argv, arguments, str(selected), selected.parent)


def _is_capture_flag(argument: str) -> bool:
    return any(argument == flag or argument.startswith(f"{flag}=") for flag in _CAPTURE_FLAGS)


def _reserve(
    argv: tuple[str, ...], target: tuple[str, ...], selector: str, retained_directory: Path | None
) -> CaptureSession | RejectedBeforeTarget:
    # Split the spelling as given: Path would fold a trailing "." into its parent's name.
    head, separator, name = selector.rstrip("/").rpartition("/")
    if name in {"", ".", ".."}:
        return _invalid_capture("Choose a destination with a nonempty file name, not dot or dot-dot.")
    try:
        parent = Path(head or (separator or ".")).resolve(strict=True)
        if not parent.is_dir():
            raise NotADirectoryError(parent)
    except OSError:
        return _invalid_capture(
            "The selected capture parent is unavailable; restore that exact directory or choose an existing intended parent."
        )
    destination = parent / name
    if os.path.lexists(destination):
        return _invalid_capture(
            "The selected capture destination already exists; inspect it and choose a fresh exact destination. Do not overwrite it."
        )
    try:
        stdout = tempfile.TemporaryFile(dir=parent)  # noqa: SIM115 - the capture session closes it
    except OSError:
        return _invalid_capture(
            "Cannot reserve private capture staging in the selected parent; request write access to that exact directory before retrying."
        )
    try:
        stderr = tempfile.TemporaryFile(dir=parent)  # noqa: SIM115 - the capture session closes it
    except OSError:
        stdout.close()
        return _invalid_capture(
            "Cannot reserve private capture staging in the selected parent; request write access to that exact directory before retrying."
        )
    stage = parent / f".pinboard-capture-stage.{secrets.token_hex(16)}"
    try:
        descriptor = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        stdout.close()
        stderr.close()
        return _invalid_capture(
            "Cannot reserve the selected private capture publication file; restore write access to the selected parent before retrying."
        )
    try:
        os.fchmod(descriptor, 0o600)
    except OSError:
        os.close(descriptor)
        stage.unlink(missing_ok=True)
        stdout.close()
        stderr.close()
        return _invalid_capture(
            "Cannot make the selected capture publication file private; restore write access to the selected parent before retrying."
        )
    return CaptureSession(
        argv, target, _Reservation(selector, destination, stage, descriptor, stdout, stderr), retained_directory
    )


def _target_exit_status(outcome: int | BaseException) -> int:
    """The exit status the interpreter would give this outcome, writing what it would write to stderr."""
    match outcome:
        case int():
            return outcome % 256
        case CaptureInterrupted(signal_number=signal_number):
            return 128 + signal_number
        case SystemExit(code=None):
            return 0
        case SystemExit(code=int() as code):
            return code % 256
        case SystemExit(code=code):
            print(code, file=sys.stderr)
            return 1
        case BaseException():
            _print_uncaught(outcome)
            return 1
        case _ as unreachable:
            assert_never(unreachable)


def _print_uncaught(error: BaseException) -> None:
    """Print the traceback the interpreter would print had the exception left `entrypoint.main`.

    The exception was caught in `entrypoint.main`, so its traceback starts at that frame; the frames
    still running above it are added in the form the interpreter records while unwinding.
    """
    entry = error.__traceback__
    frame = None if entry is None else entry.tb_frame.f_back
    while frame is not None:
        entry = types.TracebackType(entry, frame, frame.f_lasti, frame.f_lineno)
        frame = frame.f_back
    sys.excepthook(type(error), error.with_traceback(entry), entry)


def _flush_python_streams() -> None:
    for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        if stream is not None:
            stream.flush()


def _read_all(file: IO[bytes]) -> bytes:
    file.seek(0)
    return file.read()


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        view = view[os.write(descriptor, view) :]


def _captured(content: bytes) -> CapturedBytes:
    return CapturedBytes("hex", hashlib.sha256(content).hexdigest(), len(content), content.hex())


def _record(selector: str, argv: tuple[str, ...], status: int, stdout: bytes, stderr: bytes) -> bytes:
    record = CliInvocationCapture(
        "pinboard-cli-invocation-capture/v1",
        _captured(os.fsencode(selector)),
        tuple(_captured(os.fsencode(argument)) for argument in argv),
        "interrupted" if status >= 128 else "completed",
        status,
        _captured(stdout),
        _captured(stderr),
        "not-captured",
        "outer-presentation-unavailable",
        False,
    )
    return msgspec.json.encode(record) + b"\n"


def _launcher_result(result: LauncherResult) -> bytes:
    return msgspec.json.encode(result) + b"\n"


def _invalid_capture(observation: str) -> RejectedBeforeTarget:
    _flush_python_streams()
    _write_all(
        1,
        _launcher_result(
            LauncherResult(
                "pinboard-launcher-result/v1",
                "invalid-capture-arguments",
                False,
                RuntimeLocation("launcher-root", ".pinboard-runtime"),
                (observation,),
                None,
                "correct-input",
                "unchanged",
                (),
                None,
            )
        ),
    )
    return RejectedBeforeTarget(_INVALID_CAPTURE_EXIT)


def _publication_failure(destination: Path, error: OSError, status: int) -> bytes:
    return _launcher_result(
        LauncherResult(
            "pinboard-launcher-result/v1",
            "capture-publication-failed",
            True,
            RuntimeLocation("launcher-root", ".pinboard-runtime"),
            (
                f"The target ran once, but its exact capture could not be published at {destination}: {error.strerror or error}.",
                f"Do not replay a mutation to recover it; inspect {destination} and its directory before choosing a fresh destination for a later call.",
            ),
            status,
            "do-not-retry",
            "potentially-changed",
            ("target",),
            None,
        )
    )


def _report_trace_setup_failure(error: ValueError | OSError | FileIOError | StorageError) -> None:
    print(f"Contributor trace setup failed before Pinboard ran; the target did not run: {error}", file=sys.stderr)
    if isinstance(error, SettingResolutionError):
        print(
            f"Settings resource={error.resource}; operation={error.operation}; settings_parent_creation={error.effects.parent_creation}; settings_file_creation={error.effects.file_creation}; settings_mode_write={error.effects.key_write}. Inspect stored settings at {error.path} before retrying; the target did not run.",
            file=sys.stderr,
        )
        print(_settings_error_repair(error), file=sys.stderr)
    elif "Git status could not be verified" in str(error):
        print("Inspect Git metadata or use a work root outside Git, then retry the original request.", file=sys.stderr)


def _settings_error_repair(error: SettingResolutionError) -> str:
    if isinstance(error.cause, FileExistsError):
        return (
            f"Wait for the settings edit at {error.resource} to finish; report a stale lock to the human "
            "rather than deleting an active edit."
        )
    if isinstance(error.cause, PermissionError):
        target = error.resource if error.operation == "read" else error.resource.parent
        access = "read" if error.operation == "read" else "write"
        return f"Request {access} access to {target} for the settings operation, then retry the original request."
    if isinstance(error.cause, DanglingLink):
        return f"Create {error.cause.target}, or fix or remove the link {error.cause.link}, then retry."
    if isinstance(error.cause, UnusableLinkTarget):
        return f"Fix or remove the link {error.cause.link}, then retry."
    return (
        "Report the named settings condition to the human and resolve it before retrying the original request; "
        "preserve any acknowledged settings writes."
    )
