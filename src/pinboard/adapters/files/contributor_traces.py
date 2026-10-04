"""Contributor-only local settings and private automatic invocation traces."""

from __future__ import annotations

import os
import secrets
import stat
import subprocess
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import msgspec

from pinboard.adapters.files import git_config
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode, ImmutableFilePublishedError, RootError
from pinboard.adapters.files.file_io import create_immutable
from pinboard.adapters.files.root import resolve_shared_repository_root, resolve_source_checkout_root
from pinboard.adapters.files.setting_resolution import SettingEffects, SettingResolution, SettingResolutionError

SETTINGS_NAME = "contributor-traces.config"
TRACE_DIRECTORY = "invocation-traces"
TRACE_LIMIT = 100


class ContributorTraceSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    unsafe_persist_exact_pinboard_traces: Literal["off", "on"]
    item_overrides: Mapping[str, Literal["inherit", "off", "on"]]


def _project_data_root(project_root: Path, work_root: Path | None) -> Path | None:
    try:
        checkout = resolve_source_checkout_root(project_root)
        shared_repository = resolve_shared_repository_root(checkout)
    except RootError:
        return None
    data_root = work_root.absolute() if work_root is not None else shared_repository / ".pinboard"
    if data_root.is_symlink():
        raise ValueError("Contributor trace work root must be a real directory.")
    if not data_root.is_dir():
        return None
    data_root = data_root.resolve()
    if not _ignored_or_external(data_root, SETTINGS_NAME):
        if (data_root / SETTINGS_NAME).exists(follow_symlinks=False):
            raise ValueError("Contributor trace settings must be Git-ignored before Pinboard can use them.")
        return None
    return data_root


def _ignored_or_external(data_root: Path, name: str) -> bool:
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "--", name],
        cwd=data_root,
        check=False,
        capture_output=True,
    )
    if ignored.returncode == 0:
        return True
    if ignored.returncode == 1:
        return False
    if ignored.returncode == 128 and not any((parent / ".git").exists() for parent in (data_root, *data_root.parents)):
        return True
    raise ValueError(f"Contributor trace Git status could not be verified for {data_root / name}.")


def _decode_settings(path: Path) -> ContributorTraceSettings | git_config.ReadFailed | None:
    if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
        raise ValueError("Contributor trace settings must be a regular file.")
    listed = git_config.list_entries(path)
    if isinstance(listed, git_config.ReadFailed):
        return listed
    project_mode: Literal["off", "on"] | None = None
    overrides: dict[str, Literal["inherit", "off", "on"]] = {}
    seen: set[str] = set()
    for entry in listed.entries:
        key, value = entry.key, entry.value
        if key in seen:
            raise ValueError("Contributor trace settings contain duplicate keys.")
        seen.add(key)
        if key == "pinboard.unsafe_persist_exact_pinboard_traces.mode" and value in ("off", "on"):
            project_mode = value
        elif key.startswith("item.") and key.endswith(".mode") and value in ("inherit", "off", "on"):
            item = key.removeprefix("item.").removesuffix(".mode")
            if not item:
                raise ValueError("Contributor trace item override needs an item ID.")
            overrides[item] = value
        else:
            raise ValueError("Contributor trace settings contain an unknown key or mode.")
    return ContributorTraceSettings(project_mode, MappingProxyType(overrides)) if project_mode is not None else None


def _observe_or_create_settings_file(path: Path) -> SettingEffects:
    """Observe file presence without masking access errors; preserve this invocation's creation effect."""
    effects = SettingEffects("none", "none", "none")
    try:
        path.stat(follow_symlinks=False)
        missing = False
    except FileNotFoundError:
        missing = True
    except OSError as error:
        raise SettingResolutionError(str(error), path, effects, error) from error
    if missing:
        try:
            effects = SettingEffects("none", "confirmed" if create_immutable(path, b"") else "none", "none")
        except ImmutableFilePublishedError as error:
            raise SettingResolutionError(
                str(error), path, SettingEffects("none", "confirmed", "none"), error
            ) from error
        except FileIOError as error:
            if error.code != FileIOErrorCode.FILE_ALREADY_EXISTS:
                raise SettingResolutionError(str(error), path, effects, error) from error
    return effects


