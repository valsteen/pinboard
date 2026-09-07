"""Strict portable handover models and pure projection from one stored snapshot."""

from enum import Enum
from pathlib import PurePosixPath
from typing import Literal, assert_never

import msgspec

from pinboard.application import query_models, stored_state
from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import ProposalId


class ContentEncoding(Enum):
    UTF8 = "utf-8"
    BASE64 = "base64"


class HandoverProject(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    application: Literal["pinboard"]
    schema_version: Literal[5]
    created_at: str
    updated_at: str


class HandoverWorkItem(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    state: stored_state.StoredWorkItemState
    timing: work_models.Timing | None
    source: str | None
    outcome_evidence: str | None
    next_action: str | None
    notes: str | None
    subject_revision: int
    recorded_at: str
    updated_at: str
    queue_position: int | None


class HandoverDefinitionRevision(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    revision: int
    digest: str
    definition: query_models.WorkItemDefinitionView
    reason: str
    source_task_id: str
    before_digest: str | None
    after_digest: str
    accepted_project_revision: int
    accepted_at: str


class HandoverDependency(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    dependency_id: str
    position: int


class HandoverAttempt(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: str
    item_id: str
    state: work_models.AttemptState
    branch: str
    base_revision: str
    provenance: str
    brief_artifact_ref_id: int
    result_artifact_ref_id: int | None
    candidate_revision: str | None
    candidate_recorded_at: str | None
    accepted_scope_revision: int
    accepted_scope_digest: str
    subject_revision: int
    recorded_at: str
    updated_at: str


class HandoverProposal(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    proposal_id: str
    created_at: str
    recorded_at: str
    source_task_id: str
    user_label: str
    trigger: str
    why_it_matters: str
    effect: str
    unlock: str
    urgency_evidence: str
    subject_revision: int
    evidence: tuple[str, ...]
    freshness: tuple[str, ...]


class IndependentProposalRelation(
    msgspec.Struct,
    tag="independent",
    tag_field="kind",
    frozen=True,
    forbid_unknown_fields=True,
):
    proposal_id: str


class PrerequisiteProposalRelation(
    msgspec.Struct,
    tag="prerequisite",
    tag_field="kind",
    frozen=True,
    forbid_unknown_fields=True,
):
    proposal_id: str
    target_item_id: str


class FollowUpProposalRelation(
    msgspec.Struct,
    tag="follow-up",
    tag_field="kind",
    frozen=True,
    forbid_unknown_fields=True,
):
    proposal_id: str
    target_item_id: str


class DuplicateProposalRelation(
    msgspec.Struct,
    tag="duplicate",
    tag_field="kind",
    frozen=True,
    forbid_unknown_fields=True,
):
    proposal_id: str
    target_item_id: str


class ContradictionProposalRelation(
    msgspec.Struct,
    tag="contradiction",
    tag_field="kind",
    frozen=True,
    forbid_unknown_fields=True,
):
    proposal_id: str
    target_item_id: str


class ClarificationProposalRelation(
    msgspec.Struct,
    tag="clarification",
    tag_field="kind",
    frozen=True,
    forbid_unknown_fields=True,
):
    proposal_id: str


type HandoverProposalRelation = (
    IndependentProposalRelation
    | PrerequisiteProposalRelation
    | FollowUpProposalRelation
    | DuplicateProposalRelation
    | ContradictionProposalRelation
    | ClarificationProposalRelation
)


class HandoverTransition(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: int
    project_revision: int
    action_id: str
    action_kind: decision_models.ActionKind
    subject_id: str
    artifact_ref_id: int | None
    authorization: decision_models.AuthorizationKind
    actor_task_id: str | None
    actor_host_id: str | None
    input_schema: str
    input: msgspec.Raw
    outcome_schema: str
    outcome: msgspec.Raw
    committed_at: str


class HandoverItemArtifactLink(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    artifact_ref_id: int
    role: work_models.ArtifactKind
    position: int


class HandoverArtifactReference(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    artifact_ref_id: int
    logical_name: str
    revision: int
    kind: work_models.ArtifactKind
    selector: str
    filename: str
    media_type: str
    content_sha256: str
    size_bytes: int
    accepted_revision: int
    created_at: str


class HandoverArtifactContent(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    artifact_ref_id: int
    encoding: ContentEncoding
    content: str


class ProjectHandover(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-project-handover/v2"]
    authority: Literal["sqlite-v5"]
    revision: int
    project: HandoverProject
    work_items: tuple[HandoverWorkItem, ...]
    definition_revisions: tuple[HandoverDefinitionRevision, ...]
    dependencies: tuple[HandoverDependency, ...]
    attempts: tuple[HandoverAttempt, ...]
    proposals: tuple[HandoverProposal, ...]
    proposal_relations: tuple[HandoverProposalRelation, ...]
    transitions: tuple[HandoverTransition, ...]
    item_artifact_links: tuple[HandoverItemArtifactLink, ...]
    artifact_references: tuple[HandoverArtifactReference, ...]
    artifact_contents: tuple[HandoverArtifactContent, ...]


def project_artifact_reference(
    reference: stored_state.ArtifactReference,
    *,
    media_type: str,
) -> HandoverArtifactReference:
    return HandoverArtifactReference(
        int(reference.artifact_ref_id),
        reference.key,
        reference.revision,
        reference.kind,
        reference.selector,
        PurePosixPath(reference.selector).name,
        media_type,
        reference.content_sha256,
        reference.size_bytes,
        reference.accepted_revision,
        reference.created_at.isoformat(),
    )


def _project_definition(value: work_models.WorkItemDefinition) -> query_models.WorkItemDefinitionView:
    return query_models.WorkItemDefinitionView(
        "pinboard-work-item-definition/v1",
        value.title,
        value.objective,
        value.hypothesis,
        value.evidence,
        value.scope,
        value.non_scope,
        value.acceptance_criteria,
        tuple(value.dependencies),
        value.effect,
        value.unlock,
    )


def _project_proposal_relation(value: stored_state.StoredProposal) -> HandoverProposalRelation:
    proposal_id = str(value.proposal_id)
    match value.relation:
        case work_models.IndependentProposalRelation():
            return IndependentProposalRelation(proposal_id)
        case work_models.PrerequisiteProposalRelation(item=item):
            return PrerequisiteProposalRelation(proposal_id, str(item))
        case work_models.FollowUpProposalRelation(item=item):
            return FollowUpProposalRelation(proposal_id, str(item))
        case work_models.DuplicateProposalRelation(item=item):
            return DuplicateProposalRelation(proposal_id, str(item))
        case work_models.ContradictionProposalRelation(item=item):
            return ContradictionProposalRelation(proposal_id, str(item))
        case work_models.ClarificationProposalRelation():
            return ClarificationProposalRelation(proposal_id)
        case _ as unreachable:
            assert_never(unreachable)


def _project_item_artifact_links(state: stored_state.StoredWorkState) -> tuple[HandoverItemArtifactLink, ...]:
    attempts = {str(value.attempt_id): value for value in state.lifecycle.attempts}
    item_ids = {str(value.item_id) for value in state.lifecycle.work_items}
    references = {value.artifact_ref_id: value for value in state.artifact_references}
    links: list[tuple[str, int, work_models.ArtifactKind]] = []
    for attempt in state.lifecycle.attempts:
        links.append((str(attempt.item_id), int(attempt.brief_artifact_ref_id), work_models.ArtifactKind.BRIEF))
        if attempt.result_artifact_ref_id is not None:
            links.append((str(attempt.item_id), int(attempt.result_artifact_ref_id), work_models.ArtifactKind.RESULT))
    for transition in state.transition_receipts:
        if transition.artifact_ref_id is None:
            continue
        subject = str(transition.subject_id)
        item_id = str(attempts[subject].item_id) if subject in attempts else subject if subject in item_ids else None
        if item_id is not None:
            links.append((item_id, int(transition.artifact_ref_id), references[transition.artifact_ref_id].kind))

    positions: dict[tuple[str, work_models.ArtifactKind], int] = {}
    unique: list[HandoverItemArtifactLink] = []
    for item_id, artifact_ref_id, role in dict.fromkeys(links):
        key = item_id, role
        position = positions.get(key, 0)
        unique.append(HandoverItemArtifactLink(item_id, artifact_ref_id, role, position))
        positions[key] = position + 1
    return tuple(unique)


def project_handover_from_state(
    state: stored_state.StoredWorkState,
    artifact_references: tuple[HandoverArtifactReference, ...],
    artifact_contents: tuple[HandoverArtifactContent, ...],
) -> ProjectHandover:
    """Project one already-loaded stored snapshot without outer effects."""

    pending_proposals = tuple(value for value in state.proposals.proposals if value.disposition is None)
    proposal_ids = frozenset(value.proposal_id for value in pending_proposals)
    proposal_evidence_groups: dict[ProposalId, list[str]] = {proposal_id: [] for proposal_id in proposal_ids}
    for evidence in state.proposals.evidence:
        if evidence.proposal_id in proposal_evidence_groups:
            proposal_evidence_groups[evidence.proposal_id].append(evidence.selector)
    proposal_evidence = {proposal_id: tuple(evidence) for proposal_id, evidence in proposal_evidence_groups.items()}
    proposal_freshness_groups: dict[ProposalId, list[str]] = {proposal_id: [] for proposal_id in proposal_ids}
    for freshness in state.proposals.freshness:
        if freshness.proposal_id in proposal_freshness_groups:
            proposal_freshness_groups[freshness.proposal_id].append(freshness.assumption)
    proposal_freshness = {
        proposal_id: tuple(assumptions) for proposal_id, assumptions in proposal_freshness_groups.items()
    }
    return ProjectHandover(
        "pinboard-project-handover/v2",
        "sqlite-v5",
        state.lifecycle.project.revision,
        HandoverProject(
            state.lifecycle.project.application,
            state.lifecycle.project.schema_version,
            state.lifecycle.project.created_at.isoformat(),
            state.lifecycle.project.updated_at.isoformat(),
        ),
        tuple(
            HandoverWorkItem(
                str(value.item_id),
                value.state,
                value.timing,
                value.source,
                value.outcome_evidence,
                value.next_action,
                value.notes,
                value.subject_revision,
                value.recorded_at.isoformat(),
                value.updated_at.isoformat(),
                value.queue_position,
            )
            for value in state.lifecycle.work_items
        ),
        tuple(
            HandoverDefinitionRevision(
                str(value.item_id),
                value.revision,
                value.digest,
                _project_definition(value.definition),
                value.reason,
                str(value.source_task_id),
                value.before_digest,
                value.after_digest,
                value.accepted_project_revision,
                value.accepted_at.isoformat(),
            )
            for value in state.lifecycle.definition_revisions
        ),
        tuple(
            HandoverDependency(str(value.item_id), str(value.dependency_id), value.position)
            for value in state.lifecycle.dependencies
        ),
        tuple(
            HandoverAttempt(
                str(value.attempt_id),
                str(value.item_id),
                value.state,
                value.branch,
                value.base_revision,
                value.provenance,
                int(value.brief_artifact_ref_id),
                None if value.result_artifact_ref_id is None else int(value.result_artifact_ref_id),
                value.candidate_revision,
                None if value.candidate_recorded_at is None else value.candidate_recorded_at.isoformat(),
                value.accepted_scope_revision,
                value.accepted_scope_digest,
                value.subject_revision,
                value.recorded_at.isoformat(),
                value.updated_at.isoformat(),
            )
            for value in state.lifecycle.attempts
        ),
        tuple(
            HandoverProposal(
                str(value.proposal_id),
                value.created_at.isoformat(),
                value.recorded_at.isoformat(),
                str(value.source_task_id),
                value.user_label,
                value.trigger,
                value.why_it_matters,
                value.effect,
                value.unlock,
                value.urgency_evidence,
                value.subject_revision,
                proposal_evidence[value.proposal_id],
                proposal_freshness[value.proposal_id],
            )
            for value in pending_proposals
        ),
        tuple(_project_proposal_relation(value) for value in pending_proposals),
        tuple(
            HandoverTransition(
                int(value.history_id),
                value.project_revision,
                str(value.action_id),
                value.action_kind,
                str(value.subject_id),
                None if value.artifact_ref_id is None else int(value.artifact_ref_id),
                value.authorization,
                None if value.actor_task_id is None else str(value.actor_task_id),
                None if value.actor_host_id is None else str(value.actor_host_id),
                value.input_schema,
                msgspec.Raw(bytes(value.input_payload)),
                value.outcome_schema,
                msgspec.Raw(bytes(value.outcome_payload)),
                value.committed_at.isoformat(),
            )
            for value in state.transition_receipts
        ),
        _project_item_artifact_links(state),
        artifact_references,
        artifact_contents,
    )
