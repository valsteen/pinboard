"""Compose and validate complete state on a supplied connection.

This module reads and writes SQLite but never commits, rolls back, closes the
connection, calls callbacks, reads the filesystem, or obtains time. SQLite and
persisted-invariant failures remain exceptional; the transaction owner stays in
``persistence``.
"""

import hashlib
import sqlite3
from collections import Counter
from collections.abc import Mapping, Set
from itertools import pairwise
from types import MappingProxyType

import msgspec

from pinboard.adapters.sqlite import initiatives
from pinboard.adapters.sqlite.artifacts import read_artifacts
from pinboard.adapters.sqlite.authority import read_authority, validate_attempt_authority
from pinboard.adapters.sqlite.database import decode_row, select_by_ids
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import (
    TransitionHistoryRow,
    read_lifecycle,
    stored_receipt_from_row,
    validate_current_attempt_relation,
)
from pinboard.adapters.sqlite.pr_review import validate_review_history
from pinboard.adapters.sqlite.proposals import read_pending_proposals, read_proposals
from pinboard.application import (
    consolidation,
    consolidation_models,
    decision_batches,
    project_export,
    stored_state,
    unblockers,
)
from pinboard.domain import authority_models, decision_models, history, work_models
from pinboard.domain.identifiers import (
    AttemptId,
    CheckpointId,
    HistoryId,
    WorkItemId,
)
from pinboard.domain.ledger import LedgerSnapshot


class _StateCountRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    state: stored_state.StoredWorkItemState
    item_count: int


def _read_replacements(connection: sqlite3.Connection) -> stored_state.ReplacementRecords:
    planned = tuple(
        decode_row(row, stored_state.StoredPlannedReplacement)
        for row in connection.execute(
            """
            SELECT affected_item_id, relation_revision, replacement_item_id, replacement_cost,
                   status, recorded_by, recorded_at, accepted_project_revision
            FROM planned_replacements
            ORDER BY affected_item_id, relation_revision
            """
        ).fetchall()
    )
    dispositions = tuple(
        decode_row(row, stored_state.StoredReplacementDisposition)
        for row in connection.execute(
            """
            SELECT affected_item_id, relation_revision, rationale, accepted_cost,
                   recorded_by, recorded_at, accepted_project_revision
            FROM replacement_dispositions
            ORDER BY affected_item_id, relation_revision
            """
        ).fetchall()
    )
    return stored_state.ReplacementRecords(planned, dispositions)


def _read_project(connection: sqlite3.Connection) -> stored_state.ProjectRecord:
    rows = tuple(
        connection.execute(
            """
            SELECT application, schema_version, revision, host_epoch, created_at, updated_at
            FROM project_meta
            ORDER BY singleton
            """
        ).fetchall()
    )
    if len(rows) != 1:
        raise StorageError(StorageErrorCode.INVALID_STATE, "The database must contain one project record.")
    return decode_row(rows[0], stored_state.ProjectRecord)


def _read_history(connection: sqlite3.Connection) -> tuple[stored_state.StoredTransitionReceipt, ...]:
    return tuple(
        stored_receipt_from_row(decode_row(row, TransitionHistoryRow))
        for row in connection.execute(
            """
            SELECT history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
                   authorization_kind AS authorization, actor_task_id, actor_host_id, input_schema,
                   input_json, outcome_schema, outcome_json, committed_at
            FROM transition_history
            ORDER BY history_id
            """
        ).fetchall()
    )


def read_history_receipt(
    connection: sqlite3.Connection, history_id: HistoryId
) -> stored_state.StoredTransitionReceipt | None:
    row = connection.execute(
        """
        SELECT history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
               authorization_kind AS authorization, actor_task_id, actor_host_id, input_schema,
               input_json, outcome_schema, outcome_json, committed_at
        FROM transition_history WHERE history_id = ?
        """,
        (history_id,),
    ).fetchone()
    return None if row is None else stored_receipt_from_row(decode_row(row, TransitionHistoryRow))


