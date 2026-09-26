"""Run Git configuration reads and key-level writes for Pinboard settings."""

import subprocess
from pathlib import Path


def _run(path: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "config", "--file", str(path), *arguments], capture_output=True, check=False)


def get_all(path: Path, key: str, *, as_bool: bool) -> subprocess.CompletedProcess[bytes]:
    return _run(path, "--null", *(("--type=bool",) if as_bool else ()), "--get-all", key)


def list_entries(path: Path) -> subprocess.CompletedProcess[bytes]:
    return _run(path, "--null", "--list")


def add(path: Path, key: str, value: str) -> subprocess.CompletedProcess[bytes]:
    return _run(path, "--add", key, value)
