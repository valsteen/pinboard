"""Compose native consolidation decoding, selected observation, locked commit and view refresh."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal, assert_never

import msgspec

from pinboard.adapters.dependency_operations import resolve_dependency_brief_pins
from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.errors import RootError
from pinboard.adapters.files.models import AffectedViews
from pinboard.application import consolidation, consolidation_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import HostId, TaskId
from pinboard.mcp import common, execution
from pinboard.mcp import consolidation_contracts as contracts
from pinboard.mcp.contracts import JsonValue, WarningResult
from pinboard.mcp.tool_names import CONSOLIDATE_TOOL


def rejected(
    code: DecisionFailureCode, message: str, retry: Literal["correct-input", "refresh-action"]
) -> execution.TypedOperationResult[contracts.Result]:
    result = contracts.Rejected(
        "pinboard-consolidation-result/v1",
        "rejected",
        code,
        message,
        "Read a fresh pinboard_consolidate preview with the exact project/work roots and selected live items, show the complete map, and obtain a separate explicit human confirmation. Correct any named condition first; nothing was consolidated.",
        False,
        "unchanged",
        retry,
        (),
    )
    return execution.TypedOperationResult(result, "rejected", None)


def operate(
    arguments: Mapping[str, JsonValue], token: execution.CancellationToken
) -> execution.TypedOperationResult[contracts.Result]:
    try:
        request = msgspec.convert(arguments, type=contracts.Envelope, strict=True).request
    except (msgspec.ValidationError, ValueError) as error:
        return rejected(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            f"Cannot decode consolidation request: {error}",
            "correct-input",
        )
    token.checkpoint()
    try:
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (ValueError, OSError, RootError) as error:
        return rejected(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            f"Cannot resolve consolidation project '{request.project_root}' and work root '{request.work_root}': {error}",
            "correct-input",
        )
    store = common.compose_store(durable)
    now = datetime.now(UTC)
    dependency_pins = resolve_dependency_brief_pins(
        store,
        ArtifactRepository(durable),
        consolidation.requested_dependencies(consolidation_models.plan(request.selection)),
        now,
    )
    if isinstance(dependency_pins, DecisionFailure):
        return rejected(dependency_pins.code, dependency_pins.message, "refresh-action")
    match request:
        case contracts.PreviewRequest():
            preview = consolidation.preview(
                store, request.selection, TaskId(request.actor_task_id), now, dependency_pins
            )
            if isinstance(preview, DecisionFailure):
                return rejected(preview.code, preview.message, "refresh-action")
            result = contracts.PreviewResult(
                "pinboard-consolidation-preview-result/v1",
                "ok",
                preview,
                consolidation.binding(preview, durable.work_root),
                "Show the complete campaign, exact carried work, original source waits, surviving consumer changes, queue and lifecycle consequences; then stop for a separate explicit human confirmation of this map. Confirmation authorizes no launch, process or repository effect.",
                False,
                "unchanged",
                "safe-to-repeat",
                (),
            )
            return execution.TypedOperationResult(result, "ok", None)
        case contracts.ConfirmRequest():
            token.checkpoint()
            effect = consolidation.confirm(
                store,
                request.selection,
                request.binding,
                request.human_confirmation,
                TaskId(request.actor_task_id),
                HostId(request.actor_host_id),
                now,
                durable.work_root,
                dependency_pins,
            )
            if isinstance(effect, DecisionFailure):
                return rejected(effect.code, effect.message, "refresh-action")
            refreshed = common._refresh_affected_views(
                durable,
                store,
                AffectedViews(effect.work_item_ids, effect.attempt_ids, (effect.receipt.history_id,)),
                now,
            )
            warning = refreshed.warning
            committed = contracts.Committed(
                "pinboard-consolidation-result/v1",
                "committed" if warning is None else "committed-with-warning",
                request.selection.campaign.item_id,
                tuple(value.item_id for value in request.selection.sources),
                effect.receipt.project_revision,
                int(effect.receipt.history_id),
                True,
                "committed",
                "do-not-retry",
                ("ledger",),
                None if warning is None else WarningResult(warning.message, warning.repair),
            )
            return execution.TypedOperationResult(
                committed,
                "committed" if warning is None else "committed-warning",
                str(effect.receipt.project_revision),
            )
        case _ as unreachable:
            assert_never(unreachable)


CONSOLIDATE: execution.ToolContract[contracts.Envelope, contracts.Result] = execution.ToolContract(
    name=CONSOLIDATE_TOOL,
    description="Preview a new or live existing campaign from selected live sources, then atomically confirm the exact displayed map only after a separate human confirmation. Carries every source obligation with its direct origin, folds internal waits into retained context, preserves external waits, retargets surviving consumers and ends sources as superseded. Put all fields inside request. No worker launch or repository effect.",
    request=contracts.Envelope,
    results=contracts.Result,
    operate=operate,
    meta=None,
)