def read_checkpoint_receipts(
    connection: sqlite3.Connection, attempt_ids: tuple[AttemptId, ...]
) -> dict[AttemptId, tuple[stored_state.StoredTransitionReceipt, ...]]:
    """Read every checkpoint-acceptance/v2 receipt of the selected attempts, oldest first per attempt.

    The selection uses the checkpoint partial index in fixed-size batches and returns no other subject's receipts.
    """

    grouped: dict[AttemptId, list[stored_state.StoredTransitionReceipt]] = {}
    for row in select_by_ids(
        connection,
        """SELECT history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
                  authorization_kind AS authorization, actor_task_id, actor_host_id, input_schema,
                  input_json, outcome_schema, outcome_json, committed_at
           FROM transition_history
           WHERE subject_id IN ({ids}) AND outcome_schema = 'checkpoint-acceptance/v2'
           ORDER BY subject_id, history_id""",
        attempt_ids,
    ):
        receipt = stored_receipt_from_row(decode_row(row, TransitionHistoryRow))
        grouped.setdefault(AttemptId(str(receipt.subject_id)), []).append(receipt)
    return {attempt_id: tuple(receipts) for attempt_id, receipts in grouped.items()}


def read_review_history_for_items(
    connection: sqlite3.Connection, item_ids: tuple[WorkItemId, ...]
) -> dict[WorkItemId, tuple[stored_state.StoredTransitionReceipt, ...]]:
    grouped: dict[WorkItemId, list[stored_state.StoredTransitionReceipt]] = {}
    for row in select_by_ids(
        connection,
        """SELECT history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
                  authorization_kind AS authorization, actor_task_id, actor_host_id, input_schema,
                  input_json, outcome_schema, outcome_json, committed_at
           FROM transition_history WHERE subject_id IN ({ids})
           AND action_kind IN ('start-pr-review', 'review-pr-brief', 'observe-pr-head', 'record-pr-round', 'close-pr-review')
           ORDER BY history_id""",
        item_ids,
    ):
        receipt = stored_receipt_from_row(decode_row(row, TransitionHistoryRow))
        grouped.setdefault(WorkItemId(str(receipt.subject_id)), []).append(receipt)
    return {item_id: tuple(receipts) for item_id, receipts in grouped.items()}


def _definition_revision_number(value: stored_state.ItemDefinitionRevision) -> int:
    return value.revision


def _dependency_identity_position(value: stored_state.ItemDependency) -> tuple[str, int]:
    return str(value.item_id), value.position


def _current_definitions(
    state: stored_state.StoredWorkState,
    item_ids: Set[WorkItemId],
    error_code: StorageErrorCode,
) -> Mapping[WorkItemId, stored_state.ItemDefinitionRevision]:
    definitions_by_item: dict[WorkItemId, list[stored_state.ItemDefinitionRevision]] = {
        item_id: [] for item_id in item_ids
    }
    for value in state.lifecycle.definition_revisions:
        if value.item_id not in definitions_by_item:
            raise StorageError(error_code, "Definition history names an unknown work item.")
        definitions_by_item[value.item_id].append(value)
        payload = stored_state.stored_definition_bytes(value.definition)
        if hashlib.sha256(payload).hexdigest() != value.digest or value.after_digest != value.digest:
            raise StorageError(error_code, "Definition history digest does not match its canonical definition.")
    current_definitions: dict[WorkItemId, stored_state.ItemDefinitionRevision] = {}
    retained_origins = {
        work_models.ObligationOrigin(value.item_id, value.revision, obligation.obligation_id)
        for value in state.lifecycle.definition_revisions
        if isinstance(value.definition, work_models.WorkItemDefinition)
        for obligation in value.definition.obligations
    }
    for value in state.lifecycle.definition_revisions:
        if isinstance(value.definition, work_models.WorkItemDefinition):
            for obligation in value.definition.obligations:
                if (
                    isinstance(obligation, work_models.CarriedWorkObligation)
                    and obligation.origin not in retained_origins
                ):
                    origin = obligation.origin
                    raise StorageError(
                        error_code,
                        f"Definition '{value.item_id}@{value.revision}' names missing retained origin '{origin.item_id}@{origin.definition_revision}/obligation/{origin.obligation_id}'.",
                    )
    for item_id, revisions in definitions_by_item.items():
        ordered = sorted(revisions, key=_definition_revision_number)
        if [value.revision for value in ordered] != list(range(1, len(ordered) + 1)) or not ordered:
            raise StorageError(error_code, "Every work item must have contiguous definition history from revision 1.")
        if ordered[0].before_digest is not None or any(
            value.before_digest != previous.digest for previous, value in pairwise(ordered)
        ):
            raise StorageError(error_code, "Definition history digest links are not contiguous.")
        current_definitions[item_id] = ordered[-1]
    return MappingProxyType(current_definitions)


