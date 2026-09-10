from datetime import datetime
from typing import assert_never, overload

from pinboard.application import query_models
from pinboard.application.actions import action_subject_ids
from pinboard.application.artifact_publication import validate_transition_work_brief
from pinboard.application.artifacts import CheckpointArtifacts, CompletionArtifacts, WorkBriefIdentity
from pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    CommittedEffect,
    MutationReceipt,
    PreparationAuthorityMutation,
    PreparationStart,
    ProposalCreationMutation,
)
from pinboard.application.mutations import (
    project_checkpoint_acceptance_mutation,
    project_completion_acceptance_mutation,
    project_transition_mutation,
)
from pinboard.application.ports import WorkStore, WorkTransaction
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.authority_decisions import (
    decide_attempt_authority,
    decide_preparation_authority,
)
from pinboard.domain.decisions import decide, validate_checkpoint_candidate, validate_supplied_action
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HistorySubjectId,
    HostId,
    ItemId,
    LeaseId,
    TaskId,
)
from pinboard.domain.ledger import LedgerSnapshot
from pinboard.domain.proposal_decisions import decide_proposal_creation
from pinboard.domain.proposal_models import (
    CreateProposalOperation,
)


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
        item=attempt.item,
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
) -> DecisionResult[CommittedEffect]:
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
        decision_context = transaction.read_decision_facts(
            query_models.DecisionScope((), (), (), (), (attempt_id,), (), (), ()), decided_at
        ).snapshot
        retained = transaction.read_attempt_authority_status(attempt_id)
        generation_before = transaction.read_attempt_generation(attempt_id)
        decision_result = decide_attempt_authority(
            retained=_project_retained_attempt_authority(decision_context, retained, attempt_id),
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
        return transaction.commit(mutation)


def _project_retained_preparation_authority(
    snapshot: LedgerSnapshot,
    retained: query_models.PreparationAuthorityStatus | None,
    item_id: ItemId,
) -> authority_models.PreparationLeaseAuthority | None:
    if retained is None:
        return None
    return authority_models.PreparationLeaseAuthority(
        host_epoch=snapshot.host_epoch,
        item=item_id,
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
) -> DecisionResult[CommittedEffect]:
    """Reread locked state, decide, and commit one exact preparation change."""

    with store.write() as transaction:
        committed = _commit_preparation_authority_change(transaction, requested_change)
        if isinstance(committed, DecisionFailure):
            return committed
        effect, _lease = committed
        return effect


def start_preparation(
    store: WorkStore,
    *,
    item_id: ItemId,
    task_id: TaskId,
    host_id: HostId,
    lease_id: LeaseId,
    acquired_at: datetime,
    expires_at: datetime,
) -> DecisionResult[PreparationStart]:
    """Select current initial acquisition or inactive transfer under one write lock."""

    with store.write() as transaction:
        snapshot = transaction.read_decision_facts(
            query_models.DecisionScope((item_id,), (), (), (), (), (), (), ()), acquired_at
        ).snapshot
        retained = _project_retained_preparation_authority(
            snapshot, transaction.read_preparation_authority_status(item_id), item_id
        )
        if retained is None:
            definition = snapshot.definition(item_id)
            subject_revision = snapshot.subject_revision(item_id)
            if definition is None or subject_revision is None:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE, f"Item '{item_id}' has no definition.", None
                )
            requested_change = authority_models.AcquireInitialPreparationAuthority(
                snapshot.host_epoch,
                item_id,
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
                    retained.item,
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
        effect, lease = committed
        return PreparationStart(effect, lease)


def _commit_preparation_authority_change(
    transaction: WorkTransaction,
    requested_change: authority_models.PreparationAuthorityOperation,
) -> DecisionResult[tuple[CommittedEffect, authority_models.PreparationLeaseAuthority]]:
    """Decide and persist inside the caller's existing transaction."""

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
    decision_context = transaction.read_decision_facts(
        query_models.DecisionScope((item_id,), (), (), (), (), (), (), ()), decided_at
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
        item=item_id,
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
    return committed, proposed_replacement


def create_proposal(
    store: WorkStore,
    operation: CreateProposalOperation,
    now: datetime,
    *,
    actor_task_id: TaskId,
    actor_host_id: HostId,
) -> DecisionResult[CommittedEffect]:
    """Reread locked state, decide, and commit proposal facts plus their intake item."""

    with store.write() as transaction:
        allocation = transaction.read_mutation_allocation()
        live_item_count = transaction.read_live_item_count()
        relation_item = operation.intake.relation.item
        proposal_item = ItemId(operation.intake.proposal_id)
        primary_items = (proposal_item,)
        related_items: tuple[ItemId, ...] = ()
        if relation_item is not None:
            if isinstance(operation.intake.relation, work_models.PrerequisiteProposalRelation):
                primary_items = (*primary_items, relation_item)
            else:
                related_items = (relation_item,)
        decision_context = transaction.read_decision_facts(
            query_models.DecisionScope(
                item_ids=primary_items,
                related_item_ids=related_items,
                dependency_closure_roots=(),
                live_dependent_roots=(),
                attempt_ids=(),
                proposal_ids=(operation.intake.proposal_id,),
                artifact_ref_ids=(),
                completion_history_attempt_ids=(),
            ),
            now,
        ).snapshot
        decision_result = decide_proposal_creation(
            decision_context,
            operation,
            live_item_count,
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
                preparations=(authority.item,),
            )
        case _ as unreachable:
            assert_never(unreachable)


def _transition_decision_scope(  # noqa: C901, PLR0912 - one exhaustive command-to-read-scope boundary
    command: decision_models.TransitionCommand,
) -> query_models.DecisionScope:
    item_ids, attempt_ids, proposal_ids = action_subject_ids(command.action)
    related_item_ids: tuple[ItemId, ...] = ()
    dependency_closure_roots: tuple[ItemId, ...] = ()
    live_dependent_roots: tuple[ItemId, ...] = ()
    artifact_ids: tuple[ArtifactRefId, ...] = ()
    completion_history_attempt_ids: tuple[AttemptId, ...] = ()
    match command:
        case decision_models.ActivateCommand(value=value):
            artifact_ids = (value.brief_artifact_ref_id,)
        case decision_models.ResumeCommand(value=value) | decision_models.RebindAttemptCommand(value=value):
            if value.brief_artifact_ref_id is not None:
                artifact_ids = (value.brief_artifact_ref_id,)
        case decision_models.BlockCommand(value=value) | decision_models.BlockItemCommand(value=value):
            related_item_ids = value.depends_on
        case decision_models.AcceptProposalCommand(value=value):
            item_ids = (*item_ids, value.item)
            related_item_ids = value.depends_on
            dependency_closure_roots = (value.item, *value.depends_on)
        case decision_models.MergeProposalCommand(value=value):
            related_item_ids = (value.target,)
        case decision_models.ReviseItemCommand(value=value):
            item_ids = (*item_ids, value.item_id)
            related_item_ids = value.definition.dependencies
            dependency_closure_roots = value.definition.dependencies
        case decision_models.RecordReplacementCommand(value=value):
            related_item_ids = (value.replacement_item,)
        case decision_models.RetainTemporarilyCommand():
            pass
        case decision_models.CloseCommand():
            live_dependent_roots = item_ids
        case decision_models.CompleteCommand() | decision_models.CoveredCompleteCommand():
            completion_history_attempt_ids = attempt_ids
        case (
            decision_models.AcceptCheckpointCommand()
            | decision_models.AcceptReviewAndContinueCommand()
            | decision_models.PauseCommand()
            | decision_models.SubmitReviewCommand()
            | decision_models.ReturnForCorrectionCommand()
            | decision_models.ReopenCommand()
            | decision_models.MarkReadyCommand()
            | decision_models.DeferCommand()
            | decision_models.ReturnProposalCommand()
            | decision_models.RejectProposalCommand()
        ):
            pass
        case _ as unreachable:
            assert_never(unreachable)
    return query_models.DecisionScope(
        item_ids=tuple(dict.fromkeys(item_ids)),
        related_item_ids=tuple(dict.fromkeys(related_item_ids)),
        dependency_closure_roots=tuple(dict.fromkeys(dependency_closure_roots)),
        live_dependent_roots=tuple(dict.fromkeys(live_dependent_roots)),
        attempt_ids=tuple(dict.fromkeys(attempt_ids)),
        proposal_ids=tuple(dict.fromkeys(proposal_ids)),
        artifact_ref_ids=artifact_ids,
        completion_history_attempt_ids=completion_history_attempt_ids,
    )


@overload
def _validate_supplied_transition_and_decide(
    facts: query_models.DecisionFacts,
    command: decision_models.AcceptCheckpointCommand,
    now: datetime,
    transition_brief_identity: WorkBriefIdentity | None,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
) -> DecisionResult[decision_models.CheckpointAcceptanceDecision]: ...


@overload
def _validate_supplied_transition_and_decide(
    facts: query_models.DecisionFacts,
    command: decision_models.NonCheckpointTransitionCommand,
    now: datetime,
    transition_brief_identity: WorkBriefIdentity | None,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
) -> DecisionResult[decision_models.TransitionDecision]: ...


@overload
def _validate_supplied_transition_and_decide(
    facts: query_models.DecisionFacts,
    command: decision_models.CoveredCompleteCommand,
    now: datetime,
    transition_brief_identity: WorkBriefIdentity | None,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
) -> DecisionResult[decision_models.CompletionAcceptanceDecision]: ...


def _validate_supplied_transition_and_decide(
    facts: query_models.DecisionFacts,
    command: decision_models.TransitionCommand,
    now: datetime,
    transition_brief_identity: WorkBriefIdentity | None,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
) -> DecisionResult[decision_models.Decision]:
    """Resolve supplied authority and reject stale context before deciding."""

    decision_context = facts.snapshot
    if command.action.capability.authorization == decision_models.AuthorizationKind.PROJECT and (
        actor_task_id is None or actor_host_id is None
    ):
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Project actions require the invoking task and host identity.",
            None,
        )
    actor_authority = _resolve_actor_authority(decision_context, command.action, now)
    if isinstance(actor_authority, DecisionFailure):
        return actor_authority
    if (failure := validate_supplied_action(decision_context, actor_authority, command.action)) is not None:
        return failure
    if (failure := validate_transition_work_brief(facts, command, transition_brief_identity)) is not None:
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
) -> DecisionResult[CommittedEffect]:
    """Validate, decide, and commit one lifecycle mutation under one write lock."""

    with store.write() as transaction:
        facts = transaction.read_decision_facts(_transition_decision_scope(command), now)
        decision_result = _validate_supplied_transition_and_decide(
            facts, command, now, transition_brief_identity, actor_task_id, actor_host_id
        )
        if isinstance(decision_result, DecisionFailure):
            return decision_result
        accepted_decision = decision_result
        allocation = transaction.read_mutation_allocation()
        mutation = project_transition_mutation(allocation, accepted_decision, actor_task_id, actor_host_id)
        return transaction.commit(mutation)


