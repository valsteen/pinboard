from datetime import datetime
from typing import assert_never, overload

from pinboard.application import stored_state
from pinboard.application.artifact_publication import validate_transition_work_brief
from pinboard.application.artifacts import CheckpointArtifacts, WorkBriefIdentity
from pinboard.application.decision_projection import (
    project_decision_snapshot,
)
from pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    MutationReceipt,
    PreparationAuthorityMutation,
    ProposalCreationMutation,
)
from pinboard.application.mutations import (
    project_checkpoint_acceptance_mutation,
    project_transition_mutation,
)
from pinboard.application.ports import WorkStore
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.authority_decisions import (
    decide_attempt_authority,
    decide_preparation_authority,
)
from pinboard.domain.decisions import decide, validate_supplied_action
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import (
    ActionId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    TaskId,
)
from pinboard.domain.ledger import LedgerSnapshot
from pinboard.domain.proposal_decisions import decide_proposal_creation
from pinboard.domain.proposal_models import (
    CreateProposalOperation,
)


def _next_history_id(state: stored_state.StoredWorkState) -> HistoryId:
    return HistoryId(1 + max((int(value.history_id) for value in state.transition_receipts), default=0))


def _project_retained_attempt_authority(
    state: stored_state.StoredWorkState,
    attempt_id: AttemptId,
) -> authority_models.AttemptLeaseAuthority | None:
    retained = stored_state.retained_attempt(state, attempt_id)
    attempt = next((value for value in state.lifecycle.attempts if value.attempt_id == attempt_id), None)
    if retained is None or attempt is None:
        return None
    lease, anchor = retained
    if anchor is None:
        return None
    lease_id = anchor.lease_id
    task_id = anchor.task_id
    host_id = anchor.host_id
    return authority_models.AttemptLeaseAuthority(
        host_epoch=state.lifecycle.project.host_epoch,
        attempt=attempt_id,
        item=attempt.item_id,
        task_id=task_id,
        host_id=host_id,
        lease_id=lease_id,
        generation=lease.generation,
        acquired_at=lease.acquired_at,
        expires_at=lease.expires_at,
        state=lease.state,
    )


def decide_and_commit_attempt_authority_change(
    store: WorkStore,
    requested_change: authority_models.AttemptAuthorityOperation,
) -> DecisionResult[MutationReceipt]:
    """Reread locked state, decide, and commit one attempt-authority change."""

    match requested_change:
        case authority_models.AcquireInitialAttemptAuthority(
            attempt=attempt_id, task_id=actor_task_id, host_id=actor_host_id, acquired_at=decided_at
        ):
            history_outcome = "acquire-initial-attempt-authority"
        case authority_models.TransferAttemptAuthority(
            current=current, task_id=actor_task_id, host_id=actor_host_id, acquired_at=decided_at
        ):
            attempt_id = current.attempt
            history_outcome = "transfer-attempt-authority"
        case authority_models.RenewAttemptAuthority(current=current, renewed_at=decided_at):
            attempt_id = current.attempt
            actor_task_id, actor_host_id = current.task_id, current.host_id
            history_outcome = "renew-attempt-authority"
        case authority_models.ReleaseAttemptAuthority(current=current, released_at=decided_at):
            attempt_id = current.attempt
            actor_task_id, actor_host_id = current.task_id, current.host_id
            history_outcome = "release-attempt-authority"
        case authority_models.RevokeAttemptAuthority(
            attempt=attempt_id, task_id=actor_task_id, host_id=actor_host_id, revoked_at=decided_at
        ):
            history_outcome = "revoke-attempt-authority"
        case _ as unreachable:
            assert_never(unreachable)
    with store.write() as transaction:
        locked_state = transaction.snapshot()
        decision_context = project_decision_snapshot(locked_state, decided_at)
        generation_before = next(
            (
                value.generation_high_water
                for value in locked_state.authority.attempt_counters
                if value.attempt_id == attempt_id
            ),
            0,
        )
        decision_result = decide_attempt_authority(
            retained=_project_retained_attempt_authority(locked_state, attempt_id),
            counter=generation_before,
            operation=requested_change,
            live_attempt=(
                (attempt_id, attempt.item)
                if (attempt := decision_context.attempt(attempt_id)) is not None
                and attempt.state == work_models.AttemptState.ACTIVE
                else None
            ),
            transferable_attempt=(
                (attempt_id, attempt.item)
                if (attempt := decision_context.attempt(attempt_id)) is not None
                and attempt.state != work_models.AttemptState.DONE
                else None
            ),
            project_host_epoch=decision_context.host_epoch,
        )
        if isinstance(decision_result, DecisionFailure):
            return decision_result
        accepted_decision = decision_result
        proposed_replacement = accepted_decision.proposed_replacement
        transition_receipt = decision_models.TransitionReceipt(
            action_id=ActionId(f"continue:attempt-authority:{attempt_id}:{proposed_replacement.generation}"),
            item=proposed_replacement.item,
            outcome=history_outcome,
            evidence=None,
            decided_at=decided_at,
        )
        mutation_receipt = MutationReceipt(
            transition=transition_receipt,
            history_id=_next_history_id(locked_state),
            project_revision=locked_state.lifecycle.project.revision + 1,
            action_kind=decision_models.ActionKind.CONTINUE,
            subject_id=HistorySubjectId(attempt_id),
            artifact_ref_id=None,
            authorization=decision_models.AuthorizationKind.ATTEMPT,
            actor_task_id=actor_task_id,
            actor_host_id=actor_host_id,
            input_schema="attempt-authority/v1",
            input_payload=work_models.CanonicalJson(b"{}"),
        )
        mutation = AttemptAuthorityMutation(
            receipt=mutation_receipt,
            decision=accepted_decision,
        )
        return transaction.commit(mutation)