def _validate_dependencies(
    state: stored_state.StoredWorkState,
    item_ids: Set[WorkItemId],
    current_definitions: Mapping[WorkItemId, stored_state.ItemDefinitionRevision],
    error_code: StorageErrorCode,
) -> None:
    dependency_groups: dict[WorkItemId, list[work_models.Dependency]] = {item_id: [] for item_id in item_ids}
    for value in sorted(state.lifecycle.dependencies, key=_dependency_identity_position):
        if value.item_id not in item_ids or value.dependency_id not in item_ids:
            raise StorageError(error_code, "A dependency names an unknown work item.")
        dependency_groups[value.item_id].append(value.target)
    if any(
        tuple(dependency_groups[item_id]) != current.definition.dependencies
        for item_id, current in current_definitions.items()
    ):
        raise StorageError(error_code, "Current definition dependencies do not match relational dependencies.")
    for item_id in item_ids:
        pending = [work_models.dependency_item(target) for target in dependency_groups[item_id]]
        visited: set[WorkItemId] = set()
        while pending:
            dependency = pending.pop()
            if dependency == item_id:
                raise StorageError(error_code, "Current definition dependencies contain a cycle.")
            if dependency not in visited:
                visited.add(dependency)
                pending.extend(work_models.dependency_item(target) for target in dependency_groups[dependency])


def _validate_checkpoint_acceptances(state: stored_state.StoredWorkState, error_code: StorageErrorCode) -> None:
    attempts = {value.attempt_id: value.item_id for value in state.lifecycle.attempts}
    expected: list[stored_state.CheckpointAcceptance] = []
    for receipt in state.transition_receipts:
        if receipt.outcome_schema != "checkpoint-acceptance/v2":
            continue
        try:
            outcome = msgspec.json.decode(bytes(receipt.outcome_payload), type=history.CheckpointAcceptanceOutcome)
        except msgspec.DecodeError as error:
            raise StorageError(error_code, f"Checkpoint acceptance receipt {receipt.history_id} is invalid.") from error
        producer = attempts.get(AttemptId(receipt.subject_id))
        if receipt.action_kind != decision_models.ActionKind.ACCEPT_CHECKPOINT or producer is None:
            raise StorageError(
                error_code, f"Checkpoint acceptance receipt {receipt.history_id} has no producer attempt."
            )
        expected.append(
            stored_state.CheckpointAcceptance(receipt.history_id, producer, CheckpointId(outcome.checkpoint))
        )
    if tuple(sorted(expected, key=lambda value: value.history_id)) != tuple(
        sorted(state.lifecycle.checkpoint_acceptances, key=lambda value: value.history_id)
    ):
        raise StorageError(
            error_code, "Checkpoint acceptance facts do not match committed receipt provenance in both directions."
        )


def _replacement_revision(value: stored_state.StoredPlannedReplacement) -> int:
    return value.relation_revision


def _validate_replacements(
    records: stored_state.ReplacementRecords,
    item_ids: Set[WorkItemId],
    error_code: StorageErrorCode,
) -> None:
    replacements_by_item: dict[WorkItemId, list[stored_state.StoredPlannedReplacement]] = {}
    for replacement in records.planned_replacements:
        if replacement.affected_item_id not in item_ids or replacement.replacement_item_id not in item_ids:
            raise StorageError(error_code, "A planned replacement names an unknown work item.")
        replacements_by_item.setdefault(replacement.affected_item_id, []).append(replacement)
    indexed_replacements: dict[tuple[WorkItemId, int], stored_state.StoredPlannedReplacement] = {}
    for affected_item, replacements in replacements_by_item.items():
        ordered = sorted(replacements, key=_replacement_revision)
        if [value.relation_revision for value in ordered] != list(range(1, len(ordered) + 1)):
            raise StorageError(error_code, "Planned replacement revisions must be contiguous from revision 1.")
        indexed_replacements.update(((affected_item, value.relation_revision), value) for value in ordered)
    disposition_keys: set[tuple[WorkItemId, int]] = set()
    for disposition in records.dispositions:
        key = (disposition.affected_item_id, disposition.relation_revision)
        replacement = indexed_replacements.get(key)
        if replacement is None:
            raise StorageError(error_code, "A temporary-retention disposition names an unknown replacement revision.")
        if replacement.status != work_models.PlannedReplacementStatus.CURRENT:
            raise StorageError(error_code, "A temporary-retention disposition names a withdrawn replacement revision.")
        if disposition.accepted_cost != replacement.replacement_cost:
            raise StorageError(error_code, "A temporary-retention disposition must accept the exact replacement cost.")
        if key in disposition_keys:
            raise StorageError(error_code, "A replacement revision has duplicate temporary-retention dispositions.")
        disposition_keys.add(key)


