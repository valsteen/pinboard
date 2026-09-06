"""Compose direct coordination-authority commands from observation through presentation."""

import sys
from datetime import UTC, datetime, timedelta
from typing import assert_never
from uuid import uuid4

from pinboard.adapters.files.models import AffectedViews
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.service import decide_and_commit_coordination_authority_change
from pinboard.domain import authority_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import LeaseId
from pinboard.interfaces import cli_commands, work_views
from pinboard.interfaces.cli_output import authority_lease_fields, write_json
from pinboard.interfaces.errors import CommandFailure, CommandResult

type CoordinationAuthorityCommand = (
    cli_commands.CoordinationAcquireCommand
    | cli_commands.CoordinationRenewCommand
    | cli_commands.CoordinationReleaseCommand
    | cli_commands.CoordinationRevokeCommand
)


def _present_latest_coordination_authority(state: stored_state.StoredWorkState, *, json: bool) -> int:
    retained_authority = state.authority.coordination
    values = (
        None
        if retained_authority is None
        else authority_lease_fields(
            task_id=str(retained_authority.task_id),
            host_id=str(retained_authority.host_id),
            lease_id=str(retained_authority.lease_id),
            generation=retained_authority.generation,
            acquired_at=retained_authority.acquired_at,
            expires_at=retained_authority.expires_at,
            status=retained_authority.state.value,
        )
    )
    if json:
        write_json({"lease": None} if values is None else values)
    elif values is None:
        print("OK COORDINATION_AVAILABLE")
    else:
        print("OK " + " ".join(f"{key}={value}" for key, value in values.items()))
    return 0


def show_coordination_authority_status(
    roots: cli_commands.ResolvedRoots, command: cli_commands.CoordinationStatusCommand
) -> int:
    latest_committed_state = SQLiteWorkStore(roots.work / "state.sqlite3").coordination_state()
    return _present_latest_coordination_authority(latest_committed_state, json=command.json)


def find_retained_coordination_authority(
    observed_state: stored_state.StoredWorkState,
) -> CommandResult[stored_state.StoredCoordinationLease]:
    retained_authority = observed_state.authority.coordination
    if retained_authority is None:
        return CommandFailure(DecisionFailureCode.COORDINATION_LEASE_REQUIRED, "Coordination authority does not exist.")
    return retained_authority


def _resolve_supplied_coordination_authority(
    observed_state: stored_state.StoredWorkState,
    retained_authority: stored_state.StoredCoordinationLease,
    lease_id: LeaseId,
    generation: int,
) -> work_models.CoordinationCommandAuthority:
    return work_models.CoordinationCommandAuthority(
        observed_state.lifecycle.project.host_epoch,
        retained_authority.task_id,
        retained_authority.host_id,
        lease_id,
        generation,
        retained_authority.expires_at,
    )


def _resolve_requested_coordination_change(
    observed_state: stored_state.StoredWorkState,
    command: CoordinationAuthorityCommand,
    requested_at: datetime,
) -> CommandResult[authority_models.CoordinationAuthorityOperation]:
    match command:
        case cli_commands.CoordinationAcquireCommand(task_id=task_id, host_id=host_id, ttl_seconds=ttl_seconds):
            return authority_models.AcquireCoordinationAuthority(
                observed_state.lifecycle.project.host_epoch,
                task_id,
                host_id,
                LeaseId(uuid4().hex),
                requested_at,
                requested_at + timedelta(seconds=ttl_seconds),
            )
        case cli_commands.CoordinationRenewCommand(lease_id=lease_id, generation=generation, ttl_seconds=ttl_seconds):
            retained_authority = find_retained_coordination_authority(observed_state)
            if isinstance(retained_authority, CommandFailure):
                return retained_authority
            return authority_models.RenewCoordinationAuthority(
                _resolve_supplied_coordination_authority(observed_state, retained_authority, lease_id, generation),
                requested_at,
                requested_at + timedelta(seconds=ttl_seconds),
            )
        case cli_commands.CoordinationReleaseCommand(lease_id=lease_id, generation=generation):
            retained_authority = find_retained_coordination_authority(observed_state)
            if isinstance(retained_authority, CommandFailure):
                return retained_authority
            return authority_models.ReleaseCoordinationAuthority(
                _resolve_supplied_coordination_authority(observed_state, retained_authority, lease_id, generation),
                requested_at,
            )
        case cli_commands.CoordinationRevokeCommand():
            retained_authority = find_retained_coordination_authority(observed_state)
            if isinstance(retained_authority, CommandFailure):
                return retained_authority
            return authority_models.RevokeCoordinationAuthority(
                retained_authority.lease_id,
                retained_authority.generation,
                requested_at,
            )
        case _ as unreachable:
            assert_never(unreachable)


def change_coordination_authority(
    roots: cli_commands.ResolvedRoots,
    command: CoordinationAuthorityCommand,
) -> CommandResult[int]:
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    observed_state = store.coordination_state()
    requested_at = datetime.now(UTC)
    requested_change = _resolve_requested_coordination_change(observed_state, command, requested_at)
    if isinstance(requested_change, CommandFailure):
        return requested_change
    commit_result = decide_and_commit_coordination_authority_change(store, requested_change)
    if isinstance(commit_result, DecisionFailure):
        return CommandFailure(commit_result.code, commit_result.message)
    latest_committed_state = store.coordination_state()
    refresh_result = work_views.refresh(
        roots,
        latest_committed_state,
        AffectedViews(history_receipts=(commit_result.history_id,)),
        datetime.now(UTC),
    )
    if refresh_result.warning is not None:
        print(refresh_result.warning.message, file=sys.stderr)
    return _present_latest_coordination_authority(latest_committed_state, json=command.json)
