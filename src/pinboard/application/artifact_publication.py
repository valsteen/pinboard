from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pinboard.application import stored_state
from pinboard.application.artifacts import ArtifactRef, NewArtifact, WorkBriefIdentity
from pinboard.application.ports import WorkStore
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import (
    ArtifactAcceptanceAfterPublicationError,
    ChangedSurface,
    DecisionFailure,
    DecisionFailureCode,
    DecisionResult,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    FailureMismatch,
    RetryDisposition,
)


class ArtifactPublisher(Protocol):
    @property
    def work_root(self) -> Path: ...

    def publish(self, artifact: NewArtifact) -> ArtifactRef: ...

    def revision_exists(self, artifact: NewArtifact) -> bool: ...


class ArtifactReader(Protocol):
    def verify(self, reference: stored_state.ArtifactReference) -> None: ...

    def path(self, reference: stored_state.ArtifactReference) -> Path: ...


@dataclass(frozen=True, slots=True)
class AcceptedArtifactPublication:
    reference: stored_state.ArtifactReference
    artifact_created: bool


def _committed_artifact_details(
    reference: ArtifactRef,
    prior: FailureDetails | None,
) -> FailureDetails:
    return FailureDetails(
        observed=(
            FailureFact("published_artifact_selector", reference.selector),
            *(() if prior is None else prior.observed),
        ),
        mismatches=() if prior is None else prior.mismatches,
        retry=RetryDisposition.DO_NOT_RETRY,
        effect=EffectDisposition.COMMITTED,
        changed_surfaces=(ChangedSurface.IMMUTABLE_ARTIFACT,),
        alternatives=(),
    )


def publish_accepted_artifact(
    store: WorkStore,
    publisher: ArtifactPublisher,
    artifact: NewArtifact,
    accepted_at: datetime,
) -> DecisionResult[AcceptedArtifactPublication]:
    """Publish immutable bytes, then accept their verified reference in SQLite."""

    artifact_existed = publisher.revision_exists(artifact)
    published_reference = publisher.publish(artifact)
    artifact_created = not artifact_existed
    try:
        accepted = store.accept_artifact_reference(publisher.work_root, published_reference, accepted_at)
    except Exception as error:
        if artifact_created:
            raise ArtifactAcceptanceAfterPublicationError(published_reference.selector, error) from error
        raise
    if isinstance(accepted, DecisionFailure):
        if artifact_created:
            return DecisionFailure(
                accepted.code,
                accepted.message,
                _committed_artifact_details(published_reference, accepted.details),
            )
        return accepted
    return AcceptedArtifactPublication(accepted, artifact_created)


def validate_transition_work_brief(  # noqa: C901, PLR0912
    state: stored_state.StoredWorkState,
    command: decision_models.TransitionCommand,
    identity: WorkBriefIdentity | None,
) -> DecisionFailure | None:
    """Validate activation, resume, or rebind brief identity against the locked SQLite snapshot."""

    match command:
        case decision_models.ActivateCommand(action=action, value=value):
            attempt_id = str(value.attempt)
            item_id = str(action.capability.subject)
            branch = value.branch
            base_revision = value.base_revision
        case decision_models.ResumeCommand(action=action, value=value) if value.brief_artifact_ref_id is not None:
            item_id = str(action.capability.subject)
            attempt = next(
                (candidate for candidate in state.lifecycle.attempts if str(candidate.item_id) == item_id), None
            )
            if attempt is None:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Resuming with a revised brief requires an existing attempt.",
                    None,
                )
            attempt_id = str(attempt.attempt_id)
            branch = attempt.branch
            base_revision = attempt.base_revision
        case decision_models.RebindAttemptCommand(action=action, value=value):
            attempt_id = str(action.capability.subject)
            attempt = next(
                (
                    candidate
                    for candidate in state.lifecycle.attempts
                    if candidate.attempt_id == action.capability.subject
                ),
                None,
            )
            if attempt is None:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Rebinding requires an existing attempt.",
                    None,
                )
            item_id = str(attempt.item_id)
            branch = value.branch
            base_revision = value.base_revision
        case _:
            return None
    reference = transition_work_brief_reference(state, command)
    if reference is None or reference.kind != work_models.ArtifactKind.BRIEF:
        return None
    if identity is None:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "The selected brief artifact identity was not decoded from the accepted reference.",
            FailureDetails(
                observed=(FailureFact("brief_identity", None),),
                mismatches=(FailureMismatch("brief_identity", "decoded", None),),
                retry=RetryDisposition.CORRECT_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    item = next((candidate for candidate in state.lifecycle.work_items if str(candidate.item_id) == item_id), None)
    if item is None:
        return None
    definition = next(
        (value for value in reversed(state.lifecycle.definition_revisions) if value.item_id == item.item_id),
        None,
    )
    if definition is None:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            "The selected work item has no current definition.",
            None,
        )
    if isinstance(command, decision_models.ActivateCommand):
        preparation = command.action.capability.preparation_authority
        preparation_mismatches = (
            (FailureMismatch("preparation_item", str(item.item_id), None),)
            if preparation is None
            else tuple(
                mismatch
                for mismatch in (
                    FailureMismatch("preparation_item", str(item.item_id), str(preparation.item)),
                    FailureMismatch(
                        "preparation_definition_revision",
                        definition.revision,
                        preparation.definition_revision,
                    ),
                    FailureMismatch(
                        "preparation_definition_digest",
                        definition.digest,
                        preparation.definition_digest,
                    ),
                )
                if mismatch.expected != mismatch.observed
            )
        )
        if preparation_mismatches:
            return DecisionFailure(
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                "The selected work brief does not match the live preparation pin.",
                FailureDetails(
                    observed=(),
                    mismatches=preparation_mismatches,
                    retry=RetryDisposition.REFRESH_ACTION,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
            )
    expected = WorkBriefIdentity(
        attempt_id,
        item_id,
        branch,
        base_revision,
        definition.revision,
        definition.digest,
    )
    identity_mismatches = tuple(
        mismatch
        for mismatch in (
            FailureMismatch("attempt_id", expected.attempt_id, identity.attempt_id),
            FailureMismatch("item_id", expected.item_id, identity.item_id),
            FailureMismatch("branch", expected.branch, identity.branch),
            FailureMismatch("base_revision", expected.base_revision, identity.base_revision),
            FailureMismatch(
                "accepted_scope_revision",
                expected.accepted_scope_revision,
                identity.accepted_scope_revision,
            ),
            FailureMismatch(
                "accepted_scope_digest",
                expected.accepted_scope_digest,
                identity.accepted_scope_digest,
            ),
        )
        if mismatch.expected != mismatch.observed
    )
    if identity_mismatches:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "The selected brief artifact does not match the attempt, item, branch, base revision, and accepted scope.",
            FailureDetails(
                observed=(),
                mismatches=identity_mismatches,
                retry=RetryDisposition.CORRECT_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    return None


def transition_work_brief_reference(
    state: stored_state.StoredWorkState,
    command: decision_models.TransitionCommand,
) -> stored_state.ArtifactReference | None:
    match command:
        case (
            decision_models.ActivateCommand(value=value)
            | decision_models.ResumeCommand(value=value)
            | decision_models.RebindAttemptCommand(value=value)
        ) if value.brief_artifact_ref_id is not None:
            artifact_ref_id = value.brief_artifact_ref_id
        case _:
            return None
    return next(
        (
            candidate
            for candidate in state.artifact_references
            if candidate.artifact_ref_id == artifact_ref_id and candidate.kind == work_models.ArtifactKind.BRIEF
        ),
        None,
    )
