import contextlib
import fcntl
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.files import views as views_module
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode
from pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from pinboard.adapters.files.views import derive_expected_view_bytes, rebuild_facts, refresh_facts
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import ports, queries, query_models, stored_state
from pinboard.application.artifacts import NewArtifact
from pinboard.application.service import create_proposal
from pinboard.cli import work_views
from pinboard.cli.entrypoint import main
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import AttemptId, HostId, ProposalId, TaskId, WorkItemId
from pinboard.domain.proposal_models import CreateProposalOperation, ProposalIntake
from pinboard.mcp import execution as mcp_execution
from pinboard.mcp import read_operations as mcp_reads
from tests.artifact_support import write_revision
from tests.decision_support import BOARD
from tests.domain_support import expect_success
from tests.native_support import call_advertised_tool
from tests.support import (
    SQLITE_NOW,
    JsonObject,
    complete_sqlite_state,
    initialize_store,
    test_definition,
    with_definition_dependencies,
)

HOSTILE_TITLE = "</script><img src=x onerror=alert(1)> & <!-- -->"
HOSTILE_INTAKE = "</SCRIPT>\u2028<b>next</b>\u2029&amp;"
BOARD_FILES = ("board.md", "board.html")
LATER = SQLITE_NOW + timedelta(days=3)


def _item(
    item_id: WorkItemId,
    state: stored_state.StoredWorkItemState,
    position: int,
    *,
    source: str | None = None,
) -> stored_state.StoredWorkItem:
    return stored_state.StoredWorkItem(
        item_id,
        state,
        None,
        source,
        None,
        "activate",
        None,
        7,
        SQLITE_NOW,
        SQLITE_NOW,
        position,
    )


def _definition(
    item_id: WorkItemId, dependencies: tuple[WorkItemId, ...], title: str | None = None
) -> stored_state.ItemDefinitionRevision:
    definition, _digest = test_definition(item_id)
    definition = replace(definition, dependencies=dependencies, title=definition.title if title is None else title)
    digest = work_item_definition_digest(definition)
    assert isinstance(digest, str)
    return stored_state.ItemDefinitionRevision(
        item_id, 1, digest, definition, "Accepted test definition.", TaskId("test-source"), None, digest, 3, SQLITE_NOW
    )


def _every_state() -> stored_state.StoredWorkState:
    """Live items in every state, a terminal item, retained receipts, and both proposal-derived reasons."""

    state = complete_sqlite_state()
    paused = WorkItemId("paused-work")
    review = WorkItemId("review-work")
    blocked = WorkItemId("blocked-work")
    deferred = WorkItemId("deferred-work")
    needed = WorkItemId("needed-first")
    paused_definition = _definition(paused, ())
    review_definition = _definition(review, ())
    lifecycle = state.lifecycle
    proposal = stored_state.StoredProposal(
        ProposalId(needed),
        SQLITE_NOW,
        SQLITE_NOW,
        TaskId("source-task"),
        "Needed first",
        "Blocked work needs it.",
        "Blocked work cannot start without it.",
        work_models.PrerequisiteProposalRelation(blocked),
        "Record the prerequisite.",
        "Blocked work can start.",
        "Required before blocked work.",
        None,
        4,
    )
    return replace(
        state,
        lifecycle=replace(
            lifecycle,
            work_items=(
                *lifecycle.work_items,
                _item(review, stored_state.StoredWorkItemState.REVIEW, 5),
                _item(paused, stored_state.StoredWorkItemState.PAUSED, 6),
                _item(needed, stored_state.StoredWorkItemState.READY, 7, source="proposal:needed-first"),
                _item(blocked, stored_state.StoredWorkItemState.BLOCKED, 8),
                _item(deferred, stored_state.StoredWorkItemState.DEFERRED, 9),
            ),
            dependencies=(*lifecycle.dependencies, stored_state.ItemDependency(blocked, needed, 0)),
            attempts=(
                *lifecycle.attempts,
                replace(
                    lifecycle.attempts[0],
                    attempt_id=AttemptId("paused-work-1"),
                    item_id=paused,
                    state=work_models.AttemptState.PAUSED,
                    branch="codex/paused-work",
                    accepted_scope_digest=paused_definition.digest,
                ),
                replace(
                    lifecycle.attempts[0],
                    attempt_id=AttemptId("review-work-1"),
                    item_id=review,
                    state=work_models.AttemptState.REVIEW,
                    branch="codex/review-work",
                    candidate_revision="candidate-review",
                    candidate_recorded_at=SQLITE_NOW,
                    accepted_scope_digest=review_definition.digest,
                ),
            ),
            definition_revisions=(
                *lifecycle.definition_revisions,
                review_definition,
                paused_definition,
                _definition(needed, ()),
                _definition(blocked, (needed,)),
                _definition(deferred, ()),
            ),
        ),
        proposals=replace(
            state.proposals,
            proposals=(*state.proposals.proposals, proposal),
            evidence=(*state.proposals.evidence, stored_state.ProposalEvidence(ProposalId(needed), 0, "source:local")),
            freshness=(
                *state.proposals.freshness,
                stored_state.ProposalFreshness(ProposalId(needed), 0, "Blocked work remains live."),
            ),
        ),
    )


