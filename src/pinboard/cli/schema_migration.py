"""Present the bound, quiescent SQLite schema migration procedure."""

from datetime import UTC, datetime
from typing import Literal, assert_never

from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.sqlite.database import inspect_current_schema_migration
from pinboard.adapters.sqlite.schema_procedure import (
    AppliedSchema,
    PlannedSchema,
    SchemaProcedureFailure,
    apply_schema,
    preview_schema,
)
from pinboard.application import ports
from pinboard.application.ports import WorkStoreError
from pinboard.cli import cli_commands, work_views
from pinboard.cli.cli_output import write_json
from pinboard.cli.errors import CommandFailure, CommandResult
from pinboard.cli.work_state_models import SchemaMigrationView
from pinboard.domain.errors import (
    ChangedSurface,
    DecisionFailureCode,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    RetryDisposition,
)


def _failure(failure: SchemaProcedureFailure, database_path: str, plan_id: str, recovery: str) -> CommandFailure:
    changed = tuple(ChangedSurface(value) for value in failure.changed_surfaces)
    return CommandFailure(
        DecisionFailureCode.SCHEMA_MIGRATION_FAILED if changed else DecisionFailureCode.SCHEMA_MIGRATION_INVALID,
        failure.message,
        FailureDetails(
            observed=(
                FailureFact("database_path", database_path),
                FailureFact("plan_id", plan_id),
                FailureFact(
                    "next_step",
                    recovery,
                ),
            ),
            mismatches=(),
            retry=RetryDisposition.RETRY_SAME_INPUT
            if failure.retry == "retry-same-input"
            else RetryDisposition.CORRECT_INPUT,
            effect=EffectDisposition.COMMITTED if changed else EffectDisposition.UNCHANGED,
            changed_surfaces=changed,
            alternatives=(),
        ),
    )


def migrate_schema(
    roots: DurableRoots,
    store: ports.GeneratedViewSetReader,
    command: cli_commands.MigrateSchemaPreviewCommand
    | cli_commands.MigrateSchemaApplyCommand
    | cli_commands.MigrateSchemaReverseCommand,
) -> CommandResult[int]:
    path = roots.database_path
    match command:
        case cli_commands.MigrateSchemaPreviewCommand():
            selected = preview_schema(path)
            requested_id = ""
            recovery = "Inspect the named database and resolve the reported cause, then rerun pinboard migrate-schema with the same project_root and work_root. No plan ID is available until preview succeeds."
        case cli_commands.MigrateSchemaReverseCommand(reverse=forward_id):
            selected = preview_schema(path, forward_id)
            requested_id = forward_id
            recovery = f"Inspect the named database and retained migration evidence, resolve the reported refusal, then request pinboard migrate-schema --reverse {forward_id} with the same roots. Preserve later writes; a refused reverse preview supplies no apply plan."
        case cli_commands.MigrateSchemaApplyCommand(apply=plan_id):
            selected = apply_schema(path, plan_id)
            requested_id = plan_id
            recovery = f"Inspect the named database and migration evidence first. Resume pinboard migrate-schema --apply {plan_id} with the same roots only after resolving the reported cause and confirming the recorded plan still matches; preserve later writes."
        case _ as unreachable:
            assert_never(unreachable)
    if isinstance(selected, SchemaProcedureFailure):
        return _failure(selected, str(path), requested_id, recovery)
    if isinstance(selected, PlannedSchema):
        version, _, _ = inspect_current_schema_migration(path)
        write_json(
            SchemaMigrationView(
                "pinboard-schema-migration/v2",
                "planned",
                selected.plan_id,
                selected.plan,
                str(path),
                _authority(version),
                False,
                "unchanged",
                "safe-to-repeat",
                (),
                None,
            )
        )
    else:
        assert isinstance(selected, AppliedSchema)
        changed = bool(selected.changed_surfaces)
        version = selected.resulting_version
        warning: str | None = None
        if "ledger" in selected.changed_surfaces and version == 8:
            try:
                refreshed = work_views.rebuild(roots, store, datetime.now(UTC))
                if refreshed.warning is not None:
                    warning = f"{refreshed.warning.message} {refreshed.warning.repair} Do not replay the committed schema change."
            except WorkStoreError as error:
                warning = f"Schema migration committed at '{path}', but generated views could not be read: {error}. Repair the named state condition, then run pinboard views rebuild; do not replay the migration."
        write_json(
            SchemaMigrationView(
                "pinboard-schema-migration/v2",
                "migrated" if changed and selected.direction == "forward" else "reversed" if changed else "unchanged",
                selected.plan_id,
                None,
                str(path),
                _authority(version),
                changed,
                "committed" if changed else "unchanged",
                "do-not-retry" if changed else "safe-to-repeat",
                selected.changed_surfaces,
                warning,
            )
        )
    return 0


def _authority(version: int) -> Literal["sqlite-v6", "sqlite-v7", "sqlite-v8"]:
    match version:
        case 6:
            return "sqlite-v6"
        case 7:
            return "sqlite-v7"
        case 8:
            return "sqlite-v8"
        case _:
            raise ValueError(f"Unsupported schema authority version: {version}")
