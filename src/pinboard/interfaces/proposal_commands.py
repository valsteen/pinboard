"""Create one proposal from its installed boundary representation.

This outer command owner reads and decodes the candidate, performs the explicit
boundary-to-domain conversion, opens the concrete store, invokes the proposal
use case, and refreshes its affected views. Expected proposal rejections are
returned as values; infrastructure failures remain exceptions.
"""

import sys
from datetime import UTC, datetime
from typing import assert_never

from pinboard.adapters.files import models as file_models
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import service
from pinboard.domain import proposal_models as domain_proposal_models
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import ItemId, ProposalId, TaskId
from pinboard.interfaces import cli_commands, proposal_models, proposals, work_views
from pinboard.interfaces.errors import ProposalFailure, ProposalResult


def _convert_proposal_relation(value: proposal_models.ProposalRelation) -> work_models.ProposalRelation:
    match value:
        case proposal_models.IndependentProposalRelation():
            return work_models.IndependentProposalRelation()
        case proposal_models.PrerequisiteProposalRelation(item=item):
            return work_models.PrerequisiteProposalRelation(ItemId(item))
        case proposal_models.FollowUpProposalRelation(item=item):
            return work_models.FollowUpProposalRelation(ItemId(item))
        case proposal_models.DuplicateProposalRelation(item=item):
            return work_models.DuplicateProposalRelation(ItemId(item))
        case proposal_models.ContradictionProposalRelation(item=item):
            return work_models.ContradictionProposalRelation(ItemId(item))
        case proposal_models.ClarificationProposalRelation():
            return work_models.ClarificationProposalRelation()
        case _ as unreachable:
            assert_never(unreachable)


def create_proposal(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.ProposalCommand,
) -> ProposalResult[int]:
    proposal_path = command.file
    try:
        encoded_proposal = proposal_path.read_bytes()
    except OSError as error:
        return ProposalFailure(
            DecisionFailureCode.PROPOSAL_INVALID,
            f"Cannot read proposal at '{proposal_path}': {error}",
        )
    decoded_proposal = proposals.parse_proposal(encoded_proposal)
    if isinstance(decoded_proposal, ProposalFailure):
        return decoded_proposal
    requested_intake = domain_proposal_models.ProposalIntake(
        ProposalId(decoded_proposal.proposal_id),
        decoded_proposal.created_at_utc(),
        TaskId(decoded_proposal.source_task_id),
        decoded_proposal.user_label,
        decoded_proposal.trigger,
        decoded_proposal.why_it_matters,
        decoded_proposal.effect,
        decoded_proposal.unlock,
        _convert_proposal_relation(decoded_proposal.relation),
        decoded_proposal.urgency_evidence,
        decoded_proposal.evidence,
        decoded_proposal.freshness_assumptions,
        decoded_proposal.position,
    )
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    creation_result = service.create_proposal(
        store,
        domain_proposal_models.CreateProposalOperation(requested_intake),
        datetime.now(UTC),
        actor_task_id=command.task_id,
        actor_host_id=command.host_id,
    )
    if isinstance(creation_result, DecisionFailure):
        return ProposalFailure(creation_result.code, creation_result.message, creation_result.details)
    committed_state = store.snapshot()
    changed_items = [ItemId(decoded_proposal.proposal_id)]
    if isinstance(decoded_proposal.relation, proposal_models.PrerequisiteProposalRelation):
        changed_items.append(ItemId(decoded_proposal.relation.item))
    view_result = work_views.refresh(
        roots,
        store,
        file_models.AffectedViews(queue=True, history=True, items=tuple(changed_items)),
        datetime.now(UTC),
    )
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    intake_item = next(
        value for value in committed_state.lifecycle.work_items if str(value.item_id) == decoded_proposal.proposal_id
    )
    print(
        f"OK PROPOSAL_CREATED {decoded_proposal.proposal_id} "
        f"position={intake_item.queue_position} state={intake_item.state.value}"
    )
    return 0
