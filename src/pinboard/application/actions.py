"""Discover currently legal actions from application-owned decision facts."""

from typing import assert_never

from pinboard.domain import decision_models
from pinboard.domain.decisions import available_actions
from pinboard.domain.errors import DecisionResult
from pinboard.domain.identifiers import AttemptId, ItemId, LeaseId, ProposalId
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
            | decision_models.ResumeAction(capability=capability)
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
