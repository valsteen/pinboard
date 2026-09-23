"""Agent coordination, preparation, and read-only MCP use-case composition."""

from __future__ import annotations

import hashlib
import shlex
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard.adapters import review_operations
from pinboard.adapters.files.artifacts import ArtifactRepository, read_reference
from pinboard.adapters.files.brief_sources import select_checkout_brief_source
from pinboard.adapters.files.errors import ArtifactError, FileIOError, ImmutableFilePublishedError, RootError
from pinboard.adapters.files.file_io import DurableRoots, create_immutable
from pinboard.adapters.files.models import AffectedViews
from pinboard.adapters.files.root import resolve_source_checkout_root
from pinboard.application import (
    action_models,
    actions,
    brief_source_codec,
    brief_source_models,
    brief_sources,
    candidate_snapshots,
    queries,
    query_models,
    service,
    work_brief_contract,
    work_brief_models,
    work_briefs,
)
from pinboard.application.ports import WorkStore
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import (
    ArtifactAcceptanceAfterPublicationError,
    DecisionFailure,
    DecisionFailureCode,
    DecisionResult,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    FailureMismatch,
    RetryDisposition,
)
from pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HostId,
    ItemId,
    LeaseId,
    TaskId,
)
from pinboard.mcp import common, contracts, execution, tool_names
from pinboard.mcp.contracts import JsonValue

type IntegerBoundaryValue = bool | int | float | str | None


def _item_status_json(status: query_models.ItemStatus) -> dict[str, JsonValue]:
    preparation = status.preparation
    return {
        "schema": status.schema,
        "authority": status.authority,
        "revision": status.revision,
        "item_id": status.item_id,
        "label": status.label,
        "state": status.state.value,
        "timing": None if status.timing is None else status.timing.value,
        "outcome_evidence": status.outcome_evidence,
        "next_action": status.next_action,
        "source": status.source,
        "notes": status.notes,
        "queue_position": status.queue_position,
        "attempts": [
            {
                "attempt_id": attempt.attempt_id,
                "state": attempt.state.value,
                "candidate_revision": attempt.candidate_revision,
            }
            for attempt in status.attempts
        ],
        "preparation": (
            None
            if preparation is None
            else {
                "definition_revision": preparation.definition_revision,
                "definition_digest": preparation.definition_digest,
                "task_id": preparation.task_id,
                "host_id": preparation.host_id,
                "lease_id": preparation.lease_id,
                "generation": preparation.generation,
                "expires_at": preparation.expires_at,
                "status": preparation.status.value,
            }
        ),
    }


def _read_item_status(
    project_root: str,
    work_root: str,
    item_id: str,
    token: execution.CancellationToken,
) -> execution.OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(
            {"project_root": project_root, "work_root": work_root, "item_id": item_id},
            type=contracts.ItemStatusRequest,
            strict=True,
        )
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return common._item_status_failure("ITEM_STATUS_INVALID", f"Cannot read item status: {error}", None)
    token.checkpoint()
    projected = queries.project_item_status(common.compose_store(durable), ItemId(request.item_id), datetime.now(UTC))
    if isinstance(projected, DecisionFailure):
        return common._item_status_failure(projected.code.value, projected.message, projected.details)
    token.checkpoint()
    return execution.OperationResult(_item_status_json(projected), "ok", projected.revision)


def _brief_preparation_failure(schema: str, code: str, message: str) -> execution.OperationResult:
    return execution.OperationResult(
        {
            "schema": schema,
            "status": "rejected",
            "code": code,
            "message": message,
            "state_changed": False,
            "effect": "unchanged",
            "retry": "correct-input",
            "changed_surfaces": [],
        },
        "rejected",
        None,
    )


def _brief_contract(raw: dict[str, JsonValue], token: execution.CancellationToken) -> execution.OperationResult:
    """Construct unresolved contract data without resolving roots, stores or authority."""
    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.BriefContractEnvelope, strict=True).request
    except (msgspec.ValidationError, ValueError) as error:
        return _brief_preparation_failure(
            "pinboard-mcp-brief-contract-result/v1", "BRIEF_CONTRACT_REQUEST_INVALID", str(error)
        )
    match request:
        case contracts.BriefContractFullRequest():
            contract = work_brief_contract.describe_work_brief_contract()
        case contracts.BriefContractStarterRequest():
            contract = work_brief_contract.describe_work_brief_starter(request.boundary)
        case _ as unreachable:
            assert_never(unreachable)
    # Raw fragments require JSON output decoding, not dictionary conversion.
    content: dict[str, JsonValue] = msgspec.json.decode(msgspec.json.encode(contract))
    return execution.OperationResult(content, "read", None)


def _publish_source_plan(
    destination: Path,
    source_plan: brief_source_models.BriefSourcePlan,
    token: execution.CancellationToken,
) -> execution.OperationResult:
    """Check cancellation before immutable publication; report terminal visibility without a late check."""
    schema = "pinboard-mcp-brief-sources-result/v1"
    plan_bytes = brief_source_codec.encode_brief_source_plan(source_plan)
    token.checkpoint()
    try:
        created = create_immutable(destination, plan_bytes)
    except ImmutableFilePublishedError as error:
        return execution.OperationResult(
            {
                "schema": schema,
                "status": "committed-effect",
                "code": error.code.value,
                "message": str(error),
                "destination": str(error.path),
                "state_changed": True,
                "effect": "committed",
                "retry": "do-not-retry",
                "changed_surfaces": ["selected-output"],
            },
            "committed-effect",
            None,
        )
    except FileIOError as error:
        return _brief_preparation_failure(schema, error.code.value, str(error))
    receipt: dict[str, JsonValue] = msgspec.to_builtins(
        brief_source_codec.plan_output_receipt(str(destination), created, plan_bytes, source_plan)
    )
    receipt.update(
        {
            "state_changed": created,
            "effect": "committed" if created else "unchanged",
            "retry": "do-not-retry" if created else "safe-to-repeat",
            "changed_surfaces": ["selected-output"] if created else [],
        }
    )
    return execution.OperationResult(receipt, "committed" if created else "unchanged", None)


