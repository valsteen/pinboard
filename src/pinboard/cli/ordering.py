"""Decode a full live order, commit it, and publish only its changed views."""

import sys
from datetime import UTC, datetime
from typing import Literal

import msgspec

from pinboard.adapters.files.file_io import DurableRoots
from pinboard.application import ports, service
from pinboard.cli import cli_commands, work_views
from pinboard.cli.cli_output import write_json
from pinboard.cli.errors import CommandFailure, CommandResult
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import ItemId
from pinboard.domain.ordering import OrderRequest


class OrderedView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-live-order-committed/v1"]
    order: tuple[ItemId, ...]
    committed_revision: int
    history_id: int


def reorder(durable: DurableRoots, store: ports.WorkStore, command: cli_commands.OrderCommand) -> CommandResult[int]:
    try:
        request = msgspec.json.decode(command.file.read_bytes(), type=OrderRequest, strict=True)
    except (OSError, msgspec.DecodeError) as error:
        return CommandFailure(DecisionFailureCode.TRANSITION_INPUT_INVALID, f"Invalid order request: {error}", None)
    now = datetime.now(UTC)
    effect = service.reorder(
        store, request.expected_order, request.requested_order, command.task_id, command.host_id, now
    )
    if isinstance(effect, DecisionFailure):
        return CommandFailure(effect.code, effect.message, effect.details)
    refreshed = work_views.refresh_effect(durable, store, effect, now)
    if refreshed.warning is not None:
        print(refreshed.warning.message, file=sys.stderr)
    result = OrderedView(
        "pinboard-live-order-committed/v1",
        request.requested_order,
        effect.receipt.project_revision,
        int(effect.receipt.history_id),
    )
    if command.json:
        write_json(result)
    else:
        print(f"OK LIVE_ORDER_COMMITTED revision={result.committed_revision} history_id={result.history_id}")
        print("order=" + ",".join(result.order))
    return 0
