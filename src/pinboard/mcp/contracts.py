"""Strict request and correlated result contracts for the MCP transport."""

from collections.abc import Mapping
from typing import Annotated, Any, Literal, assert_never  # noqa: TID251 - validated against the selected action leaf

import msgspec

from pinboard import diagnostic_codes
from pinboard.adapters import dispatch_operations, review_operations
from pinboard.application import (
    action_models,
    brief_source_models,
    cancellation_models,
    decision_batches,
    dispatch_models,
    obligation_coverage,
    pr_reviews,
    proposal_models,
    query_models,
    work_brief_models,
)
from pinboard.domain import authority_models, decision_models, ordering
from pinboard.domain.errors import DecisionFailureCode, RetryDisposition
from pinboard.domain.history import DependencyPayload, Identity, RecordText

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
type IntegrationTarget = Annotated[
    str,
    msgspec.Meta(min_length=1, pattern=r"\A[^\r\n\x00" "\u2028\u2029" r"-][^\r\n\x00" "\u2028\u2029" r"]*\z"),
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


class PrReviewStatusRequest(
    msgspec.Struct, tag="status", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    item_id: PathComponent


class PrReviewActionsRequest(
    msgspec.Struct, tag="actions", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    item_id: PathComponent


class PrReviewStartRequest(msgspec.Struct, tag="start", tag_field="operation", frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    item_id: PathComponent
    expected_subject_revision: NonNegativeInt
    actor_task_id: RuntimeIdentity
    actor_host_id: RuntimeIdentity
    brief: pr_reviews.ReviewBrief


class PrReviewStartFileRequest(
    msgspec.Struct, tag="start-file", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    item_id: PathComponent
    expected_subject_revision: NonNegativeInt
    actor_task_id: RuntimeIdentity
    actor_host_id: RuntimeIdentity
    brief_path: RootPath
    brief_sha256: Sha256


class PrReviewObserveRequest(
    msgspec.Struct, tag="observe", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    item_id: PathComponent
    expected_subject_revision: NonNegativeInt
    actor_task_id: RuntimeIdentity
    actor_host_id: RuntimeIdentity
    observation: pr_reviews.HeadObservation


class PrReviewBriefReviewRequest(
    msgspec.Struct, tag="review-brief", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    item_id: PathComponent
    expected_subject_revision: NonNegativeInt
    actor_task_id: RuntimeIdentity
    actor_host_id: RuntimeIdentity
    brief_review: pr_reviews.BriefReview


class PrReviewBriefReviewFileRequest(
    msgspec.Struct, tag="review-brief-file", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    item_id: PathComponent
    expected_subject_revision: NonNegativeInt
    actor_task_id: RuntimeIdentity
    actor_host_id: RuntimeIdentity
    brief_review_path: RootPath
    brief_review_sha256: Sha256


class PrReviewRoundRequest(msgspec.Struct, tag="round", tag_field="operation", frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    item_id: PathComponent
    expected_subject_revision: NonNegativeInt
    actor_task_id: RuntimeIdentity
    actor_host_id: RuntimeIdentity
    round: pr_reviews.ReviewRound


class PrReviewRoundFileRequest(
    msgspec.Struct, tag="round-file", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    item_id: PathComponent
    expected_subject_revision: NonNegativeInt
    actor_task_id: RuntimeIdentity
    actor_host_id: RuntimeIdentity
    round_path: RootPath
    round_sha256: Sha256


class PrReviewCloseRequest(msgspec.Struct, tag="close", tag_field="operation", frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    item_id: PathComponent
    expected_subject_revision: NonNegativeInt
    actor_task_id: RuntimeIdentity
    actor_host_id: RuntimeIdentity
    close: pr_reviews.ReviewClose


type PrReviewRequest = (
    PrReviewStatusRequest
    | PrReviewActionsRequest
    | PrReviewStartRequest
    | PrReviewStartFileRequest
    | PrReviewBriefReviewRequest
    | PrReviewBriefReviewFileRequest
    | PrReviewObserveRequest
    | PrReviewRoundRequest
    | PrReviewRoundFileRequest
    | PrReviewCloseRequest
)


class PrReviewEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: PrReviewRequest


class PrReviewRoundView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: int
    round: pr_reviews.ReviewRound


class PrReviewSuccess(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-pr-review-status/v1"]
    status: Literal["ok", "committed", "committed-with-warning"]
    item_id: str
    item_state: str
    subject_revision: int
    project_revision: int
    brief_history_id: int | None
    brief: pr_reviews.ReviewBrief | None
    brief_review: pr_reviews.BriefReview | None
    rounds: tuple[PrReviewRoundView, ...]
    round_status: Literal["no-pr-round-completed", "reviewed-head-recorded"]
    latest_observation: pr_reviews.HeadObservation | None
    unreviewed_head: str | None
    close: pr_reviews.ReviewClose | None
    available_actions: tuple[Literal["start", "review-brief", "observe", "round", "close"], ...]
    remote_freshness: Literal["unverified"]
    state_changed: bool
    effect: Literal["committed", "unchanged"]
    retry: Literal["do-not-retry", "safe-to-repeat"]
    changed_surfaces: tuple[Literal["ledger"], ...]
    view_warning: str | None


class PrReviewRejected(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-pr-review-result/v1"]
    status: Literal["rejected"]
    code: str
    message: str
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input", "refresh-action", "do-not-retry"]
    changed_surfaces: tuple[()]


class _FixedStateChangedResult:
    @property
    def state_changed(self) -> bool:
        raise NotImplementedError

    def __post_init__(self) -> None:
        _require_fixed_state_changed(self)


class _ChangedResult(_FixedStateChangedResult):
    pass


class _UnchangedResult(_FixedStateChangedResult):
    pass


class _VariableStateChangedResult:
    pass


def _fixed_state_changed(result_type: type[Any]) -> bool | None:
    if issubclass(result_type, _ChangedResult):
        return True
    if issubclass(result_type, _UnchangedResult):
        return False
    return None


def _require_fixed_state_changed(result: _FixedStateChangedResult) -> None:
    expected = _fixed_state_changed(type(result))
    if expected is None:
        raise TypeError("A fixed result must declare changed or unchanged classification.")
    _require_state_changed(result.state_changed, expected)


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


class ItemStatusItemRequest(msgspec.Struct, tag="item", tag_field="operation", frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    item_id: PathComponent


class ItemStatusBranchRequest(
    msgspec.Struct, tag="branch", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    branch: action_models.NonEmptyLine


class ItemStatusIntegrationRequest(
    msgspec.Struct, tag="integration", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    item_id: PathComponent
    target: IntegrationTarget
    """A local branch, remote-tracking ref, tag, or full commit id; Pinboard never fetches it."""


class ItemStatusEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: ItemStatusItemRequest | ItemStatusBranchRequest | ItemStatusIntegrationRequest


class ProposalCreateRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    proposal: proposal_models.ProposalInput
    actor_task_id: RuntimeIdentity
    actor_host_id: RuntimeIdentity


class BriefPublishRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    brief: work_brief_models.WorkBrief


class BriefPublishFileRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    brief_path: RootPath
    brief_sha256: Sha256


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
    item_ids: Annotated[tuple[ordering.OrderWorkItemId, ...], msgspec.Meta(min_length=1)]

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


class BriefContractStarterEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: BriefContractStarterRequest


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
    request: BriefSourcesPlanRequest | BriefSourcesEmitRequest | BriefSourcesEmitFileRequest


class BriefSourcePlanOutputEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: BriefSourcesPlanToFileRequest


class StartupArtifactSource(msgspec.Struct, tag="artifact", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    artifact_ref_id: PositiveInt
    selector: NonEmptyText
    sha256: Sha256
    size_bytes: PositiveInt


class StartupFileSource(msgspec.Struct, tag="file", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    path: RootPath
    sha256: Sha256
    size_bytes: NonNegativeInt


type StartupSource = StartupArtifactSource | StartupFileSource
# An integer is a ceiling under the connected host profile; "host-profile" selects that profile's page size.
type StartupBudget = PositiveInt | Literal["host-profile"]


class StartupArtifactStart(
    msgspec.Struct, tag="artifact-start", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    source: StartupArtifactSource
    max_batch_bytes: StartupBudget


class StartupFileStart(
    msgspec.Struct, tag="file-start", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    path: RootPath
    max_batch_bytes: StartupBudget


class StartupContinue(msgspec.Struct, tag="continue", tag_field="operation", frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    source: StartupSource
    start_character: PositiveInt
    max_batch_bytes: StartupBudget


class StartupTextEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: StartupArtifactStart | StartupFileStart | StartupContinue


class StartupTextPage(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-startup-text-page/v1"]
    status: Literal["ok"]
    source: StartupSource
    start_character: NonNegativeInt
    end_character: NonNegativeInt
    text: str
    max_batch_bytes: PositiveInt
    continuation: StartupContinue | None
    authority: Literal["delivery-only"]
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        if self.end_character - self.start_character != len(self.text):
            raise ValueError("Character range must identify exactly the returned text.")
        if self.continuation is not None and (
            self.continuation.source != self.source
            or self.continuation.start_character != self.end_character
            or self.continuation.max_batch_bytes != self.max_batch_bytes
        ):
            raise ValueError("Continuation must preserve source, budget and next character.")


class StartupTextRejected(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-startup-text-result/v1"]
    status: Literal["rejected"]
    code: diagnostic_codes.StartupTextRejectedCode
    resource: NonEmptyText
    message: NonEmptyText
    recovery: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty


class StartupHostSettingFailed(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """The connected host's per-user page size could not be verified; no page was read.

    `setting_key` is the key the repair concerns: the offending entry's own key when stored content is
    invalid, which may belong to another host, otherwise the connected host's key. Effects report this
    call's own directory creation, file creation and seeding write in the user's host settings, separately
    from the unchanged Pinboard project state.
    """

    schema: Literal["pinboard-startup-text-result/v1"]
    status: Literal["rejected"]
    code: diagnostic_codes.StartupHostSettingFailedCode
    setting_file: NonEmptyText
    setting_key: NonEmptyText
    resource: NonEmptyText
    message: NonEmptyText
    recovery: NonEmptyText
    settings_directory_creation: Literal["none", "confirmed"]
    settings_file_creation: Literal["none", "confirmed", "unconfirmed"]
    settings_key_write: Literal["none", "acknowledged", "unconfirmed"]
    state_changed: bool | None
    effect: Literal["unchanged", "committed", "unconfirmed"]
    retry: Literal["correct-input", "retry-same-input"]
    changed_surfaces: tuple[Literal["user-settings"], ...]

    def __post_init__(self) -> None:
        confirmed = (
            self.settings_directory_creation == "confirmed"
            or self.settings_file_creation == "confirmed"
            or self.settings_key_write == "acknowledged"
        )
        unconfirmed = self.settings_file_creation == "unconfirmed" or self.settings_key_write == "unconfirmed"
        effect = "unconfirmed" if unconfirmed else "committed" if confirmed else "unchanged"
        if self.effect != effect or self.state_changed != (None if unconfirmed else confirmed):
            raise ValueError("Host-setting effect and state_changed must follow the reported settings effects.")
        if self.changed_surfaces != (("user-settings",) if confirmed else ()):
            raise ValueError("Host-setting changed surfaces must name user settings exactly when they changed.")


class FixtureSource(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    path: RootPath
    sha256: Sha256


class FixtureRepeatRecipe(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    repeat_text: NonEmptyText
    repeat_count: PositiveInt

    def __post_init__(self) -> None:
        self.repeat_text.encode("utf-8")


class FixtureReviewStartRequest(
    msgspec.Struct, tag="start", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    before: FixtureSource
    after: FixtureSource
    recipe: FixtureRepeatRecipe
    max_batch_bytes: Annotated[int, msgspec.Meta(ge=1, le=16_000)]


class FixtureReviewContinueRequest(
    msgspec.Struct, tag="continue", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    before: FixtureSource
    after: FixtureSource
    recipe: FixtureRepeatRecipe
    max_batch_bytes: Annotated[int, msgspec.Meta(ge=1, le=16_000)]
    view_sha256: Sha256
    start_character: PositiveInt


class FixtureReviewEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: FixtureReviewStartRequest | FixtureReviewContinueRequest


class FixtureReviewRejected(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-fixture-review-result/v1"]
    status: Literal["rejected"]
    code: diagnostic_codes.FixtureReviewRejectedCode
    resource: NonEmptyText
    message: NonEmptyText
    recovery: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty


class FixtureReviewPage(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-fixture-review-page/v1"]
    status: Literal["ok"]
    before: FixtureSource
    before_size_bytes: NonNegativeInt
    after: FixtureSource
    after_size_bytes: NonNegativeInt
    recipe: FixtureRepeatRecipe
    view_sha256: Sha256
    view_size_bytes: PositiveInt
    start_character: NonNegativeInt
    end_character: PositiveInt
    text: NonEmptyText
    max_batch_bytes: PositiveInt
    continuation: FixtureReviewContinueRequest | None
    authority: Literal["presentation-only"]
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.end_character != self.start_character + len(self.text):
            raise ValueError("Page character bounds must match the text")
        if len(msgspec.json.encode(self)) > self.max_batch_bytes:
            raise ValueError("The complete serialized page exceeds max_batch_bytes")
        if self.continuation is not None and (
            self.continuation.start_character != self.end_character
            or self.continuation.view_sha256 != self.view_sha256
            or self.continuation.before != self.before
            or self.continuation.after != self.after
            or self.continuation.recipe != self.recipe
            or self.continuation.max_batch_bytes != self.max_batch_bytes
        ):
            raise ValueError("Continuation must retain exact source, recipe and view binding")


class BriefContractRejected(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-contract-result/v1"]
    status: Literal["rejected"]
    code: diagnostic_codes.BriefContractRejectedCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty


class BriefSourcesRejected(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-sources-result/v1"]
    status: Literal["rejected"]
    code: diagnostic_codes.BriefSourcesRejectedCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty


class BriefSourceBatchResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-brief-source-batch/v1"]
    batch_index: NonNegativeInt
    content_byte_count: NonNegativeInt
    rendered_byte_count: PositiveInt
    presented_byte_count: PositiveInt
    text: NonEmptyText

    def __post_init__(self) -> None:
        if len(self.text.encode("utf-8")) != self.rendered_byte_count:
            raise ValueError("rendered_byte_count must equal the UTF-8 batch text size")
        if len(msgspec.json.encode(msgspec.to_builtins(self))) != self.presented_byte_count:
            raise ValueError("presented_byte_count must equal the JSON result size")


class BriefSourcePlanOutputResult(
    _VariableStateChangedResult, brief_source_models.BriefSourcePlanOutputReceipt, frozen=True
):
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


class BriefSourcesPublishedFailure(_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-sources-result/v1"]
    status: Literal["committed-effect"]
    code: diagnostic_codes.BriefSourcesPublishedFailureCode
    message: NonEmptyText
    destination: RootPath
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: tuple[Literal["selected-output"]]


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


class BriefReviewPublishFileRequest(
    msgspec.Struct, tag="publish-file", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    brief_artifact_ref_id: PositiveInt
    review_path: RootPath
    review_sha256: Sha256


class BriefReviewStatusRequest(
    msgspec.Struct, tag="status", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    brief_artifact_ref_id: PositiveInt


class BriefReviewEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: BriefReviewPublishRequest | BriefReviewPublishFileRequest | BriefReviewStatusRequest


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
    reconciliation: query_models.AttemptReconciliation | None


class CorrectionContextRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    attempt_id: PathComponent
    correction_history_id: PositiveInt


class HistoricalReturnSelection(
    msgspec.Struct, tag="historical", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    return_history_id: PositiveInt


class CurrentCandidateReturnSelection(
    msgspec.Struct, tag="current-candidate", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    return_history_id: PositiveInt


type ReturnSelection = HistoricalReturnSelection | CurrentCandidateReturnSelection


class ReturnContextRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    attempt_id: PathComponent
    selection: ReturnSelection


class CandidateObserveRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    attempt_id: PathComponent


class CandidateRestoreRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    attempt_id: PathComponent
    candidate: NonEmptyText


class ArtifactVerifyRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    artifact_ref_id: PositiveInt
    selector: NonEmptyText
    sha256: Sha256
    size_bytes: PositiveInt


class BriefLookupRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    attempt_id: PathComponent
    artifact_revision: PositiveInt


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


class ReuseCorrectionDispatchChoice(DispatchChoiceBase, tag="reuse-correction", tag_field="kind", frozen=True):
    brief_review: work_brief_models.ReusedCoverageCorrectionReview
    review_id: PathComponent
    correction_history_id: PositiveInt


class LocalCorrectionDispatchChoice(DispatchChoiceBase, tag="local-correction", tag_field="kind", frozen=True):
    brief_review: work_brief_models.LocalCorrectionSourceReview
    review_id: PathComponent
    correction_history_id: PositiveInt


class CandidateCorrectionDispatchChoice(DispatchChoiceBase, tag="candidate-correction", tag_field="kind", frozen=True):
    brief_review: work_brief_models.CurrentCorrectionReview
    review_id: PathComponent
    correction_history_id: PositiveInt


class ReplacementReadinessChoice(
    msgspec.Struct, tag="replacement-readiness", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    receipt: DispatchReceipt
    checkpoint_id: PathComponent
    brief_review: work_brief_models.CorrectionSourceReview
    current_candidate: NonEmptyText
    review_id: PathComponent
    correction_history_id: PositiveInt


class ReviewedFileDispatchChoice(DispatchChoiceBase, tag="reviewed-file", tag_field="kind", frozen=True):
    brief_review_path: RootPath
    brief_review_sha256: Sha256
    review_id: PathComponent


class CorrectionFileDispatchChoice(DispatchChoiceBase, tag="correction-file", tag_field="kind", frozen=True):
    brief_review_path: RootPath
    brief_review_sha256: Sha256
    review_id: PathComponent
    correction_history_id: PositiveInt


class ReuseCorrectionFileDispatchChoice(DispatchChoiceBase, tag="reuse-correction-file", tag_field="kind", frozen=True):
    brief_review_path: RootPath
    brief_review_sha256: Sha256
    review_id: PathComponent
    correction_history_id: PositiveInt


class LocalCorrectionFileDispatchChoice(DispatchChoiceBase, tag="local-correction-file", tag_field="kind", frozen=True):
    brief_review_path: RootPath
    brief_review_sha256: Sha256
    review_id: PathComponent
    correction_history_id: PositiveInt


class CandidateCorrectionFileDispatchChoice(
    DispatchChoiceBase, tag="candidate-correction-file", tag_field="kind", frozen=True
):
    brief_review_path: RootPath
    brief_review_sha256: Sha256
    review_id: PathComponent
    correction_history_id: PositiveInt


class ReplacementReadinessFileChoice(
    msgspec.Struct, tag="replacement-readiness-file", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    receipt: DispatchReceipt
    checkpoint_id: PathComponent
    brief_review_path: RootPath
    brief_review_sha256: Sha256
    current_candidate: NonEmptyText
    review_id: PathComponent
    correction_history_id: PositiveInt


type DispatchChoice = (
    OrdinaryDispatchChoice
    | ReviewedDispatchChoice
    | CorrectionDispatchChoice
    | ReuseCorrectionDispatchChoice
    | LocalCorrectionDispatchChoice
    | CandidateCorrectionDispatchChoice
    | ReplacementReadinessChoice
    | ReviewedFileDispatchChoice
    | CorrectionFileDispatchChoice
    | ReuseCorrectionFileDispatchChoice
    | LocalCorrectionFileDispatchChoice
    | CandidateCorrectionFileDispatchChoice
    | ReplacementReadinessFileChoice
)


class DispatchRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    dispatch: DispatchChoice


class ReviewChoiceBase(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: PathComponent
    candidate_revision: NonEmptyText
    runtime: dispatch_models.NativeRuntime
    background: bool


class InitialReviewChoice(ReviewChoiceBase, tag="initial", tag_field="kind", frozen=True):
    pass


class PackageInitialReviewChoice(ReviewChoiceBase, tag="package-initial", tag_field="kind", frozen=True):
    checkpoint_history_id: PositiveInt


class CorrectionReviewChoice(ReviewChoiceBase, tag="correction", tag_field="kind", frozen=True):
    correction_history_id: PositiveInt


class PackageCorrectionReviewChoice(ReviewChoiceBase, tag="package-correction", tag_field="kind", frozen=True):
    checkpoint_history_id: PositiveInt
    correction_history_id: PositiveInt


class RecordReadyReviewChoice(
    msgspec.Struct, tag="record-ready", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    attempt_id: PathComponent
    candidate_revision: NonEmptyText
    candidate_snapshot_sha256: Sha256
    accepted_brief_sha256: Sha256
    result_sha256: Sha256
    review_sha256: Sha256
    reviewer_prompt_sha256: Sha256
    reviewer_task_id: RuntimeIdentity
    verdict: Literal["ready"]
    acceptance_evidence: NonEmptyText


type ReviewLaunchChoice = (
    InitialReviewChoice | PackageInitialReviewChoice | CorrectionReviewChoice | PackageCorrectionReviewChoice
)
type ReviewChoice = ReviewLaunchChoice | RecordReadyReviewChoice


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
type ActivateTransitionRequest = PreparerTransitionRequest[Literal["activate"], action_models.ActivateInputPayload]
type BlockTransitionRequest = ProjectTransitionRequest[Literal["block"], action_models.BlockInputPayload]
type BlockItemTransitionRequest = ProjectTransitionRequest[Literal["block-item"], action_models.BlockInputPayload]
type DirectCompleteTransitionRequest = ProjectTransitionRequest[Literal["complete"], action_models.EvidenceInputPayload]
type ReviewedCompleteTransitionRequest = ProjectTransitionRequest[
    Literal["complete"], action_models.ReviewedCompleteInputPayload
]
type DeferTransitionRequest = ProjectTransitionRequest[Literal["defer"], action_models.DeferInputPayload]
type MergeProposalTransitionRequest = ProjectTransitionRequest[
    Literal["merge-proposal"], action_models.MergeProposalInputPayload
]
type PauseTransitionRequest = ProjectTransitionRequest[Literal["pause"], action_models.ReasonInputPayload]
type WorkerPauseTransitionRequest = WorkerTransitionRequest[Literal["pause"], action_models.ReasonInputPayload]
type DecisionPauseTransitionRequest = ProjectTransitionRequest[Literal["pause"], decision_batches.PauseDecisionsInput]
type WorkerDecisionPauseTransitionRequest = WorkerTransitionRequest[
    Literal["pause"], decision_batches.PauseDecisionsInput
]
type RejectProposalTransitionRequest = ProjectTransitionRequest[
    Literal["reject-proposal"], action_models.RejectProposalInputPayload
]
type ReopenTransitionRequest = ProjectTransitionRequest[Literal["reopen"], action_models.EvidenceInputPayload]
type RecordReplacementTransitionRequest = ProjectTransitionRequest[
    Literal["record-replacement"], action_models.RecordPlannedReplacementInputPayload
]
type RebindAttemptTransitionRequest = ProjectTransitionRequest[
    Literal["rebind-attempt"], action_models.RebindAttemptInputPayload
]
type ResumeTransitionRequest = ProjectTransitionRequest[Literal["resume"], action_models.ResumeInputPayload]
type ResolveDecisionsTransitionRequest = ProjectTransitionRequest[
    Literal["resume"], decision_batches.ResolveDecisionsInput
]
type ReturnForCorrectionTransitionRequest = ProjectTransitionRequest[
    Literal["return-for-correction"], action_models.ReasonInputPayload
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
type DeclaredSubmitReviewTransitionRequest = WorkerTransitionRequest[
    Literal["submit-review"], action_models.DeclaredSubmitReviewInputPayload
]

type TransitionRequest = (
    AcceptCheckpointTransitionRequest
    | AcceptReviewAndContinueTransitionRequest
    | ActivateTransitionRequest
    | BlockTransitionRequest
    | BlockItemTransitionRequest
    | DirectCompleteTransitionRequest
    | ReviewedCompleteTransitionRequest
    | DeferTransitionRequest
    | MergeProposalTransitionRequest
    | PauseTransitionRequest
    | WorkerPauseTransitionRequest
    | DecisionPauseTransitionRequest
    | WorkerDecisionPauseTransitionRequest
    | RejectProposalTransitionRequest
    | ReopenTransitionRequest
    | RecordReplacementTransitionRequest
    | RebindAttemptTransitionRequest
    | ResumeTransitionRequest
    | ResolveDecisionsTransitionRequest
    | ReturnForCorrectionTransitionRequest
    | RetainTemporarilyTransitionRequest
    | ReviseItemTransitionRequest
    | SubmitReviewTransitionRequest
    | DeclaredSubmitReviewTransitionRequest
)


class TransitionEnvelope[RequestT](msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: RequestT


class CloseRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """The pinboard_close tool's ordinary top-level arguments; close has one project role and no wrapper."""

    project_root: RootPath
    work_root: RootPath
    receipt: TransitionReceipt[Literal["close"]]
    payload: action_models.CloseInputPayload
    actor_task_id: RuntimeIdentity
    actor_host_id: RuntimeIdentity


def decode_transition_request(raw: Mapping[str, JsonValue]) -> TransitionRequest:  # noqa: C901, PLR0912, PLR0915 - exhaustive exact wire leaves
    """Decode one strict envelope and exact leaf before resources or effects.

    Transition leaves share role tags, so receipt action identity selects the
    leaf. Complete additionally distinguishes its independently required payload
    shapes. These relational choices cannot be an ordinary tagged union. Close
    is not a leaf: it has its own human-confirmed pinboard_close request.
    """

    inner = raw.get("request")
    receipt = inner.get("receipt") if isinstance(inner, dict) else None
    if isinstance(inner, dict) and not isinstance(receipt, dict):
        if "action" in inner:
            raise ValueError(
                "Transition request supplied 'request.action' instead of 'request.receipt'; no action identity was "
                "decoded (placeholder action_id 'inspect'/'invalid'). Pass request.receipt: "
                "{'action_id': {'kind': '<action>', 'subject': '<id>'}, 'subject_revision': '<revision>'} "
                "containing only action_id and subject_revision from pinboard_actions, not the whole action."
            )
        if "action_id" in inner or "subject_revision" in inner:
            misplaced = ", ".join(f"request.{key}" for key in ("action_id", "subject_revision") if key in inner)
            raise ValueError(
                f"Transition request placed receipt fields at the top of request ({misplaced}) instead of inside "
                "'request.receipt'; no action identity was decoded (placeholder action_id 'inspect'/'invalid'). "
                "Pass request.receipt: {'action_id': {'kind': '<action>', 'subject': '<id>'}, "
                "'subject_revision': '<revision>'} containing only action_id and subject_revision from pinboard_actions."
            )
        raise ValueError(
            "Transition request is missing 'request.receipt'; no action identity was decoded (placeholder action_id "
            "'inspect'/'invalid'). Pass request.receipt: {'action_id': {'kind': '<action>', 'subject': '<id>'}, "
            "'subject_revision': '<revision>'} containing only action_id and subject_revision from pinboard_actions."
        )
    action_id = receipt.get("action_id") if isinstance(receipt, dict) else None
    kind = action_id.get("kind") if isinstance(action_id, dict) else None
    if isinstance(receipt, dict) and (not isinstance(action_id, dict) or not isinstance(kind, str)):
        raise ValueError(
            "Transition request 'request.receipt' is missing a valid 'action_id'; no action identity was decoded "
            "(placeholder action_id 'inspect'/'invalid'). Pass request.receipt: {'action_id': {'kind': '<action>', "
            "'subject': '<id>'}, 'subject_revision': '<revision>'} containing only action_id and subject_revision "
            "from pinboard_actions."
        )
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
        case "activate":
            request = msgspec.convert(raw, type=TransitionEnvelope[ActivateTransitionRequest], strict=True).request
        case "block":
            request = msgspec.convert(raw, type=TransitionEnvelope[BlockTransitionRequest], strict=True).request
        case "block-item":
            request = msgspec.convert(raw, type=TransitionEnvelope[BlockItemTransitionRequest], strict=True).request
        case "complete":
            payload = inner.get("payload") if isinstance(inner, dict) else None
            schema = payload.get("schema") if isinstance(payload, dict) else None
            if schema is not None:
                request = msgspec.convert(
                    raw, type=TransitionEnvelope[ReviewedCompleteTransitionRequest], strict=True
                ).request
            else:
                request = msgspec.convert(
                    raw, type=TransitionEnvelope[DirectCompleteTransitionRequest], strict=True
                ).request
        case "defer":
            request = msgspec.convert(raw, type=TransitionEnvelope[DeferTransitionRequest], strict=True).request
        case "merge-proposal":
            request = msgspec.convert(raw, type=TransitionEnvelope[MergeProposalTransitionRequest], strict=True).request
        case "pause":
            payload = inner.get("payload") if isinstance(inner, dict) else None
            schema = payload.get("schema") if isinstance(payload, dict) else None
            role = inner.get("role") if isinstance(inner, dict) else None
            if schema is not None:
                if role == "worker":
                    request = msgspec.convert(
                        raw, type=TransitionEnvelope[WorkerDecisionPauseTransitionRequest], strict=True
                    ).request
                else:
                    request = msgspec.convert(
                        raw, type=TransitionEnvelope[DecisionPauseTransitionRequest], strict=True
                    ).request
            elif role == "worker":
                request = msgspec.convert(
                    raw, type=TransitionEnvelope[WorkerPauseTransitionRequest], strict=True
                ).request
            else:
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
            payload = inner.get("payload") if isinstance(inner, dict) else None
            schema = payload.get("schema") if isinstance(payload, dict) else None
            if schema is not None:
                request = msgspec.convert(
                    raw, type=TransitionEnvelope[ResolveDecisionsTransitionRequest], strict=True
                ).request
            else:
                request = msgspec.convert(raw, type=TransitionEnvelope[ResumeTransitionRequest], strict=True).request
        case "return-for-correction":
            request = msgspec.convert(
                raw, type=TransitionEnvelope[ReturnForCorrectionTransitionRequest], strict=True
            ).request
        case "retain-temporarily":
            request = msgspec.convert(
                raw, type=TransitionEnvelope[RetainTemporarilyTransitionRequest], strict=True
            ).request
        case "revise-item":
            request = msgspec.convert(raw, type=TransitionEnvelope[ReviseItemTransitionRequest], strict=True).request
        case "submit-review":
            payload = inner.get("payload") if isinstance(inner, dict) else None
            schema = payload.get("schema") if isinstance(payload, dict) else None
            if schema is None:
                request = msgspec.convert(
                    raw, type=TransitionEnvelope[SubmitReviewTransitionRequest], strict=True
                ).request
            else:
                request = msgspec.convert(
                    raw, type=TransitionEnvelope[DeclaredSubmitReviewTransitionRequest], strict=True
                ).request
        case _:
            raise ValueError(f"Action '{kind}' is not a supported MCP transition.")
    return request


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


class ItemStatusInvalid(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-item-status-result/v3"]
    status: Literal["rejected"]
    code: diagnostic_codes.ItemStatusInvalidCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty


class ItemStatusUnavailable(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-item-status-result/v3"]
    status: Literal["rejected"]
    code: diagnostic_codes.ItemStatusUnavailableCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty


class ItemStatusInconsistent(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-item-status-result/v3"]
    status: Literal["rejected"]
    code: diagnostic_codes.ItemStatusInconsistentCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["do-not-retry"]
    changed_surfaces: Empty
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=1)]
    mismatches: Annotated[tuple[FailureMismatch, ...], msgspec.Meta(min_length=1)]


class BranchOwnerNotFound(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-item-status-result/v3"]
    status: Literal["rejected"]
    code: diagnostic_codes.BranchOwnerNotFoundCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=2)]
    mismatches: Empty
    recovery: NonEmptyText


class _DamagedEvidenceResult(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """An unchanged, do-not-retry rejection naming damaged stored evidence, with a diagnosis-only recovery."""

    status: Literal["rejected"]
    code: NonEmptyText
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["do-not-retry"]
    changed_surfaces: Empty
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=4)]
    mismatches: Annotated[tuple[FailureMismatch, ...], msgspec.Meta(min_length=1)]
    recovery: NonEmptyText


class DamagedReceiptResult(_DamagedEvidenceResult, frozen=True):
    code: diagnostic_codes.DamagedReceiptResultCode


class ItemStatusReceiptDamaged(DamagedReceiptResult, frozen=True):
    schema: Literal["pinboard-mcp-item-status-result/v3"]


class IntegrationTargetUnresolved(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-item-status-result/v3"]
    status: Literal["rejected"]
    code: diagnostic_codes.IntegrationTargetUnresolvedCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=2)]
    mismatches: Empty
    recovery: NonEmptyText


class IntegrationCandidateUnavailable(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-item-status-result/v3"]
    status: Literal["rejected"]
    code: diagnostic_codes.IntegrationCandidateUnavailableCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=4)]
    mismatches: Empty
    recovery: NonEmptyText


class IntegrationCandidateEvidenceInvalid(_DamagedEvidenceResult, frozen=True):
    code: diagnostic_codes.IntegrationCandidateEvidenceInvalidCode
    schema: Literal["pinboard-mcp-item-status-result/v3"]


class ItemStatusGitUnavailable(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-item-status-result/v3"]
    status: Literal["rejected"]
    code: diagnostic_codes.ItemStatusGitCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=1)]
    mismatches: Empty
    recovery: NonEmptyText


class OverviewRejected(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-overview-result/v1"]
    status: Literal["rejected"]
    code: diagnostic_codes.OverviewRejectedCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty


class RejectedReadResult(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    status: Literal["rejected"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    changed_surfaces: Empty


class ActionsInvalid(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-actions-result/v1"]
    code: diagnostic_codes.ActionsInvalidCode
    retry: Literal["correct-input"]
    observed: Empty
    mismatches: Empty


class ItemDefinitionRejected(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-item-definition-result/v1"]
    code: diagnostic_codes.ItemDefinitionRejectedCode
    retry: Literal["correct-input"]
    observed: Empty
    mismatches: Empty


class ActionUnavailable(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-actions-result/v1"]
    code: diagnostic_codes.ActionUnavailableCode
    retry: Literal["refresh-action"]
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]


class AttemptLeaseRequired(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-actions-result/v1"]
    code: diagnostic_codes.AttemptLeaseRequiredCode
    retry: Literal["reacquire-authority"]
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=1)]
    mismatches: Annotated[tuple[FailureMismatch, ...], msgspec.Meta(min_length=1)]


class AttemptInspectInvalid(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-attempt-inspection-result/v1"]
    code: diagnostic_codes.AttemptInspectInvalidCode
    retry: Literal["correct-input"]
    observed: Empty
    mismatches: Empty


class AttemptNotFound(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-attempt-inspection-result/v1"]
    code: diagnostic_codes.AttemptNotFoundCode
    retry: Literal["correct-input"]
    observed: Empty
    mismatches: Empty


class AttemptBriefInvalid(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-attempt-inspection-result/v1"]
    code: diagnostic_codes.AttemptBriefInvalidCode
    retry: Literal["do-not-retry"]
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=1)]
    mismatches: Annotated[tuple[FailureMismatch, ...], msgspec.Meta(min_length=1)]


class AttemptReceiptDamaged(DamagedReceiptResult, frozen=True):
    schema: Literal["pinboard-mcp-attempt-inspection-result/v1"]


class AttemptActionUnavailable(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-attempt-inspection-result/v1"]
    code: diagnostic_codes.ActionUnavailableCode
    retry: Literal["refresh-action"]
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]


class ArtifactVerificationInvalid(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-artifact-verification-result/v1"]
    code: diagnostic_codes.ArtifactVerificationInvalidCode
    retry: Literal["correct-input"]
    observed: Empty
    mismatches: Empty


class ArtifactReferenceMismatch(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-artifact-verification-result/v1"]
    code: diagnostic_codes.ArtifactReferenceMismatchCode
    retry: Literal["correct-input"]
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=1)]
    mismatches: Annotated[tuple[FailureMismatch, ...], msgspec.Meta(min_length=1)]


class ArtifactBytesInvalid(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-artifact-verification-result/v1"]
    code: diagnostic_codes.ArtifactBytesInvalidCode
    retry: Literal["do-not-retry"]
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=1)]
    mismatches: Annotated[tuple[FailureMismatch, ...], msgspec.Meta(min_length=1)]


class BriefLookupInvalid(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-brief-lookup-result/v1"]
    code: diagnostic_codes.BriefLookupInvalidCode
    retry: Literal["correct-input"]
    observed: Empty
    mismatches: Empty


class BriefNotFound(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-brief-lookup-result/v1"]
    code: diagnostic_codes.BriefNotFoundCode
    retry: Literal["correct-input"]
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=1)]
    mismatches: Annotated[tuple[FailureMismatch, ...], msgspec.Meta(min_length=1)]


class NoPayloadInputContract(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Decode an omitted lifecycle schema before constructing the exact internal record."""

    action_kind: decision_models.ActionKind
    semantics: action_models.ActionSemanticsView


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


class ActionsSuccess(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-actions-result/v1"]
    status: Literal["ok"]
    actions: tuple[ActionView, ...]
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty


class CompletionActionView(ActionView, frozen=True, forbid_unknown_fields=True):
    input_contract: action_models.CompletionInputContractView


class CompletionActionsSuccess(ActionsSuccess, frozen=True, forbid_unknown_fields=True):
    actions: Annotated[tuple[CompletionActionView, ...], msgspec.Meta(min_length=1, max_length=1)]


class CloseActionView(ActionView, frozen=True, forbid_unknown_fields=True):
    replacement_coverage: obligation_coverage.CoverageReport | None


class CloseActionsSuccess(ActionsSuccess, frozen=True, forbid_unknown_fields=True):
    actions: Annotated[tuple[CloseActionView, ...], msgspec.Meta(min_length=1, max_length=1)]


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


class CandidateReviewAbsent(msgspec.Struct, tag="absent", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    pass


class CandidateReviewPresent(msgspec.Struct, tag="present", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    artifact_ref_id: PositiveInt
    selector: NonEmptyText
    sha256: Sha256
    size_bytes: PositiveInt
    accepted_revision: PositiveInt


type CandidateReviewReference = CandidateReviewAbsent | CandidateReviewPresent


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


class ContinuationReconcileRepository(
    msgspec.Struct, tag="reconcile-repository", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    candidate_revision: NonEmptyText
    condition: NonEmptyText


class ContinuationDependencies(
    msgspec.Struct, tag="wait-for-dependencies", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    dependencies: Annotated[tuple[DependencyPayload, ...], msgspec.Meta(min_length=1)]

    def __post_init__(self) -> None:
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("dependency continuations require unique dependencies")


class ContinuationRefreshTarget(
    msgspec.Struct, tag="refresh-target", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    target_revision: NonEmptyText


class ContinuationPermissionRecovery(
    msgspec.Struct, tag="permission-recovery", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    target_revision: NonEmptyText
    effect: query_models.RuntimeEffect
    status: query_models.RuntimeEffectStatus

    def __post_init__(self) -> None:
        if self.status not in (query_models.RuntimeEffectStatus.DENIED, query_models.RuntimeEffectStatus.UNKNOWN):
            raise ValueError("permission recovery requires a denied or unknown runtime effect")


class ContinuationRepositoryDisposition(
    msgspec.Struct, tag="repository-disposition", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    target_revision: NonEmptyText
    relation: query_models.IntegrationRelation

    def __post_init__(self) -> None:
        if self.relation not in (
            query_models.IntegrationRelation.CANDIDATE_PENDING_ON_ACCEPTED_BASE,
            query_models.IntegrationRelation.CANDIDATE_PENDING_ON_SQUASH_EQUIVALENT_BASE,
        ):
            raise ValueError("repository disposition requires a pending candidate relation")


class ContinuationCommitThenReinspect(
    msgspec.Struct, tag="commit-then-reinspect", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    target_revision: NonEmptyText
    relation: query_models.IntegrationRelation

    def __post_init__(self) -> None:
        if self.relation not in (
            query_models.IntegrationRelation.CANDIDATE_PENDING_ON_ACCEPTED_BASE,
            query_models.IntegrationRelation.CANDIDATE_PENDING_ON_SQUASH_EQUIVALENT_BASE,
        ):
            raise ValueError("commit then reinspect requires a pending candidate relation")


class ContinuationRepositoryCleanup(
    msgspec.Struct, tag="repository-cleanup", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    target_revision: NonEmptyText


type ReconciliationContinuation = (
    ContinuationRefreshTarget
    | ContinuationPermissionRecovery
    | ContinuationRepositoryDisposition
    | ContinuationCommitThenReinspect
    | ContinuationRepositoryCleanup
)
type ContinuationOperation = (
    ContinuationAction
    | ContinuationReview
    | ContinuationReconcileRepository
    | ContinuationDependencies
    | ReconciliationContinuation
)


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
)
_BLOCKED_CONTINUATION_ACTION_KINDS = (
    decision_models.ActionKind.RECORD_REPLACEMENT,
    decision_models.ActionKind.RETAIN_TEMPORARILY,
    decision_models.ActionKind.REVISE_ITEM,
    decision_models.ActionKind.RESUME,
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


class CancelledAttemptContinuation(
    TerminalAttemptContinuation, tag="dropped", tag_field="state", frozen=True, forbid_unknown_fields=True
):
    """Dropped parent disposition for a terminal retained attempt."""


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
    decision_batch: decision_batches.DecisionBatchView | None

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
            expected = (
                RelativeActionIdentity("attempt", decision_models.ActionKind.ACCEPT_CHECKPOINT),
                RelativeActionIdentity("attempt", decision_models.ActionKind.COMPLETE),
            )
            if not any(action in self.legal_actions for action in expected):
                raise ValueError("review continuation requires a matching checkpoint or completion action")

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
            decision_models.ActionKind.REBIND_ATTEMPT,
            decision_models.ActionKind.PAUSE,
            decision_models.ActionKind.COMPLETE,
        ):
            raise ValueError("an active continuation requires a continue, rebind, pause, or completion action")


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
            isinstance(
                operation,
                (
                    ContinuationReview,
                    ContinuationReconcileRepository,
                    ContinuationRefreshTarget,
                    ContinuationPermissionRecovery,
                    ContinuationRepositoryDisposition,
                    ContinuationCommitThenReinspect,
                    ContinuationRepositoryCleanup,
                ),
            )
            or (
                isinstance(operation, ContinuationAction)
                and operation.action.action_kind
                in (decision_models.ActionKind.COMPLETE, decision_models.ActionKind.RETURN_FOR_CORRECTION)
            )
        ):
            raise ValueError("a review continuation requires review, completion, or correction work")


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
    pause_reason: NonEmptyText | None

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
type AttemptContinuation = TerminalAttemptContinuation | CancelledAttemptContinuation | NonterminalAttemptContinuation


class CandidateRecoveryAbsent(msgspec.Struct, tag="absent", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    pass


class CandidateRestoreArguments(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: None
    work_root: RootPath
    attempt_id: PathComponent
    candidate: NonEmptyText


class CandidateRestoreInvocation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    tool: Literal["pinboard_candidate_restore"]
    arguments: CandidateRestoreArguments
    unresolved_fields: tuple[Literal["project_root"]]


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
    restore: CandidateRestoreInvocation

    def __post_init__(self) -> None:
        if self.candidate != self.restore.arguments.candidate:
            raise ValueError("Recovery must retain its inspected candidate binding.")


type CandidateRecovery = CandidateRecoveryAbsent | CandidateRecoveryPresent


class TerminalAttemptInspectionSuccess(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-attempt-inspection-result/v1"]
    status: Literal["ok"]
    continuation: TerminalAttemptContinuation | CancelledAttemptContinuation
    candidate_recovery: CandidateRecovery
    accepted_brief: None
    result: EvidenceReference
    review: EvidenceReference
    blocker: EvidenceReference
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty


class NonterminalAttemptInspectionSuccess(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-attempt-inspection-result/v1"]
    status: Literal["ok"]
    continuation: NonterminalAttemptContinuation
    candidate_recovery: CandidateRecovery
    candidate_review: CandidateReviewReference
    accepted_brief: AcceptedBriefIdentity
    result: EvidenceReference
    review: EvidenceReference
    blocker: EvidenceReference
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty


class ReviewEvidenceReference(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    artifact_ref_id: PositiveInt
    kind: Literal["evidence"]
    key: NonEmptyText
    revision: PositiveInt
    selector: NonEmptyText
    sha256: Sha256
    size_bytes: PositiveInt
    accepted_revision: PositiveInt


class CorrectionSnapshotMetadata(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    snapshot_schema: Literal[
        "pinboard-candidate-snapshot/v1", "pinboard-candidate-snapshot/v2", "pinboard-candidate-snapshot/v3"
    ]
    candidate_kind: Literal["working-tree", "commit"]
    attempt_id: NonEmptyText
    item_id: NonEmptyText
    candidate: NonEmptyText
    branch: NonEmptyText
    preimage_revision: NonEmptyText
    accepted_base_revision: NonEmptyText
    recorded_at: NonEmptyText
    diff_sha256: Sha256
    diff_size_bytes: NonNegativeInt
    excluded_untracked_paths: tuple[action_models.CanonicalRepoRelativeFile, ...]

    def __post_init__(self) -> None:
        action_models.require_canonical_excluded_paths(self.excluded_untracked_paths)


class CorrectionReviewEvidence(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: PathComponent
    correction_history_id: PositiveInt
    correction_reason: NonEmptyText
    effective_brief: work_brief_models.WorkBrief
    effective_brief_sha256: Sha256
    checkpoint_sha256: Sha256
    reviewed_authority_set_sha256: Sha256 | None
    starting_candidate: work_brief_models.PortableArtifactIdentity
    starting_candidate_reference: ReviewEvidenceReference
    starting_candidate_verification: ArtifactVerifyRequest
    starting_snapshot_metadata: CorrectionSnapshotMetadata
    reuse_eligible: bool
    reuse_blockers: tuple[NonEmptyText, ...]

    def __post_init__(self) -> None:
        if self.reuse_eligible == bool(self.reuse_blockers):
            raise ValueError("Correction reuse eligibility and blockers disagree.")


class CorrectionContextReady(_UnchangedResult, CorrectionReviewEvidence, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-correction-context/v2"]
    status: Literal["ready"]
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        super().__post_init__()
        CorrectionReviewEvidence.__post_init__(self)


class CorrectionContextRejected(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-correction-context/v2"]
    code: NonEmptyText
    retry: Literal["correct-input", "refresh-action", "do-not-retry"]
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]


class ReturnReceiptEvidence(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: PositiveInt
    project_revision: PositiveInt
    action_id: NonEmptyText
    action_kind: Literal["return-for-correction"]
    subject_id: PathComponent
    artifact_ref_id: None
    authorization: Literal["project"]
    input_schema: Literal["return-for-correction/v1"]
    input_payload: NonEmptyText
    outcome_schema: Literal["transition-receipt/v1"]
    outcome_payload: NonEmptyText
    actor_task_id: NonEmptyText | None
    actor_host_id: NonEmptyText | None
    committed_at: NonEmptyText


class HistoricalReturnContext(
    msgspec.Struct, tag="historical", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    return_receipt: ReturnReceiptEvidence
    correction_reason: NonEmptyText
    starting_candidate: work_brief_models.PortableArtifactIdentity
    starting_candidate_reference: ReviewEvidenceReference
    starting_candidate_verification: ArtifactVerifyRequest
    starting_snapshot_metadata: CorrectionSnapshotMetadata


class CurrentCandidateReturnContext(
    msgspec.Struct, tag="current-candidate", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    return_receipt: ReturnReceiptEvidence
    review_context: CorrectionReviewEvidence
    current_candidate: work_brief_models.CorrectionCandidateBinding


class ReturnContextReady(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-return-context/v2"]
    status: Literal["ready"]
    attempt_id: PathComponent
    context: HistoricalReturnContext | CurrentCandidateReturnContext
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty


class ReturnContextRejected(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-return-context/v2"]
    code: NonEmptyText
    retry: Literal["correct-input", "refresh-action", "do-not-retry"]
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]


class ArtifactVerified(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
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
        _require_fixed_state_changed(self)
        if not self.verified:
            raise ValueError("verified must be true for a verified artifact result.")


class SchemaMismatch(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    database_path: RootPath
    observed_schema: NonEmptyText
    required_schema: NonEmptyText
    preview_arguments: (
        tuple[
            Literal["--project-root"],
            RootPath,
            Literal["--work-root"],
            RootPath,
            Literal["migrate-schema"],
            Literal["--json"],
        ]
        | None
    )


class TracePreflightEffects(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Auxiliary effects before the callback, independent of its ledger effect."""

    settings_parent_creation: Literal["none", "unconfirmed"]
    settings_file_creation: Literal["none", "confirmed", "unconfirmed"]
    settings_mode_write: Literal["none", "acknowledged", "unconfirmed"]
    trace_directory_creation: Literal["none", "confirmed"]


class SchemaUnsupportedResult(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Ledger and accepted evidence are unchanged; trace preflight effects are separate."""

    schema: Literal["pinboard-mcp-schema-result/v1"]
    status: Literal["rejected"]
    code: Literal["SCHEMA_UNSUPPORTED"]
    message: NonEmptyText
    resource: RootPath
    repair: NonEmptyText
    schema_mismatch: SchemaMismatch
    trace_preflight_effects: TracePreflightEffects
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["do-not-retry"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        _require_fixed_state_changed(self)


class TracePreflightResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-execution-result/v1"]
    status: Literal["rejected"]
    code: diagnostic_codes.TracePreflightResultCode
    message: NonEmptyText
    resource: NonEmptyText
    repair: NonEmptyText
    schema_mismatch: SchemaMismatch | None
    target_ran: bool
    state_changed: bool | None
    effect: Literal["unchanged", "committed", "unconfirmed"]
    retry: Literal["correct-input", "retry-same-input"]
    changed_surfaces: tuple[Literal["work-root"], ...]
    settings_parent_creation: Literal["none", "unconfirmed"]
    settings_file_creation: Literal["none", "confirmed", "unconfirmed"]
    settings_mode_write: Literal["none", "acknowledged", "unconfirmed"]
    trace_directory_creation: Literal["none", "confirmed", "unconfirmed"]
    capture_probe_effect: Literal["none", "unconfirmed"]

    def __post_init__(self) -> None:
        if self.target_ran:
            raise ValueError("Trace preflight must precede its target callback.")
        confirmed = (
            self.settings_file_creation == "confirmed"
            or self.settings_mode_write == "acknowledged"
            or self.trace_directory_creation == "confirmed"
        )
        unconfirmed = (
            self.settings_parent_creation == "unconfirmed"
            or self.settings_file_creation == "unconfirmed"
            or self.settings_mode_write == "unconfirmed"
            or self.trace_directory_creation == "unconfirmed"
            or self.capture_probe_effect == "unconfirmed"
        )
        expected = {
            "unchanged": (False, ()),
            "committed": (True, ("work-root",)),
            "unconfirmed": (None, ("work-root",) if confirmed else ()),
        }
        if self.effect != ("unconfirmed" if unconfirmed else "committed" if confirmed else "unchanged"):
            raise ValueError("Trace preflight effect disagrees with its auxiliary effects.")
        if (self.state_changed, self.changed_surfaces) != expected[self.effect]:
            raise ValueError("Trace preflight effect and changed surfaces disagree.")


class ExecutorBusyResult(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-execution-result/v1"]
    status: Literal["busy"]
    code: diagnostic_codes.ExecutorBusyResultCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["retry-same-input"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty


PR_REVIEW_RESULT_TYPES = (PrReviewSuccess, PrReviewRejected, ExecutorBusyResult)
CORRECTION_CONTEXT_RESULT_TYPES = (CorrectionContextReady, CorrectionContextRejected, ExecutorBusyResult)
RETURN_CONTEXT_RESULT_TYPES = (ReturnContextReady, ReturnContextRejected, ExecutorBusyResult)


class WarningResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    message: NonEmptyText
    recovery: NonEmptyText


class OrderRecovery(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    tool: Literal["pinboard_overview"]
    arguments: OverviewRequest
    meaning: Literal["current-state-only-not-caller-commit-proof"]


class OrderRejected(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-order-result/v1"]
    code: diagnostic_codes.OrderRejectedCode
    retry: Literal["correct-input"]
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]
    recovery: OrderRecovery | None


class OrderCommitted(_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-order-result/v1"]
    order: tuple[ordering.OrderWorkItemId, ...]
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
        _require_fixed_state_changed(self)
        if (self.status == "committed-with-warning") != (self.warning is not None):
            raise ValueError("Order terminal status must agree with its view-repair warning.")


class ParallelPreviewRejected(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-parallel-preview-result/v1"]
    code: diagnostic_codes.ParallelPreviewRejectedCode
    retry: Literal["correct-input"]
    observed: Empty
    mismatches: Empty


class ParallelPreviewSuccess(_UnchangedResult, query_models.ParallelPreviewView, frozen=True):
    status: Literal["ok"]
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty

    def __post_init__(self) -> None:
        _require_fixed_state_changed(self)


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
            | decision_models.ActionKind.ACTIVATE
            | decision_models.ActionKind.BLOCK
            | decision_models.ActionKind.BLOCK_ITEM
            | decision_models.ActionKind.CANCEL
            | decision_models.ActionKind.CLOSE
            | decision_models.ActionKind.DEFER
            | decision_models.ActionKind.MERGE_PROPOSAL
            | decision_models.ActionKind.PAUSE
            | decision_models.ActionKind.REJECT_PROPOSAL
            | decision_models.ActionKind.REOPEN
            | decision_models.ActionKind.RECORD_REPLACEMENT
            | decision_models.ActionKind.REBIND_ATTEMPT
            | decision_models.ActionKind.RESUME
            | decision_models.ActionKind.RETURN_FOR_CORRECTION
            | decision_models.ActionKind.RETAIN_TEMPORARILY
            | decision_models.ActionKind.REVISE_ITEM
            | decision_models.ActionKind.START_PR_REVIEW
            | decision_models.ActionKind.REVIEW_PR_BRIEF
            | decision_models.ActionKind.OBSERVE_PR_HEAD
            | decision_models.ActionKind.RECORD_PR_ROUND
            | decision_models.ActionKind.CLOSE_PR_REVIEW
        ):
            return (("ledger",),)
        case _ as unreachable:
            assert_never(unreachable)


class TransitionCommitted(_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
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
    continuation: NonEmptyText
    warning: WarningResult | None

    def __post_init__(self) -> None:
        _require_fixed_state_changed(self)
        if (self.status == "committed-with-warning") != (self.warning is not None):
            raise ValueError("committed-with-warning must carry one warning")
        if self.changed_surfaces not in _committed_transition_surfaces(self.action_id.kind):
            raise ValueError("committed transition must pair a mutating action with its exact supported surfaces")


class TransitionRejected(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
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
    continuation: NonEmptyText


class PublishedFailureResult(_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Publication already changed durable bytes; exact result leaves own identity and code."""

    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ArtifactSurface
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=1)]
    mismatches: tuple[FailureMismatch, ...]


class TransitionFailedAfterPublication(PublishedFailureResult, frozen=True):
    schema: Literal["pinboard-mcp-transition-result/v1"]
    status: Literal["failed-after-publication"]
    action_id: ActionIdentity
    code: NonEmptyText
    message: NonEmptyText
    continuation: NonEmptyText


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


class PreparationAuthorityStatusPresent(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
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


class PreparationAuthorityStatusAbsent(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-preparation-authority-result/v1"]
    status: Literal["absent"]
    item_id: PathComponent
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty


class AuthorityCommittedResult[StatusT](_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
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
        _require_fixed_state_changed(self)
        if (self.status == "committed-with-warning") != (self.warning is not None):
            raise ValueError("committed-with-warning must carry one warning")


class PreparationAuthorityCommitted(AuthorityCommittedResult[authority_models.PreparationLeaseStatus], frozen=True):
    schema: Literal["pinboard-mcp-preparation-authority-result/v1"]
    item_id: PathComponent
    definition_revision: PositiveInt
    definition_digest: Sha256


class PreparationAuthorityRejected(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
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


class AttemptAuthorityStatusPresent(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
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


class AttemptAuthorityStatusAbsent(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-attempt-authority-result/v1"]
    status: Literal["absent"]
    attempt_id: PathComponent
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty


class AttemptAuthorityCommitted(AuthorityCommittedResult[authority_models.AttemptLeaseStatus], frozen=True):
    schema: Literal["pinboard-mcp-attempt-authority-result/v1"]
    attempt_id: PathComponent
    item_id: PathComponent


class AttemptAuthorityRejected(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
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


class ProposalCommitted(_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-proposal-result/v2"]
    status: Literal["committed"]
    proposal_id: proposal_models.ProposalIdentity
    position: PositiveInt
    item_state: Literal["ready"]
    committed_revision: PositiveInt
    history_id: PositiveInt
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: LedgerSurface
    continuation: NonEmptyText
    warning: None


class ProposalCommittedWithWarning(_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-proposal-result/v2"]
    status: Literal["committed-with-warning"]
    proposal_id: proposal_models.ProposalIdentity
    position: PositiveInt
    item_state: Literal["ready"]
    committed_revision: PositiveInt
    history_id: PositiveInt
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: LedgerSurface
    continuation: NonEmptyText
    warning: WarningResult


class ProposalRejected(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-proposal-result/v2"]
    status: Literal["rejected"]
    code: diagnostic_codes.ProposalRejectedCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty
    recovery: None


class ProposalDuplicate(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-proposal-result/v2"]
    status: Literal["rejected"]
    code: diagnostic_codes.ProposalDuplicateCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["do-not-retry"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty
    recovery: NonEmptyText


class ArtifactReferenceResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    artifact_ref_id: PositiveInt
    kind: Literal["brief"]
    key: work_brief_models.KebabId
    revision: PositiveInt
    selector: NonEmptyText
    sha256: Sha256
    size_bytes: PositiveInt
    accepted_revision: PositiveInt


class BriefCommitted(_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: PublicationSurfaces
    continuation: NonEmptyText
    warning: None


class BriefReferenceCommitted(_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ReferenceSurfaces
    continuation: NonEmptyText
    warning: None


class BriefArtifactCommitted(_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ArtifactSurface
    continuation: NonEmptyText
    warning: None


class BriefCommittedWithWarning(_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed-with-warning"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: PublicationSurfaces
    continuation: NonEmptyText
    warning: WarningResult


class BriefReferenceCommittedWithWarning(_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed-with-warning"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ReferenceSurfaces
    continuation: NonEmptyText
    warning: WarningResult


class BriefArtifactCommittedWithWarning(_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed-with-warning"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ArtifactSurface
    continuation: NonEmptyText
    warning: WarningResult


class BriefUnchanged(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["unchanged"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["retry-same-input"]
    changed_surfaces: Empty
    continuation: NonEmptyText
    warning: None


class BriefUnchangedWithWarning(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["unchanged-with-warning"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["retry-same-input"]
    changed_surfaces: Empty
    continuation: NonEmptyText
    warning: WarningResult


class BriefRejected(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["rejected"]
    code: diagnostic_codes.BriefRejectedCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty


class BriefArchitectureImpactObservation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    field: Literal["$.brief.checkpoint.architecture_impact.kind"]
    value: str


class BriefArchitectureImpactMismatch(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    field: Literal["$.brief.checkpoint.architecture_impact.kind"]
    expected: Literal["none | read-only | update-required"]
    observed: str


class BriefContractStarterInvocation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    tool: Literal["pinboard_brief_contract"]
    arguments: BriefContractStarterEnvelope

    def __post_init__(self) -> None:
        if self.arguments.request.boundary != "cross-boundary":
            raise ValueError("architecture-impact recovery requires a cross-boundary starter")


class BriefArchitectureImpactRejected(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["rejected"]
    code: diagnostic_codes.BriefArchitectureImpactRejectedCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: tuple[BriefArchitectureImpactObservation]
    mismatches: tuple[BriefArchitectureImpactMismatch]
    allowed_selections: tuple[Literal["none"], Literal["read-only"], Literal["update-required"]]
    recovery: BriefContractStarterInvocation


class BriefPublishedRejection(PublishedFailureResult, frozen=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["rejected"]
    code: diagnostic_codes.ActionUnavailableCode
    message: NonEmptyText


class PublicationInfrastructureFailureResult(_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    status: Literal["failed-after-publication"]
    code: diagnostic_codes.PublicationInfrastructureFailureResultCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ArtifactSurface
    observed: Empty
    mismatches: Empty
    published_selector: NonEmptyText
    recovery: NonEmptyText


class BriefPublicationInfrastructureFailure(PublicationInfrastructureFailureResult, frozen=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]


class PublicationInfrastructureUnchangedFailureResult(
    _UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True
):
    status: Literal["infrastructure-failure"]
    code: diagnostic_codes.PublicationInfrastructureUnchangedFailureResultCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["retry-same-input", "do-not-retry"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty
    published_selector: NonEmptyText
    recovery: NonEmptyText


class BriefPublicationInfrastructureUnchangedFailure(PublicationInfrastructureUnchangedFailureResult, frozen=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]


class CorrectedBriefPublicationTarget(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    tool: Literal["pinboard_brief_publish"]
    project_root: RootPath
    work_root: RootPath


class BriefReviewCorrection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    status_request: BriefReviewStatusRequest
    corrected_brief_publication: CorrectedBriefPublicationTarget
    negative_review_tool: Literal["pinboard_brief_review"]
    instruction: NonEmptyText


class BriefReviewStatusResult(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-review-result/v1"]
    accepted_brief: ArtifactReferenceResult
    accepted_brief_sha256: Sha256
    checkpoint_sha256: Sha256
    reviewed_authority_set_sha256: Sha256
    correction: BriefReviewCorrection
    retry: Literal["safe-to-repeat"]
    state_changed: bool
    effect: Literal["unchanged"]
    changed_surfaces: Empty


class BriefReviewNoEvidence(BriefReviewStatusResult, frozen=True):
    status: Literal["no-needs-correction-evidence"]
    brief: work_brief_models.WorkBrief


class BriefReviewNeedsCorrection(BriefReviewStatusResult, frozen=True):
    status: Literal["needs-correction"]
    brief: work_brief_models.WorkBrief
    reference: ReviewEvidenceReference
    review: work_brief_models.WorkBriefReviewNeedsCorrection


class BriefReviewCommitted(_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-review-result/v1"]
    status: Literal["committed", "committed-with-warning"]
    reference: ReviewEvidenceReference
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: Annotated[tuple[JobPublicationSurface, ...], msgspec.Meta(min_length=1)]
    correction: BriefReviewCorrection

    warning: WarningResult | None

    def __post_init__(self) -> None:
        _require_fixed_state_changed(self)
        _require_publication_surfaces(self.changed_surfaces)


class BriefReviewUnchanged(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-review-result/v1"]
    status: Literal["unchanged"]
    reference: ReviewEvidenceReference
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["retry-same-input"]
    changed_surfaces: Empty
    correction: BriefReviewCorrection

    warning: WarningResult | None


class BriefReviewRejected(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-brief-review-result/v1"]
    code: diagnostic_codes.BriefReviewRejectedCode
    retry: Literal["correct-input", "do-not-retry"]
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]


class BriefReviewPublishedRejection(PublishedFailureResult, frozen=True):
    schema: Literal["pinboard-mcp-brief-review-result/v1"]
    status: Literal["rejected"]
    code: diagnostic_codes.ActionUnavailableCode
    message: NonEmptyText


class BriefReviewInfrastructureFailure(PublicationInfrastructureFailureResult, frozen=True):
    schema: Literal["pinboard-mcp-brief-review-result/v1"]


class BriefReviewInfrastructureUnchangedFailure(PublicationInfrastructureUnchangedFailureResult, frozen=True):
    schema: Literal["pinboard-mcp-brief-review-result/v1"]


def _require_job_publication(
    surfaces: tuple[JobPublicationSurface, ...],
    state_changed: bool,
    effect: Literal["unchanged", "committed"],
    retry: Literal["safe-to-repeat", "do-not-retry"],
) -> None:
    if surfaces not in (
        (),
        ("immutable-artifact",),
        ("accepted-artifact-reference", "ledger"),
        ("immutable-artifact", "accepted-artifact-reference", "ledger"),
    ):
        raise ValueError("Job publication must expose an exact immutable/reference/ledger surface set.")
    changed = bool(surfaces)
    _require_state_changed(state_changed, changed)
    if effect != ("committed" if changed else "unchanged") or retry != (
        "do-not-retry" if changed else "safe-to-repeat"
    ):
        raise ValueError("Job publication effect and retry must match its terminal surfaces.")


class PublishedJobReady(_VariableStateChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    status: Literal["ready"]
    prompt_reference: dispatch_models.PromptReferenceView
    native_launch: dispatch_models.NativeLaunchEnvelope
    state_changed: bool
    effect: Literal["unchanged", "committed"]
    retry: Literal["safe-to-repeat", "do-not-retry"]
    changed_surfaces: tuple[JobPublicationSurface, ...]

    warning: WarningResult | None

    def __post_init__(self) -> None:
        surfaces = self.changed_surfaces
        _require_job_publication(surfaces, self.state_changed, self.effect, self.retry)
        reference = self.prompt_reference
        if reference.artifact_created and "immutable-artifact" not in surfaces:
            raise ValueError("A newly created prompt must expose its immutable artifact effect.")
        if reference.ledger_changed and ("accepted-artifact-reference" not in surfaces or "ledger" not in surfaces):
            raise ValueError("A newly accepted prompt must expose reference and ledger effects.")


class DispatchReady(PublishedJobReady, frozen=True):
    schema: Literal["pinboard-mcp-dispatch-result/v1"]
    attempt_id: PathComponent
    checkpoint_id: PathComponent


class ReplacementReadinessReady(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-dispatch-result/v1"]
    status: Literal["replacement-ready"]
    attempt_id: PathComponent
    checkpoint_id: PathComponent
    candidate_revision: NonEmptyText
    proof_reference: ReviewEvidenceReference
    ready_review_reference: ReviewEvidenceReference
    next_step: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged", "committed"]
    retry: Literal["safe-to-repeat", "do-not-retry"]
    changed_surfaces: tuple[JobPublicationSurface, ...]

    warning: WarningResult | None

    def __post_init__(self) -> None:
        _require_job_publication(self.changed_surfaces, self.state_changed, self.effect, self.retry)


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


class CandidateReviewRecorded(_VariableStateChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-review-job-result/v1"]
    status: Literal["recorded"]
    attempt_id: PathComponent
    candidate_revision: NonEmptyText
    candidate_review: CandidateReviewPresent
    state_changed: bool
    effect: Literal["unchanged", "committed"]
    retry: Literal["safe-to-repeat", "do-not-retry"]
    changed_surfaces: tuple[JobPublicationSurface, ...]
    warning: WarningResult | None
    """The ordinary view warning when the committed record left generated views needing repair."""

    def __post_init__(self) -> None:
        surfaces = self.changed_surfaces
        if surfaces not in (
            (),
            ("accepted-artifact-reference", "ledger"),
            ("immutable-artifact", "accepted-artifact-reference", "ledger"),
        ):
            raise ValueError("Candidate review publication must expose exact immutable/reference/ledger surfaces.")
        changed = bool(surfaces)
        _require_state_changed(self.state_changed, changed)
        if self.warning is not None and not changed:
            raise ValueError("Only a committed candidate review refreshes views and can carry a view warning.")
        if self.effect != ("committed" if changed else "unchanged") or self.retry != (
            "do-not-retry" if changed else "safe-to-repeat"
        ):
            raise ValueError("Candidate review effect and retry must match its publication surfaces.")


class DispatchInvalid(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-dispatch-result/v1"]
    code: diagnostic_codes.DispatchInvalidCode
    retry: Literal["correct-input"]
    observed: Empty
    mismatches: Empty


class ReviewJobInvalid(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-review-job-result/v1"]
    code: diagnostic_codes.ReviewJobInvalidCode
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
    code: NonEmptyText
    retry: RetryDisposition
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        DecisionFailureCode(self.code)


class JobInfrastructureFailure(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    status: Literal["infrastructure-failure"]
    attempt_id: PathComponent
    code: diagnostic_codes.PublicationInfrastructureUnchangedFailureResultCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["retry-same-input", "do-not-retry"]
    changed_surfaces: Empty
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]

    def __post_init__(self) -> None:
        _require_fixed_state_changed(self)


class DispatchInfrastructureFailure(JobInfrastructureFailure, frozen=True):
    schema: Literal["pinboard-mcp-dispatch-result/v1"]


class ReviewJobInfrastructureFailure(JobInfrastructureFailure, frozen=True):
    schema: Literal["pinboard-mcp-review-job-result/v1"]


class JobFailedAfterPublication(_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
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
        _require_fixed_state_changed(self)
        _require_publication_surfaces(self.changed_surfaces)


class DispatchFailedAfterPublication(JobFailedAfterPublication, frozen=True):
    schema: Literal["pinboard-mcp-dispatch-result/v1"]

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.code not in {"ARTIFACT_ACCEPTANCE_FAILED", "ARTIFACT_PUBLICATION_FAILED"}:
            try:
                dispatch_operations.DispatchErrorCode(self.code)
            except ValueError:
                DecisionFailureCode(self.code)


class ReviewJobFailedAfterPublication(JobFailedAfterPublication, frozen=True):
    schema: Literal["pinboard-mcp-review-job-result/v1"]

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.code not in {
            "ARTIFACT_ACCEPTANCE_FAILED",
            "ARTIFACT_PUBLICATION_FAILED",
            "REVIEW_JOB_PREPARATION_FAILED",
            "REVIEW_PROMPT_PUBLICATION_FAILED",
        }:
            DecisionFailureCode(self.code)


DISPATCH_RESULT_TYPES = (
    DispatchReady,
    ReplacementReadinessReady,
    DispatchInvalid,
    DispatchRejected,
    DispatchInfrastructureFailure,
    DispatchFailedAfterPublication,
    ExecutorBusyResult,
)
REVIEW_JOB_RESULT_TYPES = (
    ReviewJobReady,
    CandidateReviewRecorded,
    ReviewJobInvalid,
    ReviewJobRejected,
    ReviewJobInfrastructureFailure,
    ReviewJobFailedAfterPublication,
    ExecutorBusyResult,
)


class CandidateRestoreReady(_VariableStateChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-candidate-restore-result/v1"]
    status: Literal["restored"]
    attempt_id: PathComponent
    candidate: NonEmptyText
    source_checkout: RootPath
    state_changed: bool
    effect: Literal["unchanged", "committed"]
    retry: Literal["safe-to-repeat", "do-not-retry"]
    changed_surfaces: Annotated[tuple[Literal["source-checkout"], ...], msgspec.Meta(max_length=1)]

    def __post_init__(self) -> None:
        changed = bool(self.changed_surfaces)
        _require_state_changed(self.state_changed, changed)
        if self.effect != ("committed" if changed else "unchanged") or self.retry != (
            "do-not-retry" if changed else "safe-to-repeat"
        ):
            raise ValueError("Restoration must retain its exact source effect and retry disposition.")


class CandidateRestoreInvalid(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-candidate-restore-result/v1"]
    code: diagnostic_codes.CandidateRestoreInvalidCode
    retry: Literal["correct-input"]
    observed: Empty
    mismatches: Empty


class CandidateRestoreRejected(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-candidate-restore-result/v1"]
    attempt_id: PathComponent
    code: DecisionFailureCode
    retry: RetryDisposition
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]


class CandidateRestoreFailed(_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-candidate-restore-result/v1"]
    status: Literal["failed-after-mutation"]
    attempt_id: PathComponent
    code: DecisionFailureCode
    message: NonEmptyText
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: tuple[Literal["source-checkout"]]
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]

    def __post_init__(self) -> None:
        _require_fixed_state_changed(self)


CANDIDATE_RESTORE_RESULT_TYPES = (
    CandidateRestoreReady,
    CandidateRestoreInvalid,
    CandidateRestoreRejected,
    CandidateRestoreFailed,
    ExecutorBusyResult,
)


class CandidateObserved(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-candidate-observation-result/v1"]
    status: Literal["observed"]
    attempt_id: PathComponent
    candidate: Annotated[str, msgspec.Meta(pattern=r"\Aworking-tree-state-sha256:[0-9a-f]{64}\z")]
    source_checkout: RootPath
    branch: NonEmptyText
    accepted_base_revision: NonEmptyText
    preimage_revision: NonEmptyText
    diff_sha256: Sha256
    diff_size_bytes: NonNegativeInt
    omitted_untracked_paths: tuple[NonEmptyText, ...]
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty


class CandidateObservationRejected(RejectedReadResult, frozen=True):
    schema: Literal["pinboard-mcp-candidate-observation-result/v1"]
    code: diagnostic_codes.CandidateObservationRejectedCode
    retry: Literal["correct-input"]
    observed: tuple[FailureObservation, ...]
    mismatches: tuple[FailureMismatch, ...]


CANDIDATE_OBSERVATION_RESULT_TYPES = (CandidateObserved, CandidateObservationRejected, ExecutorBusyResult)

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
    BriefReviewInfrastructureFailure,
    BriefReviewInfrastructureUnchangedFailure,
    ExecutorBusyResult,
)


ITEM_STATUS_RESULT_TYPES = (
    query_models.ItemStatus,
    query_models.BranchOwners,
    query_models.ItemIntegration,
    ItemStatusInvalid,
    ItemStatusUnavailable,
    ItemStatusInconsistent,
    BranchOwnerNotFound,
    ItemStatusReceiptDamaged,
    IntegrationTargetUnresolved,
    IntegrationCandidateUnavailable,
    IntegrationCandidateEvidenceInvalid,
    ItemStatusGitUnavailable,
    ExecutorBusyResult,
)
OVERVIEW_RESULT_TYPES = (query_models.WorkOverview, OverviewRejected, ExecutorBusyResult)
ORDER_RESULT_TYPES = (OrderCommitted, OrderRejected, ExecutorBusyResult)
PARALLEL_PREVIEW_RESULT_TYPES = (ParallelPreviewSuccess, ParallelPreviewRejected, ExecutorBusyResult)
ACTIONS_RESULT_TYPES = (
    ActionsSuccess,
    CompletionActionsSuccess,
    CloseActionsSuccess,
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
    AttemptReceiptDamaged,
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
BRIEF_LOOKUP_RESULT_TYPES = (
    ArtifactVerified,
    BriefLookupInvalid,
    BriefNotFound,
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
    BriefArchitectureImpactRejected,
    BriefRejected,
    BriefPublishedRejection,
    BriefPublicationInfrastructureFailure,
    BriefPublicationInfrastructureUnchangedFailure,
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
STARTUP_TEXT_RESULT_TYPES = (StartupTextPage, StartupTextRejected, StartupHostSettingFailed, ExecutorBusyResult)
FIXTURE_REVIEW_RESULT_TYPES = (FixtureReviewPage, FixtureReviewRejected, ExecutorBusyResult)


class CancellationPreviewRequest(
    msgspec.Struct, tag="preview", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    item_id: action_models.Identity


class CancellationConfirmRequest(
    msgspec.Struct, tag="confirm", tag_field="operation", frozen=True, forbid_unknown_fields=True
):
    project_root: RootPath
    work_root: RootPath
    item_id: action_models.Identity
    binding: cancellation_models.CancellationBinding
    human_confirmation: RecordText
    actor_task_id: RuntimeIdentity
    actor_host_id: RuntimeIdentity

    def __post_init__(self) -> None:
        if not self.human_confirmation.strip():
            raise ValueError("human_confirmation must contain the human's own explicit confirmation")


class CancellationEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: CancellationPreviewRequest | CancellationConfirmRequest


class CancellationPreviewResult(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-cancellation-preview-result/v1"]
    status: Literal["ok"]
    preview: cancellation_models.CancellationPreview
    binding: cancellation_models.CancellationBinding
    continuation: str
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: Empty


class CancellationRejected(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-cancellation-result/v1"]
    status: Literal["rejected"]
    code: Literal["TRANSITION_INPUT_INVALID", "ACTION_NOT_AVAILABLE"]
    message: str
    recovery: str
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input", "refresh-action"]
    changed_surfaces: Empty


class CancellationCommitted(_ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-cancellation-result/v1"]
    status: Literal["committed", "committed-with-warning"]
    item_id: str
    outcome: Literal["dropped"]
    committed_revision: int
    history_id: int
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: LedgerSurface
    warning: WarningResult | None


CANCELLATION_RESULT_TYPES = (CancellationPreviewResult, CancellationRejected, CancellationCommitted, ExecutorBusyResult)


class ObligationCoveragePairRequest(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="pair", tag_field="operation"
):
    project_root: RootPath
    work_root: RootPath
    source_item: Identity
    replacement_item: Identity


class ObligationCoveragePlannedRequest(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="planned-replacement", tag_field="operation"
):
    project_root: RootPath
    work_root: RootPath
    item_id: Identity


class ObligationCoverageEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: ObligationCoveragePairRequest | ObligationCoveragePlannedRequest


class ObligationCoverageSuccess(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-obligation-coverage-result/v1"]
    status: Literal["ok"]
    coverage: obligation_coverage.CoverageReport
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: tuple[()]


class ObligationCoverageRejected(_UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-obligation-coverage-result/v1"]
    status: Literal["rejected"]
    code: DecisionFailureCode
    message: str
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: tuple[()]
