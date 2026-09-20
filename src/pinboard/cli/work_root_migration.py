"""Select, verify, and present the explicit legacy work-root migration."""

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.files.legacy_storage import StorageLocation, migrate_legacy_storage, observe_storage_location
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
    *,
    effect: EffectDisposition = EffectDisposition.UNCHANGED,
    changed_surfaces: tuple[ChangedSurface, ...] = (),
) -> CommandFailure:
    return CommandFailure(
        code,
        message,
        FailureDetails(
            observed=(FailureFact("recovery_command", "pinboard migrate-work-root"),),
            mismatches=(),
            retry=RetryDisposition.DO_NOT_RETRY
            if effect == EffectDisposition.COMMITTED
            else RetryDisposition.CORRECT_INPUT,
            effect=effect,
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
            "Legacy Pinboard state is unchanged; run 'pinboard migrate-work-root', then retry this command.",
        )
    if location == StorageLocation.CONFLICT:
        return _failure(
            DecisionFailureCode.WORK_ROOT_MIGRATION_INVALID,
            "The legacy and canonical work-root entries conflict; inspect both paths before retrying.",
        )
    return None


def _changed_surfaces(git_exclude_changed: bool, root_moved: bool, alias_created: bool) -> tuple[ChangedSurface, ...]:
    return (
        *((ChangedSurface.REPOSITORY_GIT_EXCLUDE,) if git_exclude_changed else ()),
        *((ChangedSurface.WORK_ROOT,) if root_moved else ()),
        *((ChangedSurface.COMPATIBILITY_ALIAS,) if alias_created else ()),
    )


def migrate_work_root(roots: cli_commands.ResolvedRoots) -> CommandResult[int]:
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
        )
    source = legacy if location == StorageLocation.LEGACY else roots.work
    connection = open_database(resolve_durable_roots(roots.shared_repository, source).database_path, OpenMode.READ_ONLY)
    connection.close()
    effects = migrate_legacy_storage(roots.shared_repository, location)
    changed_surfaces = _changed_surfaces(effects.git_exclude_changed, effects.root_moved, effects.alias_created)
    if effects.failure is not None:
        return _failure(
            DecisionFailureCode.WORK_ROOT_MIGRATION_FAILED,
            effects.failure,
            effect=EffectDisposition.COMMITTED if changed_surfaces else EffectDisposition.UNCHANGED,
            changed_surfaces=changed_surfaces,
        )
    changed = bool(changed_surfaces)
    write_json(
        WorkRootMigrationView(
            "pinboard-work-root-migration/v1",
            "migrated" if changed else "unchanged",
            str(roots.work),
            str(legacy),
            changed,
            "committed" if changed else "unchanged",
            "do-not-retry" if changed else "safe-to-repeat",
            tuple(surface.value for surface in changed_surfaces),
        )
    )
    return 0
