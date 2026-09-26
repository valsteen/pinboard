"""User-level settings needed before MCP tool registration."""

from __future__ import annotations

import os
from pathlib import Path

from pinboard.adapters.files import git_config
from pinboard.adapters.files.setting_resolution import SettingEffects, SettingResolution, SettingResolutionError


def read_mcp_omit_regex_lookarounds() -> SettingResolution[bool]:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    path = (Path(config_home) if config_home else Path.home() / ".config") / "pinboard" / "config"
    effects = SettingEffects("none", "none", "none")
    try:
        absent = not path.exists(follow_symlinks=False)
        result = git_config.get_all(path, "mcp.omitRegexLookarounds", as_bool=True)
        if isinstance(result, git_config.Missing):
            if not path.parent.exists(follow_symlinks=False):
                effects = SettingEffects("unconfirmed", "none", "none")
            path.parent.mkdir(parents=True, exist_ok=True)
            written = git_config.add(path, "mcp.omitRegexLookarounds", "true")
            if isinstance(written, git_config.WriteUnconfirmed):
                effects = SettingEffects(effects.parent_creation, "unconfirmed" if absent else "none", "unconfirmed")
                raise SettingResolutionError(
                    f"Cannot write Pinboard MCP setting in {path}: {written.diagnostic}", path, effects
                )
            effects = SettingEffects(effects.parent_creation, "unconfirmed" if absent else "none", "acknowledged")
            result = git_config.get_all(path, "mcp.omitRegexLookarounds", as_bool=True)
        if isinstance(result, git_config.ReadFailed):
            raise SettingResolutionError(
                f"Cannot read Pinboard MCP config at {path}: {result.diagnostic}", path, effects
            )
        if not isinstance(result, git_config.Values) or result.values not in ((True,), (False,)):
            raise SettingResolutionError(
                f"Invalid Pinboard MCP config at {path}: set [mcp] omitRegexLookarounds to true or false exactly once.",
                path,
                effects,
            )
        return SettingResolution(path, result.values[0], effects)
    except OSError as error:
        raise SettingResolutionError(
            f"Cannot read or write Pinboard MCP config at {path}: {error}", path, effects
        ) from error
