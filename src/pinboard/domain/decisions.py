from dataclasses import dataclass, replace
from datetime import datetime
from typing import assert_never, overload

from pinboard.domain import decision_models, work_models
from pinboard.domain.definition_decisions import decide_definition_revision, introduces_dependency_cycle
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import AttemptId, ItemId, LedgerId, ProposalId, SubjectId
from pinboard.domain.ledger import LedgerSnapshot


@dataclass(frozen=True, slots=True)
class ActionCapabilityFactory:
    revision: str
    actor: decision_models.ActorAuthority

    def make[SubjectT: SubjectId](
        self,
        subject: SubjectT,
        label: str,
        subject_revision: str | None = None,
        command_authority: work_models.CommandAttemptAuthority | None = None,
        preparation_authority: work_models.PreparationCommandAuthority | None = None,
    ) -> decision_models.MutationActionCapability[SubjectT]:
        return decision_models.MutationActionCapability(
            subject=subject,
            label=label,
            expected_revision=self.revision,
            subject_revision=subject_revision,
            authorization=self.actor.authorization,
            lease_id=self.actor.lease_id,
            command_authority=command_authority,
            preparation_authority=preparation_authority,
        )


def _find_attempt_authority(
    snapshot: LedgerSnapshot, actor: decision_models.ActorAuthority, attempt: AttemptId
) -> work_models.AttemptAuthority | None:
    if attempt not in actor.attempts:
        return None
    return snapshot.authority_for(attempt, actor.lease_id, actor.generation)


def _definition_stale(snapshot: LedgerSnapshot, item: work_models.WorkItem) -> bool:
    if item.attempt is None:
        return False
    attempt = snapshot.attempts_by_id().get(item.attempt)
    definition = snapshot.definition(item.item)
    if attempt is None or attempt.accepted_scope_revision is None or definition is None:
        return False
    current_identity = definition.revision, definition.digest
    return (attempt.accepted_scope_revision, attempt.accepted_scope_digest) != current_identity


def _worker_actions(snapshot: LedgerSnapshot, factory: ActionCapabilityFactory) -> tuple[decision_models.Action, ...]:
    result: list[decision_models.Action] = []
    for attempt in factory.actor.attempts:
        item = snapshot.item_for_attempt(attempt)
        authority = _find_attempt_authority(snapshot, factory.actor, attempt)
        if item is None or authority is None or item.state != work_models.WorkState.ACTIVE:
            continue
        command_authority = next(
            (value for value in snapshot.command_attempt_authorities if value.attempt == attempt),
            None,
        )
        revision = snapshot.subject_revision(item.item)
        result.append(
            decision_models.ReportBlockerAction(
                factory.make(attempt, f"Prepare blocker report for {item.item}", revision, command_authority)
            )
        )
        if not _definition_stale(snapshot, item):
            result.extend(
                (
                    decision_models.ContinueAction(
                        factory.make(attempt, f"Continue {item.item}", revision, command_authority)
                    ),
                    decision_models.SubmitReviewAction(
                        factory.make(attempt, f"Submit {item.item} for review", revision, command_authority)
                    ),
                )
            )
    return tuple(result)


def _preparer_actions(snapshot: LedgerSnapshot, factory: ActionCapabilityFactory) -> tuple[decision_models.Action, ...]:
    result: list[decision_models.Action] = []
    for item_id in factory.actor.preparations:
        item = snapshot.item(item_id)
        authority = snapshot.preparation_for(item_id, factory.actor.lease_id, factory.actor.generation)
        command = next(
            (value for value in snapshot.command_preparation_authorities if value.item == item_id),
            None,
        )
        if item is None or authority is None or command is None or item.state != work_models.WorkState.READY:
            continue
        result.append(
            decision_models.ActivateAction(
                factory.make(
                    item.item,
                    f"Activate {item.item}",
                    snapshot.subject_revision(item.item),
                    preparation_authority=command,
                )
            )
        )
    return tuple(result)


