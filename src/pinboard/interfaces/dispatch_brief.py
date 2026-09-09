import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, assert_never

import msgspec

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.application.dispatch import (
    find_dispatch_review,
    publish_dispatch_review,
    recheck_dispatch_authority,
    select_dispatch,
)
from pinboard.application.dispatch_models import (
    DispatchArtifactPort,
    DispatchEnvironment,
    DispatchRejectionCode,
)
from pinboard.application.dispatch_models import DispatchFailure as ApplicationDispatchFailure
from pinboard.application.ports import WorkStore
from pinboard.domain import decision_models
from pinboard.domain.errors import DecisionFailureCode
from pinboard.domain.identifiers import ReviewId
from pinboard.interfaces import action_selection, cli_commands, work_brief_models
from pinboard.interfaces.cli_output import write_json
from pinboard.interfaces.errors import (
    CliResult,
    CommandFailure,
    DispatchErrorCode,
    DispatchFailure,
    DispatchResult,
    WorkBriefErrorCode,
    WorkBriefFailure,
)
from pinboard.interfaces.work_briefs import (
    canonical_checkpoint_bytes,
    canonical_work_brief_review_bytes,
    decode_canonical_work_brief,
    decode_canonical_work_brief_review,
    decode_work_brief_review,
    validate_reviewed_authority_digests,
    validate_work_brief_review,
)


@dataclass(frozen=True, slots=True)
class SuppliedDispatchReview:
    content: bytes
    review_id: ReviewId


@dataclass(frozen=True, slots=True)
class ReuseAcceptedDispatchReview:
    checkpoint_sha256: str


@dataclass(frozen=True, slots=True)
class PublishSuppliedDispatchReview:
    checkpoint_sha256: str
    candidate: bytes
    review_id: ReviewId


type DispatchReviewChoice = ReuseAcceptedDispatchReview | PublishSuppliedDispatchReview


class DispatchReadyView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-dispatch-ready/v1"]
    status: Literal["ready"]
    prompt: str | None


def _present_dispatch_ready(rendered_prompt: str, *, supplied_prompt: bool, json: bool) -> None:
    if json:
        write_json(
            DispatchReadyView(
                "pinboard-dispatch-ready/v1",
                "ready",
                None if supplied_prompt else rendered_prompt,
            )
        )
    elif supplied_prompt:
        print("OK DISPATCH_READY")
    else:
        print(rendered_prompt, end="")


def read_dispatch_environment(path: Path) -> DispatchResult[DispatchEnvironment]:
    try:
        data = path.read_bytes()
    except OSError as error:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_ENVIRONMENT_UNREADABLE,
            f"Cannot read '{path}': {error}",
            None,
        )
    try:
        return msgspec.json.decode(data, type=DispatchEnvironment)
    except msgspec.DecodeError as error:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_ENVIRONMENT_INVALID,
            f"Cannot decode dispatch environment: {error}",
            None,
        )


def _review_failure(error: WorkBriefFailure) -> DispatchFailure:
    match error.code:
        case (
            WorkBriefErrorCode.BRIEF_INVALID
            | WorkBriefErrorCode.BRIEF_NOT_CANONICAL
            | WorkBriefErrorCode.REVIEW_INVALID
            | WorkBriefErrorCode.REVIEW_NOT_CANONICAL
            | WorkBriefErrorCode.PACKAGE_INVALID
            | WorkBriefErrorCode.PACKAGE_NOT_CANONICAL
            | WorkBriefErrorCode.PACKAGE_PROVENANCE_INVALID
        ):
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID
        case WorkBriefErrorCode.REVIEW_NOT_INDEPENDENT:
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT
        case WorkBriefErrorCode.REVIEW_NOT_READY:
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_NOT_READY
        case WorkBriefErrorCode.REVIEW_STALE:
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_STALE
        case _ as unreachable:
            assert_never(unreachable)
    return DispatchFailure(code, error.message, None)


def _dispatch_failure(failure: ApplicationDispatchFailure) -> DispatchFailure:
    match failure.code:
        case DecisionFailureCode() as code:
            return DispatchFailure(code, failure.message, None)
        case DispatchRejectionCode.ACTION_INVALID:
            code = DispatchErrorCode.DISPATCH_ACTION_INVALID
        case DispatchRejectionCode.ACTION_UNAVAILABLE:
            code = DispatchErrorCode.DISPATCH_ACTION_UNAVAILABLE
        case DispatchRejectionCode.ATTEMPT_NOT_ACTIVE:
            code = DispatchErrorCode.DISPATCH_ATTEMPT_NOT_ACTIVE
        case DispatchRejectionCode.BRIEF_MISSING:
            code = DispatchErrorCode.DISPATCH_BRIEF_MISSING
        case DispatchRejectionCode.REVIEW_COLLISION:
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_COLLISION
        case DispatchRejectionCode.REVIEW_MISSING:
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_MISSING
        case DispatchRejectionCode.STALE_ACTION:
            code = DispatchErrorCode.STALE_ACTION
        case _ as unreachable:
            assert_never(unreachable)
    return DispatchFailure(code, failure.message, failure.details)


