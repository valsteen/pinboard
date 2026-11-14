"""Proposal, brief, lifecycle, and authority MCP mutation composition."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, assert_never, overload
from uuid import uuid4

import msgspec

from pinboard import diagnostic_codes
from pinboard.adapters import (
    lifecycle_artifacts,
    lifecycle_operations,
    review_operations,
)
from pinboard.adapters.dependency_operations import resolve_dependency_brief_pins
from pinboard.adapters.files import file_io, views
from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.errors import ArtifactError, FileIOError
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.files.models import AffectedViews, ViewWarning
from pinboard.adapters.files.root import resolve_shared_repository_root, resolve_source_checkout_root
from pinboard.application import (
    action_models,
    authority_operations,
    ports,
    proposal_models,
    proposals,
    queries,
    query_models,
    service,
    work_brief_models,
    work_briefs,
)
from pinboard.application.artifact_publication import (
    AcceptedArtifactPublication,
    ArtifactAcceptanceFailure,
    ArtifactWriteFailure,
)
from pinboard.application.mutation_models import (
    AttemptAuthorityMutationResult,
    PreparationAuthorityMutationResult,
)
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import (
    DecisionFailure,
    DecisionFailureCode,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    FailureMismatch,
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


def _proposal_rejected_code(code: DecisionFailureCode) -> diagnostic_codes.ProposalRejectedCode:
    match code:
        case DecisionFailureCode.PROPOSAL_INVALID:
            return "PROPOSAL_INVALID"
        case DecisionFailureCode.ITEM_ALREADY_EXISTS:
            return "ITEM_ALREADY_EXISTS"
        case DecisionFailureCode.ITEM_NOT_FOUND:
            return "ITEM_NOT_FOUND"
        case DecisionFailureCode.ACTION_NOT_AVAILABLE:
            return "ACTION_NOT_AVAILABLE"
        case DecisionFailureCode.ITEM_DEFINITION_INVALID:
            return "ITEM_DEFINITION_INVALID"
        case (
            DecisionFailureCode.ACTION_NOT_MUTATING
            | DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED
            | DecisionFailureCode.ATTEMPT_LEASE_REQUIRED
            | DecisionFailureCode.ATTEMPT_LEASE_EXPIRED
            | DecisionFailureCode.ATTEMPT_NOT_FOUND
            | DecisionFailureCode.CANDIDATE_REVIEW_REQUIRED
            | DecisionFailureCode.DEPENDENCY_NOT_SATISFIED
            | DecisionFailureCode.HISTORY_RECORD_EXISTS
            | DecisionFailureCode.ITEM_DEFINITION_STALE
            | DecisionFailureCode.ROUTE_CLASSIFICATION_REQUIRED
            | DecisionFailureCode.ITEM_DEFINITION_LIFECYCLE_INVALID
            | DecisionFailureCode.ITEM_DEPENDENCY_CYCLE
            | DecisionFailureCode.ITEM_STATUS_INCONSISTENT
            | DecisionFailureCode.LIVE_DEPENDENTS
            | DecisionFailureCode.LEASE_FENCED
            | DecisionFailureCode.PROPOSAL_NOT_FOUND
            | DecisionFailureCode.PROPOSAL_ALREADY_EXISTS
            | DecisionFailureCode.REPLACEMENT_INVALID
            | DecisionFailureCode.REPLACEMENT_STALE
            | DecisionFailureCode.REVIEWER_PROMPT_NOT_COMMISSIONED
            | DecisionFailureCode.TRANSITION_INPUT_INVALID
            | DecisionFailureCode.SCHEMA_MIGRATION_INVALID
            | DecisionFailureCode.SCHEMA_MIGRATION_FAILED
        ):
            raise ValueError(f"Proposal creation cannot emit rejection code {code.value}.")
        case _ as unreachable:
            assert_never(unreachable)


def _proposal_failure(
    failure: proposal_models.ProposalFailure | DecisionFailure,
) -> execution.TypedOperationResult[contracts.ProposalResult]:
    schema = "pinboard-mcp-proposal-result/v2"
    if failure.code == DecisionFailureCode.PROPOSAL_ALREADY_EXISTS:
        result = contracts.ProposalDuplicate(
            schema,
            "rejected",
            "PROPOSAL_ALREADY_EXISTS",
            failure.message,
            False,
            "unchanged",
            "do-not-retry",
            (),
            (),
            (),
            "Read item status for the proposal_id before deciding whether any new proposal is needed.",
        )
    else:
        result = contracts.ProposalRejected(
            schema,
            "rejected",
            _proposal_rejected_code(failure.code),
            failure.message,
            False,
            "unchanged",
            "correct-input",
            (),
            (),
            (),
            None,
        )
    return execution.TypedOperationResult(result, "rejected", None)


def _brief_failure(
    code: diagnostic_codes.BriefRejectedCode,
    message: str,
) -> execution.TypedOperationResult[contracts.BriefPublicationResult]:
    return execution.TypedOperationResult(
        contracts.BriefRejected(
            "pinboard-mcp-brief-publication-result/v1",
            "rejected",
            code,
            message,
            False,
            "unchanged",
            "correct-input",
            (),
            (),
            (),
        ),
        "rejected",
        None,
    )


def _brief_decode_failure(
    project_root: str,
    work_root: str,
    brief: JsonValue,
    error: msgspec.ValidationError,
) -> execution.TypedOperationResult[contracts.BriefPublicationResult]:
    message = f"Cannot decode brief publication request: {error}"
    if not str(error).endswith(f" - at `{_ARCHITECTURE_IMPACT_KIND_PATH}`"):
        return _brief_failure("WORK_BRIEF_INVALID", message)
    checkpoint = brief.get("checkpoint") if isinstance(brief, dict) else None
    if not isinstance(checkpoint, dict) or checkpoint.get("boundary") != "cross-boundary":
        return _brief_failure("WORK_BRIEF_INVALID", message)
    architecture_impact = checkpoint.get("architecture_impact")
    if not isinstance(architecture_impact, dict):
        return _brief_failure("WORK_BRIEF_INVALID", message)
    kind = architecture_impact.get("kind")
    if not isinstance(kind, str) or kind in _ARCHITECTURE_IMPACT_KINDS:
        return _brief_failure("WORK_BRIEF_INVALID", message)
    return execution.TypedOperationResult(
        contracts.BriefArchitectureImpactRejected(
            "pinboard-mcp-brief-publication-result/v1",
            "rejected",
            "WORK_BRIEF_INVALID",
            message,
            False,
            "unchanged",
            "correct-input",
            (),
            (contracts.BriefArchitectureImpactObservation(_ARCHITECTURE_IMPACT_KIND_PATH, kind),),
            (
                contracts.BriefArchitectureImpactMismatch(
                    _ARCHITECTURE_IMPACT_KIND_PATH,
                    "none | read-only | update-required",
                    kind,
                ),
            ),
            _ARCHITECTURE_IMPACT_KINDS,
            contracts.BriefContractStarterInvocation(
                tool_names.BRIEF_CONTRACT_TOOL,
                contracts.BriefContractStarterEnvelope(
                    contracts.BriefContractStarterRequest(project_root, work_root, "cross-boundary")
                ),
            ),
        ),
        "rejected",
        None,
    )


def _proposal_created(
    raw: Mapping[str, JsonValue],
    token: execution.CancellationToken,
) -> execution.TypedOperationResult[contracts.ProposalResult]:
    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.ProposalCreateRequest, strict=True)
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
    operation = proposals.convert_proposal(decoded)
    dependencies = (
        (operation.intake.relation.dependency,)
        if isinstance(operation.intake.relation, work_models.FollowUpProposalRelation)
        else ()
    )
    dependency_pins = resolve_dependency_brief_pins(store, ArtifactRepository(durable), dependencies, now)
    if isinstance(dependency_pins, DecisionFailure):
        return _proposal_failure(
            proposal_models.ProposalFailure(dependency_pins.code, dependency_pins.message, dependency_pins.details)
        )
    committed = service.create_proposal(
        store,
        operation,
        now,
        actor_task_id=TaskId(request.actor_task_id),
        actor_host_id=HostId(request.actor_host_id),
        dependency_brief_pins=dependency_pins,
    )
    if isinstance(committed, DecisionFailure):
        return _proposal_failure(committed)
    effect = committed.effect
    view_result = common._refresh_affected_views(
        durable,
        store,
        AffectedViews(effect.work_item_ids, effect.attempt_ids, (effect.receipt.history_id,)),
        now,
    )
    warning = view_result.warning
    continuation = "Read item status or inspect the proposal before choosing a project disposition."
    if warning is None:
        result = contracts.ProposalCommitted(
            "pinboard-mcp-proposal-result/v2",
            "committed",
            decoded.proposal_id,
            committed.item.queue_position,
            "ready",
            effect.receipt.project_revision,
            int(effect.receipt.history_id),
            True,
            "committed",
            "do-not-retry",
            ("ledger",),
            continuation,
            None,
        )
    else:
        result = contracts.ProposalCommittedWithWarning(
            "pinboard-mcp-proposal-result/v2",
            "committed-with-warning",
            decoded.proposal_id,
            committed.item.queue_position,
            "ready",
            effect.receipt.project_revision,
            int(effect.receipt.history_id),
            True,
            "committed",
            "do-not-retry",
            ("ledger",),
            continuation,
            contracts.WarningResult(warning.message, warning.repair),
        )
    return execution.TypedOperationResult(
        result, "committed" if warning is None else "committed-warning", str(effect.receipt.project_revision)
    )


def _brief_published(
    raw: Mapping[str, JsonValue],
    token: execution.CancellationToken,
) -> execution.TypedOperationResult[contracts.BriefPublicationResult]:
    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.BriefPublishRequest, strict=True)
        durable = common._resolve_durable(request.project_root, request.work_root)
    except msgspec.ValidationError as error:
        return _brief_decode_failure(
            str(raw.get("project_root", "")), str(raw.get("work_root", "")), raw.get("brief"), error
        )
    except (ValueError, OSError) as error:
        return _brief_failure("WORK_BRIEF_INVALID", f"Cannot decode brief publication request: {error}")
    return _publish_brief(durable, request.brief, token)


type SelectedBrief = work_brief_models.WorkBrief | execution.TypedOperationResult[contracts.BriefPublicationResult]


def _read_brief_file(raw: Mapping[str, JsonValue]) -> SelectedBrief:
    try:
        request = msgspec.convert(raw, type=contracts.BriefPublishFileRequest, strict=True)
    except msgspec.ValidationError as error:
        return _brief_failure("WORK_BRIEF_INVALID", str(error))
    content = file_io.read_verified_input(Path(request.brief_path), request.brief_sha256)
    if isinstance(content, file_io.SelectedInputFailure):
        return _brief_failure(content.code, content.message)
    try:
        return msgspec.json.decode(content, type=work_brief_models.WorkBrief)
    except msgspec.DecodeError as error:
        return _brief_failure(
            "WORK_BRIEF_INVALID",
            f"Cannot decode selected brief '{request.brief_path}' with verified SHA-256 {request.brief_sha256}: {error}. Correct that file to the current work-brief schema before retrying.",
        )


def _brief_published_file(
    raw: Mapping[str, JsonValue],
    token: execution.CancellationToken,
    read_brief: Callable[[], SelectedBrief],
) -> execution.TypedOperationResult[contracts.BriefPublicationResult]:
    token.checkpoint()
    brief = read_brief()
    if not isinstance(brief, work_brief_models.WorkBrief):
        return brief
    try:
        durable = common._resolve_durable(str(raw["project_root"]), str(raw["work_root"]))
    except (ValueError, OSError) as error:
        return _brief_failure(
            "WORK_BRIEF_INVALID",
            f"Cannot resolve brief publication roots: {error}. Correct the selected project_root and work_root before retrying.",
        )
    return _publish_brief(durable, brief, token)


def _brief_publication_rejected(
    failure: DecisionFailure,
) -> execution.TypedOperationResult[contracts.BriefPublicationResult]:
    if failure.code != DecisionFailureCode.ACTION_NOT_AVAILABLE:
        raise ValueError(failure.message)
    details = failure.details
    if details is not None and details.effect == EffectDisposition.COMMITTED:
        return execution.TypedOperationResult(
            contracts.BriefPublishedRejection(
                "pinboard-mcp-brief-publication-result/v1",
                "rejected",
                "ACTION_NOT_AVAILABLE",
                failure.message,
                True,
                "committed",
                "do-not-retry",
                ("immutable-artifact",),
                _observations(details),
                _mismatches(details),
            ),
            "rejected",
            None,
        )
    return _brief_failure("ACTION_NOT_AVAILABLE", failure.message)


def _brief_publication_failed(
    failure: ArtifactAcceptanceFailure | ArtifactWriteFailure,
) -> execution.TypedOperationResult[contracts.BriefPublicationResult]:
    acceptance_failed = isinstance(failure, ArtifactAcceptanceFailure)
    code = "ARTIFACT_ACCEPTANCE_FAILED" if acceptance_failed else "ARTIFACT_PUBLICATION_FAILED"
    message = (
        f"The brief's accepted reference could not be committed. {failure.cause}"
        if acceptance_failed
        else f"The brief's immutable publication could not be completed. {failure.cause}"
    )
    recovery = (
        "Preserve the published selector and repair artifact-reference acceptance before continuing."
        if acceptance_failed
        else "Inspect the published selector and repair immutable publication before continuing."
    )
    schema = "pinboard-mcp-brief-publication-result/v1"
    if failure.details.effect == EffectDisposition.COMMITTED:
        result = contracts.BriefPublicationInfrastructureFailure(
            schema,
            "failed-after-publication",
            code,
            message,
            True,
            "committed",
            "do-not-retry",
            ("immutable-artifact",),
            (),
            (),
            failure.selector,
            recovery,
        )
    else:
        result = contracts.BriefPublicationInfrastructureUnchangedFailure(
            schema,
            "infrastructure-failure",
            code,
            message,
            False,
            "unchanged",
            "retry-same-input" if failure.details.retry == RetryDisposition.RETRY_SAME_INPUT else "do-not-retry",
            (),
            (),
            (),
            failure.selector,
            recovery,
        )
    return execution.TypedOperationResult(result, "infrastructure-failure", failure.selector)


def _publish_brief(
    durable: DurableRoots, decoded: work_brief_models.WorkBrief, token: execution.CancellationToken
) -> execution.TypedOperationResult[contracts.BriefPublicationResult]:
    store = common.compose_store(durable)
    token.checkpoint()
    now = datetime.now(UTC)
    publication = work_briefs.publish_work_brief(store, ArtifactRepository(durable), decoded, now)
    if isinstance(publication, work_brief_models.WorkBriefFailure):
        return _brief_failure("WORK_BRIEF_INVALID", publication.message)
    if isinstance(publication, DecisionFailure):
        return _brief_publication_rejected(publication)
    if isinstance(publication, (ArtifactAcceptanceFailure, ArtifactWriteFailure)):
        return _brief_publication_failed(publication)
    view_result = common._refresh_affected_views(durable, store, AffectedViews((), (), ()), now)
    warning = view_result.warning
    return _brief_publication_success(publication, warning)


def _brief_publication_success(
    publication: AcceptedArtifactPublication, warning: ViewWarning | None
) -> execution.TypedOperationResult[contracts.BriefPublicationResult]:
    schema = "pinboard-mcp-brief-publication-result/v1"
    accepted = publication.reference
    reference = contracts.ArtifactReferenceResult(
        int(accepted.artifact_ref_id),
        "brief",
        accepted.key,
        accepted.revision,
        accepted.selector,
        accepted.content_sha256,
        accepted.size_bytes,
        accepted.accepted_revision,
    )
    continuation = "Use the verified reference when preparing or rebinding the matching attempt."
    if warning is None:
        match publication.artifact_created, publication.ledger_changed:
            case True, True:
                result = contracts.BriefCommitted(
                    schema,
                    "committed",
                    reference,
                    True,
                    "committed",
                    "do-not-retry",
                    ("immutable-artifact", "accepted-artifact-reference", "ledger"),
                    continuation,
                    None,
                )
            case False, True:
                result = contracts.BriefReferenceCommitted(
                    schema,
                    "committed",
                    reference,
                    True,
                    "committed",
                    "do-not-retry",
                    ("accepted-artifact-reference", "ledger"),
                    continuation,
                    None,
                )
            case True, False:
                result = contracts.BriefArtifactCommitted(
                    schema,
                    "committed",
                    reference,
                    True,
                    "committed",
                    "do-not-retry",
                    ("immutable-artifact",),
                    continuation,
                    None,
                )
            case False, False:
                result = contracts.BriefUnchanged(
                    schema,
                    "unchanged",
                    reference,
                    False,
                    "unchanged",
                    "retry-same-input",
                    (),
                    continuation,
                    None,
                )
            case _ as unreachable:
                assert_never(unreachable)
        classification = "committed" if result.state_changed else "unchanged"
    else:
        rendered_warning = contracts.WarningResult(warning.message, warning.repair)
        match publication.artifact_created, publication.ledger_changed:
            case True, True:
                result = contracts.BriefCommittedWithWarning(
                    schema,
                    "committed-with-warning",
                    reference,
                    True,
                    "committed",
                    "do-not-retry",
                    ("immutable-artifact", "accepted-artifact-reference", "ledger"),
                    continuation,
                    rendered_warning,
                )
            case False, True:
                result = contracts.BriefReferenceCommittedWithWarning(
                    schema,
                    "committed-with-warning",
                    reference,
                    True,
                    "committed",
                    "do-not-retry",
                    ("accepted-artifact-reference", "ledger"),
                    continuation,
                    rendered_warning,
                )
            case True, False:
                result = contracts.BriefArtifactCommittedWithWarning(
                    schema,
                    "committed-with-warning",
                    reference,
                    True,
                    "committed",
                    "do-not-retry",
                    ("immutable-artifact",),
                    continuation,
                    rendered_warning,
                )
            case False, False:
                result = contracts.BriefUnchangedWithWarning(
                    schema,
                    "unchanged-with-warning",
                    reference,
                    False,
                    "unchanged",
                    "retry-same-input",
                    (),
                    continuation,
                    rendered_warning,
                )
            case _ as unreachable:
                assert_never(unreachable)
        classification = "committed-warning" if result.state_changed else "unchanged-warning"
    return execution.TypedOperationResult(result, classification, str(accepted.artifact_ref_id))


def _observations(details: FailureDetails | None) -> tuple[contracts.FailureObservation, ...]:
    return (
        ()
        if details is None
        else tuple(contracts.FailureObservation(fact.field, fact.value) for fact in details.observed)
    )


def _mismatches(details: FailureDetails | None) -> tuple[contracts.FailureMismatch, ...]:
    return (
        ()
        if details is None
        else tuple(
            contracts.FailureMismatch(mismatch.field, mismatch.expected, mismatch.observed)
            for mismatch in details.mismatches
        )
    )


_ATTEMPT_LEASE_CODES = frozenset(
    {
        DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
        DecisionFailureCode.ATTEMPT_LEASE_REQUIRED,
        DecisionFailureCode.ATTEMPT_LEASE_EXPIRED,
    }
)


def _transition_rejected(
    action_id: contracts.ActionIdentity,
    failure: DecisionFailure,
    *,
    continuation: str | None,
) -> execution.TypedOperationResult[contracts.TransitionResult]:
    retry = _rejection_retry(failure)
    return execution.TypedOperationResult(
        contracts.TransitionRejected(
            "pinboard-mcp-transition-result/v1",
            "rejected",
            action_id,
            failure.code,
            failure.message,
            False,
            "unchanged",
            retry,
            (),
            _observations(failure.details),
            _mismatches(failure.details),
            _transition_rejection_continuation(action_id.kind, failure.code, retry)
            if continuation is None
            else continuation,
        ),
        "rejected",
        None,
    )


def _transition_rejection_continuation(
    kind: decision_models.ActionKind, code: DecisionFailureCode, retry: RetryDisposition
) -> str:
    match retry:
        case RetryDisposition.CORRECT_INPUT:
            return (
                "Use the current pinboard-reviewed-completion/v3 input with the human's explicit whole-item "
                "completion decision in their own words as human_decision. Reuse their prior exact authorization "
                "only after its stated boundary is satisfied. If it is missing, ask the human and stop; never "
                "invent it. Then discover this exact current action through pinboard_actions."
                if kind == decision_models.ActionKind.COMPLETE
                else "Correct the reported input, then discover this exact current action through pinboard_actions."
            )
        case RetryDisposition.REFRESH_ACTION | RetryDisposition.RETRY_SAME_INPUT:
            return "Discover this exact current action through pinboard_actions before another transition."
        case RetryDisposition.REACQUIRE_AUTHORITY:
            if code in _ATTEMPT_LEASE_CODES:
                return common.ATTEMPT_LEASE_RECOVERY
            return (
                "Check the relevant authority status, reacquire only if permitted, "
                "then discover this action through pinboard_actions."
            )
        case RetryDisposition.DO_NOT_RETRY:
            return (
                _COMMISSIONED_REVIEW_ROUTE
                if code == DecisionFailureCode.CANDIDATE_REVIEW_REQUIRED
                else _transition_current_state_route(kind, item_status_returned=False)
            )
        case _ as unreachable:
            assert_never(unreachable)


_COMMISSIONED_REVIEW_ROUTE = (
    "Do not replay this completion. Inspect this exact attempt through pinboard_attempt_inspect first. "
    "Reuse a valid current commissioned ready record and its recorded reviewer_task_id; a mismatched supplied "
    "reviewer identity does not require another review. Only when that record is missing or invalid, run "
    "pinboard_review_job for the reported round, launch exactly its native reviewer and record its ready verdict "
    "with kind:'record-ready' and prompt_reference.sha256 as reviewer_prompt_sha256. Then rediscover completion "
    "through pinboard_actions with the verified record's reviewer_task_id."
)
_CLOSE_DECISION_ROUTE = (
    "Close only through pinboard_close with the human's explicit close decision in their own words as "
    "payload.human_decision. If you do not have that decision, ask the human and stop; never write it yourself. If "
    "the host denies pinboard_close, report that the human must confirm the close and seek no other route."
)


def _close_route_rejected(
    identity: contracts.ActionIdentity, message: str, observed: FailureFact
) -> execution.TypedOperationResult[contracts.TransitionResult]:
    """Refuse a close attempt unchanged and send the agent back to the human-confirmed close route."""

    return _transition_rejected(
        identity,
        DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            message,
            FailureDetails(
                observed=(observed,),
                mismatches=(),
                retry=RetryDisposition.CORRECT_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        ),
        continuation=_CLOSE_DECISION_ROUTE,
    )


def _transition_current_state_route(kind: decision_models.ActionKind, *, item_status_returned: bool) -> str:
    match decision_models.action_semantics(kind).subject_kind:
        case decision_models.ActionSubjectKind.ATTEMPT:
            return "Inspect this attempt with pinboard_attempt_inspect and follow its next_operation; do not replay."
        case decision_models.ActionSubjectKind.ITEM:
            if item_status_returned:
                return "Use the returned item_status for current item facts, then discover its current action through pinboard_actions; do not replay."
            return "Read this item with pinboard_item_status operation item, then discover its current action; do not replay."
        case decision_models.ActionSubjectKind.PROPOSAL:
            if item_status_returned:
                return "Use the returned item_status for the same-identity proposal item, then discover its current action through pinboard_actions; do not replay."
            return "Read the exact proposal's same-identity item through pinboard_item_status operation item before another action; do not replay."
        case decision_models.ActionSubjectKind.LEDGER:
            return "Read pinboard_overview with the same roots, max_items:25 and continuation:null, then follow continuation for complete coverage before another action; do not replay."
        case _ as unreachable:
            assert_never(unreachable)


def _with_completion_reinspection(
    failure: DecisionFailure,
    identity: contracts.ActionIdentity,
    project_root: str,
    work_root: str,
) -> DecisionFailure:
    """Attach the read-only focused route after any rejected completion receipt.

    A missing commissioned review keeps its own retry and names one evidence inspection.
    """
    if identity.kind != decision_models.ActionKind.COMPLETE:
        return failure
    details = failure.details
    roots: dict[str, JsonValue] = {"project_root": project_root, "work_root": work_root}
    request: dict[str, JsonValue] = {
        "request": {
            **roots,
            "role": "project",
            "action_id": {"kind": "complete", "subject": identity.subject},
        }
    }
    retry = RetryDisposition.REFRESH_ACTION
    if failure.code == DecisionFailureCode.CANDIDATE_REVIEW_REQUIRED and details is not None:
        retry = details.retry
        inspection: dict[str, JsonValue] = {**roots, "attempt_id": identity.subject, "reconciliation": None}
        continuation = (
            FailureFact("completion_inspection_tool", tool_names.ATTEMPT_INSPECT_TOOL),
            FailureFact("completion_inspection_input", msgspec.json.encode(inspection, order="sorted").decode()),
        )
    else:
        continuation = (
            FailureFact("completion_reinspection_tool", tool_names.ACTIONS_TOOL),
            FailureFact("completion_reinspection_input", msgspec.json.encode(request, order="sorted").decode()),
        )
    return DecisionFailure(
        failure.code,
        failure.message,
        FailureDetails(
            observed=(
                *(() if details is None else details.observed),
                *continuation,
            ),
            mismatches=() if details is None else details.mismatches,
            retry=retry,
            effect=EffectDisposition.UNCHANGED if details is None else details.effect,
            changed_surfaces=() if details is None else details.changed_surfaces,
            alternatives=() if details is None else details.alternatives,
        ),
    )


_REQUIRED_TRANSITION_RECEIPT_SHAPE = (
    '{"action_id":{"kind":"<action>","subject":"<id>"},"subject_revision":"<revision>"}'
)


def _transition_decode_details(inner: JsonValue, *, identity_decoded: bool) -> FailureDetails | None:
    if not isinstance(inner, dict):
        return None
    receipt = inner.get("receipt")
    if isinstance(receipt, dict) and identity_decoded:
        return None
    if not isinstance(receipt, dict):
        if "action" in inner:
            misplaced_key = "request.action"
        elif "action_id" in inner or "subject_revision" in inner:
            misplaced_key = ", ".join(f"request.{key}" for key in ("action_id", "subject_revision") if key in inner)
        else:
            misplaced_key = "request.receipt"
    else:
        misplaced_key = "request.receipt.action_id"
    return FailureDetails(
        observed=(
            FailureFact("decoded_action_id", None),
            FailureFact("misplaced_key", misplaced_key),
            FailureFact("required_receipt_field", "request.receipt"),
            FailureFact("required_receipt_keys", "action_id, subject_revision"),
            FailureFact("required_receipt_shape", _REQUIRED_TRANSITION_RECEIPT_SHAPE),
        ),
        mismatches=(FailureMismatch("request.receipt", _REQUIRED_TRANSITION_RECEIPT_SHAPE, misplaced_key),),
        retry=RetryDisposition.CORRECT_INPUT,
        effect=EffectDisposition.UNCHANGED,
        changed_surfaces=(),
        alternatives=(),
    )


def _transition(
    raw: Mapping[str, JsonValue],
    token: execution.CancellationToken,
) -> execution.TypedOperationResult[contracts.TransitionResult]:
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
            identity_decoded = True
        except msgspec.ValidationError:
            # An undecodable identity names no action, so it must never select the close route.
            identity = contracts.ActionIdentity(decision_models.ActionKind.INSPECT, "invalid")
            identity_decoded = False
        if identity.kind == decision_models.ActionKind.CLOSE:
            return _close_route_rejected(
                identity,
                "pinboard_transition does not apply close; close is applied only through pinboard_close, which "
                "records the human's decision. Claude Code v2.1.199 or later asks the human to confirm that call; "
                "Codex and earlier Claude Code versions may not ask. Antigravity asks through Pinboard's PreToolUse "
                "force_ask decision, so close is unavailable in headless agy runs; when that hook does not run, "
                "Antigravity's own approval settings decide.",
                FailureFact("close_tool", tool_names.CLOSE_TOOL),
            )
        return _transition_rejected(
            identity,
            DecisionFailure(
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                f"Cannot decode transition request: {error}",
                _transition_decode_details(inner, identity_decoded=identity_decoded),
            ),
            continuation=None,
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
    return _apply_transition(
        source_checkout,
        durable,
        request.project_root,
        request.work_root,
        identity,
        lifecycle_operations.TransitionReceipt(
            selected_role,
            ActionId(f"{identity.kind.value}:{identity.subject}"),
            request.receipt.subject_revision,
            selected_lease,
            selected_generation,
            selected_task,
            selected_host,
        ),
        request.payload,
        token,
    )


def _close(
    raw: Mapping[str, JsonValue],
    token: execution.CancellationToken,
) -> execution.TypedOperationResult[contracts.TransitionResult]:
    """Apply one human-confirmed close; a request without the human's own decision never reaches the ledger."""

    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.CloseRequest, strict=True)
        source_checkout = resolve_source_checkout_root(Path(request.project_root))
        durable = common._require_initialized_durable(
            resolve_shared_repository_root(source_checkout), Path(request.work_root)
        )
    except (msgspec.ValidationError, ValueError, OSError) as error:
        receipt = raw.get("receipt")
        raw_identity = receipt.get("action_id") if isinstance(receipt, dict) else None
        subject = raw_identity.get("subject") if isinstance(raw_identity, dict) else None
        identity = contracts.ActionIdentity(decision_models.ActionKind.CLOSE, _fallback_subject(subject))
        payload = raw.get("payload")
        decision = payload.get("human_decision") if isinstance(payload, dict) else None
        return _close_route_rejected(
            identity,
            f"Cannot decode close request: {error}",
            FailureFact("human_decision_valid", isinstance(decision, str) and bool(decision) and "\n" not in decision),
        )
    identity = contracts.ActionIdentity(decision_models.ActionKind.CLOSE, request.receipt.action_id.subject)
    return _apply_transition(
        source_checkout,
        durable,
        request.project_root,
        request.work_root,
        identity,
        lifecycle_operations.TransitionReceipt(
            decision_models.Role.PROJECT,
            ActionId(f"{identity.kind.value}:{identity.subject}"),
            request.receipt.subject_revision,
            None,
            0,
            TaskId(request.actor_task_id),
            HostId(request.actor_host_id),
        ),
        request.payload,
        token,
    )


