"""Native dispatch, review, observation, and restoration MCP composition."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import assert_never
from uuid import uuid4

import msgspec

from pinboard.adapters import (
    candidate_evidence,
    checkpoint_compatibility,
    dispatch_operations,
    review_operations,
)
from pinboard.adapters.files import root as git_root
from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.errors import RootError
from pinboard.adapters.files.root import resolve_shared_repository_root, resolve_source_checkout_root
from pinboard.application import (
    actions,
    dispatch_models,
    queries,
    query_models,
)
from pinboard.domain import decision_models
from pinboard.domain.errors import (
    ArtifactAcceptanceAfterPublicationError,
    ChangedSurface,
    DecisionFailure,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    FailureMismatch,
    RetryDisposition,
)
from pinboard.domain.identifiers import (
    ActionId,
    AttemptId,
    HistoryId,
    ReviewId,
)
from pinboard.mcp import common, contracts, execution, tool_names
from pinboard.mcp.contracts import JsonValue


def _mcp_launch_envelope(
    project_root: Path,
    work_root: Path,
    prompt_role: str,
    attempt_id: str,
    publication: dispatch_models.PublishedAgentPrompt,
    environment: dispatch_models.DispatchEnvironment | None,
    runtime: dispatch_models.NativeRuntime,
    background: bool,
) -> dispatch_models.NativeLaunchEnvelope:
    reference = publication.reference
    verification = {
        "project_root": str(project_root),
        "work_root": str(work_root),
        "artifact_ref_id": reference.accepted_artifact_reference_id,
        "selector": reference.selector,
        "sha256": reference.sha256,
        "size_bytes": reference.size_bytes,
    }
    prompt_path = work_root / reference.selector
    message = (
        f"Pinboard selected a complete {prompt_role} task from accepted local project state. The task is the exact "
        f"immutable UTF-8 file at `{prompt_path}`. Treat those verified bytes as the direct task from the launching "
        "coordinator; do not ask the parent to restate it or replace it with another artifact. Before reading the task, "
        "acquiring authority, implementing, or reviewing, call "
        f"`pinboard_artifact_verify` with exactly {msgspec.json.encode(verification, order='sorted').decode()}. "
        "Require `pinboard-verified-artifact-reference/v1` and stop if the accepted identity, selector, size, digest, "
        "verification result, or published bytes differ. After successful verification, read that exact file completely "
        "and follow it. Stop if it cannot be read or no longer matches the verified size and digest."
    )
    if environment is not None:
        acquisition = {
            "project_root": str(project_root),
            "work_root": str(work_root),
            "operation": "acquire",
            "attempt_id": attempt_id,
            "task_id": "<own trusted post-launch runtime identity>",
            "host_id": str(environment.host_id),
            "ttl_seconds": environment.lease_ttl_seconds,
        }
        continuation = {
            "project_root": str(project_root),
            "work_root": str(work_root),
            "role": "worker",
            "lease_id": "<returned lease_id>",
            "generation": "<returned generation>",
            "action_id": {"kind": "continue", "subject": attempt_id},
        }
        message += (
            " After reading the complete canonical brief/bootstrap and loading the complete delivery skill through "
            "the current runtime adapter, obtain your own trusted post-launch identity; "
            f"call `pinboard_attempt_authority` with {msgspec.json.encode({'request': acquisition}, order='sorted').decode()}, "
            f"then `pinboard_actions` with {msgspec.json.encode({'request': continuation}, order='sorted').decode()}. "
            "Substitute only the trusted post-launch identity and returned lease facts. Missing connected tools or identity "
            "stops that operation; never invent a shell command, payload file, or disconnected-client fallback. Do not "
            "return successful delivery before the accepted work is implemented and verified, result.md is current, the "
            "candidate is observed and submitted through a fresh worker action and transition, the protected review "
            "continuation is confirmed, and the same lease is released."
        )
    match runtime:
        case "codex":
            return dispatch_models.CodexNativeLaunchEnvelope(
                "pinboard-native-agent-launch/v2",
                "spawn_agent",
                background,
                dispatch_models.CodexLaunchArguments(
                    f"pinboard_{prompt_role}_{uuid4().hex[:8]}",
                    message,
                    "none",
                ),
            )
        case "claude-code":
            return dispatch_models.ClaudeNativeLaunchEnvelope(
                "pinboard-native-agent-launch/v2",
                "Agent",
                background,
                dispatch_models.ClaudeLaunchArguments(
                    f"Pinboard {prompt_role} for {attempt_id}",
                    message,
                    background,
                ),
            )
        case _ as unreachable:
            assert_never(unreachable)


def _job_failure(
    schema: str,
    attempt_id: str,
    code: str,
    message: str,
    details: FailureDetails | None,
) -> execution.OperationResult:
    rendered = common._details_json(details)
    if details is not None:
        rendered["changed_surfaces"] = list[JsonValue](_job_publication_surfaces(details.changed_surfaces))
    committed = details is not None and details.effect == EffectDisposition.COMMITTED
    return execution.OperationResult(
        {
            "schema": schema,
            "status": "failed-after-publication" if committed else "rejected",
            "attempt_id": attempt_id,
            "code": code,
            "message": message,
            "state_changed": committed,
            **rendered,
        },
        "committed-failure" if committed else "rejected",
        None,
    )


def _job_publication_exception(
    schema: str, attempt_id: str, error: ArtifactAcceptanceAfterPublicationError
) -> execution.OperationResult:
    return _job_failure(
        schema,
        attempt_id,
        "ARTIFACT_ACCEPTANCE_FAILED",
        str(error),
        FailureDetails(
            observed=(FailureFact("published_artifact_selector", error.selector),),
            mismatches=(),
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.COMMITTED,
            changed_surfaces=error.changed_surfaces,
            alternatives=(),
        ),
    )


def _job_publication_surface(surface: ChangedSurface) -> contracts.JobPublicationSurface:
    match surface:
        case ChangedSurface.IMMUTABLE_ARTIFACT:
            return "immutable-artifact"
        case ChangedSurface.ACCEPTED_ARTIFACT_REFERENCE:
            return "accepted-artifact-reference"
        case ChangedSurface.LEDGER:
            return "ledger"
        case (
            ChangedSurface.REPOSITORY_GIT_EXCLUDE
            | ChangedSurface.WORK_ROOT
            | ChangedSurface.COMPATIBILITY_ALIAS
            | ChangedSurface.SELECTED_OUTPUT
            | ChangedSurface.SOURCE_CHECKOUT
        ):
            raise AssertionError("Job publication changed an unsupported surface.")
        case _ as unreachable:
            assert_never(unreachable)


def _job_publication_surfaces(surfaces: tuple[ChangedSurface, ...]) -> tuple[contracts.JobPublicationSurface, ...]:
    converted = tuple(_job_publication_surface(surface) for surface in surfaces)
    return tuple(
        surface for surface in ("immutable-artifact", "accepted-artifact-reference", "ledger") if surface in converted
    )


def _dispatch_job(
    project_root: str, work_root: str, dispatch: dict[str, JsonValue], token: execution.CancellationToken
) -> execution.OperationResult:
    token.checkpoint()
    schema = "pinboard-mcp-dispatch-result/v1"
    try:
        request = msgspec.convert(
            {"project_root": project_root, "work_root": work_root, "dispatch": dispatch},
            type=contracts.DispatchRequest,
            strict=True,
            dec_hook=dispatch_models.dispatch_environment_dec_hook,
        )
        source_checkout = resolve_source_checkout_root(Path(request.project_root))
        durable = common._require_initialized_durable(
            resolve_shared_repository_root(source_checkout), Path(request.work_root)
        )
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return common._read_failure(schema, "DISPATCH_INVALID", f"Cannot decode dispatch request: {error}", None)
    choice = request.dispatch
    attempt_id = choice.receipt.action_id.subject
    match choice:
        case contracts.OrdinaryDispatchChoice():
            preparation_choice = dispatch_operations.OrdinaryDispatch()
        case contracts.ReviewedDispatchChoice():
            preparation_choice = dispatch_operations.ReviewedDispatch(choice.brief_review, ReviewId(choice.review_id))
        case contracts.CorrectionDispatchChoice():
            preparation_choice = dispatch_operations.CorrectionDispatch(
                choice.brief_review, ReviewId(choice.review_id), HistoryId(choice.correction_history_id)
            )
        case _ as unreachable:
            assert_never(unreachable)
    store = common.compose_store(durable)
    token.checkpoint()
    selected = actions.select_current_actions(
        store,
        decision_models.Role.PROJECT,
        observed_at=datetime.now(UTC),
        lease_id=None,
        generation=None,
        action_id=ActionId(f"dispatch:{attempt_id}"),
    )
    if isinstance(selected, DecisionFailure):
        return _job_failure(schema, attempt_id, selected.code.value, selected.message, selected.details)
    action = selected[0]
    if not isinstance(action, decision_models.DispatchAction):
        raise AssertionError("Exact dispatch discovery returned a different action.")
    supplied_action = replace(
        action, capability=replace(action.capability, subject_revision=choice.receipt.subject_revision)
    )
    token.checkpoint()
    # Publication has entered its commit section: finish terminal effects before honoring cancellation.
    try:
        publication = dispatch_operations.prepare_dispatch(
            store,
            ArtifactRepository(durable),
            source_checkout,
            supplied_action,
            choice.checkpoint_id,
            choice.environment,
            None if choice.prompt is None else choice.prompt.encode(),
            preparation_choice,
        )
    except ArtifactAcceptanceAfterPublicationError as error:
        return _job_publication_exception(schema, attempt_id, error)
    if isinstance(publication, dispatch_operations.DispatchFailure):
        return _job_failure(schema, attempt_id, publication.code.value, publication.message, publication.details)
    surfaces = _job_publication_surfaces(publication.changed_surfaces)
    content = msgspec.to_builtins(
        contracts.DispatchReady(
            "ready",
            publication.reference,
            _mcp_launch_envelope(
                source_checkout,
                durable.work_root,
                "worker",
                attempt_id,
                publication,
                choice.environment,
                choice.environment.runtime,
                choice.environment.background,
            ),
            bool(surfaces),
            "committed" if surfaces else "unchanged",
            "do-not-retry" if surfaces else "safe-to-repeat",
            surfaces,
            schema,
            attempt_id,
            choice.checkpoint_id,
        )
    )
    assert isinstance(content, dict)
    return execution.OperationResult(
        content, "committed" if surfaces else "unchanged", str(publication.reference.accepted_revision)
    )


def _review_job(
    project_root: str, work_root: str, review: dict[str, JsonValue], token: execution.CancellationToken
) -> execution.OperationResult:
    token.checkpoint()
    schema = "pinboard-mcp-review-job-result/v1"
    try:
        request = msgspec.convert(
            {"project_root": project_root, "work_root": work_root, "review": review},
            type=contracts.ReviewJobRequest,
            strict=True,
        )
        source_checkout = resolve_source_checkout_root(Path(request.project_root))
        durable = common._require_initialized_durable(
            resolve_shared_repository_root(source_checkout), Path(request.work_root)
        )
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return common._read_failure(schema, "REVIEW_JOB_INVALID", f"Cannot decode review-job request: {error}", None)
    choice = request.review
    match choice:
        case contracts.InitialReviewChoice():
            checkpoint_history_id, correction_history_id = None, None
        case contracts.PackageInitialReviewChoice() | contracts.PackageInitialRecoveryReviewChoice():
            checkpoint_history_id, correction_history_id = HistoryId(choice.checkpoint_history_id), None
        case contracts.CorrectionReviewChoice():
            checkpoint_history_id, correction_history_id = None, HistoryId(choice.correction_history_id)
        case contracts.PackageCorrectionReviewChoice() | contracts.PackageCorrectionRecoveryReviewChoice():
            checkpoint_history_id, correction_history_id = (
                HistoryId(choice.checkpoint_history_id),
                HistoryId(choice.correction_history_id),
            )
        case _ as unreachable:
            assert_never(unreachable)
    store = common.compose_store(durable)
    token.checkpoint()
    # Cancellation cannot turn an entered publication into an unchanged/replayable result.
    try:
        if isinstance(
            choice, (contracts.PackageInitialRecoveryReviewChoice, contracts.PackageCorrectionRecoveryReviewChoice)
        ):
            assert checkpoint_history_id is not None
            prepared = checkpoint_compatibility.prepare_recovered_review_job(
                durable.work_root,
                store,
                ArtifactRepository(durable),
                AttemptId(choice.attempt_id),
                choice.candidate_revision,
                checkpoint_history_id,
                correction_history_id,
                choice.candidate_patch,
            )
        else:
            prepared = review_operations.prepare_review_job(
                durable.work_root,
                store,
                ArtifactRepository(durable),
                AttemptId(choice.attempt_id),
                choice.candidate_revision,
                checkpoint_history_id,
                correction_history_id,
            )
    except ArtifactAcceptanceAfterPublicationError as error:
        return _job_publication_exception(schema, choice.attempt_id, error)
    if isinstance(prepared, DecisionFailure):
        if isinstance(prepared, review_operations.CompatibilityCandidateRequired):
            return _review_candidate_required(
                source_checkout, durable.work_root, choice, prepared, correction_history_id
            )
        return _job_failure(schema, choice.attempt_id, prepared.code.value, prepared.message, prepared.details)
    publication = prepared.published_prompt
    recovery = common._candidate_recovery_view(durable, prepared.candidate_evidence)
    reference = prepared.brief_reference
    brief = prepared.brief
    surfaces = _job_publication_surfaces(publication.changed_surfaces)
    content = msgspec.to_builtins(
        contracts.ReviewJobReady(
            "ready",
            publication.reference,
            _mcp_launch_envelope(
                source_checkout,
                durable.work_root,
                "reviewer",
                choice.attempt_id,
                publication,
                None,
                choice.runtime,
                choice.background,
            ),
            bool(surfaces),
            "committed" if surfaces else "unchanged",
            "do-not-retry" if surfaces else "safe-to-repeat",
            surfaces,
            schema,
            choice.attempt_id,
            choice.candidate_revision,
            recovery,
            brief.owner_task_id,
            str(durable.work_root / reference.selector),
            reference.content_sha256,
            brief.accepted_scope.revision,
            brief.accepted_scope.digest,
            str(prepared.result_path),
            prepared.result_sha256,
            prepared.prior_checkpoint_package,
            prepared.review_round,
            prepared.return_contract,
        )
    )
    assert isinstance(content, dict)
    return execution.OperationResult(
        content, "committed" if surfaces else "unchanged", str(publication.reference.accepted_revision)
    )


def _review_candidate_required(
    source_checkout: Path,
    work_root: Path,
    choice: contracts.ReviewChoice,
    required: review_operations.CompatibilityCandidateRequired,
    correction_history_id: HistoryId | None,
) -> execution.OperationResult:
    historical = required.package.candidate
    if not historical.startswith("working-tree-sha256:") or len(historical.removeprefix("working-tree-sha256:")) != 64:
        return _job_failure(
            "pinboard-mcp-review-job-result/v1",
            choice.attempt_id,
            required.code.value,
            "Selected historical candidate has no recoverable patch identity.",
            required.details,
        )
    if correction_history_id is None:
        template = contracts.InitialRecoveryTemplate(
            choice.attempt_id,
            choice.candidate_revision,
            choice.runtime,
            choice.background,
            int(required.checkpoint_history_id),
            None,
        )
    else:
        template = contracts.CorrectionRecoveryTemplate(
            choice.attempt_id,
            choice.candidate_revision,
            choice.runtime,
            choice.background,
            int(required.checkpoint_history_id),
            int(correction_history_id),
            None,
        )
    failure = _job_failure(
        "pinboard-mcp-review-job-result/v1",
        choice.attempt_id,
        required.code.value,
        "Selected retained-v1 patch bytes are missing; supply exact historical patch bytes in the native recovery request.",
        required.details,
    )
    failure.content["recovery"] = msgspec.to_builtins(
        contracts.ReviewRecoveryInvocation(
            tool_names.REVIEW_JOB_TOOL,
            contracts.ReviewRecoveryArguments(str(source_checkout), str(work_root), template),
            ("review.candidate_patch",),
            historical,
            historical.removeprefix("working-tree-sha256:"),
        )
    )
    return failure


def _observe_candidate(
    project_root: str,
    work_root: str,
    attempt_id: str,
    token: execution.CancellationToken,
) -> execution.OperationResult:
    """Read one checkout and selected attempt context; never prepare, freeze or submit."""

    token.checkpoint()
    schema = "pinboard-mcp-candidate-observation-result/v1"
    try:
        request = msgspec.convert(
            {"project_root": project_root, "work_root": work_root, "attempt_id": attempt_id},
            type=contracts.CandidateObserveRequest,
            strict=True,
        )
    except (msgspec.ValidationError, ValueError) as error:
        return common._read_failure(
            schema, "CANDIDATE_OBSERVATION_INVALID", f"Cannot decode candidate observation: {error}", None
        )
    try:
        source_checkout = resolve_source_checkout_root(Path(request.project_root))
        durable = common._require_initialized_durable(
            resolve_shared_repository_root(source_checkout), Path(request.work_root)
        )
    except (RootError, OSError, ValueError) as error:
        return common._read_failure(
            schema, "CANDIDATE_GIT_UNAVAILABLE", f"Cannot resolve candidate checkout: {error}", None
        )
    store = common.compose_store(durable)
    context = queries.select_attempt_context(store, AttemptId(request.attempt_id))
    if isinstance(context, DecisionFailure) or not isinstance(context, query_models.NonterminalAttemptContextFacts):
        return common._read_failure(
            schema, "CANDIDATE_CONTEXT_UNAVAILABLE", "Observation requires one current nonterminal attempt.", None
        )
    token.checkpoint()
    try:
        branch, _ = git_root.observe_checkout_identity(source_checkout)
        if branch != context.branch:
            return common._read_failure(
                schema,
                "CANDIDATE_BRANCH_MISMATCH",
                "Candidate observation requires the attempt's exact branch.",
                FailureDetails(
                    observed=(FailureFact("branch", branch),),
                    mismatches=(FailureMismatch("branch", context.branch, branch),),
                    retry=RetryDisposition.CORRECT_INPUT,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
            )
        candidate = git_root.read_working_tree_candidate(source_checkout)
        omitted = git_root.read_untracked_paths(source_checkout)
    except (RootError, OSError, ValueError) as error:
        return common._read_failure(
            schema, "CANDIDATE_GIT_UNAVAILABLE", f"Cannot read candidate checkout: {error}", None
        )
    content = msgspec.to_builtins(
        contracts.CandidateObserved(
            schema,
            "observed",
            request.attempt_id,
            candidate.identity,
            str(source_checkout),
            branch,
            context.base_revision,
            candidate.preimage_revision,
            hashlib.sha256(candidate.diff).hexdigest(),
            len(candidate.diff),
            omitted,
            False,
            "unchanged",
            "safe-to-repeat",
            (),
        )
    )
    return execution.OperationResult(content, "ok", None)


def _candidate_restore(
    project_root: str,
    work_root: str,
    attempt_id: str,
    candidate: str,
    token: execution.CancellationToken,
) -> execution.OperationResult:
    token.checkpoint()
    schema = "pinboard-mcp-candidate-restore-result/v1"
    try:
        request = msgspec.convert(
            {"project_root": project_root, "work_root": work_root, "attempt_id": attempt_id, "candidate": candidate},
            type=contracts.CandidateRestoreRequest,
            strict=True,
        )
        source_checkout = resolve_source_checkout_root(Path(request.project_root))
        durable = common._require_initialized_durable(
            resolve_shared_repository_root(source_checkout), Path(request.work_root)
        )
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return common._read_failure(
            schema, "CANDIDATE_RESTORE_INVALID", f"Cannot decode candidate restore request: {error}", None
        )
    store = common.compose_store(durable)
    token.checkpoint()
    # Entered source effects finish before cancellation can discard their terminal outcome.
    restored = candidate_evidence.restore_candidate(
        source_checkout, durable.work_root, store, AttemptId(request.attempt_id), request.candidate
    )
    if isinstance(restored, DecisionFailure):
        details = common._details_json(restored.details)
        committed = restored.details is not None and restored.details.effect == EffectDisposition.COMMITTED
        return execution.OperationResult(
            {
                "schema": schema,
                "status": "failed-after-mutation" if committed else "rejected",
                "attempt_id": request.attempt_id,
                "code": restored.code.value,
                "message": restored.message,
                "state_changed": committed,
                **details,
            },
            "committed-failure" if committed else "rejected",
            None,
        )
    content = msgspec.to_builtins(
        contracts.CandidateRestoreReady(
            schema,
            "restored",
            request.attempt_id,
            restored.candidate,
            str(source_checkout),
            restored.changed,
            "committed" if restored.changed else "unchanged",
            "do-not-retry" if restored.changed else "safe-to-repeat",
            ("source-checkout",) if restored.changed else (),
        )
    )
    assert isinstance(content, dict)
    return execution.OperationResult(content, "committed" if restored.changed else "unchanged", None)
