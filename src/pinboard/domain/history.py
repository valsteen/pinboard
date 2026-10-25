import hashlib
import re
from dataclasses import dataclass
from typing import Annotated, Literal

import msgspec

from pinboard.domain import work_models
from pinboard.domain.errors import (
    DecisionFailure,
    DecisionFailureCode,
    DecisionResult,
)
from pinboard.domain.identifiers import CheckpointId, WorkItemId

type CanonicalLine = Annotated[str, msgspec.Meta(pattern=r"\A\S(?:[^\r\n]*\S)?\z")]
type Identity = Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]
# Human-read record text: pause and return reasons, checkpoint and review-continue acceptance evidence and
# scope-revision reasons. It is any non-empty string, line breaks included, and is stored and projected exactly as
# accepted, never trimmed or re-wrapped. Definitions and identities stay CanonicalLine.
type RecordText = Annotated[str, msgspec.Meta(min_length=1)]


# Markdown ends a line at CRLF, a bare CR or LF; record text keeps whichever it was accepted with.
_RECORD_LINE_BREAK = re.compile(r"(\r\n|\r|\n)")


def record_text_is_multiline(text: str) -> bool:
    return _RECORD_LINE_BREAK.search(text) is not None


def nest_record_text(text: str, indent: str) -> str:
    """Keep record text inside the Markdown container whose first line holds it.

    Every continuation line after a CRLF, CR or LF break is indented under the container, so no line of record text
    can start a heading or list item at the container's level; empty lines stay empty and each break keeps its
    accepted bytes. Single-line text is returned unchanged.
    """

    first, *rest = _RECORD_LINE_BREAK.split(text)
    breaks_and_lines = zip(rest[::2], rest[1::2], strict=True)
    return first + "".join(
        f"{line_break}{indent}{line}" if line else line_break for line_break, line in breaks_and_lines
    )


def _encoded_record(value: msgspec.Struct) -> bytes:
    return msgspec.json.encode(value, order="sorted") + b"\n"


class WorkObligationPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    obligation_id: Identity
    statement: CanonicalLine
    deferral_policy: work_models.ObligationDeferralPolicy


class CheckpointDependencyPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item: Identity
    checkpoint: Identity


type DependencyPayload = Identity | CheckpointDependencyPayload


def dependency_from_payload(value: DependencyPayload) -> work_models.Dependency:
    if isinstance(value, str):
        return WorkItemId(value)
    return work_models.CheckpointDependency(WorkItemId(value.item), CheckpointId(value.checkpoint))


def dependency_payload(value: work_models.Dependency) -> DependencyPayload:
    if isinstance(value, str):
        return value
    return msgspec.convert({"item": value.item_id, "checkpoint": value.checkpoint_id}, type=CheckpointDependencyPayload)


class WorkItemDefinitionPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    acceptance_criteria: Annotated[tuple[CanonicalLine, ...], msgspec.Meta(min_length=1)]
    dependencies: tuple[DependencyPayload, ...]
    effect: CanonicalLine
    evidence: tuple[CanonicalLine, ...]
    hypothesis: CanonicalLine
    non_scope: tuple[CanonicalLine, ...]
    objective: CanonicalLine
    schema: Literal["pinboard-work-item-definition/v2"]
    scope: Annotated[tuple[CanonicalLine, ...], msgspec.Meta(min_length=1)]
    title: CanonicalLine
    unlock: CanonicalLine
    checkout_policy: Literal["main", "isolated", "coordinator-selected"]
    obligations: Annotated[tuple[WorkObligationPayload, ...], msgspec.Meta(min_length=1)]

    def __post_init__(self) -> None:
        for field, values in (
            ("acceptance_criteria", self.acceptance_criteria),
            ("evidence", self.evidence),
            ("non_scope", self.non_scope),
            ("scope", self.scope),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{field} entries must be ordered and unique.")
        obligation_ids = tuple(value.obligation_id for value in self.obligations)
        if len(obligation_ids) != len(set(obligation_ids)):
            raise ValueError("obligation identities must be ordered and unique.")
        dependency_ids = tuple(value if isinstance(value, str) else value.item for value in self.dependencies)
        if len(dependency_ids) != len(set(dependency_ids)):
            raise ValueError("dependencies must contain one entry per prerequisite item.")


class TransitionReceiptOutcome(msgspec.Struct, frozen=True, forbid_unknown_fields=True, omit_defaults=True):
    evidence: str | None
    outcome: str | None
    candidate: str | None = None
    checkpoint: str | None = None


class CheckpointAcceptanceOutcome(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    candidate: CanonicalLine
    checkpoint: CanonicalLine
    evidence: RecordText
    outcome: CanonicalLine


class CompletionAcceptanceOutcome(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    candidate: CanonicalLine
    evidence: CanonicalLine
    outcome: Literal["complete"]


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


def encode_checkpoint_acceptance_outcome(
    *,
    candidate: str,
    checkpoint: str,
    evidence: str,
    outcome: str,
) -> bytes:
    return msgspec.json.encode(
        CheckpointAcceptanceOutcome(candidate, checkpoint, evidence, outcome),
        order="sorted",
    )


def encode_completion_acceptance_outcome(*, candidate: str, evidence: str) -> bytes:
    return msgspec.json.encode(CompletionAcceptanceOutcome(candidate, evidence, "complete"), order="sorted")


def _work_item_definition_payload(
    definition: work_models.WorkItemDefinition,
) -> DecisionResult[WorkItemDefinitionPayload]:
    try:
        return msgspec.convert(
            {
                "acceptance_criteria": definition.acceptance_criteria,
                "dependencies": tuple(dependency_payload(value) for value in definition.dependencies),
                "effect": definition.effect,
                "evidence": definition.evidence,
                "hypothesis": definition.hypothesis,
                "non_scope": definition.non_scope,
                "objective": definition.objective,
                "schema": "pinboard-work-item-definition/v2",
                "scope": definition.scope,
                "title": definition.title,
                "unlock": definition.unlock,
                "checkout_policy": definition.checkout_policy.value,
                "obligations": tuple(
                    {
                        "obligation_id": value.obligation_id,
                        "statement": value.statement,
                        "deferral_policy": value.deferral_policy.value,
                    }
                    for value in definition.obligations
                ),
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
    return work_item_definition_from_payload(record)


def work_item_definition_from_payload(record: WorkItemDefinitionPayload) -> work_models.WorkItemDefinition:
    return work_models.WorkItemDefinition(
        record.title,
        record.objective,
        record.hypothesis,
        record.evidence,
        record.scope,
        record.non_scope,
        record.acceptance_criteria,
        tuple(dependency_from_payload(value) for value in record.dependencies),
        record.effect,
        record.unlock,
        work_models.CheckoutPolicy(record.checkout_policy),
        tuple(
            work_models.WorkObligation(
                work_models.ObligationId(value.obligation_id),
                value.statement,
                value.deferral_policy,
            )
            for value in record.obligations
        ),
    )
