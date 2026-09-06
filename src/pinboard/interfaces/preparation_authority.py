"""Compose preparation-authority commands from observation through presentation.

This interface may read the clock and concrete store, refresh generated views, and
present output. The application use case owns the locked reread, decision, and
durable commit; an earlier observation here only helps resolve the caller's request.
"""

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import assert_never
from uuid import uuid4

from pinboard.adapters.files.models import AffectedViews
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import service, stored_state
from pinboard.application.decision_projection import project_decision_snapshot
from pinboard.application.service import decide_and_commit_preparation_authority_change
from pinboard.domain import authority_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import ItemId, LeaseId
from pinboard.interfaces import cli_commands, work_views
from pinboard.interfaces.cli_output import authority_lease_fields, retained_authority_lease_fields, write_json
from pinboard.interfaces.errors import CommandErrorCode, CommandFailure, CommandResult


def _find_retained_preparation_claim(
    state: stored_state.StoredWorkState, item_id: ItemId, evaluated_at: datetime
) -> CommandResult[tuple[stored_state.StoredPreparationLease, stored_state.PreparationLeaseGeneration]]:
    retained = stored_state.retained_preparation(state, item_id)
    if retained is None:
        return CommandFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, f"Item '{item_id}' has no preparation claim.")
    lease, anchor = retained
    if anchor is None:
        return CommandFailure(CommandErrorCode.WORK_STATE_INVALID, "Preparation authority has no identity anchor.")
    if lease.state == authority_models.PreparationLeaseStatus.ACTIVE and lease.expires_at <= evaluated_at:
        lease = replace(lease, state=authority_models.PreparationLeaseStatus.EXPIRED)
    return lease, anchor


def _present_latest_preparation_authority(
    state: stored_state.StoredWorkState,
    item_id: ItemId,
    presented_at: datetime,
    *,
    json: bool,
) -> CommandResult[int]:
    retained = _find_retained_preparation_claim(state, item_id, presented_at)
    if isinstance(retained, CommandFailure):
        return retained
    lease, _anchor = retained
    values: dict[str, str | int] = {
        "item_id": str(item_id),
        "definition_revision": lease.definition_revision,
        "definition_digest": lease.definition_digest,
        **retained_authority_lease_fields(retained),
    }
    if json:
        write_json(values)
    else:
        print("OK " + " ".join(f"{key}={value}" for key, value in values.items()))
    return 0


def show_preparation_authority_status(
    roots: cli_commands.ResolvedRoots, command: cli_commands.PreparationStatusCommand
) -> CommandResult[int]:
    presented_at = datetime.now(UTC)
    latest_committed_state = SQLiteWorkStore(roots.work / "state.sqlite3").snapshot()
    return _present_latest_preparation_authority(
        latest_committed_state, command.item_id, presented_at, json=command.json
    )


def start_preparation(
    roots: cli_commands.ResolvedRoots, command: cli_commands.PreparationStartCommand
) -> CommandResult[int]:
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    requested_at = datetime.now(UTC)
    committed = service.start_preparation(
        store,
        item_id=command.item_id,
        task_id=command.task_id,
        host_id=command.host_id,
        lease_id=LeaseId(uuid4().hex),
        acquired_at=requested_at,
        expires_at=requested_at + timedelta(seconds=command.ttl_seconds),
    )
    if isinstance(committed, DecisionFailure):
        return CommandFailure(committed.code, committed.message)
    refreshed = work_views.refresh(
        roots, store, AffectedViews(queue=True, items=(command.item_id,), history=True), datetime.now(UTC)
    )
    if refreshed.warning is not None:
        print(refreshed.warning.message, file=sys.stderr)
    values = {
        "item_id": committed.item,
        "definition_revision": committed.definition_revision,
        "definition_digest": committed.definition_digest,
        **authority_lease_fields(
            task_id=committed.task_id,
            host_id=committed.host_id,
            lease_id=committed.lease_id,
            generation=committed.generation,
            acquired_at=committed.acquired_at,
            expires_at=committed.expires_at,
            status=committed.state.value,
        ),
    }
    if command.json:
        write_json(values)
    else:
        print("OK " + " ".join(f"{key}={value}" for key, value in values.items()))
    return 0


def _resolve_supplied_preparation_authority(
    observed_state: stored_state.StoredWorkState,
    item_id: ItemId,
    lease_id: LeaseId,
    generation: int,
    requested_at: datetime,
) -> CommandResult[work_models.PreparationCommandAuthority]:
    observed_authority = next(
        (
            value
            for value in project_decision_snapshot(observed_state, requested_at).command_preparation_authorities
            if value.item == item_id
        ),
        None,
    )
    if observed_authority is None:
        return CommandFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation authority is not active.")
    return replace(observed_authority, lease_id=lease_id, generation=generation)


