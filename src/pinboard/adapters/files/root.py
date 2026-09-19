import fcntl
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from pinboard.adapters.files.errors import RootError, RootErrorCode
from pinboard.application.candidate_identity import working_tree_identity
from pinboard.domain import work_models

PINBOARD_GIT_EXCLUDE = b"/.codex/pinboard/"
_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class WorkingTreeCandidate:
    identity: str
    preimage_revision: str
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


@dataclass(frozen=True, slots=True)
class CandidateRestoreSuccess:
    changed: bool
    candidate: str


@dataclass(frozen=True, slots=True)
class CandidateRestoreRejection:
    reason: str
    branch: str
    head: str


type CandidateRestoreResult = CandidateRestoreSuccess | CandidateRestoreRejection


class CandidateRestoreAfterMutationError(RootError):
    """The checkout changed before exact restoration verification failed."""


def _resolve_git_path(cwd: Path, selector: str, unavailable_message: str) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", selector],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise CandidateRestoreAfterMutationError(
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
        raise CandidateRestoreAfterMutationError(
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


def classify_checkout(cwd: Path) -> work_models.CheckoutSelection:
    """Classify one supported Git checkout without relying on its current branch name."""

    source_root = resolve_source_checkout_root(cwd)
    common_directory = _resolve_git_common_directory(cwd)
    git_directory = _resolve_git_path(cwd, "--git-dir", f"Cannot resolve the Git directory for '{cwd}'.")
    primary_root = common_directory.parent.resolve()
    if source_root == primary_root and git_directory == common_directory:
        return work_models.CheckoutSelection.MAIN
    registered = _git_bytes(
        cwd,
        "worktree",
        "list",
        "--porcelain",
        "-z",
        unavailable_message=f"Cannot read registered Git worktrees for '{cwd}'.",
    )
    worktree_roots = {
        Path(field.removeprefix(b"worktree ").decode()).resolve()
        for field in registered.split(b"\0")
        if field.startswith(b"worktree ")
    }
    if source_root != primary_root and git_directory != common_directory and source_root in worktree_roots:
        return work_models.CheckoutSelection.ISOLATED
    raise RootError(
        RootErrorCode.PROJECT_GIT_LAYOUT_UNSUPPORTED,
        f"Checkout '{source_root}' is neither the primary checkout nor a registered linked worktree.",
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
    """Read actual full HEAD and its exact binary diff without changing Git state."""

    diff = _git_bytes(
        cwd,
        "-c",
        "diff.autoRefreshIndex=false",
        "diff",
        "--binary",
        "HEAD",
        "--",
        unavailable_message=f"Cannot read the working-tree diff at '{cwd}'.",
    )
    head = _git_text(cwd, "rev-parse", "--verify", "HEAD")
    return WorkingTreeCandidate(working_tree_identity(head, diff), head, diff)


def read_untracked_paths(cwd: Path) -> tuple[str, ...]:
    """Read Git-visible nonignored paths excluded from the tracked candidate diff."""

    paths = _git_bytes(
        cwd,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        unavailable_message=f"Cannot read untracked paths at '{cwd}'.",
    )
    return tuple(path.decode() for path in paths.split(b"\0") if path)


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


def _working_tree_status(cwd: Path) -> bytes:
    return _git_bytes(
        cwd,
        "--no-optional-locks",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        unavailable_message=f"Cannot read the working-tree status at '{cwd}'.",
    )


def restore_working_tree_candidate(
    cwd: Path,
    *,
    expected_branch: str,
    preimage_revision: str,
    candidate: str,
    diff: bytes,
) -> CandidateRestoreResult:
    """Apply one exact working-tree snapshot with index participation."""

    branch, head = observe_checkout_identity(cwd)
    if branch != expected_branch:
        return CandidateRestoreRejection("wrong-branch", branch, head)
    if head != preimage_revision:
        return CandidateRestoreRejection("wrong-head", branch, head)
    current = read_working_tree_candidate(cwd)
    if current.identity == candidate and current.diff == diff:
        if any(record.startswith(b"?? ") for record in _working_tree_status(cwd).split(b"\0")):
            return CandidateRestoreRejection("dirty-working-tree", branch, head)
        return CandidateRestoreSuccess(False, candidate)
    if _working_tree_status(cwd):
        return CandidateRestoreRejection("dirty-working-tree", branch, head)
    applied = subprocess.run(
        ["git", "apply", "--index", "--binary", "-"],
        cwd=cwd,
        input=diff,
        capture_output=True,
        check=False,
    )
    if applied.returncode != 0:
        return CandidateRestoreRejection("patch-rejected", branch, head)
    try:
        restored = read_working_tree_candidate(cwd)
    except RootError as error:
        raise CandidateRestoreAfterMutationError(
            error.code,
            "Candidate restoration changed the checkout before exact snapshot verification failed.",
        ) from error
    if restored.identity != candidate or restored.diff != diff:
        raise CandidateRestoreAfterMutationError(
            RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE,
            "Candidate restoration changed the checkout but did not produce the exact snapshot.",
        )
    return CandidateRestoreSuccess(True, candidate)


def restore_commit_candidate(
    cwd: Path,
    *,
    expected_branch: str,
    preimage_revision: str,
    accepted_base_revision: str,
    candidate: str,
    diff: bytes,
) -> CandidateRestoreResult:
    """Reuse or fast-forward one exact clean commit candidate."""

    branch, head = observe_checkout_identity(cwd)
    if branch != expected_branch:
        return CandidateRestoreRejection("wrong-branch", branch, head)
    if _working_tree_status(cwd):
        return CandidateRestoreRejection("dirty-working-tree", branch, head)
    if head not in {preimage_revision, candidate}:
        return CandidateRestoreRejection("wrong-head", branch, head)
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{candidate}^{{commit}}"], cwd=cwd, capture_output=True, check=False
    )
    if exists.returncode != 0:
        return CandidateRestoreRejection("missing-commit", branch, head)
    observed = _git_bytes(
        cwd,
        "diff",
        "--binary",
        accepted_base_revision,
        candidate,
        "--",
        unavailable_message=f"Cannot compare accepted base '{accepted_base_revision}' with '{candidate}'.",
    )
    if observed != diff:
        return CandidateRestoreRejection("candidate-diff-mismatch", branch, head)
    if head == candidate:
        return CandidateRestoreSuccess(False, candidate)
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", preimage_revision, candidate],
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    if ancestor.returncode != 0:
        return CandidateRestoreRejection("non-fast-forward", branch, head)
    advanced = subprocess.run(["git", "merge", "--ff-only", candidate], cwd=cwd, capture_output=True, check=False)
    if advanced.returncode != 0:
        return CandidateRestoreRejection("fast-forward-rejected", branch, head)
    try:
        restored_branch, restored_head = observe_checkout_identity(cwd)
        restored_status = _working_tree_status(cwd)
    except RootError as error:
        raise CandidateRestoreAfterMutationError(
            error.code,
            "Candidate restoration changed the checkout before exact commit verification failed.",
        ) from error
    if restored_branch != expected_branch or restored_head != candidate or restored_status:
        raise CandidateRestoreAfterMutationError(
            RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE,
            "Candidate restoration changed the checkout but did not produce the exact clean commit.",
        )
    return CandidateRestoreSuccess(True, candidate)


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
