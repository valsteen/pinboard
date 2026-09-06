"""Concrete stdout effects shared by installed command presenters."""

import sys
from datetime import datetime
from typing import Literal

import msgspec

from pinboard.application import stored_state
from pinboard.domain.errors import EffectDisposition, FailureDetails, FailureFactValue, RetryDisposition
from pinboard.interfaces.errors import CliFailure

type RetainedAuthorityLease = (
    tuple[stored_state.StoredAttemptLease, stored_state.AttemptLeaseGeneration]
    | tuple[stored_state.StoredPreparationLease, stored_state.PreparationLeaseGeneration]
)


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
    expected_revision: str
    subject_revision: str | None
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


def retained_authority_lease_fields(retained: RetainedAuthorityLease) -> dict[str, str | int]:
    """Project common status fields from one retained attempt or preparation lease."""

    lease, anchor = retained
    return authority_lease_fields(
        task_id=str(anchor.task_id),
        host_id=str(anchor.host_id),
        lease_id=str(anchor.lease_id),
        generation=lease.generation,
        acquired_at=lease.acquired_at,
        expires_at=lease.expires_at,
        status=lease.state.value,
    )


def write_json[T](value: T) -> None:
    """Write one canonical, human-readable JSON value and nothing else."""

    encoded = msgspec.json.encode(value, order="sorted")
    sys.stdout.write(msgspec.json.format(encoded, indent=2).decode() + "\n")


def write_rejected_operation(operation: str, failure: CliFailure) -> None:
    """Present one expected failure without reconstructing facts from its prose message."""
    details = failure.details
    write_operation_rejection(
        operation,
        failure.code.value,
        failure.message,
        FailureDetails(
            observed=(),
            mismatches=(),
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        )
        if details is None
        else details,
        (),
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
                        value.expected_revision,
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
