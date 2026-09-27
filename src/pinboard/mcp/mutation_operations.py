"""Proposal, brief, lifecycle, and authority MCP mutation composition."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import assert_never
from uuid import uuid4

import msgspec

from pinboard.adapters import (
    lifecycle_artifacts,
    lifecycle_operations,
)
from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.files.models import AffectedViews, ViewWarning
from pinboard.adapters.files.root import resolve_shared_repository_root, resolve_source_checkout_root
from pinboard.application import (
    authority_operations,
    ports,
    proposal_models,
    proposals,
    query_models,
    service,
    work_brief_models,
    work_briefs,
)
from pinboard.application.artifact_publication import ArtifactAcceptanceFailure, ArtifactWriteFailure
from pinboard.application.mutation_models import CommittedEffect
from pinboard.domain import decision_models
from pinboard.domain.errors import (
    ChangedSurface,
    DecisionFailure,
    DecisionFailureCode,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    RetryDisposition,
)
from pinboard.domain.identifiers import (
    ActionId,
    AttemptId,
    HostId,
    LeaseId,
    TaskId,
    WorkItemId,
)
from pinboard.mcp import common, contracts, execution, tool_names
from pinboard.mcp.contracts import JsonValue

_ARCHITECTURE_IMPACT_KIND_PATH = "$.brief.checkpoint.architecture_impact.kind"
_ARCHITECTURE_IMPACT_KINDS = ("none", "read-only", "update-required")


def _proposal_failure(failure: proposal_models.ProposalFailure | DecisionFailure) -> execution.OperationResult:
    details = common._details_json(failure.details)
    if failure.details is None and failure.code == DecisionFailureCode.PROPOSAL_ALREADY_EXISTS:
        details["retry"] = RetryDisposition.DO_NOT_RETRY.value
    content: dict[str, JsonValue] = {
        "schema": "pinboard-mcp-proposal-result/v2",
        "status": "rejected",
        "code": failure.code.value,
        "message": failure.message,
        "state_changed": details["effect"] == EffectDisposition.COMMITTED.value,
        **details,
    }
    if failure.code == DecisionFailureCode.PROPOSAL_ALREADY_EXISTS:
        content["recovery"] = "Read item status for the proposal_id before deciding whether any new proposal is needed."
    else:
        content["recovery"] = None
    return execution.OperationResult(content, "rejected", None)


def _brief_failure(failure: work_brief_models.WorkBriefFailure | DecisionFailure) -> execution.OperationResult:
    details = common._details_json(failure.details if isinstance(failure, DecisionFailure) else None)
    return execution.OperationResult(
        {
            "schema": "pinboard-mcp-brief-publication-result/v1",
            "status": "rejected",
            "code": failure.code.value,
            "message": failure.message,
            "state_changed": details["effect"] == EffectDisposition.COMMITTED.value,
            **details,
        },
        "rejected",
        None,
    )


def _brief_decode_failure(
    project_root: str,
    work_root: str,
    brief: dict[str, work_brief_models.WorkBriefJsonValue],
    error: msgspec.ValidationError,
) -> execution.OperationResult:
    failure = work_brief_models.WorkBriefFailure(
        work_brief_models.WorkBriefErrorCode.BRIEF_INVALID,
        f"Cannot decode brief publication request: {error}",
    )
    if not str(error).endswith(f" - at `{_ARCHITECTURE_IMPACT_KIND_PATH}`"):
        return _brief_failure(failure)
    checkpoint = brief.get("checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("boundary") != "cross-boundary":
        return _brief_failure(failure)
    architecture_impact = checkpoint.get("architecture_impact")
    if not isinstance(architecture_impact, dict):
        return _brief_failure(failure)
    kind = architecture_impact.get("kind")
    if not isinstance(kind, str) or kind in _ARCHITECTURE_IMPACT_KINDS:
        return _brief_failure(failure)
    result = _brief_failure(failure)
    return execution.OperationResult(
        {
            **result.content,
            "observed": [{"field": _ARCHITECTURE_IMPACT_KIND_PATH, "value": kind}],
            "mismatches": [
                {
                    "field": _ARCHITECTURE_IMPACT_KIND_PATH,
                    "expected": "none | read-only | update-required",
                    "observed": kind,
                }
            ],
            "allowed_selections": list[JsonValue](_ARCHITECTURE_IMPACT_KINDS),
            "recovery": {
                "tool": tool_names.BRIEF_CONTRACT_TOOL,
                "arguments": {
                    "request": {
                        "operation": "starter",
                        "project_root": project_root,
                        "work_root": work_root,
                        "boundary": "cross-boundary",
                    }
                },
            },
        },
        "rejected",
        None,
    )


def _proposal_created(
    project_root: str,
    work_root: str,
    proposal: dict[str, proposal_models.ProposalJsonValue],
    actor_task_id: str,
    actor_host_id: str,
    token: execution.CancellationToken,
) -> execution.OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(
            {
                "project_root": project_root,
                "work_root": work_root,
                "proposal": proposal,
                "actor_task_id": actor_task_id,
                "actor_host_id": actor_host_id,
            },
            type=contracts.ProposalCreateRequest,
            strict=True,
        )
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return _proposal_failure(
            proposal_models.ProposalFailure(
                DecisionFailureCode.PROPOSAL_INVALID,
                f"Cannot decode proposal request: {error}",
                None,
            )
        )
    decoded = request.proposal
    store = common.compose_store(durable)
    token.checkpoint()
    now = datetime.now(UTC)
    committed = service.create_proposal(
        store,
        proposals.convert_proposal(decoded),
        now,
        actor_task_id=TaskId(request.actor_task_id),
        actor_host_id=HostId(request.actor_host_id),
    )
    if isinstance(committed, DecisionFailure):
        return _proposal_failure(committed)
    view_result = common._refresh_affected_views(
        durable,
        store,
        AffectedViews(committed.work_item_ids, committed.attempt_ids, (committed.receipt.history_id,)),
        now,
    )
    status = store.read_item_status(WorkItemId(decoded.proposal_id))
    if status is None or status.work_item.queue_position is None:
        raise RuntimeError("Committed proposal status did not reload exactly.")
    warning = view_result.warning
    content: dict[str, JsonValue] = {
        "schema": "pinboard-mcp-proposal-result/v2",
        "status": "committed" if warning is None else "committed-with-warning",
        "proposal_id": decoded.proposal_id,
        "position": status.work_item.queue_position,
        "item_state": status.work_item.state.value,
        "committed_revision": committed.receipt.project_revision,
        "history_id": int(committed.receipt.history_id),
        "state_changed": True,
        "effect": EffectDisposition.COMMITTED.value,
        "retry": RetryDisposition.DO_NOT_RETRY.value,
        "changed_surfaces": [ChangedSurface.LEDGER.value],
        "continuation": "Read item status or inspect the proposal before choosing a project disposition.",
        "warning": None if warning is None else {"message": warning.message, "recovery": warning.repair},
    }
    return execution.OperationResult(
        content, "committed" if warning is None else "committed-warning", str(committed.receipt.project_revision)
    )


def _brief_published(
    project_root: str,
    work_root: str,
    brief: dict[str, work_brief_models.WorkBriefJsonValue],
    token: execution.CancellationToken,
) -> execution.OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(
            {"project_root": project_root, "work_root": work_root, "brief": brief},
            type=contracts.BriefPublishRequest,
            strict=True,
        )
        durable = common._resolve_durable(request.project_root, request.work_root)
    except msgspec.ValidationError as error:
        return _brief_decode_failure(project_root, work_root, brief, error)
    except (ValueError, OSError) as error:
        return _brief_failure(
            work_brief_models.WorkBriefFailure(
                work_brief_models.WorkBriefErrorCode.BRIEF_INVALID,
                f"Cannot decode brief publication request: {error}",
            )
        )
    decoded = request.brief
    store = common.compose_store(durable)
    token.checkpoint()
    now = datetime.now(UTC)
    publication = work_briefs.publish_work_brief(store, ArtifactRepository(durable), decoded, now)
    if isinstance(publication, (DecisionFailure, work_brief_models.WorkBriefFailure)):
        return _brief_failure(publication)
    if isinstance(publication, (ArtifactAcceptanceFailure, ArtifactWriteFailure)):
        return common._artifact_publication_failure(
            "pinboard-mcp-brief-publication-result/v1",
            publication,
            acceptance_message="The brief's accepted reference could not be committed.",
            publication_message="The brief's immutable publication could not be completed.",
            acceptance_recovery="Preserve the published selector and repair artifact-reference acceptance before continuing.",
            publication_recovery="Inspect the published selector and repair immutable publication before continuing.",
        )
    view_result = common._refresh_affected_views(durable, store, AffectedViews((), (), ()), now)
    warning = view_result.warning
    changed_surfaces: list[JsonValue] = [
        *([ChangedSurface.IMMUTABLE_ARTIFACT.value] if publication.artifact_created else []),
        *(
            [ChangedSurface.ACCEPTED_ARTIFACT_REFERENCE.value, ChangedSurface.LEDGER.value]
            if publication.ledger_changed
            else []
        ),
    ]
    state_changed = bool(changed_surfaces)
    if state_changed:
        status = "committed" if warning is None else "committed-with-warning"
        classification = "committed" if warning is None else "committed-warning"
    else:
        status = "unchanged" if warning is None else "unchanged-with-warning"
        classification = "unchanged" if warning is None else "unchanged-warning"
    content: dict[str, JsonValue] = {
        "schema": "pinboard-mcp-brief-publication-result/v1",
        "status": status,
        "reference": common._artifact_reference_json(publication.reference),
        "state_changed": state_changed,
        "effect": (EffectDisposition.COMMITTED.value if state_changed else EffectDisposition.UNCHANGED.value),
        "retry": (RetryDisposition.DO_NOT_RETRY.value if state_changed else RetryDisposition.RETRY_SAME_INPUT.value),
        "changed_surfaces": changed_surfaces,
        "continuation": "Use the verified reference when preparing or rebinding the matching attempt.",
        "warning": None if warning is None else {"message": warning.message, "recovery": warning.repair},
    }
    return execution.OperationResult(
        content,
        classification,
        str(publication.reference.artifact_ref_id),
    )


def _transition_rejected(
    action_id: contracts.ActionIdentity,
    failure: DecisionFailure,
) -> execution.OperationResult:
    details = common._details_json(failure.details)
    retry = _rejection_retry(failure)
    details["retry"] = retry.value
    match retry:
        case RetryDisposition.CORRECT_INPUT:
            continuation = (
                "Correct the reported input, then discover this exact current action through pinboard_actions."
            )
        case RetryDisposition.REFRESH_ACTION | RetryDisposition.RETRY_SAME_INPUT:
            continuation = "Discover this exact current action through pinboard_actions before another transition."
        case RetryDisposition.REACQUIRE_AUTHORITY:
            if failure.code in {
                DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
                DecisionFailureCode.ATTEMPT_LEASE_REQUIRED,
                DecisionFailureCode.ATTEMPT_LEASE_EXPIRED,
            }:
                continuation = (
                    "Check pinboard_attempt_authority status, reacquire only if permitted, "
                    "then discover this action through pinboard_actions."
                )
            else:
                continuation = (
                    "Check the relevant authority status, reacquire only if permitted, "
                    "then discover this action through pinboard_actions."
                )
        case RetryDisposition.DO_NOT_RETRY:
            continuation = _transition_current_state_route(action_id.kind)
        case _ as unreachable:
            assert_never(unreachable)
    return execution.OperationResult(
        {
            "schema": "pinboard-mcp-transition-result/v1",
            "status": "rejected",
            "action_id": common._transition_action_json(action_id),
            "code": failure.code.value,
            "message": failure.message,
            "state_changed": False,
            **details,
            "continuation": continuation,
        },
        "rejected",
        None,
    )


def _transition_current_state_route(kind: decision_models.ActionKind) -> str:
    match decision_models.action_semantics(kind).subject_kind:
        case decision_models.ActionSubjectKind.ATTEMPT:
            return "Inspect this attempt with pinboard_attempt_inspect and follow its next_operation; do not replay."
        case decision_models.ActionSubjectKind.ITEM:
            return "Read this item with pinboard_item_status, then discover its current action; do not replay."
        case decision_models.ActionSubjectKind.PROPOSAL:
            return "Read the current proposal or item in pinboard_overview before another action; do not replay."
        case decision_models.ActionSubjectKind.LEDGER:
            return "Read the current pinboard_overview before another action; do not replay."
        case _ as unreachable:
            assert_never(unreachable)


def _with_completion_reinspection(
    failure: DecisionFailure,
    identity: contracts.ActionIdentity,
    project_root: str,
    work_root: str,
) -> DecisionFailure:
    """Attach the read-only focused route after any rejected completion receipt."""
    if identity.kind != decision_models.ActionKind.COMPLETE:
        return failure
    details = failure.details
    request: dict[str, JsonValue] = {
        "request": {
            "project_root": project_root,
            "work_root": work_root,
            "role": "project",
            "action_id": {"kind": "complete", "subject": identity.subject},
        }
    }
    return DecisionFailure(
        failure.code,
        failure.message,
        FailureDetails(
            observed=(
                *(() if details is None else details.observed),
                FailureFact("completion_reinspection_tool", tool_names.ACTIONS_TOOL),
                FailureFact("completion_reinspection_input", msgspec.json.encode(request, order="sorted").decode()),
            ),
            mismatches=() if details is None else details.mismatches,
            retry=RetryDisposition.REFRESH_ACTION,
            effect=EffectDisposition.UNCHANGED if details is None else details.effect,
            changed_surfaces=() if details is None else details.changed_surfaces,
            alternatives=() if details is None else details.alternatives,
        ),
    )


def _with_retained_brief_recovery(
    failure: DecisionFailure,
    identity: contracts.ActionIdentity,
    project_root: str,
    work_root: str,
) -> DecisionFailure:
    """Expose the exact current-brief route after retained-brief submission rejection."""
    details = failure.details
    if (
        identity.kind != decision_models.ActionKind.SUBMIT_REVIEW
        or details is None
        or not any(
            fact.field == "accepted_brief_schema" and fact.value in {"pinboard-work-brief/v2", "pinboard-work-brief/v3"}
            for fact in details.observed
        )
    ):
        return failure
    roots: dict[str, JsonValue] = {"project_root": project_root, "work_root": work_root}
    action: dict[str, JsonValue] = {"kind": "rebind-attempt", "subject": identity.subject}
    action_request: dict[str, JsonValue] = {"request": {**roots, "role": "project", "action_id": action}}
    transition_request: dict[str, JsonValue] = {
        "request": {
            **roots,
            "role": "project",
            "actor_task_id": "<owning-task-id>",
            "actor_host_id": "<host-id>",
            "receipt": {"action_id": action, "subject_revision": "<current-subject-revision>"},
            "payload": {
                "branch": "<accepted-brief-branch>",
                "base_revision": "<accepted-brief-base-revision>",
                "brief_artifact_ref_id": "<published-v4-artifact-ref-id>",
            },
        }
    }
    publication: dict[str, JsonValue] = {
        **roots,
        "brief": "<complete-matching-pinboard-work-brief/v4>",
    }
    return DecisionFailure(
        failure.code,
        failure.message,
        FailureDetails(
            observed=(
                *details.observed,
                FailureFact("brief_publication_tool", tool_names.BRIEF_PUBLISH_TOOL),
                FailureFact("brief_publication_input", msgspec.json.encode(publication, order="sorted").decode()),
                FailureFact(
                    "brief_review_requirement",
                    "Obtain an independent ready review of the exact published v4 brief before binding it.",
                ),
                FailureFact("brief_binding_action_tool", tool_names.ACTIONS_TOOL),
                FailureFact("brief_binding_action_input", msgspec.json.encode(action_request, order="sorted").decode()),
                FailureFact("brief_binding_transition_tool", tool_names.TRANSITION_TOOL),
                FailureFact(
                    "brief_binding_transition_input",
                    msgspec.json.encode(transition_request, order="sorted").decode(),
                ),
                FailureFact(
                    "brief_binding_next_step",
                    "After rebind, dispatch the reviewed v4 brief and submit a freshly observed candidate through the worker's own lease.",
                ),
            ),
            mismatches=details.mismatches,
            retry=RetryDisposition.REFRESH_ACTION,
            effect=details.effect,
            changed_surfaces=details.changed_surfaces,
            alternatives=details.alternatives,
        ),
    )


def _transition(  # noqa: PLR0912, PLR0915 - one strict request-to-terminal-result boundary
    raw: dict[str, JsonValue],
    token: execution.CancellationToken,
) -> execution.OperationResult:
    token.checkpoint()
    try:
        request = contracts.decode_transition_request(raw)
        source_checkout = resolve_source_checkout_root(Path(request.project_root))
        durable = common._require_initialized_durable(
            resolve_shared_repository_root(source_checkout), Path(request.work_root)
        )
    except (msgspec.ValidationError, ValueError, OSError) as error:
        inner = raw.get("request")
        receipt = inner.get("receipt") if isinstance(inner, dict) else None
        raw_identity = receipt.get("action_id") if isinstance(receipt, dict) else None
        try:
            identity = msgspec.convert(raw_identity, type=contracts.ActionIdentity, strict=True)
        except msgspec.ValidationError:
            identity = contracts.ActionIdentity(decision_models.ActionKind.CLOSE, "invalid")
        return _transition_rejected(
            identity,
            DecisionFailure(
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                f"Cannot decode transition request: {error}",
                None,
            ),
        )
    match request:
        case contracts.ProjectTransitionRequest(actor_task_id=task_id, actor_host_id=host_id):
            selected_role = decision_models.Role.PROJECT
            selected_lease = None
            selected_generation = 0
            selected_task = TaskId(task_id)
            selected_host = HostId(host_id)
        case contracts.WorkerTransitionRequest(lease_id=selected_lease_value, generation=selected_generation):
            selected_role = decision_models.Role.WORKER
            selected_lease = LeaseId(selected_lease_value)
            selected_task = None
            selected_host = None
        case contracts.PreparerTransitionRequest(lease_id=selected_lease_value, generation=selected_generation):
            selected_role = decision_models.Role.PREPARER
            selected_lease = LeaseId(selected_lease_value)
            selected_task = None
            selected_host = None
        case _ as unreachable:
            assert_never(unreachable)
    identity = contracts.ActionIdentity(
        decision_models.ActionKind(request.receipt.action_id.kind), request.receipt.action_id.subject
    )
    action_id = ActionId(f"{identity.kind.value}:{identity.subject}")
    store = common.compose_store(durable)
    artifacts = ArtifactRepository(durable)
    operation_time = datetime.now(UTC)
    selected = lifecycle_operations.select_transition(
        source_checkout,
        store,
        artifacts,
        lifecycle_operations.TransitionReceipt(
            selected_role,
            action_id,
            request.receipt.subject_revision,
            selected_lease,
            selected_generation,
            selected_task,
            selected_host,
        ),
        request.payload,
        operation_time,
    )
    if isinstance(selected, DecisionFailure):
        return _transition_rejected(
            identity,
            _with_completion_reinspection(selected, identity, request.project_root, request.work_root),
        )
    token.checkpoint()
    if isinstance(
        selected.command,
        (
            decision_models.AcceptCheckpointCommand,
            decision_models.CoveredCompleteCommand,
            decision_models.SubmitReviewCommand,
        ),
    ):
        committed = lifecycle_artifacts.execute_artifact_transition(
            source_checkout,
            durable.work_root,
            store,
            artifacts,
            selected,
            operation_time,
            lambda: datetime.now(UTC),
        )
    else:
        committed = lifecycle_operations.commit_direct_transition(
            store, artifacts, selected, operation_time, lambda: datetime.now(UTC)
        )
    if isinstance(committed, lifecycle_artifacts.PublishedTransitionFailure):
        details = common._details_json(committed.details)
        return execution.OperationResult(
            {
                "schema": "pinboard-mcp-transition-result/v1",
                "status": "failed-after-publication",
                "action_id": common._transition_action_json(identity),
                "code": committed.code,
                "message": committed.message,
                "state_changed": True,
                **details,
                "continuation": _transition_current_state_route(identity.kind),
            },
            "failed-after-publication",
            None,
        )
    if isinstance(committed, DecisionFailure):
        if committed.details is not None and committed.details.effect == EffectDisposition.COMMITTED:
            details = common._details_json(committed.details)
            return execution.OperationResult(
                {
                    "schema": "pinboard-mcp-transition-result/v1",
                    "status": "failed-after-publication",
                    "action_id": common._transition_action_json(identity),
                    "code": committed.code.value,
                    "message": committed.message,
                    "state_changed": True,
                    **details,
                    "continuation": _transition_current_state_route(identity.kind),
                },
                "failed-after-publication",
                None,
            )
        return _transition_rejected(
            identity,
            _with_retained_brief_recovery(
                _with_completion_reinspection(committed, identity, request.project_root, request.work_root),
                identity,
                request.project_root,
                request.work_root,
            ),
        )
    changed_surfaces: list[JsonValue]
    if isinstance(committed, lifecycle_artifacts.ArtifactTransitionSuccess):
        committed_effect = committed.effect
        changed_surfaces = [
            *([ChangedSurface.IMMUTABLE_ARTIFACT.value] if committed.published_selectors else []),
            ChangedSurface.ACCEPTED_ARTIFACT_REFERENCE.value,
            ChangedSurface.LEDGER.value,
        ]
    else:
        committed_effect = committed
        changed_surfaces = [ChangedSurface.LEDGER.value]
    refreshed = common._refresh_affected_views(
        durable,
        store,
        AffectedViews(
            committed_effect.work_item_ids,
            committed_effect.attempt_ids,
            (committed_effect.receipt.history_id,),
        ),
        datetime.now(UTC),
    )
    warning = refreshed.warning
    return execution.OperationResult(
        {
            "schema": "pinboard-mcp-transition-result/v1",
            "status": "committed" if warning is None else "committed-with-warning",
            "action_id": common._transition_action_json(identity),
            "committed_revision": committed_effect.receipt.project_revision,
            "history_id": int(committed_effect.receipt.history_id),
            "state_changed": True,
            "effect": EffectDisposition.COMMITTED.value,
            "retry": RetryDisposition.DO_NOT_RETRY.value,
            "changed_surfaces": changed_surfaces,
            "continuation": _transition_current_state_route(identity.kind),
            "warning": None if warning is None else {"message": warning.message, "recovery": warning.repair},
        },
        "committed" if warning is None else "committed-warning",
        str(committed_effect.receipt.project_revision),
    )


def _rejection_retry(failure: DecisionFailure) -> RetryDisposition:
    if failure.details is not None:
        return failure.details.retry
    if failure.code in {
        DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
        DecisionFailureCode.ATTEMPT_LEASE_EXPIRED,
        DecisionFailureCode.ATTEMPT_LEASE_REQUIRED,
        DecisionFailureCode.LEASE_FENCED,
    }:
        return RetryDisposition.REACQUIRE_AUTHORITY
    if failure.code in {
        DecisionFailureCode.ACTION_NOT_AVAILABLE,
        DecisionFailureCode.ITEM_DEFINITION_STALE,
    }:
        return RetryDisposition.REFRESH_ACTION
    return RetryDisposition.CORRECT_INPUT


def _authority_rejection_details(failure: DecisionFailure) -> dict[str, JsonValue]:
    if failure.details is not None:
        return common._details_json(failure.details)
    return {
        "effect": EffectDisposition.UNCHANGED.value,
        "retry": _rejection_retry(failure).value,
        "changed_surfaces": [],
        "observed": [],
        "mismatches": [],
    }


def _authority_identity(
    retained: query_models.PreparationAuthorityStatus | query_models.AttemptAuthorityStatus,
) -> dict[str, JsonValue]:
    """Render shared authority claims without erasing the family's scope or status enum."""
    return {
        "task_id": retained.task_id,
        "host_id": retained.host_id,
        "lease_id": retained.lease_id,
        "generation": retained.generation,
        "expires_at": retained.expires_at.isoformat(),
        "authority_status": retained.status.value,
    }