def _with_hostile_text(state: stored_state.StoredWorkState, item_id: WorkItemId) -> stored_state.StoredWorkState:
    revisions = tuple(
        _definition(item_id, value.definition.dependencies, HOSTILE_TITLE) if value.item_id == item_id else value
        for value in state.lifecycle.definition_revisions
    )
    items = tuple(
        replace(value, next_action=HOSTILE_INTAKE) if value.item_id == item_id else value
        for value in state.lifecycle.work_items
    )
    return replace(state, lifecycle=replace(state.lifecycle, definition_revisions=revisions, work_items=items))


def _prerequisite_intake() -> ProposalIntake:
    return ProposalIntake(
        ProposalId("required-before-work-c"),
        SQLITE_NOW,
        TaskId("discovering-task"),
        "Required before Work C",
        "Work C needs one newly discovered prerequisite.",
        "The dependency must be preserved before activation.",
        "Record the prerequisite and relationship.",
        "A task can evaluate it.",
        work_models.PrerequisiteProposalRelation(WorkItemId("work-c")),
        "The relationship is current.",
        ("source:local",),
        ("Work C remains ready.",),
        work_models.CheckoutPolicy.COORDINATOR_SELECTED,
        (
            work_models.WorkObligation(
                work_models.ObligationId("proposal-outcome"),
                "A task can evaluate it.",
                work_models.ObligationDeferralPolicy.FORBIDDEN,
            ),
        ),
    )


def _validatable_state() -> stored_state.StoredWorkState:
    """Live items without attempts, receipts, or artifact references, so full validation can check the ledger."""

    state = complete_sqlite_state()
    return replace(
        state,
        lifecycle=replace(
            state.lifecycle,
            work_items=tuple(
                replace(value, state=stored_state.StoredWorkItemState.READY)
                if value.item_id == WorkItemId("work-a")
                else value
                for value in state.lifecycle.work_items
            ),
            attempts=(),
        ),
        artifact_references=(),
        authority=replace(state.authority, attempt_counters=(), attempt_generations=(), attempt_leases=()),
        transition_receipts=(),
    )


def _with_solved_dependencies() -> stored_state.StoredWorkState:
    """Give the deferred item solved and live dependencies interleaved, and the review item only a solved one."""

    state = with_definition_dependencies(
        _every_state(),
        WorkItemId("deferred-work"),
        (WorkItemId("work-b"), WorkItemId("work-c"), WorkItemId("needed-first")),
    )
    return with_definition_dependencies(state, WorkItemId("review-work"), (WorkItemId("work-b"),))


def _page_data(html: str) -> dict[str, object]:
    match = re.search(r'<script id="board-data" type="application/json">(.*?)</script>', html, re.DOTALL)
    assert match is not None
    decoded = json.loads(match.group(1))
    assert isinstance(decoded, dict)
    return decoded


def _page_items(html: str) -> list[dict[str, object]]:
    items = _page_data(html)["items"]
    assert isinstance(items, list)
    return [value for value in items if isinstance(value, dict)]


def _page_dependencies(item: dict[str, object]) -> list[tuple[object, object]]:
    dependencies = item["dependencies"]
    assert isinstance(dependencies, list)
    return [(value["item_id"], value["on_board"]) for value in dependencies if isinstance(value, dict)]


def _markdown_groups(markdown: str) -> dict[str, list[tuple[str, str]]]:
    groups: dict[str, list[tuple[str, str]]] = {}
    for section in markdown.split("\n## ")[1:]:
        heading, _, body = section.partition("\n")
        groups[heading] = re.findall(
            r"^- \[.*\]\(items/.*\.md\) `([^`]+)` \((\w+)\), changed \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC$",
            body,
            re.MULTILINE,
        )
    return groups


def _with_change_times(state: stored_state.StoredWorkState) -> tuple[stored_state.StoredWorkState, dict[str, datetime]]:
    """Give every item, including terminal ones, a distinct change time that the project record never matches."""

    times = {
        str(value.item_id): SQLITE_NOW + timedelta(hours=index, seconds=index)
        for index, value in enumerate(state.lifecycle.work_items, start=1)
    }
    items = tuple(replace(value, updated_at=times[str(value.item_id)]) for value in state.lifecycle.work_items)
    project = replace(state.lifecycle.project, updated_at=SQLITE_NOW + timedelta(days=30))
    return replace(state, lifecycle=replace(state.lifecycle, work_items=items, project=project)), times


