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

from pinboard.adapters.files.file_io import DurableRoots
from pinboard.application import authority_operations, ports, queries, query_models
from pinboard.application.service import decide_and_commit_preparation_authority_change
from pinboard.cli import cli_commands, work_views
from pinboard.cli.cli_output import (
    authority_lease_fields,
    authority_status_fields,
    write_json,
)
from pinboard.cli.errors import CommandFailure, CommandResult
from pinboard.domain import authority_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import ItemId, LeaseId
from pinboard.domain.ledger import LedgerSnapshot


def _find_retained_preparation_claim(
    store: ports.WorkStore, item_id: ItemId, evaluated_at: datetime
) -> CommandResult[query_models.PreparationAuthorityStatus]:
    retained = store.read_preparation_authority_status(item_id)
    if retained is None:
        return CommandFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, f"Item '{item_id}' has no preparation claim.", None
        )
    if retained.status == authority_models.PreparationLeaseStatus.ACTIVE and retained.expires_at <= evaluated_at:
        return replace(retained, status=authority_models.PreparationLeaseStatus.EXPIRED)
    return retained


def _present_latest_preparation_authority(
    store: ports.WorkStore,
    item_id: ItemId,
    presented_at: datetime,
    *,
    json: bool,
) -> CommandResult[int]:
    retained = _find_retained_preparation_claim(store, item_id, presented_at)
    if isinstance(retained, CommandFailure):
        return retained
    values: dict[str, str | int] = {
        "item_id": str(item_id),
        "definition_revision": retained.definition_revision,
        "definition_digest": retained.definition_digest,
        **authority_status_fields(retained),
    }
    if json:
        write_json(values)
    else:
        print("OK " + " ".join(f"{key}={value}" for key, value in values.items()))
    return 0


def show_preparation_authority_status(
    store: ports.WorkStore,
    command: cli_commands.PreparationStatusCommand,
) -> CommandResult[int]:
    presented_at = datetime.now(UTC)
    selected = queries.select_preparation_authority_status(store, command.item_id, presented_at)
    if isinstance(selected, DecisionFailure):
        return CommandFailure(selected.code, selected.message, selected.details)
    values: dict[str, str | int] = {
        "item_id": str(selected.item_id),
        "definition_revision": selected.definition_revision,
        "definition_digest": selected.definition_digest,
        **authority_status_fields(selected),
    }
    if command.json:
        write_json(values)
    else:
        print("OK " + " ".join(f"{key}={value}" for key, value in values.items()))
    return 0


def start_preparation(
    durable: DurableRoots,
    store: ports.WorkStore,
    command: cli_commands.PreparationStartCommand,
) -> CommandResult[int]:
    requested_at = datetime.now(UTC)
    committed = authority_operations.start_preparation_authority(
        store,
        item_id=command.item_id,
        task_id=command.task_id,
        host_id=command.host_id,
        lease_id=LeaseId(uuid4().hex),
        acquired_at=requested_at,
        expires_at=requested_at + timedelta(seconds=command.ttl_seconds),
    )
    if isinstance(committed, DecisionFailure):
        return CommandFailure(committed.code, committed.message, committed.details)
    refreshed = work_views.refresh_effect(durable, store, committed.effect, datetime.now(UTC))
    if refreshed.warning is not None:
        print(refreshed.warning.message, file=sys.stderr)
    values = {
        "item_id": committed.authority.item_id,
        "definition_revision": committed.authority.definition_revision,
        "definition_digest": committed.authority.definition_digest,
        **authority_lease_fields(
            task_id=committed.authority.task_id,
            host_id=committed.authority.host_id,
            lease_id=committed.authority.lease_id,
            generation=committed.authority.generation,
            acquired_at=committed.authority.acquired_at,
            expires_at=committed.authority.expires_at,
            status=committed.authority.status.value,
        ),
    }
    if command.json:
        write_json(values)
    else:
        print("OK " + " ".join(f"{key}={value}" for key, value in values.items()))
    return 0


