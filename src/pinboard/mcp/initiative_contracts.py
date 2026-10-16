"""Exact initiative operation leaves; all writes require dated human attribution."""

from datetime import date as HumanDate
from typing import Annotated, Literal

import msgspec

from pinboard.application import stored_state
from pinboard.application.initiative_records import InitiativeRecord
from pinboard.domain.initiatives import Judgment, RejectionCode

Text = Annotated[str, msgspec.Meta(min_length=1)]
Identity = Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9][a-z0-9-]*\z")]
Revision = Annotated[int, msgspec.Meta(ge=1)]


class Roots(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    project_root: Text
    work_root: Text


class Mutation(Roots, frozen=True, forbid_unknown_fields=True, kw_only=True):
    initiative_id: Identity
    human: Text
    date: HumanDate
    reason: Text
    actor_task_id: Text
    actor_host_id: Text


class DefinitionFields(Mutation, frozen=True, forbid_unknown_fields=True, kw_only=True):
    title: Text
    goal: Text
    rationale: Text
    success_signal: Text
    project_references: tuple[Text, ...]


class Create(
    DefinitionFields, tag="create", tag_field="operation", frozen=True, forbid_unknown_fields=True, kw_only=True
):
    pass


class Revise(
    DefinitionFields, tag="revise", tag_field="operation", frozen=True, forbid_unknown_fields=True, kw_only=True
):
    expected_revision: Revision


class AddMember(
    Mutation, tag="member-add", tag_field="operation", frozen=True, forbid_unknown_fields=True, kw_only=True
):
    expected_revision: Revision
    item_id: Identity


class RemoveMember(
    Mutation, tag="member-remove", tag_field="operation", frozen=True, forbid_unknown_fields=True, kw_only=True
):
    expected_revision: Revision
    item_id: Identity


class Decision(Mutation, tag="decision", tag_field="operation", frozen=True, forbid_unknown_fields=True, kw_only=True):
    expected_revision: Revision
    decision: Text
    supporting_links: tuple[Text, ...]


class Assess(Mutation, tag="assess", tag_field="operation", frozen=True, forbid_unknown_fields=True, kw_only=True):
    expected_revision: Revision
    evaluation_context: Revision
    judgment: Judgment
    supporting_links: tuple[Text, ...]


class Read(Roots, tag="read", tag_field="operation", frozen=True, forbid_unknown_fields=True, kw_only=True):
    initiative_id: Identity


class Roadmap(Roots, tag="roadmap", tag_field="operation", frozen=True, forbid_unknown_fields=True, kw_only=True):
    pass


type Operation = Create | Revise | AddMember | RemoveMember | Decision | Assess | Read | Roadmap


class Envelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: Operation


class Rejected(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-initiative-result/v1"]
    status: Literal["rejected"]
    code: RejectionCode
    message: Text
    recovery: Text
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: tuple[()]

    def __post_init__(self) -> None:
        if self.state_changed:
            raise ValueError("An initiative rejection has no committed effect.")


class Warning(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    message: Text
    recovery: Text


class ReadResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-initiative-result/v1"]
    status: Literal["ok"]
    initiative: InitiativeRecord
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: tuple[()]

    def __post_init__(self) -> None:
        if self.state_changed:
            raise ValueError("An initiative read has no committed effect.")


class Committed(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-initiative-result/v1"]
    status: Literal["committed", "committed-with-warning"]
    initiative_id: Text
    committed_revision: Revision
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: tuple[Literal["ledger"]]
    warning: Warning | None

    def __post_init__(self) -> None:
        if not self.state_changed or ((self.status == "committed") != (self.warning is None)):
            raise ValueError("Initiative commitment and warning status must match the reported effect.")


class Wave(msgspec.Struct, tag="wave", frozen=True, forbid_unknown_fields=True):
    number: Revision


class Blocked(msgspec.Struct, tag="blocked", frozen=True, forbid_unknown_fields=True):
    path: tuple[Text, ...]


class Unknown(msgspec.Struct, tag="unknown", frozen=True, forbid_unknown_fields=True):
    condition: Text


class Terminal(msgspec.Struct, tag="terminal", frozen=True, forbid_unknown_fields=True):
    pass


class RoadmapItem(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: Text
    title: Text
    state: stored_state.StoredWorkItemState
    dependencies: tuple[Text, ...]
    queue_position: int | None
    placement: Wave | Blocked | Unknown | Terminal


class Counts(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    delivered: int
    cancelled: int
    superseded: int
    unfinished: int


class RoadmapResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-roadmap-result/v1"]
    status: Literal["ok"]
    snapshot_revision: int
    initiatives: tuple[InitiativeRecord, ...]
    items: tuple[RoadmapItem, ...]
    unassigned: tuple[Text, ...]
    external_prerequisites: tuple[Text, ...]
    totals: Counts
    selection: Literal["Distinct current initiative members plus unassigned live items"]
    continuation: Literal[
        "Read-only snapshot; reload generated roadmap.html after refresh. Waves grant no execution authority."
    ]
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: tuple[()]

    def __post_init__(self) -> None:
        if self.state_changed:
            raise ValueError("A roadmap read has no committed effect.")
