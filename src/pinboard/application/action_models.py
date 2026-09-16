"""Transport-neutral action payload and discovery contracts."""

from typing import (
    Annotated,
    Any,  # noqa: TID251 - generated JSON Schema is recursive boundary data
    Literal,
    assert_never,
)

import msgspec

from pinboard.domain import decision_models, work_models
from pinboard.domain.history import WorkItemDefinitionPayload

type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\n]+\z")]
type Identity = Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]
type Sha256 = Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{64}\z")]
type PositiveInt = Annotated[int, msgspec.Meta(ge=1)]
type JsonSchema = dict[str, Any]
type ActionAuthorization = Literal["observer", "project", "attempt", "preparation"]


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


def action_input_model(kind: decision_models.ActionKind) -> InputModel | None:  # noqa: C901, PLR0912
    """Return the exact shared payload record selected by one action kind."""

    match kind:
        case decision_models.ActionKind.ACCEPT_CHECKPOINT:
            return AcceptCheckpointInputPayload
        case decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE:
            return AcceptReviewAndContinueInputPayload
        case decision_models.ActionKind.ACCEPT_PROPOSAL:
            return AcceptProposalInputPayload
        case decision_models.ActionKind.ACTIVATE:
            return ActivateInputPayload
        case decision_models.ActionKind.BLOCK | decision_models.ActionKind.BLOCK_ITEM:
            return BlockInputPayload
        case decision_models.ActionKind.CLOSE:
            return CloseInputPayload
        case decision_models.ActionKind.COMPLETE:
            return None
        case decision_models.ActionKind.REOPEN:
            return EvidenceInputPayload
        case decision_models.ActionKind.RECORD_REPLACEMENT:
            return RecordPlannedReplacementInputPayload
        case decision_models.ActionKind.RETAIN_TEMPORARILY:
            return RetainTemporarilyInputPayload
        case decision_models.ActionKind.DEFER:
            return DeferInputPayload
        case (
            decision_models.ActionKind.MARK_READY
            | decision_models.ActionKind.PAUSE
            | decision_models.ActionKind.REJECT_PROPOSAL
            | decision_models.ActionKind.RETURN_FOR_CORRECTION
            | decision_models.ActionKind.RETURN_PROPOSAL
        ):
            return ReasonInputPayload
        case decision_models.ActionKind.MERGE_PROPOSAL:
            return MergeProposalInputPayload
        case decision_models.ActionKind.RESUME:
            return ResumeInputPayload
        case decision_models.ActionKind.REBIND_ATTEMPT:
            return RebindAttemptInputPayload
        case decision_models.ActionKind.REVISE_ITEM:
            return ReviseItemInputPayload
        case decision_models.ActionKind.SUBMIT_REVIEW:
            return SubmitReviewInputPayload
        case (
            decision_models.ActionKind.CONTINUE
            | decision_models.ActionKind.DISPATCH
            | decision_models.ActionKind.INSPECT
            | decision_models.ActionKind.REPORT_BLOCKER
        ):
            return None
        case _ as unreachable:
            assert_never(unreachable)


def action_payload_schema(kind: decision_models.ActionKind) -> JsonSchema | None:
    """Return the canonical strict payload schema for one action kind."""

    if kind == decision_models.ActionKind.COMPLETE:
        direct = msgspec.json.schema(EvidenceInputPayload)
        covered = msgspec.json.schema(CoveredCompleteInputPayload)
        return {
            "oneOf": [{"$ref": direct["$ref"]}, {"$ref": covered["$ref"]}],
            "$defs": {**direct["$defs"], **covered["$defs"]},
        }
    model = action_input_model(kind)
    return None if model is None else msgspec.json.schema(model)


class ActionSemanticsView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    use_case: str
    effect: decision_models.LifecycleEffect
    permitted_roles: tuple[decision_models.Role, ...]
    subject_kind: decision_models.ActionSubjectKind
    lifecycle_precondition: decision_models.ActionLifecyclePrecondition
    practical_result: str


class InputContractView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_kind: decision_models.ActionKind
    semantics: ActionSemanticsView
    payload_schema: JsonSchema | None

    def __post_init__(self) -> None:
        expected = decision_models.action_semantics(self.action_kind)
        if self.semantics != ActionSemanticsView(
            expected.use_case,
            expected.lifecycle_effect,
            expected.permitted_roles,
            expected.subject_kind,
            expected.lifecycle_precondition,
            expected.practical_result,
        ):
            raise ValueError("input contract semantics must match its action kind")
        if msgspec.json.encode(self.payload_schema, order="sorted") != msgspec.json.encode(
            action_payload_schema(self.action_kind), order="sorted"
        ):
            raise ValueError("payload schema must match its action kind")


