"""Reconstruct the released v8 inventory once during explicit ownership migration.

Free-name selection is retained solely to preserve independently selected predecessor
membership, including unlinked sources and overlap. Remove this boundary only after
released v8 ledgers and their retained migration plans are retired.
"""

from pinboard.application import history_archives, stored_state
from pinboard.domain import work_models
from pinboard.domain.identifiers import ArtifactRefId


def select_v8_archive_facts(
    attempt: stored_state.StoredAttempt,
    references: tuple[stored_state.ArtifactReference, ...],
    receipts: tuple[stored_state.StoredTransitionReceipt, ...],
    definitions: tuple[stored_state.ItemDefinitionRevision, ...],
) -> stored_state.ArchiveHistoryFacts:
    attempt_id = str(attempt.attempt_id)
    selected_receipts = tuple(
        value for value in receipts if history_archives.receipt_belongs_to_attempt(value, attempt_id)
    )
    linked_ids = {
        attempt.brief_artifact_ref_id,
        attempt.result_artifact_ref_id,
        *(value.artifact_ref_id for value in selected_receipts),
    }
    checkpoint_keys = {
        f"{reference.key.removesuffix('-review-package')}-{role}"
        for receipt in selected_receipts
        if receipt.outcome_schema == "checkpoint-acceptance/v2"
        for reference in references
        if reference.artifact_ref_id == receipt.artifact_ref_id
        for role in ("candidate", "result", "review", "review-package")
    }
    return stored_state.ArchiveHistoryFacts(
        attempt,
        select_v8_artifact_references(attempt_id, references, frozenset(linked_ids), frozenset(checkpoint_keys)),
        selected_receipts,
        tuple(value for value in definitions if value.item_id == attempt.item_id),
    )


def select_v8_artifact_references(
    attempt_id: str,
    references: tuple[stored_state.ArtifactReference, ...],
    linked_ids: frozenset[ArtifactRefId | None],
    checkpoint_keys: frozenset[str],
) -> tuple[stored_state.ArtifactReference, ...]:
    sibling_briefs = tuple(
        value
        for value in references
        if value.kind == work_models.ArtifactKind.BRIEF and value.key.startswith(f"{attempt_id}-")
    )
    return tuple(
        value
        for value in references
        if (value.key == attempt_id or value.key.startswith(f"{attempt_id}-"))
        and value.key != history_archives.archive_key(attempt_id)
        and (
            value.artifact_ref_id in linked_ids
            or value.key in checkpoint_keys
            or not any(
                (value.key == sibling.key or value.key.startswith(f"{sibling.key}-"))
                and value.accepted_revision >= sibling.accepted_revision
                for sibling in sibling_briefs
            )
        )
    )
