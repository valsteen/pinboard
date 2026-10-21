"""Render and replace human-readable views from supplied authoritative facts.

Ordinary and rebuild callers supply exact projection facts and verified brief
content, plus a composed board reader for the two board projections, which are
read and replaced under one board lock. Validation alone supplies complete state
and decoded board history facts to derive every expected byte. This adapter never
reads generated views as authority, decodes briefs or reads the store itself.
"""

import fcntl
import os
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Literal, assert_never

import msgspec

from pinboard.adapters.files import roadmap as roadmap_files
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode
from pinboard.adapters.files.file_io import atomic_replace, ensure_child_directory
from pinboard.adapters.files.models import ViewRefreshResult, ViewWarning
from pinboard.application import definition_compatibility, ports, pr_reviews, query_models, roadmap, stored_state
from pinboard.application.queries import (
    damaged_receipt_message,
    damaged_receipt_recovery,
    decode_recorded_pause_reason,
    project_board_portfolio,
)
from pinboard.domain import history, work_models
from pinboard.domain.identifiers import AttemptId, WorkItemId

NOTICE = "Generated projection; SQLite is authoritative."


def _dependency_key(value: stored_state.ItemDependency) -> tuple[str, int]:
    return str(value.item_id), value.position


def _render_header(kind: str) -> str:
    return f"---\nkind: {kind}\nauthority: sqlite-v9\n---\n\n> {NOTICE}\n\n"


def _bullets(values: tuple[str, ...]) -> str:
    return "".join(f"- {value}\n" for value in values) or "- None recorded.\n"


def _deferral_label(policy: work_models.ObligationDeferralPolicy) -> str:
    match policy:
        case work_models.ObligationDeferralPolicy.ALLOWED:
            return "deferral allowed"
        case work_models.ObligationDeferralPolicy.FORBIDDEN:
            return "deferral not allowed"
        case _ as unreachable:
            assert_never(unreachable)


@dataclass(frozen=True, slots=True)
class _ViewInputs:
    board: query_models.BoardPortfolio
    overview_items: Mapping[str, query_models.OverviewItem]
    dependencies: Mapping[WorkItemId, tuple[WorkItemId, ...]]
    definitions: Mapping[WorkItemId, stored_state.ItemDefinitionRevision]


def _project_view_inputs(
    state: stored_state.StoredWorkState, board_history: query_models.BoardHistoryFacts, now: datetime
) -> _ViewInputs:
    board = project_board_portfolio(state, board_history, now)
    dependency_groups: dict[WorkItemId, list[WorkItemId]] = {item.item_id: [] for item in state.lifecycle.work_items}
    for dependency in sorted(state.lifecycle.dependencies, key=_dependency_key):
        dependency_groups[dependency.item_id].append(dependency.dependency_id)
    return _ViewInputs(
        board,
        MappingProxyType({value.overview.item_id: value.overview for value in board.items}),
        MappingProxyType({item_id: tuple(dependencies) for item_id, dependencies in dependency_groups.items()}),
        MappingProxyType({definition.item_id: definition for definition in state.lifecycle.definition_revisions}),
    )


def _render_item(
    item: stored_state.StoredWorkItem,
    dependencies: tuple[WorkItemId, ...],
    overview_item: query_models.OverviewItem | None,
    definition: stored_state.ItemDefinitionRevision,
    review_history: tuple[stored_state.StoredTransitionReceipt, ...],
    pause_reason: str | None,
) -> bytes:
    dependency_reasons = (
        tuple(f"{value.item_id}: {value.reason}" for value in overview_item.dependency_reasons)
        if overview_item is not None
        else tuple(str(value) for value in dependencies)
    )
    origin = None if overview_item is None else overview_item.proposal_origin
    accepted = definition.definition
    replacement = None if overview_item is None else overview_item.planned_replacement
    attempt = overview_item.attempt_id if overview_item is not None else None
    if overview_item is not None and overview_item.state == work_models.WorkState.READY:
        next_step = (
            "Saving this work did not start an attempt. To start it, ask the agent to check current "
            "dependencies and holds, prepare the agreed brief, and obtain start authorization.\n\n"
        )
    elif overview_item is not None:
        next_step = ""
    else:
        next_step = "No current action is recorded for this finished item.\n\n"
    match accepted:
        case work_models.WorkItemDefinition():
            execution_details = "\n### Obligations\n\n" + _bullets(
                tuple(
                    f"{value.obligation_id} ({_deferral_label(value.deferral_policy)}): {value.statement}"
                    for value in accepted.obligations
                )
            )
            policy_details = f"- Checkout policy: {accepted.checkout_policy.value}\n"
            format_details = ""
        case definition_compatibility.HistoricalDefinitionV1():
            execution_details = ""
            policy_details = ""
            format_details = "- Historical definition schema: pinboard-work-item-definition/v1\n"
        case _ as unreachable:
            assert_never(unreachable)
    return (
        _render_header("work-item-view")
        + f"# {accepted.title}\n\n{accepted.objective}\n\n"
        + "## Current position\n\n"
        + f"- State: {overview_item.state.value if overview_item is not None else item.state.value}\n"
        + f"- Current attempt: {attempt or 'none'}\n"
        + ("" if pause_reason is None else f"- Pause reason: {history.nest_record_text(pause_reason, '  ')}\n")
        + f"- Dependency eligibility: {'yes' if overview_item is not None and overview_item.eligible else 'no'}\n\n"
        + next_step
        + "## Original intake context\n\n"
        + "Recorded when this work was saved; original context, not the current plan.\n\n"
        + f"- Next action at intake: {item.next_action if item.next_action is not None else 'none'}\n"
        + f"- Notes at intake: {item.notes if item.notes is not None else 'none'}\n\n"
        + "## Expected result\n\n"
        + f"{accepted.effect}\n\n"
        + f"**What this unlocks:** {accepted.unlock}\n\n"
        + "## Agreed work\n\n"
        + f"**Reason:** {accepted.hypothesis}\n\n"
        + "### Scope\n\n"
        + _bullets(accepted.scope)
        + "\n### Outside scope\n\n"
        + _bullets(accepted.non_scope)
        + "\n### Acceptance criteria\n\n"
        + _bullets(accepted.acceptance_criteria)
        + "\n### Evidence\n\n"
        + _bullets(accepted.evidence)
        + "\n### Dependencies\n\n"
        + _bullets(dependency_reasons)
        + execution_details
        + "\n"
        + pr_reviews.render_review_history(item.item_id, review_history)
        + "\n## Record details\n\n"
        + f"- Item: {item.item_id}\n"
        + f"- Queue position: {item.queue_position if item.queue_position is not None else 'none'}\n"
        + f"- Source: {item.source if item.source is not None else 'none'}\n"
        + f"- Subject revision: {item.subject_revision}\n"
        + f"- Preparation: {overview_item.preparation.status.value if overview_item is not None and overview_item.preparation is not None else 'none'}\n"
        + f"- Proposal source task: {origin.source_task_id if origin is not None else 'none'}\n"
        + f"- Proposal trigger: {origin.trigger if origin is not None else 'none'}\n"
        + f"- Proposal relation: {origin.relation_kind.value if origin is not None else 'none'}\n"
        + f"- Related item: {origin.related_item if origin is not None and origin.related_item is not None else 'none'}\n"
        + f"- Proposal reason: {origin.why_it_matters if origin is not None else 'none'}\n"
        + f"- Proposal disposition: {origin.disposition.value if origin is not None and origin.disposition is not None else 'none'}\n"
        + f"- Disposition reason: {origin.disposition_reason if origin is not None and origin.disposition_reason is not None else 'none'}\n"
        + f"- Planned replacement: {replacement.replacement_item_id if replacement is not None else 'none'}\n"
        + f"- Replacement revision: {replacement.relation_revision if replacement is not None else 'none'}\n"
        + f"- Replacement cost: {replacement.replacement_cost if replacement is not None else 'none'}\n"
        + f"- Temporarily retained: {'yes' if replacement is not None and replacement.temporarily_retained else 'no'}\n"
        + f"- Outcome evidence: {item.outcome_evidence or 'none'}\n"
        + f"- Definition revision: {definition.revision}\n"
        + f"- Definition digest: {definition.digest}\n"
        + format_details
        + policy_details
    ).encode()


