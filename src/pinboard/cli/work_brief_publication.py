"""Publish one canonical work brief and present its accepted reference.

The command function reads the selected candidate, strictly decodes and
cross-validates it, canonicalizes its bytes, publishes the immutable artifact,
accepts its reference in SQLite, and presents that
stable reference. It returns advertised decision failures and lets filesystem,
storage, and malformed boundary data remain exact exceptions.
"""

import hashlib
import shlex
import sys
from datetime import UTC, datetime
from typing import Literal

import msgspec

from pinboard.adapters.files import artifacts as artifact_files
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.files.models import AffectedViews
from pinboard.application import (
    artifact_publication,
    artifacts,
    ports,
    stored_state,
    work_brief_models,
    work_briefs,
)
from pinboard.cli import agent_launch, cli_commands, work_views
from pinboard.cli.cli_output import write_json
from pinboard.cli.errors import (
    CommandFailure,
    CommandResult,
)
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import ArtifactRefId


class BriefPublicationView(msgspec.Struct, frozen=True):
    artifact_ref_id: int
    kind: str
    key: str
    revision: int
    selector: str
    content_sha256: str
    size_bytes: int
    accepted_revision: int


class NoNeedsCorrectionEvidenceView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-work-brief-review-status/v1"]
    status: Literal["no-needs-correction-evidence"]
    attempt_id: str
    checkpoint_id: str
    accepted_brief_sha256: str
    status_command: str
    correction_command: str
    republication_command: str
    independent_rereview_instruction: str


class NeedsCorrectionEvidenceView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-work-brief-review-status/v1"]
    status: Literal["needs-correction"]
    attempt_id: str
    checkpoint_id: str
    accepted_brief_sha256: str
    artifact_revision: int
    reference: BriefPublicationView
    reviewer_task_id: str
    findings: tuple[work_brief_models.BlockingReviewFinding, ...]
    status_command: str
    correction_command: str
    republication_command: str
    independent_rereview_instruction: str


def publish_brief(
    durable: DurableRoots,
    store: ports.WorkStore,
    command: cli_commands.BriefPublishCommand,
) -> CommandResult[int] | work_brief_models.WorkBriefFailure:
    try:
        candidate_bytes = command.file.read_bytes()
    except OSError as error:
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.BRIEF_INVALID,
            f"Cannot read work brief candidate '{command.file}': {error}",
        )
    validated_brief = work_briefs.decode_work_brief(candidate_bytes)
    if isinstance(validated_brief, work_brief_models.WorkBriefFailure):
        return validated_brief
    operation_time = datetime.now(UTC)
    accepted_reference = work_briefs.publish_work_brief(
        store,
        artifact_files.ArtifactRepository(durable),
        validated_brief,
        operation_time,
    )
    if isinstance(accepted_reference, DecisionFailure):
        return CommandFailure(accepted_reference.code, accepted_reference.message, accepted_reference.details)
    view_result = work_views.refresh(durable, store, AffectedViews((), (), ()), operation_time)
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
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


def _publication_view(reference: stored_state.ArtifactReference) -> BriefPublicationView:
    return BriefPublicationView(
        int(reference.artifact_ref_id),
        reference.kind.value,
        reference.key,
        reference.revision,
        reference.selector,
        reference.content_sha256,
        reference.size_bytes,
        reference.accepted_revision,
    )


def _read_accepted_brief(
    durable: DurableRoots,
    store: ports.WorkStore,
    brief_artifact_ref_id: int,
) -> work_brief_models.WorkBriefFailure | tuple[work_brief_models.WorkBrief, bytes]:
    reference = store.read_artifact_reference_by_id(ArtifactRefId(brief_artifact_ref_id))
    if reference is None or reference.kind != work_models.ArtifactKind.BRIEF:
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.BRIEF_INVALID,
            "The selected accepted brief reference does not exist or is not a brief.",
        )
    content = artifact_files.read_reference(durable.work_root, reference)
    brief = work_briefs.decode_canonical_work_brief(content)
    if isinstance(brief, work_brief_models.WorkBriefFailure):
        return brief
    return brief, content


