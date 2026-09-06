from typing import Annotated, Literal

import msgspec

from pinboard.domain import work_models
from pinboard.domain.history import WorkItemDefinitionPayload

type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\n]+\z")]
type Identity = Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]
type Sha256 = Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{64}\z")]
type PositiveInt = Annotated[int, msgspec.Meta(ge=1)]


def _require_unique_dependencies(depends_on: tuple[Identity, ...]) -> None:
    if len(set(depends_on)) != len(depends_on):
        raise ValueError("depends_on must contain unique identities")


class ResumeInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    brief_artifact_ref_id: Annotated[int, msgspec.Meta(ge=1)] | None = None


class RebindAttemptInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt: Identity
    branch: NonEmptyLine
    base_revision: NonEmptyLine
    brief_artifact_ref_id: Annotated[int, msgspec.Meta(ge=1)]


class StoredActivateInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt: Identity
    branch: NonEmptyLine
    base_revision: NonEmptyLine
    owner: NonEmptyLine
    brief_artifact_ref_id: Annotated[int, msgspec.Meta(ge=1)]


class SubmitReviewInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    candidate: NonEmptyLine


class ReasonInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    reason: NonEmptyLine


class BlockInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    reason: NonEmptyLine
    depends_on: tuple[Identity, ...] = ()

    def __post_init__(self) -> None:
        _require_unique_dependencies(self.depends_on)


class EvidenceInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    evidence: NonEmptyLine


class AcceptCheckpointInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    checkpoint: Identity
    candidate: NonEmptyLine
    evidence: NonEmptyLine


class AcceptReviewAndContinueInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    candidate: NonEmptyLine
    evidence: NonEmptyLine


class CloseInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    outcome: work_models.CloseOutcome
    reason: NonEmptyLine


class DeferInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    timing: work_models.Timing
    reopen_condition: NonEmptyLine


class AcceptProposalInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    item: Identity
    state: work_models.AcceptedProposalState
    next_action: NonEmptyLine
    timing: work_models.Timing | None = None
    depends_on: tuple[Identity, ...] = ()

    def __post_init__(self) -> None:
        _require_unique_dependencies(self.depends_on)
        if self.item in self.depends_on:
            raise ValueError("depends_on must not contain the accepted item")


class MergeProposalInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    target: Identity


class ReviseItemInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-item-revision/v1"]
    item_id: Identity
    expected_revision: PositiveInt
    expected_digest: Sha256
    source_task: NonEmptyLine
    reason: NonEmptyLine
    definition: WorkItemDefinitionPayload


type InputPayload = (
    ResumeInputPayload
    | RebindAttemptInputPayload
    | StoredActivateInputPayload
    | SubmitReviewInputPayload
    | ReasonInputPayload
    | BlockInputPayload
    | EvidenceInputPayload
    | AcceptCheckpointInputPayload
    | AcceptReviewAndContinueInputPayload
    | CloseInputPayload
    | DeferInputPayload
    | AcceptProposalInputPayload
    | MergeProposalInputPayload
    | ReviseItemInputPayload
)
type InputModel = type[InputPayload]


class CloseView(msgspec.Struct, frozen=True):
    item_id: str
    outcome: str
    reason: str
    revision: str


class ItemRevisionView(msgspec.Struct, frozen=True):
    item_id: str
    definition_revision: int
    definition_digest: str
    project_revision: str
