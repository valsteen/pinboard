import errno
import hashlib
import os
import secrets
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from pinboard import diagnostic_codes
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode, ImmutableFilePublishedError


@dataclass(frozen=True, slots=True)
class SelectedInputFailure:
    code: diagnostic_codes.SelectedInputRejectedCode
    message: str


def read_verified_input(path: Path, expected_sha256: str) -> bytes | SelectedInputFailure:
    """Read one caller-selected file and hash those same bytes before decoding."""
    try:
        content = path.read_bytes()
    except OSError as error:
        return SelectedInputFailure(
            "INPUT_FILE_UNREADABLE",
            f"Cannot read selected input '{path}': {error}. Expected SHA-256 {expected_sha256}; observed SHA-256 is unavailable. Make that exact file readable before retrying.",
        )
    observed_sha256 = hashlib.sha256(content).hexdigest()
    if observed_sha256 != expected_sha256:
        return SelectedInputFailure(
            "INPUT_FILE_DIGEST_MISMATCH",
            f"Selected input '{path}' has SHA-256 {observed_sha256}, expected {expected_sha256}. "
            "Inspect the selected bytes and supply the intended file fingerprint before retrying.",
        )
    return content


@dataclass(frozen=True, slots=True)
class DurableRoots:
    anchor: Path
    work_components: tuple[str, ...]

    @property
    def work_root(self) -> Path:
        return self.anchor.joinpath(*self.work_components)

    @property
    def artifacts_root(self) -> Path:
        return self.work_root / "artifacts"

    @property
    def database_path(self) -> Path:
        return self.work_root / "state.sqlite3"


@dataclass(frozen=True, slots=True)
class DanglingSymlink:
    link: Path
    target: Path


@dataclass(frozen=True, slots=True)
class SymlinkLoop:
    link: Path
    target: Path


@dataclass(frozen=True, slots=True)
class NonDirectorySymlink:
    link: Path
    target: Path


type DirectoryLinkProblem = DanglingSymlink | SymlinkLoop | NonDirectorySymlink


def loop_target(link: Path) -> Path:
    try:
        raw_target = link.readlink()
    except OSError:
        return link.resolve(strict=False)
    return (link.parent / raw_target).absolute()


def first_directory_link_problem(path: Path) -> DirectoryLinkProblem | None:
    """Return the first broken or non-directory symbolic link at or above `path`."""
    for candidate in (*reversed(path.parents), path):
        if not candidate.is_symlink():
            continue
        target = candidate.resolve(strict=False)
        try:
            mode = candidate.stat().st_mode
        except FileNotFoundError:
            return DanglingSymlink(candidate, target)
        except OSError as error:
            if error.errno == errno.ELOOP:
                return SymlinkLoop(candidate, loop_target(candidate))
            if isinstance(error, NotADirectoryError):
                return NonDirectorySymlink(candidate, target)
            continue
        if not stat.S_ISDIR(mode):
            return NonDirectorySymlink(candidate, target)
    return None


def _verify_directory_links(path: Path, *, label: str) -> None:
    match first_directory_link_problem(path):
        case None:
            return
        case DanglingSymlink(link=link, target=target):
            raise FileIOError(
                FileIOErrorCode.DIRECTORY_VERIFY_FAILED,
                f"{label} could not be verified: {link} is a symbolic link to {target}, which does not exist. "
                f"Create {target}, or fix or remove the link {link}, then retry.",
            )
        case SymlinkLoop(link=link, target=target):
            raise FileIOError(
                FileIOErrorCode.DIRECTORY_VERIFY_FAILED,
                f"{label} could not be verified: {link} is a symbolic link loop to {target}. "
                f"Fix or remove the link {link}, then retry.",
            )
        case NonDirectorySymlink(link=link, target=target):
            raise FileIOError(
                FileIOErrorCode.DIRECTORY_VERIFY_FAILED,
                f"{label} could not be verified: {link} is a symbolic link to {target}, which is not a directory. "
                f"Fix or remove the link {link}, then retry.",
            )
        case _ as unreachable:
            assert_never(unreachable)


def _verified_directory(path: Path, *, label: str) -> Path:
    _verify_directory_links(path, label=label)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise FileIOError(FileIOErrorCode.DIRECTORY_VERIFY_FAILED, f"{label} could not be verified: {path}") from error
    if not resolved.is_dir():
        raise FileIOError(FileIOErrorCode.DIRECTORY_INVALID, f"{label} must be an existing directory: {path}")
    return resolved


def _validate_component(component: str) -> None:
    if component in {"", ".", ".."} or "/" in component or os.sep in component:
        raise FileIOError(FileIOErrorCode.DIRECTORY_INVALID, f"Invalid durable-root component: {component!r}")