def _project_actions(snapshot: LedgerSnapshot, factory: ActionCapabilityFactory) -> list[decision_models.Action]:
    result: list[decision_models.Action] = []
    for item in snapshot.items:
        if item.state not in {work_models.WorkState.ACTIVE, work_models.WorkState.REVIEW} or item.attempt is None:
            continue
        if item.state == work_models.WorkState.ACTIVE:
            if not _definition_stale(snapshot, item):
                result.extend(
                    (
                        decision_models.ContinueAction(factory.make(item.attempt, f"Continue {item.item}")),
                        decision_models.DispatchAction(
                            factory.make(item.attempt, f"Prepare a worker launch for {item.item}")
                        ),
                        decision_models.RebindAttemptAction(
                            factory.make(item.attempt, f"Rebind the Git baseline for {item.item}")
                        ),
                    )
                )
            result.extend(
                (
                    decision_models.PauseAction(factory.make(item.attempt, f"Pause and preserve {item.item}")),
                    decision_models.BlockAttemptAction(
                        factory.make(item.attempt, f"Block active attempt for {item.item}")
                    ),
                )
            )
        if not _definition_stale(snapshot, item):
            result.append(
                decision_models.CompleteAction(factory.make(item.attempt, f"Accept and complete {item.item}"))
            )
        if item.state == work_models.WorkState.REVIEW:
            attempt = snapshot.attempt(item.attempt)
            result.append(
                decision_models.ReturnForCorrectionAction(
                    factory.make(item.attempt, f"Return {item.item} for correction")
                )
            )
            if not _definition_stale(snapshot, item):
                result.append(
                    decision_models.AcceptCheckpointAction(
                        factory.make(item.attempt, f"Accept a checkpoint for {item.item}")
                    )
                )
                if attempt is not None and attempt.state == work_models.AttemptState.REVIEW:
                    result.append(
                        decision_models.AcceptReviewAndContinueAction(
                            factory.make(item.attempt, f"Accept the review and continue {item.item}")
                        )
                    )
    return result


def _item_actions(
    snapshot: LedgerSnapshot, item: work_models.WorkItem, factory: ActionCapabilityFactory
) -> list[decision_models.Action]:
    close = decision_models.CloseAction(factory.make(item.item, f"Record a terminal decision for {item.item}"))
    if item.state == work_models.WorkState.INTAKE:
        return [
            decision_models.MarkReadyAction(factory.make(item.item, f"Mark {item.item} ready")),
            decision_models.BlockItemAction(factory.make(item.item, f"Block unstarted work item {item.item}")),
            decision_models.DeferAction(factory.make(item.item, f"Defer {item.item} with a reopen condition")),
            close,
        ]
    if item.state == work_models.WorkState.READY:
        return [
            decision_models.DeferAction(factory.make(item.item, f"Defer {item.item} with a reopen condition")),
            close,
        ]
    dependencies_live = any(dependency in snapshot.items_by_id() for dependency in item.depends_on)
    if item.state == work_models.WorkState.PAUSED and item.attempt is not None:
        result: list[decision_models.Action] = []
        if not _definition_stale(snapshot, item):
            result.append(
                decision_models.RebindAttemptAction(
                    factory.make(item.attempt, f"Rebind the Git baseline for {item.item}")
                )
            )
        if not dependencies_live:
            result.append(decision_models.ResumeAction(factory.make(item.item, f"Return {item.item} to active")))
        return [*result, close]
    if item.state in {work_models.WorkState.PAUSED, work_models.WorkState.BLOCKED} and not dependencies_live:
        target = "active" if item.attempt is not None else "ready"
        result: list[decision_models.Action] = [
            decision_models.ResumeAction(factory.make(item.item, f"Return {item.item} to {target}"))
        ]
        if item.attempt is None:
            result.append(
                decision_models.DeferAction(factory.make(item.item, f"Defer {item.item} with a reopen condition"))
            )
        return [*result, close]
    if item.state in {work_models.WorkState.PAUSED, work_models.WorkState.BLOCKED}:
        return [close]
    if item.state == work_models.WorkState.DEFERRED:
        return [decision_models.ReopenAction(factory.make(item.item, f"Reopen {item.item} for intake")), close]
    return []


def available_actions(
    snapshot: LedgerSnapshot, actor: decision_models.ActionActorAuthority
) -> DecisionResult[tuple[decision_models.Action, ...]]:
    revision = snapshot.revision if actor.revision_scoped else ""
    match actor:
        case decision_models.ObserverActorAuthority():
            return (
                decision_models.InspectAction(
                    decision_models.ActionCapability(
                        LedgerId("ledger"),
                        "Inspect current work",
                        revision,
                        authorization=actor.authorization,
                    )
                ),
            )
        case decision_models.ActorAuthority():
            factory = ActionCapabilityFactory(revision, actor)
            match actor.role:
                case decision_models.Role.WORKER:
                    result = _worker_actions(snapshot, factory)
                    if not result:
                        return DecisionFailure(
                            DecisionFailureCode.ATTEMPT_LEASE_REQUIRED,
                            "The supplied attempt lease is not current for an active item.",
                        )
                    return result
                case decision_models.Role.PREPARER:
                    result = _preparer_actions(snapshot, factory)
                    if not result:
                        return DecisionFailure(
                            DecisionFailureCode.ACTION_NOT_AVAILABLE,
                            "The supplied preparation lease is not current for a ready item.",
                        )
                    return result
                case decision_models.Role.PROJECT:
                    result = _project_actions(snapshot, factory)
                    for item in snapshot.items:
                        if any(authority.item == item.item for authority in snapshot.command_preparation_authorities):
                            continue
                        result.append(
                            decision_models.ReviseItemAction(
                                factory.make(item.item, f"Revise the accepted definition for {item.item}")
                            )
                        )
                        result.extend(_item_actions(snapshot, item, factory))
                    for proposal in snapshot.proposals:
                        result.extend(
                            (
                                decision_models.AcceptProposalAction(
                                    factory.make(
                                        proposal.proposal,
                                        f"Accept proposal {proposal.proposal}",
                                        proposal.revision,
                                    )
                                ),
                                decision_models.MergeProposalAction(
                                    factory.make(
                                        proposal.proposal,
                                        f"Merge proposal {proposal.proposal}",
                                        proposal.revision,
                                    )
                                ),
                                decision_models.ReturnProposalAction(
                                    factory.make(
                                        proposal.proposal,
                                        f"Return proposal {proposal.proposal}",
                                        proposal.revision,
                                    )
                                ),
                                decision_models.RejectProposalAction(
                                    factory.make(
                                        proposal.proposal,
                                        f"Reject proposal {proposal.proposal}",
                                        proposal.revision,
                                    )
                                ),
                            )
                        )
                    return tuple(result)
                case _ as unreachable:
                    assert_never(unreachable)
        case _ as unreachable:
            assert_never(unreachable)


