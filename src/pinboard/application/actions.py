"""Discover and describe currently legal actions from application-owned facts."""

from datetime import datetime
from typing import assert_never

import msgspec

from pinboard.application import action_models, ports, query_models
from pinboard.domain import decision_models, work_models
from pinboard.domain.decisions import available_actions
from pinboard.domain.errors import (
    DecisionFailure,
    DecisionFailureCode,
    DecisionResult,
)
from pinboard.domain.identifiers import ActionId, AttemptId, ItemId, LeaseId, ProposalId
from pinboard.domain.ledger import LedgerSnapshot


def action_subject_ids(
    action: decision_models.Action,
) -> tuple[tuple[ItemId, ...], tuple[AttemptId, ...], tuple[ProposalId, ...]]:
    """Return the exact persisted subject family selected by one action."""

    match action:
        case (
            decision_models.AcceptCheckpointAction(capability=capability)
            | decision_models.AcceptReviewAndContinueAction(capability=capability)
            | decision_models.BlockAttemptAction(capability=capability)
            | decision_models.CompleteAction(capability=capability)
            | decision_models.ContinueAction(capability=capability)
            | decision_models.DispatchAction(capability=capability)
            | decision_models.PauseAction(capability=capability)
            | decision_models.RebindAttemptAction(capability=capability)
            | decision_models.ReportBlockerAction(capability=capability)
            | decision_models.ReturnForCorrectionAction(capability=capability)
            | decision_models.SubmitReviewAction(capability=capability)
        ):
            return (), (capability.subject,), ()
        case (
            decision_models.ActivateAction(capability=capability)
            | decision_models.BlockItemAction(capability=capability)
            | decision_models.CloseAction(capability=capability)
            | decision_models.DeferAction(capability=capability)
            | decision_models.MarkReadyAction(capability=capability)
            | decision_models.ReopenAction(capability=capability)
            | decision_models.RecordReplacementAction(capability=capability)
            | decision_models.ResumeAction(capability=capability)
            | decision_models.RetainTemporarilyAction(capability=capability)
            | decision_models.ReviseItemAction(capability=capability)
        ):
            return (capability.subject,), (), ()
        case (
            decision_models.AcceptProposalAction(capability=capability)
            | decision_models.MergeProposalAction(capability=capability)
            | decision_models.RejectProposalAction(capability=capability)
            | decision_models.ReturnProposalAction(capability=capability)
        ):
            return (), (), (capability.subject,)
        case decision_models.InspectAction():
            return (), (), ()
        case _ as unreachable:
            assert_never(unreachable)


def discover_current_actions(
    snapshot: LedgerSnapshot,
    role: decision_models.Role,
    *,
    lease_id: LeaseId | None = None,
    generation: int | None = None,
) -> DecisionResult[tuple[decision_models.Action, ...]]:
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
                authority.item
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
            return query_models.DecisionScope((ItemId(subject),), (), (), (), (), (), (), ())
        case decision_models.ActionSubjectKind.ATTEMPT:
            return query_models.DecisionScope((), (), (), (), (AttemptId(subject),), (), (), ())
        case decision_models.ActionSubjectKind.PROPOSAL:
            return query_models.DecisionScope((), (), (), (), (), (ProposalId(subject),), (), ())
        case decision_models.ActionSubjectKind.LEDGER:
            return query_models.DecisionScope((), (), (), (), (), (), (), ())
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
) -> DecisionResult[tuple[decision_models.Action, ...]]:
    """Read the minimum current facts and select an optional exact legal action."""

    if role == decision_models.Role.OBSERVER:
        snapshot = LedgerSnapshot("", ())
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
        snapshot = LedgerSnapshot("", ())
    discovered = discover_current_actions(snapshot, role, lease_id=lease_id, generation=generation)
    if isinstance(discovered, DecisionFailure) or action_id is None:
        return discovered
    selected = tuple(action for action in discovered if decision_models.action_id(action) == action_id)
    if selected:
        return selected
    return DecisionFailure(
        DecisionFailureCode.ACTION_NOT_AVAILABLE,
        f"Action '{action_id}' is not currently legal for this role and lease.",
        None,
    )


def completion_candidate_recovery(
    selected: query_models.CompletionContextFacts,
) -> query_models.CompletionCandidateRequired | None:
    """Withhold checkpointed completion until the existing submission route protects a candidate."""
    attempt = selected.attempt
    if not selected.checkpoints or (
        isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        and attempt.state == work_models.AttemptState.REVIEW
        and attempt.candidate_revision is not None
    ):
        return None
    return query_models.CompletionCandidateRequired(attempt.attempt_id)


def completion_input_contract(
    reader: ports.WorkStore,
    action: decision_models.CompleteAction,
    semantics: action_models.ActionSemanticsView,
) -> DecisionResult[action_models.CompletionInputContractView] | query_models.CompletionCandidateRequired:
    """Read only this attempt's final completion evidence and exact payload leaf."""
    completion = reader.read_completion_context(action.capability.subject)
    if completion is None:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Completion attempt disappeared; reinspect the focused action.",
            None,
        )
    if (recovery := completion_candidate_recovery(completion)) is not None:
        return recovery
    packages: list[action_models.CompletionPackageView] = []
    for checkpoint in completion.checkpoints:
        reference = checkpoint.package_reference
        if reference is None:
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_AVAILABLE, "An accepted checkpoint package reference is missing.", None
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
    model = action_models.CoveredCompleteInputPayload if packages else action_models.EvidenceInputPayload
    attempt = completion.attempt
    candidate = attempt.candidate_revision if isinstance(attempt, query_models.NonterminalAttemptContextFacts) else None
    return action_models.CompletionInputContractView(
        action.kind, semantics, msgspec.json.schema(model), candidate, tuple(packages)
    )


def encoded_action_input_schema(kind: decision_models.ActionKind) -> bytes | None:
    """Return the canonical strict payload schema, or no payload for advisory actions."""

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
        decision_models.action_id(action),
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
            None
            if (schema := encoded_action_input_schema(action.kind)) is None
            else msgspec.json.decode(schema, type=action_models.JsonSchema),
        ),
    )