def _resolve_requested_preparation_change(
    store: ports.WorkStore,
    snapshot: LedgerSnapshot,
    command: (cli_commands.PreparationAcquireCommand | cli_commands.PreparationTransferCommand),
    requested_at: datetime,
) -> CommandResult[authority_models.AcquireInitialPreparationAuthority | authority_models.TransferPreparationAuthority]:
    match command:
        case cli_commands.PreparationAcquireCommand():
            return authority_models.AcquireInitialPreparationAuthority(
                host_epoch=snapshot.host_epoch,
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
            retained = _find_retained_preparation_claim(store, command.item_id, requested_at)
            if isinstance(retained, CommandFailure):
                return retained
            if retained.status == authority_models.PreparationLeaseStatus.ACTIVE:
                return CommandFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation authority remains live.", None
                )
            return authority_models.TransferPreparationAuthority(
                current=authority_models.InactivePreparationAuthority(
                    host_epoch=snapshot.host_epoch,
                    item=retained.item_id,
                    definition_revision=retained.definition_revision,
                    definition_digest=retained.definition_digest,
                    task_id=retained.task_id,
                    host_id=retained.host_id,
                    lease_id=retained.lease_id,
                    generation=retained.generation,
                    expires_at=retained.expires_at,
                    state=retained.status,
                ),
                task_id=command.task_id,
                host_id=command.host_id,
                lease_id=LeaseId(uuid4().hex),
                acquired_at=requested_at,
                expires_at=requested_at + timedelta(seconds=command.ttl_seconds),
            )
        case _ as unreachable:
            assert_never(unreachable)


def change_preparation_authority(
    durable: DurableRoots,
    store: ports.WorkStore,
    command: (
        cli_commands.PreparationAcquireCommand
        | cli_commands.PreparationTransferCommand
        | cli_commands.PreparationRenewCommand
        | cli_commands.PreparationReleaseCommand
        | cli_commands.PreparationRevokeCommand
    ),
) -> CommandResult[int]:
    requested_at = datetime.now(UTC)
    match command:
        case cli_commands.PreparationAcquireCommand() | cli_commands.PreparationTransferCommand():
            snapshot = store.read_decision_facts(
                query_models.DecisionScope((command.item_id,), (), (), (), (), (), (), ()), requested_at
            ).snapshot
            requested_change = _resolve_requested_preparation_change(store, snapshot, command, requested_at)
            if isinstance(requested_change, CommandFailure):
                return requested_change
            commit_result = decide_and_commit_preparation_authority_change(store, requested_change)
        case cli_commands.PreparationRenewCommand():
            committed = authority_operations.renew_preparation_authority(
                store,
                item_id=command.item_id,
                lease_id=command.lease_id,
                generation=command.generation,
                renewed_at=requested_at,
                expires_at=requested_at + timedelta(seconds=command.ttl_seconds),
            )
            if isinstance(committed, DecisionFailure):
                return CommandFailure(committed.code, committed.message, committed.details)
            commit_result = committed.effect
        case cli_commands.PreparationReleaseCommand():
            committed = authority_operations.release_preparation_authority(
                store,
                item_id=command.item_id,
                lease_id=command.lease_id,
                generation=command.generation,
                released_at=requested_at,
            )
            if isinstance(committed, DecisionFailure):
                return CommandFailure(committed.code, committed.message, committed.details)
            commit_result = committed.effect
        case cli_commands.PreparationRevokeCommand():
            committed = authority_operations.revoke_preparation_authority(
                store,
                item_id=command.item_id,
                lease_id=command.lease_id,
                generation=command.generation,
                actor_task_id=command.task_id,
                actor_host_id=command.host_id,
                revoked_at=requested_at,
            )
            if isinstance(committed, DecisionFailure):
                return CommandFailure(committed.code, committed.message, committed.details)
            commit_result = committed.effect
        case _ as unreachable:
            assert_never(unreachable)
    if isinstance(commit_result, DecisionFailure):
        return CommandFailure(commit_result.code, commit_result.message, commit_result.details)
    refresh_result = work_views.refresh_effect(durable, store, commit_result, datetime.now(UTC))
    if refresh_result.warning is not None:
        print(refresh_result.warning.message, file=sys.stderr)
    presented_at = datetime.now(UTC)
    return _present_latest_preparation_authority(store, command.item_id, presented_at, json=command.json)
