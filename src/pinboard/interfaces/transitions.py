import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import assert_never

import msgspec

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.errors import ArtifactError
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.application import ports, query_models
from pinboard.application.actions import discover_current_actions
from pinboard.application.artifacts import (
    CheckpointArtifacts,
    EvidenceArtifactRef,
    NewArtifact,
    ResultArtifactRef,
    WorkBriefIdentity,
)
from pinboard.application.mutation_models import CommittedEffect
from pinboard.application.service import (
    decide_and_commit_checkpoint_acceptance,
    decide_and_commit_transition,
)
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import (
    ChangedSurface,
    DecisionFailure,
    DecisionFailureCode,
    EffectDisposition,
    FailureDetails,
    RetryDisposition,
)
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import ActionId, AttemptId, HostId, ItemId, ProposalId, TaskId
from pinboard.interfaces import (
    action_selection,
    cli_commands,
    transition_models,
    work_inspection,
    work_inspection_models,
    work_views,
)
from pinboard.interfaces.cli_output import write_json
from pinboard.interfaces.errors import (
    CommandFailure,
    CommandResult,
    CommittedEffectFailure,
    TransitionInputFailure,
)
from pinboard.interfaces.transition_input import parse_item_revision_input, parse_transition_command
from pinboard.interfaces.work_briefs import read_selected_work_brief_identity


@dataclass(frozen=True, slots=True)
class _EncodedProjectTransitionRequest:
    action_id: ActionId
    encoded_payload: bytes


@dataclass(frozen=True, slots=True)
class _ValidatedItemRevisionRequest:
    validated_revision: work_models.ReviseItemDefinitionInput


type _ProjectTransitionRequest = _EncodedProjectTransitionRequest | _ValidatedItemRevisionRequest


@dataclass(frozen=True, slots=True)
class _CheckpointArtifactPublication:
    artifacts: CheckpointArtifacts
    created_immutable_artifact: bool


def _project_action_scope(action_id: ActionId) -> query_models.DecisionScope:
    kind_value, subject = str(action_id).split(":", 1)
    semantics = decision_models.action_semantics(decision_models.ActionKind(kind_value))
    match semantics.subject_kind:
        case decision_models.ActionSubjectKind.ITEM:
            return query_models.DecisionScope(
                item_ids=(ItemId(subject),),
                related_item_ids=(),
                dependency_closure_roots=(),
                live_dependent_roots=(),
                attempt_ids=(),
                proposal_ids=(),
                artifact_ref_ids=(),
            )
        case decision_models.ActionSubjectKind.ATTEMPT:
            return query_models.DecisionScope(
                item_ids=(),
                related_item_ids=(),
                dependency_closure_roots=(),
                live_dependent_roots=(),
                attempt_ids=(AttemptId(subject),),
                proposal_ids=(),
                artifact_ref_ids=(),
            )
        case decision_models.ActionSubjectKind.PROPOSAL:
            return query_models.DecisionScope(
                item_ids=(),
                related_item_ids=(),
                dependency_closure_roots=(),
                live_dependent_roots=(),
                attempt_ids=(),
                proposal_ids=(ProposalId(subject),),
                artifact_ref_ids=(),
            )
        case decision_models.ActionSubjectKind.LEDGER:
            return query_models.DecisionScope(
                item_ids=(),
                related_item_ids=(),
                dependency_closure_roots=(),
                live_dependent_roots=(),
                attempt_ids=(),
                proposal_ids=(),
                artifact_ref_ids=(),
            )
        case _ as unreachable:
            assert_never(unreachable)