def _settings(data_root: Path) -> SettingResolution[ContributorTraceSettings]:
    path = data_root / SETTINGS_NAME
    effects = _observe_or_create_settings_file(path)
    try:
        settings = _read_settings(path, effects)
        if settings is not None:
            return SettingResolution(path, settings, effects)
        # Git writers and the pre-runtime launcher honor this same exclusive lock.
        # Recheck while holding it; a stale absence cannot replace an explicit choice.
        lock = path.with_name(f"{path.name}.lock")
        with lock.open("xb") as staged:
            try:
                os.fchmod(staged.fileno(), stat.S_IMODE(path.stat().st_mode))
                settings = _read_settings(path, effects)
                if settings is not None:
                    return SettingResolution(path, settings, effects)
                staged.write(path.read_bytes())
                staged.flush()
                written = git_config.add(lock, "pinboard.unsafe_persist_exact_pinboard_traces.mode", "off")
                if isinstance(written, git_config.WriteUnconfirmed):
                    raise SettingResolutionError(
                        f"Cannot write Contributor trace project mode in {path}: {written.cause.diagnostic}",
                        path,
                        effects,
                        written,
                    )
                lock.replace(path)
                effects = SettingEffects(effects.parent_creation, effects.file_creation, "acknowledged")
            finally:
                lock.unlink(missing_ok=True)
        settings = _read_settings(path, effects)
        if settings is None:
            raise ValueError("Contributor trace settings must declare the project mode.")
        return SettingResolution(path, settings, effects)
    except (ValueError, OSError) as error:
        if isinstance(error, SettingResolutionError):
            raise
        raise SettingResolutionError(str(error), path, effects, error) from error


def _read_settings(path: Path, effects: SettingEffects) -> ContributorTraceSettings | None:
    settings = _decode_settings(path)
    if isinstance(settings, git_config.ReadFailed):
        raise SettingResolutionError(
            f"Cannot read Contributor trace settings at {path}: {settings.cause.diagnostic}",
            path,
            effects,
            settings,
        )
    return settings


def _trace_directory(data_root: Path) -> Path:
    path = data_root / TRACE_DIRECTORY
    if not _ignored_or_external(data_root, f"{TRACE_DIRECTORY}/"):
        raise ValueError("Contributor trace directory must be Git-ignored before Pinboard can use it.")
    with suppress(FileExistsError):
        path.mkdir(mode=0o700)
    if path.is_symlink() or not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise ValueError("Contributor trace directory must be a real private directory (0700).")
    return path


def prune_traces(directory: Path) -> None:
    def order(path: Path) -> tuple[int, str]:
        return path.stat().st_mtime_ns, path.name

    traces = sorted(
        (path for path in directory.iterdir() if path.name.startswith("pinboard-auto-") and path.is_file()),
        key=order,
    )
    for path in traces[:-TRACE_LIMIT]:
        path.unlink(missing_ok=True)


def read_project_trace_settings(
    project_root: Path, work_root: Path | None
) -> tuple[Path, SettingResolution[ContributorTraceSettings]] | None:
    data_root = _project_data_root(project_root, work_root)
    return None if data_root is None else (data_root, _settings(data_root))


def read_configured_project_trace_mode(work_root: Path) -> Literal["off", "on"]:
    path = work_root / SETTINGS_NAME
    try:
        settings = _decode_settings(path)
    except FileNotFoundError as error:
        raise ValueError("Contributor trace settings file is missing.") from error
    except OSError as error:
        raise ValueError(f"Cannot read Contributor trace settings at {path}: {error}") from error
    if isinstance(settings, git_config.ReadFailed):
        raise ValueError(f"Cannot read Contributor trace settings at {path}: {settings.cause.diagnostic}")
    if settings is None:
        raise ValueError("Contributor trace settings must declare the project mode.")
    return settings.unsafe_persist_exact_pinboard_traces


def automatic_trace_directory(data_root: Path, settings: ContributorTraceSettings, item_id: str | None) -> Path | None:
    mode = settings.item_overrides.get(item_id, "inherit") if item_id is not None else "inherit"
    effective = settings.unsafe_persist_exact_pinboard_traces if mode == "inherit" else mode
    return _trace_directory(data_root) if effective == "on" else None


def select_cli_trace(arguments: tuple[str, ...]) -> Path | None:
    project_root: Path | None = None
    work_root: Path | None = None
    position = 0
    while position < len(arguments):
        argument = arguments[position]
        if argument in {"--project-root", "--work-root"} and position + 1 < len(arguments):
            value = arguments[position + 1]
            position += 2
        elif argument.startswith(("--project-root=", "--work-root=")):
            argument, value = argument.split("=", 1)
            position += 1
        else:
            break
        if argument == "--project-root":
            project_root = Path(value)
        elif argument == "--work-root":
            work_root = Path(value)
    selected = project_root if project_root is not None else Path.cwd()
    state = read_project_trace_settings(selected, work_root)
    if state is None:
        return None
    item_id = (
        arguments[position + 1]
        if arguments[position : position + 1] == ("close",) and position + 1 < len(arguments)
        else None
    )
    directory = automatic_trace_directory(state[0], state[1].value, item_id)
    if directory is None:
        return None
    prune_traces(directory)
    return directory / f"pinboard-auto-cli-{secrets.token_hex(16)}.json"


def prune_cli_traces(selected: Path) -> None:
    if (
        selected.parent.name != TRACE_DIRECTORY
        or not selected.name.startswith("pinboard-auto-cli-")
        or selected.suffix != ".json"
    ):
        raise ValueError("Automatic trace destination is invalid.")
    prune_traces(selected.parent)
