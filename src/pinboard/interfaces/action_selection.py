from dataclasses import dataclass
from datetime import UTC, datetime
from typing import assert_never

from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.actions import discover_actions
from pinboard.domain import decision_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import AttemptId, ItemId, LedgerId, ProposalId, SubjectId
from pinboard.interfaces import cli_commands
from pinboard.interfaces.errors import CommandErrorCode, CommandFailure, CommandResult


@dataclass(frozen=True, slots=True)
class ParsedActionReceipt:
    action: decision_models.Action
    role: decision_models.MutationRole
    generation: int


def parse_action_receipt(  # noqa: C901, PLR0912, PLR0915
    command: cli_commands.TransitionCommand | cli_commands.DispatchCommand,
) -> CommandResult[ParsedActionReceipt]:
    selected_action_id = command.action_id
    if ":" not in selected_action_id:
        return CommandFailure(CommandErrorCode.ACTION_ID_INVALID, "Action identity must be 'kind:subject'.")
    kind_value, subject = selected_action_id.split(":", 1)
    try:
        kind = decision_models.ActionKind(kind_value)
    except ValueError as error:
        return CommandFailure(CommandErrorCode.ACTION_ID_INVALID, f"Unknown action kind: {error}.")
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


def select_current_action(
    roots: cli_commands.ResolvedRoots,
    supplied: ParsedActionReceipt,
) -> CommandResult[decision_models.Action]:
    supplied_action = supplied.action
    supplied_capability = supplied_action.capability
    operation_time = datetime.now(UTC)
    current_state = SQLiteWorkStore(roots.work / "state.sqlite3").snapshot()
    current_actions = discover_actions(
        current_state,
        supplied.role,
        lease_id=supplied_capability.lease_id,
        generation=supplied.generation,
        now=operation_time,
    )
    if isinstance(current_actions, DecisionFailure):
        return CommandFailure(current_actions.code, current_actions.message)
    current_action = next(
        (
            value
            for value in current_actions
            if decision_models.action_id(value) == decision_models.action_id(supplied_action)
        ),
        None,
    )
    if current_action is None:
        return CommandFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Action '{decision_models.action_id(supplied_action)}' is not currently legal.",
        )
    current_capability = current_action.capability
    if current_capability.expected_revision != supplied_capability.expected_revision:
        return CommandFailure(CommandErrorCode.STALE_ACTION, "The work ledger changed after this action was selected.")
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
        return CommandFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Action '{decision_models.action_id(supplied_action)}' no longer has exact current authority.",
        )
    return current_action
