"""Bound, reversible current-schema advancement with retained predecessor plan meaning.

The database transaction is the authority switch. Immutable plans and exact predecessor
bytes are published before that transaction, so a fresh invocation can decide
whether the old or predicted new database is authoritative.

The compatibility-prefixed v1 and v2 records share this selected plan directory and
strict decoder with current plans; retaining that mixed boundary preserves their
original byte identities. Remove them only when no supported predecessor plan or backup
requires resume or reversal.
"""

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal, assert_never

import msgspec

from pinboard.adapters.files.errors import ArtifactError, FileIOError, ImmutableFilePublishedError
from pinboard.adapters.files.file_io import atomic_replace, create_immutable, ensure_child_directory
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.migration.database import (
    current_schema_logical_digest,
    inspect_schema_migration,
    migrate_v6_database,
    migrate_v7_database,
)
from pinboard.adapters.sqlite.migration.schema_amendments_v13 import migrate_v13_database
from pinboard.adapters.sqlite.migration.schema_decisions_v10 import migrate_v10_database
from pinboard.adapters.sqlite.migration.schema_dependencies_v9 import migrate_v9_database
from pinboard.adapters.sqlite.migration.schema_holds_v14 import (
    PlannedHoldRelease,
    inspect_v14_migration,
    migrate_v14_database,
)
from pinboard.adapters.sqlite.migration.schema_membership_v11 import migrate_v11_database
from pinboard.adapters.sqlite.migration.schema_ownership_v8 import inspect_current_schema_migration, migrate_v8_database
from pinboard.adapters.sqlite.migration.schema_unblockers_v12 import (
    LabelInput,
    inspect_v12_migration,
    migrate_v12_database,
    read_labels,
)


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


class CompatibilityAdvancePlanV2(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="advance", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v2"]
    database_path: str
    source_version: Literal[7]
    target_version: Literal[8]
    source_sha256: str
    postimage_sha256: str


class CompatibilityCurrentPlanV2(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="current", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v2"]
    database_path: str
    source_sha256: str


class CompatibilityRestorePlanV2(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="restore", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v2"]
    database_path: str
    forward_plan_id: str
    postimage_sha256: str
    backup_sha256: str


class OwnershipAdvancePlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="ownership-advance", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v3"]
    database_path: str
    source_version: Literal[8]
    target_version: Literal[9]
    source_sha256: str
    postimage_sha256: str


class OwnershipCurrentPlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="ownership-current", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v3"]
    database_path: str
    source_sha256: str


class OwnershipRestorePlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="ownership-restore", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v3"]
    database_path: str
    forward_plan_id: str
    postimage_sha256: str
    backup_sha256: str


class DependencyAdvancePlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="dependency-advance", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v4"]
    database_path: str
    source_version: Literal[9]
    target_version: Literal[10]
    source_sha256: str
    postimage_sha256: str


class DependencyCurrentPlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="dependency-current", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v4"]
    database_path: str
    source_sha256: str


class DependencyRestorePlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="dependency-restore", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v4"]
    database_path: str
    forward_plan_id: str
    postimage_sha256: str
    backup_sha256: str


class DecisionBatchAdvancePlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="decisions-advance", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v5"]
    database_path: str
    source_version: Literal[10]
    target_version: Literal[11]
    source_sha256: str
    postimage_sha256: str


class DecisionBatchCurrentPlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="decisions-current", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v5"]
    database_path: str
    source_sha256: str


class DecisionBatchRestorePlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="decisions-restore", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v5"]
    database_path: str
    forward_plan_id: str
    postimage_sha256: str
    backup_sha256: str


class MembershipAdvancePlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="membership-advance", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v6"]
    database_path: str
    source_version: Literal[11]
    target_version: Literal[12]
    source_sha256: str
    postimage_sha256: str


class MembershipCurrentPlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="membership-current", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v6"]
    database_path: str
    source_sha256: str


class MembershipRestorePlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="membership-restore", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v6"]
    database_path: str
    forward_plan_id: str
    postimage_sha256: str
    backup_sha256: str


class UnblockerAdvancePlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="unblockers-advance", tag_field="kind"
):
    """The v12-to-v13 step, bound to its source ledger and to the exact hold label file it applies, if any."""

    schema: Literal["pinboard-schema-plan/v7"]
    database_path: str
    source_version: Literal[12]
    target_version: Literal[13]
    source_sha256: str
    labels_sha256: str | None
    postimage_sha256: str


class UnblockerCurrentPlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="unblockers-current", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v7"]
    database_path: str
    source_sha256: str


class UnblockerRestorePlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="unblockers-restore", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v7"]
    database_path: str
    forward_plan_id: str
    postimage_sha256: str
    backup_sha256: str


class AmendmentAdvancePlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="amendments-advance", tag_field="kind"
):
    """The v13-to-v14 step, which adds the empty attempt amendments table."""

    schema: Literal["pinboard-schema-plan/v8"]
    database_path: str
    source_version: Literal[13]
    target_version: Literal[14]
    source_sha256: str
    postimage_sha256: str


class AmendmentCurrentPlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="amendments-current", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v8"]
    database_path: str
    source_sha256: str


class AmendmentRestorePlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="amendments-restore", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v8"]
    database_path: str
    forward_plan_id: str
    postimage_sha256: str
    backup_sha256: str


class HoldReleaseAdvancePlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="hold-releases-advance", tag_field="kind"
):
    """The v14-to-v15 step, bound to its source ledger and the exact unstarted items holds it releases."""

    schema: Literal["pinboard-schema-plan/v9"]
    database_path: str
    source_version: Literal[14]
    target_version: Literal[15]
    source_sha256: str
    released_holds: tuple[PlannedHoldRelease, ...]
    postimage_sha256: str


class HoldReleaseCurrentPlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="hold-releases-current", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v9"]
    database_path: str
    source_sha256: str


class HoldReleaseRestorePlan(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="hold-releases-restore", tag_field="kind"
):
    schema: Literal["pinboard-schema-plan/v9"]
    database_path: str
    forward_plan_id: str
    postimage_sha256: str
    backup_sha256: str


type SchemaPlan = (
    CompatibilityForwardPlan
    | CompatibilityUnchangedPlan
    | CompatibilityReversePlan
    | CompatibilityAdvancePlanV2
    | CompatibilityCurrentPlanV2
    | CompatibilityRestorePlanV2
    | OwnershipAdvancePlan
    | OwnershipCurrentPlan
    | OwnershipRestorePlan
    | DependencyAdvancePlan
    | DependencyCurrentPlan
    | DependencyRestorePlan
    | DecisionBatchAdvancePlan
    | DecisionBatchCurrentPlan
    | DecisionBatchRestorePlan
    | MembershipAdvancePlan
    | MembershipCurrentPlan
    | MembershipRestorePlan
    | UnblockerAdvancePlan
    | UnblockerCurrentPlan
    | UnblockerRestorePlan
    | AmendmentAdvancePlan
    | AmendmentCurrentPlan
    | AmendmentRestorePlan
    | HoldReleaseAdvancePlan
    | HoldReleaseCurrentPlan
    | HoldReleaseRestorePlan
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
    resulting_version: Literal[6, 7, 8, 9, 10, 11, 12, 13, 14, 15]


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
    if isinstance(
        plan,
        (
            CompatibilityReversePlan,
            CompatibilityRestorePlanV2,
            OwnershipRestorePlan,
            DependencyRestorePlan,
            DecisionBatchRestorePlan,
            MembershipRestorePlan,
            UnblockerRestorePlan,
            AmendmentRestorePlan,
            HoldReleaseRestorePlan,
        ),
    ):
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


def _read_selected_labels(labels: Path | None) -> LabelInput | SchemaProcedureFailure | None:
    if labels is None:
        return None
    try:
        return read_labels(labels)
    except (OSError, ValueError) as error:
        return SchemaProcedureFailure(f"Schema preview rejected the hold label file: {error}", (), "correct-input")


def _preview_v14_advancement(path: Path) -> PlannedSchema | SchemaProcedureFailure:
    try:
        source, released_holds, postimage = inspect_v14_migration(path)
    except (StorageError, ValueError, msgspec.ValidationError) as error:
        return SchemaProcedureFailure(f"Schema preview refused the v14 step: {error}", (), "correct-input")
    plan = HoldReleaseAdvancePlan("pinboard-schema-plan/v9", str(path), 14, 15, source, released_holds, postimage)
    return PlannedSchema(_identity(plan), plan)


def _preview_advancement(  # noqa: C901, PLR0912 - exact supported predecessor steps
    path: Path, labels: Path | None
) -> PlannedSchema | SchemaProcedureFailure:
    try:
        version, source, postimage = inspect_current_schema_migration(path)
    except (ArtifactError, StorageError, OSError, ValueError, msgspec.ValidationError) as error:
        return SchemaProcedureFailure(
            f"Schema preview rejected the named predecessor evidence: {error}", (), "correct-input"
        )
    if version == 6:
        return _preview_v6_compatibility(path)
    if labels is not None and version != 12:
        return SchemaProcedureFailure(
            f"Hold labels apply only to the v12 step; this ledger is sqlite-v{version}. Preview without --labels.",
            (),
            "correct-input",
        )
    plan: SchemaPlan
    if version == 12:
        selected = _read_selected_labels(labels)
        if isinstance(selected, SchemaProcedureFailure):
            return selected
        try:
            source, postimage = inspect_v12_migration(path, selected)
        except (StorageError, ValueError, msgspec.ValidationError) as error:
            return SchemaProcedureFailure(f"Schema preview refused the v12 step: {error}", (), "correct-input")
        plan = UnblockerAdvancePlan(
            "pinboard-schema-plan/v7",
            str(path),
            12,
            13,
            source,
            None if selected is None else selected.sha256,
            postimage,
        )
        return PlannedSchema(_identity(plan), plan)
    if version == 14:
        return _preview_v14_advancement(path)
    if version == 7 and postimage is not None:
        plan = CompatibilityAdvancePlanV2("pinboard-schema-plan/v2", str(path), 7, 8, source, postimage)
    elif version == 8 and postimage is not None:
        plan = OwnershipAdvancePlan("pinboard-schema-plan/v3", str(path), 8, 9, source, postimage)
    elif version == 9 and postimage is not None:
        plan = DependencyAdvancePlan("pinboard-schema-plan/v4", str(path), 9, 10, source, postimage)
    elif version == 10 and postimage is not None:
        plan = DecisionBatchAdvancePlan("pinboard-schema-plan/v5", str(path), 10, 11, source, postimage)
    elif version == 11 and postimage is not None:
        plan = MembershipAdvancePlan("pinboard-schema-plan/v6", str(path), 11, 12, source, postimage)
    elif version == 13 and postimage is not None:
        plan = AmendmentAdvancePlan("pinboard-schema-plan/v8", str(path), 13, 14, source, postimage)
    else:
        plan = HoldReleaseCurrentPlan("pinboard-schema-plan/v9", str(path), source)
    return PlannedSchema(_identity(plan), plan)


def preview_schema(  # noqa: C901, PLR0912 - exact retained reversal families
    path: Path, reverse_forward_id: str | None = None, labels: Path | None = None
) -> PlannedSchema | SchemaProcedureFailure:
    """Preview the selected advancement or unchanged-state reversal; only the v12 step takes hold labels."""
    if reverse_forward_id is None:
        return _preview_advancement(path, labels)
    folder = path.parent / "migration"
    try:
        forward = _read_plan(folder, reverse_forward_id)
        if isinstance(forward, CompatibilityForwardPlan):
            return _preview_v6_compatibility(path, reverse_forward_id)
        if not isinstance(
            forward,
            (
                CompatibilityAdvancePlanV2,
                OwnershipAdvancePlan,
                DependencyAdvancePlan,
                DecisionBatchAdvancePlan,
                MembershipAdvancePlan,
                UnblockerAdvancePlan,
                AmendmentAdvancePlan,
                HoldReleaseAdvancePlan,
            ),
        ) or forward.database_path != str(path):
            return SchemaProcedureFailure(
                "The selected current forward schema plan is unavailable at this work root.", (), "correct-input"
            )
        backup = folder / f"{reverse_forward_id}.v{forward.source_version}"
        backup_digest = sha256(backup.read_bytes()).hexdigest()
        if backup_digest != forward.source_sha256:
            return SchemaProcedureFailure(
                f"The retained exact v{forward.source_version} backup differs from its forward plan.",
                (),
                "correct-input",
            )
        version, source, _ = inspect_current_schema_migration(path)
        match forward:
            case CompatibilityAdvancePlanV2():
                plan = CompatibilityRestorePlanV2(
                    "pinboard-schema-plan/v2", str(path), reverse_forward_id, forward.postimage_sha256, backup_digest
                )
            case OwnershipAdvancePlan():
                plan = OwnershipRestorePlan(
                    "pinboard-schema-plan/v3", str(path), reverse_forward_id, forward.postimage_sha256, backup_digest
                )
            case DependencyAdvancePlan():
                plan = DependencyRestorePlan(
                    "pinboard-schema-plan/v4", str(path), reverse_forward_id, forward.postimage_sha256, backup_digest
                )
            case DecisionBatchAdvancePlan():
                plan = DecisionBatchRestorePlan(
                    "pinboard-schema-plan/v5", str(path), reverse_forward_id, forward.postimage_sha256, backup_digest
                )
            case MembershipAdvancePlan():
                plan = MembershipRestorePlan(
                    "pinboard-schema-plan/v6", str(path), reverse_forward_id, forward.postimage_sha256, backup_digest
                )
            case UnblockerAdvancePlan():
                plan = UnblockerRestorePlan(
                    "pinboard-schema-plan/v7", str(path), reverse_forward_id, forward.postimage_sha256, backup_digest
                )
            case AmendmentAdvancePlan():
                plan = AmendmentRestorePlan(
                    "pinboard-schema-plan/v8", str(path), reverse_forward_id, forward.postimage_sha256, backup_digest
                )
            case HoldReleaseAdvancePlan():
                plan = HoldReleaseRestorePlan(
                    "pinboard-schema-plan/v9", str(path), reverse_forward_id, forward.postimage_sha256, backup_digest
                )
            case _ as unreachable:
                assert_never(unreachable)
        if version == forward.target_version:
            if current_schema_logical_digest(path) != forward.postimage_sha256:
                return SchemaProcedureFailure(
                    "The migrated ledger changed after schema migration; retain later writes and diagnose before reversal.",
                    (),
                    "correct-input",
                )
        elif (
            version != forward.source_version or source != backup_digest or _read_plan(folder, _identity(plan)) != plan
        ):
            return SchemaProcedureFailure(
                "The forward migration has not committed or its predecessor changed; preserve the ledger and resume its original plan before reversal.",
                (),
                "correct-input",
            )
        return PlannedSchema(_identity(plan), plan)
    except (OSError, ValueError, msgspec.ValidationError) as error:
        return SchemaProcedureFailure(f"Schema reversal evidence is unavailable: {error}", (), "correct-input")


def apply_schema(path: Path, plan_id: str, labels: Path | None = None) -> SchemaApplication:  # noqa: C901, PLR0912, PLR0915 - one bound migration commit point
    folder = path.parent / "migration"
    changed: list[str] = []
    try:
        selected = _read_plan(folder, plan_id)
        if selected is None:
            if plan_id.startswith("reverse-") and _valid_id(plan_id):
                preview = preview_schema(path, plan_id.split("-")[1])
            else:
                preview = preview_schema(path, None, labels)
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
        if isinstance(
            selected,
            (
                CompatibilityCurrentPlanV2,
                OwnershipCurrentPlan,
                DependencyCurrentPlan,
                DecisionBatchCurrentPlan,
                MembershipCurrentPlan,
                UnblockerCurrentPlan,
                AmendmentCurrentPlan,
                HoldReleaseCurrentPlan,
            ),
        ):
            match selected:
                case CompatibilityCurrentPlanV2():
                    expected_version = 8
                case OwnershipCurrentPlan():
                    expected_version = 9
                case DependencyCurrentPlan():
                    expected_version = 10
                case DecisionBatchCurrentPlan():
                    expected_version = 11
                case MembershipCurrentPlan():
                    expected_version = 12
                case UnblockerCurrentPlan():
                    expected_version = 13
                case AmendmentCurrentPlan():
                    expected_version = 14
                case HoldReleaseCurrentPlan():
                    expected_version = 15
                case _ as unreachable:
                    assert_never(unreachable)
            if version != expected_version or source != selected.source_sha256:
                return SchemaProcedureFailure(
                    "The unchanged current-schema plan no longer matches the database.", (), "correct-input"
                )
            return AppliedSchema(plan_id, "unchanged", (), expected_version)
        if isinstance(
            selected,
            (
                CompatibilityAdvancePlanV2,
                OwnershipAdvancePlan,
                DependencyAdvancePlan,
                DecisionBatchAdvancePlan,
                MembershipAdvancePlan,
                UnblockerAdvancePlan,
                AmendmentAdvancePlan,
                HoldReleaseAdvancePlan,
            ),
        ):
            selected_labels: LabelInput | None = None
            if isinstance(selected, UnblockerAdvancePlan):
                read = _read_selected_labels(labels)
                if isinstance(read, SchemaProcedureFailure):
                    return read
                if (None if read is None else read.sha256) != selected.labels_sha256:
                    return SchemaProcedureFailure(
                        "The v12 step must apply the exact hold label file its plan was previewed with; "
                        "pass that same --labels file or request a new preview.",
                        (),
                        "correct-input",
                    )
                selected_labels = read
            elif labels is not None:
                return SchemaProcedureFailure(
                    "Hold labels apply only to the v12 step; resume this plan without --labels.", (), "correct-input"
                )
            backup_path = folder / f"{plan_id}.v{selected.source_version}"
            if version == selected.target_version:
                if (
                    current_schema_logical_digest(path) != selected.postimage_sha256
                    or sha256(backup_path.read_bytes()).hexdigest() != selected.source_sha256
                ):
                    return SchemaProcedureFailure(
                        f"The migrated ledger or retained exact v{selected.source_version} backup differs from its bound plan; preserve both for diagnosis.",
                        (),
                        "correct-input",
                    )
                return AppliedSchema(plan_id, "forward", (), selected.target_version)
            if version != selected.source_version or source != selected.source_sha256:
                return SchemaProcedureFailure(
                    f"The v{selected.source_version} source changed since preview; request a new plan.",
                    (),
                    "correct-input",
                )
            ensure_child_directory(path.parent, "migration")
            if create_immutable(folder / f"{plan_id}.json", _canonical(selected)):
                changed.append("migration-evidence")
            backup = path.read_bytes()
            if sha256(backup).hexdigest() != selected.source_sha256:
                return SchemaProcedureFailure(
                    f"The v{selected.source_version} source changed before backup publication.",
                    tuple(changed),
                    "correct-input",
                )
            if create_immutable(backup_path, backup) and "migration-evidence" not in changed:
                changed.append("migration-evidence")
            if sha256(backup_path.read_bytes()).hexdigest() != selected.source_sha256:
                return SchemaProcedureFailure(
                    f"The retained exact v{selected.source_version} backup is invalid.", tuple(changed), "correct-input"
                )
            if (
                selected_labels is not None
                and create_immutable(folder / f"{plan_id}.labels.json", selected_labels.content)
                and "migration-evidence" not in changed
            ):
                changed.append("migration-evidence")
            match selected:
                case CompatibilityAdvancePlanV2():
                    migrated = migrate_v7_database(path)
                case OwnershipAdvancePlan():
                    migrated = migrate_v8_database(path, selected.postimage_sha256)
                case DependencyAdvancePlan():
                    migrated = migrate_v9_database(path, selected.postimage_sha256)
                case DecisionBatchAdvancePlan():
                    migrated = migrate_v10_database(path, selected.postimage_sha256)
                case MembershipAdvancePlan():
                    migrated = migrate_v11_database(path, selected.postimage_sha256)
                case UnblockerAdvancePlan():
                    migrated = migrate_v12_database(
                        path, selected.source_sha256, selected_labels, selected.postimage_sha256
                    )
                case AmendmentAdvancePlan():
                    migrated = migrate_v13_database(path, selected.postimage_sha256)
                case HoldReleaseAdvancePlan():
                    migrated = migrate_v14_database(path, selected.postimage_sha256)
                case _ as unreachable:
                    assert_never(unreachable)
            if migrated:
                changed.append("ledger")
            if current_schema_logical_digest(path) != selected.postimage_sha256:
                return SchemaProcedureFailure(
                    "The current result differs from the bound postimage; retain ledger and backup for diagnosis.",
                    tuple(changed),
                    "correct-input",
                )
            return AppliedSchema(plan_id, "forward", tuple(changed), selected.target_version)
        reverse_preview = preview_schema(path, selected.forward_plan_id)
        if not isinstance(reverse_preview, PlannedSchema) or selected != reverse_preview.plan:
            return SchemaProcedureFailure(
                "The reverse schema plan no longer matches its predecessor; preserve later writes.", (), "correct-input"
            )
        forward = _read_plan(folder, selected.forward_plan_id)
        if not isinstance(
            forward,
            (
                CompatibilityAdvancePlanV2,
                OwnershipAdvancePlan,
                DependencyAdvancePlan,
                DecisionBatchAdvancePlan,
                MembershipAdvancePlan,
                UnblockerAdvancePlan,
                AmendmentAdvancePlan,
                HoldReleaseAdvancePlan,
            ),
        ):
            return SchemaProcedureFailure("The original forward migration is unavailable.", (), "correct-input")
        if version == forward.source_version and source == selected.backup_sha256:
            return AppliedSchema(plan_id, "reverse", (), forward.source_version)
        if version != forward.target_version or current_schema_logical_digest(path) != selected.postimage_sha256:
            return SchemaProcedureFailure(
                "The migrated ledger changed after schema migration; retain later writes and diagnose before reversal.",
                (),
                "correct-input",
            )
        backup = (folder / f"{selected.forward_plan_id}.v{forward.source_version}").read_bytes()
        if sha256(backup).hexdigest() != selected.backup_sha256:
            return SchemaProcedureFailure(
                f"The retained exact v{forward.source_version} backup is invalid.", (), "correct-input"
            )
        return _restore_predecessor(path, plan_id, selected, backup, forward.source_version, changed)
    except (ArtifactError, FileIOError, StorageError, OSError, ValueError, msgspec.ValidationError) as error:
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
    selected: CompatibilityReversePlan
    | CompatibilityRestorePlanV2
    | OwnershipRestorePlan
    | DependencyRestorePlan
    | DecisionBatchRestorePlan
    | MembershipRestorePlan
    | UnblockerRestorePlan
    | AmendmentRestorePlan
    | HoldReleaseRestorePlan,
    backup: bytes,
    version: Literal[6, 7, 8, 9, 10, 11, 12, 13, 14],
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
