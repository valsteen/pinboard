import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import assert_never

import msgspec

from pinboard.adapters import lifecycle_artifacts, lifecycle_operations
from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.errors import ArtifactError, FileIOError
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.transition_input import TransitionInputFailure, parse_item_revision_input, parse_transition_input
from pinboard.application import (
    action_models,
    ports,
)
from pinboard.application.actions import action_identity_scope, discover_current_actions
from pinboard.application.mutation_models import CommittedEffect
from pinboard.cli import (
    action_selection,
    cli_commands,
    transition_models,
    work_inspection,
    work_inspection_models,
    work_views,
)
from pinboard.cli.cli_output import write_json
from pinboard.cli.errors import (
    CommandFailure,
    CommandResult,
    CommittedEffectFailure,
    storage_failure_details,
)
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import (
    ChangedSurface,
    DecisionFailure,
    DecisionFailureCode,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    RetryDisposition,
)
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import ActionId, AttemptId, HostId, TaskId


@dataclass(frozen=True, slots=True)
class _EncodedProjectTransitionRequest:
    action_id: ActionId
    encoded_payload: bytes


@dataclass(frozen=True, slots=True)
class _ValidatedItemRevisionRequest:
    validated_revision: work_models.ReviseItemDefinitionInput


type _ProjectTransitionRequest = _EncodedProjectTransitionRequest | _ValidatedItemRevisionRequest


def _committed_immutable_artifact_failure(
    error: ArtifactError | FileIOError | StorageError,
    artifact_selectors: tuple[str, ...],
    roots: cli_commands.ResolvedRoots,
) -> CommittedEffectFailure:
    artifact_observations = tuple(
        FailureFact("published_artifact_selector", selector) for selector in artifact_selectors
    )
    return CommittedEffectFailure(
        error.code.value,
        str(error),
        storage_failure_details(
            error,
            "transition:project",
            roots,
            EffectDisposition.COMMITTED,
            (ChangedSurface.IMMUTABLE_ARTIFACT,),
            artifact_observations,
        )
        if isinstance(error, StorageError)
        else FailureDetails(
            observed=artifact_observations,
            mismatches=(),
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.COMMITTED,
            changed_surfaces=(ChangedSurface.IMMUTABLE_ARTIFACT,),
            alternatives=(),
        ),
    )


def close(
    roots: cli_commands.ResolvedRoots,
    durable: DurableRoots,
    store: ports.WorkStore,
    command: cli_commands.CloseCommand,
) -> CommandResult[int] | CommittedEffectFailure:
    encoded_transition = msgspec.json.encode(
        {"outcome": command.outcome.value, "reason": command.reason}, order="sorted"
    )
    transition_revision = execute_project_transition(
        roots,
        durable,
        store,
        command.task_id,
        command.host_id,
        _EncodedProjectTransitionRequest(ActionId(f"close:{command.item_id}"), encoded_transition),
    )
    if isinstance(transition_revision, (CommandFailure, CommittedEffectFailure)):
        return transition_revision
    value = transition_models.CloseView(command.item_id, command.outcome.value, command.reason, transition_revision)
    if command.json:
        write_json(value)
    else:
        print(f"OK WORK_ITEM_CLOSED item={value.item_id} outcome={value.outcome} revision={value.revision}")
    return 0


def revise_item(
    roots: cli_commands.ResolvedRoots,
    durable: DurableRoots,
    store: ports.WorkStore,
    command: cli_commands.ItemReviseCommand,
) -> CommandResult[int] | CommittedEffectFailure:
    try:
        revision_bytes = command.file.read_bytes()
    except OSError as error:
        return CommandFailure(DecisionFailureCode.TRANSITION_INPUT_INVALID, f"Cannot read item revision: {error}", None)
    validated_revision = parse_item_revision_input(revision_bytes)
    if isinstance(validated_revision, TransitionInputFailure):
        return CommandFailure(validated_revision.code, validated_revision.message, validated_revision.details)
    definition_digest = work_item_definition_digest(validated_revision.definition)
    if not isinstance(definition_digest, str):
        return CommandFailure(definition_digest.code, definition_digest.message, None)
    transition_revision = execute_project_transition(
        roots,
        durable,
        store,
        command.task_id,
        command.host_id,
        _ValidatedItemRevisionRequest(validated_revision),
    )
    if isinstance(transition_revision, (CommandFailure, CommittedEffectFailure)):
        return transition_revision
    value = transition_models.ItemRevisionView(
        str(validated_revision.item_id),
        validated_revision.expected_revision + 1,
        definition_digest,
        transition_revision,
    )
    if command.json:
        write_json(value)
    else:
        print(
            f"OK ITEM_REVISED item={value.item_id} definition_revision={value.definition_revision} "
            f"definition_digest={value.definition_digest} project_revision={value.project_revision}"
        )
    return 0


