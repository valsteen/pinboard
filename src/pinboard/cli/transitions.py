"""Compose the retained direct human close with shared locked lifecycle rules."""

import sys
from datetime import UTC, datetime

import msgspec

from pinboard.adapters import lifecycle_operations
from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.transition_input import TransitionInputFailure, parse_transition_input
from pinboard.application import actions, ports, query_models
from pinboard.cli import cli_commands, work_views
from pinboard.cli.cli_output import write_json
from pinboard.cli.errors import CommandFailure, CommandResult
from pinboard.domain import decision_models
from pinboard.domain.errors import DecisionFailure, EffectDisposition, FailureAction, FailureDetails, RetryDisposition
from pinboard.domain.identifiers import ActionId


class CloseView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    outcome: str
    reason: str
    revision: str


def _with_close_alternatives(
    store: ports.WorkStore,
    selected_action: decision_models.CloseAction,
    failure: CommandFailure,
) -> CommandFailure:
    item_ids, attempt_ids, proposal_ids = actions.action_subject_ids(selected_action)
    current = actions.discover_current_actions(
        store.read_decision_facts(
            query_models.DecisionScope(item_ids, (), (), (), attempt_ids, proposal_ids, (), ()),
            datetime.now(UTC),
        ).snapshot,
        decision_models.Role.PROJECT,
        lease_id=None,
        generation=0,
    )
    if isinstance(current, DecisionFailure):
        return failure
    alternatives = tuple(
        FailureAction(
            decision_models.action_id(value),
            "project",
            value.capability.subject_revision,
            None if value.capability.authorization is None else value.capability.authorization.value,
            None if value.capability.lease_id is None else str(value.capability.lease_id),
            value.capability.command_authority.generation
            if value.capability.command_authority is not None
            else value.capability.preparation_authority.generation
            if value.capability.preparation_authority is not None
            else None,
        )
        for value in current
        if isinstance(value.capability, decision_models.MutationActionCapability)
        and value.capability.subject == selected_action.capability.subject
        and not isinstance(value, decision_models.CompleteAction)
    )
    details = failure.details
    return CommandFailure(
        failure.code,
        failure.message,
        FailureDetails(
            observed=() if details is None else details.observed,
            mismatches=() if details is None else details.mismatches,
            retry=RetryDisposition.DO_NOT_RETRY if details is None else details.retry,
            effect=EffectDisposition.UNCHANGED if details is None else details.effect,
            changed_surfaces=() if details is None else details.changed_surfaces,
            alternatives=alternatives,
        ),
    )


def close(
    durable: DurableRoots,
    store: ports.WorkStore,
    command: cli_commands.CloseCommand,
) -> CommandResult[int]:
    selected = actions.select_current_actions(
        store,
        decision_models.Role.PROJECT,
        observed_at=datetime.now(UTC),
        lease_id=None,
        generation=None,
        action_id=ActionId(f"close:{command.item_id}"),
    )
    if isinstance(selected, DecisionFailure):
        return CommandFailure(selected.code, selected.message, selected.details)
    selected_action = selected[0]
    if not isinstance(selected_action, decision_models.CloseAction):
        raise AssertionError("The exact close identity must select a close action.")
    decoded = parse_transition_input(
        selected_action,
        msgspec.json.encode({"outcome": command.outcome.value, "reason": command.reason}, order="sorted"),
    )
    if isinstance(decoded, TransitionInputFailure):
        return CommandFailure(decoded.code, decoded.message, decoded.details)
    assert isinstance(decoded, decision_models.CloseCommand)
    committed = lifecycle_operations.commit_direct_transition(
        store,
        ArtifactRepository(durable),
        lifecycle_operations.SelectedTransition(selected_action, decoded, command.task_id, command.host_id),
        datetime.now(UTC),
        lambda: datetime.now(UTC),
    )
    if isinstance(committed, DecisionFailure):
        return _with_close_alternatives(
            store, selected_action, CommandFailure(committed.code, committed.message, committed.details)
        )
    views = work_views.refresh_effect(durable, store, committed, datetime.now(UTC))
    if views.warning is not None:
        print(views.warning.message, file=sys.stderr)
    value = CloseView(command.item_id, command.outcome.value, command.reason, str(committed.receipt.project_revision))
    if command.json:
        write_json(value)
    else:
        print(f"OK WORK_ITEM_CLOSED item={value.item_id} outcome={value.outcome} revision={value.revision}")
    return 0
