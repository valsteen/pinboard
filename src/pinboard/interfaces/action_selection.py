from dataclasses import dataclass
from datetime import UTC, datetime
from typing import assert_never

from pinboard.application import ports, query_models
from pinboard.application.actions import action_subject_ids, discover_current_actions
from pinboard.domain import decision_models
from pinboard.domain.errors import (
    DecisionFailure,
    EffectDisposition,
    FailureAction,
    FailureDetails,
    FailureFact,
    FailureMismatch,
    RetryDisposition,
)
from pinboard.domain.identifiers import AttemptId, ItemId, LedgerId, ProposalId, SubjectId
from pinboard.domain.ledger import LedgerSnapshot
from pinboard.interfaces import cli_commands
from pinboard.interfaces.errors import CommandErrorCode, CommandFailure, CommandResult


@dataclass(frozen=True, slots=True)
class ParsedActionReceipt:
    action: decision_models.Action
    role: decision_models.MutationRole
    generation: int


def _failure_alternatives(
    actions: tuple[decision_models.Action, ...],
    supplied: ParsedActionReceipt,
) -> tuple[FailureAction, ...]:
    subject = supplied.action.capability.subject
    alternatives: list[FailureAction] = []
    for action in actions:
        capability = action.capability
        if capability.subject != subject:
            continue
        generation = (
            capability.command_authority.generation
            if capability.command_authority is not None
            else capability.preparation_authority.generation
            if capability.preparation_authority is not None
            else None
        )
        alternatives.append(
            FailureAction(
                decision_models.action_id(action),
                supplied.role.value,
                capability.expected_revision,
                capability.subject_revision,
                None if capability.authorization is None else capability.authorization.value,
                None if capability.lease_id is None else str(capability.lease_id),
                generation,
            )
        )
    return tuple(alternatives)


def with_current_alternatives(
    store: ports.WorkStore,
    supplied: ParsedActionReceipt,
    failure: CommandFailure,
) -> CommandFailure:
    """Attach fresh same-subject actions after a locked execution rejection."""
    observed_at = datetime.now(UTC)
    item_ids, attempt_ids, proposal_ids = action_subject_ids(supplied.action)
    current_actions = discover_current_actions(
        store.read_decision_facts(
            query_models.DecisionScope(item_ids, attempt_ids, proposal_ids, ()), observed_at
        ).snapshot,
        supplied.role,
        lease_id=supplied.action.capability.lease_id,
        generation=supplied.generation,
    )
    if isinstance(current_actions, DecisionFailure):
        return failure
    details = (
        FailureDetails(
            observed=(),
            mismatches=(),
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        )
        if failure.details is None
        else failure.details
    )
    return CommandFailure(
        failure.code,
        failure.message,
        FailureDetails(
            observed=details.observed,
            mismatches=details.mismatches,
            retry=details.retry,
            effect=details.effect,
            changed_surfaces=details.changed_surfaces,
            alternatives=_failure_alternatives(current_actions, supplied),
        ),
    )