def _brief_sources(raw: dict[str, JsonValue], token: execution.CancellationToken) -> execution.OperationResult:
    """Acquire selected-checkout sources and optional explicit output, never durable work state."""
    schema = "pinboard-mcp-brief-sources-result/v1"
    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.BriefSourcesEnvelope, strict=True).request
    except (msgspec.ValidationError, ValueError) as error:
        return _brief_preparation_failure(schema, "BRIEF_SOURCES_REQUEST_INVALID", str(error))
    try:
        source_checkout = resolve_source_checkout_root(Path(request.project_root))
    except RootError as error:
        return _brief_preparation_failure(schema, error.code.value, str(error))
    select_source = partial(select_checkout_brief_source, source_checkout)
    token.checkpoint()
    match request:
        case contracts.BriefSourcesPlanRequest() | contracts.BriefSourcesPlanToFileRequest():
            source_plan = brief_sources.plan_brief_sources(select_source, request.manifest, request.max_batch_bytes)
            if isinstance(source_plan, brief_source_models.BriefSourceFailure):
                return _brief_preparation_failure(schema, source_plan.code.value, source_plan.message)
            if isinstance(request, contracts.BriefSourcesPlanRequest):
                content: dict[str, JsonValue] = msgspec.to_builtins(
                    brief_source_codec.project_brief_source_plan(source_plan)
                )
                return execution.OperationResult(content, "read", None)
            return _publish_source_plan(Path(request.destination).absolute(), source_plan, token)
        case contracts.BriefSourcesEmitRequest():
            source_plan = brief_source_codec.plan_from_view(request.plan)
        case contracts.BriefSourcesEmitFileRequest():
            try:
                plan_bytes = Path(request.plan_path).read_bytes()
            except OSError as error:
                return _brief_preparation_failure(
                    schema, "BRIEF_SOURCE_PLAN_INVALID", f"Cannot read brief source plan '{request.plan_path}': {error}"
                )
            source_plan = brief_source_codec.decode_brief_source_plan(plan_bytes)
            if isinstance(source_plan, brief_source_models.BriefSourceFailure):
                return _brief_preparation_failure(schema, source_plan.code.value, source_plan.message)
        case _ as unreachable:
            assert_never(unreachable)
    token.checkpoint()
    batch = brief_sources.render_brief_source_batch(select_source, source_plan, request.batch_index)
    if isinstance(batch, brief_source_models.BriefSourceFailure):
        return _brief_preparation_failure(schema, batch.code.value, batch.message)
    return execution.OperationResult(
        {
            "schema": "pinboard-brief-source-batch/v1",
            "batch_index": request.batch_index,
            "content_byte_count": source_plan.batches[request.batch_index].content_byte_count,
            "rendered_byte_count": len(batch),
            "text": batch.decode("utf-8"),
        },
        "read",
        None,
    )


def _read_item_definition(raw: dict[str, JsonValue], token: execution.CancellationToken) -> execution.OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.ItemDefinitionEnvelope, strict=True).request
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return common._read_failure(
            "pinboard-mcp-item-definition-result/v1", "ITEM_DEFINITION_REQUEST_INVALID", str(error), None
        )
    token.checkpoint()
    store = common.compose_store(durable)
    match request:
        case contracts.ItemDefinitionCurrentRequest():
            selected = queries.select_item_definition(store, ItemId(request.item_id))
        case contracts.ItemDefinitionHistoryRequest():
            selected = queries.select_item_definition_history(
                store, ItemId(request.item_id), limit=request.limit, before_revision=request.before_revision
            )
        case _ as unreachable:
            assert_never(unreachable)
    if isinstance(selected, DecisionFailure):
        return common._read_failure(
            "pinboard-mcp-item-definition-result/v1", selected.code.value, selected.message, selected.details
        )
    token.checkpoint()
    content = msgspec.to_builtins(selected)
    assert isinstance(content, dict)
    return execution.OperationResult(content, "ok", str(selected.project_revision))


def _brief_review_correction(project_root: str, work_root: str, brief_artifact_ref_id: int) -> dict[str, JsonValue]:
    return {
        "status_request": {
            "operation": "status",
            "project_root": project_root,
            "work_root": work_root,
            "brief_artifact_ref_id": brief_artifact_ref_id,
        },
        "corrected_brief_publication": {
            "tool": tool_names.BRIEF_PUBLISH_TOOL,
            "project_root": project_root,
            "work_root": work_root,
        },
        "negative_review_tool": tool_names.BRIEF_REVIEW_TOOL,
        "instruction": (
            "Correct the returned accepted brief using verified findings, then add the corrected brief as the "
            "brief argument to pinboard_brief_publish at the returned roots with a new artifact revision. "
            "Independently reassess the corrected accepted brief under the bounded-correction review policy; "
            "retain the same independent reviewer unless widening requires a fresh reviewer. "
            "Use pinboard_brief_review publish with the new accepted brief reference and an exact bound "
            "needs-correction review only if blocking findings remain. Ready review remains dispatch-only; "
            "absence or publication of negative evidence grants no readiness, lifecycle change or authority."
        ),
    }


def _brief_review(raw: dict[str, JsonValue], token: execution.CancellationToken) -> execution.OperationResult:
    token.checkpoint()
    schema = "pinboard-mcp-brief-review-result/v1"
    try:
        request = msgspec.convert(raw, type=contracts.BriefReviewEnvelope, strict=True).request
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return common._read_failure(schema, "BRIEF_REVIEW_REQUEST_INVALID", str(error), None)
    token.checkpoint()
    store = common.compose_store(durable)
    repository = ArtifactRepository(durable)
    correction = _brief_review_correction(request.project_root, request.work_root, request.brief_artifact_ref_id)
    match request:
        case contracts.BriefReviewStatusRequest():
            selected = work_briefs.read_brief_review_status(
                store, repository, ArtifactRefId(request.brief_artifact_ref_id)
            )
            if isinstance(selected, work_brief_models.WorkBriefFailure):
                return common._read_failure(schema, selected.code.value, selected.message, None)
            token.checkpoint()
            content: dict[str, JsonValue] = {
                "schema": schema,
                "accepted_brief": common._artifact_reference_json(selected.accepted_brief.reference),
                "brief": msgspec.to_builtins(selected.accepted_brief.brief),
                "correction": correction,
                "state_changed": False,
                "effect": "unchanged",
                "retry": "safe-to-repeat",
                "changed_surfaces": [],
            }
            match selected:
                case work_brief_models.NoNeedsCorrectionEvidence():
                    content["status"] = "no-needs-correction-evidence"
                case work_brief_models.NeedsCorrectionEvidence(reference=reference, review=review):
                    content["status"] = "needs-correction"
                    content["reference"] = common._artifact_reference_json(reference)
                    content["review"] = msgspec.to_builtins(review)
                case _ as unreachable:
                    assert_never(unreachable)
            return execution.OperationResult(content, "ok", None)
        case contracts.BriefReviewPublishRequest():
            try:
                publication = work_briefs.publish_brief_review_needs_correction(
                    store,
                    repository,
                    repository,
                    ArtifactRefId(request.brief_artifact_ref_id),
                    request.review,
                    datetime.now(UTC),
                )
            except ArtifactAcceptanceAfterPublicationError as error:
                return execution.OperationResult(
                    {
                        "schema": schema,
                        "status": "failed-after-publication",
                        "code": "ARTIFACT_ACCEPTANCE_FAILED",
                        "message": "The review was published, but its accepted reference could not be committed.",
                        "state_changed": True,
                        "effect": "committed",
                        "retry": "do-not-retry",
                        "changed_surfaces": [surface.value for surface in error.changed_surfaces],
                        "observed": [],
                        "mismatches": [],
                        "published_selector": error.selector,
                        "recovery": "Preserve the published selector and repair artifact-reference acceptance before continuing.",
                    },
                    "infrastructure-failure",
                    error.selector,
                )
            if isinstance(publication, work_brief_models.WorkBriefFailure):
                return common._read_failure(schema, publication.code.value, publication.message, None)
            if isinstance(publication, DecisionFailure):
                details = common._details_json(publication.details)
                return execution.OperationResult(
                    {
                        "schema": schema,
                        "status": "rejected",
                        "code": publication.code.value,
                        "message": publication.message,
                        "state_changed": details["effect"] == "committed",
                        **details,
                    },
                    "rejected",
                    None,
                )
            surfaces: list[JsonValue] = [
                *(["immutable-artifact"] if publication.artifact_created else []),
                *(["accepted-artifact-reference", "ledger"] if publication.ledger_changed else []),
            ]
            return execution.OperationResult(
                {
                    "schema": schema,
                    "status": "committed" if surfaces else "unchanged",
                    "reference": common._artifact_reference_json(publication.reference),
                    "correction": correction,
                    "state_changed": bool(surfaces),
                    "effect": "committed" if surfaces else "unchanged",
                    "retry": "do-not-retry" if surfaces else "retry-same-input",
                    "changed_surfaces": surfaces,
                },
                "committed" if surfaces else "unchanged",
                str(publication.reference.artifact_ref_id),
            )
        case _ as unreachable:
            assert_never(unreachable)