def _project_retained_preparation_authority(
    state: stored_state.StoredWorkState,
    item_id: ItemId,
) -> authority_models.PreparationLeaseAuthority | None:
    retained = stored_state.retained_preparation(state, item_id)
    if retained is None:
        return None
    lease, anchor = retained
    if anchor is None:
        return None
    return authority_models.PreparationLeaseAuthority(
        host_epoch=state.lifecycle.project.host_epoch,
        item=item_id,
        definition_revision=lease.definition_revision,
        definition_digest=lease.definition_digest,
        task_id=anchor.task_id,
        host_id=anchor.host_id,
        lease_id=anchor.lease_id,
        generation=lease.generation,
        acquired_at=lease.acquired_at,
        expires_at=lease.expires_at,
        state=lease.state,
    )


def decide_and_commit_preparation_authority_change(
    store: WorkStore,
    requested_change: authority_models.PreparationAuthorityOperation,
) -> DecisionResult[MutationReceipt]:
    """Reread locked state, decide, and commit one preparation-authority change."""

    match requested_change:
        case authority_models.AcquireInitialPreparationAuthority(
            item=item_id, task_id=actor_task_id, host_id=actor_host_id, acquired_at=decided_at
        ):
            history_outcome = "acquire-initial-preparation-authority"
        case authority_models.TransferPreparationAuthority(
            current=current, task_id=actor_task_id, host_id=actor_host_id, acquired_at=decided_at
        ):
            item_id = current.item
            history_outcome = "transfer-preparation-authority"
        case authority_models.RenewPreparationAuthority(current=current, renewed_at=decided_at):
            item_id = current.item
            actor_task_id, actor_host_id = current.task_id, current.host_id
            history_outcome = "renew-preparation-authority"
        case authority_models.ReleasePreparationAuthority(current=current, released_at=decided_at):
            item_id = current.item
            actor_task_id, actor_host_id = current.task_id, current.host_id
            history_outcome = "release-preparation-authority"
        case authority_models.RevokePreparationAuthority(
            item=item_id, task_id=actor_task_id, host_id=actor_host_id, revoked_at=decided_at
        ):
            history_outcome = "revoke-preparation-authority"
        case _ as unreachable:
            assert_never(unreachable)
    with store.write() as transaction:
        locked_state = transaction.snapshot()
        decision_context = project_decision_snapshot(locked_state, decided_at)
        generation_before = next(
            (
                value.generation_high_water
                for value in locked_state.authority.preparation_counters
                if value.item_id == item_id
            ),
            0,
        )
        decision_result = decide_preparation_authority(
            retained=_project_retained_preparation_authority(locked_state, item_id),
            counter=generation_before,
            operation=requested_change,
            snapshot=decision_context,
            now=decided_at,
        )
        if isinstance(decision_result, DecisionFailure):
            return decision_result
        accepted_decision = decision_result
        proposed_replacement = accepted_decision.proposed_replacement
        transition_receipt = decision_models.TransitionReceipt(
            action_id=ActionId(f"continue:preparation-authority:{item_id}:{proposed_replacement.generation}"),
            item=item_id,
            outcome=history_outcome,
            evidence=None,
            decided_at=decided_at,
        )
        mutation_receipt = MutationReceipt(
            transition=transition_receipt,
            history_id=_next_history_id(locked_state),
            project_revision=locked_state.lifecycle.project.revision + 1,
            action_kind=decision_models.ActionKind.CONTINUE,
            subject_id=HistorySubjectId(item_id),
            artifact_ref_id=None,
            authorization=decision_models.AuthorizationKind.PREPARATION,
            actor_task_id=actor_task_id,
            actor_host_id=actor_host_id,
            input_schema="preparation-authority/v1",
            input_payload=work_models.CanonicalJson(b"{}"),
        )
        mutation = PreparationAuthorityMutation(receipt=mutation_receipt, decision=accepted_decision)
        return transaction.commit(mutation)