def _authority_conflict(
    retained: query_models.PreparationAuthorityStatus | query_models.AttemptAuthorityStatus | None,
) -> dict[str, JsonValue] | None:
    return None if retained is None else _authority_identity(retained)


def _authority_rejected(
    schema: str,
    identity: dict[str, JsonValue],
    failure: DecisionFailure,
    conflict: dict[str, JsonValue] | None,
) -> execution.OperationResult:
    return execution.OperationResult(
        {
            "schema": schema,
            "status": "rejected",
            **identity,
            "code": failure.code.value,
            "message": failure.message,
            "conflict": conflict,
            "state_changed": False,
            **_authority_rejection_details(failure),
        },
        "rejected",
        None,
    )


def _refresh_authority_views(
    durable: DurableRoots, store: ports.WorkStore, effect: CommittedEffect, now: datetime
) -> ViewWarning | None:
    return common._refresh_affected_views(
        durable,
        store,
        AffectedViews(effect.work_item_ids, effect.attempt_ids, (effect.receipt.history_id,)),
        now,
    ).warning


def _authority_status_fields(
    retained: query_models.PreparationAuthorityStatus | query_models.AttemptAuthorityStatus | None,
) -> dict[str, JsonValue]:
    """Render read-only authority presence; callers retain exact family-specific identity."""
    return {
        "status": "absent" if retained is None else "present",
        **(
            {}
            if retained is None
            else {**_authority_identity(retained), "acquired_at": retained.acquired_at.isoformat()}
        ),
        "state_changed": False,
        "effect": EffectDisposition.UNCHANGED.value,
        "retry": "safe-to-repeat",
        "changed_surfaces": [],
    }


