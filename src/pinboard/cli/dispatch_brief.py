import hashlib
import shlex
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Literal, assert_never

import msgspec

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.brief_sources import select_checkout_brief_source
from pinboard.adapters.files.errors import ArtifactError
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.application import work_brief_models
from pinboard.application.brief_source_models import BriefSourceFailure, authority_selector
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
    NativeLaunchEnvelope,
    PromptReferenceView,
    PublishedAgentPrompt,
    dispatch_environment_dec_hook,
    pinboard_launcher_command,
    publish_agent_prompt,
)
from pinboard.application.dispatch_models import DispatchFailure as ApplicationDispatchFailure
from pinboard.application.ports import WorkStore, WorkStoreError
from pinboard.application.work_briefs import (
    canonical_checkpoint_bytes,
    canonical_reviewed_authority_set_bytes,
    canonical_work_brief_review_bytes,
    decode_canonical_work_brief,
    decode_canonical_work_brief_review,
    decode_work_brief_review,
    validate_reviewed_authority_digests,
    validate_work_brief_review,
)
from pinboard.cli import action_selection, cli_commands, work_inspection
from pinboard.cli.cli_output import write_json
from pinboard.cli.errors import (
    CliResult,
    CommandFailure,
    DispatchErrorCode,
    DispatchFailure,
    DispatchFailureCode,
    DispatchResult,
)
from pinboard.domain import decision_models
from pinboard.domain.errors import (
    ArtifactAcceptanceAfterPublicationError,
    ChangedSurface,
    DecisionFailure,
    DecisionFailureCode,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    FailureMismatch,
    RetryDisposition,
)
from pinboard.domain.identifiers import AttemptId, HistoryId, ReviewId


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


FRESH_REVIEW_PREPARATION_COMMAND: str = shlex.join(
    (*pinboard_launcher_command(), "tool-contract", "--operation", "brief-sources:plan", "--json")
)


class DispatchReadyView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-dispatch-ready/v2"]
    status: Literal["ready"]
    prompt_reference: PromptReferenceView
    native_launch: NativeLaunchEnvelope
    changed_surfaces: tuple[str, ...]


class _DispatchEnvironmentIdentity(msgspec.Struct, frozen=True, forbid_unknown_fields=False):
    schema: str


def _present_dispatch_ready(launch: PublishedAgentPrompt, *, json: bool) -> None:
    if json:
        write_json(
            DispatchReadyView(
                "pinboard-dispatch-ready/v2",
                "ready",
                launch.reference,
                launch.native_launch,
                tuple(surface.value for surface in launch.changed_surfaces),
            )
        )
    else:
        print(launch.native_launch.message)


def _merge_changed_surfaces(*groups: tuple[ChangedSurface, ...]) -> tuple[ChangedSurface, ...]:
    return tuple(dict.fromkeys(surface for group in groups for surface in group))


def _after_publication_failure(
    code: DispatchFailureCode,
    message: str,
    changed_surfaces: tuple[ChangedSurface, ...],
    details: FailureDetails | None = None,
) -> DispatchFailure:
    if not changed_surfaces:
        return DispatchFailure(code, message, details)
    prior = details
    return DispatchFailure(
        code,
        message,
        FailureDetails(
            observed=() if prior is None else prior.observed,
            mismatches=() if prior is None else prior.mismatches,
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.COMMITTED,
            changed_surfaces=changed_surfaces,
            alternatives=() if prior is None else prior.alternatives,
        ),
    )


def _fresh_review_details(
    observed: tuple[FailureFact, ...],
    mismatches: tuple[FailureMismatch, ...],
) -> FailureDetails:
    return FailureDetails(
        observed=(*observed, FailureFact("fresh_review_preparation_command", FRESH_REVIEW_PREPARATION_COMMAND)),
        mismatches=mismatches,
        retry=RetryDisposition.CORRECT_INPUT,
        effect=EffectDisposition.UNCHANGED,
        changed_surfaces=(),
        alternatives=(),
    )


