"""Recover missing candidate bytes for retained checkpoint v1 packages.

The selected recovery path can publish and accept candidate evidence. Its
failure and exception aftermath preserves every committed publication surface.
Current acceptance never creates v1 packages; ordinary review remains in
work_inspection and calls this owner only for compatibility-specific work.
"""

import shlex
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Never, assert_never

from pinboard.adapters.files.artifacts import ArtifactRepository, read_reference
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.application import artifact_publication, dispatch_models, ports, query_models, stored_state
from pinboard.application.artifacts import NewArtifact
from pinboard.cli import checkpoint_compatibility_models, cli_commands, errors, work_state
from pinboard.domain import errors as domain_errors
from pinboard.domain import work_models
from pinboard.domain.identifiers import HistoryId


def _candidate_patch_path(command: cli_commands.ReviewJobCommand) -> Path | None:
    match command:
        case (
            cli_commands.CompatibilityPackageInitialRecoveryReviewJobCommand(candidate_patch=candidate_patch)
            | cli_commands.CompatibilityPackageCorrectionRecoveryReviewJobCommand(candidate_patch=candidate_patch)
        ):
            return candidate_patch
        case (
            cli_commands.InitialReviewJobCommand()
            | cli_commands.PackageInitialReviewJobCommand()
            | cli_commands.CorrectionReviewJobCommand()
            | cli_commands.PackageCorrectionReviewJobCommand()
        ):
            return None
        case _ as unreachable:
            assert_never(unreachable)