def _commit_selected_transition(
    roots: cli_commands.ResolvedRoots,
    store: ports.WorkStore,
    artifacts: ArtifactRepository,
    selected: lifecycle_operations.SelectedTransition,
    operation_time: datetime,
) -> CommandResult[CommittedEffect] | CommittedEffectFailure:
    if isinstance(
        selected.command,
        (
            decision_models.AcceptCheckpointCommand,
            decision_models.CoveredCompleteCommand,
            decision_models.SubmitReviewCommand,
        ),
    ):
        committed = lifecycle_artifacts.execute_artifact_transition(
            roots.source_checkout,
            roots.work,
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
        if committed.storage_error is not None:
            selectors = tuple(
                str(fact.value) for fact in committed.details.observed if fact.field == "published_artifact_selector"
            )
            return _committed_immutable_artifact_failure(committed.storage_error, selectors, roots)
        return CommittedEffectFailure(committed.code, committed.message, committed.details)
    if isinstance(committed, DecisionFailure):
        return CommandFailure(committed.code, committed.message, committed.details)
    if isinstance(committed, lifecycle_artifacts.ArtifactTransitionSuccess):
        return committed.effect
    return committed


def transition(
    roots: cli_commands.ResolvedRoots,
    durable: DurableRoots,
    store: ports.WorkStore,
    cli_command: cli_commands.TransitionCommand,
) -> CommandResult[int] | CommittedEffectFailure:
    supplied_action_receipt = action_selection.parse_action_receipt(cli_command)
    if isinstance(supplied_action_receipt, CommandFailure):
        return supplied_action_receipt
    try:
        encoded_payload = cli_command.payload.read_bytes()
    except OSError as error:
        return CommandFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID, f"Cannot read transition payload: {error}", None
        )
    selected_action = action_selection.select_current_action(store, supplied_action_receipt)
    if isinstance(selected_action, CommandFailure):
        return selected_action
    artifacts = ArtifactRepository(durable)
    decoded_input = parse_transition_input(selected_action, encoded_payload)
    if isinstance(decoded_input, TransitionInputFailure):
        return action_selection.with_completion_reinspection(
            supplied_action_receipt,
            CommandFailure(decoded_input.code, decoded_input.message, decoded_input.details),
        )
    if isinstance(decoded_input, action_models.ActivateInputPayload):
        decoded_command = lifecycle_operations.resolve_activation(
            roots.source_checkout, store, artifacts, selected_action, decoded_input
        )
        if isinstance(decoded_command, DecisionFailure):
            return CommandFailure(decoded_command.code, decoded_command.message, decoded_command.details)
    else:
        decoded_command = decoded_input
    match cli_command:
        case cli_commands.ProjectTransitionCommand(task_id=actor_task_id, host_id=actor_host_id):
            pass
        case cli_commands.AttemptTransitionCommand() | cli_commands.PreparationTransitionCommand():
            actor_task_id = None
            actor_host_id = None
        case _ as unreachable:
            assert_never(unreachable)
    selected = lifecycle_operations.SelectedTransition(
        selected_action,
        decoded_command,
        actor_task_id,
        actor_host_id,
    )
    commit_result = _commit_selected_transition(roots, store, artifacts, selected, datetime.now(UTC))
    if isinstance(commit_result, CommandFailure):
        return action_selection.with_current_alternatives(
            store,
            supplied_action_receipt,
            commit_result,
        )
    if isinstance(commit_result, CommittedEffectFailure):
        return commit_result
    return _present_committed_transition(
        roots,
        durable,
        store,
        selected.action,
        commit_result,
        json=cli_command.json,
    )


