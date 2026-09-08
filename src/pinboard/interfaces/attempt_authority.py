"""Compose direct attempt-authority commands from observation through presentation."""

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import assert_never
from uuid import uuid4

from pinboard.adapters.files.file_io import DurableRoots
from pinboard.application import ports, queries, query_models
from pinboard.application.service import decide_and_commit_attempt_authority_change
from pinboard.domain import authority_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import AttemptId, LeaseId
from pinboard.domain.ledger import LedgerSnapshot
from pinboard.interfaces import cli_commands, work_views
from pinboard.interfaces.cli_output import authority_status_fields, write_json
from pinboard.interfaces.errors import CommandFailure, CommandResult

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


def _find_attempt_record(snapshot: LedgerSnapshot, attempt_id: AttemptId) -> CommandResult[work_models.AttemptRecord]:
    attempt = snapshot.attempt(attempt_id)
    if attempt is None:
        return CommandFailure(
            DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, f"Attempt '{attempt_id}' is not current.", None
        )
    return attempt


def _resolve_requested_attempt_acquisition(
    snapshot: LedgerSnapshot,
    attempt_record: work_models.AttemptRecord,
    retained: query_models.AttemptAuthorityStatus | None,
    command: cli_commands.AttemptAcquireCommand,
    requested_at: datetime,
) -> CommandResult[authority_models.AttemptAuthorityOperation]:
    attempt_id = command.attempt_id
    lease_id = LeaseId(uuid4().hex)
    if retained is None:
        return authority_models.AcquireInitialAttemptAuthority(
            snapshot.host_epoch,
            attempt_id,
            attempt_record.item,
            command.task_id,
            command.host_id,
            lease_id,
            requested_at,
            requested_at + timedelta(seconds=command.ttl_seconds),
        )
    state = retained.status
    if state == authority_models.AttemptLeaseStatus.ACTIVE:
        if retained.expires_at > requested_at:
            return CommandFailure(
                DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED, "Attempt authority remains live.", None
            )
        state = authority_models.AttemptLeaseStatus.EXPIRED
    inactive = authority_models.InactiveAttemptAuthority(
        snapshot.host_epoch,
        attempt_id,
        attempt_record.item,
        retained.task_id,
        retained.host_id,
        retained.lease_id,
        retained.generation,
        retained.expires_at,
        state,
    )
    return authority_models.TransferAttemptAuthority(
        inactive,
        command.task_id,
        command.host_id,
        lease_id,
        requested_at,
        requested_at + timedelta(seconds=command.ttl_seconds),
    )


def _resolve_supplied_attempt_authority(
    snapshot: LedgerSnapshot,
    attempt_id: AttemptId,
) -> CommandResult[work_models.CommandAttemptAuthority]:
    observed_authority = next(
        (value for value in snapshot.command_attempt_authorities if value.attempt == attempt_id),
        None,
    )
    if observed_authority is None:
        return CommandFailure(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, "Attempt authority is not active.", None)
    return observed_authority


def _resolve_requested_attempt_change(
    snapshot: LedgerSnapshot,
    attempt_record: work_models.AttemptRecord,
    retained: query_models.AttemptAuthorityStatus | None,
    command: AttemptAuthorityCommand,
    requested_at: datetime,
) -> CommandResult[authority_models.AttemptAuthorityOperation]:
    match command:
        case cli_commands.AttemptAcquireCommand():
            return _resolve_requested_attempt_acquisition(snapshot, attempt_record, retained, command, requested_at)
        case cli_commands.AttemptRenewCommand():
            supplied_authority = _resolve_supplied_attempt_authority(snapshot, command.attempt_id)
            if isinstance(supplied_authority, CommandFailure):
                return supplied_authority
            return authority_models.RenewAttemptAuthority(
                replace(supplied_authority, lease_id=command.lease_id, generation=command.generation),
                requested_at,
                requested_at + timedelta(seconds=command.ttl_seconds),
            )
        case cli_commands.AttemptReleaseCommand():
            supplied_authority = _resolve_supplied_attempt_authority(snapshot, command.attempt_id)
            if isinstance(supplied_authority, CommandFailure):
                return supplied_authority
            return authority_models.ReleaseAttemptAuthority(
                replace(supplied_authority, lease_id=command.lease_id, generation=command.generation),
                requested_at,
            )
        case cli_commands.AttemptRevokeCommand():
            return authority_models.RevokeAttemptAuthority(
                command.attempt_id,
                command.lease_id,
                command.generation,
                command.task_id,
                command.host_id,
                requested_at,
            )
        case _ as unreachable:
            assert_never(unreachable)


def change_attempt_authority(
    durable: DurableRoots,
    store: ports.WorkStore,
    command: AttemptAuthorityCommand,
) -> CommandResult[int]:
    requested_at = datetime.now(UTC)
    snapshot = store.read_decision_facts(
        query_models.DecisionScope((), (), (), (), (command.attempt_id,), (), ()), requested_at
    ).snapshot
    attempt_record = _find_attempt_record(snapshot, command.attempt_id)
    if isinstance(attempt_record, CommandFailure):
        return attempt_record
    requested_change = _resolve_requested_attempt_change(
        snapshot, attempt_record, store.read_attempt_authority_status(command.attempt_id), command, requested_at
    )
    if isinstance(requested_change, CommandFailure):
        return requested_change
    commit_result = decide_and_commit_attempt_authority_change(store, requested_change)
    if isinstance(commit_result, DecisionFailure):
        return CommandFailure(commit_result.code, commit_result.message, commit_result.details)
    refresh_result = work_views.refresh_effect(durable, store, commit_result, datetime.now(UTC))
    if refresh_result.warning is not None:
        print(refresh_result.warning.message, file=sys.stderr)
    return _present_latest_attempt_authority(store, command.attempt_id, json=command.json)
