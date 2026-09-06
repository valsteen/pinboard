import os
import secrets
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode


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


def _verified_directory(path: Path, *, label: str) -> Path:
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
    local_work_root = shared_repository_root.absolute() / ".codex" / "pinboard"
    if external_work_root is None or external_work_root.absolute() == local_work_root:
        anchor = _verified_directory(shared_repository_root, label="Shared repository root")
        return DurableRoots(anchor, (".codex", "pinboard"))

    external = external_work_root.absolute()
    _validate_component(external.name)
    if external.parent == local_work_root.parent:
        anchor = _verified_directory(shared_repository_root, label="Shared repository root")
        return DurableRoots(anchor, (".codex", external.name))
    anchor = _verified_directory(external.parent, label="External work-root parent")
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
    current_directory = roots.anchor
    for component in (*roots.work_components, "artifacts"):
        _validate_component(component)
        child = current_directory / component
        try:
            child.mkdir()
        except FileExistsError:
            pass
        except OSError as error:
            raise FileIOError(
                FileIOErrorCode.DIRECTORY_CREATE_FAILED,
                f"Durable-root component could not be created: {child}",
            ) from error
        if child.is_symlink() or not child.is_dir():
            raise FileIOError(
                FileIOErrorCode.DIRECTORY_INVALID,
                f"Durable-root component is not a real directory: {child}",
            )
        _sync_directory(current_directory)
        current_directory = child


def ensure_child_directory(parent: Path, component: str) -> Path:
    """Create or re-observe one real child directory and durably record its entry."""

    _validate_component(component)
    verified_parent = _verified_directory(parent, label="Durable parent")
    child = verified_parent / component
    try:
        child.mkdir()
    except FileExistsError:
        pass
    except OSError as error:
        raise FileIOError(
            FileIOErrorCode.DIRECTORY_CREATE_FAILED, f"Durable directory could not be created: {child}"
        ) from error
    if child.is_symlink() or not child.is_dir():
        raise FileIOError(FileIOErrorCode.DIRECTORY_INVALID, f"Durable directory is not a real directory: {child}")
    _sync_directory(verified_parent)
    return child


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


def create_immutable(path: Path, content: bytes) -> None:
    parent = _verified_directory(path.parent, label="Immutable-file parent")
    staging = parent / f".pinboard-stage-{secrets.token_hex(16)}"
    try:
        _write_and_sync(staging, content)
        try:
            os.link(staging, path, follow_symlinks=False)
        except FileExistsError as error:
            raise FileIOError(FileIOErrorCode.FILE_ALREADY_EXISTS, f"Immutable file already exists: {path}") from error
        _sync_directory(parent)
    except OSError as error:
        raise FileIOError(
            FileIOErrorCode.FILE_PUBLISH_FAILED, f"Immutable file could not be published: {path}"
        ) from error
    finally:
        _cleanup_staging(staging, parent)


def atomic_replace(path: Path, content: bytes) -> None:
    parent = _verified_directory(path.parent, label="Replacement-file parent")
    try:
        if path.is_file(follow_symlinks=False) and path.read_bytes() == content:
            return
    except OSError:
        pass
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