def _present_committed_transition(
    roots: cli_commands.ResolvedRoots,
    durable: DurableRoots,
    store: ports.WorkStore,
    selected_action: decision_models.Action,
    committed_mutation: CommittedEffect,
    *,
    json: bool,
) -> int:
    """Refresh replaceable views, reload canonical continuation, then present the committed receipt."""

    subject_kind = decision_models.action_semantics(selected_action.kind).subject_kind
    affected_attempt = (
        AttemptId(selected_action.capability.subject)
        if subject_kind == decision_models.ActionSubjectKind.ATTEMPT
        else None
    )
    view_result = work_views.refresh_effect(durable, store, committed_mutation, datetime.now(UTC))
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    committed_revision = str(committed_mutation.receipt.project_revision)
    if affected_attempt is None:
        affected_attempt = committed_mutation.continuation_attempt_id
    continuation = None
    if affected_attempt is not None:
        continuation = work_inspection.read_attempt_continuation(roots, store, affected_attempt)
        if isinstance(continuation, CommandFailure):
            # The mutation already committed. An unavailable read projection is a warning, not rollback.
            print(f"Transition committed; continuation unavailable: {continuation}", file=sys.stderr)
            continuation = None
    if json:
        write_json(
            work_inspection_models.TransitionView(
                decision_models.action_id(selected_action),
                committed_revision,
                int(committed_mutation.receipt.history_id),
                continuation,
            )
        )
    else:
        print(f"OK TRANSITION_APPLIED {decision_models.action_id(selected_action)} revision={committed_revision}")
        if continuation is not None:
            assert affected_attempt is not None
            context = store.read_attempt_context(affected_attempt)
            if context is None:
                recovery: work_inspection_models.CandidateRecoverySelection = (
                    work_inspection_models.NoCandidateRecovery()
                )
            else:
                selected_recovery = work_inspection.read_candidate_recovery(roots, store, context, affected_attempt)
                if isinstance(selected_recovery, CommandFailure):
                    print(f"Transition committed; candidate recovery unavailable: {selected_recovery}", file=sys.stderr)
                    recovery = work_inspection_models.NoCandidateRecovery()
                else:
                    recovery = selected_recovery
            write_json(work_inspection_models.AttemptView(continuation, recovery))
    return 0


def _requested_project_action_id(request: _ProjectTransitionRequest) -> ActionId:
    match request:
        case _EncodedProjectTransitionRequest(action_id=action_id):
            return action_id
        case _ValidatedItemRevisionRequest(validated_revision=validated_revision):
            return ActionId(f"revise-item:{validated_revision.item_id}")
        case _ as unreachable:
            assert_never(unreachable)


def _decode_selected_project_transition(
    action: decision_models.Action,
    request: _ProjectTransitionRequest,
) -> CommandResult[decision_models.TransitionCommand]:
    match request:
        case _EncodedProjectTransitionRequest(encoded_payload=encoded_payload):
            decoded_input = parse_transition_input(action, encoded_payload)
            if isinstance(decoded_input, TransitionInputFailure):
                return CommandFailure(decoded_input.code, decoded_input.message, decoded_input.details)
            if isinstance(decoded_input, action_models.ActivateInputPayload):
                return CommandFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    "Activation requires preparation authority.",
                    None,
                )
            return decoded_input
        case _ValidatedItemRevisionRequest(validated_revision=validated_revision):
            if not isinstance(action, decision_models.ReviseItemAction):
                return CommandFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    f"Action '{_requested_project_action_id(request)}' is not an item-revision action.",
                    None,
                )
            return decision_models.ReviseItemCommand(action, validated_revision)
        case _ as unreachable:
            assert_never(unreachable)


def execute_project_transition(
    roots: cli_commands.ResolvedRoots,
    durable: DurableRoots,
    store: ports.WorkStore,
    task_id: TaskId,
    host_id: HostId,
    request: _ProjectTransitionRequest,
) -> CommandResult[str] | CommittedEffectFailure:
    """Select and commit one exact current project action."""

    artifacts = ArtifactRepository(durable)
    requested_action_id = _requested_project_action_id(request)
    observed_at = datetime.now(UTC)
    scope = action_identity_scope(requested_action_id)
    if scope is None:
        return CommandFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Action '{requested_action_id}' is not currently legal.",
            None,
        )
    current_actions = discover_current_actions(
        store.read_decision_facts(scope, observed_at).snapshot,
        decision_models.Role.PROJECT,
    )
    if isinstance(current_actions, DecisionFailure):
        return CommandFailure(current_actions.code, current_actions.message, current_actions.details)
    selected_action = next(
        (candidate for candidate in current_actions if decision_models.action_id(candidate) == requested_action_id),
        None,
    )
    if selected_action is None:
        return CommandFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Action '{requested_action_id}' is not currently legal.",
            None,
        )
    decoded_transition = _decode_selected_project_transition(selected_action, request)
    if isinstance(decoded_transition, CommandFailure):
        return decoded_transition
    committed_mutation = _commit_selected_transition(
        roots,
        store,
        artifacts,
        lifecycle_operations.SelectedTransition(selected_action, decoded_transition, task_id, host_id),
        datetime.now(UTC),
    )
    if isinstance(committed_mutation, CommittedEffectFailure):
        return committed_mutation
    if isinstance(committed_mutation, CommandFailure):
        return action_selection.with_current_alternatives(
            store,
            action_selection.ParsedActionReceipt(selected_action, decision_models.Role.PROJECT, 0),
            committed_mutation,
        )
    view_result = work_views.refresh_effect(durable, store, committed_mutation, datetime.now(UTC))
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    return str(committed_mutation.receipt.project_revision)