def _read_overview(project_root: str, work_root: str, token: execution.CancellationToken) -> execution.OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(
            {"project_root": project_root, "work_root": work_root},
            type=contracts.OverviewRequest,
            strict=True,
        )
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return common._read_failure(
            "pinboard-mcp-overview-result/v1", "OVERVIEW_INVALID", f"Cannot read overview: {error}", None
        )
    token.checkpoint()
    operation_time = datetime.now(UTC)
    store = common.compose_store(durable)
    overview = queries.project_current_overview(store.read_project_overview(operation_time), operation_time)
    token.checkpoint()
    content = msgspec.to_builtins(overview)
    assert isinstance(content, dict)
    return execution.OperationResult(content, "ok", overview.revision)


def _order(raw: dict[str, JsonValue], token: execution.CancellationToken) -> execution.OperationResult:
    """Decode human-authorized order, commit under the shared lock, then refresh selected views."""
    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.OrderEnvelope, strict=True).request
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return execution.OperationResult(
            {
                "schema": "pinboard-mcp-order-result/v1",
                "status": "rejected",
                "code": "ORDER_INVALID",
                "message": f"Cannot decode order request: {error}",
                "state_changed": False,
                **common._details_json(None),
                "recovery": None,
            },
            "rejected",
            None,
        )
    recovery: dict[str, JsonValue] = {
        "tool": tool_names.OVERVIEW_TOOL,
        "arguments": {"project_root": request.project_root, "work_root": request.work_root},
        "meaning": "current-state-only-not-caller-commit-proof",
    }
    store = common.compose_store(durable)
    now = datetime.now(UTC)
    token.checkpoint()
    committed = service.reorder(
        store,
        request.order.expected_order,
        request.order.requested_order,
        TaskId(request.actor_task_id),
        HostId(request.actor_host_id),
        now,
    )
    if isinstance(committed, DecisionFailure):
        return execution.OperationResult(
            {
                "schema": "pinboard-mcp-order-result/v1",
                "status": "rejected",
                "code": committed.code.value,
                "message": committed.message,
                "state_changed": False,
                **common._details_json(committed.details),
                "recovery": recovery,
            },
            "rejected",
            None,
        )
    refreshed = common._refresh_affected_views(
        durable, store, AffectedViews(committed.item_ids, committed.attempt_ids, (committed.receipt.history_id,)), now
    )
    content: dict[str, JsonValue] = {
        "schema": "pinboard-mcp-order-result/v1",
        "order": list[JsonValue](request.order.requested_order),
        **common._committed_authority_fields(committed, refreshed.warning),
        "recovery": recovery,
    }
    if refreshed.warning is not None:
        content["warning"] = {
            "message": refreshed.warning.message,
            "recovery": shlex.join(
                (
                    "pinboard",
                    "--project-root",
                    request.project_root,
                    "--work-root",
                    str(durable.work_root),
                    "views",
                    "rebuild",
                )
            ),
        }
    return execution.OperationResult(
        content,
        "committed" if refreshed.warning is None else "committed-warning",
        str(committed.receipt.project_revision),
    )


def _parallel_preview(raw: dict[str, JsonValue], token: execution.CancellationToken) -> execution.OperationResult:
    """Read only exact selected constraints or explicit current portfolio facts; never launch work."""
    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.ParallelPreviewEnvelope, strict=True).request
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return common._read_failure(
            "pinboard-mcp-parallel-preview-result/v1",
            "PARALLEL_PREVIEW_INVALID",
            f"Cannot decode parallel preview: {error}",
            None,
        )
    token.checkpoint()
    store = common.compose_store(durable)
    operation_time = datetime.now(UTC)
    match request:
        case contracts.SelectedParallelPreviewRequest(item_ids=item_ids):
            preview = queries.select_parallel_preview(store, selected=item_ids, now=operation_time)
        case contracts.AllSafeParallelPreviewRequest():
            preview = queries.project_current_parallel_preview(
                store.read_current_parallel_snapshot(operation_time), now=operation_time
            )
        case _ as unreachable:
            assert_never(unreachable)
    if isinstance(preview, query_models.ParallelSelectionInvalid):
        return common._read_failure(
            "pinboard-mcp-parallel-preview-result/v1", "PARALLEL_SELECTION_INVALID", preview.message, None
        )
    token.checkpoint()
    content = msgspec.to_builtins(queries.present_parallel_preview(preview))
    assert isinstance(content, dict)
    return execution.OperationResult(
        {
            **content,
            "status": "ok",
            "state_changed": False,
            "effect": "unchanged",
            "retry": "safe-to-repeat",
            "changed_surfaces": [],
        },
        "ok",
        preview.revision,
    )


