"""Agent coordination, preparation, and read-only MCP use-case composition."""

from __future__ import annotations

import hashlib
import shlex
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard.adapters import candidate_evidence, dispatch_operations, review_operations
from pinboard.adapters.files import file_io, root, views
from pinboard.adapters.files import fixture_review as fixture_review_files
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
    fixture_review,
    obligation_coverage,
    ports,
    queries,
    query_models,
    service,
    stored_state,
    work_brief_contract,
    work_brief_models,
    work_briefs,
)
from pinboard.application.artifact_publication import ArtifactAcceptanceFailure, ArtifactWriteFailure
from pinboard.application.ports import WorkStore
from pinboard.diagnostic_codes import ProducerOnlyCode
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import (
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
    HistoryId,
    HostId,
    LeaseId,
    TaskId,
    WorkItemId,
)
from pinboard.mcp import common, contracts, execution, tool_names
from pinboard.mcp.contracts import JsonValue

type IntegerBoundaryValue = bool | int | float | str | None


_ITEM_STATUS_RESULT_SCHEMA = "pinboard-mcp-item-status-result/v3"


def _branch_owner_not_found(branch: str, work_root: Path) -> execution.OperationResult:
    return execution.OperationResult(
        {
            "schema": _ITEM_STATUS_RESULT_SCHEMA,
            "status": "rejected",
            "code": "BRANCH_OWNER_NOT_FOUND",
            "message": f"No retained attempt in work root {work_root} records branch '{branch}'.",
            "state_changed": False,
            **common._details_json(
                FailureDetails(
                    observed=(FailureFact("branch", branch), FailureFact("work_root", str(work_root))),
                    mismatches=(),
                    retry=RetryDisposition.CORRECT_INPUT,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                )
            ),
            "recovery": (
                "Check the exact branch name and work root, then read the owning item with operation item when its "
                "id is known. A branch recorded before a rebind is not found because rebind overwrites the "
                "attempt's branch."
            ),
        },
        "rejected",
        None,
    )


def _integration_rejection(
    code: str, message: str, details: FailureDetails, recovery: str
) -> execution.OperationResult:
    return execution.OperationResult(
        {
            "schema": _ITEM_STATUS_RESULT_SCHEMA,
            "status": "rejected",
            "code": code,
            "message": message,
            "state_changed": False,
            **common._details_json(details),
            "recovery": recovery,
        },
        "rejected",
        None,
    )


