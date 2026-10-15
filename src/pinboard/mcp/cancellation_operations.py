"""Exact cancellation preview and confirmation; human authorship remains trusted-local attribution."""

from datetime import UTC, datetime
from typing import Literal, assert_never

import msgspec

from pinboard.adapters.files.errors import RootError
from pinboard.adapters.files.models import AffectedViews
from pinboard.application import cancellation
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import HostId, TaskId, WorkItemId
from pinboard.mcp import common, contracts, execution
from pinboard.mcp.contracts import JsonValue


def operate(arguments: dict[str, JsonValue], token: execution.CancellationToken) -> execution.OperationResult:
    try:
        request = msgspec.convert(arguments, type=contracts.CancellationEnvelope, strict=True).request
    except (msgspec.ValidationError, ValueError) as error:
        return rejected(
            "TRANSITION_INPUT_INVALID",
            f"Cannot decode cancellation request: {error}",
            "correct-input",
            "Correct the request, then read a fresh pinboard_cancel preview for the exact item and obtain a separate explicit human confirmation.",
        )
    token.checkpoint()
    try:
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (ValueError, OSError, RootError) as error:
        return rejected(
            "TRANSITION_INPUT_INVALID",
            f"Cannot resolve cancellation project '{request.project_root}' and work root '{request.work_root}': {error}",
            "correct-input",
            "Use the exact available Git checkout and initialized Pinboard work root, then read a fresh cancellation preview. No cancellation was committed.",
        )
    store = common.compose_store(durable)
    item_id = WorkItemId(request.item_id)
    match request:
        case contracts.CancellationPreviewRequest():
            preview = cancellation.preview(store, item_id)
            if isinstance(preview, DecisionFailure):
                return rejected(
                    "ACTION_NOT_AVAILABLE",
                    preview.message,
                    "refresh-action",
                    "Read a fresh pinboard_cancel preview for the exact live item and obtain a separate explicit human confirmation.",
                )
            result = contracts.CancellationPreviewResult(
                "pinboard-cancellation-preview-result/v1",
                "ok",
                preview,
                cancellation.binding(preview, durable.work_root),
                "Show the exact item and all consequences to the human, then stop and obtain a separate explicit confirmation before calling confirm. All sharing clients must use a cancellation-aware build before first cancellation.",
                False,
                "unchanged",
                "safe-to-repeat",
                (),
            )
            return execution.OperationResult(msgspec.to_builtins(result), "ok", None)
        case contracts.CancellationConfirmRequest():
            token.checkpoint()
            now = datetime.now(UTC)
            effect = cancellation.confirm(
                store,
                item_id,
                request.binding,
                request.human_confirmation,
                TaskId(request.actor_task_id),
                HostId(request.actor_host_id),
                now,
                durable.work_root,
            )
            if isinstance(effect, DecisionFailure):
                return rejected(
                    "ACTION_NOT_AVAILABLE",
                    effect.message,
                    "refresh-action",
                    "Read a fresh pinboard_cancel preview for the exact live item and obtain a separate explicit human confirmation.",
                )
            refreshed = common._refresh_affected_views(
                durable,
                store,
                AffectedViews(effect.work_item_ids, effect.attempt_ids, (effect.receipt.history_id,)),
                now,
            )
            warning = refreshed.warning
            result = contracts.CancellationCommitted(
                "pinboard-cancellation-result/v1",
                "committed" if warning is None else "committed-with-warning",
                request.item_id,
                "dropped",
                effect.receipt.project_revision,
                int(effect.receipt.history_id),
                True,
                "committed",
                "do-not-retry",
                ("ledger",),
                None if warning is None else contracts.WarningResult(warning.message, warning.repair),
            )
            return execution.OperationResult(
                msgspec.to_builtins(result),
                "committed" if warning is None else "committed-warning",
                str(effect.receipt.project_revision),
            )
        case _ as unreachable:
            assert_never(unreachable)


def rejected(
    code: Literal["TRANSITION_INPUT_INVALID", "ACTION_NOT_AVAILABLE"],
    message: str,
    retry: Literal["correct-input", "refresh-action"],
    recovery: str,
) -> execution.OperationResult:
    result = contracts.CancellationRejected(
        "pinboard-cancellation-result/v1",
        "rejected",
        code,
        message,
        recovery,
        False,
        "unchanged",
        retry,
        (),
    )
    return execution.OperationResult(msgspec.to_builtins(result), "rejected", None)
