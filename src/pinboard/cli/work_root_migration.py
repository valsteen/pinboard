"""Select, verify, and present the bound legacy work-root procedure."""

from typing import assert_never

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.files.legacy_storage import (
    AppliedRoot,
    PlannedRoot,
    RootProcedureFailure,
    StorageLocation,
    apply_root,
    observe_storage_location,
    preview_root,
)
from pinboard.adapters.sqlite.database import open_database
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.cli import cli_commands
from pinboard.cli.cli_output import write_json
from pinboard.cli.errors import CommandFailure, CommandResult
from pinboard.cli.work_state_models import WorkRootMigrationView
from pinboard.domain.errors import (
    ChangedSurface,
    DecisionFailureCode,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    RetryDisposition,
)


def _failure(
    code: DecisionFailureCode,
    message: str,
    roots: cli_commands.ResolvedRoots,
    plan_id: str,
    changed_surfaces: tuple[ChangedSurface, ...],
    retry: RetryDisposition,
) -> CommandFailure:
    return CommandFailure(
        code,
        message,
        FailureDetails(
            observed=(
                FailureFact("plan_id", plan_id),
                FailureFact("recovery_command", "pinboard migrate-work-root, then --apply <plan-id>"),
                FailureFact(
                    "permission_request",
                    f"Grant this CLI access to {roots.shared_repository / '.git' / 'info' / 'exclude'}, "
                    f"{roots.shared_repository / '.codex' / 'pinboard-migration'}, "
                    f"{roots.shared_repository / '.codex' / 'pinboard'}, and {roots.work}.",
                ),
            ),
            mismatches=(),
            retry=retry,
            effect=EffectDisposition.COMMITTED if changed_surfaces else EffectDisposition.UNCHANGED,
            changed_surfaces=changed_surfaces,
            alternatives=(),
        ),
    )


def require_current_work_root(roots: cli_commands.ResolvedRoots) -> CommandFailure | None:
    if roots.explicit_work_root:
        return None
    location = observe_storage_location(roots.shared_repository)
    if location == StorageLocation.LEGACY:
        return _failure(
            DecisionFailureCode.WORK_ROOT_MIGRATION_REQUIRED,
            "Legacy Pinboard state is unchanged; preview 'pinboard migrate-work-root', apply its plan, then retry.",
            roots,
            "",
            (),
            RetryDisposition.CORRECT_INPUT,
        )
    if location == StorageLocation.CONFLICT:
        return _failure(
            DecisionFailureCode.WORK_ROOT_MIGRATION_INVALID,
            "The legacy and canonical work-root entries conflict; inspect both paths before retrying.",
            roots,
            "",
            (),
            RetryDisposition.CORRECT_INPUT,
        )
    return None


def migrate_work_root(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.MigrateWorkRootPreviewCommand
    | cli_commands.MigrateWorkRootApplyCommand
    | cli_commands.MigrateWorkRootReverseCommand,
) -> CommandResult[int]:
    location = observe_storage_location(roots.shared_repository)
    legacy = roots.shared_repository / ".codex" / "pinboard"
    if (
        roots.explicit_work_root
        or location in (StorageLocation.FRESH, StorageLocation.CONFLICT)
        or legacy.parent.is_symlink()
    ):
        return _failure(
            DecisionFailureCode.WORK_ROOT_MIGRATION_INVALID,
            "Migration requires the default root and one verified current-schema ledger without conflicting paths.",
            roots,
            "",
            (),
            RetryDisposition.CORRECT_INPUT,
        )
    source = legacy if location == StorageLocation.LEGACY else roots.work
    connection = open_database(resolve_durable_roots(roots.shared_repository, source).database_path, OpenMode.READ_ONLY)
    connection.close()
    match command:
        case cli_commands.MigrateWorkRootPreviewCommand():
            selected = preview_root(roots.shared_repository)
            requested_id = ""
        case cli_commands.MigrateWorkRootReverseCommand(reverse=forward_id):
            selected = preview_root(roots.shared_repository, forward_id)
            requested_id = forward_id
        case cli_commands.MigrateWorkRootApplyCommand(apply=plan_id):
            selected = apply_root(roots.shared_repository, plan_id)
            requested_id = plan_id
        case _ as unreachable:
            assert_never(unreachable)
    if isinstance(selected, RootProcedureFailure):
        changed_surfaces = tuple(ChangedSurface(value) for value in selected.changed_surfaces)
        return _failure(
            DecisionFailureCode.WORK_ROOT_MIGRATION_FAILED
            if changed_surfaces
            else DecisionFailureCode.WORK_ROOT_MIGRATION_INVALID,
            selected.message,
            roots,
            requested_id,
            changed_surfaces,
            RetryDisposition.RETRY_SAME_INPUT
            if selected.retry == "retry-same-input"
            else RetryDisposition.CORRECT_INPUT,
        )
    changed = isinstance(selected, AppliedRoot) and bool(selected.changed_surfaces)
    write_json(
        WorkRootMigrationView(
            "pinboard-work-root-migration/v2",
            "planned"
            if isinstance(selected, PlannedRoot)
            else "migrated"
            if changed and selected.direction == "forward"
            else "reversed"
            if changed
            else "unchanged",
            selected.plan_id,
            selected.plan if isinstance(selected, PlannedRoot) else None,
            str(roots.work),
            str(legacy),
            changed,
            "committed" if changed else "unchanged",
            "do-not-retry" if changed else "safe-to-repeat",
            selected.changed_surfaces if isinstance(selected, AppliedRoot) else (),
        )
    )
    return 0
