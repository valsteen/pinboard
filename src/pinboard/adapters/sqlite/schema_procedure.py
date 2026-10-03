"""Bound, reversible v6-to-v7 procedure around one atomic SQLite upgrade.

The database transaction is the authority switch. Immutable plans and exact v6
bytes are published before that transaction, so a fresh invocation can decide
whether the old or predicted new database is authoritative.
"""

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

import msgspec

from pinboard.adapters.files.errors import FileIOError, ImmutableFilePublishedError
from pinboard.adapters.files.file_io import atomic_replace, create_immutable, ensure_child_directory
from pinboard.adapters.sqlite.database import (
    current_schema_logical_digest,
    inspect_schema_migration,
    migrate_v6_database,
)
from pinboard.adapters.sqlite.errors import StorageError


class ForwardPlan(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="forward", tag_field="kind"):
    schema: Literal["pinboard-schema-plan/v1"]
    database_path: str
    source_sha256: str
    postimage_sha256: str


class UnchangedPlan(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="unchanged", tag_field="kind"):
    schema: Literal["pinboard-schema-plan/v1"]
    database_path: str
    source_sha256: str


class ReversePlan(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="reverse", tag_field="kind"):
    schema: Literal["pinboard-schema-plan/v1"]
    database_path: str
    forward_plan_id: str
    postimage_sha256: str
    backup_sha256: str


type SchemaPlan = ForwardPlan | UnchangedPlan | ReversePlan


@dataclass(frozen=True, slots=True)
class PlannedSchema:
    plan_id: str
    plan: SchemaPlan


@dataclass(frozen=True, slots=True)
class AppliedSchema:
    plan_id: str
    direction: Literal["forward", "reverse", "unchanged"]
    changed_surfaces: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SchemaProcedureFailure:
    message: str
    changed_surfaces: tuple[str, ...]
    retry: Literal["correct-input", "retry-same-input"]


type SchemaApplication = AppliedSchema | SchemaProcedureFailure


def _canonical(plan: SchemaPlan) -> bytes:
    return msgspec.json.encode(plan, order="sorted")


def _identity(plan: SchemaPlan) -> str:
    digest = sha256(_canonical(plan)).hexdigest()
    if isinstance(plan, ReversePlan):
        return f"reverse-{plan.forward_plan_id}-{digest}"
    return digest


def _valid_id(plan_id: str) -> bool:
    return re.fullmatch(r"(?:[0-9a-f]{64}|reverse-[0-9a-f]{64}-[0-9a-f]{64})", plan_id) is not None


def _read_plan(folder: Path, plan_id: str) -> SchemaPlan | None:
    if not _valid_id(plan_id):
        return None
    path = folder / f"{plan_id}.json"
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return None
    plan: SchemaPlan = msgspec.json.decode(content, type=SchemaPlan)
    if content != _canonical(plan) or _identity(plan) != plan_id:
        raise ValueError(f"Stored schema plan differs from its identity: {path}")
    return plan


def preview_schema(path: Path, reverse_forward_id: str | None = None) -> PlannedSchema | SchemaProcedureFailure:
    folder = path.parent / "migration"
    if reverse_forward_id is None:
        version, source, postimage = inspect_schema_migration(path)
        plan: SchemaPlan = (
            ForwardPlan("pinboard-schema-plan/v1", str(path), source, postimage)
            if version == 6 and postimage is not None
            else UnchangedPlan("pinboard-schema-plan/v1", str(path), source)
        )
        return PlannedSchema(_identity(plan), plan)
    try:
        forward = _read_plan(folder, reverse_forward_id)
        if not isinstance(forward, ForwardPlan) or forward.database_path != str(path):
            return SchemaProcedureFailure(
                "The selected forward schema plan is unavailable at this work root.", (), "correct-input"
            )
        backup = folder / f"{reverse_forward_id}.v6"
        backup_digest = sha256(backup.read_bytes()).hexdigest()
        if backup_digest != forward.source_sha256:
            return SchemaProcedureFailure(
                "The retained exact v6 backup differs from its forward plan.", (), "correct-input"
            )
        version, source, _ = inspect_schema_migration(path)
        if version == 7:
            if current_schema_logical_digest(path) != forward.postimage_sha256:
                return SchemaProcedureFailure(
                    "The migrated ledger changed after schema migration; retain it and diagnose the later writes before reversal.",
                    (),
                    "correct-input",
                )
        elif source != backup_digest:
            return SchemaProcedureFailure(
                "The schema predecessor differs from the retained backup.", (), "correct-input"
            )
        plan = ReversePlan(
            "pinboard-schema-plan/v1", str(path), reverse_forward_id, forward.postimage_sha256, backup_digest
        )
        if version == 6 and _read_plan(folder, _identity(plan)) != plan:
            return SchemaProcedureFailure(
                "The forward schema migration has not committed; resume its original --apply plan before reversal.",
                (),
                "correct-input",
            )
        return PlannedSchema(_identity(plan), plan)
    except (OSError, ValueError, msgspec.ValidationError) as error:
        return SchemaProcedureFailure(f"Schema reversal evidence is unavailable: {error}", (), "correct-input")


