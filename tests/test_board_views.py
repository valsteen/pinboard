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

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files import views as views_module
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode
from pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from pinboard.adapters.files.views import derive_expected_view_bytes, rebuild_facts, refresh_facts
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import (
    definition_compatibility,
    ports,
    queries,
    query_models,
    roadmap,
    stored_state,
    work_brief_models,
)
from pinboard.application.artifacts import NewArtifact
from pinboard.application.service import create_proposal
from pinboard.cli import work_views
from pinboard.cli.entrypoint import main
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import AttemptId, HistoryId, HostId, ProposalId, TaskId, WorkItemId
from pinboard.domain.proposal_models import CreateProposalOperation, ProposalIntake
from pinboard.mcp import execution as mcp_execution
from pinboard.mcp import read_operations as mcp_reads
from pinboard.mcp import server as mcp_server
from tests.artifact_support import write_revision
from tests.checkpoint_support import CheckpointFixture
from tests.decision_support import BOARD
from tests.domain_support import expect_success
from tests.native_support import call_advertised_tool, call_native_tool
from tests.support import (
    SQLITE_NOW,
    JsonObject,
    board_reader,
    complete_sqlite_state,
    initialize_store,
    loaded_board_history,
    test_definition,
    with_definition_dependencies,
)
from tests.test_item_status_facts import ItemLifecycleSupport

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
    item_id: WorkItemId, dependencies: tuple[work_models.Dependency, ...], title: str | None = None
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
            dependencies=(
                *lifecycle.dependencies,
                stored_state.ItemDependency(
                    blocked,
                    needed,
                    0,
                    checkpoint_id=None,
                ),
            ),
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
        _definition(
            item_id,
            tuple(
                WorkItemId(dependency) if isinstance(dependency, str) else dependency
                for dependency in value.definition.dependencies
            ),
            HOSTILE_TITLE,
        )
        if value.item_id == item_id
        else value
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


def _page_data(html: str) -> views_module._BoardPage:
    match = re.search(r'<script id="board-data" type="application/json">(.*?)</script>', html, re.DOTALL)
    assert match is not None
    return msgspec.json.decode(match.group(1), type=views_module._BoardPage)


def _page_items(html: str) -> list[views_module._BoardPageItem]:
    return list(_page_data(html).items)


def _page_dependencies(item: views_module._BoardPageItem) -> list[tuple[str, bool]]:
    return [(value.item_id, value.on_board) for value in item.dependencies]


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


class _GatedBoard:
    """Delegate composed board reads while letting a test hold a refresh between its read and its writes."""

    def __init__(self, board: ports.BoardReader, read: threading.Event, release: threading.Event) -> None:
        self.board = board
        self.read = read
        self.release = release

    def read_roadmap(self) -> roadmap.Projection:
        return self.board.read_roadmap()

    def read_board(self, now: datetime) -> query_models.BoardPortfolio:
        facts = self.board.read_board(now)
        self.read.set()
        if not self.release.wait(10):
            raise AssertionError("The held refresh was never released.")
        return facts