_HELD_STATES = frozenset({stored_state.StoredWorkItemState.PAUSED, stored_state.StoredWorkItemState.BLOCKED})


def _validate_held_unblockers(lifecycle: stored_state.LifecycleRecords, error_code: StorageErrorCode) -> None:
    """Require exactly one current unblocker for every paused or blocked item and none for any other item."""

    held = {value.item_id for value in lifecycle.unblockers}
    for item in lifecycle.work_items:
        if item.state in _HELD_STATES and item.item_id not in held:
            raise StorageError(
                error_code,
                f"Held item '{item.item_id}' ({item.state.value}) records no unblocker; it must name the items, "
                "human decision or outside condition it waits for.",
            )
        if item.state not in _HELD_STATES and item.item_id in held:
            raise StorageError(
                error_code, f"Item '{item.item_id}' ({item.state.value}) is not held but still records an unblocker."
            )
    known = {item.item_id for item in lifecycle.work_items}
    for value in lifecycle.unblockers:
        if value.item_id not in known:
            raise StorageError(error_code, f"Unblocker for '{value.item_id}' names no stored work item.")


def _validate_hold_receipts(
    receipts: tuple[stored_state.StoredTransitionReceipt, ...], error_code: StorageErrorCode
) -> None:
    for receipt in receipts:
        try:
            match receipt.input_schema:
                case unblockers.HOLD_RECEIPT_SCHEMA:
                    msgspec.json.decode(bytes(receipt.input_payload), type=unblockers.HoldReceiptInput, strict=True)
                case unblockers.PLAIN_PAUSE_RECEIPT_SCHEMA:
                    msgspec.json.decode(
                        bytes(receipt.input_payload), type=unblockers.PlainPauseReceiptInput, strict=True
                    )
                case _:
                    continue
        except (msgspec.DecodeError, ValueError) as error:
            raise StorageError(
                error_code, f"Hold receipt {receipt.history_id} does not record a readable unblocker: {error}"
            ) from error


def _validate_amendments(
    lifecycle: stored_state.LifecycleRecords,
    receipts: tuple[stored_state.StoredTransitionReceipt, ...],
    error_code: StorageErrorCode,
) -> None:
    """Check approval and supersession receipt ownership."""

    attempts = {value.attempt_id: value.item_id for value in lifecycle.attempts}
    by_history = {value.history_id: value for value in receipts}
    recorded = {value.history_id for value in lifecycle.amendments}
    for amendment in lifecycle.amendments:
        receipt = by_history.get(amendment.history_id)
        if (
            receipt is None
            or receipt.action_kind != decision_models.ActionKind.RECORD_AMENDMENT
            or str(receipt.subject_id) != str(amendment.attempt_id)
            or amendment.attempt_id not in attempts
        ):
            raise StorageError(
                error_code, f"Amendment {amendment.history_id} is not recorded by its attempt's amendment receipt."
            )
        if (amendment.amendment.cleared_candidate is None) != (
            amendment.amendment.cleared_snapshot_artifact_ref_id is None
        ):
            raise StorageError(
                error_code, f"Amendment {amendment.history_id} records an incomplete cleared submission."
            )
        if isinstance(amendment.end, stored_state.SupersededAmendment):
            superseding = by_history.get(amendment.end.history_id)
            if superseding is None or not (
                (
                    superseding.action_kind == decision_models.ActionKind.REBIND_ATTEMPT
                    and superseding.subject_id == amendment.attempt_id
                )
                or (
                    superseding.action_kind == decision_models.ActionKind.RESUME
                    and superseding.input_schema == decision_batches.REVISED_RESUME_SCHEMA
                    and superseding.subject_id == attempts[amendment.attempt_id]
                )
            ):
                raise StorageError(
                    error_code, f"Amendment {amendment.history_id} has no replacement receipt on its attempt."
                )
    for receipt in receipts:
        if receipt.action_kind == decision_models.ActionKind.RECORD_AMENDMENT and receipt.history_id not in recorded:
            raise StorageError(error_code, f"Amendment receipt {receipt.history_id} has no amendment row.")


