"""Read released proposal and history values no longer produced by current actions.

The v6 proposal actions and the defer and reopen item actions retired with the deferred state in v13 stay
decodable here so retained receipts keep their history, validation and export meaning. Retire these bindings only
after an explicitly authorized recoverable migration or retirement of the supported released data.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import WorkItemId


class HistoricalActionKind(Enum):
    ACCEPT_PROPOSAL = "accept-proposal"
    MARK_READY = "mark-ready"
    RETURN_PROPOSAL = "return-proposal"
    DEFER = "defer"
    REOPEN = "reopen"


def decode_released_action_kind(value: str) -> decision_models.ActionKind | HistoricalActionKind:
    try:
        return decision_models.ActionKind(value)
    except ValueError:
        return HistoricalActionKind(value)


@dataclass(frozen=True, slots=True)
class HistoricalAcceptedProposalDisposition:
    target: WorkItemId
    disposed_at: datetime
    kind: work_models.ProposalDispositionKind = field(init=False, default=work_models.ProposalDispositionKind.ACCEPTED)


@dataclass(frozen=True, slots=True)
class HistoricalReturnedProposalDisposition:
    reason: str
    disposed_at: datetime
    kind: work_models.ProposalDispositionKind = field(init=False, default=work_models.ProposalDispositionKind.RETURNED)


type StoredProposalDisposition = (
    work_models.ProposalDisposition | HistoricalAcceptedProposalDisposition | HistoricalReturnedProposalDisposition
)
