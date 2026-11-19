"""Select the recorded human decisions that qualify an exact landing review."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import msgspec

from pinboard.application import decision_batches, stored_state
from pinboard.domain import work_models
from pinboard.domain.history import RecordText
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, HistoryId, WorkItemId


class LandingApproval(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: decision_batches.PositiveInt
    brief_artifact_ref_id: decision_batches.PositiveInt
    amendment_class: work_models.AmendmentClass
    change: RecordText
    human_decision: RecordText


class LandingDecisionBasis(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    resolutions: tuple[decision_batches.DecisionBatchView, ...]
    approvals: tuple[LandingApproval, ...]


type BoundResolution = tuple[stored_state.StoredTransitionReceipt, decision_batches.ResolvedDecisionsReceipt]


def select_bound_resolutions(
    receipts: Iterable[stored_state.StoredTransitionReceipt], brief_ref_id: ArtifactRefId
) -> tuple[BoundResolution, ...]:
    selected: list[BoundResolution] = []
    for receipt in receipts:
        bound = decision_batches.read_bound_resume(receipt)
        if bound.brief_artifact_ref_id == brief_ref_id and bound.pause_resolution is not None:
            selected.append((receipt, bound.pause_resolution))
    return tuple(selected)


def decision_basis(
    attempt_id: AttemptId,
    item_id: WorkItemId,
    resumes: tuple[BoundResolution, ...],
    pauses: Mapping[HistoryId, stored_state.StoredTransitionReceipt],
    approvals: Iterable[stored_state.StoredAttemptAmendment],
) -> LandingDecisionBasis:
    batches: list[decision_batches.DecisionBatchView] = []
    for resume, resolution in resumes:
        pause_id = HistoryId(resolution.pause_history_id)
        pause = pauses[pause_id]
        if pause.subject_id == attempt_id:
            batches.append(
                decision_batches.project_batch_with_resolution(
                    stored_state.DecisionBatchReference(attempt_id, pause_id, resume.history_id),
                    pause,
                    resume,
                    item_id,
                    resolution,
                )
            )
    return LandingDecisionBasis(
        tuple(batches),
        tuple(
            LandingApproval(
                int(approval.history_id),
                int(approval.amendment.brief_artifact_ref_id),
                approval.amendment.amendment_class,
                approval.amendment.change,
                approval.amendment.human_decision,
            )
            for approval in approvals
        ),
    )


@dataclass(frozen=True, slots=True)
class HistoricalLandingDecisions:
    receipts_by_id: Mapping[HistoryId, stored_state.StoredTransitionReceipt]
    resumes_by_item: Mapping[WorkItemId, list[stored_state.StoredTransitionReceipt]]
    approvals_by_attempt: Mapping[AttemptId, list[stored_state.StoredAttemptAmendment]]

    def at_completion(
        self,
        attempt: stored_state.StoredAttempt,
        brief: stored_state.ArtifactReference,
        completion: stored_state.StoredTransitionReceipt,
    ) -> LandingDecisionBasis:
        return decision_basis(
            attempt.attempt_id,
            attempt.item_id,
            select_bound_resolutions(
                (
                    resume
                    for resume in self.resumes_by_item.get(attempt.item_id, [])
                    if brief.accepted_revision < resume.project_revision < completion.project_revision
                ),
                brief.artifact_ref_id,
            ),
            self.receipts_by_id,
            (
                approval
                for approval in self.approvals_by_attempt.get(attempt.attempt_id, [])
                if self.receipts_by_id[approval.history_id].project_revision < completion.project_revision
                and (
                    approval.end is None
                    or (
                        isinstance(approval.end, stored_state.SupersededAmendment)
                        and self.receipts_by_id[approval.end.history_id].project_revision >= completion.project_revision
                    )
                )
            ),
        )


def index_historical_decisions(
    receipts: tuple[stored_state.StoredTransitionReceipt, ...],
    amendments: tuple[stored_state.StoredAttemptAmendment, ...],
) -> HistoricalLandingDecisions:
    resumes_by_item: dict[WorkItemId, list[stored_state.StoredTransitionReceipt]] = {}
    for receipt in receipts:
        if receipt.input_schema in {decision_batches.SAME_BRIEF_RESUME_SCHEMA, decision_batches.REVISED_RESUME_SCHEMA}:
            resumes_by_item.setdefault(WorkItemId(receipt.subject_id), []).append(receipt)
    approvals_by_attempt: dict[AttemptId, list[stored_state.StoredAttemptAmendment]] = {}
    for approval in amendments:
        approvals_by_attempt.setdefault(approval.attempt_id, []).append(approval)
    return HistoricalLandingDecisions(
        {receipt.history_id: receipt for receipt in receipts}, resumes_by_item, approvals_by_attempt
    )
