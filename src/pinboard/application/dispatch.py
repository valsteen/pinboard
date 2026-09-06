from dataclasses import dataclass, replace
from datetime import datetime

from pinboard.application import stored_state
from pinboard.application.actions import discover_actions
from pinboard.application.artifacts import NewArtifact
from pinboard.application.dispatch_models import (
    DispatchArtifactPort,
    DispatchFailure,
    DispatchRejectionCode,
    DispatchResult,
)
from pinboard.application.ports import WorkStore
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import AttemptId, ReviewId


@dataclass(frozen=True, slots=True)
class SelectedDispatch:
    attempt: stored_state.StoredAttempt
    brief_reference: stored_state.ArtifactReference


@dataclass(frozen=True, slots=True)
class AcceptedDispatchReview:
    reference: stored_state.ArtifactReference
    own_publication_revision: int | None
    content: bytes


def _rediscover_dispatch_action(
    store: WorkStore,
    supplied: decision_models.DispatchAction,
    now: datetime,
) -> DispatchResult[decision_models.Action | None]:
    capability = supplied.capability
    state = store.decision_state(subject_attempt_ids=(capability.subject,))
    actions = discover_actions(
        state,
        decision_models.Role.COORDINATOR,
        lease_id=capability.lease_id,
        generation=capability.coordinator_generation,
        now=now,
    )
    if isinstance(actions, DecisionFailure):
        return DispatchFailure(actions.code, actions.message)
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
        )
    if current != supplied:
        if current.capability.expected_revision != supplied.capability.expected_revision:
            return DispatchFailure(
                DispatchRejectionCode.STALE_ACTION,
                "The work ledger changed after this dispatch action was selected.",
            )
        return DispatchFailure(
            DispatchRejectionCode.ACTION_INVALID,
            "The dispatch action does not carry exact current authority.",
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
        )
    current = _current_dispatch_action(store, action, now)
    if isinstance(current, DispatchFailure):
        return current
    attempt_id = current.capability.subject
    state = store.decision_state(subject_attempt_ids=(attempt_id,))
    attempt = next((value for value in state.lifecycle.attempts if value.attempt_id == attempt_id), None)
    if attempt is None or attempt.state != work_models.AttemptState.ACTIVE:
        return DispatchFailure(DispatchRejectionCode.ATTEMPT_NOT_ACTIVE, f"Attempt '{attempt_id}' is not active.")
    reference = next(
        (
            value
            for value in state.artifact_references
            if value.artifact_ref_id == attempt.brief_artifact_ref_id and value.kind == work_models.ArtifactKind.BRIEF
        ),
        None,
    )
    if reference is None:
        return DispatchFailure(DispatchRejectionCode.BRIEF_MISSING, "The attempt has no accepted brief artifact.")
    return SelectedDispatch(attempt, reference)


def _find_ready_review_reference(
    store: WorkStore,
    attempt_id: AttemptId,
    checkpoint_sha256: str,
) -> stored_state.ArtifactReference | None:
    key = f"{attempt_id}-brief-review-{checkpoint_sha256}"
    return store.artifact_reference(work_models.ArtifactKind.EVIDENCE, key, 1)


def find_dispatch_review(
    store: WorkStore,
    attempt_id: AttemptId,
    checkpoint_sha256: str,
) -> DispatchResult[stored_state.ArtifactReference]:
    existing = _find_ready_review_reference(store, attempt_id, checkpoint_sha256)
    if existing is None:
        return DispatchFailure(DispatchRejectionCode.REVIEW_MISSING, "The exact ready brief review is absent.")
    return existing


def publish_dispatch_review(
    store: WorkStore,
    artifacts: DispatchArtifactPort,
    attempt_id: AttemptId,
    checkpoint_sha256: str,
    candidate: bytes,
    review_id: ReviewId,
    accepted_at: datetime,
) -> DispatchResult[AcceptedDispatchReview]:
    key = f"{attempt_id}-brief-review-{checkpoint_sha256}"
    existing = _find_ready_review_reference(store, attempt_id, checkpoint_sha256)
    if existing is not None:
        existing_content = artifacts.read(existing)
        if existing_content == candidate:
            return AcceptedDispatchReview(existing, None, existing_content)
        rejected = artifacts.publish(
            NewArtifact(
                work_models.ArtifactKind.EVIDENCE,
                f"{key}-rejected-{review_id}",
                1,
                ".json",
                candidate,
            )
        )
        rejected_acceptance = store.accept_artifact_reference(
            rejected,
            accepted_at,
        )
        if isinstance(rejected_acceptance, DecisionFailure):
            return DispatchFailure(DispatchRejectionCode.STALE_ACTION, rejected_acceptance.message)
        return DispatchFailure(
            DispatchRejectionCode.REVIEW_COLLISION,
            f"Ready review already differs; later evidence is preserved at '{rejected.selector}'.",
        )
    published = artifacts.publish(NewArtifact(work_models.ArtifactKind.EVIDENCE, key, 1, ".json", candidate))
    accepted = store.accept_artifact_reference(
        published,
        accepted_at,
    )
    if isinstance(accepted, DecisionFailure):
        return DispatchFailure(DispatchRejectionCode.STALE_ACTION, accepted.message)
    return AcceptedDispatchReview(accepted, accepted.accepted_revision, candidate)


def recheck_dispatch_authority(
    store: WorkStore,
    supplied: decision_models.DispatchAction,
    own_review_publication_revision: int | None,
    now: datetime,
) -> DispatchFailure | None:
    capability = supplied.capability
    rediscovered = _rediscover_dispatch_action(store, supplied, now)
    if isinstance(rediscovered, DispatchFailure):
        return rediscovered
    current = rediscovered if isinstance(rediscovered, decision_models.DispatchAction) else None
    current_matches = current == supplied
    if current is not None and own_review_publication_revision is not None:
        current_matches = (
            capability.expected_revision == str(own_review_publication_revision - 1)
            and current.capability.expected_revision == str(own_review_publication_revision)
            and replace(
                current,
                capability=replace(current.capability, expected_revision=capability.expected_revision),
            )
            == supplied
        )
    if current_matches:
        return None
    return DispatchFailure(
        DispatchRejectionCode.ACTION_UNAVAILABLE,
        "Dispatch authority changed during prompt preparation.",
    )