_HOLD_RELEASING_ACTIONS = frozenset(
    {
        decision_models.ActionKind.COMPLETE,
        decision_models.ActionKind.CLOSE,
        decision_models.ActionKind.CLOSE_PR_REVIEW,
        decision_models.ActionKind.ACCEPT_CHECKPOINT,
        decision_models.ActionKind.MERGE_PROPOSAL,
        decision_models.ActionKind.CONSOLIDATE,
        decision_models.ActionKind.REVISE_ITEM,
        decision_models.ActionKind.RECORD_REPLACEMENT,
        decision_models.ActionKind.RETAIN_TEMPORARILY,
    }
)


def _validate_released_holds_and_unstarted_holds(  # noqa: C901, PLR0912
    lifecycle: stored_state.LifecycleRecords,
    replacements: stored_state.ReplacementRecords,
    receipts: tuple[stored_state.StoredTransitionReceipt, ...],
    error_code: StorageErrorCode,
) -> None:
    """Reject stuck releasable unstarted holds and verify normalized release provenance rows."""

    items_by_id = {item.item_id: item for item in lifecycle.work_items}
    dependencies_by_item: dict[WorkItemId, list[work_models.Dependency]] = {item_id: [] for item_id in items_by_id}
    for dependency in sorted(lifecycle.dependencies, key=_dependency_identity_position):
        if dependency.item_id in dependencies_by_item:
            dependencies_by_item[dependency.item_id].append(dependency.target)
    open_attempts = {
        attempt.item_id: attempt.attempt_id
        for attempt in lifecycle.attempts
        if attempt.state != work_models.AttemptState.DONE
    }
    unblockers_by_item = {value.item_id: value.unblocker for value in lifecycle.unblockers}
    snapshot = LedgerSnapshot(
        revision=str(lifecycle.project.revision),
        items=tuple(
            work_models.WorkItem(
                item.item_id,
                live_state,
                None if item.timing is None else item.timing.value,
                tuple(dependencies_by_item[item.item_id]),
                open_attempts.get(item.item_id),
                item.source,
                item.next_action,
                item.notes,
                item.queue_position,
                item.outcome_evidence,
                unblocker=unblockers_by_item.get(item.item_id),
            )
            for item in lifecycle.work_items
            if (live_state := stored_state.live_work_state(item.state)) is not None
        ),
        dropped_item_ids=frozenset(
            item.item_id for item in lifecycle.work_items if item.state == stored_state.StoredWorkItemState.DROPPED
        ),
        dependency_facts=(),
        accepted_checkpoints=frozenset(
            work_models.CheckpointDependency(value.item_id, value.checkpoint_id)
            for value in lifecycle.checkpoint_acceptances
        ),
        planned_replacements=tuple(
            work_models.PlannedReplacement(
                value.affected_item_id,
                value.relation_revision,
                value.replacement_item_id,
                value.replacement_cost,
                value.status,
                value.recorded_by,
                value.recorded_at,
            )
            for value in replacements.planned_replacements
        ),
        replacement_dispositions=tuple(
            work_models.ReplacementDisposition(
                value.affected_item_id,
                value.relation_revision,
                value.rationale,
                value.accepted_cost,
                value.recorded_by,
                value.recorded_at,
            )
            for value in replacements.dispositions
        ),
    )
    for item in snapshot.items:
        if snapshot.releasable_unstarted_hold(item) is not None:
            raise StorageError(
                error_code,
                f"Unstarted held item '{item.work_item_id}' has no remaining unsatisfied dependency or unresolved "
                "planned replacement and must be released to ready.",
            )

    receipts_by_id = {receipt.history_id: receipt for receipt in receipts}
    seen_keys: set[tuple[HistoryId, WorkItemId]] = set()
    releases_by_history: dict[HistoryId, list[stored_state.StoredReleasedHold]] = {}
    for hold in lifecycle.released_holds:
        key = (hold.history_id, hold.item_id)
        if key in seen_keys:
            raise StorageError(error_code, f"Duplicate released-hold record at receipt {hold.history_id}.")
        seen_keys.add(key)
        receipt = receipts_by_id.get(hold.history_id)
        if receipt is None or receipt.action_kind not in _HOLD_RELEASING_ACTIONS:
            raise StorageError(
                error_code,
                f"Released hold for '{hold.item_id}' at receipt {hold.history_id} names no releasing transition receipt.",
            )
        stored_item = items_by_id.get(hold.item_id)
        if stored_item is None:
            raise StorageError(error_code, f"Released hold for '{hold.item_id}' names no stored work item.")
        if hold.previous_state != stored_state.StoredWorkItemState.BLOCKED:
            raise StorageError(
                error_code,
                f"Released hold for '{hold.item_id}' at receipt {hold.history_id} has unsupported previous state "
                f"'{hold.previous_state.value}'.",
            )
        if (
            hold.previous_subject_revision < 0
            or hold.previous_subject_revision >= receipt.project_revision
            or stored_item.subject_revision < receipt.project_revision
        ):
            raise StorageError(
                error_code,
                f"Released hold for '{hold.item_id}' at receipt {hold.history_id} has inconsistent subject revision.",
            )
        waited = hold.unblocker.items
        if not waited or len(set(waited)) != len(waited) or any(target not in items_by_id for target in waited):
            raise StorageError(
                error_code,
                f"Released hold for '{hold.item_id}' at receipt {hold.history_id} names invalid waited items.",
            )
        releases_by_history.setdefault(hold.history_id, []).append(hold)

    for receipt in receipts:
        if (
            receipt.action_kind == decision_models.ActionKind.CONSOLIDATE
            and receipt.input_schema == "pinboard-consolidation/v3"
        ):
            record = msgspec.json.decode(bytes(receipt.input_payload), type=consolidation_models.Record)
            expected_consolidation_releases = tuple(
                (
                    value.item_id,
                    stored_state.StoredWorkItemState(value.previous_state),
                    value.previous_subject_revision,
                    work_models.WaitingForItemsToFinish(tuple(WorkItemId(item) for item in value.unblocker.items)),
                )
                for value in record.preview.released_holds
            )
            actual_consolidation_releases = tuple(
                (
                    hold.item_id,
                    hold.previous_state,
                    hold.previous_subject_revision,
                    hold.unblocker,
                )
                for hold in sorted(releases_by_history.get(receipt.history_id, ()), key=lambda value: value.item_id)
            )
            if actual_consolidation_releases != expected_consolidation_releases:
                raise StorageError(
                    error_code,
                    f"Consolidation receipt {receipt.history_id} released holds do not match its confirmed preview.",
                )


