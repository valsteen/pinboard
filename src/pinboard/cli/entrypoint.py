"""Installed command router and process exit boundary.

This module owns the single exhaustive command-family branch and final rendering
of typed failures. Command grammar and use-case composition live with their
thematic CLI owners; this root performs no storage or domain work itself.
"""

import contextlib
import errno
import io
import os
import sys
from collections.abc import Sequence
from typing import TextIO, assert_never, override

from pinboard.adapters.files.errors import (
    ArtifactError,
    FileIOError,
    ImmutableFilePublishedError,
    RootError,
)
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.cli import (
    cli_commands,
    cli_output,
    cli_parser,
    code_catalog,
    invocation_capture,
    project_export,
    tool_contract,
    transitions,
    work_inspection,
    work_state_commands,
)
from pinboard.cli.errors import (
    CliEffectFailure,
    CliErrorCode,
    CliResult,
    CommandFailure,
    ExportMediaFailure,
    InitializationAfterCommittedEffects,
    ViewRefreshFailure,
    WorkBriefFailure,
    initialization_failure_details,
    storage_failure_details,
)
from pinboard.domain.errors import (
    ChangedSurface,
    DecisionFailureCode,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    RetryDisposition,
)

build_parser = cli_parser.build_parser


def _dispatch(  # noqa: C901, PLR0912 - keep the installed command routes visible
    invocation: cli_commands.CliInvocation,
    roots: cli_commands.ResolvedRoots | None,
) -> CliResult[int]:
    if isinstance(invocation.command, cli_commands.ToolContractCommand):
        return tool_contract.show_tool_contract(invocation.command)
    if isinstance(invocation.command, cli_commands.CodeCatalogCommand):
        return code_catalog.show_code_catalog(invocation.command)
    if roots is None:
        raise AssertionError("A rooted command requires resolved project roots.")
    if isinstance(invocation.command, cli_commands.RootCommand):
        return work_state_commands.show_roots(roots, invocation.command)
    durable = work_state_commands.resolve_durable_layout(roots)
    store = work_state_commands.compose_store(durable)
    if isinstance(
        invocation.command,
        (
            cli_commands.MigrateSchemaPreviewCommand,
            cli_commands.MigrateSchemaApplyCommand,
            cli_commands.MigrateSchemaReverseCommand,
        ),
    ):
        from pinboard.cli.migration import schema_migration  # noqa: PLC0415 - load migration only on its command route

        return schema_migration.migrate_schema(durable, store, invocation.command)
    match invocation.command:
        case cli_commands.ValidateCommand() as command:
            return work_state_commands.validate_state(roots, durable, store, command)
        case cli_commands.StatusCommand() as command:
            return work_inspection.show_status(roots, store, command)
        case cli_commands.DiagnoseCommand() as command:
            return work_inspection.show_diagnosis(roots, durable, store, command)
        case cli_commands.CloseCommand() as command:
            return transitions.close(durable, store, command)
        case cli_commands.ExportCommand() as command:
            return project_export.export_project(durable, store, command)
        case cli_commands.InitializeCommand() as command:
            return work_state_commands.initialize_state(roots, durable, store, command)
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


def _failure_exit_code(result: int | CommandFailure | WorkBriefFailure | InitializationAfterCommittedEffects) -> int:
    match result:
        case int():
            return result
        case CommandFailure():
            return 11
        case WorkBriefFailure():
            return 16
        case InitializationAfterCommittedEffects():
            return 12
        case _ as unreachable:
            assert_never(unreachable)


def _present_expected_result(
    result: int | CommandFailure | WorkBriefFailure | InitializationAfterCommittedEffects,
    operation: str,
    roots: cli_commands.ResolvedRoots | None,
    *,
    json_requested: bool,
) -> int:
    exit_code = _failure_exit_code(result)
    if isinstance(result, int):
        return exit_code
    if isinstance(result, InitializationAfterCommittedEffects):
        details = initialization_failure_details(result, operation, roots)
        cause = result.cause
        if json_requested:
            cli_output.write_operation_rejection(operation, cause.code.value, str(cause), details, ())
        else:
            print(
                f"{cause}; initialization committed: {', '.join(value.value for value in details.changed_surfaces)}",
                file=sys.stderr,
            )
        return exit_code
    if isinstance(result, WorkBriefFailure):
        if roots is None:
            raise AssertionError("Accepted-evidence failure requires the selected work root.")
        result = WorkBriefFailure(
            result.code,
            f"{result.message} Report this accepted-evidence defect under '{roots.work}' to the human for diagnosis; preserve authoritative state and do not reconstruct accepted bytes.",
        )
    if json_requested:
        if isinstance(result, CommandFailure) and result.code == DecisionFailureCode.ITEM_STATUS_INCONSISTENT:
            cli_output.write_operation_rejection(
                operation, result.code.value, result.message, result.details, ("pinboard validate",)
            )
        else:
            cli_output.write_rejected_operation(operation, result)
    else:
        print(str(result), file=sys.stderr)
    return exit_code