def recover_checkpoint_candidate(
    roots: cli_commands.ResolvedRoots,
    durable: DurableRoots,
    store: ports.WorkStore,
    command: cli_commands.ReviewJobCommand,
    facts: query_models.ReviewJobContextFacts,
    checkpoint_history_id: HistoryId | None,
) -> errors.CommandResult[artifact_publication.AcceptedArtifactPublication | None]:
    candidate_patch = _candidate_patch_path(command)
    if candidate_patch is None:
        return None
    if checkpoint_history_id is None:
        raise AssertionError("Recovery commands require checkpoint history.")
    receipt = facts.checkpoint_receipt
    package_reference = facts.checkpoint_package_reference
    if receipt is None or package_reference is None:
        return errors.CommandFailure(
            domain_errors.DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Selected checkpoint history does not link an accepted package artifact.",
            None,
        )
    package = work_state.validate_selected_checkpoint_review_package(
        receipt,
        package_reference,
        read_reference(roots.work, package_reference),
        attempt_id=str(command.attempt_id),
        item_id=str(facts.attempt.item_id),
    )
    if isinstance(package, errors.WorkBriefFailure):
        return errors.CommandFailure(domain_errors.DecisionFailureCode.ACTION_NOT_AVAILABLE, package.message, None)
    if not isinstance(package, checkpoint_compatibility_models.CheckpointReviewPackage):
        return errors.CommandFailure(
            domain_errors.DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Current checkpoint packages cannot use legacy candidate recovery.",
            None,
        )
    expected_sha256 = package.candidate.removeprefix("working-tree-sha256:")
    if not package.candidate.startswith("working-tree-sha256:") or len(expected_sha256) != 64:
        return errors.CommandFailure(
            domain_errors.DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Selected checkpoint candidate is not a recoverable working-tree identity.",
            None,
        )
    try:
        patch_bytes = candidate_patch.read_bytes()
    except OSError as error:
        return errors.CommandFailure(
            domain_errors.DecisionFailureCode.TRANSITION_INPUT_INVALID,
            f"Cannot read candidate patch: {error}",
            domain_errors.FailureDetails(
                observed=(domain_errors.FailureFact("candidate_patch", str(candidate_patch)),),
                mismatches=(),
                retry=domain_errors.RetryDisposition.CORRECT_INPUT,
                effect=domain_errors.EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    observed_sha256 = sha256(patch_bytes).hexdigest()
    if observed_sha256 != expected_sha256:
        return errors.CommandFailure(
            domain_errors.DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Candidate patch does not match the accepted checkpoint candidate.",
            domain_errors.FailureDetails(
                observed=(
                    domain_errors.FailureFact("checkpoint_history_id", int(checkpoint_history_id)),
                    domain_errors.FailureFact("candidate_patch", str(candidate_patch)),
                    domain_errors.FailureFact("observed_sha256", observed_sha256),
                ),
                mismatches=(domain_errors.FailureMismatch("candidate_sha256", expected_sha256, observed_sha256),),
                retry=domain_errors.RetryDisposition.CORRECT_INPUT,
                effect=domain_errors.EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    published = artifact_publication.publish_accepted_artifact(
        store,
        ArtifactRepository(durable),
        NewArtifact(
            work_models.ArtifactKind.EVIDENCE,
            f"{package.attempt_id}-{package.checkpoint.id}-candidate",
            1,
            ".patch",
            patch_bytes,
        ),
        datetime.now(UTC),
    )
    if isinstance(published, domain_errors.DecisionFailure):
        return errors.CommandFailure(published.code, published.message, published.details)
    return published


def recovery_surfaces(
    publication: artifact_publication.AcceptedArtifactPublication | None,
) -> tuple[domain_errors.ChangedSurface, ...]:
    if publication is None:
        return ()
    return (
        *((domain_errors.ChangedSurface.IMMUTABLE_ARTIFACT,) if publication.artifact_created else ()),
        *((domain_errors.ChangedSurface.ACCEPTED_ARTIFACT_REFERENCE,) if publication.ledger_changed else ()),
        *((domain_errors.ChangedSurface.LEDGER,) if publication.ledger_changed else ()),
    )


def after_recovery_failure(
    failure: errors.CommandFailure,
    publication: artifact_publication.AcceptedArtifactPublication | None,
) -> errors.CommandFailure:
    surfaces = recovery_surfaces(publication)
    if not surfaces:
        return failure
    assert publication is not None
    details = failure.details
    return errors.CommandFailure(
        failure.code,
        failure.message,
        domain_errors.FailureDetails(
            observed=(
                domain_errors.FailureFact("recovered_candidate_selector", publication.reference.selector),
                *(() if details is None else details.observed),
            ),
            mismatches=() if details is None else details.mismatches,
            retry=domain_errors.RetryDisposition.DO_NOT_RETRY,
            effect=domain_errors.EffectDisposition.COMMITTED,
            changed_surfaces=surfaces,
            alternatives=() if details is None else details.alternatives,
        ),
    )


def raise_after_recovery_exception(
    error: Exception,
    publication: artifact_publication.AcceptedArtifactPublication | None,
) -> Never:
    surfaces = recovery_surfaces(publication)
    if not surfaces:
        raise error
    assert publication is not None
    if isinstance(error, domain_errors.ArtifactAcceptanceAfterPublicationError):
        raise domain_errors.ArtifactAcceptanceAfterPublicationError(
            error.selector,
            error.cause,
            tuple(dict.fromkeys((*surfaces, *error.changed_surfaces))),
        ) from error
    raise domain_errors.ArtifactAcceptanceAfterPublicationError(
        publication.reference.selector,
        error,
        surfaces,
    ) from error


def require_candidate_reference(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.ReviewJobCommand,
    package: checkpoint_compatibility_models.CheckpointReviewPackage,
    candidate_reference: stored_state.ArtifactReference | None,
    checkpoint_history_id: HistoryId,
) -> errors.CommandFailure | None:
    if (
        not package.candidate.startswith("working-tree-sha256:")
        or len(package.candidate.removeprefix("working-tree-sha256:")) != 64
        or candidate_reference is None
    ):
        candidate_patch = "CANDIDATE_PATCH"
        recovery_command = shlex.join(
            (
                *dispatch_models.pinboard_launcher_command(),
                "--project-root",
                str(roots.source_checkout),
                "--work-root",
                str(roots.work),
                "review-job",
                "--attempt-id",
                str(command.attempt_id),
                "--candidate-revision",
                command.candidate_revision,
                "--checkpoint-history-id",
                str(int(checkpoint_history_id)),
                *(
                    ()
                    if not isinstance(
                        command,
                        (
                            cli_commands.PackageCorrectionReviewJobCommand,
                            cli_commands.CompatibilityPackageCorrectionRecoveryReviewJobCommand,
                        ),
                    )
                    else ("--correction-history-id", str(command.correction_history_id))
                ),
                "--candidate-patch",
                candidate_patch,
                "--json",
            )
        )
        return errors.CommandFailure(
            domain_errors.DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Selected checkpoint candidate bytes are not accepted; use the exact recovery command with the matching patch.",
            domain_errors.FailureDetails(
                observed=(
                    domain_errors.FailureFact("checkpoint_history_id", int(checkpoint_history_id)),
                    domain_errors.FailureFact("accepted_candidate", package.candidate),
                    domain_errors.FailureFact("candidate_evidence_reference", None),
                    domain_errors.FailureFact("recovery_command", recovery_command),
                ),
                mismatches=(domain_errors.FailureMismatch("candidate_evidence", "accepted immutable reference", None),),
                retry=domain_errors.RetryDisposition.CORRECT_INPUT,
                effect=domain_errors.EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    return None


def candidate_matches(
    package: checkpoint_compatibility_models.CheckpointReviewPackage,
    reference: stored_state.ArtifactReference,
) -> bool:
    return reference.content_sha256 == package.candidate.removeprefix("working-tree-sha256:")
