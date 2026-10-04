"""Canonical records and decisions for human-owned pull request reviews."""

from typing import Annotated, Literal

import msgspec

from pinboard.application import stored_state
from pinboard.domain import decision_models
from pinboard.domain.identifiers import WorkItemId

type Line = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\n]+\z")]
type Sha = Annotated[str, msgspec.Meta(pattern=r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\z")]
type Digest = Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{64}\z")]


class Requirement(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    source: Line
    expected_behavior: Line
    consumer: Line
    owner: Line


class ReviewBrief(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-pr-review-brief/v1"]
    item_id: Line
    definition_revision: Annotated[int, msgspec.Meta(ge=1)]
    definition_digest: Digest
    pr_url: Line
    pr_author: Line
    requirements: Annotated[tuple[Requirement, ...], msgspec.Meta(min_length=1)]
    repository_criteria: Annotated[tuple[Line, ...], msgspec.Meta(min_length=1)]
    prepared_by_task_id: Line

    def __post_init__(self) -> None:
        if len({value.source for value in self.requirements}) != len(self.requirements):
            raise ValueError("Requirement sources must be unique.")
        if len(set(self.repository_criteria)) != len(self.repository_criteria):
            raise ValueError("Repository criteria must be unique.")


class BriefReview(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-pr-review-brief-review/v1"]
    item_id: Line
    brief_history_id: Annotated[int, msgspec.Meta(ge=1)]
    reviewer_task_id: Line
    verdict: Literal["ready", "needs-correction"]
    evidence: Line


class Finding(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    finding_id: Line
    severity: Literal["blocking", "concern", "note"]
    description: Line
    evidence: Line


class PriorFindingDisposition(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    finding_id: Line
    disposition: Literal["resolved", "carried-forward", "accepted-residual"]
    evidence: Line


class ReviewRound(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-pr-review-round/v1"]
    item_id: Line
    brief_history_id: Annotated[int, msgspec.Meta(ge=1)]
    previous_round_history_id: Annotated[int, msgspec.Meta(ge=1)] | None
    observed_head: Sha
    observation_source: Line
    findings: tuple[Finding, ...]
    verification_limits: tuple[Line, ...]
    prior_dispositions: tuple[PriorFindingDisposition, ...]
    reviewer_task_id: Line

    def __post_init__(self) -> None:
        ids = tuple(value.finding_id for value in self.findings)
        if len(set(ids)) != len(ids):
            raise ValueError("Finding identities must be unique in a round.")
        prior_ids = tuple(value.finding_id for value in self.prior_dispositions)
        if len(set(prior_ids)) != len(prior_ids):
            raise ValueError("Prior finding dispositions must be unique.")
        if self.previous_round_history_id is None and self.prior_dispositions:
            raise ValueError("A first round cannot dispose of prior findings.")


class HeadObservation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-pr-head-observation/v1"]
    item_id: Line
    observed_head: Sha
    observation_source: Line


class FinalFindingDisposition(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    finding_id: Line
    disposition: Literal["resolved", "accepted-residual"]
    evidence: Line


class ReviewClose(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-pr-review-close/v1"]
    item_id: Line
    final_round_history_id: Annotated[int, msgspec.Meta(ge=1)] | None
    last_reviewed_head: Sha | None
    newer_observed_head: Sha | None
    newer_observation_source: Line | None
    final_dispositions: tuple[FinalFindingDisposition, ...]
    human_direction: Line
    human_task_id: Line
    outcome: Literal["accepted", "stopped"]

    def __post_init__(self) -> None:
        if (self.final_round_history_id is None) != (self.last_reviewed_head is None):
            raise ValueError("The final round and reviewed head must be supplied together.")
        if self.final_round_history_id is None and self.final_dispositions:
            raise ValueError("A review without a round has no findings to dispose of.")
        if (self.newer_observed_head is None) != (self.newer_observation_source is None):
            raise ValueError("A newer observed head requires its observation source.")
        if self.newer_observed_head is not None and self.newer_observed_head == self.last_reviewed_head:
            raise ValueError("The newer observed head must differ from the last reviewed head.")
        ids = tuple(value.finding_id for value in self.final_dispositions)
        if len(set(ids)) != len(ids):
            raise ValueError("Final finding dispositions must be unique.")


def render_review_history(item_id: WorkItemId, receipts: tuple[stored_state.StoredTransitionReceipt, ...]) -> str:
    """Render recorded review facts without turning observation into remote certification."""

    selected = tuple(
        value
        for value in receipts
        if value.subject_id == item_id
        and value.action_kind
        in {
            decision_models.ActionKind.START_PR_REVIEW,
            decision_models.ActionKind.REVIEW_PR_BRIEF,
            decision_models.ActionKind.OBSERVE_PR_HEAD,
            decision_models.ActionKind.RECORD_PR_ROUND,
            decision_models.ActionKind.CLOSE_PR_REVIEW,
        }
    )
    if not selected:
        return ""
    lines = [
        "## Human-owned PR review",
        "",
        "Pinboard records harness observations. Remote head freshness and hosted checks are unverified.",
        "",
    ]
    for receipt in selected:
        raw = bytes(receipt.input_payload)
        match receipt.action_kind:
            case decision_models.ActionKind.START_PR_REVIEW:
                brief = msgspec.json.decode(raw, type=ReviewBrief, strict=True)
                lines.extend(
                    (
                        f"### Review brief {receipt.history_id}",
                        "",
                        f"- PR: {brief.pr_url}",
                        f"- PR author: {brief.pr_author}",
                        f"- Definition: {brief.definition_revision} ({brief.definition_digest})",
                        f"- Prepared by: {brief.prepared_by_task_id}",
                        "",
                    )
                )
                lines.extend(
                    f"- Requirement {value.source}: {value.expected_behavior} (consumer: {value.consumer}; owner: {value.owner})"
                    for value in brief.requirements
                )
                lines.extend(f"- Repository criterion: {value}" for value in brief.repository_criteria)
                lines.append("")
            case decision_models.ActionKind.REVIEW_PR_BRIEF:
                review = msgspec.json.decode(raw, type=BriefReview, strict=True)
                lines.extend(
                    (
                        f"### Independent brief review {receipt.history_id}",
                        "",
                        f"- Reviewer: {review.reviewer_task_id}",
                        f"- Verdict: {review.verdict}",
                        f"- Evidence: {review.evidence}",
                        "",
                    )
                )
            case decision_models.ActionKind.OBSERVE_PR_HEAD:
                observation = msgspec.json.decode(raw, type=HeadObservation, strict=True)
                lines.extend(
                    (
                        f"### Observed head {observation.observed_head}",
                        "",
                        f"- Source: {observation.observation_source}",
                        "- Reviewed by this observation: no",
                        "",
                    )
                )
            case decision_models.ActionKind.RECORD_PR_ROUND:
                round_value = msgspec.json.decode(raw, type=ReviewRound, strict=True)
                lines.extend(
                    (
                        f"### Review round {receipt.history_id}",
                        "",
                        f"- Reviewed head: {round_value.observed_head}",
                        f"- Observation source: {round_value.observation_source}",
                        f"- Reviewer: {round_value.reviewer_task_id}",
                        "",
                    )
                )
                lines.extend(
                    f"- Finding {value.finding_id} ({value.severity}): {value.description}; evidence: {value.evidence}"
                    for value in round_value.findings
                )
                lines.extend(
                    f"- Prior finding {value.finding_id}: {value.disposition}; {value.evidence}"
                    for value in round_value.prior_dispositions
                )
                lines.extend(f"- Verification limit: {value}" for value in round_value.verification_limits)
                lines.append("")
            case decision_models.ActionKind.CLOSE_PR_REVIEW:
                close = msgspec.json.decode(raw, type=ReviewClose, strict=True)
                lines.extend(
                    (
                        "### Human-directed close",
                        "",
                        f"- Outcome: {close.outcome}",
                        f"- Last reviewed head: {close.last_reviewed_head or 'none; no PR round completed'}",
                        f"- Newer observed but unreviewed head: {close.newer_observed_head or 'none'}",
                        f"- Human direction: {close.human_direction}",
                        "",
                    )
                )
                lines.extend(
                    f"- Finding {value.finding_id}: {value.disposition}; {value.evidence}"
                    for value in close.final_dispositions
                )
                lines.append("")
            case _:
                raise ValueError("Unexpected action kind in PR review history.")
    return "\n".join(lines) + "\n"
