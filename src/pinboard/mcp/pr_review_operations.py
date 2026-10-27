"""Native boundary for the distinct human-owned PR review route."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, assert_never

import msgspec

from pinboard.adapters.files import file_io
from pinboard.adapters.files.models import AffectedViews
from pinboard.adapters.sqlite import pr_review
from pinboard.application import pr_reviews
from pinboard.diagnostic_codes import ProducerOnlyCode
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import HistoryId, WorkItemId
from pinboard.mcp import common, contracts, execution
from pinboard.mcp.contracts import JsonValue


def _rejected(code: str, message: str) -> execution.OperationResult:
    value = contracts.PrReviewRejected(
        "pinboard-pr-review-result/v1", "rejected", code, message, False, "unchanged", "correct-input", ()
    )
    content: dict[str, JsonValue] = msgspec.to_builtins(value)
    return execution.OperationResult(content, "rejected", None)


def _present(
    state: pr_review.ReviewState, *, committed: bool, view_warning: str | None = None
) -> execution.OperationResult:
    brief = state.brief
    rounds = state.rounds
    latest = state.latest_observation
    last_head = None if not rounds else rounds[-1][1].observed_head
    unreviewed = None if latest is None or latest.observed_head == last_head else latest.observed_head
    current_brief = brief is not None and (brief[1].definition_revision, brief[1].definition_digest) == (
        state.definition_revision,
        state.definition_digest,
    )
    review = state.brief_review
    actions: tuple[Literal["start", "review-brief", "observe", "round", "close"], ...]
    if (state.item_state.value == "ready" and brief is None and not state.has_attempt) or (
        state.item_state.value == "review" and not current_brief
    ):
        actions = ("start",) if state.item_state.value == "ready" else ("start", "close")
    elif state.item_state.value == "review" and review is None:
        actions = ("review-brief", "observe", "close")
    elif state.item_state.value == "review" and review is not None and review.verdict == "needs-correction":
        actions = ("start", "close")
    elif state.item_state.value == "review" and current_brief:
        actions = ("observe", "round", "close")
    else:
        actions = ()
    value = contracts.PrReviewSuccess(
        "pinboard-pr-review-status/v1",
        "committed-with-warning" if view_warning is not None else "committed" if committed else "ok",
        str(state.item_id),
        state.item_state.value,
        state.subject_revision,
        state.project_revision,
        None if brief is None else brief[0],
        None if brief is None else brief[1],
        review,
        tuple(contracts.PrReviewRoundView(history_id, round_value) for history_id, round_value in rounds),
        "reviewed-head-recorded" if rounds else "no-pr-round-completed",
        latest,
        unreviewed,
        state.close,
        actions,
        "unverified",
        committed,
        "committed" if committed else "unchanged",
        "do-not-retry" if committed else "safe-to-repeat",
        ("ledger",) if committed else (),
        view_warning,
    )
    content: dict[str, JsonValue] = msgspec.to_builtins(value)
    return execution.OperationResult(content, "committed" if committed else "ok", str(state.project_revision))


def _selected_record[T](path: str, sha256: str, record_type: type[T]) -> T | execution.OperationResult:
    content = file_io.read_verified_input(Path(path), sha256)
    if isinstance(content, file_io.SelectedInputFailure):
        return _rejected(content.code, content.message)
    try:
        return msgspec.json.decode(content, type=record_type)
    except msgspec.DecodeError as error:
        return _rejected(
            ProducerOnlyCode.PR_REVIEW_INVALID.value,
            f"Cannot decode selected PR review record '{path}' with verified SHA-256 {sha256}: {error}. Correct that file to this exact operation's record schema before retrying.",
        )


def execute(  # noqa: C901, PLR0912, PLR0915 - exact inline/file leaves retain actor and persistence checks
    raw: dict[str, JsonValue], token: execution.CancellationToken
) -> execution.OperationResult:
    token.checkpoint()
    try:
        envelope = msgspec.convert(raw, type=contracts.PrReviewEnvelope, strict=True)
        request = envelope.request
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return _rejected(ProducerOnlyCode.PR_REVIEW_INVALID.value, f"Cannot decode PR review request: {error}")
    item_id = WorkItemId(request.item_id)
    token.checkpoint()
    match request:
        case contracts.PrReviewStatusRequest() | contracts.PrReviewActionsRequest():
            state = pr_review.read(durable.database_path, item_id)
            if state is None:
                return _rejected(
                    DecisionFailureCode.ITEM_NOT_FOUND.value,
                    f"Review item '{request.item_id}' does not exist. Read pinboard_overview with project_root={request.project_root!r} and work_root={request.work_root!r} to discover current items; do not create a replacement without human intent.",
                )
            return _present(state, committed=False)
        case contracts.PrReviewStartRequest() | contracts.PrReviewStartFileRequest():
            if isinstance(request, contracts.PrReviewStartFileRequest):
                selected = _selected_record(request.brief_path, request.brief_sha256, pr_reviews.ReviewBrief)
                if isinstance(selected, execution.OperationResult):
                    return selected
                payload = selected
            else:
                payload = request.brief
            claimed_task_id = payload.prepared_by_task_id
        case contracts.PrReviewBriefReviewRequest() | contracts.PrReviewBriefReviewFileRequest():
            if isinstance(request, contracts.PrReviewBriefReviewFileRequest):
                selected = _selected_record(
                    request.brief_review_path, request.brief_review_sha256, pr_reviews.BriefReview
                )
                if isinstance(selected, execution.OperationResult):
                    return selected
                payload = selected
            else:
                payload = request.brief_review
            claimed_task_id = payload.reviewer_task_id
        case contracts.PrReviewObserveRequest():
            payload = request.observation
            claimed_task_id = request.actor_task_id
        case contracts.PrReviewRoundRequest() | contracts.PrReviewRoundFileRequest():
            if isinstance(request, contracts.PrReviewRoundFileRequest):
                selected = _selected_record(request.round_path, request.round_sha256, pr_reviews.ReviewRound)
                if isinstance(selected, execution.OperationResult):
                    return selected
                payload = selected
            else:
                payload = request.round
            claimed_task_id = payload.reviewer_task_id
        case contracts.PrReviewCloseRequest():
            payload = request.close
            claimed_task_id = payload.human_task_id
        case _ as unreachable:
            assert_never(unreachable)
    if claimed_task_id != request.actor_task_id:
        return _rejected(
            ProducerOnlyCode.PR_REVIEW_INVALID.value,
            "Review task identity must match actor_task_id. Use your own trusted runtime identity; this rejection supplies no identity or authority.",
        )
    token.checkpoint()
    result = pr_review.write(
        durable.database_path,
        item_id,
        request.expected_subject_revision,
        payload,
        request.actor_task_id,
        request.actor_host_id,
        datetime.now(UTC),
    )
    if isinstance(result, DecisionFailure):
        inspection = {
            "request": {
                "project_root": request.project_root,
                "work_root": request.work_root,
                "item_id": request.item_id,
                "operation": "status",
            }
        }
        return _rejected(
            result.code.value,
            result.message
            + f" Read pinboard_pr_review with {msgspec.json.encode(inspection, order='sorted').decode()} and use its current subject_revision, brief, rounds and legal_actions before choosing another action. This rejection grants no permission to close or mutate the item.",
        )
    token.checkpoint()
    refreshed = common._refresh_affected_views(
        durable,
        common.compose_store(durable),
        AffectedViews((item_id,), (), (HistoryId(result.evidence[-1].history_id),)),
        datetime.now(UTC),
    )
    warning = None if refreshed.warning is None else f"{refreshed.warning.message} {refreshed.warning.repair}"
    return _present(result, committed=True, view_warning=warning)
