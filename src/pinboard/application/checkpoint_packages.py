"""Validate selected checkpoint review packages and their immutable closure."""

import hashlib
from collections.abc import Mapping

import msgspec

from pinboard.application import (
    action_models,
    candidate_snapshots,
    checkpoint_compatibility_models,
    stored_state,
    work_brief_compatibility_models,
    work_brief_models,
    work_briefs,
)
from pinboard.application.work_briefs import (
    canonical_checkpoint_bytes,
    canonical_reviewed_authority_set_bytes,
    decode_canonical_checkpoint_review_package,
    decode_canonical_work_brief,
    decode_canonical_work_brief_review,
    ready_review_key_sha256,
    validate_work_brief_review,
)
from pinboard.domain import decision_models, history, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import ArtifactRefId


def _invalid(message: str) -> work_brief_models.WorkBriefFailure:
    return work_brief_models.WorkBriefFailure(work_brief_models.WorkBriefErrorCode.PACKAGE_PROVENANCE_INVALID, message)


def _portable_reference(
    identity: work_brief_models.PortableArtifactIdentity,
    references: Mapping[tuple[str, str, int], stored_state.ArtifactReference],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> work_brief_models.WorkBriefResult[stored_state.ArtifactReference]:
    reference = references.get((identity.kind, identity.key, identity.revision))
    if reference is None:
        return _invalid(f"The {identity.role} identity does not resolve to an accepted artifact reference.")
    if (
        reference.selector != identity.selector
        or reference.content_sha256 != identity.content_sha256
        or reference.size_bytes != identity.size_bytes
        or reference.artifact_ref_id not in artifact_bytes
    ):
        return _invalid(f"The {identity.role} identity does not match its verified accepted artifact bytes.")
    return reference


def _artifact_identities(
    package: work_briefs.CheckpointPackage,
    references: Mapping[tuple[str, str, int], stored_state.ArtifactReference],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> work_brief_models.WorkBriefResult[stored_state.ArtifactReference]:
    accepted_brief = _portable_reference(package.accepted_brief, references, artifact_bytes)
    if isinstance(accepted_brief, work_brief_models.WorkBriefFailure):
        return accepted_brief
    result = _portable_reference(package.result, references, artifact_bytes)
    if isinstance(result, work_brief_models.WorkBriefFailure):
        return result
    implementation_review = _portable_reference(package.implementation_review, references, artifact_bytes)
    if isinstance(implementation_review, work_brief_models.WorkBriefFailure):
        return implementation_review
    checkpoint_id = package.checkpoint.id
    if isinstance(
        package,
        (work_brief_models.CheckpointReviewPackageV3, checkpoint_compatibility_models.CheckpointReviewPackageV2),
    ):
        candidate = _portable_reference(package.candidate_snapshot, references, artifact_bytes)
        if isinstance(candidate, work_brief_models.WorkBriefFailure):
            return candidate
        if (package.candidate_snapshot.kind, package.candidate_snapshot.key, package.candidate_snapshot.revision) != (
            work_models.ArtifactKind.EVIDENCE.value,
            f"{package.attempt_id}-{checkpoint_id}-candidate",
            1,
        ):
            return _invalid("Checkpoint package candidate snapshot identity is not canonical.")
    if (
        (package.accepted_brief.kind, package.accepted_brief.key)
        != (work_models.ArtifactKind.BRIEF.value, package.attempt_id)
        or (package.result.kind, package.result.key)
        != (work_models.ArtifactKind.RESULT.value, f"{package.attempt_id}-{checkpoint_id}-result")
        or (package.implementation_review.kind, package.implementation_review.key)
        != (work_models.ArtifactKind.EVIDENCE.value, f"{package.attempt_id}-{checkpoint_id}-review")
    ):
        return _invalid("Checkpoint package artifact roles do not use their canonical identities.")
    return accepted_brief


def _brief(
    package: work_briefs.CheckpointPackage,
    reference: stored_state.ArtifactReference,
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> work_brief_models.WorkBriefResult[work_briefs.WorkBriefValue]:
    brief = decode_canonical_work_brief(artifact_bytes[reference.artifact_ref_id])
    if isinstance(brief, work_brief_models.WorkBriefFailure):
        return _invalid(f"The package accepted brief is invalid: {brief.message}")
    if (
        brief.attempt_id != package.attempt_id
        or brief.item_id != package.item_id
        or brief.artifact_revision != package.accepted_brief.revision
        or brief.accepted_scope != package.accepted_scope
    ):
        return _invalid("The package accepted brief does not match its historical attempt binding.")
    digest = hashlib.sha256(canonical_checkpoint_bytes(brief.checkpoint)).hexdigest()
    if brief.checkpoint.checkpoint_id != package.checkpoint.id or digest != package.checkpoint.sha256:
        return _invalid("The package checkpoint identity does not match the accepted brief.")
    return brief


def _review_basis(
    package: work_briefs.CheckpointPackage,
    brief: work_brief_models.ReadableWorkBrief,
    references: Mapping[tuple[str, str, int], stored_state.ArtifactReference],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> work_brief_models.WorkBriefFailure | None:
    match brief.checkpoint, package.review_basis:
        case (
            work_brief_models.LocalCheckpoint() | work_brief_compatibility_models.LocalCheckpointV3(),
            work_brief_models.LocalReviewBasis(),
        ):
            return None
        case (
            work_brief_models.CrossBoundaryCheckpoint(reviewed_authorities=authorities)
            | work_brief_compatibility_models.CrossBoundaryCheckpointV3(reviewed_authorities=authorities),
            work_brief_models.CrossBoundaryReviewBasis(
                brief_review=review_identity,
                checkpoint_sha256=checkpoint_sha256,
                reviewed_authority_set_sha256=authority_set_sha256,
            ),
        ):
            review_reference = _portable_reference(review_identity, references, artifact_bytes)
            if isinstance(review_reference, work_brief_models.WorkBriefFailure):
                return review_reference
            if (
                (review_identity.kind, review_identity.key)
                != (
                    work_models.ArtifactKind.EVIDENCE.value,
                    f"{package.attempt_id}-brief-review-{ready_review_key_sha256(brief)}",
                )
                or checkpoint_sha256 != package.checkpoint.sha256
                or authority_set_sha256
                != hashlib.sha256(canonical_reviewed_authority_set_bytes(authorities)).hexdigest()
            ):
                return _invalid("The cross-boundary review basis has a stale identity or digest.")
            review = decode_canonical_work_brief_review(artifact_bytes[review_reference.artifact_ref_id])
            if isinstance(review, work_brief_models.WorkBriefFailure):
                return _invalid(f"The package brief review is invalid: {review.message}")
            if (failure := validate_work_brief_review(review, brief)) is not None:
                return _invalid(f"The package brief review is not ready: {failure.message}")
            return None
        case _:
            return _invalid("The package review basis does not match the accepted checkpoint boundary.")


def validate_selected_checkpoint_review_package(
    receipt: stored_state.StoredTransitionReceipt,
    package_reference: stored_state.ArtifactReference,
    package_bytes: bytes,
    *,
    attempt_id: str,
    item_id: str,
) -> work_brief_models.WorkBriefResult[work_briefs.CheckpointPackage]:
    package = decode_canonical_checkpoint_review_package(package_bytes)
    if isinstance(package, work_brief_models.WorkBriefFailure):
        return package
    try:
        outcome = msgspec.json.decode(bytes(receipt.outcome_payload), type=history.CheckpointAcceptanceOutcome)
    except msgspec.DecodeError as error:
        return _invalid(f"Checkpoint acceptance history {int(receipt.history_id)} has an invalid outcome: {error}")
    if (
        msgspec.json.encode(outcome, order="sorted") != bytes(receipt.outcome_payload)
        or receipt.outcome_schema != "checkpoint-acceptance/v2"
        or receipt.action_kind != decision_models.ActionKind.ACCEPT_CHECKPOINT
        or receipt.authorization != decision_models.AuthorizationKind.PROJECT
        or str(receipt.action_id) != f"accept-checkpoint:{package.attempt_id}"
        or str(receipt.subject_id) != package.attempt_id
        or outcome.candidate != package.candidate
        or outcome.checkpoint != package.checkpoint.id
        or outcome.evidence != package.acceptance_evidence
        or outcome.outcome != decision_models.ActionKind.ACCEPT_CHECKPOINT.value
    ):
        return _invalid(f"Checkpoint acceptance history {int(receipt.history_id)} does not match its review package.")
    if (
        receipt.artifact_ref_id != package_reference.artifact_ref_id
        or package_reference.kind != work_models.ArtifactKind.EVIDENCE
        or package_reference.key != f"{package.attempt_id}-{package.checkpoint.id}-review-package"
        or package_reference.revision != 1
        or package.attempt_id != attempt_id
        or package.item_id != item_id
    ):
        return _invalid(
            f"Checkpoint acceptance history {int(receipt.history_id)} does not resolve the selected attempt package."
        )
    return package


def validate_checkpoint_package_closure(
    package: work_briefs.CheckpointPackage,
    artifact_references: tuple[stored_state.ArtifactReference, ...],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> work_brief_models.WorkBriefFailure | None:
    references = {(value.kind.value, value.key, value.revision): value for value in artifact_references}
    accepted_brief = _artifact_identities(package, references, artifact_bytes)
    if isinstance(accepted_brief, work_brief_models.WorkBriefFailure):
        return accepted_brief
    brief = _brief(package, accepted_brief, artifact_bytes)
    if isinstance(brief, work_brief_models.WorkBriefFailure):
        return brief
    if isinstance(package, work_brief_models.CheckpointReviewPackageV3):
        candidate_reference = _portable_reference(package.candidate_snapshot, references, artifact_bytes)
        if isinstance(candidate_reference, work_brief_models.WorkBriefFailure):
            return candidate_reference
        try:
            snapshot = candidate_snapshots.decode_candidate_snapshot(
                artifact_bytes[candidate_reference.artifact_ref_id]
            )
        except (msgspec.DecodeError, ValueError) as error:
            return _invalid(f"The current portable candidate snapshot is invalid: {error}")
        if (
            snapshot.attempt_id,
            snapshot.item_id,
            snapshot.candidate,
            snapshot.branch,
            snapshot.accepted_base_revision,
        ) != (package.attempt_id, package.item_id, package.candidate, brief.branch, brief.base_revision):
            return _invalid("The portable candidate snapshot does not match its package and accepted brief.")
    return _review_basis(package, brief, references, artifact_bytes)


def _correction_failure(message: str) -> DecisionFailure:
    return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, message, None)


def decode_correction_outcome(
    receipt: stored_state.StoredTransitionReceipt,
    attempt_id: str,
) -> DecisionResult[history.TransitionReceiptOutcome]:
    if (
        receipt.action_kind != decision_models.ActionKind.RETURN_FOR_CORRECTION
        or receipt.authorization != decision_models.AuthorizationKind.PROJECT
        or str(receipt.action_id) != f"return-for-correction:{attempt_id}"
        or str(receipt.subject_id) != attempt_id
        or receipt.artifact_ref_id is not None
        or receipt.input_schema != "return-for-correction/v1"
        or receipt.outcome_schema != "transition-receipt/v1"
    ):
        return _correction_failure("Selected correction history does not match this attempt's review return.")
    try:
        correction_input = msgspec.json.decode(
            bytes(receipt.input_payload),
            type=action_models.ReasonInputPayload,
            strict=True,
        )
    except msgspec.DecodeError as error:
        return _correction_failure(f"Selected correction history has an invalid input: {error}")
    if msgspec.json.encode(correction_input, order="sorted") != bytes(receipt.input_payload):
        return _correction_failure("Selected correction history has a noncanonical input.")
    try:
        outcome = msgspec.json.decode(
            bytes(receipt.outcome_payload),
            type=history.TransitionReceiptOutcome,
            strict=True,
        )
    except msgspec.DecodeError as error:
        return _correction_failure(f"Selected correction history has an invalid outcome: {error}")
    if msgspec.json.encode(outcome, order="sorted") != bytes(receipt.outcome_payload):
        return _correction_failure("Selected correction history has a noncanonical outcome.")
    if (
        outcome.outcome != decision_models.ActionKind.RETURN_FOR_CORRECTION.value
        or outcome.evidence is None
        or outcome.candidate is None
        or outcome.checkpoint is not None
        or correction_input.reason != outcome.evidence
    ):
        return _correction_failure("Selected correction history does not preserve its candidate and reason.")
    return outcome
