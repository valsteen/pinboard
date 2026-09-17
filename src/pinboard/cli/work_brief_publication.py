"""Publish one canonical work brief and present its accepted reference.

The command function reads the selected candidate, strictly decodes and
cross-validates it, canonicalizes its bytes, publishes the immutable artifact,
accepts its reference in SQLite, and presents that
stable reference. It returns advertised decision failures and lets filesystem,
storage, and malformed boundary data remain exact exceptions.
"""

import shlex
import sys
from datetime import UTC, datetime
from typing import Literal, assert_never

import msgspec

from pinboard.adapters.files import artifacts as artifact_files
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.files.models import AffectedViews
from pinboard.application import (
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
    publication_view = _publication_view(accepted_reference.reference)
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


def publish_brief_review_needs_correction(
    durable: DurableRoots,
    store: ports.WorkStore,
    command: cli_commands.BriefReviewNeedsCorrectionCommand,
) -> CommandResult[int] | work_brief_models.WorkBriefFailure:
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
    repository = artifact_files.ArtifactRepository(durable)
    accepted = work_briefs.publish_brief_review_needs_correction(
        store,
        repository,
        repository,
        ArtifactRefId(command.brief_artifact_ref_id),
        review,
        datetime.now(UTC),
    )
    if isinstance(accepted, work_brief_models.WorkBriefFailure):
        return accepted
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
            "Independently reassess the corrected accepted brief under the bounded-correction review policy; "
            "retain the same independent reviewer unless widening requires a fresh reviewer. "
            "Publish another needs-correction record only if blocking findings remain; ready evidence remains dispatch-only."
        ),
    )


def show_brief_review_status(
    roots: cli_commands.ResolvedRoots,
    durable: DurableRoots,
    store: ports.WorkStore,
    command: cli_commands.BriefReviewStatusCommand,
) -> CommandResult[int] | work_brief_models.WorkBriefFailure:
    selected = work_briefs.read_brief_review_status(
        store, artifact_files.ArtifactRepository(durable), ArtifactRefId(command.brief_artifact_ref_id)
    )
    if isinstance(selected, work_brief_models.WorkBriefFailure):
        return selected
    brief = selected.accepted_brief.brief
    checkpoint = brief.checkpoint
    assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
    brief_sha256 = selected.accepted_brief.reference.content_sha256
    status_command, correction_command, republication_command, rereview_instruction = _review_recovery(
        roots, command.brief_artifact_ref_id
    )
    match selected:
        case work_brief_models.NoNeedsCorrectionEvidence():
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
        case work_brief_models.NeedsCorrectionEvidence(reference=reference, review=review):
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
        case _ as unreachable:
            assert_never(unreachable)
    if command.json:
        write_json(view)
    else:
        print(f"OK BRIEF_REVIEW_STATUS status={view.status} attempt={view.attempt_id}")
    return 0
