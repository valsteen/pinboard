"""Installed work-inspection composition and presentation.

Most functions only read an explicitly selected SQLite work root and write
command output. Review-job additionally publishes and accepts its immutable
prompt without changing lifecycle or authority.
"""

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard.adapters import review_operations
from pinboard.adapters.files.artifacts import ArtifactRepository, read_reference
from pinboard.adapters.files.errors import ArtifactError
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.application import (
    action_models,
    ports,
    queries,
    query_models,
    work_brief_models,
)
from pinboard.application import (
    actions as action_queries,
)
from pinboard.application.work_briefs import decode_canonical_work_brief
from pinboard.cli import (
    agent_launch,
    candidate_recovery,
    checkpoint_compatibility,
    cli_commands,
    errors,
    work_inspection_models,
)
from pinboard.cli.cli_output import write_json
from pinboard.domain import decision_models, work_models
from pinboard.domain import errors as domain_errors
from pinboard.domain.identifiers import ActionId, ArtifactRefId, AttemptId, HistoryId, LeaseId, TaskId
from pinboard.domain.ledger import LedgerSnapshot


def _read_attempt_brief(
    roots: cli_commands.ResolvedRoots,
    context: query_models.NonterminalAttemptContextFacts,
) -> errors.CommandResult[work_brief_models.WorkBrief]:
    reference = context.brief_reference
    brief = decode_canonical_work_brief(read_reference(roots.work, reference))
    if isinstance(brief, work_brief_models.WorkBriefFailure):
        return errors.CommandFailure(domain_errors.DecisionFailureCode.ACTION_NOT_AVAILABLE, str(brief), None)
    if (failure := queries.validate_attempt_brief_identity(context, brief)) is not None:
        return errors.CommandFailure(failure.code, failure.message, failure.details)
    return brief


@dataclass(frozen=True, slots=True)
class _TerminalAttemptInspection:
    context: query_models.TerminalAttemptContextFacts
    continuation: query_models.AttemptContinuation


@dataclass(frozen=True, slots=True)
class _NonterminalAttemptInspection:
    context: query_models.NonterminalAttemptContextFacts
    brief: work_brief_models.WorkBrief
    continuation: query_models.AttemptContinuation


type _AttemptInspection = _TerminalAttemptInspection | _NonterminalAttemptInspection


def _inspect_selected_attempt(
    roots: cli_commands.ResolvedRoots,
    context: query_models.AttemptContextFacts,
) -> errors.CommandResult[_AttemptInspection]:
    match context:
        case query_models.TerminalAttemptContextFacts():
            continuation = queries.project_attempt_continuation(context, None)
            if isinstance(continuation, domain_errors.DecisionFailure):
                return errors.CommandFailure(continuation.code, continuation.message, continuation.details)
            return _TerminalAttemptInspection(context, continuation)
        case query_models.NonterminalAttemptContextFacts():
            brief = _read_attempt_brief(roots, context)
            if isinstance(brief, errors.CommandFailure):
                return brief
            continuation = queries.project_attempt_continuation(context, TaskId(brief.owner_task_id))
            if isinstance(continuation, domain_errors.DecisionFailure):
                return errors.CommandFailure(continuation.code, continuation.message, continuation.details)
            return _NonterminalAttemptInspection(context, brief, continuation)
        case _ as unreachable:
            assert_never(unreachable)


def _inspect_attempt(
    roots: cli_commands.ResolvedRoots,
    reader: ports.AttemptContextReader,
    attempt_id: AttemptId,
) -> errors.CommandResult[_AttemptInspection]:
    context = queries.select_attempt_context(reader, attempt_id)
    if isinstance(context, domain_errors.DecisionFailure):
        return errors.CommandFailure(context.code, context.message, context.details)
    return _inspect_selected_attempt(roots, context)


def read_attempt_continuation(
    roots: cli_commands.ResolvedRoots,
    reader: ports.AttemptContextReader,
    attempt_id: AttemptId,
) -> errors.CommandResult[query_models.AttemptContinuation]:
    """Resolve accepted owner evidence and derive continuation from one exact read."""
    selected = _inspect_attempt(roots, reader, attempt_id)
    return selected if isinstance(selected, errors.CommandFailure) else selected.continuation


