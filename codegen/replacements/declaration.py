"""Canonical projection and invariant declaration for replacement records."""

from dataclasses import dataclass

from pinboard.application import stored_state


@dataclass(frozen=True, slots=True)
class Copy:
    pass


@dataclass(frozen=True, slots=True)
class Text:
    pass


@dataclass(frozen=True, slots=True)
class Isoformat:
    pass


type ProjectionOperation = Copy | Text | Isoformat


@dataclass(frozen=True, slots=True)
class KnownItems:
    fields: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class ContiguousRevisions:
    item_field: str
    revision_field: str
    message: str


@dataclass(frozen=True, slots=True)
class KnownReplacement:
    item_field: str
    revision_field: str
    message: str


@dataclass(frozen=True, slots=True)
class CurrentReplacement:
    status_field: str
    message: str


@dataclass(frozen=True, slots=True)
class ExactCost:
    disposition_field: str
    replacement_field: str
    message: str


@dataclass(frozen=True, slots=True)
class UniqueDisposition:
    message: str


type InvariantOperation = (
    KnownItems | ContiguousRevisions | KnownReplacement | CurrentReplacement | ExactCost | UniqueDisposition
)


@dataclass(frozen=True, slots=True)
class ProjectedField:
    source: str
    target: str
    operation: ProjectionOperation


@dataclass(frozen=True, slots=True)
class Collection:
    source: str
    target: str
    target_row: str
    fields: tuple[ProjectedField, ...]
    invariants: tuple[InvariantOperation, ...]


@dataclass(frozen=True, slots=True)
class ReplacementFamily:
    source: type
    collections: tuple[Collection, ...]


REPLACEMENTS = ReplacementFamily(
    stored_state.ReplacementRecords,
    (
        Collection(
            "planned_replacements",
            "planned_replacements",
            "_HandoverPlannedReplacement",
            (
                ProjectedField("affected_item_id", "affected_item_id", Text()),
                ProjectedField("relation_revision", "relation_revision", Copy()),
                ProjectedField("replacement_item_id", "replacement_item_id", Text()),
                ProjectedField("replacement_cost", "replacement_cost", Copy()),
                ProjectedField("status", "status", Copy()),
                ProjectedField("recorded_by", "recorded_by", Text()),
                ProjectedField("recorded_at", "recorded_at", Isoformat()),
                ProjectedField("accepted_project_revision", "accepted_project_revision", Copy()),
            ),
            (
                KnownItems(
                    ("affected_item_id", "replacement_item_id"),
                    "A planned replacement names an unknown work item.",
                ),
                ContiguousRevisions(
                    "affected_item_id",
                    "relation_revision",
                    "Planned replacement revisions must be contiguous from revision 1.",
                ),
            ),
        ),
        Collection(
            "dispositions",
            "replacement_dispositions",
            "_HandoverReplacementDisposition",
            (
                ProjectedField("affected_item_id", "affected_item_id", Text()),
                ProjectedField("relation_revision", "relation_revision", Copy()),
                ProjectedField("rationale", "rationale", Copy()),
                ProjectedField("accepted_cost", "accepted_cost", Copy()),
                ProjectedField("recorded_by", "recorded_by", Text()),
                ProjectedField("recorded_at", "recorded_at", Isoformat()),
                ProjectedField("accepted_project_revision", "accepted_project_revision", Copy()),
            ),
            (
                KnownReplacement(
                    "affected_item_id",
                    "relation_revision",
                    "A temporary-retention disposition names an unknown replacement revision.",
                ),
                CurrentReplacement(
                    "status",
                    "A temporary-retention disposition names a withdrawn replacement revision.",
                ),
                ExactCost(
                    "accepted_cost",
                    "replacement_cost",
                    "A temporary-retention disposition must accept the exact replacement cost.",
                ),
                UniqueDisposition(
                    "A replacement revision has duplicate temporary-retention dispositions.",
                ),
            ),
        ),
    ),
)
