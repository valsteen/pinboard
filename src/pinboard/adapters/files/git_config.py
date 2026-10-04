"""Translate Git configuration process results for Python setting owners."""

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkingDirectoryUnavailable:
    diagnostic: str
    process_failure: LaunchFailed | ProcessFailed


@dataclass(frozen=True)
class LaunchFailed:
    diagnostic: str


@dataclass(frozen=True)
class ProcessFailed:
    returncode: int
    diagnostic: str


@dataclass(frozen=True)
class InvalidOutput:
    diagnostic: str


type Failure = WorkingDirectoryUnavailable | LaunchFailed | ProcessFailed | InvalidOutput


@dataclass(frozen=True)
class Entry:
    key: str
    value: str


@dataclass(frozen=True)
class Entries:
    path: Path
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class ReadFailed:
    path: Path
    cause: Failure


@dataclass(frozen=True)
class WriteAcknowledged:
    """Git returned success; this does not certify durable storage."""

    path: Path
    key: str


@dataclass(frozen=True)
class WriteUnconfirmed:
    path: Path
    key: str
    cause: WorkingDirectoryUnavailable | LaunchFailed | ProcessFailed


def _run(
    path: Path, *arguments: str
) -> subprocess.CompletedProcess[bytes] | WorkingDirectoryUnavailable | LaunchFailed:
    try:
        result = subprocess.run(["git", "config", "--file", str(path), *arguments], capture_output=True, check=False)
    except OSError as error:
        result = LaunchFailed(str(error))
    if isinstance(result, LaunchFailed) or result.returncode != 0:
        # A process diagnostic alone cannot prove that its inherited cwd is unavailable.
        try:
            Path.cwd()
        except OSError as error:
            failure = (
                result
                if isinstance(result, LaunchFailed)
                else ProcessFailed(result.returncode, result.stderr.decode(errors="replace").strip())
            )
            return WorkingDirectoryUnavailable(
                f"{failure.diagnostic}; service working directory observation: {error}", failure
            )
    return result


def _fields(output: bytes) -> tuple[str, ...] | None:
    if not output:
        return ()
    if not output.endswith(b"\0"):
        return None
    try:
        return tuple(part.decode("utf-8") for part in output[:-1].split(b"\0"))
    except UnicodeError:
        return None


def list_entries(path: Path) -> Entries | ReadFailed:
    result = _run(path, "--null", "--list")
    if isinstance(result, WorkingDirectoryUnavailable | LaunchFailed):
        return ReadFailed(path, result)
    if result.returncode != 0:
        return ReadFailed(path, ProcessFailed(result.returncode, result.stderr.decode(errors="replace").strip()))
    fields = _fields(result.stdout)
    if fields is None or any("\n" not in field for field in fields):
        return ReadFailed(path, InvalidOutput("Invalid Git configuration entry framing."))
    return Entries(path, tuple(Entry(*field.split("\n", 1)) for field in fields))


def add(path: Path, key: str, value: str) -> WriteAcknowledged | WriteUnconfirmed:
    result = _run(path, "--add", key, value)
    if isinstance(result, WorkingDirectoryUnavailable | LaunchFailed):
        return WriteUnconfirmed(path, key, result)
    if result.returncode != 0:
        return WriteUnconfirmed(
            path, key, ProcessFailed(result.returncode, result.stderr.decode(errors="replace").strip())
        )
    return WriteAcknowledged(path, key)
