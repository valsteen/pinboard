import hashlib
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, assert_never

import msgspec

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.errors import ArtifactError, FileIOError
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.application import ports, query_models, stored_state
from pinboard.application.actions import discover_current_actions
from pinboard.application.artifacts import (
    BriefArtifactRef,
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
    preflight_checkpoint_candidate,
)
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import (
    ArtifactAcceptanceAfterPublicationError,
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
from pinboard.interfaces import (
    action_selection,
    cli_commands,
    transition_models,
    work_brief_models,
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
    WorkBriefFailure,
    storage_failure_details,
)
from pinboard.interfaces.transition_input import parse_item_revision_input, parse_transition_command
from pinboard.interfaces.work_briefs import (
    canonical_checkpoint_bytes,
    canonical_checkpoint_review_package_bytes,
    canonical_reviewed_authority_set_bytes,
    decode_canonical_work_brief,
    decode_canonical_work_brief_review,
    read_selected_work_brief_identity,
    validate_work_brief_review,
)


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
    new_artifact_selectors: tuple[str, ...]


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


@dataclass(frozen=True, slots=True)
class _CheckpointBriefContext:
    brief: work_brief_models.WorkBrief
    reference: BriefArtifactRef
    review_reference: EvidenceArtifactRef | None


def _evidence_reference(reference: stored_state.ArtifactReference) -> EvidenceArtifactRef:
    return EvidenceArtifactRef(
        reference.key,
        reference.revision,
        reference.selector,
        reference.content_sha256,
        reference.size_bytes,
    )


def _portable_identity(
    role: Literal["accepted-brief", "result", "implementation-review", "brief-review"],
    reference: BriefArtifactRef | ResultArtifactRef | EvidenceArtifactRef,
) -> work_brief_models.PortableArtifactIdentity:
    return msgspec.convert(
        {
            "role": role,
            "kind": reference.kind.value,
            "key": reference.key,
            "revision": reference.revision,
            "selector": reference.selector,
            "content_sha256": reference.content_sha256,
            "size_bytes": reference.size_bytes,
        },
        type=work_brief_models.PortableArtifactIdentity,
        strict=True,
    )


def _read_checkpoint_brief_context(
    store: ports.WorkStore,
    command: decision_models.AcceptCheckpointCommand,
    artifacts: ArtifactRepository,
) -> CommandResult[_CheckpointBriefContext]:
    context = store.read_attempt_context(command.action.capability.subject)
    if not isinstance(context, query_models.NonterminalAttemptContextFacts):
        return CommandFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Checkpoint acceptance requires a current accepted brief.",
            None,
        )
    brief = decode_canonical_work_brief(artifacts.read(context.brief_reference))
    if isinstance(brief, WorkBriefFailure):
        return CommandFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            f"The accepted brief is invalid: {brief}",
            None,
        )
    expected_identity = (
        str(context.attempt_id),
        str(context.item_id),
        context.branch,
        context.base_revision,
        context.accepted_scope_revision,
        context.accepted_scope_digest,
    )
    observed_identity = (
        brief.attempt_id,
        brief.item_id,
        brief.branch,
        brief.base_revision,
        brief.accepted_scope.revision,
        brief.accepted_scope.digest,
    )
    if observed_identity != expected_identity:
        return CommandFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "The accepted brief identity does not match the current attempt.",
            None,
        )
    checkpoint = brief.checkpoint
    if checkpoint.checkpoint_id != command.value.checkpoint:
        return CommandFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Checkpoint acceptance requires the accepted brief checkpoint.",
            None,
        )
    match checkpoint:
        case work_brief_models.LocalCheckpoint():
            review_reference = None
        case work_brief_models.CrossBoundaryCheckpoint():
            checkpoint_sha256 = hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest()
            stored_review = store.read_artifact_reference(
                work_models.ArtifactKind.EVIDENCE,
                f"{brief.attempt_id}-brief-review-{checkpoint_sha256}",
                1,
            )
            if stored_review is None:
                return CommandFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Checkpoint acceptance requires the exact ready brief review.",
                    None,
                )
            review = decode_canonical_work_brief_review(artifacts.read(stored_review))
            if (
                isinstance(review, WorkBriefFailure)
                or (failure := validate_work_brief_review(review, brief)) is not None
            ):
                detail = review if isinstance(review, WorkBriefFailure) else failure
                return CommandFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    f"The accepted ready brief review is invalid: {detail}",
                    None,
                )
            review_reference = _evidence_reference(stored_review)
        case _ as unreachable:
            assert_never(unreachable)
    return _CheckpointBriefContext(brief, context.brief_reference, review_reference)


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
        continuation = work_inspection.read_attempt_continuation(roots, store, affected_attempt)
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
    brief_context: _CheckpointBriefContext,
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
    new_artifact_selectors: list[str] = []
    try:
        result_publication = artifacts.publish(result_artifact)
        result = result_publication.reference
        if result_publication.created:
            new_artifact_selectors.append(result.selector)
        review_publication = artifacts.publish(review_artifact)
        review = review_publication.reference
        if review_publication.created:
            new_artifact_selectors.append(review.selector)
        checkpoint = brief_context.brief.checkpoint
        checkpoint_sha256 = hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest()
        accepted_brief_identity = _portable_identity("accepted-brief", brief_context.reference)
        result_reference = ResultArtifactRef(
            result.key, result.revision, result.selector, result.content_sha256, result.size_bytes
        )
        implementation_review_reference = EvidenceArtifactRef(
            review.key, review.revision, review.selector, review.content_sha256, review.size_bytes
        )
        match checkpoint:
            case work_brief_models.LocalCheckpoint():
                review_basis = msgspec.convert(
                    {"boundary": "local"},
                    type=work_brief_models.ReviewBasis,
                    strict=True,
                )
            case work_brief_models.CrossBoundaryCheckpoint():
                ready_review = brief_context.review_reference
                if ready_review is None:
                    raise AssertionError("Cross-boundary checkpoint context requires a ready review.")
                review_basis = msgspec.convert(
                    {
                        "boundary": "cross-boundary",
                        "brief_review": msgspec.to_builtins(_portable_identity("brief-review", ready_review)),
                        "checkpoint_sha256": checkpoint_sha256,
                        "reviewed_authority_set_sha256": hashlib.sha256(
                            canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities)
                        ).hexdigest(),
                    },
                    type=work_brief_models.ReviewBasis,
                    strict=True,
                )
            case _ as unreachable:
                assert_never(unreachable)
        package = msgspec.convert(
            {
                "schema": "pinboard-checkpoint-review-package/v1",
                "attempt_id": brief_context.brief.attempt_id,
                "item_id": brief_context.brief.item_id,
                "candidate": str(value.candidate),
                "acceptance_evidence": value.evidence,
                "accepted_scope": msgspec.to_builtins(brief_context.brief.accepted_scope),
                "checkpoint": {"id": checkpoint.checkpoint_id, "sha256": checkpoint_sha256},
                "accepted_brief": msgspec.to_builtins(accepted_brief_identity),
                "result": msgspec.to_builtins(_portable_identity("result", result_reference)),
                "implementation_review": msgspec.to_builtins(
                    _portable_identity("implementation-review", implementation_review_reference)
                ),
                "verdict": "ready",
                "review_basis": msgspec.to_builtins(review_basis),
            },
            type=work_brief_models.CheckpointReviewPackage,
            strict=True,
        )
        package_artifact = NewArtifact(
            work_models.ArtifactKind.EVIDENCE,
            f"{attempt_id}-{checkpoint_id}-review-package",
            1,
            ".json",
            canonical_checkpoint_review_package_bytes(package),
        )
        package_publication = artifacts.publish(package_artifact)
        published_package = package_publication.reference
        if package_publication.created:
            new_artifact_selectors.append(published_package.selector)
    except ArtifactAcceptanceAfterPublicationError as error:
        cause = error.cause
        if not isinstance(cause, FileIOError):
            raise
        return _committed_immutable_artifact_failure(
            cause,
            (*new_artifact_selectors, error.selector),
            roots,
        )
    except ArtifactError as error:
        if new_artifact_selectors:
            return _committed_immutable_artifact_failure(error, tuple(new_artifact_selectors), roots)
        raise
    return _CheckpointArtifactPublication(
        CheckpointArtifacts(
            result_reference,
            implementation_review_reference,
            EvidenceArtifactRef(
                published_package.key,
                published_package.revision,
                published_package.selector,
                published_package.content_sha256,
                published_package.size_bytes,
            ),
        ),
        tuple(new_artifact_selectors),
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
            if (candidate_failure := preflight_checkpoint_candidate(store, command, datetime.now(UTC))) is not None:
                return CommandFailure(candidate_failure.code, candidate_failure.message, candidate_failure.details)
            brief_context = _read_checkpoint_brief_context(store, command, artifacts)
            if isinstance(brief_context, CommandFailure):
                return brief_context
            checkpoint_artifacts = publish_checkpoint_artifacts(roots, command, artifacts, brief_context)
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
                if checkpoint_artifacts.new_artifact_selectors:
                    return _committed_immutable_artifact_failure(
                        error, checkpoint_artifacts.new_artifact_selectors, roots
                    )
                raise
            if isinstance(result, DecisionFailure) and checkpoint_artifacts.new_artifact_selectors:
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
                        observed=(
                            *tuple(
                                FailureFact("published_artifact_selector", selector)
                                for selector in checkpoint_artifacts.new_artifact_selectors
                            ),
                            *details.observed,
                        ),
                        mismatches=details.mismatches,
                        retry=RetryDisposition.DO_NOT_RETRY,
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
    scope = action_selection.action_identity_scope(requested_action_id)
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
