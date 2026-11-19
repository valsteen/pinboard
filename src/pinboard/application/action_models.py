"""Transport-neutral action payload and discovery contracts."""

from __future__ import annotations

from typing import (
    Annotated,
    Any,  # noqa: TID251 - generated JSON Schema is recursive boundary data
    Literal,
    assert_never,
)

import msgspec

from pinboard.application import decision_batches, pr_reviews, unblockers
from pinboard.application.amendment_supersessions import AmendmentSupersessionPayload
from pinboard.domain import decision_models, work_models
from pinboard.domain.history import DefinitionPayload, RecordText
from pinboard.domain.identifiers import WorkItemId

type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\n]+\z")]
type Identity = Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]
type Sha256 = Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{64}\z")]
type PositiveInt = Annotated[int, msgspec.Meta(ge=1)]
type IntegrationTarget = Annotated[
    str,
    msgspec.Meta(min_length=1, pattern=r"\A[^\r\n\x00" "\u2028\u2029" r"-][^\r\n\x00" "\u2028\u2029" r"]*\z"),
]
type FullGitCommit = Annotated[str, msgspec.Meta(pattern=r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\z")]
type JsonSchema = dict[str, Any]
type ActionAuthorization = Literal["observer", "project", "attempt", "preparation"]
# Excluded path characters; U+2028 and U+2029 stay literal because RE2 rejects \u escapes in advertised patterns.
_PATH_EXCLUDED = r"\\\x00-\x1f\x7f" "\u2028\u2029" r"*?\[\]"
_PATH_COMPONENT = rf"(?:[^./{_PATH_EXCLUDED}]|\.[^./{_PATH_EXCLUDED}]|\.\.[^/{_PATH_EXCLUDED}])[^/{_PATH_EXCLUDED}]*"
type CanonicalRepoRelativeFile = Annotated[str, msgspec.Meta(pattern=rf"\A{_PATH_COMPONENT}(?:/{_PATH_COMPONENT})*\z")]


def require_canonical_excluded_paths(paths: tuple[str, ...]) -> None:
    if paths != tuple(sorted(set(paths))):
        raise ValueError("excluded_untracked_paths must be unique and sorted")


class ResumeInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    brief_artifact_ref_id: Annotated[int, msgspec.Meta(ge=1)] | None = None
    supersessions: tuple[AmendmentSupersessionPayload, ...] = ()


class RebindAttemptInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    branch: NonEmptyLine
    base_revision: NonEmptyLine
    brief_artifact_ref_id: Annotated[int, msgspec.Meta(ge=1)]
    supersessions: tuple[AmendmentSupersessionPayload, ...] = ()


class ActivateInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    brief_artifact_ref_id: Annotated[int, msgspec.Meta(ge=1)]


class SubmitReviewInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    candidate: NonEmptyLine


class DeclaredSubmitReviewInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-candidate-declaration/v1"]
    candidate: NonEmptyLine
    excluded_untracked_paths: tuple[CanonicalRepoRelativeFile, ...]

    def __post_init__(self) -> None:
        require_canonical_excluded_paths(self.excluded_untracked_paths)


class ReasonInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Return-for-correction input; its reason is human-read record text."""

    reason: RecordText


class PlainPauseInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Plain pause input: the human-read reason and exactly one thing the paused work waits for."""

    reason: RecordText
    unblocker: unblockers.UnblockerPayload


class RejectProposalInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    reason: NonEmptyLine


class BlockInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Block and block-item input: exactly one thing the held work waits for.

    Items name accepted dependencies of the item's current definition; a human decision states its question; an
    outside condition states in one line what must become true, which Pinboard does not check.
    """

    unblocker: unblockers.UnblockerPayload


class EvidenceInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    evidence: NonEmptyLine


class CoveredCompletionPackageInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: PositiveInt
    package_sha256: Sha256
    disposition: Literal["reused", "revalidated"]
    evidence: NonEmptyLine


class LandingTargetDepthsPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    pre_landing: PositiveInt
    landed: Annotated[int, msgspec.Meta(ge=0)]


class LandedCompletionPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True, omit_defaults=True):
    target: IntegrationTarget
    pre_landing_revision: FullGitCommit
    landed_revision: FullGitCommit
    retain_local_resources: bool = False
    difference_review_ref_id: PositiveInt | msgspec.UnsetType = msgspec.UNSET
    target_depths: LandingTargetDepthsPayload | msgspec.UnsetType = msgspec.UNSET


class ReviewedCompletionReceiptPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Canonical v3 receipt input shared by every reviewed completion."""

    schema: Literal["pinboard-reviewed-completion/v3"]
    human_decision: RecordText
    candidate: NonEmptyLine
    evidence: NonEmptyLine
    reviewer_task_id: NonEmptyLine
    result_sha256: Sha256
    review_sha256: Sha256
    packages: tuple[CoveredCompletionPackageInputPayload, ...]

    def __post_init__(self) -> None:
        history_ids = tuple(row.history_id for row in self.packages)
        if history_ids != tuple(sorted(set(history_ids))):
            raise ValueError("packages must be unique and strictly ascending by history_id")


class CarryingObligationPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: Identity
    definition_revision: PositiveInt
    definition_digest: Sha256
    obligation_id: Identity


class ReviewedObligationPayload(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="reviewed", tag_field="kind"
):
    obligation_id: Identity


class DoneObligationPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="done", tag_field="kind"):
    obligation_id: Identity
    evidence: RecordText


class MovedObligationPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="moved", tag_field="kind"):
    obligation_id: Identity
    receiver: CarryingObligationPayload


