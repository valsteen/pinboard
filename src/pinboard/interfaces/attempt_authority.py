"""Compose direct attempt-authority commands from observation through presentation."""

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import assert_never
from uuid import uuid4

from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.decision_projection import (
    project_decision_snapshot,
    project_inactive_attempt_authority,
)
from pinboard.application.service import decide_and_commit_attempt_authority_change
from pinboard.domain import authority_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import AttemptId, LeaseId
from pinboard.interfaces import cli_commands, work_views
from pinboard.interfaces.cli_output import retained_authority_lease_fields, write_json
from pinboard.interfaces.errors import CommandErrorCode, CommandFailure, CommandResult

type AttemptAuthorityCommand = (
    cli_commands.AttemptAcquireCommand
    | cli_commands.AttemptRenewCommand
    | cli_commands.AttemptReleaseCommand
    | cli_commands.AttemptRevokeCommand
)


def _present_latest_attempt_authority(
    state: stored_state.StoredWorkState, attempt_id: AttemptId, *, json: bool
) -> CommandResult[int]:
    retained = stored_state.retained_attempt(state, attempt_id)
    if retained is None:
        return CommandFailure(
            DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, f"Attempt '{attempt_id}' has no retained authority.", None
        )
    lease, anchor = retained
    if anchor is None:
        return CommandFailure(
            CommandErrorCode.WORK_STATE_INVALID, "Attempt authority has no exact identity anchor.", None
        )
    values: dict[str, str | int] = {
        "attempt_id": str(attempt_id),
        **retained_authority_lease_fields((lease, anchor)),
    }
    if json:
        write_json(values)
    else:
        print("OK " + " ".join(f"{key}={value}" for key, value in values.items()))
    return 0


def show_attempt_authority_status(
    roots: cli_commands.ResolvedRoots, command: cli_commands.AttemptStatusCommand
) -> CommandResult[int]:
    latest_committed_state = SQLiteWorkStore(roots.work / "state.sqlite3").snapshot()
    return _present_latest_attempt_authority(latest_committed_state, command.attempt_id, json=command.json)


def _find_attempt_record(
    observed_state: stored_state.StoredWorkState, attempt_id: AttemptId
) -> CommandResult[stored_state.StoredAttempt]:
    attempt = next((value for value in observed_state.lifecycle.attempts if value.attempt_id == attempt_id), None)
    if attempt is None:
        return CommandFailure(
            DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, f"Attempt '{attempt_id}' is not current.", None
        )
    return attempt


def _resolve_requested_attempt_acquisition(
    observed_state: stored_state.StoredWorkState,
    attempt_record: stored_state.StoredAttempt,
    command: cli_commands.AttemptAcquireCommand,
    requested_at: datetime,
) -> CommandResult[authority_models.AttemptAuthorityOperation]:
    attempt_id = command.attempt_id
    retained_record = next(
        (value for value in observed_state.authority.attempt_leases if value.attempt_id == attempt_id),
        None,
    )
    lease_id = LeaseId(uuid4().hex)
    if retained_record is None:
        return authority_models.AcquireInitialAttemptAuthority(
            observed_state.lifecycle.project.host_epoch,
            attempt_id,
            attempt_record.item_id,
            command.task_id,
            command.host_id,
            lease_id,
            requested_at,
            requested_at + timedelta(seconds=command.ttl_seconds),
        )
    inactive = project_inactive_attempt_authority(observed_state, attempt_id, requested_at)
    if isinstance(inactive, DecisionFailure):
        return CommandFailure(inactive.code, inactive.message, inactive.details)
    return authority_models.TransferAttemptAuthority(
        inactive,
        command.task_id,
        command.host_id,
        lease_id,
        requested_at,
        requested_at + timedelta(seconds=command.ttl_seconds),
    )


def _resolve_supplied_attempt_authority(
    observed_state: stored_state.StoredWorkState,
    attempt_id: AttemptId,
    requested_at: datetime,
) -> CommandResult[work_models.CommandAttemptAuthority]:
    observed_authority = next(
        (
            value
            for value in project_decision_snapshot(observed_state, requested_at).command_attempt_authorities
            if value.attempt == attempt_id
        ),
        None,
    )
    if observed_authority is None:
        return CommandFailure(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, "Attempt authority is not active.", None)
    return observed_authority


def _resolve_requested_attempt_change(
    observed_state: stored_state.StoredWorkState,
    attempt_record: stored_state.StoredAttempt,
    command: AttemptAuthorityCommand,
    requested_at: datetime,
) -> CommandResult[authority_models.AttemptAuthorityOperation]:
    match command:
        case cli_commands.AttemptAcquireCommand():
            return _resolve_requested_attempt_acquisition(observed_state, attempt_record, command, requested_at)
        case cli_commands.AttemptRenewCommand():
            supplied_authority = _resolve_supplied_attempt_authority(observed_state, command.attempt_id, requested_at)
            if isinstance(supplied_authority, CommandFailure):
                return supplied_authority
            return authority_models.RenewAttemptAuthority(
                replace(supplied_authority, lease_id=command.lease_id, generation=command.generation),
                requested_at,
                requested_at + timedelta(seconds=command.ttl_seconds),
            )
        case cli_commands.AttemptReleaseCommand():
            supplied_authority = _resolve_supplied_attempt_authority(observed_state, command.attempt_id, requested_at)
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
    roots: cli_commands.ResolvedRoots,
    command: AttemptAuthorityCommand,
) -> CommandResult[int]:
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    observed_state = store.snapshot()
    attempt_record = _find_attempt_record(observed_state, command.attempt_id)
    if isinstance(attempt_record, CommandFailure):
        return attempt_record
    requested_at = datetime.now(UTC)
    requested_change = _resolve_requested_attempt_change(observed_state, attempt_record, command, requested_at)
    if isinstance(requested_change, CommandFailure):
        return requested_change
    commit_result = decide_and_commit_attempt_authority_change(store, requested_change)
    if isinstance(commit_result, DecisionFailure):
        return CommandFailure(commit_result.code, commit_result.message, commit_result.details)
    refresh_result = work_views.refresh_shared_authority_views(roots, store, datetime.now(UTC))
    if refresh_result.warning is not None:
        print(refresh_result.warning.message, file=sys.stderr)
    latest_committed_state = store.snapshot()
    return _present_latest_attempt_authority(latest_committed_state, command.attempt_id, json=command.json)
