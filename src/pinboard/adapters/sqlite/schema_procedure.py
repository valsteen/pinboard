"""Bound, reversible current-schema advancement with retained v6-to-v7 plan meaning.

The database transaction is the authority switch. Immutable plans and exact predecessor
bytes are published before that transaction, so a fresh invocation can decide
whether the old or predicted new database is authoritative.

The compatibility-prefixed v1 records share this selected plan directory and
strict decoder with current plans; retaining that mixed boundary preserves their
original byte identities. Remove them only when no supported v1 plan or backup
requires resume or reversal.
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
    inspect_current_schema_migration,
    inspect_schema_migration,
    migrate_v6_database,
    migrate_v7_database,
)
from pinboard.adapters.sqlite.errors import StorageError


class CompatibilityForwardPlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="forward", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v1"]
    database_path: str
    source_sha256: str
    postimage_sha256: str


class CompatibilityUnchangedPlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="unchanged", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v1"]
    database_path: str
    source_sha256: str


class CompatibilityReversePlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="reverse", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v1"]
    database_path: str
    forward_plan_id: str
    postimage_sha256: str
    backup_sha256: str


class AdvancePlan(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="advance", tag_field="kind"):
    schema: Literal["pinboard-schema-plan/v2"]
    database_path: str
    source_version: Literal[7]
    target_version: Literal[8]
    source_sha256: str
    postimage_sha256: str


class CurrentPlan(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="current", tag_field="kind"):
    schema: Literal["pinboard-schema-plan/v2"]
    database_path: str
    source_sha256: str


class RestorePlan(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="restore", tag_field="kind"):
    schema: Literal["pinboard-schema-plan/v2"]
    database_path: str
    forward_plan_id: str
    postimage_sha256: str
    backup_sha256: str


type SchemaPlan = (
    CompatibilityForwardPlan
    | CompatibilityUnchangedPlan
    | CompatibilityReversePlan
    | AdvancePlan
    | CurrentPlan
    | RestorePlan
)


@dataclass(frozen=True, slots=True)
class PlannedSchema:
    plan_id: str
    plan: SchemaPlan


@dataclass(frozen=True, slots=True)
class AppliedSchema:
    plan_id: str
    direction: Literal["forward", "reverse", "unchanged"]
    changed_surfaces: tuple[str, ...]
    resulting_version: Literal[6, 7, 8]


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
    if isinstance(plan, (CompatibilityReversePlan, RestorePlan)):
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


def _preview_v6_compatibility(
    path: Path, reverse_forward_id: str | None = None
) -> PlannedSchema | SchemaProcedureFailure:
    folder = path.parent / "migration"
    if reverse_forward_id is None:
        version, source, postimage = inspect_schema_migration(path)
        plan: SchemaPlan = (
            CompatibilityForwardPlan("pinboard-schema-plan/v1", str(path), source, postimage)
            if version == 6 and postimage is not None
            else CompatibilityUnchangedPlan("pinboard-schema-plan/v1", str(path), source)
        )
        return PlannedSchema(_identity(plan), plan)
    try:
        forward = _read_plan(folder, reverse_forward_id)
        if not isinstance(forward, CompatibilityForwardPlan) or forward.database_path != str(path):
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
        plan = CompatibilityReversePlan(
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


def _apply_v6_compatibility(path: Path, plan_id: str) -> SchemaApplication:  # noqa: C901, PLR0912 - one atomic effect owner
    folder = path.parent / "migration"
    changed: list[str] = []
    try:
        selected = _read_plan(folder, plan_id)
        if selected is None:
            if plan_id.startswith("reverse-") and _valid_id(plan_id):
                forward_id = plan_id.split("-")[1]
                preview = _preview_v6_compatibility(path, forward_id)
            else:
                preview = _preview_v6_compatibility(path)
            if isinstance(preview, SchemaProcedureFailure):
                return preview
            if preview.plan_id != plan_id:
                return SchemaProcedureFailure(
                    "The observed schema plan changed; request a new preview.", (), "correct-input"
                )
            selected = preview.plan
        if not isinstance(selected, (CompatibilityForwardPlan, CompatibilityReversePlan, CompatibilityUnchangedPlan)):
            return SchemaProcedureFailure("The selected plan is not a retained v6 migration plan.", (), "correct-input")
        if selected.database_path != str(path):
            return SchemaProcedureFailure("The schema plan belongs to another selected database.", (), "correct-input")
        if isinstance(selected, CompatibilityUnchangedPlan):
            version, source, _ = inspect_schema_migration(path)
            if version != 7 or source != selected.source_sha256:
                return SchemaProcedureFailure(
                    "The unchanged schema plan no longer matches the database.", (), "correct-input"
                )
            return AppliedSchema(plan_id, "unchanged", (), 7)
        if isinstance(selected, CompatibilityForwardPlan):
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
                return AppliedSchema(plan_id, "forward", (), 7)
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
            return AppliedSchema(plan_id, "forward", tuple(changed), 7)
        forward = _read_plan(folder, selected.forward_plan_id)
        reverse_preview = _preview_v6_compatibility(path, selected.forward_plan_id)
        if (
            not isinstance(forward, CompatibilityForwardPlan)
            or not isinstance(reverse_preview, PlannedSchema)
            or selected != reverse_preview.plan
        ):
            return SchemaProcedureFailure(
                "The reverse schema plan no longer matches its predecessor.", (), "correct-input"
            )
        version, source, _ = inspect_schema_migration(path)
        if version == 6 and source == selected.backup_sha256:
            return AppliedSchema(plan_id, "reverse", (), 6)
        if version != 7 or current_schema_logical_digest(path) != selected.postimage_sha256:
            return SchemaProcedureFailure(
                "The migrated ledger changed after schema migration; retain it and diagnose the later writes.",
                (),
                "correct-input",
            )
        backup = (folder / f"{selected.forward_plan_id}.v6").read_bytes()
        if sha256(backup).hexdigest() != selected.backup_sha256:
            return SchemaProcedureFailure("The retained v6 backup is invalid.", (), "correct-input")
        return _restore_predecessor(path, plan_id, selected, backup, 6, changed)
    except (FileIOError, StorageError, OSError, ValueError, msgspec.ValidationError) as error:
        if isinstance(error, ImmutableFilePublishedError) and "migration-evidence" not in changed:
            changed.append("migration-evidence")
        return SchemaProcedureFailure(
            f"Schema migration stopped: {error}. Resume only the same --apply {plan_id} after repairing access.",
            tuple(changed),
            "retry-same-input",
        )


def preview_schema(path: Path, reverse_forward_id: str | None = None) -> PlannedSchema | SchemaProcedureFailure:
    """Preview a current advancement, or preserve the exact meaning of retained v1 plans."""
    if reverse_forward_id is None:
        version, source, postimage = inspect_current_schema_migration(path)
        if version == 6:
            return _preview_v6_compatibility(path)
        plan: SchemaPlan
        if version == 7 and postimage is not None:
            plan = AdvancePlan("pinboard-schema-plan/v2", str(path), 7, 8, source, postimage)
        else:
            plan = CurrentPlan("pinboard-schema-plan/v2", str(path), source)
        return PlannedSchema(_identity(plan), plan)
    folder = path.parent / "migration"
    try:
        forward = _read_plan(folder, reverse_forward_id)
        if isinstance(forward, CompatibilityForwardPlan):
            return _preview_v6_compatibility(path, reverse_forward_id)
        if not isinstance(forward, AdvancePlan) or forward.database_path != str(path):
            return SchemaProcedureFailure(
                "The selected current forward schema plan is unavailable at this work root.", (), "correct-input"
            )
        backup = folder / f"{reverse_forward_id}.v7"
        backup_digest = sha256(backup.read_bytes()).hexdigest()
        if backup_digest != forward.source_sha256:
            return SchemaProcedureFailure(
                "The retained exact v7 backup differs from its forward plan.", (), "correct-input"
            )
        version, source, _ = inspect_current_schema_migration(path)
        plan = RestorePlan(
            "pinboard-schema-plan/v2", str(path), reverse_forward_id, forward.postimage_sha256, backup_digest
        )
        if version == 8:
            if current_schema_logical_digest(path) != forward.postimage_sha256:
                return SchemaProcedureFailure(
                    "The migrated ledger changed after schema migration; retain later writes and diagnose before reversal.",
                    (),
                    "correct-input",
                )
        elif version != 7 or source != backup_digest or _read_plan(folder, _identity(plan)) != plan:
            return SchemaProcedureFailure(
                "The forward migration has not committed or its predecessor changed; preserve the ledger and resume its original plan before reversal.",
                (),
                "correct-input",
            )
        return PlannedSchema(_identity(plan), plan)
    except (OSError, ValueError, msgspec.ValidationError) as error:
        return SchemaProcedureFailure(f"Schema reversal evidence is unavailable: {error}", (), "correct-input")


def apply_schema(path: Path, plan_id: str) -> SchemaApplication:  # noqa: C901, PLR0912 - one bound migration commit point
    folder = path.parent / "migration"
    changed: list[str] = []
    try:
        selected = _read_plan(folder, plan_id)
        if selected is None:
            if plan_id.startswith("reverse-") and _valid_id(plan_id):
                preview = preview_schema(path, plan_id.split("-")[1])
            else:
                preview = preview_schema(path)
            if isinstance(preview, SchemaProcedureFailure):
                return preview
            if preview.plan_id != plan_id:
                return SchemaProcedureFailure(
                    "The observed schema plan changed; request a new preview.", (), "correct-input"
                )
            selected = preview.plan
        if isinstance(selected, (CompatibilityForwardPlan, CompatibilityUnchangedPlan, CompatibilityReversePlan)):
            return _apply_v6_compatibility(path, plan_id)
        if selected.database_path != str(path):
            return SchemaProcedureFailure("The schema plan belongs to another selected database.", (), "correct-input")
        version, source, _ = inspect_current_schema_migration(path)
        if isinstance(selected, CurrentPlan):
            if version != 8 or source != selected.source_sha256:
                return SchemaProcedureFailure(
                    "The unchanged current-schema plan no longer matches the database.", (), "correct-input"
                )
            return AppliedSchema(plan_id, "unchanged", (), 8)
        if isinstance(selected, AdvancePlan):
            backup_path = folder / f"{plan_id}.v7"
            if version == 8:
                if (
                    current_schema_logical_digest(path) != selected.postimage_sha256
                    or sha256(backup_path.read_bytes()).hexdigest() != selected.source_sha256
                ):
                    return SchemaProcedureFailure(
                        "The migrated ledger or retained exact v7 backup differs from its bound plan; preserve both for diagnosis.",
                        (),
                        "correct-input",
                    )
                return AppliedSchema(plan_id, "forward", (), 8)
            if version != selected.source_version or source != selected.source_sha256:
                return SchemaProcedureFailure(
                    "The v7 source changed since preview; request a new plan.", (), "correct-input"
                )
            ensure_child_directory(path.parent, "migration")
            if create_immutable(folder / f"{plan_id}.json", _canonical(selected)):
                changed.append("migration-evidence")
            backup = path.read_bytes()
            if sha256(backup).hexdigest() != selected.source_sha256:
                return SchemaProcedureFailure(
                    "The v7 source changed before backup publication.", tuple(changed), "correct-input"
                )
            if create_immutable(backup_path, backup) and "migration-evidence" not in changed:
                changed.append("migration-evidence")
            if sha256(backup_path.read_bytes()).hexdigest() != selected.source_sha256:
                return SchemaProcedureFailure(
                    "The retained exact v7 backup is invalid.", tuple(changed), "correct-input"
                )
            if migrate_v7_database(path):
                changed.append("ledger")
            if current_schema_logical_digest(path) != selected.postimage_sha256:
                return SchemaProcedureFailure(
                    "The current result differs from the bound postimage; retain ledger and backup for diagnosis.",
                    tuple(changed),
                    "correct-input",
                )
            return AppliedSchema(plan_id, "forward", tuple(changed), 8)
        reverse_preview = preview_schema(path, selected.forward_plan_id)
        if not isinstance(reverse_preview, PlannedSchema) or selected != reverse_preview.plan:
            return SchemaProcedureFailure(
                "The reverse schema plan no longer matches its predecessor; preserve later writes.", (), "correct-input"
            )
        if version == 7 and source == selected.backup_sha256:
            return AppliedSchema(plan_id, "reverse", (), 7)
        if version != 8 or current_schema_logical_digest(path) != selected.postimage_sha256:
            return SchemaProcedureFailure(
                "The migrated ledger changed after schema migration; retain later writes and diagnose before reversal.",
                (),
                "correct-input",
            )
        backup = (folder / f"{selected.forward_plan_id}.v7").read_bytes()
        if sha256(backup).hexdigest() != selected.backup_sha256:
            return SchemaProcedureFailure("The retained exact v7 backup is invalid.", (), "correct-input")
        return _restore_predecessor(path, plan_id, selected, backup, 7, changed)
    except (FileIOError, StorageError, OSError, ValueError, msgspec.ValidationError) as error:
        if isinstance(error, ImmutableFilePublishedError) and "migration-evidence" not in changed:
            changed.append("migration-evidence")
        return SchemaProcedureFailure(
            f"Schema migration stopped: {error}. Resume only the same --apply {plan_id} after repairing access.",
            tuple(changed),
            "retry-same-input",
        )


def _restore_predecessor(
    path: Path,
    plan_id: str,
    selected: CompatibilityReversePlan | RestorePlan,
    backup: bytes,
    version: Literal[6, 7],
    changed: list[str],
) -> SchemaApplication:
    """Publish the bound reverse plan, replace exact predecessor bytes and retain confirmed effects."""
    if create_immutable(path.parent / "migration" / f"{plan_id}.json", _canonical(selected)):
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
            f"The restored v{version} bytes differ from the retained backup.", tuple(changed), "correct-input"
        )
    return AppliedSchema(plan_id, "reverse", tuple(changed), version)
