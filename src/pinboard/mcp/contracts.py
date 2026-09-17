"""Strict request and correlated result contracts for the MCP transport."""

from typing import Annotated, Any, Literal, assert_never  # noqa: TID251 - validated against the selected action leaf

import msgspec

from pinboard.adapters import dispatch_operations, review_operations
from pinboard.application import (
    action_models,
    brief_source_models,
    dispatch_models,
    proposal_models,
    query_models,
    work_brief_contract,
    work_brief_models,
)
from pinboard.domain import authority_models, decision_models, ordering
from pinboard.domain.errors import DecisionFailureCode, RetryDisposition

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonSchemaValue = JsonScalar | list[JsonSchemaValue] | dict[str, JsonSchemaValue]
type NonEmptyText = Annotated[str, msgspec.Meta(min_length=1)]
type RootPath = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\x00]+\z")]
type PositiveInt = Annotated[int, msgspec.Meta(ge=1)]
type NonNegativeInt = Annotated[int, msgspec.Meta(ge=0)]
type Sha256 = Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{64}\z")]
type PathComponent = Annotated[
    str,
    msgspec.Meta(min_length=1, pattern=r"\A(?!\.{1,2}\z)[^/\r\n\x00]+\z"),
]
type RuntimeIdentity = Annotated[
    str,
    msgspec.Meta(
        min_length=1,
        pattern=r"\A(?!\s)(?!\.{1,2}\z)[^/\r\n\x00]*[^\s/\r\n\x00]\z",
    ),
]
type Empty = tuple[()]
type LedgerSurface = tuple[Literal["ledger"]]
type ArtifactSurface = tuple[Literal["immutable-artifact"]]
type ReferenceSurfaces = tuple[Literal["accepted-artifact-reference"], Literal["ledger"]]
type PublicationSurfaces = tuple[
    Literal["immutable-artifact"],
    Literal["accepted-artifact-reference"],
    Literal["ledger"],
]
type JobPublicationSurface = Literal["immutable-artifact", "accepted-artifact-reference", "ledger"]


def _require_state_changed(actual: bool, expected: bool) -> None:
    if actual is not expected:
        raise ValueError(f"state_changed must be {str(expected).lower()} for this result.")


def _require_publication_surfaces(surfaces: tuple[JobPublicationSurface, ...]) -> None:
    if surfaces not in (
        ("immutable-artifact",),
        ("accepted-artifact-reference", "ledger"),
        ("immutable-artifact", "accepted-artifact-reference", "ledger"),
    ):
        raise ValueError("Publication must retain exact terminal publication surfaces.")


class ItemStatusRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    item_id: PathComponent


class ProposalCreateRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    proposal: proposal_models.Proposal
    actor_task_id: RuntimeIdentity
    actor_host_id: RuntimeIdentity


class BriefPublishRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    brief: work_brief_models.WorkBrief


class OverviewRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath


class OrderRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    order: ordering.OrderRequest
    actor_task_id: RuntimeIdentity
    actor_host_id: RuntimeIdentity


class OrderEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: OrderRequest