def validate_supplied_action(
    snapshot: LedgerSnapshot, actor: decision_models.ActionActorAuthority, supplied: decision_models.Action
) -> DecisionFailure | None:
    """Reject an action that no longer matches its current subject-scoped mutation authority."""

    available = available_actions(snapshot, actor)
    if isinstance(available, DecisionFailure):
        return available
    current = next(
        (
            candidate
            for candidate in available
            if decision_models.action_id(candidate) == decision_models.action_id(supplied)
        ),
        None,
    )
    if current is None:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Action '{decision_models.action_id(supplied)}' is no longer legal.",
        )
    supplied_capability = supplied.capability
    current_capability = current.capability
    if supplied_capability.authorization in {
        decision_models.AuthorizationKind.ATTEMPT,
        decision_models.AuthorizationKind.PREPARATION,
    }:
        capability_matches = (
            supplied_capability.label == current_capability.label
            and supplied_capability.subject_revision == current_capability.subject_revision
            and supplied_capability.authorization == current_capability.authorization
            and supplied_capability.lease_id == current_capability.lease_id
            and supplied_capability.command_authority == current_capability.command_authority
            and supplied_capability.preparation_authority == current_capability.preparation_authority
        )
    else:
        capability_matches = supplied_capability == current_capability
    if not capability_matches:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Action '{decision_models.action_id(supplied)}' no longer carries the exact current authority.",
        )
    return None


def _build_transition_receipt(
    action: decision_models.TransitionAction,
    item: ItemId | None,
    outcome: str,
    evidence: str | None,
    now: datetime,
) -> decision_models.TransitionReceipt:
    return decision_models.TransitionReceipt(decision_models.action_id(action), item, outcome, evidence, now)


def _accepted_transition_decision(
    action: decision_models.NonCheckpointTransitionAction,
    now: datetime,
    change: decision_models.NonCheckpointDecisionChange,
    *,
    item: ItemId | None = None,
    outcome: str | None = None,
    evidence: str | None = None,
) -> decision_models.TransitionDecision:
    return decision_models.TransitionDecision(
        action,
        change,
        _build_transition_receipt(action, item, outcome or action.kind.value, evidence, now),
    )


def _accepted_checkpoint_decision(
    action: decision_models.AcceptCheckpointAction,
    now: datetime,
    change: decision_models.CheckpointAcceptanceChange,
    *,
    item: ItemId,
    evidence: str,
) -> decision_models.CheckpointAcceptanceDecision:
    return decision_models.CheckpointAcceptanceDecision(
        action,
        change,
        _build_transition_receipt(action, item, action.kind.value, evidence, now),
    )


def _activate(
    snapshot: LedgerSnapshot, command: decision_models.ActivateCommand, now: datetime
) -> DecisionResult[decision_models.TransitionDecision]:
    action = command.action
    value = command.value
    item_id = action.capability.subject
    item = snapshot.item(item_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    if item.state != work_models.WorkState.READY:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' is not ready for activation."
        )
    preparation = action.capability.preparation_authority
    definition = snapshot.definition(item.item)
    if (
        preparation is None
        or definition is None
        or (
            preparation.item,
            preparation.definition_revision,
            preparation.definition_digest,
        )
        != (item.item, definition.revision, definition.digest)
    ):
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Activation requires the exact live preparation authority and definition pin.",
        )
    artifact = next(
        (candidate for candidate in snapshot.artifacts if candidate.artifact_ref_id == value.brief_artifact_ref_id),
        None,
    )
    if artifact is None or artifact.kind != work_models.ArtifactKind.BRIEF:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Activation requires one existing brief artifact reference.",
        )
    return _accepted_transition_decision(
        action,
        now,
        decision_models.ActivationChange(
            item.item,
            item.state,
            value.attempt,
            value.brief_artifact_ref_id,
            value.branch,
            value.base_revision,
            value.owner,
        ),
        item=item.item,
    )