def _resolve_requested_preparation_change(
    observed_state: stored_state.StoredWorkState,
    command: (
        cli_commands.PreparationAcquireCommand
        | cli_commands.PreparationTransferCommand
        | cli_commands.PreparationRenewCommand
        | cli_commands.PreparationReleaseCommand
        | cli_commands.PreparationRevokeCommand
    ),
    requested_at: datetime,
) -> CommandResult[authority_models.PreparationAuthorityOperation]:
    match command:
        case cli_commands.PreparationAcquireCommand():
            return authority_models.AcquireInitialPreparationAuthority(
                host_epoch=observed_state.lifecycle.project.host_epoch,
                item=command.item_id,
                expected_project_revision=command.expected_project_revision,
                expected_item_subject_revision=command.expected_item_subject_revision,
                expected_definition_revision=command.expected_definition_revision,
                expected_definition_digest=command.expected_definition_digest,
                task_id=command.task_id,
                host_id=command.host_id,
                lease_id=LeaseId(uuid4().hex),
                acquired_at=requested_at,
                expires_at=requested_at + timedelta(seconds=command.ttl_seconds),
            )
        case cli_commands.PreparationTransferCommand():
            retained = _find_retained_preparation_claim(observed_state, command.item_id, requested_at)
            if isinstance(retained, CommandFailure):
                return retained
            lease, anchor = retained
            if lease.state == authority_models.PreparationLeaseStatus.ACTIVE:
                return CommandFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation authority remains live.")
            return authority_models.TransferPreparationAuthority(
                current=authority_models.InactivePreparationAuthority(
                    host_epoch=observed_state.lifecycle.project.host_epoch,
                    item=lease.item_id,
                    definition_revision=lease.definition_revision,
                    definition_digest=lease.definition_digest,
                    task_id=anchor.task_id,
                    host_id=anchor.host_id,
                    lease_id=anchor.lease_id,
                    generation=lease.generation,
                    expires_at=lease.expires_at,
                    state=lease.state,
                ),
                task_id=command.task_id,
                host_id=command.host_id,
                lease_id=LeaseId(uuid4().hex),
                acquired_at=requested_at,
                expires_at=requested_at + timedelta(seconds=command.ttl_seconds),
            )
        case cli_commands.PreparationRenewCommand():
            supplied_authority = _resolve_supplied_preparation_authority(
                observed_state, command.item_id, command.lease_id, command.generation, requested_at
            )
            if isinstance(supplied_authority, CommandFailure):
                return supplied_authority
            return authority_models.RenewPreparationAuthority(
                current=supplied_authority,
                renewed_at=requested_at,
                expires_at=requested_at + timedelta(seconds=command.ttl_seconds),
            )
        case cli_commands.PreparationReleaseCommand():
            supplied_authority = _resolve_supplied_preparation_authority(
                observed_state, command.item_id, command.lease_id, command.generation, requested_at
            )
            if isinstance(supplied_authority, CommandFailure):
                return supplied_authority
            return authority_models.ReleasePreparationAuthority(current=supplied_authority, released_at=requested_at)
        case cli_commands.PreparationRevokeCommand():
            return authority_models.RevokePreparationAuthority(
                item=command.item_id,
                lease_id=command.lease_id,
                generation=command.generation,
                task_id=command.task_id,
                host_id=command.host_id,
                revoked_at=requested_at,
            )
        case _ as unreachable:
            assert_never(unreachable)


def change_preparation_authority(
    roots: cli_commands.ResolvedRoots,
    command: (
        cli_commands.PreparationAcquireCommand
        | cli_commands.PreparationTransferCommand
        | cli_commands.PreparationRenewCommand
        | cli_commands.PreparationReleaseCommand
        | cli_commands.PreparationRevokeCommand
    ),
) -> CommandResult[int]:
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    observed_state = store.snapshot()
    requested_at = datetime.now(UTC)
    requested_change = _resolve_requested_preparation_change(observed_state, command, requested_at)
    if isinstance(requested_change, CommandFailure):
        return requested_change
    commit_result = decide_and_commit_preparation_authority_change(store, requested_change)
    if isinstance(commit_result, DecisionFailure):
        return CommandFailure(commit_result.code, commit_result.message)
    refresh_result = work_views.refresh(
        roots,
        store,
        AffectedViews(queue=True, items=(command.item_id,), history=True),
        datetime.now(UTC),
    )
    if refresh_result.warning is not None:
        print(refresh_result.warning.message, file=sys.stderr)
    presented_at = datetime.now(UTC)
    latest_committed_state = store.snapshot()
    return _present_latest_preparation_authority(
        latest_committed_state, command.item_id, presented_at, json=command.json
    )
