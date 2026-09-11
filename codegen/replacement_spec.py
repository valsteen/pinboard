"""Declarative projection and invariant ownership for replacement records."""

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


@dataclass(frozen=True, slots=True)
class ContiguousRevisions:
    item_field: str
    revision_field: str


@dataclass(frozen=True, slots=True)
class KnownReplacement:
    item_field: str
    revision_field: str


@dataclass(frozen=True, slots=True)
class CurrentReplacement:
    pass


@dataclass(frozen=True, slots=True)
class ExactCost:
    disposition_field: str
    replacement_field: str


@dataclass(frozen=True, slots=True)
class UniqueDisposition:
    pass


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
            "HandoverPlannedReplacement",
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
                KnownItems(("affected_item_id", "replacement_item_id")),
                ContiguousRevisions("affected_item_id", "relation_revision"),
            ),
        ),
        Collection(
            "dispositions",
            "replacement_dispositions",
            "HandoverReplacementDisposition",
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
                KnownReplacement("affected_item_id", "relation_revision"),
                CurrentReplacement(),
                ExactCost("accepted_cost", "replacement_cost"),
                UniqueDisposition(),
            ),
        ),
    ),
)