def _validate_current_state(state: stored_state.StoredWorkState, error_code: StorageErrorCode) -> None:
    _validate_consolidation_history(state.transition_receipts, error_code)
    _validate_held_unblockers(state.lifecycle, error_code)
    _validate_hold_receipts(state.transition_receipts, error_code)
    _validate_amendments(state.lifecycle, state.transition_receipts, error_code)
    validate_attempt_authority(state, error_code)
    positions = sorted(value.queue_position for value in state.lifecycle.work_items if value.queue_position is not None)
    if positions != list(range(1, len(positions) + 1)):
        raise StorageError(error_code, "Live work-item queue positions must be contiguous and one-based.")
    item_ids = {value.item_id for value in state.lifecycle.work_items}
    _validate_replacements(state.replacements, item_ids, error_code)
    current_definitions = _current_definitions(state, item_ids, error_code)
    item_states = {value.item_id: value.state for value in state.lifecycle.work_items}
    for item_id, item_state in item_states.items():
        if stored_state.live_work_state(item_state) is not None and not isinstance(
            current_definitions[item_id].definition, work_models.WorkItemDefinition
        ):
            raise StorageError(
                error_code,
                "A live work item requires a genuine current supported definition: origin-free v2 or origin-bearing v3; "
                "historical v1 is not executable.",
            )
    current_attempt_states = {
        value.item_id: value.state for value in state.lifecycle.attempts if value.state != work_models.AttemptState.DONE
    }
    for item_id, item_state in item_states.items():
        attempt_state = current_attempt_states.get(item_id)
        validate_current_attempt_relation("read_state", item_id, item_state, attempt_state, error_code)
    for lease in state.authority.preparation_leases:
        if (
            lease.state != authority_models.PreparationLeaseStatus.ACTIVE
            or lease.expires_at <= state.lifecycle.project.updated_at
        ):
            continue
        current = current_definitions.get(lease.item_id)
        if item_states.get(lease.item_id) not in {
            stored_state.StoredWorkItemState.INTAKE,
            stored_state.StoredWorkItemState.READY,
        }:
            raise StorageError(error_code, "An active preparation lease must name a ready work item.")
        if current is None or (lease.definition_revision, lease.definition_digest) != (
            current.revision,
            current.digest,
        ):
            raise StorageError(error_code, "An active preparation lease must pin the current item definition.")
    _validate_dependencies(state, item_ids, current_definitions, error_code)
    _validate_checkpoint_acceptances(state, error_code)
    _validate_released_holds_and_unstarted_holds(
        state.lifecycle, state.replacements, state.transition_receipts, error_code
    )
    try:
        decision_batches.validate_batch_history(state.lifecycle, state.transition_receipts)
    except (ValueError, KeyError) as error:
        raise StorageError(error_code, f"Stored decision batch history is invalid: {error}") from error