class SelectedParallelPreviewRequest(
    msgspec.Struct, tag="selected", tag_field="selection", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    item_ids: Annotated[tuple[ordering.OrderItemId, ...], msgspec.Meta(min_length=1)]

    def __post_init__(self) -> None:
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("Selected item identities must be unique.")


class AllSafeParallelPreviewRequest(
    msgspec.Struct, tag="all-safe", tag_field="selection", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath


class ParallelPreviewEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: SelectedParallelPreviewRequest | AllSafeParallelPreviewRequest


class BriefContractFullRequest(
    msgspec.Struct, tag="full", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath


class BriefContractStarterRequest(
    msgspec.Struct, tag="starter", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    boundary: Literal["local", "cross-boundary"]


class BriefContractEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: BriefContractFullRequest | BriefContractStarterRequest


class BriefSourcesPlanRequest(
    msgspec.Struct, tag="plan", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    manifest: brief_source_models.BriefSourceManifest
    max_batch_bytes: PositiveInt


class BriefSourcesPlanToFileRequest(
    msgspec.Struct, tag="plan-to-file", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    manifest: brief_source_models.BriefSourceManifest
    max_batch_bytes: PositiveInt
    destination: RootPath


class BriefSourcesEmitRequest(
    msgspec.Struct, tag="emit", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    plan: brief_source_models.BriefSourcePlanView
    batch_index: NonNegativeInt


class BriefSourcesEmitFileRequest(
    msgspec.Struct, tag="emit-file", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    plan_path: RootPath
    batch_index: NonNegativeInt


class BriefSourcesEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: (
        BriefSourcesPlanRequest | BriefSourcesPlanToFileRequest | BriefSourcesEmitRequest | BriefSourcesEmitFileRequest
    )


class BriefContractRejected(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-contract-result/v1"]
    status: Literal["rejected"]
    code: Literal["BRIEF_CONTRACT_REQUEST_INVALID"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class BriefSourcesRejected(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-sources-result/v1"]
    status: Literal["rejected"]
    code: Literal[
        "BRIEF_SOURCES_REQUEST_INVALID",
        "BRIEF_SOURCE_BATCH_NOT_FOUND",
        "BRIEF_SOURCE_LINE_TOO_LARGE",
        "BRIEF_SOURCE_MANIFEST_INVALID",
        "BRIEF_SOURCE_PLAN_INVALID",
        "BRIEF_SOURCE_SELECTOR_INVALID",
        "BRIEF_SOURCE_SELECTOR_OVERLAP",
        "BRIEF_SOURCE_NOT_UTF8",
        "BRIEF_SOURCE_CHANGED",
        "BRIEF_SOURCE_UNREADABLE",
        "DIRECTORY_CREATE_FAILED",
        "DIRECTORY_INVALID",
        "DIRECTORY_SYNC_FAILED",
        "DIRECTORY_VERIFY_FAILED",
        "FILE_ALREADY_EXISTS",
        "FILE_PUBLISH_FAILED",
        "PROJECT_GIT_CHECKOUT_UNAVAILABLE",
        "PROJECT_GIT_EXCLUDE_UNAVAILABLE",
        "PROJECT_GIT_LAYOUT_UNSUPPORTED",
        "PROJECT_GIT_ROOT_UNAVAILABLE",
    ]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class BriefSourceBatchResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-brief-source-batch/v1"]
    batch_index: NonNegativeInt
    content_byte_count: NonNegativeInt
    rendered_byte_count: PositiveInt
    text: NonEmptyText

    def __post_init__(self) -> None:
        if len(self.text.encode("utf-8")) != self.rendered_byte_count:
            raise ValueError("rendered_byte_count must equal the UTF-8 batch text size")


class BriefSourcePlanOutputResult(brief_source_models.BriefSourcePlanOutputReceipt, frozen=True):
    state_changed: bool
    effect: Literal["committed", "unchanged"]
    retry: Literal["do-not-retry", "safe-to-repeat"]
    changed_surfaces: tuple[Literal["selected-output"], ...]

    def __post_init__(self) -> None:
        expected = (
            (True, "committed", "do-not-retry", ("selected-output",))
            if self.created
            else (False, "unchanged", "safe-to-repeat", ())
        )
        if (self.state_changed, self.effect, self.retry, self.changed_surfaces) != expected:
            raise ValueError("plan output aftermath must agree with created disposition")


class BriefSourcesPublishedFailure(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-sources-result/v1"]
    status: Literal["committed-effect"]
    code: Literal["DIRECTORY_SYNC_FAILED"]
    message: NonEmptyText
    destination: RootPath
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: tuple[Literal["selected-output"]]

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class ItemDefinitionCurrentRequest(
    msgspec.Struct, tag="current", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    item_id: PathComponent


class ItemDefinitionHistoryRequest(
    msgspec.Struct, tag="history", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    item_id: PathComponent
    limit: Annotated[int, msgspec.Meta(ge=1, le=100)]
    before_revision: PositiveInt | None


class ItemDefinitionEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: ItemDefinitionCurrentRequest | ItemDefinitionHistoryRequest


class BriefReviewPublishRequest(
    msgspec.Struct, tag="publish", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    brief_artifact_ref_id: PositiveInt
    review: work_brief_models.WorkBriefReviewNeedsCorrection


class BriefReviewStatusRequest(
    msgspec.Struct, tag="status", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    brief_artifact_ref_id: PositiveInt


class BriefReviewEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: BriefReviewPublishRequest | BriefReviewStatusRequest


class ActionIdentity(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    kind: decision_models.ActionKind
    subject: PathComponent


class ObserverActionsRequest(msgspec.Struct, tag="observer", tag_field="role", frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    action_id: ActionIdentity | None = None


class ProjectActionsRequest(msgspec.Struct, tag="project", tag_field="role", frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    action_id: ActionIdentity | None = None


class WorkerActionsRequest(msgspec.Struct, tag="worker", tag_field="role", frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    lease_id: RuntimeIdentity
    generation: PositiveInt
    action_id: ActionIdentity | None = None


class PreparerActionsRequest(msgspec.Struct, tag="preparer", tag_field="role", frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    lease_id: RuntimeIdentity
    generation: PositiveInt
    action_id: ActionIdentity | None = None


type ActionsRequest = ObserverActionsRequest | ProjectActionsRequest | WorkerActionsRequest | PreparerActionsRequest


class ActionsEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: ActionsRequest


class AttemptInspectRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    attempt_id: PathComponent


class ArtifactVerifyRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    artifact_ref_id: PositiveInt
    selector: NonEmptyText
    sha256: Sha256
    size_bytes: PositiveInt


class TransitionActionIdentity[KindT](msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    kind: KindT
    subject: PathComponent


class DispatchReceipt(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: TransitionActionIdentity[Literal["dispatch"]]
    subject_revision: NonEmptyText


class DispatchChoiceBase(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    receipt: DispatchReceipt
    checkpoint_id: PathComponent
    environment: dispatch_models.DispatchEnvironment
    prompt: str | None


class OrdinaryDispatchChoice(DispatchChoiceBase, tag="ordinary", tag_field="kind", frozen=True):
    pass


class ReviewedDispatchChoice(DispatchChoiceBase, tag="reviewed", tag_field="kind", frozen=True):
    brief_review: work_brief_models.WorkBriefReview
    review_id: PathComponent


class CorrectionDispatchChoice(DispatchChoiceBase, tag="correction", tag_field="kind", frozen=True):
    brief_review: work_brief_models.CorrectionSourceReview
    review_id: PathComponent
    correction_history_id: PositiveInt


type DispatchChoice = OrdinaryDispatchChoice | ReviewedDispatchChoice | CorrectionDispatchChoice


class DispatchRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    dispatch: DispatchChoice


class ReviewChoiceBase(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: PathComponent
    candidate_revision: NonEmptyText


class InitialReviewChoice(ReviewChoiceBase, tag="initial", tag_field="kind", frozen=True):
    pass


class PackageInitialReviewChoice(ReviewChoiceBase, tag="package-initial", tag_field="kind", frozen=True):
    checkpoint_history_id: PositiveInt


class CorrectionReviewChoice(ReviewChoiceBase, tag="correction", tag_field="kind", frozen=True):
    correction_history_id: PositiveInt


class PackageCorrectionReviewChoice(ReviewChoiceBase, tag="package-correction", tag_field="kind", frozen=True):
    checkpoint_history_id: PositiveInt
    correction_history_id: PositiveInt


type ReviewChoice = (
    InitialReviewChoice | PackageInitialReviewChoice | CorrectionReviewChoice | PackageCorrectionReviewChoice
)


class ReviewJobRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    review: ReviewChoice


class TransitionReceipt[KindT](msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: TransitionActionIdentity[KindT]
    subject_revision: NonEmptyText


class ProjectTransitionRequest[KindT, PayloadT](
    msgspec.Struct, tag="project", tag_field="role", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    receipt: TransitionReceipt[KindT]
    payload: PayloadT
    actor_task_id: RuntimeIdentity
    actor_host_id: RuntimeIdentity


class WorkerTransitionRequest[KindT, PayloadT](
    msgspec.Struct, tag="worker", tag_field="role", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    receipt: TransitionReceipt[KindT]
    payload: PayloadT
    lease_id: RuntimeIdentity
    generation: PositiveInt


class PreparerTransitionRequest[KindT, PayloadT](
    msgspec.Struct, tag="preparer", tag_field="role", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    receipt: TransitionReceipt[KindT]
    payload: PayloadT
    lease_id: RuntimeIdentity
    generation: PositiveInt


type AcceptCheckpointTransitionRequest = ProjectTransitionRequest[
    Literal["accept-checkpoint"], action_models.AcceptCheckpointInputPayload
]
type AcceptReviewAndContinueTransitionRequest = ProjectTransitionRequest[
    Literal["accept-review-and-continue"], action_models.AcceptReviewAndContinueInputPayload
]
type AcceptProposalTransitionRequest = ProjectTransitionRequest[
    Literal["accept-proposal"], action_models.AcceptProposalInputPayload
]
type ActivateTransitionRequest = PreparerTransitionRequest[Literal["activate"], action_models.ActivateInputPayload]
type BlockTransitionRequest = ProjectTransitionRequest[Literal["block"], action_models.BlockInputPayload]
type BlockItemTransitionRequest = ProjectTransitionRequest[Literal["block-item"], action_models.BlockInputPayload]
type DirectCompleteTransitionRequest = ProjectTransitionRequest[Literal["complete"], action_models.EvidenceInputPayload]
type CoveredCompleteTransitionRequest = ProjectTransitionRequest[
    Literal["complete"], action_models.CoveredCompleteInputPayload
]
type CloseTransitionRequest = ProjectTransitionRequest[Literal["close"], action_models.CloseInputPayload]
type DeferTransitionRequest = ProjectTransitionRequest[Literal["defer"], action_models.DeferInputPayload]
type MarkReadyTransitionRequest = ProjectTransitionRequest[Literal["mark-ready"], action_models.ReasonInputPayload]
type MergeProposalTransitionRequest = ProjectTransitionRequest[
    Literal["merge-proposal"], action_models.MergeProposalInputPayload
]
type PauseTransitionRequest = ProjectTransitionRequest[Literal["pause"], action_models.ReasonInputPayload]
type RejectProposalTransitionRequest = ProjectTransitionRequest[
    Literal["reject-proposal"], action_models.ReasonInputPayload
]
type ReopenTransitionRequest = ProjectTransitionRequest[Literal["reopen"], action_models.EvidenceInputPayload]
type RecordReplacementTransitionRequest = ProjectTransitionRequest[
    Literal["record-replacement"], action_models.RecordPlannedReplacementInputPayload
]
type RebindAttemptTransitionRequest = ProjectTransitionRequest[
    Literal["rebind-attempt"], action_models.RebindAttemptInputPayload
]
type ResumeTransitionRequest = ProjectTransitionRequest[Literal["resume"], action_models.ResumeInputPayload]
type ReturnForCorrectionTransitionRequest = ProjectTransitionRequest[
    Literal["return-for-correction"], action_models.ReasonInputPayload
]
type ReturnProposalTransitionRequest = ProjectTransitionRequest[
    Literal["return-proposal"], action_models.ReasonInputPayload
]
type RetainTemporarilyTransitionRequest = ProjectTransitionRequest[
    Literal["retain-temporarily"], action_models.RetainTemporarilyInputPayload
]
type ReviseItemTransitionRequest = ProjectTransitionRequest[
    Literal["revise-item"], action_models.ReviseItemInputPayload
]
type SubmitReviewTransitionRequest = WorkerTransitionRequest[
    Literal["submit-review"], action_models.SubmitReviewInputPayload
]

type TransitionRequest = (
    AcceptCheckpointTransitionRequest
    | AcceptReviewAndContinueTransitionRequest
    | AcceptProposalTransitionRequest
    | ActivateTransitionRequest
    | BlockTransitionRequest
    | BlockItemTransitionRequest
    | DirectCompleteTransitionRequest
    | CoveredCompleteTransitionRequest
    | CloseTransitionRequest
    | DeferTransitionRequest
    | MarkReadyTransitionRequest
    | MergeProposalTransitionRequest
    | PauseTransitionRequest
    | RejectProposalTransitionRequest
    | ReopenTransitionRequest
    | RecordReplacementTransitionRequest
    | RebindAttemptTransitionRequest
    | ResumeTransitionRequest
    | ReturnForCorrectionTransitionRequest
    | ReturnProposalTransitionRequest
    | RetainTemporarilyTransitionRequest
    | ReviseItemTransitionRequest
    | SubmitReviewTransitionRequest
)


class TransitionEnvelope[RequestT](msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: RequestT


def decode_transition_request(raw: dict[str, JsonValue]) -> TransitionRequest:  # noqa: C901, PLR0912, PLR0915 - exhaustive exact wire leaves
    """Decode one strict envelope and exact leaf before resources or effects.

    Transition leaves share role tags, so receipt action identity selects the
    leaf. Complete additionally distinguishes its independently required payload
    shapes. These relational choices cannot be an ordinary tagged union.
    """

    inner = raw.get("request")
    receipt = inner.get("receipt") if isinstance(inner, dict) else None
    action_id = receipt.get("action_id") if isinstance(receipt, dict) else None
    kind = action_id.get("kind") if isinstance(action_id, dict) else None
    request: TransitionRequest
    match kind:
        case "accept-checkpoint":
            request = msgspec.convert(
                raw, type=TransitionEnvelope[AcceptCheckpointTransitionRequest], strict=True
            ).request
        case "accept-review-and-continue":
            request = msgspec.convert(
                raw, type=TransitionEnvelope[AcceptReviewAndContinueTransitionRequest], strict=True
            ).request
        case "accept-proposal":
            request = msgspec.convert(
                raw, type=TransitionEnvelope[AcceptProposalTransitionRequest], strict=True
            ).request
        case "activate":
            request = msgspec.convert(raw, type=TransitionEnvelope[ActivateTransitionRequest], strict=True).request
        case "block":
            request = msgspec.convert(raw, type=TransitionEnvelope[BlockTransitionRequest], strict=True).request
        case "block-item":
            request = msgspec.convert(raw, type=TransitionEnvelope[BlockItemTransitionRequest], strict=True).request
        case "complete":
            payload = inner.get("payload") if isinstance(inner, dict) else None
            if isinstance(payload, dict) and "schema" in payload:
                request = msgspec.convert(
                    raw, type=TransitionEnvelope[CoveredCompleteTransitionRequest], strict=True
                ).request
            else:
                request = msgspec.convert(
                    raw, type=TransitionEnvelope[DirectCompleteTransitionRequest], strict=True
                ).request
        case "close":
            request = msgspec.convert(raw, type=TransitionEnvelope[CloseTransitionRequest], strict=True).request
        case "defer":
            request = msgspec.convert(raw, type=TransitionEnvelope[DeferTransitionRequest], strict=True).request
        case "mark-ready":
            request = msgspec.convert(raw, type=TransitionEnvelope[MarkReadyTransitionRequest], strict=True).request
        case "merge-proposal":
            request = msgspec.convert(raw, type=TransitionEnvelope[MergeProposalTransitionRequest], strict=True).request
        case "pause":
            request = msgspec.convert(raw, type=TransitionEnvelope[PauseTransitionRequest], strict=True).request
        case "reject-proposal":
            request = msgspec.convert(
                raw, type=TransitionEnvelope[RejectProposalTransitionRequest], strict=True
            ).request
        case "reopen":
            request = msgspec.convert(raw, type=TransitionEnvelope[ReopenTransitionRequest], strict=True).request
        case "record-replacement":
            request = msgspec.convert(
                raw, type=TransitionEnvelope[RecordReplacementTransitionRequest], strict=True
            ).request
        case "rebind-attempt":
            request = msgspec.convert(raw, type=TransitionEnvelope[RebindAttemptTransitionRequest], strict=True).request
        case "resume":
            request = msgspec.convert(raw, type=TransitionEnvelope[ResumeTransitionRequest], strict=True).request
        case "return-for-correction":
            request = msgspec.convert(
                raw, type=TransitionEnvelope[ReturnForCorrectionTransitionRequest], strict=True
            ).request
        case "return-proposal":
            request = msgspec.convert(
                raw, type=TransitionEnvelope[ReturnProposalTransitionRequest], strict=True
            ).request
        case "retain-temporarily":
            request = msgspec.convert(
                raw, type=TransitionEnvelope[RetainTemporarilyTransitionRequest], strict=True
            ).request
        case "revise-item":
            request = msgspec.convert(raw, type=TransitionEnvelope[ReviseItemTransitionRequest], strict=True).request
        case "submit-review":
            request = msgspec.convert(raw, type=TransitionEnvelope[SubmitReviewTransitionRequest], strict=True).request
        case _:
            raise ValueError(f"Action '{kind}' is not a supported MCP transition.")
    return request


TRANSITION_REQUEST_TYPES: tuple[Any, ...] = (
    AcceptCheckpointTransitionRequest,
    AcceptReviewAndContinueTransitionRequest,
    AcceptProposalTransitionRequest,
    ActivateTransitionRequest,
    BlockTransitionRequest,
    BlockItemTransitionRequest,
    DirectCompleteTransitionRequest,
    CoveredCompleteTransitionRequest,
    CloseTransitionRequest,
    DeferTransitionRequest,
    MarkReadyTransitionRequest,
    MergeProposalTransitionRequest,
    PauseTransitionRequest,
    RejectProposalTransitionRequest,
    ReopenTransitionRequest,
    RecordReplacementTransitionRequest,
    RebindAttemptTransitionRequest,
    ResumeTransitionRequest,
    ReturnForCorrectionTransitionRequest,
    ReturnProposalTransitionRequest,
    RetainTemporarilyTransitionRequest,
    ReviseItemTransitionRequest,
    SubmitReviewTransitionRequest,
)


class AuthorityRequestBase(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath


class PreparationAuthorityStatusRequest(
    AuthorityRequestBase, tag="status", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    item_id: PathComponent


class PreparationAuthorityStartRequest(
    AuthorityRequestBase, tag="start", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    item_id: PathComponent
    task_id: RuntimeIdentity
    host_id: RuntimeIdentity
    ttl_seconds: PositiveInt


class PreparationAuthorityRenewRequest(
    AuthorityRequestBase, tag="renew", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    item_id: PathComponent
    lease_id: RuntimeIdentity
    generation: PositiveInt
    ttl_seconds: PositiveInt


class PreparationAuthorityReleaseRequest(
    AuthorityRequestBase, tag="release", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    item_id: PathComponent
    lease_id: RuntimeIdentity
    generation: PositiveInt


class PreparationAuthorityRevokeRequest(
    AuthorityRequestBase, tag="revoke", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    item_id: PathComponent
    lease_id: RuntimeIdentity
    generation: PositiveInt
    actor_task_id: RuntimeIdentity
    actor_host_id: RuntimeIdentity


type PreparationAuthorityRequest = (
    PreparationAuthorityStatusRequest
    | PreparationAuthorityStartRequest
    | PreparationAuthorityRenewRequest
    | PreparationAuthorityReleaseRequest
    | PreparationAuthorityRevokeRequest
)


class AttemptAuthorityStatusRequest(
    AuthorityRequestBase, tag="status", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    attempt_id: PathComponent


class AttemptAuthorityAcquireRequest(
    AuthorityRequestBase, tag="acquire", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    attempt_id: PathComponent
    task_id: RuntimeIdentity
    host_id: RuntimeIdentity
    ttl_seconds: PositiveInt


class AttemptAuthorityRenewRequest(
    AuthorityRequestBase, tag="renew", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    attempt_id: PathComponent
    lease_id: RuntimeIdentity
    generation: PositiveInt
    ttl_seconds: PositiveInt


class AttemptAuthorityReleaseRequest(
    AuthorityRequestBase, tag="release", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    attempt_id: PathComponent
    lease_id: RuntimeIdentity
    generation: PositiveInt


class AttemptAuthorityRevokeRequest(
    AuthorityRequestBase, tag="revoke", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    attempt_id: PathComponent
    lease_id: RuntimeIdentity
    generation: PositiveInt
    actor_task_id: RuntimeIdentity
    actor_host_id: RuntimeIdentity


type AttemptAuthorityRequest = (
    AttemptAuthorityStatusRequest
    | AttemptAuthorityAcquireRequest
    | AttemptAuthorityRenewRequest
    | AttemptAuthorityReleaseRequest
    | AttemptAuthorityRevokeRequest
)


class PreparationAuthorityEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: PreparationAuthorityRequest


class AttemptAuthorityEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: AttemptAuthorityRequest


class FailureObservation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    field: NonEmptyText
    value: JsonScalar


class FailureMismatch(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    field: NonEmptyText
    expected: JsonScalar
    observed: JsonScalar


class ItemStatusInvalid(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-item-status-result/v1"]
    status: Literal["rejected"]
    code: Literal["ITEM_STATUS_INVALID"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class ItemStatusUnavailable(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-item-status-result/v1"]
    status: Literal["rejected"]
    code: Literal["ITEM_NOT_FOUND", "ITEM_DEFINITION_INVALID"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class ItemStatusInconsistent(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-item-status-result/v1"]
    status: Literal["rejected"]
    code: Literal["ITEM_STATUS_INCONSISTENT"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["do-not-retry"]
    changed_surfaces: Empty
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=1)]
    mismatches: Annotated[tuple[FailureMismatch, ...], msgspec.Meta(min_length=1)]

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class OverviewRejected(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-overview-result/v1"]
    status: Literal["rejected"]
    code: Literal["OVERVIEW_INVALID"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class RejectedReadResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    status: Literal["rejected"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class ActionsInvalid(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-actions-result/v1"]
    code: Literal["ACTIONS_INVALID"]
    retry: Literal["correct-input"]
    observed: Empty
    mismatches: Empty


class ItemDefinitionRejected(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-item-definition-result/v1"]
    code: Literal["ITEM_DEFINITION_REQUEST_INVALID", "ITEM_NOT_FOUND", "ITEM_DEFINITION_INVALID"]
    retry: Literal["correct-input"]
    observed: Empty
    mismatches: Empty


class ActionUnavailable(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-actions-result/v1"]
    code: Literal["ACTION_NOT_AVAILABLE"]
    retry: Literal["refresh-action"]
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]


class AttemptLeaseRequired(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-actions-result/v1"]
    code: Literal["ATTEMPT_LEASE_REQUIRED"]
    retry: Literal["reacquire-authority"]
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=1)]
    mismatches: Annotated[tuple[FailureMismatch, ...], msgspec.Meta(min_length=1)]


class AttemptInspectInvalid(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-attempt-inspection-result/v1"]
    code: Literal["ATTEMPT_INSPECT_INVALID"]
    retry: Literal["correct-input"]
    observed: Empty
    mismatches: Empty


class AttemptNotFound(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-attempt-inspection-result/v1"]
    code: Literal["ATTEMPT_NOT_FOUND"]
    retry: Literal["correct-input"]
    observed: Empty
    mismatches: Empty


class AttemptBriefInvalid(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-attempt-inspection-result/v1"]
    code: Literal["ATTEMPT_BRIEF_INVALID"]
    retry: Literal["do-not-retry"]
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=1)]
    mismatches: Annotated[tuple[FailureMismatch, ...], msgspec.Meta(min_length=1)]


class AttemptActionUnavailable(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-attempt-inspection-result/v1"]
    code: Literal["ACTION_NOT_AVAILABLE"]
    retry: Literal["refresh-action"]
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]


class ArtifactVerificationInvalid(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-artifact-verification-result/v1"]
    code: Literal["ARTIFACT_VERIFY_INVALID"]
    retry: Literal["correct-input"]
    observed: Empty
    mismatches: Empty


class ArtifactReferenceMismatch(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-artifact-verification-result/v1"]
    code: Literal["ARTIFACT_REFERENCE_MISMATCH"]
    retry: Literal["correct-input"]
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=1)]
    mismatches: Annotated[tuple[FailureMismatch, ...], msgspec.Meta(min_length=1)]


class ArtifactBytesInvalid(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-artifact-verification-result/v1"]
    code: Literal["ARTIFACT_BYTES_INVALID"]
    retry: Literal["do-not-retry"]
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=1)]
    mismatches: Annotated[tuple[FailureMismatch, ...], msgspec.Meta(min_length=1)]


class ActionView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: ActionIdentity
    label: NonEmptyText
    subject_revision: NonEmptyText | None
    authorization: action_models.ActionAuthorization
    lease_id: NonEmptyText | None
    generation: PositiveInt | None
    semantics: action_models.ActionSemanticsView
    input_contract: action_models.InputContractView

    def __post_init__(self) -> None:
        identity = self.action_id
        action_models.ActionView(
            f"{identity.kind.value}:{identity.subject}",
            identity.kind,
            identity.subject,
            self.label,
            self.subject_revision,
            self.authorization,
            self.lease_id,
            self.generation,
            self.semantics,
            self.input_contract,
        )


class ActionsSuccess(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-actions-result/v1"]
    status: Literal["ok"]
    actions: tuple[ActionView, ...]
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class CompletionActionView(ActionView, frozen=True, forbid_unknown_fields=True):
    input_contract: action_models.CompletionInputContractView


class CompletionActionsSuccess(ActionsSuccess, frozen=True, forbid_unknown_fields=True):
    actions: Annotated[tuple[CompletionActionView, ...], msgspec.Meta(min_length=1, max_length=1)]


class AcceptedBriefIdentity(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    artifact_ref_id: PositiveInt
    path: NonEmptyText
    selector: NonEmptyText
    sha256: Sha256
    size_bytes: PositiveInt
    accepted_revision: PositiveInt
    accepted_scope_revision: PositiveInt
    accepted_scope_digest: Sha256


class EvidenceAbsent(msgspec.Struct, tag="absent", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    path: NonEmptyText


class EvidencePresent(msgspec.Struct, tag="present", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    path: NonEmptyText
    sha256: Sha256
    size_bytes: NonNegativeInt


type EvidenceReference = EvidenceAbsent | EvidencePresent


class RelativeActionIdentity(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    target: Literal["attempt", "item"]
    action_kind: decision_models.ActionKind

    def __post_init__(self) -> None:
        if decision_models.action_semantics(self.action_kind).subject_kind.value != self.target:
            raise ValueError("continuation action target must match its action kind")


class ContinuationAction(msgspec.Struct, tag="action", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    action: RelativeActionIdentity
    condition: NonEmptyText


class ContinuationReview(
    msgspec.Struct, tag="review-subagent", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    candidate_revision: NonEmptyText
    required_capability: Literal["runtime-subagent"]


class ContinuationDependencies(
    msgspec.Struct, tag="wait-for-dependencies", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    dependencies: Annotated[tuple[PathComponent, ...], msgspec.Meta(min_length=1)]

    def __post_init__(self) -> None:
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("dependency continuations require unique dependencies")


type ContinuationOperation = ContinuationAction | ContinuationReview | ContinuationDependencies


_ACTIVE_CONTINUATION_ACTION_KINDS = (
    decision_models.ActionKind.CONTINUE,
    decision_models.ActionKind.DISPATCH,
    decision_models.ActionKind.REBIND_ATTEMPT,
    decision_models.ActionKind.PAUSE,
    decision_models.ActionKind.BLOCK,
    decision_models.ActionKind.COMPLETE,
    decision_models.ActionKind.RECORD_REPLACEMENT,
    decision_models.ActionKind.RETAIN_TEMPORARILY,
    decision_models.ActionKind.REVISE_ITEM,
)
_REVIEW_CONTINUATION_ACTION_KINDS = (
    decision_models.ActionKind.COMPLETE,
    decision_models.ActionKind.RETURN_FOR_CORRECTION,
    decision_models.ActionKind.ACCEPT_CHECKPOINT,
    decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE,
    decision_models.ActionKind.RECORD_REPLACEMENT,
    decision_models.ActionKind.RETAIN_TEMPORARILY,
    decision_models.ActionKind.REVISE_ITEM,
)
_PAUSED_CONTINUATION_ACTION_KINDS = (
    decision_models.ActionKind.REBIND_ATTEMPT,
    decision_models.ActionKind.RECORD_REPLACEMENT,
    decision_models.ActionKind.RETAIN_TEMPORARILY,
    decision_models.ActionKind.REVISE_ITEM,
    decision_models.ActionKind.RESUME,
    decision_models.ActionKind.CLOSE,
)
_BLOCKED_CONTINUATION_ACTION_KINDS = (
    decision_models.ActionKind.RECORD_REPLACEMENT,
    decision_models.ActionKind.RETAIN_TEMPORARILY,
    decision_models.ActionKind.REVISE_ITEM,
    decision_models.ActionKind.RESUME,
    decision_models.ActionKind.CLOSE,
)


class TerminalAttemptContinuation(
    msgspec.Struct, tag="done", tag_field="state", frozen=True, forbid_unknown_fields=True
):
    schema: Literal["pinboard-attempt-continuation/v1"]
    attempt_id: PathComponent
    item_id: PathComponent
    revision: PositiveInt
    owner_task_id: None
    terminal: bool
    user_input_required: bool
    next_operation: None
    legal_actions: Empty
    forbidden_routes: tuple[Literal["create-user-task", "wake-user-task", "return-ownership-to-parent"], ...]

    def __post_init__(self) -> None:
        if not self.terminal or self.user_input_required:
            raise ValueError("a terminal continuation requires exact terminal flags")
        if self.forbidden_routes != ("create-user-task", "wake-user-task", "return-ownership-to-parent"):
            raise ValueError("an attempt continuation requires the exact forbidden routes")


class NonterminalAttemptContinuationBase(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-attempt-continuation/v1"]
    attempt_id: PathComponent
    item_id: PathComponent
    revision: PositiveInt
    owner_task_id: RuntimeIdentity
    terminal: bool
    user_input_required: bool
    next_operation: ContinuationOperation
    legal_actions: Annotated[tuple[RelativeActionIdentity, ...], msgspec.Meta(min_length=1)]
    forbidden_routes: tuple[Literal["create-user-task", "wake-user-task", "return-ownership-to-parent"], ...]

    def _validate_common(self) -> None:
        if self.terminal or self.user_input_required:
            raise ValueError("a nonterminal continuation requires exact nonterminal flags")
        if self.forbidden_routes != ("create-user-task", "wake-user-task", "return-ownership-to-parent"):
            raise ValueError("an attempt continuation requires the exact forbidden routes")
        if len(set(self.legal_actions)) != len(self.legal_actions):
            raise ValueError("continuation legal actions must be unique")
        operation = self.next_operation
        if isinstance(operation, ContinuationAction):
            if operation.action not in self.legal_actions:
                raise ValueError("continuation action must be one of its legal actions")
        elif isinstance(operation, ContinuationReview):
            expected = RelativeActionIdentity("attempt", decision_models.ActionKind.ACCEPT_CHECKPOINT)
            if expected not in self.legal_actions:
                raise ValueError("review continuation requires the matching accept-checkpoint action")

    def _validate_legal_action_kinds(self, allowed: tuple[decision_models.ActionKind, ...]) -> None:
        if any(action.action_kind not in allowed for action in self.legal_actions):
            raise ValueError("continuation legal actions must match its state")


class ActiveAttemptContinuation(
    NonterminalAttemptContinuationBase,
    tag="active",
    tag_field="state",
    frozen=True,
    forbid_unknown_fields=True,
):
    def __post_init__(self) -> None:
        self._validate_common()
        self._validate_legal_action_kinds(_ACTIVE_CONTINUATION_ACTION_KINDS)
        operation = self.next_operation
        if not isinstance(operation, ContinuationAction) or operation.action.action_kind not in (
            decision_models.ActionKind.CONTINUE,
            decision_models.ActionKind.PAUSE,
        ):
            raise ValueError("an active continuation requires a continue or pause action")


class ReviewAttemptContinuation(
    NonterminalAttemptContinuationBase,
    tag="review",
    tag_field="state",
    frozen=True,
    forbid_unknown_fields=True,
):
    def __post_init__(self) -> None:
        self._validate_common()
        self._validate_legal_action_kinds(_REVIEW_CONTINUATION_ACTION_KINDS)
        operation = self.next_operation
        if not (
            isinstance(operation, ContinuationReview)
            or (
                isinstance(operation, ContinuationAction)
                and operation.action.action_kind == decision_models.ActionKind.RETURN_FOR_CORRECTION
            )
        ):
            raise ValueError("a review continuation requires review or correction work")


class DependencyOrResumeAttemptContinuationBase(NonterminalAttemptContinuationBase, frozen=True):
    def _validate_dependency_or_resume(self) -> None:
        self._validate_common()
        operation = self.next_operation
        if not (
            isinstance(operation, ContinuationDependencies)
            or (
                isinstance(operation, ContinuationAction)
                and operation.action.action_kind == decision_models.ActionKind.RESUME
            )
        ):
            raise ValueError("a paused or blocked continuation requires dependency or resume work")


class PausedAttemptContinuation(
    DependencyOrResumeAttemptContinuationBase,
    tag="paused",
    tag_field="state",
    frozen=True,
    forbid_unknown_fields=True,
):
    def __post_init__(self) -> None:
        self._validate_dependency_or_resume()
        self._validate_legal_action_kinds(_PAUSED_CONTINUATION_ACTION_KINDS)


class BlockedAttemptContinuation(
    DependencyOrResumeAttemptContinuationBase,
    tag="blocked",
    tag_field="state",
    frozen=True,
    forbid_unknown_fields=True,
):
    def __post_init__(self) -> None:
        self._validate_dependency_or_resume()
        self._validate_legal_action_kinds(_BLOCKED_CONTINUATION_ACTION_KINDS)


type NonterminalAttemptContinuation = (
    ActiveAttemptContinuation | ReviewAttemptContinuation | PausedAttemptContinuation | BlockedAttemptContinuation
)
type AttemptContinuation = TerminalAttemptContinuation | NonterminalAttemptContinuation


class CandidateRecoveryAbsent(msgspec.Struct, tag="absent", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    pass


class CandidateRecoveryPresent(
    msgspec.Struct, tag="present", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    candidate_kind: Literal["working-tree", "commit"]
    candidate: NonEmptyText
    branch: NonEmptyText
    preimage_revision: NonEmptyText
    artifact_ref_id: PositiveInt
    selector: NonEmptyText
    sha256: Sha256
    size_bytes: PositiveInt
    restore_command: tuple[NonEmptyText, ...]


type CandidateRecovery = CandidateRecoveryAbsent | CandidateRecoveryPresent


class TerminalAttemptInspectionSuccess(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-attempt-inspection-result/v1"]
    status: Literal["ok"]
    continuation: TerminalAttemptContinuation
    candidate_recovery: CandidateRecovery
    accepted_brief: None
    result: EvidenceReference
    review: EvidenceReference
    blocker: EvidenceReference
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class NonterminalAttemptInspectionSuccess(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-attempt-inspection-result/v1"]
    status: Literal["ok"]
    continuation: NonterminalAttemptContinuation
    candidate_recovery: CandidateRecovery
    accepted_brief: AcceptedBriefIdentity
    result: EvidenceReference
    review: EvidenceReference
    blocker: EvidenceReference
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class ArtifactVerified(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-verified-artifact-reference/v1"]
    artifact_ref_id: PositiveInt
    selector: NonEmptyText
    sha256: Sha256
    size_bytes: PositiveInt
    accepted_revision: PositiveInt
    verified: bool
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)
        if not self.verified:
            raise ValueError("verified must be true for a verified artifact result.")


class ExecutorBusyResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-execution-result/v1"]
    status: Literal["busy"]
    code: Literal["EXECUTOR_BUSY"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["retry-same-input"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class WarningResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    message: NonEmptyText
    recovery: NonEmptyText


class OrderRecovery(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    tool: Literal["pinboard_overview"]
    arguments: OverviewRequest
    meaning: Literal["current-state-only-not-caller-commit-proof"]


class OrderRejected(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-order-result/v1"]
    code: Literal["ORDER_INVALID", "ACTION_NOT_AVAILABLE", "TRANSITION_INPUT_INVALID"]
    retry: Literal["correct-input"]
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]
    recovery: OrderRecovery | None


class OrderCommitted(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-order-result/v1"]
    order: tuple[ordering.OrderItemId, ...]
    committed_revision: PositiveInt
    history_id: PositiveInt
    recovery: OrderRecovery
    status: Literal["committed", "committed-with-warning"]
    warning: WarningResult | None
    changed_surfaces: LedgerSurface
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    state_changed: bool

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)
        if (self.status == "committed-with-warning") != (self.warning is not None):
            raise ValueError("Order terminal status must agree with its view-repair warning.")


class ParallelPreviewRejected(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-parallel-preview-result/v1"]
    code: Literal["PARALLEL_PREVIEW_INVALID", "PARALLEL_SELECTION_INVALID"]
    retry: Literal["correct-input"]
    observed: Empty
    mismatches: Empty


class ParallelPreviewSuccess(query_models.ParallelPreviewView, frozen=True):
    status: Literal["ok"]
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


def _committed_transition_surfaces(kind: decision_models.ActionKind) -> tuple[tuple[str, ...], ...]:
    """Own the supported action/effect combinations at the MCP result boundary."""
    match kind:
        case (
            decision_models.ActionKind.CONTINUE
            | decision_models.ActionKind.DISPATCH
            | decision_models.ActionKind.INSPECT
            | decision_models.ActionKind.REPORT_BLOCKER
        ):
            return ()
        case decision_models.ActionKind.ACCEPT_CHECKPOINT | decision_models.ActionKind.SUBMIT_REVIEW:
            return (
                ("accepted-artifact-reference", "ledger"),
                ("immutable-artifact", "accepted-artifact-reference", "ledger"),
            )
        case decision_models.ActionKind.COMPLETE:
            return (
                ("ledger",),
                ("accepted-artifact-reference", "ledger"),
                ("immutable-artifact", "accepted-artifact-reference", "ledger"),
            )
        case (
            decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE
            | decision_models.ActionKind.ACCEPT_PROPOSAL
            | decision_models.ActionKind.ACTIVATE
            | decision_models.ActionKind.BLOCK
            | decision_models.ActionKind.BLOCK_ITEM
            | decision_models.ActionKind.CLOSE
            | decision_models.ActionKind.DEFER
            | decision_models.ActionKind.MARK_READY
            | decision_models.ActionKind.MERGE_PROPOSAL
            | decision_models.ActionKind.PAUSE
            | decision_models.ActionKind.REJECT_PROPOSAL
            | decision_models.ActionKind.REOPEN
            | decision_models.ActionKind.RECORD_REPLACEMENT
            | decision_models.ActionKind.REBIND_ATTEMPT
            | decision_models.ActionKind.RESUME
            | decision_models.ActionKind.RETURN_FOR_CORRECTION
            | decision_models.ActionKind.RETURN_PROPOSAL
            | decision_models.ActionKind.RETAIN_TEMPORARILY
            | decision_models.ActionKind.REVISE_ITEM
        ):
            return (("ledger",),)
        case _ as unreachable:
            assert_never(unreachable)


class TransitionCommitted(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-transition-result/v1"]
    status: Literal["committed", "committed-with-warning"]
    action_id: ActionIdentity
    committed_revision: PositiveInt
    history_id: PositiveInt
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: Annotated[
        tuple[Literal["immutable-artifact", "accepted-artifact-reference", "ledger"], ...],
        msgspec.Meta(min_length=1),
    ]
    warning: WarningResult | None

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)
        if (self.status == "committed-with-warning") != (self.warning is not None):
            raise ValueError("committed-with-warning must carry one warning")
        if self.changed_surfaces not in _committed_transition_surfaces(self.action_id.kind):
            raise ValueError("committed transition must pair a mutating action with its exact supported surfaces")


class TransitionRejected(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-transition-result/v1"]
    status: Literal["rejected"]
    action_id: ActionIdentity
    code: DecisionFailureCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: RetryDisposition
    changed_surfaces: Empty
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class PublishedFailureResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Publication already changed durable bytes; exact result leaves own identity and code."""

    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ArtifactSurface
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=1)]
    mismatches: tuple[FailureMismatch, ...]

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class TransitionFailedAfterPublication(PublishedFailureResult, frozen=True):
    schema: Literal["pinboard-mcp-transition-result/v1"]
    status: Literal["failed-after-publication"]
    action_id: ActionIdentity
    code: NonEmptyText
    message: NonEmptyText


class PreparationAuthorityConflict(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    task_id: RuntimeIdentity
    host_id: RuntimeIdentity
    lease_id: RuntimeIdentity
    generation: PositiveInt
    expires_at: NonEmptyText
    authority_status: authority_models.PreparationLeaseStatus


class AttemptAuthorityConflict(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    task_id: RuntimeIdentity
    host_id: RuntimeIdentity
    lease_id: RuntimeIdentity
    generation: PositiveInt
    expires_at: NonEmptyText
    authority_status: authority_models.AttemptLeaseStatus


class PreparationAuthorityStatusPresent(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-preparation-authority-result/v1"]
    status: Literal["present"]
    item_id: PathComponent
    definition_revision: PositiveInt
    definition_digest: Sha256
    task_id: RuntimeIdentity
    host_id: RuntimeIdentity
    lease_id: RuntimeIdentity
    generation: PositiveInt
    acquired_at: NonEmptyText
    expires_at: NonEmptyText
    authority_status: authority_models.PreparationLeaseStatus
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class PreparationAuthorityStatusAbsent(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-preparation-authority-result/v1"]
    status: Literal["absent"]
    item_id: PathComponent
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class AuthorityCommittedResult[StatusT](msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Reloaded committed authority and its optional replaceable-view warning."""

    status: Literal["committed", "committed-with-warning"]
    task_id: RuntimeIdentity
    host_id: RuntimeIdentity
    lease_id: RuntimeIdentity
    generation: PositiveInt
    acquired_at: NonEmptyText
    expires_at: NonEmptyText
    authority_status: StatusT
    committed_revision: PositiveInt
    history_id: PositiveInt
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: LedgerSurface
    warning: WarningResult | None

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)
        if (self.status == "committed-with-warning") != (self.warning is not None):
            raise ValueError("committed-with-warning must carry one warning")


class PreparationAuthorityCommitted(AuthorityCommittedResult[authority_models.PreparationLeaseStatus], frozen=True):
    schema: Literal["pinboard-mcp-preparation-authority-result/v1"]
    item_id: PathComponent
    definition_revision: PositiveInt
    definition_digest: Sha256


class PreparationAuthorityRejected(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-preparation-authority-result/v1"]
    status: Literal["rejected"]
    item_id: PathComponent
    code: DecisionFailureCode
    message: NonEmptyText
    conflict: PreparationAuthorityConflict | None
    state_changed: bool
    effect: Literal["unchanged"]
    retry: RetryDisposition
    changed_surfaces: Empty
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class AttemptAuthorityStatusPresent(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-attempt-authority-result/v1"]
    status: Literal["present"]
    attempt_id: PathComponent
    task_id: RuntimeIdentity
    host_id: RuntimeIdentity
    lease_id: RuntimeIdentity
    generation: PositiveInt
    acquired_at: NonEmptyText
    expires_at: NonEmptyText
    authority_status: authority_models.AttemptLeaseStatus
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class AttemptAuthorityStatusAbsent(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-attempt-authority-result/v1"]
    status: Literal["absent"]
    attempt_id: PathComponent
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class AttemptAuthorityCommitted(AuthorityCommittedResult[authority_models.AttemptLeaseStatus], frozen=True):
    schema: Literal["pinboard-mcp-attempt-authority-result/v1"]
    attempt_id: PathComponent
    item_id: PathComponent


class AttemptAuthorityRejected(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-attempt-authority-result/v1"]
    status: Literal["rejected"]
    attempt_id: PathComponent
    code: DecisionFailureCode
    message: NonEmptyText
    conflict: AttemptAuthorityConflict | None
    state_changed: bool
    effect: Literal["unchanged"]
    retry: RetryDisposition
    changed_surfaces: Empty
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class ProposalCommitted(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-proposal-result/v1"]
    status: Literal["committed"]
    proposal_id: proposal_models.ProposalIdentity
    position: PositiveInt
    item_state: Literal["intake"]
    committed_revision: PositiveInt
    history_id: PositiveInt
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: LedgerSurface
    continuation: NonEmptyText
    warning: None

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class ProposalCommittedWithWarning(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-proposal-result/v1"]
    status: Literal["committed-with-warning"]
    proposal_id: proposal_models.ProposalIdentity
    position: PositiveInt
    item_state: Literal["intake"]
    committed_revision: PositiveInt
    history_id: PositiveInt
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: LedgerSurface
    continuation: NonEmptyText
    warning: WarningResult

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class ProposalRejected(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-proposal-result/v1"]
    status: Literal["rejected"]
    code: Literal[
        "PROPOSAL_INVALID",
        "ITEM_ALREADY_EXISTS",
        "ITEM_NOT_FOUND",
        "ACTION_NOT_AVAILABLE",
        "ITEM_DEFINITION_INVALID",
    ]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty
    recovery: None

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class ProposalDuplicate(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-proposal-result/v1"]
    status: Literal["rejected"]
    code: Literal["PROPOSAL_ALREADY_EXISTS"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["do-not-retry"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty
    recovery: NonEmptyText

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class ArtifactReferenceResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    artifact_ref_id: PositiveInt
    kind: Literal["brief"]
    key: work_brief_models.KebabId
    revision: PositiveInt
    selector: NonEmptyText
    sha256: Sha256
    size_bytes: PositiveInt
    accepted_revision: PositiveInt


class BriefCommitted(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: PublicationSurfaces
    continuation: NonEmptyText
    warning: None

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class BriefReferenceCommitted(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ReferenceSurfaces
    continuation: NonEmptyText
    warning: None

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class BriefArtifactCommitted(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ArtifactSurface
    continuation: NonEmptyText
    warning: None

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class BriefCommittedWithWarning(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed-with-warning"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: PublicationSurfaces
    continuation: NonEmptyText
    warning: WarningResult

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class BriefReferenceCommittedWithWarning(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed-with-warning"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ReferenceSurfaces
    continuation: NonEmptyText
    warning: WarningResult

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class BriefArtifactCommittedWithWarning(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed-with-warning"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ArtifactSurface
    continuation: NonEmptyText
    warning: WarningResult

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class BriefUnchanged(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["unchanged"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["retry-same-input"]
    changed_surfaces: Empty
    continuation: NonEmptyText
    warning: None

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class BriefUnchangedWithWarning(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["unchanged-with-warning"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["retry-same-input"]
    changed_surfaces: Empty
    continuation: NonEmptyText
    warning: WarningResult

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class BriefRejected(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["rejected"]
    code: Literal["WORK_BRIEF_INVALID", "ACTION_NOT_AVAILABLE"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class BriefPublishedRejection(PublishedFailureResult, frozen=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["rejected"]
    code: Literal["ACTION_NOT_AVAILABLE"]
    message: NonEmptyText


class PublicationAcceptanceFailureResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    status: Literal["failed-after-publication"]
    code: Literal["ARTIFACT_ACCEPTANCE_FAILED"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ArtifactSurface
    observed: Empty
    mismatches: Empty
    published_selector: NonEmptyText
    recovery: NonEmptyText

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class BriefPublicationAcceptanceFailure(PublicationAcceptanceFailureResult, frozen=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]


class ReviewEvidenceReference(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    artifact_ref_id: PositiveInt
    kind: Literal["evidence"]
    key: NonEmptyText
    revision: PositiveInt
    selector: NonEmptyText
    sha256: Sha256
    size_bytes: PositiveInt
    accepted_revision: PositiveInt


class CorrectedBriefPublicationTarget(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    tool: Literal["pinboard_brief_publish"]
    project_root: RootPath
    work_root: RootPath


class BriefReviewCorrection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    status_request: BriefReviewStatusRequest
    corrected_brief_publication: CorrectedBriefPublicationTarget
    negative_review_tool: Literal["pinboard_brief_review"]
    instruction: NonEmptyText


class BriefReviewStatusResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-review-result/v1"]
    accepted_brief: ArtifactReferenceResult
    brief: work_brief_models.WorkBrief
    correction: BriefReviewCorrection
    retry: Literal["safe-to-repeat"]
    state_changed: bool
    effect: Literal["unchanged"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class BriefReviewNoEvidence(BriefReviewStatusResult, frozen=True):
    status: Literal["no-needs-correction-evidence"]


class BriefReviewNeedsCorrection(BriefReviewStatusResult, frozen=True):
    status: Literal["needs-correction"]
    reference: ReviewEvidenceReference
    review: work_brief_models.WorkBriefReviewNeedsCorrection


class BriefReviewCommitted(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-review-result/v1"]
    status: Literal["committed"]
    reference: ReviewEvidenceReference
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: Annotated[tuple[JobPublicationSurface, ...], msgspec.Meta(min_length=1)]
    correction: BriefReviewCorrection

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)
        _require_publication_surfaces(self.changed_surfaces)


class BriefReviewUnchanged(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-review-result/v1"]
    status: Literal["unchanged"]
    reference: ReviewEvidenceReference
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["retry-same-input"]
    changed_surfaces: Empty
    correction: BriefReviewCorrection

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class BriefReviewRejected(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-brief-review-result/v1"]
    code: Literal[
        "BRIEF_REVIEW_REQUEST_INVALID",
        "WORK_BRIEF_INVALID",
        "WORK_BRIEF_NOT_CANONICAL",
        "WORK_BRIEF_REVIEW_INVALID",
        "WORK_BRIEF_REVIEW_NOT_CANONICAL",
        "WORK_BRIEF_REVIEW_NOT_INDEPENDENT",
        "WORK_BRIEF_REVIEW_STALE",
        "ACTION_NOT_AVAILABLE",
    ]
    retry: Literal["correct-input"]
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]


class BriefReviewPublishedRejection(PublishedFailureResult, frozen=True):
    schema: Literal["pinboard-mcp-brief-review-result/v1"]
    status: Literal["rejected"]
    code: Literal["ACTION_NOT_AVAILABLE"]
    message: NonEmptyText


class BriefReviewAcceptanceFailure(PublicationAcceptanceFailureResult, frozen=True):
    schema: Literal["pinboard-mcp-brief-review-result/v1"]


class PublishedJobReady(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    status: Literal["ready"]
    prompt_reference: dispatch_models.PromptReferenceView
    native_launch: dispatch_models.NativeLaunchEnvelope
    state_changed: bool
    effect: Literal["unchanged", "committed"]
    retry: Literal["safe-to-repeat", "do-not-retry"]
    changed_surfaces: tuple[JobPublicationSurface, ...]

    def __post_init__(self) -> None:
        surfaces = self.changed_surfaces
        if surfaces not in (
            (),
            ("immutable-artifact",),
            ("accepted-artifact-reference", "ledger"),
            ("immutable-artifact", "accepted-artifact-reference", "ledger"),
        ):
            raise ValueError("Job publication must expose an exact immutable/reference/ledger surface set.")
        changed = bool(surfaces)
        _require_state_changed(self.state_changed, changed)
        if self.effect != ("committed" if changed else "unchanged") or self.retry != (
            "do-not-retry" if changed else "safe-to-repeat"
        ):
            raise ValueError("Job publication effect and retry must match its terminal surfaces.")
        reference = self.prompt_reference
        if reference.artifact_created and "immutable-artifact" not in surfaces:
            raise ValueError("A newly created prompt must expose its immutable artifact effect.")
        if reference.ledger_changed and ("accepted-artifact-reference" not in surfaces or "ledger" not in surfaces):
            raise ValueError("A newly accepted prompt must expose reference and ledger effects.")


class DispatchReady(PublishedJobReady, frozen=True):
    schema: Literal["pinboard-mcp-dispatch-result/v1"]
    attempt_id: PathComponent
    checkpoint_id: PathComponent


class ReviewJobReady(PublishedJobReady, frozen=True):
    schema: Literal["pinboard-mcp-review-job-result/v1"]
    attempt_id: PathComponent
    candidate_revision: NonEmptyText
    candidate_recovery: CandidateRecoveryPresent
    owner_task_id: RuntimeIdentity
    brief_path: NonEmptyText
    brief_sha256: Sha256
    accepted_scope_revision: PositiveInt
    accepted_scope_digest: Sha256
    result_path: NonEmptyText
    result_sha256: Sha256
    prior_checkpoint_package: review_operations.PriorCheckpointPackageSelection
    review_round: review_operations.ReviewRound
    return_contract: NonEmptyText

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.candidate_revision != self.candidate_recovery.candidate:
            raise ValueError("Review job and protected snapshot must identify the same candidate.")


class DispatchInvalid(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-dispatch-result/v1"]
    code: Literal["DISPATCH_INVALID"]
    retry: Literal["correct-input"]
    observed: Empty
    mismatches: Empty


class ReviewJobInvalid(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-review-job-result/v1"]
    code: Literal["REVIEW_JOB_INVALID"]
    retry: Literal["correct-input"]
    observed: Empty
    mismatches: Empty


class DispatchRejected(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-dispatch-result/v1"]
    attempt_id: PathComponent
    code: NonEmptyText
    retry: RetryDisposition
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        try:
            dispatch_operations.DispatchErrorCode(self.code)
        except ValueError:
            DecisionFailureCode(self.code)


class ReviewJobRejected(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-review-job-result/v1"]
    attempt_id: PathComponent
    code: DecisionFailureCode
    retry: RetryDisposition
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]


class JobFailedAfterPublication(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    status: Literal["failed-after-publication"]
    attempt_id: PathComponent
    code: NonEmptyText
    message: NonEmptyText
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: Annotated[tuple[JobPublicationSurface, ...], msgspec.Meta(min_length=1)]
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)
        _require_publication_surfaces(self.changed_surfaces)


class DispatchFailedAfterPublication(JobFailedAfterPublication, frozen=True):
    schema: Literal["pinboard-mcp-dispatch-result/v1"]

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.code != "ARTIFACT_ACCEPTANCE_FAILED":
            try:
                dispatch_operations.DispatchErrorCode(self.code)
            except ValueError:
                DecisionFailureCode(self.code)


class ReviewJobFailedAfterPublication(JobFailedAfterPublication, frozen=True):
    schema: Literal["pinboard-mcp-review-job-result/v1"]
    code: Literal["ARTIFACT_ACCEPTANCE_FAILED"]


DISPATCH_RESULT_TYPES = (
    DispatchReady,
    DispatchInvalid,
    DispatchRejected,
    DispatchFailedAfterPublication,
    ExecutorBusyResult,
)
REVIEW_JOB_RESULT_TYPES = (
    ReviewJobReady,
    ReviewJobInvalid,
    ReviewJobRejected,
    ReviewJobFailedAfterPublication,
    ExecutorBusyResult,
)

ITEM_DEFINITION_RESULT_TYPES = (
    query_models.ItemDefinition,
    query_models.ItemDefinitionHistory,
    ItemDefinitionRejected,
    ExecutorBusyResult,
)
BRIEF_REVIEW_RESULT_TYPES = (
    BriefReviewNoEvidence,
    BriefReviewNeedsCorrection,
    BriefReviewCommitted,
    BriefReviewUnchanged,
    BriefReviewRejected,
    BriefReviewPublishedRejection,
    BriefReviewAcceptanceFailure,
    ExecutorBusyResult,
)


ITEM_STATUS_RESULT_TYPES = (
    query_models.ItemStatus,
    ItemStatusInvalid,
    ItemStatusUnavailable,
    ItemStatusInconsistent,
    ExecutorBusyResult,
)
OVERVIEW_RESULT_TYPES = (query_models.WorkOverview, OverviewRejected, ExecutorBusyResult)
ORDER_RESULT_TYPES = (OrderCommitted, OrderRejected, ExecutorBusyResult)
PARALLEL_PREVIEW_RESULT_TYPES = (ParallelPreviewSuccess, ParallelPreviewRejected, ExecutorBusyResult)
ACTIONS_RESULT_TYPES = (
    ActionsSuccess,
    CompletionActionsSuccess,
    ActionsInvalid,
    ActionUnavailable,
    AttemptLeaseRequired,
    ExecutorBusyResult,
)
ATTEMPT_INSPECTION_RESULT_TYPES = (
    TerminalAttemptInspectionSuccess,
    NonterminalAttemptInspectionSuccess,
    AttemptInspectInvalid,
    AttemptNotFound,
    AttemptBriefInvalid,
    AttemptActionUnavailable,
    ExecutorBusyResult,
)
ARTIFACT_VERIFICATION_RESULT_TYPES = (
    ArtifactVerified,
    ArtifactVerificationInvalid,
    ArtifactReferenceMismatch,
    ArtifactBytesInvalid,
    ExecutorBusyResult,
)
PROPOSAL_RESULT_TYPES = (
    ProposalCommitted,
    ProposalCommittedWithWarning,
    ProposalRejected,
    ProposalDuplicate,
    ExecutorBusyResult,
)
BRIEF_PUBLICATION_RESULT_TYPES = (
    BriefCommitted,
    BriefReferenceCommitted,
    BriefArtifactCommitted,
    BriefCommittedWithWarning,
    BriefReferenceCommittedWithWarning,
    BriefArtifactCommittedWithWarning,
    BriefUnchanged,
    BriefUnchangedWithWarning,
    BriefRejected,
    BriefPublishedRejection,
    BriefPublicationAcceptanceFailure,
    ExecutorBusyResult,
)
PREPARATION_AUTHORITY_RESULT_TYPES = (
    PreparationAuthorityStatusPresent,
    PreparationAuthorityStatusAbsent,
    PreparationAuthorityCommitted,
    PreparationAuthorityRejected,
    ExecutorBusyResult,
)
ATTEMPT_AUTHORITY_RESULT_TYPES = (
    AttemptAuthorityStatusPresent,
    AttemptAuthorityStatusAbsent,
    AttemptAuthorityCommitted,
    AttemptAuthorityRejected,
    ExecutorBusyResult,
)
TRANSITION_RESULT_TYPES = (
    TransitionCommitted,
    TransitionRejected,
    TransitionFailedAfterPublication,
    ExecutorBusyResult,
)
type RequestBoundary = (
    type[BriefContractEnvelope]
    | type[BriefSourcesEnvelope]
    | type[ItemStatusRequest]
    | type[ProposalCreateRequest]
    | type[BriefPublishRequest]
    | type[OverviewRequest]
    | type[AttemptInspectRequest]
    | type[ArtifactVerifyRequest]
    | type[DispatchRequest]
    | type[ReviewJobRequest]
    | type[ActionsEnvelope]
    | type[PreparationAuthorityEnvelope]
    | type[AttemptAuthorityEnvelope]
    | type[ItemDefinitionEnvelope]
    | type[BriefReviewEnvelope]
    | type[OrderEnvelope]
    | type[ParallelPreviewEnvelope]
)
type ResultBoundary = (
    type[work_brief_contract.WorkBriefContract]
    | type[work_brief_contract.WorkBriefStarterContract]
    | type[BriefContractRejected]
    | type[brief_source_models.BriefSourcePlanView]
    | type[BriefSourcePlanOutputResult]
    | type[BriefSourceBatchResult]
    | type[BriefSourcesRejected]
    | type[BriefSourcesPublishedFailure]
    | type[query_models.ItemStatus]
    | type[ItemStatusInvalid]
    | type[ItemStatusUnavailable]
    | type[ItemStatusInconsistent]
    | type[ExecutorBusyResult]
    | type[ProposalCommitted]
    | type[ProposalCommittedWithWarning]
    | type[ProposalRejected]
    | type[ProposalDuplicate]
    | type[BriefCommitted]
    | type[BriefReferenceCommitted]
    | type[BriefArtifactCommitted]
    | type[BriefCommittedWithWarning]
    | type[BriefReferenceCommittedWithWarning]
    | type[BriefArtifactCommittedWithWarning]
    | type[BriefUnchanged]
    | type[BriefUnchangedWithWarning]
    | type[BriefRejected]
    | type[BriefPublishedRejection]
    | type[BriefPublicationAcceptanceFailure]
    | type[query_models.WorkOverview]
    | type[OverviewRejected]
    | type[OrderCommitted]
    | type[OrderRejected]
    | type[ParallelPreviewSuccess]
    | type[ParallelPreviewRejected]
    | type[ActionsInvalid]
    | type[ActionUnavailable]
    | type[AttemptLeaseRequired]
    | type[AttemptInspectInvalid]
    | type[AttemptNotFound]
    | type[AttemptBriefInvalid]
    | type[AttemptActionUnavailable]
    | type[ArtifactVerificationInvalid]
    | type[ArtifactReferenceMismatch]
    | type[ArtifactBytesInvalid]
    | type[ActionsSuccess]
    | type[CompletionActionsSuccess]
    | type[TerminalAttemptInspectionSuccess]
    | type[NonterminalAttemptInspectionSuccess]
    | type[ArtifactVerified]
    | type[PreparationAuthorityStatusPresent]
    | type[PreparationAuthorityStatusAbsent]
    | type[PreparationAuthorityCommitted]
    | type[PreparationAuthorityRejected]
    | type[AttemptAuthorityStatusPresent]
    | type[AttemptAuthorityStatusAbsent]
    | type[AttemptAuthorityCommitted]
    | type[AttemptAuthorityRejected]
    | type[TransitionCommitted]
    | type[TransitionRejected]
    | type[TransitionFailedAfterPublication]
    | type[DispatchReady]
    | type[DispatchInvalid]
    | type[DispatchRejected]
    | type[DispatchFailedAfterPublication]
    | type[ReviewJobReady]
    | type[ReviewJobInvalid]
    | type[ReviewJobRejected]
    | type[ReviewJobFailedAfterPublication]
    | type[query_models.ItemDefinition]
    | type[query_models.ItemDefinitionHistory]
    | type[ItemDefinitionRejected]
    | type[BriefReviewNoEvidence]
    | type[BriefReviewNeedsCorrection]
    | type[BriefReviewCommitted]
    | type[BriefReviewUnchanged]
    | type[BriefReviewRejected]
    | type[BriefReviewPublishedRejection]
    | type[BriefReviewAcceptanceFailure]
)


def schema_for(boundary_type: RequestBoundary) -> dict[str, JsonSchemaValue]:
    """Return the exact msgspec schema for one MCP request."""
    schema: dict[str, JsonSchemaValue] = msgspec.json.schema(
        boundary_type, schema_hook=dispatch_models.dispatch_environment_schema_hook
    )
    reference = schema.pop("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
        raise ValueError("The MCP request schema must have one named root record.")
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        raise TypeError("The MCP request schema must contain named definitions.")
    definition = definitions.get(reference.removeprefix("#/$defs/"))
    if not isinstance(definition, dict):
        raise TypeError("The MCP request schema root must be an object definition.")
    return {**definition, **schema}


def actions_request_schema() -> dict[str, JsonSchemaValue]:
    """Return the exact role-discriminated action request schema."""

    return schema_for(ActionsEnvelope)


def preparation_authority_request_schema() -> dict[str, JsonSchemaValue]:
    return schema_for(PreparationAuthorityEnvelope)


def attempt_authority_request_schema() -> dict[str, JsonSchemaValue]:
    return schema_for(AttemptAuthorityEnvelope)


def transition_request_schema() -> dict[str, JsonSchemaValue]:
    schemas, definitions = msgspec.json.schema_components(TRANSITION_REQUEST_TYPES)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["request"],
        "properties": {"request": {"oneOf": list[JsonSchemaValue](schemas)}},
        "$defs": definitions,
    }


def _apply_boolean_constants(definitions: dict[str, JsonSchemaValue]) -> None:
    changed_results = {
        "OrderCommitted",
        "BriefReviewCommitted",
        "BriefReviewPublishedRejection",
        "BriefReviewAcceptanceFailure",
        "ProposalCommitted",
        "ProposalCommittedWithWarning",
        "BriefCommitted",
        "BriefReferenceCommitted",
        "BriefArtifactCommitted",
        "BriefCommittedWithWarning",
        "BriefReferenceCommittedWithWarning",
        "BriefArtifactCommittedWithWarning",
        "BriefPublishedRejection",
        "BriefPublicationAcceptanceFailure",
        "PreparationAuthorityCommitted",
        "AttemptAuthorityCommitted",
        "TransitionCommitted",
        "TransitionFailedAfterPublication",
        "DispatchFailedAfterPublication",
        "ReviewJobFailedAfterPublication",
    }
    unchanged_results = {
        "OrderRejected",
        "ParallelPreviewSuccess",
        "ParallelPreviewRejected",
        "ItemDefinitionRejected",
        "BriefReviewNoEvidence",
        "BriefReviewNeedsCorrection",
        "BriefReviewUnchanged",
        "BriefReviewRejected",
        "ItemStatusInvalid",
        "ItemStatusUnavailable",
        "ItemStatusInconsistent",
        "ExecutorBusyResult",
        "ProposalRejected",
        "ProposalDuplicate",
        "BriefUnchanged",
        "BriefUnchangedWithWarning",
        "BriefRejected",
        "OverviewRejected",
        "ActionsInvalid",
        "ActionUnavailable",
        "AttemptLeaseRequired",
        "AttemptInspectInvalid",
        "AttemptNotFound",
        "AttemptBriefInvalid",
        "AttemptActionUnavailable",
        "ArtifactVerificationInvalid",
        "ArtifactReferenceMismatch",
        "ArtifactBytesInvalid",
        "ActionsSuccess",
        "CompletionActionsSuccess",
        "TerminalAttemptInspectionSuccess",
        "NonterminalAttemptInspectionSuccess",
        "ArtifactVerified",
        "PreparationAuthorityStatusPresent",
        "PreparationAuthorityStatusAbsent",
        "PreparationAuthorityRejected",
        "AttemptAuthorityStatusPresent",
        "AttemptAuthorityStatusAbsent",
        "AttemptAuthorityRejected",
        "TransitionRejected",
        "DispatchInvalid",
        "DispatchRejected",
        "ReviewJobInvalid",
        "ReviewJobRejected",
    }
    for name, definition in definitions.items():
        if name not in changed_results | unchanged_results:
            continue
        if not isinstance(definition, dict):
            raise TypeError(f"MCP result definition '{name}' must be an object.")
        properties = definition.get("properties")
        if not isinstance(properties, dict):
            raise TypeError(f"MCP result definition '{name}' must declare properties.")
        properties["state_changed"] = {"type": "boolean", "const": name in changed_results}
        if name == "BriefReviewCommitted":
            properties["changed_surfaces"] = {
                "enum": [
                    ["immutable-artifact"],
                    ["accepted-artifact-reference", "ledger"],
                    ["immutable-artifact", "accepted-artifact-reference", "ledger"],
                ],
            }
        if name == "ArtifactVerified":
            properties["verified"] = {"type": "boolean", "const": True}
        if name == "OrderCommitted":
            definition["oneOf"] = [
                {"properties": {"status": {"const": "committed"}, "warning": {"type": "null"}}},
                {
                    "properties": {
                        "status": {"const": "committed-with-warning"},
                        "warning": {"$ref": "#/$defs/WarningResult"},
                    }
                },
            ]


def _action_semantics_constraint(kind: decision_models.ActionKind) -> dict[str, JsonSchemaValue]:
    semantics = decision_models.action_semantics(kind)
    roles = [role.value for role in semantics.permitted_roles]
    return {
        "type": "object",
        "properties": {
            "use_case": {"const": semantics.use_case},
            "effect": {"const": semantics.lifecycle_effect.value},
            "permitted_roles": {
                "type": "array",
                "prefixItems": [{"const": role} for role in roles],
                "minItems": len(roles),
                "maxItems": len(roles),
            },
            "subject_kind": {"const": semantics.subject_kind.value},
            "lifecycle_precondition": {"const": semantics.lifecycle_precondition.value},
            "practical_result": {"const": semantics.practical_result},
        },
    }


def _apply_action_constraints(definitions: dict[str, JsonSchemaValue]) -> None:
    definition = definitions.get("ActionView")
    if not isinstance(definition, dict):
        return
    authority_shapes: list[JsonSchemaValue] = [
        {
            "properties": {
                "authorization": {"const": "observer"},
                "subject_revision": {"type": "null"},
                "lease_id": {"type": "null"},
                "generation": {"type": "null"},
            }
        },
        {
            "properties": {
                "authorization": {"const": "project"},
                "subject_revision": {"type": "string"},
                "lease_id": {"type": "null"},
                "generation": {"type": "null"},
            }
        },
        {
            "properties": {
                "authorization": {"const": "attempt"},
                "subject_revision": {"type": "string"},
                "lease_id": {"type": "string"},
                "generation": {"type": "integer", "minimum": 1},
            }
        },
        {
            "properties": {
                "authorization": {"const": "preparation"},
                "subject_revision": {"type": "string"},
                "lease_id": {"type": "string"},
                "generation": {"type": "integer", "minimum": 1},
            }
        },
    ]
    role_authorities = {
        decision_models.Role.OBSERVER: "observer",
        decision_models.Role.PROJECT: "project",
        decision_models.Role.WORKER: "attempt",
        decision_models.Role.PREPARER: "preparation",
    }
    correlations: list[JsonSchemaValue] = [{"oneOf": authority_shapes}]
    for kind in decision_models.ActionKind:
        semantics = decision_models.action_semantics(kind)
        semantic_constraint = _action_semantics_constraint(kind)
        payload_schema = action_models.action_payload_schema(kind)
        correlations.append(
            {
                "if": {
                    "properties": {
                        "action_id": {"properties": {"kind": {"const": kind.value}}},
                    }
                },
                "then": {
                    "properties": {
                        "authorization": {"enum": [role_authorities[role] for role in semantics.permitted_roles]},
                        "semantics": semantic_constraint,
                        "input_contract": {
                            "type": "object",
                            "properties": {
                                "action_kind": {"const": kind.value},
                                "semantics": semantic_constraint,
                                "payload_schema": {"const": payload_schema},
                            },
                        },
                    }
                },
            }
        )
    definition["allOf"] = correlations


def _apply_transition_constraints(definitions: dict[str, JsonSchemaValue]) -> None:
    definition = definitions.get("TransitionCommitted")
    if not isinstance(definition, dict):
        return
    definition["oneOf"] = [
        {
            "properties": {
                "action_id": {"properties": {"kind": {"const": kind.value}}},
                "changed_surfaces": {
                    "type": "array",
                    "prefixItems": [{"const": surface} for surface in surfaces],
                    "minItems": len(surfaces),
                    "maxItems": len(surfaces),
                },
            }
        }
        for kind in decision_models.ActionKind
        for surfaces in _committed_transition_surfaces(kind)
    ]


def _relative_action(kind: decision_models.ActionKind) -> dict[str, JsonSchemaValue]:
    target = decision_models.action_semantics(kind).subject_kind.value
    if target not in {"attempt", "item"}:
        raise ValueError(f"Action kind '{kind.value}' cannot appear in an attempt continuation.")
    return {
        "allOf": [
            {"$ref": "#/$defs/RelativeActionIdentity"},
            {
                "properties": {
                    "target": {"const": target},
                    "action_kind": {"const": kind.value},
                }
            },
        ]
    }


def _apply_relative_action_constraints(definitions: dict[str, JsonSchemaValue]) -> None:
    definition = definitions.get("RelativeActionIdentity")
    if not isinstance(definition, dict):
        return
    definition["oneOf"] = [
        {
            "properties": {
                "target": {"const": semantics.subject_kind.value},
                "action_kind": {"const": kind.value},
            }
        }
        for kind in decision_models.ActionKind
        if (semantics := decision_models.action_semantics(kind)).subject_kind
        in {decision_models.ActionSubjectKind.ATTEMPT, decision_models.ActionSubjectKind.ITEM}
    ]


def _action_continuation(kind: decision_models.ActionKind) -> dict[str, JsonSchemaValue]:
    return {
        "allOf": [
            {"$ref": "#/$defs/ContinuationAction"},
            {
                "properties": {
                    "action": _relative_action(kind),
                }
            },
        ]
    }


def _forbidden_routes_constraint() -> dict[str, JsonSchemaValue]:
    values = ("create-user-task", "wake-user-task", "return-ownership-to-parent")
    return {
        "type": "array",
        "prefixItems": [{"const": value} for value in values],
        "minItems": len(values),
        "maxItems": len(values),
    }


def _apply_attempt_constraints(definitions: dict[str, JsonSchemaValue]) -> None:
    terminal = definitions.get("TerminalAttemptContinuation")
    if isinstance(terminal, dict) and isinstance(terminal.get("properties"), dict):
        terminal["properties"]["state"] = {"const": "done"}
        terminal["properties"]["terminal"] = {"type": "boolean", "const": True}
        terminal["properties"]["user_input_required"] = {"type": "boolean", "const": False}
        terminal["properties"]["forbidden_routes"] = _forbidden_routes_constraint()
    continuation_constraints: tuple[
        tuple[
            str,
            tuple[dict[str, JsonSchemaValue], ...],
            tuple[decision_models.ActionKind, ...],
        ],
        ...,
    ] = (
        (
            "ActiveAttemptContinuation",
            (
                _action_continuation(decision_models.ActionKind.CONTINUE),
                _action_continuation(decision_models.ActionKind.PAUSE),
            ),
            _ACTIVE_CONTINUATION_ACTION_KINDS,
        ),
        (
            "ReviewAttemptContinuation",
            (
                {"$ref": "#/$defs/ContinuationReview"},
                _action_continuation(decision_models.ActionKind.RETURN_FOR_CORRECTION),
            ),
            _REVIEW_CONTINUATION_ACTION_KINDS,
        ),
        (
            "PausedAttemptContinuation",
            (
                {"$ref": "#/$defs/ContinuationDependencies"},
                _action_continuation(decision_models.ActionKind.RESUME),
            ),
            _PAUSED_CONTINUATION_ACTION_KINDS,
        ),
        (
            "BlockedAttemptContinuation",
            (
                {"$ref": "#/$defs/ContinuationDependencies"},
                _action_continuation(decision_models.ActionKind.RESUME),
            ),
            _BLOCKED_CONTINUATION_ACTION_KINDS,
        ),
    )
    for definition_name, next_operations, legal_action_kinds in continuation_constraints:
        definition = definitions.get(definition_name)
        if not isinstance(definition, dict):
            continue
        properties = definition.get("properties")
        if isinstance(properties, dict):
            properties["terminal"] = {"type": "boolean", "const": False}
            properties["user_input_required"] = {"type": "boolean", "const": False}
            properties["owner_task_id"] = {"type": "string", "minLength": 1}
            properties["legal_actions"] = {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"anyOf": [_relative_action(kind) for kind in legal_action_kinds]},
            }
            properties["forbidden_routes"] = _forbidden_routes_constraint()
            properties["next_operation"] = {"anyOf": list(next_operations)}


def _apply_job_constraints(definitions: dict[str, JsonSchemaValue]) -> None:
    publication_sets = (
        (),
        ("immutable-artifact",),
        ("accepted-artifact-reference", "ledger"),
        ("immutable-artifact", "accepted-artifact-reference", "ledger"),
    )
    for name in (
        "DispatchReady",
        "ReviewJobReady",
        "DispatchFailedAfterPublication",
        "ReviewJobFailedAfterPublication",
    ):
        definition = definitions.get(name)
        if not isinstance(definition, dict):
            continue
        failed = name.endswith("FailedAfterPublication")
        alternatives: list[JsonSchemaValue] = []
        for surfaces in publication_sets:
            if failed and not surfaces:
                continue
            surface_constraint: dict[str, JsonSchemaValue] = {
                "type": "array",
                "minItems": len(surfaces),
                "maxItems": len(surfaces),
            }
            if surfaces:
                surface_constraint["prefixItems"] = [{"const": surface} for surface in surfaces]
            properties: dict[str, JsonSchemaValue] = {
                "changed_surfaces": surface_constraint,
                "state_changed": {"const": bool(surfaces)},
                "effect": {"const": "committed" if surfaces else "unchanged"},
                "retry": {"const": "do-not-retry" if surfaces else "safe-to-repeat"},
            }
            if not failed:
                reference_properties: dict[str, JsonSchemaValue] = {}
                if "immutable-artifact" not in surfaces:
                    reference_properties["artifact_created"] = {"const": False}
                if "ledger" not in surfaces:
                    reference_properties["ledger_changed"] = {"const": False}
                properties["prompt_reference"] = {"properties": reference_properties}
            alternatives.append({"properties": properties})
        definition["oneOf"] = alternatives
    rejected = definitions.get("DispatchRejected")
    if isinstance(rejected, dict):
        rejected_properties = rejected.get("properties")
        if isinstance(rejected_properties, dict):
            rejected_properties["code"] = {
                "type": "string",
                "enum": list[JsonSchemaValue](
                    sorted(
                        {
                            *(code.value for code in dispatch_operations.DispatchErrorCode),
                            *(code.value for code in DecisionFailureCode),
                        }
                    )
                ),
            }


def union_schema_for(boundary_types: tuple[ResultBoundary, ...]) -> dict[str, JsonSchemaValue]:
    """Return one closed MCP result schema from separate correlated records."""
    components = msgspec.json.schema_components(boundary_types)
    schemas: tuple[dict[str, JsonSchemaValue], ...] = components[0]
    definitions: dict[str, JsonSchemaValue] = components[1]
    _apply_boolean_constants(definitions)
    _apply_action_constraints(definitions)
    _apply_transition_constraints(definitions)
    _apply_relative_action_constraints(definitions)
    _apply_attempt_constraints(definitions)
    _apply_job_constraints(definitions)
    return {"type": "object", "anyOf": list[JsonSchemaValue](schemas), "$defs": definitions}


BRIEF_CONTRACT_RESULT_TYPES = (
    work_brief_contract.WorkBriefContract,
    work_brief_contract.WorkBriefStarterContract,
    BriefContractRejected,
    ExecutorBusyResult,
)
BRIEF_SOURCES_RESULT_TYPES = (
    brief_source_models.BriefSourcePlanView,
    BriefSourcePlanOutputResult,
    BriefSourceBatchResult,
    BriefSourcesRejected,
    BriefSourcesPublishedFailure,
    ExecutorBusyResult,
)


def validate_brief_preparation_result(content: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Decode Raw-bearing construction outputs exactly; structured requests never take this route."""
    match content.get("schema"):
        case "pinboard-work-brief-contract/v1":
            msgspec.json.decode(msgspec.json.encode(content), type=work_brief_contract.WorkBriefContract)
        case "pinboard-work-brief-starter/v1":
            msgspec.json.decode(msgspec.json.encode(content), type=work_brief_contract.WorkBriefStarterContract)
        case "pinboard-brief-source-plan/v1":
            msgspec.convert(content, type=brief_source_models.BriefSourcePlanView, strict=True)
        case "pinboard-brief-source-plan-output/v1":
            msgspec.convert(content, type=BriefSourcePlanOutputResult, strict=True)
        case "pinboard-brief-source-batch/v1":
            msgspec.convert(content, type=BriefSourceBatchResult, strict=True)
        case "pinboard-mcp-brief-contract-result/v1":
            msgspec.convert(content, type=BriefContractRejected, strict=True)
        case "pinboard-mcp-brief-sources-result/v1":
            if content.get("status") == "committed-effect":
                msgspec.convert(content, type=BriefSourcesPublishedFailure, strict=True)
            else:
                msgspec.convert(content, type=BriefSourcesRejected, strict=True)
        case "pinboard-mcp-execution-result/v1":
            msgspec.convert(content, type=ExecutorBusyResult, strict=True)
        case unexpected:
            raise ValueError(f"Unsupported brief preparation result schema: {unexpected}")
    return content


def validate_result(tool_name: str, content: dict[str, JsonValue]) -> dict[str, JsonValue]:  # noqa: C901, PLR0912, PLR0915
    """Validate one emitted result against the exact alternative it claims."""
    if tool_name in {"pinboard_brief_contract", "pinboard_brief_sources"}:
        return validate_brief_preparation_result(content)
    schema = content.get("schema")
    status = content.get("status")
    code = content.get("code")
    surfaces = content.get("changed_surfaces")
    if schema == "pinboard-mcp-execution-result/v1":
        msgspec.convert(content, type=ExecutorBusyResult, strict=True)
    elif tool_name == "pinboard_order":
        msgspec.convert(content, type=OrderRejected if status == "rejected" else OrderCommitted, strict=True)
    elif tool_name == "pinboard_parallel_preview":
        msgspec.convert(
            content, type=ParallelPreviewRejected if status == "rejected" else ParallelPreviewSuccess, strict=True
        )
    elif tool_name == "pinboard_item_definition":
        if schema == "pinboard-item-definition/v1":
            msgspec.convert(content, type=query_models.ItemDefinition, strict=True)
        elif schema == "pinboard-item-definition-history/v1":
            msgspec.convert(content, type=query_models.ItemDefinitionHistory, strict=True)
        else:
            msgspec.convert(content, type=ItemDefinitionRejected, strict=True)
    elif tool_name == "pinboard_brief_review":
        if status == "no-needs-correction-evidence":
            msgspec.convert(content, type=BriefReviewNoEvidence, strict=True)
        elif status == "needs-correction":
            msgspec.convert(content, type=BriefReviewNeedsCorrection, strict=True)
        elif status == "committed":
            msgspec.convert(content, type=BriefReviewCommitted, strict=True)
        elif status == "unchanged":
            msgspec.convert(content, type=BriefReviewUnchanged, strict=True)
        elif status == "failed-after-publication":
            msgspec.convert(content, type=BriefReviewAcceptanceFailure, strict=True)
        elif surfaces == ["immutable-artifact"]:
            msgspec.convert(content, type=BriefReviewPublishedRejection, strict=True)
        else:
            msgspec.convert(content, type=BriefReviewRejected, strict=True)
    elif tool_name == "pinboard_dispatch":
        if status == "ready":
            result_type = DispatchReady
        elif status == "failed-after-publication":
            result_type = DispatchFailedAfterPublication
        elif code == "DISPATCH_INVALID":
            result_type = DispatchInvalid
        else:
            result_type = DispatchRejected
        msgspec.convert(content, type=result_type, strict=True)
    elif tool_name == "pinboard_review_job":
        if status == "ready":
            result_type = ReviewJobReady
        elif status == "failed-after-publication":
            result_type = ReviewJobFailedAfterPublication
        elif code == "REVIEW_JOB_INVALID":
            result_type = ReviewJobInvalid
        else:
            result_type = ReviewJobRejected
        msgspec.convert(content, type=result_type, strict=True)
    elif tool_name == "pinboard_transition":
        if status in {"committed", "committed-with-warning"}:
            result_type = TransitionCommitted
        elif status == "failed-after-publication":
            result_type = TransitionFailedAfterPublication
        else:
            result_type = TransitionRejected
        msgspec.convert(content, type=result_type, strict=True)
    elif tool_name == "pinboard_preparation_authority":
        if status == "present":
            result_type = PreparationAuthorityStatusPresent
        elif status == "absent":
            result_type = PreparationAuthorityStatusAbsent
        elif status in {"committed", "committed-with-warning"}:
            result_type = PreparationAuthorityCommitted
        else:
            result_type = PreparationAuthorityRejected
        msgspec.convert(content, type=result_type, strict=True)
    elif tool_name == "pinboard_attempt_authority":
        if status == "present":
            result_type = AttemptAuthorityStatusPresent
        elif status == "absent":
            result_type = AttemptAuthorityStatusAbsent
        elif status in {"committed", "committed-with-warning"}:
            result_type = AttemptAuthorityCommitted
        else:
            result_type = AttemptAuthorityRejected
        msgspec.convert(content, type=result_type, strict=True)
    elif schema == "pinboard-overview/v5" and tool_name == "pinboard_overview":
        msgspec.convert(content, type=query_models.WorkOverview, strict=True)
    elif tool_name == "pinboard_actions" and status == "ok":
        action_values = content.get("actions")
        focused_completion = (
            isinstance(action_values, list)
            and len(action_values) == 1
            and isinstance(action_values[0], dict)
            and isinstance(action_values[0].get("input_contract"), dict)
            and "checkpoint_packages" in action_values[0]["input_contract"]
        )
        if focused_completion:
            msgspec.convert(content, type=CompletionActionsSuccess, strict=True)
        else:
            msgspec.convert(content, type=ActionsSuccess, strict=True)
    elif tool_name == "pinboard_attempt_inspect" and status == "ok":
        continuation = content.get("continuation")
        state = continuation.get("state") if isinstance(continuation, dict) else None
        result_type = TerminalAttemptInspectionSuccess if state == "done" else NonterminalAttemptInspectionSuccess
        msgspec.convert(content, type=result_type, strict=True)
    elif tool_name == "pinboard_artifact_verify" and schema == "pinboard-verified-artifact-reference/v1":
        msgspec.convert(content, type=ArtifactVerified, strict=True)
    elif tool_name == "pinboard_overview":
        msgspec.convert(content, type=OverviewRejected, strict=True)
    elif tool_name == "pinboard_actions" and code == "ACTIONS_INVALID":
        msgspec.convert(content, type=ActionsInvalid, strict=True)
    elif tool_name == "pinboard_actions" and code == "ATTEMPT_LEASE_REQUIRED":
        msgspec.convert(content, type=AttemptLeaseRequired, strict=True)
    elif tool_name == "pinboard_actions":
        msgspec.convert(content, type=ActionUnavailable, strict=True)
    elif tool_name == "pinboard_attempt_inspect" and code == "ATTEMPT_INSPECT_INVALID":
        msgspec.convert(content, type=AttemptInspectInvalid, strict=True)
    elif tool_name == "pinboard_attempt_inspect" and code == "ATTEMPT_NOT_FOUND":
        msgspec.convert(content, type=AttemptNotFound, strict=True)
    elif tool_name == "pinboard_attempt_inspect" and code == "ATTEMPT_BRIEF_INVALID":
        msgspec.convert(content, type=AttemptBriefInvalid, strict=True)
    elif tool_name == "pinboard_attempt_inspect":
        msgspec.convert(content, type=AttemptActionUnavailable, strict=True)
    elif tool_name == "pinboard_artifact_verify" and code == "ARTIFACT_VERIFY_INVALID":
        msgspec.convert(content, type=ArtifactVerificationInvalid, strict=True)
    elif tool_name == "pinboard_artifact_verify" and code == "ARTIFACT_REFERENCE_MISMATCH":
        msgspec.convert(content, type=ArtifactReferenceMismatch, strict=True)
    elif tool_name == "pinboard_artifact_verify":
        msgspec.convert(content, type=ArtifactBytesInvalid, strict=True)
    elif schema == "pinboard-item-status/v1" and tool_name == "pinboard_item_status":
        msgspec.convert(content, type=query_models.ItemStatus, strict=True)
    elif tool_name == "pinboard_item_status" and code == "ITEM_STATUS_INVALID":
        msgspec.convert(content, type=ItemStatusInvalid, strict=True)
    elif tool_name == "pinboard_item_status" and code in {"ITEM_NOT_FOUND", "ITEM_DEFINITION_INVALID"}:
        msgspec.convert(content, type=ItemStatusUnavailable, strict=True)
    elif tool_name == "pinboard_item_status":
        msgspec.convert(content, type=ItemStatusInconsistent, strict=True)
    elif tool_name == "pinboard_proposal_create" and status == "committed":
        msgspec.convert(content, type=ProposalCommitted, strict=True)
    elif tool_name == "pinboard_proposal_create" and status == "committed-with-warning":
        msgspec.convert(content, type=ProposalCommittedWithWarning, strict=True)
    elif tool_name == "pinboard_proposal_create" and code == "PROPOSAL_ALREADY_EXISTS":
        msgspec.convert(content, type=ProposalDuplicate, strict=True)
    elif tool_name == "pinboard_proposal_create":
        msgspec.convert(content, type=ProposalRejected, strict=True)
    elif status == "failed-after-publication":
        msgspec.convert(content, type=BriefPublicationAcceptanceFailure, strict=True)
    elif status == "rejected" and surfaces == ["immutable-artifact"]:
        msgspec.convert(content, type=BriefPublishedRejection, strict=True)
    elif status == "rejected":
        msgspec.convert(content, type=BriefRejected, strict=True)
    elif status == "unchanged":
        msgspec.convert(content, type=BriefUnchanged, strict=True)
    elif status == "unchanged-with-warning":
        msgspec.convert(content, type=BriefUnchangedWithWarning, strict=True)
    elif status == "committed" and surfaces == ["immutable-artifact"]:
        msgspec.convert(content, type=BriefArtifactCommitted, strict=True)
    elif status == "committed" and surfaces == ["accepted-artifact-reference", "ledger"]:
        msgspec.convert(content, type=BriefReferenceCommitted, strict=True)
    elif status == "committed":
        msgspec.convert(content, type=BriefCommitted, strict=True)
    elif status == "committed-with-warning" and surfaces == ["immutable-artifact"]:
        msgspec.convert(content, type=BriefArtifactCommittedWithWarning, strict=True)
    elif status == "committed-with-warning" and surfaces == ["accepted-artifact-reference", "ledger"]:
        msgspec.convert(content, type=BriefReferenceCommittedWithWarning, strict=True)
    else:
        msgspec.convert(content, type=BriefCommittedWithWarning, strict=True)
    return content