class _FixedBoard:
    def read_roadmap(self) -> roadmap.Projection:
        return roadmap.project(roadmap.Facts(0, (), (), frozenset()))

    """A board reader returning one already projected board, for presentation tests."""

    def __init__(self, board: query_models.BoardPortfolio) -> None:
        self.board = board

    def read_board(self, now: datetime) -> query_models.BoardPortfolio:
        del now
        return self.board


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
        result = rebuild_facts(
            store.read_all_generated_view_facts(now), roots.work_root, {}, board_reader(roots.work_root, store), now
        )
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
            [value.item_id for value in _page_items(html)],
        )
        state = store.validated_snapshot()
        self.assertEqual(
            {
                name: derive_expected_view_bytes(
                    state, {}, loaded_board_history(roots.work_root, state), now=SQLITE_NOW
                ).views[name]
                for name in BOARD_FILES
            },
            {
                name: derive_expected_view_bytes(
                    state, {}, loaded_board_history(roots.work_root, state), now=LATER
                ).views[name]
                for name in BOARD_FILES
            },
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
        self.assertEqual(_utc_instant(latest), _page_data(html).updated_at)
        items = _page_items(html)
        self.assertEqual(live, {value.item_id for value in items})
        for value in items:
            self.assertEqual(_utc_instant(times[value.item_id]), value.updated_at)

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
        self.assertIsNone(_page_data(html).updated_at)
        self.assertEqual([], _page_items(html))

    def test_artifact_only_commit_preserves_board_and_requires_revision_labelled_roadmap_refresh(self) -> None:
        project, roots, store = self._ledger(_validatable_state())
        common = ("--project-root", str(project), "--work-root", str(roots.work_root))
        self._rebuild(roots, store)
        view_root = roots.work_root / "views"
        before = {name: (view_root / name).read_bytes() for name in BOARD_FILES}
        published = write_revision(
            roots, NewArtifact(work_models.ArtifactKind.EVIDENCE, "later-evidence", 1, ".md", b"later\n")
        )

        expect_success(store.accept_artifact_reference(roots.work_root, published, LATER, ownership=None))

        reloaded = store.validated_snapshot()
        self.assertEqual(LATER, reloaded.lifecycle.project.updated_at)
        expected = derive_expected_view_bytes(
            reloaded, {}, loaded_board_history(roots.work_root, reloaded), now=LATER
        ).views
        self.assertEqual(before, {name: expected[name] for name in BOARD_FILES})
        self.assertEqual(before, {name: (view_root / name).read_bytes() for name in BOARD_FILES})
        result, stdout, stderr = self._run_cli(*common, "validate")
        self.assertEqual(0, result, stderr)
        self.assertIn("VIEW_REFRESH_REQUIRED", stdout)
        self.assertIn("roadmap.html", stdout)
        self._rebuild(roots, store)
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
        items = {value.item_id: value for value in _page_items(html)}
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
            expected = derive_expected_view_bytes(
                store.validated_snapshot(),
                {},
                loaded_board_history(roots.work_root, store.validated_snapshot()),
                now=SQLITE_NOW,
            ).views
            self.assertEqual(outputs[-1], {name: expected[name] for name in BOARD_FILES})

        self.assertEqual(outputs[0], outputs[1])

    def test_committed_reorder_changes_neither_an_items_time_nor_the_board_time(self) -> None:
        project, roots, store = self._ledger(complete_sqlite_state())
        self._rebuild(roots, store)
        before = {value.item_id: value.updated_at for value in _page_items(self._board(roots)[1])}
        before_board = _page_data(self._board(roots)[1]).updated_at
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
        self.assertEqual(before, {value.item_id: value.updated_at for value in _page_items(html)})
        self.assertEqual(before_board, _page_data(html).updated_at)

    def test_refresh_rebuild_and_validate_derive_identical_board_bytes(self) -> None:
        _project, roots, store = self._ledger(_every_state())
        view_root = roots.work_root / "views"
        self._rebuild(roots, store)
        rebuilt = {name: (view_root / name).read_bytes() for name in BOARD_FILES}
        for name in BOARD_FILES:
            (view_root / name).unlink()

        refreshed = refresh_facts(
            store.read_generated_view_facts((), (), (), SQLITE_NOW),
            roots.work_root,
            {},
            board_reader(roots.work_root, store),
            SQLITE_NOW,
        )

        self.assertIsNone(refreshed.warning)
        self.assertEqual(rebuilt, {name: (view_root / name).read_bytes() for name in BOARD_FILES})
        expected = derive_expected_view_bytes(
            store.validated_snapshot(),
            {},
            loaded_board_history(roots.work_root, store.validated_snapshot()),
            now=SQLITE_NOW,
        ).views
        self.assertEqual(rebuilt, {name: expected[name] for name in BOARD_FILES})

    def test_unchanged_refresh_preserves_board_bytes_inode_and_modification_time(self) -> None:
        _project, roots, store = self._ledger(complete_sqlite_state())
        self._rebuild(roots, store)
        paths = tuple(roots.work_root / "views" / name for name in BOARD_FILES)
        before = {path: _file_identity(path) for path in paths}

        refreshed = refresh_facts(
            store.read_generated_view_facts((), (), (), LATER),
            roots.work_root,
            {},
            board_reader(roots.work_root, store),
            LATER,
        )

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
        work_c = next(value for value in _page_items(html) if value.item_id == "work-c")
        self.assertEqual(HOSTILE_TITLE, work_c.title)
        self.assertEqual(HOSTILE_INTAKE, work_c.intake_next_action)
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
        self.assertEqual("pinboard-board-page/v2", data.schema)
        states = data.states
        self.assertEqual({state.value for state in work_models.WorkState}, set(states))
        items = {value.item_id: value for value in _page_items(html)}
        attempts = {"work-a", "paused-work", "review-work"}
        for item_id, value in items.items():
            raw_item = msgspec.to_builtins(value)
            self.assertNotIn("prompt", raw_item)
            self.assertNotIn("attempt_id", raw_item)
            self.assertNotIn("item_view", raw_item)
            self.assertTrue(value.effect and value.unlock)
            standing = value.standing
            if item_id in attempts:
                assert isinstance(standing, views_module._BoardPageStanding)
                assert isinstance(standing.checkpoint, views_module._BoardPageUnreadableBrief)
                self.assertEqual(f"{item_id}-1" if item_id != "work-a" else "work-a-1", standing.checkpoint.attempt_id)
            else:
                assert isinstance(standing, views_module._BoardPageNextStep)
                self.assertTrue(standing.text)
        self.assertNotIn("prompt", markdown.lower())
        self.assertNotIn("<textarea", html)
        self.assertNotIn("clipboard", html.lower())
        self.assertEqual("continue", items["work-a"].intake_next_action)
        self.assertEqual("Current work remains bounded.", items["work-a"].intake_notes)
        self.assertEqual(
            [
                {
                    "item_id": "needed-first",
                    "reason": "Inferred prerequisite needed-first: Blocked work cannot start without it.",
                    "on_board": True,
                    "unsatisfied": True,
                }
            ],
            list(msgspec.to_builtins(items["blocked-work"].dependencies)),
        )
        self.assertIn("Original intake context", html)

    def test_page_presents_every_condition_with_its_time_record_and_named_absence(self) -> None:
        _project, roots, store = self._ledger(complete_sqlite_state())
        board = board_reader(roots.work_root, store).read_board(SQLITE_NOW)
        receipt_at = SQLITE_NOW + timedelta(hours=1)
        ready_at = SQLITE_NOW + timedelta(hours=2)
        kinds = decision_models.ActionKind
        cases: tuple[tuple[query_models.BoardCondition, tuple[str, datetime | None, str | None, bool]], ...] = (
            (query_models.ActivatedCondition(receipt_at), ("activated", receipt_at, None, False)),
            (query_models.ResumedCondition(receipt_at), ("resumed", receipt_at, None, False)),
            (query_models.PausedCondition(receipt_at, "Wait."), ("paused", receipt_at, "Wait.", False)),
            (query_models.PausedCondition(receipt_at, None), ("paused", receipt_at, None, True)),
            (query_models.BlockedCondition(receipt_at), ("blocked", receipt_at, None, True)),
            (
                query_models.ReboundCondition(receipt_at, work_models.AttemptState.PAUSED, None),
                ("rebound", receipt_at, None, True),
            ),
            (
                query_models.ReboundCondition(receipt_at, work_models.AttemptState.ACTIVE, None),
                ("rebound", receipt_at, None, False),
            ),
            (query_models.SubmittedForReviewCondition(receipt_at), ("submitted", receipt_at, None, False)),
            (query_models.ReviewedReadyCondition(receipt_at, ready_at), ("reviewed-ready", ready_at, None, False)),
            (query_models.ReturnedCondition(receipt_at, "Rework."), ("returned", receipt_at, "Rework.", False)),
            (query_models.RetainedReturnedCondition(receipt_at, None), ("returned", receipt_at, None, True)),
            (query_models.ContinuedCondition(receipt_at, None), ("continued", receipt_at, None, True)),
            (
                query_models.CheckpointAcceptedCondition(receipt_at, "first-step", "Done."),
                ("checkpoint-accepted", receipt_at, "Done.", False),
            ),
            (
                query_models.RetainedCheckpointAcceptedCondition(receipt_at, None, None),
                ("checkpoint-accepted", receipt_at, None, True),
            ),
            (
                query_models.RecordedActionCondition(receipt_at, kinds.DISPATCH),
                ("recorded-action", receipt_at, None, False),
            ),
            (
                query_models.UnreadableCondition(HistoryId(7), receipt_at, "Bad outcome."),
                ("unreadable", receipt_at, None, True),
            ),
            (query_models.NoReceiptCondition(), ("no-receipt", None, None, False)),
        )
        for condition, (kind, at, text, absent) in cases:
            with self.subTest(kind=kind, condition=condition):
                items = tuple(
                    replace(
                        value,
                        attempt=query_models.BoardAttemptProgress(
                            query_models.UnreadableBrief(AttemptId("work-a-1"), "opaque"),
                            condition,
                            (),
                            decision_batch=None,
                        ),
                    )
                    if value.overview.item_id == "work-a"
                    else value
                    for value in board.items
                )
                result = rebuild_facts(
                    store.read_all_generated_view_facts(SQLITE_NOW),
                    roots.work_root,
                    {},
                    _FixedBoard(query_models.BoardPortfolio(items)),
                    SQLITE_NOW,
                )
                self.assertIsNone(result.warning)
                work_a = next(value for value in _page_items(self._board(roots)[1]) if value.item_id == "work-a")
                standing = work_a.standing
                assert isinstance(standing, views_module._BoardPageStanding)
                page = standing.condition
                self.assertEqual(
                    (kind, None if at is None else _utc_instant(at), text, absent),
                    (page.kind, page.at, page.text, page.absence is not None),
                )
                self.assertEqual(at is not None, page.time_source is not None)
                self.assertTrue(page.label)

    def test_page_names_every_changed_definition_field_and_a_retained_definition_schema(self) -> None:
        _project, roots, store = self._ledger(complete_sqlite_state())
        board = board_reader(roots.work_root, store).read_board(SQLITE_NOW)
        obligation = work_models.WorkObligation(
            work_models.ObligationId("kept"), "Kept.", work_models.ObligationDeferralPolicy.FORBIDDEN
        )
        changes = query_models.ScopeRevisionChanges(
            tuple(query_models.TextFieldChange(field, None, field.value) for field in query_models.DefinitionTextField),
            tuple(
                query_models.ListFieldChange(field, (field.value,), ()) for field in query_models.DefinitionListField
            ),
            (obligation,),
            (obligation,),
            (query_models.ObligationChange(obligation, replace(obligation, statement="Changed.")),),
        )
        work_a = next(value for value in board.items if value.overview.item_id == "work-a")
        definition = work_a.definition
        assert isinstance(definition, query_models.WorkItemDefinitionView)
        retained = definition_compatibility.HistoricalDefinitionV1(
            definition.acceptance_criteria,
            tuple(value for value in definition.dependencies if isinstance(value, str)),
            definition.effect,
            definition.evidence,
            definition.hypothesis,
            definition.non_scope,
            definition.objective,
            definition.scope,
            definition.title,
            definition.unlock,
        )
        revisions = (*work_a.scope_revisions, query_models.BoardScopeRevision(2, LATER, "Every field.", changes))
        items = tuple(
            replace(value, scope_revisions=revisions, definition=retained) if value is work_a else value
            for value in board.items
        )

        rebuild_facts(
            store.read_all_generated_view_facts(SQLITE_NOW),
            roots.work_root,
            {},
            _FixedBoard(query_models.BoardPortfolio(items)),
            SQLITE_NOW,
        )

        page = next(value for value in _page_items(self._board(roots)[1]) if value.item_id == "work-a")
        latest = page.scope_revisions[-1].changes
        assert isinstance(latest, views_module._BoardPageScopeChanges)
        self.assertEqual(len(query_models.DefinitionTextField), len({value.field for value in latest.texts}))
        self.assertEqual(len(query_models.DefinitionListField), len({value.field for value in latest.lists}))
        self.assertEqual(1, len(latest.obligations_added))
        self.assertEqual(1, len(latest.obligations_changed))
        scope = page.scope
        self.assertEqual((2, None, None), (scope.revision, scope.obligations, scope.checkout_policy))

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
        self.assertEqual(requested, [value.item_id for value in _page_items(html)])

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
                store.read_generated_view_facts((), (), (), SQLITE_NOW),
                roots.work_root,
                {},
                board_reader(roots.work_root, store),
                SQLITE_NOW,
            )
        self.assertIsNotNone(refreshed.warning)
        with patch.object(views_module.fcntl, "flock", side_effect=OSError("no locks available")):
            unlocked = rebuild_facts(
                store.read_all_generated_view_facts(SQLITE_NOW),
                roots.work_root,
                {},
                board_reader(roots.work_root, store),
                SQLITE_NOW,
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
            dependency_brief_pins=(),
        )
        assert not isinstance(committed, DecisionFailure)

        refreshed = work_views.refresh_effect(roots, store, committed.effect, SQLITE_NOW)

        self.assertIsNone(refreshed.warning)
        markdown, html = self._board(roots)
        self.assertIn("`required-before-work-c` (ready)", markdown)
        self.assertIn(
            "  - Depends on required-before-work-c: Inferred prerequisite required-before-work-c: "
            "The dependency must be preserved before activation.\n",
            markdown,
        )
        self.assertIn("required-before-work-c", [value.item_id for value in _page_items(html)])

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

        def refresh(portfolio: ports.BoardReader) -> None:
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

        earlier_thread = threading.Thread(
            target=refresh, args=(_GatedBoard(board_reader(roots.work_root, store), earlier_read, release_earlier),)
        )
        later_thread = threading.Thread(target=refresh, args=(board_reader(roots.work_root, store),))

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
                dependency_brief_pins=(),
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
        self.assertIn("required-before-work-c", [value.item_id for value in _page_items(html)])
        expected = derive_expected_view_bytes(
            store.validated_snapshot(),
            {},
            loaded_board_history(roots.work_root, store.validated_snapshot()),
            now=SQLITE_NOW,
        ).views
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
            store.read_generated_view_facts((), (), (), SQLITE_NOW),
            roots.work_root,
            {},
            board_reader(roots.work_root, store),
            SQLITE_NOW,
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
                self.assertEqual("pinboard-item-status/v4", status["schema"])
                self.assertEqual(expected, status["board"])
                self.assertNotEqual(project_root, work_root.parent)
                self.assertFalse((work_root / "views" / "board.md").exists())
                self.assertFalse((work_root / "views" / "board.html").exists())