def _block_dependencies(
    snapshot: LedgerSnapshot,
    item: work_models.WorkItem,
    value: work_models.BlockInput,
) -> DecisionResult[tuple[ItemId, ...]]:
    if not value.depends_on:
        return item.depends_on
    definition = snapshot.definition(item.item)
    if definition is None:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            "The blocked item has no current definition.",
        )
    if any(dependency not in definition.definition.dependencies for dependency in value.depends_on):
        return DecisionFailure(
            DecisionFailureCode.DEPENDENCY_NOT_SATISFIED,
            "Blocker dependencies must be identities from the current definition.",
        )
    return item.depends_on


def _pause_or_block(
    snapshot: LedgerSnapshot,
    command: decision_models.PauseCommand | decision_models.BlockCommand,
    now: datetime,
) -> DecisionResult[decision_models.TransitionDecision]:
    action = command.action
    attempt_id = action.capability.subject
    item = snapshot.item_for_attempt(attempt_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    if item.state != work_models.WorkState.ACTIVE:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "The named attempt is not active.")
    match command:
        case decision_models.PauseCommand():
            change: decision_models.NonCheckpointDecisionChange = decision_models.AttemptStateChange(
                item.item,
                item.state,
                work_models.WorkState.PAUSED,
                attempt_id,
                work_models.AttemptState.ACTIVE,
                work_models.AttemptState.PAUSED,
            )
        case decision_models.BlockCommand(value=value):
            dependencies = _block_dependencies(snapshot, item, value)
            if isinstance(dependencies, DecisionFailure):
                return dependencies
            change = decision_models.BlockAttemptChange(
                item.item,
                item.state,
                attempt_id,
                work_models.AttemptState.ACTIVE,
                dependencies,
            )
        case _ as unreachable:
            assert_never(unreachable)
    return _accepted_transition_decision(
        action,
        now,
        change,
        item=item.item,
    )


def _fence_retained_attempt_authority(
    snapshot: LedgerSnapshot,
    attempt: AttemptId,
) -> decision_models.AttemptAuthorityChange | None:
    authority = next((value for value in snapshot.attempt_authorities if value.attempt == attempt), None)
    if authority is None:
        return None
    return decision_models.AttemptAuthorityChange(
        authority,
        replace(authority, lease_id=None, generation=authority.generation + 1),
    )


def _complete(
    snapshot: LedgerSnapshot, command: decision_models.CompleteCommand, now: datetime
) -> DecisionResult[decision_models.TransitionDecision]:
    action = command.action
    value = command.value
    attempt_id = action.capability.subject
    item = snapshot.item_for_attempt(attempt_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    if item.state not in {work_models.WorkState.ACTIVE, work_models.WorkState.REVIEW}:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, "The named attempt is not active or in review."
        )
    if _definition_stale(snapshot, item):
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_STALE,
            "The attempt has not accepted the item's current definition.",
        )
    if item.item in snapshot.history_items:
        return DecisionFailure(DecisionFailureCode.HISTORY_RECORD_EXISTS, f"History already contains '{item.item}'.")
    before = (
        work_models.AttemptState.REVIEW
        if item.state == work_models.WorkState.REVIEW
        else work_models.AttemptState.ACTIVE
    )
    authority_change = _fence_retained_attempt_authority(snapshot, attempt_id)
    return _accepted_transition_decision(
        action,
        now,
        decision_models.CompletionChange(item.item, item.state, attempt_id, before, value.evidence, authority_change),
        item=item.item,
        evidence=value.evidence,
    )


def _close(
    snapshot: LedgerSnapshot, command: decision_models.CloseCommand, now: datetime
) -> DecisionResult[decision_models.TransitionDecision]:
    action = command.action
    value = command.value
    item_id = action.capability.subject
    item = snapshot.item(item_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    if item.state in {work_models.WorkState.ACTIVE, work_models.WorkState.REVIEW}:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, "Active or review work requires the acceptance path."
        )
    if value.outcome == work_models.CloseOutcome.DROPPED and any(
        item.item in candidate.depends_on for candidate in snapshot.items
    ):
        return DecisionFailure(DecisionFailureCode.LIVE_DEPENDENTS, f"Item '{item.item}' still has live dependents.")
    if item.item in snapshot.history_items:
        return DecisionFailure(DecisionFailureCode.HISTORY_RECORD_EXISTS, f"History already contains '{item.item}'.")
    authority_change = None if item.attempt is None else _fence_retained_attempt_authority(snapshot, item.attempt)
    if item.attempt is None:
        change: decision_models.NonCheckpointDecisionChange = decision_models.ItemClosureChange(
            item.item, item.state, value.outcome, value.reason
        )
    else:
        attempt = snapshot.attempts_by_id().get(item.attempt)
        if attempt is None:
            return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{item.attempt}' does not exist.")
        change = decision_models.AttemptClosureChange(
            item.item,
            item.state,
            value.outcome,
            value.reason,
            item.attempt,
            attempt.state,
            authority_change,
        )
    return _accepted_transition_decision(
        action,
        now,
        change,
        item=item.item,
        outcome=value.outcome.value,
        evidence=value.reason,
    )