def show_attempt(
    roots: cli_commands.ResolvedRoots, store: ports.WorkStore, command: cli_commands.AttemptInspectCommand
) -> errors.CommandResult[int]:
    selected = _inspect_attempt(roots, store, command.attempt_id)
    if isinstance(selected, errors.CommandFailure):
        return selected
    recovery = read_candidate_recovery(roots, store, selected.context, command.attempt_id)
    if isinstance(recovery, errors.CommandFailure):
        return recovery
    # The same strict record is useful in both interactive and machine inspection.
    write_json(work_inspection_models.AttemptView(selected.continuation, recovery))
    return 0


def read_candidate_recovery(
    roots: cli_commands.ResolvedRoots,
    store: ports.WorkStore,
    context: query_models.AttemptContextFacts,
    attempt_id: AttemptId,
) -> errors.CommandResult[work_inspection_models.CandidateRecoverySelection]:
    if not isinstance(context, query_models.NonterminalAttemptContextFacts) or context.candidate_revision is None:
        return work_inspection_models.NoCandidateRecovery()
    candidate = context.candidate_revision
    if (snapshot_context := store.read_candidate_snapshot_context(attempt_id)) is not None:
        evidence = candidate_recovery.read_candidate_evidence_from_context(roots.work, snapshot_context, candidate)
        if isinstance(evidence, errors.CommandFailure):
            return evidence
        return candidate_recovery.recovery_view(roots, evidence)
    return work_inspection_models.NoCandidateRecovery()


