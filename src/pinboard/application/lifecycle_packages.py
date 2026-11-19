"""Validate checkpoint and terminal packages from verified accepted facts without effects."""

from collections.abc import Mapping

import msgspec

from pinboard.application import (
    action_models,
    artifact_identity,
    checkpoint_packages,
    dispatch_models,
    history_archives,
    landing_decisions,
    project_export,
    stored_state,
    work_brief_compatibility_models,
    work_brief_models,
    work_briefs,
)
from pinboard.application.work_briefs import (
    decode_canonical_checkpoint_review_package,
    decode_canonical_historical_work_brief,
    decode_completion_history_package,
)
from pinboard.domain import decision_models, history, work_models
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, HistoryId


def _package_provenance_failure(message: str) -> work_brief_models.ReviewEvidenceFailure:
    return work_brief_models.ReviewEvidenceFailure(
        work_brief_models.ReviewEvidenceErrorCode.PACKAGE_PROVENANCE_INVALID, message
    )


def _validate_one_checkpoint_package(
    receipt: stored_state.StoredTransitionReceipt,
    package_reference: stored_state.ArtifactReference,
    package: work_brief_models.CheckpointReviewPackageV3,
    attempts: Mapping[str, stored_state.StoredAttempt],
    item_ids: frozenset[str],
    definition_digests: Mapping[tuple[str, int], str],
    references: Mapping[tuple[str, str, int], stored_state.ArtifactReference],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> work_brief_models.ReviewEvidenceResult[project_export.ProjectExportCheckpointPackageV3]:
    selected = checkpoint_packages.validate_selected_checkpoint_review_package(
        receipt,
        package_reference,
        artifact_bytes[package_reference.artifact_ref_id],
        attempt_id=package.attempt_id,
        item_id=package.item_id,
    )
    if isinstance(selected, work_brief_models.ReviewEvidenceFailure):
        return selected
    attempt = attempts.get(package.attempt_id)
    if attempt is None or str(attempt.item_id) != package.item_id or package.item_id not in item_ids:
        return _package_provenance_failure("Checkpoint package does not resolve to its historical attempt and item.")
    if (
        definition_digests.get((package.item_id, package.accepted_scope.revision)) != package.accepted_scope.digest
        or package_reference.kind != work_models.ArtifactKind.EVIDENCE
        or package_reference.revision != 1
    ):
        return _package_provenance_failure("Checkpoint package does not match its accepted scope or package identity.")
    if (
        failure := checkpoint_packages.validate_checkpoint_package_closure(
            package, tuple(references.values()), artifact_bytes
        )
    ) is not None:
        return failure
    packaged = {
        "history_id": int(receipt.history_id),
        "package_artifact_ref_id": int(package_reference.artifact_ref_id),
        **msgspec.to_builtins(package),
    }
    return msgspec.convert(packaged, type=project_export.ProjectExportCheckpointPackageV3, strict=True)


def validate_checkpoint_review_packages(
    lifecycle: stored_state.LifecycleRecords,
    artifact_references: tuple[stored_state.ArtifactReference, ...],
    transition_receipts: tuple[stored_state.StoredTransitionReceipt, ...],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
    archives: Mapping[AttemptId, history_archives.HistoryArchive],
) -> work_brief_models.ReviewEvidenceResult[tuple[project_export.ProjectExportCheckpointPackageV3, ...]]:
    """Validate historical package provenance from one loaded state and verified bytes."""

    references_by_id = {value.artifact_ref_id: value for value in artifact_references}
    references = {(value.kind.value, value.key, value.revision): value for value in artifact_references}
    attempts = {str(value.attempt_id): value for value in lifecycle.attempts}
    item_ids = frozenset(str(value.item_id) for value in lifecycle.work_items)
    definition_digests = {
        (str(value.item_id), value.revision): value.digest for value in lifecycle.definition_revisions
    }
    linked_package_ids: set[ArtifactRefId] = set()
    packages: list[project_export.ProjectExportCheckpointPackageV3] = []
    for receipt in transition_receipts:
        if receipt.outcome_schema != "checkpoint-acceptance/v2":
            continue
        if receipt.artifact_ref_id is None:
            return _package_provenance_failure(
                f"Checkpoint acceptance history {int(receipt.history_id)} does not link its review package."
            )
        package_reference = references_by_id.get(receipt.artifact_ref_id)
        if package_reference is None or receipt.artifact_ref_id not in artifact_bytes:
            return _package_provenance_failure(
                f"Checkpoint acceptance history {int(receipt.history_id)} links an unavailable review package."
            )
        linked_package_ids.add(receipt.artifact_ref_id)
        if history_archives.retired_checkpoint_package(artifact_bytes[receipt.artifact_ref_id]):
            archive = archives.get(AttemptId(str(receipt.subject_id)))
            if archive is None or not any(
                value.history_id == int(receipt.history_id)
                and value.package_artifact_ref_id == int(receipt.artifact_ref_id)
                for value in archive.checkpoints
            ):
                return _package_provenance_failure("Retired checkpoint requires its verified terminal archive.")
            continue
        package = decode_canonical_checkpoint_review_package(artifact_bytes[receipt.artifact_ref_id])
        if isinstance(package, work_brief_models.ReviewEvidenceFailure):
            return work_brief_models.ReviewEvidenceFailure(
                package.code,
                f"Checkpoint acceptance history {int(receipt.history_id)}: {package.message}",
            )
        validated = _validate_one_checkpoint_package(
            receipt,
            package_reference,
            package,
            attempts,
            item_ids,
            definition_digests,
            references,
            artifact_bytes,
        )
        if isinstance(validated, work_brief_models.ReviewEvidenceFailure):
            return validated
        packages.append(validated)
    orphaned = tuple(
        value.key
        for value in artifact_references
        if value.kind == work_models.ArtifactKind.EVIDENCE
        and any(
            isinstance(owner, artifact_identity.CheckpointAddress)
            and owner.role == artifact_identity.CheckpointRole.PACKAGE
            for owner in value.owners
        )
        and value.artifact_ref_id not in linked_package_ids
    )
    if orphaned:
        return _package_provenance_failure(
            f"Accepted checkpoint packages are not linked from history: {', '.join(orphaned)}"
        )
    return tuple(packages)


def _completion_outcome(
    receipt: stored_state.StoredTransitionReceipt,
) -> work_brief_models.ReviewEvidenceResult[history.CompletionAcceptanceOutcome]:
    try:
        outcome = msgspec.json.decode(bytes(receipt.outcome_payload), type=history.CompletionAcceptanceOutcome)
    except msgspec.DecodeError as error:
        return _package_provenance_failure(
            f"Completion history {int(receipt.history_id)} has an invalid outcome: {error}"
        )
    if msgspec.json.encode(outcome, order="sorted") != bytes(receipt.outcome_payload):
        return _package_provenance_failure(f"Completion history {int(receipt.history_id)} has a noncanonical outcome.")
    return outcome


def _completion_input(
    receipt: stored_state.StoredTransitionReceipt,
) -> work_brief_models.ReviewEvidenceResult[
    work_brief_compatibility_models.HistoricalCoveredCompletionInput
    | work_brief_compatibility_models.HistoricalReviewedCompletionInputV2
    | action_models.ReviewedCompletionReceiptPayload
]:
    match receipt.input_schema:
        case "pinboard-covered-completion/v1":
            model = work_brief_compatibility_models.HistoricalCoveredCompletionInput
        case "pinboard-reviewed-completion/v2":
            model = work_brief_compatibility_models.HistoricalReviewedCompletionInputV2
        case "pinboard-reviewed-completion/v3":
            model = action_models.ReviewedCompletionReceiptPayload
        case _:
            return _package_provenance_failure(
                f"Completion history {int(receipt.history_id)} has unsupported completion input."
            )
    try:
        value = msgspec.json.decode(bytes(receipt.input_payload), type=model)
    except (msgspec.DecodeError, ValueError) as error:
        return _package_provenance_failure(
            f"Completion history {int(receipt.history_id)} has invalid covered completion input: {error}"
        )
    if msgspec.json.encode(value, order="sorted") != bytes(receipt.input_payload):
        return _package_provenance_failure(
            f"Completion history {int(receipt.history_id)} has noncanonical covered completion input."
        )
    return value


def _completion_input_matches_package(
    value: work_brief_compatibility_models.HistoricalCoveredCompletionInput
    | work_brief_compatibility_models.HistoricalReviewedCompletionInputV2
    | action_models.ReviewedCompletionReceiptPayload,
    package: work_briefs.CompletionHistoryPackage,
) -> bool:
    return (
        value.candidate == package.candidate
        and value.evidence == package.outcome_evidence
        and value.reviewer_task_id == package.reviewer_task_id
        and (
            isinstance(package, work_brief_models.VerifiedCompletionReviewPackage)
            or (
                value.result_sha256 == package.terminal_result.content_sha256
                and value.review_sha256 == package.final_review.content_sha256
            )
        )
        and tuple(
            (
                row.history_id,
                row.package_sha256,
                row.disposition,
                row.evidence,
            )
            for row in value.packages
        )
        == tuple(
            (
                row.history_id,
                row.package.content_sha256,
                row.disposition,
                row.evidence,
            )
            for row in package.checkpoint_coverage
        )
    )


def _completion_reference(
    identity: work_brief_models.CompletionPortableArtifactIdentity,
    references: Mapping[tuple[str, str, int], stored_state.ArtifactReference],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> work_brief_models.ReviewEvidenceResult[stored_state.ArtifactReference]:
    reference = references.get((identity.kind, identity.key, identity.revision))
    if reference is None or (
        reference.selector != identity.selector
        or reference.content_sha256 != identity.content_sha256
        or reference.size_bytes != identity.size_bytes
        or reference.artifact_ref_id not in artifact_bytes
    ):
        return _package_provenance_failure(
            f"The {type(identity).__name__} completion identity does not match verified accepted artifact bytes."
        )
    return reference


def _completion_evidence_reference(
    proof: work_brief_models.AcceptedCompletionEvidence,
    references: Mapping[ArtifactRefId, stored_state.ArtifactReference],
    before_revision: int,
) -> stored_state.ArtifactReference | None:
    reference = references.get(ArtifactRefId(proof.artifact_ref_id))
    if (
        reference is None
        or reference.kind != work_models.ArtifactKind.EVIDENCE
        or reference.content_sha256 != proof.sha256
        or reference.accepted_revision >= before_revision
    ):
        return None
    return reference


def _validate_landing_difference(
    package: work_brief_models.VerifiedCompletionReviewPackage,
    receipt: stored_state.StoredTransitionReceipt,
    brief_owner: str,
    references: Mapping[ArtifactRefId, stored_state.ArtifactReference],
    by_key: Mapping[tuple[str, str, int], stored_state.ArtifactReference],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> work_brief_models.ReviewEvidenceFailure | None:
    landing = package.landing
    if landing is None or landing.difference_review is None:
        return None
    reference = _completion_evidence_reference(landing.difference_review, references, receipt.project_revision)
    if reference is None or (raw := artifact_bytes.get(reference.artifact_ref_id)) is None:
        return _package_provenance_failure("Completion's landing difference review has no matching accepted evidence.")
    try:
        review = msgspec.json.decode(raw, type=work_brief_models.LandingDifferenceReview)
    except (msgspec.DecodeError, ValueError) as error:
        return _package_provenance_failure(f"Completion's landing difference review is invalid: {error}")
    expected = work_brief_models.LandingDifferenceSubject(
        package.attempt_id,
        package.candidate,
        package.accepted_brief.content_sha256,
        package.snapshot.sha256,
        package.ready_review.sha256,
        landing.target,
        landing.pre_landing_revision,
        landing.landed_revision,
        landing.diff_sha256,
        landing.decisions,
    )
    if (
        review.subject != expected
        or review.reviewer_task_id in (brief_owner, str(receipt.actor_task_id))
        or raw != work_briefs.landing_review_bytes(review)
        or reference.key != work_briefs.landing_review_key(review)
    ):
        return _package_provenance_failure("Completion's landing review names another subject or lacks independence.")
    if isinstance(review.commission, work_brief_models.ExternalLandingReviewCommission):
        if review.commission.commissioning_task_id == review.reviewer_task_id:
            return _package_provenance_failure("Completion's external landing reviewer commissioned their own review.")
        return None
    prompt_subject = dispatch_models.LandingReviewerPromptSubject(
        package.attempt_id, work_briefs.landing_review_subject_sha256(expected)
    )
    prompt = review.commission.prompt
    accepted = by_key.get(("evidence", dispatch_models.agent_prompt_key(prompt_subject, prompt.sha256), 1))
    if accepted is None or accepted != _completion_evidence_reference(prompt, references, reference.accepted_revision):
        return _package_provenance_failure("Completion's landing review has no accepted commission for that range.")
    return None


def _validate_verified_completion(
    package: work_brief_models.VerifiedCompletionReviewPackage,
    completion_input: action_models.ReviewedCompletionReceiptPayload,
    receipt: stored_state.StoredTransitionReceipt,
    attempt: stored_state.StoredAttempt,
    accepted_brief: stored_state.ArtifactReference,
    references: Mapping[ArtifactRefId, stored_state.ArtifactReference],
    by_key: Mapping[tuple[str, str, int], stored_state.ArtifactReference],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
    decision_history: landing_decisions.HistoricalLandingDecisions,
) -> work_brief_models.ReviewEvidenceFailure | None:
    brief = work_briefs.decode_canonical_accepted_brief(artifact_bytes[accepted_brief.artifact_ref_id])
    if isinstance(brief, work_brief_models.WorkBriefFailure):
        return _package_provenance_failure("Verified completion has no supported current brief.")
    ready_reference = _completion_evidence_reference(package.ready_review, references, receipt.project_revision)
    snapshot_reference = _completion_evidence_reference(package.snapshot, references, receipt.project_revision)
    if ready_reference is None or snapshot_reference is None:
        return _package_provenance_failure(
            "Verified completion is missing its exact snapshot or ready review reference."
        )
    ready = work_briefs.decode_canonical_candidate_review(artifact_bytes[ready_reference.artifact_ref_id])
    if isinstance(ready, work_brief_models.WorkBriefFailure) or (
        ready.reviewer_task_id != package.reviewer_task_id
        or work_briefs.validate_candidate_review(
            ready,
            brief=brief,
            candidate=package.candidate,
            candidate_snapshot=snapshot_reference,
            accepted_brief=accepted_brief,
            result_sha256=completion_input.result_sha256,
            review_sha256=completion_input.review_sha256,
        )
        is not None
    ):
        return _package_provenance_failure(
            "Verified completion's ready review is invalid or names a different subject."
        )
    prompt_subject = dispatch_models.ReviewerPromptSubject(
        package.attempt_id,
        package.candidate,
        package.snapshot.sha256,
        package.accepted_brief.content_sha256,
        ready.result_sha256,
    )
    prompt_key = dispatch_models.agent_prompt_key(prompt_subject, ready.reviewer_prompt_sha256)
    prompt = by_key.get(("evidence", prompt_key, 1))
    if (
        prompt is None
        or prompt.content_sha256 != ready.reviewer_prompt_sha256
        or prompt.accepted_revision >= ready_reference.accepted_revision
    ):
        return _package_provenance_failure("Verified completion's original reviewer commission is missing or invalid.")
    if (
        attempt.candidate_revision != package.candidate
        or attempt.candidate_recorded_at != snapshot_reference.created_at
        or snapshot_reference.accepted_revision >= ready_reference.accepted_revision
        or work_briefs.candidate_review_key(
            package.attempt_id,
            package.candidate,
            package.snapshot.sha256,
            package.accepted_brief.content_sha256,
            ready.result_sha256,
            ready.review_sha256,
        )
        != ready_reference.key
    ):
        return _package_provenance_failure(
            "Verified completion's selected candidate does not match its terminal attempt."
        )
    if package.continued_acceptance_history_id is not None:
        accepted = decision_history.receipts_by_id.get(HistoryId(package.continued_acceptance_history_id))
        if (
            accepted is None
            or str(accepted.subject_id) != package.attempt_id
            or accepted.action_kind
            not in {decision_models.ActionKind.ACCEPT_CHECKPOINT, decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE}
            or not ready_reference.accepted_revision < accepted.project_revision < receipt.project_revision
        ):
            return _package_provenance_failure(
                "Verified completion's continued acceptance does not match its original review."
            )
    if package.accounting is not None and package.accounting.brief_sha256 != accepted_brief.content_sha256:
        return _package_provenance_failure("Verified completion's accounting does not name its accepted brief.")
    if package.landing is not None:
        try:
            decision_basis = decision_history.at_completion(attempt, accepted_brief, receipt)
        except (ValueError, KeyError) as error:
            return _package_provenance_failure(f"Completion's landing decision history is invalid: {error}")
        if package.landing.decisions != decision_basis:
            return _package_provenance_failure(
                "Completion's landing decisions do not match its accepted receipt history."
            )
    return _validate_landing_difference(package, receipt, brief.owner_task_id, references, by_key, artifact_bytes)


def validate_completion_review_packages(  # noqa: C901, PLR0912 - one exact terminal-package validator
    lifecycle: stored_state.LifecycleRecords,
    artifact_references: tuple[stored_state.ArtifactReference, ...],
    transition_receipts: tuple[stored_state.StoredTransitionReceipt, ...],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
    checkpoint_packages: tuple[project_export.ProjectExportCheckpointPackageV3, ...],
    archives: Mapping[AttemptId, history_archives.HistoryArchive],
) -> work_brief_models.ReviewEvidenceResult[tuple[project_export.ProjectExportCompletionReviewPackage, ...]]:
    references_by_id = {value.artifact_ref_id: value for value in artifact_references}
    decision_history = landing_decisions.index_historical_decisions(transition_receipts, lifecycle.amendments)
    references = {(value.kind.value, value.key, value.revision): value for value in artifact_references}
    attempts = {str(value.attempt_id): value for value in lifecycle.attempts}
    definitions = {(str(value.item_id), value.revision): value.digest for value in lifecycle.definition_revisions}
    checkpoints_by_attempt: dict[str, list[tuple[int, str, str, str, int]]] = {}
    for checkpoint in checkpoint_packages:
        checkpoints_by_attempt.setdefault(checkpoint.attempt_id, []).append(
            (
                checkpoint.history_id,
                checkpoint.checkpoint.id,
                checkpoint.checkpoint.sha256,
                checkpoint.candidate,
                checkpoint.package_artifact_ref_id,
            )
        )
    for archive in archives.values():
        for checkpoint in archive.checkpoints:
            if history_archives.retired_checkpoint_package(
                artifact_bytes[ArtifactRefId(checkpoint.package_artifact_ref_id)]
            ):
                checkpoints_by_attempt.setdefault(archive.attempt_id, []).append(
                    (
                        checkpoint.history_id,
                        checkpoint.checkpoint_id,
                        checkpoint.checkpoint_sha256,
                        checkpoint.candidate.candidate,
                        checkpoint.package_artifact_ref_id,
                    )
                )
    for checkpoints in checkpoints_by_attempt.values():
        checkpoints.sort()
    # Checkpoint provenance is already verified, including archive-covered packages.
    linked_package_ids: set[ArtifactRefId] = {
        receipt.artifact_ref_id
        for receipt in transition_receipts
        if receipt.outcome_schema == "checkpoint-acceptance/v2" and receipt.artifact_ref_id is not None
    }
    completed: list[project_export.ProjectExportCompletionReviewPackage] = []
    for receipt in transition_receipts:
        if receipt.outcome_schema != "completion-acceptance/v2":
            continue
        completion_input = _completion_input(receipt)
        if isinstance(completion_input, work_brief_models.ReviewEvidenceFailure):
            return completion_input
        outcome = _completion_outcome(receipt)
        if isinstance(outcome, work_brief_models.ReviewEvidenceFailure):
            return outcome
        if receipt.artifact_ref_id is None:
            return _package_provenance_failure(
                f"Completion history {int(receipt.history_id)} does not link its review package."
            )
        reference = references_by_id.get(receipt.artifact_ref_id)
        if reference is None or receipt.artifact_ref_id not in artifact_bytes:
            return _package_provenance_failure(
                f"Completion history {int(receipt.history_id)} links an unavailable review package."
            )
        package = decode_completion_history_package(artifact_bytes[receipt.artifact_ref_id])
        if isinstance(package, work_brief_models.ReviewEvidenceFailure):
            return package
        if (
            receipt.action_kind != decision_models.ActionKind.COMPLETE
            or receipt.authorization != decision_models.AuthorizationKind.PROJECT
            or receipt.actor_task_id is None
            or receipt.actor_host_id is None
            or str(receipt.action_id) != f"complete:{package.attempt_id}"
            or str(receipt.subject_id) != package.attempt_id
            or reference.kind != work_models.ArtifactKind.EVIDENCE
            or artifact_identity.CompletionAddress(
                AttemptId(package.attempt_id), artifact_identity.CompletionRole.PACKAGE, reference.revision
            )
            not in reference.owners
            or reference.revision != 1
            or outcome.candidate != package.candidate
            or outcome.evidence != package.outcome_evidence
            or not _completion_input_matches_package(completion_input, package)
            or str(receipt.actor_task_id) == package.reviewer_task_id
        ):
            return _package_provenance_failure(
                f"Completion history {int(receipt.history_id)} does not match its review package."
            )
        linked_package_ids.add(receipt.artifact_ref_id)
        attempt = attempts.get(package.attempt_id)
        if (
            attempt is None
            or str(attempt.item_id) != package.item_id
            or definitions.get((package.item_id, package.accepted_scope.revision)) != package.accepted_scope.digest
        ):
            return _package_provenance_failure("Completion package does not match its terminal attempt or scope.")
        accepted_brief = _completion_reference(package.accepted_brief, references, artifact_bytes)
        if isinstance(accepted_brief, work_brief_models.ReviewEvidenceFailure):
            return accepted_brief
        if (
            not any(owner.attempt_id == package.attempt_id for owner in accepted_brief.owners)
            or attempt.state != work_models.AttemptState.DONE
            or attempt.brief_artifact_ref_id != accepted_brief.artifact_ref_id
        ):
            return _package_provenance_failure("Completion package artifact identities are not canonical.")
        if isinstance(package, work_brief_models.VerifiedCompletionReviewPackage):
            if not isinstance(completion_input, action_models.ReviewedCompletionReceiptPayload):
                return _package_provenance_failure("Verified completion needs its current decision receipt.")
            failure = _validate_verified_completion(
                package,
                completion_input,
                receipt,
                attempt,
                accepted_brief,
                references_by_id,
                references,
                artifact_bytes,
                decision_history,
            )
            if failure is not None:
                return failure
        else:
            terminal_result = _completion_reference(package.terminal_result, references, artifact_bytes)
            if isinstance(terminal_result, work_brief_models.ReviewEvidenceFailure):
                return terminal_result
            final_review = _completion_reference(package.final_review, references, artifact_bytes)
            if isinstance(final_review, work_brief_models.ReviewEvidenceFailure):
                return final_review
            if (
                artifact_identity.CompletionAddress(
                    AttemptId(package.attempt_id), artifact_identity.CompletionRole.RESULT, terminal_result.revision
                )
                not in terminal_result.owners
                or artifact_identity.CompletionAddress(
                    AttemptId(package.attempt_id), artifact_identity.CompletionRole.REVIEW, final_review.revision
                )
                not in final_review.owners
                or attempt.result_artifact_ref_id != terminal_result.artifact_ref_id
            ):
                return _package_provenance_failure("Completion package report identities are not canonical.")
        brief = decode_canonical_historical_work_brief(artifact_bytes[accepted_brief.artifact_ref_id])
        if (
            isinstance(brief, work_brief_models.WorkBriefFailure)
            or brief.attempt_id != package.attempt_id
            or brief.item_id != package.item_id
            or brief.accepted_scope != package.accepted_scope
            or brief.owner_task_id == package.reviewer_task_id
        ):
            return _package_provenance_failure("Completion package accepted brief is invalid or stale.")
        expected_checkpoints = checkpoints_by_attempt.get(package.attempt_id, [])
        if tuple(row.history_id for row in package.checkpoint_coverage) != tuple(
            value[0] for value in expected_checkpoints
        ):
            return _package_provenance_failure("Completion checkpoint coverage is incomplete or stale.")
        for row, checkpoint in zip(package.checkpoint_coverage, expected_checkpoints, strict=True):
            package_reference = _completion_reference(row.package, references, artifact_bytes)
            if isinstance(package_reference, work_brief_models.ReviewEvidenceFailure):
                return package_reference
            if (
                row.checkpoint.id != checkpoint[1]
                or row.checkpoint.sha256 != checkpoint[2]
                or row.candidate != checkpoint[3]
                or int(package_reference.artifact_ref_id) != checkpoint[4]
            ):
                return _package_provenance_failure("Completion checkpoint coverage does not match its package.")
        completed.append(
            msgspec.convert(
                {
                    "history_id": int(receipt.history_id),
                    "package_artifact_ref_id": int(receipt.artifact_ref_id),
                    **msgspec.to_builtins(package),
                },
                type=project_export.ProjectExportCompletionReviewPackage,
                strict=True,
            )
        )
    orphaned = tuple(
        value.key
        for value in artifact_references
        if value.kind == work_models.ArtifactKind.EVIDENCE
        and any(
            isinstance(owner, artifact_identity.CompletionAddress)
            and owner.role == artifact_identity.CompletionRole.PACKAGE
            for owner in value.owners
        )
        and value.artifact_ref_id not in linked_package_ids
    )
    if orphaned:
        return _package_provenance_failure(
            f"Accepted completion packages are not linked from history: {', '.join(orphaned)}"
        )
    return tuple(completed)
