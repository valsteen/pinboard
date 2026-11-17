"""Present the bound, quiescent SQLite schema migration procedure."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, assert_never

import msgspec

from pinboard.adapters.files.errors import ArtifactError, FileIOError
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.sqlite.database import SCHEMA_VERSION
from pinboard.adapters.sqlite.migration.schema_ownership_v8 import inspect_current_schema_migration
from pinboard.adapters.sqlite.migration.schema_procedure import (
    AppliedSchema,
    PlannedSchema,
    SchemaPlan,
    SchemaProcedureFailure,
    apply_schema,
    preview_schema,
)
from pinboard.application import ports
from pinboard.application.ports import WorkStoreError
from pinboard.cli import cli_commands, work_views
from pinboard.cli.cli_output import write_json
from pinboard.cli.errors import CommandFailure, CommandResult
from pinboard.domain.errors import (
    ChangedSurface,
    DecisionFailureCode,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    RetryDisposition,
)


class SchemaMigrationView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-schema-migration/v2"]
    status: Literal["planned", "migrated", "reversed", "unchanged"]
    plan_id: str
    plan: SchemaPlan | None
    database_path: str
    authority: Literal[
        "sqlite-v6",
        "sqlite-v7",
        "sqlite-v8",
        "sqlite-v9",
        "sqlite-v10",
        "sqlite-v11",
        "sqlite-v12",
        "sqlite-v13",
        "sqlite-v14",
    ]
    state_changed: bool
    effect: Literal["committed", "unchanged"]
    retry: Literal["do-not-retry", "safe-to-repeat"]
    changed_surfaces: tuple[str, ...]
    warning: str | None


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
    | cli_commands.MigrateSchemaReverseCommand
    | cli_commands.MigrateSchemaLabelledPreviewCommand
    | cli_commands.MigrateSchemaLabelledApplyCommand,
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
        case cli_commands.MigrateSchemaLabelledPreviewCommand(labels=labels):
            selected = preview_schema(path, None, Path(labels))
            requested_id = ""
            recovery = f"Correct or complete the hold label file {labels} for the exact current ledger as INSTALL.md describes, then rerun pinboard migrate-schema --labels {labels} with the same roots. No plan ID is available until preview succeeds."
        case cli_commands.MigrateSchemaLabelledApplyCommand(apply=plan_id, labels=labels):
            selected = apply_schema(path, plan_id, Path(labels))
            requested_id = plan_id
            recovery = f"Inspect the named database and migration evidence first. Resume pinboard migrate-schema --apply {plan_id} --labels {labels} with the same roots and the exact label file previewed with that plan; preserve later writes."
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
        warning = (
            _refresh_committed_views(roots, store)
            if "ledger" in selected.changed_surfaces and version == SCHEMA_VERSION
            else None
        )
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


def _refresh_committed_views(roots: DurableRoots, store: ports.GeneratedViewSetReader) -> str | None:
    """Refresh generated views after a committed schema change and return a warning when that fails.

    The schema change has already committed, so an expected state, artifact or file failure here leaves the ledger
    untouched: the migration is reported as committed with a warning to rebuild views and never to replay it.
    """

    path = roots.database_path
    try:
        refreshed = work_views.rebuild(roots, store, datetime.now(UTC))
    except WorkStoreError as error:
        return f"Schema migration committed at '{path}', but generated views could not be read: {error}. Repair the named state condition, then run pinboard views rebuild; do not replay the migration."
    except (ArtifactError, FileIOError) as error:
        return f"Schema migration committed at '{path}', but generated views could not be refreshed: {error}. Repair the named artifact or file condition, then run pinboard views rebuild; do not replay the migration."
    if refreshed.warning is not None:
        return f"{refreshed.warning.message} {refreshed.warning.repair} Do not replay the committed schema change."
    return None


def _authority(
    version: int,
) -> Literal[
    "sqlite-v6",
    "sqlite-v7",
    "sqlite-v8",
    "sqlite-v9",
    "sqlite-v10",
    "sqlite-v11",
    "sqlite-v12",
    "sqlite-v13",
    "sqlite-v14",
]:
    match version:
        case 6:
            return "sqlite-v6"
        case 7:
            return "sqlite-v7"
        case 8:
            return "sqlite-v8"
        case 9:
            return "sqlite-v9"
        case 10:
            return "sqlite-v10"
        case 11:
            return "sqlite-v11"
        case 12:
            return "sqlite-v12"
        case 13:
            return "sqlite-v13"
        case 14:
            return "sqlite-v14"
        case _:
            raise ValueError(f"Unsupported schema authority version: {version}")