def _render_attempt(
    attempt: stored_state.StoredAttempt,
    item_state: stored_state.StoredWorkItemState,
    attempt_briefs: Mapping[AttemptId, bytes],
) -> bytes:
    if (brief := attempt_briefs.get(attempt.attempt_id)) is not None:
        return brief
    return (
        _render_header("work-attempt-view")
        + f"# Attempt {attempt.attempt_id}\n\n"
        + f"- Item: {attempt.item_id}\n"
        + f"- State: {'cancelled (item dropped)' if item_state == stored_state.StoredWorkItemState.DROPPED else attempt.state.value}\n"
        + f"- Branch: {attempt.branch}\n"
        + f"- Base revision: {attempt.base_revision}\n"
        + f"- Candidate revision: {attempt.candidate_revision or 'none'}\n"
    ).encode()


def _render_history_row(receipt: stored_state.StoredTransitionReceipt) -> str:
    outcome_json = bytes(receipt.outcome_payload).decode("utf-8").replace("|", r"\|")
    return (
        f"| {receipt.history_id} | {receipt.project_revision} | {receipt.action_id} | {outcome_json} | "
        f"{receipt.subject_id} | {receipt.committed_at.isoformat()} |\n"
    )


def _render_history(receipt: stored_state.StoredTransitionReceipt) -> bytes:
    return (
        _render_header("work-history-receipt-view")
        + f"# Transition {receipt.history_id}\n\n"
        + "| History | Revision | Action receipt | Recorded outcome | Subject | Committed |\n"
        + "| --- | --- | --- | --- | --- | --- |\n"
        + _render_history_row(receipt)
    ).encode()


def _damaged_view_message(damaged: tuple[query_models.DamagedTransitionReceipt, ...]) -> str:
    return " ".join(damaged_receipt_message(value) for value in damaged)


BOARD_MARKDOWN = "board.md"
BOARD_HTML = "board.html"
BOARD_LOCK = "board.lock"


def board_pages(work_root: Path) -> query_models.BoardPages:
    """Name both board projections under the selected work root, whether or not a refresh has written them yet."""

    view_root = work_root / "views"
    return query_models.BoardPages(str(view_root / BOARD_MARKDOWN), str(view_root / BOARD_HTML))


class _BoardGroup(Enum):
    WAITING_ON_HUMAN = "Paused or in review"
    IN_PROGRESS = "In progress"
    READY = "Ready"
    BLOCKED_OR_DEFERRED = "Blocked or deferred"


def _board_group(state: work_models.WorkState) -> _BoardGroup:
    match state:
        case work_models.WorkState.PAUSED | work_models.WorkState.REVIEW:
            return _BoardGroup.WAITING_ON_HUMAN
        case work_models.WorkState.ACTIVE:
            return _BoardGroup.IN_PROGRESS
        case work_models.WorkState.READY:
            return _BoardGroup.READY
        case work_models.WorkState.BLOCKED | work_models.WorkState.DEFERRED:
            return _BoardGroup.BLOCKED_OR_DEFERRED
        case _ as unreachable:
            assert_never(unreachable)


def _board_next_step(state: work_models.WorkState) -> str:
    match state:
        case work_models.WorkState.READY:
            return (
                "Start it: check its dependencies and holds, prepare the agreed brief, and obtain start authorization."
            )
        case work_models.WorkState.ACTIVE:
            return "Let its active attempt continue, or check its progress."
        case work_models.WorkState.PAUSED:
            return "Read why it is paused and decide whether it should resume."
        case work_models.WorkState.REVIEW:
            return "Have its submitted candidate reviewed independently and decide on the verdict."
        case work_models.WorkState.BLOCKED:
            return "Check whether what blocks it has been resolved."
        case work_models.WorkState.DEFERRED:
            return "Check whether its reopen condition is met."
        case _ as unreachable:
            assert_never(unreachable)


def _board_groups(
    items: tuple[query_models.BoardItem, ...],
) -> tuple[tuple[_BoardGroup, tuple[query_models.BoardItem, ...]], ...]:
    """Partition live items into display groups while keeping saved order within each."""

    return tuple(
        (group, tuple(item for item in items if _board_group(item.overview.state) is group)) for group in _BoardGroup
    )