def _resume(
    snapshot: LedgerSnapshot, command: decision_models.ResumeCommand, now: datetime
) -> DecisionResult[decision_models.TransitionDecision]:
    action = command.action
    value = command.value
    item_id = action.capability.subject
    item = snapshot.item(item_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    if item.state not in {work_models.WorkState.PAUSED, work_models.WorkState.BLOCKED}:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' is not paused or blocked."
        )
    if any(dependency in snapshot.items_by_id() for dependency in item.depends_on):
        return DecisionFailure(
            DecisionFailureCode.DEPENDENCY_NOT_SATISFIED, f"Item '{item.item}' still has a live dependency."
        )
    revised_brief: decision_models.RevisedAttemptBrief | None = None
    if value.brief_artifact_ref_id is not None:
        if item.attempt is None:
            return DecisionFailure(
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                "Resuming with a revised brief requires an existing attempt.",
            )
        artifact = next(
            (candidate for candidate in snapshot.artifacts if candidate.artifact_ref_id == value.brief_artifact_ref_id),
            None,
        )
        if artifact is None or artifact.kind != work_models.ArtifactKind.BRIEF:
            return DecisionFailure(
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                "Resuming with a revised brief requires one existing brief artifact reference.",
            )
        definition = snapshot.definition(item.item)
        if definition is None:
            return DecisionFailure(
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                "Resuming with a revised brief requires the item's accepted definition.",
            )
        revised_brief = decision_models.RevisedAttemptBrief(
            value.brief_artifact_ref_id,
            definition.revision,
            definition.digest,
        )
    target = work_models.WorkState.ACTIVE if item.attempt is not None else work_models.WorkState.READY
    if item.attempt is not None:
        before = (
            work_models.AttemptState.PAUSED
            if item.state == work_models.WorkState.PAUSED
            else work_models.AttemptState.BLOCKED
        )
        change: decision_models.NonCheckpointDecisionChange = decision_models.ResumeAttemptChange(
            item.item,
            item.state,
            item.attempt,
            before,
            revised_brief,
        )
    else:
        change = decision_models.ItemStateChange(item.item, item.state, target)
    return _accepted_transition_decision(
        action,
        now,
        change,
        item=item.item,
    )


def _rebind_attempt(
    snapshot: LedgerSnapshot,
    command: decision_models.RebindAttemptCommand,
    now: datetime,
) -> DecisionResult[decision_models.TransitionDecision]:
    action = command.action
    value = command.value
    attempt_id = action.capability.subject
    if value.attempt != attempt_id:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "The rebind payload must name the selected attempt.",
        )
    item = snapshot.item_for_attempt(attempt_id)
    attempt = snapshot.attempt(attempt_id)
    if item is None or attempt is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    match item.state:
        case work_models.WorkState.ACTIVE:
            expected_attempt_state = work_models.AttemptState.ACTIVE
        case work_models.WorkState.PAUSED:
            expected_attempt_state = work_models.AttemptState.PAUSED
        case (
            work_models.WorkState.INTAKE
            | work_models.WorkState.READY
            | work_models.WorkState.BLOCKED
            | work_models.WorkState.DEFERRED
            | work_models.WorkState.REVIEW
        ):
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_AVAILABLE,
                "Only an active or paused attempt can be rebound.",
            )
        case _ as unreachable:
            assert_never(unreachable)
    if attempt.state != expected_attempt_state:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Only an active or paused attempt can be rebound.",
        )
    definition = snapshot.definition(item.item)
    if (
        definition is None
        or attempt.accepted_scope_revision is None
        or attempt.accepted_scope_digest is None
        or (attempt.accepted_scope_revision, attempt.accepted_scope_digest) != (definition.revision, definition.digest)
    ):
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_STALE,
            "The attempt has not accepted the item's current definition.",
        )
    artifact = next(
        (candidate for candidate in snapshot.artifacts if candidate.artifact_ref_id == value.brief_artifact_ref_id),
        None,
    )
    if artifact is None or artifact.kind != work_models.ArtifactKind.BRIEF:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Rebinding requires one existing brief artifact reference.",
        )
    authorities = tuple(candidate for candidate in snapshot.attempt_authorities if candidate.attempt == attempt_id)
    if len(authorities) != 1:
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Rebinding requires exactly one current attempt-authority record to fence.",
        )
    authority = authorities[0]
    return _accepted_transition_decision(
        action,
        now,
        decision_models.RebindAttemptChange(
            item.item,
            attempt_id,
            expected_attempt_state,
            value.branch,
            value.base_revision,
            value.brief_artifact_ref_id,
            definition.revision,
            definition.digest,
            decision_models.AttemptAuthorityChange(
                authority,
                replace(authority, lease_id=None, generation=authority.generation + 1),
            ),
        ),
        item=item.item,
    )


