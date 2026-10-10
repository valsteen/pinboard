"""Exact, non-executable certificates for terminal historical evidence.

Certificates supplement retained readers. Their accepted source references,
receipt bodies, definition bindings and readable facts must agree independently;
current candidate snapshots retain their ordinary validation. This owner performs
no publication, persistence, lifecycle operation or candidate restoration.
"""

import hashlib
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Literal, assert_never

import msgspec

from pinboard.application import (
    candidate_snapshots,
    checkpoint_compatibility_models,
    stored_state,
    work_brief_models,
    work_briefs,
)
from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import ArtifactRefId, AttemptId

type Sha256 = Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{64}\z")]
type Positive = Annotated[int, msgspec.Meta(gt=0)]
type Identity = Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]


class ArchiveSource(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    artifact_ref_id: Positive
    kind: work_models.ArtifactKind
    key: Identity
    revision: Positive
    selector: str
    content_sha256: Sha256
    size_bytes: Annotated[int, msgspec.Meta(ge=0)]
    accepted_revision: Positive
    created_at: str


class ArchiveReceipt(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: Positive
    content_sha256: Sha256


class ArchiveDefinition(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: Positive
    digest: Sha256


class ArchiveBrief(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    artifact_ref_id: Positive
    markdown: Annotated[str, msgspec.Meta(min_length=1)]


class UnavailableCandidate(
    msgspec.Struct, tag="unavailable", tag_field="assurance", frozen=True, forbid_unknown_fields=True
):
    candidate: str


class PatchCandidate(msgspec.Struct, tag="patch-only", tag_field="assurance", frozen=True, forbid_unknown_fields=True):
    candidate: str
    artifact_ref_id: Positive


class CompleteCandidate(
    msgspec.Struct, tag="complete-state", tag_field="assurance", frozen=True, forbid_unknown_fields=True
):
    candidate: str
    artifact_ref_id: Positive
    branch: str
    preimage_revision: str
    accepted_base_revision: str
    diff_sha256: Sha256


type ArchiveCandidate = UnavailableCandidate | PatchCandidate | CompleteCandidate


class ArchiveCheckpoint(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: Positive
    package_artifact_ref_id: Positive
    checkpoint_id: Identity
    checkpoint_sha256: Sha256
    accepted_scope_revision: Positive
    accepted_scope_digest: Sha256
    candidate: ArchiveCandidate
    acceptance_evidence: str


class ArchiveCompletion(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: Positive
    package_artifact_ref_id: Positive
    candidate: str
    accepted_scope_revision: Positive
    accepted_scope_digest: Sha256
    checkpoint_history_ids: tuple[Positive, ...]
    outcome_evidence: str


class HistoryArchive(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-history-archive/v1"]
    attempt_id: Identity
    item_id: Identity
    attempt_sha256: Sha256
    sources: tuple[ArchiveSource, ...]
    receipts: tuple[ArchiveReceipt, ...]
    definitions: tuple[ArchiveDefinition, ...]
    briefs: tuple[ArchiveBrief, ...]
    checkpoints: tuple[ArchiveCheckpoint, ...]
    completions: tuple[ArchiveCompletion, ...]

    def __post_init__(self) -> None:
        for label, identities in (
            ("sources", tuple(value.artifact_ref_id for value in self.sources)),
            ("receipts", tuple(value.history_id for value in self.receipts)),
            ("definitions", tuple(value.revision for value in self.definitions)),
            ("briefs", tuple(value.artifact_ref_id for value in self.briefs)),
            ("checkpoints", tuple(value.history_id for value in self.checkpoints)),
            ("completions", tuple(value.history_id for value in self.completions)),
        ):
            if not identities or identities != tuple(sorted(set(identities))):
                if label in ("checkpoints", "completions") and not identities:
                    continue
                raise ValueError(f"Archive {label} must have sorted unique identities.")
        source_ids = {value.artifact_ref_id for value in self.sources}
        receipt_ids = {value.history_id for value in self.receipts}
        if any(value.artifact_ref_id not in source_ids for value in self.briefs):
            raise ValueError("Archive briefs must bind original source references.")
        for value in (*self.checkpoints, *self.completions):
            if value.history_id not in receipt_ids or value.package_artifact_ref_id not in source_ids:
                raise ValueError("Archived acceptance must bind its original receipt and package.")


def archive_key(attempt_id: str) -> str:
    return f"{attempt_id}-history-archive"


def canonical_archive_bytes(archive: HistoryArchive) -> bytes:
    return msgspec.json.encode(archive, order="sorted") + b"\n"


def _failure(message: str) -> work_brief_models.WorkBriefFailure:
    return work_brief_models.WorkBriefFailure(work_brief_models.WorkBriefErrorCode.PACKAGE_PROVENANCE_INVALID, message)


def decode_archive(data: bytes) -> work_brief_models.WorkBriefResult[HistoryArchive]:
    try:
        archive = msgspec.json.decode(data, type=HistoryArchive)
    except (msgspec.DecodeError, ValueError) as error:
        return _failure(f"Historical archive is invalid: {error}")
    if canonical_archive_bytes(archive) != data:
        return _failure("Historical archive must use its canonical encoding.")
    return archive


def _digest(value: stored_state.StoredAttempt | stored_state.StoredTransitionReceipt) -> str:
    return hashlib.sha256(msgspec.json.encode(value, order="sorted")).hexdigest()


def _candidate(
    package: work_briefs.CheckpointPackage,
    references: Mapping[tuple[str, str, int], stored_state.ArtifactReference],
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> ArchiveCandidate:
    match package:
        case work_brief_models.CheckpointReviewPackageV3():
            identity = package.candidate_snapshot
            reference = references[(identity.kind, identity.key, identity.revision)]
            snapshot = candidate_snapshots.decode_candidate_snapshot(artifact_bytes[reference.artifact_ref_id])
            if isinstance(
                snapshot, candidate_snapshots.candidate_snapshot_compatibility_models.WorkingTreeCandidateSnapshot
            ):
                raise ValueError("Current checkpoint evidence cannot lose complete-state assurance.")
            return CompleteCandidate(
                package.candidate,
                int(reference.artifact_ref_id),
                snapshot.branch,
                snapshot.preimage_revision,
                snapshot.accepted_base_revision,
                hashlib.sha256(snapshot.diff).hexdigest(),
            )
        case checkpoint_compatibility_models.CheckpointReviewPackageV2():
            identity = package.candidate_snapshot
            reference = references[(identity.kind, identity.key, identity.revision)]
            return PatchCandidate(package.candidate, int(reference.artifact_ref_id))
        case checkpoint_compatibility_models.CheckpointReviewPackage():
            reference = references.get(
                (work_models.ArtifactKind.EVIDENCE.value, f"{package.attempt_id}-{package.checkpoint.id}-candidate", 1)
            )
            return (
                UnavailableCandidate(package.candidate)
                if reference is None
                else PatchCandidate(package.candidate, int(reference.artifact_ref_id))
            )
        case _ as unreachable:
            assert_never(unreachable)


def derive_archive(  # noqa: C901, PLR0912 - one complete original-history projection
    facts: stored_state.ArchiveHistoryFacts,
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> work_brief_models.WorkBriefResult[HistoryArchive]:
    """Derive archival facts from original bytes; retained readers still certify semantics."""

    attempt = facts.attempt
    if attempt.state != work_models.AttemptState.DONE:
        return _failure("Historical archives cannot bind a nonterminal attempt or authorize execution.")
    references = {(value.kind.value, value.key, value.revision): value for value in facts.artifact_references}
    references_by_id = {value.artifact_ref_id: value for value in facts.artifact_references}
    sources: list[ArchiveSource] = []
    briefs: list[ArchiveBrief] = []
    checkpoints: list[ArchiveCheckpoint] = []
    completions: list[ArchiveCompletion] = []
    for reference in facts.artifact_references:
        data = artifact_bytes.get(reference.artifact_ref_id)
        if (
            data is None
            or len(data) != reference.size_bytes
            or hashlib.sha256(data).hexdigest() != reference.content_sha256
        ):
            return _failure(
                f"Archive source {int(reference.artifact_ref_id)} is missing or differs from its accepted reference."
            )
        sources.append(
            ArchiveSource(
                int(reference.artifact_ref_id),
                reference.kind,
                reference.key,
                reference.revision,
                reference.selector,
                reference.content_sha256,
                reference.size_bytes,
                reference.accepted_revision,
                reference.created_at.isoformat(),
            )
        )
        if reference.kind == work_models.ArtifactKind.BRIEF and reference.selector.endswith(".md"):
            briefs.append(ArchiveBrief(int(reference.artifact_ref_id), data.decode("utf-8")))
        elif reference.kind == work_models.ArtifactKind.BRIEF:
            brief = work_briefs.decode_canonical_work_brief(data)
            if isinstance(brief, work_brief_models.WorkBriefFailure):
                return brief
            if (brief.attempt_id, brief.item_id) != (str(attempt.attempt_id), str(attempt.item_id)):
                return _failure("Archived brief does not belong to its terminal attempt.")
            briefs.append(
                ArchiveBrief(int(reference.artifact_ref_id), work_briefs.render_work_brief_markdown(brief).decode())
            )
    for receipt in facts.transition_receipts:
        if receipt.outcome_schema not in ("checkpoint-acceptance/v2", "completion-acceptance/v2"):
            continue
        reference = references_by_id.get(receipt.artifact_ref_id) if receipt.artifact_ref_id is not None else None
        if reference is None:
            return _failure("Archived acceptance is missing its original package reference.")
        if receipt.outcome_schema == "checkpoint-acceptance/v2":
            package = work_briefs.decode_canonical_checkpoint_review_package(artifact_bytes[reference.artifact_ref_id])
            if isinstance(package, work_brief_models.WorkBriefFailure):
                return package
            try:
                candidate = _candidate(package, references, artifact_bytes)
            except (KeyError, msgspec.DecodeError, ValueError) as error:
                return _failure(f"Archived candidate closure is invalid: {error}")
            checkpoints.append(
                ArchiveCheckpoint(
                    int(receipt.history_id),
                    int(reference.artifact_ref_id),
                    package.checkpoint.id,
                    package.checkpoint.sha256,
                    package.accepted_scope.revision,
                    package.accepted_scope.digest,
                    candidate,
                    package.acceptance_evidence,
                )
            )
        else:
            completed = work_briefs.decode_canonical_completion_review_package(
                artifact_bytes[reference.artifact_ref_id]
            )
            if isinstance(completed, work_brief_models.WorkBriefFailure):
                return completed
            completions.append(
                ArchiveCompletion(
                    int(receipt.history_id),
                    int(reference.artifact_ref_id),
                    completed.candidate,
                    completed.accepted_scope.revision,
                    completed.accepted_scope.digest,
                    tuple(row.history_id for row in completed.checkpoint_coverage),
                    completed.outcome_evidence,
                )
            )
    try:
        return HistoryArchive(
            "pinboard-history-archive/v1",
            str(attempt.attempt_id),
            str(attempt.item_id),
            _digest(attempt),
            tuple(value for _, value in sorted((value.artifact_ref_id, value) for value in sources)),
            tuple(
                ArchiveReceipt(int(value.history_id), _digest(value))
                for value in (
                    value for _, value in sorted((int(value.history_id), value) for value in facts.transition_receipts)
                )
            ),
            tuple(
                ArchiveDefinition(value.revision, value.digest)
                for value in (
                    value for _, value in sorted((value.revision, value) for value in facts.definition_revisions)
                )
            ),
            tuple(value for _, value in sorted((value.artifact_ref_id, value) for value in briefs)),
            tuple(checkpoints),
            tuple(completions),
        )
    except ValueError as error:
        return _failure(f"Historical archive closure is invalid: {error}")


def _receipt_belongs_to_attempt(receipt: stored_state.StoredTransitionReceipt, attempt_id: str) -> bool:
    if str(receipt.subject_id) != attempt_id:
        return False
    if receipt.input_schema == "attempt-authority/v1":
        return str(receipt.action_id).startswith(f"continue:attempt-authority:{attempt_id}:")
    if receipt.input_schema in ("preparation-authority/v1", "proposal-intake/v1"):
        return False
    return (
        isinstance(receipt.action_kind, decision_models.ActionKind)
        and decision_models.action_semantics(receipt.action_kind).subject_kind
        == decision_models.ActionSubjectKind.ATTEMPT
        and str(receipt.action_id) == f"{receipt.action_kind.value}:{attempt_id}"
    )


def select_archive_facts(
    attempt: stored_state.StoredAttempt,
    references: tuple[stored_state.ArtifactReference, ...],
    receipts: tuple[stored_state.StoredTransitionReceipt, ...],
    definitions: tuple[stored_state.ItemDefinitionRevision, ...],
) -> stored_state.ArchiveHistoryFacts:
    """Select original ownership without letting later sibling publication reassign it."""

    attempt_id = str(attempt.attempt_id)
    selected_receipts = tuple(value for value in receipts if _receipt_belongs_to_attempt(value, attempt_id))
    linked_ids = {
        attempt.brief_artifact_ref_id,
        attempt.result_artifact_ref_id,
        *(value.artifact_ref_id for value in selected_receipts),
    }
    checkpoint_keys = {
        f"{reference.key.removesuffix('-review-package')}-{role}"
        for receipt in selected_receipts
        if receipt.outcome_schema == "checkpoint-acceptance/v2"
        for reference in references
        if reference.artifact_ref_id == receipt.artifact_ref_id
        for role in ("candidate", "result", "review", "review-package")
    }
    sibling_briefs = tuple(
        value
        for value in references
        if value.kind == work_models.ArtifactKind.BRIEF and value.key.startswith(f"{attempt_id}-")
    )
    return stored_state.ArchiveHistoryFacts(
        attempt,
        tuple(
            value
            for value in references
            if (value.key == attempt_id or value.key.startswith(f"{attempt_id}-"))
            and value.key != archive_key(attempt_id)
            and (
                value.artifact_ref_id in linked_ids
                or value.key in checkpoint_keys
                or not any(
                    (value.key == sibling.key or value.key.startswith(f"{sibling.key}-"))
                    and value.accepted_revision >= sibling.accepted_revision
                    for sibling in sibling_briefs
                )
            )
        ),
        selected_receipts,
        tuple(value for value in definitions if value.item_id == attempt.item_id),
    )


def verify_archive(
    reference: stored_state.ArtifactReference,
    data: bytes,
    facts: stored_state.ArchiveHistoryFacts,
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> work_brief_models.WorkBriefResult[HistoryArchive]:
    if (
        (reference.kind, reference.key, reference.revision, reference.selector)
        != (
            work_models.ArtifactKind.EVIDENCE,
            archive_key(str(facts.attempt.attempt_id)),
            1,
            f"artifacts/evidence/{archive_key(str(facts.attempt.attempt_id))}/1.json",
        )
        or len(data) != reference.size_bytes
        or hashlib.sha256(data).hexdigest() != reference.content_sha256
    ):
        return _failure("Historical archive does not match its exact accepted certificate reference.")
    supplied = decode_archive(data)
    if isinstance(supplied, work_brief_models.WorkBriefFailure):
        return supplied
    expected = derive_archive(facts, artifact_bytes)
    if isinstance(expected, work_brief_models.WorkBriefFailure):
        return expected
    if canonical_archive_bytes(supplied) != canonical_archive_bytes(expected):
        return _failure("Historical archive differs from its complete original closure, identities or readable facts.")
    return supplied


def verify_archives(
    state: candidate_snapshots.CandidateSnapshotState,
    artifact_bytes: Mapping[ArtifactRefId, bytes],
) -> work_brief_models.WorkBriefResult[Mapping[AttemptId, HistoryArchive]]:
    archives: dict[AttemptId, HistoryArchive] = {}
    attempts = {str(value.attempt_id): value for value in state.lifecycle.attempts}
    for reference in state.artifact_references:
        if not reference.key.endswith("-history-archive"):
            continue
        attempt = attempts.get(reference.key.removesuffix("-history-archive"))
        if attempt is None or reference.artifact_ref_id not in artifact_bytes:
            return _failure("Accepted historical archive lacks its terminal attempt or verified certificate bytes.")
        facts = select_archive_facts(
            attempt, state.artifact_references, state.transition_receipts, state.lifecycle.definition_revisions
        )
        verified = verify_archive(reference, artifact_bytes[reference.artifact_ref_id], facts, artifact_bytes)
        if isinstance(verified, work_brief_models.WorkBriefFailure):
            return verified
        archives[attempt.attempt_id] = verified
    return MappingProxyType(archives)


def render_archive(archive: HistoryArchive) -> bytes:
    lines = [
        f"# Archived attempt {archive.attempt_id}",
        "",
        f"Item: {archive.item_id}",
        "",
        "This is historical evidence. It cannot authorize execution, restoration, correction or a new ready review.",
        "",
        "## Original accepted checkpoints",
        "",
    ]
    for checkpoint in archive.checkpoints:
        match checkpoint.candidate:
            case UnavailableCandidate():
                assurance = "candidate bytes unavailable"
            case PatchCandidate():
                assurance = "patch-only evidence"
            case CompleteCandidate():
                assurance = "complete-state snapshot"
            case _ as unreachable:
                assert_never(unreachable)
        lines.append(
            f"- History {checkpoint.history_id}: {checkpoint.checkpoint_id}; {checkpoint.candidate.candidate}; {assurance}. {checkpoint.acceptance_evidence}"
        )
    lines.extend(["", "## Original completion evidence", ""])
    lines.extend(
        f"- History {value.history_id}: {value.candidate}; covers {', '.join(map(str, value.checkpoint_history_ids)) or 'no checkpoints'}. {value.outcome_evidence}"
        for value in archive.completions
    )
    for brief in archive.briefs:
        lines.extend(["", f"## Original brief reference {brief.artifact_ref_id}", "", brief.markdown])
    return ("\n".join(lines) + "\n").encode()
