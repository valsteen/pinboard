from dataclasses import dataclass
from datetime import datetime

from pinboard.application import artifact_identity, query_models, stored_state
from pinboard.application.actions import discover_current_actions
from pinboard.application.artifact_publication import (
    ArtifactAcceptanceFailure,
    ArtifactWriteFailure,
    PublicationSurface,
    PublishedArtifactRejected,
    PublishedFailureDetails,
    accepted_publication_surfaces,
    publish_accepted_evidence,
)
from pinboard.application.artifacts import BriefArtifactRef
from pinboard.application.dispatch_models import (
    DispatchArtifactPort,
    DispatchFailure,
    DispatchRejectionCode,
    DispatchResult,
)
from pinboard.application.ports import WorkStore
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import (
    DecisionFailure,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    FailureMismatch,
    RetryDisposition,
)
from pinboard.domain.identifiers import AttemptId, ReviewId


@dataclass(frozen=True, slots=True)
class SelectedDispatch:
    attempt: query_models.NonterminalAttemptContextFacts
    brief_reference: BriefArtifactRef


@dataclass(frozen=True, slots=True)
class AcceptedDispatchReview:
    reference: stored_state.EvidenceArtifactReference
    changed_surfaces: tuple[PublicationSurface, ...]


def _rediscover_dispatch_action(
    store: WorkStore,
    supplied: decision_models.DispatchAction,
    now: datetime,
) -> DispatchResult[decision_models.Action | None]:
    actions = discover_current_actions(
        store.read_decision_facts(
            query_models.DecisionScope(
                work_item_ids=(),
                related_work_item_ids=(),
                dependency_closure_roots=(),
                live_dependent_roots=(),
                live_dependent_attempt_ids=(),
                attempt_ids=(supplied.capability.subject,),
                proposal_ids=(),
                artifact_ref_ids=(),
                completion_history_attempt_ids=(),
                checkpoint_targets=(),
            ),
            now,
        ).snapshot,
        decision_models.Role.PROJECT,
    )
    if isinstance(actions, DecisionFailure):
        return DispatchFailure(actions.code, actions.message, actions.details)
    return next(
        (value for value in actions if decision_models.action_id(value) == decision_models.action_id(supplied)), None
    )