def _stale_review_failure(
    review: work_brief_models.WorkBriefReview,
    brief: work_brief_models.WorkBrief,
) -> DispatchFailure:
    checkpoint = brief.checkpoint
    assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
    current_checkpoint_sha256 = hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest()
    current_authority_set_sha256 = hashlib.sha256(
        canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities)
    ).hexdigest()
    return DispatchFailure(
        DispatchErrorCode.DISPATCH_BRIEF_REVIEW_STALE,
        "Brief review is not bound to the current checkpoint and reviewed authorities.",
        _fresh_review_details(
            (
                FailureFact("provided_checkpoint_sha256", review.checkpoint_sha256),
                FailureFact("current_checkpoint_sha256", current_checkpoint_sha256),
                FailureFact(
                    "provided_reviewed_authority_set_sha256",
                    review.reviewed_authority_set_sha256,
                ),
                FailureFact("current_reviewed_authority_set_sha256", current_authority_set_sha256),
            ),
            (
                *(
                    (FailureMismatch("checkpoint_sha256", current_checkpoint_sha256, review.checkpoint_sha256),)
                    if review.checkpoint_sha256 != current_checkpoint_sha256
                    else ()
                ),
                *(
                    (
                        FailureMismatch(
                            "reviewed_authority_set_sha256",
                            current_authority_set_sha256,
                            review.reviewed_authority_set_sha256,
                        ),
                    )
                    if review.reviewed_authority_set_sha256 != current_authority_set_sha256
                    else ()
                ),
            ),
        ),
    )


def _stale_authority_failure(
    authority_id: str,
    provided_sha256: str,
    current_sha256: str,
    message: str,
) -> DispatchFailure:
    return DispatchFailure(
        DispatchErrorCode.DISPATCH_AUTHORITY_STALE,
        message,
        _fresh_review_details(
            (
                FailureFact("authority_id", authority_id),
                FailureFact("provided_selected_source_sha256", provided_sha256),
                FailureFact("current_selected_source_sha256", current_sha256),
            ),
            (FailureMismatch("selected_source_sha256", current_sha256, provided_sha256),),
        ),
    )


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
        identity = msgspec.json.decode(data, type=_DispatchEnvironmentIdentity)
    except msgspec.DecodeError:
        identity = None
    try:
        return msgspec.json.decode(data, type=DispatchEnvironment, dec_hook=dispatch_environment_dec_hook)
    except msgspec.DecodeError as error:
        details = None
        if identity is not None and identity.schema != "pinboard-dispatch/v2":
            details = FailureDetails(
                observed=(FailureFact("environment_schema", identity.schema),),
                mismatches=(FailureMismatch("environment_schema", "pinboard-dispatch/v2", identity.schema),),
                retry=RetryDisposition.CORRECT_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            )
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_ENVIRONMENT_INVALID,
            f"Cannot decode dispatch environment: {error}",
            details,
        )


