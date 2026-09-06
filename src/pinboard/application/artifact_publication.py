from datetime import datetime
from pathlib import Path
from typing import Protocol

from pinboard.application import stored_state
from pinboard.application.artifacts import ArtifactRef, NewArtifact, WorkBriefIdentity
from pinboard.application.ports import WorkStore
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult


class ArtifactPublisher(Protocol):
    def publish(self, artifact: NewArtifact) -> ArtifactRef: ...


class ArtifactContentReader(Protocol):
    def read(self, reference: stored_state.ArtifactReference) -> bytes: ...


class ArtifactReader(ArtifactContentReader, Protocol):
    def path(self, reference: stored_state.ArtifactReference) -> Path: ...


def publish_accepted_artifact(
    store: WorkStore,
    publisher: ArtifactPublisher,
    artifact: NewArtifact,
    accepted_at: datetime,
) -> DecisionResult[stored_state.ArtifactReference]:
    """Publish immutable bytes, then accept the publisher-verified reference."""

    published_reference = publisher.publish(artifact)
    return store.accept_artifact_reference(published_reference, accepted_at)


def validate_transition_work_brief(
    state: stored_state.StoredWorkState,
    command: decision_models.TransitionCommand,
    identity: WorkBriefIdentity | None,
) -> DecisionFailure | None:
    """Validate activation or resume brief identity against the locked SQLite snapshot."""

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
                )
            attempt_id = str(attempt.attempt_id)
            branch = attempt.branch
            base_revision = attempt.base_revision
        case _:
            return None
    reference = transition_work_brief_reference(state, command)
    if reference is None or reference.kind != work_models.ArtifactKind.BRIEF:
        return None
    if identity is None:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "The selected brief artifact identity was not decoded from the accepted reference.",
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
        )
    if isinstance(command, decision_models.ActivateCommand):
        preparation = command.action.capability.preparation_authority
        if preparation is None or (
            preparation.item,
            preparation.definition_revision,
            preparation.definition_digest,
        ) != (item.item_id, definition.revision, definition.digest):
            return DecisionFailure(
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                "The selected work brief does not match the live preparation pin.",
            )
    expected = WorkBriefIdentity(
        attempt_id,
        item_id,
        branch,
        base_revision,
        definition.revision,
        definition.digest,
    )
    if identity != expected:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "The selected brief artifact does not match the attempt, item, branch, base revision, and accepted scope.",
        )
    return None


def transition_work_brief_reference(
    state: stored_state.StoredWorkState,
    command: decision_models.TransitionCommand,
) -> stored_state.ArtifactReference | None:
    match command:
        case decision_models.ActivateCommand(value=value) | decision_models.ResumeCommand(value=value) if (
            value.brief_artifact_ref_id is not None
        ):
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