def _utc_instant(value: datetime) -> str:
    """Name a stored time as a UTC instant that a browser parses and that never depends on the process timezone."""

    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_label(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _markdown_link_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _unsatisfied_dependencies(item: query_models.BoardItem) -> tuple[query_models.DependencyReason, ...]:
    """Name the live or dropped prerequisites whose obligations still apply."""

    return tuple(
        value for value in item.overview.dependency_reasons if value.item_id in item.overview.unsatisfied_dependencies
    )


def _render_board_markdown(board: query_models.BoardPortfolio) -> bytes:
    sections: list[str] = []
    for group, grouped in _board_groups(board.items):
        entries = "".join(
            f"- [{_markdown_link_text(item.overview.label)}](items/{item.overview.item_id}.md) "
            + f"`{item.overview.item_id}` ({item.overview.state.value}), changed {_utc_label(item.updated_at)}\n"
            + "".join(f"  - Depends on {value.item_id}: {value.reason}\n" for value in _unsatisfied_dependencies(item))
            for item in grouped
        )
        sections.append(f"## {group.value}\n\n{entries or '- None.\n'}\n")
    updated = (
        "" if board.updated_at is None else f"Updated {_utc_label(board.updated_at)}, when the newest item changed. "
    )
    return (
        _render_header("work-board-view")
        + "# Board\n\n"
        + updated
        + "Each item shows when its record last changed and lists only the dependencies that still apply. "
        + "Live items in saved order, grouped by what they wait on. "
        + f"[{BOARD_HTML}]({BOARD_HTML}) adds filtering and item detail.\n\n"
        + "".join(sections)
    ).encode()


class _BoardPageDependency(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    reason: str
    on_board: bool
    unsatisfied: bool


class _BoardPageCondition(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One recorded condition: its kind, label, the time and the record that time comes from, and recorded text."""

    kind: str
    label: str
    at: str | None
    time_source: str | None
    text: str | None
    absence: str | None


class _BoardPageRecordedRemainingWork(
    msgspec.Struct, tag="recorded", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    text: str


class _BoardPageFinalCheckpoint(msgspec.Struct, tag="final", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    pass


class _BoardPageCheckpoint(msgspec.Struct, tag="checkpoint", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    checkpoint_id: str
    title: str
    brief_revision: int
    brief_accepted_at: str
    remaining_work: _BoardPageRecordedRemainingWork | _BoardPageFinalCheckpoint


class _BoardPageUnreadableBrief(
    msgspec.Struct, tag="unreadable-brief", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    attempt_id: str
    message: str


class _BoardPageMilestone(msgspec.Struct, tag="accepted", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    history_id: int
    accepted_at: str
    checkpoint_id: str
    evidence: str


class _BoardPageUnreadableMilestone(
    msgspec.Struct, tag="unreadable", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    history_id: int
    committed_at: str
    defect: str


class _BoardPageNextStep(msgspec.Struct, tag="next-step", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    text: str


class _BoardPageStanding(msgspec.Struct, tag="attempt", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    checkpoint: _BoardPageCheckpoint | _BoardPageUnreadableBrief
    condition: _BoardPageCondition
    milestones: tuple[_BoardPageMilestone | _BoardPageUnreadableMilestone, ...]


class _BoardPageTextChange(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    field: str
    before: str | None
    after: str | None


class _BoardPageListChange(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    field: str
    added: tuple[str, ...]
    removed: tuple[str, ...]


class _BoardPageObligationChange(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    before: str
    after: str


class _BoardPageScopeChanges(msgspec.Struct, tag="changes", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    texts: tuple[_BoardPageTextChange, ...]
    lists: tuple[_BoardPageListChange, ...]
    obligations_added: tuple[str, ...]
    obligations_removed: tuple[str, ...]
    obligations_changed: tuple[_BoardPageObligationChange, ...]


class _BoardPageOriginalScope(
    msgspec.Struct, tag="original", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    pass


class _BoardPageScopeRevision(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: int
    accepted_at: str
    reason: str
    changes: _BoardPageScopeChanges | _BoardPageOriginalScope


class _BoardPageScope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """The current accepted definition; a field its stored schema lacks is null."""

    revision: int
    objective: str
    hypothesis: str
    scope: tuple[str, ...]
    non_scope: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    obligations: tuple[str, ...] | None
    evidence: tuple[str, ...]
    checkout_policy: str | None


class _BoardPageItem(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    title: str
    state: str
    group: str
    updated_at: str
    effect: str
    unlock: str
    dependencies: tuple[_BoardPageDependency, ...]
    standing: _BoardPageNextStep | _BoardPageStanding
    intake_next_action: str | None
    intake_notes: str | None
    scope_revisions: tuple[_BoardPageScopeRevision, ...]
    scope: _BoardPageScope


class _BoardPage(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-board-page/v2"]
    notice: str
    updated_at: str | None
    groups: tuple[str, ...]
    states: tuple[str, ...]
    items: tuple[_BoardPageItem, ...]


def _unsatisfied_first(dependencies: tuple[_BoardPageDependency, ...]) -> tuple[_BoardPageDependency, ...]:
    """List dependencies that still apply before solved ones, keeping stored order within each group."""

    return tuple(value for value in dependencies if value.unsatisfied) + tuple(
        value for value in dependencies if not value.unsatisfied
    )


def _condition(  # noqa: C901, PLR0912 - one exhaustive presentation of the closed condition family
    condition: query_models.BoardCondition,
) -> _BoardPageCondition:
    """Present a recorded condition with the record its time comes from, and name absent text as absent."""

    match condition:
        case query_models.ActivatedCondition(at=at):
            return _BoardPageCondition(
                "activated", "Attempt started", _utc_instant(at), "Activation receipt committed", None, None
            )
        case query_models.ResumedCondition(at=at):
            return _BoardPageCondition("resumed", "Resumed", _utc_instant(at), "Resume receipt committed", None, None)
        case query_models.PausedCondition(at=at, reason=reason):
            return _BoardPageCondition(
                "paused",
                "Paused",
                _utc_instant(at),
                "Pause receipt committed",
                reason,
                "No pause reason is recorded." if reason is None else None,
            )
        case query_models.BlockedCondition(at=at):
            return _BoardPageCondition(
                "blocked", "Blocked", _utc_instant(at), "Block receipt committed", None, "Blocking records no reason."
            )
        case query_models.ReboundCondition(at=at, attempt_state=attempt_state, pause_reason=reason):
            paused = attempt_state == work_models.AttemptState.PAUSED
            return _BoardPageCondition(
                "rebound",
                "Rebound to a new brief, still paused" if paused else "Rebound to a new brief",
                _utc_instant(at),
                "Rebind receipt committed",
                reason,
                "No pause reason is recorded." if paused and reason is None else None,
            )
        case query_models.SubmittedForReviewCondition(at=at):
            return _BoardPageCondition(
                "submitted", "Submitted for review", _utc_instant(at), "Submission receipt committed", None, None
            )
        case query_models.ReviewedReadyCondition(ready_at=ready_at):
            return _BoardPageCondition(
                "reviewed-ready",
                "Reviewed ready, waiting for acceptance",
                _utc_instant(ready_at),
                "Ready review record accepted",
                None,
                None,
            )
        case query_models.ReturnedCondition(at=at, reason=reason):
            return _BoardPageCondition(
                "returned", "Returned for correction", _utc_instant(at), "Return receipt committed", reason, None
            )
        case query_models.RetainedReturnedCondition(at=at, reason=reason):
            return _BoardPageCondition(
                "returned",
                "Returned for correction",
                _utc_instant(at),
                "Return receipt committed",
                reason,
                "No return reason is recorded." if reason is None else None,
            )
        case query_models.ContinuedCondition(at=at, evidence=evidence):
            return _BoardPageCondition(
                "continued",
                "Review accepted, work continues",
                _utc_instant(at),
                "Acceptance receipt committed",
                evidence,
                "No acceptance evidence is recorded." if evidence is None else None,
            )
        case query_models.CheckpointAcceptedCondition(at=at, checkpoint=checkpoint, evidence=evidence):
            return _BoardPageCondition(
                "checkpoint-accepted",
                f"Checkpoint {checkpoint} accepted",
                _utc_instant(at),
                "Checkpoint acceptance receipt committed",
                evidence,
                None,
            )
        case query_models.RetainedCheckpointAcceptedCondition(at=at, checkpoint=checkpoint, evidence=evidence):
            return _BoardPageCondition(
                "checkpoint-accepted",
                "Checkpoint accepted" if checkpoint is None else f"Checkpoint {checkpoint} accepted",
                _utc_instant(at),
                "Checkpoint acceptance receipt committed",
                evidence,
                "No acceptance evidence is recorded." if evidence is None else None,
            )
        case query_models.RecordedActionCondition(at=at, action_kind=action_kind):
            return _BoardPageCondition(
                "recorded-action",
                f"Latest recorded action: {action_kind.value}",
                _utc_instant(at),
                "Receipt committed",
                None,
                None,
            )
        case query_models.UnreadableCondition(history_id=history_id, at=at, defect=defect):
            return _BoardPageCondition(
                "unreadable",
                f"Unreadable recorded condition (history {history_id})",
                _utc_instant(at),
                "Receipt committed",
                None,
                defect,
            )
        case query_models.NoReceiptCondition():
            return _BoardPageCondition(
                "no-receipt",
                "No receipt is recorded at this attempt's latest revision",
                None,
                None,
                None,
                None,
            )
        case _ as unreachable:
            assert_never(unreachable)


def _page_checkpoint(
    checkpoint: query_models.BoardCheckpoint | query_models.UnreadableBrief,
) -> _BoardPageCheckpoint | _BoardPageUnreadableBrief:
    match checkpoint:
        case query_models.UnreadableBrief(attempt_id=attempt_id, message=message):
            return _BoardPageUnreadableBrief(str(attempt_id), message)
        case query_models.BoardCheckpoint(remaining_work=remaining_work):
            remaining: _BoardPageRecordedRemainingWork | _BoardPageFinalCheckpoint
            match remaining_work:
                case query_models.RecordedRemainingWork(text=text):
                    remaining = _BoardPageRecordedRemainingWork(text)
                case query_models.FinalCheckpoint():
                    remaining = _BoardPageFinalCheckpoint()
                case _ as unreachable:
                    assert_never(unreachable)
            return _BoardPageCheckpoint(
                checkpoint.checkpoint_id,
                checkpoint.title,
                checkpoint.brief_revision,
                _utc_instant(checkpoint.brief_accepted_at),
                remaining,
            )
        case _ as unreachable:
            assert_never(unreachable)


def _standing(item: query_models.BoardItem) -> _BoardPageNextStep | _BoardPageStanding:
    attempt = item.attempt
    if attempt is None:
        return _BoardPageNextStep(_board_next_step(item.overview.state))
    return _BoardPageStanding(
        _page_checkpoint(attempt.checkpoint),
        _condition(attempt.condition),
        tuple(
            _BoardPageMilestone(
                int(value.history_id), _utc_instant(value.accepted_at), value.checkpoint_id, value.evidence
            )
            if isinstance(value, query_models.BoardMilestone)
            else _BoardPageUnreadableMilestone(int(value.history_id), _utc_instant(value.committed_at), value.defect)
            for value in attempt.milestones
        ),
    )


def _text_field_label(field: query_models.DefinitionTextField) -> str:
    match field:
        case query_models.DefinitionTextField.TITLE:
            return "Title"
        case query_models.DefinitionTextField.OBJECTIVE:
            return "Objective"
        case query_models.DefinitionTextField.HYPOTHESIS:
            return "Reason"
        case query_models.DefinitionTextField.EFFECT:
            return "Expected result"
        case query_models.DefinitionTextField.UNLOCK:
            return "What this unlocks"
        case query_models.DefinitionTextField.CHECKOUT_POLICY:
            return "Checkout policy"
        case _ as unreachable:
            assert_never(unreachable)


def _list_field_label(field: query_models.DefinitionListField) -> str:
    match field:
        case query_models.DefinitionListField.SCOPE:
            return "Scope"
        case query_models.DefinitionListField.NON_SCOPE:
            return "Outside scope"
        case query_models.DefinitionListField.ACCEPTANCE_CRITERIA:
            return "Acceptance criteria"
        case query_models.DefinitionListField.EVIDENCE:
            return "Evidence"
        case query_models.DefinitionListField.DEPENDENCIES:
            return "Dependencies"
        case _ as unreachable:
            assert_never(unreachable)


def _obligation_text(value: work_models.WorkObligation | query_models.WorkObligationView) -> str:
    return f"{value.obligation_id} ({_deferral_label(value.deferral_policy)}): {value.statement}"


def _scope_revision(value: query_models.BoardScopeRevision) -> _BoardPageScopeRevision:
    changes = value.changes
    match changes:
        case query_models.OriginalScope():
            page_changes: _BoardPageScopeChanges | _BoardPageOriginalScope = _BoardPageOriginalScope()
        case query_models.ScopeRevisionChanges():
            page_changes = _BoardPageScopeChanges(
                tuple(
                    _BoardPageTextChange(_text_field_label(change.field), change.before, change.after)
                    for change in changes.texts
                ),
                tuple(
                    _BoardPageListChange(_list_field_label(change.field), change.added, change.removed)
                    for change in changes.lists
                ),
                tuple(_obligation_text(obligation) for obligation in changes.obligations_added),
                tuple(_obligation_text(obligation) for obligation in changes.obligations_removed),
                tuple(
                    _BoardPageObligationChange(_obligation_text(change.before), _obligation_text(change.after))
                    for change in changes.obligations_changed
                ),
            )
        case _ as unreachable:
            assert_never(unreachable)
    return _BoardPageScopeRevision(value.revision, _utc_instant(value.accepted_at), value.reason, page_changes)


def _page_scope(item: query_models.BoardItem) -> _BoardPageScope:
    definition = item.definition
    match definition:
        case query_models.WorkItemDefinitionView():
            obligations: tuple[str, ...] | None = tuple(_obligation_text(value) for value in definition.obligations)
            checkout_policy: str | None = definition.checkout_policy.value
        case definition_compatibility.HistoricalDefinitionV1():
            obligations = None
            checkout_policy = None
        case _ as unreachable:
            assert_never(unreachable)
    return _BoardPageScope(
        item.scope_revisions[-1].revision,
        definition.objective,
        definition.hypothesis,
        definition.scope,
        definition.non_scope,
        definition.acceptance_criteria,
        obligations,
        definition.evidence,
        checkout_policy,
    )


def _board_page_item(value: query_models.BoardItem, live_item_ids: frozenset[str]) -> _BoardPageItem:
    item = value.overview
    return _BoardPageItem(
        item.item_id,
        item.label,
        item.state.value,
        _board_group(item.state).value,
        _utc_instant(value.updated_at),
        item.effect,
        item.unlock,
        _unsatisfied_first(
            tuple(
                _BoardPageDependency(
                    dependency.item_id,
                    dependency.reason,
                    dependency.item_id in live_item_ids,
                    dependency.item_id in item.unsatisfied_dependencies,
                )
                for dependency in item.dependency_reasons
            )
        ),
        _standing(value),
        item.next_action,
        item.notes,
        tuple(_scope_revision(revision) for revision in value.scope_revisions),
        _page_scope(value),
    )


def _board_page_data(board: query_models.BoardPortfolio) -> bytes:
    """Encode page data as JSON that cannot close its script element or break a JavaScript string."""

    live_item_ids = frozenset(item.overview.item_id for item in board.items)
    page = _BoardPage(
        "pinboard-board-page/v2",
        NOTICE,
        None if board.updated_at is None else _utc_instant(board.updated_at),
        tuple(group.value for group in _BoardGroup),
        tuple(state.value for group in _BoardGroup for state in work_models.WorkState if _board_group(state) is group),
        tuple(_board_page_item(value, live_item_ids) for value in board.items),
    )
    return (
        msgspec.json.encode(page)
        .replace(b"&", b"\\u0026")
        .replace(b"<", b"\\u003c")
        .replace(b">", b"\\u003e")
        .replace("\u2028".encode(), b"\\u2028")
        .replace("\u2029".encode(), b"\\u2029")
    )


_BOARD_PAGE_HEAD = """<!doctype html>
<!-- Generated projection; SQLite is authoritative. -->
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pinboard board</title>
<style>
:root { color-scheme: light dark; --bg: #fbfbfa; --fg: #1d1d1b; --muted: #6a6a64; --line: #deded8; --card: #ffffff;
  --accent: #2f5fb3; --add: #2e7d32; --remove: #b3261e; --badge-width: 4.5rem; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #171716; --fg: #ececea; --muted: #a2a29b; --line: #3a3a37; --card: #20201f; --accent: #8fb0ea;
    --add: #7cc47f; --remove: #f28b82; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
header, .controls, main { max-width: 960px; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 4px; }
h2 { font-size: 1.05rem; margin: 24px 0 8px; }
h3 { font-size: 0.95rem; margin: 12px 0 4px; }
.notice, .count, .empty, .context { color: var(--muted); }
.notice { margin: 0 0 12px; }
.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px; }
.controls input, .controls select { font: inherit; padding: 6px 8px; border: 1px solid var(--line);
  border-radius: 6px; background: var(--card); color: var(--fg); }
.controls input { flex: 1 1 240px; }
details.item { background: var(--card); border: 1px solid var(--line); border-radius: 8px; margin: 6px 0; }
details.item > summary { cursor: pointer; padding: 8px 12px; display: grid;
  grid-template-columns: minmax(0, 1fr) var(--badge-width);
  grid-template-areas: "title state" "meta meta" "standing standing" "deps deps";
  gap: 2px 10px; align-items: baseline; }
.title { grid-area: title; font-weight: 600; overflow-wrap: anywhere; }
.state { grid-area: state; width: var(--badge-width); text-align: center; font-size: 0.8rem; padding: 0 6px;
  border: 1px solid var(--line); border-radius: 10px; }
.meta { grid-area: meta; display: flex; flex-wrap: wrap; gap: 0 10px; align-items: baseline; }
.id { overflow-wrap: anywhere; }
.deps { grid-area: deps; overflow-wrap: anywhere; }
.id, .deps, .when { color: var(--muted); font-size: 0.85rem; }
.standing { grid-area: standing; font-size: 0.88rem; overflow-wrap: anywhere; display: -webkit-box;
  -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.checkpoint-title { font-weight: 600; }
.condition { color: var(--muted); }
.stamp { cursor: pointer; border-bottom: 1px dotted currentColor; }
.item-time { margin-left: auto; color: var(--muted); font-size: 0.85rem; }
.body { padding: 0 12px 12px; border-top: 1px solid var(--line); overflow-wrap: anywhere; }
.body p { margin: 6px 0; }
.body ul, .body ol { margin: 4px 0; padding-left: 20px; }
.unreadable { border-left: 3px solid var(--remove); padding: 2px 8px; }
.verbatim { white-space: pre-wrap; }
details.section { margin: 8px 0; }
details.section > summary { cursor: pointer; font-weight: 600; font-size: 0.92rem; }
.entry { border-top: 1px solid var(--line); padding: 6px 0; }
.entry-head { display: flex; flex-wrap: wrap; gap: 0 10px; align-items: baseline; font-size: 0.88rem; }
.entry-kind { font-weight: 600; }
.added { color: var(--add); }
.removed { color: var(--remove); }
.record { margin: 6px 0; }
.record .lead { font-weight: 600; }
.record .detail p { margin: 2px 0; }
.record .detail ul, .record .detail ol { margin: 2px 0 6px; padding-left: 22px; }
.record .label { color: var(--add); font-weight: 600; margin-right: 4px; }
.record .references { color: var(--muted); font-size: 0.8rem; }
code.inline { font-size: 0.86em; padding: 0 3px; border-radius: 3px;
  background: color-mix(in srgb, var(--line) 55%, transparent); }
a { color: var(--accent); }
</style>
</head>
<body>
<nav><a href="roadmap.html">Open the initiative roadmap</a></nav>
<header>
<h1>Pinboard board</h1>
<p class="notice">Generated projection; SQLite is authoritative. This page is read-only.</p>
<p id="board-updated" class="when"></p>
</header>
<div class="controls">
<input id="filter-text" type="search" placeholder="Filter by text" aria-label="Filter by text">
<select id="filter-state" aria-label="Filter by state"><option value="">All states</option></select>
<span id="count" class="count"></span>
</div>
<main id="board"></main>
<noscript><p>This page needs JavaScript. Open board.md for the same board as plain text.</p></noscript>
<script id="board-data" type="application/json">"""

_BOARD_PAGE_TAIL = """</script>
<script>
(function () {
  "use strict";
  var data = JSON.parse(document.getElementById("board-data").textContent);
  var board = document.getElementById("board");
  var text = document.getElementById("filter-text");
  var state = document.getElementById("filter-state");
  var count = document.getElementById("count");
  var sections = [];
  var stamps = [];
  var absoluteVisible = false;

  function node(tag, className, content) {
    var element = document.createElement(tag);
    if (className) { element.className = className; }
    if (content !== undefined && content !== null) { element.textContent = content; }
    return element;
  }

  function relativeAge(instant) {
    var seconds = Math.floor((Date.now() - Date.parse(instant)) / 1000);
    var units = [["year", 31536000], ["month", 2592000], ["day", 86400], ["hour", 3600], ["minute", 60]];
    for (var index = 0; index < units.length; index += 1) {
      if (seconds >= units[index][1]) {
        var amount = Math.floor(seconds / units[index][1]);
        return amount + " " + units[index][0] + (amount === 1 ? "" : "s") + " ago";
      }
    }
    return "just now";
  }

  function localTime(instant) {
    return new Date(instant).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "long" });
  }

  function renderStamps() {
    stamps.forEach(function (entry) {
      var relative = relativeAge(entry.instant);
      var absolute = localTime(entry.instant);
      entry.element.textContent = absoluteVisible ? absolute : relative;
      entry.element.title = entry.source + ": " + (absoluteVisible ? relative : absolute);
    });
  }

  function swapStamps(event) {
    event.preventDefault();
    event.stopPropagation();
    absoluteVisible = !absoluteVisible;
    renderStamps();
  }

  // Every shown time names the record it comes from in its tooltip.
  function stamp(className, instant, source) {
    var time = node("time", className);
    time.dateTime = instant;
    time.tabIndex = 0;
    time.setAttribute("role", "button");
    time.addEventListener("click", swapStamps);
    time.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") { swapStamps(event); }
    });
    stamps.push({ element: time, instant: instant, source: source });
    return time;
  }

  function field(parent, label, value) {
    var row = node("p");
    row.appendChild(node("strong", null, label + ": "));
    row.appendChild(document.createTextNode(value));
    parent.appendChild(row);
  }

  // Light rendering of stored plain text, inserted only as text nodes: backtick spans become inline code and a
  // leading capitalized "Label:" is highlighted.
  function inline(parent, value) {
    value.split(/(`[^`]+`)/).forEach(function (part) {
      if (/^`[^`]+`$/.test(part)) { parent.appendChild(node("code", "inline", part.slice(1, -1))); }
      else if (part) { parent.appendChild(document.createTextNode(part)); }
    });
    return parent;
  }

  function labelled(parent, value) {
    var match = value.match(/^([A-Z][A-Za-z ]{1,30}):(\\s.*)?$/);
    if (!match) { return inline(parent, value); }
    parent.appendChild(node("span", "label", match[1] + ":"));
    if (match[2]) { inline(parent, match[2]); }
    return parent;
  }

  function lightDetail(value) {
    var root = node("div", "detail");
    var list = null;
    value.split("\\n").forEach(function (line) {
      var bullet = line.match(/^- (.*)$/);
      var numbered = line.match(/^(\\d+)\\. (.*)$/);
      if (bullet || numbered) {
        var kind = bullet ? "ul" : "ol";
        if (!list || list.tagName.toLowerCase() !== kind) { list = node(kind); root.appendChild(list); }
        labelled(list.appendChild(node("li")), bullet ? bullet[1] : numbered[2]);
        return;
      }
      list = null;
      if (line.trim()) { labelled(root.appendChild(node("p")), line); }
    });
    return root;
  }

  // Single-line record text renders as written. Multi-line text leads with its first line in bold, renders "- "
  // bullets and "1. " numbered items, and shows a trailing "References:" line small and muted.
  function recordText(parent, value) {
    var box = node("div", "record");
    var lines = value.split("\\n");
    if (lines.length === 1) {
      box.appendChild(node("p", null, value));
      parent.appendChild(box);
      return;
    }
    box.appendChild(node("p", "lead", lines[0]));
    var rest = lines.slice(1).join("\\n").replace(/^\\n+/, "");
    var references = null;
    var match = rest.match(/\\n*(References: [^\\n]*)$/);
    if (match) { references = match[1]; rest = rest.slice(0, match.index); }
    if (rest) { box.appendChild(lightDetail(rest)); }
    if (references) { box.appendChild(node("p", "references", references)); }
    parent.appendChild(box);
  }

  function firstLine(value) { return value.split("\\n")[0]; }

  function conditionTime(parent, condition) {
    if (condition.at) {
      parent.appendChild(document.createTextNode(" "));
      parent.appendChild(stamp("stamp", condition.at, condition.time_source));
    }
  }

  function standingLine(summary, standing) {
    if (standing.kind !== "attempt") { return; }
    var line = node("span", "standing");
    if (standing.checkpoint.kind === "checkpoint") {
      line.appendChild(node("span", "checkpoint-title", standing.checkpoint.title));
    } else {
      line.appendChild(node("span", "checkpoint-title", "Current brief unreadable"));
    }
    line.appendChild(document.createTextNode(" · "));
    line.appendChild(node("span", "condition", standing.condition.label));
    conditionTime(line, standing.condition);
    if (standing.condition.text) {
      line.appendChild(node("span", "condition", " — " + firstLine(standing.condition.text)));
    }
    summary.appendChild(line);
  }

  function whereItStands(body, item) {
    var standing = item.standing;
    if (standing.kind !== "attempt") {
      field(body, "Next step", standing.text);
      return;
    }
    body.appendChild(node("h3", null, "Where it stands"));
    var checkpoint = standing.checkpoint;
    if (checkpoint.kind === "checkpoint") {
      var current = node("p");
      current.appendChild(node("strong", null, "Current checkpoint: "));
      current.appendChild(document.createTextNode(checkpoint.title + " "));
      current.appendChild(node("code", "id", checkpoint.checkpoint_id));
      body.appendChild(current);
      var when = node("p", "when");
      when.appendChild(document.createTextNode("Brief revision " + checkpoint.brief_revision + " accepted "));
      when.appendChild(stamp("stamp", checkpoint.brief_accepted_at, "Brief revision accepted"));
      body.appendChild(when);
    } else {
      body.appendChild(node("p", "unreadable", "The current brief of attempt " + checkpoint.attempt_id +
        " is unreadable, so its checkpoint is not shown: " + checkpoint.message));
    }
    var condition = node("p");
    condition.appendChild(node("strong", null, standing.condition.label));
    conditionTime(condition, standing.condition);
    body.appendChild(condition);
    if (standing.condition.text) {
      recordText(body, standing.condition.text);
    } else if (standing.condition.absence) {
      body.appendChild(node("p", "empty", standing.condition.absence));
    }
    body.appendChild(node("p", "context", "\\u201c" + item.state + "\\u201d is the recorded state; it does not say " +
      "whether an agent is running right now."));
  }

  function remainingWork(body, standing) {
    if (standing.kind !== "attempt" || standing.checkpoint.kind !== "checkpoint") { return; }
    body.appendChild(node("h3", null, "Recorded remaining work"));
    var remaining = standing.checkpoint.remaining_work;
    if (remaining.kind === "final") {
      body.appendChild(node("p", "empty", "The current brief records no further checkpoint after this one."));
      return;
    }
    body.appendChild(node("p", "context", "Recorded in the current brief; not a schedule or checklist."));
    recordText(body, remaining.text);
  }

  function section(body, title, build) {
    var details = node("details", "section");
    details.appendChild(node("summary", null, title));
    var inner = node("div");
    details.appendChild(inner);
    var built = false;
    details.addEventListener("toggle", function () {
      if (details.open && !built) { build(inner); built = true; renderStamps(); }
    });
    body.appendChild(details);
  }

  function entryHead(parent, label, instant, source) {
    var head = node("div", "entry-head");
    head.appendChild(node("span", "entry-kind", label));
    head.appendChild(stamp("stamp", instant, source));
    parent.appendChild(head);
    return head;
  }

  function milestones(inner, standing) {
    inner.appendChild(node("p", "context", "Recorded checkpoint acceptances of the current attempt, newest first. " +
      "Acceptance is not integration: whether a change reached the target branch is not part of this record. " +
      "Acceptances recorded in the older transition-receipt/v1 format are not listed."));
    if (!standing.milestones.length) {
      inner.appendChild(node("p", "empty", "No checkpoint acceptance is recorded for the current attempt."));
    }
    standing.milestones.forEach(function (milestone) {
      var entry = node("div", "entry");
      if (milestone.kind === "accepted") {
        var head = entryHead(entry, "Accepted", milestone.accepted_at, "Checkpoint acceptance receipt committed");
        head.appendChild(node("code", "id", milestone.checkpoint_id));
        recordText(entry, milestone.evidence);
      } else {
        entryHead(entry, "Unreadable acceptance (history " + milestone.history_id + ")", milestone.committed_at,
          "Receipt committed");
        entry.appendChild(node("p", "empty", milestone.defect));
      }
      inner.appendChild(entry);
    });
  }

  function change(list, className, sign, label, value) {
    var entry = node("li");
    entry.appendChild(node("span", className, sign + " " + label + ": "));
    entry.appendChild(document.createTextNode(value === null ? "(not in this schema)" : value));
    list.appendChild(entry);
  }

  function scopeChanges(inner, item) {
    inner.appendChild(node("p", "context", "Each accepted definition revision, newest first, with its recorded " +
      "reason and the fields that changed from the previous revision."));
    item.scope_revisions.slice().reverse().forEach(function (revision) {
      var entry = node("div", "entry");
      var original = revision.changes.kind === "original";
      entryHead(entry, "Revision " + revision.revision + (original ? " (original scope)" : ""), revision.accepted_at,
        "Definition revision accepted");
      recordText(entry, revision.reason);
      if (!original) {
        var changes = revision.changes;
        var list = node("ul");
        changes.texts.forEach(function (value) {
          change(list, "removed", "\\u2212", value.field + " was", value.before);
          change(list, "added", "+", value.field + " now", value.after);
        });
        changes.lists.forEach(function (value) {
          value.added.forEach(function (entryText) { change(list, "added", "+", value.field, entryText); });
          value.removed.forEach(function (entryText) { change(list, "removed", "\\u2212", value.field, entryText); });
        });
        changes.obligations_added.forEach(function (value) { change(list, "added", "+", "Obligation", value); });
        changes.obligations_removed.forEach(function (value) {
          change(list, "removed", "\\u2212", "Obligation", value);
        });
        changes.obligations_changed.forEach(function (value) {
          change(list, "removed", "\\u2212", "Obligation was", value.before);
          change(list, "added", "+", "Obligation now", value.after);
        });
        if (!list.childNodes.length) { list.appendChild(node("li", "empty", "No field changed.")); }
        entry.appendChild(list);
      }
      inner.appendChild(entry);
    });
  }

  function fullScope(inner, item) {
    var scope = item.scope;
    function listSection(title, values) {
      inner.appendChild(node("h3", null, title));
      if (values === null) { inner.appendChild(node("p", "empty", "Not part of this definition schema.")); return; }
      if (!values.length) { inner.appendChild(node("p", "empty", "None recorded.")); return; }
      var list = node("ul");
      values.forEach(function (value) { list.appendChild(node("li", null, value)); });
      inner.appendChild(list);
    }
    inner.appendChild(node("p", "context", "The current accepted definition, revision " + scope.revision + "."));
    field(inner, "Objective", scope.objective);
    field(inner, "Reason", scope.hypothesis);
    listSection("Scope", scope.scope);
    listSection("Outside scope", scope.non_scope);
    listSection("Acceptance criteria", scope.acceptance_criteria);
    listSection("Obligations", scope.obligations);
    listSection("Evidence", scope.evidence);
    if (scope.checkout_policy !== null) { field(inner, "Checkout policy", scope.checkout_policy); }
  }

  function card(item) {
    var details = node("details", "item");
    details.id = "item-" + item.item_id;
    var summary = node("summary");
    summary.appendChild(node("span", "title", item.title));
    summary.appendChild(node("span", "state", item.state));
    var meta = node("span", "meta");
    meta.appendChild(node("code", "id", item.item_id));
    meta.appendChild(stamp("stamp item-time", item.updated_at, "Item record changed"));
    summary.appendChild(meta);
    standingLine(summary, item.standing);
    var applying = item.dependencies.filter(function (value) { return value.unsatisfied; });
    if (applying.length) {
      summary.appendChild(node("span", "deps", "Depends on " + applying.map(function (value) {
        return value.item_id;
      }).join(", ")));
    }
    details.appendChild(summary);
    var body = node("div", "body");
    whereItStands(body, item);
    remainingWork(body, item.standing);
    field(body, "Expected result", item.effect);
    field(body, "What this unlocks", item.unlock);
    body.appendChild(node("h3", null, "Dependencies"));
    if (item.dependencies.length) {
      var list = node("ul");
      item.dependencies.forEach(function (value) {
        var entry = node("li");
        if (value.on_board) {
          var link = node("a", null, value.item_id);
          link.href = "#item-" + value.item_id;
          link.addEventListener("click", function () { document.getElementById(link.hash.slice(1)).open = true; });
          entry.appendChild(link);
        } else {
          entry.appendChild(document.createTextNode(value.item_id +
            (value.unsatisfied ? " (unsatisfied; no longer live)" : " (no longer live)")));
        }
        entry.appendChild(document.createTextNode(": " + value.reason));
        list.appendChild(entry);
      });
      body.appendChild(list);
    } else {
      body.appendChild(node("p", "empty", "None recorded."));
    }
    if (item.intake_next_action || item.intake_notes) {
      body.appendChild(node("h3", null, "Original intake context"));
      body.appendChild(node("p", "context", "Recorded when this work was saved; original context, not the current plan."));
      if (item.intake_next_action) { field(body, "Next action at intake", item.intake_next_action); }
      if (item.intake_notes) { field(body, "Notes at intake", item.intake_notes); }
    }
    if (item.standing.kind === "attempt") {
      section(body, "Accepted milestones \\u00b7 " + item.standing.milestones.length, function (inner) {
        milestones(inner, item.standing);
      });
    }
    section(body, "Scope changes \\u00b7 " + item.scope_revisions.length + " revision" +
      (item.scope_revisions.length === 1 ? "" : "s"), function (inner) { scopeChanges(inner, item); });
    section(body, "Full accepted scope", function (inner) { fullScope(inner, item); });
    details.appendChild(body);
    details.dataset.state = item.state;
    var standingText = item.standing.kind === "attempt" ? [item.standing.condition.label].concat(
      item.standing.checkpoint.kind === "checkpoint" ? [item.standing.checkpoint.title] : []) : [item.standing.text];
    details.dataset.search = [item.item_id, item.title, item.state, item.effect, item.unlock].concat(standingText)
      .concat(item.dependencies.map(function (value) { return value.item_id + " " + value.reason; }))
      .join(" ").toLowerCase();
    return details;
  }

  var updated = document.getElementById("board-updated");
  if (data.updated_at) {
    updated.appendChild(document.createTextNode("Board updated "));
    updated.appendChild(stamp("stamp", data.updated_at, "Newest live item record changed"));
  }
  window.setInterval(renderStamps, 60000);

  data.states.forEach(function (value) {
    var option = node("option", null, value);
    option.value = value;
    state.appendChild(option);
  });
  data.groups.forEach(function (group) {
    var groupSection = node("section");
    groupSection.appendChild(node("h2", null, group));
    var cards = data.items.filter(function (item) { return item.group === group; }).map(card);
    cards.forEach(function (element) { groupSection.appendChild(element); });
    var empty = node("p", "empty", "None.");
    groupSection.appendChild(empty);
    board.appendChild(groupSection);
    sections.push({ element: groupSection, cards: cards, empty: empty });
  });

  function applyFilter() {
    var query = text.value.trim().toLowerCase();
    var selected = state.value;
    var shown = 0;
    sections.forEach(function (groupSection) {
      var visible = 0;
      groupSection.cards.forEach(function (element) {
        var match = (!selected || element.dataset.state === selected) &&
          (!query || element.dataset.search.indexOf(query) !== -1);
        element.hidden = !match;
        if (match) { visible += 1; }
      });
      groupSection.empty.hidden = visible > 0;
      shown += visible;
    });
    count.textContent = shown + " of " + data.items.length + " live items";
  }

  text.addEventListener("input", applyFilter);
  state.addEventListener("change", applyFilter);
  applyFilter();
  renderStamps();
}());
</script>
</body>
</html>
"""


def _render_board_html(board: query_models.BoardPortfolio) -> bytes:
    return _BOARD_PAGE_HEAD.encode() + _board_page_data(board) + _BOARD_PAGE_TAIL.encode()


@contextmanager
def _board_lock(view_root: Path) -> Generator[None]:
    """Hold the exclusive board lock; the kernel releases it when this process exits."""

    path = view_root / BOARD_LOCK
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o644)
    except OSError as error:
        raise FileIOError(FileIOErrorCode.VIEW_REFRESH_FAILED, f"Board lock could not be opened: {path}") from error
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as error:
            raise FileIOError(
                FileIOErrorCode.VIEW_REFRESH_FAILED, f"Board lock could not be acquired: {path}"
            ) from error
        yield
    finally:
        os.close(descriptor)


def _write_board(view_root: Path, board_reader: ports.BoardReader, now: datetime) -> None:
    """Read the composed board and replace both board files under one board lock."""

    with _board_lock(view_root):
        board = board_reader.read_board(now)
        roadmap_markdown, roadmap_html = roadmap_files.render(board_reader.read_roadmap())
        atomic_replace(view_root / "roadmap.md", roadmap_markdown)
        atomic_replace(view_root / "roadmap.html", roadmap_html)
        atomic_replace(view_root / BOARD_MARKDOWN, _render_board_markdown(board))
        atomic_replace(view_root / BOARD_HTML, _render_board_html(board))


def refresh_facts(
    facts: query_models.GeneratedViewFacts,
    work_root: Path,
    attempt_briefs: Mapping[AttemptId, bytes],
    board_reader: ports.BoardReader,
    now: datetime,
) -> ViewRefreshResult:
    """Write selectors named by exact post-commit projection facts, then both board projections."""

    try:
        damaged = _write_facts(facts, work_root, attempt_briefs)
        _write_board(ensure_child_directory(work_root, "views"), board_reader, now)
    except (FileIOError, ports.WorkStoreError) as error:
        return ViewRefreshResult(
            facts.project_revision,
            ViewWarning(
                f"The SQLite transition succeeded, but generated views need repair: {error}",
                "Run 'pinboard views rebuild'.",
            ),
        )
    if damaged:
        return ViewRefreshResult(
            facts.project_revision,
            ViewWarning(
                "The SQLite transition succeeded, but an item view was not refreshed: "
                f"{_damaged_view_message(damaged)}",
                damaged_receipt_recovery(damaged[0]),
            ),
        )
    return ViewRefreshResult(facts.project_revision, None)


def _write_facts(
    facts: query_models.GeneratedViewFacts,
    work_root: Path,
    attempt_briefs: Mapping[AttemptId, bytes],
) -> tuple[query_models.DamagedTransitionReceipt, ...]:
    """Write every derivable view and return consumed receipts that left an item view underivable."""

    damaged: list[query_models.DamagedTransitionReceipt] = []
    view_root = ensure_child_directory(work_root, "views")
    if facts.items:
        item_root = ensure_child_directory(view_root, "items")
        for selected in facts.items:
            item = selected.work_item
            if isinstance(selected.pause_reason, query_models.DamagedTransitionReceipt):
                damaged.append(selected.pause_reason)
                continue
            atomic_replace(
                item_root / f"{item.item_id}.md",
                _render_item(
                    item,
                    selected.dependencies,
                    selected.overview,
                    selected.definition,
                    selected.review_history,
                    selected.pause_reason,
                ),
            )
    if facts.attempts:
        attempt_root = ensure_child_directory(view_root, "attempts")
        for selected in facts.attempts:
            attempt = selected.attempt
            atomic_replace(
                attempt_root / f"{attempt.attempt_id}.md", _render_attempt(attempt, selected.item_state, attempt_briefs)
            )
    if facts.receipts:
        history_root = ensure_child_directory(view_root, "history")
        for receipt in facts.receipts:
            atomic_replace(history_root / f"{receipt.history_id}.md", _render_history(receipt))
    return tuple(damaged)


def rebuild_facts(
    facts: query_models.GeneratedViewFacts,
    work_root: Path,
    attempt_briefs: Mapping[AttemptId, bytes],
    board_reader: ports.BoardReader,
    now: datetime,
) -> ViewRefreshResult:
    """Reconcile every declared view from project-wide projection facts and the composed board."""

    try:
        view_root = ensure_child_directory(work_root, "views")
        damaged = _write_facts(facts, work_root, attempt_briefs)
        _write_board(view_root, board_reader, now)
    except (FileIOError, ports.WorkStoreError) as error:
        return ViewRefreshResult(
            facts.project_revision,
            ViewWarning(
                f"Generated views could not be rebuilt: {error}",
                "Resolve the filesystem problem and run 'pinboard views rebuild' again.",
            ),
        )
    if damaged:
        return ViewRefreshResult(
            facts.project_revision,
            ViewWarning(
                f"Generated views could not be rebuilt: {_damaged_view_message(damaged)}",
                damaged_receipt_recovery(damaged[0]),
            ),
        )
    return ViewRefreshResult(facts.project_revision, None)


@dataclass(frozen=True, slots=True)
class ExpectedViews:
    """Canonical bytes for every derivable view and the consumed receipts that prevented the rest."""

    views: Mapping[str, bytes]
    damaged: tuple[query_models.DamagedTransitionReceipt, ...]


def derive_expected_view_bytes(
    state: stored_state.StoredWorkState,
    attempt_briefs: Mapping[AttemptId, bytes],
    board_history: query_models.BoardHistoryFacts,
    *,
    now: datetime,
) -> ExpectedViews:
    """Return every generated selector and its canonical bytes for one SQLite snapshot and its decoded board facts."""

    view_inputs = _project_view_inputs(state, board_history, now)
    receipts_by_revision = {receipt.project_revision: receipt for receipt in state.transition_receipts}
    pause_reasons: dict[WorkItemId, query_models.RecordedPauseReason] = {}
    for attempt in state.lifecycle.attempts:
        if attempt.state != work_models.AttemptState.DONE:
            latest = receipts_by_revision.get(attempt.subject_revision)
            pause_reasons[attempt.item_id] = (
                None
                if latest is None
                else decode_recorded_pause_reason(
                    attempt.attempt_id,
                    attempt.state,
                    latest.history_id,
                    latest.committed_at,
                    latest.action_kind,
                    latest.outcome_schema,
                    bytes(latest.outcome_payload),
                )
            )
    expected_views: dict[str, bytes] = {}
    damaged: list[query_models.DamagedTransitionReceipt] = []
    for item in state.lifecycle.work_items:
        pause_reason = pause_reasons.get(item.item_id)
        if isinstance(pause_reason, query_models.DamagedTransitionReceipt):
            damaged.append(pause_reason)
            continue
        expected_views[f"items/{item.item_id}.md"] = _render_item(
            item,
            view_inputs.dependencies[item.item_id],
            view_inputs.overview_items.get(str(item.item_id)),
            view_inputs.definitions[item.item_id],
            tuple(receipt for receipt in state.transition_receipts if receipt.subject_id == item.item_id),
            pause_reason,
        )
    expected_views.update(
        (
            f"attempts/{attempt.attempt_id}.md",
            _render_attempt(
                attempt,
                next(item.state for item in state.lifecycle.work_items if item.item_id == attempt.item_id),
                attempt_briefs,
            ),
        )
        for attempt in state.lifecycle.attempts
    )
    expected_views.update(
        (f"history/{receipt.history_id}.md", _render_history(receipt)) for receipt in state.transition_receipts
    )
    roadmap_markdown, roadmap_html = roadmap_files.render(roadmap.from_state(state))
    expected_views["roadmap.md"] = roadmap_markdown
    expected_views["roadmap.html"] = roadmap_html
    expected_views[BOARD_MARKDOWN] = _render_board_markdown(view_inputs.board)
    expected_views[BOARD_HTML] = _render_board_html(view_inputs.board)
    return ExpectedViews(MappingProxyType(expected_views), tuple(damaged))
