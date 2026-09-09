"""Compose initialization and read-only integrity validation.

Initialization may publish SQLite state and rebuild generated views from exact
projection facts. Validation reads one complete SQLite snapshot, verifies accepted
artifacts, and only classifies replaceable view drift; it never repairs state.
"""

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import msgspec

from pinboard.adapters.files.artifacts import ArtifactRepository, read_reference
from pinboard.adapters.files.errors import ArtifactError, FileIOError, FileIOErrorCode
from pinboard.adapters.files.file_io import DurableRoots, ensure_directory_chain
from pinboard.adapters.files.root import ensure_default_git_exclude
from pinboard.adapters.files.views import derive_expected_view_bytes, rebuild_facts
from pinboard.adapters.sqlite.database import initialize_database, open_database, reconcile_database_publication
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.models import InitReceipt, OpenMode
from pinboard.application import handover, ports, stored_state
from pinboard.domain import decision_models, history, work_models
from pinboard.domain.identifiers import ArtifactRefId, AttemptId
from pinboard.interfaces import work_brief_models
from pinboard.interfaces.errors import (
    InitializationAfterCommittedEffectsError,
    WorkBriefErrorCode,
    WorkBriefFailure,
    WorkBriefResult,
)
from pinboard.interfaces.work_briefs import (
    build_selected_attempt_brief_views,
    canonical_checkpoint_bytes,
    canonical_reviewed_authority_set_bytes,
    decode_canonical_checkpoint_review_package,
    decode_canonical_completion_review_package,
    decode_canonical_work_brief,
    decode_canonical_work_brief_review,
    validate_work_brief_review,
)
from pinboard.interfaces.work_state_models import Diagnostic, Severity, ValidationReport


def initialize_work_state(
    shared_repository_root: Path,
    roots: DurableRoots,
    *,
    default_work_root: bool,
    store: ports.GeneratedViewSetReader,
    now: datetime | None = None,
) -> WorkBriefResult[InitReceipt]:
    git_exclude_path = ensure_default_git_exclude(shared_repository_root) if default_work_root else None
    database_path: Path | None = None
    try:
        database_already_exists = roots.database_path.exists()
        operation_time = now or datetime.now(UTC)
        if database_already_exists:
            connection = open_database(roots.database_path, OpenMode.READ_WRITE)
            connection.close()
            reconcile_database_publication(roots.database_path)
            ensure_directory_chain(roots)
        else:
            initialize_database(roots, operation_time)
            database_path = roots.database_path
        projection_facts = store.read_all_generated_view_facts(operation_time)
        rendered_attempt_briefs = build_selected_attempt_brief_views(
            projection_facts.attempts, ArtifactRepository(roots)
        )
        if isinstance(rendered_attempt_briefs, WorkBriefFailure):
            if git_exclude_path is None and database_path is None:
                return rendered_attempt_briefs
            raise InitializationAfterCommittedEffectsError(
                git_exclude_path,
                database_path,
                rendered_attempt_briefs,
            )
        rebuild_result = rebuild_facts(projection_facts, roots.work_root, rendered_attempt_briefs)
        if rebuild_result.warning is not None:
            raise FileIOError(FileIOErrorCode.VIEW_REFRESH_FAILED, rebuild_result.warning.message)
    except (StorageError, ArtifactError, FileIOError) as error:
        if git_exclude_path is None and database_path is None:
            raise
        raise InitializationAfterCommittedEffectsError(git_exclude_path, database_path, error) from error
    return InitReceipt(
        roots.work_root,
        roots.database_path,
        projection_facts.project_revision,
        database_already_exists,
    )


def _error_diagnostic(code: str, path: Path, message: str, hint: str | None = None) -> Diagnostic:
    return Diagnostic(code=code, severity=Severity.ERROR, path=path, message=message, hint=hint)


def read_state_for_validation(
    database_path: Path, store: ports.ValidatedStateReader
) -> stored_state.StoredWorkState | ValidationReport:
    """Read and structurally validate the authoritative SQLite snapshot once."""

    try:
        return store.validated_snapshot()
    except StorageError as error:
        return ValidationReport((_error_diagnostic(error.code.value, database_path, str(error)),))


