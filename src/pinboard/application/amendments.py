"""Decide which recorded amendments form an attempt's current contract and which review base they keep.

An amendment belongs to the accepted brief it was recorded against, so rebinding to another brief ends it as part
of the current contract while history keeps it. A review base survives later submissions but ends at a return for
correction, a rebind or an acceptance, each of which makes an earlier ready review no longer the latest verdict.
"""

from collections.abc import Mapping

from pinboard.application import query_models, stored_state
from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import ArtifactRefId, HistoryId

_REVIEW_BASE_BREAKS = frozenset(
    {
        decision_models.ActionKind.RETURN_FOR_CORRECTION,
        decision_models.ActionKind.REBIND_ATTEMPT,
        decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE,
        decision_models.ActionKind.ACCEPT_CHECKPOINT,
    }
)


def current_amendments(
    amendments: tuple[stored_state.StoredAttemptAmendment, ...], brief_artifact_ref_id: ArtifactRefId
) -> tuple[stored_state.StoredAttemptAmendment, ...]:
    """Return the amendments recorded against the attempt's current brief, in recording order."""

    return tuple(value for value in amendments if value.amendment.brief_artifact_ref_id == brief_artifact_ref_id)


def carried_review_base(
    current: tuple[stored_state.StoredAttemptAmendment, ...],
    later_review_kinds: tuple[decision_models.ActionKind, ...],
) -> work_models.ReviewBase | None:
    """Return the latest current amendment's review base unless a later receipt ended it.

    later_review_kinds are the attempt's review-relevant receipts after that latest amendment, oldest first.
    """

    if not current:
        return None
    base = current[-1].amendment.review_base
    if base is None or any(kind in _REVIEW_BASE_BREAKS for kind in later_review_kinds):
        return None
    return base


def delta_review_base(
    current: tuple[stored_state.StoredAttemptAmendment, ...],
    later_review_kinds: tuple[decision_models.ActionKind, ...],
) -> work_models.ReviewBase | None:
    """Return the review base an amendment-only delta review may use for the current submission.

    The attempt must have resubmitted after its latest amendment with no return, rebind or acceptance between.
    """

    base = carried_review_base(current, later_review_kinds)
    if base is None or not later_review_kinds or later_review_kinds[-1] != decision_models.ActionKind.SUBMIT_REVIEW:
        return None
    return base


def delta_amendments(
    current: tuple[stored_state.StoredAttemptAmendment, ...], base: work_models.ReviewBase
) -> tuple[stored_state.StoredAttemptAmendment, ...]:
    """Return the current amendments recorded since the reviewed submission the base names."""

    return tuple(value for value in current if value.amendment.review_base == base)


def condition_receipt(
    subject_revision: int,
    receipts_by_revision: Mapping[int, query_models.ConsumedTransitionReceipt],
    amendments: Mapping[HistoryId, stored_state.StoredAttemptAmendment],
) -> query_models.ConsumedTransitionReceipt | None:
    """Select the receipt that names an attempt's condition.

    An amendment that left the attempt active keeps the condition it found, so the walk follows each such amendment
    to the receipt at the attempt revision before it. An amendment that returned a reviewed attempt to active names
    its own condition until the next receipt replaces it.
    """

    receipt = receipts_by_revision.get(subject_revision)
    while receipt is not None and receipt.action_kind == decision_models.ActionKind.RECORD_AMENDMENT:
        amendment = amendments.get(receipt.history_id)
        if amendment is None or amendment.amendment.cleared_candidate is not None:
            return receipt
        receipt = receipts_by_revision.get(amendment.prior_attempt_revision)
    return receipt