def resolve_durable_roots(shared_repository_root: Path, external_work_root: Path | None = None) -> DurableRoots:
    shared_anchor = _verified_directory(shared_repository_root, label="Shared repository root")
    shared_root = shared_repository_root.absolute()
    if external_work_root is None or external_work_root.absolute() in {
        shared_root / ".pinboard",
        shared_anchor / ".pinboard",
    }:
        local_work_spelling = (
            external_work_root.absolute() if external_work_root is not None else shared_anchor / ".pinboard"
        )
        _verify_directory_links(local_work_spelling, label="Work root")
        local_work_root = shared_anchor / ".pinboard"
        if local_work_root.is_symlink():
            resolved_work_root = _verified_directory(local_work_root, label="Work root")
            _validate_component(resolved_work_root.name)
            return DurableRoots(resolved_work_root.parent, (resolved_work_root.name,))
        return DurableRoots(shared_anchor, (".pinboard",))

    external = external_work_root.absolute()
    _validate_component(external.name)
    anchor = _verified_directory(external.parent, label="External work-root parent")
    _verify_directory_links(external, label="Work root")
    candidate = anchor / external.name
    _verify_directory_links(candidate, label="Work root")
    if candidate.is_symlink():
        resolved_work_root = _verified_directory(candidate, label="Work root")
        _validate_component(resolved_work_root.name)
        return DurableRoots(resolved_work_root.parent, (resolved_work_root.name,))
    return DurableRoots(anchor, (external.name,))


def _sync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        raise FileIOError(
            FileIOErrorCode.DIRECTORY_SYNC_FAILED, "Directory synchronization is unsupported on this platform."
        )
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise FileIOError(
            FileIOErrorCode.DIRECTORY_SYNC_FAILED, f"Directory could not be synchronized: {path}"
        ) from error


def ensure_directory_chain(roots: DurableRoots) -> None:
    current_directory = _verified_directory(roots.anchor, label="Durable-root anchor")
    for component in (*roots.work_components, "artifacts"):
        _validate_component(component)
        child = current_directory / component
        _verify_directory_links(child, label="Durable-root component")
        try:
            child.mkdir()
        except FileExistsError:
            pass
        except OSError as error:
            raise FileIOError(
                FileIOErrorCode.DIRECTORY_CREATE_FAILED,
                f"Durable-root component could not be created: {child}",
            ) from error
        if not child.is_dir():
            raise FileIOError(
                FileIOErrorCode.DIRECTORY_INVALID,
                f"Durable-root component is not a directory: {child}",
            )
        _sync_directory(current_directory)
        current_directory = child.resolve()


def ensure_child_directory(parent: Path, component: str) -> Path:
    """Create or re-observe one child directory and durably record its entry."""

    _validate_component(component)
    verified_parent = _verified_directory(parent, label="Durable parent")
    child = verified_parent / component
    _verify_directory_links(child, label="Durable directory")
    try:
        child.mkdir()
    except FileExistsError:
        pass
    except OSError as error:
        raise FileIOError(
            FileIOErrorCode.DIRECTORY_CREATE_FAILED, f"Durable directory could not be created: {child}"
        ) from error
    if not child.is_dir():
        raise FileIOError(FileIOErrorCode.DIRECTORY_INVALID, f"Durable directory is not a directory: {child}")
    _sync_directory(verified_parent)
    return child.resolve()


def _write_and_sync(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _cleanup_staging(path: Path, parent: Path) -> None:
    try:
        path.unlink()
    except OSError:
        return
    with suppress(FileIOError):
        _sync_directory(parent)


def create_immutable(path: Path, content: bytes) -> bool:
    parent = _verified_directory(path.parent, label="Immutable-file parent")
    staging = parent / f".pinboard-stage-{secrets.token_hex(16)}"
    try:
        _write_and_sync(staging, content)
        try:
            os.link(staging, path, follow_symlinks=False)
        except FileExistsError as error:
            try:
                if path.is_file() and path.read_bytes() == content:
                    return False
            except OSError as verification_error:
                raise FileIOError(
                    FileIOErrorCode.FILE_PUBLISH_FAILED,
                    f"Immutable file could not be verified: {path}",
                ) from verification_error
            raise FileIOError(FileIOErrorCode.FILE_ALREADY_EXISTS, f"Immutable file already exists: {path}") from error
        try:
            _sync_directory(parent)
        except FileIOError as error:
            raise ImmutableFilePublishedError(path, error) from error
        return True
    except OSError as error:
        raise FileIOError(
            FileIOErrorCode.FILE_PUBLISH_FAILED, f"Immutable file could not be published: {path}"
        ) from error
    finally:
        _cleanup_staging(staging, parent)


def atomic_replace(path: Path, content: bytes) -> None:
    parent = _verified_directory(path.parent, label="Replacement-file parent")
    try:
        if path.is_file() and path.read_bytes() == content:
            return
    except OSError as error:
        raise FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, f"Replacement file could not be read: {path}") from error
    staging = parent / f".pinboard-stage-{secrets.token_hex(16)}"
    try:
        _write_and_sync(staging, content)
        staging.replace(path)
        _sync_directory(parent)
    except OSError as error:
        raise FileIOError(
            FileIOErrorCode.FILE_PUBLISH_FAILED, f"Replacement file could not be published: {path}"
        ) from error
    finally:
        _cleanup_staging(staging, parent)
