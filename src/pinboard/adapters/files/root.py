import fcntl
import subprocess
from pathlib import Path
from typing import BinaryIO

from pinboard.adapters.files.errors import RootError, RootErrorCode

PINBOARD_GIT_EXCLUDE = b"/.codex/pinboard/"
_READ_CHUNK_BYTES = 64 * 1024


def _resolve_git_path(cwd: Path, selector: str, unavailable_message: str) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", selector],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RootError(
            RootErrorCode.PROJECT_GIT_ROOT_UNAVAILABLE,
            result.stderr.strip() or unavailable_message,
        )
    return Path(result.stdout.strip()).resolve()


def _resolve_git_common_directory(cwd: Path) -> Path:
    common_directory = _resolve_git_path(
        cwd,
        "--git-common-dir",
        f"'{cwd}' is not inside a Git repository.",
    )
    if common_directory.name != ".git":
        raise RootError(
            RootErrorCode.PROJECT_GIT_LAYOUT_UNSUPPORTED,
            f"Expected the shared Git directory to end in '.git', found '{common_directory}'.",
        )
    return common_directory


def resolve_source_checkout_root(cwd: Path) -> Path:
    return _resolve_git_path(
        cwd,
        "--show-toplevel",
        f"'{cwd}' is not inside a Git checkout.",
    )


def resolve_shared_repository_root(cwd: Path) -> Path:
    return _resolve_git_common_directory(cwd).parent


def _exclude_contains_pinboard_line(stream: BinaryIO) -> tuple[bool, bool]:
    """Scan one Git exclude in bounded memory and report whether append needs a separator."""

    stream.seek(0)
    line_matches = True
    line_length = 0
    last_byte: int | None = None
    while chunk := stream.read(_READ_CHUNK_BYTES):
        for byte in chunk:
            last_byte = byte
            if byte in (10, 13):
                if line_matches and line_length == len(PINBOARD_GIT_EXCLUDE):
                    return True, False
                line_matches = True
                line_length = 0
                continue
            if line_length >= len(PINBOARD_GIT_EXCLUDE) or byte != PINBOARD_GIT_EXCLUDE[line_length]:
                line_matches = False
            line_length += 1
    if line_matches and line_length == len(PINBOARD_GIT_EXCLUDE):
        return True, False
    return False, last_byte is not None and last_byte not in (10, 13)


def ensure_default_git_exclude(shared_repository_root: Path) -> Path | None:
    """Exclude the default work root and return the path only when this call changed it."""

    try:
        common_directory = _resolve_git_common_directory(shared_repository_root)
    except RootError as error:
        if error.code == RootErrorCode.PROJECT_GIT_ROOT_UNAVAILABLE:
            return None
        raise
    exclude = common_directory / "info" / "exclude"
    try:
        try:
            with exclude.open("rb") as stream:
                contains_line, _ = _exclude_contains_pinboard_line(stream)
        except FileNotFoundError:
            pass
        else:
            if contains_line:
                return None
        with exclude.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            contains_line, needs_separator = _exclude_contains_pinboard_line(stream)
            if contains_line:
                return None
            stream.write((b"\n" if needs_separator else b"") + PINBOARD_GIT_EXCLUDE + b"\n")
    except OSError as error:
        raise RootError(
            RootErrorCode.PROJECT_GIT_EXCLUDE_UNAVAILABLE,
            f"Repository-local Git exclude could not be updated: {exclude}",
        ) from error
    return exclude
