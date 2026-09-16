"""Compose shared lifecycle selection with checkout and artifact adapters."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import assert_never

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.brief_sources import select_checkout_brief_source
from pinboard.adapters.files.root import observe_checkout_identity
from pinboard.adapters.transition_input import ParsedTransitionInput, TransitionInputFailure, parse_transition_input
from pinboard.application import action_models, actions, ports, service, work_brief_models
from pinboard.application.artifacts import WorkBriefIdentity
from pinboard.application.mutation_models import CommittedEffect
from pinboard.application.work_briefs import (
    decode_canonical_work_brief,
    read_selected_work_brief_identity,
    validate_reviewed_authority_digests,
)
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import (
    DecisionFailure,
    DecisionFailureCode,
    DecisionResult,
    EffectDisposition,
    FailureDetails,
    FailureMismatch,
    RetryDisposition,
)
from pinboard.domain.identifiers import ActionId, ArtifactRefId, AttemptId, HostId, LeaseId, TaskId


@dataclass(frozen=True, slots=True)
class TransitionReceipt:
    role: decision_models.MutationRole
    action_id: ActionId
    subject_revision: str
    lease_id: LeaseId | None
    generation: int
    actor_task_id: TaskId | None
    actor_host_id: HostId | None


@dataclass(frozen=True, slots=True)
class SelectedTransition:
    action: decision_models.Action
    command: decision_models.TransitionCommand
    actor_task_id: TaskId | None
    actor_host_id: HostId | None


def _input_failure(message: str, mismatches: tuple[FailureMismatch, ...]) -> DecisionFailure:
    return DecisionFailure(
        DecisionFailureCode.TRANSITION_INPUT_INVALID,
        message,
        FailureDetails(
            observed=(),
            mismatches=mismatches,
            retry=RetryDisposition.CORRECT_INPUT,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def resolve_activation(
    source_checkout: Path,
    store: ports.WorkStore,
    artifacts: ArtifactRepository,
    action: decision_models.Action,
    decoded: action_models.ActivateInputPayload,
) -> DecisionResult[decision_models.ActivateCommand]:
    if not isinstance(action, decision_models.ActivateAction):
        raise AssertionError("Activate input requires an activate action.")
    artifact_ref_id = ArtifactRefId(decoded.brief_artifact_ref_id)
    reference = store.read_artifact_reference_by_id(artifact_ref_id)
    if reference is None or reference.kind != work_models.ArtifactKind.BRIEF:
        return _input_failure("Activation requires one existing brief artifact reference.", ())
    brief = decode_canonical_work_brief(artifacts.read(reference))
    if isinstance(brief, work_brief_models.WorkBriefFailure):
        return _input_failure(f"The selected brief artifact is invalid: {brief.message}", ())
    preparation = action.capability.preparation_authority
    if preparation is None:
        return _input_failure("Activation requires exact live preparation authority.", ())
    identity_mismatches = tuple(
        mismatch
        for mismatch in (
            FailureMismatch("item_id", str(action.capability.subject), brief.item_id),
            FailureMismatch("owner_task_id", str(preparation.task_id), brief.owner_task_id),
        )
        if mismatch.expected != mismatch.observed
    )
    if identity_mismatches:
        return _input_failure(
            "The selected brief item and owner must match the activate action and preparer.",
            identity_mismatches,
        )
    branch, revision = observe_checkout_identity(source_checkout)
    checkout_mismatches = tuple(
        mismatch
        for mismatch in (
            FailureMismatch("branch", brief.branch, branch),
            FailureMismatch("base_revision", brief.base_revision, revision),
        )
        if mismatch.expected != mismatch.observed
    )
    if checkout_mismatches:
        return _input_failure(
            "The selected source checkout does not match the accepted brief branch and base revision.",
            checkout_mismatches,
        )
    if isinstance(brief.checkpoint, work_brief_models.CrossBoundaryCheckpoint):
        authority_failure = validate_reviewed_authority_digests(
            partial(select_checkout_brief_source, source_checkout),
            brief.checkpoint.reviewed_authorities,
        )
        match authority_failure:
            case None:
                pass
            case work_brief_models.ReviewedAuthoritySelectionFailure(authority_id=authority_id, reason=reason):
                return _input_failure(f"Cannot read reviewed authority '{authority_id}': {reason}", ())
            case work_brief_models.ReviewedAuthorityDigestMismatch(authority_id=authority_id):
                return _input_failure(f"Reviewed authority '{authority_id}' changed after review.", ())
            case _ as unreachable:
                assert_never(unreachable)
    return decision_models.ActivateCommand(
        action,
        work_models.ActivateInput(
            AttemptId(brief.attempt_id),
            brief.branch,
            brief.base_revision,
            brief.owner_task_id,
            artifact_ref_id,
        ),
    )


def select_transition(
    source_checkout: Path,
    store: ports.WorkStore,
    artifacts: ArtifactRepository,
    receipt: TransitionReceipt,
    payload: bytes | action_models.InputPayload,
    observed_at: datetime,
) -> DecisionResult[SelectedTransition]:
    """Select one exact current receipt and decode its exact leaf payload."""

    selected = actions.select_current_actions(
        store,
        receipt.role,
        observed_at=observed_at,
        lease_id=receipt.lease_id,
        generation=receipt.generation,
        action_id=receipt.action_id,
    )
    if isinstance(selected, DecisionFailure):
        return selected
    action = selected[0]
    capability = action.capability
    if not isinstance(capability, decision_models.MutationActionCapability):
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_MUTATING,
            f"Action '{receipt.action_id}' is not a canonical transition.",
            None,
        )
    if capability.subject_revision != receipt.subject_revision:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "The action subject changed after this action was selected.",
            FailureDetails(
                observed=(),
                mismatches=(
                    FailureMismatch("subject_revision", capability.subject_revision, receipt.subject_revision),
                ),
                retry=RetryDisposition.REFRESH_ACTION,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    decoded: ParsedTransitionInput | TransitionInputFailure = parse_transition_input(action, payload)
    if isinstance(decoded, TransitionInputFailure):
        return DecisionFailure(decoded.code, decoded.message, decoded.details)
    command: decision_models.TransitionCommand | DecisionFailure
    if isinstance(decoded, action_models.ActivateInputPayload):
        command = resolve_activation(source_checkout, store, artifacts, action, decoded)
    else:
        command = decoded
    if isinstance(command, DecisionFailure):
        return command
    return SelectedTransition(action, command, receipt.actor_task_id, receipt.actor_host_id)


def transition_brief_identity(
    store: ports.WorkStore,
    command: decision_models.TransitionCommand,
    artifacts: ArtifactRepository,
) -> DecisionResult[WorkBriefIdentity | None]:
    match command:
        case (
            decision_models.ActivateCommand(value=value)
            | decision_models.ResumeCommand(value=value)
            | decision_models.RebindAttemptCommand(value=value)
        ) if value.brief_artifact_ref_id is not None:
            reference = store.read_artifact_reference_by_id(value.brief_artifact_ref_id)
        case _:
            reference = None
    return read_selected_work_brief_identity(reference, artifacts)


def commit_direct_transition(
    store: ports.WorkStore,
    artifacts: ArtifactRepository,
    selected: SelectedTransition,
    operation_time: datetime,
    read_authorization_time: Callable[[], datetime],
) -> DecisionResult[CommittedEffect]:
    """Commit a transition whose contract has no candidate or evidence publication phase."""

    command = selected.command
    if isinstance(
        command,
        (
            decision_models.AcceptCheckpointCommand,
            decision_models.CoveredCompleteCommand,
            decision_models.SubmitReviewCommand,
        ),
    ):
        raise ValueError("Artifact-sensitive transitions require the shared artifact execution path.")
    brief_identity = transition_brief_identity(store, command, artifacts)
    if isinstance(brief_identity, DecisionFailure):
        return brief_identity
    return service.decide_and_commit_transition(
        store,
        command,
        operation_time,
        read_authorization_time=read_authorization_time,
        actor_task_id=selected.actor_task_id,
        actor_host_id=selected.actor_host_id,
        transition_brief_identity=brief_identity,
    )
