"""Versioned read-only discovery for the installed CLI and lifecycle actions."""

from collections.abc import Sequence
from pathlib import Path
from types import UnionType
from typing import Literal, TypeAliasType, assert_never, get_args

import msgspec

from pinboard import __version__
from pinboard.application import dispatch_models
from pinboard.domain import decision_models
from pinboard.domain.errors import DecisionFailureCode
from pinboard.interfaces import (
    brief_source_models,
    cli_commands,
    cli_parser,
    proposal_models,
    transition_input,
    transition_models,
    work_brief_contract,
)
from pinboard.interfaces.cli_output import write_json
from pinboard.interfaces.errors import CommandFailure, CommandResult

type MutationClass = Literal[
    "read-only",
    "mutates-ledger",
    "publishes-and-records-artifact",
    "may-publish-and-record-artifact",
    "repairs-derived-views",
]
type ActionExecutionRoute = Literal[
    "transition",
    "dispatch",
    "overview",
    "runtime-continuation",
    "runtime-blocker-artifact",
]


class OperationIndexEntry(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation_id: str
    variant: str
    mutation_class: MutationClass
    detail_selector: str


class OperationVariantIndex(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-agent-tool-operation-variants/v1"]
    operation_id: str
    variants: tuple[OperationIndexEntry, ...]
    selection_rule: str


class ActionIndexEntry(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_kind: str
    mutation_class: MutationClass
    execution_route: ActionExecutionRoute
    detail_selector: str


class PresentationIndexEntry(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    presentation: str
    detail_selector: str


class BriefStarterIndexEntry(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    boundary: cli_commands.BriefBoundary
    detail_selector: str


class ToolContractIndex(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-agent-tool-contract/v1"]
    tool_version: str
    operations: tuple[OperationIndexEntry, ...]
    actions: tuple[ActionIndexEntry, ...]
    brief_starters: tuple[BriefStarterIndexEntry, ...]
    presentations: tuple[PresentationIndexEntry, ...]


class OperationContract(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-agent-tool-operation/v1"]
    operation_id: str
    variant: str
    cli_usage: str
    purpose: str
    mutation_class: MutationClass
    permitted_roles: tuple[str, ...]
    required_authority: str
    subject_kind: str
    lifecycle_precondition: str
    input_schema: msgspec.Raw | None
    artifact_selector: str | None
    artifact_schema: msgspec.Raw | None
    work_brief: work_brief_contract.WorkBriefContract | None
    success_postcondition: str
    retry_semantics: str


class ActionContract(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-agent-tool-action/v1"]
    action_kind: str
    purpose: str
    mutation_class: MutationClass
    execution_route: ActionExecutionRoute
    lifecycle_effect: str
    permitted_roles: tuple[str, ...]
    required_authority: str
    subject_kind: str
    lifecycle_precondition: str
    input_schema: msgspec.Raw | None
    artifact_selector: str | None
    success_postcondition: str
    retry_semantics: str


class PresentationContract(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-agent-tool-presentation/v1"]
    presentation: str
    purpose: str
    mutation_class: Literal["read-only"]
    permitted_roles: tuple[str, ...]
    required_authority: Literal["none"]
    subject_kind: Literal["installed-cli"]
    lifecycle_precondition: Literal["none"]
    input_schema: msgspec.Raw
    artifact_selector: None
    success_postcondition: str
    retry_semantics: Literal["safe-to-repeat"]


type ToolContractDetail = (
    OperationContract
    | OperationVariantIndex
    | ActionContract
    | PresentationContract
    | work_brief_contract.WorkBriefStarterContract
)
type OperationDetail = OperationContract | PresentationContract
type OperationKey = tuple[str, str]


class UnknownToolContractSelector(ValueError):
    pass


def _mutation_class(command_type: type[cli_commands.CliCommand]) -> MutationClass:
    if command_type in (
        cli_commands.ItemReviseCommand,
        cli_commands.CloseCommand,
        cli_commands.InitializeCommand,
        cli_commands.ProposalCommand,
        cli_commands.ProjectTransitionCommand,
        cli_commands.AttemptTransitionCommand,
        cli_commands.PreparationTransitionCommand,
        cli_commands.AttemptAcquireCommand,
        cli_commands.AttemptRenewCommand,
        cli_commands.AttemptReleaseCommand,
        cli_commands.AttemptRevokeCommand,
        cli_commands.PreparationStartCommand,
        cli_commands.PreparationAcquireCommand,
        cli_commands.PreparationTransferCommand,
        cli_commands.PreparationRenewCommand,
        cli_commands.PreparationReleaseCommand,
        cli_commands.PreparationRevokeCommand,
    ):
        return "mutates-ledger"
    if command_type is cli_commands.BriefPublishCommand:
        return "publishes-and-records-artifact"
    if command_type is cli_commands.ProjectReviewedDispatchCommand:
        return "may-publish-and-record-artifact"
    if command_type is cli_commands.RebuildViewsCommand:
        return "repairs-derived-views"
    if command_type in (
        cli_commands.RootCommand,
        cli_commands.ValidateCommand,
        cli_commands.StatusCommand,
        cli_commands.OverviewCommand,
        cli_commands.ItemStatusCommand,
        cli_commands.ItemDefinitionCommand,
        cli_commands.ItemDefinitionHistoryCommand,
        cli_commands.ActionsCommand,
        cli_commands.LeasedActionsCommand,
        cli_commands.InputContractCommand,
        cli_commands.ToolContractCommand,
        cli_commands.BriefSourcesPlanCommand,
        cli_commands.BriefSourcesEmitCommand,
        cli_commands.HandoverCommand,
        cli_commands.ProjectDispatchCommand,
        cli_commands.AttemptStatusCommand,
        cli_commands.AttemptInspectCommand,
        cli_commands.ReviewJobCommand,
        cli_commands.PreparationStatusCommand,
        cli_commands.ParallelPreviewCommand,
    ):
        return "read-only"
    raise ValueError(f"missing operation classification: {command_type.__name__}")


def _purpose(operation_id: str, variant: str) -> str:  # noqa: C901, PLR0912 - exhaustive installed purpose owner
    if operation_id == "transition":
        return f"Execute one freshly selected lifecycle action using {variant} authority."
    if operation_id == "actions":
        return f"Discover fresh legal actions using {variant} authority facts."
    if operation_id == "brief-sources":
        return f"{variant.capitalize()} deterministic, bounded accepted-authority source batches."
    if operation_id == "dispatch":
        return f"Prepare a candidate-bound worker launch {variant.replace('-', ' ')}."
    if operation_id.startswith("attempt/"):
        return f"{operation_id.removeprefix('attempt/').capitalize()} one attempt authority claim."
    if operation_id.startswith("preparation/"):
        return f"{operation_id.removeprefix('preparation/').capitalize()} one preparation authority claim."
    if operation_id.startswith("item/"):
        return f"{operation_id.removeprefix('item/').replace('-', ' ').capitalize()} for one work item."
    match operation_id:
        case "root":
            return "Resolve the source checkout, shared repository, and Pinboard work root."
        case "validate":
            return "Validate authoritative work state and its replaceable projections."
        case "status":
            return "Read bounded current work facts."
        case "overview":
            return "Read one coherent live-work overview."
        case "close":
            return "Record a terminal decision for eligible non-active work."
        case "input-contract":
            return "Read the canonical transition payload schema for one action kind."
        case "tool-contract":
            return "Discover the complete installed command and action contract."
        case "handover":
            return "Export one complete tool-neutral project handover."
        case "init":
            return "Create or verify an empty current Pinboard work state."
        case "brief/publish":
            return "Validate, publish, and accept one canonical work brief."
        case "proposal":
            return "Preserve one proposal as an intake item."
        case "review-job":
            return "Render a candidate-bound read-only review job."
        case "parallel/preview":
            return "Preview structurally independent work without launching it."
        case "views/rebuild":
            return "Rebuild replaceable human-readable views from authoritative state."
        case _:
            raise ValueError(f"missing operation purpose classification: {operation_id}:{variant}")


def _roles_and_authority(
    command_type: type[cli_commands.CliCommand],
) -> tuple[tuple[str, ...], str]:
    if command_type in (
        cli_commands.AttemptTransitionCommand,
        cli_commands.AttemptAcquireCommand,
        cli_commands.AttemptRenewCommand,
        cli_commands.AttemptReleaseCommand,
    ):
        return ("worker",), "attempt-lease"
    if command_type in (
        cli_commands.PreparationTransitionCommand,
        cli_commands.PreparationStartCommand,
        cli_commands.PreparationAcquireCommand,
        cli_commands.PreparationTransferCommand,
        cli_commands.PreparationRenewCommand,
        cli_commands.PreparationReleaseCommand,
    ):
        return ("preparer",), "preparation-lease"
    if command_type is cli_commands.LeasedActionsCommand:
        return ("worker", "preparer"), "selected-lease"
    if command_type is cli_commands.ActionsCommand:
        return tuple(role.value for role in decision_models.Role), "role-selection"
    if command_type in (
        cli_commands.ItemReviseCommand,
        cli_commands.CloseCommand,
        cli_commands.ProposalCommand,
        cli_commands.ProjectTransitionCommand,
        cli_commands.ProjectDispatchCommand,
        cli_commands.ProjectReviewedDispatchCommand,
        cli_commands.AttemptRevokeCommand,
        cli_commands.PreparationRevokeCommand,
    ):
        return ("project",), "direct-project-operation-with-task-host-attribution"
    if command_type is cli_commands.BriefPublishCommand:
        return ("local-caller",), "validated-brief-identity"
    if command_type is cli_commands.InitializeCommand:
        return ("local-caller",), "filesystem-access"
    return ("observer",), "none"


def _subject_and_precondition(
    operation_id: str,
    command_type: type[cli_commands.CliCommand],
) -> tuple[str, str]:
    if command_type in (
        cli_commands.AttemptAcquireCommand,
        cli_commands.AttemptRenewCommand,
        cli_commands.AttemptReleaseCommand,
        cli_commands.AttemptRevokeCommand,
        cli_commands.AttemptStatusCommand,
        cli_commands.AttemptInspectCommand,
        cli_commands.ReviewJobCommand,
    ):
        return "attempt", "attempt-exists"
    if command_type in (
        cli_commands.PreparationStartCommand,
        cli_commands.PreparationAcquireCommand,
        cli_commands.PreparationTransferCommand,
        cli_commands.PreparationRenewCommand,
        cli_commands.PreparationReleaseCommand,
        cli_commands.PreparationRevokeCommand,
        cli_commands.PreparationStatusCommand,
    ):
        return "item", "ready-item"
    if operation_id.startswith("item/") or command_type is cli_commands.CloseCommand:
        return "item", "item-exists"
    if command_type in (
        cli_commands.ProjectTransitionCommand,
        cli_commands.AttemptTransitionCommand,
        cli_commands.PreparationTransitionCommand,
    ):
        return "action-subject", "selected-action-remains-legal"
    if command_type in (cli_commands.ProjectDispatchCommand, cli_commands.ProjectReviewedDispatchCommand):
        return "attempt", "active-attempt-current-scope"
    if command_type is cli_commands.ProposalCommand:
        return "proposal", "valid-ledger"
    if command_type is cli_commands.InitializeCommand:
        return "work-root", "source-checkout-resolvable"
    if command_type is cli_commands.RootCommand:
        return "repository-roots", "source-checkout-resolvable"
    if command_type in (cli_commands.BriefSourcesPlanCommand, cli_commands.BriefSourcesEmitCommand):
        return "source-manifest", "selected-source-checkout-readable"
    if command_type is cli_commands.BriefPublishCommand:
        return "brief-artifact", "canonical-brief-valid"
    return "ledger", "valid-ledger" if operation_id not in {
        "tool-contract",
        "input-contract",
        "brief-sources",
    } else "none"


def _artifact_selector(command_type: type[cli_commands.CliCommand]) -> str | None:
    if command_type is cli_commands.ItemReviseCommand:
        return "pinboard-item-revision/v1 file"
    if command_type in (cli_commands.BriefSourcesPlanCommand, cli_commands.BriefSourcesEmitCommand):
        return "pinboard-brief-sources/v1 file"
    if command_type is cli_commands.BriefPublishCommand:
        return "pinboard-work-brief/v2 file"
    if command_type is cli_commands.ProposalCommand:
        return "pinboard-proposal/v1 file"
    if command_type in (cli_commands.ProjectDispatchCommand, cli_commands.ProjectReviewedDispatchCommand):
        return "pinboard-dispatch/v1 environment file; optional canonical prompt and ready-review files"
    if command_type in (
        cli_commands.ProjectTransitionCommand,
        cli_commands.AttemptTransitionCommand,
        cli_commands.PreparationTransitionCommand,
    ):
        return "payload schema selected by the fresh action kind"
    return None


def _artifact_schema(command_type: type[cli_commands.CliCommand]) -> msgspec.Raw | None:
    if command_type is cli_commands.ItemReviseCommand:
        model = transition_models.ReviseItemInputPayload
    elif command_type in (cli_commands.BriefSourcesPlanCommand, cli_commands.BriefSourcesEmitCommand):
        model = brief_source_models.BriefSourceManifest
    elif command_type is cli_commands.BriefPublishCommand:
        return None
    elif command_type is cli_commands.ProposalCommand:
        model = proposal_models.Proposal
    elif command_type in (cli_commands.ProjectDispatchCommand, cli_commands.ProjectReviewedDispatchCommand):
        model = dispatch_models.DispatchEnvironment
    else:
        return None
    return msgspec.Raw(msgspec.json.encode(msgspec.json.schema(model), order="sorted"))


def _success_postcondition(mutation_class: MutationClass) -> str:
    match mutation_class:
        case "read-only":
            return "Return current output without changing authoritative or replaceable state."
        case "mutates-ledger":
            return "Commit exactly one accepted change and return its committed revision."
        case "publishes-and-records-artifact":
            return "Publish canonical immutable bytes and accept their exact artifact reference."
        case "may-publish-and-record-artifact":
            return "Return the verified launch; publish only explicitly supplied independent review evidence."
        case "repairs-derived-views":
            return "Make replaceable views match the current authoritative state."


def _retry_semantics(operation_id: str, mutation_class: MutationClass) -> str:
    if operation_id == "transition":
        return "never-retry-with-stale-action-facts"
    if mutation_class == "read-only" or mutation_class == "repairs-derived-views":
        return "safe-to-repeat"
    if mutation_class == "publishes-and-records-artifact":
        return "repeat-only-with-the-same-canonical-artifact"
    return "inspect-current-state-before-retry"


def _encoded_schema(command_type: type[cli_commands.CliCommand]) -> msgspec.Raw:
    def schema_hook(value_type: type) -> dict[str, str]:
        if value_type is Path:
            return {"format": "path", "type": "string"}
        raise TypeError(f"unsupported command input type: {value_type!r}")

    return msgspec.Raw(msgspec.json.encode(msgspec.json.schema(command_type, schema_hook=schema_hook), order="sorted"))


def _closed_command_types(value: TypeAliasType | UnionType | type) -> tuple[type, ...]:
    if isinstance(value, TypeAliasType):
        return _closed_command_types(value.__value__)
    arguments = get_args(value)
    if arguments:
        return tuple(command_type for argument in arguments for command_type in _closed_command_types(argument))
    if isinstance(value, type):
        return (value,)
    raise TypeError(f"unsupported command union member: {value!r}")


def _operation_contract(variant: cli_parser.InstalledCommandVariant) -> OperationContract:
    mutation_class = _mutation_class(variant.command_type)
    roles, authority = _roles_and_authority(variant.command_type)
    subject, precondition = _subject_and_precondition(variant.operation_id, variant.command_type)
    return OperationContract(
        "pinboard-agent-tool-operation/v1",
        variant.operation_id,
        variant.variant,
        variant.cli_usage,
        _purpose(variant.operation_id, variant.variant),
        mutation_class,
        roles,
        authority,
        subject,
        precondition,
        _encoded_schema(variant.command_type),
        _artifact_selector(variant.command_type),
        _artifact_schema(variant.command_type),
        work_brief_contract.describe_work_brief_contract()
        if variant.command_type is cli_commands.BriefPublishCommand
        else None,
        _success_postcondition(mutation_class),
        _retry_semantics(variant.operation_id, mutation_class),
    )


def _operation_index_entry(variant: cli_parser.InstalledCommandVariant) -> OperationIndexEntry:
    return OperationIndexEntry(
        variant.operation_id,
        variant.variant,
        _mutation_class(variant.command_type),
        f"--operation {variant.operation_id}{'' if variant.variant == 'default' else f':{variant.variant}'}",
    )


def _action_mutation_class(kind: decision_models.ActionKind) -> MutationClass:
    if kind == decision_models.ActionKind.DISPATCH:
        return "may-publish-and-record-artifact"
    if decision_models.action_semantics(kind).lifecycle_effect == decision_models.LifecycleEffect.CHANGES_LIFECYCLE:
        return "mutates-ledger"
    return "read-only"


def _action_execution_route(kind: decision_models.ActionKind) -> ActionExecutionRoute:
    match kind:
        case (
            decision_models.ActionKind.ACCEPT_CHECKPOINT
            | decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE
            | decision_models.ActionKind.ACCEPT_PROPOSAL
            | decision_models.ActionKind.ACTIVATE
            | decision_models.ActionKind.BLOCK
            | decision_models.ActionKind.BLOCK_ITEM
            | decision_models.ActionKind.COMPLETE
            | decision_models.ActionKind.CLOSE
            | decision_models.ActionKind.DEFER
            | decision_models.ActionKind.MARK_READY
            | decision_models.ActionKind.MERGE_PROPOSAL
            | decision_models.ActionKind.PAUSE
            | decision_models.ActionKind.REJECT_PROPOSAL
            | decision_models.ActionKind.REOPEN
            | decision_models.ActionKind.REBIND_ATTEMPT
            | decision_models.ActionKind.RESUME
            | decision_models.ActionKind.RETURN_FOR_CORRECTION
            | decision_models.ActionKind.RETURN_PROPOSAL
            | decision_models.ActionKind.REVISE_ITEM
            | decision_models.ActionKind.SUBMIT_REVIEW
        ):
            return "transition"
        case decision_models.ActionKind.DISPATCH:
            return "dispatch"
        case decision_models.ActionKind.INSPECT:
            return "overview"
        case decision_models.ActionKind.CONTINUE:
            return "runtime-continuation"
        case decision_models.ActionKind.REPORT_BLOCKER:
            return "runtime-blocker-artifact"
        case _ as unreachable:
            assert_never(unreachable)


def _action_authority(roles: tuple[decision_models.Role, ...]) -> str:
    if roles == (decision_models.Role.WORKER,):
        return "attempt-lease"
    if roles == (decision_models.Role.PREPARER,):
        return "preparation-lease"
    if roles == (decision_models.Role.OBSERVER,):
        return "none"
    if roles == (decision_models.Role.PROJECT,):
        return "direct-project-operation-with-task-host-attribution"
    return "selected-role-authority"


def describe_action(kind: decision_models.ActionKind) -> ActionContract:
    semantics = decision_models.action_semantics(kind)
    input_schema: msgspec.Raw | None = None
    if semantics.lifecycle_effect == decision_models.LifecycleEffect.CHANGES_LIFECYCLE:
        encoded = transition_input.encoded_transition_input_schema(kind)
        if not isinstance(encoded, bytes):
            raise ValueError(str(encoded))
        input_schema = msgspec.Raw(encoded)
    mutation_class = _action_mutation_class(kind)
    return ActionContract(
        "pinboard-agent-tool-action/v1",
        kind.value,
        semantics.use_case,
        mutation_class,
        _action_execution_route(kind),
        semantics.lifecycle_effect.value,
        tuple(role.value for role in semantics.permitted_roles),
        _action_authority(semantics.permitted_roles),
        semantics.subject_kind.value,
        semantics.lifecycle_precondition.value,
        input_schema,
        "pinboard-dispatch/v1 environment file" if kind == decision_models.ActionKind.DISPATCH else None,
        semantics.practical_result,
        "reselect-after-any-rejection" if mutation_class != "read-only" else "safe-to-repeat-after-fresh-inspection",
    )


def _presentation_contract(presentation: str) -> PresentationContract:
    match presentation:
        case "root":
            purpose = "Present the installed CLI root and require one exact command."
            schema = msgspec.Raw(b'{"maxItems":0,"type":"array"}')
            postcondition = "Reject with root usage and exit status 2 without opening project state."
        case "help":
            purpose = "Present installed command help without opening project state."
            schema = msgspec.Raw(b'{"const":["--help"]}')
            postcondition = "Print installed help and exit successfully without opening project state."
        case "version":
            purpose = "Present the installed Pinboard version without opening project state."
            schema = msgspec.Raw(b'{"const":["--version"]}')
            postcondition = "Print the installed version and exit successfully without opening project state."
        case _:
            raise UnknownToolContractSelector(f"unknown installed presentation: {presentation}")
    return PresentationContract(
        "pinboard-agent-tool-presentation/v1",
        presentation,
        purpose,
        "read-only",
        ("observer",),
        "none",
        "installed-cli",
        "none",
        schema,
        None,
        postcondition,
        "safe-to-repeat",
    )


def validate_contract_inventory(
    installed_operations: Sequence[OperationKey],
    classified_operations: Sequence[OperationKey],
    installed_actions: Sequence[str],
    classified_actions: Sequence[str],
) -> None:
    def validate(label: str, installed: Sequence[str | OperationKey], classified: Sequence[str | OperationKey]) -> None:
        duplicate = next((value for value in classified if classified.count(value) > 1), None)
        if duplicate is not None:
            raise ValueError(f"duplicate {label} classification: {duplicate}")
        missing = set(installed) - set(classified)
        if missing:
            raise ValueError(f"missing {label} classification: {sorted(missing)!r}")
        unknown = set(classified) - set(installed)
        if unknown:
            raise ValueError(f"unknown {label} classification: {sorted(unknown)!r}")

    validate("operation", installed_operations, classified_operations)
    validate("action", installed_actions, classified_actions)


def installed_tool_contract() -> ToolContractIndex:
    installed = cli_parser.installed_command_variants()
    installed_types = tuple(variant.command_type for variant in installed)
    union_types = _closed_command_types(cli_commands.CliCommand)
    missing_types = set(union_types) - set(installed_types)
    unknown_types = set(installed_types) - set(union_types)
    duplicate_type = next((value for value in installed_types if installed_types.count(value) > 1), None)
    if missing_types:
        raise ValueError(
            f"missing parser operation classification: {sorted(value.__name__ for value in missing_types)!r}"
        )
    if unknown_types:
        raise ValueError(
            f"unknown parser operation classification: {sorted(value.__name__ for value in unknown_types)!r}"
        )
    if duplicate_type is not None:
        raise ValueError(f"duplicate parser command classification: {duplicate_type.__name__}")
    operations = tuple(_operation_index_entry(variant) for variant in installed)
    actions = tuple(
        ActionIndexEntry(
            kind.value,
            _action_mutation_class(kind),
            _action_execution_route(kind),
            f"--action-kind {kind.value}",
        )
        for kind in decision_models.ActionKind
    )
    installed_keys = tuple((variant.operation_id, variant.variant) for variant in installed)
    operation_keys = tuple((entry.operation_id, entry.variant) for entry in operations)
    action_kinds = tuple(kind.value for kind in decision_models.ActionKind)
    validate_contract_inventory(
        installed_keys, operation_keys, action_kinds, tuple(entry.action_kind for entry in actions)
    )
    return ToolContractIndex(
        "pinboard-agent-tool-contract/v1",
        __version__,
        operations,
        actions,
        tuple(
            BriefStarterIndexEntry(boundary, f"--brief-starter {boundary}")
            for boundary in cli_commands.BRIEF_BOUNDARIES
        ),
        tuple(PresentationIndexEntry(name, f"--operation presentation/{name}") for name in ("root", "help", "version")),
    )


def describe_operation(operation_id: str, variant: str) -> OperationDetail:
    if operation_id.startswith("presentation/"):
        return _presentation_contract(operation_id.removeprefix("presentation/"))
    selected = tuple(
        candidate
        for candidate in cli_parser.installed_command_variants()
        if candidate.operation_id == operation_id and candidate.variant == variant
    )
    if len(selected) != 1:
        raise UnknownToolContractSelector(f"unknown installed operation: {operation_id}:{variant}")
    return _operation_contract(selected[0])


def describe_operation_selector(operation_id: str) -> OperationContract | OperationVariantIndex:
    selected = tuple(
        candidate for candidate in cli_parser.installed_command_variants() if candidate.operation_id == operation_id
    )
    if len(selected) == 1 and selected[0].variant == "default":
        return _operation_contract(selected[0])
    if selected:
        return OperationVariantIndex(
            "pinboard-agent-tool-operation-variants/v1",
            operation_id,
            tuple(_operation_index_entry(candidate) for candidate in selected),
            "Choose the detail_selector whose variant matches the action authority or artifact path, then request it exactly.",
        )
    raise UnknownToolContractSelector(f"unknown installed operation: {operation_id}")


def operation_identity(command: cli_commands.CliCommand) -> str:
    """Return the installed operation selector for one already decoded command."""
    selected = tuple(
        candidate for candidate in cli_parser.installed_command_variants() if type(command) is candidate.command_type
    )
    if len(selected) != 1:
        raise ValueError(f"decoded command has no unique installed operation: {type(command).__name__}")
    variant = selected[0]
    return variant.operation_id if variant.variant == "default" else f"{variant.operation_id}:{variant.variant}"


def select_tool_contract(command: cli_commands.ToolContractCommand) -> ToolContractIndex | ToolContractDetail:
    if command.brief_starter is not None:
        return work_brief_contract.describe_work_brief_starter(command.brief_starter)
    if command.action_kind is not None:
        return describe_action(command.action_kind)
    if command.operation is None:
        return installed_tool_contract()
    operation_id, separator, variant = command.operation.partition(":")
    return describe_operation(operation_id, variant) if separator else describe_operation_selector(operation_id)


def show_tool_contract(command: cli_commands.ToolContractCommand) -> CommandResult[int]:
    try:
        selected = select_tool_contract(command)
    except UnknownToolContractSelector as error:
        return CommandFailure(DecisionFailureCode.TRANSITION_INPUT_INVALID, str(error), None)
    if command.json:
        write_json(selected)
    elif isinstance(selected, ToolContractIndex):
        print(
            f"OK TOOL_CONTRACT operations={len(selected.operations)} "
            f"actions={len(selected.actions)} brief_starters={len(selected.brief_starters)} "
            f"presentations={len(selected.presentations)}"
        )
        print("Use --json for the compact index and one returned detail selector for exact execution facts.")
    elif isinstance(selected, OperationVariantIndex):
        print(f"OK TOOL_CONTRACT_VARIANTS operation={selected.operation_id} variants={len(selected.variants)}")
        print("selectors=" + ",".join(variant.detail_selector for variant in selected.variants))
    else:
        if isinstance(selected, ActionContract):
            identity = selected.action_kind
            mutation_class = selected.mutation_class
            purpose = selected.purpose
            retry_semantics = selected.retry_semantics
        elif isinstance(selected, PresentationContract):
            identity = selected.presentation
            mutation_class = selected.mutation_class
            purpose = selected.purpose
            retry_semantics = selected.retry_semantics
        elif isinstance(selected, work_brief_contract.WorkBriefStarterContract):
            identity = f"brief-starter:{selected.boundary}"
            mutation_class = "read-only"
            purpose = "Return one complete unresolved work-brief starter without the full schema."
            retry_semantics = "safe-to-repeat"
        else:
            identity = f"{selected.operation_id}:{selected.variant}"
            mutation_class = selected.mutation_class
            purpose = selected.purpose
            retry_semantics = selected.retry_semantics
        print(f"OK TOOL_CONTRACT_DETAIL identity={identity} mutation_class={mutation_class}")
        print(f"purpose={purpose}")
        print(f"retry_semantics={retry_semantics}")
    return 0
