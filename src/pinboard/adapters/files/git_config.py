"""Translate Git configuration process results for Python setting owners."""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, overload


@dataclass(frozen=True)
class Values[T]:
    path: Path
    key: str
    values: tuple[T, ...]


@dataclass(frozen=True)
class Entry:
    key: str
    value: str


@dataclass(frozen=True)
class Entries:
    path: Path
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class Missing:
    path: Path
    key: str


@dataclass(frozen=True)
class ReadFailed:
    path: Path
    operation: Literal["get-all", "list-entries"]
    key: str | None
    diagnostic: str


@dataclass(frozen=True)
class WriteAcknowledged:
    """Git returned success; this does not certify durable storage."""

    path: Path
    key: str


@dataclass(frozen=True)
class WriteUnconfirmed:
    path: Path
    key: str
    diagnostic: str


def _run(path: Path, *arguments: str) -> subprocess.CompletedProcess[bytes] | OSError:
    try:
        return subprocess.run(["git", "config", "--file", str(path), *arguments], capture_output=True, check=False)
    except OSError as error:
        return error


def _diagnostic(result: subprocess.CompletedProcess[bytes] | OSError) -> str:
    return str(result) if isinstance(result, OSError) else result.stderr.decode(errors="replace").strip()


def _fields(output: bytes) -> tuple[str, ...] | None:
    if not output:
        return ()
    if not output.endswith(b"\0"):
        return None
    try:
        return tuple(part.decode("utf-8") for part in output[:-1].split(b"\0"))
    except UnicodeError:
        return None


@overload
def get_all(path: Path, key: str, *, as_bool: Literal[True]) -> Values[bool] | Missing | ReadFailed: ...


@overload
def get_all(path: Path, key: str, *, as_bool: Literal[False]) -> Values[str] | Missing | ReadFailed: ...


def get_all(path: Path, key: str, *, as_bool: bool) -> Values[bool] | Values[str] | Missing | ReadFailed:
    result = _run(path, "--null", *(("--type=bool",) if as_bool else ()), "--get-all", key)
    if isinstance(result, OSError):
        return ReadFailed(path, "get-all", key, _diagnostic(result))
    if result.returncode == 1 and not result.stdout and not result.stderr:
        return Missing(path, key)
    if result.returncode != 0 or (values := _fields(result.stdout)) is None or not values:
        return ReadFailed(path, "get-all", key, _diagnostic(result) or "Invalid Git configuration value framing.")
    if as_bool:
        if any(value not in {"true", "false"} for value in values):
            return ReadFailed(path, "get-all", key, "Invalid Git boolean value.")
        return Values(path, key, tuple(value == "true" for value in values))
    return Values(path, key, values)


def list_entries(path: Path) -> Entries | ReadFailed:
    result = _run(path, "--null", "--list")
    if isinstance(result, OSError) or result.returncode != 0:
        return ReadFailed(path, "list-entries", None, _diagnostic(result))
    fields = _fields(result.stdout)
    if fields is None or any("\n" not in field for field in fields):
        return ReadFailed(path, "list-entries", None, "Invalid Git configuration entry framing.")
    return Entries(path, tuple(Entry(*field.split("\n", 1)) for field in fields))


def add(path: Path, key: str, value: str) -> WriteAcknowledged | WriteUnconfirmed:
    result = _run(path, "--add", key, value)
    if isinstance(result, OSError) or result.returncode != 0:
        return WriteUnconfirmed(path, key, _diagnostic(result))
    return WriteAcknowledged(path, key)
