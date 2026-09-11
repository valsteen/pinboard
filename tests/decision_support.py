from datetime import datetime

from pinboard.application import stored_state
from pinboard.application.actions import discover_current_actions
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import AttemptId, CandidateId, ItemId, LeaseId, ProposalId
from pinboard.domain.ledger import LedgerSnapshot


def project_inactive_attempt_authority(
    state: stored_state.StoredWorkState,
    attempt_id: AttemptId,
    now: datetime,
) -> DecisionResult[authority_models.InactiveAttemptAuthority]:
    """Select exact retained inactive authority after ordinary interruption recovery."""

    attempt = next((value for value in state.lifecycle.attempts if value.attempt_id == attempt_id), None)
    lease = next((value for value in state.authority.attempt_leases if value.attempt_id == attempt_id), None)
    if attempt is None or lease is None:
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED, "No retained attempt authority exists.", None
        )
    anchor = next(
        (
            value
            for value in state.authority.attempt_generations
            if value.attempt_id == attempt_id and value.generation == lease.generation
        ),
        None,
    )
    if anchor is None:
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            "The retained attempt generation has no exact identity anchor.",
            None,
        )
    if lease.state == authority_models.AttemptLeaseStatus.ACTIVE:
        if lease.expires_at > now:
            return DecisionFailure(
                DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED, "Attempt authority remains live.", None
            )
        status = authority_models.AttemptLeaseStatus.EXPIRED
    elif lease.state in {authority_models.AttemptLeaseStatus.RELEASED, authority_models.AttemptLeaseStatus.REVOKED}:
        status = lease.state
    else:
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED, "Attempt authority is not inactive.", None
        )
    return authority_models.InactiveAttemptAuthority(
        host_epoch=state.lifecycle.project.host_epoch,
        attempt=attempt_id,
        item=attempt.item_id,
        task_id=anchor.task_id,
        host_id=anchor.host_id,
        lease_id=anchor.lease_id,
        generation=anchor.generation,
        expires_at=lease.expires_at,
        state=status,
    )


def _dependency_order(value: stored_state.ItemDependency) -> tuple[str, int]:
    return str(value.item_id), value.position


def _proposal_evidence_order(value: stored_state.ProposalEvidence) -> tuple[str, int]:
    return str(value.proposal_id), value.position


def _proposal_freshness_order(value: stored_state.ProposalFreshness) -> tuple[str, int]:
    return str(value.proposal_id), value.position


def _project_work_item(
    value: stored_state.StoredWorkItem, state: work_models.WorkState, attempt_by_item: dict[ItemId, AttemptId]
) -> work_models.WorkItem:
    return work_models.WorkItem(
        value.item_id,
        state,
        value.timing.value if value.timing is not None else None,
        (),
        attempt_by_item.get(value.item_id),
        value.source,
        value.next_action,
        value.notes,
        value.queue_position,
        value.outcome_evidence,
    )


