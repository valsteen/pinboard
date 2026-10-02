"""The harness's only subprocess owner: the claude, codex and git CLIs and the evaluated revision's launcher.

Executables other than the launcher resolve through PATH. The launcher is always the exported revision's own
``scripts/pinboard``; it runs only for ``--prepare-runtime``, board ``init`` and ``--mcp`` (the last through the
MCP stdio client in ``board``).
"""

import os
import shutil
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Tool(Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    GIT = "git"


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


@dataclass(frozen=True)
class Window:
    """One absolute monotonic deadline; None means an ordinary standalone command."""

    deadline: float | None

    def reserving(self, seconds: float) -> Window:
        """End work early enough for its existing owner to finish bounded cleanup."""
        return Window(None if self.deadline is None else self.deadline - seconds)

    def timeout(self, ordinary: float) -> float:
        if self.deadline is None:
            return ordinary
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("evaluation deadline expired; no new effect starts")
        return min(ordinary, remaining)


class ToolUnavailableError(Exception):
    """A required CLI is not on PATH; nothing ran."""


def executable(tool: Tool) -> str:
    match tool:
        case Tool.CLAUDE | Tool.CODEX | Tool.GIT:
            found = shutil.which(tool.value)
        case _ as unreachable:
            raise AssertionError(unreachable)
    if found is None:
        raise ToolUnavailableError(f"{tool.value} is not on PATH")
    return found


class ProcessIncomplete(subprocess.SubprocessError):
    """A started subprocess failed to deliver its result after confirmed cleanup; preserve available output."""

    def __init__(self, stdout: bytes, stderr: bytes, cause: BaseException) -> None:
        super().__init__(
            f"started subprocess result incomplete after confirmed cleanup: {cause!r}; usage may be unreported"
        )
        self.stdout = stdout.decode(errors="replace")
        self.stderr = stderr.decode(errors="replace")


class CleanupUnconfirmed(subprocess.SubprocessError):
    """Native shutdown did not complete; owned effects may remain and credentials must be retained."""

    def __init__(self, pid: int, stdout: bytes, stderr: bytes) -> None:
        super().__init__(
            f"native shutdown for process {pid} unconfirmed; stop and inspect owned effects before credential cleanup"
        )
        self.stdout = stdout.decode(errors="replace")
        self.stderr = stderr.decode(errors="replace")


def _cancel(
    child: subprocess.Popen[bytes], native_shutdown: bool, window: Window, stdout: bytes, stderr: bytes
) -> tuple[bytes, bytes]:
    """Request Codex's SIGINT turn interruption and shutdown before any forceful fallback.

    Its supported tools own separate sessions; only completed native shutdown establishes their cleanup.
    Other harness commands receive SIGINT in their inherited group. A forced fallback is never confirmation.
    """
    try:
        with suppress(ProcessLookupError):
            if native_shutdown:
                child.send_signal(signal.SIGINT)
            else:
                os.killpg(child.pid, signal.SIGINT)
        try:
            stdout, stderr = child.communicate(timeout=window.timeout(10))
        except (subprocess.TimeoutExpired, TimeoutError) as interrupted:
            if isinstance(interrupted, subprocess.TimeoutExpired):
                stdout, stderr = interrupted.output or stdout, interrupted.stderr or stderr
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            try:
                stdout, stderr = child.communicate(timeout=window.timeout(10))
            except subprocess.TimeoutExpired as incomplete:
                stdout, stderr = incomplete.output or stdout, incomplete.stderr or stderr
                raise CleanupUnconfirmed(child.pid, stdout, stderr) from incomplete
            except TimeoutError as exhausted:
                raise CleanupUnconfirmed(child.pid, stdout, stderr) from exhausted
            raise CleanupUnconfirmed(child.pid, stdout, stderr) from interrupted
        return stdout, stderr
    except CleanupUnconfirmed:
        raise
    except BaseException as failure:
        unconfirmed = CleanupUnconfirmed(child.pid, stdout, stderr)
        unconfirmed.args = (*unconfirmed.args, f"shutdown failed: {failure!r}")
        raise unconfirmed from failure


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None,
    environment: Mapping[str, str],
    stdin: bytes | None,
    timeout_seconds: float,
    window: Window,
    native_shutdown: bool,
) -> tuple[int, bytes, bytes, bool]:
    """Bound a subprocess; finish supported shutdown before returning to credential settlement.

    Every post-start failure preventing output delivery crosses this boundary in ProcessIncomplete. CleanupUnconfirmed prevents
    settlement when the native owner could not finish stopping its separate tool groups.
    """
    timeout = window.reserving(20).timeout(timeout_seconds)
    child = subprocess.Popen(
        argv,
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    interruption: ProcessIncomplete | CleanupUnconfirmed | None = None
    stdout, stderr = b"", b""
    timed_out = False
    communication_failure: BaseException | None = None
    try:
        try:
            stdout, stderr = child.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired as interrupted:
            stdout, stderr = _cancel(
                child, native_shutdown, window, interrupted.output or b"", interrupted.stderr or b""
            )
            timed_out = True
        except BaseException as failure:
            stdout, stderr = _cancel(child, native_shutdown, window, stdout, stderr)
            communication_failure = failure
        if native_shutdown and (
            child.returncode < 0
            or b"in-process app-server shutdown failed" in stderr
            or b"thread/unsubscribe failed during shutdown" in stderr
        ):
            raise CleanupUnconfirmed(child.pid, stdout, stderr) from communication_failure
        if communication_failure is not None:
            raise ProcessIncomplete(stdout, stderr, communication_failure) from communication_failure
        return child.returncode, stdout, stderr, timed_out
    except (ProcessIncomplete, CleanupUnconfirmed) as failure:
        interruption = failure
        raise
    finally:
        # Popen.__exit__ waits without a timeout, including after unconfirmed cleanup.
        try:
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
        except BaseException as close_failure:
            if interruption is not None:
                interruption.args = (*interruption.args, f"pipe close failed: {close_failure!r}")
                close_failure.__cause__ = interruption.__cause__
                raise interruption from close_failure
            raise ProcessIncomplete(stdout, stderr, close_failure) from close_failure


def claude_environment() -> dict[str, str]:
    """Keep OS/login identity and the authentication input documented by Claude CLI --help.

    ANTHROPIC_API_KEY is authentication-only; desktop, messaging and session context never passes.
    Real HOME retains local OAuth discovery without copying credential files.
    """
    names = ("HOME", "PATH", "TMPDIR", "USER", "LOGNAME", "SHELL", "LANG", "ANTHROPIC_API_KEY")
    return {name: os.environ[name] for name in names if name in os.environ} | {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}


def run_tool(
    tool: Tool,
    arguments: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    stdin: str | None,
    timeout_seconds: float,
    window: Window,
) -> Completed:
    code, stdout, stderr, timed_out = _run(
        [executable(tool), *arguments],
        cwd=cwd,
        environment=environment,
        stdin=None if stdin is None else stdin.encode(),
        timeout_seconds=timeout_seconds,
        window=window,
        native_shutdown=tool is Tool.CODEX,
    )
    return Completed(code, stdout.decode(errors="replace"), stderr.decode(errors="replace"), timed_out)


def git(arguments: Sequence[str], *, cwd: Path, window: Window) -> Completed:
    """Suppress automatic maintenance before copying disposable fixture histories."""
    return run_tool(
        Tool.GIT,
        ["-c", "maintenance.auto=false", *arguments],
        cwd=cwd,
        environment=os.environ,
        stdin=None,
        timeout_seconds=300,
        window=window,
    )


def git_checked(arguments: Sequence[str], *, cwd: Path, window: Window) -> str:
    completed = git(arguments, cwd=cwd, window=window)
    if completed.timed_out:
        raise TimeoutError(f"git timed out in {cwd}; effects may be partial")
    if completed.returncode != 0:
        raise GitError(f"git {' '.join(arguments)} failed in {cwd}: {completed.stderr.strip()}")
    return completed.stdout


def git_archive(commit: str, *, cwd: Path, window: Window) -> bytes:
    code, stdout, stderr, timed_out = _run(
        [executable(Tool.GIT), "archive", "--format=tar", commit],
        cwd=cwd,
        environment=os.environ,
        stdin=None,
        timeout_seconds=300,
        window=window,
        native_shutdown=False,
    )
    if timed_out:
        raise TimeoutError("candidate archive timed out")
    if code != 0:
        raise GitError(f"git archive {commit} failed in {cwd}: {stderr.decode(errors='replace').strip()}")
    return stdout


class GitError(Exception):
    """A git command the harness needs for world construction failed."""


def launcher_prepare_runtime(launcher: Path, window: Window) -> Completed:
    return _launcher([str(launcher), "--prepare-runtime"], window)


def launcher_init(launcher: Path, project_root: Path, work_root: Path | None, window: Window) -> Completed:
    selected = [] if work_root is None else ["--work-root", str(work_root)]
    return _launcher([str(launcher), "--project-root", str(project_root), *selected, "init", "--json"], window)


def _launcher(argv: list[str], window: Window) -> Completed:
    code, stdout, stderr, timed_out = _run(
        argv,
        cwd=None,
        environment=os.environ,
        stdin=None,
        timeout_seconds=1800,
        window=window,
        native_shutdown=False,
    )
    return Completed(code, stdout.decode(errors="replace"), stderr.decode(errors="replace"), timed_out)