def _current_dispatch_action(
    store: WorkStore,
    supplied: decision_models.DispatchAction,
    now: datetime,
) -> DispatchResult[decision_models.DispatchAction]:
    current = _rediscover_dispatch_action(store, supplied, now)
    if isinstance(current, DispatchFailure):
        return current
    if not isinstance(current, decision_models.DispatchAction):
        return DispatchFailure(
            DispatchRejectionCode.ACTION_UNAVAILABLE,
            f"Action '{decision_models.action_id(supplied)}' is not available.",
            None,
        )
    if current != supplied:
        if current.capability.subject_revision != supplied.capability.subject_revision:
            return DispatchFailure(
                DispatchRejectionCode.STALE_ACTION,
                "The attempt changed after this dispatch action was selected.",
                FailureDetails(
                    observed=(),
                    mismatches=(
                        FailureMismatch(
                            "subject_revision",
                            current.capability.subject_revision,
                            supplied.capability.subject_revision,
                        ),
                    ),
                    retry=RetryDisposition.REFRESH_ACTION,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
            )
        return DispatchFailure(
            DispatchRejectionCode.ACTION_INVALID,
            "The dispatch action does not carry exact current authority.",
            None,
        )
    return current


def select_dispatch(
    store: WorkStore,
    action: decision_models.Action,
    now: datetime,
) -> DispatchResult[SelectedDispatch]:
    if not isinstance(action, decision_models.DispatchAction):
        return DispatchFailure(
            DispatchRejectionCode.ACTION_UNAVAILABLE,
            f"Action '{decision_models.action_id(action)}' is not a dispatch action.",
            None,
        )
    current = _current_dispatch_action(store, action, now)
    if isinstance(current, DispatchFailure):
        return current
    attempt_id = current.capability.subject
    attempt = store.read_attempt_context(attempt_id)
    if (
        not isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        or attempt.state != work_models.AttemptState.ACTIVE
    ):
        return DispatchFailure(DispatchRejectionCode.ATTEMPT_NOT_ACTIVE, f"Attempt '{attempt_id}' is not active.", None)
    return SelectedDispatch(attempt, attempt.brief_reference)


def _find_ready_review_reference(
    store: WorkStore,
    attempt_id: AttemptId,
    review_key_sha256: str,
) -> stored_state.EvidenceArtifactReference | None:
    key = f"{attempt_id}-brief-review-{review_key_sha256}"
    return store.read_evidence_reference(key, 1)


def find_dispatch_review(
    store: WorkStore,
    attempt_id: AttemptId,
    review_key_sha256: str,
) -> DispatchResult[stored_state.EvidenceArtifactReference]:
    existing = _find_ready_review_reference(store, attempt_id, review_key_sha256)
    if existing is None:
        return DispatchFailure(DispatchRejectionCode.REVIEW_MISSING, "The exact ready brief review is absent.", None)
    return existing


def publish_dispatch_review(
    store: WorkStore,
    artifacts: DispatchArtifactPort,
    attempt_id: AttemptId,
    review_key_sha256: str,
    candidate: bytes,
    review_id: ReviewId,
    accepted_at: datetime,
) -> DispatchResult[AcceptedDispatchReview] | ArtifactAcceptanceFailure | ArtifactWriteFailure:
    key = f"{attempt_id}-brief-review-{review_key_sha256}"
    existing = _find_ready_review_reference(store, attempt_id, review_key_sha256)
    if existing is not None:
        if artifacts.read(existing) == candidate:
            return AcceptedDispatchReview(existing, ())
        rejected_acceptance = publish_accepted_evidence(
            store,
            artifacts,
            f"{key}-rejected-{review_id}",
            1,
            ".json",
            candidate,
            accepted_at,
            artifact_identity.AttemptMembership(attempt_id, artifact_identity.AttemptRole.SOURCE),
        )
        if isinstance(rejected_acceptance, (ArtifactAcceptanceFailure, ArtifactWriteFailure)):
            return rejected_acceptance
        if isinstance(rejected_acceptance, (DecisionFailure, PublishedArtifactRejected)):
            return DispatchFailure(
                DispatchRejectionCode.STALE_ACTION,
                rejected_acceptance.message,
                rejected_acceptance.details,
            )
        rejected = rejected_acceptance.reference
        changed_surfaces = accepted_publication_surfaces(rejected_acceptance)
        observed = (
            FailureFact("published_artifact_selector", rejected.selector),
            FailureFact("accepted_revision", rejected.accepted_revision),
        )
        return DispatchFailure(
            DispatchRejectionCode.REVIEW_COLLISION,
            f"Ready review already differs; later evidence is preserved at '{rejected.selector}'.",
            PublishedFailureDetails(observed, (), changed_surfaces, ())
            if changed_surfaces
            else FailureDetails(
                observed=observed,
                mismatches=(),
                retry=RetryDisposition.DO_NOT_RETRY,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    accepted_publication = publish_accepted_evidence(
        store,
        artifacts,
        key,
        1,
        ".json",
        candidate,
        accepted_at,
        artifact_identity.AttemptMembership(attempt_id, artifact_identity.AttemptRole.SOURCE),
    )
    if isinstance(accepted_publication, (ArtifactAcceptanceFailure, ArtifactWriteFailure)):
        return accepted_publication
    if isinstance(accepted_publication, (DecisionFailure, PublishedArtifactRejected)):
        return DispatchFailure(
            DispatchRejectionCode.STALE_ACTION,
            accepted_publication.message,
            accepted_publication.details,
        )
    return AcceptedDispatchReview(accepted_publication.reference, accepted_publication_surfaces(accepted_publication))


def recheck_dispatch_authority(
    store: WorkStore,
    supplied: decision_models.DispatchAction,
    review_publication_surfaces: tuple[PublicationSurface, ...],
    now: datetime,
) -> DispatchFailure | None:
    capability = supplied.capability
    rediscovered = _rediscover_dispatch_action(store, supplied, now)
    if isinstance(rediscovered, DispatchFailure):
        return rediscovered
    current = rediscovered if isinstance(rediscovered, decision_models.DispatchAction) else None
    if current == supplied:
        return None
    mismatches = (
        FailureMismatch(
            "subject_revision",
            None if current is None else current.capability.subject_revision,
            capability.subject_revision,
        ),
    )
    return DispatchFailure(
        DispatchRejectionCode.ACTION_UNAVAILABLE,
        "Dispatch authority changed during prompt preparation.",
        PublishedFailureDetails((), mismatches, review_publication_surfaces, ())
        if review_publication_surfaces
        else FailureDetails(
            observed=(),
            mismatches=mismatches,
            retry=RetryDisposition.REFRESH_ACTION,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )
