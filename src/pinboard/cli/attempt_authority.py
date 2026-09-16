"""Compose direct attempt-authority commands from observation through presentation."""

import sys
from datetime import UTC, datetime, timedelta
from typing import assert_never
from uuid import uuid4

from pinboard.adapters.files.file_io import DurableRoots
from pinboard.application import authority_operations, ports, queries
from pinboard.cli import cli_commands, work_views
from pinboard.cli.cli_output import authority_status_fields, write_json
from pinboard.cli.errors import CommandFailure, CommandResult
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import AttemptId, LeaseId

type AttemptAuthorityCommand = (
    cli_commands.AttemptAcquireCommand
    | cli_commands.AttemptRenewCommand
    | cli_commands.AttemptReleaseCommand
    | cli_commands.AttemptRevokeCommand
)


def _present_latest_attempt_authority(
    store: ports.WorkStore, attempt_id: AttemptId, *, json: bool
) -> CommandResult[int]:
    retained = store.read_attempt_authority_status(attempt_id)
    if retained is None:
        return CommandFailure(
            DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, f"Attempt '{attempt_id}' has no retained authority.", None
        )
    values: dict[str, str | int] = {
        "attempt_id": str(attempt_id),
        **authority_status_fields(retained),
    }
    if json:
        write_json(values)
    else:
        print("OK " + " ".join(f"{key}={value}" for key, value in values.items()))
    return 0


def show_attempt_authority_status(
    store: ports.WorkStore,
    command: cli_commands.AttemptStatusCommand,
) -> CommandResult[int]:
    selected = queries.select_attempt_authority_status(store, command.attempt_id)
    if isinstance(selected, DecisionFailure):
        return CommandFailure(selected.code, selected.message, selected.details)
    values: dict[str, str | int] = {
        "attempt_id": str(selected.attempt_id),
        **authority_status_fields(selected),
    }
    if command.json:
        write_json(values)
    else:
        print("OK " + " ".join(f"{key}={value}" for key, value in values.items()))
    return 0


def change_attempt_authority(
    durable: DurableRoots,
    store: ports.WorkStore,
    command: AttemptAuthorityCommand,
) -> CommandResult[int]:
    requested_at = datetime.now(UTC)
    match command:
        case cli_commands.AttemptAcquireCommand():
            committed = authority_operations.acquire_attempt_authority(
                store,
                attempt_id=command.attempt_id,
                task_id=command.task_id,
                host_id=command.host_id,
                lease_id=LeaseId(uuid4().hex),
                acquired_at=requested_at,
                expires_at=requested_at + timedelta(seconds=command.ttl_seconds),
            )
        case cli_commands.AttemptRenewCommand():
            committed = authority_operations.renew_attempt_authority(
                store,
                attempt_id=command.attempt_id,
                lease_id=command.lease_id,
                generation=command.generation,
                renewed_at=requested_at,
                expires_at=requested_at + timedelta(seconds=command.ttl_seconds),
            )
        case cli_commands.AttemptReleaseCommand():
            committed = authority_operations.release_attempt_authority(
                store,
                attempt_id=command.attempt_id,
                lease_id=command.lease_id,
                generation=command.generation,
                released_at=requested_at,
            )
        case cli_commands.AttemptRevokeCommand():
            committed = authority_operations.revoke_attempt_authority(
                store,
                attempt_id=command.attempt_id,
                lease_id=command.lease_id,
                generation=command.generation,
                revoked_at=requested_at,
                actor_task_id=command.task_id,
                actor_host_id=command.host_id,
            )
        case _ as unreachable:
            assert_never(unreachable)
    if isinstance(committed, DecisionFailure):
        return CommandFailure(committed.code, committed.message, committed.details)
    refresh_result = work_views.refresh_effect(durable, store, committed.effect, datetime.now(UTC))
    if refresh_result.warning is not None:
        print(refresh_result.warning.message, file=sys.stderr)
    return _present_latest_attempt_authority(store, command.attempt_id, json=command.json)