type CompletionObligationPayload = ReviewedObligationPayload | DoneObligationPayload | MovedObligationPayload


class DoneHumanStepPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="done", tag_field="kind"):
    step_id: Identity
    evidence: RecordText


class MovedHumanStepPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="moved", tag_field="kind"):
    step_id: Identity
    receiver: CarryingObligationPayload


class FinalHumanStepsAccountingPayload(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="final-human-steps", tag_field="kind"
):
    brief_sha256: Sha256
    steps: tuple[DoneHumanStepPayload, ...]
    obligations: tuple[CompletionObligationPayload, ...]


class ContinuingWorkAccountingPayload(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="continue", tag_field="kind"
):
    brief_sha256: Sha256
    remaining_work: RecordText
    required_step_ids: Annotated[tuple[Identity, ...], msgspec.Meta(min_length=1)]
    steps: Annotated[tuple[DoneHumanStepPayload | MovedHumanStepPayload, ...], msgspec.Meta(min_length=1)]
    obligations: tuple[CompletionObligationPayload, ...]


type CompletionAccountingPayload = FinalHumanStepsAccountingPayload | ContinuingWorkAccountingPayload


class ReviewedCompleteInputPayload(ReviewedCompletionReceiptPayload, frozen=True, forbid_unknown_fields=True):
    """Current request with landing intent and additional accounting when the brief calls for it."""

    landed: LandedCompletionPayload | msgspec.UnsetType = msgspec.UNSET
    accounting: CompletionAccountingPayload | msgspec.UnsetType = msgspec.UNSET


def _carrying_obligation(value: CarryingObligationPayload) -> work_models.CarryingObligation:
    return work_models.CarryingObligation(
        WorkItemId(value.item_id),
        value.definition_revision,
        value.definition_digest,
        work_models.ObligationId(value.obligation_id),
    )


def _completion_obligation(value: CompletionObligationPayload) -> work_models.CompletionObligation:
    match value:
        case ReviewedObligationPayload(obligation_id=identity):
            return work_models.ReviewedObligation(work_models.ObligationId(identity))
        case DoneObligationPayload(obligation_id=identity, evidence=evidence):
            return work_models.DoneObligation(work_models.ObligationId(identity), evidence)
        case MovedObligationPayload(obligation_id=identity, receiver=receiver):
            return work_models.MovedObligation(work_models.ObligationId(identity), _carrying_obligation(receiver))
        case _ as unreachable:
            assert_never(unreachable)


def _done_human_step(value: DoneHumanStepPayload) -> work_models.DoneHumanStep:
    return work_models.DoneHumanStep(work_models.CompletionStepId(value.step_id), value.evidence)


def _completion_human_step(value: DoneHumanStepPayload | MovedHumanStepPayload) -> work_models.CompletionHumanStep:
    match value:
        case DoneHumanStepPayload():
            return _done_human_step(value)
        case MovedHumanStepPayload(step_id=identity, receiver=receiver):
            return work_models.MovedHumanStep(work_models.CompletionStepId(identity), _carrying_obligation(receiver))
        case _ as unreachable:
            assert_never(unreachable)