def _package_provenance_failure(message: str) -> WorkBriefFailure:
    return WorkBriefFailure(WorkBriefErrorCode.PACKAGE_PROVENANCE_INVALID, message)


def _portable_reference(
    identity: work_brief_models.PortableArtifactIdentity,
    references: Mapping[tuple[str, str, int], stored_state.ArtifactReference],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> WorkBriefResult[stored_state.ArtifactReference]:
    reference = references.get((identity.kind, identity.key, identity.revision))
    if reference is None:
        return _package_provenance_failure(
            f"The {identity.role} identity does not resolve to an accepted artifact reference."
        )
    if (
        reference.selector != identity.selector
        or reference.content_sha256 != identity.content_sha256
        or reference.size_bytes != identity.size_bytes
        or reference.artifact_ref_id not in artifact_bytes
    ):
        return _package_provenance_failure(
            f"The {identity.role} identity does not match its verified accepted artifact bytes."
        )
    return reference


def _validate_package_artifact_identities(
    package: work_brief_models.CheckpointReviewPackage,
    references: Mapping[tuple[str, str, int], stored_state.ArtifactReference],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> WorkBriefResult[tuple[stored_state.ArtifactReference, stored_state.ArtifactReference]]:
    accepted_brief = _portable_reference(package.accepted_brief, references, artifact_bytes)
    if isinstance(accepted_brief, WorkBriefFailure):
        return accepted_brief
    result = _portable_reference(package.result, references, artifact_bytes)
    if isinstance(result, WorkBriefFailure):
        return result
    implementation_review = _portable_reference(package.implementation_review, references, artifact_bytes)
    if isinstance(implementation_review, WorkBriefFailure):
        return implementation_review
    checkpoint_id = package.checkpoint.id
    if (
        (package.accepted_brief.kind, package.accepted_brief.key)
        != (work_models.ArtifactKind.BRIEF.value, package.attempt_id)
        or (package.result.kind, package.result.key)
        != (work_models.ArtifactKind.RESULT.value, f"{package.attempt_id}-{checkpoint_id}-result")
        or (package.implementation_review.kind, package.implementation_review.key)
        != (work_models.ArtifactKind.EVIDENCE.value, f"{package.attempt_id}-{checkpoint_id}-review")
    ):
        return _package_provenance_failure("Checkpoint package artifact roles do not use their canonical identities.")
    return accepted_brief, implementation_review


def _validate_package_brief(
    package: work_brief_models.CheckpointReviewPackage,
    accepted_brief_reference: stored_state.ArtifactReference,
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> WorkBriefResult[work_brief_models.WorkBrief]:
    brief = decode_canonical_work_brief(artifact_bytes[accepted_brief_reference.artifact_ref_id])
    if isinstance(brief, WorkBriefFailure):
        return _package_provenance_failure(f"The package accepted brief is invalid: {brief.message}")
    if (
        brief.attempt_id != package.attempt_id
        or brief.item_id != package.item_id
        or brief.artifact_revision != package.accepted_brief.revision
        or brief.accepted_scope != package.accepted_scope
    ):
        return _package_provenance_failure("The package accepted brief does not match its historical attempt binding.")
    checkpoint_sha256 = hashlib.sha256(canonical_checkpoint_bytes(brief.checkpoint)).hexdigest()
    if brief.checkpoint.checkpoint_id != package.checkpoint.id or checkpoint_sha256 != package.checkpoint.sha256:
        return _package_provenance_failure("The package checkpoint identity does not match the accepted brief.")
    return brief


def _validate_package_review_basis(
    package: work_brief_models.CheckpointReviewPackage,
    brief: work_brief_models.WorkBrief,
    references: Mapping[tuple[str, str, int], stored_state.ArtifactReference],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> WorkBriefFailure | None:
    match brief.checkpoint, package.review_basis:
        case work_brief_models.LocalCheckpoint(), work_brief_models.LocalReviewBasis():
            return None
        case (
            work_brief_models.CrossBoundaryCheckpoint(reviewed_authorities=authorities),
            work_brief_models.CrossBoundaryReviewBasis(
                brief_review=review_identity,
                checkpoint_sha256=checkpoint_sha256,
                reviewed_authority_set_sha256=authority_set_sha256,
            ),
        ):
            review_reference = _portable_reference(review_identity, references, artifact_bytes)
            if isinstance(review_reference, WorkBriefFailure):
                return review_reference
            if (
                (review_identity.kind, review_identity.key)
                != (
                    work_models.ArtifactKind.EVIDENCE.value,
                    f"{package.attempt_id}-brief-review-{package.checkpoint.sha256}",
                )
                or checkpoint_sha256 != package.checkpoint.sha256
                or authority_set_sha256
                != hashlib.sha256(canonical_reviewed_authority_set_bytes(authorities)).hexdigest()
            ):
                return _package_provenance_failure("The cross-boundary review basis has a stale identity or digest.")
            review = decode_canonical_work_brief_review(artifact_bytes[review_reference.artifact_ref_id])
            if isinstance(review, WorkBriefFailure):
                return _package_provenance_failure(f"The package brief review is invalid: {review.message}")
            if (failure := validate_work_brief_review(review, brief)) is not None:
                return _package_provenance_failure(f"The package brief review is not ready: {failure.message}")
            return None
        case _:
            return _package_provenance_failure(
                "The package review basis does not match the accepted checkpoint boundary."
            )


def _checkpoint_outcome(
    receipt: stored_state.StoredTransitionReceipt,
) -> WorkBriefResult[history.CheckpointAcceptanceOutcome]:
    try:
        outcome = msgspec.json.decode(bytes(receipt.outcome_payload), type=history.CheckpointAcceptanceOutcome)
    except msgspec.DecodeError as error:
        return _package_provenance_failure(
            f"Checkpoint acceptance history {int(receipt.history_id)} has an invalid outcome: {error}"
        )
    if msgspec.json.encode(outcome, order="sorted") != bytes(receipt.outcome_payload):
        return _package_provenance_failure(
            f"Checkpoint acceptance history {int(receipt.history_id)} has a noncanonical outcome."
        )
    return outcome


def _validate_checkpoint_receipt(
    receipt: stored_state.StoredTransitionReceipt,
    package: work_brief_models.CheckpointReviewPackage,
) -> WorkBriefFailure | None:
    outcome = _checkpoint_outcome(receipt)
    if isinstance(outcome, WorkBriefFailure):
        return outcome
    if (
        receipt.outcome_schema != "checkpoint-acceptance/v2"
        or receipt.action_kind != decision_models.ActionKind.ACCEPT_CHECKPOINT
        or receipt.authorization != decision_models.AuthorizationKind.PROJECT
        or str(receipt.action_id) != f"accept-checkpoint:{package.attempt_id}"
        or str(receipt.subject_id) != package.attempt_id
        or outcome.candidate != package.candidate
        or outcome.checkpoint != package.checkpoint.id
        or outcome.evidence != package.acceptance_evidence
        or outcome.outcome != decision_models.ActionKind.ACCEPT_CHECKPOINT.value
    ):
        return _package_provenance_failure(
            f"Checkpoint acceptance history {int(receipt.history_id)} does not match its review package."
        )
    return None


def validate_selected_checkpoint_review_package(
    receipt: stored_state.StoredTransitionReceipt,
    package_reference: stored_state.ArtifactReference,
    package_bytes: bytes,
    *,
    attempt_id: str,
    item_id: str,
) -> WorkBriefResult[work_brief_models.CheckpointReviewPackage]:
    """Validate one caller-selected package without scanning retained state."""

    package = decode_canonical_checkpoint_review_package(package_bytes)
    if isinstance(package, WorkBriefFailure):
        return package
    if (
        receipt.artifact_ref_id != package_reference.artifact_ref_id
        or package_reference.kind != work_models.ArtifactKind.EVIDENCE
        or package_reference.key != f"{package.attempt_id}-{package.checkpoint.id}-review-package"
        or package_reference.revision != 1
        or package.attempt_id != attempt_id
        or package.item_id != item_id
    ):
        return _package_provenance_failure(
            f"Checkpoint acceptance history {int(receipt.history_id)} does not resolve the selected attempt package."
        )
    if (failure := _validate_checkpoint_receipt(receipt, package)) is not None:
        return failure
    return package


def validate_checkpoint_package_closure(
    package: work_brief_models.CheckpointReviewPackage,
    artifact_references: tuple[stored_state.ArtifactReference, ...],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> WorkBriefFailure | None:
    references = {(value.kind.value, value.key, value.revision): value for value in artifact_references}
    resolved = _validate_package_artifact_identities(package, references, artifact_bytes)
    if isinstance(resolved, WorkBriefFailure):
        return resolved
    accepted_brief_reference, _implementation_review_reference = resolved
    brief = _validate_package_brief(package, accepted_brief_reference, artifact_bytes)
    if isinstance(brief, WorkBriefFailure):
        return brief
    return _validate_package_review_basis(package, brief, references, artifact_bytes)


def _validate_one_checkpoint_package(
    receipt: stored_state.StoredTransitionReceipt,
    package_reference: stored_state.ArtifactReference,
    package: work_brief_models.CheckpointReviewPackage,
    attempts: Mapping[str, stored_state.StoredAttempt],
    item_ids: frozenset[str],
    definition_digests: Mapping[tuple[str, int], str],
    references: Mapping[tuple[str, str, int], stored_state.ArtifactReference],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> WorkBriefResult[handover.HandoverCheckpointPackage]:
    if (failure := _validate_checkpoint_receipt(receipt, package)) is not None:
        return failure
    attempt = attempts.get(package.attempt_id)
    if attempt is None or str(attempt.item_id) != package.item_id or package.item_id not in item_ids:
        return _package_provenance_failure("Checkpoint package does not resolve to its historical attempt and item.")
    if (
        definition_digests.get((package.item_id, package.accepted_scope.revision)) != package.accepted_scope.digest
        or package_reference.kind != work_models.ArtifactKind.EVIDENCE
        or package_reference.key != f"{package.attempt_id}-{package.checkpoint.id}-review-package"
        or package_reference.revision != 1
    ):
        return _package_provenance_failure("Checkpoint package does not match its accepted scope or package identity.")
    artifact_result = _validate_package_artifact_identities(package, references, artifact_bytes)
    if isinstance(artifact_result, WorkBriefFailure):
        return artifact_result
    accepted_brief_reference, _implementation_review_reference = artifact_result
    brief = _validate_package_brief(package, accepted_brief_reference, artifact_bytes)
    if isinstance(brief, WorkBriefFailure):
        return brief
    if (failure := _validate_package_review_basis(package, brief, references, artifact_bytes)) is not None:
        return failure
    if receipt.artifact_ref_id is None:
        raise AssertionError("Validated package receipt must link one artifact.")
    return msgspec.convert(
        {
            "history_id": int(receipt.history_id),
            "package_artifact_ref_id": int(receipt.artifact_ref_id),
            **msgspec.to_builtins(package),
        },
        type=handover.HandoverCheckpointPackage,
        strict=True,
    )


def validate_checkpoint_review_packages(
    lifecycle: stored_state.LifecycleRecords,
    artifact_references: tuple[stored_state.ArtifactReference, ...],
    transition_receipts: tuple[stored_state.StoredTransitionReceipt, ...],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> WorkBriefResult[tuple[handover.HandoverCheckpointPackage, ...]]:
    """Validate historical package provenance from one loaded state and verified bytes."""

    references_by_id = {value.artifact_ref_id: value for value in artifact_references}
    references = {(value.kind.value, value.key, value.revision): value for value in artifact_references}
    attempts = {str(value.attempt_id): value for value in lifecycle.attempts}
    item_ids = frozenset(str(value.item_id) for value in lifecycle.work_items)
    definition_digests = {
        (str(value.item_id), value.revision): value.digest for value in lifecycle.definition_revisions
    }
    linked_package_ids: set[ArtifactRefId] = set()
    packages: list[handover.HandoverCheckpointPackage] = []
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
        package = decode_canonical_checkpoint_review_package(artifact_bytes[receipt.artifact_ref_id])
        if isinstance(package, WorkBriefFailure):
            return WorkBriefFailure(
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
        if isinstance(validated, WorkBriefFailure):
            return validated
        packages.append(validated)
    orphaned = tuple(
        value.key
        for value in artifact_references
        if value.kind == work_models.ArtifactKind.EVIDENCE
        and value.key.endswith("-review-package")
        and not value.key.endswith("-completion-review-package")
        and value.artifact_ref_id not in linked_package_ids
    )
    if orphaned:
        return _package_provenance_failure(
            f"Accepted checkpoint packages are not linked from history: {', '.join(orphaned)}"
        )
    return tuple(packages)


def _completion_outcome(
    receipt: stored_state.StoredTransitionReceipt,
) -> WorkBriefResult[history.CompletionAcceptanceOutcome]:
    try:
        outcome = msgspec.json.decode(bytes(receipt.outcome_payload), type=history.CompletionAcceptanceOutcome)
    except msgspec.DecodeError as error:
        return _package_provenance_failure(
            f"Completion history {int(receipt.history_id)} has an invalid outcome: {error}"
        )
    if msgspec.json.encode(outcome, order="sorted") != bytes(receipt.outcome_payload):
        return _package_provenance_failure(f"Completion history {int(receipt.history_id)} has a noncanonical outcome.")
    return outcome


def _completion_reference(
    identity: work_brief_models.CompletionPortableArtifactIdentity,
    references: Mapping[tuple[str, str, int], stored_state.ArtifactReference],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> WorkBriefResult[stored_state.ArtifactReference]:
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


def validate_completion_review_packages(  # noqa: C901, PLR0912 - one exact terminal-package validator
    lifecycle: stored_state.LifecycleRecords,
    artifact_references: tuple[stored_state.ArtifactReference, ...],
    transition_receipts: tuple[stored_state.StoredTransitionReceipt, ...],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
    checkpoint_packages: tuple[handover.HandoverCheckpointPackage, ...],
) -> WorkBriefResult[tuple[handover.HandoverCompletionReviewPackage, ...]]:
    references_by_id = {value.artifact_ref_id: value for value in artifact_references}
    references = {(value.kind.value, value.key, value.revision): value for value in artifact_references}
    attempts = {str(value.attempt_id): value for value in lifecycle.attempts}
    definitions = {(str(value.item_id), value.revision): value.digest for value in lifecycle.definition_revisions}
    checkpoints_by_attempt: dict[str, list[handover.HandoverCheckpointPackage]] = {}
    for checkpoint in checkpoint_packages:
        checkpoints_by_attempt.setdefault(checkpoint.attempt_id, []).append(checkpoint)
    linked_package_ids: set[ArtifactRefId] = set()
    completed: list[handover.HandoverCompletionReviewPackage] = []
    for receipt in transition_receipts:
        if receipt.outcome_schema != "completion-acceptance/v2":
            continue
        outcome = _completion_outcome(receipt)
        if isinstance(outcome, WorkBriefFailure):
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
        package = decode_canonical_completion_review_package(artifact_bytes[receipt.artifact_ref_id])
        if isinstance(package, WorkBriefFailure):
            return package
        if (
            receipt.action_kind != decision_models.ActionKind.COMPLETE
            or receipt.authorization != decision_models.AuthorizationKind.PROJECT
            or str(receipt.action_id) != f"complete:{package.attempt_id}"
            or str(receipt.subject_id) != package.attempt_id
            or reference.kind != work_models.ArtifactKind.EVIDENCE
            or reference.key != f"{package.attempt_id}-completion-review-package"
            or reference.revision != 1
            or outcome.candidate != package.candidate
            or outcome.evidence != package.outcome_evidence
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
        terminal_result = _completion_reference(package.terminal_result, references, artifact_bytes)
        final_review = _completion_reference(package.final_review, references, artifact_bytes)
        if any(isinstance(value, WorkBriefFailure) for value in (accepted_brief, terminal_result, final_review)):
            return next(
                value
                for value in (accepted_brief, terminal_result, final_review)
                if isinstance(value, WorkBriefFailure)
            )
        assert isinstance(accepted_brief, stored_state.ArtifactReference)
        assert isinstance(terminal_result, stored_state.ArtifactReference)
        assert isinstance(final_review, stored_state.ArtifactReference)
        if (
            package.accepted_brief.key != package.attempt_id
            or package.terminal_result.key != f"{package.attempt_id}-terminal-result"
            or package.final_review.key != f"{package.attempt_id}-terminal-review"
            or attempt.state != work_models.AttemptState.DONE
            or attempt.brief_artifact_ref_id != accepted_brief.artifact_ref_id
            or attempt.result_artifact_ref_id != terminal_result.artifact_ref_id
        ):
            return _package_provenance_failure("Completion package artifact identities are not canonical.")
        brief = decode_canonical_work_brief(artifact_bytes[accepted_brief.artifact_ref_id])
        if (
            isinstance(brief, WorkBriefFailure)
            or brief.attempt_id != package.attempt_id
            or brief.item_id != package.item_id
            or brief.accepted_scope != package.accepted_scope
            or brief.owner_task_id == package.reviewer_task_id
            or (receipt.actor_task_id is not None and str(receipt.actor_task_id) == package.reviewer_task_id)
        ):
            return _package_provenance_failure("Completion package accepted brief is invalid or stale.")
        expected_checkpoints = checkpoints_by_attempt.get(package.attempt_id, [])
        if tuple(row.history_id for row in package.checkpoint_coverage) != tuple(
            value.history_id for value in expected_checkpoints
        ):
            return _package_provenance_failure("Completion checkpoint coverage is incomplete or stale.")
        for row, checkpoint in zip(package.checkpoint_coverage, expected_checkpoints, strict=True):
            package_reference = _completion_reference(row.package, references, artifact_bytes)
            if isinstance(package_reference, WorkBriefFailure):
                return package_reference
            if (
                row.checkpoint.id != checkpoint.checkpoint.id
                or row.checkpoint.sha256 != checkpoint.checkpoint.sha256
                or row.candidate != checkpoint.candidate
                or int(package_reference.artifact_ref_id) != checkpoint.package_artifact_ref_id
            ):
                return _package_provenance_failure("Completion checkpoint coverage does not match its package.")
        completed.append(
            msgspec.convert(
                {
                    "history_id": int(receipt.history_id),
                    "package_artifact_ref_id": int(receipt.artifact_ref_id),
                    **msgspec.to_builtins(package),
                },
                type=handover.HandoverCompletionReviewPackage,
                strict=True,
            )
        )
    orphaned = tuple(
        value.key
        for value in artifact_references
        if value.kind == work_models.ArtifactKind.EVIDENCE
        and value.key.endswith("-completion-review-package")
        and value.artifact_ref_id not in linked_package_ids
    )
    if orphaned:
        return _package_provenance_failure(
            f"Accepted completion packages are not linked from history: {', '.join(orphaned)}"
        )
    return tuple(completed)


def validate_loaded_work_state(
    work_root: Path,
    state: stored_state.StoredWorkState,
    attempt_briefs: Mapping[AttemptId, bytes],
    *,
    now: datetime,
) -> ValidationReport:
    """Verify accepted bytes, then classify replaceable generated-view drift."""

    diagnostics: list[Diagnostic] = []
    verified_artifacts: dict[ArtifactRefId, bytes] = {}
    for reference in state.artifact_references:
        try:
            verified_artifacts[reference.artifact_ref_id] = read_reference(work_root, reference)
        except ArtifactError as error:
            diagnostics.append(_error_diagnostic(error.code.value, work_root / reference.selector, str(error)))
    packages = validate_checkpoint_review_packages(
        state.lifecycle,
        state.artifact_references,
        state.transition_receipts,
        verified_artifacts,
    )
    if isinstance(packages, WorkBriefFailure):
        diagnostics.append(_error_diagnostic(packages.code.value, work_root, packages.message))
    else:
        completion_packages = validate_completion_review_packages(
            state.lifecycle,
            state.artifact_references,
            state.transition_receipts,
            verified_artifacts,
            packages,
        )
        if isinstance(completion_packages, WorkBriefFailure):
            diagnostics.append(
                _error_diagnostic(completion_packages.code.value, work_root, completion_packages.message)
            )
    view_root = work_root / "views"
    for selector, expected in derive_expected_view_bytes(state, attempt_briefs, now=now).items():
        path = view_root / selector
        try:
            actual_view_bytes = path.read_bytes()
        except OSError:
            actual_view_bytes = None
        if actual_view_bytes != expected:
            diagnostics.append(
                Diagnostic(
                    "VIEW_REFRESH_REQUIRED",
                    Severity.WARNING,
                    path,
                    "Generated view is absent or stale; SQLite remains authoritative.",
                    "Run 'pinboard views rebuild'.",
                )
            )
    return ValidationReport(tuple(diagnostics))