def _submit_review(
    snapshot: LedgerSnapshot, command: decision_models.SubmitReviewCommand, now: datetime
) -> DecisionResult[decision_models.TransitionDecision]:
    action = command.action
    value = command.value
    attempt_id = action.capability.subject
    item = snapshot.item_for_attempt(attempt_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    if item.state != work_models.WorkState.ACTIVE:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, "Only an active attempt can be submitted for review."
        )
    if _definition_stale(snapshot, item):
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_STALE,
            "The attempt has not accepted the item's current definition.",
        )
    attempt = snapshot.attempt(attempt_id)
    if attempt is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    return _accepted_transition_decision(
        action,
        now,
        decision_models.ReviewSubmissionChange(
            item.item,
            attempt_id,
            value.candidate,
            now,
        ),
        item=item.item,
    )


def _return_for_correction(
    snapshot: LedgerSnapshot,
    command: decision_models.ReturnForCorrectionCommand,
    now: datetime,
) -> DecisionResult[decision_models.TransitionDecision]:
    action = command.action
    value = command.value
    attempt_id = action.capability.subject
    item = snapshot.item_for_attempt(attempt_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    if item.state != work_models.WorkState.REVIEW:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, "Only an attempt in review can be returned for correction."
        )
    authorities = tuple(candidate for candidate in snapshot.attempt_authorities if candidate.attempt == attempt_id)
    if len(authorities) != 1:
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Returning a review requires exactly one current attempt-authority record to fence.",
        )
    authority = authorities[0]
    authority_change = decision_models.AttemptAuthorityChange(
        authority,
        replace(authority, lease_id=None, generation=authority.generation + 1),
    )
    return _accepted_transition_decision(
        action,
        now,
        decision_models.ReviewReturnChange(
            item.item,
            attempt_id,
            authority_change,
        ),
        item=item.item,
        evidence=value.reason,
    )


def _accept_checkpoint(
    snapshot: LedgerSnapshot,
    command: decision_models.AcceptCheckpointCommand,
    now: datetime,
) -> DecisionResult[decision_models.CheckpointAcceptanceDecision]:
    action = command.action
    value = command.value
    attempt_id = action.capability.subject
    item = snapshot.item_for_attempt(attempt_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    if item.state != work_models.WorkState.REVIEW:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Only an attempt in review can have a checkpoint accepted.",
        )
    if _definition_stale(snapshot, item):
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_STALE,
            "The attempt has not accepted the item's current definition.",
        )
    authorities = tuple(candidate for candidate in snapshot.attempt_authorities if candidate.attempt == attempt_id)
    if len(authorities) != 1:
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Checkpoint acceptance requires exactly one current attempt-authority record to fence.",
        )
    authority = authorities[0]
    authority_change = decision_models.AttemptAuthorityChange(
        authority,
        replace(authority, lease_id=None, generation=authority.generation + 1),
    )
    return _accepted_checkpoint_decision(
        action,
        now,
        decision_models.CheckpointAcceptanceChange(
            item.item,
            value.checkpoint,
            attempt_id,
            value.candidate,
            authority_change,
        ),
        item=item.item,
        evidence=value.evidence,
    )


def _accept_review_and_continue(
    snapshot: LedgerSnapshot,
    command: decision_models.AcceptReviewAndContinueCommand,
    now: datetime,
) -> DecisionResult[decision_models.TransitionDecision]:
    action = command.action
    value = command.value
    attempt_id = action.capability.subject
    item = snapshot.item_for_attempt(attempt_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    if item.state != work_models.WorkState.REVIEW:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Only an item in review can have its review accepted for continuation.",
        )
    if _definition_stale(snapshot, item):
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_STALE,
            "The attempt has not accepted the item's current definition.",
        )
    attempt = snapshot.attempt(attempt_id)
    if attempt is None or attempt.state != work_models.AttemptState.REVIEW:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Only an attempt in review can have its review accepted for continuation.",
        )
    if attempt.protected_candidate_revision != value.candidate:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Review continuation requires the exact protected candidate.",
        )
    authorities = tuple(candidate for candidate in snapshot.attempt_authorities if candidate.attempt == attempt_id)
    if len(authorities) != 1:
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Review continuation requires exactly one current attempt-authority record to fence.",
        )
    authority = authorities[0]
    authority_change = decision_models.AttemptAuthorityChange(
        authority,
        replace(authority, lease_id=None, generation=authority.generation + 1),
    )
    return _accepted_transition_decision(
        action,
        now,
        decision_models.ReviewAcceptanceChange(item.item, attempt_id, value.candidate, authority_change),
        item=item.item,
        evidence=value.evidence,
    )