def _validate_item_state_counts(
    connection: sqlite3.Connection,
    items: tuple[stored_state.StoredWorkItem, ...],
) -> None:
    rows = tuple(
        decode_row(row, _StateCountRow)
        for row in connection.execute("SELECT state, item_count FROM work_item_state_counts ORDER BY state").fetchall()
    )
    if tuple(sorted(value.state.value for value in rows)) != tuple(
        sorted(value.value for value in stored_state.StoredWorkItemState)
    ):
        raise StorageError(StorageErrorCode.INVALID_STATE, "Work-item state counts are incomplete.")
    expected = Counter(value.state for value in items)
    if any(value.item_count != expected[value.state] for value in rows):
        raise StorageError(StorageErrorCode.INVALID_STATE, "Work-item state counts do not match stored items.")


def read_state(connection: sqlite3.Connection) -> stored_state.StoredWorkState:
    project = _read_project(connection)
    initiative_values = initiatives.read_all(connection)
    off_roadmap = initiatives.read_off_roadmap(connection)
    state = stored_state.StoredWorkState(
        read_lifecycle(connection, project),
        read_proposals(connection),
        _read_replacements(connection),
        read_artifacts(connection),
        read_authority(connection),
        _read_history(connection),
        initiative_values,
        off_roadmap,
    )
    initiatives.validate_all(connection, state.initiatives, project.revision)
    initiatives.validate_off_roadmap(state.initiatives, state.off_roadmap_episodes, project.revision)
    _validate_current_state(state, StorageErrorCode.INVALID_STATE)
    validate_review_history(connection, None)
    _validate_item_state_counts(connection, state.lifecycle.work_items)
    return state


def _validate_consolidation_history(
    receipts: tuple[stored_state.StoredTransitionReceipt, ...], error_code: StorageErrorCode
) -> None:
    for receipt in receipts:
        if (
            receipt.action_kind == decision_models.ActionKind.CONSOLIDATE
            and (message := consolidation.receipt_failure(receipt)) is not None
        ):
            raise StorageError(error_code, f"Consolidation receipt {receipt.history_id} is invalid: {message}")