class BoardHistoryTest(ItemLifecycleSupport):
    """The board shows where long-running work stands from records committed through the native workflow."""

    def board_item(self, fixture: CheckpointFixture, item_id: str = "work-a") -> JsonObject:
        html = (fixture.work / "views" / "board.html").read_text(encoding="utf-8")
        match = re.search(r'<script id="board-data" type="application/json">(.*?)</script>', html, re.DOTALL)
        assert match is not None
        items = self.json_array(self.json_object(json.loads(match.group(1)))["items"])
        return next(self.json_object(value) for value in items if self.json_object(value)["item_id"] == item_id)

    def standing(self, fixture: CheckpointFixture) -> JsonObject:
        standing = self.json_object(self.board_item(fixture)["standing"])
        self.assertEqual("attempt", standing["kind"])
        return standing

    def condition(self, fixture: CheckpointFixture) -> JsonObject:
        return self.json_object(self.standing(fixture)["condition"])

    def committed_at(self, fixture: CheckpointFixture, history_id: int) -> str:
        receipts = fixture.store.validated_snapshot().transition_receipts
        return _utc_instant(next(value for value in receipts if int(value.history_id) == history_id).committed_at)

    def rebuild(self, fixture: CheckpointFixture) -> None:
        rebuilt, stdout, stderr = self.run_cli(*fixture.common, "views", "rebuild")
        self.assertEqual(0, rebuilt, f"{stdout}\n{stderr}")

    def assert_board_parity(self, fixture: CheckpointFixture) -> dict[str, bytes]:
        """Require refresh, rebuild, validation and diagnosis to agree on the board bytes of this ledger."""

        view_root = fixture.work / "views"
        refreshed = {name: (view_root / name).read_bytes() for name in BOARD_FILES}
        for command in ("validate", "diagnose"):
            _status, stdout, stderr = self.run_cli(*fixture.common, command, "--json")
            report = self.json_object(json.loads(stdout))
            if command == "diagnose":
                report = self.json_object(report["validation"])
            drifted = [
                diagnostic
                for diagnostic in self.json_array(report["diagnostics"])
                if self.json_object(diagnostic)["code"] == "VIEW_REFRESH_REQUIRED"
                and str(self.json_object(diagnostic)["path"]).endswith(BOARD_FILES)
            ]
            self.assertEqual([], drifted, f"{command}: {stdout}\n{stderr}")
        self.rebuild(fixture)
        self.assertEqual(refreshed, {name: (view_root / name).read_bytes() for name in BOARD_FILES})
        return refreshed

    def publish_rebind(self, fixture: CheckpointFixture, brief: work_brief_models.WorkBrief) -> None:
        published = call_native_tool(
            mcp_server.BRIEF_PUBLISH_TOOL,
            {**self.roots(fixture), "brief": self.json_object(msgspec.json.decode(msgspec.json.encode(brief)))},
        )
        self.assertEqual("committed", published["status"], published)
        reference = self.json_object(published["reference"])
        self.transition(
            fixture,
            "rebind-attempt:work-a-1",
            {
                "branch": brief.branch,
                "base_revision": brief.base_revision,
                "brief_artifact_ref_id": reference["artifact_ref_id"],
            },
        )

    def traced_board_read(self, fixture: CheckpointFixture) -> tuple[query_models.BoardPortfolio, list[str], set[str]]:
        """Compose one board read while recording every SQLite statement and table it touches."""

        statements: list[str] = []
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
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(sqlite_store, "open_database", recording_open):
            board = board_reader(fixture.work, fixture.store).read_board(LATER)
        return board, [value for value in statements if value.lstrip().upper().startswith("SELECT")], tables

    def grow_unrelated_history(self, fixture: CheckpointFixture, count: int) -> None:
        """Add retained receipts, acceptances and finished attempts that no live card shows."""

        snapshot = fixture.store.validated_snapshot()
        brief_id = next(
            value.artifact_ref_id
            for value in snapshot.artifact_references
            if value.kind == work_models.ArtifactKind.BRIEF
        )
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            revision = connection.execute("SELECT revision FROM project_meta").fetchone()[0]
            for index in range(count):
                subject = f"finished-{index}-1"
                connection.execute(
                    """
                    INSERT INTO attempts (
                        attempt_id, item_id, state, branch, base_revision, provenance, brief_artifact_ref_id,
                        brief_artifact_kind, candidate_revision, candidate_recorded_at, accepted_scope_revision,
                        accepted_scope_digest, subject_revision, recorded_at, updated_at
                    ) VALUES (?, 'work-b', 'done', 'old', 'base', 'test', ?, 'brief', NULL, NULL, 1, ?, 1, ?, ?)
                    """,
                    (subject, brief_id, "0" * 64, SQLITE_NOW.isoformat(), SQLITE_NOW.isoformat()),
                )
                for schema in ("checkpoint-acceptance/v2", "transition-receipt/v1"):
                    revision += 1
                    connection.execute(
                        """
                        INSERT INTO transition_history (
                            project_revision, action_id, action_kind, subject_id, authorization_kind, input_schema,
                            input_json, outcome_schema, outcome_json, committed_at
                        ) VALUES (?, 'accept-checkpoint:old', 'accept-checkpoint', ?, 'project', 'decision/v1', '{}',
                                  ?, '{}', ?)
                        """,
                        (revision, subject, schema, SQLITE_NOW.isoformat()),
                    )
            connection.execute("UPDATE project_meta SET revision = ?", (revision,))

    def test_board_read_stays_selected_and_bounded_as_unrelated_history_grows(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Rework.")
        candidate = self.submit_candidate(fixture, "reviewed")
        self.record_ready(fixture, candidate)
        before, before_statements, tables = self.traced_board_read(fixture)

        self.grow_unrelated_history(fixture, 30)
        after, after_statements, _tables = self.traced_board_read(fixture)

        self.assertEqual(before, after)
        self.assertEqual(len(before_statements), len(after_statements))
        keyed = [value for value in after_statements if "artifact_key = '" in value]
        self.assertEqual(1, len(keyed), keyed)
        self.assertTrue({"transition_history", "artifact_refs", "work_item_definition_revisions"} <= tables)
        self.assertNotIn("preparation_leases", tables)
        records = fixture.store.read_board_records(LATER).records
        self.assertEqual(["work-a-1"], [str(value.attempt_id) for value in records.attempts])
        self.assertTrue(
            all(str(value.subject_id) == "work-a-1" for value in records.attempts[0].checkpoint_acceptances)
        )
        live = {value.overview.item_id for value in after.items}
        self.assertEqual(live, {str(value.item_id) for value in records.definition_histories})
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            plans = {
                statement: " ".join(row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}"))
                for statement in after_statements
            }
        expected_indexes = {
            "FROM attempts INDEXED BY one_live_attempt_per_item": "one_live_attempt_per_item",
            "WHERE project_revision IN": "project_revision",
            "outcome_schema = 'checkpoint-acceptance/v2'": "checkpoint_history_by_subject",
            "FROM work_item_definition_revisions": "sqlite_autoindex_work_item_definition_revisions_1",
            "FROM artifact_refs WHERE artifact_ref_id IN": "INTEGER PRIMARY KEY",
            "FROM artifact_ownership WHERE artifact_ref_id IN": "sqlite_autoindex_artifact_ownership",
            "kind = 'evidence' AND artifact_key IN": "sqlite_autoindex_artifact_refs",
        }
        for marker, index in expected_indexes.items():
            selected = [plan for statement, plan in plans.items() if marker in statement]
            self.assertTrue(selected, marker)
            self.assertTrue(all(index in plan for plan in selected), (marker, selected))

    def test_where_it_stands_follows_the_current_brief_and_latest_receipt_through_the_workflow(self) -> None:
        fixture = self.checkpoint_fixture()
        reason = "Missing: `views` evidence for criterion 2."
        returned = self.return_for_review(fixture, reason)

        standing = self.standing(fixture)
        checkpoint = self.json_object(standing["checkpoint"])
        brief_reference = fixture.store.read_latest_artifact_reference(work_models.ArtifactKind.BRIEF, "work-a-1")
        assert brief_reference is not None
        self.assertEqual(
            {
                "kind": "checkpoint",
                "checkpoint_id": fixture.brief.checkpoint.checkpoint_id,
                "title": fixture.brief.checkpoint.title,
                "brief_revision": brief_reference.revision,
                "brief_accepted_at": _utc_instant(brief_reference.created_at),
            },
            {key: value for key, value in checkpoint.items() if key != "remaining_work"},
        )
        disposition = fixture.brief.checkpoint.disposition
        assert isinstance(disposition, work_brief_models.ContinueCheckpointDisposition)
        self.assertEqual({"kind": "recorded", "text": disposition.remaining_work}, checkpoint["remaining_work"])
        condition = self.condition(fixture)
        self.assertEqual(
            ("returned", self.committed_at(fixture, returned), reason),
            (condition["kind"], condition["at"], condition["text"]),
        )
        self.assertEqual([], standing["milestones"])
        self.assert_board_parity(fixture)

        self.close_prerequisite(fixture)
        self.transition(fixture, "pause:work-a-1", {"reason": "Wait for the maintainer."})
        paused = self.condition(fixture)
        self.assertEqual(
            ("paused", self.committed_at(fixture, self.latest_history_id(fixture)), "Wait for the maintainer."),
            (paused["kind"], paused["at"], paused["text"]),
        )
        self.update_receipt(
            fixture,
            self.latest_history_id(fixture),
            outcome_json=json.dumps({"evidence": None, "outcome": "pause"}, separators=(",", ":")),
        )
        self.rebuild(fixture)
        unrecorded = self.condition(fixture)
        self.assertEqual(("paused", None), (unrecorded["kind"], unrecorded["text"]))
        self.assertIsNotNone(unrecorded["absence"])

        self.transition(fixture, "resume:work-a", {})
        self.assertEqual("resumed", self.condition(fixture)["kind"])
        rebound = replace_struct(
            fixture.brief,
            artifact_revision=2,
            checkpoint=replace_struct(
                fixture.brief.checkpoint,
                title="Second checkpoint title",
                disposition=work_brief_models.TerminalCheckpointDisposition(),
            ),
        )
        self.publish_rebind(fixture, rebound)
        checkpoint = self.json_object(self.standing(fixture)["checkpoint"])
        self.assertEqual(("Second checkpoint title", 2), (checkpoint["title"], checkpoint["brief_revision"]))
        self.assertEqual({"kind": "final"}, checkpoint["remaining_work"])
        self.assertEqual(("rebound", None), (self.condition(fixture)["kind"], self.condition(fixture)["text"]))

        candidate = self.submit_candidate(fixture, "resubmitted")
        submitted = self.condition(fixture)
        self.assertEqual(
            ("submitted", self.committed_at(fixture, self.latest_history_id(fixture))),
            (submitted["kind"], submitted["at"]),
        )
        recorded = self.record_ready(fixture, candidate)
        ready = self.condition(fixture)
        ready_reference = next(
            value
            for value in fixture.store.validated_snapshot().artifact_references
            if value.artifact_ref_id == self.json_object(recorded["candidate_review"])["artifact_ref_id"]
        )
        self.assertEqual(("reviewed-ready", _utc_instant(ready_reference.created_at)), (ready["kind"], ready["at"]))
        self.assertIsNone(recorded["warning"])
        self.assert_board_parity(fixture)
        reloaded = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        self.assertEqual(
            (fixture.work / "views" / "board.html").read_bytes(),
            derive_expected_view_bytes(
                reloaded, {}, loaded_board_history(fixture.work, reloaded), now=SQLITE_NOW
            ).views["board.html"],
        )

        (fixture.work / "attempts" / "work-a-1" / "review.md").write_text("A later review.\n", encoding="utf-8")
        _status, stdout, _stderr = self.run_cli(*fixture.common, "validate", "--json")
        self.assertIn("board.html", stdout)
        self.rebuild(fixture)
        self.assertEqual("submitted", self.condition(fixture)["kind"])
        self.assert_board_parity(fixture)

    def test_ready_record_leaf_refresh_failure_is_a_view_warning_with_the_record_intact(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Rework.")
        candidate = self.submit_candidate(fixture, "reviewed")
        original_open = os.open

        def refuse_board_lock(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
            if Path(path).name == "board.lock":
                raise PermissionError("board lock denied")
            return original_open(path, flags, mode)

        with patch.object(views_module.os, "open", refuse_board_lock):
            recorded = self.record_ready(fixture, candidate)

        warning = self.json_object(recorded["warning"])
        self.assertIn("views rebuild", str(warning["recovery"]))
        self.assertEqual("submitted", self.condition(fixture)["kind"])
        self.assertEqual("ready", self.verdict(fixture)["kind"])
        self.rebuild(fixture)
        self.assertEqual("reviewed-ready", self.condition(fixture)["kind"])

    def test_absent_evidence_or_an_unreadable_ready_record_leaves_the_attempt_submitted_for_review(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Rework.")
        candidate = self.submit_candidate(fixture, "reviewed")
        recorded = self.record_ready(fixture, candidate)
        self.assertEqual("reviewed-ready", self.condition(fixture)["kind"])
        result = fixture.work / "attempts" / "work-a-1" / "result.md"
        kept = result.read_bytes()
        result.unlink()
        _board, statements, _tables = self.traced_board_read(fixture)
        self.assertEqual([], [value for value in statements if "artifact_key = '" in value])
        self.rebuild(fixture)
        self.assertEqual("submitted", self.condition(fixture)["kind"])
        result.write_bytes(kept)
        selector = str(self.json_object(recorded["candidate_review"])["selector"])
        (fixture.work / selector).write_bytes(b"damaged\n")
        self.rebuild(fixture)
        self.assertEqual("submitted", self.condition(fixture)["kind"])

    def test_milestones_list_only_the_open_attempts_acceptances_newest_first(self) -> None:
        fixture = self.accepted_package_fixture()
        accepted = self.latest_history_id(fixture)
        accepted_at = self.committed_at(fixture, accepted)
        condition = self.condition(fixture)
        self.assertEqual(("checkpoint-accepted", "Accepted evidence."), (condition["kind"], condition["text"]))
        self.assert_board_parity(fixture)
        outcome = json.dumps(
            {
                "candidate": "c",
                "checkpoint": "later-checkpoint",
                "evidence": "Later evidence.",
                "outcome": "accept-checkpoint",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            revision = connection.execute("SELECT revision FROM project_meta").fetchone()[0]
            for offset, subject in enumerate(("work-a-1", "other-attempt-1", "done-attempt-1", "work-a-1")):
                connection.execute(
                    """
                    INSERT INTO transition_history (
                        project_revision, action_id, action_kind, subject_id, authorization_kind, input_schema,
                        input_json, outcome_schema, outcome_json, committed_at
                    ) VALUES (?, ?, 'accept-checkpoint', ?, 'project', 'decision/v1', '{}',
                              'checkpoint-acceptance/v2', ?, ?)
                    """,
                    (
                        revision + 1 + offset,
                        f"accept-checkpoint:{subject}",
                        subject,
                        outcome if offset < 3 else '{"not":"an acceptance"}',
                        (LATER + timedelta(minutes=offset)).isoformat(),
                    ),
                )
            connection.execute("UPDATE project_meta SET revision = ?", (revision + 4,))
        self.rebuild(fixture)

        milestones = [self.json_object(value) for value in self.json_array(self.standing(fixture)["milestones"])]
        self.assertEqual(
            [
                ("unreadable", _utc_instant(LATER + timedelta(minutes=3))),
                ("accepted", _utc_instant(LATER)),
                ("accepted", accepted_at),
            ],
            [(value["kind"], value.get("accepted_at", value.get("committed_at"))) for value in milestones],
        )
        self.assertEqual(
            [("later-checkpoint", "Later evidence."), (fixture.brief.checkpoint.checkpoint_id, "Accepted evidence.")],
            [(value["checkpoint_id"], value["evidence"]) for value in milestones if value["kind"] == "accepted"],
        )
        context = fixture.store.read_completion_context(AttemptId("work-a-1"))
        assert context is not None
        self.assertEqual(
            [value["history_id"] for value in reversed(milestones)],
            [int(value.receipt.history_id) for value in context.checkpoints],
        )

    def test_scope_changes_list_each_revision_with_its_reason_and_mechanical_differences(self) -> None:
        fixture = self.checkpoint_fixture()
        current = call_native_tool(
            mcp_server.ITEM_DEFINITION_TOOL,
            {"request": {**self.roots(fixture), "operation": "current", "item_id": "work-a"}},
        )
        definition = self.json_object(current["definition"])
        scope = [str(value) for value in self.json_array(definition["scope"])]
        obligations = [self.json_object(value) for value in self.json_array(definition["obligations"])]
        revised: JsonObject = {
            **definition,
            "title": "Revised title",
            "scope": [scope[0] + " Edited.", *scope[1:]],
            "obligations": [
                {**obligations[0], "statement": "Changed statement."},
                {"obligation_id": "added-obligation", "statement": "Added.", "deferral_policy": "allowed"},
            ],
        }
        self.transition(
            fixture,
            "revise-item:work-a",
            {
                "schema": "pinboard-item-revision/v1",
                "expected_revision": current["definition_revision"],
                "expected_digest": current["definition_digest"],
                "source_task": "review-owner",
                "reason": "Narrow the scope.",
                "definition": revised,
            },
        )

        item = self.board_item(fixture)
        revisions = [self.json_object(value) for value in self.json_array(item["scope_revisions"])]
        history = fixture.store.read_item_definition_history(WorkItemId("work-a"), limit=10, before_revision=None)
        self.assertEqual(
            [(value.revision, _utc_instant(value.accepted_at), value.reason) for value in reversed(history.revisions)],
            [(value["revision"], value["accepted_at"], value["reason"]) for value in revisions],
        )
        self.assertEqual({"kind": "original"}, revisions[0]["changes"])
        changes = self.json_object(revisions[-1]["changes"])
        self.assertEqual(
            [{"field": "Title", "before": definition["title"], "after": "Revised title"}], changes["texts"]
        )
        self.assertEqual(
            [{"field": "Scope", "added": [scope[0] + " Edited."], "removed": [scope[0]]}], changes["lists"]
        )
        self.assertEqual(["added-obligation (deferral allowed): Added."], changes["obligations_added"])
        self.assertEqual([], changes["obligations_removed"])
        changed = [self.json_object(value) for value in self.json_array(changes["obligations_changed"])]
        self.assertEqual(
            [obligations[0]["statement"], "Changed statement."],
            [str(changed[0][key]).partition("): ")[2] for key in ("before", "after")],
        )
        scope_page = self.json_object(item["scope"])
        self.assertEqual(revisions[-1]["revision"], scope_page["revision"])
        self.assertEqual(revised["scope"], scope_page["scope"])
        self.assertNotIn(
            "work-c", [str(self.json_object(value)["reason"]) for value in self.json_array(item["scope_revisions"])]
        )
        self.assert_board_parity(fixture)

    def test_record_text_round_trips_inert_and_multiline_remaining_work_stays_verbatim(self) -> None:
        fixture = self.checkpoint_fixture()
        hostile_reason = "Label: `</script>` & <b>bold</b> \u2028 next"
        self.return_for_review(fixture, hostile_reason)
        self.assertEqual(hostile_reason, self.condition(fixture)["text"])
        remaining = "Lead </SCRIPT> line\n\n- Item with `code` & <i>tag</i>\n1. Numbered\u2029item\nReferences: a.md"
        checkpoint = fixture.brief.checkpoint
        self.publish_rebind(
            fixture,
            replace_struct(
                fixture.brief,
                artifact_revision=2,
                checkpoint=replace_struct(
                    checkpoint,
                    title=HOSTILE_TITLE,
                    disposition=work_brief_models.ContinueCheckpointDisposition(remaining),
                ),
            ),
        )
        definition = call_native_tool(
            mcp_server.ITEM_DEFINITION_TOOL,
            {"request": {**self.roots(fixture), "operation": "current", "item_id": "work-a"}},
        )
        self.transition(
            fixture,
            "revise-item:work-a",
            {
                "schema": "pinboard-item-revision/v1",
                "expected_revision": definition["definition_revision"],
                "expected_digest": definition["definition_digest"],
                "source_task": "review-owner",
                "reason": hostile_reason,
                "definition": {**self.json_object(definition["definition"]), "unlock": HOSTILE_INTAKE},
            },
        )

        html = (fixture.work / "views" / "board.html").read_text(encoding="utf-8")
        for raw in ("</script><img", "<b>bold", "</SCRIPT>", "<i>tag", "\u2028", "\u2029", "&amp;"):
            self.assertNotIn(raw, html)
        self.assertEqual(2, html.lower().count("</script"))
        checkpoint_page = self.json_object(self.standing(fixture)["checkpoint"])
        self.assertEqual(HOSTILE_TITLE, checkpoint_page["title"])
        self.assertEqual({"kind": "recorded", "text": remaining}, checkpoint_page["remaining_work"])
        revision = self.json_object(self.json_array(self.board_item(fixture)["scope_revisions"])[-1])
        self.assertEqual(hostile_reason, revision["reason"])
        self.assertEqual(
            [
                {
                    "field": "What this unlocks",
                    "before": self.json_object(definition["definition"])["unlock"],
                    "after": HOSTILE_INTAKE,
                }
            ],
            self.json_object(revision["changes"])["texts"],
        )
        script = html.rpartition("<script>")[2]
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("insertAdjacentHTML", script)
        self.assert_board_parity(fixture)

    def test_multiline_records_of_every_kind_reach_the_page_data_exactly(self) -> None:
        def record(kind: str) -> str:
            return f"Lead sentence for the {kind}.\n\nProblem:\n- `{kind}` detail.\n1. Step.\n\nReferences: {kind}.md"

        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, record("return"))
        self.assertEqual(record("return"), self.condition(fixture)["text"])
        continued = self.submit_candidate(fixture, "continued")
        self.record_ready(fixture, continued)
        self.transition(
            fixture, "accept-review-and-continue:work-a-1", {"candidate": continued, "evidence": record("continue")}
        )
        self.assertEqual(record("continue"), self.condition(fixture)["text"])
        accepted = self.submit_candidate(fixture, "accepted")
        self.record_ready(fixture, accepted)
        checkpoint_id = fixture.brief.checkpoint.checkpoint_id
        self.transition(
            fixture,
            "accept-checkpoint:work-a-1",
            {"checkpoint": checkpoint_id, "candidate": accepted, "evidence": record("acceptance")},
        )
        self.assertEqual(record("acceptance"), self.condition(fixture)["text"])
        milestone = self.json_object(self.json_array(self.standing(fixture)["milestones"])[0])
        self.assertEqual((checkpoint_id, record("acceptance")), (milestone["checkpoint_id"], milestone["evidence"]))
        self.close_prerequisite(fixture)
        published = call_native_tool(
            mcp_server.BRIEF_PUBLISH_TOOL,
            {
                **self.roots(fixture),
                "brief": self.json_object(
                    msgspec.json.decode(msgspec.json.encode(replace_struct(fixture.brief, artifact_revision=2)))
                ),
            },
        )
        self.assertEqual("committed", published["status"], published)
        self.transition(
            fixture,
            "resume:work-a",
            {"brief_artifact_ref_id": self.json_object(published["reference"])["artifact_ref_id"]},
        )
        self.transition(fixture, "pause:work-a-1", {"reason": record("pause")})
        self.assertEqual(record("pause"), self.condition(fixture)["text"])
        self.publish_rebind(fixture, replace_struct(fixture.brief, artifact_revision=3))
        self.assertEqual(
            ("rebound", record("pause")), (self.condition(fixture)["kind"], self.condition(fixture)["text"])
        )
        definition = call_native_tool(
            mcp_server.ITEM_DEFINITION_TOOL,
            {"request": {**self.roots(fixture), "operation": "current", "item_id": "work-a"}},
        )
        self.transition(
            fixture,
            "revise-item:work-a",
            {
                "schema": "pinboard-item-revision/v1",
                "expected_revision": definition["definition_revision"],
                "expected_digest": definition["definition_digest"],
                "source_task": "review-owner",
                "reason": record("scope revision"),
                "definition": {**self.json_object(definition["definition"]), "title": "Renamed"},
            },
        )
        revision = self.json_object(self.json_array(self.board_item(fixture)["scope_revisions"])[-1])
        self.assertEqual(record("scope revision"), revision["reason"])
        self.assert_board_parity(fixture)

    def test_unreadable_brief_is_a_card_notice_while_the_board_refreshes_and_validation_agrees(self) -> None:
        fixture = self.checkpoint_fixture()
        self.return_for_review(fixture, "Rework.")
        brief_reference = fixture.store.read_latest_artifact_reference(work_models.ArtifactKind.BRIEF, "work-a-1")
        assert brief_reference is not None
        (fixture.work / brief_reference.selector).write_bytes(b"{}\n")

        self.close_prerequisite(fixture)

        standing = self.standing(fixture)
        notice = self.json_object(standing["checkpoint"])
        self.assertEqual(("unreadable-brief", "work-a-1"), (notice["kind"], notice["attempt_id"]))
        self.assertEqual("returned", self.json_object(standing["condition"])["kind"])
        self.assertNotIn(
            "work-c", [value.item_id for value in _page_items((fixture.work / "views" / "board.html").read_text())]
        )
        _status, stdout, _stderr = self.run_cli(*fixture.common, "validate", "--json")
        report = self.json_object(json.loads(stdout))
        codes = {str(self.json_object(value)["code"]) for value in self.json_array(report["diagnostics"])}
        self.assertIn("STORAGE_INVARIANT_VIOLATION", codes)
        board_drift = [
            value
            for value in self.json_array(report["diagnostics"])
            if str(self.json_object(value)["path"]).endswith(BOARD_FILES)
        ]
        self.assertEqual([], board_drift)

    def test_damaged_current_format_receipt_fails_only_its_card_and_retained_shapes_render_their_facts(self) -> None:
        fixture = self.checkpoint_fixture()
        returned = self.return_for_review(fixture, "Current reason.")
        self.update_receipt(fixture, returned, outcome_json='{"evidence":"Current reason."}')
        self.rebuild(fixture)
        damaged = self.condition(fixture)
        self.assertEqual("unreadable", damaged["kind"])
        self.assertIn(str(returned), str(damaged["label"]))
        self.assertTrue(
            any(value.item_id == "work-c" for value in _page_items((fixture.work / "views" / "board.html").read_text()))
        )
        self.assert_board_parity(fixture)
        self.update_receipt(
            fixture,
            returned,
            input_schema="decision/v1",
            input_json="{}",
            outcome_json='{"evidence":"Retained reason.","outcome":"return-for-correction"}',
        )
        self.rebuild(fixture)
        retained = self.condition(fixture)
        self.assertEqual(("returned", "Retained reason."), (retained["kind"], retained["text"]))
        self.assert_board_parity(fixture)


if __name__ == "__main__":
    unittest.main()
