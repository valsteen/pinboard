"""Installed command router and process exit boundary.

This module owns the single exhaustive command-family branch and final rendering
of typed failures. Command grammar and use-case composition live with their
thematic interface owners; this root performs no storage or domain work itself.
"""

import sys
from collections.abc import Sequence
from typing import assert_never

from pinboard.adapters.files.errors import ArtifactError, FileIOError, RootError
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.domain.errors import DecisionFailureCode
from pinboard.interfaces import (
    attempt_authority,
    brief_source_commands,
    cli_commands,
    cli_parser,
    dispatch_brief,
    preparation_authority,
    project_handover,
    proposal_commands,
    transitions,
    work_brief_publication,
    work_inspection,
    work_state_commands,
)
from pinboard.interfaces.errors import (
    BriefSourceError,
    CliResult,
    CommandFailure,
    DispatchFailure,
    ProposalFailure,
    WorkBriefError,
)

build_parser = cli_parser.build_parser


def _dispatch(  # noqa: C901, PLR0912 - one visible exhaustive command-family router
    invocation: cli_commands.CliInvocation,
) -> CliResult[int]:
    if isinstance(invocation.command, cli_commands.InputContractCommand):
        return work_inspection.show_input_contract(invocation.command)
    roots = work_state_commands.resolve_roots(invocation.roots)
    match invocation.command:
        case cli_commands.RootCommand() as command:
            return work_state_commands.show_roots(roots, command)
        case cli_commands.ValidateCommand() as command:
            return work_state_commands.validate_state(roots, command)
        case cli_commands.StatusCommand() as command:
            return work_inspection.show_status(roots, command)
        case cli_commands.OverviewCommand() as command:
            return work_inspection.show_overview(roots, command)
        case cli_commands.ItemStatusCommand() as command:
            return work_inspection.show_item_status(roots, command)
        case cli_commands.ItemReviseCommand() as command:
            return transitions.revise_item(roots, command)
        case cli_commands.ItemDefinitionCommand() as command:
            return work_inspection.show_item_definition(roots, command)
        case cli_commands.ItemDefinitionHistoryCommand() as command:
            return work_inspection.show_item_definition_history(roots, command)
        case cli_commands.CloseCommand() as command:
            return transitions.close(roots, command)
        case cli_commands.ActionsCommand() | cli_commands.LeasedActionsCommand() as command:
            return work_inspection.show_actions(roots, command)
        case (cli_commands.BriefSourcesPlanCommand() | cli_commands.BriefSourcesEmitCommand()) as command:
            return brief_source_commands.plan_or_emit_brief_sources(roots, command)
        case cli_commands.BriefPublishCommand() as command:
            return work_brief_publication.publish_brief(roots, command)
        case cli_commands.HandoverCommand() as command:
            return project_handover.export_project_handover(roots, command)
        case cli_commands.InitializeCommand() as command:
            return work_state_commands.initialize_state(roots, command)
        case cli_commands.ProposalCommand() as command:
            return proposal_commands.create_proposal(roots, command)
        case (
            cli_commands.ProjectTransitionCommand()
            | cli_commands.AttemptTransitionCommand()
            | cli_commands.PreparationTransitionCommand()
        ) as command:
            return transitions.transition(roots, command)
        case (cli_commands.ProjectDispatchCommand() | cli_commands.ProjectReviewedDispatchCommand()) as command:
            return dispatch_brief.prepare_dispatch_command(roots, command)
        case cli_commands.AttemptStatusCommand() as command:
            return attempt_authority.show_attempt_authority_status(roots, command)
        case (
            cli_commands.AttemptAcquireCommand()
            | cli_commands.AttemptRenewCommand()
            | cli_commands.AttemptReleaseCommand()
            | cli_commands.AttemptRevokeCommand()
        ) as command:
            return attempt_authority.change_attempt_authority(roots, command)
        case cli_commands.PreparationStatusCommand() as command:
            return preparation_authority.show_preparation_authority_status(roots, command)
        case (
            cli_commands.PreparationAcquireCommand()
            | cli_commands.PreparationTransferCommand()
            | cli_commands.PreparationRenewCommand()
            | cli_commands.PreparationReleaseCommand()
            | cli_commands.PreparationRevokeCommand()
        ) as command:
            return preparation_authority.change_preparation_authority(roots, command)
        case cli_commands.ParallelPreviewCommand() as command:
            return work_inspection.show_parallel_preview(roots, command)
        case cli_commands.RebuildViewsCommand() as command:
            return work_state_commands.rebuild_views(roots, command)
        case _ as unreachable:
            assert_never(unreachable)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = _dispatch(cli_parser.parse_invocation(argv))
        match result:
            case int():
                return result
            case CommandFailure():
                exit_code = 11
            case ProposalFailure(code=DecisionFailureCode.PROPOSAL_INVALID):
                exit_code = 2
            case ProposalFailure():
                exit_code = 13
            case DispatchFailure():
                exit_code = 14
            case _ as unreachable:
                assert_never(unreachable)
        print(str(result), file=sys.stderr)
        return exit_code
    except (RootError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except (StorageError, ArtifactError, FileIOError) as error:
        print(str(error), file=sys.stderr)
        return 12
    except BriefSourceError as error:
        print(str(error), file=sys.stderr)
        return 15
    except WorkBriefError as error:
        print(str(error), file=sys.stderr)
        return 16
