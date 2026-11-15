"""Stored pinboard-close-decision/v1 receipt facts for historical validation and export only.

Released direct close once recorded ``dropped``; current close requests accept only ``DirectCloseOutcome``,
and cancellation is the only current drop route. Retain this record while ledger history keeps those dropped
close receipts, and remove it with an authorized retirement or migration of them.
"""

import msgspec

from pinboard.application.action_models import NonEmptyLine
from pinboard.domain import work_models


class StoredCloseDecisionInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One stored close decision over the full released outcome vocabulary; never a current close request."""

    outcome: work_models.CloseOutcome
    reason: NonEmptyLine
    human_decision: NonEmptyLine
