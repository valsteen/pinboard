"""Artifact-sensitive lifecycle execution shared by command boundaries."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, assert_never

import msgspec

from pinboard.adapters import candidate_evidence
from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.errors import ArtifactError, FileIOError
from pinboard.adapters.files.root import (
    CurrentHeadCandidate,
    DifferentHeadCandidate,
    DirtyHeadCandidate,
    RootError,
    observe_checkout_identity,
    read_current_head_candidate,
    read_working_tree_candidate,
)
from pinboard.adapters.lifecycle_operations import SelectedTransition, transition_brief_identity
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.application import (
    candidate_snapshots,
    checkpoint_packages,
    ports,
    query_models,
    service,
    stored_state,
    work_brief_models,
)
from pinboard.application.artifacts import (
    ArtifactRef,
    BriefArtifactRef,
    CheckpointArtifacts,
    CompletionArtifacts,
    EvidenceArtifactRef,
    NewArtifact,
    ResultArtifactRef,
)
from pinboard.application.mutation_models import CommittedEffect
from pinboard.application.work_briefs import (
    canonical_checkpoint_bytes,
    canonical_checkpoint_review_package_bytes,
    canonical_completion_review_package_bytes,
    canonical_reviewed_authority_set_bytes,
    decode_canonical_work_brief,
    decode_canonical_work_brief_review,
    validate_work_brief_review,
)
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import (
    ArtifactAcceptanceAfterPublicationError,
    ChangedSurface,
    DecisionFailure,
    DecisionFailureCode,
    DecisionResult,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    RetryDisposition,
)
from pinboard.domain.identifiers import ArtifactRefId


@dataclass(frozen=True, slots=True)
class PublishedTransitionFailure:
    code: str
    message: str
    details: FailureDetails
    storage_error: StorageError | None


@dataclass(frozen=True, slots=True)
class ArtifactTransitionSuccess:
    effect: CommittedEffect
    published_selectors: tuple[str, ...]


type ArtifactTransitionResult = DecisionResult[ArtifactTransitionSuccess] | PublishedTransitionFailure


def _unchanged(message: str, *, candidate: str | None = None) -> DecisionFailure:
    return DecisionFailure(
        DecisionFailureCode.TRANSITION_INPUT_INVALID,
        message,
        FailureDetails(
            observed=() if candidate is None else (FailureFact("candidate_revision", candidate),),
            mismatches=(),
            retry=RetryDisposition.CORRECT_INPUT,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def _published_failure(
    code: str,
    message: str,
    selectors: tuple[str, ...],
    *,
    storage_error: StorageError | None = None,
) -> PublishedTransitionFailure:
    return PublishedTransitionFailure(
        code,
        message,
        FailureDetails(
            observed=tuple(FailureFact("published_artifact_selector", value) for value in selectors),
            mismatches=(),
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.COMMITTED,
            changed_surfaces=(ChangedSurface.IMMUTABLE_ARTIFACT,),
            alternatives=(),
        ),
        storage_error,
    )


def _committed_decision_failure(
    failure: DecisionFailure,
    selectors: tuple[str, ...],
) -> DecisionFailure:
    details = failure.details
    return DecisionFailure(
        failure.code,
        failure.message,
        FailureDetails(
            observed=(
                *tuple(FailureFact("published_artifact_selector", value) for value in selectors),
                *(() if details is None else details.observed),
            ),
            mismatches=() if details is None else details.mismatches,
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.COMMITTED,
            changed_surfaces=(ChangedSurface.IMMUTABLE_ARTIFACT,),
            alternatives=(),
        ),
    )


def _observe_review_candidate(
    source_checkout: Path,
    store: ports.WorkStore,
    command: decision_models.SubmitReviewCommand,
    recorded_at: datetime,
) -> DecisionResult[candidate_snapshots.CandidateSnapshot]:
    context = store.read_attempt_context(command.action.capability.subject)
    candidate = str(command.value.candidate)
    if not isinstance(context, query_models.NonterminalAttemptContextFacts):
        return _unchanged("Review submission requires one current nonterminal attempt.", candidate=candidate)
    try:
        branch, head = observe_checkout_identity(source_checkout)
    except RootError as error:
        return _unchanged(f"Cannot observe the review candidate checkout: {error}", candidate=candidate)
    if branch != context.branch:
        return _unchanged("Review submission requires the attempt's exact branch.", candidate=candidate)
    if candidate.startswith("working-tree-sha256:"):
        try:
            observed = read_working_tree_candidate(source_checkout)
        except RootError as error:
            return _unchanged(f"Cannot read the working-tree candidate: {error}", candidate=candidate)
        if observed.identity != candidate:
            return _unchanged("Review submission requires the exact current binary HEAD diff.", candidate=candidate)
        return candidate_snapshots.WorkingTreeCandidateSnapshot(
            "pinboard-candidate-snapshot/v1",
            str(context.attempt_id),
            str(context.item_id),
            candidate,
            branch,
            head,
            context.base_revision,
            recorded_at.isoformat(),
            observed.diff,
        )
    try:
        observed_commit = read_current_head_candidate(source_checkout, candidate, context.base_revision)
    except RootError as error:
        return _unchanged(f"Cannot read the commit candidate: {error}", candidate=candidate)
    match observed_commit:
        case CurrentHeadCandidate():
            return candidate_snapshots.CommitCandidateSnapshot(
                "pinboard-candidate-snapshot/v1",
                str(context.attempt_id),
                str(context.item_id),
                candidate,
                branch,
                context.base_revision,
                context.base_revision,
                recorded_at.isoformat(),
                observed_commit.diff,
            )
        case DifferentHeadCandidate():
            return _unchanged("Review submission requires a commit candidate to match the exact current HEAD.")
        case DirtyHeadCandidate():
            return _unchanged("Review submission requires a clean working tree for a commit candidate.")
        case _ as unreachable:
            assert_never(unreachable)


def _submit_review(
    source_checkout: Path,
    store: ports.WorkStore,
    artifacts: ArtifactRepository,
    command: decision_models.SubmitReviewCommand,
    operation_time: datetime,
) -> ArtifactTransitionResult:
    snapshot = _observe_review_candidate(source_checkout, store, command, operation_time)
    if isinstance(snapshot, DecisionFailure):
        return snapshot
    artifact = NewArtifact(
        work_models.ArtifactKind.EVIDENCE,
        candidate_snapshots.candidate_snapshot_key(snapshot),
        1,
        ".json",
        candidate_snapshots.canonical_candidate_snapshot_bytes(snapshot),
    )
    try:
        publication = artifacts.publish(artifact)
    except ArtifactAcceptanceAfterPublicationError as error:
        if not isinstance(error.cause, FileIOError):
            raise
        return _published_failure(error.cause.code.value, str(error.cause), (error.selector,))
    reference = EvidenceArtifactRef(
        publication.reference.key,
        publication.reference.revision,
        publication.reference.selector,
        publication.reference.content_sha256,
        publication.reference.size_bytes,
    )
    try:
        result = service.decide_and_commit_review_submission(store, command, operation_time, reference)
    except StorageError as error:
        if publication.created:
            return _published_failure(
                error.code.value,
                str(error),
                (reference.selector,),
                storage_error=error,
            )
        raise
    if isinstance(result, DecisionFailure) and publication.created:
        return _committed_decision_failure(result, (reference.selector,))
    if isinstance(result, DecisionFailure):
        return result
    return ArtifactTransitionSuccess(result, (reference.selector,) if publication.created else ())


def _evidence_reference(reference: stored_state.ArtifactReference | ArtifactRef) -> EvidenceArtifactRef:
    return EvidenceArtifactRef(
        reference.key,
        reference.revision,
        reference.selector,
        reference.content_sha256,
        reference.size_bytes,
    )


def _portable(
    role: Literal["accepted-brief", "candidate", "result", "implementation-review", "brief-review"],
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


@dataclass(frozen=True, slots=True)
class _CheckpointContext:
    brief: work_brief_models.WorkBrief
    reference: BriefArtifactRef
    review_reference: EvidenceArtifactRef | None


def _checkpoint_context(
    store: ports.WorkStore,
    artifacts: ArtifactRepository,
    command: decision_models.AcceptCheckpointCommand,
) -> DecisionResult[_CheckpointContext]:
    context = store.read_attempt_context(command.action.capability.subject)
    if not isinstance(context, query_models.NonterminalAttemptContextFacts):
        return _unchanged("Checkpoint acceptance requires a current accepted brief.")
    brief = decode_canonical_work_brief(artifacts.read(context.brief_reference))
    if isinstance(brief, work_brief_models.WorkBriefFailure):
        return _unchanged(f"The accepted brief is invalid: {brief.message}")
    if (
        brief.attempt_id,
        brief.item_id,
        brief.branch,
        brief.base_revision,
        brief.accepted_scope.revision,
        brief.accepted_scope.digest,
    ) != (
        str(context.attempt_id),
        str(context.item_id),
        context.branch,
        context.base_revision,
        context.accepted_scope_revision,
        context.accepted_scope_digest,
    ):
        return _unchanged("The accepted brief identity does not match the current attempt.")
    if brief.checkpoint.checkpoint_id != command.value.checkpoint:
        return _unchanged("Checkpoint acceptance requires the accepted brief checkpoint.")
    match brief.checkpoint:
        case work_brief_models.LocalCheckpoint():
            review_reference = None
        case work_brief_models.CrossBoundaryCheckpoint():
            digest = hashlib.sha256(canonical_checkpoint_bytes(brief.checkpoint)).hexdigest()
            stored_review = store.read_artifact_reference(
                work_models.ArtifactKind.EVIDENCE,
                f"{brief.attempt_id}-brief-review-{digest}",
                1,
            )
            if stored_review is None:
                return _unchanged("Checkpoint acceptance requires the exact ready brief review.")
            review = decode_canonical_work_brief_review(artifacts.read(stored_review))
            if isinstance(review, work_brief_models.WorkBriefFailure):
                return _unchanged(f"The accepted ready brief review is invalid: {review.message}")
            if (failure := validate_work_brief_review(review, brief)) is not None:
                return _unchanged(f"The accepted ready brief review is invalid: {failure.message}")
            review_reference = _evidence_reference(stored_review)
        case _ as unreachable:
            assert_never(unreachable)
    return _CheckpointContext(brief, context.brief_reference, review_reference)


def _publish_checkpoint(  # noqa: C901, PLR0912 - one ordered immutable publication boundary
    work_root: Path,
    store: ports.WorkStore,
    artifacts: ArtifactRepository,
    command: decision_models.AcceptCheckpointCommand,
    context: _CheckpointContext,
) -> tuple[CheckpointArtifacts, tuple[str, ...]] | DecisionFailure | PublishedTransitionFailure:
    attempt_id = str(command.action.capability.subject)
    checkpoint_id = str(command.value.checkpoint)
    selected = candidate_evidence.read_candidate_evidence(
        work_root,
        store,
        command.action.capability.subject,
        str(command.value.candidate),
    )
    if isinstance(selected, DecisionFailure):
        return selected
    attempt_root = work_root / "attempts" / attempt_id
    try:
        result_bytes = (attempt_root / "result.md").read_bytes()
        review_bytes = (attempt_root / "review.md").read_bytes()
    except OSError as error:
        return _unchanged(f"Cannot read current checkpoint result.md and review.md: {error}")
    publications = (
        NewArtifact(
            work_models.ArtifactKind.EVIDENCE,
            f"{attempt_id}-{checkpoint_id}-candidate",
            1,
            ".patch",
            selected.snapshot.diff,
        ),
        NewArtifact(
            work_models.ArtifactKind.RESULT,
            f"{attempt_id}-{checkpoint_id}-result",
            1,
            ".md",
            result_bytes,
        ),
        NewArtifact(
            work_models.ArtifactKind.EVIDENCE,
            f"{attempt_id}-{checkpoint_id}-review",
            1,
            ".md",
            review_bytes,
        ),
    )
    created: list[str] = []
    try:
        candidate_publication = artifacts.publish(publications[0])
        if candidate_publication.created:
            created.append(candidate_publication.reference.selector)
        result_publication = artifacts.publish(publications[1])
        if result_publication.created:
            created.append(result_publication.reference.selector)
        review_publication = artifacts.publish(publications[2])
        if review_publication.created:
            created.append(review_publication.reference.selector)
        candidate = _evidence_reference(candidate_publication.reference)
        result = ResultArtifactRef(
            result_publication.reference.key,
            result_publication.reference.revision,
            result_publication.reference.selector,
            result_publication.reference.content_sha256,
            result_publication.reference.size_bytes,
        )
        implementation_review = _evidence_reference(review_publication.reference)
        checkpoint = context.brief.checkpoint
        checkpoint_sha256 = hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest()
        match checkpoint:
            case work_brief_models.LocalCheckpoint():
                review_basis = msgspec.convert(
                    {"boundary": "local"},
                    type=work_brief_models.ReviewBasis,
                    strict=True,
                )
            case work_brief_models.CrossBoundaryCheckpoint():
                if context.review_reference is None:
                    raise AssertionError("Cross-boundary checkpoint context requires a ready review.")
                review_basis = msgspec.convert(
                    {
                        "boundary": "cross-boundary",
                        "brief_review": msgspec.to_builtins(_portable("brief-review", context.review_reference)),
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
                "schema": "pinboard-checkpoint-review-package/v2",
                "attempt_id": context.brief.attempt_id,
                "item_id": context.brief.item_id,
                "candidate": str(command.value.candidate),
                "acceptance_evidence": command.value.evidence,
                "accepted_scope": msgspec.to_builtins(context.brief.accepted_scope),
                "checkpoint": {"id": checkpoint.checkpoint_id, "sha256": checkpoint_sha256},
                "candidate_snapshot": msgspec.to_builtins(_portable("candidate", candidate)),
                "accepted_brief": msgspec.to_builtins(_portable("accepted-brief", context.reference)),
                "result": msgspec.to_builtins(_portable("result", result)),
                "implementation_review": msgspec.to_builtins(_portable("implementation-review", implementation_review)),
                "verdict": "ready",
                "review_basis": msgspec.to_builtins(review_basis),
            },
            type=work_brief_models.CheckpointReviewPackageV2,
            strict=True,
        )
        package_publication = artifacts.publish(
            NewArtifact(
                work_models.ArtifactKind.EVIDENCE,
                f"{attempt_id}-{checkpoint_id}-review-package",
                1,
                ".json",
                canonical_checkpoint_review_package_bytes(package),
            )
        )
        if package_publication.created:
            created.append(package_publication.reference.selector)
    except ArtifactAcceptanceAfterPublicationError as error:
        if not isinstance(error.cause, FileIOError):
            raise
        return _published_failure(
            error.cause.code.value,
            str(error.cause),
            (*created, error.selector),
        )
    except ArtifactError as error:
        if created:
            return _published_failure(error.code.value, str(error), tuple(created))
        raise
    return (
        CheckpointArtifacts(
            candidate,
            result,
            implementation_review,
            _evidence_reference(package_publication.reference),
        ),
        tuple(created),
    )


def _accept_checkpoint(
    work_root: Path,
    store: ports.WorkStore,
    artifacts: ArtifactRepository,
    selected: SelectedTransition,
    command: decision_models.AcceptCheckpointCommand,
    operation_time: datetime,
) -> ArtifactTransitionResult:
    if (failure := service.preflight_checkpoint_candidate(store, command, operation_time)) is not None:
        return failure
    context = _checkpoint_context(store, artifacts, command)
    if isinstance(context, DecisionFailure):
        return context
    published = _publish_checkpoint(work_root, store, artifacts, command, context)
    if isinstance(published, (DecisionFailure, PublishedTransitionFailure)):
        return published
    checkpoint_artifacts, created = published
    brief_identity = transition_brief_identity(store, command, artifacts)
    if isinstance(brief_identity, DecisionFailure):
        return brief_identity
    try:
        result = service.decide_and_commit_checkpoint_acceptance(
            store,
            command,
            operation_time,
            checkpoint_artifacts,
            actor_task_id=selected.actor_task_id,
            actor_host_id=selected.actor_host_id,
            transition_brief_identity=brief_identity,
        )
    except StorageError as error:
        if created:
            return _published_failure(error.code.value, str(error), created, storage_error=error)
        raise
    if isinstance(result, DecisionFailure) and created:
        return _committed_decision_failure(result, created)
    if isinstance(result, DecisionFailure):
        return result
    return ArtifactTransitionSuccess(result, created)


@dataclass(frozen=True, slots=True)
class _CompletionContext:
    brief: work_brief_models.WorkBrief
    reference: BriefArtifactRef
    checkpoint_coverage: tuple[work_brief_models.CompletionCheckpointCoverage, ...]


def _completion_context(  # noqa: C901, PLR0912 - one exact completion-closure validation boundary
    store: ports.WorkStore,
    artifacts: ArtifactRepository,
    command: decision_models.CoveredCompleteCommand,
) -> DecisionResult[_CompletionContext]:
    selected = store.read_completion_context(command.action.capability.subject)
    if selected is None or not isinstance(selected.attempt, query_models.NonterminalAttemptContextFacts):
        return _unchanged("Covered completion requires one current nonterminal attempt.")
    attempt = selected.attempt
    brief = decode_canonical_work_brief(artifacts.read(attempt.brief_reference))
    if isinstance(brief, work_brief_models.WorkBriefFailure):
        return _unchanged(f"The accepted brief is invalid: {brief.message}")
    if (
        brief.attempt_id,
        brief.item_id,
        brief.branch,
        brief.base_revision,
        brief.accepted_scope.revision,
        brief.accepted_scope.digest,
    ) != (
        str(attempt.attempt_id),
        str(attempt.item_id),
        attempt.branch,
        attempt.base_revision,
        attempt.accepted_scope_revision,
        attempt.accepted_scope_digest,
    ):
        return _unchanged("The accepted brief identity does not match the current attempt.")
    if str(command.value.reviewer_task_id) == brief.owner_task_id:
        return _unchanged("The completion reviewer must be independent from the attempt owner.")
    if len(selected.checkpoints) != len(command.value.packages):
        return _unchanged("Covered completion must name the complete authoritative checkpoint set.")
    coverage: list[work_brief_models.CompletionCheckpointCoverage] = []
    for facts, supplied in zip(selected.checkpoints, command.value.packages, strict=True):
        reference = facts.package_reference
        if (
            int(facts.receipt.history_id) != int(supplied.history_id)
            or reference is None
            or reference.content_sha256 != supplied.package_sha256
        ):
            return _unchanged("Covered completion checkpoint identities do not match current history.")
        package = checkpoint_packages.validate_selected_checkpoint_review_package(
            facts.receipt,
            reference,
            artifacts.read(reference),
            attempt_id=str(attempt.attempt_id),
            item_id=str(attempt.item_id),
        )
        if isinstance(package, work_brief_models.WorkBriefFailure):
            return _unchanged(package.message)
        identities = [package.accepted_brief, package.result, package.implementation_review]
        if isinstance(package, work_brief_models.CheckpointReviewPackageV2):
            identities.append(package.candidate_snapshot)
        if isinstance(package.review_basis, work_brief_models.CrossBoundaryReviewBasis):
            identities.append(package.review_basis.brief_review)
        references: list[stored_state.ArtifactReference] = []
        artifact_bytes: dict[ArtifactRefId, bytes] = {}
        for identity in identities:
            accepted = store.read_artifact_reference(
                work_models.ArtifactKind(identity.kind),
                identity.key,
                identity.revision,
            )
            if accepted is None:
                return _unchanged("A covered checkpoint artifact identity is no longer accepted.")
            references.append(accepted)
            artifact_bytes[accepted.artifact_ref_id] = artifacts.read(accepted)
        if (
            failure := checkpoint_packages.validate_checkpoint_package_closure(
                package,
                tuple(references),
                artifact_bytes,
            )
        ) is not None:
            return _unchanged(failure.message)
        coverage.append(
            work_brief_models.CompletionCheckpointCoverage(
                int(facts.receipt.history_id),
                package.checkpoint,
                package.candidate,
                work_brief_models.CheckpointPackageCompletionIdentity(
                    "evidence",
                    reference.key,
                    reference.revision,
                    reference.selector,
                    reference.content_sha256,
                    reference.size_bytes,
                ),
                "reused" if supplied.disposition == work_models.CompletionPackageDisposition.REUSED else "revalidated",
                supplied.evidence,
            )
        )
    return _CompletionContext(brief, attempt.brief_reference, tuple(coverage))


def _publish_completion(
    work_root: Path,
    artifacts: ArtifactRepository,
    command: decision_models.CoveredCompleteCommand,
    context: _CompletionContext,
) -> tuple[CompletionArtifacts, tuple[str, ...]] | DecisionFailure | PublishedTransitionFailure:
    attempt_id = str(command.action.capability.subject)
    attempt_root = work_root / "attempts" / attempt_id
    try:
        result_bytes = (attempt_root / "result.md").read_bytes()
        review_bytes = (attempt_root / "review.md").read_bytes()
    except OSError as error:
        return _unchanged(f"Cannot read current completion result.md and review.md: {error}")
    if hashlib.sha256(result_bytes).hexdigest() != command.value.result_sha256:
        return _unchanged("Current result.md does not match result_sha256.")
    if hashlib.sha256(review_bytes).hexdigest() != command.value.review_sha256:
        return _unchanged("Current review.md does not match review_sha256.")
    created: list[str] = []
    try:
        result_publication = artifacts.publish(
            NewArtifact(work_models.ArtifactKind.RESULT, f"{attempt_id}-terminal-result", 1, ".md", result_bytes)
        )
        if result_publication.created:
            created.append(result_publication.reference.selector)
        review_publication = artifacts.publish(
            NewArtifact(work_models.ArtifactKind.EVIDENCE, f"{attempt_id}-terminal-review", 1, ".md", review_bytes)
        )
        if review_publication.created:
            created.append(review_publication.reference.selector)
        result = ResultArtifactRef(
            result_publication.reference.key,
            result_publication.reference.revision,
            result_publication.reference.selector,
            result_publication.reference.content_sha256,
            result_publication.reference.size_bytes,
        )
        review = _evidence_reference(review_publication.reference)
        package = work_brief_models.CompletionReviewPackage(
            "pinboard-completion-review-package/v1",
            context.brief.attempt_id,
            context.brief.item_id,
            str(command.value.candidate),
            command.value.evidence,
            str(command.value.reviewer_task_id),
            context.brief.accepted_scope,
            work_brief_models.AcceptedBriefCompletionIdentity(
                "brief",
                context.reference.key,
                context.reference.revision,
                context.reference.selector,
                context.reference.content_sha256,
                context.reference.size_bytes,
            ),
            work_brief_models.TerminalResultCompletionIdentity(
                "result",
                result.key,
                result.revision,
                result.selector,
                result.content_sha256,
                result.size_bytes,
            ),
            work_brief_models.FinalReviewCompletionIdentity(
                "evidence",
                review.key,
                review.revision,
                review.selector,
                review.content_sha256,
                review.size_bytes,
            ),
            context.checkpoint_coverage,
        )
        package_publication = artifacts.publish(
            NewArtifact(
                work_models.ArtifactKind.EVIDENCE,
                f"{attempt_id}-completion-review-package",
                1,
                ".json",
                canonical_completion_review_package_bytes(package),
            )
        )
        if package_publication.created:
            created.append(package_publication.reference.selector)
    except ArtifactAcceptanceAfterPublicationError as error:
        if not isinstance(error.cause, FileIOError):
            raise
        return _published_failure(
            error.cause.code.value,
            str(error.cause),
            (*created, error.selector),
        )
    except ArtifactError as error:
        if created:
            return _published_failure(error.code.value, str(error), tuple(created))
        raise
    return (
        CompletionArtifacts(result, review, _evidence_reference(package_publication.reference)),
        tuple(created),
    )


def _complete(
    work_root: Path,
    store: ports.WorkStore,
    artifacts: ArtifactRepository,
    selected: SelectedTransition,
    command: decision_models.CoveredCompleteCommand,
    operation_time: datetime,
) -> ArtifactTransitionResult:
    if selected.actor_task_id is None or selected.actor_host_id is None:
        return _unchanged("Covered completion requires project task and host attribution.")
    if command.value.reviewer_task_id == selected.actor_task_id:
        return _unchanged("The completion reviewer must differ from the invoking task.")
    if (
        failure := service.preflight_covered_completion(
            store,
            command,
            operation_time,
            actor_task_id=selected.actor_task_id,
            actor_host_id=selected.actor_host_id,
        )
    ) is not None:
        return failure
    context = _completion_context(store, artifacts, command)
    if isinstance(context, DecisionFailure):
        return context
    published = _publish_completion(work_root, artifacts, command, context)
    if isinstance(published, (DecisionFailure, PublishedTransitionFailure)):
        return published
    completion_artifacts, created = published
    try:
        result = service.decide_and_commit_covered_completion(
            store,
            command,
            operation_time,
            completion_artifacts,
            actor_task_id=selected.actor_task_id,
            actor_host_id=selected.actor_host_id,
        )
    except StorageError as error:
        if created:
            return _published_failure(error.code.value, str(error), created, storage_error=error)
        raise
    if isinstance(result, DecisionFailure) and created:
        return _committed_decision_failure(result, created)
    if isinstance(result, DecisionFailure):
        return result
    return ArtifactTransitionSuccess(result, created)


def execute_artifact_transition(
    source_checkout: Path,
    work_root: Path,
    store: ports.WorkStore,
    artifacts: ArtifactRepository,
    selected: SelectedTransition,
    operation_time: datetime,
) -> ArtifactTransitionResult:
    match selected.command:
        case decision_models.SubmitReviewCommand() as command:
            return _submit_review(source_checkout, store, artifacts, command, operation_time)
        case decision_models.AcceptCheckpointCommand() as command:
            return _accept_checkpoint(work_root, store, artifacts, selected, command, operation_time)
        case decision_models.CoveredCompleteCommand() as command:
            return _complete(work_root, store, artifacts, selected, command, operation_time)
        case _:
            raise ValueError("The selected transition is not artifact-sensitive.")
