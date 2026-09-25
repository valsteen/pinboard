"""User-level settings needed before MCP tool registration."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode
from pinboard.adapters.files.file_io import create_immutable


def read_mcp_omit_regex_lookarounds() -> bool:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    path = (Path(config_home) if config_home else Path.home() / ".config") / "pinboard" / "config"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            try:
                create_immutable(path, b"[mcp]\n\tomitRegexLookarounds = true\n")
            except FileIOError as error:
                if error.code != FileIOErrorCode.FILE_ALREADY_EXISTS:
                    raise
        for missing in (False, True):
            result = subprocess.run(
                [
                    "git",
                    "config",
                    "--file",
                    str(path),
                    "--null",
                    "--type=bool",
                    "--get-all",
                    "mcp.omitRegexLookarounds",
                ],
                capture_output=True,
                check=False,
            )
            if result.returncode == 1 and not missing:
                written = subprocess.run(
                    ["git", "config", "--file", str(path), "--add", "mcp.omitRegexLookarounds", "true"],
                    capture_output=True,
                    check=False,
                )
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
    except (OSError, FileIOError) as error:
        raise ValueError(f"Cannot read or write Pinboard MCP config at {path}: {error}") from error
    raise AssertionError("Pinboard MCP config did not resolve after materialization.")
