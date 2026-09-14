"""Compatibility location upgrade; remove when legacy project paths are retired.

The caller must ensure no other Pinboard invocation accesses this project during
the move. This filesystem operation does not convert a SQLite schema.
"""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pinboard.adapters.files.errors import RootError
from pinboard.adapters.files.root import ensure_git_exclude


class StorageLocation(Enum):
    FRESH = "fresh"
    LEGACY = "legacy"
    NEUTRAL = "neutral"
    ALIASED = "aliased"
    CONFLICT = "conflict"


def observe_storage_location(repository: Path) -> StorageLocation:
    legacy = repository / ".codex" / "pinboard"
    neutral = repository / ".pinboard"
    if legacy.parent.is_symlink() and legacy.exists(follow_symlinks=False):
        return StorageLocation.CONFLICT
    if neutral.exists(follow_symlinks=False) and (neutral.is_symlink() or not neutral.is_dir()):
        return StorageLocation.CONFLICT
    if legacy.is_symlink():
        if neutral.is_dir() and legacy.readlink() == Path("../.pinboard") and legacy.resolve() == neutral:
            return StorageLocation.ALIASED
        return StorageLocation.CONFLICT
    if legacy.exists():
        if not legacy.is_dir() or legacy.parent.is_symlink() or neutral.exists():
            return StorageLocation.CONFLICT
        return StorageLocation.LEGACY
    return StorageLocation.NEUTRAL if neutral.exists() else StorageLocation.FRESH


@dataclass(frozen=True, slots=True)
class MigrationEffects:
    changed_paths: tuple[Path, ...]
    failure: str | None


def migrate_legacy_storage(repository: Path, location: StorageLocation) -> MigrationEffects:
    """Perform the observed nonconflicting upgrade and retain exact partial effects."""

    legacy = repository / ".codex" / "pinboard"
    neutral = repository / ".pinboard"
    changed: list[Path] = []
    exclude = repository / ".git" / "info" / "exclude"
    original_exclude = exclude.read_bytes() if exclude.is_file() else None
    try:
        match location:
            case StorageLocation.LEGACY:
                legacy.rename(neutral)
                changed.extend((legacy, neutral))
            case StorageLocation.NEUTRAL | StorageLocation.ALIASED:
                pass
            case StorageLocation.FRESH | StorageLocation.CONFLICT:
                return MigrationEffects((), "Migration requires one existing nonconflicting current project ledger.")
        if location != StorageLocation.ALIASED:
            if not legacy.parent.exists():
                legacy.parent.mkdir()
                changed.append(legacy.parent)
            legacy.symlink_to("../.pinboard", target_is_directory=True)
            if legacy not in changed:
                changed.append(legacy)
        exclusion = ensure_git_exclude(repository, b"/.pinboard/")
        if exclusion is not None:
            changed.append(exclusion)
        exclusion = ensure_git_exclude(repository, b"/.codex/pinboard")
        if exclusion is not None and exclusion not in changed:
            changed.append(exclusion)
    except (OSError, RootError) as error:
        if exclude.is_file() and exclude.read_bytes() != original_exclude and exclude not in changed:
            changed.append(exclude)
        return MigrationEffects(tuple(changed), str(error))
    return MigrationEffects(tuple(changed), None)
