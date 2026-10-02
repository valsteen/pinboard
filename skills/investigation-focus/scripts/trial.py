"""Local metadata collector for a human-reviewed investigation trial return."""

import argparse
import fcntl
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, assert_never

import msgspec

type Label = Annotated[str, msgspec.Meta(min_length=1)]
type Commit = Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{40}\z")]


class Source(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    source_id: Label
    revision: Label
    window: Label


class Output(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    output_id: Label
    revision: Label
    sources: tuple[Source, ...]


class Intervention(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    kind: Literal["direction", "correction", "acknowledgement", "dismissal"]
    summary: Label


class Failure(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    kind: Label
    summary: Label


class Coverage(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    source_id: Label
    status: Literal["inaccessible", "unsearched", "withheld"]
    summary: Label


class Session(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    inquiry_id: Label
    session_id: Label
    context: Literal["fresh", "resumed"]
    candidate_commit: Commit
    model: Label
    reasoning: Label
    sources: tuple[Source, ...]
    outputs: tuple[Output, ...]
    interventions: tuple[Intervention, ...]
    failures: tuple[Failure, ...]
    coverage: tuple[Coverage, ...]
    cost_usd: float | None
    cost_basis: Literal["reported", "estimated", "unavailable"]

    def __post_init__(self) -> None:
        if (self.cost_usd is None) != (self.cost_basis == "unavailable"):
            raise ValueError("cost and cost_basis must agree")
        if self.cost_usd is not None and (self.cost_usd < 0 or not math.isfinite(self.cost_usd)):
            raise ValueError("cost_usd must be a nonnegative finite value")


class Input(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["investigation-trial-session/v1"]
    session: Session
    local_note: Label


class Stored(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    sequence: int
    recorded_at: str
    session: Session
    local_note: str


class ReturnRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    sequence: int
    recorded_at: str
    session: Session


class ReturnDraft(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["investigation-trial-return-draft/v1"]
    sessions: tuple[ReturnRow, ...]


@dataclass(frozen=True)
class Record:
    home: Path
    input_path: Path


@dataclass(frozen=True)
class Export:
    home: Path


def read_rows(data: bytes) -> list[Stored]:
    rows = [msgspec.json.decode(line, type=Stored) for line in data.splitlines()]
    if any(row.sequence != index for index, row in enumerate(rows, 1)):
        raise ValueError("trial log sequence is damaged")
    return rows


def record(home: Path, input_path: Path) -> Stored:
    entry = msgspec.json.decode(input_path.read_bytes(), type=Input)
    fd = os.open(home / "trial.jsonl", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as log:
        fcntl.flock(log, fcntl.LOCK_EX)
        log.seek(0)
        rows = read_rows(log.read())
        if any(
            (row.session.inquiry_id, row.session.session_id) == (entry.session.inquiry_id, entry.session.session_id)
            for row in rows
        ):
            raise ValueError("session_id already exists in this inquiry")
        row = Stored(len(rows) + 1, datetime.now(UTC).isoformat(), entry.session, entry.local_note)
        log.seek(0, os.SEEK_END)
        log.write(msgspec.json.encode(row) + b"\n")
        log.flush()
        os.fsync(log.fileno())
        return row


def export(home: Path) -> Path:
    with (home / "trial.jsonl").open("rb") as log:
        fcntl.flock(log, fcntl.LOCK_SH)
        rows = read_rows(log.read())
    draft = ReturnDraft(
        "investigation-trial-return-draft/v1",
        tuple(ReturnRow(row.sequence, row.recorded_at, row.session) for row in rows),
    )
    destination = home / f"trial-return-draft-{len(rows)}.json"
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(msgspec.json.encode(draft) + b"\n")
    return destination


def parse_command() -> Record | Export:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    record_parser = commands.add_parser("record")
    record_parser.add_argument("--home", required=True, type=Path)
    record_parser.add_argument("--input", required=True, type=Path)
    export_parser = commands.add_parser("export")
    export_parser.add_argument("--home", required=True, type=Path)
    args = parser.parse_args()
    if not args.home.is_dir():
        parser.error("--home must be an existing caller-selected directory")
    match args.command:
        case "record":
            return Record(args.home, args.input)
        case "export":
            return Export(args.home)
    return parser.error("unsupported command")


def main() -> None:
    match parse_command():
        case Record(home, input_path):
            print(f"recorded sequence {record(home, input_path).sequence}")
        case Export(home):
            print(export(home))
        case _ as unreachable:
            assert_never(unreachable)


if __name__ == "__main__":
    main()