def _canonical_prompt(
    attempt_path: Path,
    attempt_id: str,
    checkpoint_id: str,
    environment: DispatchEnvironment,
) -> str:
    permissions = ", ".join(sorted(permission.value for permission in environment.permissions)) or "none"
    return (
        "Use $pinboard-deliver for this repository attempt.\n\n"
        f"Attempt: {attempt_id}\n"
        f"Checkpoint: {checkpoint_id}\n"
        f"Canonical brief: {attempt_path}\n\n"
        "Read and follow that canonical attempt brief. It is the sole semantic execution contract. "
        "Do not restate, narrow, defer, or add acceptance semantics in this launch.\n\n"
        "Execution environment declaration:\n"
        f"- Checkout: {environment.checkout}\n"
        f"- Branch: {environment.branch}\n"
        f"- Starting revision: {environment.starting_revision}\n"
        f"- Declared permissions: {permissions}\n\n"
        "Pinboard validates the checkout and branch. The starting revision and permissions are task "
        "declarations for the worker; they neither grant authority nor enforce the environment.\n"
    )


def _validate_dispatch_identity(
    brief: work_brief_models.WorkBrief,
    attempt_id: str,
    attempt_branch: str,
    attempt_base_revision: str,
    checkpoint_id: str,
    environment: DispatchEnvironment,
    accepted_item_id: str | None,
    accepted_scope_revision: int | None,
    accepted_scope_digest: str | None,
    source_checkout_root: Path,
) -> DispatchFailure | None:
    if brief.attempt_id != attempt_id:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_BRIEF_INVALID, "Canonical work brief names a different attempt.", None
        )
    if accepted_item_id is not None and brief.item_id != accepted_item_id:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_BRIEF_INVALID, "Canonical work brief names a different item.", None
        )
    if accepted_scope_revision is not None and brief.accepted_scope.revision != accepted_scope_revision:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_BRIEF_INVALID,
            "Canonical work brief names a different accepted scope revision.",
            None,
        )
    if accepted_scope_digest is not None and brief.accepted_scope.digest != accepted_scope_digest:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_BRIEF_INVALID,
            "Canonical work brief names a different accepted scope digest.",
            None,
        )
    if brief.branch != attempt_branch or environment.branch != attempt_branch:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_BRANCH_MISMATCH,
            "Canonical brief, attempt, and dispatch environment branches must match.",
            None,
        )
    if brief.base_revision != attempt_base_revision or environment.starting_revision != attempt_base_revision:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_BASE_REVISION_MISMATCH,
            "Canonical brief, attempt, and dispatch environment base revisions must match.",
            None,
        )
    checkout = Path(environment.checkout)
    if not checkout.is_dir():
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_CHECKOUT_MISSING, f"Checkout '{checkout}' is not a directory.", None
        )
    if checkout.resolve() != source_checkout_root.resolve():
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_CHECKOUT_MISMATCH,
            "The dispatch environment checkout must match the selected source checkout.",
            None,
        )
    if brief.checkpoint.checkpoint_id != checkpoint_id:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_CHECKPOINT_MISSING,
            f"Checkpoint '{checkpoint_id}' is not the current canonical checkpoint.",
            None,
        )
    return None


