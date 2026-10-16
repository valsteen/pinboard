"""Strict stored initiative JSON values and their domain conversions."""

from datetime import date as HumanDate
from datetime import datetime
from typing import Annotated

import msgspec

from pinboard.domain import initiatives as models
from pinboard.domain.identifiers import HostId, TaskId

Text = Annotated[str, msgspec.Meta(min_length=1)]


class DefinitionRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    title: Text
    goal: Text
    rationale: Text
    success_signal: Text
    project_references: tuple[Text, ...]


class ReasonRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    human: Text
    date: HumanDate
    reason: Text


class ProvenanceRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    reason: ReasonRecord
    task_id: Text
    host_id: Text
    committed_at: datetime
    project_revision: Annotated[int, msgspec.Meta(ge=1)]


def definition(value: DefinitionRecord) -> models.Definition:
    return models.Definition(value.title, value.goal, value.rationale, value.success_signal, value.project_references)


def provenance(value: ProvenanceRecord) -> models.Provenance:
    return models.Provenance(
        models.HumanReason(value.reason.human, value.reason.date, value.reason.reason),
        TaskId(value.task_id),
        HostId(value.host_id),
        value.committed_at,
        value.project_revision,
    )


def encode_definition(value: models.Definition) -> bytes:
    return msgspec.json.encode(msgspec.convert(value, type=DefinitionRecord, from_attributes=True), order="sorted")


def encode_provenance(value: models.Provenance) -> bytes:
    return msgspec.json.encode(msgspec.convert(value, type=ProvenanceRecord, from_attributes=True), order="sorted")


class RevisionRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    initiative_id: Text
    revision: int
    evaluation_context: int
    definition: DefinitionRecord
    provenance: ProvenanceRecord


class MembershipRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    initiative_id: Text
    item_id: Text
    episode: int
    added: ProvenanceRecord
    removed: ProvenanceRecord | None


class DecisionRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    initiative_id: Text
    decision: Text
    supporting_links: tuple[Text, ...]
    provenance: ProvenanceRecord


class AssessmentRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    initiative_id: Text
    evaluation_context: int
    evaluated_goal: Text
    evaluated_signal: Text
    judgment: models.Judgment
    supporting_links: tuple[Text, ...]
    provenance: ProvenanceRecord


class InitiativeRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    initiative_id: Text
    subject_revision: int
    revisions: Annotated[tuple[RevisionRecord, ...], msgspec.Meta(min_length=1)]
    memberships: tuple[MembershipRecord, ...]
    decisions: tuple[DecisionRecord, ...]
    assessments: tuple[AssessmentRecord, ...]


def initiative_record(value: models.Initiative) -> InitiativeRecord:
    return msgspec.convert(value, type=InitiativeRecord, from_attributes=True)