def _preparation_authority(
    raw: dict[str, JsonValue],
    token: execution.CancellationToken,
) -> execution.OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.PreparationAuthorityEnvelope, strict=True).request
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        inner = raw.get("request")
        subject = inner.get("item_id") if isinstance(inner, dict) else None
        return execution.OperationResult(
            {
                "schema": "pinboard-mcp-preparation-authority-result/v1",
                "status": "rejected",
                "item_id": subject if isinstance(subject, str) and subject else "invalid",
                "code": DecisionFailureCode.TRANSITION_INPUT_INVALID.value,
                "message": f"Cannot perform preparation authority operation: {error}",
                "conflict": None,
                "state_changed": False,
                **_authority_rejection_details(
                    DecisionFailure(
                        DecisionFailureCode.TRANSITION_INPUT_INVALID,
                        f"Cannot perform preparation authority operation: {error}",
                        None,
                    )
                ),
            },
            "rejected",
            None,
        )
    store = common.compose_store(durable)
    now = datetime.now(UTC)
    if isinstance(request, contracts.PreparationAuthorityStatusRequest):
        selected = authority_operations.preparation_authority_status(store, WorkItemId(request.item_id), now)
        content: dict[str, JsonValue] = {
            "schema": "pinboard-mcp-preparation-authority-result/v1",
            "item_id": request.item_id,
            **_authority_status_fields(selected),
        }
        if selected is not None:
            content.update(
                definition_revision=selected.definition_revision, definition_digest=selected.definition_digest
            )
        token.checkpoint()
        return execution.OperationResult(content, "ok", None)
    token.checkpoint()
    work_item_id = WorkItemId(request.item_id)
    match request:
        case contracts.PreparationAuthorityStartRequest():
            result = authority_operations.start_preparation_authority(
                store,
                work_item_id=work_item_id,
                task_id=TaskId(request.task_id),
                host_id=HostId(request.host_id),
                lease_id=LeaseId(uuid4().hex),
                acquired_at=now,
                expires_at=now + timedelta(seconds=request.ttl_seconds),
            )
        case contracts.PreparationAuthorityRenewRequest():
            result = authority_operations.renew_preparation_authority(
                store,
                work_item_id=work_item_id,
                lease_id=LeaseId(request.lease_id),
                generation=request.generation,
                renewed_at=now,
                expires_at=now + timedelta(seconds=request.ttl_seconds),
            )
        case contracts.PreparationAuthorityReleaseRequest():
            result = authority_operations.release_preparation_authority(
                store,
                work_item_id=work_item_id,
                lease_id=LeaseId(request.lease_id),
                generation=request.generation,
                released_at=now,
            )
        case contracts.PreparationAuthorityRevokeRequest():
            result = authority_operations.revoke_preparation_authority(
                store,
                work_item_id=work_item_id,
                lease_id=LeaseId(request.lease_id),
                generation=request.generation,
                revoked_at=now,
                actor_task_id=TaskId(request.actor_task_id),
                actor_host_id=HostId(request.actor_host_id),
            )
        case _ as unreachable:
            assert_never(unreachable)
    if isinstance(result, DecisionFailure):
        return _authority_rejected(
            "pinboard-mcp-preparation-authority-result/v1",
            {"item_id": request.item_id},
            result,
            _authority_conflict(authority_operations.preparation_authority_status(store, work_item_id, now)),
        )
    warning = _refresh_authority_views(durable, store, result.effect, now)
    retained = result.authority
    return execution.OperationResult(
        {
            "schema": "pinboard-mcp-preparation-authority-result/v1",
            "item_id": retained.work_item_id,
            "definition_revision": retained.definition_revision,
            "definition_digest": retained.definition_digest,
            **_authority_identity(retained),
            "acquired_at": retained.acquired_at.isoformat(),
            **common._committed_authority_fields(result.effect, warning),
        },
        "committed" if warning is None else "committed-warning",
        str(result.effect.receipt.project_revision),
    )


