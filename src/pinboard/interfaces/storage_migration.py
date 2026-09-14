"""Present the explicit location upgrade and unchanged legacy-root recovery."""

from typing import Literal

import msgspec

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.files.legacy_storage import (
    StorageLocation,
    migrate_legacy_storage,
    observe_storage_location,
)
from pinboard.adapters.sqlite.database import open_database
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.interfaces import cli_commands
from pinboard.interfaces.cli_output import write_json


class MigrationAction(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    launcher: Literal["self"]
    arguments: tuple[str, ...]
    requires: tuple[str, ...]


class StorageRecovery(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-storage-recovery/v1"]
    status: Literal[
        "legacy-storage-detected",
        "storage-roots-conflict",
        "migration-unavailable",
        "migration-incomplete",
        "storage-migrated",
    ]
    work_root: str
    message: str
    effect_disposition: Literal["unchanged", "changed"]
    changed_paths: tuple[str, ...]
    retry_disposition: Literal["retry-original-command", "run-migration", "correct-roots"]
    next_action: MigrationAction | None


def _migration_action(roots: cli_commands.ResolvedRoots) -> MigrationAction:
    return MigrationAction(
        "self",
        ("--project-root", str(roots.source_checkout), "migrate-storage", "--json"),
        (
            "No other Pinboard command accesses this project during the move.",
            "Write access to .pinboard, .codex/pinboard, and the repository-local Git exclusion.",
        ),
    )


def require_current_storage(roots: cli_commands.ResolvedRoots) -> int | None:
    if roots.explicit_work_root:
        return None
    location = observe_storage_location(roots.shared_repository)
    if location not in (StorageLocation.LEGACY, StorageLocation.CONFLICT):
        return None
    legacy = location == StorageLocation.LEGACY
    write_json(
        StorageRecovery(
            "pinboard-storage-recovery/v1",
            "legacy-storage-detected" if legacy else "storage-roots-conflict",
            str(roots.work),
            "Run the exact migration action, then retry the original command. The existing ledger is unchanged."
            if legacy
            else f"Resolve conflicting or unexpected entries at {roots.shared_repository / '.codex' / 'pinboard'} and {roots.work} without overwriting data.",
            "unchanged",
            (),
            "run-migration" if legacy else "correct-roots",
            _migration_action(roots) if legacy else None,
        )
    )
    return 2


def migrate_storage(roots: cli_commands.ResolvedRoots) -> int:
    location = observe_storage_location(roots.shared_repository)
    if (
        roots.explicit_work_root
        or location in (StorageLocation.FRESH, StorageLocation.CONFLICT)
        or (roots.shared_repository / ".codex").is_symlink()
    ):
        write_json(
            StorageRecovery(
                "pinboard-storage-recovery/v1",
                "migration-unavailable",
                str(roots.work),
                "Migration requires the default root and one existing nonconflicting current ledger.",
                "unchanged",
                (),
                "correct-roots",
                None,
            )
        )
        return 2
    source = roots.shared_repository / ".codex" / "pinboard" if location == StorageLocation.LEGACY else roots.work
    connection = open_database(resolve_durable_roots(roots.shared_repository, source).database_path, OpenMode.READ_ONLY)
    connection.close()
    effects = migrate_legacy_storage(roots.shared_repository, location)
    failed = effects.failure is not None
    write_json(
        StorageRecovery(
            "pinboard-storage-recovery/v1",
            "migration-incomplete" if failed else "storage-migrated",
            str(roots.work),
            effects.failure
            if effects.failure is not None
            else "Ledger and immutable bytes preserved; retry the original command.",
            "changed" if effects.changed_paths else "unchanged",
            tuple(str(path) for path in effects.changed_paths),
            "run-migration" if failed else "retry-original-command",
            _migration_action(roots) if failed else None,
        )
    )
    return 12 if failed else 0