def _review_failure(error: work_brief_models.WorkBriefFailure) -> DispatchFailure:
    match error.code:
        case (
            work_brief_models.WorkBriefErrorCode.BRIEF_INVALID
            | work_brief_models.WorkBriefErrorCode.BRIEF_NOT_CANONICAL
            | work_brief_models.WorkBriefErrorCode.REVIEW_INVALID
            | work_brief_models.WorkBriefErrorCode.REVIEW_NOT_CANONICAL
            | work_brief_models.WorkBriefErrorCode.PACKAGE_INVALID
            | work_brief_models.WorkBriefErrorCode.PACKAGE_NOT_CANONICAL
            | work_brief_models.WorkBriefErrorCode.PACKAGE_PROVENANCE_INVALID
        ):
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID
        case work_brief_models.WorkBriefErrorCode.REVIEW_NOT_INDEPENDENT:
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT
        case work_brief_models.WorkBriefErrorCode.REVIEW_NOT_READY:
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_NOT_READY
        case work_brief_models.WorkBriefErrorCode.REVIEW_STALE:
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
    work_root: Path,
    attempt_path: Path,
    attempt_id: str,
    checkpoint_id: str,
    environment: DispatchEnvironment,
) -> str:
    permissions = ", ".join(sorted(permission.value for permission in environment.permissions)) or "none"
    result_path = work_root / "attempts" / attempt_id / "result.md"
    blocker_path = work_root / "attempts" / attempt_id / "blocker.md"
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
        "- Fresh context: required\n"
        f"- Runtime host: {environment.host_id}\n"
        f"- Declared permissions: {permissions}\n\n"
        "Pinboard validates the checkout and branch. The starting revision and permissions are task "
        "declarations for the worker; they neither grant authority nor enforce the environment.\n\n"
        "Worker startup after native launch:\n"
        "1. Worker task identity: read `CODEX_THREAD_ID` after launch. Do not use `CODEX_SESSION_ID` or a "
        "pre-launch identity.\n"
        "2. Call `pinboard_attempt_authority` with the exact startup values:\n"
        f"   project_root={environment.checkout}; work_root={work_root}; operation=acquire; "
        f"attempt_id={attempt_id}; task_id=<post-launch CODEX_THREAD_ID>; "
        f"host_id={environment.host_id}; ttl_seconds={environment.lease_ttl_seconds}.\n"
        "3. Call `pinboard_actions` with the same explicit roots, role=worker, the returned lease_id "
        "and generation, and action_id="
        f'{{"kind":"continue","subject":"{attempt_id}"}}. '
        "Follow that leased continuation; do not manufacture a shell command or temporary payload file.\n\n"
        "Attempt evidence locations:\n"
        f"- Result: {result_path}\n"
        f"- Blocker: {blocker_path}\n"
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
    work_root: Path,
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
    base_mismatches = tuple(
        mismatch
        for mismatch in (
            FailureMismatch("brief_base_revision", attempt_base_revision, brief.base_revision),
            FailureMismatch("environment_base_revision", attempt_base_revision, environment.starting_revision),
        )
        if mismatch.expected != mismatch.observed
    )
    if base_mismatches:
        prefix = (
            *pinboard_launcher_command(),
            "--project-root",
            str(source_checkout_root),
            "--work-root",
            str(work_root),
        )
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_BASE_REVISION_MISMATCH,
            "Canonical brief, attempt, and dispatch environment base revisions must match.",
            FailureDetails(
                observed=(
                    FailureFact("attempt_base_revision", attempt_base_revision),
                    FailureFact("brief_base_revision", brief.base_revision),
                    FailureFact("environment_base_revision", environment.starting_revision),
                    FailureFact(
                        "tool_contract_command",
                        shlex.join((*prefix, "tool-contract", "--operation", "dispatch", "--json")),
                    ),
                    FailureFact(
                        "current_dispatch_action_command",
                        shlex.join(
                            (
                                *prefix,
                                "actions",
                                "--role",
                                "project",
                                "--action-id",
                                f"dispatch:{attempt_id}",
                                "--json",
                            )
                        ),
                    ),
                ),
                mismatches=base_mismatches,
                retry=RetryDisposition.CORRECT_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
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
    work_root: Path,
    checkpoint: str,
    environment: DispatchEnvironment,
    accepted_item_id: str | None,
    accepted_scope_revision: int | None,
    accepted_scope_digest: str | None,
    *,
    validate_original_authorities: bool,
) -> DispatchResult[work_brief_models.WorkBrief]:
    brief = decode_canonical_work_brief(accepted_brief_bytes)
    if isinstance(brief, work_brief_models.WorkBriefFailure):
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
            work_root,
        )
    ) is not None:
        return failure
    if validate_original_authorities and isinstance(brief.checkpoint, work_brief_models.CrossBoundaryCheckpoint):
        failure = validate_reviewed_authority_digests(
            partial(select_checkout_brief_source, source_checkout_root),
            brief.checkpoint.reviewed_authorities,
        )
        match failure:
            case None:
                pass
            case work_brief_models.ReviewedAuthoritySelectionFailure(authority_id=authority_id, reason=reason):
                return DispatchFailure(
                    DispatchErrorCode.DISPATCH_AUTHORITY_UNREADABLE,
                    f"Cannot read reviewed authority '{authority_id}': {reason}",
                    None,
                )
            case work_brief_models.ReviewedAuthorityDigestMismatch(
                authority_id=authority_id,
                expected_sha256=provided_sha256,
                observed_sha256=current_sha256,
            ):
                return _stale_authority_failure(
                    authority_id,
                    provided_sha256,
                    current_sha256,
                    f"Reviewed authority '{authority_id}' changed after review.",
                )
            case _ as unreachable:
                assert_never(unreachable)
    return brief


def _validate_correction_history(
    store: WorkStore,
    attempt_id: str,
    subject_revision: str,
    correction_history_id: int,
) -> DispatchFailure | None:
    facts = store.read_review_job_context(AttemptId(attempt_id), None, HistoryId(correction_history_id))
    receipt = None if facts is None else facts.correction_receipt
    if receipt is None:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID,
            "Selected correction history does not match this attempt's review return.",
            None,
        )
    if str(receipt.project_revision) != subject_revision:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID,
            "Selected correction history is not the attempt's current correction round.",
            FailureDetails(
                observed=(
                    FailureFact("selected_correction_history_id", correction_history_id),
                    FailureFact("selected_correction_project_revision", receipt.project_revision),
                    FailureFact("current_attempt_subject_revision", subject_revision),
                ),
                mismatches=(
                    FailureMismatch(
                        "correction_project_revision",
                        subject_revision,
                        receipt.project_revision,
                    ),
                ),
                retry=RetryDisposition.CORRECT_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    outcome = work_inspection.decode_correction_outcome(receipt, attempt_id)
    if isinstance(outcome, CommandFailure):
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID,
            outcome.message,
            outcome.details,
        )
    return None


def _effective_correction_brief(
    source_checkout_root: Path,
    brief: work_brief_models.WorkBrief,
) -> DispatchResult[work_brief_models.WorkBrief]:
    checkpoint = brief.checkpoint
    if not isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint):
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID,
            "Correction dispatch source review is only valid for a cross-boundary checkpoint.",
            None,
        )
    refreshed: list[work_brief_models.ReviewedAuthority] = []
    for authority in checkpoint.reviewed_authorities:
        selected = select_checkout_brief_source(source_checkout_root, authority_selector(authority.selector), True)
        if isinstance(selected, BriefSourceFailure):
            return DispatchFailure(
                DispatchErrorCode.DISPATCH_AUTHORITY_UNREADABLE,
                f"Cannot read reviewed authority '{authority.authority_id}': {selected.message}",
                None,
            )
        refreshed.append(
            msgspec.structs.replace(authority, reviewed_sha256=hashlib.sha256(selected.content).hexdigest())
        )
    effective_checkpoint = msgspec.structs.replace(checkpoint, reviewed_authorities=tuple(refreshed))
    return msgspec.structs.replace(brief, checkpoint=effective_checkpoint)


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
            if isinstance(review, work_brief_models.WorkBriefFailure):
                return _review_failure(review)
            if (failure := validate_work_brief_review(review, brief)) is not None:
                if failure.code == work_brief_models.WorkBriefErrorCode.REVIEW_STALE:
                    return _stale_review_failure(review, brief)
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
            if isinstance(review, work_brief_models.WorkBriefFailure):
                return _review_failure(review)
            if (failure := validate_work_brief_review(review, brief)) is not None:
                if failure.code == work_brief_models.WorkBriefErrorCode.REVIEW_STALE:
                    return _stale_review_failure(review, brief)
                return _review_failure(failure)
        case _ as unreachable:
            assert_never(unreachable)
    return None


def _render_dispatch_prompt(
    brief: work_brief_models.WorkBrief,
    work_root: Path,
    attempt_path: Path,
    checkpoint: str,
    environment: DispatchEnvironment,
    accepted_review: bytes | None,
    supplied_prompt: bytes | None,
) -> DispatchResult[str]:
    if (failure := _validate_accepted_review(brief, accepted_review)) is not None:
        return failure
    prompt = _canonical_prompt(work_root, attempt_path, brief.attempt_id, checkpoint, environment)
    if supplied_prompt is not None and supplied_prompt != prompt.encode():
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_PROMPT_NOT_CANONICAL,
            "The launch adds or changes instructions outside the canonical attempt brief; render and use the exact prompt.",
            None,
        )
    return prompt


