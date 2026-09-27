from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from pinboard.domain import work_models
from pinboard.domain.identifiers import AttemptId, HistoryId, LeaseId, ProposalId, WorkItemId


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    revision: str
    items: tuple[work_models.WorkItem, ...]
    attempts: tuple[work_models.AttemptRecord, ...] = ()
    artifacts: tuple[work_models.ArtifactRecord, ...] = ()
    proposals: tuple[work_models.ProposalRecord, ...] = ()
    subject_revisions: tuple[work_models.SubjectRevision, ...] = ()
    attempt_authorities: tuple[work_models.AttemptAuthority, ...] = ()
    command_attempt_authorities: tuple[work_models.CommandAttemptAuthority, ...] = ()
    preparation_authorities: tuple[work_models.PreparationAuthority, ...] = ()
    command_preparation_authorities: tuple[work_models.PreparationCommandAuthority, ...] = ()
    history_items: tuple[WorkItemId, ...] = ()
    definitions: tuple[work_models.DefinitionAnchor, ...] = ()
    host_epoch: int = 0
    checkpoint_history_ids: tuple[HistoryId, ...] = ()
    planned_replacements: tuple[work_models.PlannedReplacement, ...] = ()
    replacement_dispositions: tuple[work_models.ReplacementDisposition, ...] = ()

    def work_items_by_id(self) -> Mapping[WorkItemId, work_models.WorkItem]:
        return MappingProxyType({item.work_item_id: item for item in self.items})

    def work_item(self, work_item_id: WorkItemId) -> work_models.WorkItem | None:
        return next((item for item in self.items if item.work_item_id == work_item_id), None)

    def work_item_for_attempt(self, attempt_id: AttemptId) -> work_models.WorkItem | None:
        return next((item for item in self.items if item.attempt == attempt_id), None)

    def definition(self, work_item_id: WorkItemId) -> work_models.DefinitionAnchor | None:
        return next((definition for definition in self.definitions if definition.work_item_id == work_item_id), None)

    def attempts_by_id(self) -> Mapping[AttemptId, work_models.AttemptRecord]:
        return MappingProxyType({attempt.attempt: attempt for attempt in self.attempts})

    def attempt(self, attempt_id: AttemptId) -> work_models.AttemptRecord | None:
        return next((attempt for attempt in self.attempts if attempt.attempt == attempt_id), None)

    def proposal(self, proposal_id: ProposalId) -> work_models.ProposalRecord | None:
        return next((proposal for proposal in self.proposals if proposal.proposal == proposal_id), None)

    def subject_revision(self, subject: WorkItemId | AttemptId | ProposalId) -> str | None:
        return next((value.revision for value in self.subject_revisions if value.subject == subject), None)

    def authority_for(
        self, attempt: AttemptId, lease_id: LeaseId | None, generation: int
    ) -> work_models.AttemptAuthority | None:
        return next(
            (
                authority
                for authority in self.attempt_authorities
                if authority.attempt == attempt
                and authority.lease_id == lease_id
                and authority.generation == generation
            ),
            None,
        )

    def command_attempt_authority(self, attempt_id: AttemptId) -> work_models.CommandAttemptAuthority | None:
        return next((value for value in self.command_attempt_authorities if value.attempt == attempt_id), None)

    def command_preparation_authority(self, work_item_id: WorkItemId) -> work_models.PreparationCommandAuthority | None:
        return next(
            (value for value in self.command_preparation_authorities if value.work_item_id == work_item_id), None
        )

    def preparation_for(
        self, work_item_id: WorkItemId, lease_id: LeaseId | None, generation: int
    ) -> work_models.PreparationAuthority | None:
        return next(
            (
                authority
                for authority in self.preparation_authorities
                if authority.work_item_id == work_item_id
                and authority.lease_id == lease_id
                and authority.generation == generation
            ),
            None,
        )

    def current_replacement(self, work_item_id: WorkItemId) -> work_models.PlannedReplacement | None:
        matching_relations: tuple[work_models.PlannedReplacement, ...] = tuple(
            relation for relation in self.planned_replacements if relation.affected_item == work_item_id
        )
        latest = max(matching_relations, key=work_models.planned_replacement_revision, default=None)
        if latest is None or latest.status != work_models.PlannedReplacementStatus.CURRENT:
            return None
        return latest

    def replacement_disposition(
        self, work_item_id: WorkItemId, relation_revision: int
    ) -> work_models.ReplacementDisposition | None:
        return next(
            (
                disposition
                for disposition in self.replacement_dispositions
                if disposition.affected_item == work_item_id and disposition.relation_revision == relation_revision
            ),
            None,
        )

    def unresolved_replacement(self, work_item_id: WorkItemId) -> work_models.PlannedReplacement | None:
        relation = self.current_replacement(work_item_id)
        if relation is None:
            return None
        disposition = self.replacement_disposition(work_item_id, relation.relation_revision)
        if disposition is not None and disposition.accepted_cost == relation.replacement_cost:
            return None
        return relation