def _block_item(
    snapshot: LedgerSnapshot, command: decision_models.BlockItemCommand, now: datetime
) -> DecisionResult[decision_models.TransitionDecision]:
    action = command.action
    item_id = action.capability.subject
    item = snapshot.item(item_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    if item.state != work_models.WorkState.INTAKE:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Item '{item.item}' cannot perform '{action.kind.value}' now.",
        )
    dependencies = _block_dependencies(snapshot, item, command.value)
    if isinstance(dependencies, DecisionFailure):
        return dependencies
    return _accepted_transition_decision(
        action,
        now,
        decision_models.BlockItemChange(item.item, item.state, dependencies),
        item=item.item,
    )


def _simple_item_transition(
    snapshot: LedgerSnapshot,
    command: decision_models.ReopenCommand | decision_models.MarkReadyCommand,
    now: datetime,
) -> DecisionResult[decision_models.TransitionDecision]:
    action = command.action
    match command:
        case decision_models.ReopenCommand():
            expected = (work_models.WorkState.DEFERRED,)
            target = work_models.WorkState.INTAKE
        case decision_models.MarkReadyCommand():
            expected = (work_models.WorkState.INTAKE,)
            target = work_models.WorkState.READY
        case _ as unreachable:
            assert_never(unreachable)
    item_id = action.capability.subject
    item = snapshot.item(item_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    if item.state not in expected:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Item '{item.item}' cannot perform '{action.kind.value}' now.",
        )
    return _accepted_transition_decision(
        action, now, decision_models.ItemStateChange(item.item, item.state, target), item=item.item
    )


def _defer(
    snapshot: LedgerSnapshot, command: decision_models.DeferCommand, now: datetime
) -> DecisionResult[decision_models.TransitionDecision]:
    action = command.action
    item_id = action.capability.subject
    item = snapshot.item(item_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    if (
        item.state not in {work_models.WorkState.INTAKE, work_models.WorkState.READY, work_models.WorkState.BLOCKED}
        or item.attempt is not None
    ):
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' cannot be deferred now.")
    return _accepted_transition_decision(
        action,
        now,
        decision_models.ItemStateChange(item.item, item.state, work_models.WorkState.DEFERRED),
        item=item.item,
    )


def _require_current_intake_proposal(
    snapshot: LedgerSnapshot,
    proposal_id: ProposalId,
    unavailable_message: str,
) -> DecisionResult[tuple[work_models.ProposalRecord, work_models.WorkItem]]:
    proposal = snapshot.proposal(proposal_id)
    if proposal is None:
        return DecisionFailure(DecisionFailureCode.PROPOSAL_NOT_FOUND, f"Proposal '{proposal_id}' does not exist.")
    item = snapshot.item(ItemId(proposal_id))
    if item is None or item.state != work_models.WorkState.INTAKE or item.attempt is not None:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, unavailable_message)
    return proposal, item


def _accept_proposal(
    snapshot: LedgerSnapshot,
    command: decision_models.AcceptProposalCommand,
    now: datetime,
) -> DecisionResult[decision_models.TransitionDecision]:
    action = command.action
    value = command.value
    proposal_id = action.capability.subject
    current_proposal = _require_current_intake_proposal(
        snapshot, proposal_id, "Only a current intake proposal can be accepted."
    )
    if isinstance(current_proposal, DecisionFailure):
        return current_proposal
    proposal, current_item = current_proposal
    if value.item != ItemId(proposal_id):
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "An intake proposal must be accepted with its same work-item identity.",
        )
    dependencies = tuple(dict.fromkeys((*current_item.depends_on, *value.depends_on)))
    if any(
        snapshot.item(dependency) is None and dependency not in snapshot.history_items for dependency in dependencies
    ):
        return DecisionFailure(
            DecisionFailureCode.DEPENDENCY_NOT_SATISFIED,
            "Accepted proposal dependencies must be existing identities.",
        )
    if introduces_dependency_cycle(snapshot, value.item, dependencies):
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEPENDENCY_CYCLE,
            "Accepted proposal dependencies must not introduce a cycle.",
        )
    current_definition = snapshot.definition(value.item)
    if current_definition is None:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            "The accepted proposal item has no current definition.",
        )
    accepted_definition = replace(current_definition.definition, dependencies=dependencies)
    definition_digest = work_item_definition_digest(accepted_definition)
    if isinstance(definition_digest, DecisionFailure):
        return definition_digest
    accepted_item = decision_models.AcceptedProposalItem(
        value.item,
        value.state,
        value.timing,
        value.next_action,
        dependencies,
        f"proposal:{proposal.proposal}",
        proposal.urgency_evidence,
        current_definition.revision + (definition_digest != current_definition.digest),
        current_definition.digest,
        definition_digest,
        accepted_definition,
        proposal.source_task_id,
    )
    return _accepted_transition_decision(
        action,
        now,
        decision_models.AcceptedProposalChange(proposal.proposal, now, accepted_item),
        item=value.item,
    )