def create_proposal(
    store: WorkStore,
    operation: CreateProposalOperation,
    now: datetime,
    *,
    actor_task_id: TaskId,
    actor_host_id: HostId,
) -> DecisionResult[MutationReceipt]:
    """Reread locked state, decide, and commit proposal facts plus their intake item."""

    with store.write() as transaction:
        locked_state = transaction.snapshot()
        project = locked_state.lifecycle.project
        decision_result = decide_proposal_creation(
            project_decision_snapshot(locked_state, now),
            operation,
        )
        if isinstance(decision_result, DecisionFailure):
            return decision_result
        accepted_decision = decision_result
        intake = accepted_decision.proposal
        transition_receipt = decision_models.TransitionReceipt(
            ActionId(f"inspect:proposal:{intake.proposal_id}"),
            None,
            "create-proposal",
            intake.urgency_evidence,
            now,
        )
        mutation_receipt = MutationReceipt(
            transition_receipt,
            _next_history_id(locked_state),
            project.revision + 1,
            decision_models.ActionKind.INSPECT,
            HistorySubjectId(intake.proposal_id),
            None,
            decision_models.AuthorizationKind.PROJECT,
            actor_task_id,
            actor_host_id,
            "proposal-intake/v1",
            work_models.CanonicalJson(b"{}"),
        )
        mutation = ProposalCreationMutation(mutation_receipt, accepted_decision)
        return transaction.commit(mutation)


def _resolve_actor_authority(
    snapshot: LedgerSnapshot,
    action: decision_models.TransitionAction,
    now: datetime,
) -> DecisionResult[decision_models.ActorAuthority]:
    capability = action.capability
    match capability.authorization:
        case decision_models.AuthorizationKind.PROJECT:
            return decision_models.ActorAuthority(decision_models.Role.PROJECT, capability.authorization, 0)
        case decision_models.AuthorizationKind.ATTEMPT:
            authority = capability.command_authority
            if (
                authority is None
                or capability.lease_id != authority.lease_id
                or authority.expires_at <= now
                or authority not in snapshot.command_attempt_authorities
            ):
                return DecisionFailure(
                    DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
                    "The supplied attempt authority is no longer current.",
                )
            return decision_models.ActorAuthority(
                decision_models.Role.WORKER,
                capability.authorization,
                authority.generation,
                capability.lease_id,
                (authority.attempt,),
                False,
            )
        case decision_models.AuthorizationKind.PREPARATION:
            authority = capability.preparation_authority
            if (
                authority is None
                or capability.lease_id != authority.lease_id
                or authority.expires_at <= now
                or authority not in snapshot.command_preparation_authorities
            ):
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    "The supplied preparation authority is no longer current.",
                )
            return decision_models.ActorAuthority(
                decision_models.Role.PREPARER,
                capability.authorization,
                authority.generation,
                capability.lease_id,
                preparations=(authority.item,),
            )
        case _ as unreachable:
            assert_never(unreachable)


