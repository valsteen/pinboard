"""Static discovery of the installed human and maintenance CLI only."""

from pathlib import Path
from typing import Literal, get_args

import msgspec

from pinboard import __version__
from pinboard.cli import cli_commands, cli_parser
from pinboard.cli.cli_output import write_json
from pinboard.cli.errors import CommandFailure, CommandResult
from pinboard.domain.errors import (
    DecisionFailureCode,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    FailureMismatch,
    RetryDisposition,
)

type MutationClass = Literal["read-only", "mutates-ledger", "repairs-derived-views", "migrates-work-root"]
type DataScope = Literal["static", "focused", "current-project", "explicit-project-wide"]


class OperationIndexEntry(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation_id: str
    mutation_class: MutationClass
    data_scope: DataScope
    detail_selector: str


class ToolContractIndex(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-cli-tool-contract/v1"]
    tool_version: str
    operations: tuple[OperationIndexEntry, ...]
    presentation_selectors: tuple[str, ...]


class OperationContract(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-cli-tool-operation/v1"]
    operation_id: str
    cli_usage: str
    purpose: str
    mutation_class: MutationClass
    data_scope: DataScope
    permitted_roles: tuple[str, ...]
    required_authority: str
    subject_kind: str
    lifecycle_precondition: str
    input_schema: msgspec.Raw
    success_postcondition: str
    retry_semantics: str


class PresentationContract(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-cli-tool-presentation/v1"]
    presentation: Literal["root", "help", "version"]
    cli_usage: str
    success_postcondition: str


type ToolContractDetail = OperationContract | PresentationContract


def _unknown_selector(supplied: str) -> CommandFailure:
    return CommandFailure(
        DecisionFailureCode.TRANSITION_INPUT_INVALID,
        f"Unknown installed CLI operation: {supplied}",
        FailureDetails(
            observed=(FailureFact("operation", supplied),),
            mismatches=(FailureMismatch("operation", "selector returned by pinboard tool-contract --json", supplied),),
            retry=RetryDisposition.CORRECT_INPUT,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def _encoded_schema(command_type: type[cli_commands.CliCommand]) -> msgspec.Raw:
    def schema_hook(value_type: type) -> dict[str, str]:
        if value_type is Path:
            return {"format": "path", "type": "string"}
        raise TypeError(f"unsupported command input type: {value_type!r}")

    return msgspec.Raw(msgspec.json.encode(msgspec.json.schema(command_type, schema_hook=schema_hook), order="sorted"))


def _operation_contract(command: cli_parser.InstalledCommand) -> OperationContract:  # noqa: PLR0915 - exact installed semantic owner
    match command.operation_id:
        case "root":
            purpose = "Resolve the source checkout, shared repository, and Pinboard work root."
            mutation: MutationClass = "read-only"
            scope: DataScope = "focused"
            roles = ("observer",)
            authority = "none"
            subject = "repository-roots"
            precondition = "source-checkout-resolvable"
            postcondition = "Return the selected roots without opening project state."
            retry = "safe-to-repeat"
        case "tool-contract":
            purpose = "Discover the installed CLI grammar without opening project state."
            mutation = "read-only"
            scope = "static"
            roles = ("observer",)
            authority = "none"
            subject = "installed-cli"
            precondition = "none"
            postcondition = "Return the current CLI-only index or one exact selected detail."
            retry = "safe-to-repeat"
        case "status":
            purpose = "Read bounded current work facts."
            mutation = "read-only"
            scope = "current-project"
            roles = ("observer",)
            authority = "none"
            subject = "ledger"
            precondition = "valid-ledger"
            postcondition = "Return current active identities and maintained state counts without changing state."
            retry = "safe-to-repeat"
        case "validate":
            purpose = "Validate authoritative work state and its replaceable projections."
            mutation = "read-only"
            scope = "explicit-project-wide"
            roles = ("observer",)
            authority = "none"
            subject = "ledger"
            precondition = "source-checkout-resolvable"
            postcondition = "Return integrity and projection diagnostics without repairing state."
            retry = "safe-to-repeat"
        case "export":
            purpose = "Export one complete tool-neutral project package."
            mutation = "read-only"
            scope = "explicit-project-wide"
            roles = ("observer",)
            authority = "none"
            subject = "ledger"
            precondition = "valid-ledger"
            postcondition = (
                "Return verified portable lifecycle, history, and immutable artifact evidence without changing state."
            )
            retry = "safe-to-repeat"
        case "init":
            purpose = "Create or verify an empty current Pinboard work state."
            mutation = "mutates-ledger"
            scope = "explicit-project-wide"
            roles = ("local-caller",)
            authority = "filesystem-access"
            subject = "work-root"
            precondition = "source-checkout-resolvable"
            postcondition = "Return work_root, resumed state, and optional next guidance; default initialization also owns its exact local Git exclusion."
            retry = "inspect-current-state-before-retry"
        case "migrate-work-root":
            purpose = "Move verified legacy project state to .pinboard and install its compatibility alias."
            mutation = "migrates-work-root"
            scope = "explicit-project-wide"
            roles = ("local-caller",)
            authority = "filesystem-and-repository-git-exclude-access"
            subject = "work-root"
            precondition = "default-root-state-is-legacy-current-or-exact-compatibility-alias"
            postcondition = "Preserve ledger and artifact bytes while establishing .pinboard and the exact relative compatibility alias."
            retry = "inspect-current-state-before-retry"
        case "close":
            purpose = "Record a terminal decision for eligible live work without an accepted attempt."
            mutation = "mutates-ledger"
            scope = "focused"
            roles = ("project",)
            authority = "direct-project-operation-with-task-host-attribution"
            subject = "item"
            precondition = "item-without-attempt"
            postcondition = (
                "Commit exactly one legal close with invoking actor attribution and return its committed revision."
            )
            retry = "inspect-current-state-before-retry"
        case "views/rebuild":
            purpose = "Rebuild replaceable human-readable views from authoritative state."
            mutation = "repairs-derived-views"
            scope = "explicit-project-wide"
            roles = ("observer",)
            authority = "filesystem-access"
            subject = "ledger"
            precondition = "valid-ledger"
            postcondition = (
                "Make declared generated views match current authoritative state without changing that authority."
            )
            retry = "safe-to-repeat"
        case _:
            raise ValueError(f"missing installed CLI semantic classification: {command.operation_id}")
    return OperationContract(
        "pinboard-cli-tool-operation/v1",
        command.operation_id,
        command.cli_usage,
        purpose,
        mutation,
        scope,
        roles,
        authority,
        subject,
        precondition,
        _encoded_schema(command.command_type),
        postcondition,
        retry,
    )


def installed_tool_contract() -> ToolContractIndex:
    commands = cli_parser.installed_commands()
    command_types = tuple(command.command_type for command in commands)
    if len(set(command_types)) != len(command_types) or set(command_types) != set(
        get_args(cli_commands.CliCommand.__value__)
    ):
        raise ValueError("installed parser must select every exact CLI command once")
    operations = tuple(_operation_contract(command) for command in commands)
    return ToolContractIndex(
        "pinboard-cli-tool-contract/v1",
        __version__,
        tuple(
            OperationIndexEntry(
                value.operation_id, value.mutation_class, value.data_scope, f"--operation {value.operation_id}"
            )
            for value in operations
        ),
        ("presentation/root", "presentation/help", "presentation/version"),
    )


def describe_operation(operation_id: str) -> CommandResult[ToolContractDetail]:
    match operation_id:
        case "presentation/root":
            return PresentationContract(
                "pinboard-cli-tool-presentation/v1",
                "root",
                "pinboard",
                "Reject with root usage and exit status 2 before project state.",
            )
        case "presentation/help":
            return PresentationContract(
                "pinboard-cli-tool-presentation/v1",
                "help",
                "pinboard --help",
                "Print installed help and exit successfully before project state.",
            )
        case "presentation/version":
            return PresentationContract(
                "pinboard-cli-tool-presentation/v1",
                "version",
                "pinboard --version",
                "Print installed version and exit successfully before project state.",
            )
    selected = tuple(command for command in cli_parser.installed_commands() if command.operation_id == operation_id)
    if len(selected) != 1:
        return _unknown_selector(operation_id)
    return _operation_contract(selected[0])


def operation_identity(command: cli_commands.CliCommand) -> str:
    selected = tuple(value for value in cli_parser.installed_commands() if type(command) is value.command_type)
    if len(selected) != 1:
        raise ValueError(f"decoded command has no unique installed operation: {type(command).__name__}")
    return selected[0].operation_id


def select_tool_contract(
    command: cli_commands.ToolContractCommand,
) -> CommandResult[ToolContractIndex | ToolContractDetail]:
    return installed_tool_contract() if command.operation is None else describe_operation(command.operation)


def show_tool_contract(command: cli_commands.ToolContractCommand) -> CommandResult[int]:
    selected = select_tool_contract(command)
    if isinstance(selected, CommandFailure):
        return selected
    if command.json:
        write_json(selected)
    elif isinstance(selected, ToolContractIndex):
        print(f"OK TOOL_CONTRACT operations={len(selected.operations)}")
        print("Use --json for the CLI-only index and a returned selector for exact execution facts.")
    elif isinstance(selected, PresentationContract):
        print(f"OK TOOL_CONTRACT_PRESENTATION presentation={selected.presentation}")
        print(f"postcondition={selected.success_postcondition}")
    else:
        print(f"OK TOOL_CONTRACT_DETAIL identity={selected.operation_id} mutation_class={selected.mutation_class}")
        print(f"purpose={selected.purpose}")
        print(f"retry_semantics={selected.retry_semantics}")
    return 0