def apply_schema(path: Path, plan_id: str) -> SchemaApplication:  # noqa: C901, PLR0912, PLR0915 - one atomic effect owner
    folder = path.parent / "migration"
    changed: list[str] = []
    try:
        selected = _read_plan(folder, plan_id)
        if selected is None:
            if plan_id.startswith("reverse-") and _valid_id(plan_id):
                forward_id = plan_id.split("-")[1]
                preview = preview_schema(path, forward_id)
            else:
                preview = preview_schema(path)
            if isinstance(preview, SchemaProcedureFailure):
                return preview
            if preview.plan_id != plan_id:
                return SchemaProcedureFailure(
                    "The observed schema plan changed; request a new preview.", (), "correct-input"
                )
            selected = preview.plan
        if selected.database_path != str(path):
            return SchemaProcedureFailure("The schema plan belongs to another selected database.", (), "correct-input")
        if isinstance(selected, UnchangedPlan):
            version, source, _ = inspect_schema_migration(path)
            if version != 7 or source != selected.source_sha256:
                return SchemaProcedureFailure(
                    "The unchanged schema plan no longer matches the database.", (), "correct-input"
                )
            return AppliedSchema(plan_id, "unchanged", ())
        if isinstance(selected, ForwardPlan):
            version, source, _ = inspect_schema_migration(path)
            if version == 7:
                if current_schema_logical_digest(path) != selected.postimage_sha256:
                    return SchemaProcedureFailure(
                        "The migrated ledger differs from the recorded postimage; preserve it and diagnose later writes.",
                        (),
                        "correct-input",
                    )
                backup = folder / f"{plan_id}.v6"
                if sha256(backup.read_bytes()).hexdigest() != selected.source_sha256:
                    return SchemaProcedureFailure("The retained v6 backup is invalid.", (), "correct-input")
                return AppliedSchema(plan_id, "forward", ())
            if source != selected.source_sha256:
                return SchemaProcedureFailure(
                    "The v6 source changed since preview; request a new plan.", (), "correct-input"
                )
            ensure_child_directory(path.parent, "migration")
            if create_immutable(folder / f"{plan_id}.json", _canonical(selected)):
                changed.append("migration-evidence")
            backup_bytes = path.read_bytes()
            if sha256(backup_bytes).hexdigest() != selected.source_sha256:
                return SchemaProcedureFailure(
                    "The v6 source changed before backup publication.", tuple(changed), "correct-input"
                )
            if create_immutable(folder / f"{plan_id}.v6", backup_bytes) and "migration-evidence" not in changed:
                changed.append("migration-evidence")
            if sha256((folder / f"{plan_id}.v6").read_bytes()).hexdigest() != selected.source_sha256:
                return SchemaProcedureFailure("The v6 backup could not be verified.", tuple(changed), "correct-input")
            if migrate_v6_database(path):
                changed.append("ledger")
            if current_schema_logical_digest(path) != selected.postimage_sha256:
                return SchemaProcedureFailure(
                    "The v7 result differs from the bound postimage; retain the database and backup for diagnosis.",
                    tuple(changed),
                    "correct-input",
                )
            return AppliedSchema(plan_id, "forward", tuple(changed))
        forward = _read_plan(folder, selected.forward_plan_id)
        reverse_preview = preview_schema(path, selected.forward_plan_id)
        if (
            not isinstance(forward, ForwardPlan)
            or not isinstance(reverse_preview, PlannedSchema)
            or selected != reverse_preview.plan
        ):
            return SchemaProcedureFailure(
                "The reverse schema plan no longer matches its predecessor.", (), "correct-input"
            )
        version, source, _ = inspect_schema_migration(path)
        if version == 6 and source == selected.backup_sha256:
            return AppliedSchema(plan_id, "reverse", ())
        if version != 7 or current_schema_logical_digest(path) != selected.postimage_sha256:
            return SchemaProcedureFailure(
                "The migrated ledger changed after schema migration; retain it and diagnose the later writes.",
                (),
                "correct-input",
            )
        backup = (folder / f"{selected.forward_plan_id}.v6").read_bytes()
        if sha256(backup).hexdigest() != selected.backup_sha256:
            return SchemaProcedureFailure("The retained v6 backup is invalid.", (), "correct-input")
        if create_immutable(folder / f"{plan_id}.json", _canonical(selected)):
            changed.append("migration-evidence")
        try:
            atomic_replace(path, backup)
        except FileIOError:
            if path.read_bytes() == backup:
                changed.append("ledger")
            raise
        changed.append("ledger")
        if sha256(path.read_bytes()).hexdigest() != selected.backup_sha256:
            return SchemaProcedureFailure(
                "The restored v6 bytes differ from the backup.", tuple(changed), "correct-input"
            )
        return AppliedSchema(plan_id, "reverse", tuple(changed))
    except (FileIOError, StorageError, OSError, ValueError, msgspec.ValidationError) as error:
        if isinstance(error, ImmutableFilePublishedError) and "migration-evidence" not in changed:
            changed.append("migration-evidence")
        return SchemaProcedureFailure(
            f"Schema migration stopped: {error}. Resume only the same --apply {plan_id} after repairing access.",
            tuple(changed),
            "retry-same-input",
        )
