"""Installed command router and process exit boundary.

This module owns the single exhaustive command-family branch and final rendering
of typed failures. Command grammar and use-case composition live with their
thematic CLI owners; this root performs no storage or domain work itself.
"""

import contextlib
import io
import shlex
import sys
from collections.abc import Sequence
from typing import assert_never

from pinboard.adapters.files.errors import (
    ArtifactError,
    FileIOError,
    FileIOErrorCode,
    ImmutableFilePublishedError,
    RootError,
)
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.cli import (
    attempt_authority,
    brief_source_commands,
    cli_commands,
    cli_output,
    cli_parser,
    dispatch_brief,
    ordering,
    preparation_authority,
    project_handover,
    proposal_commands,
    tool_contract,
    transitions,
    work_brief_publication,
    work_inspection,
    work_state_commands,
)
from pinboard.cli.errors import (
    BriefSourceFailure,
    CliResult,
    CommandFailure,
    CommittedEffectFailure,
    DispatchFailure,
    InitializationAfterCommittedEffectsError,
    ProposalFailure,
    WorkBriefFailure,
    initialization_failure_details,
    storage_failure_details,
)
from pinboard.domain.errors import (
    ArtifactAcceptanceAfterPublicationError,
    ChangedSurface,
    DecisionFailureCode,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    FailureMismatch,
    RetryDisposition,
)

build_parser = cli_parser.build_parser


def _dispatch(  # noqa: C901, PLR0912 - one visible exhaustive command-family router
    invocation: cli_commands.CliInvocation,
    roots: cli_commands.ResolvedRoots | None,
) -> CliResult[int]:
    if isinstance(invocation.command, cli_commands.InputContractCommand):
        return work_inspection.show_input_contract(invocation.command)
    if isinstance(invocation.command, cli_commands.ToolContractCommand):
        return tool_contract.show_tool_contract(invocation.command)
    if roots is None:
        raise AssertionError("A rooted command requires resolved project roots.")
    if isinstance(invocation.command, cli_commands.RootCommand):
        return work_state_commands.show_roots(roots, invocation.command)
    if isinstance(
        invocation.command,
        cli_commands.BriefSourcesPlanCommand
        | cli_commands.BriefSourcesPlanToFileCommand
        | cli_commands.BriefSourcesEmitCommand,
    ):
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
        case cli_commands.BriefReviewNeedsCorrectionCommand() as command:
            return work_brief_publication.publish_brief_review_needs_correction(durable, store, command)
        case cli_commands.BriefReviewStatusCommand() as command:
            return work_brief_publication.show_brief_review_status(roots, durable, store, command)
        case cli_commands.ArtifactVerifyCommand() as command:
            return work_inspection.verify_artifact_reference(durable, store, command)
        case cli_commands.HandoverCommand() as command:
            return project_handover.export_project_handover(durable, store, command)
        case cli_commands.InitializeCommand() as command:
            return work_state_commands.initialize_state(roots, durable, store, command)
        case cli_commands.OrderCommand() as command:
            return ordering.reorder(durable, store, command)
        case cli_commands.ProposalCommand() as command:
            return proposal_commands.create_proposal(durable, store, command)
        case (
            cli_commands.ProjectTransitionCommand()
            | cli_commands.AttemptTransitionCommand()
            | cli_commands.PreparationTransitionCommand()
        ) as command:
            return transitions.transition(roots, durable, store, command)
        case (
            cli_commands.ProjectDispatchCommand()
            | cli_commands.ProjectReviewedDispatchCommand()
            | cli_commands.ProjectCorrectionDispatchCommand()
        ) as command:
            return dispatch_brief.prepare_dispatch_command(roots, durable, store, command)
        case cli_commands.AttemptStatusCommand() as command:
            return attempt_authority.show_attempt_authority_status(store, command)
        case cli_commands.AttemptInspectCommand() as command:
            return work_inspection.show_attempt(roots, store, command)
        case (
            cli_commands.InitialReviewJobCommand()
            | cli_commands.PackageInitialReviewJobCommand()
            | cli_commands.CompatibilityPackageInitialRecoveryReviewJobCommand()
            | cli_commands.CorrectionReviewJobCommand()
            | cli_commands.PackageCorrectionReviewJobCommand()
            | cli_commands.CompatibilityPackageCorrectionRecoveryReviewJobCommand()
        ) as command:
            return work_inspection.show_review_job(roots, durable, store, command)
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