def _present_unchanged_effect_failure(operation: str, code: str, message: str, json_requested: bool) -> None:
    if json_requested:
        cli_output.write_operation_rejection(
            operation,
            code,
            message,
            FailureDetails(
                observed=(),
                mismatches=(),
                retry=RetryDisposition.DO_NOT_RETRY,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
            (),
        )
    else:
        print(message, file=sys.stderr)


def _present_direct_effect_failure(
    operation: str, result: ExportMediaFailure | ViewRefreshFailure, json_requested: bool
) -> None:
    try:
        _present_unchanged_effect_failure(operation, result.code.value, str(result), json_requested)
    except OSError:
        if json_requested:
            with contextlib.suppress(OSError):
                print(result, file=sys.stderr)
        raise


class _ResultOutput(io.TextIOBase):
    """Forward the command's result stream and remember the operating-system error that writing it raised.

    The CLI boundary uses that identity to keep `CLI_IO_ERROR` for undeliverable output and to classify every other
    unclassified `OSError` as an operation failure.
    """

    def __init__(self, target: TextIO) -> None:
        super().__init__()
        self._target = target
        self.failure: OSError | None = None

    @override
    def writable(self) -> bool:
        return True

    @override
    def write(self, text: str, /) -> int:
        try:
            return self._target.write(text)
        except OSError as error:
            self.failure = error
            raise

    @override
    def flush(self) -> None:
        try:
            self._target.flush()
        except OSError as error:
            self.failure = error
            raise

    @override
    def close(self) -> None:
        """Leave the forwarded stream to its owner; finalizing this wrapper must not flush it again."""


def _selected_roots_text(roots: cli_commands.ResolvedRoots | None) -> str:
    return (
        "the selected command roots"
        if roots is None
        else f"project_root='{roots.source_checkout}' and work_root='{roots.work}'"
    )


def _present_filesystem_failure(
    error: OSError,
    command: cli_commands.CliCommand,
    operation: str,
    roots: cli_commands.ResolvedRoots | None,
    *,
    json_requested: bool,
) -> int:
    """Report an operation's unclassified `OSError` with its path, error, truthful effect and one next step."""
    paths = tuple(
        os.fsdecode(value) if isinstance(value, (str, bytes)) else str(value)
        for value in (error.filename, error.filename2)
        if value is not None
    )
    resource = " and ".join(f"'{value}'" for value in paths) if paths else "a path the operating system did not name"
    errno_name = None if error.errno is None else errno.errorcode.get(error.errno, str(error.errno))
    os_error = str(error) if error.strerror is None else error.strerror
    reason = os_error if errno_name is None else f"{errno_name} ({os_error})"
    code = CliErrorCode.FILESYSTEM_ERROR.value
    root_facts = (
        ()
        if roots is None
        else (FailureFact("project_root", str(roots.source_checkout)), FailureFact("work_root", str(roots.work)))
    )
    match tool_contract.operation_mutation_class(command):
        case "read-only":
            message = f"The '{operation}' operation failed with {reason} on {resource}; it changed nothing."
            next_step = (
                f"Resolve that condition, for example by restoring access to {resource}, then repeat the same "
                "request; it is read-only and safe to repeat. Report the condition to the human if you cannot resolve it."
            )
            effect = "unchanged"
        case "mutates-ledger" | "repairs-derived-views":
            message = (
                f"The '{operation}' operation failed with {reason} on {resource}; its effects are unknown and it may "
                "have changed state."
            )
            next_step = (
                f"Do not repeat the request yet: inspect current state with 'pinboard diagnose' using "
                f"{_selected_roots_text(roots)}, resolve the named condition, then decide whether to repeat it. "
                "Report the condition to the human if inspection is unavailable."
            )
            effect = "unknown"
        case _ as unreachable:
            assert_never(unreachable)
    observed = (
        FailureFact("operation", operation),
        FailureFact("path", paths[0] if paths else None),
        *((FailureFact("second_path", paths[1]),) if len(paths) > 1 else ()),
        FailureFact("errno", errno_name),
        FailureFact("os_error", os_error),
        FailureFact("effect", effect),
        *root_facts,
        FailureFact("next_step", next_step),
    )
    if not json_requested:
        print(f"{code}: {message} {next_step}", file=sys.stderr)
    elif effect == "unchanged":
        cli_output.write_operation_rejection(
            operation,
            code,
            message,
            FailureDetails(
                observed=observed,
                mismatches=(),
                retry=RetryDisposition.RETRY_SAME_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
            (),
        )
    else:
        cli_output.write_effect_unknown(operation, code, message, observed, ("pinboard diagnose --json",))
    return 12


def _run_invocation(
    invocation: cli_commands.CliInvocation,
    operation: str,
    *,
    json_requested: bool,
) -> int:
    output = _ResultOutput(sys.stdout)
    with contextlib.redirect_stdout(output):
        return _classified_invocation(invocation, operation, output, json_requested=json_requested)


def _classified_invocation(  # noqa: PLR0912 - preserve persisted-state invariant tracebacks at the CLI boundary
    invocation: cli_commands.CliInvocation,
    operation: str,
    output: _ResultOutput,
    *,
    json_requested: bool,
) -> int:
    roots: cli_commands.ResolvedRoots | None = None
    try:
        if not isinstance(invocation.command, (cli_commands.ToolContractCommand, cli_commands.CodeCatalogCommand)):
            roots = work_state_commands.resolve_roots(invocation.roots)
        result = _dispatch(invocation, roots)
        if not isinstance(result, CliEffectFailure):
            status = _present_expected_result(result, operation, roots, json_requested=json_requested)
            sys.stdout.flush()
            return status
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
    except RootError as error:
        if json_requested:
            code = error.code.value
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
    except OSError as error:
        if error is not output.failure:
            return _present_filesystem_failure(
                error, invocation.command, operation, roots, json_requested=json_requested
            )
        print(
            f"{CliErrorCode.IO_ERROR.value}: {error}. The '{operation}' result could not be delivered; earlier command effects are unknown. Restore the output connection, then inspect current state using {_selected_roots_text(roots)} before repeating any mutating request. Report this condition to the human if inspection is unavailable.",
            file=sys.stderr,
        )
        return 2
    except (StorageError, ArtifactError, FileIOError) as error:
        if isinstance(error, StorageError) and error.invariant_violation:
            raise
        if isinstance(error, StorageError) and json_requested:
            details = storage_failure_details(error, operation, roots, EffectDisposition.UNCHANGED, (), ())
            cli_output.write_operation_rejection(operation, error.code.value, str(error), details, ())
        else:
            _present_unchanged_effect_failure(operation, error.code.value, str(error), json_requested)
        return 12
    # Direct effect failures retain the output boundary of adapter rejections.
    _present_direct_effect_failure(operation, result, json_requested)
    return 12


def _run_cli(arguments: tuple[str, ...]) -> int:
    json_requested = "--json" in arguments
    invocation = _parse_arguments(arguments, json_requested=json_requested)
    if isinstance(invocation, int):
        return invocation
    operation = tool_contract.operation_identity(invocation.command)
    return _run_invocation(invocation, operation, json_requested=json_requested)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one CLI call, inside its capture boundary when the launcher selected capture.

    Captured and uncaptured calls reach the target through the same `_run_cli` line, so an uncaught
    exception presents the same traceback either way. A HUP, INT or TERM that arrives after the target
    ended but before the session stopped interrupting raises `CaptureInterrupted` in this handoff; the
    outer handler then finishes the session with it, so the call is recorded as interrupted and its
    output, record or publication failure and stage cleanup still happen.
    """
    match invocation_capture.prepare(tuple(sys.argv[1:] if argv is None else argv)):
        case invocation_capture.RejectedBeforeTarget(exit_status=rejected):
            return rejected
        case invocation_capture.Uncaptured(target_arguments=arguments):
            session = None
        case invocation_capture.CaptureSession() as session:
            arguments = session.target_arguments
        case _ as unreachable:
            assert_never(unreachable)
    try:
        try:
            if session is not None:
                session.arm()
            status = _run_cli(arguments)
            if session is not None:
                session.disarm()
        except BaseException as error:
            if session is None:
                raise
            return session.finish(error)
        return status if session is None else session.finish(status)
    except invocation_capture.CaptureInterrupted as late:
        if session is None:
            raise
        return session.finish(late)
