"""Remedy selected retained-v1 patch evidence, then freshly prepare review.

Publishes canonical revision-one historical patch bytes without strengthening
their assurance. Owns invocation-total publication aftermath, not lifecycle,
authority, file acquisition, terminal presentation or correction-start legality.
Infrastructure failure after publication preserves every committed surface.
"""

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from pinboard.adapters import review_operations
from pinboard.adapters.files.artifacts import read_reference
from pinboard.adapters.files.errors import ArtifactError
from pinboard.application import (
    artifact_publication,
    checkpoint_compatibility_models,
    checkpoint_packages,
    dispatch_models,
    ports,
    queries,
    work_brief_models,
)
from pinboard.application.artifacts import NewArtifact
from pinboard.domain import errors, work_models
from pinboard.domain.identifiers import AttemptId, HistoryId


def prepare_recovered_review_job(  # noqa: C901 - one cohesive selected remedy and invocation-total aftermath
    work_root: Path,
    store: ports.WorkStore,
    artifacts: dispatch_models.DispatchArtifactPort,
    attempt_id: AttemptId,
    candidate_revision: str,
    checkpoint_history_id: HistoryId,
    correction_history_id: HistoryId | None,
    patch: bytes,
) -> errors.DecisionResult[review_operations.PreparedReviewJob]:
    facts = queries.select_review_job_context(store, attempt_id, checkpoint_history_id, correction_history_id)
    if isinstance(facts, errors.DecisionFailure):
        return facts
    receipt, reference = facts.checkpoint_receipt, facts.checkpoint_package_reference
    if receipt is None or reference is None:
        return errors.DecisionFailure(
            errors.DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Selected checkpoint history does not link an accepted package artifact.",
            None,
        )
    package = checkpoint_packages.validate_selected_checkpoint_review_package(
        receipt,
        reference,
        read_reference(work_root, reference),
        attempt_id=str(attempt_id),
        item_id=str(facts.attempt.item_id),
    )
    if isinstance(package, work_brief_models.WorkBriefFailure):
        return errors.DecisionFailure(errors.DecisionFailureCode.ACTION_NOT_AVAILABLE, package.message, None)
    if not isinstance(package, checkpoint_compatibility_models.CheckpointReviewPackage):
        return errors.DecisionFailure(
            errors.DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Current checkpoint packages cannot use legacy candidate recovery.",
            None,
        )
    expected = package.candidate.removeprefix("working-tree-sha256:")
    if not package.candidate.startswith("working-tree-sha256:") or len(expected) != 64:
        return errors.DecisionFailure(
            errors.DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Selected checkpoint candidate is not a recoverable working-tree identity.",
            None,
        )
    observed = sha256(patch).hexdigest()
    if observed != expected:
        return errors.DecisionFailure(
            errors.DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Candidate patch does not match the accepted checkpoint candidate.",
            errors.FailureDetails(
                observed=(
                    errors.FailureFact("checkpoint_history_id", int(checkpoint_history_id)),
                    errors.FailureFact("observed_sha256", observed),
                ),
                mismatches=(errors.FailureMismatch("candidate_sha256", expected, observed),),
                retry=errors.RetryDisposition.CORRECT_INPUT,
                effect=errors.EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    publication = artifact_publication.publish_accepted_artifact(
        store,
        artifacts,
        NewArtifact(
            work_models.ArtifactKind.EVIDENCE,
            f"{package.attempt_id}-{package.checkpoint.id}-candidate",
            1,
            ".patch",
            patch,
        ),
        datetime.now(UTC),
    )
    if isinstance(publication, errors.DecisionFailure):
        return publication
    surfaces = (
        *((errors.ChangedSurface.IMMUTABLE_ARTIFACT,) if publication.artifact_created else ()),
        *(
            (errors.ChangedSurface.ACCEPTED_ARTIFACT_REFERENCE, errors.ChangedSurface.LEDGER)
            if publication.ledger_changed
            else ()
        ),
    )
    try:
        prepared = review_operations.prepare_review_job(
            work_root,
            store,
            artifacts,
            attempt_id,
            candidate_revision,
            checkpoint_history_id,
            correction_history_id,
        )
    except (errors.ArtifactAcceptanceAfterPublicationError, ArtifactError, ports.WorkStoreError) as error:
        if not surfaces:
            raise
        if isinstance(error, errors.ArtifactAcceptanceAfterPublicationError):
            raise errors.ArtifactAcceptanceAfterPublicationError(
                error.selector,
                error.cause,
                tuple(dict.fromkeys((*surfaces, *error.changed_surfaces))),
            ) from error
        raise errors.ArtifactAcceptanceAfterPublicationError(publication.reference.selector, error, surfaces) from error
    if isinstance(prepared, errors.DecisionFailure):
        if not surfaces:
            return prepared
        details = prepared.details
        return errors.DecisionFailure(
            prepared.code,
            prepared.message,
            errors.FailureDetails(
                observed=(
                    errors.FailureFact("recovered_candidate_selector", publication.reference.selector),
                    *(() if details is None else details.observed),
                ),
                mismatches=() if details is None else details.mismatches,
                retry=errors.RetryDisposition.DO_NOT_RETRY,
                effect=errors.EffectDisposition.COMMITTED,
                changed_surfaces=tuple(
                    dict.fromkeys((*surfaces, *(() if details is None else details.changed_surfaces)))
                ),
                alternatives=() if details is None else details.alternatives,
            ),
        )
    prompt = prepared.published_prompt
    return replace(
        prepared,
        published_prompt=dispatch_models.PublishedAgentPrompt(
            str(prompt),
            prompt.reference,
            tuple(dict.fromkeys((*surfaces, *prompt.changed_surfaces))),
        ),
    )