def _command_failure_recovery(
    failure: CommandFailure,
    roots: cli_commands.ResolvedRoots | None,
) -> tuple[str, ...]:
    details = failure.details
    if failure.code == DecisionFailureCode.ITEM_STATUS_INCONSISTENT and details is not None:
        return ("pinboard validate",)
    if roots is None or details is None:
        return ()
    observed = {value.field: value.value for value in details.observed}
    if failure.code == DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED:
        attempt_id = observed.get("attempt_id")
        if attempt_id is None:
            return ()
        suffix = ("attempt", "status", "--attempt-id", str(attempt_id), "--json")
    elif failure.code.value == "ACTION_AUTHORITY_WRONG" and observed.get("action_id") is not None:
        expected_roles = {str(value.expected) for value in details.mismatches if value.field == "role"}
        subject = observed.get("subject")
        if expected_roles == {"project"}:
            suffix = ("actions", "--role", "project", "--action-id", str(observed["action_id"]), "--json")
        elif expected_roles == {"worker"} and subject is not None:
            suffix = ("attempt", "status", "--attempt-id", str(subject), "--json")
        elif expected_roles == {"preparer"} and subject is not None:
            suffix = ("preparation", "status", "--item-id", str(subject), "--json")
        else:
            return ()
    else:
        return ()
    return (
        shlex.join(
            (
                "pinboard",
                "--project-root",
                str(roots.source_checkout),
                "--work-root",
                str(roots.work),
                *suffix,
            )
        ),
    )


def _dispatch_failure_recovery(failure: DispatchFailure, operation: str) -> tuple[str, ...]:
    if failure.details is None:
        return ()
    observed = {value.field: value.value for value in failure.details.observed}
    reviewed_dispatch = observed.get("reviewed_dispatch_command")
    if failure.code.value == "DISPATCH_BASE_REVISION_MISMATCH":
        commands = (
            observed.get("tool_contract_command"),
            observed.get("current_dispatch_action_command"),
        )
        return tuple(str(command) for command in commands if command is not None)
    if failure.code.value == "DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID" and reviewed_dispatch is not None:
        return (str(reviewed_dispatch),)
    fresh_review = observed.get("fresh_review_preparation_command")
    if (
        failure.code.value in {"DISPATCH_BRIEF_REVIEW_STALE", "DISPATCH_AUTHORITY_STALE"}
        and failure.details.effect == EffectDisposition.UNCHANGED
        and fresh_review is not None
    ):
        return (str(fresh_review),)
    if failure.code.value != "DISPATCH_ENVIRONMENT_INVALID":
        return ()
    mismatches = {value.field for value in failure.details.mismatches}
    if "environment_schema" not in mismatches:
        return ()
    return (shlex.join(("pinboard", "tool-contract", "--operation", operation, "--json")),)


def _expected_failure_recovery(
    result: CommandFailure | DispatchFailure,
    operation: str,
    roots: cli_commands.ResolvedRoots | None,
) -> tuple[str, ...]:
    if isinstance(result, CommandFailure):
        return _command_failure_recovery(result, roots)
    if isinstance(result, DispatchFailure):
        return _dispatch_failure_recovery(result, operation)
    return ()


def _present_expected_result(
    result: CliResult[int],
    operation: str,
    *,
    json_requested: bool,
    roots: cli_commands.ResolvedRoots | None,
) -> int:
    exit_code = _failure_exit_code(result)
    if isinstance(result, int):
        return exit_code
    if json_requested:
        if isinstance(result, CommittedEffectFailure):
            cli_output.write_operation_rejection(operation, result.code, result.message, result.details, ())
        elif isinstance(result, CommandFailure | DispatchFailure) and (
            recovery := _expected_failure_recovery(result, operation, roots)
        ):
            cli_output.write_operation_rejection(
                operation,
                result.code.value,
                result.message,
                result.details,
                recovery,
            )
        else:
            cli_output.write_rejected_operation(operation, result)
    else:
        print(str(result), file=sys.stderr)
    return exit_code