def _read_dispatch_brief(
    accepted_brief_bytes: bytes,
    attempt_id: str,
    attempt_branch: str,
    attempt_base_revision: str,
    source_checkout_root: Path,
    checkpoint: str,
    environment: DispatchEnvironment,
    accepted_item_id: str | None,
    accepted_scope_revision: int | None,
    accepted_scope_digest: str | None,
) -> DispatchResult[work_brief_models.WorkBrief]:
    brief = decode_canonical_work_brief(accepted_brief_bytes)
    if isinstance(brief, WorkBriefFailure):
        return DispatchFailure(DispatchErrorCode.DISPATCH_BRIEF_INVALID, brief.message, None)
    if (
        failure := _validate_dispatch_identity(
            brief,
            attempt_id,
            attempt_branch,
            attempt_base_revision,
            checkpoint,
            environment,
            accepted_item_id,
            accepted_scope_revision,
            accepted_scope_digest,
            source_checkout_root,
        )
    ) is not None:
        return failure
    if isinstance(brief.checkpoint, work_brief_models.CrossBoundaryCheckpoint):
        failure = validate_reviewed_authority_digests(source_checkout_root, brief.checkpoint.reviewed_authorities)
        match failure:
            case None:
                pass
            case work_brief_models.ReviewedAuthoritySelectionFailure(authority_id=authority_id, reason=reason):
                return DispatchFailure(
                    DispatchErrorCode.DISPATCH_AUTHORITY_UNREADABLE,
                    f"Cannot read reviewed authority '{authority_id}': {reason}",
                    None,
                )
            case work_brief_models.ReviewedAuthorityDigestMismatch(authority_id=authority_id):
                return DispatchFailure(
                    DispatchErrorCode.DISPATCH_AUTHORITY_STALE,
                    f"Reviewed authority '{authority_id}' changed after review.",
                    None,
                )
            case _ as unreachable:
                assert_never(unreachable)
    return brief


def _select_dispatch_review(
    brief: work_brief_models.WorkBrief,
    supplied_review: SuppliedDispatchReview | None,
) -> DispatchResult[DispatchReviewChoice | None]:
    match brief.checkpoint:
        case work_brief_models.LocalCheckpoint():
            if supplied_review is not None:
                return DispatchFailure(
                    DispatchErrorCode.DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID,
                    "Local checkpoints do not publish cross-boundary brief reviews.",
                    None,
                )
            return None
        case work_brief_models.CrossBoundaryCheckpoint() as checkpoint:
            if supplied_review is None:
                return ReuseAcceptedDispatchReview(hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest())
            review = decode_work_brief_review(supplied_review.content)
            if isinstance(review, WorkBriefFailure):
                return _review_failure(review)
            if (failure := validate_work_brief_review(review, brief)) is not None:
                return _review_failure(failure)
            candidate = canonical_work_brief_review_bytes(review)
            return PublishSuppliedDispatchReview(review.checkpoint_sha256, candidate, supplied_review.review_id)
        case _ as unreachable:
            assert_never(unreachable)


def _validate_accepted_review(
    brief: work_brief_models.WorkBrief, accepted_review: bytes | None
) -> DispatchFailure | None:
    match brief.checkpoint:
        case work_brief_models.LocalCheckpoint():
            if accepted_review is not None:
                return DispatchFailure(
                    DispatchErrorCode.DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID,
                    "Local checkpoints do not use cross-boundary brief reviews.",
                    None,
                )
        case work_brief_models.CrossBoundaryCheckpoint():
            if accepted_review is None:
                return DispatchFailure(
                    DispatchErrorCode.DISPATCH_BRIEF_REVIEW_MISSING,
                    "The exact ready brief review is absent.",
                    None,
                )
            review = decode_canonical_work_brief_review(accepted_review)
            if isinstance(review, WorkBriefFailure):
                return _review_failure(review)
            if (failure := validate_work_brief_review(review, brief)) is not None:
                return _review_failure(failure)
        case _ as unreachable:
            assert_never(unreachable)
    return None


def _render_dispatch_prompt(
    brief: work_brief_models.WorkBrief,
    attempt_path: Path,
    checkpoint: str,
    environment: DispatchEnvironment,
    accepted_review: bytes | None,
    supplied_prompt: bytes | None,
) -> DispatchResult[str]:
    if (failure := _validate_accepted_review(brief, accepted_review)) is not None:
        return failure
    prompt = _canonical_prompt(attempt_path, brief.attempt_id, checkpoint, environment)
    if supplied_prompt is not None and supplied_prompt != prompt.encode():
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_PROMPT_NOT_CANONICAL,
            "The launch adds or changes instructions outside the canonical attempt brief; render and use the exact prompt.",
            None,
        )
    return prompt


