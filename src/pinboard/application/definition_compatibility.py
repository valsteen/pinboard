"""Strict original v1 definition facts; remove only when no retained v1 revisions remain.

This historical record never supplies execution policy or obligations.
"""

from typing import Annotated

import msgspec

from pinboard.domain.history import CanonicalLine, Identity


class HistoricalDefinitionV1(
    msgspec.Struct, tag="pinboard-work-item-definition/v1", tag_field="schema", frozen=True, forbid_unknown_fields=True
):
    acceptance_criteria: Annotated[tuple[CanonicalLine, ...], msgspec.Meta(min_length=1)]
    dependencies: tuple[Identity, ...]
    effect: CanonicalLine
    evidence: tuple[CanonicalLine, ...]
    hypothesis: CanonicalLine
    non_scope: tuple[CanonicalLine, ...]
    objective: CanonicalLine
    scope: Annotated[tuple[CanonicalLine, ...], msgspec.Meta(min_length=1)]
    title: CanonicalLine
    unlock: CanonicalLine

    def __post_init__(self) -> None:
        for field, values in (
            ("acceptance_criteria", self.acceptance_criteria),
            ("dependencies", self.dependencies),
            ("evidence", self.evidence),
            ("non_scope", self.non_scope),
            ("scope", self.scope),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{field} entries must be ordered and unique.")
