"""Exact selected consolidation facts, displayed changes and a separately confirmed binding."""

from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated, Literal

import msgspec

from pinboard.domain import consolidation, history, work_models
from pinboard.domain.identifiers import AttemptId, CheckpointId, WorkItemId
from pinboard.domain.ledger import LedgerSnapshot


class Source(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: history.Identity
    replacement_cost: history.CanonicalLine


class CheckpointTarget(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    consumer: history.Identity
    checkpoint: history.Identity
    covers_carried_work: history.CanonicalLine


class Campaign(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: history.Identity
    create: bool
    definition: history.DefinitionPayload


class Selection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    sources: Annotated[tuple[Source, ...], msgspec.Meta(min_length=1)]
    campaign: Campaign
    checkpoint_targets: tuple[CheckpointTarget, ...]

    def __post_init__(self) -> None:
        ids = tuple(value.item_id for value in self.sources)
        if len(ids) != len(set(ids)) or self.campaign.item_id in ids:
            raise ValueError("sources must be unique and exclude the campaign owner")
        consumers = tuple(value.consumer for value in self.checkpoint_targets)
        if len(consumers) != len(set(consumers)):
            raise ValueError("checkpoint targets must name unique consumers")


@dataclass(frozen=True, slots=True)
class Facts:
    snapshot: LedgerSnapshot
    live_order: tuple[WorkItemId, ...]


class NoAttempt(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="none", tag_field="kind"):
    pass


class RetainedAttempt(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="retained", tag_field="kind"):
    attempt_id: AttemptId
    state: work_models.AttemptState
    subject_revision: str
    accepted_scope_revision: int | None
    accepted_scope_digest: str | None
    candidate: str | None
    brief_artifact_ref_id: int | None


class SelectedItem(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    state: work_models.WorkState
    subject_revision: str
    definition_revision: int
    definition_digest: str
    definition: history.DefinitionPayload
    attempt: NoAttempt | RetainedAttempt


class DefinitionChange(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    revision: int
    digest: str
    definition: history.DefinitionPayload


class QueueChange(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    before: int | None
    after: int | None


class Replacement(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    source: WorkItemId
    relation_revision: int
    campaign: WorkItemId
    replacement_cost: str


class Preview(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="pinboard-consolidation-preview/v1", tag_field="schema"
):
    selection: Selection
    selected_items: tuple[SelectedItem, ...]
    campaign: DefinitionChange
    dependents: tuple[DefinitionChange, ...]
    replacements: tuple[Replacement, ...]
    attempt_authorities: tuple[work_models.AttemptAuthority, ...]
    preparation_authorities: tuple[work_models.PreparationAuthority, ...]
    live_attempt_authorities: tuple[work_models.CommandAttemptAuthority, ...]
    live_preparation_authorities: tuple[work_models.PreparationCommandAuthority, ...]
    queue_changes: tuple[QueueChange, ...]
    lifecycle_effect: Literal[
        "Sources end as superseded; paused/blocked attempts retain evidence and lose write authority. Campaign and surviving consumer lifecycles, candidates and authority are preserved; their accepted scopes may become stale. No process, launch or repository effect."
    ]
    checkpoint_effect: Literal[
        "Declared checkpoints do not enforce execution ordering. Outside consumers wait for the whole campaign unless the separately approved checkpoint mapping covers all their carried prerequisite work."
    ]


class Binding(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    work_root: Annotated[str, msgspec.Meta(min_length=1)]
    schema: Literal["pinboard-consolidation-binding/v1"]
    preview_sha256: Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{64}\z")]


class Record(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="pinboard-consolidation/v1", tag_field="schema"
):
    binding: Binding
    preview: Preview
    human_confirmation: history.RecordText

    def __post_init__(self) -> None:
        if not self.human_confirmation.strip():
            raise ValueError("consolidation requires the human's separate confirmation")
        if self.binding.preview_sha256 != sha256(msgspec.json.encode(self.preview, order="sorted")).hexdigest():
            raise ValueError("consolidation binding differs from its recorded preview")


def plan(selection: Selection) -> consolidation.Plan:
    return consolidation.Plan(
        tuple(consolidation.Source(WorkItemId(value.item_id), value.replacement_cost) for value in selection.sources),
        WorkItemId(selection.campaign.item_id),
        history.work_item_definition_from_payload(selection.campaign.definition),
        selection.campaign.create,
        tuple(
            consolidation.CheckpointTarget(
                WorkItemId(value.consumer), CheckpointId(value.checkpoint), value.covers_carried_work
            )
            for value in selection.checkpoint_targets
        ),
    )
