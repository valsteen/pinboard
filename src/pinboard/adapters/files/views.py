"""Render and replace human-readable views from supplied authoritative facts.

Ordinary and rebuild callers supply exact projection facts and verified brief
content, plus a live-portfolio reader for the two board projections, which are
read and replaced under one board lock. Validation alone supplies complete state
to derive every expected byte. This adapter never reads generated views as authority.
"""

import fcntl
import os
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Literal, assert_never

import msgspec

from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode
from pinboard.adapters.files.file_io import atomic_replace, ensure_child_directory, remove_replaceable
from pinboard.adapters.files.models import ViewRefreshResult, ViewWarning
from pinboard.application import ports, pr_reviews, query_models, stored_state
from pinboard.application.queries import (
    damaged_receipt_message,
    damaged_receipt_recovery,
    decode_recorded_pause_reason,
    project_live_portfolio,
    project_portfolio,
)
from pinboard.domain import work_models
from pinboard.domain.identifiers import AttemptId, WorkItemId

NOTICE = "Generated projection; SQLite is authoritative."


def _dependency_key(value: stored_state.ItemDependency) -> tuple[str, int]:
    return str(value.item_id), value.position


def _render_header(kind: str) -> str:
    return f"---\nkind: {kind}\nauthority: sqlite-v7\n---\n\n> {NOTICE}\n\n"


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
    portfolio: tuple[query_models.OverviewItem, ...]
    overview_items: Mapping[str, query_models.OverviewItem]
    dependencies: Mapping[WorkItemId, tuple[WorkItemId, ...]]
    definitions: Mapping[WorkItemId, stored_state.ItemDefinitionRevision]


