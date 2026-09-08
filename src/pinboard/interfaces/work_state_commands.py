"""Installed work-root resolution, initialization, validation, and repair commands."""

import os
import tomllib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from pinboard.adapters.files.errors import ArtifactError, FileIOError, FileIOErrorCode, RootError, RootErrorCode
from pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from pinboard.adapters.files.root import resolve_shared_repository_root, resolve_source_checkout_root
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import ports
from pinboard.domain.identifiers import AttemptId
from pinboard.interfaces import cli_commands, work_views
from pinboard.interfaces.cli_output import write_json
from pinboard.interfaces.errors import CliResult, WorkBriefFailure
from pinboard.interfaces.work_state import (
    initialize_work_state,
    read_state_for_validation,
    validate_loaded_work_state,
)
from pinboard.interfaces.work_state_models import (
    Diagnostic,
    DiagnosticView,
    InitializationView,
    RootView,
    Severity,
    ValidationReport,
    ValidationView,
)


def resolve_roots(selection: cli_commands.RootSelection) -> cli_commands.ResolvedRoots:
    project_argument = selection.project_root
    selected_checkout = Path.cwd() if project_argument is None else project_argument
    try:
        source_checkout = resolve_source_checkout_root(selected_checkout)
        shared_repository = resolve_shared_repository_root(source_checkout)
    except RootError as error:
        if project_argument is None or error.code != RootErrorCode.PROJECT_GIT_ROOT_UNAVAILABLE:
            raise
        source_checkout = project_argument.resolve()
        shared_repository = source_checkout
    work_argument = selection.work_root
    work = work_argument.resolve() if work_argument is not None else shared_repository / ".codex" / "pinboard"
    return cli_commands.ResolvedRoots(source_checkout, shared_repository, work, work_argument is not None)


def resolve_durable_layout(roots: cli_commands.ResolvedRoots) -> DurableRoots:
    """Validate and name the configured durable layout for a stateful command."""

    return resolve_durable_roots(roots.shared_repository, roots.work)


def compose_store(durable: DurableRoots) -> SQLiteWorkStore:
    """Construct the configured store once for one installed invocation."""

    return SQLiteWorkStore(durable.database_path)


def _project_validation(report: ValidationReport) -> ValidationView:
    return ValidationView(
        valid=report.valid,
        diagnostics=tuple(
            DiagnosticView(
                code=value.code,
                severity=value.severity.value,
                path=str(value.path),
                message=value.message,
                hint=value.hint,
            )
            for value in report.diagnostics
        ),
    )


def show_roots(roots: cli_commands.ResolvedRoots, _command: cli_commands.RootCommand) -> int:
    write_json(RootView(str(roots.source_checkout), str(roots.shared_repository), str(roots.work)))
    return 0


def validate_state(
    roots: cli_commands.ResolvedRoots,
    durable: DurableRoots,
    store: ports.ValidatedStateReader,
    command: cli_commands.ValidateCommand,
) -> int:
    operation_time = datetime.now(UTC)
    loaded_state = read_state_for_validation(durable.database_path, store)
    if isinstance(loaded_state, ValidationReport):
        validation_report = loaded_state
    else:
        current_state = loaded_state
        brief_diagnostic: Diagnostic | None = None
        attempt_briefs: Mapping[AttemptId, bytes]
        try:
            brief_result = work_views.read_attempt_brief_views(durable, current_state)
        except ArtifactError as error:
            attempt_briefs = {}
            brief_diagnostic = Diagnostic(error.code.value, Severity.ERROR, roots.work, str(error))
        else:
            if isinstance(brief_result, WorkBriefFailure):
                attempt_briefs = {}
                brief_diagnostic = Diagnostic(
                    brief_result.code.value,
                    Severity.ERROR,
                    roots.work,
                    brief_result.message,
                )
            else:
                attempt_briefs = brief_result
        validation_report = validate_loaded_work_state(roots.work, current_state, attempt_briefs, now=operation_time)
        if brief_diagnostic is not None:
            validation_report = ValidationReport(
                (
                    *validation_report.diagnostics,
                    brief_diagnostic,
                )
            )
    if command.json:
        write_json(_project_validation(validation_report))
    else:
        print(validation_report.render())
    return 0 if validation_report.valid else 10


def _read_user_config_and_recommend_body_after_prefix() -> str | None:
    codex_home = Path(os.environ["CODEX_HOME"]) if "CODEX_HOME" in os.environ else Path.home() / ".codex"
    user_config = codex_home / "config.toml"
    try:
        with user_config.open("rb") as stream:
            setting_present_in_user_config = "model_auto_compact_token_limit_scope" in tomllib.load(stream)
    except FileNotFoundError:
        setting_present_in_user_config = False
    except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError:
        return None
    if setting_present_in_user_config:
        return None
    return (
        'OPTIONAL: For long Codex tasks, set the user default model_auto_compact_token_limit_scope = "body_after_prefix" '
        f"in {user_config} to count only context added after a compaction. A trusted project's .codex/config.toml "
        "can override this user default. Reference: https://learn.chatgpt.com/docs/config-file/config-reference"
    )


def initialize_state(
    roots: cli_commands.ResolvedRoots,
    durable: DurableRoots,
    store: ports.GeneratedViewSetReader,
    command: cli_commands.InitializeCommand,
) -> CliResult[int]:
    operation_time = datetime.now(UTC)
    receipt = initialize_work_state(
        roots.shared_repository,
        durable,
        default_work_root=not roots.explicit_work_root,
        store=store,
        now=operation_time,
    )
    if isinstance(receipt, WorkBriefFailure):
        return receipt
    optional_next_skills = (
        () if receipt.resumed else ("repository-readiness", "slop-cleanup", "maintaining-agent-guidance")
    )
    recommendation = (
        None
        if receipt.resumed or os.environ.get("PINBOARD_RUNTIME") == "claude"
        else _read_user_config_and_recommend_body_after_prefix()
    )
    view = InitializationView(
        "pinboard-work-state-initialized/v1",
        str(receipt.work_root),
        receipt.resumed,
        optional_next_skills,
        recommendation,
    )
    if command.json:
        write_json(view)
        return 0
    print(f"OK WORK_STATE_INITIALIZED {view.work_root}")
    if view.optional_next_skills:
        print(
            "Optional next steps: $repository-readiness maps safe change paths; $slop-cleanup removes unsupported "
            "residue; $maintaining-agent-guidance places durable AI guidance."
        )
        if view.configuration_recommendation is not None:
            print(view.configuration_recommendation)
    return 0


def rebuild_views(
    durable: DurableRoots,
    store: ports.GeneratedViewSetReader,
    _command: cli_commands.RebuildViewsCommand,
) -> int:
    operation_time = datetime.now(UTC)
    rebuild_result = work_views.rebuild(durable, store, operation_time)
    if rebuild_result.warning is not None:
        raise FileIOError(FileIOErrorCode.VIEW_REFRESH_FAILED, rebuild_result.warning.message)
    print(f"OK VIEWS_REBUILT revision={rebuild_result.database_revision}")
    return 0
