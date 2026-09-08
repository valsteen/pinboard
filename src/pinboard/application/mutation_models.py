from dataclasses import dataclass

from pinboard.application import stored_state
from pinboard.application.artifacts import EvidenceArtifactRef, ResultArtifactRef
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, HistoryId, HistorySubjectId, HostId, ItemId, TaskId
from pinboard.domain.proposal_models import ProposalCreationDecision


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    """Stored-history identity for an ordinary transition receipt."""

    transition: decision_models.TransitionReceipt
    history_id: HistoryId
    project_revision: int
    action_kind: decision_models.ActionKind
    subject_id: HistorySubjectId
    artifact_ref_id: ArtifactRefId | None
    authorization: decision_models.AuthorizationKind
    actor_task_id: TaskId | None
    actor_host_id: HostId | None
    input_schema: str
    input_payload: work_models.CanonicalJson


@dataclass(frozen=True, slots=True)
class MutationAllocation:
    """Exact singleton allocation facts read inside one locked write transaction."""

    project_revision: int
    next_history_id: HistoryId


@dataclass(frozen=True, slots=True)
class CheckpointMutationAllocation(MutationAllocation):
    """Additional identities required only by checkpoint artifact acceptance."""

    next_artifact_ref_id: ArtifactRefId
    accepted_artifacts: tuple[stored_state.ArtifactReference, ...]


@dataclass(frozen=True, slots=True)
class CommittedEffect:
    """One receipt plus the exact generated projections changed by its commit."""

    receipt: MutationReceipt
    item_ids: tuple[ItemId, ...]
    attempt_ids: tuple[AttemptId, ...]
    continuation_attempt_id: AttemptId | None


@dataclass(frozen=True, slots=True)
class PreparationStart:
    effect: CommittedEffect
    authority: authority_models.PreparationLeaseAuthority


@dataclass(frozen=True, slots=True)
class ProposalCreationMutation:
    """Persists an authorized proposal intake without deciding its legality."""

    receipt: MutationReceipt
    decision: ProposalCreationDecision


@dataclass(frozen=True, slots=True)
class CheckpointArtifactChanges:
    """Exact identities assigned to one accepted checkpoint result and review."""

    result: ResultArtifactRef
    result_id: ArtifactRefId
    review: EvidenceArtifactRef
    review_id: ArtifactRefId


@dataclass(frozen=True, slots=True)
class TransitionMutation:
    """Persists one accepted closed lifecycle decision as an exact relational delta."""

    decision: decision_models.TransitionDecision
    receipt: MutationReceipt


@dataclass(frozen=True, slots=True)
class CheckpointAcceptanceMutation:
    """Persists checkpoint acceptance with its required result and review artifacts."""

    decision: decision_models.CheckpointAcceptanceDecision
    receipt: MutationReceipt
    checkpoint_artifacts: CheckpointArtifactChanges


@dataclass(frozen=True, slots=True)
class AttemptAuthorityMutation:
    """Persists an authorized attempt-authority change without deciding its legality."""

    receipt: MutationReceipt
    decision: authority_models.AttemptAuthorityDecision


@dataclass(frozen=True, slots=True)
class PreparationAuthorityMutation:
    """Persists an authorized preparation-authority change without deciding its legality."""

    receipt: MutationReceipt
    decision: authority_models.PreparationAuthorityDecision


type StoredStateMutation = (
    TransitionMutation
    | CheckpointAcceptanceMutation
    | ProposalCreationMutation
    | AttemptAuthorityMutation
    | PreparationAuthorityMutation
)
