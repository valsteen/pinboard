"""Release-scoped compatibility move from .codex/pinboard to .pinboard.

The caller guarantees that no other Pinboard process accesses the project during
the move. This operation relocates current-schema bytes; it never rewrites them.
"""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pinboard.adapters.files.errors import RootError
from pinboard.adapters.files.root import ensure_git_exclude


class StorageLocation(Enum):
    FRESH = "fresh"
    LEGACY = "legacy"
    CURRENT = "current"
    ALIASED = "aliased"
    CONFLICT = "conflict"


def observe_storage_location(repository: Path) -> StorageLocation:
    legacy = repository / ".codex" / "pinboard"
    current = repository / ".pinboard"
    if current.exists(follow_symlinks=False) and (current.is_symlink() or not current.is_dir()):
        return StorageLocation.CONFLICT
    if legacy.is_symlink():
        if (
            not legacy.parent.is_symlink()
            and current.is_dir()
            and legacy.readlink() == Path("../.pinboard")
            and legacy.resolve() == current
        ):
            return StorageLocation.ALIASED
        return StorageLocation.CONFLICT
    if legacy.exists(follow_symlinks=False):
        if not legacy.is_dir() or legacy.parent.is_symlink() or current.exists(follow_symlinks=False):
            return StorageLocation.CONFLICT
        return StorageLocation.LEGACY
    return StorageLocation.CURRENT if current.is_dir() else StorageLocation.FRESH


@dataclass(frozen=True, slots=True)
class MigrationEffects:
    git_exclude_changed: bool
    root_moved: bool
    alias_created: bool
    failure: str | None


def migrate_legacy_storage(repository: Path, location: StorageLocation) -> MigrationEffects:
    legacy = repository / ".codex" / "pinboard"
    current = repository / ".pinboard"
    git_exclude_changed = False
    root_moved = False
    alias_created = False
    try:
        git_exclude_changed = ensure_git_exclude(repository, b"/.pinboard/") is not None
        if location == StorageLocation.LEGACY:
            legacy.rename(current)
            root_moved = True
        elif location not in (StorageLocation.CURRENT, StorageLocation.ALIASED):
            return MigrationEffects(git_exclude_changed, False, False, "Migration requires one current ledger.")
        if location != StorageLocation.ALIASED:
            parent_created = not legacy.parent.exists()
            if parent_created:
                legacy.parent.mkdir()
            try:
                legacy.symlink_to("../.pinboard", target_is_directory=True)
            except OSError:
                if parent_created:
                    legacy.parent.rmdir()
                raise
            alias_created = True
        legacy_exclude_changed = ensure_git_exclude(repository, b"/.codex/pinboard") is not None
        git_exclude_changed = git_exclude_changed or legacy_exclude_changed
    except (OSError, RootError) as error:
        return MigrationEffects(git_exclude_changed, root_moved, alias_created, str(error))
    return MigrationEffects(git_exclude_changed, root_moved, alias_created, None)
