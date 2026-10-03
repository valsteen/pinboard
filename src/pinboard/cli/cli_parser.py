"""Installed human and maintenance grammar; decoding performs no effects."""

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import msgspec

from pinboard import __version__
from pinboard.cli import cli_commands
from pinboard.domain import work_models


class _RawCliArguments(argparse.Namespace):
    command_selection: type[cli_commands.CliCommand] | None
    selected_parser: argparse.ArgumentParser | None

    def __init__(self) -> None:
        super().__init__()
        self.command_selection = None
        self.selected_parser = None


@dataclass(frozen=True, slots=True)
class InstalledCommand:
    operation_id: str
    command_type: type[cli_commands.CliCommand]
    cli_usage: str


def _select_command(parser: argparse.ArgumentParser, command_type: type[cli_commands.CliCommand]) -> None:
    parser.set_defaults(command_selection=command_type, selected_parser=parser)


def _add_root_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-root", type=Path, help="Select the exact source checkout.")
    parser.add_argument("--work-root", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pinboard", description="Inspect and maintain one pinboard.")
    parser.add_argument("--version", action="version", version=__version__)
    _add_root_selection(parser)
    commands = parser.add_subparsers(required=True)
    root = commands.add_parser("root", help="Resolve source checkout, shared repository, and work roots.")
    _select_command(root, cli_commands.RootCommand)
    validate = commands.add_parser("validate", help="Validate work state without modifying it.")
    validate.add_argument("--json", action="store_true")
    _select_command(validate, cli_commands.ValidateCommand)
    status = commands.add_parser("status", help="Show bounded current work facts.")
    status.add_argument("--json", action="store_true")
    _select_command(status, cli_commands.StatusCommand)
    diagnose = commands.add_parser(
        "diagnose", help="Read selected roots, unfinished work, recent receipts, and full validation health."
    )
    diagnose.add_argument("--json", action="store_true", required=True)
    _select_command(diagnose, cli_commands.DiagnoseCommand)
    close = commands.add_parser("close", help="Record a terminal decision for live work without an accepted attempt.")
    close.add_argument("item_id")
    close.add_argument("--outcome", choices=tuple(outcome.value for outcome in work_models.CloseOutcome), required=True)
    close.add_argument("--reason", required=True)
    close.add_argument("--task-id", required=True)
    close.add_argument("--host-id", required=True)
    close.add_argument("--json", action="store_true")
    _select_command(close, cli_commands.CloseCommand)
    tool_contract = commands.add_parser("tool-contract", help="Discover installed CLI grammar without project state.")
    tool_contract.add_argument("--operation", help="One operation selector returned by the installed index.")
    tool_contract.add_argument("--json", action="store_true")
    _select_command(tool_contract, cli_commands.ToolContractCommand)
    code_catalog = commands.add_parser(
        "code-catalog", help="Look up installed failure and event codes without project state."
    )
    code_catalog.add_argument("--code", help="One exact code returned by the installed catalog.")
    code_catalog.add_argument("--json", action="store_true", required=True)
    _select_command(code_catalog, cli_commands.CodeCatalogCommand)
    export = commands.add_parser("export", help="Export one complete tool-neutral project package.")
    export.add_argument("--json", action="store_true", required=True)
    _select_command(export, cli_commands.ExportCommand)
    initialize = commands.add_parser("init", help="Create an empty current SQLite work state.")
    initialize.add_argument("--json", action="store_true")
    _select_command(initialize, cli_commands.InitializeCommand)
    migrate = commands.add_parser("migrate-work-root", help="Move legacy project state to .pinboard explicitly.")
    migrate.add_argument("--json", action="store_true")
    migrate_action = migrate.add_mutually_exclusive_group()
    migrate_action.add_argument("--apply", metavar="PLAN_ID")
    migrate_action.add_argument("--reverse", metavar="FORWARD_PLAN_ID")
    _select_command(migrate, cli_commands.MigrateWorkRootPreviewCommand)
    migrate_schema = commands.add_parser("migrate-schema", help="Upgrade a verified v6 ledger to v7 explicitly.")
    migrate_schema.add_argument("--json", action="store_true")
    schema_action = migrate_schema.add_mutually_exclusive_group()
    schema_action.add_argument("--apply", metavar="PLAN_ID")
    schema_action.add_argument("--reverse", metavar="FORWARD_PLAN_ID")
    _select_command(migrate_schema, cli_commands.MigrateSchemaPreviewCommand)
    views = commands.add_parser("views", help="Repair generated human-readable views.")
    rebuild = views.add_subparsers(required=True).add_parser("rebuild")
    _select_command(rebuild, cli_commands.RebuildViewsCommand)
    return parser


def parse_invocation(argv: Sequence[str] | None = None) -> cli_commands.CliInvocation:
    parser = build_parser()
    raw = parser.parse_args(argv, namespace=_RawCliArguments())
    selected_parser = raw.selected_parser
    command_type = raw.command_selection
    if selected_parser is None or command_type is None:
        parser.error("the selected command has no decoder")
    untyped_values = vars(raw).copy()
    untyped_values.pop("command_selection")
    untyped_values.pop("selected_parser")
    if command_type in (cli_commands.MigrateSchemaPreviewCommand, cli_commands.MigrateWorkRootPreviewCommand):
        apply = untyped_values.pop("apply")
        reverse = untyped_values.pop("reverse")
        if command_type is cli_commands.MigrateSchemaPreviewCommand:
            command_type = (
                cli_commands.MigrateSchemaApplyCommand
                if apply is not None
                else cli_commands.MigrateSchemaReverseCommand
                if reverse is not None
                else cli_commands.MigrateSchemaPreviewCommand
            )
        else:
            command_type = (
                cli_commands.MigrateWorkRootApplyCommand
                if apply is not None
                else cli_commands.MigrateWorkRootReverseCommand
                if reverse is not None
                else cli_commands.MigrateWorkRootPreviewCommand
            )
        if apply is not None:
            untyped_values["apply"] = apply
        if reverse is not None:
            untyped_values["reverse"] = reverse
    try:
        roots = msgspec.convert(
            {
                "project_root": untyped_values.pop("project_root", None),
                "work_root": untyped_values.pop("work_root", None),
            },
            type=cli_commands.RootSelection,
            strict=True,
        )
        command = msgspec.convert(untyped_values, type=command_type, strict=True)
    except msgspec.ValidationError as error:
        selected_parser.error(str(error))
    return cli_commands.CliInvocation(roots, command)


def installed_commands() -> tuple[InstalledCommand, ...]:
    discovered: list[InstalledCommand] = []
    root_parser = argparse.ArgumentParser(prog="pinboard", add_help=False)
    _add_root_selection(root_parser)
    root_usage = " ".join(root_parser.format_usage().removeprefix("usage: ").split())

    def visit(parser: argparse.ArgumentParser) -> None:
        command_type = parser.get_default("command_selection")
        if command_type is not None:
            operation_id = parser.prog.removeprefix("pinboard ").replace(" ", "/")
            leaf_usage = " ".join(parser.format_usage().removeprefix("usage: ").split())
            usage = root_usage + leaf_usage.removeprefix("pinboard")
            if command_type is cli_commands.MigrateSchemaPreviewCommand:
                preview_usage = usage.removesuffix(" [--apply PLAN_ID | --reverse FORWARD_PLAN_ID]")
                variants = (
                    (operation_id, cli_commands.MigrateSchemaPreviewCommand, preview_usage),
                    (
                        f"{operation_id}/apply",
                        cli_commands.MigrateSchemaApplyCommand,
                        preview_usage + " --apply PLAN_ID",
                    ),
                    (
                        f"{operation_id}/reverse",
                        cli_commands.MigrateSchemaReverseCommand,
                        preview_usage + " --reverse FORWARD_PLAN_ID",
                    ),
                )
            elif command_type is cli_commands.MigrateWorkRootPreviewCommand:
                preview_usage = usage.removesuffix(" [--apply PLAN_ID | --reverse FORWARD_PLAN_ID]")
                variants = (
                    (operation_id, cli_commands.MigrateWorkRootPreviewCommand, preview_usage),
                    (
                        f"{operation_id}/apply",
                        cli_commands.MigrateWorkRootApplyCommand,
                        preview_usage + " --apply PLAN_ID",
                    ),
                    (
                        f"{operation_id}/reverse",
                        cli_commands.MigrateWorkRootReverseCommand,
                        preview_usage + " --reverse FORWARD_PLAN_ID",
                    ),
                )
            else:
                variants = ((operation_id, command_type, usage),)
            discovered.extend(
                InstalledCommand(identifier, variant, variant_usage) for identifier, variant, variant_usage in variants
            )
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    if isinstance(child, argparse.ArgumentParser):
                        visit(child)

    visit(build_parser())
    return tuple(discovered)