def _integration_git_failure(project_root: str, code: str, diagnostic: str) -> execution.OperationResult:
    return _integration_rejection(
        code,
        f"The Git content check cannot read the checkout at {project_root}: {diagnostic}",
        FailureDetails(
            observed=(FailureFact("project_root", project_root), FailureFact("diagnostic", diagnostic)),
            mismatches=(),
            retry=RetryDisposition.CORRECT_INPUT,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
        "Pass a project_root inside the Git checkout that holds the reviewed history, or correct the Git or "
        "system temporary-directory condition the diagnostic names, then retry. Nothing was changed.",
    )


def _integration_unavailable_message(unavailable: query_models.IntegrationCandidateUnavailable) -> str:
    item = f"Item '{unavailable.work_item_id}' in state '{unavailable.state.value}'"
    attempt = f"attempt '{unavailable.attempt_id}'"
    match unavailable.reason:
        case query_models.IntegrationUnavailableReason.NO_REVIEWED_CANDIDATE:
            return (
                f"{item} has no reviewed candidate with accepted snapshot bytes: its current attempt has no "
                "protected review candidate and no checkpoint acceptance, and it has no completion candidate."
            )
        case query_models.IntegrationUnavailableReason.CLOSURE_UNKNOWN:
            return f"{item} has no closing receipt at its subject revision, so its closure outcome is unknown."
        case query_models.IntegrationUnavailableReason.CLOSED_WITHOUT_COMPLETION:
            return f"{item} closed without a completion, so no reviewed candidate was accepted."
        case query_models.IntegrationUnavailableReason.CHECKPOINT_WITHOUT_CANDIDATE_SNAPSHOT:
            return (
                f"{item}: the latest checkpoint acceptance of {attempt} has no accepted package reference "
                "for its complete candidate snapshot."
            )
        case query_models.IntegrationUnavailableReason.PRE_SNAPSHOT_CANDIDATE:
            return f"{item}: the reviewed candidate of {attempt} predates accepted candidate snapshots."
        case _ as unreachable:
            assert_never(unreachable)


def _present_integration(
    request: contracts.ItemStatusIntegrationRequest,
    observation: candidate_evidence.ItemIntegrationObservation,
) -> execution.OperationResult:
    match observation:
        case query_models.ItemIntegration():
            content = msgspec.to_builtins(observation)
            assert isinstance(content, dict)
            return execution.OperationResult(content, "ok", observation.revision)
        case root.TargetUnresolved():
            return _integration_rejection(
                "INTEGRATION_TARGET_UNRESOLVED",
                f"Target '{observation.target}' does not name a commit in the local repository at "
                f"{request.project_root}.",
                FailureDetails(
                    observed=(
                        FailureFact("target", observation.target),
                        FailureFact("project_root", request.project_root),
                    ),
                    mismatches=(),
                    retry=RetryDisposition.CORRECT_INPUT,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
                "Name an existing local branch, remote-tracking ref, tag, or full commit id. Pinboard never "
                "fetches; when remote freshness matters, fetch the target outside Pinboard first, then retry.",
            )
        case query_models.IntegrationCandidateUnavailable():
            return _integration_rejection(
                "INTEGRATION_CANDIDATE_UNAVAILABLE",
                _integration_unavailable_message(observation),
                FailureDetails(
                    observed=(
                        FailureFact("item_id", str(observation.work_item_id)),
                        FailureFact("item_state", observation.state.value),
                        FailureFact(
                            "attempt_id", None if observation.attempt_id is None else str(observation.attempt_id)
                        ),
                        FailureFact("reason", observation.reason.value),
                    ),
                    mismatches=(),
                    retry=RetryDisposition.CORRECT_INPUT,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
                "Read the item with operation item to see its attempts, review verdict, and closure; check "
                "integration only for an item with a protected review candidate, an accepted checkpoint on its "
                "current attempt, or a completion.",
            )
        case candidate_evidence.IntegrationEvidenceInvalid():
            reference = observation.reference
            return _integration_rejection(
                "INTEGRATION_CANDIDATE_EVIDENCE_INVALID",
                f"The accepted candidate evidence of attempt '{observation.attempt_id}' failed verification: "
                f"{observation.defect}",
                FailureDetails(
                    observed=(
                        FailureFact("attempt_id", str(observation.attempt_id)),
                        FailureFact("artifact_ref_id", int(reference.artifact_ref_id)),
                        FailureFact("selector", reference.selector),
                        FailureFact("sha256", reference.content_sha256),
                    ),
                    mismatches=(FailureMismatch("evidence", "verified accepted evidence", observation.defect),),
                    retry=RetryDisposition.DO_NOT_RETRY,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
                "Diagnose the damaged evidence with 'pinboard validate --json'; this read repairs nothing and "
                "repeating it cannot succeed until the accepted evidence is restored.",
            )
        case candidate_evidence.IntegrationGitUnavailable():
            return _integration_git_failure(request.project_root, observation.code.value, observation.diagnostic)
        case query_models.DamagedTransitionReceipt():
            return common._damaged_receipt_failure(_ITEM_STATUS_RESULT_SCHEMA, observation)
        case DecisionFailure():
            return common._item_status_failure(observation.code.value, observation.message, observation.details)
        case _ as unreachable:
            assert_never(unreachable)


def _read_item_status(  # noqa: PLR0912 - distinct leaves and selected-state diagnosis
    raw: Mapping[str, JsonValue], token: execution.CancellationToken
) -> execution.OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.ItemStatusEnvelope, strict=True).request
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return common._item_status_failure("ITEM_STATUS_INVALID", f"Cannot read item status: {error}", None)
    try:
        source_checkout, durable = common._resolve_source_and_durable(request.project_root, request.work_root)
    except (ValueError, OSError) as error:
        return common._item_status_failure("ITEM_STATUS_INVALID", f"Cannot read item status: {error}", None)
    except RootError as error:
        if isinstance(request, contracts.ItemStatusIntegrationRequest):
            return _integration_git_failure(request.project_root, error.code.value, str(error))
        raise
    token.checkpoint()
    store = common.compose_store(durable)
    match request:
        case contracts.ItemStatusItemRequest():
            projected = queries.project_item_status(
                store,
                review_operations.AttemptReadyRecordReader(durable, store),
                WorkItemId(request.item_id),
                datetime.now(UTC),
                views.board_pages(durable.work_root),
            )
            if isinstance(projected, DecisionFailure):
                message = projected.message
                if projected.code == DecisionFailureCode.ITEM_STATUS_INCONSISTENT:
                    message += " Report the named item and current-attempt facts to the human for diagnosis. Preserve the ledger; do not retry unchanged or change lifecycle to conceal the inconsistency."
                else:
                    message += " Check the exact project_root, work_root and item_id against the accepted task or pinboard_overview. Report a missing accepted definition to the human; do not create a replacement merely to satisfy this read."
                return common._item_status_failure(projected.code.value, message, projected.details)
            if isinstance(projected, query_models.DamagedTransitionReceipt):
                return common._damaged_receipt_failure(_ITEM_STATUS_RESULT_SCHEMA, projected)
            selected: query_models.ItemStatus | query_models.BranchOwners = projected
        case contracts.ItemStatusBranchRequest():
            owners = queries.project_branch_owners(store, request.branch)
            if owners is None:
                return _branch_owner_not_found(request.branch, durable.work_root)
            selected = owners
        case contracts.ItemStatusIntegrationRequest():
            observation = candidate_evidence.observe_item_integration(
                source_checkout, durable.work_root, store, WorkItemId(request.item_id), request.target
            )
            token.checkpoint()
            return _present_integration(request, observation)
        case _ as unreachable:
            assert_never(unreachable)
    token.checkpoint()
    content = msgspec.to_builtins(selected)
    assert isinstance(content, dict)
    return execution.OperationResult(content, "ok", selected.revision)


def _read_correction_context(
    project_root: str,
    work_root: str,
    attempt_id: str,
    correction_history_id: IntegerBoundaryValue,
    token: execution.CancellationToken,
) -> execution.OperationResult:
    """Expose the dispatcher's effective review subject without publishing it."""
    token.checkpoint()
    try:
        request = msgspec.convert(
            {
                "project_root": project_root,
                "work_root": work_root,
                "attempt_id": attempt_id,
                "correction_history_id": correction_history_id,
            },
            type=contracts.CorrectionContextRequest,
            strict=True,
        )
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return common._read_failure(
            "pinboard-correction-context/v2",
            ProducerOnlyCode.CORRECTION_CONTEXT_INVALID.value,
            str(error),
            None,
        )
    token.checkpoint()
    context = dispatch_operations.read_correction_context(
        common.compose_store(durable),
        ArtifactRepository(durable),
        resolve_source_checkout_root(Path(request.project_root)),
        AttemptId(request.attempt_id),
        HistoryId(request.correction_history_id),
        dispatch_operations.ReturnedCorrectionStart(),
    )
    if isinstance(context, dispatch_operations.DispatchFailure):
        return common._read_failure(
            "pinboard-correction-context/v2",
            context.code.value,
            context.message
            + f" Inspect attempt '{request.attempt_id}' with pinboard_attempt_inspect using the same project_root/work_root and reconciliation:null before choosing a correction round. Report invalid accepted evidence to the human; do not reconstruct it or reset the checkout.",
            context.details,
        )
    result = _correction_context_view(context, request.project_root, request.work_root)
    token.checkpoint()
    content = msgspec.to_builtins(result)
    assert isinstance(content, dict)
    return execution.OperationResult(content, "read", None)


def _correction_snapshot_metadata(
    snapshot: candidate_snapshots.CandidateSnapshot,
) -> contracts.CorrectionSnapshotMetadata:
    return contracts.CorrectionSnapshotMetadata(
        snapshot.schema,
        candidate_snapshots.candidate_kind(snapshot),
        snapshot.attempt_id,
        snapshot.item_id,
        snapshot.candidate,
        snapshot.branch,
        snapshot.preimage_revision,
        snapshot.accepted_base_revision,
        snapshot.recorded_at,
        hashlib.sha256(snapshot.diff).hexdigest(),
        len(snapshot.diff),
        candidate_snapshots.excluded_untracked_paths(snapshot),
    )


def _correction_snapshot_verification(
    reference: stored_state.ArtifactReference, project_root: str, work_root: str
) -> contracts.ArtifactVerifyRequest:
    return contracts.ArtifactVerifyRequest(
        project_root,
        work_root,
        reference.artifact_ref_id,
        reference.selector,
        reference.content_sha256,
        reference.size_bytes,
    )


def _correction_context_view(
    context: dispatch_operations.CorrectionContext, project_root: str, work_root: str
) -> contracts.CorrectionContextReady:
    evidence = _correction_review_evidence(context, project_root, work_root)
    return contracts.CorrectionContextReady(
        schema="pinboard-correction-context/v2",
        status="ready",
        attempt_id=evidence.attempt_id,
        correction_history_id=evidence.correction_history_id,
        correction_reason=evidence.correction_reason,
        effective_brief=evidence.effective_brief,
        effective_brief_sha256=evidence.effective_brief_sha256,
        checkpoint_sha256=evidence.checkpoint_sha256,
        reviewed_authority_set_sha256=evidence.reviewed_authority_set_sha256,
        starting_candidate=evidence.starting_candidate,
        starting_candidate_reference=evidence.starting_candidate_reference,
        starting_candidate_verification=evidence.starting_candidate_verification,
        starting_snapshot_metadata=evidence.starting_snapshot_metadata,
        reuse_eligible=evidence.reuse_eligible,
        reuse_blockers=evidence.reuse_blockers,
        state_changed=False,
        effect="unchanged",
        retry="safe-to-repeat",
        changed_surfaces=(),
    )


def _correction_review_evidence(
    context: dispatch_operations.CorrectionContext, project_root: str, work_root: str
) -> contracts.CorrectionReviewEvidence:
    brief = context.effective_brief
    checkpoint = brief.checkpoint
    reviewed_set_sha256 = (
        hashlib.sha256(work_briefs.canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities)).hexdigest()
        if isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        else None
    )
    reference = context.starting_candidate_reference
    return contracts.CorrectionReviewEvidence(
        context.effective_brief.attempt_id,
        context.correction_history_id,
        context.correction_reason,
        brief,
        hashlib.sha256(work_briefs.canonical_work_brief_bytes(brief)).hexdigest(),
        hashlib.sha256(work_briefs.canonical_checkpoint_bytes(checkpoint)).hexdigest(),
        reviewed_set_sha256,
        context.starting_candidate,
        msgspec.convert(
            common._artifact_reference_json(reference), type=contracts.ReviewEvidenceReference, strict=True
        ),
        _correction_snapshot_verification(reference, project_root, work_root),
        _correction_snapshot_metadata(context.starting_snapshot),
        context.reuse_eligible,
        context.reuse_blockers,
    )


def _read_return_context(raw: Mapping[str, JsonValue], token: execution.CancellationToken) -> execution.OperationResult:
    token.checkpoint()
    schema = "pinboard-return-context/v2"
    try:
        request = msgspec.convert(raw, type=contracts.ReturnContextRequest, strict=True)
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return common._read_failure(schema, ProducerOnlyCode.RETURN_CONTEXT_INVALID.value, str(error), None)
    store = common.compose_store(durable)
    artifacts = ArtifactRepository(durable)
    history_id = HistoryId(request.selection.return_history_id)
    evidence = dispatch_operations.read_returned_evidence(store, artifacts, AttemptId(request.attempt_id), history_id)
    if isinstance(evidence, dispatch_operations.DispatchFailure):
        return common._read_failure(
            schema,
            evidence.code.value,
            evidence.message
            + f" Inspect attempt '{request.attempt_id}' with pinboard_attempt_inspect using the same project_root/work_root and reconciliation:null, and verify the selected return history. Report damaged accepted evidence to the human; do not reconstruct or overwrite it.",
            evidence.details,
        )
    receipt = evidence.receipt
    receipt_view = contracts.ReturnReceiptEvidence(
        receipt.history_id,
        receipt.project_revision,
        receipt.action_id,
        "return-for-correction",
        receipt.subject_id,
        None,
        "project",
        "return-for-correction/v1",
        bytes(receipt.input_payload).decode(),
        "transition-receipt/v1",
        bytes(receipt.outcome_payload).decode(),
        receipt.actor_task_id,
        receipt.actor_host_id,
        receipt.committed_at.isoformat(),
    )
    selected: contracts.HistoricalReturnContext | contracts.CurrentCandidateReturnContext
    match request.selection:
        case contracts.HistoricalReturnSelection():
            selected = contracts.HistoricalReturnContext(
                receipt_view,
                evidence.reason,
                evidence.identity,
                msgspec.convert(
                    common._artifact_reference_json(evidence.reference),
                    type=contracts.ReviewEvidenceReference,
                    strict=True,
                ),
                _correction_snapshot_verification(evidence.reference, request.project_root, request.work_root),
                _correction_snapshot_metadata(evidence.snapshot),
            )
        case contracts.CurrentCandidateReturnSelection():
            source_checkout = resolve_source_checkout_root(Path(request.project_root))
            binding = dispatch_operations.observe_correction_candidate(source_checkout)
            if isinstance(binding, dispatch_operations.DispatchFailure):
                return common._read_failure(schema, binding.code.value, binding.message, binding.details)
            correction = dispatch_operations.read_correction_context(
                store,
                artifacts,
                source_checkout,
                AttemptId(request.attempt_id),
                history_id,
                dispatch_operations.ReviewedCurrentCorrectionStart(binding),
            )
            if isinstance(correction, dispatch_operations.DispatchFailure):
                return common._read_failure(
                    schema,
                    correction.code.value,
                    correction.message
                    + f" Inspect attempt '{request.attempt_id}' with pinboard_attempt_inspect using the same project_root/work_root and reconciliation:null, and verify the selected return history. Report damaged accepted evidence to the human; do not reconstruct or overwrite it.",
                    correction.details,
                )
            selected = contracts.CurrentCandidateReturnContext(
                receipt_view, _correction_review_evidence(correction, request.project_root, request.work_root), binding
            )
        case _ as unreachable:
            assert_never(unreachable)
    token.checkpoint()
    content = msgspec.to_builtins(
        contracts.ReturnContextReady(
            schema, "ready", request.attempt_id, selected, False, "unchanged", "safe-to-repeat", ()
        )
    )
    assert isinstance(content, dict)
    return execution.OperationResult(content, "read", None)


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


def _brief_contract(raw: Mapping[str, JsonValue], token: execution.CancellationToken) -> execution.OperationResult:
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
                "message": f"{error} The plan is visible at '{error.path}'; inspect that exact file and report the synchronization failure to the human. Do not replay publication or delete the visible output.",
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
        return _brief_preparation_failure(
            schema,
            error.code.value,
            f"{error} Inspect the selected output '{destination}' and resolve its named filesystem condition. If different plan bytes already occupy it, choose a fresh explicit destination; immutable plans are never overwritten.",
        )
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


def _brief_sources(raw: Mapping[str, JsonValue], token: execution.CancellationToken) -> execution.OperationResult:
    """Read selected-checkout sources without an output write."""
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
        case contracts.BriefSourcesPlanRequest():
            source_plan = brief_sources.plan_brief_sources(select_source, request.manifest, request.max_batch_bytes)
            if isinstance(source_plan, brief_source_models.BriefSourceFailure):
                return _brief_preparation_failure(schema, source_plan.code.value, source_plan.message)
            content: dict[str, JsonValue] = msgspec.to_builtins(
                brief_source_codec.project_brief_source_plan(source_plan)
            )
            presented_bytes = len(msgspec.json.encode(content))
            if presented_bytes > brief_sources.MAX_PRESENTED_BATCH_BYTES:
                return _brief_preparation_failure(
                    schema,
                    "BRIEF_SOURCE_PLAN_INVALID",
                    f"Read-only plan requires {presented_bytes} presented bytes; limit is "
                    f"{brief_sources.MAX_PRESENTED_BATCH_BYTES}. Split the manifest into smaller source selections "
                    "and plan and read each selection independently without publishing a plan.",
                )
            return execution.OperationResult(content, "read", None)
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
                return _brief_preparation_failure(
                    schema,
                    source_plan.code.value,
                    source_plan.message
                    + f" Inspect saved plan '{request.plan_path}'. Re-plan the original selected manifest under the current negotiated schema at a fresh explicit destination; do not rewrite the old plan.",
                )
        case _ as unreachable:
            assert_never(unreachable)
    token.checkpoint()
    batch = brief_sources.render_brief_source_batch(select_source, source_plan, request.batch_index)
    if isinstance(batch, brief_source_models.BriefSourceFailure):
        message = batch.message
        if batch.code == brief_source_models.BriefSourceErrorCode.SOURCE_CHANGED:
            message += " Re-plan the original selected manifest through pinboard_brief_sources operation plan with the same project_root/work_root and restart at batch_index:0. If saving the plan, use pinboard_brief_source_plan_output operation plan-to-file with a fresh explicit destination; preserve the previous plan."
        return _brief_preparation_failure(schema, batch.code.value, message)
    content: dict[str, JsonValue] = msgspec.to_builtins(
        brief_sources.brief_source_batch_payload(
            request.batch_index, source_plan.batches[request.batch_index].content_byte_count, batch
        )
    )
    return execution.OperationResult(content, "read", None)


def _brief_source_plan_output(
    raw: Mapping[str, JsonValue], token: execution.CancellationToken
) -> execution.OperationResult:
    schema = "pinboard-mcp-brief-sources-result/v1"
    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.BriefSourcePlanOutputEnvelope, strict=True).request
        source_checkout = resolve_source_checkout_root(Path(request.project_root))
    except (msgspec.ValidationError, ValueError) as error:
        return _brief_preparation_failure(schema, "BRIEF_SOURCES_REQUEST_INVALID", str(error))
    except RootError as error:
        return _brief_preparation_failure(schema, error.code.value, str(error))
    token.checkpoint()
    source_plan = brief_sources.plan_brief_sources(
        partial(select_checkout_brief_source, source_checkout), request.manifest, request.max_batch_bytes
    )
    if isinstance(source_plan, brief_source_models.BriefSourceFailure):
        return _brief_preparation_failure(schema, source_plan.code.value, source_plan.message)
    token.checkpoint()
    return _publish_source_plan(Path(request.destination).absolute(), source_plan, token)


def _read_item_definition(
    raw: Mapping[str, JsonValue], token: execution.CancellationToken
) -> execution.OperationResult:
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
            selected = queries.select_item_definition(store, WorkItemId(request.item_id))
        case contracts.ItemDefinitionHistoryRequest():
            selected = queries.select_item_definition_history(
                store, WorkItemId(request.item_id), limit=request.limit, before_revision=request.before_revision
            )
        case _ as unreachable:
            assert_never(unreachable)
    if isinstance(selected, DecisionFailure):
        return common._read_failure(
            "pinboard-mcp-item-definition-result/v1",
            selected.code.value,
            selected.message
            + f" Check item '{request.item_id}' and the exact roots against pinboard_overview or the accepted task. Report a missing accepted definition to the human; do not invent one.",
            selected.details,
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


def _brief_review(  # noqa: C901, PLR0912 - strict inline/file publication and read-only status exits
    raw: Mapping[str, JsonValue], token: execution.CancellationToken
) -> execution.OperationResult:
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
            try:
                selected = work_briefs.read_brief_review_status(
                    store, repository, ArtifactRefId(request.brief_artifact_ref_id)
                )
            except ArtifactError as error:
                return common._read_failure(
                    schema,
                    "ARTIFACT_BYTES_INVALID",
                    f"Brief-review status for accepted brief {request.brief_artifact_ref_id} at work_root "
                    f"'{request.work_root}' could not verify its accepted bytes: {error}. "
                    "Report this exact reference and defect to the human; do not retry unchanged, "
                    "reconstruct or overwrite accepted bytes.",
                    FailureDetails(
                        observed=(FailureFact("brief_artifact_ref_id", request.brief_artifact_ref_id),),
                        mismatches=(
                            FailureMismatch(
                                "artifact_bytes", "match accepted size and SHA-256", "unreadable or mismatched"
                            ),
                        ),
                        retry=RetryDisposition.DO_NOT_RETRY,
                        effect=EffectDisposition.UNCHANGED,
                        changed_surfaces=(),
                        alternatives=(),
                    ),
                )
            if isinstance(selected, work_brief_models.WorkBriefFailure):
                return common._read_failure(
                    schema,
                    selected.code.value,
                    selected.message
                    + f" Check accepted brief reference {request.brief_artifact_ref_id} at work_root '{request.work_root}'. Correct a mistaken selection; report damaged accepted bytes to the human without rewriting them.",
                    None,
                )
            token.checkpoint()
            checkpoint = selected.accepted_brief.brief.checkpoint
            assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
            content: dict[str, JsonValue] = {
                "schema": schema,
                "accepted_brief": common._artifact_reference_json(selected.accepted_brief.reference),
                "accepted_brief_sha256": selected.accepted_brief.reference.content_sha256,
                "checkpoint_sha256": hashlib.sha256(work_briefs.canonical_checkpoint_bytes(checkpoint)).hexdigest(),
                "reviewed_authority_set_sha256": hashlib.sha256(
                    work_briefs.canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities)
                ).hexdigest(),
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
        case contracts.BriefReviewPublishRequest() | contracts.BriefReviewPublishFileRequest():
            if isinstance(request, contracts.BriefReviewPublishFileRequest):
                review_bytes = file_io.read_verified_input(Path(request.review_path), request.review_sha256)
                if isinstance(review_bytes, file_io.SelectedInputFailure):
                    return common._read_failure(schema, review_bytes.code, review_bytes.message, None)
                try:
                    review = msgspec.json.decode(review_bytes, type=work_brief_models.WorkBriefReviewNeedsCorrection)
                except msgspec.DecodeError as error:
                    return common._read_failure(
                        schema,
                        "BRIEF_REVIEW_REQUEST_INVALID",
                        f"Cannot decode selected review '{request.review_path}' with verified SHA-256 {request.review_sha256}: {error}. Correct that file to the needs-correction review schema before retrying.",
                        None,
                    )
            else:
                review = request.review
            token.checkpoint()
            publication = work_briefs.publish_brief_review_needs_correction(
                store,
                repository,
                repository,
                ArtifactRefId(request.brief_artifact_ref_id),
                review,
                datetime.now(UTC),
            )
            if isinstance(publication, (ArtifactAcceptanceFailure, ArtifactWriteFailure)):
                return common._artifact_publication_failure(
                    schema,
                    publication,
                    acceptance_message="The review's accepted reference could not be committed.",
                    publication_message="The review's immutable publication could not be completed.",
                    acceptance_recovery=f"Inspect the published selector under '{durable.work_root}' and pinboard_brief_review operation status with brief_artifact_ref_id:{request.brief_artifact_ref_id} and the same roots. Report the named storage condition; do not replay after a committed effect.",
                    publication_recovery=f"Inspect the published selector under '{durable.work_root}' and resolve the named filesystem condition. After any committed publication, do not publish again merely to recover the response.",
                )
            if isinstance(publication, work_brief_models.WorkBriefFailure):
                return common._read_failure(
                    schema,
                    publication.code.value,
                    publication.message
                    + " Correct the named review binding against pinboard_brief_review operation status for the same brief reference and roots; independent assessment must remain independent. Report damaged accepted evidence to the human without rewriting it.",
                    None,
                )
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
            view_warning = (
                common._refresh_affected_views(durable, store, AffectedViews((), (), ()), datetime.now(UTC)).warning
                if publication.ledger_changed
                else None
            )
            return execution.OperationResult(
                {
                    "schema": schema,
                    "status": ("committed" if view_warning is None else "committed-with-warning")
                    if surfaces
                    else "unchanged",
                    "warning": None
                    if view_warning is None
                    else {"message": view_warning.message, "recovery": view_warning.repair},
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
    overview = queries.project_current_overview(
        store.read_project_overview(operation_time), operation_time, views.board_pages(durable.work_root)
    )
    token.checkpoint()
    content = msgspec.to_builtins(overview)
    assert isinstance(content, dict)
    return execution.OperationResult(content, "ok", overview.revision)


def _order(raw: Mapping[str, JsonValue], token: execution.CancellationToken) -> execution.OperationResult:
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
        durable,
        store,
        AffectedViews(committed.work_item_ids, committed.attempt_ids, (committed.receipt.history_id,)),
        now,
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


def _parallel_preview(raw: Mapping[str, JsonValue], token: execution.CancellationToken) -> execution.OperationResult:
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
            "pinboard-mcp-parallel-preview-result/v1",
            "PARALLEL_SELECTION_INVALID",
            preview.message
            + " Read pinboard_overview with the same exact roots to verify current item IDs, then correct the intended selection; this preview grants no launch authority.",
            None,
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
    selected_action: contracts.ActionIdentity | None,
    project_root: str,
    work_root: str,
) -> FailureDetails:
    observations = (
        FailureFact("role", role.value),
        FailureFact("lease_id", lease_id),
        FailureFact("generation", generation),
        FailureFact("action_id", action_id),
    )
    inspection_tool = tool_names.OVERVIEW_TOOL
    inspection_input: dict[str, JsonValue] = {"project_root": project_root, "work_root": work_root}
    match role:
        case decision_models.Role.WORKER:
            if (
                selected_action is not None
                and decision_models.action_semantics(selected_action.kind).subject_kind
                == decision_models.ActionSubjectKind.ATTEMPT
            ):
                inspection_tool = tool_names.ATTEMPT_AUTHORITY_TOOL
                inspection_input = {
                    "request": {**inspection_input, "operation": "status", "attempt_id": selected_action.subject}
                }
            note = common.ATTEMPT_LEASE_RECOVERY
        case decision_models.Role.PREPARER:
            if (
                selected_action is not None
                and decision_models.action_semantics(selected_action.kind).subject_kind
                == decision_models.ActionSubjectKind.ITEM
            ):
                inspection_tool = tool_names.PREPARATION_AUTHORITY_TOOL
                inspection_input = {
                    "request": {**inspection_input, "operation": "status", "item_id": selected_action.subject}
                }
            note = "Read pinboard_preparation_authority operation status for your accepted item's item_id before starting preparation again; do not borrow a conflicting lease."
        case decision_models.Role.PROJECT | decision_models.Role.OBSERVER:
            note = "Use current subject facts to rediscover only the intended supported action; discovery grants no mutation or human-decision authority."
        case _ as unreachable:
            assert_never(unreachable)
    observations += (
        FailureFact("next_step_tool", inspection_tool),
        FailureFact("next_step_input", msgspec.json.encode(inspection_input, order="sorted").decode()),
        FailureFact("next_step", note),
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
    raw: Mapping[str, JsonValue],
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
                failure = _completion_warning(
                    recovery.attempt_id, recovery.reason, request.project_root, request.work_root
                )
                return common._read_failure(
                    "pinboard-mcp-actions-result/v1", failure.code.value, failure.message, failure.details
                )
        return common._read_failure(
            "pinboard-mcp-actions-result/v1",
            selected.code.value,
            selected.message,
            _action_failure_details(
                selected,
                selected_role,
                selected_lease,
                selected_generation,
                selected_action,
                selected_action_id,
                request.project_root,
                request.work_root,
            ),
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
    """Project a legal action; focused ending detail adds its current decision evidence."""
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
                f"Accepted attempt brief could not be verified: {error}. Report the exact accepted brief and defect to the human; do not retry unchanged, reconstruct or overwrite accepted bytes.",
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
            return _completion_warning(
                completion.attempt_id,
                "Terminal completion lacks a protected current review candidate.",
                project_root,
                work_root,
            )
        if isinstance(completion, query_models.CompletionRecoveryRequired):
            return _completion_warning(completion.attempt_id, completion.reason, project_root, work_root)
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
    if isinstance(action, decision_models.CloseAction) and focused:
        facts = store.read_obligation_coverage(action.capability.subject, None)
        coverage = None if facts.replacement_item is None else obligation_coverage.report(facts)
        if isinstance(coverage, DecisionFailure):
            return coverage
        content["replacement_coverage"] = msgspec.to_builtins(coverage)
    return content


def _completion_warning(
    attempt_id: AttemptId,
    reason: str,
    project_root: str,
    work_root: str,
) -> DecisionFailure:
    """Report pending closure evidence with one read-only inspection."""
    inspection: dict[str, JsonValue] = {
        "project_root": project_root,
        "work_root": work_root,
        "attempt_id": str(attempt_id),
        "reconciliation": None,
    }
    return DecisionFailure(
        DecisionFailureCode.ACTION_NOT_AVAILABLE,
        f"{reason} Inspect this attempt's accepted work and current evidence before deciding what remains.",
        FailureDetails(
            observed=(
                FailureFact("attempt_id", str(attempt_id)),
                FailureFact("completion_inspection_tool", tool_names.ATTEMPT_INSPECT_TOOL),
                FailureFact("completion_inspection_input", msgspec.json.encode(inspection, order="sorted").decode()),
            ),
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
    item = attempt.work_item
    if not item.replacement_resolved:
        return query_models.CompletionRecoveryRequired(
            attempt_id,
            "Completion is withheld until the unresolved replacement is recorded or its temporary cost is explicitly retained.",
        )
    if (attempt.accepted_scope_revision, attempt.accepted_scope_digest) != (
        item.current_definition_revision,
        item.current_definition_digest,
    ):
        return query_models.CompletionRecoveryRequired(
            attempt_id,
            "Completion is withheld because the stale accepted attempt binding differs from the current definition.",
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
    brief: work_brief_models.WorkBrief | None,
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
    subject = review_operations.ready_record_subject(
        brief,
        context.candidate_revision,
        None if snapshot is None else snapshot.reference,
        context.brief_reference,
        review_operations.AttemptEvidenceDigests(result.sha256, review.sha256),
    )
    current = (
        None if subject is None else review_operations.read_ready_record(store, ArtifactRepository(durable), subject)
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
        case query_models.ReconcileRepositoryContinuation(candidate_revision=candidate, condition=condition):
            return contracts.ContinuationReconcileRepository(candidate, condition)
        case query_models.DependencyContinuation(dependencies=dependencies):
            return contracts.ContinuationDependencies(dependencies)
        case query_models.RefreshTargetContinuation(target_revision=target_revision):
            return contracts.ContinuationRefreshTarget(target_revision)
        case query_models.PermissionRecoveryContinuation(target_revision=target_revision, effect=effect, status=status):
            return contracts.ContinuationPermissionRecovery(target_revision, effect, status)
        case query_models.RepositoryDispositionContinuation(target_revision=target_revision, relation=relation):
            return contracts.ContinuationRepositoryDisposition(target_revision, relation)
        case query_models.CommitThenReinspectContinuation(target_revision=target_revision, relation=relation):
            return contracts.ContinuationCommitThenReinspect(target_revision, relation)
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
            constructor = (
                contracts.CancelledAttemptContinuation
                if isinstance(continuation, query_models.CancelledAttemptContinuation)
                else contracts.TerminalAttemptContinuation
            )
            return constructor(
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
                continuation.decision_batch,
            )
            match continuation:
                case query_models.ActiveAttemptContinuation():
                    return contracts.ActiveAttemptContinuation(*arguments)
                case query_models.ReviewAttemptContinuation():
                    return contracts.ReviewAttemptContinuation(*arguments)
                case query_models.PausedAttemptContinuation(pause_reason=pause_reason):
                    return contracts.PausedAttemptContinuation(*arguments, pause_reason)
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
            f"Accepted candidate snapshot '{durable.work_root / snapshot_context.reference.selector}' could not be verified: {error}. Report this accepted reference and defect to the human; do not retry unchanged, reconstruct or overwrite the snapshot.",
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


def _candidate_lineage_failure(attempt_id: str, message: str) -> execution.OperationResult:
    return common._read_failure(
        "pinboard-mcp-attempt-inspection-result/v1",
        "ATTEMPT_BRIEF_INVALID",
        message
        + " Report the selected checkout, protected candidate and exact access/evidence defect to the human. Restore the named resource before reinspection; do not reset the checkout or rewrite accepted snapshot bytes.",
        FailureDetails(
            observed=(FailureFact("attempt_id", attempt_id),),
            mismatches=(FailureMismatch("candidate_lineage", "readable exact protected candidate", "unavailable"),),
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def _candidate_lineage_for_disposition(
    project_root: str,
    durable: DurableRoots,
    store: WorkStore,
    context: query_models.AttemptContextFacts,
    attempt_id: str,
    ready_review: bool,
    reconciliation: query_models.AttemptReconciliation | None,
) -> query_models.CandidateLineage | execution.OperationResult | None:
    if (
        reconciliation is None
        or not ready_review
        or not isinstance(context, query_models.NonterminalAttemptContextFacts)
        or reconciliation.relation
        not in (
            query_models.IntegrationRelation.CANDIDATE_PENDING_ON_ACCEPTED_BASE,
            query_models.IntegrationRelation.CANDIDATE_PENDING_ON_SQUASH_EQUIVALENT_BASE,
        )
        or any(
            observation.status in (query_models.RuntimeEffectStatus.DENIED, query_models.RuntimeEffectStatus.UNKNOWN)
            for observation in reconciliation.effects
        )
    ):
        return None
    evidence = candidate_evidence.read_candidate_evidence(
        durable.work_root, store, AttemptId(attempt_id), context.candidate_revision
    )
    if isinstance(evidence, DecisionFailure):
        return _candidate_lineage_failure(
            attempt_id,
            f"Accepted candidate snapshot could not be revalidated: {evidence.message}",
        )
    try:
        source_checkout = resolve_source_checkout_root(Path(project_root))
        lineage = candidate_evidence.observe_candidate_lineage(source_checkout, evidence)
    except RootError as error:
        return _candidate_lineage_failure(
            attempt_id,
            f"Cannot reobserve the protected candidate checkout: {error}",
        )
    if isinstance(lineage, DecisionFailure):
        return _candidate_lineage_failure(attempt_id, lineage.message)
    return lineage


def _inspection_context(
    store: ports.AttemptInspectionContextReader, attempt_id: AttemptId
) -> query_models.AttemptContextFacts | execution.OperationResult:
    context = store.read_attempt_inspection_context(attempt_id)
    if context is None:
        return common._read_failure(
            "pinboard-mcp-attempt-inspection-result/v1",
            "ATTEMPT_NOT_FOUND",
            f"Attempt '{attempt_id}' does not exist. Check the exact roots and accepted attempt_id; pinboard_item_status operation item for the accepted item lists its current attempt. Do not create a new task or attempt merely to satisfy this read.",
            None,
        )
    if isinstance(context, query_models.DamagedTransitionReceipt):
        return common._damaged_receipt_failure("pinboard-mcp-attempt-inspection-result/v1", context)
    return context


def _read_attempt_inspection(  # noqa: C901 - exact read path preserves independent evidence and failure gates
    project_root: str,
    work_root: str,
    attempt_id: str,
    reconciliation: Mapping[str, JsonValue] | None,
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
    context = _inspection_context(store, AttemptId(request.attempt_id))
    if isinstance(context, execution.OperationResult):
        return context
    token.checkpoint()
    accepted_brief: contracts.AcceptedBriefIdentity | None = None
    owner_task_id: TaskId | None = None
    decoded_brief: work_brief_models.WorkBrief | None = None
    if isinstance(context, query_models.NonterminalAttemptContextFacts):
        reference = context.brief_reference
        try:
            brief = work_briefs.decode_canonical_work_brief(read_reference(durable.work_root, reference))
        except ArtifactError as error:
            return common._read_failure(
                "pinboard-mcp-attempt-inspection-result/v1",
                "ATTEMPT_BRIEF_INVALID",
                f"Accepted attempt brief could not be verified: {error}. Report the exact accepted brief and defect to the human; do not retry unchanged, reconstruct or overwrite accepted bytes.",
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
                brief.message
                + f" Report accepted brief '{durable.work_root / reference.selector}' and its canonical-content defect to the human; do not rewrite accepted bytes.",
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
                failure.message
                + f" Report accepted brief '{durable.work_root / reference.selector}' and the current attempt identity mismatch to the human; do not normalize or rewrite accepted facts.",
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
                f"Accepted brief reference identity differs from attempt '{request.attempt_id}' at '{durable.work_root / reference.selector}'. Report the conflicting reference facts to the human; preserve accepted bytes and ledger state.",
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
            f"Attempt evidence under '{attempt_root}' could not be inspected: {error}. Ask the human to restore access to the named evidence resource before reinspection; do not fabricate missing result or review evidence.",
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
    candidate_lineage = _candidate_lineage_for_disposition(
        project_root,
        durable,
        store,
        context,
        request.attempt_id,
        current_review is not None,
        request.reconciliation,
    )
    if isinstance(candidate_lineage, execution.OperationResult):
        return candidate_lineage
    continuation = queries.project_attempt_continuation(
        context,
        owner_task_id,
        decoded_brief,
        request.reconciliation,
        candidate_lineage,
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
            continuation.message
            + f" Read pinboard_item_status operation item for '{context.work_item_id}' with the same roots and compare its review_verdict and current binding. Report an unresolved inconsistency to the human; do not choose a substitute task or lifecycle mutation.",
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
            f"Cannot verify artifact request: {error}. "
            "Compare supplied project_root, work_root, artifact_ref_id, selector, sha256 and size_bytes with the "
            "exact producing response in available conversation/tool history; copy returned values exactly, "
            "never guess or pad a digest. For a worker/reviewer prompt, stop this invalid native context and ask "
            "the coordinator for a fresh coordinator-validated dispatch/review-job recipe; launch with unchanged "
            "native arguments and verify before reading the prompt. Never bypass verification or read an "
            "unverified prompt. If original response history is inaccessible, supported recovery is unavailable, "
            "or exact returned values still fail, preserve the full original response when available and the "
            "complete structured error and report them to the human/coordinator for investigation.",
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
            f"Accepted artifact reference {request.artifact_ref_id} does not exist under '{request.work_root}'. Check the exact work_root and obtain the intended accepted tuple from its producing operation; do not create a reference to satisfy verification.",
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
            "The supplied facts do not match the accepted artifact reference. Confirm this is the intended accepted artifact, then correct the supplied selector, SHA-256 or size from the reported mismatches; never silently select different bytes.",
            FailureDetails(
                observations,
                mismatches,
                RetryDisposition.CORRECT_INPUT,
                EffectDisposition.UNCHANGED,
                (),
                (),
            ),
        )
    return _verify_artifact_reference(durable, reference, token)


def _lookup_brief(
    project_root: str,
    work_root: str,
    attempt_id: str,
    artifact_revision: IntegerBoundaryValue,
    token: execution.CancellationToken,
) -> execution.OperationResult:
    """Recover one accepted brief receipt without publication, binding, or capture effects."""
    token.checkpoint()
    try:
        request = msgspec.convert(
            {
                "project_root": project_root,
                "work_root": work_root,
                "attempt_id": attempt_id,
                "artifact_revision": artifact_revision,
            },
            type=contracts.BriefLookupRequest,
            strict=True,
        )
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError, RootError) as error:
        return common._read_failure(
            "pinboard-mcp-brief-lookup-result/v1",
            "BRIEF_LOOKUP_INVALID",
            f"Cannot look up brief for project_root={project_root!r}, work_root={work_root!r}, "
            f"attempt_id={attempt_id!r}, artifact_revision={artifact_revision!r}: {error}. "
            "Correct the named input or root condition, then repeat this read; do not replay publication.",
            None,
        )
    reference = common.compose_store(durable).read_artifact_reference(
        work_models.ArtifactKind.BRIEF, request.attempt_id, request.artifact_revision
    )
    if reference is None:
        return common._read_failure(
            "pinboard-mcp-brief-lookup-result/v1",
            "BRIEF_NOT_FOUND",
            f"No accepted brief matches attempt '{request.attempt_id}' revision {request.artifact_revision} "
            f"under project_root '{request.project_root}', work_root '{request.work_root}'. "
            "Confirm these original request values. Absence does not prove the original publication had no "
            "partial immutable-file effect; report the lost publication and these values to the human for "
            "diagnosis. Do not reconstruct bytes or blindly replay publication.",
            FailureDetails(
                observed=(
                    FailureFact("project_root", request.project_root),
                    FailureFact("work_root", request.work_root),
                    FailureFact("attempt_id", request.attempt_id),
                    FailureFact("artifact_revision", request.artifact_revision),
                ),
                mismatches=(FailureMismatch("accepted_brief_reference", "present", "absent"),),
                retry=RetryDisposition.CORRECT_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    return _verify_artifact_reference(durable, reference, token)


def _verify_artifact_reference(
    durable: DurableRoots,
    reference: stored_state.ArtifactReference,
    token: execution.CancellationToken,
) -> execution.OperationResult:
    observations = (
        FailureFact("artifact_ref_id", int(reference.artifact_ref_id)),
        FailureFact("selector", reference.selector),
        FailureFact("sha256", reference.content_sha256),
        FailureFact("size_bytes", reference.size_bytes),
    )
    try:
        read_reference(durable.work_root, reference)
    except ArtifactError as error:
        return common._read_failure(
            "pinboard-mcp-artifact-verification-result/v1",
            "ARTIFACT_BYTES_INVALID",
            f"Accepted artifact {reference.artifact_ref_id} at '{durable.work_root / reference.selector}' could not be verified: {error}. Report this exact reference and defect to the human; do not retry unchanged, reconstruct or overwrite accepted bytes.",
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
            int(reference.artifact_ref_id),
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


def _fixture_review_rejected(
    resource: str, message: str, recovery: str, max_batch_bytes: int | None
) -> execution.OperationResult:
    result = contracts.FixtureReviewRejected(
        "pinboard-fixture-review-result/v1",
        "rejected",
        "FIXTURE_REVIEW_INVALID",
        resource,
        message,
        recovery,
        False,
        "unchanged",
        "correct-input",
        (),
    )
    if max_batch_bytes is not None and len(msgspec.json.encode(result)) > max_batch_bytes:
        result = msgspec.structs.replace(result, message="Selected input is invalid.")
        if len(msgspec.json.encode(result)) > max_batch_bytes:
            result = msgspec.structs.replace(
                result,
                resource="max_batch_bytes",
                message="The budget cannot fit the rejection's resource and correction.",
                recovery="Raise max_batch_bytes (up to 16000), or shorten selected paths, then restart with operation start.",
            )
    content: dict[str, JsonValue] = msgspec.to_builtins(result)
    return execution.OperationResult(content, "rejected", None)


def _fixture_page_content(
    request: contracts.FixtureReviewStartRequest | contracts.FixtureReviewContinueRequest,
    view: str,
    view_sha256: str,
    start: int,
    end: int,
    before_size: int,
    after_size: int,
    view_size: int,
) -> dict[str, JsonValue]:
    continuation = (
        contracts.FixtureReviewContinueRequest(
            request.project_root,
            request.work_root,
            request.before,
            request.after,
            request.recipe,
            request.max_batch_bytes,
            view_sha256,
            end,
        )
        if end < len(view)
        else None
    )
    return {
        "schema": "pinboard-fixture-review-page/v1",
        "status": "ok",
        "before": msgspec.to_builtins(request.before),
        "before_size_bytes": before_size,
        "after": msgspec.to_builtins(request.after),
        "after_size_bytes": after_size,
        "recipe": msgspec.to_builtins(request.recipe),
        "view_sha256": view_sha256,
        "view_size_bytes": view_size,
        "start_character": start,
        "end_character": end,
        "text": view[start:end],
        "max_batch_bytes": request.max_batch_bytes,
        "continuation": msgspec.to_builtins(continuation),
        "authority": "presentation-only",
        "state_changed": False,
        "effect": "unchanged",
        "retry": "safe-to-repeat",
        "changed_surfaces": [],
    }


def _fixture_review(raw: Mapping[str, JsonValue], token: execution.CancellationToken) -> execution.OperationResult:
    """Verify two selected files, compare decoded fields and return one bound page without state access."""
    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.FixtureReviewEnvelope, strict=True).request
    except (msgspec.ValidationError, ValueError) as error:
        return _fixture_review_rejected(
            "request",
            str(error),
            "Correct the request against pinboard_fixture_review's advertised start/continue schema.",
            None,
        )
    selected: list[bytes] = []
    selections: list[contracts.FixtureSource] = []
    for source in (request.before, request.after):
        path = Path(source.path)
        if not path.is_absolute():
            path = Path(request.project_root) / path
        path = path.absolute()
        acquired = fixture_review_files.read_verified_fixture(path, source.sha256)
        if isinstance(acquired, fixture_review.FixtureReviewFailure):
            return _fixture_review_rejected(
                acquired.resource,
                acquired.message,
                "Select the intended readable fixture and supply the SHA-256 of its complete bytes; restart with operation start.",
                request.max_batch_bytes,
            )
        selected.append(acquired)
        selections.append(contracts.FixtureSource(str(path), source.sha256))
        token.checkpoint()
    request = msgspec.structs.replace(request, before=selections[0], after=selections[1])
    view = fixture_review.render_fixture_review(
        selected[0],
        selected[1],
        request.recipe.repeat_text,
        request.recipe.repeat_count,
    )
    if isinstance(view, fixture_review.FixtureReviewFailure):
        source = request.before if view.resource == "before" else request.after
        return _fixture_review_rejected(
            source.path,
            view.message,
            "Select unambiguous UTF-8 JSON objects of serialized JSON strings, supply their exact hashes, and restart with operation start.",
            request.max_batch_bytes,
        )
    view_sha256 = hashlib.sha256(msgspec.json.encode((request.before, request.after, request.recipe, view))).hexdigest()
    match request:
        case contracts.FixtureReviewStartRequest():
            start = 0
        case contracts.FixtureReviewContinueRequest():
            start = request.start_character
            if request.view_sha256 != view_sha256 or start >= len(view):
                return _fixture_review_rejected(
                    "view",
                    "Continuation does not identify this exact source/recipe/view or an available character.",
                    "Restart with operation start for these exact sources and recipe, then follow its continuation verbatim.",
                    request.max_batch_bytes,
                )
        case _ as unreachable:
            assert_never(unreachable)
    token.checkpoint()
    page = _bounded_fixture_page(request, view, view_sha256, start, len(selected[0]), len(selected[1]))
    if isinstance(page, execution.OperationResult):
        return page
    msgspec.convert(page, type=contracts.FixtureReviewPage, strict=True)
    token.checkpoint()
    return execution.OperationResult(page, "read", None)


def _bounded_fixture_page(
    request: contracts.FixtureReviewStartRequest | contracts.FixtureReviewContinueRequest,
    view: str,
    view_sha256: str,
    start: int,
    before_size: int,
    after_size: int,
) -> dict[str, JsonValue] | execution.OperationResult:
    view_size = len(view.encode("utf-8"))
    # A final page has less metadata; price it first when its text can fit at all.
    if len(view) - start <= request.max_batch_bytes:
        page = _fixture_page_content(request, view, view_sha256, start, len(view), before_size, after_size, view_size)
        if len(msgspec.json.encode(page)) <= request.max_batch_bytes:
            return page
    low, high = start, min(len(view) - 1, start + request.max_batch_bytes)
    while low < high:
        end = (low + high + 1) // 2
        candidate = _fixture_page_content(request, view, view_sha256, start, end, before_size, after_size, view_size)
        if len(msgspec.json.encode(candidate)) <= request.max_batch_bytes:
            low = end
        else:
            high = end - 1
    if low == start:
        return _fixture_review_rejected(
            "max_batch_bytes",
            "The budget cannot fit page identities, continuation and one character.",
            "Raise max_batch_bytes (up to 16000), or shorten the selected paths/recipe, then restart with operation start.",
            request.max_batch_bytes,
        )
    return _fixture_page_content(request, view, view_sha256, start, low, before_size, after_size, view_size)
