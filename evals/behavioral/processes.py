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


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None,
    environment: Mapping[str, str],
    stdin: bytes | None,
    timeout_seconds: float,
    window: Window,
) -> tuple[int, bytes, bytes, bool]:
    """Bound a subprocess and its inherited process group; retain partial bytes on timeout.

    Cleanup completes before returning to credential settlement. macOS and Linux both support POSIX groups.
    """
    timeout = window.timeout(timeout_seconds)
    with subprocess.Popen(
        argv,
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as child:
        try:
            stdout, stderr = child.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            stdout, stderr = child.communicate(timeout=10)
            return child.returncode, stdout, stderr, True
        except BaseException:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate(timeout=10)
            raise
        return child.returncode, stdout, stderr, False


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
    )
    return Completed(code, stdout.decode(errors="replace"), stderr.decode(errors="replace"), timed_out)


def git(arguments: Sequence[str], *, cwd: Path, window: Window) -> Completed:
    return run_tool(
        Tool.GIT, arguments, cwd=cwd, environment=os.environ, stdin=None, timeout_seconds=300, window=window
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
    )
    return Completed(code, stdout.decode(errors="replace"), stderr.decode(errors="replace"), timed_out)
