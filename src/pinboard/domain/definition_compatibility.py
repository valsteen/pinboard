"""Exact retained work-item definition v1 codec."""

import hashlib
from typing import Annotated, Literal

import msgspec

from pinboard.domain import work_models
from pinboard.domain.identifiers import ItemId

type CanonicalLine = Annotated[str, msgspec.Meta(pattern=r"\A\S(?:[^\r\n]*\S)?\z")]
type Identity = Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]


class WorkItemDefinitionPayloadV1(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    acceptance_criteria: Annotated[tuple[CanonicalLine, ...], msgspec.Meta(min_length=1)]
    dependencies: tuple[Identity, ...]
    effect: CanonicalLine
    evidence: tuple[CanonicalLine, ...]
    hypothesis: CanonicalLine
    non_scope: tuple[CanonicalLine, ...]
    objective: CanonicalLine
    schema: Literal["pinboard-work-item-definition/v1"]
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


def _legacy_obligations(criteria: tuple[str, ...]) -> tuple[work_models.WorkObligation, ...]:
    return tuple(
        work_models.WorkObligation(
            work_models.ObligationId(f"legacy-{hashlib.sha256(statement.encode()).hexdigest()[:24]}"),
            statement,
            work_models.ObligationDeferralPolicy.FORBIDDEN,
        )
        for statement in criteria
    )


def decode_v1(payload: bytes) -> work_models.WorkItemDefinition:
    record = msgspec.json.decode(payload, type=WorkItemDefinitionPayloadV1, strict=True)
    if msgspec.json.encode(record, order="sorted") + b"\n" != payload:
        raise ValueError("Definition JSON must use the canonical encoding.")
    return work_models.WorkItemDefinition(
        record.title,
        record.objective,
        record.hypothesis,
        record.evidence,
        record.scope,
        record.non_scope,
        record.acceptance_criteria,
        tuple(ItemId(value) for value in record.dependencies),
        record.effect,
        record.unlock,
        work_models.CheckoutPolicy.LEGACY_UNRECORDED,
        _legacy_obligations(record.acceptance_criteria),
    )


def encode_v1(definition: work_models.WorkItemDefinition) -> bytes:
    return (
        msgspec.json.encode(
            WorkItemDefinitionPayloadV1(
                definition.acceptance_criteria,
                tuple(definition.dependencies),
                definition.effect,
                definition.evidence,
                definition.hypothesis,
                definition.non_scope,
                definition.objective,
                "pinboard-work-item-definition/v1",
                definition.scope,
                definition.title,
                definition.unlock,
            ),
            order="sorted",
        )
        + b"\n"
    )