def prepare_dispatch(  # noqa: C901, PLR0912, PLR0915 - one ordered selection, review, source, and authority recheck
    store: WorkStore,
    artifacts: DispatchArtifactPort,
    source_checkout_root: Path,
    action: decision_models.Action,
    checkpoint: str,
    environment: DispatchEnvironment,
    supplied_prompt: bytes | None,
    supplied_review: SuppliedDispatchReview | None,
    correction_history_id: int | None,
) -> DispatchResult[PublishedAgentPrompt]:
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
        artifacts.work_root,
        checkpoint,
        environment,
        str(selected_dispatch.attempt.item_id),
        selected_dispatch.attempt.accepted_scope_revision,
        selected_dispatch.attempt.accepted_scope_digest,
        validate_original_authorities=correction_history_id is None,
    )
    if isinstance(validated_brief, DispatchFailure):
        return validated_brief
    if correction_history_id is not None:
        if (
            failure := _validate_correction_history(
                store,
                str(selected_dispatch.attempt.attempt_id),
                selected_dispatch.attempt.subject_revision,
                correction_history_id,
            )
        ) is not None:
            return failure
        effective_brief = _effective_correction_brief(source_checkout_root, validated_brief)
        if isinstance(effective_brief, DispatchFailure):
            return effective_brief
        validated_brief = effective_brief
    review_choice = _select_dispatch_review(validated_brief, supplied_review)
    if isinstance(review_choice, DispatchFailure):
        return review_choice
    accepted_review_bytes: bytes | None = None
    review_publication_selector: str | None = None
    review_publication_surfaces = ()
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
            review_publication_selector = accepted_review_reference.selector
            review_publication_surfaces = accepted_review.changed_surfaces
            try:
                accepted_review_bytes = artifacts.read(accepted_review_reference)
            except ArtifactError as error:
                if not review_publication_surfaces:
                    raise
                raise ArtifactAcceptanceAfterPublicationError(
                    accepted_review_reference.selector,
                    error,
                    review_publication_surfaces,
                ) from error
        case _ as unreachable:
            assert_never(unreachable)
    if correction_history_id is not None:
        checkpoint_value = validated_brief.checkpoint
        assert isinstance(checkpoint_value, work_brief_models.CrossBoundaryCheckpoint)
        failure = validate_reviewed_authority_digests(
            partial(select_checkout_brief_source, source_checkout_root),
            checkpoint_value.reviewed_authorities,
        )
        match failure:
            case None:
                pass
            case work_brief_models.ReviewedAuthoritySelectionFailure(authority_id=authority_id, reason=reason):
                return _after_publication_failure(
                    DispatchErrorCode.DISPATCH_AUTHORITY_UNREADABLE,
                    f"Cannot read reviewed authority '{authority_id}': {reason}",
                    review_publication_surfaces,
                )
            case work_brief_models.ReviewedAuthorityDigestMismatch(
                authority_id=authority_id,
                expected_sha256=provided_sha256,
                observed_sha256=current_sha256,
            ):
                stale = _stale_authority_failure(
                    authority_id,
                    provided_sha256,
                    current_sha256,
                    f"Reviewed authority '{authority_id}' changed during correction dispatch.",
                )
                return _after_publication_failure(
                    stale.code,
                    stale.message,
                    review_publication_surfaces,
                    stale.details,
                )
            case _ as unreachable:
                assert_never(unreachable)
    rendered_prompt = _render_dispatch_prompt(
        validated_brief,
        artifacts.work_root,
        accepted_brief_path,
        checkpoint,
        environment,
        accepted_review_bytes,
        supplied_prompt,
    )
    if isinstance(rendered_prompt, DispatchFailure):
        return _after_publication_failure(
            rendered_prompt.code,
            rendered_prompt.message,
            review_publication_surfaces,
        )
    try:
        published_prompt = publish_agent_prompt(
            store,
            artifacts,
            project_root=source_checkout_root,
            prompt_role="worker",
            attempt_id=str(attempt.attempt_id),
            prompt=rendered_prompt,
            accepted_at=datetime.now(UTC),
        )
    except ArtifactAcceptanceAfterPublicationError as error:
        raise ArtifactAcceptanceAfterPublicationError(
            error.selector,
            error.cause,
            _merge_changed_surfaces(review_publication_surfaces, error.changed_surfaces),
        ) from error
    except (ArtifactError, WorkStoreError) as error:
        if not review_publication_surfaces:
            raise
        assert review_publication_selector is not None
        raise ArtifactAcceptanceAfterPublicationError(
            review_publication_selector,
            error,
            review_publication_surfaces,
        ) from error
    if isinstance(published_prompt, DecisionFailure):
        details = published_prompt.details
        if review_publication_surfaces:
            details = FailureDetails(
                observed=() if details is None else details.observed,
                mismatches=() if details is None else details.mismatches,
                retry=RetryDisposition.DO_NOT_RETRY,
                effect=EffectDisposition.COMMITTED,
                changed_surfaces=_merge_changed_surfaces(
                    review_publication_surfaces,
                    () if details is None else details.changed_surfaces,
                ),
                alternatives=() if details is None else details.alternatives,
            )
        return DispatchFailure(
            DispatchErrorCode.STALE_ACTION,
            published_prompt.message,
            details,
        )
    invocation_surfaces = _merge_changed_surfaces(review_publication_surfaces, published_prompt.changed_surfaces)
    try:
        failure = recheck_dispatch_authority(
            store,
            action,
            invocation_surfaces,
            datetime.now(UTC),
        )
    except WorkStoreError as error:
        if not invocation_surfaces:
            raise
        published_selector = (
            published_prompt.reference.selector if published_prompt.changed_surfaces else review_publication_selector
        )
        assert published_selector is not None
        raise ArtifactAcceptanceAfterPublicationError(
            published_selector,
            error,
            invocation_surfaces,
        ) from error
    if failure is not None:
        return _dispatch_failure(failure)
    return published_prompt


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
        case (
            cli_commands.ProjectReviewedDispatchCommand(brief_review=brief_review_path, review_id=review_id)
            | cli_commands.ProjectCorrectionDispatchCommand(brief_review=brief_review_path, review_id=review_id)
        ):
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
    published_prompt = prepare_dispatch(
        store,
        ArtifactRepository(durable),
        roots.source_checkout,
        selected_action,
        command.checkpoint,
        decoded_environment,
        supplied_prompt_bytes,
        supplied_review,
        command.correction_history_id if isinstance(command, cli_commands.ProjectCorrectionDispatchCommand) else None,
    )
    if isinstance(published_prompt, DispatchFailure):
        if (
            isinstance(command, cli_commands.ProjectCorrectionDispatchCommand)
            and published_prompt.details is not None
            and any(value.field == "correction_project_revision" for value in published_prompt.details.mismatches)
        ):
            suffix = (
                "dispatch",
                "--action-id",
                command.action_id,
                "--subject-revision",
                command.subject_revision,
                "--task-id",
                command.task_id,
                "--host-id",
                command.host_id,
                "--checkpoint",
                command.checkpoint,
                "--environment",
                str(command.environment),
                "--brief-review",
                str(command.brief_review),
                "--review-id",
                command.review_id,
                *(("--prompt", str(command.prompt)) if command.prompt is not None else ()),
                *(("--json",) if command.json else ()),
            )
            recovery_command = shlex.join(
                (
                    *pinboard_launcher_command(),
                    "--project-root",
                    str(roots.source_checkout),
                    "--work-root",
                    str(roots.work),
                    *suffix,
                )
            )
            published_prompt = replace(
                published_prompt,
                details=replace(
                    published_prompt.details,
                    observed=(
                        *published_prompt.details.observed,
                        FailureFact("reviewed_dispatch_command", recovery_command),
                    ),
                ),
            )
        return published_prompt
    _present_dispatch_ready(published_prompt, json=command.json)
    return 0