def completion_accounting(value: CompletionAccountingPayload) -> work_models.CompletionAccounting:
    obligations = tuple(_completion_obligation(row) for row in value.obligations)
    match value:
        case FinalHumanStepsAccountingPayload():
            return work_models.FinalHumanStepsAccounting(
                value.brief_sha256, tuple(_done_human_step(step) for step in value.steps), obligations
            )
        case ContinuingWorkAccountingPayload():
            return work_models.ContinuingWorkAccounting(
                value.brief_sha256,
                value.remaining_work,
                tuple(work_models.CompletionStepId(identity) for identity in value.required_step_ids),
                tuple(_completion_human_step(step) for step in value.steps),
                obligations,
            )
        case _ as unreachable:
            assert_never(unreachable)


def _carrying_payload(value: work_models.CarryingObligation) -> CarryingObligationPayload:
    return CarryingObligationPayload(
        str(value.item_id), value.definition_revision, value.definition_digest, str(value.obligation_id)
    )


def _completion_obligation_payload(value: work_models.CompletionObligation) -> CompletionObligationPayload:
    identity = str(value.obligation_id)
    match value:
        case work_models.ReviewedObligation():
            return ReviewedObligationPayload(identity)
        case work_models.DoneObligation(evidence=evidence):
            return DoneObligationPayload(identity, evidence)
        case work_models.MovedObligation(receiver=receiver):
            return MovedObligationPayload(identity, _carrying_payload(receiver))
        case _ as unreachable:
            assert_never(unreachable)


def _completion_step_payload(
    value: work_models.CompletionHumanStep,
) -> DoneHumanStepPayload | MovedHumanStepPayload:
    match value:
        case work_models.DoneHumanStep(step_id=identity, evidence=evidence):
            return DoneHumanStepPayload(str(identity), evidence)
        case work_models.MovedHumanStep(step_id=identity, receiver=receiver):
            return MovedHumanStepPayload(str(identity), _carrying_payload(receiver))
        case _ as unreachable:
            assert_never(unreachable)


def completion_accounting_payload(value: work_models.CompletionAccounting) -> CompletionAccountingPayload:
    obligations = tuple(_completion_obligation_payload(row) for row in value.obligations)
    match value:
        case work_models.FinalHumanStepsAccounting():
            return FinalHumanStepsAccountingPayload(
                value.brief_sha256,
                tuple(DoneHumanStepPayload(str(step.step_id), step.evidence) for step in value.steps),
                obligations,
            )
        case work_models.ContinuingWorkAccounting():
            return ContinuingWorkAccountingPayload(
                value.brief_sha256,
                value.remaining_work,
                tuple(str(identity) for identity in value.required_step_ids),
                tuple(_completion_step_payload(step) for step in value.steps),
                obligations,
            )
        case _ as unreachable:
            assert_never(unreachable)


class AcceptCheckpointInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    checkpoint: Identity
    candidate: NonEmptyLine
    evidence: RecordText


class AcceptReviewAndContinueInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    candidate: NonEmptyLine
    evidence: RecordText


class RecordAmendmentInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One human-approved incidental repair recorded as an attempt amendment.

    human_decision carries the human's exact authorizing words; it is attribution, not authentication of the human.
    """

    schema: Literal["pinboard-attempt-amendment/v1"]
    amendment_class: work_models.AmendmentClass
    change: RecordText
    reason: RecordText
    human_decision: RecordText


class RecordPlannedReplacementInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-planned-replacement/v1"]
    expected_relation_revision: Annotated[int, msgspec.Meta(ge=0)]
    replacement_item: Identity
    replacement_cost: NonEmptyLine
    status: work_models.PlannedReplacementStatus
    recorded_by: NonEmptyLine


class RetainTemporarilyInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-replacement-disposition/v1"]
    relation_revision: PositiveInt
    rationale: NonEmptyLine
    accepted_cost: NonEmptyLine
    recorded_by: NonEmptyLine


class CloseInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Current close input, stored as pinboard-close-decision/v1.

    Stored receipts are validated by close_compatibility.StoredCloseDecisionInput, which keeps released outcomes.

    human_decision carries the human's explicit close decision in their own words; it is attribution, not
    authentication of the human.
    """

    outcome: work_models.DirectCloseOutcome
    reason: NonEmptyLine
    human_decision: NonEmptyLine


class MergeProposalInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    target: Identity


class ReviseItemInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-item-revision/v1"]
    expected_revision: PositiveInt
    expected_digest: Sha256
    source_task: NonEmptyLine
    reason: RecordText
    definition: DefinitionPayload


type InputPayload = (
    pr_reviews.ReviewBrief
    | pr_reviews.BriefReview
    | pr_reviews.HeadObservation
    | pr_reviews.ReviewRound
    | pr_reviews.ReviewClose
    | ResumeInputPayload
    | decision_batches.PauseDecisionsInput
    | decision_batches.ResolveDecisionsInput
    | RebindAttemptInputPayload
    | ActivateInputPayload
    | SubmitReviewInputPayload
    | DeclaredSubmitReviewInputPayload
    | ReasonInputPayload
    | PlainPauseInputPayload
    | RejectProposalInputPayload
    | BlockInputPayload
    | EvidenceInputPayload
    | AcceptCheckpointInputPayload
    | AcceptReviewAndContinueInputPayload
    | RecordAmendmentInputPayload
    | RecordPlannedReplacementInputPayload
    | RetainTemporarilyInputPayload
    | CloseInputPayload
    | MergeProposalInputPayload
    | ReviseItemInputPayload
    | ReviewedCompleteInputPayload
)
type InputModel = type[InputPayload]


def action_input_model(kind: decision_models.ActionKind) -> InputModel | None:  # noqa: C901, PLR0912
    """Return the exact shared payload record selected by one action kind."""

    match kind:
        case decision_models.ActionKind.START_PR_REVIEW:
            return pr_reviews.ReviewBrief
        case decision_models.ActionKind.REVIEW_PR_BRIEF:
            return pr_reviews.BriefReview
        case decision_models.ActionKind.OBSERVE_PR_HEAD:
            return pr_reviews.HeadObservation
        case decision_models.ActionKind.RECORD_PR_ROUND:
            return pr_reviews.ReviewRound
        case decision_models.ActionKind.CLOSE_PR_REVIEW:
            return pr_reviews.ReviewClose
        case decision_models.ActionKind.ACCEPT_CHECKPOINT:
            return AcceptCheckpointInputPayload
        case decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE:
            return AcceptReviewAndContinueInputPayload
        case decision_models.ActionKind.ACTIVATE:
            return ActivateInputPayload
        case decision_models.ActionKind.BLOCK | decision_models.ActionKind.BLOCK_ITEM:
            return BlockInputPayload
        case decision_models.ActionKind.CLOSE:
            return CloseInputPayload
        case decision_models.ActionKind.RECORD_AMENDMENT:
            return RecordAmendmentInputPayload
        case decision_models.ActionKind.RECORD_REPLACEMENT:
            return RecordPlannedReplacementInputPayload
        case decision_models.ActionKind.RETAIN_TEMPORARILY:
            return RetainTemporarilyInputPayload
        case decision_models.ActionKind.PAUSE:
            return PlainPauseInputPayload
        case decision_models.ActionKind.RETURN_FOR_CORRECTION:
            return ReasonInputPayload
        case decision_models.ActionKind.REJECT_PROPOSAL:
            return RejectProposalInputPayload
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
            decision_models.ActionKind.CONSOLIDATE
            | decision_models.ActionKind.CANCEL
            | decision_models.ActionKind.COMPLETE
            | decision_models.ActionKind.CONTINUE
            | decision_models.ActionKind.DISPATCH
            | decision_models.ActionKind.INSPECT
            | decision_models.ActionKind.PREPARE
            | decision_models.ActionKind.CREATE_PROPOSAL
            | decision_models.ActionKind.PREVIEW_PARALLEL
            | decision_models.ActionKind.ORDER
            | decision_models.ActionKind.INSPECT_INITIATIVE
            | decision_models.ActionKind.REVIEW_CANDIDATE
            | decision_models.ActionKind.REPORT_BLOCKER
        ):
            return None
        case _ as unreachable:
            assert_never(unreachable)


