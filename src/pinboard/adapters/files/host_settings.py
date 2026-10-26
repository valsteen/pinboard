"""Per-user startup page sizes for recognized MCP hosts in `~/.config/pinboard/hosts.config`.

The file is Git config format with one `host.<host>.startup-page-bytes` key per recognized host. A missing
key is seeded with the caller's measured default through the shared key-level route and read back; the user
owns the value afterwards. Every installed Pinboard version shares the file, so any host name is accepted,
while every key name and value is validated.
"""

import re
import stat
from pathlib import Path

from pinboard.adapters.files import git_config
from pinboard.adapters.files.setting_resolution import (
    DanglingLink,
    EntryProblem,
    InvalidEntry,
    NotDirectory,
    NotRegularFile,
    SettingEffects,
    SettingResolution,
    SettingResolutionError,
    resolve_seeded_key,
)

MIN_PAGE_BYTES = 4000
MAX_PAGE_BYTES = 200000
_KEY = re.compile(r"host\.(.+)\.startup-page-bytes")
_PAGE_BYTES = re.compile(r"[0-9]{1,7}")


def settings_path() -> Path:
    """The per-user settings file; raises RuntimeError when the home directory cannot be determined."""
    return Path.home() / ".config" / "pinboard" / "hosts.config"


def setting_key(host: str) -> str:
    return f"host.{host}.startup-page-bytes"


def real_location(path: Path) -> Path:
    """Where the settings file really lives, so a linked file or directory is read and seeded through its link.

    Every later read, creation and seeding step uses this location, so a dotfile manager's link stays in place.
    A link whose target does not exist is reported rather than followed into creating that target.
    """
    for candidate in (path, *path.parents):
        if not candidate.is_symlink():
            continue
        target = candidate.resolve(strict=False)
        try:
            target.stat()
        except FileNotFoundError:
            raise SettingResolutionError(
                f"{candidate} is a symbolic link to {target}, which does not exist.",
                path,
                SettingEffects("none", "none", "none"),
                DanglingLink(candidate, target),
                "read",
                candidate,
            ) from None
        except OSError:
            # Later steps report access failures at the location they touch.
            continue
    return path.resolve(strict=False)


def _blocking_path(directory: Path) -> Path | None:
    """The nearest existing path at or above `directory` that is not a directory, which prevents its creation."""
    for candidate in (directory, *directory.parents):
        if candidate.exists(follow_symlinks=False) and not candidate.is_dir():
            return candidate
    return None


def ensure_directory(path: Path) -> bool:
    """Create the settings file's parent privately when missing; report whether this call created it."""
    directory = path.parent
    if directory.is_dir():
        return False
    unchanged = SettingEffects("none", "none", "none")
    try:
        directory.mkdir(mode=0o700, parents=True)
    except OSError as error:
        blocking = _blocking_path(directory) if isinstance(error, FileExistsError | NotADirectoryError) else None
        if blocking is not None:
            raise SettingResolutionError(
                f"Cannot create the host settings directory {directory}: {blocking} exists and is not a directory.",
                path,
                unchanged,
                NotDirectory(blocking),
                "create",
                blocking,
            ) from error
        if isinstance(error, FileExistsError) and directory.is_dir():
            return False
        raise SettingResolutionError(
            f"Cannot create the host settings directory {directory}: {error}",
            path,
            unchanged,
            error,
            "create",
            directory,
        ) from error
    return True


def _validated(path: Path, key: str, value: str | None, seen: set[str]) -> tuple[str, int] | InvalidEntry:
    """One entry's host and page size, or the rejection naming that entry's own key."""
    if key in seen:
        return InvalidEntry(key, EntryProblem.DUPLICATE_KEY, f"{path} sets key {key} more than once.")
    matched = _KEY.fullmatch(key)
    if matched is None:
        return InvalidEntry(
            key,
            EntryProblem.UNKNOWN_KEY,
            f"{path} contains unknown key {key}; host settings keys are host.<host>.startup-page-bytes.",
        )
    if value is None or _PAGE_BYTES.fullmatch(value) is None or not MIN_PAGE_BYTES <= int(value) <= MAX_PAGE_BYTES:
        found = "no value" if value is None else repr(value)
        return InvalidEntry(
            key,
            EntryProblem.INVALID_VALUE,
            f"{path} key {key} must be a whole number of bytes from {MIN_PAGE_BYTES} to {MAX_PAGE_BYTES}; found {found}.",
        )
    return matched[1], int(value)


def _read(path: Path, effects: SettingEffects, host: str) -> int | None:
    """Validate every stored entry, since all hosts share the file; return this host's value or None if absent."""
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as error:
        raise SettingResolutionError(str(error), path, effects, error, "read", path) from error
    if not stat.S_ISREG(mode):
        raise SettingResolutionError(
            f"Host settings {path} must be a regular file.", path, effects, NotRegularFile(path), "read", path
        )
    # Git reports an unreadable file only through its exit status; opening it first names the denied access.
    try:
        path.open("rb").close()
    except OSError as error:
        raise SettingResolutionError(str(error), path, effects, error, "read", path) from error
    listed = git_config.list_entries(path)
    if isinstance(listed, git_config.ReadFailed):
        raise SettingResolutionError(
            f"Cannot read host settings at {path}: {listed.cause.diagnostic}", path, effects, listed, "read", path
        )
    selected: int | None = None
    seen: set[str] = set()
    for entry in listed.entries:
        validated = _validated(path, entry.key, entry.value, seen)
        if isinstance(validated, InvalidEntry):
            raise SettingResolutionError(validated.diagnostic, path, effects, validated, "read", path)
        seen.add(entry.key)
        if validated[0] == host:
            selected = validated[1]
    return selected


def resolve(path: Path, host: str, measured_default: int) -> SettingResolution[int]:
    """Read the host's stored page size, seeding the measured default when its key is missing."""

    def read(settings: Path, effects: SettingEffects) -> int | None:
        return _read(settings, effects, host)

    return resolve_seeded_key(path, read, setting_key(host), str(measured_default), "Host page-size setting")
