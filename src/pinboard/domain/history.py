import hashlib
from dataclasses import dataclass
from typing import Annotated, Literal

import msgspec

from pinboard.domain import work_models
from pinboard.domain.errors import (
    DecisionFailure,
    DecisionFailureCode,
    DecisionResult,
)
from pinboard.domain.identifiers import ItemId

type CanonicalLine = Annotated[str, msgspec.Meta(pattern=r"\A\S(?:[^\r\n]*\S)?\z")]
type Identity = Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]


def _encoded_record(value: msgspec.Struct) -> bytes:
    return msgspec.json.encode(value, order="sorted") + b"\n"


class WorkItemDefinitionPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
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


class TransitionReceiptOutcome(msgspec.Struct, frozen=True, forbid_unknown_fields=True, omit_defaults=True):
    evidence: str | None
    outcome: str | None
    candidate: str | None = None
    checkpoint: str | None = None


@dataclass(frozen=True, slots=True)
class HistoryOutcome:
    outcome_schema: str
    payload: bytes


def encode_transition_receipt_outcome(
    *,
    evidence: str | None,
    outcome: str | None,
    candidate: str | None = None,
    checkpoint: str | None = None,
) -> bytes:
    return msgspec.json.encode(
        TransitionReceiptOutcome(evidence, outcome, candidate, checkpoint),
        order="sorted",
    )


def _work_item_definition_payload(
    definition: work_models.WorkItemDefinition,
) -> DecisionResult[WorkItemDefinitionPayload]:
    try:
        return msgspec.convert(
            {
                "acceptance_criteria": definition.acceptance_criteria,
                "dependencies": definition.dependencies,
                "effect": definition.effect,
                "evidence": definition.evidence,
                "hypothesis": definition.hypothesis,
                "non_scope": definition.non_scope,
                "objective": definition.objective,
                "schema": "pinboard-work-item-definition/v1",
                "scope": definition.scope,
                "title": definition.title,
                "unlock": definition.unlock,
            },
            type=WorkItemDefinitionPayload,
            strict=True,
        )
    except msgspec.ValidationError as error:
        return DecisionFailure(DecisionFailureCode.ITEM_DEFINITION_INVALID, f"Definition is invalid: {error}", None)


def work_item_definition_bytes(definition: work_models.WorkItemDefinition) -> DecisionResult[bytes]:
    payload = _work_item_definition_payload(definition)
    if isinstance(payload, DecisionFailure):
        return payload
    return _encoded_record(payload)


def work_item_definition_digest(definition: work_models.WorkItemDefinition) -> DecisionResult[str]:
    payload = work_item_definition_bytes(definition)
    if isinstance(payload, DecisionFailure):
        return payload
    return hashlib.sha256(payload).hexdigest()


def decode_work_item_definition(payload: bytes) -> DecisionResult[work_models.WorkItemDefinition]:
    try:
        record = msgspec.json.decode(payload, type=WorkItemDefinitionPayload, strict=True)
    except msgspec.DecodeError as error:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID, f"Definition JSON is invalid: {error}", None
        )
    if _encoded_record(record) != payload:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            "Definition JSON must use the canonical encoding.",
            None,
        )
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
    )