def _merge_proposal(
    snapshot: LedgerSnapshot,
    command: decision_models.MergeProposalCommand,
    now: datetime,
) -> DecisionResult[decision_models.TransitionDecision]:
    action = command.action
    value = command.value
    proposal_id = action.capability.subject
    current_proposal = _require_current_intake_proposal(
        snapshot, proposal_id, "Only a current intake proposal can be merged."
    )
    if isinstance(current_proposal, DecisionFailure):
        return current_proposal
    proposal, _item = current_proposal
    if snapshot.item(value.target) is None and value.target not in snapshot.history_items:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{value.target}' does not exist.")
    return _accepted_transition_decision(
        action,
        now,
        decision_models.MergedProposalChange(proposal.proposal, value.target, now),
    )


def _dispose_proposal(
    snapshot: LedgerSnapshot,
    command: decision_models.ReturnProposalCommand | decision_models.RejectProposalCommand,
    now: datetime,
) -> DecisionResult[decision_models.TransitionDecision]:
    action = command.action
    proposal_id = action.capability.subject
    current_proposal = _require_current_intake_proposal(
        snapshot,
        proposal_id,
        "Only a current intake proposal can be returned or rejected.",
    )
    if isinstance(current_proposal, DecisionFailure):
        return current_proposal
    proposal, _item = current_proposal
    match command:
        case decision_models.ReturnProposalCommand(value=value):
            change: decision_models.ReturnedProposalChange | decision_models.RejectedProposalChange = (
                decision_models.ReturnedProposalChange(proposal.proposal, value.reason, now)
            )
        case decision_models.RejectProposalCommand(value=value):
            change = decision_models.RejectedProposalChange(proposal.proposal, value.reason, now)
        case _ as unreachable:
            assert_never(unreachable)
    return _accepted_transition_decision(
        action,
        now,
        change,
        evidence=value.reason,
    )


def _revise_item(
    snapshot: LedgerSnapshot,
    command: decision_models.ReviseItemCommand,
    now: datetime,
) -> DecisionResult[decision_models.TransitionDecision]:
    revision = decide_definition_revision(snapshot, command.action.capability.subject, command.value, now)
    if isinstance(revision, DecisionFailure):
        return revision
    return _accepted_transition_decision(
        command.action,
        now,
        revision,
        item=revision.item,
        evidence=revision.reason,
    )


@overload
def decide(
    snapshot: LedgerSnapshot,
    command: decision_models.AcceptCheckpointCommand,
    now: datetime,
) -> DecisionResult[decision_models.CheckpointAcceptanceDecision]: ...


@overload
def decide(
    snapshot: LedgerSnapshot,
    command: decision_models.NonCheckpointTransitionCommand,
    now: datetime,
) -> DecisionResult[decision_models.TransitionDecision]: ...


@overload
def decide(
    snapshot: LedgerSnapshot,
    command: decision_models.TransitionCommand,
    now: datetime,
) -> DecisionResult[decision_models.Decision]: ...


def decide(  # noqa: C901, PLR0912
    snapshot: LedgerSnapshot,
    command: decision_models.TransitionCommand,
    now: datetime,
) -> DecisionResult[decision_models.Decision]:
    match command:
        case decision_models.AcceptCheckpointCommand():
            return _accept_checkpoint(snapshot, command, now)
        case decision_models.AcceptReviewAndContinueCommand():
            return _accept_review_and_continue(snapshot, command, now)
        case decision_models.ActivateCommand():
            return _activate(snapshot, command, now)
        case decision_models.PauseCommand() | decision_models.BlockCommand():
            return _pause_or_block(snapshot, command, now)
        case decision_models.CompleteCommand():
            return _complete(snapshot, command, now)
        case decision_models.CloseCommand():
            return _close(snapshot, command, now)
        case decision_models.RebindAttemptCommand():
            return _rebind_attempt(snapshot, command, now)
        case decision_models.ResumeCommand():
            return _resume(snapshot, command, now)
        case decision_models.SubmitReviewCommand():
            return _submit_review(snapshot, command, now)
        case decision_models.ReturnForCorrectionCommand():
            return _return_for_correction(snapshot, command, now)
        case decision_models.BlockItemCommand():
            return _block_item(snapshot, command, now)
        case decision_models.ReopenCommand() | decision_models.MarkReadyCommand():
            return _simple_item_transition(snapshot, command, now)
        case decision_models.DeferCommand():
            return _defer(snapshot, command, now)
        case decision_models.AcceptProposalCommand():
            return _accept_proposal(snapshot, command, now)
        case decision_models.MergeProposalCommand():
            return _merge_proposal(snapshot, command, now)
        case decision_models.ReturnProposalCommand() | decision_models.RejectProposalCommand():
            return _dispose_proposal(snapshot, command, now)
        case decision_models.ReviseItemCommand():
            return _revise_item(snapshot, command, now)
        case _ as unreachable:
            assert_never(unreachable)