def _malformed_action_id_failure(action_id: str, message: str) -> CommandFailure:
    return CommandFailure(
        CommandErrorCode.ACTION_ID_MALFORMED,
        message,
        FailureDetails(
            observed=(FailureFact("action_id", action_id),),
            mismatches=(FailureMismatch("action_id", "kind:subject", action_id),),
            retry=RetryDisposition.CORRECT_INPUT,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def parse_action_receipt(  # noqa: C901, PLR0912, PLR0915
    command: cli_commands.TransitionCommand | cli_commands.DispatchCommand,
) -> CommandResult[ParsedActionReceipt]:
    selected_action_id = command.action_id
    if ":" not in selected_action_id:
        return _malformed_action_id_failure(
            str(selected_action_id),
            "Action identity must be 'kind:subject'.",
        )
    kind_value, subject = selected_action_id.split(":", 1)
    if not kind_value or not subject:
        return _malformed_action_id_failure(
            str(selected_action_id),
            "Action identity must contain a non-empty kind and subject.",
        )
    try:
        kind = decision_models.ActionKind(kind_value)
    except ValueError:
        return CommandFailure(
            CommandErrorCode.ACTION_KIND_UNKNOWN,
            f"Unknown action kind: {kind_value!r}.",
            FailureDetails(
                observed=(),
                mismatches=(FailureMismatch("action_kind", "known action kind", kind_value),),
                retry=RetryDisposition.CORRECT_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    match command:
        case cli_commands.ProjectTransitionCommand(subject_revision=subject_revision):
            authorization = decision_models.AuthorizationKind.PROJECT
            role = decision_models.Role.PROJECT
            lease_id = None
            generation = 0
        case cli_commands.AttemptTransitionCommand(lease_id=lease_id, subject_revision=subject_revision):
            authorization = decision_models.AuthorizationKind.ATTEMPT
            role = decision_models.Role.WORKER
            generation = command.generation
        case cli_commands.PreparationTransitionCommand(lease_id=lease_id, subject_revision=subject_revision):
            authorization = decision_models.AuthorizationKind.PREPARATION
            role = decision_models.Role.PREPARER
            generation = command.generation
        case cli_commands.ProjectDispatchCommand() | cli_commands.ProjectReviewedDispatchCommand():
            authorization = decision_models.AuthorizationKind.PROJECT
            role = decision_models.Role.PROJECT
            lease_id = None
            subject_revision = None
            generation = 0
        case _ as unreachable:
            assert_never(unreachable)

    def capability[SubjectT: SubjectId](
        subject_id: SubjectT,
    ) -> decision_models.MutationActionCapability[SubjectT]:
        return decision_models.MutationActionCapability(
            subject=subject_id,
            label=str(selected_action_id),
            expected_revision=command.expected_revision,
            subject_revision=subject_revision,
            authorization=authorization,
            lease_id=lease_id,
        )

    match kind:
        case decision_models.ActionKind.ACCEPT_CHECKPOINT:
            action = decision_models.AcceptCheckpointAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE:
            action = decision_models.AcceptReviewAndContinueAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.ACCEPT_PROPOSAL:
            action = decision_models.AcceptProposalAction(capability(ProposalId(subject)))
        case decision_models.ActionKind.ACTIVATE:
            action = decision_models.ActivateAction(capability(ItemId(subject)))
        case decision_models.ActionKind.BLOCK:
            action = decision_models.BlockAttemptAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.BLOCK_ITEM:
            action = decision_models.BlockItemAction(capability(ItemId(subject)))
        case decision_models.ActionKind.COMPLETE:
            action = decision_models.CompleteAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.CLOSE:
            action = decision_models.CloseAction(capability(ItemId(subject)))
        case decision_models.ActionKind.CONTINUE:
            action = decision_models.ContinueAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.DEFER:
            action = decision_models.DeferAction(capability(ItemId(subject)))
        case decision_models.ActionKind.DISPATCH:
            action = decision_models.DispatchAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.INSPECT:
            action = decision_models.InspectAction(capability(LedgerId(subject)))
        case decision_models.ActionKind.MARK_READY:
            action = decision_models.MarkReadyAction(capability(ItemId(subject)))
        case decision_models.ActionKind.MERGE_PROPOSAL:
            action = decision_models.MergeProposalAction(capability(ProposalId(subject)))
        case decision_models.ActionKind.PAUSE:
            action = decision_models.PauseAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.REJECT_PROPOSAL:
            action = decision_models.RejectProposalAction(capability(ProposalId(subject)))
        case decision_models.ActionKind.REOPEN:
            action = decision_models.ReopenAction(capability(ItemId(subject)))
        case decision_models.ActionKind.REBIND_ATTEMPT:
            action = decision_models.RebindAttemptAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.REPORT_BLOCKER:
            action = decision_models.ReportBlockerAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.RESUME:
            action = decision_models.ResumeAction(capability(ItemId(subject)))
        case decision_models.ActionKind.RETURN_FOR_CORRECTION:
            action = decision_models.ReturnForCorrectionAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.RETURN_PROPOSAL:
            action = decision_models.ReturnProposalAction(capability(ProposalId(subject)))
        case decision_models.ActionKind.REVISE_ITEM:
            action = decision_models.ReviseItemAction(capability(ItemId(subject)))
        case decision_models.ActionKind.SUBMIT_REVIEW:
            action = decision_models.SubmitReviewAction(capability(AttemptId(subject)))
        case _ as unreachable:
            assert_never(unreachable)
    return ParsedActionReceipt(action, role, generation)


def _wrong_authority_failure(
    supplied: ParsedActionReceipt,
    expected_generation: int | None,
    expected_lease_id: str | None,
) -> CommandFailure:
    supplied_lease_id = supplied.action.capability.lease_id
    mismatches = tuple(
        mismatch
        for mismatch in (
            FailureMismatch("generation", expected_generation, supplied.generation),
            FailureMismatch(
                "lease_id",
                expected_lease_id,
                None if supplied_lease_id is None else str(supplied_lease_id),
            ),
        )
        if mismatch.expected != mismatch.observed
    )
    return CommandFailure(
        CommandErrorCode.ACTION_AUTHORITY_WRONG,
        f"Action '{decision_models.action_id(supplied.action)}' does not carry current authority.",
        FailureDetails(
            observed=(
                FailureFact(
                    "authorization",
                    None
                    if supplied.action.capability.authorization is None
                    else supplied.action.capability.authorization.value,
                ),
            ),
            mismatches=mismatches,
            retry=RetryDisposition.REFRESH_ACTION,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def _inactive_authority_failure(
    action_id: str,
    status: str,
    *,
    expired: bool,
    expires_at: datetime,
    operation_time: datetime,
) -> CommandFailure:
    if expired:
        code = CommandErrorCode.ACTION_AUTHORITY_EXPIRED
        observed_status = "expired"
    elif status == "released":
        code = CommandErrorCode.ACTION_AUTHORITY_RELEASED
        observed_status = status
    else:
        code = CommandErrorCode.ACTION_AUTHORITY_WRONG
        observed_status = status
    return CommandFailure(
        code,
        f"Action '{action_id}' authority is {observed_status}.",
        FailureDetails(
            observed=(
                FailureFact("authority_status", observed_status),
                FailureFact("authority_expires_at", expires_at.isoformat()),
                FailureFact("operation_time", operation_time.isoformat()),
            ),
            mismatches=(FailureMismatch("authority_status", "active-unexpired", observed_status),),
            retry=RetryDisposition.REACQUIRE_AUTHORITY,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def _retained_authority_failure(
    supplied: ParsedActionReceipt,
    current_generation: int,
    current_lease_id: str | None,
    status: str,
    expires_at: datetime,
    operation_time: datetime,
) -> CommandFailure | None:
    supplied_lease_id = supplied.action.capability.lease_id
    if (
        supplied.generation != current_generation
        or (None if supplied_lease_id is None else str(supplied_lease_id)) != current_lease_id
    ):
        return _wrong_authority_failure(supplied, current_generation, current_lease_id)
    if status == "active" and expires_at > operation_time:
        return None
    return _inactive_authority_failure(
        str(decision_models.action_id(supplied.action)),
        status,
        expired=status == "expired" or (status == "active" and expires_at <= operation_time),
        expires_at=expires_at,
        operation_time=operation_time,
    )


def _authority_failure(
    store: ports.WorkStore,
    supplied: ParsedActionReceipt,
    operation_time: datetime,
) -> CommandFailure | None:
    capability = supplied.action.capability
    match supplied.role:
        case decision_models.Role.PROJECT:
            return None
        case decision_models.Role.WORKER:
            retained = store.read_attempt_authority_status(AttemptId(str(capability.subject)))
            if retained is None:
                return _wrong_authority_failure(supplied, None, None)
            current_generation = retained.generation
            current_lease_id = str(retained.lease_id)
            status = retained.status.value
            expires_at = retained.expires_at
        case decision_models.Role.PREPARER:
            retained = store.read_preparation_authority_status(ItemId(str(capability.subject)))
            if retained is None:
                return _wrong_authority_failure(supplied, None, None)
            current_generation = retained.generation
            current_lease_id = str(retained.lease_id)
            status = retained.status.value
            expires_at = retained.expires_at
    return _retained_authority_failure(
        supplied,
        current_generation,
        current_lease_id,
        status,
        expires_at,
        operation_time,
    )


def _subject_state(snapshot: LedgerSnapshot, action: decision_models.Action) -> str | None:
    subject = str(action.capability.subject)
    match decision_models.action_semantics(action.kind).subject_kind:
        case decision_models.ActionSubjectKind.ATTEMPT:
            attempt = next(
                (value for value in snapshot.attempts if str(value.attempt) == subject),
                None,
            )
            return None if attempt is None else attempt.state.value
        case decision_models.ActionSubjectKind.ITEM | decision_models.ActionSubjectKind.PROPOSAL:
            item = next(
                (value for value in snapshot.items if str(value.item) == subject),
                None,
            )
            return None if item is None else item.state.value
        case decision_models.ActionSubjectKind.LEDGER:
            return "valid"


def select_current_action(
    store: ports.WorkStore,
    supplied: ParsedActionReceipt,
) -> CommandResult[decision_models.Action]:
    supplied_action = supplied.action
    supplied_capability = supplied_action.capability
    operation_time = datetime.now(UTC)
    if (authority_failure := _authority_failure(store, supplied, operation_time)) is not None:
        return authority_failure
    item_ids, attempt_ids, proposal_ids = action_subject_ids(supplied_action)
    current_snapshot = store.read_decision_facts(
        query_models.DecisionScope(item_ids, attempt_ids, proposal_ids, ()), operation_time
    ).snapshot
    current_actions = discover_current_actions(
        current_snapshot,
        supplied.role,
        lease_id=supplied_capability.lease_id,
        generation=supplied.generation,
    )
    if isinstance(current_actions, DecisionFailure):
        return CommandFailure(current_actions.code, current_actions.message, current_actions.details)
    current_action = next(
        (
            value
            for value in current_actions
            if decision_models.action_id(value) == decision_models.action_id(supplied_action)
        ),
        None,
    )
    alternatives = _failure_alternatives(current_actions, supplied)
    if current_action is None:
        semantics = decision_models.action_semantics(supplied_action.kind)
        observed_state = _subject_state(current_snapshot, supplied_action)
        return CommandFailure(
            CommandErrorCode.ACTION_LIFECYCLE_UNAVAILABLE,
            f"Action '{decision_models.action_id(supplied_action)}' is not currently legal.",
            FailureDetails(
                observed=(FailureFact("subject_state", observed_state),),
                mismatches=(
                    FailureMismatch(
                        "lifecycle_precondition",
                        semantics.lifecycle_precondition.value,
                        observed_state,
                    ),
                ),
                retry=RetryDisposition.REFRESH_ACTION,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=alternatives,
            ),
        )
    current_capability = current_action.capability
    if current_capability.expected_revision != supplied_capability.expected_revision:
        return CommandFailure(
            CommandErrorCode.ACTION_REVISION_STALE,
            "The work ledger changed after this action was selected.",
            FailureDetails(
                observed=(),
                mismatches=(
                    FailureMismatch(
                        "expected_revision",
                        current_capability.expected_revision,
                        supplied_capability.expected_revision,
                    ),
                ),
                retry=RetryDisposition.REFRESH_ACTION,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=alternatives,
            ),
        )
    supplied_authority = (
        supplied_capability.subject_revision,
        supplied_capability.authorization,
        supplied_capability.lease_id,
    )
    current_authority = (
        current_capability.subject_revision,
        current_capability.authorization,
        current_capability.lease_id,
    )
    if current_authority != supplied_authority:
        mismatches = tuple(
            mismatch
            for mismatch in (
                FailureMismatch(
                    "subject_revision",
                    current_capability.subject_revision,
                    supplied_capability.subject_revision,
                ),
                FailureMismatch(
                    "authorization",
                    None if current_capability.authorization is None else current_capability.authorization.value,
                    None if supplied_capability.authorization is None else supplied_capability.authorization.value,
                ),
                FailureMismatch(
                    "lease_id",
                    None if current_capability.lease_id is None else str(current_capability.lease_id),
                    None if supplied_capability.lease_id is None else str(supplied_capability.lease_id),
                ),
            )
            if mismatch.expected != mismatch.observed
        )
        return CommandFailure(
            CommandErrorCode.ACTION_AUTHORITY_WRONG,
            f"Action '{decision_models.action_id(supplied_action)}' no longer has exact current authority.",
            FailureDetails(
                observed=(),
                mismatches=mismatches,
                retry=RetryDisposition.REFRESH_ACTION,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=alternatives,
            ),
        )
    return current_action
