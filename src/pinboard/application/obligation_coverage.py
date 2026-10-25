"""Resolve exact retained origins and report current direct carrying claims; never infer equivalence."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

import msgspec

from pinboard.application import stored_state
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import WorkItemId


class OriginReader(Protocol):
    def read_obligation_origins(
        self, origins: tuple[work_models.ObligationOrigin, ...]
    ) -> frozenset[work_models.ObligationOrigin]: ...


def validate_origins(
    reader: OriginReader, obligations: Iterable[work_models.DefinitionObligation]
) -> DecisionFailure | None:
    origins = tuple(
        dict.fromkeys(value.origin for value in obligations if isinstance(value, work_models.CarriedWorkObligation))
    )
    retained = reader.read_obligation_origins(origins)
    for origin in origins:
        if origin not in retained:
            return DecisionFailure(
                DecisionFailureCode.ITEM_DEFINITION_INVALID,
                f"Origin '{origin.item_id}@{origin.definition_revision}/obligation/{origin.obligation_id}' does not resolve to a retained source obligation. Read pinboard_item_definition history for that item and supply its exact retained revision and obligation ID. Nothing was changed.",
                None,
            )
    return None


@dataclass(frozen=True, slots=True)
class CoverageFacts:
    source_item: WorkItemId
    replacement_item: WorkItemId | None
    source: stored_state.ItemDefinitionRevision | None
    replacement: stored_state.ItemDefinitionRevision | None
    relation_revision: int | None


class CarryingObligation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    definition_revision: int
    obligation_id: work_models.ObligationId
    statement: str


class _SourceObligation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    obligation_id: work_models.ObligationId
    statement: str


class CoveredObligation(_SourceObligation, frozen=True, forbid_unknown_fields=True, tag="covered", tag_field="status"):
    carrying: Annotated[tuple[CarryingObligation, ...], msgspec.Meta(min_length=1)]


class UncoveredObligation(
    _SourceObligation, frozen=True, forbid_unknown_fields=True, tag="uncovered", tag_field="status"
):
    carrying: tuple[()]


type CoverageRow = CoveredObligation | UncoveredObligation


class CoverageReport(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-obligation-coverage/v1"]
    source_item: WorkItemId
    source_revision: int
    source_digest: str
    replacement_item: WorkItemId
    replacement_revision: int
    replacement_digest: str
    relation_revision: int | None
    obligations: tuple[CoverageRow, ...]


def report(facts: CoverageFacts) -> DecisionResult[CoverageReport]:
    source = facts.source
    if source is None:
        return DecisionFailure(
            DecisionFailureCode.ITEM_NOT_FOUND,
            f"Coverage source '{facts.source_item}' has no retained definition. Check that item ID with pinboard_item_definition before retrying; nothing was changed.",
            None,
        )
    if facts.replacement_item is None:
        return DecisionFailure(
            DecisionFailureCode.REPLACEMENT_INVALID,
            f"Item '{facts.source_item}' has no current planned replacement. Read pinboard_item_definition for a named item, or request pair coverage with an explicit replacement_item.",
            None,
        )
    replacement = facts.replacement
    if replacement is None:
        return DecisionFailure(
            DecisionFailureCode.ITEM_NOT_FOUND,
            f"Coverage replacement '{facts.replacement_item}' has no retained definition. Check that item ID with pinboard_item_definition before retrying; nothing was changed.",
            None,
        )
    if not isinstance(source.definition, work_models.WorkItemDefinition):
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            f"Coverage source '{facts.source_item}' has a historical definition without obligations. Read its retained definition; coverage cannot infer missing obligations.",
            None,
        )
    carrying: dict[work_models.ObligationOrigin, list[CarryingObligation]] = {}
    if isinstance(replacement.definition, work_models.WorkItemDefinition):
        for value in replacement.definition.obligations:
            if isinstance(value, work_models.CarriedWorkObligation):
                carrying.setdefault(value.origin, []).append(
                    CarryingObligation(replacement.item_id, replacement.revision, value.obligation_id, value.statement)
                )
    rows: list[CoverageRow] = []
    for value in source.definition.obligations:
        owners = tuple(
            carrying.get(work_models.ObligationOrigin(source.item_id, source.revision, value.obligation_id), ())
        )
        rows.append(
            CoveredObligation(value.obligation_id, value.statement, owners)
            if owners
            else UncoveredObligation(value.obligation_id, value.statement, ())
        )
    return CoverageReport(
        "pinboard-obligation-coverage/v1",
        source.item_id,
        source.revision,
        source.digest,
        replacement.item_id,
        replacement.revision,
        replacement.digest,
        facts.relation_revision,
        tuple(rows),
    )