def _utc_label(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _utc_instant(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_identity(path: Path) -> tuple[bytes, int, int]:
    return path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns


class _GatedPortfolio:
    """Delegate live-portfolio reads while letting a test hold a refresh between its read and its writes."""

    def __init__(self, store: SQLiteWorkStore, read: threading.Event, release: threading.Event) -> None:
        self.store = store
        self.read = read
        self.release = release

    def read_live_portfolio(self, now: datetime) -> query_models.LivePortfolioFacts:
        facts = self.store.read_live_portfolio(now)
        self.read.set()
        if not self.release.wait(10):
            raise AssertionError("The held refresh was never released.")
        return facts


class BoardProjectionTest(unittest.TestCase):
    def _ledger(self, state: stored_state.StoredWorkState) -> tuple[Path, DurableRoots, SQLiteWorkStore]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        project = Path(temporary.name).resolve()
        subprocess.run(("git", "init", "--quiet", str(project)), check=True)
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, state)
        return project, roots, store

    def _rebuild(self, roots: DurableRoots, store: SQLiteWorkStore, now: datetime = SQLITE_NOW) -> None:
        result = rebuild_facts(store.read_all_generated_view_facts(now), roots.work_root, {}, store, now)
        self.assertIsNone(result.warning)

    def _board(self, roots: DurableRoots) -> tuple[str, str]:
        view_root = roots.work_root / "views"
        return (
            (view_root / "board.md").read_text(encoding="utf-8"),
            (view_root / "board.html").read_text(encoding="utf-8"),
        )

    def _run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_markdown_groups_live_items_in_saved_order_with_reasons_and_resolving_links(self) -> None:
        _project, roots, store = self._ledger(_every_state())

        self._rebuild(roots, store)

        markdown, html = self._board(roots)
        self.assertIn(views_module.NOTICE, markdown)
        self.assertIn(views_module.NOTICE, html)
        self.assertEqual(
            {
                "Paused or in review": [("review-work", "review"), ("paused-work", "paused")],
                "In progress": [("work-a", "active")],
                "Ready": [
                    ("intake-work", "ready"),
                    ("work-c", "ready"),
                    ("zz-proposal-a", "ready"),
                    ("needed-first", "ready"),
                ],
                "Blocked or deferred": [("blocked-work", "blocked"), ("deferred-work", "deferred")],
            },
            _markdown_groups(markdown),
        )
        self.assertIn("  - Depends on work-c: Recorded dependency.\n", markdown)
        self.assertIn("  - Depends on work-c: Follow-up to work-c: It may affect work C.\n", markdown)
        self.assertIn(
            "  - Depends on needed-first: Inferred prerequisite needed-first: Blocked work cannot start without it.\n",
            markdown,
        )
        links = re.findall(r"\]\((items/[^)]+)\)", markdown)
        self.assertEqual(9, len(links))
        for link in links:
            self.assertTrue((roots.work_root / "views" / link).is_file(), link)

    def test_board_derives_only_live_items_and_is_independent_of_operation_time(self) -> None:
        _project, roots, store = self._ledger(_every_state())
        self._rebuild(roots, store, SQLITE_NOW)
        first = self._board(roots)

        self._rebuild(roots, store, LATER)

        self.assertEqual(first, self._board(roots))
        markdown, html = first
        self.assertNotIn("`work-b`", markdown)
        self.assertNotIn("items/work-b.md", markdown)
        self.assertNotIn('"work-b"', html)
        self.assertEqual(
            [
                "intake-work",
                "work-a",
                "work-c",
                "zz-proposal-a",
                "review-work",
                "paused-work",
                "needed-first",
                "blocked-work",
                "deferred-work",
            ],
            [value["item_id"] for value in _page_items(html)],
        )
        state = store.validated_snapshot()
        self.assertEqual(
            {name: derive_expected_view_bytes(state, {}, now=SQLITE_NOW).views[name] for name in BOARD_FILES},
            {name: derive_expected_view_bytes(state, {}, now=LATER).views[name] for name in BOARD_FILES},
        )

    def test_both_projections_show_item_times_and_the_latest_live_item_time_as_the_board_time(self) -> None:
        state, times = _with_change_times(_every_state())
        times["work-b"] = SQLITE_NOW + timedelta(days=60)
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=tuple(
                    replace(value, updated_at=times[str(value.item_id)]) for value in state.lifecycle.work_items
                ),
            ),
        )
        _project, roots, store = self._ledger(state)

        self._rebuild(roots, store)

        markdown, html = self._board(roots)
        live = {value for groups in _markdown_groups(markdown).values() for value, _state in groups}
        self.assertEqual(9, len(live))
        self.assertNotIn("work-b", live)
        latest = max(times[value] for value in live)
        self.assertLess(latest, times["work-b"])
        self.assertLess(latest, state.lifecycle.project.updated_at)
        self.assertIn(f"Updated {_utc_label(latest)}", markdown)
        self.assertNotIn(_utc_label(times["work-b"]), markdown)
        self.assertNotIn(_utc_label(state.lifecycle.project.updated_at), markdown)
        lines = {
            match.group(1): match.group(2)
            for match in re.finditer(r"^- \[.*\]\(items/.*\.md\) `([^`]+)` .*, changed (.*)$", markdown, re.MULTILINE)
        }
        self.assertEqual(live, set(lines))
        for item_id in live:
            self.assertEqual(_utc_label(times[item_id]), lines[item_id])
        self.assertEqual(_utc_instant(latest), _page_data(html)["updated_at"])
        items = _page_items(html)
        self.assertEqual(live, {str(value["item_id"]) for value in items})
        for value in items:
            self.assertEqual(_utc_instant(times[str(value["item_id"])]), value["updated_at"])

    def test_a_board_without_live_items_shows_no_board_time(self) -> None:
        state = _validatable_state()
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=tuple(
                    replace(
                        value,
                        state=stored_state.StoredWorkItemState.SUPERSEDED,
                        outcome_evidence="superseded for the test",
                        queue_position=None,
                    )
                    for value in state.lifecycle.work_items
                ),
            ),
        )
        _project, roots, store = self._ledger(state)

        self._rebuild(roots, store)

        markdown, html = self._board(roots)
        self.assertNotIn("Updated ", markdown)
        self.assertNotRegex(markdown, r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC")
        self.assertIsNone(_page_data(html)["updated_at"])
        self.assertEqual([], _page_items(html))

    def test_artifact_only_commit_leaves_derived_board_bytes_and_validation_unchanged(self) -> None:
        project, roots, store = self._ledger(_validatable_state())
        common = ("--project-root", str(project), "--work-root", str(roots.work_root))
        self._rebuild(roots, store)
        view_root = roots.work_root / "views"
        before = {name: (view_root / name).read_bytes() for name in BOARD_FILES}
        published = write_revision(
            roots, NewArtifact(work_models.ArtifactKind.EVIDENCE, "later-evidence", 1, ".md", b"later\n")
        )

        expect_success(store.accept_artifact_reference(roots.work_root, published, LATER))

        reloaded = store.validated_snapshot()
        self.assertEqual(LATER, reloaded.lifecycle.project.updated_at)
        expected = derive_expected_view_bytes(reloaded, {}, now=LATER).views
        self.assertEqual(before, {name: expected[name] for name in BOARD_FILES})
        self.assertEqual(before, {name: (view_root / name).read_bytes() for name in BOARD_FILES})
        result, stdout, stderr = self._run_cli(*common, "validate")
        self.assertEqual(0, result, stderr)
        self.assertNotIn("VIEW_REFRESH_REQUIRED", stdout)

    def test_markdown_lists_only_dependencies_that_still_apply(self) -> None:
        _project, roots, store = self._ledger(_with_solved_dependencies())

        self._rebuild(roots, store)

        markdown, _html = self._board(roots)
        entries = {
            match.group(1): match.group(0)
            for match in re.finditer(r"^- \[.*\]\(items/.*\.md\) `([^`]+)`.*\n(?:  - .*\n)*", markdown, re.MULTILINE)
        }
        self.assertIn("  - Depends on work-c: Recorded dependency.\n", entries["deferred-work"])
        self.assertIn("  - Depends on needed-first: ", entries["deferred-work"])
        self.assertNotIn("work-b", entries["deferred-work"])
        self.assertNotIn("Depends on", entries["review-work"])
        self.assertNotIn("`work-b`", markdown)
        self.assertNotIn("Depends on work-b", markdown)

    def test_page_data_orders_dependencies_that_still_apply_before_solved_ones(self) -> None:
        _project, roots, store = self._ledger(_with_solved_dependencies())

        self._rebuild(roots, store)

        _markdown, html = self._board(roots)
        items = {str(value["item_id"]): value for value in _page_items(html)}
        self.assertEqual(
            [("work-c", True), ("needed-first", True), ("work-b", False)],
            _page_dependencies(items["deferred-work"]),
        )
        self.assertEqual(
            [("work-b", False)],
            _page_dependencies(items["review-work"]),
        )

    def test_board_bytes_do_not_depend_on_the_writing_process_timezone(self) -> None:
        state, _times = _with_change_times(_every_state())
        _project, roots, store = self._ledger(state)
        view_root = roots.work_root / "views"
        outputs: list[dict[str, bytes]] = []

        def restore_timezone(previous: str | None) -> None:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()

        self.addCleanup(restore_timezone, os.environ.get("TZ"))
        for zone in ("Pacific/Kiritimati", "America/Los_Angeles"):
            os.environ["TZ"] = zone
            time.tzset()
            self._rebuild(roots, store)
            outputs.append({name: (view_root / name).read_bytes() for name in BOARD_FILES})
            expected = derive_expected_view_bytes(store.validated_snapshot(), {}, now=SQLITE_NOW).views
            self.assertEqual(outputs[-1], {name: expected[name] for name in BOARD_FILES})

        self.assertEqual(outputs[0], outputs[1])

    def test_committed_reorder_changes_neither_an_items_time_nor_the_board_time(self) -> None:
        project, roots, store = self._ledger(complete_sqlite_state())
        self._rebuild(roots, store)
        before = {str(value["item_id"]): value["updated_at"] for value in _page_items(self._board(roots)[1])}
        before_board = _page_data(self._board(roots)[1])["updated_at"]
        current = ["intake-work", "work-a", "work-c", "zz-proposal-a"]

        result = mcp_reads._order(
            {
                "request": {
                    "project_root": str(project),
                    "work_root": str(roots.work_root),
                    "actor_task_id": "priority-owner",
                    "actor_host_id": "local",
                    "order": {
                        "schema": "pinboard-live-order/v1",
                        "expected_order": list(current),
                        "requested_order": [*current[1:], current[0]],
                    },
                }
            },
            mcp_execution.CancellationToken(),
        ).content

        self.assertEqual("committed", result["status"])
        _markdown, html = self._board(roots)
        self.assertEqual(before, {str(value["item_id"]): value["updated_at"] for value in _page_items(html)})
        self.assertEqual(before_board, _page_data(html)["updated_at"])

    def test_live_portfolio_read_excludes_history_and_artifact_relations(self) -> None:
        _project, roots, store = self._ledger(_every_state())
        tables: set[str] = set()
        original_open = sqlite_store.open_database

        def recording_open(path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open(path, mode)

            def authorize(
                action: int, argument: str | None, _second: str | None, _database: str | None, _trigger: str | None
            ) -> int:
                if action == sqlite3.SQLITE_READ and argument is not None:
                    tables.add(argument)
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            return connection

        with patch.object(sqlite_store, "open_database", recording_open):
            refreshed = refresh_facts(
                query_models.GeneratedViewFacts(12, (), (), ()), roots.work_root, {}, store, SQLITE_NOW
            )

        self.assertIsNone(refreshed.warning)
        self.assertTrue({"work_items", "item_dependencies", "attempts"} <= tables, tables)
        self.assertFalse({"transition_history", "artifact_references", "preparation_leases"} & tables, tables)

    def test_refresh_rebuild_and_validate_derive_identical_board_bytes(self) -> None:
        _project, roots, store = self._ledger(_every_state())
        view_root = roots.work_root / "views"
        self._rebuild(roots, store)
        rebuilt = {name: (view_root / name).read_bytes() for name in BOARD_FILES}
        for name in BOARD_FILES:
            (view_root / name).unlink()

        refreshed = refresh_facts(
            store.read_generated_view_facts((), (), (), SQLITE_NOW), roots.work_root, {}, store, SQLITE_NOW
        )

        self.assertIsNone(refreshed.warning)
        self.assertEqual(rebuilt, {name: (view_root / name).read_bytes() for name in BOARD_FILES})
        expected = derive_expected_view_bytes(store.validated_snapshot(), {}, now=SQLITE_NOW).views
        self.assertEqual(rebuilt, {name: expected[name] for name in BOARD_FILES})

    def test_unchanged_refresh_preserves_board_bytes_inode_and_modification_time(self) -> None:
        _project, roots, store = self._ledger(complete_sqlite_state())
        self._rebuild(roots, store)
        paths = tuple(roots.work_root / "views" / name for name in BOARD_FILES)
        before = {path: _file_identity(path) for path in paths}

        refreshed = refresh_facts(store.read_generated_view_facts((), (), (), LATER), roots.work_root, {}, store, LATER)

        self.assertIsNone(refreshed.warning)
        self.assertEqual(before, {path: _file_identity(path) for path in paths})

    def test_hostile_item_text_stays_inert_and_round_trips(self) -> None:
        state = _with_hostile_text(complete_sqlite_state(), WorkItemId("work-c"))
        _project, roots, store = self._ledger(state)

        self._rebuild(roots, store)

        _markdown, html = self._board(roots)
        for raw in ("</script><img", "<img src=x", "<!-- -->", "</SCRIPT>", "<b>next", "\u2028", "\u2029", "&amp;"):
            self.assertNotIn(raw, html)
        self.assertEqual(2, html.lower().count("</script"))
        work_c = next(value for value in _page_items(html) if value["item_id"] == "work-c")
        self.assertEqual(HOSTILE_TITLE, work_c["title"])
        self.assertEqual(HOSTILE_INTAKE, work_c["intake_next_action"])
        script = html.rpartition("<script>")[2]
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("insertAdjacentHTML", script)
        self.assertNotIn("document.write", script)

    def test_board_page_is_self_contained_and_offline(self) -> None:
        _project, roots, store = self._ledger(_every_state())

        self._rebuild(roots, store)

        _markdown, html = self._board(roots)
        self.assertNotRegex(html, r"https?://")
        self.assertNotRegex(html, r"<script\b[^>]*\bsrc\s*=")
        self.assertNotRegex(html, r"<link\b")
        self.assertNotRegex(html, r"\bimport\b")
        self.assertNotIn("url(", html)

    def test_page_data_carries_state_chosen_detail_without_action_prompts(self) -> None:
        _project, roots, store = self._ledger(_every_state())

        self._rebuild(roots, store)

        markdown, html = self._board(roots)
        data = _page_data(html)
        states = data["states"]
        assert isinstance(states, list)
        self.assertEqual({state.value for state in work_models.WorkState}, set(states))
        items = {str(value["item_id"]): value for value in _page_items(html)}
        for item_id, value in items.items():
            self.assertNotIn("prompt", value)
            self.assertTrue(value["next_step"])
            self.assertEqual(f"items/{item_id}.md", value["item_view"])
            self.assertTrue(value["effect"] and value["unlock"])
        self.assertNotIn("prompt", markdown.lower())
        self.assertNotIn("<textarea", html)
        self.assertNotIn("clipboard", html.lower())
        self.assertEqual("work-a-1", items["work-a"]["attempt_id"])
        self.assertEqual("continue", items["work-a"]["intake_next_action"])
        self.assertEqual(
            [
                {
                    "item_id": "needed-first",
                    "reason": "Inferred prerequisite needed-first: Blocked work cannot start without it.",
                    "on_board": True,
                }
            ],
            items["blocked-work"]["dependencies"],
        )
        self.assertIn("Original intake context", html)

    def test_mcp_committed_order_refreshes_the_board(self) -> None:
        project, roots, _store = self._ledger(complete_sqlite_state())
        current = ["intake-work", "work-a", "work-c", "zz-proposal-a"]
        requested = [*current[1:], current[0]]

        result = mcp_reads._order(
            {
                "request": {
                    "project_root": str(project),
                    "work_root": str(roots.work_root),
                    "actor_task_id": "priority-owner",
                    "actor_host_id": "local",
                    "order": {
                        "schema": "pinboard-live-order/v1",
                        "expected_order": list(current),
                        "requested_order": list(requested),
                    },
                }
            },
            mcp_execution.CancellationToken(),
        ).content

        self.assertEqual("committed", result["status"])
        markdown, html = self._board(roots)
        self.assertEqual(
            [("work-c", "ready"), ("zz-proposal-a", "ready"), ("intake-work", "ready")],
            _markdown_groups(markdown)["Ready"],
        )
        self.assertEqual(requested, [value["item_id"] for value in _page_items(html)])

    def test_board_write_failure_is_a_view_warning_with_the_commit_intact(self) -> None:
        project, roots, store = self._ledger(complete_sqlite_state())
        current = ["intake-work", "work-a", "work-c", "zz-proposal-a"]
        requested = [*current[1:], current[0]]
        original_open = os.open

        def refuse_board_lock(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
            if Path(path).name == "board.lock":
                raise PermissionError("board lock denied")
            return original_open(path, flags, mode)

        with patch.object(views_module.os, "open", refuse_board_lock):
            result = mcp_reads._order(
                {
                    "request": {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "actor_task_id": "priority-owner",
                        "actor_host_id": "local",
                        "order": {
                            "schema": "pinboard-live-order/v1",
                            "expected_order": list(current),
                            "requested_order": list(requested),
                        },
                    }
                },
                mcp_execution.CancellationToken(),
            ).content

        self.assertEqual("committed-with-warning", result["status"])
        warning = result["warning"]
        assert isinstance(warning, dict)
        self.assertIn("generated views need repair", str(warning["message"]))
        self.assertIn("views rebuild", str(warning["recovery"]))
        self.assertEqual(
            requested,
            [
                value.item_id
                for value in queries.project_current_overview(
                    store.read_project_overview(SQLITE_NOW), SQLITE_NOW, BOARD
                ).items
            ],
        )
        with patch(
            "pinboard.adapters.files.views.atomic_replace",
            side_effect=FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, "disk full"),
        ):
            refreshed = refresh_facts(
                store.read_generated_view_facts((), (), (), SQLITE_NOW), roots.work_root, {}, store, SQLITE_NOW
            )
        self.assertIsNotNone(refreshed.warning)
        with patch.object(views_module.fcntl, "flock", side_effect=OSError("no locks available")):
            unlocked = rebuild_facts(
                store.read_all_generated_view_facts(SQLITE_NOW), roots.work_root, {}, store, SQLITE_NOW
            )
        assert unlocked.warning is not None
        self.assertIn("Board lock could not be acquired", unlocked.warning.message)

    def test_cli_close_and_committed_proposal_refresh_the_board(self) -> None:
        project, roots, store = self._ledger(complete_sqlite_state())
        self._rebuild(roots, store)

        result, _stdout, stderr = self._run_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(roots.work_root),
            "close",
            "intake-work",
            "--outcome",
            "done",
            "--reason",
            "The accepted intake is complete.",
            "--task-id",
            "project-task",
            "--host-id",
            "studio",
            "--json",
        )

        self.assertEqual(0, result, stderr)
        markdown, html = self._board(roots)
        self.assertNotIn("intake-work", markdown)
        self.assertNotIn("intake-work", html)
        committed = create_proposal(
            store,
            CreateProposalOperation(_prerequisite_intake()),
            SQLITE_NOW,
            actor_task_id=TaskId("discovering-task"),
            actor_host_id=HostId("host-a"),
        )
        assert not isinstance(committed, DecisionFailure)

        refreshed = work_views.refresh_effect(roots, store, committed, SQLITE_NOW)

        self.assertIsNone(refreshed.warning)
        markdown, html = self._board(roots)
        self.assertIn("`required-before-work-c` (ready)", markdown)
        self.assertIn(
            "  - Depends on required-before-work-c: Inferred prerequisite required-before-work-c: "
            "The dependency must be preserved before activation.\n",
            markdown,
        )
        self.assertIn("required-before-work-c", [value["item_id"] for value in _page_items(html)])

    def test_rebuild_writes_both_files_and_validate_reports_board_drift(self) -> None:
        project, roots, _store = self._ledger(_validatable_state())
        common = ("--project-root", str(project), "--work-root", str(roots.work_root))
        view_root = roots.work_root / "views"

        rebuilt, _stdout, rebuild_error = self._run_cli(*common, "views", "rebuild")

        self.assertEqual(0, rebuilt, rebuild_error)
        self.assertTrue(all((view_root / name).is_file() for name in BOARD_FILES))
        self.assertEqual((0, "OK WORK_STATE_VALID\n"), self._run_cli(*common, "validate")[:2])
        (view_root / "board.md").unlink()
        (view_root / "board.html").write_text("edited\n", encoding="utf-8")

        result, stdout, stderr = self._run_cli(*common, "validate")

        self.assertEqual(0, result, stderr)
        drift = [line for line in stdout.splitlines() if "VIEW_REFRESH_REQUIRED" in line]
        self.assertEqual(2, len(drift), stdout)
        self.assertTrue(any("board.md" in line for line in drift))
        self.assertTrue(any("board.html" in line for line in drift))
        self.assertEqual(0, self._run_cli(*common, "views", "rebuild")[0])
        self.assertEqual((0, "OK WORK_STATE_VALID\n"), self._run_cli(*common, "validate")[:2])

    def test_overlapping_refreshes_leave_the_board_on_the_later_commit(self) -> None:
        _project, roots, store = self._ledger(complete_sqlite_state())
        self._rebuild(roots, store)
        earlier_read = threading.Event()
        release_earlier = threading.Event()
        # Set when the later refresh either waits for the board lock or, without one, has already written.
        later_progress = threading.Event()
        failures: list[BaseException] = []
        real_flock = fcntl.flock

        def refresh(portfolio: ports.LivePortfolioReader) -> None:
            try:
                result = refresh_facts(
                    store.read_generated_view_facts((), (), (), SQLITE_NOW), roots.work_root, {}, portfolio, SQLITE_NOW
                )
                if result.warning is not None:
                    raise AssertionError(result.warning.message)
            except BaseException as error:  # Report thread failures to the test thread.
                failures.append(error)
            finally:
                if threading.current_thread() is later_thread:
                    later_progress.set()

        earlier_thread = threading.Thread(target=refresh, args=(_GatedPortfolio(store, earlier_read, release_earlier),))
        later_thread = threading.Thread(target=refresh, args=(store,))

        def observed_flock(descriptor: int, operation: int) -> None:
            if threading.current_thread() is later_thread:
                later_progress.set()
            real_flock(descriptor, operation)

        with patch.object(views_module.fcntl, "flock", observed_flock):
            earlier_thread.start()
            self.assertTrue(earlier_read.wait(10))
            committed = create_proposal(
                store,
                CreateProposalOperation(_prerequisite_intake()),
                SQLITE_NOW,
                actor_task_id=TaskId("discovering-task"),
                actor_host_id=HostId("host-a"),
            )
            assert not isinstance(committed, DecisionFailure)
            later_thread.start()
            self.assertTrue(later_progress.wait(10))
            release_earlier.set()
            earlier_thread.join(10)
            later_thread.join(10)

        self.assertFalse(earlier_thread.is_alive() or later_thread.is_alive())
        self.assertEqual([], failures)
        markdown, html = self._board(roots)
        self.assertIn("required-before-work-c", markdown)
        self.assertIn("required-before-work-c", [value["item_id"] for value in _page_items(html)])
        expected = derive_expected_view_bytes(store.validated_snapshot(), {}, now=SQLITE_NOW).views
        self.assertEqual(expected["board.md"], markdown.encode())

    def test_lock_left_by_an_exited_refresher_does_not_block_a_later_refresh(self) -> None:
        _project, roots, store = self._ledger(complete_sqlite_state())
        self._rebuild(roots, store)
        lock_path = roots.work_root / "views" / "board.lock"
        holder = subprocess.Popen(
            (
                sys.executable,
                "-c",
                "import fcntl, os, sys\n"
                "descriptor = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
                "fcntl.flock(descriptor, fcntl.LOCK_EX)\n"
                "print('locked', flush=True)\n"
                "sys.stdin.read()\n",
                str(lock_path),
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(holder.wait)
        assert holder.stdout is not None
        self.assertEqual("locked\n", holder.stdout.readline())
        probe = os.open(lock_path, os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
        (roots.work_root / "views" / "board.md").unlink()

        holder.kill()
        holder.wait()
        holder.stdout.close()
        if holder.stdin is not None:
            holder.stdin.close()
        refreshed = refresh_facts(
            store.read_generated_view_facts((), (), (), SQLITE_NOW), roots.work_root, {}, store, SQLITE_NOW
        )

        self.assertIsNone(refreshed.warning)
        self.assertTrue((roots.work_root / "views" / "board.md").is_file())

    def test_initialization_writes_both_board_files(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        project = Path(temporary.name).resolve()
        subprocess.run(("git", "init", "--quiet", str(project)), check=True)

        result, _stdout, stderr = self._run_cli("--project-root", str(project), "init", "--json")

        self.assertEqual(0, result, stderr)
        markdown = (project / ".pinboard" / "views" / "board.md").read_text(encoding="utf-8")
        self.assertIn("## Ready\n\n- None.\n", markdown)
        self.assertEqual([], _page_items((project / ".pinboard" / "views" / "board.html").read_text(encoding="utf-8")))


class BoardPagePathTest(unittest.TestCase):
    def _git(self, repository: Path, *arguments: str) -> None:
        subprocess.run(
            ("git", "-C", str(repository), "-c", "user.name=Test", "-c", "user.email=test@example.com", *arguments),
            check=True,
            capture_output=True,
        )

    def test_overview_and_item_status_name_board_pages_under_the_selected_work_root(self) -> None:
        for layout in ("linked-worktree", "external-work-root"):
            with self.subTest(layout=layout):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                base = Path(temporary.name).resolve()
                repository = base / "repository"
                subprocess.run(("git", "init", "--quiet", str(repository)), check=True)
                (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
                self._git(repository, "add", "tracked.txt")
                self._git(repository, "commit", "--quiet", "-m", "initial")
                if layout == "linked-worktree":
                    project_root = base / "linked"
                    self._git(repository, "worktree", "add", "--quiet", "-b", "linked", str(project_root))
                    work_root = repository / ".pinboard"
                    roots = resolve_durable_roots(repository)
                else:
                    project_root = repository
                    work_root = base / "external-work"
                    roots = resolve_durable_roots(repository, work_root)
                initialize_database(roots, SQLITE_NOW)
                initialize_store(SQLiteWorkStore(roots.database_path), complete_sqlite_state())
                expected = {
                    "markdown": str(work_root / "views" / "board.md"),
                    "html": str(work_root / "views" / "board.html"),
                }
                selected: JsonObject = {"project_root": str(project_root), "work_root": str(work_root)}

                overview = call_advertised_tool("pinboard_overview", selected)
                status = call_advertised_tool(
                    "pinboard_item_status", {"request": selected | {"operation": "item", "item_id": "work-c"}}
                )

                self.assertEqual("pinboard-overview/v7", overview["schema"])
                self.assertEqual(expected, overview["board"])
                self.assertEqual("pinboard-item-status/v3", status["schema"])
                self.assertEqual(expected, status["board"])
                self.assertNotEqual(project_root, work_root.parent)
                self.assertFalse((work_root / "views" / "board.md").exists())
                self.assertFalse((work_root / "views" / "board.html").exists())


if __name__ == "__main__":
    unittest.main()
