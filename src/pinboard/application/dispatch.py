from dataclasses import dataclass, replace
from datetime import datetime

from pinboard.application import query_models, stored_state
from pinboard.application.actions import discover_current_actions
from pinboard.application.artifact_publication import publish_accepted_artifact
from pinboard.application.artifacts import BriefArtifactRef, NewArtifact
from pinboard.application.dispatch_models import (
    DispatchArtifactPort,
    DispatchFailure,
    DispatchRejectionCode,
    DispatchResult,
)
from pinboard.application.ports import WorkStore
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import (
    ChangedSurface,
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
    reference: stored_state.ArtifactReference
    own_publication_revision: int | None


def _rediscover_dispatch_action(
    store: WorkStore,
    supplied: decision_models.DispatchAction,
    now: datetime,
) -> DispatchResult[decision_models.Action | None]:
    actions = discover_current_actions(
        store.read_decision_facts(query_models.DecisionScope((), (supplied.capability.subject,), (), ()), now).snapshot,
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
        if current.capability.expected_revision != supplied.capability.expected_revision:
            return DispatchFailure(
                DispatchRejectionCode.STALE_ACTION,
                "The work ledger changed after this dispatch action was selected.",
                FailureDetails(
                    observed=(),
                    mismatches=(
                        FailureMismatch(
                            "expected_revision",
                            current.capability.expected_revision,
                            supplied.capability.expected_revision,
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
    checkpoint_sha256: str,
) -> stored_state.ArtifactReference | None:
    key = f"{attempt_id}-brief-review-{checkpoint_sha256}"
    return store.read_artifact_reference(work_models.ArtifactKind.EVIDENCE, key, 1)


def find_dispatch_review(
    store: WorkStore,
    attempt_id: AttemptId,
    checkpoint_sha256: str,
) -> DispatchResult[stored_state.ArtifactReference]:
    existing = _find_ready_review_reference(store, attempt_id, checkpoint_sha256)
    if existing is None:
        return DispatchFailure(DispatchRejectionCode.REVIEW_MISSING, "The exact ready brief review is absent.", None)
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
        if artifacts.read(existing) == candidate:
            return AcceptedDispatchReview(existing, None)
        rejected_acceptance = publish_accepted_artifact(
            store,
            artifacts,
            NewArtifact(work_models.ArtifactKind.EVIDENCE, f"{key}-rejected-{review_id}", 1, ".json", candidate),
            accepted_at,
        )
        if isinstance(rejected_acceptance, DecisionFailure):
            return DispatchFailure(
                DispatchRejectionCode.STALE_ACTION,
                rejected_acceptance.message,
                rejected_acceptance.details,
            )
        rejected = rejected_acceptance.reference
        changed_surfaces = (
            *((ChangedSurface.IMMUTABLE_ARTIFACT,) if rejected_acceptance.artifact_created else ()),
            *(
                (ChangedSurface.ACCEPTED_ARTIFACT_REFERENCE, ChangedSurface.LEDGER)
                if rejected_acceptance.ledger_changed
                else ()
            ),
        )
        return DispatchFailure(
            DispatchRejectionCode.REVIEW_COLLISION,
            f"Ready review already differs; later evidence is preserved at '{rejected.selector}'.",
            FailureDetails(
                observed=(
                    FailureFact("published_artifact_selector", rejected.selector),
                    FailureFact("accepted_revision", rejected.accepted_revision),
                ),
                mismatches=(),
                retry=RetryDisposition.DO_NOT_RETRY,
                effect=EffectDisposition.COMMITTED if changed_surfaces else EffectDisposition.UNCHANGED,
                changed_surfaces=changed_surfaces,
                alternatives=(),
            ),
        )
    accepted_publication = publish_accepted_artifact(
        store,
        artifacts,
        NewArtifact(work_models.ArtifactKind.EVIDENCE, key, 1, ".json", candidate),
        accepted_at,
    )
    if isinstance(accepted_publication, DecisionFailure):
        return DispatchFailure(
            DispatchRejectionCode.STALE_ACTION,
            accepted_publication.message,
            accepted_publication.details,
        )
    accepted = accepted_publication.reference
    return AcceptedDispatchReview(accepted, accepted.accepted_revision)


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
        FailureDetails(
            observed=(FailureFact("accepted_review_publication_revision", own_review_publication_revision),),
            mismatches=(
                FailureMismatch(
                    "expected_revision",
                    None if current is None else current.capability.expected_revision,
                    capability.expected_revision,
                ),
            ),
            retry=(
                RetryDisposition.DO_NOT_RETRY
                if own_review_publication_revision is not None
                else RetryDisposition.REFRESH_ACTION
            ),
            effect=(
                EffectDisposition.COMMITTED
                if own_review_publication_revision is not None
                else EffectDisposition.UNCHANGED
            ),
            changed_surfaces=(
                (
                    ChangedSurface.IMMUTABLE_ARTIFACT,
                    ChangedSurface.ACCEPTED_ARTIFACT_REFERENCE,
                    ChangedSurface.LEDGER,
                )
                if own_review_publication_revision is not None
                else ()
            ),
            alternatives=(),
        ),
    )
