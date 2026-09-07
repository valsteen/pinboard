"""Installed command router and process exit boundary.

This module owns the single exhaustive command-family branch and final rendering
of typed failures. Command grammar and use-case composition live with their
thematic interface owners; this root performs no storage or domain work itself.
"""

import contextlib
import io
import sys
from collections.abc import Sequence
from typing import assert_never

from pinboard.adapters.files.errors import ArtifactError, FileIOError, RootError
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.domain.errors import (
    ArtifactAcceptanceAfterPublicationError,
    ChangedSurface,
    DecisionFailureCode,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    RetryDisposition,
)
from pinboard.interfaces import (
    attempt_authority,
    brief_source_commands,
    cli_commands,
    cli_output,
    cli_parser,
    dispatch_brief,
    preparation_authority,
    project_handover,
    proposal_commands,
    tool_contract,
    transitions,
    work_brief_publication,
    work_inspection,
    work_state_commands,
)
from pinboard.interfaces.errors import (
    BriefSourceFailure,
    CliResult,
    CommandFailure,
    CommittedEffectFailure,
    DispatchFailure,
    ProposalFailure,
    WorkBriefFailure,
)

build_parser = cli_parser.build_parser


def _dispatch(  # noqa: C901, PLR0912 - one visible exhaustive command-family router
    invocation: cli_commands.CliInvocation,
) -> CliResult[int]:
    if isinstance(invocation.command, cli_commands.InputContractCommand):
        return work_inspection.show_input_contract(invocation.command)
    if isinstance(invocation.command, cli_commands.ToolContractCommand):
        return tool_contract.show_tool_contract(invocation.command)
    roots = work_state_commands.resolve_roots(invocation.roots)
    if isinstance(invocation.command, cli_commands.RootCommand):
        return work_state_commands.show_roots(roots, invocation.command)
    if isinstance(invocation.command, cli_commands.BriefSourcesPlanCommand | cli_commands.BriefSourcesEmitCommand):
        return brief_source_commands.plan_or_emit_brief_sources(roots, invocation.command)
    durable = work_state_commands.resolve_durable_layout(roots)
    store = work_state_commands.compose_store(durable)
    match invocation.command:
        case cli_commands.ValidateCommand() as command:
            return work_state_commands.validate_state(roots, durable, store, command)
        case cli_commands.StatusCommand() as command:
            return work_inspection.show_status(roots, store, command)
        case cli_commands.OverviewCommand() as command:
            return work_inspection.show_overview(store, command)
        case cli_commands.ItemStatusCommand() as command:
            return work_inspection.show_item_status(store, command)
        case cli_commands.ItemReviseCommand() as command:
            return transitions.revise_item(roots, durable, store, command)
        case cli_commands.ItemDefinitionCommand() as command:
            return work_inspection.show_item_definition(store, command)
        case cli_commands.ItemDefinitionHistoryCommand() as command:
            return work_inspection.show_item_definition_history(store, command)
        case cli_commands.CloseCommand() as command:
            return transitions.close(roots, durable, store, command)
        case cli_commands.ActionsCommand() | cli_commands.LeasedActionsCommand() as command:
            return work_inspection.show_actions(store, command)
        case cli_commands.BriefPublishCommand() as command:
            return work_brief_publication.publish_brief(durable, store, command)
        case cli_commands.HandoverCommand() as command:
            return project_handover.export_project_handover(durable, store, command)
        case cli_commands.InitializeCommand() as command:
            return work_state_commands.initialize_state(roots, durable, store, command)
        case cli_commands.ProposalCommand() as command:
            return proposal_commands.create_proposal(durable, store, command)
        case (
            cli_commands.ProjectTransitionCommand()
            | cli_commands.AttemptTransitionCommand()
            | cli_commands.PreparationTransitionCommand()
        ) as command:
            return transitions.transition(roots, durable, store, command)
        case (cli_commands.ProjectDispatchCommand() | cli_commands.ProjectReviewedDispatchCommand()) as command:
            return dispatch_brief.prepare_dispatch_command(roots, durable, store, command)
        case cli_commands.AttemptStatusCommand() as command:
            return attempt_authority.show_attempt_authority_status(store, command)
        case cli_commands.AttemptInspectCommand() as command:
            return work_inspection.show_attempt(roots, store, command)
        case cli_commands.ReviewJobCommand() as command:
            return work_inspection.show_review_job(roots, store, command)
        case (
            cli_commands.AttemptAcquireCommand()
            | cli_commands.AttemptRenewCommand()
            | cli_commands.AttemptReleaseCommand()
            | cli_commands.AttemptRevokeCommand()
        ) as command:
            return attempt_authority.change_attempt_authority(durable, store, command)
        case cli_commands.PreparationStatusCommand() as command:
            return preparation_authority.show_preparation_authority_status(store, command)
        case cli_commands.PreparationStartCommand() as command:
            return preparation_authority.start_preparation(durable, store, command)
        case (
            cli_commands.PreparationAcquireCommand()
            | cli_commands.PreparationTransferCommand()
            | cli_commands.PreparationRenewCommand()
            | cli_commands.PreparationReleaseCommand()
            | cli_commands.PreparationRevokeCommand()
        ) as command:
            return preparation_authority.change_preparation_authority(durable, store, command)
        case cli_commands.ParallelPreviewCommand() as command:
            return work_inspection.show_parallel_preview(store, command)
        case cli_commands.RebuildViewsCommand() as command:
            return work_state_commands.rebuild_views(durable, store, command)
        case _ as unreachable:
            assert_never(unreachable)


