"""Decode human-authorized amendment supersessions in brief replacement inputs."""

from typing import Annotated

import msgspec

from pinboard.domain import work_models
from pinboard.domain.history import RecordText
from pinboard.domain.identifiers import HistoryId


class AmendmentSupersessionPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: Annotated[int, msgspec.Meta(ge=1)]
    human_decision: RecordText


def supersessions_from_payload(
    supplied: tuple[AmendmentSupersessionPayload, ...],
) -> tuple[work_models.AmendmentSupersession, ...]:
    return tuple(
        work_models.AmendmentSupersession(HistoryId(value.history_id), value.human_decision) for value in supplied
    )