@overload
def _validate_supplied_transition_and_decide(
    locked_state: stored_state.StoredWorkState,
    command: decision_models.AcceptCheckpointCommand,
    now: datetime,
    transition_brief_identity: WorkBriefIdentity | None,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
) -> DecisionResult[decision_models.CheckpointAcceptanceDecision]: ...


@overload
def _validate_supplied_transition_and_decide(
    locked_state: stored_state.StoredWorkState,
    command: decision_models.NonCheckpointTransitionCommand,
    now: datetime,
    transition_brief_identity: WorkBriefIdentity | None,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
) -> DecisionResult[decision_models.TransitionDecision]: ...


def _validate_supplied_transition_and_decide(
    locked_state: stored_state.StoredWorkState,
    command: decision_models.TransitionCommand,
    now: datetime,
    transition_brief_identity: WorkBriefIdentity | None,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
) -> DecisionResult[decision_models.Decision]:
    """Resolve supplied authority and reject stale context before deciding."""

    decision_context = project_decision_snapshot(locked_state, now)
    if command.action.capability.authorization == decision_models.AuthorizationKind.PROJECT and (
        actor_task_id is None or actor_host_id is None
    ):
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Project actions require the invoking task and host identity.",
        )
    actor_authority = _resolve_actor_authority(decision_context, command.action, now)
    if isinstance(actor_authority, DecisionFailure):
        return actor_authority
    if (failure := validate_supplied_action(decision_context, actor_authority, command.action)) is not None:
        return failure
    if (failure := validate_transition_work_brief(locked_state, command, transition_brief_identity)) is not None:
        return failure
    return decide(decision_context, command, now)


def decide_and_commit_transition(
    store: WorkStore,
    command: decision_models.NonCheckpointTransitionCommand,
    now: datetime,
    *,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
    transition_brief_identity: WorkBriefIdentity | None = None,
) -> DecisionResult[MutationReceipt]:
    """Validate, decide, and commit one lifecycle mutation under one write lock."""

    with store.write() as transaction:
        locked_state = transaction.snapshot()
        decision_result = _validate_supplied_transition_and_decide(
            locked_state, command, now, transition_brief_identity, actor_task_id, actor_host_id
        )
        if isinstance(decision_result, DecisionFailure):
            return decision_result
        accepted_decision = decision_result
        mutation = project_transition_mutation(locked_state, accepted_decision, actor_task_id, actor_host_id)
        return transaction.commit(mutation)


def decide_and_commit_checkpoint_acceptance(
    store: WorkStore,
    command: decision_models.AcceptCheckpointCommand,
    now: datetime,
    checkpoint_artifacts: CheckpointArtifacts,
    *,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
    transition_brief_identity: WorkBriefIdentity | None = None,
) -> DecisionResult[MutationReceipt]:
    """Validate, decide, and commit checkpoint acceptance with its required artifacts."""

    with store.write() as transaction:
        locked_state = transaction.snapshot()
        decision_result = _validate_supplied_transition_and_decide(
            locked_state, command, now, transition_brief_identity, actor_task_id, actor_host_id
        )
        if isinstance(decision_result, DecisionFailure):
            return decision_result
        accepted_decision = decision_result
        mutation = project_checkpoint_acceptance_mutation(
            locked_state, accepted_decision, checkpoint_artifacts, actor_task_id, actor_host_id
        )
        return transaction.commit(mutation)
