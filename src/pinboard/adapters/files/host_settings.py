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


def ensure_directory(path: Path) -> bool:
    """Create the settings file's parent privately when missing; report whether this call created it."""
    directory = path.parent
    if directory.is_dir():
        return False
    try:
        directory.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        return False
    except OSError as error:
        raise SettingResolutionError(
            f"Cannot create the host settings directory {directory}: {error}",
            path,
            SettingEffects("none", "none", "none"),
            error,
            "create",
            directory,
        ) from error
    return True


def _page_bytes(path: Path, key: str, value: str) -> int:
    if _PAGE_BYTES.fullmatch(value) is None or not MIN_PAGE_BYTES <= int(value) <= MAX_PAGE_BYTES:
        raise ValueError(
            f"{path} key {key} must be a whole number of bytes from {MIN_PAGE_BYTES} to {MAX_PAGE_BYTES}; found {value!r}."
        )
    return int(value)


def _read(path: Path, effects: SettingEffects, host: str) -> int | None:
    try:
        if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
            raise ValueError(f"Host settings {path} must be a regular file.")
    except OSError as error:
        raise SettingResolutionError(str(error), path, effects, error, "read", path) from error
    except ValueError as error:
        raise SettingResolutionError(str(error), path, effects, error, "read", path) from error
    listed = git_config.list_entries(path)
    if isinstance(listed, git_config.ReadFailed):
        raise SettingResolutionError(
            f"Cannot read host settings at {path}: {listed.cause.diagnostic}", path, effects, listed, "read", path
        )
    selected: int | None = None
    seen: set[str] = set()
    for entry in listed.entries:
        try:
            if entry.key in seen:
                raise ValueError(f"{path} sets key {entry.key} more than once; keep one value.")
            seen.add(entry.key)
            matched = _KEY.fullmatch(entry.key)
            if matched is None:
                raise ValueError(
                    f"{path} contains unknown key {entry.key}; host settings keys are host.<host>.startup-page-bytes."
                )
            page_bytes = _page_bytes(path, entry.key, entry.value)
        except ValueError as error:
            raise SettingResolutionError(str(error), path, effects, error, "read", path) from error
        if matched[1] == host:
            selected = page_bytes
    return selected


def resolve(path: Path, host: str, measured_default: int) -> SettingResolution[int]:
    """Read the host's stored page size, seeding the measured default when its key is missing."""

    def read(settings: Path, effects: SettingEffects) -> int | None:
        return _read(settings, effects, host)

    return resolve_seeded_key(path, read, setting_key(host), str(measured_default), "Host page-size setting")