def _parse_arguments(
    arguments: tuple[str, ...],
    *,
    json_requested: bool,
) -> cli_commands.CliInvocation | int:
    if not json_requested:
        return cli_parser.parse_invocation(arguments)
    parser_stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(parser_stderr):
            return cli_parser.parse_invocation(arguments)
    except SystemExit as error:
        if error.code == 0:
            raise
        cli_output.write_argument_rejection(arguments, parser_stderr.getvalue().strip())
        return 2


def _failure_exit_code(result: CliResult[int]) -> int:
    match result:
        case int():
            return result
        case CommandFailure():
            return 11
        case ProposalFailure(code=DecisionFailureCode.PROPOSAL_INVALID):
            return 2
        case ProposalFailure():
            return 13
        case DispatchFailure():
            return 14
        case BriefSourceFailure():
            return 15
        case WorkBriefFailure():
            return 16
        case CommittedEffectFailure():
            return 12
        case _ as unreachable:
            assert_never(unreachable)


def _present_expected_result(result: CliResult[int], operation: str, *, json_requested: bool) -> int:
    exit_code = _failure_exit_code(result)
    if isinstance(result, int):
        return exit_code
    if json_requested:
        if isinstance(result, CommittedEffectFailure):
            cli_output.write_operation_rejection(operation, result.code, result.message, result.details, ())
        else:
            cli_output.write_rejected_operation(operation, result)
    else:
        print(str(result), file=sys.stderr)
    return exit_code


def _run_invocation(
    invocation: cli_commands.CliInvocation,
    operation: str,
    *,
    json_requested: bool,
) -> int:
    try:
        return _present_expected_result(_dispatch(invocation), operation, json_requested=json_requested)
    except ArtifactAcceptanceAfterPublicationError as error:
        cause = error.cause
        code = (
            cause.code.value
            if isinstance(cause, (StorageError, ArtifactError, FileIOError))
            else "ARTIFACT_ACCEPTANCE_FAILED"
        )
        details = FailureDetails(
            observed=(FailureFact("published_artifact_selector", error.selector),),
            mismatches=(),
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.COMMITTED,
            changed_surfaces=(ChangedSurface.IMMUTABLE_ARTIFACT,),
            alternatives=(),
        )
        if json_requested:
            cli_output.write_operation_rejection(operation, code, str(cause), details, ())
        else:
            print(f"{cause}; immutable artifact published at '{error.selector}'", file=sys.stderr)
        return 12
    except (RootError, OSError) as error:
        if json_requested:
            code = error.code.value if isinstance(error, RootError) else "CLI_IO_ERROR"
            cli_output.write_operation_rejection(
                operation,
                code,
                str(error),
                FailureDetails(
                    observed=(),
                    mismatches=(),
                    retry=RetryDisposition.CORRECT_INPUT,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
                (),
            )
        else:
            print(str(error), file=sys.stderr)
        return 2
    except (StorageError, ArtifactError, FileIOError) as error:
        if json_requested:
            retry = (
                RetryDisposition.RETRY_SAME_INPUT
                if isinstance(error, StorageError) and error.retryable
                else RetryDisposition.DO_NOT_RETRY
            )
            cli_output.write_operation_rejection(
                operation,
                error.code.value,
                str(error),
                FailureDetails(
                    observed=(),
                    mismatches=(),
                    retry=retry,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
                (),
            )
        else:
            print(str(error), file=sys.stderr)
        return 12


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in arguments
    invocation = _parse_arguments(arguments, json_requested=json_requested)
    if isinstance(invocation, int):
        return invocation
    operation = tool_contract.operation_identity(invocation.command)
    return _run_invocation(invocation, operation, json_requested=json_requested)
