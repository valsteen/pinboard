"""Canonical immutable evidence for one protected review candidate."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Protocol

import msgspec

from pinboard.application import query_models, stored_state
from pinboard.domain import decision_models, history, work_models
from pinboard.domain.identifiers import ArtifactRefId, AttemptId

type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\r\n]+\z")]
type Sha256 = Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{64}\z")]


class WorkingTreeCandidateSnapshot(
    msgspec.Struct,
    tag="working-tree",
    tag_field="candidate_kind",
    frozen=True,
    forbid_unknown_fields=True,
):
    schema: Literal["pinboard-candidate-snapshot/v1"]
    attempt_id: NonEmptyLine
    item_id: NonEmptyLine
    candidate: Annotated[str, msgspec.Meta(pattern=r"\Aworking-tree-sha256:[0-9a-f]{64}\z")]
    branch: NonEmptyLine
    preimage_revision: NonEmptyLine
    accepted_base_revision: NonEmptyLine
    recorded_at: NonEmptyLine
    diff: bytes

    def __post_init__(self) -> None:
        if self.candidate != f"working-tree-sha256:{hashlib.sha256(self.diff).hexdigest()}":
            raise ValueError("working-tree candidate identity must match the binary diff")


class CommitCandidateSnapshot(
    msgspec.Struct,
    tag="commit",
    tag_field="candidate_kind",
    frozen=True,
    forbid_unknown_fields=True,
):
    schema: Literal["pinboard-candidate-snapshot/v1"]
    attempt_id: NonEmptyLine
    item_id: NonEmptyLine
    candidate: Annotated[str, msgspec.Meta(pattern=r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\z")]
    branch: NonEmptyLine
    preimage_revision: NonEmptyLine
    accepted_base_revision: NonEmptyLine
    recorded_at: NonEmptyLine
    diff: bytes


type CandidateSnapshot = WorkingTreeCandidateSnapshot | CommitCandidateSnapshot


class CandidateSnapshotReceiptInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    candidate: NonEmptyLine
    snapshot_artifact_ref_id: int


@dataclass(frozen=True, slots=True)
class CandidateSnapshotEvidence:
    snapshot: CandidateSnapshot
    reference: stored_state.ArtifactReference
    receipt: stored_state.StoredTransitionReceipt


class CandidateSnapshotState(Protocol):
    @property
    def lifecycle(self) -> stored_state.LifecycleRecords: ...

    @property
    def artifact_references(self) -> tuple[stored_state.ArtifactReference, ...]: ...

    @property
    def transition_receipts(self) -> tuple[stored_state.StoredTransitionReceipt, ...]: ...


def canonical_candidate_snapshot_bytes(snapshot: CandidateSnapshot) -> bytes:
    return msgspec.json.encode(snapshot, order="sorted")


def decode_candidate_snapshot(value: bytes) -> CandidateSnapshot:
    decoded = msgspec.json.decode(value, type=CandidateSnapshot, strict=True)
    if isinstance(decoded, WorkingTreeCandidateSnapshot):
        snapshot: CandidateSnapshot = decoded
    elif isinstance(decoded, CommitCandidateSnapshot):
        snapshot = decoded
    else:
        raise TypeError("candidate snapshot decoder returned an unsupported value")
    if canonical_candidate_snapshot_bytes(snapshot) != value:
        raise ValueError("candidate snapshot is not canonical")
    return snapshot


def candidate_snapshot_key(snapshot: CandidateSnapshot) -> str:
    return candidate_snapshot_artifact_key(snapshot.attempt_id, snapshot.candidate, snapshot.recorded_at)


def candidate_snapshot_artifact_key(attempt_id: str, candidate: str, recorded_at: str) -> str:
    identity = b"\0".join(
        (
            attempt_id.encode(),
            candidate.encode(),
            recorded_at.encode(),
        )
    )
    return f"{attempt_id}-candidate-snapshot-{hashlib.sha256(identity).hexdigest()}"


def legacy_review_candidate(receipt: stored_state.StoredTransitionReceipt) -> str | None:
    """Return the exact candidate from a canonical pre-snapshot review receipt."""

    if receipt.action_kind != decision_models.ActionKind.SUBMIT_REVIEW or receipt.input_schema != "decision/v1":
        return None
    outcome = msgspec.json.Decoder(history.TransitionReceiptOutcome, strict=True).decode(bytes(receipt.outcome_payload))
    if (
        bytes(receipt.input_payload) != b"{}"
        or receipt.outcome_schema != "transition-receipt/v1"
        or msgspec.json.encode(outcome, order="sorted") != bytes(receipt.outcome_payload)
        or outcome.outcome != decision_models.ActionKind.SUBMIT_REVIEW.value
        or outcome.candidate is None
    ):
        raise ValueError("The legacy review submission is not canonical or correlated.")
    return outcome.candidate


def verify_candidate_snapshot_context(
    context: query_models.CandidateSnapshotContextFacts,
    candidate: str | None,
    artifact_bytes: bytes,
) -> CandidateSnapshotEvidence:
    """Verify focused persisted facts and immutable bytes for one attempt."""

    receipt = context.receipt
    reference = context.reference
    if receipt.artifact_ref_id is None or receipt.artifact_ref_id != reference.artifact_ref_id:
        raise ValueError("The candidate snapshot receipt has no matching accepted artifact reference.")
    receipt_input = msgspec.json.Decoder(CandidateSnapshotReceiptInput, strict=True).decode(
        bytes(receipt.input_payload)
    )
    if (
        receipt.input_schema != "pinboard-candidate-snapshot/v1"
        or msgspec.json.encode(receipt_input, order="sorted") != bytes(receipt.input_payload)
        or receipt_input.snapshot_artifact_ref_id != int(reference.artifact_ref_id)
    ):
        raise ValueError("The candidate snapshot receipt input is not canonical or correlated.")
    if candidate is not None and receipt_input.candidate != candidate:
        raise ValueError("The latest candidate snapshot does not match the protected candidate.")
    if reference.kind != work_models.ArtifactKind.EVIDENCE:
        raise ValueError("The candidate snapshot reference is not accepted evidence.")
    if (
        len(artifact_bytes) != reference.size_bytes
        or hashlib.sha256(artifact_bytes).hexdigest() != reference.content_sha256
    ):
        raise ValueError("The candidate snapshot bytes differ from the accepted artifact reference.")
    snapshot = decode_candidate_snapshot(artifact_bytes)
    outcome = msgspec.json.Decoder(history.TransitionReceiptOutcome, strict=True).decode(bytes(receipt.outcome_payload))
    if (
        receipt.action_kind != decision_models.ActionKind.SUBMIT_REVIEW
        or receipt.outcome_schema != "transition-receipt/v1"
        or msgspec.json.encode(outcome, order="sorted") != bytes(receipt.outcome_payload)
        or outcome.outcome != decision_models.ActionKind.SUBMIT_REVIEW.value
        or outcome.candidate != receipt_input.candidate
    ):
        raise ValueError("The candidate snapshot receipt outcome is not canonical or correlated.")
    if (
        snapshot.attempt_id != str(context.attempt_id)
        or snapshot.item_id != str(context.item_id)
        or snapshot.candidate != receipt_input.candidate
        or snapshot.recorded_at != receipt.committed_at.isoformat()
    ):
        raise ValueError("The candidate snapshot does not match its attempt and receipt.")
    if context.state == work_models.AttemptState.REVIEW and (
        context.candidate_revision != snapshot.candidate
        or context.candidate_recorded_at != receipt.committed_at
        or context.branch != snapshot.branch
        or context.base_revision != snapshot.accepted_base_revision
    ):
        raise ValueError("The live review attempt does not match its immutable candidate snapshot.")
    return CandidateSnapshotEvidence(snapshot, reference, receipt)


def validate_candidate_snapshot_history(
    state: CandidateSnapshotState,
    artifact_bytes: dict[ArtifactRefId, bytes],
) -> tuple[CandidateSnapshotEvidence, ...]:
    """Verify every retained review snapshot and every live-review correlation."""

    attempts = {value.attempt_id: value for value in state.lifecycle.attempts}
    references = {value.artifact_ref_id: value for value in state.artifact_references}
    verified: list[CandidateSnapshotEvidence] = []
    legacy_review_candidates: set[tuple[AttemptId, datetime, str]] = set()
    for receipt in state.transition_receipts:
        if receipt.action_kind != decision_models.ActionKind.SUBMIT_REVIEW:
            continue
        if (legacy_candidate := legacy_review_candidate(receipt)) is not None:
            legacy_review_candidates.add((AttemptId(str(receipt.subject_id)), receipt.committed_at, legacy_candidate))
            continue
        if receipt.input_schema != "pinboard-candidate-snapshot/v1" or receipt.artifact_ref_id is None:
            raise ValueError("Every review submission must retain one accepted candidate snapshot.")
        attempt_id = AttemptId(str(receipt.subject_id))
        attempt = attempts.get(attempt_id)
        reference = references.get(receipt.artifact_ref_id)
        encoded = artifact_bytes.get(receipt.artifact_ref_id)
        if attempt is None or reference is None or encoded is None:
            raise ValueError("Candidate snapshot history is missing its attempt, reference, or verified bytes.")
        current = attempt.candidate_recorded_at == receipt.committed_at
        context = query_models.CandidateSnapshotContextFacts(
            attempt.attempt_id,
            attempt.item_id,
            attempt.state if current else work_models.AttemptState.ACTIVE,
            attempt.branch,
            attempt.base_revision,
            attempt.candidate_revision if current else None,
            attempt.candidate_recorded_at if current else None,
            receipt,
            reference,
        )
        verified.append(verify_candidate_snapshot_context(context, None, encoded))
    for attempt in state.lifecycle.attempts:
        if (
            attempt.state == work_models.AttemptState.REVIEW
            and (
                attempt.attempt_id,
                attempt.candidate_recorded_at,
                attempt.candidate_revision,
            )
            not in legacy_review_candidates
            and not any(
                evidence.snapshot.attempt_id == str(attempt.attempt_id)
                and evidence.receipt.committed_at == attempt.candidate_recorded_at
                and evidence.snapshot.candidate == attempt.candidate_revision
                for evidence in verified
            )
        ):
            raise ValueError("A live review attempt lacks its exact accepted candidate snapshot.")
    linked = {evidence.reference.artifact_ref_id for evidence in verified}
    if any(
        reference.kind == work_models.ArtifactKind.EVIDENCE
        and "-candidate-snapshot-" in reference.key
        and reference.artifact_ref_id not in linked
        for reference in state.artifact_references
    ):
        raise ValueError("An accepted candidate snapshot is not linked from review history.")
    return tuple(verified)
