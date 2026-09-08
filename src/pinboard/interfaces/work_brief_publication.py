"""Publish one canonical work brief and present its accepted reference.

The command function reads the selected candidate, strictly decodes and
cross-validates it, canonicalizes its bytes, publishes the immutable artifact,
accepts its reference in SQLite, and presents that
stable reference. It returns advertised decision failures and lets filesystem,
storage, and malformed boundary data remain exact exceptions.
"""

from datetime import UTC, datetime

import msgspec

from pinboard.adapters.files import artifacts as artifact_files
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.application import artifact_publication, artifacts, ports
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.interfaces import cli_commands, work_briefs
from pinboard.interfaces.cli_output import write_json
from pinboard.interfaces.errors import (
    CommandFailure,
    CommandResult,
    WorkBriefErrorCode,
    WorkBriefFailure,
)


class BriefPublicationView(msgspec.Struct, frozen=True):
    artifact_ref_id: int
    kind: str
    key: str
    revision: int
    selector: str
    content_sha256: str
    size_bytes: int
    accepted_revision: int


def publish_brief(
    durable: DurableRoots,
    store: ports.WorkStore,
    command: cli_commands.BriefPublishCommand,
) -> CommandResult[int] | WorkBriefFailure:
    try:
        candidate_bytes = command.file.read_bytes()
    except OSError as error:
        return WorkBriefFailure(
            WorkBriefErrorCode.BRIEF_INVALID,
            f"Cannot read work brief candidate '{command.file}': {error}",
        )
    validated_brief = work_briefs.decode_work_brief(candidate_bytes)
    if isinstance(validated_brief, WorkBriefFailure):
        return validated_brief
    canonical_brief_bytes = work_briefs.canonical_work_brief_bytes(validated_brief)
    accepted_reference = artifact_publication.publish_accepted_artifact(
        store,
        artifact_files.ArtifactRepository(durable),
        artifacts.NewArtifact(
            work_models.ArtifactKind.BRIEF,
            validated_brief.attempt_id,
            validated_brief.artifact_revision,
            ".json",
            canonical_brief_bytes,
        ),
        datetime.now(UTC),
    )
    if isinstance(accepted_reference, DecisionFailure):
        return CommandFailure(accepted_reference.code, accepted_reference.message, accepted_reference.details)
    reference = accepted_reference.reference
    publication_view = BriefPublicationView(
        int(reference.artifact_ref_id),
        reference.kind.value,
        reference.key,
        reference.revision,
        reference.selector,
        reference.content_sha256,
        reference.size_bytes,
        reference.accepted_revision,
    )
    if command.json:
        write_json(publication_view)
    else:
        print(
            f"OK BRIEF_PUBLISHED artifact_ref_id={publication_view.artifact_ref_id} "
            f"selector={publication_view.selector} accepted_revision={publication_view.accepted_revision}"
        )
    return 0
