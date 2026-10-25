"""Preview selected facts; compare the human-confirmed binding under lock and commit cancellation."""

from datetime import datetime
from hashlib import sha256
from pathlib import Path

import msgspec

from pinboard.application import cancellation_models as models
from pinboard.application import stored_state
from pinboard.application.mutation_models import CancellationMutation, CommittedEffect, MutationReceipt
from pinboard.application.ports import WorkStore
from pinboard.domain import decision_models, history, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import ActionId, HistorySubjectId, HostId, TaskId, WorkItemId


def binding(preview: models.CompatibilityCancellationPreviewV1, work_root: Path) -> models.CancellationBinding:
    return models.CancellationBinding(
        str(work_root),
        "pinboard-cancellation-binding/v1",
        preview.item_id,
        sha256(msgspec.json.encode(preview, order="sorted")).hexdigest(),
    )


def preview(store: WorkStore, item_id: WorkItemId) -> DecisionResult[models.CancellationPreview]:
    value = store.read_cancellation_preview(item_id)
    if value is None:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Item '{item_id}' is absent or terminal; cancellation requires a live item.",
            None,
        )
    return value


def confirm(
    store: WorkStore,
    item_id: WorkItemId,
    expected: models.CancellationBinding,
    human_confirmation: str,
    actor_task_id: TaskId,
    actor_host_id: HostId,
    now: datetime,
    work_root: Path,
) -> DecisionResult[CommittedEffect]:
    with store.write() as transaction:
        current = transaction.read_cancellation_preview(item_id)
        if current is None or binding(current, work_root) != expected:
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_AVAILABLE,
                f"Cancellation facts for '{item_id}' changed or the binding names another item. Read a fresh cancellation preview and obtain a new separate exact-item human confirmation.",
                None,
            )
        allocation = transaction.read_mutation_allocation()
        record = models.CurrentCancellationRecord(expected, current, human_confirmation)
        receipt = MutationReceipt(
            decision_models.TransitionReceipt(
                ActionId(f"cancel:{item_id}"), item_id, "dropped", human_confirmation, now
            ),
            allocation.next_history_id,
            allocation.project_revision + 1,
            decision_models.ActionKind.CANCEL,
            HistorySubjectId(item_id),
            None,
            decision_models.AuthorizationKind.PROJECT,
            actor_task_id,
            actor_host_id,
            "pinboard-cancellation/v2",
            work_models.CanonicalJson(msgspec.json.encode(record, order="sorted")),
        )
        return transaction.commit(CancellationMutation(receipt, current))


def receipt_failure(receipt: stored_state.StoredTransitionReceipt) -> str | None:
    if receipt.input_schema not in ("pinboard-cancellation/v1", "pinboard-cancellation/v2"):
        return "Cancellation receipt has an unsupported input schema."
    try:
        record = msgspec.json.decode(bytes(receipt.input_payload), type=models.CancellationRecord)
    except (msgspec.DecodeError, ValueError) as error:
        return f"Cancellation receipt cannot be decoded: {error}"
    if msgspec.json.encode(record, order="sorted") != bytes(receipt.input_payload):
        return "Cancellation receipt is not canonical."
    if receipt.input_schema != (
        "pinboard-cancellation/v2"
        if isinstance(record, models.CurrentCancellationRecord)
        else "pinboard-cancellation/v1"
    ):
        return "Cancellation receipt schema differs from its recorded input."
    if receipt.subject_id != record.preview.item_id or receipt.action_id != f"cancel:{record.preview.item_id}":
        return "Cancellation receipt does not name its previewed item."
    if (
        receipt.authorization != decision_models.AuthorizationKind.PROJECT
        or receipt.actor_task_id is None
        or receipt.actor_host_id is None
    ):
        return "Cancellation receipt has no project actor attribution."
    if receipt.outcome_schema != "transition-receipt/v1":
        return "Cancellation receipt has an unsupported outcome schema."
    try:
        outcome = msgspec.json.decode(bytes(receipt.outcome_payload), type=history.TransitionReceiptOutcome)
    except msgspec.DecodeError as error:
        return f"Cancellation outcome cannot be decoded: {error}"
    if (
        outcome.outcome != "dropped"
        or outcome.evidence != record.human_confirmation
        or outcome.candidate is not None
        or outcome.checkpoint is not None
    ):
        return "Cancellation outcome differs from its recorded confirmation."
    if msgspec.json.encode(outcome, order="sorted") != bytes(receipt.outcome_payload):
        return "Cancellation outcome is not canonical."
    return None