def project_decision_snapshot(state: stored_state.StoredWorkState, now: datetime) -> LedgerSnapshot:
    """Project complete persisted state into the narrower facts consumed by pure decisions."""

    attempts_by_item = {
        attempt.item_id: attempt.attempt_id
        for attempt in state.lifecycle.attempts
        if attempt.state != work_models.AttemptState.DONE
    }
    live_items = tuple(
        _project_work_item(item, live_state, attempts_by_item)
        for item in state.lifecycle.work_items
        if (live_state := stored_state.live_work_state(item.state)) is not None
    )
    stored_items_by_id = {item.item_id: item for item in state.lifecycle.work_items}
    dependency_groups: dict[ItemId, list[ItemId]] = {item_id: [] for item_id in stored_items_by_id}
    for link in sorted(state.lifecycle.dependencies, key=_dependency_order):
        dependency_groups[link.item_id].append(link.dependency_id)
    dependencies_by_item = {item_id: tuple(values) for item_id, values in dependency_groups.items()}
    latest_definitions: dict[ItemId, stored_state.ItemDefinitionRevision] = {}
    for revision in state.lifecycle.definition_revisions:
        latest_definitions[revision.item_id] = revision
    definitions = tuple(
        work_models.DefinitionAnchor(value.item_id, value.revision, value.digest, value.definition)
        for value in latest_definitions.values()
    )
    work_items = tuple(
        work_models.WorkItem(
            item.item,
            item.state,
            item.timing,
            dependencies_by_item[item.item],
            item.attempt,
            item.source,
            item.next_action,
            item.notes,
            item.queue_position,
            item.outcome_evidence,
        )
        for item in live_items
    )

    attempt_by_id = {attempt.attempt_id: attempt for attempt in state.lifecycle.attempts}
    attempt_anchors = {(anchor.attempt_id, anchor.generation): anchor for anchor in state.authority.attempt_generations}
    attempt_authorities = tuple(
        work_models.AttemptAuthority(
            lease.attempt_id,
            attempt_by_id[lease.attempt_id].item_id,
            attempt_anchors[(lease.attempt_id, lease.generation)].lease_id
            if lease.state == authority_models.AttemptLeaseStatus.ACTIVE
            else None,
            lease.generation,
        )
        for lease in state.authority.attempt_leases
    )
    command_attempt_authorities = tuple(
        work_models.CommandAttemptAuthority(
            host_epoch=state.lifecycle.project.host_epoch,
            item=attempt_by_id[lease.attempt_id].item_id,
            item_subject_revision=str(stored_items_by_id[attempt_by_id[lease.attempt_id].item_id].subject_revision),
            attempt=lease.attempt_id,
            attempt_subject_revision=str(attempt_by_id[lease.attempt_id].subject_revision),
            task_id=anchor.task_id,
            host_id=anchor.host_id,
            lease_id=anchor.lease_id,
            generation=lease.generation,
            expires_at=lease.expires_at,
        )
        for lease in state.authority.attempt_leases
        if lease.state == authority_models.AttemptLeaseStatus.ACTIVE and lease.expires_at > now
        for anchor in (attempt_anchors[(lease.attempt_id, lease.generation)],)
    )
    preparation_anchors = {
        (anchor.item_id, anchor.generation): anchor for anchor in state.authority.preparation_generations
    }
    preparation_authorities = tuple(
        work_models.PreparationAuthority(
            lease.item_id,
            lease.definition_revision,
            lease.definition_digest,
            preparation_anchors[(lease.item_id, lease.generation)].lease_id
            if lease.state == authority_models.PreparationLeaseStatus.ACTIVE and lease.expires_at > now
            else None,
            lease.generation,
        )
        for lease in state.authority.preparation_leases
    )
    command_preparation_authorities = tuple(
        work_models.PreparationCommandAuthority(
            host_epoch=state.lifecycle.project.host_epoch,
            item=lease.item_id,
            definition_revision=lease.definition_revision,
            definition_digest=lease.definition_digest,
            task_id=anchor.task_id,
            host_id=anchor.host_id,
            lease_id=anchor.lease_id,
            generation=lease.generation,
            expires_at=lease.expires_at,
        )
        for lease in state.authority.preparation_leases
        if lease.state == authority_models.PreparationLeaseStatus.ACTIVE and lease.expires_at > now
        for anchor in (preparation_anchors[(lease.item_id, lease.generation)],)
    )
    proposal_ids = tuple(proposal.proposal_id for proposal in state.proposals.proposals)
    evidence_groups: dict[ProposalId, list[str]] = {proposal_id: [] for proposal_id in proposal_ids}
    for evidence in sorted(state.proposals.evidence, key=_proposal_evidence_order):
        evidence_groups[evidence.proposal_id].append(evidence.selector)
    evidence_by_proposal = {proposal_id: tuple(values) for proposal_id, values in evidence_groups.items()}
    freshness_groups: dict[ProposalId, list[str]] = {proposal_id: [] for proposal_id in proposal_ids}
    for freshness in sorted(state.proposals.freshness, key=_proposal_freshness_order):
        freshness_groups[freshness.proposal_id].append(freshness.assumption)
    freshness_by_proposal = {proposal_id: tuple(values) for proposal_id, values in freshness_groups.items()}

    return LedgerSnapshot(
        revision=str(state.lifecycle.project.revision),
        items=work_items,
        attempts=tuple(
            work_models.AttemptRecord(
                attempt.attempt_id,
                attempt.item_id,
                attempt.state,
                attempt.accepted_scope_revision,
                attempt.accepted_scope_digest,
                CandidateId(attempt.candidate_revision) if attempt.candidate_revision is not None else None,
                attempt.brief_artifact_ref_id,
            )
            for attempt in state.lifecycle.attempts
        ),
        artifacts=tuple(
            work_models.ArtifactRecord(artifact.artifact_ref_id, artifact.kind)
            for artifact in state.artifact_references
        ),
        proposals=tuple(
            work_models.ProposalRecord(
                proposal.proposal_id,
                str(proposal.subject_revision),
                proposal.created_at,
                proposal.source_task_id,
                proposal.user_label,
                proposal.trigger,
                proposal.why_it_matters,
                proposal.relation,
                proposal.effect,
                proposal.unlock,
                proposal.urgency_evidence,
                evidence_by_proposal[proposal.proposal_id],
                freshness_by_proposal[proposal.proposal_id],
            )
            for proposal in state.proposals.proposals
            if proposal.disposition is None
        ),
        subject_revisions=tuple(
            work_models.SubjectRevision(item.item_id, str(item.subject_revision)) for item in state.lifecycle.work_items
        )
        + tuple(
            work_models.SubjectRevision(attempt.attempt_id, str(attempt.subject_revision))
            for attempt in state.lifecycle.attempts
        )
        + tuple(
            work_models.SubjectRevision(proposal.proposal_id, str(proposal.subject_revision))
            for proposal in state.proposals.proposals
        ),
        attempt_authorities=attempt_authorities,
        command_attempt_authorities=command_attempt_authorities,
        preparation_authorities=preparation_authorities,
        command_preparation_authorities=command_preparation_authorities,
        history_items=tuple(
            item.item_id for item in state.lifecycle.work_items if stored_state.live_work_state(item.state) is None
        ),
        definitions=definitions,
        host_epoch=state.lifecycle.project.host_epoch,
        planned_replacements=tuple(
            work_models.PlannedReplacement(
                value.affected_item_id,
                value.relation_revision,
                value.replacement_item_id,
                value.replacement_cost,
                value.status,
                value.recorded_by,
                value.recorded_at,
            )
            for value in state.replacements.planned_replacements
        ),
        replacement_dispositions=tuple(
            work_models.ReplacementDisposition(
                value.affected_item_id,
                value.relation_revision,
                value.rationale,
                value.accepted_cost,
                value.recorded_by,
                value.recorded_at,
            )
            for value in state.replacements.dispositions
        ),
    )


def discover_actions(
    state: stored_state.StoredWorkState,
    role: decision_models.Role,
    *,
    lease_id: LeaseId | None = None,
    generation: int | None = None,
    now: datetime,
) -> DecisionResult[tuple[decision_models.Action, ...]]:
    """Project a complete fixture and discover its currently legal actions."""

    return discover_current_actions(
        project_decision_snapshot(state, now),
        role,
        lease_id=lease_id,
        generation=generation,
    )
