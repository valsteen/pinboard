"""Read-only installed work-inspection composition and presentation.

Functions in this module may read an explicitly selected SQLite work root and
write command output. They never mutate the ledger, publish artifacts, change
authority, refresh generated views, obtain a lease, or own a transaction.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import actions as action_queries
from pinboard.application import queries, query_models, stored_state
from pinboard.domain import decision_models, work_models
from pinboard.domain import errors as domain_errors
from pinboard.interfaces import cli_commands, errors, transition_input, work_inspection_models
from pinboard.interfaces.cli_output import write_json


def _project_action_semantics(
    semantics: decision_models.ActionSemantics,
) -> work_inspection_models.ActionSemanticsView:
    """Preserve the stable `effect` field while naming its narrower lifecycle meaning internally."""

    return work_inspection_models.ActionSemanticsView(
        semantics.use_case,
        semantics.lifecycle_effect.value,
        tuple(role.value for role in semantics.permitted_roles),
        semantics.subject_kind.value,
        semantics.lifecycle_precondition.value,
        semantics.practical_result,
    )


def describe_input_contract(
    kind: decision_models.ActionKind,
) -> errors.TransitionInputResult[work_inspection_models.InputContractView]:
    semantics = decision_models.action_semantics(kind)
    if semantics.lifecycle_effect == decision_models.LifecycleEffect.NO_LIFECYCLE_CHANGE:
        payload_schema = None
    else:
        encoded_schema = transition_input.encoded_transition_input_schema(kind)
        if isinstance(encoded_schema, errors.TransitionInputFailure):
            return encoded_schema
        payload_schema = msgspec.Raw(encoded_schema)
    return work_inspection_models.InputContractView(kind.value, _project_action_semantics(semantics), payload_schema)


def project_action(
    action: decision_models.Action,
    *,
    include_input_contract: bool = False,
) -> errors.TransitionInputResult[work_inspection_models.ActionView]:
    capability = action.capability
    input_contract: work_inspection_models.InputContractView | None = None
    if include_input_contract:
        contract = describe_input_contract(action.kind)
        if isinstance(contract, errors.TransitionInputFailure):
            return contract
        input_contract = contract
    return work_inspection_models.ActionView(
        action_id=decision_models.action_id(action),
        kind=action.kind.value,
        subject=capability.subject,
        label=capability.label,
        expected_revision=capability.expected_revision,
        coordinator_generation=capability.coordinator_generation,
        subject_revision=capability.subject_revision or "",
        authorization="observer" if capability.authorization is None else capability.authorization.value,
        lease_id=capability.lease_id or "",
        semantics=_project_action_semantics(decision_models.action_semantics(action.kind)),
        input_contract=input_contract,
    )


def project_parallel_preview(
    preview: query_models.ParallelPreview,
) -> work_inspection_models.ParallelPreviewView:
    launchable: list[work_inspection_models.ParallelItemView] = []
    excluded: list[work_inspection_models.ParallelItemView] = []
    for item in preview.items:
        match item:
            case query_models.LaunchableParallelItem():
                launchable.append(
                    work_inspection_models.ParallelItemView(
                        item.item_id,
                        item.label,
                        item.state.value,
                        item.attempt_id,
                        "launchable",
                        (),
                    )
                )
            case query_models.ExcludedParallelItem(reasons=reasons):
                excluded.append(
                    work_inspection_models.ParallelItemView(
                        item.item_id,
                        item.label,
                        item.state.value,
                        item.attempt_id,
                        "excluded",
                        reasons,
                    )
                )
            case _ as unreachable:
                assert_never(unreachable)
    return work_inspection_models.ParallelPreviewView(
        preview.schema,
        preview.revision,
        preview.selection.value,
        preview.safe,
        tuple(launchable),
        tuple(excluded),
    )


def compose_status(
    facts: stored_state.StatusFacts,
    work: Path,
    source_checkout: Path,
    shared_repository: Path,
) -> work_inspection_models.StatusView:
    coordinator = facts.coordination
    counts = {value.state.value: value.count for value in facts.counts}
    return work_inspection_models.StatusView(
        stored_state_opened=True,
        source_checkout_root=str(source_checkout),
        shared_repository_root=str(shared_repository),
        work_root=str(work),
        revision=str(facts.project.revision),
        focus_item=None if facts.focus.item_id is None else str(facts.focus.item_id),
        focus_attempt=None if facts.focus.attempt_id is None else str(facts.focus.attempt_id),
        active_attempts=tuple(str(value) for value in facts.active_attempts),
        next_action=facts.focus.next_action,
        counts=counts,
        intake_item_count=counts.get(stored_state.StoredWorkItemState.INTAKE.value, 0),
        coordinator=(
            work_inspection_models.CoordinatorView(
                str(coordinator.task_id),
                str(coordinator.host_id),
                coordinator.generation,
                str(coordinator.lease_id),
                coordinator.expires_at.isoformat(),
                coordinator.state.value,
            )
            if coordinator is not None
            else None
        ),
        authority="sqlite-v3",
    )


def show_status(roots: cli_commands.ResolvedRoots, command: cli_commands.StatusCommand) -> int:
    current_state = SQLiteWorkStore(roots.work / "state.sqlite3").status_facts()
    status_projection = compose_status(current_state, roots.work, roots.source_checkout, roots.shared_repository)
    if command.json:
        write_json(status_projection)
    else:
        print(f"OK WORK_STATE_VALID revision={status_projection.revision}")
        print(
            f"focus_item={status_projection.focus_item or 'none'} "
            f"focus_attempt={status_projection.focus_attempt or 'none'}"
        )
        print(f"next_action={status_projection.next_action} intake_items={status_projection.intake_item_count}")
    return 0


def show_overview(roots: cli_commands.ResolvedRoots, command: cli_commands.OverviewCommand) -> int:
    operation_time = datetime.now(UTC)
    current_state = SQLiteWorkStore(roots.work / "state.sqlite3").live_state()
    overview_projection = queries.project_overview(current_state, operation_time)
    if command.json:
        write_json(overview_projection)
        return 0
    print(f"OK WORK_OVERVIEW revision={overview_projection.revision} authority={overview_projection.authority}")
    if not overview_projection.items:
        print("live_work=none")
    for item in overview_projection.items:
        position = item.position if item.position is not None else "none"
        attempt = f" attempt={item.attempt_id}" if item.attempt_id is not None else ""
        preparation = (
            " preparation=none"
            if item.preparation is None
            else (
                f" preparation={item.preparation.status.value}"
                f" preparer={item.preparation.task_id}@{item.preparation.host_id}"
                f" preparation_generation={item.preparation.generation}"
                f" preparation_expires_at={item.preparation.expires_at}"
            )
        )
        next_action = item.next_action or "none"
        print(
            f"{position}\t{item.item_id}\t{item.state.value}\teligible={str(item.eligible).lower()}"
            f"\tnext={next_action}{attempt}{preparation}\t{item.label}"
        )
    print(
        f"intake_items={sum(1 for item in overview_projection.items if item.state == work_models.WorkState.INTAKE)} "
        f"immediate_options={len(overview_projection.immediate_options)}"
    )
    return 0


def show_item_status(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.ItemStatusCommand,
) -> errors.CommandResult[int]:
    operation_time = datetime.now(UTC)
    current_state = SQLiteWorkStore(roots.work / "state.sqlite3").item_status_state(command.item_id)
    item_projection = queries.project_item_status(current_state, command.item_id, operation_time)
    if isinstance(item_projection, domain_errors.DecisionFailure):
        return errors.CommandFailure(item_projection.code, item_projection.message)
    if command.json:
        write_json(item_projection)
        return 0
    print(
        f"OK ITEM_STATUS item={item_projection.item_id} state={item_projection.state.value} "
        f"revision={item_projection.revision} authority={item_projection.authority}"
    )
    print(
        f"label={item_projection.label} "
        f"timing={item_projection.timing.value if item_projection.timing is not None else 'none'} "
        f"queue_position={item_projection.queue_position if item_projection.queue_position is not None else 'none'} "
        f"next_action={item_projection.next_action or 'none'}"
    )
    print(
        f"outcome_evidence={item_projection.outcome_evidence or 'none'} source={item_projection.source or 'none'} notes={item_projection.notes or 'none'}"
    )
    if item_projection.preparation is None:
        print("preparation=none")
    else:
        print(
            f"preparation={item_projection.preparation.status.value} "
            f"preparer={item_projection.preparation.task_id}@{item_projection.preparation.host_id} "
            f"lease_id={item_projection.preparation.lease_id} generation={item_projection.preparation.generation} "
            f"expires_at={item_projection.preparation.expires_at} "
            f"definition_revision={item_projection.preparation.definition_revision} "
            f"definition_digest={item_projection.preparation.definition_digest}"
        )
    if not item_projection.attempts:
        print("attempts=none")
    for attempt in item_projection.attempts:
        print(
            f"attempt={attempt.attempt_id} state={attempt.state.value} candidate={attempt.candidate_revision or 'none'}"
        )
    return 0


def show_item_definition(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.ItemDefinitionCommand,
) -> errors.CommandResult[int]:
    current_state = SQLiteWorkStore(roots.work / "state.sqlite3").item_definition_state(command.item_id)
    definition_projection = queries.project_item_definition(current_state, command.item_id)
    if isinstance(definition_projection, domain_errors.DecisionFailure):
        return errors.CommandFailure(definition_projection.code, definition_projection.message)
    if command.json:
        write_json(definition_projection)
    else:
        print(
            f"OK ITEM_DEFINITION item={definition_projection.item_id} "
            f"item_subject_revision={definition_projection.item_subject_revision} "
            f"definition_revision={definition_projection.definition_revision} "
            f"definition_digest={definition_projection.definition_digest} "
            f"project_revision={definition_projection.project_revision}"
        )
        print(f"title={definition_projection.definition.title}")
    return 0


def show_item_definition_history(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.ItemDefinitionHistoryCommand,
) -> errors.CommandResult[int]:
    current_state = SQLiteWorkStore(roots.work / "state.sqlite3").item_definition_state(
        command.item_id,
        history_limit=command.limit + 1,
        before_revision=command.before_revision,
    )
    history_projection = queries.project_item_definition_history(
        current_state,
        command.item_id,
        limit=command.limit,
        before_revision=command.before_revision,
    )
    if isinstance(history_projection, domain_errors.DecisionFailure):
        return errors.CommandFailure(history_projection.code, history_projection.message)
    if command.json:
        write_json(history_projection)
    else:
        print(
            f"OK ITEM_DEFINITION_HISTORY item={history_projection.item_id} "
            f"revisions={len(history_projection.revisions)} project_revision={history_projection.project_revision}"
        )
        for revision in history_projection.revisions:
            print(
                f"revision={revision.revision} digest={revision.digest} "
                f"source_task={revision.source_task} timestamp={revision.timestamp}"
            )
    return 0


def show_actions(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.ActionsCommand | cli_commands.LeasedActionsCommand,
) -> errors.CommandResult[int]:
    match command:
        case cli_commands.ActionsCommand():
            lease_id = None
            generation = None
        case cli_commands.LeasedActionsCommand(lease_id=lease_id, generation=generation):
            pass
        case _ as unreachable:
            assert_never(unreachable)
    operation_time = datetime.now(UTC)
    current_state = SQLiteWorkStore(roots.work / "state.sqlite3").live_state()
    available_actions = action_queries.discover_actions(
        current_state,
        command.role,
        lease_id=lease_id,
        generation=generation,
        now=operation_time,
    )
    if isinstance(available_actions, domain_errors.DecisionFailure):
        return errors.CommandFailure(available_actions.code, available_actions.message)
    exact_action_id = command.action_id
    if exact_action_id is not None:
        available_actions = tuple(
            action for action in available_actions if decision_models.action_id(action) == exact_action_id
        )
        if not available_actions:
            return errors.CommandFailure(
                domain_errors.DecisionFailureCode.ACTION_NOT_AVAILABLE,
                f"Action '{exact_action_id}' is not currently legal for this role and lease.",
            )
    if command.json:
        action_views: list[work_inspection_models.ActionView] = []
        for action in available_actions:
            projected_action = project_action(action, include_input_contract=exact_action_id is not None)
            if isinstance(projected_action, errors.TransitionInputFailure):
                return errors.CommandFailure(projected_action.code, projected_action.message)
            action_views.append(projected_action)
        write_json(work_inspection_models.ActionsView(tuple(action_views)))
    elif not available_actions:
        print("OK NO_ACTIONS_AVAILABLE")
    else:
        for action in available_actions:
            print(f"{decision_models.action_id(action)}\t{action.capability.label}")
    return 0


def show_input_contract(
    command: cli_commands.InputContractCommand,
) -> errors.CommandResult[int]:
    contract = describe_input_contract(command.action_kind)
    if isinstance(contract, errors.TransitionInputFailure):
        return errors.CommandFailure(contract.code, contract.message)
    if command.json:
        write_json(contract)
    else:
        print(f"OK INPUT_CONTRACT action_kind={contract.action_kind}")
        print(f"use_case={contract.semantics.use_case}")
        print(
            f"effect={contract.semantics.effect} "
            f"permitted_roles={','.join(contract.semantics.permitted_roles)} "
            f"subject_kind={contract.semantics.subject_kind} "
            f"lifecycle_precondition={contract.semantics.lifecycle_precondition}"
        )
        print(f"practical_result={contract.semantics.practical_result}")
        if contract.payload_schema is None:
            print("payload_schema=none")
        else:
            sys.stdout.write(msgspec.json.format(bytes(contract.payload_schema), indent=2).decode() + "\n")
    return 0


def _print_parallel_group(title: str, items: tuple[work_inspection_models.ParallelItemView, ...]) -> None:
    print(f"{title}:")
    if not items:
        print("- none")
        return
    for item in items:
        detail = "; ".join(reason.message for reason in item.reasons)
        attempt = f", attempt {item.attempt_id}" if item.attempt_id is not None else ""
        suffix = f" — {detail}" if detail else ""
        print(f"- {item.item_id} ({item.state}{attempt}){suffix}")


def show_parallel_preview(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.ParallelPreviewCommand,
) -> errors.CommandResult[int]:
    operation_time = datetime.now(UTC)
    current_state = SQLiteWorkStore(roots.work / "state.sqlite3").live_state()
    preview = queries.project_parallel_preview(
        current_state,
        selected=tuple(command.item),
        now=operation_time,
    )
    if isinstance(preview, query_models.ParallelSelectionInvalid):
        return errors.CommandFailure(errors.CommandErrorCode.PARALLEL_SELECTION_INVALID, preview.message)
    view = project_parallel_preview(preview)
    if command.json:
        write_json(view)
    else:
        print(
            f"OK PARALLEL_PREVIEW revision={preview.revision} selection={preview.selection.value} "
            f"safe={'yes' if preview.safe else 'no'}"
        )
        _print_parallel_group("Ready to launch together", view.launchable)
        _print_parallel_group("Not launchable", view.excluded)
    return 0
