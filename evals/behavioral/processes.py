"""The harness's only subprocess owner: the claude, codex and git CLIs and the evaluated revision's launcher.

Executables other than the launcher resolve through PATH. The launcher is always the exported revision's own
``scripts/pinboard``; it runs only for ``--prepare-runtime``, board ``init`` and ``--mcp`` (the last through the
MCP stdio client in ``board``).
"""

import os
import shutil
import subprocess
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


def run_tool(
    tool: Tool,
    arguments: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    stdin: str | None,
    timeout_seconds: float,
) -> Completed:
    completed = subprocess.run(
        [executable(tool), *arguments],
        cwd=cwd,
        env=dict(environment),
        input=stdin,
        stdin=subprocess.DEVNULL if stdin is None else None,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return Completed(completed.returncode, completed.stdout, completed.stderr)


def git(arguments: Sequence[str], *, cwd: Path) -> Completed:
    return run_tool(Tool.GIT, arguments, cwd=cwd, environment=os.environ, stdin=None, timeout_seconds=300)


def git_checked(arguments: Sequence[str], *, cwd: Path) -> str:
    completed = git(arguments, cwd=cwd)
    if completed.returncode != 0:
        raise GitError(f"git {' '.join(arguments)} failed in {cwd}: {completed.stderr.strip()}")
    return completed.stdout


def git_archive(commit: str, *, cwd: Path) -> bytes:
    completed = subprocess.run(
        [executable(Tool.GIT), "archive", "--format=tar", commit],
        cwd=cwd,
        capture_output=True,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        raise GitError(f"git archive {commit} failed in {cwd}: {completed.stderr.decode(errors='replace').strip()}")
    return completed.stdout


class GitError(Exception):
    """A git command the harness needs for world construction failed."""


def launcher_prepare_runtime(launcher: Path) -> Completed:
    return _launcher([str(launcher), "--prepare-runtime"])


def launcher_init(launcher: Path, project_root: Path, work_root: Path | None) -> Completed:
    selected = [] if work_root is None else ["--work-root", str(work_root)]
    return _launcher([str(launcher), "--project-root", str(project_root), *selected, "init", "--json"])


def _launcher(argv: list[str]) -> Completed:
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=1800, check=False)
    return Completed(completed.returncode, completed.stdout, completed.stderr)
