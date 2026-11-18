"""Discover and describe currently legal actions from application-owned facts."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, assert_never

import msgspec

from pinboard.application import action_models, ports, query_models, work_brief_models, work_briefs
from pinboard.application.artifact_publication import ArtifactReader
from pinboard.domain import decision_models, routing, work_models
from pinboard.domain.decisions import available_actions
from pinboard.domain.errors import (
    DecisionFailure,
    DecisionFailureCode,
    DecisionResult,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    FailureMismatch,
    RetryDisposition,
)
from pinboard.domain.identifiers import ActionId, AttemptId, LeaseId, ProposalId, WorkItemId
from pinboard.domain.ledger import LedgerSnapshot


def action_subject_ids(
    action: decision_models.Action,
) -> tuple[tuple[WorkItemId, ...], tuple[AttemptId, ...], tuple[ProposalId, ...]]:
    """Return the exact persisted subject family selected by one action."""

    match action:
        case (
            decision_models.ReviewCandidateAction(capability=capability)
            | decision_models.AcceptCheckpointAction(capability=capability)
            | decision_models.AcceptReviewAndContinueAction(capability=capability)
            | decision_models.BlockAttemptAction(capability=capability)
            | decision_models.CompleteAction(capability=capability)
            | decision_models.ContinueAction(capability=capability)
            | decision_models.DispatchAction(capability=capability)
            | decision_models.PauseAction(capability=capability)
            | decision_models.RebindAttemptAction(capability=capability)
            | decision_models.RecordAmendmentAction(capability=capability)
            | decision_models.ReportBlockerAction(capability=capability)
            | decision_models.ReturnForCorrectionAction(capability=capability)
            | decision_models.SubmitReviewAction(capability=capability)
        ):
            return (), (capability.subject,), ()
        case (
            decision_models.ActivateAction(capability=capability)
            | decision_models.PrepareAction(capability=capability)
            | decision_models.BlockWorkItemAction(capability=capability)
            | decision_models.CancelAction(capability=capability)
            | decision_models.CloseAction(capability=capability)
            | decision_models.RecordReplacementAction(capability=capability)
            | decision_models.ResumeAction(capability=capability)
            | decision_models.RetainTemporarilyAction(capability=capability)
            | decision_models.ReviseWorkItemAction(capability=capability)
        ):
            return (capability.subject,), (), ()
        case (
            decision_models.MergeProposalAction(capability=capability)
            | decision_models.RejectProposalAction(capability=capability)
        ):
            return (), (), (capability.subject,)
        case (
            decision_models.InspectAction()
            | decision_models.CreateProposalAction()
            | decision_models.PreviewParallelAction()
            | decision_models.OrderAction()
            | decision_models.InspectInitiativeAction()
            | decision_models.ConsolidateAction()
        ):
            return (), (), ()
        case _ as unreachable:
            assert_never(unreachable)


def discover_current_actions(
    snapshot: LedgerSnapshot,
    role: decision_models.Role,
    *,
    lease_id: LeaseId | None = None,
    generation: int | None = None,
) -> DecisionResult[
    tuple[decision_models.Action, ...],
    Literal[DecisionFailureCode.ACTION_NOT_AVAILABLE, DecisionFailureCode.ATTEMPT_LEASE_REQUIRED],
]:
    """Discover actions from application-owned current facts."""

    selected_generation = generation if generation is not None else 0
    match role:
        case decision_models.Role.OBSERVER:
            actor = decision_models.ObserverActorAuthority()
        case decision_models.Role.PROJECT:
            actor = decision_models.ActorAuthority(
                decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0
            )
        case decision_models.Role.WORKER:
            attempts = tuple(
                authority.attempt
                for authority in snapshot.command_attempt_authorities
                if lease_id is not None
                and authority.lease_id == lease_id
                and authority.generation == selected_generation
            )
            actor = decision_models.ActorAuthority(
                decision_models.Role.WORKER,
                decision_models.AuthorizationKind.ATTEMPT,
                selected_generation,
                lease_id,
                attempts,
            )
        case decision_models.Role.PREPARER:
            preparations = tuple(
                authority.work_item_id
                for authority in snapshot.command_preparation_authorities
                if lease_id is not None
                and authority.lease_id == lease_id
                and authority.generation == selected_generation
            )
            actor = decision_models.ActorAuthority(
                decision_models.Role.PREPARER,
                decision_models.AuthorizationKind.PREPARATION,
                selected_generation,
                lease_id,
                preparations=preparations,
            )
        case _ as unreachable:
            assert_never(unreachable)
    return available_actions(snapshot, actor)


def action_identity_scope(action_id: ActionId) -> query_models.DecisionScope | None:
    """Map one known action identity to the exact persisted subject it selects."""

    if ":" not in action_id:
        return None
    kind_value, subject = action_id.split(":", 1)
    if not kind_value or not subject:
        return None
    try:
        semantics = decision_models.action_semantics(decision_models.ActionKind(kind_value))
    except ValueError:
        return None
    match semantics.subject_kind:
        case decision_models.ActionSubjectKind.ITEM:
            return query_models.DecisionScope((WorkItemId(subject),), (), (), (), (), (), (), (), (), ())
        case decision_models.ActionSubjectKind.ATTEMPT:
            return query_models.DecisionScope((), (), (), (), (), (AttemptId(subject),), (), (), (), ())
        case decision_models.ActionSubjectKind.PROPOSAL:
            return query_models.DecisionScope((), (), (), (), (), (), (ProposalId(subject),), (), (), ())
        case decision_models.ActionSubjectKind.LEDGER:
            return query_models.DecisionScope((), (), (), (), (), (), (), (), (), ())
        case _ as unreachable:
            assert_never(unreachable)


def select_current_actions(
    reader: ports.WorkStore,
    role: decision_models.Role,
    *,
    observed_at: datetime,
    lease_id: LeaseId | None,
    generation: int | None,
    action_id: ActionId | None,
) -> (
    DecisionResult[
        ActionSelection,
        Literal[DecisionFailureCode.ACTION_NOT_AVAILABLE, DecisionFailureCode.ATTEMPT_LEASE_REQUIRED],
    ]
    | routing.RouteClassificationRequired
):
    """Read the minimum current facts and select an optional exact legal action."""

    if role == decision_models.Role.OBSERVER or (
        role == decision_models.Role.PROJECT and action_id == ActionId("create-proposal:ledger")
    ):
        snapshot = LedgerSnapshot(
            "", (), accepted_checkpoints=frozenset(), dependency_facts=(), dropped_item_ids=frozenset()
        )
    elif action_id is not None:
        scope = action_identity_scope(action_id)
        if scope is None:
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_AVAILABLE,
                f"Action '{action_id}' is not currently legal for this role and lease.",
                None,
            )
        snapshot = reader.read_decision_facts(scope, observed_at).snapshot
    elif role == decision_models.Role.PROJECT:
        snapshot = reader.read_current_action_snapshot(observed_at)
    elif lease_id is not None and generation is not None:
        snapshot = reader.read_leased_action_snapshot(role, lease_id, generation, observed_at)
    else:
        snapshot = LedgerSnapshot(
            "", (), accepted_checkpoints=frozenset(), dependency_facts=(), dropped_item_ids=frozenset()
        )
    discovered = discover_current_actions(snapshot, role, lease_id=lease_id, generation=generation)
    if isinstance(discovered, DecisionFailure):
        return discovered
    if action_id is None:
        return ActionSelection(
            tuple(action for action in discovered if routing.validate_action_route(snapshot, action) is None), snapshot
        )
    selected = tuple(action for action in discovered if decision_models.action_id(action) == action_id)
    if selected:
        if (refused := routing.validate_action_route(snapshot, selected[0])) is not None:
            return refused
        return ActionSelection(selected, snapshot)
    return DecisionFailure(
        DecisionFailureCode.ACTION_NOT_AVAILABLE,
        f"Action '{action_id}' is not currently legal for this role and lease.",
        None,
    )


@dataclass(frozen=True, slots=True)
class ActionSelection:
    actions: tuple[decision_models.Action, ...]
    snapshot: LedgerSnapshot


def select_exact_project_action[ActionT: decision_models.Action](
    reader: ports.WorkStore,
    kind: type[ActionT],
    action_id: ActionId,
    observed_at: datetime,
) -> (
    DecisionResult[
        ActionT,
        Literal[DecisionFailureCode.ACTION_NOT_AVAILABLE, DecisionFailureCode.ATTEMPT_LEASE_REQUIRED],
    ]
    | routing.RouteClassificationRequired
):
    """Select the current legal project action of the caller's exact kind that the action identity names.

    The identity names its kind, so no current legal action of that kind with that identity is the same
    unavailable outcome as no current legal action with that identity.
    """

    selected = select_current_actions(
        reader,
        decision_models.Role.PROJECT,
        observed_at=observed_at,
        lease_id=None,
        generation=None,
        action_id=action_id,
    )
    if isinstance(selected, routing.RouteClassificationRequired):
        return selected
    if isinstance(selected, DecisionFailure):
        return selected
    for action in selected.actions:
        if isinstance(action, kind):
            return action
    return DecisionFailure(
        DecisionFailureCode.ACTION_NOT_AVAILABLE,
        f"Action '{action_id}' is not currently legal for this role and lease.",
        None,
    )


def completion_candidate_recovery(
    selected: query_models.CompletionContextFacts,
) -> query_models.CompletionCandidateRequired | None:
    """Name missing current candidate evidence without starting delivery."""
    attempt = selected.attempt
    if (
        isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        and attempt.state == work_models.AttemptState.REVIEW
        and attempt.candidate_revision is not None
    ):
        return None
    return query_models.CompletionCandidateRequired(attempt.attempt_id)


@dataclass(frozen=True, slots=True)
class AcceptedEvidenceDamaged:
    """Accepted evidence a focused action projects is damaged, so no unchanged retry can succeed."""

    message: str
    observed: tuple[FailureFact, ...]
    mismatches: tuple[FailureMismatch, ...]


def _completion_context_changed(
    attempt_id: AttemptId, message: str
) -> DecisionFailure[Literal[DecisionFailureCode.ACTION_NOT_AVAILABLE]]:
    return DecisionFailure(
        DecisionFailureCode.ACTION_NOT_AVAILABLE,
        message,
        FailureDetails(
            observed=(FailureFact("attempt_id", str(attempt_id)),),
            mismatches=(),
            retry=RetryDisposition.REFRESH_ACTION,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def completion_input_contract(
    reader: ports.WorkStore,
    artifacts: ArtifactReader,
    action: decision_models.CompleteAction,
    semantics: action_models.ActionSemanticsView,
) -> (
    DecisionResult[action_models.CompletionInputContractView, Literal[DecisionFailureCode.ACTION_NOT_AVAILABLE]]
    | AcceptedEvidenceDamaged
    | query_models.CompletionCandidateRequired
    | query_models.CompletionRecoveryRequired
):
    """Read only this attempt's final completion evidence and exact payload leaf.

    A completion context that changed since discovery is unavailable until it is read again; a missing accepted
    package reference is damaged stored evidence that no fresh read repairs.
    """
    completion = reader.read_completion_context(action.capability.subject)
    if completion is None:
        return _completion_context_changed(
            action.capability.subject, "Completion attempt disappeared; reinspect the focused action."
        )
    attempt = completion.attempt
    if not isinstance(attempt, query_models.NonterminalAttemptContextFacts):
        return _completion_context_changed(
            action.capability.subject, "Completion requires a current nonterminal attempt."
        )
    brief = work_briefs.decode_canonical_accepted_brief(artifacts.read(attempt.brief_reference))
    if isinstance(brief, work_brief_models.WorkBriefFailure):
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Completion cannot read the accepted current brief: {brief.message} Diagnose that artifact before retrying.",
            FailureDetails(
                observed=(FailureFact("accepted_brief_artifact_ref_id", int(attempt.brief_artifact_ref_id)),),
                mismatches=(),
                retry=RetryDisposition.REFRESH_ACTION,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    if isinstance(
        disposition := work_briefs.accepted_brief_disposition(brief), work_brief_models.ContinueCheckpointDisposition
    ):
        return query_models.CompletionRecoveryRequired(
            attempt.attempt_id,
            "The accepted brief marks this checkpoint as continuing work, so it cannot authorize completion. "
            f"Remaining work:\n{disposition.remaining_work}",
        )
    if (recovery := completion_candidate_recovery(completion)) is not None:
        return recovery
    packages: list[action_models.CompletionPackageView] = []
    for checkpoint in completion.checkpoints:
        reference = checkpoint.package_reference
        if reference is None:
            return AcceptedEvidenceDamaged(
                "An accepted checkpoint package reference is missing. Report this accepted evidence defect to the "
                "human; do not retry unchanged or reconstruct the package.",
                (
                    FailureFact("attempt_id", str(attempt.attempt_id)),
                    FailureFact("checkpoint_history_id", int(checkpoint.receipt.history_id)),
                ),
                (FailureMismatch("checkpoint_package_reference", "accepted package reference", None),),
            )
        packages.append(
            action_models.CompletionPackageView(
                int(checkpoint.receipt.history_id),
                reference.content_sha256,
                int(reference.artifact_ref_id),
                reference.selector,
                reference.size_bytes,
            )
        )
    model = action_models.ReviewedCompleteInputPayload
    candidate = attempt.candidate_revision
    return action_models.CompletionInputContractView(
        action.kind, semantics, msgspec.json.schema(model), candidate, tuple(packages)
    )


def encoded_action_input_schema(kind: decision_models.ActionKind) -> bytes | None:
    """Return the canonical strict payload schema, or no generic transition payload for a separately owned operation."""

    schema = action_models.action_payload_schema(kind)
    return None if schema is None else msgspec.json.encode(schema, order="sorted")


def project_action_semantics(semantics: decision_models.ActionSemantics) -> action_models.ActionSemanticsView:
    return action_models.ActionSemanticsView(
        semantics.use_case,
        semantics.lifecycle_effect,
        semantics.permitted_roles,
        semantics.subject_kind,
        semantics.lifecycle_precondition,
        semantics.practical_result,
    )


def project_action(
    action: decision_models.Action, *, include_input_contract: bool
) -> action_models.ProjectedActionView:
    """Project one action and its shared payload contract without transport concerns."""

    capability = action.capability
    semantics = project_action_semantics(decision_models.action_semantics(action.kind))
    match capability.authorization:
        case None:
            authorization: action_models.ActionAuthorization = "observer"
        case decision_models.AuthorizationKind.PROJECT:
            authorization = "project"
        case decision_models.AuthorizationKind.ATTEMPT:
            authorization = "attempt"
        case decision_models.AuthorizationKind.PREPARATION:
            authorization = "preparation"
        case _ as unreachable:
            assert_never(unreachable)
    common = (
        action.kind,
        capability.subject,
        capability.label,
        capability.subject_revision if isinstance(capability, decision_models.MutationActionCapability) else None,
        authorization,
        capability.lease_id,
        capability.command_authority.generation
        if capability.command_authority is not None
        else capability.preparation_authority.generation
        if capability.preparation_authority is not None
        else None,
        semantics,
    )
    if not include_input_contract:
        return action_models.ActionSummaryView(*common, None)
    return action_models.ActionView(
        *common,
        action_models.InputContractView(
            action.kind,
            semantics,
            msgspec.UNSET
            if (schema := encoded_action_input_schema(action.kind)) is None
            else msgspec.json.decode(schema, type=action_models.JsonSchema),
        ),
    )
