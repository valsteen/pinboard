"""Present the bound, quiescent SQLite schema migration procedure."""

from typing import assert_never

from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.sqlite.database import inspect_schema_migration
from pinboard.adapters.sqlite.schema_procedure import (
    AppliedSchema,
    PlannedSchema,
    SchemaProcedureFailure,
    apply_schema,
    preview_schema,
)
from pinboard.cli import cli_commands
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


def _failure(failure: SchemaProcedureFailure, database_path: str, plan_id: str) -> CommandFailure:
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
                    f"Inspect the named database and migration evidence, then resume --apply {plan_id} only if unchanged.",
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
    command: cli_commands.MigrateSchemaPreviewCommand
    | cli_commands.MigrateSchemaApplyCommand
    | cli_commands.MigrateSchemaReverseCommand,
) -> CommandResult[int]:
    path = roots.database_path
    match command:
        case cli_commands.MigrateSchemaPreviewCommand():
            selected = preview_schema(path)
            requested_id = ""
        case cli_commands.MigrateSchemaReverseCommand(reverse=forward_id):
            selected = preview_schema(path, forward_id)
            requested_id = forward_id
        case cli_commands.MigrateSchemaApplyCommand(apply=plan_id):
            selected = apply_schema(path, plan_id)
            requested_id = plan_id
        case _ as unreachable:
            assert_never(unreachable)
    if isinstance(selected, SchemaProcedureFailure):
        return _failure(selected, str(path), requested_id)
    if isinstance(selected, PlannedSchema):
        version, _, _ = inspect_schema_migration(path)
        write_json(
            SchemaMigrationView(
                "pinboard-schema-migration/v2",
                "planned",
                selected.plan_id,
                selected.plan,
                str(path),
                "sqlite-v6" if version == 6 else "sqlite-v7",
                False,
                "unchanged",
                "safe-to-repeat",
                (),
            )
        )
    else:
        assert isinstance(selected, AppliedSchema)
        changed = bool(selected.changed_surfaces)
        version = 6 if selected.direction == "reverse" else 7
        write_json(
            SchemaMigrationView(
                "pinboard-schema-migration/v2",
                "migrated" if changed and selected.direction == "forward" else "reversed" if changed else "unchanged",
                selected.plan_id,
                None,
                str(path),
                "sqlite-v6" if version == 6 else "sqlite-v7",
                changed,
                "committed" if changed else "unchanged",
                "do-not-retry" if changed else "safe-to-repeat",
                selected.changed_surfaces,
            )
        )
    return 0