def preflight_checkpoint_candidate(
    store: WorkStore,
    command: decision_models.AcceptCheckpointCommand,
    now: datetime,
) -> DecisionFailure | None:
    """Revalidate action currentness, then reject only candidate mismatch."""

    facts = store.read_decision_facts(_transition_decision_scope(command), now)
    actor_authority = _resolve_actor_authority(facts.snapshot, command.action, now)
    if isinstance(actor_authority, DecisionFailure):
        return actor_authority
    if (failure := validate_supplied_action(facts.snapshot, actor_authority, command.action)) is not None:
        return failure
    return validate_checkpoint_candidate(facts.snapshot, command)


def preflight_covered_completion(
    store: WorkStore,
    command: decision_models.CoveredCompleteCommand,
    now: datetime,
    *,
    actor_task_id: TaskId,
    actor_host_id: HostId,
) -> DecisionFailure | None:
    """Reject stale authority, wrong lifecycle, candidate, or checkpoint coverage before publication."""

    facts = store.read_decision_facts(_transition_decision_scope(command), now)
    result = _validate_supplied_transition_and_decide(facts, command, now, None, actor_task_id, actor_host_id)
    if isinstance(result, DecisionFailure):
        return result
    return None


def decide_and_commit_checkpoint_acceptance(
    store: WorkStore,
    command: decision_models.AcceptCheckpointCommand,
    now: datetime,
    checkpoint_artifacts: CheckpointArtifacts,
    *,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
    transition_brief_identity: WorkBriefIdentity | None = None,
) -> DecisionResult[CommittedEffect]:
    """Validate, decide, and commit checkpoint acceptance with its required artifacts."""

    with store.write() as transaction:
        facts = transaction.read_decision_facts(_transition_decision_scope(command), now)
        decision_result = _validate_supplied_transition_and_decide(
            facts, command, now, transition_brief_identity, actor_task_id, actor_host_id
        )
        if isinstance(decision_result, DecisionFailure):
            return decision_result
        accepted_decision = decision_result
        allocation = transaction.read_checkpoint_mutation_allocation(
            (checkpoint_artifacts.result, checkpoint_artifacts.review, checkpoint_artifacts.package)
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
    actor_task_id: TaskId,
    actor_host_id: HostId,
) -> DecisionResult[CommittedEffect]:
    """Revalidate the authoritative checkpoint set and atomically accept terminal evidence."""

    with store.write() as transaction:
        facts = transaction.read_decision_facts(_transition_decision_scope(command), now)
        decision_result = _validate_supplied_transition_and_decide(
            facts, command, now, None, actor_task_id, actor_host_id
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