def _action_failure_details(
    failure: DecisionFailure,
    role: decision_models.Role,
    lease_id: LeaseId | None,
    generation: int | None,
    action_id: ActionId | None,
) -> FailureDetails:
    observations = (
        FailureFact("role", role.value),
        FailureFact("lease_id", lease_id),
        FailureFact("generation", generation),
        FailureFact("action_id", action_id),
    )
    match failure.code:
        case DecisionFailureCode.ATTEMPT_LEASE_REQUIRED:
            return FailureDetails(
                observed=observations,
                mismatches=(
                    FailureMismatch(
                        "attempt_authority", "current active attempt lease and generation", "absent or stale"
                    ),
                ),
                retry=RetryDisposition.REACQUIRE_AUTHORITY,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            )
        case DecisionFailureCode.ACTION_NOT_AVAILABLE:
            return FailureDetails(
                observed=observations,
                mismatches=(FailureMismatch("legal_action", "currently available", "unavailable"),),
                retry=RetryDisposition.REFRESH_ACTION,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            )
        case _:
            raise RuntimeError(f"Unsupported action-discovery failure '{failure.code.value}'.")


def _read_actions(
    raw: dict[str, JsonValue],
    token: execution.CancellationToken,
) -> execution.OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.ActionsEnvelope, strict=True).request
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return common._read_failure(
            "pinboard-mcp-actions-result/v1", "ACTIONS_INVALID", f"Cannot discover actions: {error}", None
        )
    match request:
        case contracts.ObserverActionsRequest(action_id=selected_action_id):
            selected_role = decision_models.Role.OBSERVER
            selected_lease = None
            selected_generation = None
        case contracts.ProjectActionsRequest(action_id=selected_action_id):
            selected_role = decision_models.Role.PROJECT
            selected_lease = None
            selected_generation = None
        case contracts.WorkerActionsRequest(
            lease_id=selected_lease_value,
            generation=selected_generation,
            action_id=selected_action_id,
        ):
            selected_role = decision_models.Role.WORKER
            selected_lease = LeaseId(selected_lease_value)
        case contracts.PreparerActionsRequest(
            lease_id=selected_lease_value,
            generation=selected_generation,
            action_id=selected_action_id,
        ):
            selected_role = decision_models.Role.PREPARER
            selected_lease = LeaseId(selected_lease_value)
        case _ as unreachable:
            assert_never(unreachable)
    token.checkpoint()
    selected_action = (
        None
        if selected_action_id is None
        else ActionId(f"{selected_action_id.kind.value}:{selected_action_id.subject}")
    )
    store = common.compose_store(durable)
    selected = actions.select_current_actions(
        store,
        selected_role,
        observed_at=datetime.now(UTC),
        lease_id=selected_lease,
        generation=selected_generation,
        action_id=selected_action,
    )
    if isinstance(selected, DecisionFailure):
        if (
            selected_action_id is not None
            and selected_action_id.kind == decision_models.ActionKind.COMPLETE
            and selected_role == decision_models.Role.PROJECT
        ):
            recovery = _unavailable_completion_recovery(store, AttemptId(selected_action_id.subject))
            if recovery is not None:
                failure = _completion_recovery_failure(recovery, request.project_root, request.work_root)
                return common._read_failure(
                    "pinboard-mcp-actions-result/v1", failure.code.value, failure.message, failure.details
                )
        return common._read_failure(
            "pinboard-mcp-actions-result/v1",
            selected.code.value,
            selected.message,
            _action_failure_details(selected, selected_role, selected_lease, selected_generation, selected_action),
        )
    token.checkpoint()
    projected_actions: list[JsonValue] = []
    for action in selected:
        if selected_action is None and isinstance(action, decision_models.CompleteAction):
            continue
        projected = _mcp_action(
            action,
            store,
            request.project_root,
            request.work_root,
            focused=selected_action is not None,
            artifacts=ArtifactRepository(durable),
        )
        if isinstance(projected, DecisionFailure):
            return common._read_failure(
                "pinboard-mcp-actions-result/v1", projected.code.value, projected.message, projected.details
            )
        projected_actions.append(projected)
    content: dict[str, JsonValue] = {
        "schema": "pinboard-mcp-actions-result/v1",
        "status": "ok",
        "actions": projected_actions,
        "state_changed": False,
        "effect": EffectDisposition.UNCHANGED.value,
        "retry": "safe-to-repeat",
        "changed_surfaces": [],
    }
    return execution.OperationResult(content, "ok", None)


def _mcp_action(
    action: decision_models.Action,
    store: WorkStore,
    project_root: str,
    work_root: str,
    *,
    focused: bool,
    artifacts: ArtifactRepository | None = None,
) -> DecisionResult[dict[str, JsonValue]]:
    """Project a legal action, reading final evidence only for focused completion."""
    projected = actions.project_action(action, include_input_contract=True)
    if not isinstance(projected, action_models.ActionView):
        raise RuntimeError("MCP action discovery requires an inline input contract.")
    input_contract = projected.input_contract
    if isinstance(action, decision_models.CompleteAction) and focused:
        if artifacts is None:
            raise RuntimeError("Focused completion requires artifact access.")
        try:
            completion = actions.completion_input_contract(store, artifacts, action, projected.semantics)
        except ArtifactError as error:
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_AVAILABLE,
                f"Accepted attempt brief could not be verified: {error}",
                FailureDetails(
                    observed=(FailureFact("attempt_id", str(action.capability.subject)),),
                    mismatches=(
                        FailureMismatch(
                            "accepted_brief_bytes",
                            "match the accepted selector, size, and SHA-256",
                            "unreadable or mismatched",
                        ),
                    ),
                    retry=RetryDisposition.DO_NOT_RETRY,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
            )
        if isinstance(completion, query_models.CompletionCandidateRequired):
            return _completion_candidate_failure(completion, project_root, work_root)
        if isinstance(completion, query_models.CompletionRecoveryRequired):
            return _completion_recovery_failure(completion, project_root, work_root)
        if isinstance(completion, DecisionFailure):
            return completion
        input_contract = completion
        record = contracts.CompletionActionView(
            contracts.ActionIdentity(projected.kind, projected.subject),
            projected.label,
            projected.subject_revision,
            projected.authorization,
            projected.lease_id,
            projected.generation,
            projected.semantics,
            completion,
        )
    else:
        record = contracts.ActionView(
            contracts.ActionIdentity(projected.kind, projected.subject),
            projected.label,
            projected.subject_revision,
            projected.authorization,
            projected.lease_id,
            projected.generation,
            projected.semantics,
            input_contract,
        )
    content = msgspec.to_builtins(record)
    assert isinstance(content, dict)
    return content


