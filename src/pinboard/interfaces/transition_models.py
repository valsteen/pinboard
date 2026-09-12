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


class ActivateInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
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


class CoveredCompletionPackageInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: PositiveInt
    package_sha256: Sha256
    disposition: Literal["reused", "revalidated"]
    evidence: NonEmptyLine


class CoveredCompleteInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-covered-completion/v1"]
    candidate: NonEmptyLine
    evidence: NonEmptyLine
    reviewer_task_id: NonEmptyLine
    result_sha256: Sha256
    review_sha256: Sha256
    packages: Annotated[tuple[CoveredCompletionPackageInputPayload, ...], msgspec.Meta(min_length=1)]

    def __post_init__(self) -> None:
        history_ids = tuple(row.history_id for row in self.packages)
        if history_ids != tuple(sorted(set(history_ids))):
            raise ValueError("packages must be unique and strictly ascending by history_id")


type CompletionInputPayload = EvidenceInputPayload | CoveredCompleteInputPayload


class AcceptCheckpointInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    checkpoint: Identity
    candidate: NonEmptyLine
    evidence: NonEmptyLine


class AcceptReviewAndContinueInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    candidate: NonEmptyLine
    evidence: NonEmptyLine


class RecordPlannedReplacementInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-planned-replacement/v1"]
    affected_item: Identity
    expected_relation_revision: Annotated[int, msgspec.Meta(ge=0)]
    replacement_item: Identity
    replacement_cost: NonEmptyLine
    status: work_models.PlannedReplacementStatus
    recorded_by: NonEmptyLine

    def __post_init__(self) -> None:
        if self.affected_item == self.replacement_item:
            raise ValueError("replacement_item must differ from affected_item")


class RetainTemporarilyInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-replacement-disposition/v1"]
    affected_item: Identity
    relation_revision: PositiveInt
    rationale: NonEmptyLine
    accepted_cost: NonEmptyLine
    recorded_by: NonEmptyLine


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
    | ActivateInputPayload
    | SubmitReviewInputPayload
    | ReasonInputPayload
    | BlockInputPayload
    | EvidenceInputPayload
    | AcceptCheckpointInputPayload
    | AcceptReviewAndContinueInputPayload
    | RecordPlannedReplacementInputPayload
    | RetainTemporarilyInputPayload
    | CloseInputPayload
    | DeferInputPayload
    | AcceptProposalInputPayload
    | MergeProposalInputPayload
    | ReviseItemInputPayload
    | CoveredCompleteInputPayload
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
