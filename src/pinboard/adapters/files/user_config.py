"""User-level settings needed before MCP tool registration."""

from __future__ import annotations

import os
from pathlib import Path

from pinboard.adapters.files import git_config


def read_mcp_omit_regex_lookarounds() -> bool:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    path = (Path(config_home) if config_home else Path.home() / ".config") / "pinboard" / "config"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        for missing in (False, True):
            result = git_config.get_all(path, "mcp.omitRegexLookarounds", as_bool=True)
            if result.returncode == 1 and not missing:
                written = git_config.add(path, "mcp.omitRegexLookarounds", "true")
                if written.returncode == 0:
                    continue
                raise ValueError(
                    f"Cannot write Pinboard MCP setting in {path}: {written.stderr.decode(errors='replace').strip()}"
                )
            if result.returncode != 0 or result.stdout not in (b"true\0", b"false\0"):
                raise ValueError(
                    f"Invalid Pinboard MCP config at {path}: set [mcp] omitRegexLookarounds to true or false exactly once."
                )
            return result.stdout == b"true\0"
    except OSError as error:
        raise ValueError(f"Cannot read or write Pinboard MCP config at {path}: {error}") from error
    raise AssertionError("Pinboard MCP config did not resolve after materialization.")