class CompletionPackageView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: PositiveInt
    package_sha256: Sha256
    artifact_ref_id: PositiveInt
    selector: NonEmptyLine
    size_bytes: PositiveInt


class CompletionInputContractView(InputContractView, frozen=True, forbid_unknown_fields=True):
    candidate: str | None
    checkpoint_packages: tuple[CompletionPackageView, ...]

    def __post_init__(self) -> None:
        if self.action_kind != decision_models.ActionKind.COMPLETE:
            raise ValueError("completion input contracts require the complete action kind")
        expected = decision_models.action_semantics(self.action_kind)
        if self.semantics != ActionSemanticsView(
            expected.use_case,
            expected.lifecycle_effect,
            expected.permitted_roles,
            expected.subject_kind,
            expected.lifecycle_precondition,
            expected.practical_result,
        ):
            raise ValueError("completion input contract semantics must match complete")
        expected_model = CoveredCompleteInputPayload if self.checkpoint_packages else EvidenceInputPayload
        if msgspec.json.encode(self.payload_schema, order="sorted") != msgspec.json.encode(
            msgspec.json.schema(expected_model), order="sorted"
        ):
            raise ValueError("completion payload schema must be one exact complete-action leaf")


def _validate_action_view(
    action_id: str,
    kind: decision_models.ActionKind,
    subject: str,
    subject_revision: str | None,
    authorization: str,
    lease_id: str | None,
    generation: int | None,
    semantics: ActionSemanticsView,
    input_contract: InputContractView | None,
) -> None:
    if action_id != f"{kind.value}:{subject}":
        raise ValueError("action_id must match the action kind and subject")
    expected = decision_models.action_semantics(kind)
    if semantics != ActionSemanticsView(
        expected.use_case,
        expected.lifecycle_effect,
        expected.permitted_roles,
        expected.subject_kind,
        expected.lifecycle_precondition,
        expected.practical_result,
    ):
        raise ValueError("action semantics must match the selected action kind")
    authority_role = {
        "observer": decision_models.Role.OBSERVER,
        "project": decision_models.Role.PROJECT,
        "attempt": decision_models.Role.WORKER,
        "preparation": decision_models.Role.PREPARER,
    }[authorization]
    if authority_role not in expected.permitted_roles:
        raise ValueError("action authority must match a permitted role")
    if authorization == "observer":
        if (subject_revision, lease_id, generation) != (None, None, None):
            raise ValueError("observer actions cannot carry mutation or lease authority")
    elif authorization == "project":
        if subject_revision is None or lease_id is not None or generation is not None:
            raise ValueError("project actions require a subject revision and no lease")
    elif subject_revision is None or lease_id is None or generation is None:
        raise ValueError("leased actions require subject revision, lease id, and generation")
    if input_contract is not None and (input_contract.action_kind != kind or input_contract.semantics != semantics):
        raise ValueError("input contract must match its action kind and semantics")


class ActionSummaryView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: NonEmptyLine
    kind: decision_models.ActionKind
    subject: NonEmptyLine
    label: NonEmptyLine
    subject_revision: NonEmptyLine | None
    authorization: ActionAuthorization
    lease_id: NonEmptyLine | None
    generation: PositiveInt | None
    semantics: ActionSemanticsView
    input_contract: None

    def __post_init__(self) -> None:
        _validate_action_view(
            self.action_id,
            self.kind,
            self.subject,
            self.subject_revision,
            self.authorization,
            self.lease_id,
            self.generation,
            self.semantics,
            self.input_contract,
        )


class ActionView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: NonEmptyLine
    kind: decision_models.ActionKind
    subject: NonEmptyLine
    label: NonEmptyLine
    subject_revision: NonEmptyLine | None
    authorization: ActionAuthorization
    lease_id: NonEmptyLine | None
    generation: PositiveInt | None
    semantics: ActionSemanticsView
    input_contract: InputContractView

    def __post_init__(self) -> None:
        _validate_action_view(
            self.action_id,
            self.kind,
            self.subject,
            self.subject_revision,
            self.authorization,
            self.lease_id,
            self.generation,
            self.semantics,
            self.input_contract,
        )


type ProjectedActionView = ActionSummaryView | ActionView