def _apply_transition(
    source_checkout: Path,
    durable: DurableRoots,
    project_root: str,
    work_root: str,
    identity: contracts.ActionIdentity,
    receipt: lifecycle_operations.TransitionReceipt,
    payload: action_models.InputPayload,
    token: execution.CancellationToken,
) -> execution.TypedOperationResult[contracts.TransitionResult]:
    store = common.compose_store(durable)
    artifacts = ArtifactRepository(durable)
    operation_time = datetime.now(UTC)
    selected = lifecycle_operations.select_transition(
        source_checkout, store, artifacts, receipt, payload, operation_time
    )
    if isinstance(selected, DecisionFailure):
        return _transition_rejected(
            identity,
            _with_completion_reinspection(selected, identity, project_root, work_root),
            continuation=None,
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
        return _transition_failed_after_publication(identity, committed.code, committed.message, committed.details)
    if isinstance(committed, lifecycle_artifacts.LandingRejection):
        return _transition_rejected(identity, committed.failure, continuation=committed.continuation)
    if isinstance(committed, DecisionFailure):
        if committed.details is not None and committed.details.effect == EffectDisposition.COMMITTED:
            return _transition_failed_after_publication(
                identity,
                committed.code.value,
                committed.message,
                committed.details,
            )
        return _transition_rejected(
            identity,
            _with_completion_reinspection(committed, identity, project_root, work_root),
            continuation=None,
        )
    changed_surfaces: contracts.PublicationSurfaces | contracts.ReferenceSurfaces | contracts.LedgerSurface
    cleanup: contracts.LocalCleanupPlanView | msgspec.UnsetType = msgspec.UNSET
    cleanup_continuation = ""
    if isinstance(committed, lifecycle_artifacts.ArtifactTransitionSuccess):
        committed_effect = committed.effect
        if committed.local_cleanup is not None:
            cleanup = contracts.LocalCleanupPlanView(
                committed.local_cleanup.branch,
                "retain" if committed.local_cleanup.retain_local_resources else "remove",
            )
            cleanup_continuation = "Apply local_cleanup in this close turn using the native Git procedure. "
        changed_surfaces = (
            ("immutable-artifact", "accepted-artifact-reference", "ledger")
            if committed.published_selectors
            else ("accepted-artifact-reference", "ledger")
        )
    else:
        committed_effect = committed
        changed_surfaces = ("ledger",)
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
    item_id = committed_effect.receipt.transition.work_item_id
    if isinstance(selected.command, (decision_models.MergeProposalCommand, decision_models.RejectProposalCommand)):
        item_id = WorkItemId(selected.command.action.capability.subject)
    assert item_id is not None
    item_status = _committed_item_status(durable, store, item_id, datetime.now(UTC))
    continuation = _transition_current_state_route(identity.kind, item_status_returned=True)
    schema = "pinboard-mcp-transition-result/v1"
    revision = committed_effect.receipt.project_revision
    history_id = int(committed_effect.receipt.history_id)
    if isinstance(item_status, contracts.TransitionItemStatusUnavailable):
        warning_result = contracts.WarningResult(
            item_status.message if warning is None else f"{warning.message} {item_status.message}",
            item_status.recovery if warning is None else f"{warning.repair} {item_status.recovery}",
        )
        result = contracts.TransitionCommittedStatusUnavailable(
            schema=schema,
            status="committed-with-warning",
            action_id=identity,
            committed_revision=revision,
            history_id=history_id,
            state_changed=True,
            effect="committed",
            retry="do-not-retry",
            changed_surfaces=changed_surfaces,
            continuation=cleanup_continuation + item_status.recovery,
            warning=warning_result,
            item_status=item_status,
            local_cleanup=cleanup,
        )
        return execution.TypedOperationResult(result, "committed-warning", str(revision))
    return execution.TypedOperationResult(
        contracts.TransitionCommitted(
            schema=schema,
            status="committed" if warning is None else "committed-with-warning",
            action_id=identity,
            committed_revision=revision,
            history_id=history_id,
            state_changed=True,
            effect="committed",
            retry="do-not-retry",
            changed_surfaces=changed_surfaces,
            continuation=cleanup_continuation + continuation,
            warning=None if warning is None else contracts.WarningResult(warning.message, warning.repair),
            item_status=item_status,
            local_cleanup=cleanup,
        ),
        "committed" if warning is None else "committed-warning",
        str(revision),
    )


def _transition_failed_after_publication(
    identity: contracts.ActionIdentity,
    code: str,
    message: str,
    details: FailureDetails,
) -> execution.TypedOperationResult[contracts.TransitionResult]:
    return execution.TypedOperationResult(
        contracts.TransitionFailedAfterPublication(
            "pinboard-mcp-transition-result/v1",
            "failed-after-publication",
            identity,
            code,
            message,
            True,
            "committed",
            "do-not-retry",
            ("immutable-artifact",),
            _observations(details),
            _mismatches(details),
            _transition_current_state_route(identity.kind, item_status_returned=False),
        ),
        "failed-after-publication",
        None,
    )


def _committed_item_status(
    durable: DurableRoots, store: ports.WorkStore, item_id: WorkItemId, now: datetime
) -> query_models.ItemStatus | contracts.TransitionItemStatusUnavailable:
    """Read only the committed subject; a failed read cannot undo or invite replay of its commit."""
    try:
        status = queries.project_item_status(
            store,
            review_operations.AttemptStatusArtifactReader(durable, store),
            item_id,
            now,
            views.board_pages(durable.work_root),
        )
    except (ports.WorkStoreError, ArtifactError, FileIOError, OSError) as error:
        code, message = "ITEM_STATUS_UNAVAILABLE", str(error)
    else:
        if isinstance(status, DecisionFailure):
            code, message = status.code.value, status.message
        elif isinstance(status, query_models.DamagedTransitionReceipt):
            code, message = "TRANSITION_RECEIPT_DAMAGED", queries.damaged_receipt_message(status)
        else:
            return status
    return contracts.TransitionItemStatusUnavailable(
        str(item_id),
        str(durable.database_path),
        code,
        f"The transition committed, but current status for item '{item_id}' is unavailable: {message}",
        f"Do not replay the committed transition. Diagnose the named read failure, then read pinboard_item_status with operation:'item', item_id:'{item_id}', project_root and work_root from this call before another consequential action.",
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


def _fallback_subject(subject: JsonValue) -> str:
    if not isinstance(subject, str) or not subject:
        return "invalid"
    msgspec.convert(subject, type=contracts.PathComponent, strict=True)
    return subject


def _authority_rejection_message(failure: DecisionFailure) -> str:
    return (
        failure.message
        + " Follow next_step_tool with next_step_input in observed to inspect the current subject. This rejection grants no authority; do not borrow a conflicting owner's lease or revoke it without authorization."
        + (" " + common.ATTEMPT_LEASE_RECOVERY if failure.code in _ATTEMPT_LEASE_CODES else "")
    )


def _authority_rejection_observations(
    details: FailureDetails | None,
    inspection_tool: str,
    inspection_input: dict[str, JsonValue],
) -> tuple[contracts.FailureObservation, ...]:
    return (
        *_observations(details),
        contracts.FailureObservation("next_step_tool", inspection_tool),
        contracts.FailureObservation("next_step_input", msgspec.json.encode(inspection_input, order="sorted").decode()),
    )


@overload
def _committed_authority(
    durable: DurableRoots,
    store: ports.WorkStore,
    committed: PreparationAuthorityMutationResult,
    now: datetime,
) -> execution.TypedOperationResult[contracts.PreparationAuthorityResult]: ...


@overload
def _committed_authority(
    durable: DurableRoots,
    store: ports.WorkStore,
    committed: AttemptAuthorityMutationResult,
    now: datetime,
) -> execution.TypedOperationResult[contracts.AttemptAuthorityResult]: ...


def _committed_authority(
    durable: DurableRoots,
    store: ports.WorkStore,
    committed: PreparationAuthorityMutationResult | AttemptAuthorityMutationResult,
    now: datetime,
) -> (
    execution.TypedOperationResult[contracts.PreparationAuthorityResult]
    | execution.TypedOperationResult[contracts.AttemptAuthorityResult]
):
    effect = committed.effect
    warning = common._refresh_affected_views(
        durable,
        store,
        AffectedViews(effect.work_item_ids, effect.attempt_ids, (effect.receipt.history_id,)),
        now,
    ).warning
    revision = effect.receipt.project_revision
    status: Literal["committed", "committed-with-warning"] = (
        "committed" if warning is None else "committed-with-warning"
    )
    receipt: tuple[
        Literal["committed", "committed-with-warning"],
        int,
        int,
        Literal[True],
        Literal["committed"],
        Literal["do-not-retry"],
        contracts.LedgerSurface,
        contracts.WarningResult | None,
    ] = (
        status,
        revision,
        int(effect.receipt.history_id),
        True,
        "committed",
        "do-not-retry",
        ("ledger",),
        None if warning is None else contracts.WarningResult(warning.message, warning.repair),
    )
    classification = "committed" if warning is None else "committed-warning"
    match committed:
        case PreparationAuthorityMutationResult(authority=retained):
            result = contracts.PreparationAuthorityCommitted(
                "pinboard-mcp-preparation-authority-result/v1",
                retained.work_item_id,
                retained.definition_revision,
                retained.definition_digest,
                retained.task_id,
                retained.host_id,
                retained.lease_id,
                retained.generation,
                retained.expires_at.isoformat(),
                retained.status,
                retained.acquired_at.isoformat(),
                *receipt,
            )
            return execution.TypedOperationResult[contracts.PreparationAuthorityResult](
                result, classification, str(revision)
            )
        case AttemptAuthorityMutationResult(authority=retained):
            item_id = effect.receipt.transition.work_item_id
            if item_id is None:
                raise RuntimeError("Attempt authority mutation did not identify its item.")
            result = contracts.AttemptAuthorityCommitted(
                "pinboard-mcp-attempt-authority-result/v1",
                retained.attempt_id,
                item_id,
                retained.task_id,
                retained.host_id,
                retained.lease_id,
                retained.generation,
                retained.expires_at.isoformat(),
                retained.status,
                retained.acquired_at.isoformat(),
                *receipt,
            )
            return execution.TypedOperationResult[contracts.AttemptAuthorityResult](
                result, classification, str(revision)
            )
        case _ as unreachable:
            assert_never(unreachable)


def _preparation_authority(
    raw: Mapping[str, JsonValue],
    token: execution.CancellationToken,
) -> execution.TypedOperationResult[contracts.PreparationAuthorityResult]:
    token.checkpoint()
    schema = "pinboard-mcp-preparation-authority-result/v1"
    try:
        request = msgspec.convert(raw, type=contracts.PreparationAuthorityEnvelope, strict=True).request
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        inner = raw.get("request")
        subject = inner.get("item_id") if isinstance(inner, dict) else None
        return execution.TypedOperationResult(
            contracts.PreparationAuthorityRejected(
                schema,
                "rejected",
                _fallback_subject(subject),
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                f"Cannot perform preparation authority operation: {error}",
                None,
                False,
                "unchanged",
                RetryDisposition.CORRECT_INPUT,
                (),
                (),
                (),
            ),
            "rejected",
            None,
        )
    store = common.compose_store(durable)
    now = datetime.now(UTC)
    if isinstance(request, contracts.PreparationAuthorityStatusRequest):
        selected = authority_operations.preparation_authority_status(store, WorkItemId(request.item_id), now)
        token.checkpoint()
        if selected is None:
            return execution.TypedOperationResult(
                contracts.PreparationAuthorityStatusAbsent(
                    schema, request.item_id, "absent", False, "unchanged", "safe-to-repeat", ()
                ),
                "ok",
                None,
            )
        return execution.TypedOperationResult(
            contracts.PreparationAuthorityStatusPresent(
                schema,
                request.item_id,
                "present",
                selected.task_id,
                selected.host_id,
                selected.lease_id,
                selected.generation,
                selected.expires_at.isoformat(),
                selected.status,
                selected.acquired_at.isoformat(),
                False,
                "unchanged",
                "safe-to-repeat",
                (),
                selected.definition_revision,
                selected.definition_digest,
            ),
            "ok",
            None,
        )
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
        retained = authority_operations.preparation_authority_status(store, work_item_id, now)
        conflict = (
            None
            if retained is None
            else contracts.PreparationAuthorityConflict(
                retained.task_id,
                retained.host_id,
                retained.lease_id,
                retained.generation,
                retained.expires_at.isoformat(),
                retained.status,
            )
        )
        return execution.TypedOperationResult(
            contracts.PreparationAuthorityRejected(
                schema,
                "rejected",
                request.item_id,
                result.code,
                _authority_rejection_message(result),
                conflict,
                False,
                "unchanged",
                _rejection_retry(result),
                (),
                _authority_rejection_observations(
                    result.details,
                    tool_names.ITEM_STATUS_TOOL,
                    {
                        "request": {
                            "project_root": request.project_root,
                            "work_root": request.work_root,
                            "operation": "item",
                            "item_id": request.item_id,
                        }
                    },
                ),
                _mismatches(result.details),
            ),
            "rejected",
            None,
        )
    return _committed_authority(durable, store, result, now)


def _attempt_authority(
    raw: Mapping[str, JsonValue],
    token: execution.CancellationToken,
) -> execution.TypedOperationResult[contracts.AttemptAuthorityResult]:
    token.checkpoint()
    schema = "pinboard-mcp-attempt-authority-result/v1"
    try:
        request = msgspec.convert(raw, type=contracts.AttemptAuthorityEnvelope, strict=True).request
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        inner = raw.get("request")
        subject = inner.get("attempt_id") if isinstance(inner, dict) else None
        return execution.TypedOperationResult(
            contracts.AttemptAuthorityRejected(
                schema,
                "rejected",
                _fallback_subject(subject),
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                f"Cannot perform attempt authority operation: {error}",
                None,
                False,
                "unchanged",
                RetryDisposition.CORRECT_INPUT,
                (),
                (),
                (),
            ),
            "rejected",
            None,
        )
    store = common.compose_store(durable)
    now = datetime.now(UTC)
    if isinstance(request, contracts.AttemptAuthorityStatusRequest):
        selected = authority_operations.attempt_authority_status(store, AttemptId(request.attempt_id), now)
        token.checkpoint()
        if selected is None:
            return execution.TypedOperationResult(
                contracts.AttemptAuthorityStatusAbsent(
                    schema, request.attempt_id, "absent", False, "unchanged", "safe-to-repeat", ()
                ),
                "ok",
                None,
            )
        return execution.TypedOperationResult(
            contracts.AttemptAuthorityStatusPresent(
                schema,
                request.attempt_id,
                "present",
                selected.task_id,
                selected.host_id,
                selected.lease_id,
                selected.generation,
                selected.expires_at.isoformat(),
                selected.status,
                selected.acquired_at.isoformat(),
                False,
                "unchanged",
                "safe-to-repeat",
                (),
            ),
            "ok",
            None,
        )
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
        retained = authority_operations.attempt_authority_status(store, AttemptId(request.attempt_id), now)
        conflict = (
            None
            if retained is None
            else contracts.AttemptAuthorityConflict(
                retained.task_id,
                retained.host_id,
                retained.lease_id,
                retained.generation,
                retained.expires_at.isoformat(),
                retained.status,
            )
        )
        return execution.TypedOperationResult(
            contracts.AttemptAuthorityRejected(
                schema,
                "rejected",
                request.attempt_id,
                result.code,
                _authority_rejection_message(result),
                conflict,
                False,
                "unchanged",
                _rejection_retry(result),
                (),
                _authority_rejection_observations(
                    result.details,
                    tool_names.ATTEMPT_INSPECT_TOOL,
                    {
                        "project_root": request.project_root,
                        "work_root": request.work_root,
                        "attempt_id": request.attempt_id,
                        "reconciliation": None,
                    },
                ),
                _mismatches(result.details),
            ),
            "rejected",
            None,
        )
    return _committed_authority(durable, store, result, now)
