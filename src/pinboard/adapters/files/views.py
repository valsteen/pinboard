"""Render and replace human-readable views from supplied authoritative facts.

Ordinary and rebuild callers supply exact projection facts and verified brief
content. Validation alone supplies complete state to derive every expected byte.
This adapter never reads generated views as authority.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pinboard.adapters.files.errors import FileIOError
from pinboard.adapters.files.file_io import atomic_replace, ensure_child_directory, remove_replaceable
from pinboard.adapters.files.models import ViewRefreshResult, ViewWarning
from pinboard.application import query_models, stored_state
from pinboard.application.queries import project_overview
from pinboard.domain.identifiers import AttemptId, ItemId

NOTICE = "Generated projection; SQLite is authoritative."


def _dependency_key(value: stored_state.ItemDependency) -> tuple[str, int]:
    return str(value.item_id), value.position


def _render_header(kind: str) -> str:
    return f"---\nkind: {kind}\nauthority: sqlite-v6\n---\n\n> {NOTICE}\n\n"


@dataclass(frozen=True, slots=True)
class _ViewInputs:
    overview: query_models.WorkOverview
    overview_items: dict[str, query_models.OverviewItem]
    dependencies: dict[ItemId, tuple[ItemId, ...]]
    definitions: dict[ItemId, stored_state.ItemDefinitionRevision]


def _project_view_inputs(state: stored_state.StoredWorkState, now: datetime) -> _ViewInputs:
    overview = project_overview(state, now)
    dependency_groups: dict[ItemId, list[ItemId]] = {item.item_id: [] for item in state.lifecycle.work_items}
    for dependency in sorted(state.lifecycle.dependencies, key=_dependency_key):
        dependency_groups[dependency.item_id].append(dependency.dependency_id)
    return _ViewInputs(
        overview,
        {item.item_id: item for item in overview.items},
        {item_id: tuple(dependencies) for item_id, dependencies in dependency_groups.items()},
        {definition.item_id: definition for definition in state.lifecycle.definition_revisions},
    )


def _render_item(
    item: stored_state.StoredWorkItem,
    dependencies: tuple[ItemId, ...],
    overview_item: query_models.OverviewItem | None,
    definition: stored_state.ItemDefinitionRevision,
) -> bytes:
    dependency_reasons = (
        tuple(f"{value.item_id}: {value.reason}" for value in overview_item.dependency_reasons)
        if overview_item is not None
        else ()
    )
    review_flags = (
        tuple(
            f"{value.kind.value}{f' ({value.related_item})' if value.related_item is not None else ''}: {value.reason}"
            for value in overview_item.review_flags
        )
        if overview_item is not None
        else ()
    )
    accepted = definition.definition
    replacement = None if overview_item is None else overview_item.planned_replacement
    return (
        _render_header("work-item-view")
        + f"# {accepted.title}\n\n"
        + f"- Item: {item.item_id}\n"
        + f"- State: {item.state.value}\n"
        + f"- Queue position: {item.queue_position if item.queue_position is not None else 'none'}\n"
        + f"- Source: {item.source if item.source is not None else 'none'}\n"
        + f"- Notes: {item.notes if item.notes is not None else 'none'}\n"
        + f"- Eligible: {'yes' if overview_item is not None and overview_item.eligible else 'no'}\n"
        + f"- Subject revision: {item.subject_revision}\n"
        + f"- Preparation: {overview_item.preparation.status.value if overview_item is not None and overview_item.preparation is not None else 'none'}\n"
        + f"- Dependencies: {', '.join(dependencies) if dependencies else 'none'}\n"
        + f"- Dependency reasons: {'; '.join(dependency_reasons) if dependency_reasons else 'none'}\n"
        + f"- Review flags: {'; '.join(review_flags) if review_flags else 'none'}\n"
        + f"- Planned replacement: {replacement.replacement_item_id if replacement is not None else 'none'}\n"
        + f"- Replacement revision: {replacement.relation_revision if replacement is not None else 'none'}\n"
        + f"- Replacement cost: {replacement.replacement_cost if replacement is not None else 'none'}\n"
        + f"- Temporarily retained: {'yes' if replacement is not None and replacement.temporarily_retained else 'no'}\n"
        + f"- Outcome evidence: {item.outcome_evidence or 'none'}\n"
        + "\n## Accepted definition\n\n"
        + f"- Revision: {definition.revision}\n"
        + f"- Digest: {definition.digest}\n"
        + f"- Objective: {accepted.objective}\n"
        + f"- Hypothesis: {accepted.hypothesis}\n"
        + f"- Evidence: {'; '.join(accepted.evidence) if accepted.evidence else 'none'}\n"
        + f"- Scope: {'; '.join(accepted.scope)}\n"
        + f"- Non-scope: {'; '.join(accepted.non_scope) if accepted.non_scope else 'none'}\n"
        + f"- Acceptance criteria: {'; '.join(accepted.acceptance_criteria)}\n"
        + f"- Effect: {accepted.effect}\n"
        + f"- Unlock: {accepted.unlock}\n"
    ).encode()


def _render_attempt(
    attempt: stored_state.StoredAttempt,
    attempt_briefs: Mapping[AttemptId, bytes],
) -> bytes:
    if (brief := attempt_briefs.get(attempt.attempt_id)) is not None:
        return brief
    return (
        _render_header("work-attempt-view")
        + f"# Attempt {attempt.attempt_id}\n\n"
        + f"- Item: {attempt.item_id}\n"
        + f"- State: {attempt.state.value}\n"
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


def refresh_facts(
    facts: query_models.GeneratedViewFacts,
    work_root: Path,
    attempt_briefs: Mapping[AttemptId, bytes],
) -> ViewRefreshResult:
    """Write only selectors named by exact post-commit projection facts."""

    try:
        _write_facts(facts, work_root, attempt_briefs)
    except FileIOError as error:
        return ViewRefreshResult(
            facts.project_revision,
            ViewWarning(
                f"The SQLite transition succeeded, but generated views need repair: {error}",
                "Run 'pinboard views rebuild'.",
            ),
        )
    return ViewRefreshResult(facts.project_revision, None)


def _write_facts(
    facts: query_models.GeneratedViewFacts,
    work_root: Path,
    attempt_briefs: Mapping[AttemptId, bytes],
) -> None:
    view_root = ensure_child_directory(work_root, "views")
    if facts.items:
        item_root = ensure_child_directory(view_root, "items")
        for selected in facts.items:
            item = selected.item
            atomic_replace(
                item_root / f"{item.item_id}.md",
                _render_item(item, selected.dependencies, selected.overview, selected.definition),
            )
    if facts.attempts:
        attempt_root = ensure_child_directory(view_root, "attempts")
        for selected in facts.attempts:
            attempt = selected.attempt
            atomic_replace(attempt_root / f"{attempt.attempt_id}.md", _render_attempt(attempt, attempt_briefs))
    if facts.receipts:
        history_root = ensure_child_directory(view_root, "history")
        for receipt in facts.receipts:
            atomic_replace(history_root / f"{receipt.history_id}.md", _render_history(receipt))


def rebuild_facts(
    facts: query_models.GeneratedViewFacts,
    work_root: Path,
    attempt_briefs: Mapping[AttemptId, bytes],
) -> ViewRefreshResult:
    """Reconcile every declared view from project-wide projection facts."""

    try:
        view_root = ensure_child_directory(work_root, "views")
        remove_replaceable(view_root / "queue.md")
        remove_replaceable(view_root / "history.md")
        _write_facts(facts, work_root, attempt_briefs)
    except FileIOError as error:
        return ViewRefreshResult(
            facts.project_revision,
            ViewWarning(
                f"Generated views could not be rebuilt: {error}",
                "Resolve the filesystem problem and run 'pinboard views rebuild' again.",
            ),
        )
    return ViewRefreshResult(facts.project_revision, None)


def derive_expected_view_bytes(
    state: stored_state.StoredWorkState,
    attempt_briefs: Mapping[AttemptId, bytes],
    *,
    now: datetime,
) -> dict[str, bytes]:
    """Return every generated selector and its canonical bytes for one SQLite snapshot."""

    view_inputs = _project_view_inputs(state, now)
    expected_views: dict[str, bytes] = {}
    expected_views.update(
        (
            f"items/{item.item_id}.md",
            _render_item(
                item,
                view_inputs.dependencies[item.item_id],
                view_inputs.overview_items.get(str(item.item_id)),
                view_inputs.definitions[item.item_id],
            ),
        )
        for item in state.lifecycle.work_items
    )
    expected_views.update(
        (f"attempts/{attempt.attempt_id}.md", _render_attempt(attempt, attempt_briefs))
        for attempt in state.lifecycle.attempts
    )
    expected_views.update(
        (f"history/{receipt.history_id}.md", _render_history(receipt)) for receipt in state.transition_receipts
    )
    return expected_views