def _committed_immutable_artifact_failure(error: ArtifactError | StorageError) -> CommittedEffectFailure:
    return CommittedEffectFailure(
        error.code.value,
        str(error),
        FailureDetails(
            observed=(),
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
    decoded_command = parse_transition_command(selected_action, encoded_payload)
    if isinstance(decoded_command, TransitionInputFailure):
        return CommandFailure(decoded_command.code, decoded_command.message, decoded_command.details)
    artifacts = ArtifactRepository(durable)
    match cli_command:
        case cli_commands.ProjectTransitionCommand(task_id=actor_task_id, host_id=actor_host_id):
            pass
        case cli_commands.AttemptTransitionCommand() | cli_commands.PreparationTransitionCommand():
            actor_task_id = None
            actor_host_id = None
        case _ as unreachable:
            assert_never(unreachable)
    commit_result = _execute_transition_command(roots, store, artifacts, decoded_command, actor_task_id, actor_host_id)
    if isinstance(commit_result, CommittedEffectFailure):
        return commit_result
    if isinstance(commit_result, CommandFailure):
        return action_selection.with_current_alternatives(store, supplied_action_receipt, commit_result)
    return _present_committed_transition(roots, durable, store, selected_action, commit_result, json=cli_command.json)


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
        continuation = work_inspection.read_attempt_continuation(roots, store, affected_attempt, datetime.now(UTC))
        if isinstance(continuation, CommandFailure):
            # The mutation already committed. An unavailable read projection is a warning, not rollback.
            print(f"Transition committed; continuation unavailable: {continuation}", file=sys.stderr)
            continuation = None
    if json:
        write_json(
            work_inspection_models.TransitionView(
                decision_models.action_id(selected_action), committed_revision, continuation
            )
        )
    else:
        print(f"OK TRANSITION_APPLIED {decision_models.action_id(selected_action)} revision={committed_revision}")
        if continuation is not None:
            write_json(work_inspection_models.AttemptView(continuation))
    return 0


def read_brief_identity(
    store: ports.WorkStore,
    command: decision_models.TransitionCommand,
    artifacts: ArtifactRepository,
) -> CommandResult[WorkBriefIdentity | None]:
    match command:
        case (
            decision_models.ActivateCommand(value=value)
            | decision_models.ResumeCommand(value=value)
            | decision_models.RebindAttemptCommand(value=value)
        ) if value.brief_artifact_ref_id is not None:
            reference = store.read_artifact_reference_by_id(value.brief_artifact_ref_id)
        case _:
            reference = None
    identity = read_selected_work_brief_identity(reference, artifacts)
    if isinstance(identity, DecisionFailure):
        return CommandFailure(identity.code, identity.message, identity.details)
    return identity


def publish_checkpoint_artifacts(
    roots: cli_commands.ResolvedRoots,
    command: decision_models.AcceptCheckpointCommand,
    artifacts: ArtifactRepository,
) -> CommandResult[_CheckpointArtifactPublication] | CommittedEffectFailure:
    action = command.action
    value = command.value
    attempt_id = str(action.capability.subject)
    checkpoint_id = str(value.checkpoint)
    attempt_root = roots.work / "attempts" / attempt_id
    try:
        result_bytes = (attempt_root / "result.md").read_bytes()
        review_bytes = (attempt_root / "review.md").read_bytes()
    except OSError as error:
        return CommandFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            f"Cannot read current checkpoint result.md and review.md: {error}",
            None,
        )
    result_artifact = NewArtifact(
        work_models.ArtifactKind.RESULT,
        f"{attempt_id}-{checkpoint_id}-result",
        1,
        ".md",
        result_bytes,
    )
    review_artifact = NewArtifact(
        work_models.ArtifactKind.EVIDENCE,
        f"{attempt_id}-{checkpoint_id}-review",
        1,
        ".md",
        review_bytes,
    )
    created_immutable_artifact = False
    try:
        result_existed = artifacts.revision_exists(result_artifact)
        result = artifacts.publish(result_artifact)
        created_immutable_artifact = not result_existed
        review_existed = artifacts.revision_exists(review_artifact)
        review = artifacts.publish(review_artifact)
        created_immutable_artifact = created_immutable_artifact or not review_existed
    except ArtifactError as error:
        if created_immutable_artifact:
            return _committed_immutable_artifact_failure(error)
        raise
    return _CheckpointArtifactPublication(
        CheckpointArtifacts(
            ResultArtifactRef(result.key, result.revision, result.selector, result.content_sha256, result.size_bytes),
            EvidenceArtifactRef(review.key, review.revision, review.selector, review.content_sha256, review.size_bytes),
        ),
        created_immutable_artifact,
    )


def _execute_transition_command(
    roots: cli_commands.ResolvedRoots,
    store: ports.WorkStore,
    artifacts: ArtifactRepository,
    command: decision_models.TransitionCommand,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
) -> CommandResult[CommittedEffect] | CommittedEffectFailure:
    transition_brief_identity = read_brief_identity(store, command, artifacts)
    if isinstance(transition_brief_identity, CommandFailure):
        return transition_brief_identity
    match command:
        case decision_models.AcceptCheckpointCommand():
            checkpoint_artifacts = publish_checkpoint_artifacts(roots, command, artifacts)
            if isinstance(checkpoint_artifacts, (CommandFailure, CommittedEffectFailure)):
                return checkpoint_artifacts
            try:
                result = decide_and_commit_checkpoint_acceptance(
                    store,
                    command,
                    datetime.now(UTC),
                    checkpoint_artifacts.artifacts,
                    actor_task_id=actor_task_id,
                    actor_host_id=actor_host_id,
                    transition_brief_identity=transition_brief_identity,
                )
            except StorageError as error:
                if checkpoint_artifacts.created_immutable_artifact:
                    return _committed_immutable_artifact_failure(error)
                raise
            if isinstance(result, DecisionFailure) and checkpoint_artifacts.created_immutable_artifact:
                details = (
                    FailureDetails(
                        observed=(),
                        mismatches=(),
                        retry=RetryDisposition.DO_NOT_RETRY,
                        effect=EffectDisposition.UNCHANGED,
                        changed_surfaces=(),
                        alternatives=(),
                    )
                    if result.details is None
                    else result.details
                )
                result = DecisionFailure(
                    result.code,
                    result.message,
                    FailureDetails(
                        observed=details.observed,
                        mismatches=details.mismatches,
                        retry=details.retry,
                        effect=EffectDisposition.COMMITTED,
                        changed_surfaces=(ChangedSurface.IMMUTABLE_ARTIFACT,),
                        alternatives=(),
                    ),
                )
        case (
            decision_models.AcceptReviewAndContinueCommand()
            | decision_models.ActivateCommand()
            | decision_models.PauseCommand()
            | decision_models.BlockCommand()
            | decision_models.CompleteCommand()
            | decision_models.CloseCommand()
            | decision_models.RebindAttemptCommand()
            | decision_models.ResumeCommand()
            | decision_models.SubmitReviewCommand()
            | decision_models.ReturnForCorrectionCommand()
            | decision_models.ReopenCommand()
            | decision_models.MarkReadyCommand()
            | decision_models.BlockItemCommand()
            | decision_models.DeferCommand()
            | decision_models.AcceptProposalCommand()
            | decision_models.MergeProposalCommand()
            | decision_models.ReturnProposalCommand()
            | decision_models.RejectProposalCommand()
            | decision_models.ReviseItemCommand()
        ):
            result = decide_and_commit_transition(
                store,
                command,
                datetime.now(UTC),
                actor_task_id=actor_task_id,
                actor_host_id=actor_host_id,
                transition_brief_identity=transition_brief_identity,
            )
        case _ as unreachable:
            assert_never(unreachable)
    if isinstance(result, DecisionFailure):
        return CommandFailure(result.code, result.message, result.details)
    return result


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
            command = parse_transition_command(action, encoded_payload)
            if isinstance(command, TransitionInputFailure):
                return CommandFailure(command.code, command.message, command.details)
            return command
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
    current_actions = discover_current_actions(
        store.read_decision_facts(_project_action_scope(requested_action_id), observed_at).snapshot,
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
    committed_mutation = _execute_transition_command(roots, store, artifacts, decoded_transition, task_id, host_id)
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