def prepare_dispatch(
    store: WorkStore,
    artifacts: DispatchArtifactPort,
    source_checkout_root: Path,
    action: decision_models.Action,
    checkpoint: str,
    environment: DispatchEnvironment,
    supplied_prompt: bytes | None = None,
    supplied_review: SuppliedDispatchReview | None = None,
) -> DispatchResult[str]:
    selected_dispatch = select_dispatch(store, action, datetime.now(UTC))
    if isinstance(selected_dispatch, ApplicationDispatchFailure):
        return _dispatch_failure(selected_dispatch)
    assert isinstance(action, decision_models.DispatchAction)
    attempt = selected_dispatch.attempt
    accepted_brief_reference = selected_dispatch.brief_reference
    accepted_brief_bytes = artifacts.read(accepted_brief_reference)
    accepted_brief_path = artifacts.work_root / accepted_brief_reference.selector
    validated_brief = _read_dispatch_brief(
        accepted_brief_bytes,
        str(selected_dispatch.attempt.attempt_id),
        selected_dispatch.attempt.branch,
        selected_dispatch.attempt.base_revision,
        source_checkout_root,
        checkpoint,
        environment,
        str(selected_dispatch.attempt.item_id),
        selected_dispatch.attempt.accepted_scope_revision,
        selected_dispatch.attempt.accepted_scope_digest,
    )
    if isinstance(validated_brief, DispatchFailure):
        return validated_brief
    review_choice = _select_dispatch_review(validated_brief, supplied_review)
    if isinstance(review_choice, DispatchFailure):
        return review_choice
    accepted_review_bytes: bytes | None = None
    own_review_publication_revision: int | None = None
    match review_choice:
        case None:
            pass
        case ReuseAcceptedDispatchReview(checkpoint_sha256=checkpoint_sha256):
            accepted_review_reference = find_dispatch_review(store, attempt.attempt_id, checkpoint_sha256)
            if isinstance(accepted_review_reference, ApplicationDispatchFailure):
                return _dispatch_failure(accepted_review_reference)
            accepted_review_bytes = artifacts.read(accepted_review_reference)
        case PublishSuppliedDispatchReview(
            checkpoint_sha256=checkpoint_sha256,
            candidate=candidate,
            review_id=review_id,
        ):
            accepted_review = publish_dispatch_review(
                store,
                artifacts,
                attempt.attempt_id,
                checkpoint_sha256,
                candidate,
                review_id,
                datetime.now(UTC),
            )
            if isinstance(accepted_review, ApplicationDispatchFailure):
                return _dispatch_failure(accepted_review)
            accepted_review_reference = accepted_review.reference
            own_review_publication_revision = accepted_review.own_publication_revision
            accepted_review_bytes = artifacts.read(accepted_review_reference)
        case _ as unreachable:
            assert_never(unreachable)
    rendered_prompt = _render_dispatch_prompt(
        validated_brief,
        accepted_brief_path,
        checkpoint,
        environment,
        accepted_review_bytes,
        supplied_prompt,
    )
    if isinstance(rendered_prompt, DispatchFailure):
        return rendered_prompt
    if (
        failure := recheck_dispatch_authority(
            store,
            action,
            own_review_publication_revision,
            datetime.now(UTC),
        )
    ) is not None:
        return _dispatch_failure(failure)
    return rendered_prompt


def prepare_dispatch_command(
    roots: cli_commands.ResolvedRoots,
    durable: DurableRoots,
    store: WorkStore,
    command: cli_commands.DispatchCommand,
) -> CliResult[int]:
    """Prepare one installed dispatch request and return every advertised rejection."""

    decoded_environment = read_dispatch_environment(command.environment)
    if isinstance(decoded_environment, DispatchFailure):
        return decoded_environment
    supplied_prompt_bytes: bytes | None = None
    if command.prompt is not None:
        try:
            supplied_prompt_bytes = command.prompt.read_bytes()
        except OSError as error:
            return DispatchFailure(
                DispatchErrorCode.DISPATCH_PROMPT_UNREADABLE,
                f"Cannot read '{command.prompt}': {error}",
                None,
            )
    match command:
        case cli_commands.ProjectReviewedDispatchCommand(brief_review=brief_review_path, review_id=review_id):
            try:
                supplied_review = SuppliedDispatchReview(brief_review_path.read_bytes(), review_id)
            except OSError as error:
                return DispatchFailure(
                    DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID,
                    f"Cannot read '{brief_review_path}': {error}",
                    None,
                )
        case cli_commands.ProjectDispatchCommand():
            supplied_review = None
        case _ as unreachable:
            assert_never(unreachable)
    parsed_action = action_selection.parse_action_receipt(command)
    if isinstance(parsed_action, CommandFailure):
        return parsed_action
    selected_action = action_selection.select_current_action(store, parsed_action)
    if isinstance(selected_action, CommandFailure):
        return selected_action
    rendered_prompt = prepare_dispatch(
        store,
        ArtifactRepository(durable),
        roots.source_checkout,
        selected_action,
        command.checkpoint,
        decoded_environment,
        supplied_prompt_bytes,
        supplied_review,
    )
    if isinstance(rendered_prompt, DispatchFailure):
        return rendered_prompt
    _present_dispatch_ready(rendered_prompt, supplied_prompt=supplied_prompt_bytes is not None, json=command.json)
    return 0