def _completion_candidate_failure(
    required: query_models.CompletionCandidateRequired,
    project_root: str,
    work_root: str,
) -> DecisionFailure:
    """Present the complete conditional recovery using current MCP requests."""
    attempt_id = required.attempt_id
    roots: dict[str, JsonValue] = {"project_root": project_root, "work_root": work_root}
    claim: dict[str, JsonValue] = {"lease_id": "<current-lease-id>", "generation": "<current-generation>"}
    submit: dict[str, JsonValue] = {"kind": "submit-review", "subject": attempt_id}
    recipes: tuple[tuple[str, str, dict[str, JsonValue]], ...] = (
        ("authority_status", tool_names.ATTEMPT_AUTHORITY_TOOL, {"operation": "status", "attempt_id": attempt_id}),
        (
            "authority_acquisition",
            tool_names.ATTEMPT_AUTHORITY_TOOL,
            {
                "operation": "acquire",
                "attempt_id": attempt_id,
                "task_id": "<worker-task-id>",
                "host_id": "<host-id>",
                "ttl_seconds": 3600,
            },
        ),
        ("candidate_submission_action", tool_names.ACTIONS_TOOL, {"role": "worker", **claim, "action_id": submit}),
        (
            "candidate_submission",
            tool_names.TRANSITION_TOOL,
            {
                "role": "worker",
                **claim,
                "receipt": {"action_id": submit, "subject_revision": "<current-subject-revision>"},
                "payload": {"candidate": "<exact-candidate-revision>"},
            },
        ),
        (
            "completion_reinspection",
            tool_names.ACTIONS_TOOL,
            {
                "role": "project",
                "action_id": {"kind": "complete", "subject": attempt_id},
            },
        ),
    )
    observations = tuple(
        fact
        for prefix, tool, request in recipes
        for fact in (
            FailureFact(prefix + "_tool", tool),
            FailureFact(
                prefix + "_input", msgspec.json.encode({"request": {**roots, **request}}, order="sorted").decode()
            ),
        )
    )
    return DecisionFailure(
        DecisionFailureCode.ACTION_NOT_AVAILABLE,
        "Terminal completion requires a protected review candidate. Inspect authority status; acquire only when status permits it, using the worker's trusted task and host identity. Otherwise use only your own current lease. Discover and submit the exact candidate with the fresh receipt, then repeat focused completion discovery. These instructions do not acquire, submit, review, or complete automatically.",
        FailureDetails(
            observed=(*observations, FailureFact("candidate_payload", '{"candidate":"<exact-candidate-revision>"}')),
            mismatches=(),
            retry=RetryDisposition.REFRESH_ACTION,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def _completion_recovery_failure(
    required: query_models.CompletionRecoveryRequired,
    project_root: str,
    work_root: str,
) -> DecisionFailure:
    action: dict[str, JsonValue] = {"kind": required.route, "subject": required.route_subject}
    request: dict[str, JsonValue] = {
        "request": {
            "project_root": project_root,
            "work_root": work_root,
            "role": "project",
            "action_id": action,
        }
    }
    roots: dict[str, JsonValue] = {"project_root": project_root, "work_root": work_root}
    observations = [
        FailureFact("recovery_action_tool", tool_names.ACTIONS_TOOL),
        FailureFact("recovery_action_input", msgspec.json.encode(request, order="sorted").decode()),
    ]
    if required.alternative_route is not None and required.alternative_subject is not None:
        alternative: dict[str, JsonValue] = {
            "request": {
                **roots,
                "role": "project",
                "action_id": {"kind": required.alternative_route, "subject": required.alternative_subject},
            }
        }
        observations.extend(
            (
                FailureFact("alternative_recovery_action_tool", tool_names.ACTIONS_TOOL),
                FailureFact(
                    "alternative_recovery_action_input", msgspec.json.encode(alternative, order="sorted").decode()
                ),
            )
        )
    exceptional_action: dict[str, JsonValue] = {
        "request": {
            **roots,
            "role": "project",
            "action_id": {"kind": "revise-item", "subject": str(required.item_id)},
        }
    }
    rebind_action: dict[str, JsonValue] = {
        "request": {
            **roots,
            "role": "project",
            "action_id": {"kind": "rebind-attempt", "subject": str(required.attempt_id)},
        }
    }
    review_recovery = required.route == "return-for-correction"
    observations.extend(
        (
            FailureFact(
                "exceptional_recovery_human_decision",
                (
                    "First execute return-for-correction with the scope-supersession reason. Then ask the human to "
                    "approve the exact definition and checkpoint-disposition change before revising the item."
                    if review_recovery
                    else "Ask the human to approve the exact definition and checkpoint-disposition change before "
                    "revising the item."
                ),
            ),
            FailureFact("exceptional_revision_action_tool", tool_names.ACTIONS_TOOL),
            FailureFact(
                "exceptional_revision_action_input", msgspec.json.encode(exceptional_action, order="sorted").decode()
            ),
            FailureFact(
                "exceptional_recovery_after_revision",
                (
                    "Publish a matching pinboard-work-brief/v4, obtain its independent brief review, rebind the now-active "
                    "attempt, dispatch, and submit a new candidate."
                    if review_recovery
                    else "Publish a matching pinboard-work-brief/v4, obtain its independent brief review, then discover "
                    "and execute the exact rebind-attempt action."
                ),
            ),
            FailureFact("exceptional_rebind_action_tool", tool_names.ACTIONS_TOOL),
            FailureFact("exceptional_rebind_action_input", msgspec.json.encode(rebind_action, order="sorted").decode()),
        )
    )
    return DecisionFailure(
        DecisionFailureCode.ACTION_NOT_AVAILABLE,
        required.reason,
        FailureDetails(
            observed=tuple(observations),
            mismatches=(),
            retry=RetryDisposition.REFRESH_ACTION,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def _unavailable_completion_recovery(
    store: WorkStore,
    attempt_id: AttemptId,
) -> query_models.CompletionRecoveryRequired | None:
    """Explain why a focused completion action is absent without broad artifact reads."""
    completion = store.read_completion_context(attempt_id)
    if completion is None or not isinstance(completion.attempt, query_models.NonterminalAttemptContextFacts):
        return None
    attempt = completion.attempt
    item = attempt.item
    if not item.replacement_resolved:
        return query_models.CompletionRecoveryRequired(
            attempt_id,
            attempt.item_id,
            "record-replacement",
            str(attempt.item_id),
            "retain-temporarily",
            str(attempt.item_id),
            "Completion is withheld until the unresolved replacement is recorded or its temporary cost is explicitly retained.",
        )
    if (attempt.accepted_scope_revision, attempt.accepted_scope_digest) != (
        item.current_definition_revision,
        item.current_definition_digest,
    ):
        route = "return-for-correction" if attempt.state == work_models.AttemptState.REVIEW else "rebind-attempt"
        return query_models.CompletionRecoveryRequired(
            attempt_id,
            attempt.item_id,
            route,
            str(attempt_id),
            None,
            None,
            (
                "Completion is withheld because the reviewed candidate must be returned for correction before the "
                "stale accepted attempt binding can be replaced."
                if route == "return-for-correction"
                else "Completion is withheld because the accepted attempt binding is stale against the current definition."
            ),
        )
    return None


def _evidence_reference(path: Path) -> contracts.EvidenceReference:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return contracts.EvidenceAbsent(str(path))
    return contracts.EvidencePresent(str(path), hashlib.sha256(content).hexdigest(), len(content))


def _current_candidate_review(
    durable: DurableRoots,
    store: WorkStore,
    context: query_models.AttemptContextFacts,
    brief: work_brief_models.ReadableWorkBrief | None,
    result: contracts.EvidenceReference,
    review: contracts.EvidenceReference,
) -> tuple[review_operations.CurrentCandidateReview | None, contracts.CandidateReviewReference]:
    if not (
        isinstance(context, query_models.NonterminalAttemptContextFacts)
        and context.state == work_models.AttemptState.REVIEW
        and context.candidate_revision is not None
        and brief is not None
        and isinstance(result, contracts.EvidencePresent)
        and isinstance(review, contracts.EvidencePresent)
    ):
        return None, contracts.CandidateReviewAbsent()
    snapshot = store.read_candidate_snapshot_context(AttemptId(context.attempt_id))
    if snapshot is None:
        return None, contracts.CandidateReviewAbsent()
    current = review_operations.read_current_candidate_review(
        store,
        ArtifactRepository(durable),
        brief=brief,
        candidate_revision=context.candidate_revision,
        candidate_snapshot=snapshot.reference,
        accepted_brief=context.brief_reference,
        result_sha256=result.sha256,
        review_sha256=review.sha256,
    )
    if current is None:
        return None, contracts.CandidateReviewAbsent()
    reference = current.reference
    return current, contracts.CandidateReviewPresent(
        int(reference.artifact_ref_id),
        reference.selector,
        reference.content_sha256,
        reference.size_bytes,
        reference.accepted_revision,
    )


def _relative_action(action_id: str, attempt_id: str, item_id: str) -> contracts.RelativeActionIdentity:
    kind_value, separator, subject = action_id.partition(":")
    if not separator:
        raise RuntimeError("Attempt continuation contains a noncanonical action identity.")
    kind = decision_models.ActionKind(kind_value)
    match decision_models.action_semantics(kind).subject_kind:
        case decision_models.ActionSubjectKind.ATTEMPT:
            if subject != attempt_id:
                raise RuntimeError("Attempt continuation action targets a different attempt.")
            return contracts.RelativeActionIdentity("attempt", kind)
        case decision_models.ActionSubjectKind.ITEM:
            if subject != item_id:
                raise RuntimeError("Attempt continuation action targets a different item.")
            return contracts.RelativeActionIdentity("item", kind)
        case decision_models.ActionSubjectKind.LEDGER | decision_models.ActionSubjectKind.PROPOSAL:
            raise RuntimeError("Attempt continuation contains an unrelated action family.")
        case _ as unreachable:
            assert_never(unreachable)


def _continuation_operation(
    operation: query_models.NonterminalContinuationOperation,
    attempt_id: str,
    item_id: str,
) -> contracts.ContinuationOperation:
    match operation:
        case query_models.ActionContinuation(action_id=action_id, action_kind=action_kind, condition=condition):
            action = _relative_action(action_id, attempt_id, item_id)
            if action.action_kind != action_kind:
                raise RuntimeError("Attempt continuation action kind differs from its identity.")
            return contracts.ContinuationAction(action, condition)
        case query_models.ReviewContinuation(candidate_revision=candidate, required_capability=capability):
            return contracts.ContinuationReview(candidate, capability)
        case query_models.DependencyContinuation(dependencies=dependencies):
            return contracts.ContinuationDependencies(dependencies)
        case query_models.RefreshTargetContinuation(target_revision=target_revision):
            return contracts.ContinuationRefreshTarget(target_revision)
        case query_models.PermissionRecoveryContinuation(target_revision=target_revision, effect=effect, status=status):
            return contracts.ContinuationPermissionRecovery(target_revision, effect, status)
        case query_models.RepositoryDispositionContinuation(target_revision=target_revision, relation=relation):
            return contracts.ContinuationRepositoryDisposition(target_revision, relation)
        case query_models.RepositoryCleanupContinuation(target_revision=target_revision):
            return contracts.ContinuationRepositoryCleanup(target_revision)
        case _ as unreachable:
            assert_never(unreachable)


def _mcp_attempt_continuation(
    continuation: query_models.AttemptContinuation,
) -> contracts.AttemptContinuation:
    common = (
        continuation.schema,
        continuation.attempt_id,
        continuation.item_id,
        continuation.revision,
    )
    match continuation:
        case query_models.TerminalAttemptContinuation():
            return contracts.TerminalAttemptContinuation(
                *common,
                None,
                True,
                False,
                None,
                (),
                continuation.forbidden_routes,
            )
        case (
            query_models.ActiveAttemptContinuation()
            | query_models.ReviewAttemptContinuation()
            | query_models.PausedAttemptContinuation()
            | query_models.BlockedAttemptContinuation()
        ):
            operation = _continuation_operation(
                continuation.next_operation,
                continuation.attempt_id,
                continuation.item_id,
            )
            legal_actions = tuple(
                _relative_action(action_id, continuation.attempt_id, continuation.item_id)
                for action_id in continuation.legal_actions
            )
            arguments = (
                *common,
                continuation.owner_task_id,
                False,
                False,
                operation,
                legal_actions,
                continuation.forbidden_routes,
            )
            match continuation:
                case query_models.ActiveAttemptContinuation():
                    return contracts.ActiveAttemptContinuation(*arguments)
                case query_models.ReviewAttemptContinuation():
                    return contracts.ReviewAttemptContinuation(*arguments)
                case query_models.PausedAttemptContinuation():
                    return contracts.PausedAttemptContinuation(*arguments)
                case query_models.BlockedAttemptContinuation():
                    return contracts.BlockedAttemptContinuation(*arguments)
                case _ as unreachable:
                    assert_never(unreachable)
        case _ as unreachable:
            assert_never(unreachable)


def _attempt_inspection_success(
    continuation: query_models.AttemptContinuation,
    candidate_recovery: contracts.CandidateRecovery,
    candidate_review: contracts.CandidateReviewReference,
    accepted_brief: contracts.AcceptedBriefIdentity | None,
    result: contracts.EvidenceReference,
    review: contracts.EvidenceReference,
    blocker: contracts.EvidenceReference,
) -> dict[str, JsonValue]:
    if isinstance(continuation, query_models.TerminalAttemptContinuation):
        presented_continuation = _mcp_attempt_continuation(continuation)
        if not isinstance(presented_continuation, contracts.TerminalAttemptContinuation):
            raise RuntimeError("Terminal attempt projection changed continuation family.")
        record: contracts.TerminalAttemptInspectionSuccess | contracts.NonterminalAttemptInspectionSuccess = (
            contracts.TerminalAttemptInspectionSuccess(
                "pinboard-mcp-attempt-inspection-result/v1",
                "ok",
                presented_continuation,
                candidate_recovery,
                None,
                result,
                review,
                blocker,
                False,
                "unchanged",
                "safe-to-repeat",
                (),
            )
        )
    else:
        if accepted_brief is None:
            raise RuntimeError("A nonterminal attempt inspection requires its verified accepted brief.")
        presented_continuation = _mcp_attempt_continuation(continuation)
        if isinstance(presented_continuation, contracts.TerminalAttemptContinuation):
            raise RuntimeError("Nonterminal attempt projection changed continuation family.")
        record = contracts.NonterminalAttemptInspectionSuccess(
            "pinboard-mcp-attempt-inspection-result/v1",
            "ok",
            presented_continuation,
            candidate_recovery,
            candidate_review,
            accepted_brief,
            result,
            review,
            blocker,
            False,
            "unchanged",
            "safe-to-repeat",
            (),
        )
    content = msgspec.to_builtins(record)
    assert isinstance(content, dict)
    return content


def _mcp_candidate_recovery(
    durable: DurableRoots,
    store: WorkStore,
    context: query_models.AttemptContextFacts,
    attempt_id: str,
) -> contracts.CandidateRecovery | execution.OperationResult:
    snapshot_context = store.read_candidate_snapshot_context(AttemptId(attempt_id))
    if snapshot_context is None:
        return contracts.CandidateRecoveryAbsent()
    try:
        snapshot_bytes = read_reference(durable.work_root, snapshot_context.reference)
        candidate = (
            context.candidate_revision if isinstance(context, query_models.NonterminalAttemptContextFacts) else None
        )
        evidence = candidate_snapshots.verify_candidate_snapshot_context(snapshot_context, candidate, snapshot_bytes)
    except (ArtifactError, ValueError) as error:
        return common._read_failure(
            "pinboard-mcp-attempt-inspection-result/v1",
            "ATTEMPT_BRIEF_INVALID",
            f"Accepted candidate snapshot could not be verified: {error}",
            FailureDetails(
                observed=(FailureFact("attempt_id", attempt_id),),
                mismatches=(
                    FailureMismatch(
                        "accepted_candidate_snapshot",
                        "canonical bytes matching the accepted receipt",
                        "unreadable, invalid, or mismatched",
                    ),
                ),
                retry=RetryDisposition.DO_NOT_RETRY,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    return common._candidate_recovery_view(durable, evidence)


def _read_attempt_inspection(
    project_root: str,
    work_root: str,
    attempt_id: str,
    reconciliation: dict[str, JsonValue] | None,
    token: execution.CancellationToken,
) -> execution.OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(
            {
                "project_root": project_root,
                "work_root": work_root,
                "attempt_id": attempt_id,
                "reconciliation": reconciliation,
            },
            type=contracts.AttemptInspectRequest,
            strict=True,
        )
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return common._read_failure(
            "pinboard-mcp-attempt-inspection-result/v1",
            "ATTEMPT_INSPECT_INVALID",
            f"Cannot inspect attempt: {error}",
            None,
        )
    store = common.compose_store(durable)
    context = queries.select_attempt_context(store, AttemptId(request.attempt_id))
    if isinstance(context, DecisionFailure):
        return common._read_failure(
            "pinboard-mcp-attempt-inspection-result/v1",
            "ATTEMPT_NOT_FOUND",
            context.message,
            context.details,
        )
    token.checkpoint()
    accepted_brief: contracts.AcceptedBriefIdentity | None = None
    owner_task_id: TaskId | None = None
    decoded_brief: work_brief_models.ReadableWorkBrief | None = None
    if isinstance(context, query_models.NonterminalAttemptContextFacts):
        reference = context.brief_reference
        try:
            brief = work_briefs.decode_canonical_work_brief(read_reference(durable.work_root, reference))
        except ArtifactError as error:
            return common._read_failure(
                "pinboard-mcp-attempt-inspection-result/v1",
                "ATTEMPT_BRIEF_INVALID",
                f"Accepted attempt brief could not be verified: {error}",
                FailureDetails(
                    observed=(
                        FailureFact("attempt_id", request.attempt_id),
                        FailureFact("artifact_ref_id", int(context.brief_artifact_ref_id)),
                        FailureFact("selector", reference.selector),
                        FailureFact("sha256", reference.content_sha256),
                        FailureFact("size_bytes", reference.size_bytes),
                    ),
                    mismatches=(
                        FailureMismatch(
                            "accepted_brief_bytes",
                            "match the accepted selector, size, and SHA-256",
                            "unreadable or mismatched",
                        ),
                    ),
                    retry=RetryDisposition.DO_NOT_RETRY,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
            )
        if isinstance(brief, work_brief_models.WorkBriefFailure):
            return common._read_failure(
                "pinboard-mcp-attempt-inspection-result/v1",
                "ATTEMPT_BRIEF_INVALID",
                brief.message,
                FailureDetails(
                    observed=(
                        FailureFact("attempt_id", request.attempt_id),
                        FailureFact("artifact_ref_id", int(context.brief_artifact_ref_id)),
                        FailureFact("selector", reference.selector),
                    ),
                    mismatches=(
                        FailureMismatch("accepted_brief", "canonical work brief", "invalid canonical content"),
                    ),
                    retry=RetryDisposition.DO_NOT_RETRY,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
            )
        if (failure := queries.validate_attempt_brief_identity(context, brief)) is not None:
            return common._read_failure(
                "pinboard-mcp-attempt-inspection-result/v1",
                "ATTEMPT_BRIEF_INVALID",
                failure.message,
                FailureDetails(
                    observed=(
                        FailureFact("attempt_id", request.attempt_id),
                        FailureFact("brief_attempt_id", brief.attempt_id),
                        FailureFact("brief_item_id", brief.item_id),
                        FailureFact("brief_scope_revision", brief.accepted_scope.revision),
                        FailureFact("brief_scope_digest", brief.accepted_scope.digest),
                    ),
                    mismatches=(FailureMismatch("accepted_brief_identity", "match the current attempt", "different"),),
                    retry=RetryDisposition.DO_NOT_RETRY,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
            )
        owner_task_id = TaskId(brief.owner_task_id)
        decoded_brief = brief
        stored_reference = store.read_artifact_reference_by_id(context.brief_artifact_ref_id)
        if stored_reference is None or (
            stored_reference.selector,
            stored_reference.content_sha256,
            stored_reference.size_bytes,
        ) != (reference.selector, reference.content_sha256, reference.size_bytes):
            return common._read_failure(
                "pinboard-mcp-attempt-inspection-result/v1",
                "ATTEMPT_BRIEF_INVALID",
                "Accepted brief reference identity differs from the attempt.",
                FailureDetails(
                    observed=(
                        FailureFact("attempt_id", request.attempt_id),
                        FailureFact("artifact_ref_id", int(context.brief_artifact_ref_id)),
                        FailureFact("selector", reference.selector),
                    ),
                    mismatches=(
                        FailureMismatch(
                            "accepted_brief_reference",
                            "match the attempt brief reference",
                            "absent or different",
                        ),
                    ),
                    retry=RetryDisposition.DO_NOT_RETRY,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
            )
        accepted_brief = contracts.AcceptedBriefIdentity(
            int(context.brief_artifact_ref_id),
            str(durable.work_root / reference.selector),
            reference.selector,
            reference.content_sha256,
            reference.size_bytes,
            stored_reference.accepted_revision,
            context.accepted_scope_revision,
            context.accepted_scope_digest,
        )
    attempt_root = durable.work_root / "attempts" / request.attempt_id
    try:
        result = _evidence_reference(attempt_root / "result.md")
        review = _evidence_reference(attempt_root / "review.md")
        blocker = _evidence_reference(attempt_root / "blocker.md")
    except OSError as error:
        return common._read_failure(
            "pinboard-mcp-attempt-inspection-result/v1",
            "ATTEMPT_BRIEF_INVALID",
            f"Attempt evidence could not be inspected: {error}",
            FailureDetails(
                observed=(FailureFact("attempt_id", request.attempt_id),),
                mismatches=(FailureMismatch("attempt_evidence", "readable regular files or absence", "unreadable"),),
                retry=RetryDisposition.DO_NOT_RETRY,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    token.checkpoint()
    recovery = _mcp_candidate_recovery(durable, store, context, request.attempt_id)
    if isinstance(recovery, execution.OperationResult):
        return recovery
    current_review, candidate_review = _current_candidate_review(durable, store, context, decoded_brief, result, review)
    continuation = queries.project_attempt_continuation(
        context,
        owner_task_id,
        decoded_brief,
        request.reconciliation,
        current_review is not None,
    )
    if isinstance(continuation, DecisionFailure):
        details = continuation.details
        if details is None:
            details = FailureDetails(
                observed=(FailureFact("attempt_id", request.attempt_id),),
                mismatches=(FailureMismatch("continuation", "currently legal action", "unavailable"),),
                retry=RetryDisposition.REFRESH_ACTION,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            )
        return common._read_failure(
            "pinboard-mcp-attempt-inspection-result/v1",
            "ACTION_NOT_AVAILABLE",
            continuation.message,
            details,
        )
    content = _attempt_inspection_success(
        continuation,
        recovery,
        candidate_review,
        accepted_brief,
        result,
        review,
        blocker,
    )
    return execution.OperationResult(content, "ok", str(context.project_revision))


def _verify_artifact(
    project_root: str,
    work_root: str,
    artifact_ref_id: IntegerBoundaryValue,
    selector: str,
    sha256: str,
    size_bytes: IntegerBoundaryValue,
    token: execution.CancellationToken,
) -> execution.OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(
            {
                "project_root": project_root,
                "work_root": work_root,
                "artifact_ref_id": artifact_ref_id,
                "selector": selector,
                "sha256": sha256,
                "size_bytes": size_bytes,
            },
            type=contracts.ArtifactVerifyRequest,
            strict=True,
        )
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return common._read_failure(
            "pinboard-mcp-artifact-verification-result/v1",
            "ARTIFACT_VERIFY_INVALID",
            f"Cannot verify artifact: {error}",
            None,
        )
    store = common.compose_store(durable)
    reference = store.read_artifact_reference_by_id(ArtifactRefId(request.artifact_ref_id))
    if reference is None:
        details = FailureDetails(
            (FailureFact("artifact_ref_id", request.artifact_ref_id),),
            (FailureMismatch("accepted_artifact_reference", "present", "absent"),),
            RetryDisposition.CORRECT_INPUT,
            EffectDisposition.UNCHANGED,
            (),
            (),
        )
        return common._read_failure(
            "pinboard-mcp-artifact-verification-result/v1",
            "ARTIFACT_REFERENCE_MISMATCH",
            "The accepted artifact reference does not exist.",
            details,
        )
    mismatches = tuple(
        FailureMismatch(field, expected, observed)
        for field, expected, observed in (
            ("selector", reference.selector, request.selector),
            ("sha256", reference.content_sha256, request.sha256),
            ("size_bytes", reference.size_bytes, request.size_bytes),
        )
        if expected != observed
    )
    observations = (
        FailureFact("artifact_ref_id", request.artifact_ref_id),
        FailureFact("selector", reference.selector),
        FailureFact("sha256", reference.content_sha256),
        FailureFact("size_bytes", reference.size_bytes),
    )
    if mismatches:
        return common._read_failure(
            "pinboard-mcp-artifact-verification-result/v1",
            "ARTIFACT_REFERENCE_MISMATCH",
            "The supplied facts do not match the accepted artifact reference.",
            FailureDetails(
                observations,
                mismatches,
                RetryDisposition.CORRECT_INPUT,
                EffectDisposition.UNCHANGED,
                (),
                (),
            ),
        )
    try:
        read_reference(durable.work_root, reference)
    except ArtifactError:
        return common._read_failure(
            "pinboard-mcp-artifact-verification-result/v1",
            "ARTIFACT_BYTES_INVALID",
            "The immutable artifact bytes do not match the accepted reference.",
            FailureDetails(
                observations,
                (
                    FailureMismatch(
                        "artifact_bytes", "match accepted selector, size, and SHA-256", "unreadable or mismatched"
                    ),
                ),
                RetryDisposition.DO_NOT_RETRY,
                EffectDisposition.UNCHANGED,
                (),
                (),
            ),
        )
    token.checkpoint()
    content = msgspec.to_builtins(
        contracts.ArtifactVerified(
            "pinboard-verified-artifact-reference/v1",
            request.artifact_ref_id,
            reference.selector,
            reference.content_sha256,
            reference.size_bytes,
            reference.accepted_revision,
            True,
            False,
            "unchanged",
            "safe-to-repeat",
            (),
        )
    )
    assert isinstance(content, dict)
    return execution.OperationResult(content, "ok", str(reference.artifact_ref_id))