def verify_artifact_reference(
    durable: DurableRoots,
    store: ports.WorkStore,
    command: cli_commands.ArtifactVerifyCommand,
) -> errors.CommandResult[int]:
    reference = store.read_artifact_reference_by_id(ArtifactRefId(command.artifact_ref_id))
    if reference is None:
        return errors.CommandFailure(
            errors.CommandErrorCode.ARTIFACT_REFERENCE_MISMATCH,
            "The accepted artifact reference does not exist.",
            domain_errors.FailureDetails(
                observed=(domain_errors.FailureFact("artifact_ref_id", command.artifact_ref_id),),
                mismatches=(domain_errors.FailureMismatch("accepted_artifact_reference", "present", "absent"),),
                retry=domain_errors.RetryDisposition.CORRECT_INPUT,
                effect=domain_errors.EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    mismatches = tuple(
        domain_errors.FailureMismatch(field, expected, observed)
        for field, expected, observed in (
            ("selector", reference.selector, command.selector),
            ("sha256", reference.content_sha256, command.sha256),
            ("size_bytes", reference.size_bytes, command.size_bytes),
        )
        if expected != observed
    )
    observations = (
        domain_errors.FailureFact("artifact_ref_id", command.artifact_ref_id),
        domain_errors.FailureFact("selector", reference.selector),
        domain_errors.FailureFact("sha256", reference.content_sha256),
        domain_errors.FailureFact("size_bytes", reference.size_bytes),
    )
    if mismatches:
        return errors.CommandFailure(
            errors.CommandErrorCode.ARTIFACT_REFERENCE_MISMATCH,
            "The supplied prompt reference facts do not match the accepted artifact reference.",
            domain_errors.FailureDetails(
                observed=observations,
                mismatches=mismatches,
                retry=domain_errors.RetryDisposition.CORRECT_INPUT,
                effect=domain_errors.EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    try:
        read_reference(durable.work_root, reference)
    except ArtifactError:
        return errors.CommandFailure(
            errors.CommandErrorCode.ARTIFACT_REFERENCE_MISMATCH,
            "The immutable artifact bytes do not match the accepted reference.",
            domain_errors.FailureDetails(
                observed=observations,
                mismatches=(
                    domain_errors.FailureMismatch(
                        "artifact_bytes",
                        "match accepted selector, size, and SHA-256",
                        "unreadable or mismatched",
                    ),
                ),
                retry=domain_errors.RetryDisposition.DO_NOT_RETRY,
                effect=domain_errors.EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    view = work_inspection_models.VerifiedArtifactReferenceView(
        "pinboard-verified-artifact-reference/v1",
        command.artifact_ref_id,
        reference.selector,
        reference.content_sha256,
        reference.size_bytes,
        reference.accepted_revision,
        True,
    )
    if command.json:
        write_json(view)
    else:
        print(
            f"OK ARTIFACT_REFERENCE_VERIFIED artifact_ref_id={view.artifact_ref_id} "
            f"selector={view.selector} sha256={view.sha256} size_bytes={view.size_bytes}"
        )
    return 0


def _review_job_history_ids(
    command: cli_commands.ReviewJobCommand,
) -> tuple[HistoryId | None, HistoryId | None]:
    match command:
        case cli_commands.InitialReviewJobCommand():
            return None, None
        case (
            cli_commands.PackageInitialReviewJobCommand(checkpoint_history_id=checkpoint_history_id)
            | cli_commands.CompatibilityPackageInitialRecoveryReviewJobCommand(
                checkpoint_history_id=checkpoint_history_id
            )
        ):
            return HistoryId(checkpoint_history_id), None
        case cli_commands.CorrectionReviewJobCommand(correction_history_id=correction_history_id):
            return None, HistoryId(correction_history_id)
        case (
            cli_commands.PackageCorrectionReviewJobCommand(
                checkpoint_history_id=checkpoint_history_id,
                correction_history_id=correction_history_id,
            )
            | cli_commands.CompatibilityPackageCorrectionRecoveryReviewJobCommand(
                checkpoint_history_id=checkpoint_history_id,
                correction_history_id=correction_history_id,
            )
        ):
            return HistoryId(checkpoint_history_id), HistoryId(correction_history_id)
        case _ as unreachable:
            assert_never(unreachable)


def show_review_job(
    roots: cli_commands.ResolvedRoots,
    durable: DurableRoots,
    store: ports.WorkStore,
    command: cli_commands.ReviewJobCommand,
) -> errors.CommandResult[int]:
    checkpoint_history_id, correction_history_id = _review_job_history_ids(command)
    recovered = None
    if isinstance(
        command,
        (
            cli_commands.CompatibilityPackageInitialRecoveryReviewJobCommand,
            cli_commands.CompatibilityPackageCorrectionRecoveryReviewJobCommand,
        ),
    ):
        facts = queries.select_review_job_context(
            store, command.attempt_id, checkpoint_history_id, correction_history_id
        )
        if isinstance(facts, domain_errors.DecisionFailure):
            return errors.CommandFailure(facts.code, facts.message, facts.details)
        recovered = checkpoint_compatibility.recover_checkpoint_candidate(
            roots, durable, store, command, facts, checkpoint_history_id
        )
        if isinstance(recovered, errors.CommandFailure):
            return recovered
    try:
        prepared = review_operations.prepare_review_job(
            roots.work,
            store,
            ArtifactRepository(durable),
            command.attempt_id,
            command.candidate_revision,
            checkpoint_history_id,
            correction_history_id,
        )
    except (ArtifactError, ports.WorkStoreError, domain_errors.ArtifactAcceptanceAfterPublicationError) as error:
        checkpoint_compatibility.raise_after_recovery_exception(error, recovered)
    if isinstance(prepared, domain_errors.DecisionFailure):
        if isinstance(prepared, review_operations.CompatibilityCandidateRequired):
            failure = checkpoint_compatibility.require_candidate_reference(
                roots, command, prepared.package, prepared.candidate_reference, prepared.checkpoint_history_id
            )
            if failure is not None:
                return checkpoint_compatibility.after_recovery_failure(failure, recovered)
        return checkpoint_compatibility.after_recovery_failure(
            errors.CommandFailure(prepared.code, prepared.message, prepared.details), recovered
        )
    publication = prepared.published_prompt
    brief = prepared.brief
    reference = prepared.brief_reference
    job = work_inspection_models.ReviewJobView(
        "pinboard-review-job/v4",
        command.attempt_id,
        command.candidate_revision,
        candidate_recovery.recovery_view(roots, prepared.candidate_evidence),
        brief.owner_task_id,
        str(roots.work / reference.selector),
        reference.content_sha256,
        brief.accepted_scope.revision,
        brief.accepted_scope.digest,
        str(prepared.result_path),
        prepared.result_sha256,
        prepared.prior_checkpoint_package,
        prepared.review_round,
        publication.reference,
        agent_launch.launch_envelope(
            roots.source_checkout, roots.work, "reviewer", str(command.attempt_id), publication, None
        ),
        tuple(
            surface.value
            for surface in dict.fromkeys(
                (*checkpoint_compatibility.recovery_surfaces(recovered), *publication.changed_surfaces)
            )
        ),
        prepared.return_contract,
    )
    if command.json:
        write_json(job)
    else:
        print(job.native_launch.message)
    return 0


def describe_input_contract(
    kind: decision_models.ActionKind,
) -> action_models.InputContractView:
    semantics = decision_models.action_semantics(kind)
    return action_models.InputContractView(
        kind,
        action_queries.project_action_semantics(semantics),
        action_models.action_payload_schema(kind),
    )


def project_parallel_preview(
    preview: query_models.ParallelPreview,
) -> work_inspection_models.ParallelPreviewView:
    launchable: list[work_inspection_models.ParallelItemView] = []
    excluded: list[work_inspection_models.ParallelItemView] = []
    for item in preview.items:
        match item:
            case query_models.LaunchableParallelItem():
                launchable.append(
                    work_inspection_models.ParallelItemView(
                        item.item_id,
                        item.label,
                        item.state.value,
                        item.attempt_id,
                        "launchable",
                        (),
                    )
                )
            case query_models.ExcludedParallelItem(reasons=reasons):
                excluded.append(
                    work_inspection_models.ParallelItemView(
                        item.item_id,
                        item.label,
                        item.state.value,
                        item.attempt_id,
                        "excluded",
                        reasons,
                    )
                )
            case _ as unreachable:
                assert_never(unreachable)
    return work_inspection_models.ParallelPreviewView(
        preview.schema,
        preview.revision,
        preview.selection.value,
        preview.safe,
        tuple(launchable),
        tuple(excluded),
    )


def compose_status(
    facts: query_models.ProjectStatusFacts,
    work: Path,
    source_checkout: Path,
    shared_repository: Path,
) -> work_inspection_models.StatusView:
    counts = dict(facts.counts)
    return work_inspection_models.StatusView(
        stored_state_opened=True,
        source_checkout_root=str(source_checkout),
        shared_repository_root=str(shared_repository),
        work_root=str(work),
        revision=str(facts.project_revision),
        active_attempts=tuple(str(value) for value in facts.active_attempts),
        counts=counts,
        intake_item_count=counts.get(work_models.WorkState.INTAKE.value, 0),
        authority="sqlite-v6",
    )


def show_status(roots: cli_commands.ResolvedRoots, store: ports.WorkStore, command: cli_commands.StatusCommand) -> int:
    status_projection = compose_status(
        store.read_project_status(), roots.work, roots.source_checkout, roots.shared_repository
    )
    if command.json:
        write_json(status_projection)
    else:
        print(f"OK WORK_STATE_VALID revision={status_projection.revision}")
        print(f"active_attempts={','.join(status_projection.active_attempts) or 'none'}")
        print(f"intake_items={status_projection.intake_item_count}")
    return 0


def show_overview(store: ports.WorkStore, command: cli_commands.OverviewCommand) -> int:
    operation_time = datetime.now(UTC)
    overview_projection = queries.project_current_overview(store.read_project_overview(operation_time), operation_time)
    if command.json:
        write_json(overview_projection)
        return 0
    print(f"OK WORK_OVERVIEW revision={overview_projection.revision} authority={overview_projection.authority}")
    next_unstarted = overview_projection.next_unstarted
    if next_unstarted is None:
        print("next_unstarted=none")
    else:
        print(
            f"next_unstarted={next_unstarted.item_id} "
            f"live_dependencies={','.join(next_unstarted.live_dependencies) or 'none'}"
        )
    if not overview_projection.items:
        print("live_work=none")
    for item in overview_projection.items:
        position = item.position if item.position is not None else "none"
        attempt = f" attempt={item.attempt_id}" if item.attempt_id is not None else ""
        preparation = (
            " preparation=none"
            if item.preparation is None
            else (
                f" preparation={item.preparation.status.value}"
                f" preparer={item.preparation.task_id}@{item.preparation.host_id}"
                f" preparation_generation={item.preparation.generation}"
                f" preparation_expires_at={item.preparation.expires_at}"
            )
        )
        next_action = item.next_action or "none"
        replacement = (
            " replacement=none"
            if item.planned_replacement is None
            else (
                f" replacement={item.planned_replacement.replacement_item_id}"
                f" replacement_revision={item.planned_replacement.relation_revision}"
                f" replacement_cost={item.planned_replacement.replacement_cost!r}"
                f" temporarily_retained={str(item.planned_replacement.temporarily_retained).lower()}"
            )
        )
        print(
            f"{position}\t{item.item_id}\t{item.state.value}\teligible={str(item.eligible).lower()}"
            f"\tnext={next_action}{attempt}{preparation}{replacement}\t{item.label}"
        )
        print(f"  effect={item.effect} unlock={item.unlock}")
    print(
        f"intake_items={sum(1 for item in overview_projection.items if item.state == work_models.WorkState.INTAKE)} "
        f"immediate_options={len(overview_projection.immediate_options)}"
    )
    return 0


def show_item_status(
    store: ports.WorkStore,
    command: cli_commands.ItemStatusCommand,
) -> errors.CommandResult[int]:
    operation_time = datetime.now(UTC)
    item_projection = queries.project_item_status(store, command.item_id, operation_time)
    if isinstance(item_projection, domain_errors.DecisionFailure):
        return errors.CommandFailure(item_projection.code, item_projection.message, item_projection.details)
    if command.json:
        write_json(item_projection)
        return 0
    print(
        f"OK ITEM_STATUS item={item_projection.item_id} state={item_projection.state.value} "
        f"revision={item_projection.revision} authority={item_projection.authority}"
    )
    print(
        f"label={item_projection.label} "
        f"timing={item_projection.timing.value if item_projection.timing is not None else 'none'} "
        f"queue_position={item_projection.queue_position if item_projection.queue_position is not None else 'none'} "
        f"next_action={item_projection.next_action or 'none'}"
    )
    print(
        f"outcome_evidence={item_projection.outcome_evidence or 'none'} source={item_projection.source or 'none'} notes={item_projection.notes or 'none'}"
    )
    if item_projection.preparation is None:
        print("preparation=none")
    else:
        print(
            f"preparation={item_projection.preparation.status.value} "
            f"preparer={item_projection.preparation.task_id}@{item_projection.preparation.host_id} "
            f"lease_id={item_projection.preparation.lease_id} generation={item_projection.preparation.generation} "
            f"expires_at={item_projection.preparation.expires_at} "
            f"definition_revision={item_projection.preparation.definition_revision} "
            f"definition_digest={item_projection.preparation.definition_digest}"
        )
    if not item_projection.attempts:
        print("attempts=none")
    for attempt in item_projection.attempts:
        print(
            f"attempt={attempt.attempt_id} state={attempt.state.value} candidate={attempt.candidate_revision or 'none'}"
        )
    return 0


def show_item_definition(
    store: ports.WorkStore,
    command: cli_commands.ItemDefinitionCommand,
) -> errors.CommandResult[int]:
    definition_projection = queries.select_item_definition(store, command.item_id)
    if isinstance(definition_projection, domain_errors.DecisionFailure):
        return errors.CommandFailure(
            definition_projection.code, definition_projection.message, definition_projection.details
        )
    if command.json:
        write_json(definition_projection)
    else:
        print(
            f"OK ITEM_DEFINITION item={definition_projection.item_id} "
            f"item_subject_revision={definition_projection.item_subject_revision} "
            f"definition_revision={definition_projection.definition_revision} "
            f"definition_digest={definition_projection.definition_digest} "
            f"project_revision={definition_projection.project_revision}"
        )
        print(f"title={definition_projection.definition.title}")
    return 0


def show_item_definition_history(
    store: ports.WorkStore,
    command: cli_commands.ItemDefinitionHistoryCommand,
) -> errors.CommandResult[int]:
    history_projection = queries.select_item_definition_history(
        store,
        command.item_id,
        limit=command.limit,
        before_revision=command.before_revision,
    )
    if isinstance(history_projection, domain_errors.DecisionFailure):
        return errors.CommandFailure(history_projection.code, history_projection.message, history_projection.details)
    if command.json:
        write_json(history_projection)
    else:
        print(
            f"OK ITEM_DEFINITION_HISTORY item={history_projection.item_id} "
            f"revisions={len(history_projection.revisions)} project_revision={history_projection.project_revision}"
        )
        for revision in history_projection.revisions:
            print(
                f"revision={revision.revision} digest={revision.digest} "
                f"source_task={revision.source_task} timestamp={revision.timestamp}"
            )
    return 0


def _read_action_snapshot(
    store: ports.WorkStore,
    role: decision_models.Role,
    lease_id: LeaseId | None,
    generation: int | None,
    action_id: ActionId | None,
    operation_time: datetime,
) -> LedgerSnapshot | errors.CommandFailure:
    if role == decision_models.Role.OBSERVER or (
        role in {decision_models.Role.WORKER, decision_models.Role.PREPARER}
        and (lease_id is None or generation is None)
    ):
        return LedgerSnapshot("", ())
    if action_id is not None:
        scope = action_queries.action_identity_scope(action_id)
        if scope is None:
            return errors.CommandFailure(
                domain_errors.DecisionFailureCode.ACTION_NOT_AVAILABLE,
                f"Action '{action_id}' is not currently legal for this role and lease.",
                None,
            )
        return store.read_decision_facts(scope, operation_time).snapshot
    if role == decision_models.Role.PROJECT:
        return store.read_current_action_snapshot(operation_time)
    assert lease_id is not None and generation is not None
    return store.read_leased_action_snapshot(role, lease_id, generation, operation_time)


def _select_requested_actions(
    available_actions: tuple[decision_models.Action, ...],
    exact_action_id: ActionId | None,
) -> tuple[decision_models.Action, ...] | errors.CommandFailure:
    if exact_action_id is None:
        return available_actions
    selected = tuple(action for action in available_actions if decision_models.action_id(action) == exact_action_id)
    if selected:
        return selected
    return errors.CommandFailure(
        domain_errors.DecisionFailureCode.ACTION_NOT_AVAILABLE,
        f"Action '{exact_action_id}' is not currently legal for this role and lease.",
        None,
    )


def _completion_action_view(
    store: ports.WorkStore,
    action: decision_models.CompleteAction,
    projected: work_inspection_models.ActionView,
) -> errors.CommandResult[work_inspection_models.ActionView]:
    contract = action_queries.completion_input_contract(store, action, projected.semantics)
    if isinstance(contract, domain_errors.DecisionFailure):
        code = errors.CommandErrorCode.ACTION_LIFECYCLE_UNAVAILABLE if contract.details is not None else contract.code
        return errors.CommandFailure(code, contract.message, contract.details)
    return msgspec.structs.replace(projected, input_contract=contract)


def _discovered_action_view(
    store: ports.WorkStore,
    action: decision_models.Action,
    focused: bool,
) -> errors.CommandResult[work_inspection_models.ActionView | work_inspection_models.CompletionInspectionView]:
    if isinstance(action, decision_models.CompleteAction) and not focused:
        return work_inspection_models.CompletionInspectionView(
            decision_models.action_id(action),
            "inspect-completion",
            action.capability.subject,
            "Inspect the current completion evidence and recovery path",
            "advisory",
            ("actions", "--role", "project", "--action-id", decision_models.action_id(action), "--json"),
        )
    projected = action_queries.project_action(action, include_input_contract=focused)
    if isinstance(action, decision_models.CompleteAction):
        return _completion_action_view(store, action, projected)
    return projected


def _present_actions(
    action_views: tuple[work_inspection_models.ActionView | work_inspection_models.CompletionInspectionView, ...],
    as_json: bool,
) -> None:
    if as_json:
        write_json(work_inspection_models.ActionsView(action_views))
    elif not action_views:
        print("OK NO_ACTIONS_AVAILABLE")
    else:
        for view in action_views:
            if isinstance(view, work_inspection_models.CompletionInspectionView):
                print(" ".join(view.inspection_arguments))
            else:
                print(f"{view.action_id}\t{view.label}")


def show_actions(
    store: ports.WorkStore,
    command: cli_commands.ActionsCommand | cli_commands.LeasedActionsCommand,
) -> errors.CommandResult[int]:
    match command:
        case cli_commands.ActionsCommand():
            lease_id = None
            generation = None
        case cli_commands.LeasedActionsCommand(lease_id=lease_id, generation=generation):
            pass
        case _ as unreachable:
            assert_never(unreachable)
    operation_time = datetime.now(UTC)
    current_snapshot = _read_action_snapshot(
        store, command.role, lease_id, generation, command.action_id, operation_time
    )
    if isinstance(current_snapshot, errors.CommandFailure):
        return current_snapshot
    available_actions = action_queries.discover_current_actions(
        current_snapshot,
        command.role,
        lease_id=lease_id,
        generation=generation,
    )
    if isinstance(available_actions, domain_errors.DecisionFailure):
        return errors.CommandFailure(available_actions.code, available_actions.message, available_actions.details)
    exact_action_id = command.action_id
    selected_actions = _select_requested_actions(available_actions, exact_action_id)
    if isinstance(selected_actions, errors.CommandFailure):
        return selected_actions
    available_actions = selected_actions
    action_views: list[work_inspection_models.ActionView | work_inspection_models.CompletionInspectionView] = []
    for action in available_actions:
        projected_action = _discovered_action_view(store, action, exact_action_id is not None)
        if isinstance(projected_action, errors.CommandFailure):
            return projected_action
        action_views.append(projected_action)
    _present_actions(tuple(action_views), command.json)
    return 0


def show_input_contract(
    command: cli_commands.InputContractCommand,
) -> int:
    contract = describe_input_contract(command.action_kind)
    if command.json:
        write_json(contract)
    else:
        print(f"OK INPUT_CONTRACT action_kind={contract.action_kind.value}")
        print(f"use_case={contract.semantics.use_case}")
        print(
            f"effect={contract.semantics.effect.value} "
            f"permitted_roles={','.join(role.value for role in contract.semantics.permitted_roles)} "
            f"subject_kind={contract.semantics.subject_kind.value} "
            f"lifecycle_precondition={contract.semantics.lifecycle_precondition.value}"
        )
        print(f"practical_result={contract.semantics.practical_result}")
        if contract.payload_schema is None:
            print("payload_schema=none")
        else:
            sys.stdout.write(
                msgspec.json.format(msgspec.json.encode(contract.payload_schema), indent=2).decode() + "\n"
            )
    return 0


def _print_parallel_group(title: str, items: tuple[work_inspection_models.ParallelItemView, ...]) -> None:
    print(f"{title}:")
    if not items:
        print("- none")
        return
    for item in items:
        detail = "; ".join(reason.message for reason in item.reasons)
        attempt = f", attempt {item.attempt_id}" if item.attempt_id is not None else ""
        suffix = f" — {detail}" if detail else ""
        print(f"- {item.item_id} ({item.state}{attempt}){suffix}")


def show_parallel_preview(
    store: ports.WorkStore,
    command: cli_commands.ParallelPreviewCommand,
) -> errors.CommandResult[int]:
    operation_time = datetime.now(UTC)
    preview = (
        queries.select_parallel_preview(store, selected=tuple(command.item), now=operation_time)
        if command.item
        else queries.project_current_parallel_preview(
            store.read_current_parallel_snapshot(operation_time), now=operation_time
        )
    )
    if isinstance(preview, query_models.ParallelSelectionInvalid):
        return errors.CommandFailure(errors.CommandErrorCode.PARALLEL_SELECTION_INVALID, preview.message, None)
    view = project_parallel_preview(preview)
    if command.json:
        write_json(view)
    else:
        print(
            f"OK PARALLEL_PREVIEW revision={preview.revision} selection={preview.selection.value} "
            f"safe={'yes' if preview.safe else 'no'}"
        )
        _print_parallel_group("Ready to launch together", view.launchable)
        _print_parallel_group("Not launchable", view.excluded)
    return 0
