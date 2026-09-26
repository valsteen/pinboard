"""Read released SQLite v6 proposal and history values no longer produced by current actions.

Retire these bindings only after an explicitly authorized recoverable migration or retirement
of the supported released data.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import ItemId


class HistoricalActionKind(Enum):
    ACCEPT_PROPOSAL = "accept-proposal"
    MARK_READY = "mark-ready"
    RETURN_PROPOSAL = "return-proposal"


def decode_released_v6_action_kind(value: str) -> decision_models.ActionKind | HistoricalActionKind:
    try:
        return decision_models.ActionKind(value)
    except ValueError:
        return HistoricalActionKind(value)


@dataclass(frozen=True, slots=True)
class HistoricalAcceptedProposalDisposition:
    target: ItemId
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