def publish_brief_review_needs_correction(
    durable: DurableRoots,
    store: ports.WorkStore,
    command: cli_commands.BriefReviewNeedsCorrectionCommand,
) -> CommandResult[int] | work_brief_models.WorkBriefFailure:
    selected = _read_accepted_brief(durable, store, command.brief_artifact_ref_id)
    if isinstance(selected, work_brief_models.WorkBriefFailure):
        return selected
    brief, _brief_bytes = selected
    try:
        candidate = command.file.read_bytes()
    except OSError as error:
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_INVALID,
            f"Cannot read needs-correction brief review candidate '{command.file}': {error}",
        )
    review = work_briefs.decode_canonical_work_brief_review_needs_correction(candidate)
    if isinstance(review, work_brief_models.WorkBriefFailure):
        return review
    if (failure := work_briefs.validate_work_brief_review_needs_correction(review, brief)) is not None:
        return failure
    accepted = artifact_publication.publish_accepted_artifact(
        store,
        artifact_files.ArtifactRepository(durable),
        artifacts.NewArtifact(
            work_models.ArtifactKind.EVIDENCE,
            work_briefs.needs_correction_review_key(brief),
            review.artifact_revision,
            ".json",
            candidate,
        ),
        datetime.now(UTC),
    )
    if isinstance(accepted, DecisionFailure):
        return CommandFailure(accepted.code, accepted.message, accepted.details)
    view = _publication_view(accepted.reference)
    if command.json:
        write_json(view)
    else:
        print(
            f"OK BRIEF_REVIEW_NEEDS_CORRECTION artifact_ref_id={view.artifact_ref_id} "
            f"selector={view.selector} accepted_revision={view.accepted_revision}"
        )
    return 0


def _review_recovery(
    roots: cli_commands.ResolvedRoots,
    brief_artifact_ref_id: int,
) -> tuple[str, str, str, str]:
    prefix = (
        *agent_launch.pinboard_launcher_command(),
        "--project-root",
        str(roots.source_checkout),
        "--work-root",
        str(roots.work),
    )
    return (
        shlex.join(
            (*prefix, "brief", "review-status", "--brief-artifact-ref-id", str(brief_artifact_ref_id), "--json")
        ),
        shlex.join((*prefix, "tool-contract", "--operation", "brief/publish", "--json")),
        shlex.join((*prefix, "brief", "publish", "--file", "<corrected-brief.json>", "--json")),
        (
            "Commission a fresh independent brief reviewer for the corrected accepted brief. "
            "Publish another needs-correction record only if blocking findings remain; ready evidence remains dispatch-only."
        ),
    )


def show_brief_review_status(
    roots: cli_commands.ResolvedRoots,
    durable: DurableRoots,
    store: ports.WorkStore,
    command: cli_commands.BriefReviewStatusCommand,
) -> CommandResult[int] | work_brief_models.WorkBriefFailure:
    selected = _read_accepted_brief(durable, store, command.brief_artifact_ref_id)
    if isinstance(selected, work_brief_models.WorkBriefFailure):
        return selected
    brief, brief_bytes = selected
    checkpoint = brief.checkpoint
    if not isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint):
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_INVALID, "Local checkpoints do not use brief reviews."
        )
    brief_sha256 = hashlib.sha256(brief_bytes).hexdigest()
    status_command, correction_command, republication_command, rereview_instruction = _review_recovery(
        roots, command.brief_artifact_ref_id
    )
    reference = store.read_latest_artifact_reference(
        work_models.ArtifactKind.EVIDENCE,
        work_briefs.needs_correction_review_key(brief),
    )
    if reference is None:
        view = NoNeedsCorrectionEvidenceView(
            "pinboard-work-brief-review-status/v1",
            "no-needs-correction-evidence",
            brief.attempt_id,
            checkpoint.checkpoint_id,
            brief_sha256,
            status_command,
            correction_command,
            republication_command,
            rereview_instruction,
        )
    else:
        review_bytes = artifact_files.read_reference(durable.work_root, reference)
        review = work_briefs.decode_canonical_work_brief_review_needs_correction(review_bytes)
        if isinstance(review, work_brief_models.WorkBriefFailure):
            return review
        if (failure := work_briefs.validate_work_brief_review_needs_correction(review, brief)) is not None:
            return failure
        view = NeedsCorrectionEvidenceView(
            "pinboard-work-brief-review-status/v1",
            "needs-correction",
            brief.attempt_id,
            checkpoint.checkpoint_id,
            brief_sha256,
            review.artifact_revision,
            _publication_view(reference),
            review.reviewer_task_id,
            review.findings,
            status_command,
            correction_command,
            republication_command,
            rereview_instruction,
        )
    if command.json:
        write_json(view)
    else:
        print(f"OK BRIEF_REVIEW_STATUS status={view.status} attempt={view.attempt_id}")
    return 0
