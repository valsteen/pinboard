from collections.abc import Callable
from datetime import datetime
from typing import Literal, assert_never, overload

import msgspec

from pinboard.application import obligation_coverage, query_models, stored_state, unblockers
from pinboard.application.actions import action_subject_ids
from pinboard.application.artifact_publication import validate_transition_work_brief
from pinboard.application.artifacts import (
    CheckpointArtifacts,
    CompletionArtifacts,
    CurrentAttemptWorkBriefIdentity,
    EvidenceArtifactRef,
    WorkBriefIdentity,
)
from pinboard.application.dependency_authoring import (
    DependencyCheckpointPin,
    DependencyPinFailureCode,
    validate_dependency_brief_pins,
)
from pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    AttemptAuthorityMutationResult,
    CommittedEffect,
    MutationReceipt,
    OrderMutation,
    PreparationAuthorityMutation,
    PreparationAuthorityMutationResult,
    PreparationStart,
    ProposalCreationMutation,
    ProposalCreationResult,
)
from pinboard.application.mutations import (
    project_checkpoint_acceptance_mutation,
    project_completion_acceptance_mutation,
    project_review_submission_mutation,
    project_transition_mutation,
)
from pinboard.application.ports import StaleWriteCode, WorkStore, WorkTransaction
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.authority_decisions import (
    decide_attempt_authority,
    decide_preparation_authority,
)
from pinboard.domain.decisions import (
    TransitionDecisionCode,
    decide,
    validate_checkpoint_candidate,
    validate_supplied_action,
)
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
from pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HistorySubjectId,
    HostId,
    LeaseId,
    TaskId,
    WorkItemId,
)
from pinboard.domain.ledger import LedgerSnapshot
from pinboard.domain.ordering import OrderDecisionCode, OrderRequest, decide_order
from pinboard.domain.proposal_decisions import ProposalCreationDecisionCode, decide_proposal_creation
from pinboard.domain.proposal_models import (
    CreateProposalOperation,
)

PROPOSAL_INTAKE_RECEIPT_MEANING = (
    "A proposal intake committed an inspect-kind receipt; ordinary inspect discovery remains advisory."
)
LIVE_ORDER_RECEIPT_ACTION_ID: ActionId = ActionId("inspect:live-order")
LIVE_ORDER_RECEIPT_MEANING = "A live-order change committed a receipt under the inspect action family."


# Supplied-action validation adds lease and route refusals to the transition decision itself.
type TransitionFailureCode = (
    TransitionDecisionCode
    | Literal[DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, DecisionFailureCode.ROUTE_CLASSIFICATION_REQUIRED]
)
type CoveredCompletionFailureCode = Literal[
    DecisionFailureCode.ACTION_NOT_AVAILABLE,
    DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
    DecisionFailureCode.ATTEMPT_LEASE_REQUIRED,
    DecisionFailureCode.ATTEMPT_NOT_FOUND,
    DecisionFailureCode.HISTORY_RECORD_EXISTS,
    DecisionFailureCode.ITEM_DEFINITION_INVALID,
    DecisionFailureCode.ITEM_DEFINITION_STALE,
    DecisionFailureCode.ROUTE_CLASSIFICATION_REQUIRED,
    DecisionFailureCode.REPLACEMENT_STALE,
    DecisionFailureCode.TRANSITION_INPUT_INVALID,
]
type CheckpointAcceptanceFailureCode = Literal[
    DecisionFailureCode.ACTION_NOT_AVAILABLE,
    DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
    DecisionFailureCode.ATTEMPT_LEASE_REQUIRED,
    DecisionFailureCode.ATTEMPT_NOT_FOUND,
    DecisionFailureCode.ITEM_DEFINITION_INVALID,
    DecisionFailureCode.ITEM_DEFINITION_STALE,
    DecisionFailureCode.ROUTE_CLASSIFICATION_REQUIRED,
    DecisionFailureCode.TRANSITION_INPUT_INVALID,
]
type AttemptAuthorityChangeCode = Literal[
    DecisionFailureCode.ACTION_NOT_AVAILABLE,
    DecisionFailureCode.ATTEMPT_LEASE_REQUIRED,
    DecisionFailureCode.ATTEMPT_LEASE_EXPIRED,
    DecisionFailureCode.ITEM_DEFINITION_INVALID,
    DecisionFailureCode.ITEM_DEFINITION_STALE,
    DecisionFailureCode.LEASE_FENCED,
    DecisionFailureCode.TRANSITION_INPUT_INVALID,
]
type AttemptAuthorityAcquireCode = AttemptAuthorityChangeCode | Literal[DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED]
type PreparationAuthorityChangeCode = Literal[
    DecisionFailureCode.ACTION_NOT_AVAILABLE,
    DecisionFailureCode.ITEM_DEFINITION_INVALID,
    DecisionFailureCode.ITEM_DEFINITION_STALE,
    DecisionFailureCode.ROUTE_CLASSIFICATION_REQUIRED,
    DecisionFailureCode.LEASE_FENCED,
    DecisionFailureCode.TRANSITION_INPUT_INVALID,
]


