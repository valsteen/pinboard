"""The complete installed command grammar and exact leaf command metadata.

This module parses untyped command-line values into one closed CliInvocation.
Argument parsing may terminate through argparse, and msgspec validation failures
are converted to the selected parser's normal usage error. It does not resolve
roots, open resources, dispatch commands, or perform product decisions.
"""

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard import __version__
from pinboard.domain import decision_models, work_models
from pinboard.interfaces import cli_commands, transition_input


class _CompoundCommand(Enum):
    ACTIONS = "actions"
    BRIEF_SOURCES = "brief-sources"
    DISPATCH = "dispatch"
    TRANSITION = "transition"


class _RawCliArguments(argparse.Namespace):
    command_selection: type[cli_commands.CliCommand] | _CompoundCommand | None
    selected_parser: argparse.ArgumentParser | None
    contract_variants: tuple[tuple[str, type[cli_commands.CliCommand]], ...]

    def __init__(self) -> None:
        super().__init__()
        self.command_selection = None
        self.selected_parser = None
        self.contract_variants = ()


@dataclass(frozen=True, slots=True)
class InstalledCommandVariant:
    operation_id: str
    variant: str
    command_type: type[cli_commands.CliCommand]


class _BriefSourcesArguments(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    file: Path
    max_batch_bytes: cli_commands.PositiveInt
    json: bool
    emit_batch: int | None

    def __post_init__(self) -> None:
        if self.json == (self.emit_batch is not None):
            raise ValueError("exactly one of --json or --emit-batch is required")


class _ActionsArguments(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    role: decision_models.Role
    lease_id: cli_commands.StableLeaseId | None
    generation: int | None
    action_id: cli_commands.StableActionId | None
    json: bool

    def __post_init__(self) -> None:
        if (self.lease_id is None) != (self.generation is None):
            raise ValueError("--lease-id and --generation must be supplied together")


class _TransitionArguments(msgspec.Struct, frozen=True):
    action_id: cli_commands.StableActionId
    expected_revision: str
    payload: Path
    subject_revision: str | None
    json: bool


class _ProjectTransitionArguments(
    _TransitionArguments,
    tag="project",
    tag_field="authorization",
    frozen=True,
    forbid_unknown_fields=True,
):
    task_id: cli_commands.StableTaskId
    host_id: cli_commands.StableHostId
    lease_id: None
    generation: None


class _AttemptTransitionArguments(
    _TransitionArguments,
    tag="attempt",
    tag_field="authorization",
    frozen=True,
    forbid_unknown_fields=True,
):
    lease_id: cli_commands.StableLeaseId
    generation: int
    task_id: None
    host_id: None


class _PreparationTransitionArguments(
    _TransitionArguments,
    tag="preparation",
    tag_field="authorization",
    frozen=True,
    forbid_unknown_fields=True,
):
    lease_id: cli_commands.StableLeaseId
    generation: int
    task_id: None
    host_id: None


type _ExactTransitionArguments = (
    _ProjectTransitionArguments | _AttemptTransitionArguments | _PreparationTransitionArguments
)


class _DispatchArguments(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: cli_commands.StableActionId
    expected_revision: str
    task_id: cli_commands.StableTaskId
    host_id: cli_commands.StableHostId
    checkpoint: str
    environment: Path
    prompt: Path | None
    brief_review: Path | None
    review_id: cli_commands.KebabReviewId | None
    json: bool

    def __post_init__(self) -> None:
        if (self.brief_review is None) != (self.review_id is None):
            raise ValueError("--brief-review and --review-id must be supplied together")


def _decode_brief_sources[RawT](
    values: dict[str, RawT],
) -> cli_commands.BriefSourcesPlanCommand | cli_commands.BriefSourcesEmitCommand:
    arguments = msgspec.convert(values, type=_BriefSourcesArguments, strict=True)
    if arguments.emit_batch is None:
        return cli_commands.BriefSourcesPlanCommand(file=arguments.file, max_batch_bytes=arguments.max_batch_bytes)
    return cli_commands.BriefSourcesEmitCommand(
        file=arguments.file,
        emit_batch=arguments.emit_batch,
        max_batch_bytes=arguments.max_batch_bytes,
    )


def _decode_actions[RawT](values: dict[str, RawT]) -> cli_commands.ActionsCommand | cli_commands.LeasedActionsCommand:
    arguments = msgspec.convert(values, type=_ActionsArguments, strict=True)
    if arguments.lease_id is None or arguments.generation is None:
        return cli_commands.ActionsCommand(role=arguments.role, action_id=arguments.action_id, json=arguments.json)
    return cli_commands.LeasedActionsCommand(
        role=arguments.role,
        lease_id=arguments.lease_id,
        generation=arguments.generation,
        action_id=arguments.action_id,
        json=arguments.json,
    )


def _decode_transition[RawT](values: dict[str, RawT]) -> cli_commands.TransitionCommand:
    arguments = msgspec.convert(values, type=_ExactTransitionArguments, strict=True)
    match arguments:
        case _ProjectTransitionArguments():
            return cli_commands.ProjectTransitionCommand(
                action_id=arguments.action_id,
                expected_revision=arguments.expected_revision,
                payload=arguments.payload,
                task_id=arguments.task_id,
                host_id=arguments.host_id,
                subject_revision=arguments.subject_revision,
                json=arguments.json,
            )
        case _AttemptTransitionArguments():
            return cli_commands.AttemptTransitionCommand(
                action_id=arguments.action_id,
                expected_revision=arguments.expected_revision,
                generation=arguments.generation,
                payload=arguments.payload,
                lease_id=arguments.lease_id,
                subject_revision=arguments.subject_revision,
                json=arguments.json,
            )
        case _PreparationTransitionArguments():
            return cli_commands.PreparationTransitionCommand(
                action_id=arguments.action_id,
                expected_revision=arguments.expected_revision,
                generation=arguments.generation,
                payload=arguments.payload,
                lease_id=arguments.lease_id,
                subject_revision=arguments.subject_revision,
                json=arguments.json,
            )
        case _ as unreachable:
            assert_never(unreachable)


def _decode_dispatch[RawT](values: dict[str, RawT]) -> cli_commands.DispatchCommand:
    arguments = msgspec.convert(values, type=_DispatchArguments, strict=True)
    if arguments.brief_review is None:
        return cli_commands.ProjectDispatchCommand(
            action_id=arguments.action_id,
            expected_revision=arguments.expected_revision,
            task_id=arguments.task_id,
            host_id=arguments.host_id,
            checkpoint=arguments.checkpoint,
            environment=arguments.environment,
            prompt=arguments.prompt,
            json=arguments.json,
        )
    assert arguments.review_id is not None
    return cli_commands.ProjectReviewedDispatchCommand(
        action_id=arguments.action_id,
        expected_revision=arguments.expected_revision,
        task_id=arguments.task_id,
        host_id=arguments.host_id,
        checkpoint=arguments.checkpoint,
        environment=arguments.environment,
        brief_review=arguments.brief_review,
        prompt=arguments.prompt,
        review_id=arguments.review_id,
        json=arguments.json,
    )


def _decode_selected_command[RawT](
    command_selection: type[cli_commands.CliCommand] | _CompoundCommand,
    values: dict[str, RawT],
) -> cli_commands.CliCommand:
    if isinstance(command_selection, type):
        return msgspec.convert(values, type=command_selection, strict=True)
    match command_selection:
        case _CompoundCommand.ACTIONS:
            return _decode_actions(values)
        case _CompoundCommand.BRIEF_SOURCES:
            return _decode_brief_sources(values)
        case _CompoundCommand.DISPATCH:
            return _decode_dispatch(values)
        case _CompoundCommand.TRANSITION:
            return _decode_transition(values)
        case _ as unreachable:
            assert_never(unreachable)


def _select_command(
    parser: argparse.ArgumentParser,
    command_selection: type[cli_commands.CliCommand] | _CompoundCommand,
) -> None:
    if isinstance(command_selection, type):
        variants = (("default", command_selection),)
    else:
        match command_selection:
            case _CompoundCommand.ACTIONS:
                variants = (
                    ("unleased", cli_commands.ActionsCommand),
                    ("leased", cli_commands.LeasedActionsCommand),
                )
            case _CompoundCommand.BRIEF_SOURCES:
                variants = (
                    ("plan", cli_commands.BriefSourcesPlanCommand),
                    ("emit", cli_commands.BriefSourcesEmitCommand),
                )
            case _CompoundCommand.DISPATCH:
                variants = (
                    ("without-review", cli_commands.ProjectDispatchCommand),
                    ("with-review", cli_commands.ProjectReviewedDispatchCommand),
                )
            case _CompoundCommand.TRANSITION:
                variants = (
                    ("project", cli_commands.ProjectTransitionCommand),
                    ("attempt", cli_commands.AttemptTransitionCommand),
                    ("preparation", cli_commands.PreparationTransitionCommand),
                )
            case _ as unreachable:
                assert_never(unreachable)
    parser.set_defaults(
        command_selection=command_selection,
        selected_parser=parser,
        contract_variants=variants,
    )


def _add_attempt_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    attempt = commands.add_parser("attempt", help="Manage a renewable attempt ownership claim.")
    operations = attempt.add_subparsers(required=True)
    acquire = operations.add_parser("acquire")
    acquire.add_argument("--attempt-id", required=True)
    acquire.add_argument("--task-id", required=True)
    acquire.add_argument("--host-id", required=True)
    acquire.add_argument("--ttl-seconds", required=True, type=int)
    acquire.add_argument("--json", action="store_true")
    _select_command(acquire, cli_commands.AttemptAcquireCommand)
    renew = operations.add_parser("renew")
    renew.add_argument("--attempt-id", required=True)
    renew.add_argument("--lease-id", required=True)
    renew.add_argument("--generation", required=True, type=int)
    renew.add_argument("--ttl-seconds", required=True, type=int)
    renew.add_argument("--json", action="store_true")
    _select_command(renew, cli_commands.AttemptRenewCommand)
    release = operations.add_parser("release")
    release.add_argument("--attempt-id", required=True)
    release.add_argument("--lease-id", required=True)
    release.add_argument("--generation", required=True, type=int)
    release.add_argument("--json", action="store_true")
    _select_command(release, cli_commands.AttemptReleaseCommand)
    revoke = operations.add_parser("revoke")
    revoke.add_argument("--attempt-id", required=True)
    revoke.add_argument("--lease-id", required=True)
    revoke.add_argument("--generation", required=True, type=int)
    revoke.add_argument("--task-id", required=True)
    revoke.add_argument("--host-id", required=True)
    revoke.add_argument("--json", action="store_true")
    _select_command(revoke, cli_commands.AttemptRevokeCommand)
    status = operations.add_parser("status")
    status.add_argument("--attempt-id", required=True)
    status.add_argument("--json", action="store_true")
    _select_command(status, cli_commands.AttemptStatusCommand)
    inspect = operations.add_parser("inspect", help="Read the exact attempt and its current continuation.")
    inspect.add_argument("--attempt-id", required=True)
    inspect.add_argument("--json", action="store_true")
    _select_command(inspect, cli_commands.AttemptInspectCommand)


def _add_preparation_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # noqa: PLR0915 - complete preparation grammar
    preparation = commands.add_parser("preparation", help="Manage a renewable ready-item preparation claim.")
    operations = preparation.add_subparsers(required=True)
    start = operations.add_parser("start", help="Claim the current eligible definition atomically.")
    start.add_argument("--item-id", required=True)
    start.add_argument("--task-id", required=True)
    start.add_argument("--host-id", required=True)
    start.add_argument("--ttl-seconds", required=True, type=int)
    start.add_argument("--json", action="store_true")
    _select_command(start, cli_commands.PreparationStartCommand)
    acquire = operations.add_parser("acquire")
    acquire.add_argument("--item-id", required=True)
    acquire.add_argument("--expected-project-revision", required=True)
    acquire.add_argument("--expected-item-subject-revision", required=True)
    acquire.add_argument("--expected-definition-revision", required=True, type=int)
    acquire.add_argument("--expected-definition-digest", required=True)
    acquire.add_argument("--task-id", required=True)
    acquire.add_argument("--host-id", required=True)
    acquire.add_argument("--ttl-seconds", required=True, type=int)
    acquire.add_argument("--json", action="store_true")
    _select_command(acquire, cli_commands.PreparationAcquireCommand)
    transfer = operations.add_parser("transfer")
    transfer.add_argument("--item-id", required=True)
    transfer.add_argument("--task-id", required=True)
    transfer.add_argument("--host-id", required=True)
    transfer.add_argument("--ttl-seconds", required=True, type=int)
    transfer.add_argument("--json", action="store_true")
    _select_command(transfer, cli_commands.PreparationTransferCommand)
    renew = operations.add_parser("renew")
    renew.add_argument("--item-id", required=True)
    renew.add_argument("--lease-id", required=True)
    renew.add_argument("--generation", required=True, type=int)
    renew.add_argument("--ttl-seconds", required=True, type=int)
    renew.add_argument("--json", action="store_true")
    _select_command(renew, cli_commands.PreparationRenewCommand)
    release = operations.add_parser("release")
    release.add_argument("--item-id", required=True)
    release.add_argument("--lease-id", required=True)
    release.add_argument("--generation", required=True, type=int)
    release.add_argument("--json", action="store_true")
    _select_command(release, cli_commands.PreparationReleaseCommand)
    revoke = operations.add_parser("revoke")
    revoke.add_argument("--item-id", required=True)
    revoke.add_argument("--lease-id", required=True)
    revoke.add_argument("--generation", required=True, type=int)
    revoke.add_argument("--task-id", required=True)
    revoke.add_argument("--host-id", required=True)
    revoke.add_argument("--json", action="store_true")
    _select_command(revoke, cli_commands.PreparationRevokeCommand)
    status = operations.add_parser("status")
    status.add_argument("--item-id", required=True)
    status.add_argument("--json", action="store_true")
    _select_command(status, cli_commands.PreparationStatusCommand)


def _add_item_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    item = commands.add_parser("item", help="Inspect one exact live or terminal item.")
    operations = item.add_subparsers(required=True)
    status = operations.add_parser("status", help="Show authoritative status for one exact item.")
    status.add_argument("--item-id", required=True)
    status.add_argument("--json", action="store_true")
    _select_command(status, cli_commands.ItemStatusCommand)
    revise = operations.add_parser("revise", help="Replace one nonterminal item's complete accepted definition.")
    revise.add_argument("--file", required=True, type=Path)
    revise.add_argument("--task-id", required=True)
    revise.add_argument("--host-id", required=True)
    revise.add_argument("--json", action="store_true")
    _select_command(revise, cli_commands.ItemReviseCommand)
    definition = operations.add_parser("definition", help="Show one item's complete current accepted definition.")
    definition.add_argument("--item-id", required=True)
    definition.add_argument("--json", action="store_true")
    _select_command(definition, cli_commands.ItemDefinitionCommand)
    history = operations.add_parser("definition-history", help="Show newest-first immutable definition revisions.")
    history.add_argument("--item-id", required=True)
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--before-revision", type=int)
    history.add_argument("--json", action="store_true")
    _select_command(history, cli_commands.ItemDefinitionHistoryCommand)


def _add_parallel_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parallel = commands.add_parser("parallel", help="Preview structurally independent work without launching it.")
    operations = parallel.add_subparsers(required=True)
    preview = operations.add_parser("preview")
    preview.add_argument("--item", action="append", default=[])
    preview.add_argument("--json", action="store_true")
    _select_command(preview, cli_commands.ParallelPreviewCommand)


def _add_chat_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    overview = commands.add_parser("overview", help="Show one coherent live-work snapshot.")
    overview.add_argument("--json", action="store_true")
    _select_command(overview, cli_commands.OverviewCommand)
    close = commands.add_parser("close", help="Record a terminal decision for non-active work.")
    close.add_argument("item_id")
    close.add_argument("--outcome", choices=tuple(outcome.value for outcome in work_models.CloseOutcome), required=True)
    close.add_argument("--reason", required=True)
    close.add_argument("--task-id", required=True)
    close.add_argument("--host-id", required=True)
    close.add_argument("--json", action="store_true")
    _select_command(close, cli_commands.CloseCommand)


def _add_inspection_parsers(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    root = commands.add_parser("root", help="Resolve the source checkout, shared repository, and work roots.")
    _select_command(root, cli_commands.RootCommand)
    validate = commands.add_parser("validate", help="Validate work state without modifying it.")
    validate.add_argument("--json", action="store_true")
    _select_command(validate, cli_commands.ValidateCommand)
    status = commands.add_parser("status", help="Show bounded current work facts.")
    status.add_argument("--json", action="store_true")
    _select_command(status, cli_commands.StatusCommand)
    _add_chat_parser(commands)
    _add_item_parser(commands)
    actions = commands.add_parser("actions", help="List the legal contextual actions.")
    actions.add_argument("--role", choices=tuple(role.value for role in decision_models.Role), required=True)
    actions.add_argument("--lease-id")
    actions.add_argument("--generation", type=int)
    actions.add_argument("--action-id", help="Return only this exact currently legal action.")
    actions.add_argument("--json", action="store_true")
    _select_command(actions, _CompoundCommand.ACTIONS)
    input_contract = commands.add_parser(
        "input-contract", help="Show the canonical payload and semantics for one action kind."
    )
    input_contract.add_argument("action_kind", choices=transition_input.INPUT_CONTRACT_ACTION_KINDS)
    input_contract.add_argument("--json", action="store_true")
    _select_command(input_contract, cli_commands.InputContractCommand)
    tool_contract = commands.add_parser(
        "tool-contract", help="Discover installed command and action contracts without opening project state."
    )
    selection = tool_contract.add_mutually_exclusive_group()
    selection.add_argument("--operation", help="Installed operation ID, optionally followed by :variant.")
    selection.add_argument("--action-kind", choices=transition_input.INPUT_CONTRACT_ACTION_KINDS)
    tool_contract.add_argument("--json", action="store_true")
    _select_command(tool_contract, cli_commands.ToolContractCommand)
    brief_sources = commands.add_parser(
        "brief-sources",
        help="Plan or emit deterministic context-bounded authority source batches.",
    )
    brief_sources.add_argument("--file", type=Path, required=True, help="pinboard-brief-sources/v1 manifest.")
    brief_sources.add_argument("--max-batch-bytes", type=int, default=24_000)
    brief_source_output = brief_sources.add_mutually_exclusive_group(required=True)
    brief_source_output.add_argument("--json", action="store_true", help="Print the complete batch plan.")
    brief_source_output.add_argument("--emit-batch", type=int, help="Print exactly one zero-based planned batch.")
    _select_command(brief_sources, _CompoundCommand.BRIEF_SOURCES)


def _add_brief_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    brief = commands.add_parser("brief", help="Publish canonical typed work briefs without scheduling them.")
    operations = brief.add_subparsers(required=True)
    publish = operations.add_parser("publish", help="Validate and immutably publish one pinboard-work-brief/v2 file.")
    publish.add_argument("--file", type=Path, required=True)
    publish.add_argument("--json", action="store_true")
    _select_command(publish, cli_commands.BriefPublishCommand)


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915 - complete top-level grammar
    parser = argparse.ArgumentParser(prog="pinboard", description="Inspect and transition one pinboard.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--project-root", type=Path, help="Select the exact source checkout for authority reads.")
    parser.add_argument("--work-root", type=Path)
    commands = parser.add_subparsers(required=True)
    _add_inspection_parsers(commands)
    handover = commands.add_parser("handover", help="Export one complete tool-neutral project handover.")
    handover.add_argument("--json", action="store_true", required=True)
    _select_command(handover, cli_commands.HandoverCommand)
    initialize = commands.add_parser("init", help="Create an empty current SQLite work state.")
    _select_command(initialize, cli_commands.InitializeCommand)
    _add_brief_parser(commands)
    proposal = commands.add_parser("proposal", help="Create one intake item without activating it.")
    proposal.add_argument("--file", type=Path, required=True)
    proposal.add_argument("--task-id", required=True)
    proposal.add_argument("--host-id", required=True)
    _select_command(proposal, cli_commands.ProposalCommand)
    transition = commands.add_parser(
        "transition", help="Apply one selected lifecycle-changing action returned by the actions command."
    )
    transition.add_argument("--action-id", required=True)
    transition.add_argument("--expected-revision", required=True)
    transition.add_argument("--generation", type=int)
    transition.add_argument("--subject-revision")
    transition.add_argument("--lease-id")
    transition.add_argument("--task-id")
    transition.add_argument("--host-id")
    transition.add_argument(
        "--authorization",
        choices=("project", "attempt", "preparation"),
        required=True,
    )
    transition.add_argument("--payload", required=True, type=Path)
    transition.add_argument("--json", action="store_true")
    _select_command(transition, _CompoundCommand.TRANSITION)
    dispatch = commands.add_parser("dispatch", help="Prepare or verify a canonical worker launch.")
    review_job = commands.add_parser("review-job", help="Render a read-only job for the exact review candidate.")
    review_job.add_argument("--attempt-id", required=True)
    review_job.add_argument("--candidate-revision", required=True)
    review_job.add_argument("--json", action="store_true")
    _select_command(review_job, cli_commands.ReviewJobCommand)
    dispatch.add_argument("--action-id", required=True, help="Exact dispatch action returned by project actions.")
    dispatch.add_argument("--expected-revision", required=True, help="Ledger revision from the dispatch action.")
    dispatch.add_argument("--task-id", required=True)
    dispatch.add_argument("--host-id", required=True)
    dispatch.add_argument("--checkpoint", required=True, help="Stable checkpoint ID in the canonical work brief.")
    dispatch.add_argument(
        "--environment",
        required=True,
        type=Path,
        help="pinboard-dispatch/v1 JSON declaring the checkout, branch, revision, and already-authorized permissions.",
    )
    dispatch.add_argument(
        "--prompt",
        type=Path,
        help="Verify this transported prompt instead of rendering the canonical prompt.",
    )
    dispatch.add_argument(
        "--brief-review",
        type=Path,
        help="Validate and publish one complete ready review for this exact cross-boundary checkpoint.",
    )
    dispatch.add_argument(
        "--review-id",
        help="Kebab-case identity used only when preserving a differing later review.",
    )
    dispatch.add_argument("--json", action="store_true")
    _select_command(dispatch, _CompoundCommand.DISPATCH)
    _add_attempt_parser(commands)
    _add_preparation_parser(commands)
    _add_parallel_parser(commands)
    views = commands.add_parser("views", help="Repair generated human-readable views.")
    rebuild = views.add_subparsers(required=True).add_parser("rebuild")
    _select_command(rebuild, cli_commands.RebuildViewsCommand)
    return parser


def _decode_invocation(
    parser: argparse.ArgumentParser,
    raw: _RawCliArguments,
) -> cli_commands.CliInvocation:
    command_selection = raw.command_selection
    selected_parser = raw.selected_parser
    if selected_parser is None or command_selection is None:
        parser.error("the selected command has no decoder")
    untyped_values = vars(raw).copy()
    for metadata_name in ("command_selection", "selected_parser", "contract_variants"):
        untyped_values.pop(metadata_name, None)
    try:
        root_values = {
            "project_root": untyped_values.pop("project_root", None),
            "work_root": untyped_values.pop("work_root", None),
        }
        roots = msgspec.convert(root_values, type=cli_commands.RootSelection, strict=True)
        command = _decode_selected_command(command_selection, untyped_values)
    except msgspec.ValidationError as error:
        selected_parser.error(str(error))
    return cli_commands.CliInvocation(roots, command)


def parse_invocation(argv: Sequence[str] | None = None) -> cli_commands.CliInvocation:
    """Parse one invocation without resolving roots or executing the command."""
    parser = build_parser()
    raw = parser.parse_args(argv, namespace=_RawCliArguments())
    return _decode_invocation(parser, raw)


def installed_command_variants() -> tuple[InstalledCommandVariant, ...]:
    """Read exact decoded variants from the installed parser leaves."""

    discovered: list[InstalledCommandVariant] = []

    def visit(parser: argparse.ArgumentParser) -> None:
        variants = parser.get_default("contract_variants")
        if variants:
            operation_id = parser.prog.removeprefix("pinboard ").replace(" ", "/")
            discovered.extend(
                InstalledCommandVariant(operation_id, variant, command_type) for variant, command_type in variants
            )
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    if isinstance(child, argparse.ArgumentParser):
                        visit(child)

    visit(build_parser())
    return tuple(discovered)
