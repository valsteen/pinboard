import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath

from pinboard.adapters.files.errors import ArtifactError, ArtifactErrorCode, FileIOError
from pinboard.adapters.files.file_io import (
    DurableRoots,
    create_immutable,
    ensure_child_directory,
    ensure_directory_chain,
)
from pinboard.application import stored_state
from pinboard.application.artifacts import ArtifactRef, BriefArtifactRef, NewArtifact
from pinboard.domain import work_models

_DIRECTORIES: dict[work_models.ArtifactKind, str] = {
    work_models.ArtifactKind.REQUIREMENTS: "requirements",
    work_models.ArtifactKind.BRIEF: "briefs",
    work_models.ArtifactKind.RESULT: "results",
    work_models.ArtifactKind.EVIDENCE: "evidence",
}


def _validate_identity_component(value: str, *, label: str) -> str:
    if not value or value in {".", ".."} or "/" in value or os.sep in value or "\x00" in value:
        raise ArtifactError(
            ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION,
            f"Artifact {label} is not a stable path component.",
        )
    return value


def _validate_suffix(value: str) -> str:
    if not value.startswith(".") or value in {".", ".."} or "/" in value or os.sep in value or "\x00" in value:
        raise ArtifactError(ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION, "Artifact suffix is not canonical.")
    return value


def _build_selector(kind: work_models.ArtifactKind, key: str, revision: int, suffix: str) -> str:
    if revision < 1:
        raise ArtifactError(ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION, "Artifact revision must be positive.")
    return PurePosixPath(
        "artifacts",
        _DIRECTORIES[kind],
        _validate_identity_component(key, label="key"),
        f"{revision}{_validate_suffix(suffix)}",
    ).as_posix()


def _validate_and_resolve_reference_path(
    reference: ArtifactRef | BriefArtifactRef | stored_state.ArtifactReference,
) -> Path:
    pure = PurePosixPath(reference.selector)
    parts = pure.parts
    if pure.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise ArtifactError(ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION, "Artifact selector is not canonical.")
    if len(parts) != 4 or parts[:3] != (
        "artifacts",
        _DIRECTORIES[reference.kind],
        _validate_identity_component(reference.key, label="key"),
    ):
        raise ArtifactError(
            ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION,
            "Artifact selector does not match its identity.",
        )
    filename = parts[3]
    prefix, separator, suffix = filename.partition(".")
    if not separator or prefix != str(reference.revision) or not suffix:
        raise ArtifactError(
            ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION,
            "Artifact selector does not match its revision.",
        )
    return Path(*parts)


def read_reference(
    work_root: Path,
    reference: ArtifactRef | BriefArtifactRef | stored_state.ArtifactReference,
) -> bytes:
    relative = _validate_and_resolve_reference_path(reference)
    try:
        data = (work_root / relative).read_bytes()
    except OSError as error:
        raise ArtifactError(
            ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION,
            "Artifact bytes could not be read.",
        ) from error
    if len(data) != reference.size_bytes or sha256(data).hexdigest() != reference.content_sha256:
        raise ArtifactError(
            ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION,
            "Artifact size or digest does not match its reference.",
        )
    return data


def verify_reference(
    work_root: Path,
    reference: ArtifactRef | BriefArtifactRef | stored_state.ArtifactReference,
) -> None:
    read_reference(work_root, reference)


def write_revision(roots: DurableRoots, artifact: NewArtifact) -> ArtifactRef:
    selector = _build_selector(artifact.kind, artifact.key, artifact.revision, artifact.suffix)
    digest = sha256(artifact.content).hexdigest()
    reference = ArtifactRef(artifact.kind, artifact.key, artifact.revision, selector, digest, len(artifact.content))
    try:
        ensure_directory_chain(roots)
        kind_root = ensure_child_directory(roots.artifacts_root, _DIRECTORIES[artifact.kind])
        ensure_child_directory(kind_root, artifact.key)
        path = roots.work_root / selector
        try:
            path.lstat()
        except FileNotFoundError:
            try:
                create_immutable(path, artifact.content)
            except FileIOError:
                try:
                    verify_reference(roots.work_root, reference)
                except ArtifactError as collision:
                    raise ArtifactError(
                        ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION,
                        "Artifact revision could not be published immutably.",
                    ) from collision
        else:
            verify_reference(roots.work_root, reference)
        return reference
    except FileIOError as error:
        raise ArtifactError(ArtifactErrorCode.STORAGE_IO_ERROR, str(error)) from error


@dataclass(frozen=True, slots=True)
class ArtifactRepository:
    """Concrete durable artifact access used by interface composition."""

    roots: DurableRoots

    @property
    def work_root(self) -> Path:
        return self.roots.work_root

    def read(self, reference: stored_state.ArtifactReference) -> bytes:
        return read_reference(self.work_root, reference)

    def revision_exists(self, artifact: NewArtifact) -> bool:
        """Return whether this exact immutable artifact revision already has a filesystem entry."""
        selector = _build_selector(artifact.kind, artifact.key, artifact.revision, artifact.suffix)
        try:
            (self.work_root / selector).lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise ArtifactError(
                ArtifactErrorCode.STORAGE_IO_ERROR,
                "Artifact revision presence could not be inspected.",
            ) from error
        return True

    def publish(self, artifact: NewArtifact) -> ArtifactRef:
        return write_revision(self.roots, artifact)