def _project_retained_attempt_authority(
    snapshot: LedgerSnapshot,
    retained: query_models.AttemptAuthorityStatus | None,
    attempt_id: AttemptId,
) -> authority_models.AttemptLeaseAuthority | None:
    attempt = snapshot.attempt(attempt_id)
    if retained is None or attempt is None:
        return None
    return authority_models.AttemptLeaseAuthority(
        host_epoch=snapshot.host_epoch,
        attempt=attempt_id,
        work_item_id=attempt.work_item_id,
        task_id=retained.task_id,
        host_id=retained.host_id,
        lease_id=retained.lease_id,
        generation=retained.generation,
        acquired_at=retained.acquired_at,
        expires_at=retained.expires_at,
        state=retained.status,
    )


def decide_and_commit_attempt_authority_change(
    store: WorkStore,
    requested_change: authority_models.AttemptAuthorityOperation,
) -> DecisionResult[AttemptAuthorityMutationResult, AttemptAuthorityChangeCode]:
    """Reread locked state, decide, and commit one attempt-authority change."""

    with store.write() as transaction:
        return _commit_attempt_authority_change(transaction, requested_change)


def acquire_attempt_authority(
    store: WorkStore,
    *,
    attempt_id: AttemptId,
    task_id: TaskId,
    host_id: HostId,
    lease_id: LeaseId,
    acquired_at: datetime,
    expires_at: datetime,
) -> DecisionResult[AttemptAuthorityMutationResult, AttemptAuthorityAcquireCode]:
    """Select initial acquisition or inactive transfer under one write lock."""

    with store.write() as transaction:
        snapshot = transaction.read_decision_facts(
            query_models.DecisionScope((), (), (), (), (), (attempt_id,), (), (), (), ()), acquired_at
        ).snapshot
        attempt = snapshot.attempt(attempt_id)
        if attempt is None:
            return DecisionFailure(
                DecisionFailureCode.ATTEMPT_LEASE_REQUIRED,
                f"Attempt '{attempt_id}' is not current.",
                None,
            )
        retained = transaction.read_attempt_authority_status(attempt_id)
        if retained is None:
            requested_change: authority_models.AttemptAuthorityOperation = (
                authority_models.AcquireInitialAttemptAuthority(
                    snapshot.host_epoch,
                    attempt_id,
                    attempt.work_item_id,
                    task_id,
                    host_id,
                    lease_id,
                    acquired_at,
                    expires_at,
                )
            )
        else:
            state = retained.status
            if state == authority_models.AttemptLeaseStatus.ACTIVE and retained.expires_at <= acquired_at:
                state = authority_models.AttemptLeaseStatus.EXPIRED
            if state == authority_models.AttemptLeaseStatus.ACTIVE:
                return DecisionFailure(
                    DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
                    "Attempt authority remains live.",
                    FailureDetails(
                        observed=(
                            FailureFact("attempt_id", str(attempt_id)),
                            FailureFact("holder_task_id", str(retained.task_id)),
                            FailureFact("holder_host_id", str(retained.host_id)),
                            FailureFact("generation", retained.generation),
                            FailureFact("expires_at", retained.expires_at.isoformat()),
                            FailureFact("authority_status", retained.status.value),
                        ),
                        mismatches=(FailureMismatch("authority_availability", "available", "live-holder"),),
                        retry=RetryDisposition.DO_NOT_RETRY,
                        effect=EffectDisposition.UNCHANGED,
                        changed_surfaces=(),
                        alternatives=(),
                    ),
                )
            requested_change = authority_models.TransferAttemptAuthority(
                authority_models.InactiveAttemptAuthority(
                    snapshot.host_epoch,
                    attempt_id,
                    attempt.work_item_id,
                    retained.task_id,
                    retained.host_id,
                    retained.lease_id,
                    retained.generation,
                    retained.expires_at,
                    state,
                ),
                task_id,
                host_id,
                lease_id,
                acquired_at,
                expires_at,
            )
        return _commit_attempt_authority_change(transaction, requested_change)