def read_project_export_state(connection: sqlite3.Connection) -> project_export.ProjectExportState:
    """Read every exported relation while excluding local-only authority history."""

    project = _read_project(connection)
    lifecycle = read_lifecycle(connection, project)
    validate_review_history(connection, None)
    replacements = _read_replacements(connection)
    _validate_replacements(
        replacements,
        {value.item_id for value in lifecycle.work_items},
        StorageErrorCode.INVALID_STATE,
    )
    initiative_values = initiatives.read_all(connection)
    off_roadmap = initiatives.read_off_roadmap(connection)
    initiatives.validate_all(connection, initiative_values, project.revision)
    initiatives.validate_off_roadmap(initiative_values, off_roadmap, project.revision)
    receipts = _read_history(connection)
    _validate_consolidation_history(receipts, StorageErrorCode.INVALID_STATE)
    _validate_held_unblockers(lifecycle, StorageErrorCode.INVALID_STATE)
    _validate_hold_receipts(receipts, StorageErrorCode.INVALID_STATE)
    _validate_amendments(lifecycle, receipts, StorageErrorCode.INVALID_STATE)
    _validate_released_holds_and_unstarted_holds(lifecycle, replacements, receipts, StorageErrorCode.INVALID_STATE)
    try:
        decision_batches.validate_batch_history(lifecycle, receipts)
    except (ValueError, KeyError) as error:
        raise StorageError(
            StorageErrorCode.INVALID_STATE, f"Stored decision batch history is invalid: {error}"
        ) from error
    return project_export.ProjectExportState(
        lifecycle,
        read_pending_proposals(connection),
        replacements,
        read_artifacts(connection),
        receipts,
        initiative_values,
        off_roadmap,
    )


def _json_text(value: work_models.CanonicalJson | None) -> str | None:
    return None if value is None else bytes(value).decode("utf-8")


def append_history(
    connection: sqlite3.Connection,
    records: tuple[stored_state.StoredTransitionReceipt, ...],
) -> None:
    connection.executemany(
        """
        INSERT INTO transition_history (
            history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
            artifact_kind, authorization_kind, actor_task_id, actor_host_id, input_schema,
            input_json, outcome_schema, outcome_json, committed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(
            (
                value.history_id,
                value.project_revision,
                value.action_id,
                value.action_kind.value,
                value.subject_id,
                value.artifact_ref_id,
                None if value.artifact_ref_id is None else "evidence",
                value.authorization.value,
                value.actor_task_id,
                value.actor_host_id,
                value.input_schema,
                _json_text(value.input_payload),
                value.outcome_schema,
                _json_text(value.outcome_payload),
                value.committed_at.isoformat(),
            )
            for value in records
        ),
    )
    for receipt in records:
        if receipt.input_schema == "pinboard-pause-decisions/v1":
            msgspec.json.decode(bytes(receipt.input_payload), type=decision_batches.PauseDecisionsInput)
            if receipt.action_kind != decision_models.ActionKind.PAUSE:
                raise StorageError(StorageErrorCode.INVALID_STATE, "A decision batch must be recorded by pause.")
            connection.execute(
                """INSERT INTO attempt_decision_batches(attempt_id, pause_history_id, resume_history_id)
                   VALUES (?, ?, NULL) ON CONFLICT(attempt_id) DO UPDATE
                   SET pause_history_id = excluded.pause_history_id, resume_history_id = NULL""",
                (receipt.subject_id, receipt.history_id),
            )
        elif (resolved := decision_batches.read_resume_resolution(receipt)) is not None:
            cursor = connection.execute(
                """UPDATE attempt_decision_batches SET resume_history_id = ?
                   WHERE pause_history_id = ? AND resume_history_id IS NULL
                   AND attempt_id IN (SELECT attempt_id FROM attempts WHERE item_id = ?)""",
                (receipt.history_id, resolved.pause_history_id, receipt.subject_id),
            )
            if cursor.rowcount != 1:
                raise StorageError(
                    StorageErrorCode.INVALID_STATE, "The resolving resume has no pending decision batch."
                )
        if receipt.outcome_schema != "checkpoint-acceptance/v2":
            continue
        outcome = msgspec.json.decode(bytes(receipt.outcome_payload), type=history.CheckpointAcceptanceOutcome)
        cursor = connection.execute(
            """INSERT INTO checkpoint_acceptances(history_id, item_id, checkpoint_id)
               SELECT ?, item_id, ? FROM attempts WHERE attempt_id = ?""",
            (receipt.history_id, outcome.checkpoint, receipt.subject_id),
        )
        if cursor.rowcount != 1:
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                f"Checkpoint acceptance receipt {receipt.history_id} has no producer attempt.",
            )