def _run_invocation(  # noqa: C901, PLR0912, PLR0915 - one outer exception-to-process-result boundary
    invocation: cli_commands.CliInvocation,
    operation: str,
    *,
    json_requested: bool,
) -> int:
    roots: cli_commands.ResolvedRoots | None = None
    try:
        if not isinstance(invocation.command, cli_commands.InputContractCommand | cli_commands.ToolContractCommand):
            roots = work_state_commands.resolve_roots(invocation.roots)
        return _present_expected_result(
            _dispatch(invocation, roots),
            operation,
            json_requested=json_requested,
            roots=roots,
        )
    except ArtifactAcceptanceAfterPublicationError as error:
        cause = error.cause
        code = (
            cause.code.value
            if isinstance(cause, (StorageError, ArtifactError, FileIOError))
            else "ARTIFACT_ACCEPTANCE_FAILED"
        )
        changed_surfaces = error.changed_surfaces
        effect = EffectDisposition.COMMITTED if changed_surfaces else EffectDisposition.UNCHANGED
        if isinstance(cause, StorageError):
            details = storage_failure_details(
                cause,
                operation,
                roots,
                effect,
                changed_surfaces,
                (FailureFact("published_artifact_selector", error.selector),),
            )
        else:
            details = FailureDetails(
                observed=(FailureFact("published_artifact_selector", error.selector),),
                mismatches=(),
                retry=(RetryDisposition.DO_NOT_RETRY if changed_surfaces else RetryDisposition.RETRY_SAME_INPUT),
                effect=effect,
                changed_surfaces=changed_surfaces,
                alternatives=(),
            )
        if json_requested:
            cli_output.write_operation_rejection(operation, code, str(cause), details, ())
        else:
            suffix = (
                f"; committed surfaces: {', '.join(surface.value for surface in changed_surfaces)}"
                if changed_surfaces
                else ""
            )
            print(f"{cause}{suffix}", file=sys.stderr)
        return 12
    except InitializationAfterCommittedEffectsError as error:
        details = initialization_failure_details(error, operation, roots)
        cause = error.cause
        if json_requested:
            cli_output.write_operation_rejection(operation, cause.code.value, str(cause), details, ())
        else:
            print(
                f"{cause}; initialization committed: {', '.join(value.value for value in details.changed_surfaces)}",
                file=sys.stderr,
            )
        return 12
    except ImmutableFilePublishedError as error:
        details = FailureDetails(
            observed=(FailureFact("selected_output_path", str(error.path)),),
            mismatches=(),
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.COMMITTED,
            changed_surfaces=(ChangedSurface.SELECTED_OUTPUT,),
            alternatives=(),
        )
        if json_requested:
            cli_output.write_operation_rejection(operation, error.code.value, str(error), details, ())
        else:
            print(f"{error}; selected output published at '{error.path}'", file=sys.stderr)
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
            if isinstance(error, StorageError):
                details = storage_failure_details(error, operation, roots, EffectDisposition.UNCHANGED, (), ())
                recovery = ()
            elif (
                isinstance(error, FileIOError)
                and error.code == FileIOErrorCode.FILE_ALREADY_EXISTS
                and isinstance(invocation.command, cli_commands.BriefSourcesPlanToFileCommand)
            ):
                destination = str(invocation.command.output_plan.absolute())
                details = FailureDetails(
                    observed=(FailureFact("selected_output_path", destination),),
                    mismatches=(FailureMismatch("selected_output_path", "unoccupied", "occupied"),),
                    retry=RetryDisposition.CORRECT_INPUT,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                )
                recovery = ("mktemp -d",)
            else:
                details = FailureDetails(
                    observed=(),
                    mismatches=(),
                    retry=RetryDisposition.DO_NOT_RETRY,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                )
                recovery = ()
            cli_output.write_operation_rejection(
                operation,
                error.code.value,
                str(error),
                details,
                recovery,
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