def _commit_attempt_authority_change(
    transaction: WorkTransaction,
    requested_change: authority_models.AttemptAuthorityOperation,
) -> DecisionResult[AttemptAuthorityMutationResult, AttemptAuthorityChangeCode]:
    """Decide and persist inside the caller's existing transaction."""

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
    decision_context = transaction.read_decision_facts(
        query_models.DecisionScope((), (), (), (), (), (attempt_id,), (), (), (), ()), decided_at
    ).snapshot
    retained = transaction.read_attempt_authority_status(attempt_id)
    generation_before = transaction.read_attempt_generation(attempt_id)
    decision_result = decide_attempt_authority(
        retained=_project_retained_attempt_authority(decision_context, retained, attempt_id),
        counter=generation_before,
        operation=requested_change,
        live_attempt=(
            (attempt_id, attempt.work_item_id)
            if (attempt := decision_context.attempt(attempt_id)) is not None
            and attempt.state == work_models.AttemptState.ACTIVE
            else None
        ),
        transferable_attempt=(
            (attempt_id, attempt.work_item_id)
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
        work_item_id=proposed_replacement.work_item_id,
        outcome=history_outcome,
        evidence=None,
        decided_at=decided_at,
    )
    allocation = transaction.read_mutation_allocation()
    mutation_receipt = MutationReceipt(
        transition=transition_receipt,
        history_id=allocation.next_history_id,
        project_revision=allocation.project_revision + 1,
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
    committed = transaction.commit(mutation)
    if isinstance(committed, DecisionFailure):
        return committed
    retained = transaction.read_attempt_authority_status(attempt_id)
    if (
        retained is None
        or _project_retained_attempt_authority(decision_context, retained, attempt_id) != proposed_replacement
    ):
        raise RuntimeError("Committed attempt authority did not reload exactly inside its transaction.")
    return AttemptAuthorityMutationResult(committed, retained)


def _project_retained_preparation_authority(
    snapshot: LedgerSnapshot,
    retained: query_models.PreparationAuthorityStatus | None,
    work_item_id: WorkItemId,
) -> authority_models.PreparationLeaseAuthority | None:
    if retained is None:
        return None
    return authority_models.PreparationLeaseAuthority(
        host_epoch=snapshot.host_epoch,
        work_item_id=work_item_id,
        definition_revision=retained.definition_revision,
        definition_digest=retained.definition_digest,
        task_id=retained.task_id,
        host_id=retained.host_id,
        lease_id=retained.lease_id,
        generation=retained.generation,
        acquired_at=retained.acquired_at,
        expires_at=retained.expires_at,
        state=retained.status,
    )


def decide_and_commit_preparation_authority_change(
    store: WorkStore,
    requested_change: authority_models.PreparationAuthorityOperation,
) -> DecisionResult[PreparationAuthorityMutationResult, PreparationAuthorityChangeCode]:
    """Reread locked state, decide, and commit one exact preparation change."""

    with store.write() as transaction:
        return _commit_preparation_authority_change(transaction, requested_change)


def start_preparation(
    store: WorkStore,
    *,
    work_item_id: WorkItemId,
    task_id: TaskId,
    host_id: HostId,
    lease_id: LeaseId,
    acquired_at: datetime,
    expires_at: datetime,
) -> DecisionResult[PreparationStart, PreparationAuthorityChangeCode]:
    """Select current initial acquisition or inactive transfer under one write lock."""

    with store.write() as transaction:
        snapshot = transaction.read_decision_facts(
            query_models.DecisionScope((work_item_id,), (), (), (), (), (), (), (), (), ()), acquired_at
        ).snapshot
        retained = _project_retained_preparation_authority(
            snapshot, transaction.read_preparation_authority_status(work_item_id), work_item_id
        )
        if retained is None:
            definition = snapshot.definition(work_item_id)
            subject_revision = snapshot.subject_revision(work_item_id)
            if definition is None or subject_revision is None:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE, f"Item '{work_item_id}' has no definition.", None
                )
            requested_change = authority_models.AcquireInitialPreparationAuthority(
                snapshot.host_epoch,
                work_item_id,
                snapshot.revision,
                subject_revision,
                definition.revision,
                definition.digest,
                task_id,
                host_id,
                lease_id,
                acquired_at,
                expires_at,
            )
        else:
            state = retained.state
            if state == authority_models.PreparationLeaseStatus.ACTIVE and retained.expires_at <= acquired_at:
                state = authority_models.PreparationLeaseStatus.EXPIRED
            requested_change = authority_models.TransferPreparationAuthority(
                authority_models.InactivePreparationAuthority(
                    retained.host_epoch,
                    retained.work_item_id,
                    retained.definition_revision,
                    retained.definition_digest,
                    retained.task_id,
                    retained.host_id,
                    retained.lease_id,
                    retained.generation,
                    retained.expires_at,
                    state,
                ),
                task_id,
                host_id,
                lease_id,
                acquired_at,
                expires_at,
            )
        committed = _commit_preparation_authority_change(transaction, requested_change)
        if isinstance(committed, DecisionFailure):
            return committed
        lease = _project_retained_preparation_authority(snapshot, committed.authority, work_item_id)
        assert lease is not None
        return PreparationStart(committed.effect, lease)


def _commit_preparation_authority_change(
    transaction: WorkTransaction,
    requested_change: authority_models.PreparationAuthorityOperation,
) -> DecisionResult[PreparationAuthorityMutationResult, PreparationAuthorityChangeCode]:
    """Decide and persist inside the caller's existing transaction."""

    match requested_change:
        case authority_models.AcquireInitialPreparationAuthority(
            work_item_id=item_id, task_id=actor_task_id, host_id=actor_host_id, acquired_at=decided_at
        ):
            history_outcome = "acquire-initial-preparation-authority"
        case authority_models.TransferPreparationAuthority(
            current=current, task_id=actor_task_id, host_id=actor_host_id, acquired_at=decided_at
        ):
            item_id = current.work_item_id
            history_outcome = "transfer-preparation-authority"
        case authority_models.RenewPreparationAuthority(current=current, renewed_at=decided_at):
            item_id = current.work_item_id
            actor_task_id, actor_host_id = current.task_id, current.host_id
            history_outcome = "renew-preparation-authority"
        case authority_models.ReleasePreparationAuthority(current=current, released_at=decided_at):
            item_id = current.work_item_id
            actor_task_id, actor_host_id = current.task_id, current.host_id
            history_outcome = "release-preparation-authority"
        case authority_models.RevokePreparationAuthority(
            work_item_id=item_id, task_id=actor_task_id, host_id=actor_host_id, revoked_at=decided_at
        ):
            history_outcome = "revoke-preparation-authority"
        case _ as unreachable:
            assert_never(unreachable)
    decision_context = transaction.read_decision_facts(
        query_models.DecisionScope((item_id,), (), (), (), (), (), (), (), (), ()), decided_at
    ).snapshot
    retained = transaction.read_preparation_authority_status(item_id)
    generation_before = transaction.read_preparation_generation(item_id)
    decision_result = decide_preparation_authority(
        retained=_project_retained_preparation_authority(decision_context, retained, item_id),
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
        work_item_id=item_id,
        outcome=history_outcome,
        evidence=None,
        decided_at=decided_at,
    )
    allocation = transaction.read_mutation_allocation()
    mutation_receipt = MutationReceipt(
        transition=transition_receipt,
        history_id=allocation.next_history_id,
        project_revision=allocation.project_revision + 1,
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
    committed = transaction.commit(mutation)
    if isinstance(committed, DecisionFailure):
        return committed
    retained = transaction.read_preparation_authority_status(item_id)
    if (
        retained is None
        or _project_retained_preparation_authority(decision_context, retained, item_id) != proposed_replacement
    ):
        raise RuntimeError("Committed preparation authority did not reload exactly inside its transaction.")
    return PreparationAuthorityMutationResult(committed, retained)


type ProposalCreationFailureCode = (
    DependencyPinFailureCode  # resolved and locked checkpoint targets
    | Literal[DecisionFailureCode.ITEM_DEFINITION_INVALID]  # obligation origins
    | ProposalCreationDecisionCode
    | StaleWriteCode  # stale proposal write
)


def create_proposal(
    store: WorkStore,
    operation: CreateProposalOperation,
    now: datetime,
    *,
    actor_task_id: TaskId,
    actor_host_id: HostId,
    dependency_brief_pins: tuple[DependencyCheckpointPin, ...],
) -> DecisionResult[ProposalCreationResult, ProposalCreationFailureCode]:
    """Reread locked state, decide, and commit proposal facts plus their ready item."""

    with store.write() as transaction:
        allocation = transaction.read_mutation_allocation()
        live_item_count = transaction.read_live_item_count()
        proposal_item = WorkItemId(operation.intake.proposal_id)
        primary_items = (proposal_item,)
        related_items: tuple[WorkItemId, ...] = ()
        match operation.intake.relation:
            case (
                work_models.PrerequisiteProposalRelation(work_item_id=related_item)
                | work_models.PlannedReplacementProposalRelation(work_item_id=related_item)
            ):
                primary_items = (*primary_items, related_item)
            case (
                work_models.FollowUpProposalRelation(work_item_id=related_item)
                | work_models.DuplicateProposalRelation(work_item_id=related_item)
                | work_models.ContradictionProposalRelation(work_item_id=related_item)
            ):
                related_items = (related_item,)
            case work_models.IndependentProposalRelation() | work_models.ClarificationProposalRelation():
                pass
            case _ as unreachable:
                assert_never(unreachable)
        requested_dependencies = (
            (operation.intake.relation.dependency,)
            if isinstance(operation.intake.relation, work_models.FollowUpProposalRelation)
            else ()
        )
        checkpoint_targets = tuple(
            value for value in requested_dependencies if isinstance(value, work_models.CheckpointDependency)
        )
        primary_items = (*primary_items, *(value.item_id for value in checkpoint_targets))
        decision_context = transaction.read_decision_facts(
            query_models.DecisionScope(
                work_item_ids=primary_items,
                related_work_item_ids=related_items,
                dependency_closure_roots=(),
                live_dependent_roots=(),
                live_dependent_attempt_ids=(),
                attempt_ids=(),
                proposal_ids=(operation.intake.proposal_id,),
                artifact_ref_ids=(),
                completion_history_attempt_ids=(),
                checkpoint_targets=checkpoint_targets,
            ),
            now,
        ).snapshot
        if (
            failure := validate_dependency_brief_pins(
                decision_context, None, requested_dependencies, dependency_brief_pins
            )
        ) is not None:
            return failure
        if (failure := obligation_coverage.validate_origins(transaction, operation.intake.obligations)) is not None:
            return failure
        decision_result = decide_proposal_creation(
            decision_context,
            operation,
            live_item_count,
            transaction.read_membership_intent_facts(operation.intake.related_disposition),
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
            allocation.next_history_id,
            allocation.project_revision + 1,
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
        committed = transaction.commit_proposal(mutation)
        if isinstance(committed, DecisionFailure):
            return committed
        retained = transaction.read_created_proposal_item(accepted_decision.ready_item.work_item_id)
        if (
            retained is None
            or retained.queue_position != accepted_decision.ready_item.position
            or retained.state != stored_state.StoredWorkItemState.READY
        ):
            raise RuntimeError("Committed proposal item did not reload exactly inside its transaction.")
        return ProposalCreationResult(committed, retained)


def _resolve_actor_authority(
    snapshot: LedgerSnapshot,
    action: decision_models.TransitionAction,
    now: datetime,
) -> DecisionResult[
    decision_models.ActorAuthority,
    Literal[DecisionFailureCode.ACTION_NOT_AVAILABLE, DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED],
]:
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
                    None,
                )
            return decision_models.ActorAuthority(
                decision_models.Role.WORKER,
                capability.authorization,
                authority.generation,
                capability.lease_id,
                (authority.attempt,),
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
                    None,
                )
            return decision_models.ActorAuthority(
                decision_models.Role.PREPARER,
                capability.authorization,
                authority.generation,
                capability.lease_id,
                preparations=(authority.work_item_id,),
            )
        case _ as unreachable:
            assert_never(unreachable)


def _transition_decision_scope(
    command: decision_models.TransitionCommand,
) -> query_models.DecisionScope:
    item_ids, attempt_ids, proposal_ids = action_subject_ids(command.action)
    related_item_ids: tuple[WorkItemId, ...] = ()
    dependency_closure_roots: tuple[WorkItemId, ...] = ()
    live_dependent_roots: tuple[WorkItemId, ...] = ()
    live_dependent_attempt_ids: tuple[AttemptId, ...] = ()
    artifact_ids: tuple[ArtifactRefId, ...] = ()
    completion_history_attempt_ids: tuple[AttemptId, ...] = ()
    checkpoint_targets: tuple[work_models.CheckpointDependency, ...] = ()
    match command:
        case decision_models.ActivateCommand(value=value):
            artifact_ids = (value.brief_artifact_ref_id,)
        case decision_models.ResumeCommand(value=value) | decision_models.RebindAttemptCommand(value=value):
            artifact_ids = () if value.brief_artifact_ref_id is None else (value.brief_artifact_ref_id,)
        case (
            decision_models.BlockCommand(value=work_models.BlockInput(unblocker=unblocker))
            | decision_models.BlockWorkItemCommand(value=work_models.BlockInput(unblocker=unblocker))
            | decision_models.PauseCommand(value=work_models.PlainPauseInput(unblocker=unblocker))
        ):
            related_item_ids = unblockers.waited_items(unblocker)
        case decision_models.MergeProposalCommand(value=value):
            related_item_ids = (value.target,)
            live_dependent_roots = (WorkItemId(command.action.capability.subject),)
        case decision_models.ReviseWorkItemCommand(value=value):
            related_item_ids = tuple(work_models.dependency_item(target) for target in value.definition.dependencies)
            dependency_closure_roots = related_item_ids
            checkpoint_targets = tuple(
                target
                for target in value.definition.dependencies
                if isinstance(target, work_models.CheckpointDependency)
            )
            item_ids = (*item_ids, *(target.item_id for target in checkpoint_targets))
        case decision_models.RecordReplacementCommand(value=value):
            related_item_ids = (value.replacement_item,)
        case decision_models.CloseCommand():
            live_dependent_roots = item_ids
        case decision_models.CompleteCommand() | decision_models.CoveredCompleteCommand():
            completion_history_attempt_ids = attempt_ids
            live_dependent_attempt_ids = attempt_ids
        case decision_models.AcceptCheckpointCommand():
            live_dependent_attempt_ids = attempt_ids
        case (
            decision_models.AcceptReviewAndContinueCommand()
            | decision_models.PauseCommand()
            | decision_models.SubmitReviewCommand()
            | decision_models.ReturnForCorrectionCommand()
            | decision_models.RecordAmendmentCommand()
            | decision_models.RejectProposalCommand()
            | decision_models.RetainTemporarilyCommand()
        ):
            pass
        case _ as unreachable:
            assert_never(unreachable)
    return query_models.DecisionScope(
        work_item_ids=tuple(dict.fromkeys(item_ids)),
        related_work_item_ids=tuple(dict.fromkeys(related_item_ids)),
        dependency_closure_roots=tuple(dict.fromkeys(dependency_closure_roots)),
        live_dependent_roots=tuple(dict.fromkeys(live_dependent_roots)),
        live_dependent_attempt_ids=tuple(dict.fromkeys(live_dependent_attempt_ids)),
        attempt_ids=tuple(dict.fromkeys(attempt_ids)),
        proposal_ids=tuple(dict.fromkeys(proposal_ids)),
        artifact_ref_ids=artifact_ids,
        completion_history_attempt_ids=completion_history_attempt_ids,
        checkpoint_targets=checkpoint_targets,
    )


@overload
def _read_authorize_and_decide(
    reader: WorkStore | WorkTransaction,
    command: decision_models.AcceptCheckpointCommand,
    now: datetime,
    transition_brief_identity: WorkBriefIdentity | None,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
    read_authorization_time: Callable[[], datetime],
    dependency_brief_pins: tuple[DependencyCheckpointPin, ...],
) -> DecisionResult[decision_models.CheckpointAcceptanceDecision, CheckpointAcceptanceFailureCode]: ...


@overload
def _read_authorize_and_decide(
    reader: WorkStore | WorkTransaction,
    command: decision_models.NonCheckpointTransitionCommand,
    now: datetime,
    transition_brief_identity: WorkBriefIdentity | None,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
    read_authorization_time: Callable[[], datetime],
    dependency_brief_pins: tuple[DependencyCheckpointPin, ...],
) -> DecisionResult[decision_models.TransitionDecision, TransitionFailureCode]: ...


@overload
def _read_authorize_and_decide(
    reader: WorkStore | WorkTransaction,
    command: decision_models.CoveredCompleteCommand,
    now: datetime,
    transition_brief_identity: WorkBriefIdentity | None,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
    read_authorization_time: Callable[[], datetime],
    dependency_brief_pins: tuple[DependencyCheckpointPin, ...],
) -> DecisionResult[decision_models.CompletionAcceptanceDecision, CoveredCompletionFailureCode]: ...


def _read_authorize_and_decide(
    reader: WorkStore | WorkTransaction,
    command: decision_models.TransitionCommand,
    now: datetime,
    transition_brief_identity: WorkBriefIdentity | None,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
    read_authorization_time: Callable[[], datetime],
    dependency_brief_pins: tuple[DependencyCheckpointPin, ...],
) -> DecisionResult[decision_models.Decision, TransitionFailureCode]:
    """Read exact facts, sample fresh authority validity, and decide without committing."""

    facts = reader.read_decision_facts(_transition_decision_scope(command), read_authorization_time())
    authority_observed_at = read_authorization_time()
    decision_context = facts.snapshot
    if command.action.capability.authorization == decision_models.AuthorizationKind.PROJECT and (
        actor_task_id is None or actor_host_id is None
    ):
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Project actions require the invoking task and host identity.",
            None,
        )
    actor_authority = _resolve_actor_authority(decision_context, command.action, authority_observed_at)
    if isinstance(actor_authority, DecisionFailure):
        return actor_authority
    if (failure := validate_supplied_action(decision_context, actor_authority, command.action)) is not None:
        return failure
    if (failure := validate_transition_work_brief(facts, command, transition_brief_identity)) is not None:
        return failure
    if (
        isinstance(command, decision_models.ReviseWorkItemCommand)
        and (
            failure := validate_dependency_brief_pins(
                decision_context,
                command.action.capability.subject,
                command.value.definition.dependencies,
                dependency_brief_pins,
            )
        )
        is not None
    ):
        return failure
    if (
        isinstance(command, decision_models.ReviseWorkItemCommand)
        and (failure := obligation_coverage.validate_origins(reader, command.value.definition.obligations)) is not None
    ):
        return failure
    return decide(decision_context, command, now)


def decide_and_commit_transition(
    store: WorkStore,
    command: decision_models.NonCheckpointTransitionCommand,
    now: datetime,
    *,
    read_authorization_time: Callable[[], datetime],
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
    dependency_brief_pins: tuple[DependencyCheckpointPin, ...],
    transition_brief_identity: WorkBriefIdentity | None = None,
) -> DecisionResult[CommittedEffect, TransitionFailureCode]:
    """Authorize with the supplied clock under lock; retain request-time receipt provenance."""

    with store.write() as transaction:
        decision_result = _read_authorize_and_decide(
            transaction,
            command,
            now,
            transition_brief_identity,
            actor_task_id,
            actor_host_id,
            read_authorization_time,
            dependency_brief_pins,
        )
        if isinstance(decision_result, DecisionFailure):
            return decision_result
        accepted_decision = decision_result
        allocation = transaction.read_mutation_allocation()
        mutation = project_transition_mutation(allocation, accepted_decision, actor_task_id, actor_host_id)
        return transaction.commit(mutation)


def decide_and_commit_review_submission(
    store: WorkStore,
    command: decision_models.SubmitReviewCommand,
    now: datetime,
    candidate_snapshot: EvidenceArtifactRef,
    candidate_snapshot_schema: str,
    *,
    read_authorization_time: Callable[[], datetime],
) -> DecisionResult[CommittedEffect, TransitionFailureCode]:
    """Authorize with the supplied clock under lock; retain snapshot-time receipt provenance."""

    with store.write() as transaction:
        decision_result = _read_authorize_and_decide(
            transaction, command, now, None, None, None, read_authorization_time, ()
        )
        if isinstance(decision_result, DecisionFailure):
            return decision_result
        allocation = transaction.read_checkpoint_mutation_allocation((candidate_snapshot,))
        mutation = project_review_submission_mutation(
            allocation, decision_result, candidate_snapshot, candidate_snapshot_schema
        )
        return transaction.commit(mutation)


def preflight_checkpoint_candidate(
    store: WorkStore,
    command: decision_models.AcceptCheckpointCommand,
    now: datetime,
    transition_brief_identity: CurrentAttemptWorkBriefIdentity,
) -> (
    DecisionFailure[
        Literal[
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            DecisionFailureCode.ATTEMPT_LEASE_REQUIRED,
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            DecisionFailureCode.ROUTE_CLASSIFICATION_REQUIRED,
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
        ]
    ]
    | None
):
    """Revalidate action currentness, then reject only candidate mismatch."""

    facts = store.read_decision_facts(_transition_decision_scope(command), now)
    actor_authority = _resolve_actor_authority(facts.snapshot, command.action, now)
    if isinstance(actor_authority, DecisionFailure):
        return actor_authority
    if (failure := validate_supplied_action(facts.snapshot, actor_authority, command.action)) is not None:
        return failure
    if (failure := validate_transition_work_brief(facts, command, transition_brief_identity)) is not None:
        return failure
    return validate_checkpoint_candidate(facts.snapshot, command)


def preflight_covered_completion(
    store: WorkStore,
    command: decision_models.CoveredCompleteCommand,
    now: datetime,
    *,
    actor_task_id: TaskId,
    actor_host_id: HostId,
    transition_brief_identity: CurrentAttemptWorkBriefIdentity,
) -> DecisionFailure[CoveredCompletionFailureCode] | None:
    """Reject stale authority, wrong lifecycle, candidate, or checkpoint coverage before publication."""

    result = _read_authorize_and_decide(
        store, command, now, transition_brief_identity, actor_task_id, actor_host_id, lambda: now, ()
    )
    if isinstance(result, DecisionFailure):
        return result
    return None


def decide_and_commit_checkpoint_acceptance(
    store: WorkStore,
    command: decision_models.AcceptCheckpointCommand,
    now: datetime,
    checkpoint_artifacts: CheckpointArtifacts,
    *,
    read_authorization_time: Callable[[], datetime],
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
    transition_brief_identity: CurrentAttemptWorkBriefIdentity,
) -> DecisionResult[CommittedEffect, CheckpointAcceptanceFailureCode]:
    """Authorize with the supplied clock under lock; retain request-time receipt provenance."""

    with store.write() as transaction:
        decision_result = _read_authorize_and_decide(
            transaction,
            command,
            now,
            transition_brief_identity,
            actor_task_id,
            actor_host_id,
            read_authorization_time,
            (),
        )
        if isinstance(decision_result, DecisionFailure):
            return decision_result
        accepted_decision = decision_result
        allocation = transaction.read_checkpoint_mutation_allocation(
            (
                checkpoint_artifacts.candidate,
                checkpoint_artifacts.result,
                checkpoint_artifacts.review,
                checkpoint_artifacts.package,
            )
        )
        mutation = project_checkpoint_acceptance_mutation(
            allocation, accepted_decision, checkpoint_artifacts, actor_task_id, actor_host_id
        )
        return transaction.commit(mutation)


def decide_and_commit_covered_completion(
    store: WorkStore,
    command: decision_models.CoveredCompleteCommand,
    now: datetime,
    completion_artifacts: CompletionArtifacts,
    *,
    read_authorization_time: Callable[[], datetime],
    actor_task_id: TaskId,
    actor_host_id: HostId,
    transition_brief_identity: CurrentAttemptWorkBriefIdentity,
) -> DecisionResult[CommittedEffect, CoveredCompletionFailureCode]:
    """Authorize with the supplied clock under lock; retain request-time receipt provenance."""

    with store.write() as transaction:
        decision_result = _read_authorize_and_decide(
            transaction,
            command,
            now,
            transition_brief_identity,
            actor_task_id,
            actor_host_id,
            read_authorization_time,
            (),
        )
        if isinstance(decision_result, DecisionFailure):
            return decision_result
        allocation = transaction.read_checkpoint_mutation_allocation(
            (completion_artifacts.result, completion_artifacts.review, completion_artifacts.package)
        )
        mutation = project_completion_acceptance_mutation(
            allocation,
            decision_result,
            command.value,
            completion_artifacts,
            actor_task_id,
            actor_host_id,
        )
        return transaction.commit(mutation)


def reorder(
    store: WorkStore,
    expected: tuple[WorkItemId, ...],
    requested: tuple[WorkItemId, ...],
    actor_task_id: TaskId,
    actor_host_id: HostId,
    now: datetime,
) -> DecisionResult[CommittedEffect, OrderDecisionCode | StaleWriteCode]:
    """Compare the complete live sequence under the write lock, then commit its permutation."""
    with store.write() as transaction:
        change = decide_order(transaction.read_live_order(), expected, requested)
        if isinstance(change, DecisionFailure):
            return change
        allocation = transaction.read_mutation_allocation()
        receipt = MutationReceipt(
            decision_models.TransitionReceipt(LIVE_ORDER_RECEIPT_ACTION_ID, None, "reorder", None, now),
            allocation.next_history_id,
            allocation.project_revision + 1,
            decision_models.ActionKind.INSPECT,
            HistorySubjectId("live-order"),
            None,
            decision_models.AuthorizationKind.PROJECT,
            actor_task_id,
            actor_host_id,
            "pinboard-live-order/v1",
            work_models.CanonicalJson(
                msgspec.json.encode(OrderRequest("pinboard-live-order/v1", expected, requested), order="sorted")
            ),
        )
        return transaction.commit_order(OrderMutation(receipt, change))
