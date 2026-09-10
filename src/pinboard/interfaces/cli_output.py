"""Concrete stdout effects shared by installed command presenters."""

import sys
from datetime import datetime
from typing import Literal

import msgspec

from pinboard.application import query_models
from pinboard.domain.errors import EffectDisposition, FailureDetails, FailureFactValue, RetryDisposition
from pinboard.interfaces.errors import BriefSourceFailure, CliFailure, CommittedEffectFailure, WorkBriefFailure

type AuthorityStatus = query_models.AttemptAuthorityStatus | query_models.PreparationAuthorityStatus


class FailureFactView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    field: str
    value: FailureFactValue


class FailureMismatchView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    field: str
    expected: FailureFactValue
    observed: FailureFactValue


class RecoveryCommandView(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="command",
    tag_field="kind",
):
    command: str


class RecoveryActionView(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="action",
    tag_field="kind",
):
    action_id: str
    role: str
    subject_revision: str
    authorization: str | None
    lease_id: str | None
    generation: int | None


type RecoveryView = RecoveryCommandView | RecoveryActionView


class RejectedOperationView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-rejected-operation/v1"]
    status: Literal["rejected", "committed-effect"]
    operation: str
    code: str
    message: str
    state_changed: bool
    changed_surfaces: tuple[str, ...]
    observed: tuple[FailureFactView, ...]
    mismatches: tuple[FailureMismatchView, ...]
    retry: str
    next_actions: tuple[RecoveryView, ...]


def authority_lease_fields(
    *,
    task_id: str,
    host_id: str,
    lease_id: str,
    generation: int,
    acquired_at: datetime,
    expires_at: datetime,
    status: str,
) -> dict[str, str | int]:
    """Project the common identity and timing fields of an authority lease."""

    return {
        "task_id": task_id,
        "host_id": host_id,
        "lease_id": lease_id,
        "generation": generation,
        "acquired_at": acquired_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "status": status,
    }


def authority_status_fields(status: AuthorityStatus) -> dict[str, str | int]:
    """Project common output fields from one exact authority-status result."""

    return authority_lease_fields(
        task_id=status.task_id,
        host_id=status.host_id,
        lease_id=status.lease_id,
        generation=status.generation,
        acquired_at=status.acquired_at,
        expires_at=status.expires_at,
        status=status.status.value,
    )


def write_json[T](value: T) -> None:
    """Write one canonical, human-readable JSON value and nothing else."""

    encoded = msgspec.json.encode(value, order="sorted")
    sys.stdout.write(msgspec.json.format(encoded, indent=2).decode() + "\n")


def write_rejected_operation(operation: str, failure: CliFailure) -> None:
    """Present one expected failure without reconstructing facts from its prose message."""
    assert not isinstance(failure, CommittedEffectFailure)
    details = None if isinstance(failure, (BriefSourceFailure, WorkBriefFailure)) else failure.details
    default_retry = (
        RetryDisposition.CORRECT_INPUT
        if isinstance(failure, (BriefSourceFailure, WorkBriefFailure))
        else RetryDisposition.DO_NOT_RETRY
    )
    write_operation_rejection(
        operation,
        failure.code.value,
        failure.message,
        FailureDetails(
            observed=(),
            mismatches=(),
            retry=default_retry,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        )
        if details is None
        else details,
        ("pinboard tool-contract --json",)
        if operation == "tool-contract" and failure.code.value == "TRANSITION_INPUT_INVALID"
        else (),
    )


def write_operation_rejection(
    operation: str,
    code: str,
    message: str,
    details: FailureDetails | None,
    next_actions: tuple[str, ...],
) -> None:
    """Present one typed operation failure, including any already committed effect."""
    details = (
        FailureDetails(
            observed=(),
            mismatches=(),
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        )
        if details is None
        else details
    )
    effect = details.effect
    write_json(
        RejectedOperationView(
            "pinboard-rejected-operation/v1",
            "rejected" if effect == EffectDisposition.UNCHANGED else "committed-effect",
            operation,
            code,
            message,
            effect == EffectDisposition.COMMITTED,
            tuple(surface.value for surface in details.changed_surfaces),
            tuple(FailureFactView(value.field, value.value) for value in details.observed),
            tuple(FailureMismatchView(value.field, value.expected, value.observed) for value in details.mismatches),
            details.retry.value,
            (
                *(RecoveryCommandView(value) for value in next_actions),
                *(
                    RecoveryActionView(
                        value.action_id,
                        value.role,
                        value.subject_revision,
                        value.authorization,
                        value.lease_id,
                        value.generation,
                    )
                    for value in details.alternatives
                ),
            ),
        )
    )


def write_argument_rejection(arguments: tuple[str, ...], message: str) -> None:
    """Present a parse-time rejection before an exact command record exists."""
    write_json(
        RejectedOperationView(
            "pinboard-rejected-operation/v1",
            "rejected",
            "cli-arguments",
            "CLI_ARGUMENT_INVALID",
            message,
            False,
            (),
            (FailureFactView("arguments", " ".join(arguments)),),
            (FailureMismatchView("arguments", "one valid installed invocation", " ".join(arguments)),),
            RetryDisposition.CORRECT_INPUT.value,
            (RecoveryCommandView("pinboard tool-contract --json"),),
        )
    )