def _project_view_inputs(state: stored_state.StoredWorkState, now: datetime) -> _ViewInputs:
    portfolio = project_portfolio(state, now)
    dependency_groups: dict[WorkItemId, list[WorkItemId]] = {item.item_id: [] for item in state.lifecycle.work_items}
    for dependency in sorted(state.lifecycle.dependencies, key=_dependency_key):
        dependency_groups[dependency.item_id].append(dependency.dependency_id)
    return _ViewInputs(
        portfolio,
        MappingProxyType({item.item_id: item for item in portfolio}),
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
    return (
        _render_header("work-item-view")
        + f"# {accepted.title}\n\n{accepted.objective}\n\n"
        + "## Current position\n\n"
        + f"- State: {overview_item.state.value if overview_item is not None else item.state.value}\n"
        + f"- Current attempt: {attempt or 'none'}\n"
        + ("" if pause_reason is None else f"- Pause reason: {pause_reason}\n")
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
        + "\n### Obligations\n\n"
        + _bullets(
            tuple(
                f"{value.obligation_id} ({_deferral_label(value.deferral_policy)}): {value.statement}"
                for value in accepted.obligations
            )
        )
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
        + f"- Checkout policy: {accepted.checkout_policy.value}\n"
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


def _board_prompt(item: query_models.OverviewItem) -> str:
    subject = f'item {item.item_id} ("{item.label}")'
    match item.state:
        case work_models.WorkState.READY:
            return (
                f"Use Pinboard to start work on {subject}: check its dependencies and holds, "
                "prepare the agreed brief, and ask me before starting."
            )
        case work_models.WorkState.ACTIVE:
            return (
                f"Use Pinboard to check the progress of the active attempt for {subject} "
                "and tell me whether it needs anything from me."
            )
        case work_models.WorkState.PAUSED:
            return f"Use Pinboard to show me why {subject} is paused and what decision it needs from me to resume."
        case work_models.WorkState.REVIEW:
            return (
                f"Use Pinboard to check the review of {subject}: commission the independent review "
                "if none is running, and bring me its verdict."
            )
        case work_models.WorkState.BLOCKED:
            return f"Use Pinboard to check what blocks {subject} and whether it can be unblocked."
        case work_models.WorkState.DEFERRED:
            return f"Use Pinboard to check whether deferred {subject} should resume now, and tell me what that takes."
        case _ as unreachable:
            assert_never(unreachable)


def _board_groups(
    items: tuple[query_models.OverviewItem, ...],
) -> tuple[tuple[_BoardGroup, tuple[query_models.OverviewItem, ...]], ...]:
    """Partition live items into display groups while keeping saved order within each."""

    return tuple((group, tuple(item for item in items if _board_group(item.state) is group)) for group in _BoardGroup)


def _markdown_link_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _render_board_markdown(items: tuple[query_models.OverviewItem, ...]) -> bytes:
    sections: list[str] = []
    for group, grouped in _board_groups(items):
        entries = "".join(
            f"- [{_markdown_link_text(item.label)}](items/{item.item_id}.md) `{item.item_id}` ({item.state.value})\n"
            + "".join(f"  - Depends on {value.item_id}: {value.reason}\n" for value in item.dependency_reasons)
            for item in grouped
        )
        sections.append(f"## {group.value}\n\n{entries or '- None.\n'}\n")
    return (
        _render_header("work-board-view")
        + "# Board\n\n"
        + "Live items in saved order, grouped by what they wait on. "
        + f"[{BOARD_HTML}]({BOARD_HTML}) adds filtering, item detail, and copyable action prompts.\n\n"
        + "".join(sections)
    ).encode()


class _BoardPageDependency(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    reason: str
    on_board: bool


class _BoardPageItem(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    title: str
    state: str
    group: str
    effect: str
    unlock: str
    attempt_id: str | None
    dependencies: tuple[_BoardPageDependency, ...]
    next_step: str
    prompt: str
    intake_next_action: str | None
    item_view: str


class _BoardPage(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-board-page/v1"]
    notice: str
    groups: tuple[str, ...]
    states: tuple[str, ...]
    items: tuple[_BoardPageItem, ...]


def _board_page_data(items: tuple[query_models.OverviewItem, ...]) -> bytes:
    """Encode page data as JSON that cannot close its script element or break a JavaScript string."""

    live_ids = frozenset(item.item_id for item in items)
    page = _BoardPage(
        "pinboard-board-page/v1",
        NOTICE,
        tuple(group.value for group in _BoardGroup),
        tuple(state.value for group in _BoardGroup for state in work_models.WorkState if _board_group(state) is group),
        tuple(
            _BoardPageItem(
                item.item_id,
                item.label,
                item.state.value,
                _board_group(item.state).value,
                item.effect,
                item.unlock,
                item.attempt_id,
                tuple(
                    _BoardPageDependency(value.item_id, value.reason, value.item_id in live_ids)
                    for value in item.dependency_reasons
                ),
                _board_next_step(item.state),
                _board_prompt(item),
                item.next_action,
                f"items/{item.item_id}.md",
            )
            for item in items
        ),
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
  --accent: #2f5fb3; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #171716; --fg: #ececea; --muted: #a2a29b; --line: #3a3a37; --card: #20201f; --accent: #8fb0ea; }
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
details.item > summary { cursor: pointer; padding: 8px 12px; display: flex; flex-wrap: wrap; gap: 4px 10px;
  align-items: baseline; }
.title { font-weight: 600; }
.state { font-size: 0.8rem; padding: 0 6px; border: 1px solid var(--line); border-radius: 10px; }
.id, .deps { color: var(--muted); font-size: 0.85rem; }
.body { padding: 0 12px 12px; border-top: 1px solid var(--line); overflow-wrap: anywhere; }
.body p { margin: 6px 0; }
.body ul { margin: 4px 0; padding-left: 20px; }
a { color: var(--accent); }
textarea { width: 100%; font: inherit; padding: 6px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--bg); color: var(--fg); resize: vertical; }
button { font: inherit; padding: 4px 10px; margin-top: 4px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--card); color: var(--fg); cursor: pointer; }
</style>
</head>
<body>
<header>
<h1>Pinboard board</h1>
<p class="notice">Generated projection; SQLite is authoritative. This page is read-only: copy a prompt and ask an
agent to act through Pinboard.</p>
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

  function node(tag, className, content) {
    var element = document.createElement(tag);
    if (className) { element.className = className; }
    if (content !== undefined && content !== null) { element.textContent = content; }
    return element;
  }

  function field(parent, label, value) {
    var row = node("p");
    row.appendChild(node("strong", null, label + ": "));
    row.appendChild(document.createTextNode(value));
    parent.appendChild(row);
  }

  function copy(button, area) {
    function selectText() {
      area.focus();
      area.select();
      button.textContent = "Clipboard unavailable: prompt selected, copy it manually";
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(area.value).then(function () { button.textContent = "Copied"; }, selectText);
    } else {
      selectText();
    }
  }

  function card(item) {
    var details = node("details", "item");
    details.id = "item-" + item.item_id;
    var summary = node("summary");
    summary.appendChild(node("span", "title", item.title));
    summary.appendChild(node("span", "state", item.state));
    summary.appendChild(node("code", "id", item.item_id));
    if (item.dependencies.length) {
      summary.appendChild(node("span", "deps", "Depends on " + item.dependencies.map(function (value) {
        return value.item_id;
      }).join(", ")));
    }
    details.appendChild(summary);
    var body = node("div", "body");
    field(body, "Next step", item.next_step);
    field(body, "Expected result", item.effect);
    field(body, "What this unlocks", item.unlock);
    if (item.attempt_id) { field(body, "Current attempt", item.attempt_id); }
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
          entry.appendChild(document.createTextNode(value.item_id + " (no longer live)"));
        }
        entry.appendChild(document.createTextNode(": " + value.reason));
        list.appendChild(entry);
      });
      body.appendChild(list);
    } else {
      body.appendChild(node("p", "empty", "None recorded."));
    }
    if (item.intake_next_action) {
      body.appendChild(node("h3", null, "Original intake context"));
      body.appendChild(node("p", "context", "Recorded when this work was saved; original context, not the current plan."));
      field(body, "Next action at intake", item.intake_next_action);
    }
    body.appendChild(node("h3", null, "Action prompt"));
    var area = node("textarea");
    area.readOnly = true;
    area.rows = 3;
    area.value = item.prompt;
    body.appendChild(area);
    var button = node("button", null, "Copy prompt");
    button.type = "button";
    button.addEventListener("click", function () { copy(button, area); });
    body.appendChild(button);
    var view = node("p");
    var viewLink = node("a", null, "Open the item view");
    viewLink.href = item.item_view;
    view.appendChild(viewLink);
    body.appendChild(view);
    details.appendChild(body);
    details.dataset.state = item.state;
    details.dataset.search = [item.item_id, item.title, item.state, item.effect, item.unlock, item.next_step]
      .concat(item.dependencies.map(function (value) { return value.item_id + " " + value.reason; }))
      .join(" ").toLowerCase();
    return details;
  }

  data.states.forEach(function (value) {
    var option = node("option", null, value);
    option.value = value;
    state.appendChild(option);
  });
  data.groups.forEach(function (group) {
    var section = node("section");
    section.appendChild(node("h2", null, group));
    var cards = data.items.filter(function (item) { return item.group === group; }).map(card);
    cards.forEach(function (element) { section.appendChild(element); });
    var empty = node("p", "empty", "None.");
    section.appendChild(empty);
    board.appendChild(section);
    sections.push({ element: section, cards: cards, empty: empty });
  });

  function applyFilter() {
    var query = text.value.trim().toLowerCase();
    var selected = state.value;
    var shown = 0;
    sections.forEach(function (section) {
      var visible = 0;
      section.cards.forEach(function (element) {
        var match = (!selected || element.dataset.state === selected) &&
          (!query || element.dataset.search.indexOf(query) !== -1);
        element.hidden = !match;
        if (match) { visible += 1; }
      });
      section.empty.hidden = visible > 0;
      shown += visible;
    });
    count.textContent = shown + " of " + data.items.length + " live items";
  }

  text.addEventListener("input", applyFilter);
  state.addEventListener("change", applyFilter);
  applyFilter();
}());
</script>
</body>
</html>
"""


def _render_board_html(items: tuple[query_models.OverviewItem, ...]) -> bytes:
    return _BOARD_PAGE_HEAD.encode() + _board_page_data(items) + _BOARD_PAGE_TAIL.encode()


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


def _write_board(view_root: Path, portfolio: ports.LivePortfolioReader, now: datetime) -> None:
    """Read the live portfolio and replace both board files under one board lock."""

    with _board_lock(view_root):
        items = project_live_portfolio(portfolio.read_live_portfolio(now), now)
        atomic_replace(view_root / BOARD_MARKDOWN, _render_board_markdown(items))
        atomic_replace(view_root / BOARD_HTML, _render_board_html(items))


def refresh_facts(
    facts: query_models.GeneratedViewFacts,
    work_root: Path,
    attempt_briefs: Mapping[AttemptId, bytes],
    portfolio: ports.LivePortfolioReader,
    now: datetime,
) -> ViewRefreshResult:
    """Write selectors named by exact post-commit projection facts, then both board projections."""

    try:
        damaged = _write_facts(facts, work_root, attempt_briefs)
        _write_board(ensure_child_directory(work_root, "views"), portfolio, now)
    except FileIOError as error:
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
            atomic_replace(attempt_root / f"{attempt.attempt_id}.md", _render_attempt(attempt, attempt_briefs))
    if facts.receipts:
        history_root = ensure_child_directory(view_root, "history")
        for receipt in facts.receipts:
            atomic_replace(history_root / f"{receipt.history_id}.md", _render_history(receipt))
    return tuple(damaged)


def rebuild_facts(
    facts: query_models.GeneratedViewFacts,
    work_root: Path,
    attempt_briefs: Mapping[AttemptId, bytes],
    portfolio: ports.LivePortfolioReader,
    now: datetime,
) -> ViewRefreshResult:
    """Reconcile every declared view from project-wide projection facts and the live portfolio."""

    try:
        view_root = ensure_child_directory(work_root, "views")
        remove_replaceable(view_root / "queue.md")
        remove_replaceable(view_root / "history.md")
        damaged = _write_facts(facts, work_root, attempt_briefs)
        _write_board(view_root, portfolio, now)
    except FileIOError as error:
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
    *,
    now: datetime,
) -> ExpectedViews:
    """Return every generated selector and its canonical bytes for one SQLite snapshot."""

    view_inputs = _project_view_inputs(state, now)
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
        (f"attempts/{attempt.attempt_id}.md", _render_attempt(attempt, attempt_briefs))
        for attempt in state.lifecycle.attempts
    )
    expected_views.update(
        (f"history/{receipt.history_id}.md", _render_history(receipt)) for receipt in state.transition_receipts
    )
    expected_views[BOARD_MARKDOWN] = _render_board_markdown(view_inputs.portfolio)
    expected_views[BOARD_HTML] = _render_board_html(view_inputs.portfolio)
    return ExpectedViews(MappingProxyType(expected_views), tuple(damaged))
