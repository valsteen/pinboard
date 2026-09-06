from dataclasses import dataclass

from pinboard.domain import work_models
from pinboard.domain.identifiers import AttemptId, ItemId, LeaseId, ProposalId


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
    history_items: tuple[ItemId, ...] = ()
    definitions: tuple[work_models.DefinitionAnchor, ...] = ()
    host_epoch: int = 0

    def items_by_id(self) -> dict[ItemId, work_models.WorkItem]:
        return {item.item: item for item in self.items}

    def item(self, item_id: ItemId) -> work_models.WorkItem | None:
        return next((item for item in self.items if item.item == item_id), None)

    def item_for_attempt(self, attempt_id: AttemptId) -> work_models.WorkItem | None:
        return next((item for item in self.items if item.attempt == attempt_id), None)

    def definition(self, item_id: ItemId) -> work_models.DefinitionAnchor | None:
        return next((definition for definition in self.definitions if definition.item == item_id), None)

    def attempts_by_id(self) -> dict[AttemptId, work_models.AttemptRecord]:
        return {attempt.attempt: attempt for attempt in self.attempts}

    def attempt(self, attempt_id: AttemptId) -> work_models.AttemptRecord | None:
        return next((attempt for attempt in self.attempts if attempt.attempt == attempt_id), None)

    def proposal(self, proposal_id: ProposalId) -> work_models.ProposalRecord | None:
        return next((proposal for proposal in self.proposals if proposal.proposal == proposal_id), None)

    def subject_revision(self, subject: ItemId | AttemptId | ProposalId) -> str | None:
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

    def preparation_for(
        self, item: ItemId, lease_id: LeaseId | None, generation: int
    ) -> work_models.PreparationAuthority | None:
        return next(
            (
                authority
                for authority in self.preparation_authorities
                if authority.item == item and authority.lease_id == lease_id and authority.generation == generation
            ),
            None,
        )