def _attempt_authority(
    raw: dict[str, JsonValue],
    token: execution.CancellationToken,
) -> execution.OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.AttemptAuthorityEnvelope, strict=True).request
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        inner = raw.get("request")
        subject = inner.get("attempt_id") if isinstance(inner, dict) else None
        return execution.OperationResult(
            {
                "schema": "pinboard-mcp-attempt-authority-result/v1",
                "status": "rejected",
                "attempt_id": subject if isinstance(subject, str) and subject else "invalid",
                "code": DecisionFailureCode.TRANSITION_INPUT_INVALID.value,
                "message": f"Cannot perform attempt authority operation: {error}",
                "conflict": None,
                "state_changed": False,
                **_authority_rejection_details(
                    DecisionFailure(
                        DecisionFailureCode.TRANSITION_INPUT_INVALID,
                        f"Cannot perform attempt authority operation: {error}",
                        None,
                    )
                ),
            },
            "rejected",
            None,
        )
    store = common.compose_store(durable)
    now = datetime.now(UTC)
    if isinstance(request, contracts.AttemptAuthorityStatusRequest):
        selected = authority_operations.attempt_authority_status(store, AttemptId(request.attempt_id), now)
        content: dict[str, JsonValue] = {
            "schema": "pinboard-mcp-attempt-authority-result/v1",
            "attempt_id": request.attempt_id,
            **_authority_status_fields(selected),
        }
        token.checkpoint()
        return execution.OperationResult(content, "ok", None)
    token.checkpoint()
    match request:
        case contracts.AttemptAuthorityAcquireRequest():
            result = authority_operations.acquire_attempt_authority(
                store,
                attempt_id=AttemptId(request.attempt_id),
                task_id=TaskId(request.task_id),
                host_id=HostId(request.host_id),
                lease_id=LeaseId(uuid4().hex),
                acquired_at=now,
                expires_at=now + timedelta(seconds=request.ttl_seconds),
            )
        case contracts.AttemptAuthorityRenewRequest():
            result = authority_operations.renew_attempt_authority(
                store,
                attempt_id=AttemptId(request.attempt_id),
                lease_id=LeaseId(request.lease_id),
                generation=request.generation,
                renewed_at=now,
                expires_at=now + timedelta(seconds=request.ttl_seconds),
            )
        case contracts.AttemptAuthorityReleaseRequest():
            result = authority_operations.release_attempt_authority(
                store,
                attempt_id=AttemptId(request.attempt_id),
                lease_id=LeaseId(request.lease_id),
                generation=request.generation,
                released_at=now,
            )
        case contracts.AttemptAuthorityRevokeRequest():
            result = authority_operations.revoke_attempt_authority(
                store,
                attempt_id=AttemptId(request.attempt_id),
                lease_id=LeaseId(request.lease_id),
                generation=request.generation,
                revoked_at=now,
                actor_task_id=TaskId(request.actor_task_id),
                actor_host_id=HostId(request.actor_host_id),
            )
        case _ as unreachable:
            assert_never(unreachable)
    if isinstance(result, DecisionFailure):
        return _authority_rejected(
            "pinboard-mcp-attempt-authority-result/v1",
            {"attempt_id": request.attempt_id},
            result,
            _authority_conflict(
                authority_operations.attempt_authority_status(store, AttemptId(request.attempt_id), now)
            ),
        )
    warning = _refresh_authority_views(durable, store, result.effect, now)
    retained = result.authority
    item_id = result.effect.receipt.transition.work_item_id
    if item_id is None:
        raise RuntimeError("Attempt authority mutation did not identify its item.")
    return execution.OperationResult(
        {
            "schema": "pinboard-mcp-attempt-authority-result/v1",
            "attempt_id": retained.attempt_id,
            "item_id": item_id,
            **_authority_identity(retained),
            "acquired_at": retained.acquired_at.isoformat(),
            **common._committed_authority_fields(result.effect, warning),
        },
        "committed" if warning is None else "committed-warning",
        str(result.effect.receipt.project_revision),
    )
