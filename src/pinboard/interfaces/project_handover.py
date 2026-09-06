"""Read-only composition for the complete portable project handover."""

import base64
from pathlib import PurePosixPath

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.errors import ArtifactError, ArtifactErrorCode
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import handover, stored_state
from pinboard.interfaces import cli_commands
from pinboard.interfaces.cli_output import write_json

MEDIA_TYPE_BY_SUFFIX = {
    ".json": "application/json",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


def _encode_artifact_content(reference_id: int, value: bytes) -> handover.HandoverArtifactContent:
    try:
        content = value.decode("utf-8")
    except UnicodeDecodeError:
        return handover.HandoverArtifactContent(
            reference_id,
            handover.ContentEncoding.BASE64,
            base64.b64encode(value).decode("ascii"),
        )
    return handover.HandoverArtifactContent(reference_id, handover.ContentEncoding.UTF8, content)


def _read_and_encode_artifacts(
    state: stored_state.StoredWorkState,
    artifacts: ArtifactRepository,
) -> tuple[tuple[handover.HandoverArtifactReference, ...], tuple[handover.HandoverArtifactContent, ...]]:
    projected_references: list[handover.HandoverArtifactReference] = []
    encoded_contents: list[handover.HandoverArtifactContent] = []
    for reference in state.artifact_references:
        suffix = PurePosixPath(reference.selector).suffix.lower()
        try:
            media_type = MEDIA_TYPE_BY_SUFFIX[suffix]
        except KeyError as error:
            raise ArtifactError(
                ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION,
                f"Unsupported artifact media suffix: {suffix or '<none>'}",
            ) from error
        verified_bytes = artifacts.read(reference)
        projected_references.append(handover.project_artifact_reference(reference, media_type=media_type))
        encoded_contents.append(_encode_artifact_content(int(reference.artifact_ref_id), verified_bytes))
    return tuple(projected_references), tuple(encoded_contents)


def export_project_handover(roots: cli_commands.ResolvedRoots, _command: cli_commands.HandoverCommand) -> int:
    captured_state = SQLiteWorkStore(roots.work / "state.sqlite3").snapshot()
    artifact_repository = ArtifactRepository(resolve_durable_roots(roots.shared_repository, roots.work))
    projected_references, encoded_contents = _read_and_encode_artifacts(captured_state, artifact_repository)
    portable_package = handover.project_handover_from_state(
        captured_state,
        projected_references,
        encoded_contents,
    )
    write_json(portable_package)
    return 0
