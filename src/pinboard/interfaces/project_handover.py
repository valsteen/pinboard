"""Read-only composition for the complete portable project handover."""

import base64
from pathlib import PurePosixPath

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.errors import ArtifactError, ArtifactErrorCode
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.application import handover, ports
from pinboard.domain.identifiers import ArtifactRefId
from pinboard.interfaces import cli_commands, work_state
from pinboard.interfaces.cli_output import write_json
from pinboard.interfaces.errors import WorkBriefFailure, WorkBriefResult

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
    state: handover.HandoverState,
    artifacts: ArtifactRepository,
) -> tuple[
    tuple[handover.HandoverArtifactReference, ...],
    tuple[handover.HandoverArtifactContent, ...],
    dict[ArtifactRefId, bytes],
]:
    projected_references: list[handover.HandoverArtifactReference] = []
    encoded_contents: list[handover.HandoverArtifactContent] = []
    verified_artifacts: dict[ArtifactRefId, bytes] = {}
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
        verified_artifacts[reference.artifact_ref_id] = verified_bytes
        projected_references.append(handover.project_artifact_reference(reference, media_type=media_type))
        encoded_contents.append(_encode_artifact_content(int(reference.artifact_ref_id), verified_bytes))
    return tuple(projected_references), tuple(encoded_contents), verified_artifacts


def export_project_handover(
    durable: DurableRoots, store: ports.HandoverReader, _command: cli_commands.HandoverCommand
) -> WorkBriefResult[int]:
    captured_state = handover.merge_handover_batches(store.read_handover_batches())
    artifact_repository = ArtifactRepository(durable)
    projected_references, encoded_contents, verified_artifacts = _read_and_encode_artifacts(
        captured_state, artifact_repository
    )
    checkpoint_packages = work_state.validate_checkpoint_review_packages(
        captured_state.lifecycle,
        captured_state.artifact_references,
        captured_state.transition_receipts,
        verified_artifacts,
    )
    if isinstance(checkpoint_packages, WorkBriefFailure):
        return checkpoint_packages
    portable_package = handover.project_handover_from_state(
        captured_state,
        projected_references,
        encoded_contents,
        checkpoint_packages,
    )
    write_json(portable_package)
    return 0