def action_payload_schema(kind: decision_models.ActionKind) -> JsonSchema | None:
    """Return the canonical strict payload schema for one action kind."""

    if kind in {decision_models.ActionKind.PAUSE, decision_models.ActionKind.RESUME}:
        plain = msgspec.json.schema(
            PlainPauseInputPayload if kind == decision_models.ActionKind.PAUSE else ResumeInputPayload
        )
        batch = msgspec.json.schema(
            decision_batches.PauseDecisionsInput
            if kind == decision_models.ActionKind.PAUSE
            else decision_batches.ResolveDecisionsInput
        )
        return {
            "oneOf": [{"$ref": plain["$ref"]}, {"$ref": batch["$ref"]}],
            "$defs": {**plain["$defs"], **batch["$defs"]},
        }
    if kind == decision_models.ActionKind.SUBMIT_REVIEW:
        legacy = msgspec.json.schema(SubmitReviewInputPayload)
        declared = msgspec.json.schema(DeclaredSubmitReviewInputPayload)
        return {
            "oneOf": [{"$ref": legacy["$ref"]}, {"$ref": declared["$ref"]}],
            "$defs": {**legacy["$defs"], **declared["$defs"]},
        }
    if kind == decision_models.ActionKind.COMPLETE:
        direct = msgspec.json.schema(EvidenceInputPayload)
        reviewed = msgspec.json.schema(ReviewedCompleteInputPayload)
        return {
            "oneOf": [{"$ref": direct["$ref"]}, {"$ref": reviewed["$ref"]}],
            "$defs": {**direct["$defs"], **reviewed["$defs"]},
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
    payload_schema: JsonSchema | msgspec.UnsetType

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
        expected_schema = action_payload_schema(self.action_kind)
        if expected_schema is None:
            if self.payload_schema is not msgspec.UNSET:
                raise ValueError("actions without a lifecycle payload must omit its schema")
        elif self.payload_schema is msgspec.UNSET or msgspec.json.encode(
            self.payload_schema, order="sorted"
        ) != msgspec.json.encode(expected_schema, order="sorted"):
            raise ValueError("payload schema must match its action kind")


class CompletionPackageView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: PositiveInt
    package_sha256: Sha256
    artifact_ref_id: PositiveInt
    selector: NonEmptyLine
    size_bytes: PositiveInt


class CompletionInputContractView(InputContractView, frozen=True, forbid_unknown_fields=True):
    payload_schema: JsonSchema
    candidate: str | None
    checkpoint_packages: tuple[CompletionPackageView, ...]
    accepted_brief_sha256: Sha256
    obligation_ids: tuple[Identity, ...]
    final_step_ids: tuple[Identity, ...]
    remaining_work: str | None
    continued_review: CompletionReviewView | None

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
        expected_model = ReviewedCompleteInputPayload
        if msgspec.json.encode(self.payload_schema, order="sorted") != msgspec.json.encode(
            msgspec.json.schema(expected_model), order="sorted"
        ):
            raise ValueError("completion payload schema must be one exact complete-action leaf")


class CompletionReviewView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    artifact_ref_id: PositiveInt
    selector: NonEmptyLine
    sha256: Sha256
    size_bytes: PositiveInt
    acceptance_history_id: PositiveInt
    reviewer_task_id: NonEmptyLine
    result_sha256: Sha256
    review_sha256: Sha256


def _validate_action_view(
    kind: decision_models.ActionKind,
    subject_revision: str | None,
    authorization: str,
    lease_id: str | None,
    generation: int | None,
    semantics: ActionSemanticsView,
    input_contract: InputContractView | None,
) -> None:
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
        if (
            lease_id is not None
            or generation is not None
            or (
                subject_revision is None
                and expected.lifecycle_effect != decision_models.LifecycleEffect.NO_LIFECYCLE_CHANGE
            )
        ):
            raise ValueError("project mutations require a subject revision; project actions carry no lease")
    elif subject_revision is None or lease_id is None or generation is None:
        raise ValueError("leased actions require subject revision, lease id, and generation")
    if input_contract is not None and (input_contract.action_kind != kind or input_contract.semantics != semantics):
        raise ValueError("input contract must match its action kind and semantics")


class ActionSummaryView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
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
            self.kind,
            self.subject_revision,
            self.authorization,
            self.lease_id,
            self.generation,
            self.semantics,
            self.input_contract,
        )


class ActionView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
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
            self.kind,
            self.subject_revision,
            self.authorization,
            self.lease_id,
            self.generation,
            self.semantics,
            self.input_contract,
        )


type ProjectedActionView = ActionSummaryView | ActionView
