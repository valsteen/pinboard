import fcntl
import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from pinboard.adapters.files.errors import RootError, RootErrorCode

_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class WorkingTreeCandidate:
    identity: str
    diff: bytes


@dataclass(frozen=True, slots=True)
class CurrentHeadCandidate:
    identity: str
    diff: bytes


@dataclass(frozen=True, slots=True)
class DifferentHeadCandidate:
    candidate_revision: str
    current_head: str


@dataclass(frozen=True, slots=True)
class DirtyHeadCandidate:
    candidate_revision: str


type CommittedCandidateObservation = CurrentHeadCandidate | DifferentHeadCandidate | DirtyHeadCandidate


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


def _git_text(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise RootError(
            RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE,
            result.stderr.strip() or f"Cannot observe Git checkout at '{cwd}'.",
        )
    return value


def _git_bytes(cwd: Path, *arguments: str, unavailable_message: str) -> bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RootError(
            RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE,
            result.stderr.decode(errors="replace").strip() or unavailable_message,
        )
    return result.stdout


def observe_checkout_identity(cwd: Path) -> tuple[str, str]:
    """Return the exact current branch and HEAD revision for one selected checkout."""

    branch = _git_text(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
    revision = _git_text(cwd, "rev-parse", "--verify", "HEAD")
    return branch, revision


def read_working_tree_candidate(cwd: Path) -> WorkingTreeCandidate:
    """Read the binary HEAD diff without changing Git state."""

    diff = _git_bytes(
        cwd,
        "diff",
        "--binary",
        "HEAD",
        "--",
        unavailable_message=f"Cannot read the working-tree diff at '{cwd}'.",
    )
    digest = hashlib.sha256(diff).hexdigest()
    return WorkingTreeCandidate(f"working-tree-sha256:{digest}", diff)


def read_current_head_candidate(
    cwd: Path,
    candidate_revision: str,
    base_revision: str,
) -> CommittedCandidateObservation:
    """Read a clean exact-HEAD candidate from its accepted base without changing Git."""

    current_head = _git_text(cwd, "rev-parse", "--verify", "HEAD")
    if current_head != candidate_revision:
        return DifferentHeadCandidate(candidate_revision, current_head)
    status = _git_bytes(
        cwd,
        "--no-optional-locks",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        unavailable_message=f"Cannot read the working-tree status at '{cwd}'.",
    )
    if status:
        return DirtyHeadCandidate(candidate_revision)
    diff = _git_bytes(
        cwd,
        "diff",
        "--binary",
        base_revision,
        candidate_revision,
        "--",
        unavailable_message=f"Cannot compare accepted base '{base_revision}' with '{candidate_revision}'.",
    )
    return CurrentHeadCandidate(candidate_revision, diff)


def resolve_shared_repository_root(cwd: Path) -> Path:
    return _resolve_git_common_directory(cwd).parent


def _exclude_contains_pinboard_line(stream: BinaryIO, entry: bytes) -> tuple[bool, bool]:
    """Scan one Git exclude in bounded memory and report whether append needs a separator."""

    stream.seek(0)
    line_matches = True
    line_length = 0
    last_byte: int | None = None
    while chunk := stream.read(_READ_CHUNK_BYTES):
        for byte in chunk:
            last_byte = byte
            if byte in (10, 13):
                if line_matches and line_length == len(entry):
                    return True, False
                line_matches = True
                line_length = 0
                continue
            if line_length >= len(entry) or byte != entry[line_length]:
                line_matches = False
            line_length += 1
    if line_matches and line_length == len(entry):
        return True, False
    return False, last_byte is not None and last_byte not in (10, 13)


def ensure_git_exclude(shared_repository_root: Path, entry: bytes) -> Path | None:
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
                contains_line, _ = _exclude_contains_pinboard_line(stream, entry)
        except FileNotFoundError:
            pass
        else:
            if contains_line:
                return None
        with exclude.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            contains_line, needs_separator = _exclude_contains_pinboard_line(stream, entry)
            if contains_line:
                return None
            stream.write((b"\n" if needs_separator else b"") + entry + b"\n")
    except OSError as error:
        raise RootError(
            RootErrorCode.PROJECT_GIT_EXCLUDE_UNAVAILABLE,
            f"Repository-local Git exclude could not be updated: {exclude}",
        ) from error
    return exclude
