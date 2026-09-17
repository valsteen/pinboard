import asyncio
import io
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable, Coroutine, Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import anyio
import msgspec
from mcp.client.session import ClientSession, IncomingMessage
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver.exceptions import UnexpectedToolError
from mcp.shared.message import SessionMessage
from mcp_types import CallToolResult, TextContent, Tool
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode
from pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from pinboard.adapters.files.models import ViewRefreshResult, ViewWarning
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import (
    authority_operations,
    queries,
    query_models,
    stored_state,
    work_brief_models,
    work_briefs,
)
from pinboard.application.artifact_publication import ArtifactPublication
from pinboard.application.artifacts import NewArtifact
from pinboard.application.mutation_models import CommittedEffect
from pinboard.application.ports import WorkStore, WorkStoreError
from pinboard.application.work_briefs import canonical_work_brief_bytes
from pinboard.domain import authority_models, decision_models, ordering, work_models
from pinboard.domain.errors import (
    ChangedSurface,
    DecisionFailure,
    DecisionFailureCode,
    DecisionResult,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    RetryDisposition,
)
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, HostId, ItemId, LeaseId, TaskId
from pinboard.mcp import contracts
from pinboard.mcp import server as mcp_server
from tests.domain_support import action
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store
from tests.test_proposals import proposal as proposal_input
from tests.work_brief_support import example_work_brief, needs_correction_review, work_a_brief, work_c_brief


def _run_async[Result](operation: Coroutine[None, None, Result]) -> Result:
    return asyncio.run(operation)


def _mcp_arguments(tool: str, request: Mapping[str, contracts.JsonValue]) -> dict[str, contracts.JsonValue]:
    """Encode current protocol envelopes for integration journeys."""
    if tool in {
        mcp_server.ACTIONS_TOOL,
        mcp_server.PREPARATION_AUTHORITY_TOOL,
        mcp_server.ATTEMPT_AUTHORITY_TOOL,
        mcp_server.TRANSITION_TOOL,
        mcp_server.ORDER_TOOL,
        mcp_server.PARALLEL_PREVIEW_TOOL,
    }:
        return {"request": dict(request)}
    return dict(request)


async def _wait_for(event: threading.Event) -> None:
    if not await asyncio.to_thread(event.wait, 2):
        raise AssertionError("A deterministic synchronization point did not complete.")


async def _await_after_ready[Result](
    execution: mcp_server.Execution[Result],
    ready: asyncio.Event,
) -> Result:
    ready.set()
    return await execution.result()


class BoundedExecutorTest(unittest.TestCase):
    def test_worker_and_admission_limits_reject_before_effect(self) -> None:
        async def scenario() -> None:
            executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=3)
            release = threading.Event()
            started = (threading.Event(), threading.Event())
            rejected_effect = threading.Event()

            def blocked(token: mcp_server.CancellationToken, index: int) -> int:
                started[index].set()
                if not release.wait(2):
                    raise AssertionError("The blocking operation was not released.")
                token.checkpoint()
                return index

            first = executor.submit(lambda token: blocked(token, 0))
            second = executor.submit(lambda token: blocked(token, 1))
            await asyncio.gather(*(_wait_for(event) for event in started))
            third = executor.submit(lambda _token: 3)
            with self.assertRaises(mcp_server.ExecutorBusy):
                executor.submit(lambda _token: rejected_effect.set())
            self.assertFalse(rejected_effect.is_set())

            release.set()
            self.assertEqual((0, 1, 3), tuple(await asyncio.gather(first.result(), second.result(), third.result())))
            executor.shutdown()

        _run_async(scenario())

    def test_queued_and_running_cancellation_release_admission(self) -> None:
        async def scenario() -> None:
            executor = mcp_server.BoundedExecutor(worker_count=1, unfinished_limit=2)
            running_started = threading.Event()
            running_release = threading.Event()
            queued_effect = threading.Event()
            cooperative_cancelled = threading.Event()

            def running(token: mcp_server.CancellationToken) -> str:
                running_started.set()
                if not running_release.wait(2):
                    raise AssertionError("The running operation was not released.")
                try:
                    token.checkpoint()
                except mcp_server.OperationCancelled:
                    cooperative_cancelled.set()
                    raise
                return "unexpected"

            active = executor.submit(running)
            await _wait_for(running_started)
            queued = executor.submit(lambda _token: queued_effect.set())
            queued_ready = asyncio.Event()
            queued_waiter = asyncio.create_task(_await_after_ready(queued, queued_ready))
            await queued_ready.wait()
            queued_waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued_waiter

            active_ready = asyncio.Event()
            active_waiter = asyncio.create_task(_await_after_ready(active, active_ready))
            await active_ready.wait()
            active_waiter.cancel()
            await asyncio.sleep(0)
            self.assertFalse(active_waiter.done())
            running_release.set()
            with self.assertRaises(mcp_server.OperationCancelled):
                await active_waiter
            await _wait_for(active.finished)
            await _wait_for(queued.finished)

            self.assertFalse(queued_effect.is_set())
            self.assertTrue(cooperative_cancelled.is_set())
            replacement = executor.submit(lambda token: token.cancelled)
            self.assertFalse(await replacement.result())
            executor.shutdown()

        _run_async(scenario())

    def test_shutdown_joins_workers_and_rejects_new_work(self) -> None:
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=2)
        self.assertEqual("done", _run_async(executor.submit(lambda _token: "done").result()))
        executor.shutdown()
        self.assertFalse(any(thread.name.startswith(mcp_server.THREAD_NAME_PREFIX) for thread in threading.enumerate()))
        with self.assertRaises(mcp_server.ExecutorClosed):
            executor.submit(lambda _token: None)


class McpTransportTest(unittest.TestCase):
    def test_order_and_parallel_ingress_reject_before_resources(self) -> None:
        roots: dict[str, contracts.JsonValue] = {"project_root": "/project", "work_root": "/work"}
        order: dict[str, contracts.JsonValue] = {
            **roots,
            "order": {"schema": "pinboard-live-order/v1", "expected_order": ["work-a"], "requested_order": ["work-a"]},
            "actor_task_id": "owner",
            "actor_host_id": "local",
        }
        selected: dict[str, contracts.JsonValue] = {**roots, "selection": "selected", "item_ids": ["work-a"]}
        with patch.object(mcp_server, "_resolve_durable", side_effect=AssertionError("invalid ingress resolved roots")):
            invalid_orders: tuple[dict[str, contracts.JsonValue], ...] = (
                {**order, "extra": True},
                {**order, "actor_task_id": ""},
                {
                    **order,
                    "order": {
                        "schema": "pinboard-live-order/v1",
                        "expected_order": [],
                        "requested_order": ["work-a", "work-a"],
                    },
                },
            )
            for request in invalid_orders:
                with self.subTest(order=request):
                    result = mcp_server._order({"request": request}, mcp_server.CancellationToken())
                    self.assertEqual("ORDER_INVALID", result.content["code"])
                    contracts.validate_result(mcp_server.ORDER_TOOL, result.content)
            invalid_previews: tuple[dict[str, contracts.JsonValue], ...] = (
                {**selected, "item_ids": []},
                {**selected, "item_ids": ["work-a", "work-a"]},
                {**selected, "item_ids": ["../work-a"]},
                {**selected, "item_ids": ["Work_A"]},
                {**selected, "selection": "all-safe"},
                {**roots, "selection": "selected"},
                {**roots, "selection": "other"},
            )
            for request in invalid_previews:
                with self.subTest(preview=request):
                    result = mcp_server._parallel_preview({"request": request}, mcp_server.CancellationToken())
                    self.assertEqual("PARALLEL_PREVIEW_INVALID", result.content["code"])
                    contracts.validate_result(mcp_server.PARALLEL_PREVIEW_TOOL, result.content)

    def test_order_persists_exact_priority_and_truthful_aftermath(self) -> None:  # noqa: PLR0915 - one persisted order/recovery journey
        with patch("tests.test_mcp.datetime") as seed_clock:
            seed_clock.now.return_value = SQLITE_NOW
            temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        store = SQLiteWorkStore(roots.database_path)
        before = store.validated_snapshot()
        current = tuple(
            item.item_id
            for item in queries.project_current_overview(store.read_project_overview(SQLITE_NOW), SQLITE_NOW).items
        )
        requested = (*current[1:], current[0])
        request: dict[str, contracts.JsonValue] = {
            "project_root": str(project),
            "work_root": str(roots.work_root),
            "order": {
                "schema": "pinboard-live-order/v1",
                "expected_order": list[contracts.JsonValue](current),
                "requested_order": list[contracts.JsonValue](requested),
            },
            "actor_task_id": "priority-owner",
            "actor_host_id": "priority-host",
        }
        with patch.object(mcp_server, "datetime") as clock:
            clock.now.return_value = SQLITE_NOW
            cancelled = mcp_server.CancellationToken()
            cancelled.cancel()
            with self.assertRaises(mcp_server.OperationCancelled):
                mcp_server._order({"request": request}, cancelled)
            self.assertEqual(before, store.validated_snapshot())
            before_write = mcp_server.CancellationToken()

            def cancel_before_write(_durable: DurableRoots) -> SQLiteWorkStore:
                before_write.cancel()
                return store

            with (
                patch.object(mcp_server, "compose_store", side_effect=cancel_before_write),
                self.assertRaises(mcp_server.OperationCancelled),
            ):
                mcp_server._order({"request": request}, before_write)
            self.assertEqual(before, store.validated_snapshot())
            # Cancel after entering the shared write. Commitment still reaches terminal accounting.
            entered = mcp_server.CancellationToken()
            original = mcp_server.service.reorder

            def commit_then_cancel(
                selected_store: WorkStore,
                expected: tuple[ItemId, ...],
                replacement: tuple[ItemId, ...],
                task_id: TaskId,
                host_id: HostId,
                now: datetime,
            ) -> CommittedEffect | DecisionFailure:
                result = original(selected_store, expected, replacement, task_id, host_id, now)
                entered.cancel()
                return result

            with patch.object(mcp_server.service, "reorder", commit_then_cancel):
                committed = mcp_server._order({"request": request}, entered).content
            contracts.validate_result(mcp_server.ORDER_TOOL, committed)
            self.assertEqual(
                ("committed", ["ledger"], "do-not-retry"),
                (committed["status"], committed["changed_surfaces"], committed["retry"]),
            )
            fresh = SQLiteWorkStore(roots.database_path)
            after = fresh.validated_snapshot()
            self.assertEqual(
                requested,
                tuple(
                    item.item_id
                    for item in queries.project_current_overview(
                        fresh.read_project_overview(SQLITE_NOW), SQLITE_NOW
                    ).items
                ),
            )
            self.assertEqual(before.authority, after.authority)
            self.assertEqual(before.lifecycle.attempts, after.lifecycle.attempts)
            self.assertEqual(before.lifecycle.dependencies, after.lifecycle.dependencies)
            self.assertEqual(before.lifecycle.definition_revisions, after.lifecycle.definition_revisions)
            receipt = after.transition_receipts[-1]
            self.assertEqual(
                ("priority-owner", "priority-host", committed["history_id"]),
                (receipt.actor_task_id, receipt.actor_host_id, int(receipt.history_id)),
            )
            stale = mcp_server._order({"request": request}, mcp_server.CancellationToken()).content
            self.assertEqual("ACTION_NOT_AVAILABLE", stale["code"])
            self.assertEqual(after, fresh.validated_snapshot())
            request["order"] = {
                "schema": "pinboard-live-order/v1",
                "expected_order": list[contracts.JsonValue](requested),
                "requested_order": list[contracts.JsonValue](requested[:-1]),
            }
            invalid = mcp_server._order({"request": request}, mcp_server.CancellationToken()).content
            self.assertEqual("TRANSITION_INPUT_INVALID", invalid["code"])
            contracts.validate_result(mcp_server.ORDER_TOOL, invalid)
            self.assertEqual(after, fresh.validated_snapshot())
            request["order"] = {
                "schema": "pinboard-live-order/v1",
                "expected_order": list[contracts.JsonValue](requested),
                "requested_order": list[contracts.JsonValue](requested),
            }
            # Matching fresh order is identical before and after a no-op: it cannot prove our commit.
            prior_overview = queries.project_current_overview(fresh.read_project_overview(SQLITE_NOW), SQLITE_NOW)
            with patch(
                "pinboard.adapters.files.views.atomic_replace",
                side_effect=FileIOError(FileIOErrorCode.VIEW_REFRESH_FAILED, "injected view failure"),
            ):
                warning = mcp_server._order({"request": request}, mcp_server.CancellationToken()).content
            contracts.validate_result(mcp_server.ORDER_TOOL, warning)
            self.assertEqual("committed-with-warning", warning["status"])
            warning_view = msgspec.convert(warning, type=contracts.OrderCommitted)
            self.assertEqual("current-state-only-not-caller-commit-proof", warning_view.recovery.meaning)
            self.assertEqual(
                prior_overview.items,
                queries.project_current_overview(fresh.read_project_overview(SQLITE_NOW), SQLITE_NOW).items,
            )
            self.assertNotEqual(committed["history_id"], warning["history_id"])
            assert warning_view.warning is not None
            self.assertIn(str(roots.work_root), warning_view.warning.recovery)

    def test_order_warning_repairs_views_without_replaying_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            subprocess.run(("git", "init", "--quiet", str(project)), check=True)
            roots = resolve_durable_roots(project)
            initialize_database(roots, SQLITE_NOW)
            with patch(
                "pinboard.adapters.files.views.atomic_replace",
                side_effect=FileIOError(FileIOErrorCode.VIEW_REFRESH_FAILED, "injected view failure"),
            ):
                result = mcp_server._order(
                    {
                        "request": {
                            "project_root": str(project),
                            "work_root": str(roots.work_root),
                            "actor_task_id": "repair-owner",
                            "actor_host_id": "local",
                            "order": {"schema": "pinboard-live-order/v1", "expected_order": [], "requested_order": []},
                        }
                    },
                    mcp_server.CancellationToken(),
                ).content
            view = msgspec.convert(result, type=contracts.OrderCommitted)
            self.assertEqual("committed-with-warning", view.status)
            assert view.warning is not None
            store = SQLiteWorkStore(roots.database_path)
            committed_state = store.validated_snapshot()
            repaired = subprocess.run(
                (sys.executable, "-m", "pinboard", *shlex.split(view.warning.recovery)[1:]),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, repaired.returncode, repaired.stderr)
            self.assertEqual(committed_state, store.validated_snapshot())
            self.assertTrue((roots.work_root / "views/history" / f"{view.history_id}.md").is_file())

    def test_parallel_preview_keeps_focused_scope_wire_shape_and_fixed_time_exclusions(self) -> None:
        with patch("tests.test_mcp.datetime") as seed_clock:
            seed_clock.now.return_value = SQLITE_NOW
            temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        store = SQLiteWorkStore(roots.database_path)
        before = store.validated_snapshot()
        root_arguments: dict[str, contracts.JsonValue] = {
            "project_root": str(project),
            "work_root": str(roots.work_root),
        }
        with patch.object(mcp_server, "datetime") as clock:
            clock.now.return_value = SQLITE_NOW
            with (
                patch.object(
                    SQLiteWorkStore,
                    "read_current_parallel_snapshot",
                    side_effect=AssertionError("selected read portfolio"),
                ),
                patch(
                    "pinboard.adapters.sqlite.state.read_state",
                    side_effect=AssertionError("selected read complete state"),
                ),
            ):
                for item_id, code in (
                    ("work-c", None),
                    ("intake-work", "state-not-launchable"),
                    ("zz-proposal-a", "state-not-launchable"),
                    ("work-a", "dependency-live"),
                ):
                    with self.subTest(item=item_id):
                        result = mcp_server._parallel_preview(
                            {"request": {**root_arguments, "selection": "selected", "item_ids": [item_id]}},
                            mcp_server.CancellationToken(),
                        ).content
                        contracts.validate_result(mcp_server.PARALLEL_PREVIEW_TOOL, result)
                        preview = queries.select_parallel_preview(store, selected=(item_id,), now=SQLITE_NOW)
                        assert isinstance(preview, query_models.ParallelPreview)
                        self.assertEqual(
                            msgspec.to_builtins(queries.present_parallel_preview(preview)),
                            {
                                key: result[key]
                                for key in ("schema", "revision", "selection", "safe", "launchable", "excluded")
                            },
                        )
                        self.assertEqual(code is None, result["safe"])
                        if code is not None:
                            view = msgspec.convert(result, type=contracts.ParallelPreviewSuccess)
                            self.assertEqual(code, view.excluded[0].reasons[0].code.value)
                for item_id in ("missing-item", "work-b"):
                    rejected = mcp_server._parallel_preview(
                        {"request": {**root_arguments, "selection": "selected", "item_ids": [item_id]}},
                        mcp_server.CancellationToken(),
                    ).content
                    self.assertEqual("PARALLEL_SELECTION_INVALID", rejected["code"])
                    contracts.validate_result(mcp_server.PARALLEL_PREVIEW_TOOL, rejected)
            clock.now.return_value = SQLITE_NOW + timedelta(minutes=5)
            expired = mcp_server._parallel_preview(
                {"request": {**root_arguments, "selection": "selected", "item_ids": ["work-a"]}},
                mcp_server.CancellationToken(),
            ).content
            self.assertFalse(expired["safe"])
            with (
                patch.object(
                    SQLiteWorkStore,
                    "read_parallel_preview",
                    side_effect=AssertionError("all-safe used selected reader"),
                ),
                patch(
                    "pinboard.adapters.sqlite.state.read_state",
                    side_effect=AssertionError("all-safe read retained state"),
                ),
                patch(
                    "pinboard.adapters.sqlite.decision_reads._read_selected_proposal",
                    side_effect=AssertionError("all-safe read proposal bodies"),
                ),
            ):
                all_safe = mcp_server._parallel_preview(
                    {"request": {**root_arguments, "selection": "all-safe"}}, mcp_server.CancellationToken()
                ).content
            self.assertTrue(all_safe["safe"])
            all_safe_view = msgspec.convert(all_safe, type=contracts.ParallelPreviewSuccess)
            self.assertEqual(("work-c",), tuple(item.item_id for item in all_safe_view.launchable))
            self.assertEqual(before, store.validated_snapshot())

    def test_parallel_preview_preserves_independent_precedence_and_expiry(self) -> None:
        with patch("tests.test_mcp.datetime") as seed_clock:
            seed_clock.now.return_value = SQLITE_NOW
            temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        facts = SQLiteWorkStore(roots.database_path).read_parallel_preview((ItemId("work-c"),))
        assert facts is not None
        ready = facts.items[0]
        live_preparation = query_models.ParallelPreparationFacts(
            authority_models.PreparationLeaseStatus.ACTIVE, SQLITE_NOW + timedelta(minutes=5)
        )
        owned_attempt = query_models.ParallelAttemptFacts(
            AttemptId("work-c-1"),
            work_models.AttemptState.ACTIVE,
            authority_models.AttemptLeaseStatus.ACTIVE,
            SQLITE_NOW + timedelta(minutes=5),
        )
        cases = (
            (
                replace(
                    ready,
                    state=work_models.WorkState.INTAKE,
                    preparation=live_preparation,
                    live_dependencies=(ItemId("work-a"),),
                ),
                query_models.ParallelReasonCode.STATE_NOT_LAUNCHABLE,
            ),
            (
                replace(ready, preparation=live_preparation, live_dependencies=(ItemId("work-a"),)),
                query_models.ParallelReasonCode.PREPARATION_OWNED,
            ),
            (
                replace(
                    ready,
                    preparation=replace(live_preparation, expires_at=SQLITE_NOW),
                    live_dependencies=(ItemId("work-a"),),
                ),
                query_models.ParallelReasonCode.DEPENDENCY_LIVE,
            ),
            (
                replace(ready, state=work_models.WorkState.ACTIVE, attempt=owned_attempt),
                query_models.ParallelReasonCode.ATTEMPT_OWNED,
            ),
            (
                replace(
                    ready,
                    state=work_models.WorkState.ACTIVE,
                    attempt=replace(owned_attempt, authority_expires_at=SQLITE_NOW),
                ),
                None,
            ),
            (
                replace(
                    ready,
                    preparation=replace(live_preparation, status=authority_models.PreparationLeaseStatus.RELEASED),
                ),
                None,
            ),
        )
        request: dict[str, contracts.JsonValue] = {
            "request": {
                "project_root": str(project),
                "work_root": str(roots.work_root),
                "selection": "selected",
                "item_ids": ["work-c"],
            }
        }
        with patch.object(mcp_server, "datetime") as clock:
            clock.now.return_value = SQLITE_NOW
            for item, reason in cases:
                with (
                    self.subTest(reason=reason),
                    patch.object(SQLiteWorkStore, "read_parallel_preview", return_value=replace(facts, items=(item,))),
                ):
                    view = msgspec.convert(
                        mcp_server._parallel_preview(request, mcp_server.CancellationToken()).content,
                        type=contracts.ParallelPreviewSuccess,
                    )
                    self.assertEqual(reason is None, view.safe)
                    self.assertEqual(
                        () if reason is None else (reason,),
                        tuple(value.code for excluded in view.excluded for value in excluded.reasons),
                    )

    def test_order_competing_workers_and_empty_board_preserve_receipts(self) -> None:
        with patch("tests.test_mcp.datetime") as seed_clock:
            seed_clock.now.return_value = SQLITE_NOW
            temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        store = SQLiteWorkStore(roots.database_path)
        current = tuple(
            item.item_id
            for item in queries.project_current_overview(store.read_project_overview(SQLITE_NOW), SQLITE_NOW).items
        )
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=2)
        self.addCleanup(executor.shutdown)
        locked, contender_started = threading.Event(), threading.Event()
        original = mcp_server.service.decide_order

        def controlled_decision(
            observed: tuple[ItemId, ...], expected: tuple[ItemId, ...], requested: tuple[ItemId, ...]
        ) -> DecisionResult[ordering.OrderChange]:
            if requested == current[::-1]:
                locked.set()
                if not contender_started.wait(2):
                    raise AssertionError("Contender did not start while the winner owned the write lock")
            return original(observed, expected, requested)

        def request_for(requested: tuple[str, ...]) -> dict[str, contracts.JsonValue]:
            return {
                "request": {
                    "project_root": str(project),
                    "work_root": str(roots.work_root),
                    "actor_task_id": "owner",
                    "actor_host_id": "local",
                    "order": {
                        "schema": "pinboard-live-order/v1",
                        "expected_order": list[contracts.JsonValue](current),
                        "requested_order": list[contracts.JsonValue](requested),
                    },
                }
            }

        def contender(token: mcp_server.CancellationToken) -> mcp_server.OperationResult:
            contender_started.set()
            return mcp_server._order(request_for((*current[1:], current[0])), token)

        async def scenario() -> None:
            winner = executor.submit(lambda token: mcp_server._order(request_for(current[::-1]), token))
            await _wait_for(locked)
            loser = executor.submit(contender)
            results = await asyncio.gather(winner.result(), loser.result())
            self.assertEqual(("committed", "rejected"), tuple(result.content["status"] for result in results))
            self.assertEqual("ACTION_NOT_AVAILABLE", results[1].content["code"])

        with (
            patch.object(mcp_server.service, "decide_order", controlled_decision),
            patch.object(mcp_server, "datetime") as clock,
        ):
            clock.now.return_value = SQLITE_NOW
            _run_async(scenario())
            empty = project / "empty"
            empty.mkdir()
            subprocess.run(("git", "init", "--quiet", str(empty)), check=True)
            empty_roots = resolve_durable_roots(empty)
            initialize_database(empty_roots, SQLITE_NOW)
            result = mcp_server._order(
                {
                    "request": {
                        "project_root": str(empty),
                        "work_root": str(empty_roots.work_root),
                        "actor_task_id": "empty-owner",
                        "actor_host_id": "other-host",
                        "order": {"schema": "pinboard-live-order/v1", "expected_order": [], "requested_order": []},
                    }
                },
                mcp_server.CancellationToken(),
            ).content
            committed = msgspec.convert(result, type=contracts.OrderCommitted)
            fresh = SQLiteWorkStore(empty_roots.database_path).validated_snapshot()
            self.assertEqual((), committed.order)
            self.assertEqual(
                ("empty-owner", committed.history_id),
                (fresh.transition_receipts[-1].actor_task_id, int(fresh.transition_receipts[-1].history_id)),
            )
            original_state = store.validated_snapshot()
            self.assertEqual(
                current[::-1],
                tuple(
                    item.item_id
                    for item in queries.project_current_overview(
                        store.read_project_overview(SQLITE_NOW), SQLITE_NOW
                    ).items
                ),
            )
            self.assertEqual(2, len(original_state.transition_receipts))

    def test_negotiated_tools_have_native_compatible_roots_and_wrapped_reads(self) -> None:
        executor = mcp_server.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(io.StringIO(), event_limit=4, line_limit=256)
        )

        async def scenario() -> None:
            tools = await server.list_tools()
            self.assertEqual(18, len(tools))
            for tool in tools:
                with self.subTest(tool=tool.name):
                    self.assertEqual("object", tool.input_schema["type"])
                    self.assertFalse({"anyOf", "oneOf", "allOf"} & tool.input_schema.keys())
            temporary, project, roots = self._project()
            self.addCleanup(temporary.cleanup)
            result = await server.call_tool(
                mcp_server.ATTEMPT_AUTHORITY_TOOL,
                {
                    "request": {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "operation": "status",
                        "attempt_id": "work-a-1",
                    }
                },
            )
            assert isinstance(result, CallToolResult) and isinstance(result.structured_content, dict)
            self.assertEqual("present", result.structured_content["status"])
            self.assertFalse(result.structured_content["state_changed"])

        _run_async(scenario())

    def test_definition_and_negative_review_tools_persist_and_reload_exact_facts(self) -> None:  # noqa: PLR0915 - complete persisted query and review journey
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        executor = mcp_server.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(io.StringIO(), event_limit=4, line_limit=256)
        )
        explicit_roots = {"project_root": str(project), "work_root": str(roots.work_root)}
        brief = work_a_brief(project)
        review = msgspec.json.decode(
            needs_correction_review(brief), type=work_brief_models.WorkBriefReviewNeedsCorrection
        )

        async def call(tool: str, request: dict[str, contracts.JsonValue]) -> dict[str, contracts.JsonValue]:
            result = await server.call_tool(tool, {"request": {**explicit_roots, **request}})
            assert isinstance(result, CallToolResult) and isinstance(result.structured_content, dict)
            self.assertFalse(result.is_error)
            return result.structured_content

        async def scenario() -> None:  # noqa: PLR0915 - accepted full definition/history and review boundary journey
            current = await call(mcp_server.ITEM_DEFINITION_TOOL, {"operation": "current", "item_id": "work-c"})
            for number in (2, 3):
                definition = current["definition"]
                assert isinstance(definition, dict)
                revised = await call(
                    mcp_server.TRANSITION_TOOL,
                    {
                        "role": "project",
                        "actor_task_id": "revision-owner",
                        "actor_host_id": "local",
                        "receipt": {
                            "action_id": {"kind": "revise-item", "subject": "work-c"},
                            "subject_revision": str(current["item_subject_revision"]),
                        },
                        "payload": {
                            "schema": "pinboard-item-revision/v1",
                            "item_id": "work-c",
                            "expected_revision": current["definition_revision"],
                            "expected_digest": current["definition_digest"],
                            "source_task": "revision-owner",
                            "reason": f"Clarify outcome {number}.",
                            "definition": {**definition, "objective": f"Observable outcome {number}."},
                        },
                    },
                )
                self.assertEqual("committed", revised["status"])
                current = await call(mcp_server.ITEM_DEFINITION_TOOL, {"operation": "current", "item_id": "work-c"})
            reopened = SQLiteWorkStore(roots.database_path)
            expected = queries.select_item_definition(reopened, ItemId("work-c"))
            self.assertEqual(msgspec.json.decode(msgspec.json.encode(expected)), current)
            first = await call(
                mcp_server.ITEM_DEFINITION_TOOL,
                {
                    "operation": "history",
                    "item_id": "work-c",
                    "limit": 2,
                    "before_revision": None,
                },
            )
            second = await call(
                mcp_server.ITEM_DEFINITION_TOOL,
                {
                    "operation": "history",
                    "item_id": "work-c",
                    "limit": 2,
                    "before_revision": first["next_before_revision"],
                },
            )
            first_rows = msgspec.convert(first, type=query_models.ItemDefinitionHistory).revisions
            second_rows = msgspec.convert(second, type=query_models.ItemDefinitionHistory).revisions
            self.assertEqual([3, 2], [row.revision for row in first_rows])
            self.assertEqual([1], [row.revision for row in second_rows])
            self.assertIsNone(second["next_before_revision"])
            self.assertEqual(first_rows[0].before_digest, first_rows[1].after_digest)
            self.assertEqual(first_rows[1].before_digest, second_rows[0].digest)
            self.assertEqual("revision-owner", first_rows[0].source_task)
            for page, cursor in ((first, None), (second, 2)):
                self.assertEqual(
                    msgspec.json.decode(
                        msgspec.json.encode(
                            queries.select_item_definition_history(
                                reopened,
                                ItemId("work-c"),
                                limit=2,
                                before_revision=cursor,
                            )
                        )
                    ),
                    page,
                )
            self.assertEqual(
                "ITEM_NOT_FOUND",
                (
                    await call(
                        mcp_server.ITEM_DEFINITION_TOOL,
                        {
                            "operation": "current",
                            "item_id": "missing",
                        },
                    )
                )["code"],
            )
            status_request: dict[str, contracts.JsonValue] = {"operation": "status", "brief_artifact_ref_id": 1}
            absent = await call(mcp_server.BRIEF_REVIEW_TOOL, status_request)
            self.assertEqual("no-needs-correction-evidence", absent["status"])
            self.assertEqual(msgspec.json.decode(canonical_work_brief_bytes(brief)), absent["brief"])
            before = reopened.validated_snapshot()
            publication_request: dict[str, contracts.JsonValue] = {
                "operation": "publish",
                "brief_artifact_ref_id": 1,
                "review": msgspec.to_builtins(review),
            }
            published = await call(mcp_server.BRIEF_REVIEW_TOOL, publication_request)
            self.assertEqual("committed", published["status"])
            self.assertEqual(
                ["immutable-artifact", "accepted-artifact-reference", "ledger"], published["changed_surfaces"]
            )
            found = await call(mcp_server.BRIEF_REVIEW_TOOL, status_request)
            self.assertEqual("needs-correction", found["status"])
            self.assertEqual(msgspec.json.decode(needs_correction_review(brief)), found["review"])
            self.assertEqual(published["reference"], found["reference"])
            after = SQLiteWorkStore(roots.database_path).validated_snapshot()
            self.assertEqual(before.lifecycle.work_items, after.lifecycle.work_items)
            self.assertEqual(before.lifecycle.attempts, after.lifecycle.attempts)
            self.assertEqual(before.authority, after.authority)
            self.assertEqual("unchanged", (await call(mcp_server.BRIEF_REVIEW_TOOL, publication_request))["status"])
            self.assertEqual(after, SQLiteWorkStore(roots.database_path).validated_snapshot())
            latest = replace_struct(review, artifact_revision=2)
            publication_request["review"] = msgspec.to_builtins(latest)
            await call(mcp_server.BRIEF_REVIEW_TOOL, publication_request)
            latest_status = await call(mcp_server.BRIEF_REVIEW_TOOL, status_request)
            self.assertEqual(
                msgspec.json.decode(work_briefs.canonical_work_brief_review_needs_correction_bytes(latest)),
                latest_status["review"],
            )
            corrected = replace_struct(brief, artifact_revision=2, title="Corrected accepted brief")
            accepted_corrected = work_briefs.publish_work_brief(
                reopened, ArtifactRepository(roots), corrected, SQLITE_NOW
            )
            assert not isinstance(accepted_corrected, DecisionFailure)
            corrected_status = await call(
                mcp_server.BRIEF_REVIEW_TOOL,
                {
                    "operation": "status",
                    "brief_artifact_ref_id": int(accepted_corrected.reference.artifact_ref_id),
                },
            )
            self.assertEqual("no-needs-correction-evidence", corrected_status["status"])
            self.assertEqual(
                "WORK_BRIEF_INVALID",
                (
                    await call(
                        mcp_server.BRIEF_REVIEW_TOOL,
                        {
                            "operation": "status",
                            "brief_artifact_ref_id": 999,
                        },
                    )
                )["code"],
            )

        _run_async(scenario())

    def test_new_request_leaves_reject_before_roots_or_storage(self) -> None:
        roots: dict[str, contracts.JsonValue] = {"project_root": "/project", "work_root": "/work"}
        requests: tuple[
            tuple[
                str,
                Callable[[dict[str, contracts.JsonValue], mcp_server.CancellationToken], mcp_server.OperationResult],
                dict[str, contracts.JsonValue],
                tuple[dict[str, contracts.JsonValue], ...],
            ],
            ...,
        ] = (
            (
                mcp_server.ITEM_DEFINITION_TOOL,
                mcp_server._read_item_definition,
                {**roots, "operation": "current", "item_id": "item"},
                ({"limit": 1}, {"before_revision": None}),
            ),
            (
                mcp_server.ITEM_DEFINITION_TOOL,
                mcp_server._read_item_definition,
                {**roots, "operation": "history", "item_id": "item", "limit": 1, "before_revision": None},
                (
                    {"limit": 0},
                    {"limit": 101},
                    {"limit": True},
                    {"limit": "1"},
                    {"before_revision": 0},
                    {"before_revision": True},
                ),
            ),
            (
                mcp_server.BRIEF_REVIEW_TOOL,
                mcp_server._brief_review,
                {**roots, "operation": "status", "brief_artifact_ref_id": 1},
                ({"review": {}}, {"brief_artifact_ref_id": True}),
            ),
            (
                mcp_server.BRIEF_REVIEW_TOOL,
                mcp_server._brief_review,
                {**roots, "operation": "publish", "brief_artifact_ref_id": 1, "review": {}},
                ({},),
            ),
        )
        for tool, handler, request, changes in requests:
            invalid: list[dict[str, contracts.JsonValue]] = [
                request,
                {"request": request, "unknown": True},
                {"request": {**request, "unknown": True}},
            ]
            invalid.extend({"request": {**request, **change}} for change in changes)
            for raw in invalid:
                with self.subTest(raw=raw), patch.object(mcp_server, "_resolve_durable") as resolve:
                    result = handler(raw, mcp_server.CancellationToken())
                    self.assertEqual("rejected", result.content["status"])
                    self.assertEqual([], result.content["changed_surfaces"])
                    contracts.validate_result(tool, result.content)
                    resolve.assert_not_called()

    def test_negative_review_rejections_and_irreversible_publication_aftermath(self) -> None:  # noqa: PLR0915 - rejection and immutable-publication fault matrix
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        store = SQLiteWorkStore(roots.database_path)
        brief = work_a_brief(project)
        review = msgspec.json.decode(
            needs_correction_review(brief), type=work_brief_models.WorkBriefReviewNeedsCorrection
        )
        base: dict[str, contracts.JsonValue] = {
            "project_root": str(project),
            "work_root": str(roots.work_root),
            "operation": "publish",
            "brief_artifact_ref_id": 1,
        }
        before = store.validated_snapshot()
        for changed, expected in (
            (replace_struct(review, reviewer_task_id=brief.owner_task_id), "WORK_BRIEF_REVIEW_NOT_INDEPENDENT"),
            (replace_struct(review, accepted_brief_sha256="f" * 64), "WORK_BRIEF_REVIEW_STALE"),
            (replace_struct(review, checkpoint_sha256="f" * 64), "WORK_BRIEF_REVIEW_STALE"),
            (replace_struct(review, reviewed_authority_set_sha256="f" * 64), "WORK_BRIEF_REVIEW_STALE"),
            (replace_struct(review, checkpoint_id="other-checkpoint"), "WORK_BRIEF_REVIEW_INVALID"),
        ):
            result = mcp_server._brief_review(
                {"request": {**base, "review": msgspec.to_builtins(changed)}}, mcp_server.CancellationToken()
            )
            self.assertEqual(expected, result.content["code"])
            contracts.validate_result(mcp_server.BRIEF_REVIEW_TOOL, result.content)
            self.assertEqual(before, store.validated_snapshot())
        local = replace_struct(
            brief,
            artifact_revision=2,
            checkpoint=work_brief_models.LocalCheckpoint(
                brief.checkpoint.checkpoint_id,
                brief.checkpoint.title,
                brief.checkpoint.architecture_impact,
                brief.checkpoint.outcome_description,
                brief.checkpoint.acceptance_criteria,
                brief.checkpoint.verification,
                brief.checkpoint.deferrals,
            ),
        )
        published_local = work_briefs.publish_work_brief(store, ArtifactRepository(roots), local, SQLITE_NOW)
        assert not isinstance(published_local, DecisionFailure)
        for operation in ("status", "publish"):
            request: dict[str, contracts.JsonValue] = {
                **base,
                "operation": operation,
                "brief_artifact_ref_id": int(published_local.reference.artifact_ref_id),
            }
            if operation == "publish":
                request["review"] = msgspec.to_builtins(review)
            result = mcp_server._brief_review({"request": request}, mcp_server.CancellationToken())
            self.assertEqual("WORK_BRIEF_REVIEW_INVALID", result.content["code"])
        raw: dict[str, contracts.JsonValue] = {"request": {**base, "review": msgspec.to_builtins(review)}}
        with patch.object(
            SQLiteWorkStore, "accept_artifact_reference", side_effect=WorkStoreError("acceptance unavailable")
        ):
            failed = mcp_server._brief_review(raw, mcp_server.CancellationToken())
        self.assertEqual("failed-after-publication", failed.content["status"])
        self.assertEqual(["immutable-artifact"], failed.content["changed_surfaces"])
        self.assertEqual("do-not-retry", failed.content["retry"])
        contracts.validate_result(mcp_server.BRIEF_REVIEW_TOOL, failed.content)
        second = replace_struct(review, artifact_revision=2)
        second_raw: dict[str, contracts.JsonValue] = {"request": {**base, "review": msgspec.to_builtins(second)}}
        with patch.object(
            SQLiteWorkStore,
            "accept_artifact_reference",
            return_value=DecisionFailure(
                DecisionFailureCode.ACTION_NOT_AVAILABLE,
                "Acceptance rejected.",
                None,
            ),
        ):
            rejected = mcp_server._brief_review(second_raw, mcp_server.CancellationToken())
        self.assertEqual("rejected", rejected.content["status"])
        self.assertTrue(rejected.content["state_changed"])
        self.assertEqual(["immutable-artifact"], rejected.content["changed_surfaces"])
        contracts.validate_result(mcp_server.BRIEF_REVIEW_TOOL, rejected.content)
        adopted = mcp_server._brief_review(raw, mcp_server.CancellationToken())
        self.assertEqual(["accepted-artifact-reference", "ledger"], adopted.content["changed_surfaces"])
        contracts.validate_result(mcp_server.BRIEF_REVIEW_TOOL, adopted.content)
        stable = store.validated_snapshot()
        different = replace_struct(review, reviewer_task_id="another-independent-reviewer")
        with self.assertRaises(UnexpectedToolError):
            executor = mcp_server.BoundedExecutor(worker_count=1, unfinished_limit=1)
            self.addCleanup(executor.shutdown)
            server = mcp_server.create_server(
                executor, mcp_server.Diagnostics(io.StringIO(), event_limit=2, line_limit=256)
            )
            _run_async(
                server.call_tool(
                    mcp_server.BRIEF_REVIEW_TOOL, {"request": {**base, "review": msgspec.to_builtins(different)}}
                )
            )
        self.assertEqual(stable, store.validated_snapshot())
        status = mcp_server._brief_review({"request": {**base, "operation": "status"}}, mcp_server.CancellationToken())
        self.assertEqual("needs-correction", status.content["status"])
        selector = failed.content["published_selector"]
        assert isinstance(selector, str)
        (roots.work_root / selector).write_bytes(b"{}\n")
        with self.assertRaises(mcp_server.ArtifactError):
            mcp_server._brief_review({"request": {**base, "operation": "status"}}, mcp_server.CancellationToken())

    def test_request_envelopes_reject_mixed_fields_before_resources(self) -> None:
        roots: dict[str, contracts.JsonValue] = {"project_root": "/project", "work_root": "/work"}
        cases: tuple[
            tuple[
                Callable[[dict[str, contracts.JsonValue], mcp_server.CancellationToken], mcp_server.OperationResult],
                dict[str, contracts.JsonValue],
                tuple[dict[str, contracts.JsonValue], ...],
            ],
            ...,
        ] = (
            (
                mcp_server._read_actions,
                {**roots, "role": "project"},
                (
                    {"lease_id": None},
                    {"lease_id": "worker", "generation": 1},
                ),
            ),
            (
                mcp_server._read_actions,
                {**roots, "role": "worker", "lease_id": "worker", "generation": 1},
                (
                    {"generation": True},
                    {"generation": "1"},
                    {"generation": 0},
                    {"lease_id": None},
                ),
            ),
            (
                mcp_server._preparation_authority,
                {**roots, "operation": "release", "item_id": "item", "lease_id": "lease", "generation": 1},
                (
                    {"ttl_seconds": 60},
                    {"actor_task_id": "project", "actor_host_id": "local"},
                ),
            ),
            (
                mcp_server._attempt_authority,
                {
                    **roots,
                    "operation": "renew",
                    "attempt_id": "attempt",
                    "lease_id": "lease",
                    "generation": 1,
                    "ttl_seconds": 60,
                },
                (
                    {"actor_task_id": "project", "actor_host_id": "local"},
                    {"ttl_seconds": None},
                    {"ttl_seconds": 0},
                ),
            ),
            (
                mcp_server._transition,
                {
                    **roots,
                    "role": "project",
                    "receipt": {"action_id": {"kind": "pause", "subject": "attempt"}, "subject_revision": "1"},
                    "payload": {"reason": "Pause."},
                    "actor_task_id": "project",
                    "actor_host_id": "local",
                },
                (
                    {"role": "observer"},
                    {"lease_id": "worker", "generation": 1},
                    {"actor_host_id": None},
                ),
            ),
        )
        for handler, request, changes in cases:
            unknown_inner: dict[str, contracts.JsonValue] = {**request, "unknown": True}
            invalid: list[dict[str, contracts.JsonValue]] = [
                request,
                {"request": request, "unknown": True},
                {"request": unknown_inner},
            ]
            for change in changes:
                inner: dict[str, contracts.JsonValue] = {**request, **change}
                invalid.append({"request": inner})
            for raw in invalid:
                with (
                    self.subTest(handler=handler.__name__, raw=raw),
                    patch.object(mcp_server, "_resolve_durable") as resolve,
                    patch.object(mcp_server, "resolve_source_checkout_root") as source,
                ):
                    result = handler(raw, mcp_server.CancellationToken())
                    self.assertEqual("rejected", result.content["status"])
                    self.assertFalse(result.content["state_changed"])
                    self.assertEqual([], result.content["changed_surfaces"])
                    resolve.assert_not_called()
                    source.assert_not_called()

    def _project(self) -> tuple[tempfile.TemporaryDirectory[str], Path, DurableRoots]:
        temporary = tempfile.TemporaryDirectory()
        project = Path(temporary.name).resolve()
        subprocess.run(("git", "init", "--quiet", str(project)), check=True)
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        brief_bytes = canonical_work_brief_bytes(work_a_brief(project))
        published = (
            ArtifactRepository(roots)
            .publish(NewArtifact(work_models.ArtifactKind.BRIEF, "work-a-brief", 1, ".opaque", brief_bytes))
            .reference
        )
        state = complete_sqlite_state()
        observed_at = datetime.now(UTC)
        authority = replace(
            state.authority,
            attempt_leases=tuple(
                replace(lease, acquired_at=observed_at, expires_at=observed_at + timedelta(minutes=5))
                for lease in state.authority.attempt_leases
            ),
        )
        prior_reference = state.artifact_references[0]
        brief_reference = stored_state.ArtifactReference(
            prior_reference.artifact_ref_id,
            published.key,
            published.revision,
            published.kind,
            published.selector,
            published.content_sha256,
            published.size_bytes,
            prior_reference.accepted_revision,
            prior_reference.created_at,
        )
        initialize_store(
            SQLiteWorkStore(roots.database_path),
            replace(
                state,
                artifact_references=(brief_reference, *state.artifact_references[1:]),
                authority=authority,
            ),
        )
        return temporary, project, roots

    def test_stdio_authority_and_transition_tools_persist_exact_lifecycle_results(self) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        parameters = StdioServerParameters(command=sys.executable, args=("-m", "pinboard.mcp"))

        async def scenario() -> tuple[CallToolResult, ...]:
            async with (
                stdio_client(parameters) as streams,
                ClientSession(*streams) as session,
            ):
                await session.initialize()
                preparation_status = await session.call_tool(
                    mcp_server.PREPARATION_AUTHORITY_TOOL,
                    _mcp_arguments(
                        mcp_server.PREPARATION_AUTHORITY_TOOL,
                        {
                            "project_root": str(project),
                            "work_root": str(roots.work_root),
                            "operation": "status",
                            "item_id": "work-c",
                        },
                    ),
                )
                preparation_start = await session.call_tool(
                    mcp_server.PREPARATION_AUTHORITY_TOOL,
                    _mcp_arguments(
                        mcp_server.PREPARATION_AUTHORITY_TOOL,
                        {
                            "project_root": str(project),
                            "work_root": str(roots.work_root),
                            "operation": "start",
                            "item_id": "work-c",
                            "task_id": "preparer-task",
                            "host_id": "local",
                            "ttl_seconds": 600,
                        },
                    ),
                )
                started = preparation_start.structured_content
                if not isinstance(started, dict):
                    raise AssertionError("Preparation start did not return structured content.")
                preparation_renew = await session.call_tool(
                    mcp_server.PREPARATION_AUTHORITY_TOOL,
                    _mcp_arguments(
                        mcp_server.PREPARATION_AUTHORITY_TOOL,
                        {
                            "project_root": str(project),
                            "work_root": str(roots.work_root),
                            "operation": "renew",
                            "item_id": "work-c",
                            "lease_id": started["lease_id"],
                            "generation": started["generation"],
                            "ttl_seconds": 1200,
                        },
                    ),
                )
                renewed = preparation_renew.structured_content
                if not isinstance(renewed, dict):
                    raise AssertionError("Preparation renewal did not return structured content.")
                preparation_release = await session.call_tool(
                    mcp_server.PREPARATION_AUTHORITY_TOOL,
                    _mcp_arguments(
                        mcp_server.PREPARATION_AUTHORITY_TOOL,
                        {
                            "project_root": str(project),
                            "work_root": str(roots.work_root),
                            "operation": "release",
                            "item_id": "work-c",
                            "lease_id": renewed["lease_id"],
                            "generation": renewed["generation"],
                        },
                    ),
                )
                attempt_status = await session.call_tool(
                    mcp_server.ATTEMPT_AUTHORITY_TOOL,
                    _mcp_arguments(
                        mcp_server.ATTEMPT_AUTHORITY_TOOL,
                        {
                            "project_root": str(project),
                            "work_root": str(roots.work_root),
                            "operation": "status",
                            "attempt_id": "work-a-1",
                        },
                    ),
                )
                invalid_payload = await session.call_tool(
                    mcp_server.TRANSITION_TOOL,
                    _mcp_arguments(
                        mcp_server.TRANSITION_TOOL,
                        {
                            "project_root": str(project),
                            "work_root": str(roots.work_root),
                            "role": "project",
                            "receipt": {
                                "action_id": {"kind": "pause", "subject": "work-a-1"},
                                "subject_revision": "8",
                            },
                            "payload": {"reason": "Invalid leaf.", "unknown": True},
                            "actor_task_id": "project-task",
                            "actor_host_id": "local",
                        },
                    ),
                )
                transition = await session.call_tool(
                    mcp_server.TRANSITION_TOOL,
                    _mcp_arguments(
                        mcp_server.TRANSITION_TOOL,
                        {
                            "project_root": str(project),
                            "work_root": str(roots.work_root),
                            "role": "project",
                            "receipt": {
                                "action_id": {"kind": "pause", "subject": "work-a-1"},
                                "subject_revision": "8",
                            },
                            "payload": {"reason": "Pause through the MCP lifecycle boundary."},
                            "actor_task_id": "project-task",
                            "actor_host_id": "local",
                        },
                    ),
                )
                stale = await session.call_tool(
                    mcp_server.TRANSITION_TOOL,
                    _mcp_arguments(
                        mcp_server.TRANSITION_TOOL,
                        {
                            "project_root": str(project),
                            "work_root": str(roots.work_root),
                            "role": "project",
                            "receipt": {
                                "action_id": {"kind": "pause", "subject": "work-a-1"},
                                "subject_revision": "8",
                            },
                            "payload": {"reason": "This stale receipt must not commit."},
                            "actor_task_id": "project-task",
                            "actor_host_id": "local",
                        },
                    ),
                )
                return (
                    preparation_status,
                    preparation_start,
                    preparation_renew,
                    preparation_release,
                    attempt_status,
                    invalid_payload,
                    transition,
                    stale,
                )

        results = _run_async(scenario())
        for result in results:
            self.assertFalse(result.is_error)
            self.assertIsInstance(result.structured_content, dict)
        contents = tuple(result.structured_content for result in results)
        self.assertEqual("absent", contents[0]["status"])
        self.assertEqual(("committed", "committed", "committed"), tuple(value["status"] for value in contents[1:4]))
        self.assertEqual((1, 1, 2), tuple(value["generation"] for value in contents[1:4]))
        self.assertEqual("released", contents[3]["authority_status"])
        self.assertEqual("present", contents[4]["status"])
        self.assertEqual("rejected", contents[5]["status"])
        self.assertEqual("TRANSITION_INPUT_INVALID", contents[5]["code"])
        self.assertIn(contents[6]["status"], {"committed", "committed-with-warning"})
        self.assertEqual("rejected", contents[7]["status"])
        self.assertFalse(contents[7]["state_changed"])

        reopened = SQLiteWorkStore(roots.database_path)
        preparation = reopened.read_preparation_authority_status(ItemId("work-c"))
        self.assertIsNotNone(preparation)
        assert preparation is not None
        self.assertEqual("released", preparation.status.value)
        attempt = reopened.read_attempt_context(AttemptId("work-a-1"))
        self.assertIsInstance(attempt, query_models.NonterminalAttemptContextFacts)
        assert isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        self.assertEqual("paused", attempt.state.value)

    def test_transition_request_contract_has_only_exact_mutating_leaves(self) -> None:
        schema = contracts.transition_request_schema()
        encoded_schema = msgspec.json.encode(schema)
        properties = schema["properties"]
        assert isinstance(properties, dict) and isinstance(properties["request"], dict)
        leaves = properties["request"]["oneOf"]
        assert isinstance(leaves, list)
        self.assertEqual(23, len(leaves))
        for advisory_kind in (b'"continue"', b'"dispatch"', b'"inspect"', b'"report-blocker"'):
            self.assertNotIn(advisory_kind, encoded_schema)

    def test_review_submission_expired_during_publication_preserves_artifact_without_ledger_commit(self) -> None:
        with patch("tests.test_mcp.datetime") as fixture_clock:
            fixture_clock.now.return_value = SQLITE_NOW
            temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        subprocess.run(("git", "-C", str(project), "checkout", "-b", "codex/work-a"), check=True, capture_output=True)
        subprocess.run(
            (
                "git",
                "-C",
                str(project),
                "-c",
                "user.name=MCP test",
                "-c",
                "user.email=mcp@example.invalid",
                "commit",
                "--allow-empty",
                "-m",
                "Accepted base",
            ),
            check=True,
            capture_output=True,
        )
        candidate = mcp_server.lifecycle_artifacts.read_working_tree_candidate(project).identity
        store = SQLiteWorkStore(roots.database_path)
        before = store.validated_snapshot()
        original_publish = ArtifactRepository.publish
        publication: ArtifactPublication | None = None
        decision_time = SQLITE_NOW

        def publish_then_expire(repository: ArtifactRepository, artifact: NewArtifact) -> ArtifactPublication:
            nonlocal publication, decision_time
            publication = original_publish(repository, artifact)
            decision_time = SQLITE_NOW + timedelta(minutes=5, seconds=1)
            return publication

        def current_time(_timezone: timezone) -> datetime:
            return decision_time

        with (
            patch.object(mcp_server, "datetime") as clock,
            patch.object(ArtifactRepository, "publish", publish_then_expire),
            patch.object(
                mcp_server, "resolve_source_checkout_root", wraps=mcp_server.resolve_source_checkout_root
            ) as resolve_source,
        ):
            clock.now.side_effect = current_time
            result = mcp_server._transition(
                {
                    "request": {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "role": "worker",
                        "receipt": {
                            "action_id": {"kind": "submit-review", "subject": "work-a-1"},
                            "subject_revision": "8",
                        },
                        "payload": {"candidate": candidate},
                        "lease_id": "attempt-lease-a",
                        "generation": 3,
                    }
                },
                mcp_server.CancellationToken(),
            )
        resolve_source.assert_called_once_with(project)
        content = contracts.validate_result(mcp_server.TRANSITION_TOOL, result.content)
        self.assertEqual("failed-after-publication", content["status"], content)
        self.assertEqual("committed", content["effect"])
        self.assertEqual("do-not-retry", content["retry"])
        self.assertEqual(["immutable-artifact"], content["changed_surfaces"])
        self.assertEqual(before, SQLiteWorkStore(roots.database_path).validated_snapshot())
        assert publication is not None
        self.assertTrue(publication.created)
        self.assertEqual(
            [{"field": "published_artifact_selector", "value": publication.reference.selector}],
            content["observed"],
        )
        self.assertEqual(
            publication.reference.size_bytes, len((roots.work_root / publication.reference.selector).read_bytes())
        )

    def test_stdio_ordinary_lifecycle_reloads_proposal_activation_submission_and_correction(self) -> None:  # noqa: PLR0915 - one installed lifecycle journey
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)

        def git(*arguments: str) -> str:
            return subprocess.run(
                (
                    "git",
                    "-C",
                    str(project),
                    "-c",
                    "user.name=MCP test",
                    "-c",
                    "user.email=mcp@example.invalid",
                    *arguments,
                ),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        (project / ".git" / "info" / "exclude").write_text(".codex/\n", encoding="utf-8")
        (project / "product.txt").write_text("Accepted base\n", encoding="utf-8")
        git("add", "product.txt", "architecture.md")
        git("commit", "-m", "Accepted base")
        base = git("rev-parse", "HEAD")
        branch = git("branch", "--show-current")
        common: dict[str, contracts.JsonValue] = {"project_root": str(project), "work_root": str(roots.work_root)}
        actor: dict[str, contracts.JsonValue] = {"actor_task_id": "coordinator", "actor_host_id": "local"}

        def reloaded_state(expected: str) -> stored_state.StoredWorkState:
            snapshot = SQLiteWorkStore(roots.database_path).validated_snapshot()
            item = next(row for row in snapshot.lifecycle.work_items if row.item_id == ItemId("proposal-1"))
            self.assertEqual(expected, item.state.value)
            return snapshot

        async def scenario() -> None:  # noqa: PLR0915 - ordered real client effects
            parameters = StdioServerParameters(command=sys.executable, args=("-m", "pinboard.mcp"))
            async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                await session.initialize()

                async def call(tool: str, arguments: dict[str, contracts.JsonValue]) -> dict[str, contracts.JsonValue]:
                    result = await session.call_tool(tool, _mcp_arguments(tool, common | arguments))
                    if result.is_error or not isinstance(result.structured_content, dict):
                        raise AssertionError(f"{tool} returned {result}")
                    content = result.structured_content
                    if content.get("status") == "rejected":
                        raise AssertionError(f"{tool} rejected {content}")
                    return content

                async def transition(
                    kind: str,
                    subject: str,
                    role: str,
                    authority: dict[str, contracts.JsonValue],
                    payload: dict[str, contracts.JsonValue],
                ) -> dict[str, contracts.JsonValue]:
                    discovered = await call(
                        mcp_server.ACTIONS_TOOL,
                        {
                            "role": role,
                            "action_id": {"kind": kind, "subject": subject},
                            **({} if role == "project" else authority),
                        },
                    )
                    actions = discovered["actions"]
                    if not isinstance(actions, list) or len(actions) != 1 or not isinstance(actions[0], dict):
                        raise AssertionError(f"Missing exact {kind} action: {discovered}")
                    selected = actions[0]
                    return await call(
                        mcp_server.TRANSITION_TOOL,
                        {
                            "role": role,
                            "receipt": {
                                "action_id": selected["action_id"],
                                "subject_revision": selected["subject_revision"],
                            },
                            "payload": payload,
                            **authority,
                        },
                    )

                await call(mcp_server.PROPOSAL_CREATE_TOOL, {"proposal": proposal_input(), **actor})
                reloaded_state("intake")
                await transition(
                    "accept-proposal",
                    "proposal-1",
                    "project",
                    actor,
                    {"item": "proposal-1", "state": "ready", "next_action": "Implement accepted effect."},
                )
                reloaded_state("ready")
                prepared = await call(
                    mcp_server.PREPARATION_AUTHORITY_TOOL,
                    {
                        "operation": "start",
                        "item_id": "proposal-1",
                        "task_id": "coordinator",
                        "host_id": "local",
                        "ttl_seconds": 600,
                    },
                )
                preparation: dict[str, contracts.JsonValue] = {
                    "lease_id": prepared["lease_id"],
                    "generation": prepared["generation"],
                }
                await call(
                    mcp_server.PREPARATION_AUTHORITY_TOOL,
                    {
                        "operation": "renew",
                        "item_id": "proposal-1",
                        "ttl_seconds": 1200,
                        **preparation,
                    },
                )
                template = work_c_brief()
                checkpoint = template.checkpoint
                assert isinstance(checkpoint, work_brief_models.LocalCheckpoint)
                revision = prepared["definition_revision"]
                digest = prepared["definition_digest"]
                assert isinstance(revision, int) and isinstance(digest, str)
                brief = replace_struct(
                    template,
                    item_id="proposal-1",
                    attempt_id="proposal-1-1",
                    owner_task_id="coordinator",
                    branch=branch,
                    base_revision=base,
                    accepted_scope=work_brief_models.AcceptedScope(revision, digest),
                    checkpoint=replace_struct(
                        checkpoint,
                        architecture_impact=work_brief_models.NoArchitectureImpact(
                            "One local consumer keeps its ownership."
                        ),
                        verification=(
                            replace_struct(
                                checkpoint.verification[0],
                                authorization_basis=work_brief_models.AcceptedScopeAuthorization(
                                    "proposal-1", revision
                                ),
                            ),
                        ),
                    ),
                )
                publication = await call(mcp_server.BRIEF_PUBLISH_TOOL, {"brief": msgspec.to_builtins(brief)})
                reference = publication["reference"]
                assert isinstance(reference, dict)
                await call(
                    mcp_server.ARTIFACT_VERIFY_TOOL,
                    {
                        "artifact_ref_id": reference["artifact_ref_id"],
                        "selector": reference["selector"],
                        "sha256": reference["sha256"],
                        "size_bytes": reference["size_bytes"],
                    },
                )
                await transition(
                    "activate",
                    "proposal-1",
                    "preparer",
                    preparation,
                    {"brief_artifact_ref_id": reference["artifact_ref_id"]},
                )
                reloaded_state("active")
                acquired = await call(
                    mcp_server.ATTEMPT_AUTHORITY_TOOL,
                    {
                        "operation": "acquire",
                        "attempt_id": "proposal-1-1",
                        "task_id": "worker",
                        "host_id": "local",
                        "ttl_seconds": 600,
                    },
                )
                worker: dict[str, contracts.JsonValue] = {
                    "lease_id": acquired["lease_id"],
                    "generation": acquired["generation"],
                }
                await call(
                    mcp_server.ATTEMPT_AUTHORITY_TOOL,
                    {
                        "operation": "renew",
                        "attempt_id": "proposal-1-1",
                        "ttl_seconds": 1200,
                        **worker,
                    },
                )
                (project / "product.txt").write_text("Observable candidate\n", encoding="utf-8")
                git("add", "product.txt")
                git("commit", "-m", "Observable candidate")
                candidate = git("rev-parse", "HEAD")
                self.assertEqual("", git("status", "--porcelain"))
                await transition("submit-review", "proposal-1-1", "worker", worker, {"candidate": candidate})
                snapshot = reloaded_state("review")
                attempt = next(
                    row for row in snapshot.lifecycle.attempts if row.attempt_id == AttemptId("proposal-1-1")
                )
                self.assertEqual(candidate, attempt.candidate_revision)
                inspected = await call(mcp_server.ATTEMPT_INSPECT_TOOL, {"attempt_id": "proposal-1-1"})
                continuation = inspected["continuation"]
                assert isinstance(continuation, dict)
                self.assertEqual("review", continuation["state"])
                await call(
                    mcp_server.ATTEMPT_AUTHORITY_TOOL,
                    {
                        "operation": "release",
                        "attempt_id": "proposal-1-1",
                        **worker,
                    },
                )
                corrected = await transition(
                    "return-for-correction",
                    "proposal-1-1",
                    "project",
                    actor,
                    {"reason": "Correct the reviewed candidate."},
                )
                snapshot = reloaded_state("active")
                retained = SQLiteWorkStore(roots.database_path).read_attempt_authority_status(AttemptId("proposal-1-1"))
                assert retained is not None and isinstance(acquired["generation"], int)
                self.assertGreater(retained.generation, acquired["generation"])
                self.assertIsNone(
                    next(
                        row for row in snapshot.lifecycle.attempts if row.attempt_id == AttemptId("proposal-1-1")
                    ).candidate_revision
                )
                self.assertIsNotNone(corrected["history_id"])
                reacquired = await call(
                    mcp_server.ATTEMPT_AUTHORITY_TOOL,
                    {
                        "operation": "acquire",
                        "attempt_id": "proposal-1-1",
                        "task_id": "correction-worker",
                        "host_id": "local",
                        "ttl_seconds": 600,
                    },
                )
                assert isinstance(reacquired["generation"], int)
                self.assertGreater(reacquired["generation"], retained.generation)

        _run_async(scenario())

    def test_transition_decoder_rejects_mismatched_role_and_payload_before_effect(self) -> None:
        raw: dict[str, contracts.JsonValue] = {
            "project_root": "/project",
            "work_root": "/work",
            "role": "project",
            "receipt": {
                "action_id": {"kind": "pause", "subject": "attempt-1"},
                "subject_revision": "7",
            },
            "payload": {"reason": "Pause for correction."},
            "actor_task_id": "project-task",
            "actor_host_id": "local",
        }
        request = contracts.decode_transition_request({"request": raw})
        self.assertIsInstance(request, contracts.ProjectTransitionRequest)
        self.assertIsInstance(request.payload, contracts.action_models.ReasonInputPayload)

        wrong_role: dict[str, contracts.JsonValue] = {**raw, "role": "worker"}
        unknown_payload: dict[str, contracts.JsonValue] = {
            **raw,
            "payload": {"reason": "Pause for correction.", "unknown": True},
        }
        advisory: dict[str, contracts.JsonValue] = {
            **raw,
            "receipt": {
                "action_id": {"kind": "continue", "subject": "attempt-1"},
                "subject_revision": "7",
            },
        }
        for invalid in (wrong_role, unknown_payload, advisory):
            with self.subTest(invalid=invalid), self.assertRaises((msgspec.ValidationError, ValueError)):
                contracts.decode_transition_request({"request": invalid})
        with patch.object(mcp_server, "_resolve_durable") as resolve:
            rejected = mcp_server._transition(
                {
                    "request": {
                        "project_root": "/project",
                        "work_root": "/work",
                        "role": "project",
                        "receipt": {"action_id": {"kind": "pause", "subject": "attempt-1"}, "subject_revision": "7"},
                        "payload": {"reason": "Pause for correction.", "unknown": True},
                        "actor_task_id": "project-task",
                        "actor_host_id": "local",
                    }
                },
                mcp_server.CancellationToken(),
            )
            self.assertEqual("rejected", rejected.content["status"])
            resolve.assert_not_called()

    def test_transition_decoder_covers_each_advertised_leaf_with_positive_and_negative_payloads(self) -> None:
        reason: dict[str, contracts.JsonValue] = {"reason": "Accepted reason."}
        evidence: dict[str, contracts.JsonValue] = {"evidence": "Independent review evidence."}
        definition: dict[str, contracts.JsonValue] = {
            "schema": "pinboard-work-item-definition/v1",
            "title": "Item",
            "objective": "Change one consumer.",
            "hypothesis": "It remains observable.",
            "evidence": [],
            "scope": ["One consumer."],
            "non_scope": [],
            "acceptance_criteria": ["The change persists."],
            "dependencies": [],
            "effect": "Changed consumer.",
            "unlock": "Use the consumer.",
        }
        cases: tuple[tuple[str, str, dict[str, contracts.JsonValue]], ...] = (
            ("accept-checkpoint", "project", {"checkpoint": "checkpoint-1", "candidate": "candidate", **evidence}),
            ("accept-review-and-continue", "project", {"candidate": "candidate", **evidence}),
            (
                "accept-proposal",
                "project",
                {"item": "item-1", "state": "ready", "next_action": "Prepare accepted work."},
            ),
            ("activate", "preparer", {"brief_artifact_ref_id": 1}),
            ("block", "project", reason),
            ("block-item", "project", reason),
            ("complete", "project", evidence),
            (
                "complete",
                "project",
                {
                    "schema": "pinboard-covered-completion/v1",
                    "candidate": "candidate",
                    **evidence,
                    "reviewer_task_id": "reviewer",
                    "result_sha256": "a" * 64,
                    "review_sha256": "b" * 64,
                    "packages": [
                        {"history_id": 1, "package_sha256": "c" * 64, "disposition": "revalidated", **evidence}
                    ],
                },
            ),
            ("close", "project", {"outcome": "done", **reason}),
            ("defer", "project", {"timing": "safe-to-defer", "reopen_condition": "A supported consumer needs it."}),
            ("mark-ready", "project", reason),
            ("merge-proposal", "project", {"target": "item-2"}),
            ("pause", "project", reason),
            ("reject-proposal", "project", reason),
            ("reopen", "project", evidence),
            (
                "record-replacement",
                "project",
                {
                    "schema": "pinboard-planned-replacement/v1",
                    "affected_item": "item-1",
                    "expected_relation_revision": 0,
                    "replacement_item": "item-2",
                    "replacement_cost": "One retained owner.",
                    "status": "current",
                    "recorded_by": "coordinator",
                },
            ),
            (
                "rebind-attempt",
                "project",
                {
                    "attempt": "attempt-1",
                    "branch": "codex/candidate",
                    "base_revision": "base",
                    "brief_artifact_ref_id": 1,
                },
            ),
            ("resume", "project", {}),
            ("return-for-correction", "project", reason),
            ("return-proposal", "project", reason),
            (
                "retain-temporarily",
                "project",
                {
                    "schema": "pinboard-replacement-disposition/v1",
                    "affected_item": "item-1",
                    "relation_revision": 1,
                    "rationale": "Current consumer remains necessary.",
                    "accepted_cost": "One owner.",
                    "recorded_by": "coordinator",
                },
            ),
            (
                "revise-item",
                "project",
                {
                    "schema": "pinboard-item-revision/v1",
                    "item_id": "item-1",
                    "expected_revision": 1,
                    "expected_digest": "a" * 64,
                    "source_task": "coordinator",
                    **reason,
                    "definition": definition,
                },
            ),
            ("submit-review", "worker", {"candidate": "candidate"}),
        )
        self.assertEqual(len(contracts.TRANSITION_REQUEST_TYPES), len(cases))
        for kind, role, payload in cases:
            authority: dict[str, contracts.JsonValue] = (
                {"actor_task_id": "coordinator", "actor_host_id": "local"}
                if role == "project"
                else {"lease_id": "lease", "generation": 1}
            )
            raw: dict[str, contracts.JsonValue] = {
                "project_root": "/project",
                "work_root": "/work",
                "role": role,
                "receipt": {"action_id": {"kind": kind, "subject": "item-1"}, "subject_revision": "1"},
                "payload": payload,
                **authority,
            }
            with self.subTest(kind=kind, payload=payload):
                decoded = contracts.decode_transition_request({"request": raw})
                self.assertIsInstance(decoded.payload, msgspec.Struct)
                with self.assertRaises((msgspec.ValidationError, ValueError)):
                    contracts.decode_transition_request(
                        {"request": {**raw, "payload": {**payload, "unexpected": True}}}
                    )
                with self.assertRaises((msgspec.ValidationError, ValueError)):
                    contracts.decode_transition_request({"request": {**raw, "role": "observer"}})

    def test_attempt_acquisition_selects_initial_or_transfer_only_under_the_write_lock(self) -> None:
        temporary, _project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        store = SQLiteWorkStore(roots.database_path)
        now = datetime.now(UTC)
        retained = store.read_attempt_authority_status(AttemptId("work-a-1"))
        self.assertIsNotNone(retained)
        assert retained is not None
        released = authority_operations.release_attempt_authority(
            store,
            attempt_id=AttemptId("work-a-1"),
            lease_id=retained.lease_id,
            generation=retained.generation,
            released_at=now,
        )
        self.assertNotIsInstance(released, DecisionFailure)

        with patch.object(
            SQLiteWorkStore,
            "read_decision_facts",
            side_effect=AssertionError("attempt acquisition selected outside the write lock"),
        ):
            acquired = authority_operations.acquire_attempt_authority(
                store,
                attempt_id=AttemptId("work-a-1"),
                task_id=TaskId("replacement-worker"),
                host_id=HostId("local"),
                lease_id=LeaseId("replacement-lease"),
                acquired_at=now + timedelta(seconds=1),
                expires_at=now + timedelta(minutes=5),
            )

        self.assertNotIsInstance(acquired, DecisionFailure)

    def test_in_process_authority_and_transition_handlers_cover_exact_mutations(  # noqa: C901, PLR0915 - one complete authority matrix
        self,
    ) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=4)
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(io.StringIO(), event_limit=16, line_limit=256)
        )
        common: dict[str, contracts.JsonValue] = {
            "project_root": str(project),
            "work_root": str(roots.work_root),
        }

        async def call(tool_name: str, arguments: dict[str, contracts.JsonValue]) -> CallToolResult:
            result = await server.call_tool(tool_name, _mcp_arguments(tool_name, arguments))
            if not isinstance(result, CallToolResult):
                raise AssertionError("The lifecycle tool requested additional input.")
            return result

        def selected_action(result: CallToolResult) -> dict[str, contracts.JsonValue]:
            content = result.structured_content
            if not isinstance(content, dict):
                raise AssertionError("Action discovery did not return structured content.")
            actions = content.get("actions")
            if not isinstance(actions, list) or len(actions) != 1 or not isinstance(actions[0], dict):
                raise AssertionError("Action discovery did not return one exact action.")
            return actions[0]

        async def scenario() -> tuple[CallToolResult, ...]:  # noqa: PLR0915 - one complete authority matrix
            preparation_status = await call(
                mcp_server.PREPARATION_AUTHORITY_TOOL,
                common | {"operation": "status", "item_id": "work-c"},
            )
            preparation_start = await call(
                mcp_server.PREPARATION_AUTHORITY_TOOL,
                common
                | {
                    "operation": "start",
                    "item_id": "work-c",
                    "task_id": "preparer-task",
                    "host_id": "local",
                    "ttl_seconds": 600,
                },
            )
            started = preparation_start.structured_content
            if not isinstance(started, dict):
                raise AssertionError("Preparation start did not return structured content.")
            preparation_present = await call(
                mcp_server.PREPARATION_AUTHORITY_TOOL,
                common | {"operation": "status", "item_id": "work-c"},
            )
            preparation_conflict = await call(
                mcp_server.PREPARATION_AUTHORITY_TOOL,
                common
                | {
                    "operation": "start",
                    "item_id": "work-c",
                    "task_id": "competing-preparer",
                    "host_id": "local",
                    "ttl_seconds": 600,
                },
            )
            preparation_invalid = await call(
                mcp_server.PREPARATION_AUTHORITY_TOOL,
                common | {"operation": "unsupported", "item_id": "work-c"},
            )
            preparation_renew = await call(
                mcp_server.PREPARATION_AUTHORITY_TOOL,
                common
                | {
                    "operation": "renew",
                    "item_id": "work-c",
                    "lease_id": started["lease_id"],
                    "generation": started["generation"],
                    "ttl_seconds": 1200,
                },
            )
            renewed = preparation_renew.structured_content
            if not isinstance(renewed, dict):
                raise AssertionError("Preparation renewal did not return structured content.")
            preparation_action_arguments: dict[str, contracts.JsonValue] = {
                **common,
                "role": "preparer",
                "lease_id": renewed["lease_id"],
                "generation": renewed["generation"],
                "action_id": {"kind": "activate", "subject": "work-c"},
            }
            preparation_actions = await call(mcp_server.ACTIONS_TOOL, preparation_action_arguments)
            activation_action = selected_action(preparation_actions)
            activation_arguments: dict[str, contracts.JsonValue] = {
                **common,
                "role": "preparer",
                "receipt": {
                    "action_id": activation_action["action_id"],
                    "subject_revision": activation_action["subject_revision"],
                },
                "payload": {"brief_artifact_ref_id": 999},
                "lease_id": renewed["lease_id"],
                "generation": renewed["generation"],
            }
            activation_rejected = await call(mcp_server.TRANSITION_TOOL, activation_arguments)
            preparation_release = await call(
                mcp_server.PREPARATION_AUTHORITY_TOOL,
                common
                | {
                    "operation": "release",
                    "item_id": "work-c",
                    "lease_id": renewed["lease_id"],
                    "generation": renewed["generation"],
                },
            )
            preparation_restart = await call(
                mcp_server.PREPARATION_AUTHORITY_TOOL,
                common
                | {
                    "operation": "start",
                    "item_id": "work-c",
                    "task_id": "replacement-preparer",
                    "host_id": "local",
                    "ttl_seconds": 600,
                },
            )
            restarted = preparation_restart.structured_content
            if not isinstance(restarted, dict):
                raise AssertionError("Preparation restart did not return structured content.")
            preparation_revoke = await call(
                mcp_server.PREPARATION_AUTHORITY_TOOL,
                common
                | {
                    "operation": "revoke",
                    "item_id": "work-c",
                    "lease_id": restarted["lease_id"],
                    "generation": restarted["generation"],
                    "actor_task_id": "project-task",
                    "actor_host_id": "local",
                },
            )
            attempt_status = await call(
                mcp_server.ATTEMPT_AUTHORITY_TOOL,
                common | {"operation": "status", "attempt_id": "work-a-1"},
            )
            attempt_absent = await call(
                mcp_server.ATTEMPT_AUTHORITY_TOOL,
                common | {"operation": "status", "attempt_id": "missing-attempt"},
            )
            attempt_invalid = await call(
                mcp_server.ATTEMPT_AUTHORITY_TOOL,
                common | {"operation": "unsupported", "attempt_id": "work-a-1"},
            )
            held = attempt_status.structured_content
            if not isinstance(held, dict):
                raise AssertionError("Attempt status did not return structured content.")
            attempt_conflict = await call(
                mcp_server.ATTEMPT_AUTHORITY_TOOL,
                common
                | {
                    "operation": "acquire",
                    "attempt_id": "work-a-1",
                    "task_id": "competing-worker",
                    "host_id": "local",
                    "ttl_seconds": 600,
                },
            )
            worker_action_arguments: dict[str, contracts.JsonValue] = {
                **common,
                "role": "worker",
                "lease_id": held["lease_id"],
                "generation": held["generation"],
                "action_id": {"kind": "submit-review", "subject": "work-a-1"},
            }
            worker_actions = await call(mcp_server.ACTIONS_TOOL, worker_action_arguments)
            submit_action = selected_action(worker_actions)
            submit_arguments: dict[str, contracts.JsonValue] = {
                **common,
                "role": "worker",
                "receipt": {
                    "action_id": submit_action["action_id"],
                    "subject_revision": submit_action["subject_revision"],
                },
                "payload": {"candidate": "candidate"},
                "lease_id": held["lease_id"],
                "generation": held["generation"],
            }
            publication_details = FailureDetails(
                observed=(FailureFact("published_artifact_selector", "artifacts/evidence/candidate/1.json"),),
                mismatches=(),
                retry=RetryDisposition.DO_NOT_RETRY,
                effect=EffectDisposition.COMMITTED,
                changed_surfaces=(ChangedSurface.IMMUTABLE_ARTIFACT,),
                alternatives=(),
            )
            with patch.object(
                mcp_server.lifecycle_artifacts,
                "execute_artifact_transition",
                return_value=mcp_server.lifecycle_artifacts.PublishedTransitionFailure(
                    "FILE_PUBLISH_FAILED", "publication failed", publication_details, None
                ),
            ):
                publication_failed = await call(mcp_server.TRANSITION_TOOL, submit_arguments)
            with patch.object(
                mcp_server.lifecycle_artifacts,
                "execute_artifact_transition",
                return_value=DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    "candidate changed after publication",
                    publication_details,
                ),
            ):
                decision_failed = await call(mcp_server.TRANSITION_TOOL, submit_arguments)
            attempt_renew = await call(
                mcp_server.ATTEMPT_AUTHORITY_TOOL,
                common
                | {
                    "operation": "renew",
                    "attempt_id": "work-a-1",
                    "lease_id": held["lease_id"],
                    "generation": held["generation"],
                    "ttl_seconds": 1200,
                },
            )
            attempt_renewed = attempt_renew.structured_content
            if not isinstance(attempt_renewed, dict):
                raise AssertionError("Attempt renewal did not return structured content.")
            attempt_release = await call(
                mcp_server.ATTEMPT_AUTHORITY_TOOL,
                common
                | {
                    "operation": "release",
                    "attempt_id": "work-a-1",
                    "lease_id": attempt_renewed["lease_id"],
                    "generation": attempt_renewed["generation"],
                },
            )
            attempt_acquire = await call(
                mcp_server.ATTEMPT_AUTHORITY_TOOL,
                common
                | {
                    "operation": "acquire",
                    "attempt_id": "work-a-1",
                    "task_id": "replacement-worker",
                    "host_id": "local",
                    "ttl_seconds": 600,
                },
            )
            acquired = attempt_acquire.structured_content
            if not isinstance(acquired, dict):
                raise AssertionError("Attempt acquisition did not return structured content.")
            attempt_revoke = await call(
                mcp_server.ATTEMPT_AUTHORITY_TOOL,
                common
                | {
                    "operation": "revoke",
                    "attempt_id": "work-a-1",
                    "lease_id": acquired["lease_id"],
                    "generation": acquired["generation"],
                    "actor_task_id": "project-task",
                    "actor_host_id": "local",
                },
            )
            stale_attempt_renew = await call(
                mcp_server.ATTEMPT_AUTHORITY_TOOL,
                common
                | {
                    "operation": "renew",
                    "attempt_id": "work-a-1",
                    "lease_id": acquired["lease_id"],
                    "generation": acquired["generation"],
                    "ttl_seconds": 600,
                },
            )
            invalid_arguments: dict[str, contracts.JsonValue] = {
                **common,
                "role": "project",
                "receipt": {
                    "action_id": {"kind": "pause", "subject": "work-a-1"},
                    "subject_revision": "8",
                },
                "payload": {"reason": "Invalid leaf.", "unknown": True},
                "actor_task_id": "project-task",
                "actor_host_id": "local",
            }
            invalid = await call(mcp_server.TRANSITION_TOOL, invalid_arguments)
            committed_arguments: dict[str, contracts.JsonValue] = {
                **common,
                "role": "project",
                "receipt": {
                    "action_id": {"kind": "pause", "subject": "work-a-1"},
                    "subject_revision": "8",
                },
                "payload": {"reason": "Pause through the direct MCP handler."},
                "actor_task_id": "project-task",
                "actor_host_id": "local",
            }
            committed = await call(mcp_server.TRANSITION_TOOL, committed_arguments)
            return (
                preparation_status,
                preparation_start,
                preparation_present,
                preparation_conflict,
                preparation_invalid,
                preparation_renew,
                preparation_actions,
                activation_rejected,
                preparation_release,
                preparation_restart,
                preparation_revoke,
                attempt_status,
                attempt_absent,
                attempt_invalid,
                attempt_conflict,
                worker_actions,
                publication_failed,
                decision_failed,
                attempt_renew,
                attempt_release,
                attempt_acquire,
                attempt_revoke,
                stale_attempt_renew,
                invalid,
                committed,
            )

        try:
            results = _run_async(scenario())
        finally:
            executor.shutdown()
        contents = tuple(result.structured_content for result in results)
        for result, content in zip(results, contents, strict=True):
            self.assertIsInstance(result, CallToolResult)
            self.assertIsInstance(content, dict)
        self.assertEqual("absent", contents[0]["status"])
        self.assertEqual("present", contents[2]["status"])
        self.assertEqual("rejected", contents[3]["status"])
        self.assertIsNotNone(contents[3]["conflict"])
        self.assertEqual("TRANSITION_INPUT_INVALID", contents[4]["code"])
        self.assertEqual("TRANSITION_INPUT_INVALID", contents[7]["code"])
        self.assertEqual("released", contents[8]["authority_status"])
        self.assertEqual("revoked", contents[10]["authority_status"])
        self.assertEqual("absent", contents[12]["status"])
        self.assertEqual("TRANSITION_INPUT_INVALID", contents[13]["code"])
        self.assertIsNotNone(contents[14]["conflict"])
        self.assertEqual("failed-after-publication", contents[16]["status"])
        self.assertEqual("failed-after-publication", contents[17]["status"])
        self.assertEqual("released", contents[19]["authority_status"])
        self.assertEqual("active", contents[20]["authority_status"])
        self.assertEqual("revoked", contents[21]["authority_status"])
        self.assertEqual("rejected", contents[22]["status"])
        self.assertEqual("TRANSITION_INPUT_INVALID", contents[23]["code"])
        self.assertIn(contents[24]["status"], {"committed", "committed-with-warning"})

    def test_authority_tools_preserve_fixed_time_commit_reload_and_stale_rejection(self) -> None:
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=4)
        self.addCleanup(executor.shutdown)
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(io.StringIO(), event_limit=1, line_limit=256)
        )
        for family in ("attempt", "preparation"):
            for operation in ("renew", "release", "revoke"):
                with (
                    self.subTest(family=family, operation=operation),
                    patch("tests.test_mcp.datetime") as fixture_clock,
                ):
                    fixture_clock.now.return_value = SQLITE_NOW
                    temporary, project, roots = self._project()
                    self.addCleanup(temporary.cleanup)
                    store = SQLiteWorkStore(roots.database_path)
                    if family == "preparation":
                        started = authority_operations.start_preparation_authority(
                            store,
                            item_id=ItemId("work-c"),
                            task_id=TaskId("preparer"),
                            host_id=HostId("local"),
                            lease_id=LeaseId("preparation-lease"),
                            acquired_at=SQLITE_NOW,
                            expires_at=SQLITE_NOW + timedelta(minutes=5),
                        )
                        assert not isinstance(started, DecisionFailure)
                        retained = started.authority
                        subject: dict[str, contracts.JsonValue] = {"item_id": "work-c"}
                        tool = mcp_server.PREPARATION_AUTHORITY_TOOL
                    else:
                        retained_attempt = store.read_attempt_authority_status(AttemptId("work-a-1"))
                        assert retained_attempt is not None
                        retained = retained_attempt
                        subject = {"attempt_id": "work-a-1"}
                        tool = mcp_server.ATTEMPT_AUTHORITY_TOOL
                    arguments: dict[str, contracts.JsonValue] = {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "operation": operation,
                        "lease_id": str(retained.lease_id),
                        "generation": retained.generation,
                        **subject,
                    }
                    if operation == "renew":
                        arguments["ttl_seconds"] = 600
                    elif operation == "revoke":
                        arguments.update(actor_task_id="project-owner", actor_host_id="project-host")
                    before = store.validated_snapshot()
                    operation_time = SQLITE_NOW + timedelta(seconds=1)
                    with patch.object(mcp_server, "datetime") as clock:
                        clock.now.return_value = operation_time
                        rejected = _run_async(
                            server.call_tool(tool, _mcp_arguments(tool, arguments | {"lease_id": "stale-lease"}))
                        )
                        assert isinstance(rejected, CallToolResult)
                        assert isinstance(rejected.structured_content, dict)
                        self.assertEqual("rejected", rejected.structured_content["status"])
                        self.assertEqual(before, SQLiteWorkStore(roots.database_path).validated_snapshot())
                        result = _run_async(server.call_tool(tool, _mcp_arguments(tool, arguments)))
                    assert isinstance(result, CallToolResult)
                    assert isinstance(result.structured_content, dict)
                    content = result.structured_content
                    self.assertIn(content["status"], {"committed", "committed-with-warning"})
                    reopened = SQLiteWorkStore(roots.database_path)
                    latest = (
                        reopened.read_attempt_authority_status(AttemptId("work-a-1"))
                        if family == "attempt"
                        else reopened.read_preparation_authority_status(ItemId("work-c"))
                    )
                    assert latest is not None
                    self.assertEqual(content["authority_status"], latest.status.value)
                    self.assertEqual(content["generation"], latest.generation)
                    expected_expiry = (
                        operation_time + timedelta(seconds=600) if operation == "renew" else operation_time
                    )
                    self.assertEqual(expected_expiry, latest.expires_at)
                    self.assertEqual(
                        before.lifecycle.project.revision + 1, reopened.validated_snapshot().lifecycle.project.revision
                    )

    def test_shared_authority_operations_preserve_missing_state_and_expired_status(self) -> None:
        temporary, _project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        store = SQLiteWorkStore(roots.database_path)
        now = datetime.now(UTC)
        later = now + timedelta(days=365)

        expired_attempt = authority_operations.attempt_authority_status(store, AttemptId("work-a-1"), later)
        self.assertIsNotNone(expired_attempt)
        assert expired_attempt is not None
        self.assertEqual("expired", expired_attempt.status.value)
        missing = authority_operations.acquire_attempt_authority(
            store,
            attempt_id=AttemptId("missing-attempt"),
            task_id=TaskId("worker"),
            host_id=HostId("local"),
            lease_id=LeaseId("missing-lease"),
            acquired_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        self.assertIsInstance(missing, DecisionFailure)
        missing_change = authority_operations.release_attempt_authority(
            store,
            attempt_id=AttemptId("missing-attempt"),
            lease_id=LeaseId("missing-lease"),
            generation=1,
            released_at=now,
        )
        self.assertIsInstance(missing_change, DecisionFailure)

        started = authority_operations.start_preparation_authority(
            store,
            item_id=ItemId("work-c"),
            task_id=TaskId("preparer"),
            host_id=HostId("local"),
            lease_id=LeaseId("preparation-lease"),
            acquired_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        self.assertNotIsInstance(started, DecisionFailure)
        expired_preparation = authority_operations.preparation_authority_status(store, ItemId("work-c"), later)
        self.assertIsNotNone(expired_preparation)
        assert expired_preparation is not None
        self.assertEqual("expired", expired_preparation.status.value)
        missing_preparation = authority_operations.release_preparation_authority(
            store,
            item_id=ItemId("work-b"),
            lease_id=LeaseId("missing-lease"),
            generation=1,
            released_at=now,
        )
        self.assertIsInstance(missing_preparation, DecisionFailure)

    def _expected_bytes(self, roots: DurableRoots, item_id: str) -> bytes:
        projected = queries.project_item_status(
            SQLiteWorkStore(roots.database_path), ItemId(item_id), datetime.now(UTC)
        )
        if isinstance(projected, DecisionFailure):
            raise AssertionError(projected.message)
        return msgspec.json.encode(projected)

    def test_action_and_continuation_results_reject_cross_correlated_content(self) -> None:  # noqa: PLR0915 - one correlated contract matrix
        temporary, _project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        action_result = msgspec.json.decode(
            msgspec.json.encode(
                {
                    "schema": "pinboard-mcp-actions-result/v1",
                    "status": "ok",
                    "actions": [
                        mcp_server._mcp_action(
                            action(decision_models.AcceptCheckpointAction, AttemptId("attempt-1")),
                            SQLiteWorkStore(roots.database_path),
                            str(_project),
                            str(roots.work_root),
                            focused=False,
                        )
                    ],
                    "state_changed": False,
                    "effect": "unchanged",
                    "retry": "safe-to-repeat",
                    "changed_surfaces": [],
                }
            )
        )
        assert isinstance(action_result, dict)
        contracts.validate_result(mcp_server.ACTIONS_TOOL, action_result)

        wrong_payload = deepcopy(action_result)
        wrong_payload_action = wrong_payload["actions"][0]
        assert isinstance(wrong_payload_action, dict)
        wrong_payload_contract = wrong_payload_action["input_contract"]
        assert isinstance(wrong_payload_contract, dict)
        wrong_payload_contract["payload_schema"] = {"type": "object"}
        with self.assertRaises((msgspec.ValidationError, ValueError)):
            contracts.validate_result(mcp_server.ACTIONS_TOOL, wrong_payload)

        wrong_identity = deepcopy(action_result)
        wrong_identity_action = wrong_identity["actions"][0]
        assert isinstance(wrong_identity_action, dict)
        wrong_identity_action["action_id"] = {"kind": "block", "subject": "attempt-1"}
        with self.assertRaises((msgspec.ValidationError, ValueError)):
            contracts.validate_result(mcp_server.ACTIONS_TOOL, wrong_identity)

        continuation = query_models.ActiveAttemptContinuation(
            "pinboard-attempt-continuation/v1",
            "attempt-1",
            "item-1",
            1,
            "owner-task",
            False,
            False,
            query_models.ActionContinuation("continue:attempt-1", decision_models.ActionKind.CONTINUE, "Continue."),
            ("continue:attempt-1", "pause:attempt-1", "revise-item:item-1"),
            ("create-user-task", "wake-user-task", "return-ownership-to-parent"),
        )
        absent = contracts.EvidenceAbsent("/work/attempts/attempt-1/result.md")
        presented_continuation = mcp_server._mcp_attempt_continuation(continuation)
        assert not isinstance(presented_continuation, contracts.TerminalAttemptContinuation)
        inspection = contracts.NonterminalAttemptInspectionSuccess(
            "pinboard-mcp-attempt-inspection-result/v1",
            "ok",
            presented_continuation,
            contracts.CandidateRecoveryAbsent(),
            contracts.AcceptedBriefIdentity(
                1, "/work/brief.json", "artifacts/briefs/a/1.json", "a" * 64, 1, 1, 1, "b" * 64
            ),
            absent,
            contracts.EvidenceAbsent("/work/attempts/attempt-1/review.md"),
            contracts.EvidenceAbsent("/work/attempts/attempt-1/blocker.md"),
            False,
            "unchanged",
            "safe-to-repeat",
            (),
        )
        inspection_result = msgspec.json.decode(msgspec.json.encode(inspection))
        assert isinstance(inspection_result, dict)
        contracts.validate_result(mcp_server.ATTEMPT_INSPECT_TOOL, inspection_result)

        for field, value in (
            (
                "next_operation",
                {
                    "kind": "action",
                    "action": {"target": "item", "action_kind": "continue"},
                    "condition": "Continue.",
                },
            ),
            ("legal_actions", [{"target": "item", "action_kind": "continue"}]),
            ("forbidden_routes", list[str]()),
        ):
            with self.subTest(field=field):
                invalid = deepcopy(inspection_result)
                invalid_continuation = invalid["continuation"]
                assert isinstance(invalid_continuation, dict)
                invalid_continuation[field] = value
                with self.assertRaises((msgspec.ValidationError, ValueError)):
                    contracts.validate_result(mcp_server.ATTEMPT_INSPECT_TOOL, invalid)

        missing_forbidden = deepcopy(inspection_result)
        missing_forbidden_continuation = missing_forbidden["continuation"]
        assert isinstance(missing_forbidden_continuation, dict)
        missing_forbidden_continuation["forbidden_routes"] = list[str]()

        state_incompatible_legal = deepcopy(inspection_result)
        incompatible_continuation = state_incompatible_legal["continuation"]
        assert isinstance(incompatible_continuation, dict)
        incompatible_continuation["legal_actions"][0] = {
            "target": "item",
            "action_kind": "resume",
        }
        with self.assertRaises((msgspec.ValidationError, ValueError)):
            contracts.validate_result(mcp_server.ATTEMPT_INSPECT_TOOL, state_incompatible_legal)

        async def advertised_schema_scenario() -> None:
            parameters = StdioServerParameters(command=sys.executable, args=["-m", "pinboard.mcp"], cwd=Path.cwd())
            async with (
                stdio_client(parameters) as streams,
                ClientSession(*streams) as session,
            ):
                await session.initialize()
                for tool_name, invalid in (
                    (mcp_server.ACTIONS_TOOL, wrong_payload),
                    (mcp_server.ACTIONS_TOOL, wrong_identity),
                    (mcp_server.ATTEMPT_INSPECT_TOOL, missing_forbidden),
                    (mcp_server.ATTEMPT_INSPECT_TOOL, state_incompatible_legal),
                ):
                    with self.assertRaises(RuntimeError):
                        await session.validate_tool_result(
                            tool_name,
                            CallToolResult(content=[], structured_content=invalid),
                        )

        _run_async(advertised_schema_scenario())

    def test_committed_transition_results_correlate_mutating_actions_and_exact_surfaces(self) -> None:
        def committed(kind: str, surfaces: list[str]) -> dict[str, contracts.JsonValue]:
            return {
                "schema": "pinboard-mcp-transition-result/v1",
                "status": "committed",
                "action_id": {"kind": kind, "subject": "work-a-1"},
                "committed_revision": 13,
                "history_id": 2,
                "state_changed": True,
                "effect": "committed",
                "retry": "do-not-retry",
                "changed_surfaces": list[contracts.JsonValue](surfaces),
                "warning": None,
            }

        ledger = ["ledger"]
        references = ["accepted-artifact-reference", "ledger"]
        publication = ["immutable-artifact", *references]
        valid = (
            committed("pause", ledger),
            committed("submit-review", references),
            committed("accept-checkpoint", publication),
            committed("complete", ledger),
            committed("complete", references),
            committed("complete", publication),
        )
        invalid = (
            *(committed(kind, ledger) for kind in ("continue", "dispatch", "inspect", "report-blocker")),
            committed("pause", ["immutable-artifact"]),
            committed("pause", publication),
            committed("pause", ["ledger", "ledger"]),
            committed("submit-review", ledger),
            committed("accept-checkpoint", ledger),
            committed("complete", ["immutable-artifact"]),
            committed("complete", ["accepted-artifact-reference"]),
            committed("complete", ["ledger", "accepted-artifact-reference"]),
        )
        for content in valid:
            contracts.validate_result(mcp_server.TRANSITION_TOOL, content)
        for content in invalid:
            with self.subTest(content=content), self.assertRaises((msgspec.ValidationError, ValueError)):
                contracts.validate_result(mcp_server.TRANSITION_TOOL, content)

        async def advertised_schema_scenario() -> None:
            parameters = StdioServerParameters(command=sys.executable, args=["-m", "pinboard.mcp"])
            async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                await session.initialize()
                for content in valid:
                    await session.validate_tool_result(
                        mcp_server.TRANSITION_TOOL, CallToolResult(content=[], structured_content=content)
                    )
                for content in invalid:
                    with self.subTest(content=content), self.assertRaises(RuntimeError):
                        await session.validate_tool_result(
                            mcp_server.TRANSITION_TOOL, CallToolResult(content=[], structured_content=content)
                        )

        _run_async(advertised_schema_scenario())

    def test_actions_reject_explicit_null_authority_fields_for_unleased_roles(self) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)

        async def scenario() -> tuple[CallToolResult, CallToolResult]:
            parameters = StdioServerParameters(command=sys.executable, args=["-m", "pinboard.mcp"], cwd=Path.cwd())
            async with (
                stdio_client(parameters) as streams,
                ClientSession(*streams) as session,
            ):
                await session.initialize()
                common: dict[str, contracts.JsonValue] = {
                    "project_root": str(project),
                    "work_root": str(roots.work_root),
                }
                return (
                    await session.call_tool(
                        mcp_server.ACTIONS_TOOL,
                        _mcp_arguments(mcp_server.ACTIONS_TOOL, common | {"role": "project", "lease_id": None}),
                    ),
                    await session.call_tool(
                        mcp_server.ACTIONS_TOOL,
                        _mcp_arguments(mcp_server.ACTIONS_TOOL, common | {"role": "observer", "generation": None}),
                    ),
                )

        for result in _run_async(scenario()):
            self.assertFalse(result.is_error)
            content = result.structured_content
            self.assertIsInstance(content, dict)
            self.assertEqual("ACTIONS_INVALID", content["code"])
            self.assertFalse(content["state_changed"])

    def test_negotiated_numeric_inputs_reach_strict_request_records_without_coercion(self) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        brief_reference = SQLiteWorkStore(roots.database_path).validated_snapshot().artifact_references[0]

        async def scenario() -> tuple[tuple[CallToolResult, str], ...]:
            parameters = StdioServerParameters(command=sys.executable, args=["-m", "pinboard.mcp"], cwd=Path.cwd())
            async with (
                stdio_client(parameters) as streams,
                ClientSession(*streams) as session,
            ):
                await session.initialize()
                common: dict[str, contracts.JsonValue] = {
                    "project_root": str(project),
                    "work_root": str(roots.work_root),
                }
                artifact = {
                    "artifact_ref_id": int(brief_reference.artifact_ref_id),
                    "selector": brief_reference.selector,
                    "sha256": brief_reference.content_sha256,
                    "size_bytes": brief_reference.size_bytes,
                }
                return (
                    (
                        await session.call_tool(
                            mcp_server.ACTIONS_TOOL,
                            _mcp_arguments(
                                mcp_server.ACTIONS_TOOL,
                                common | {"role": "worker", "lease_id": "attempt-lease-a", "generation": "3"},
                            ),
                        ),
                        "ACTIONS_INVALID",
                    ),
                    (
                        await session.call_tool(
                            mcp_server.ACTIONS_TOOL,
                            _mcp_arguments(
                                mcp_server.ACTIONS_TOOL,
                                common | {"role": "worker", "lease_id": "attempt-lease-a", "generation": True},
                            ),
                        ),
                        "ACTIONS_INVALID",
                    ),
                    (
                        await session.call_tool(
                            mcp_server.ARTIFACT_VERIFY_TOOL,
                            common | artifact | {"artifact_ref_id": str(brief_reference.artifact_ref_id)},
                        ),
                        "ARTIFACT_VERIFY_INVALID",
                    ),
                    (
                        await session.call_tool(
                            mcp_server.ARTIFACT_VERIFY_TOOL,
                            common | artifact | {"artifact_ref_id": True},
                        ),
                        "ARTIFACT_VERIFY_INVALID",
                    ),
                    (
                        await session.call_tool(
                            mcp_server.ARTIFACT_VERIFY_TOOL,
                            common | artifact | {"size_bytes": str(brief_reference.size_bytes)},
                        ),
                        "ARTIFACT_VERIFY_INVALID",
                    ),
                    (
                        await session.call_tool(
                            mcp_server.ARTIFACT_VERIFY_TOOL,
                            common | artifact | {"size_bytes": True},
                        ),
                        "ARTIFACT_VERIFY_INVALID",
                    ),
                )

        for result, expected_code in _run_async(scenario()):
            self.assertFalse(result.is_error)
            content = result.structured_content
            self.assertIsInstance(content, dict)
            self.assertEqual(expected_code, content["code"])
            self.assertFalse(content["state_changed"])
            self.assertEqual([], content["changed_surfaces"])

    def test_negotiated_schemas_reject_cross_parent_action_identities(self) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        token = mcp_server.CancellationToken()
        common = (str(project), str(roots.work_root))
        action_result = msgspec.json.decode(
            msgspec.json.encode(
                mcp_server._read_actions(
                    {
                        "request": {
                            "project_root": common[0],
                            "work_root": common[1],
                            "role": "project",
                            "action_id": {"kind": "continue", "subject": "work-a-1"},
                        }
                    },
                    token,
                ).content
            )
        )
        attempt_result = msgspec.json.decode(
            msgspec.json.encode(mcp_server._read_attempt_inspection(*common, "work-a-1", token).content)
        )
        assert isinstance(action_result, dict)
        assert isinstance(attempt_result, dict)

        wrong_action_subject = deepcopy(action_result)
        action = wrong_action_subject["actions"][0]
        assert isinstance(action, dict)
        action["subject"] = "work-a-2"

        wrong_next_operation = deepcopy(attempt_result)
        continuation = wrong_next_operation["continuation"]
        assert isinstance(continuation, dict)
        operation = continuation["next_operation"]
        assert isinstance(operation, dict)
        selected_action = operation["action"]
        assert isinstance(selected_action, dict)
        selected_action["attempt_id"] = "work-a-2"

        wrong_legal_action = deepcopy(attempt_result)
        continuation = wrong_legal_action["continuation"]
        assert isinstance(continuation, dict)
        legal_action = continuation["legal_actions"][0]
        assert isinstance(legal_action, dict)
        legal_action["attempt_id"] = "work-a-2"

        async def scenario() -> None:
            parameters = StdioServerParameters(command=sys.executable, args=["-m", "pinboard.mcp"], cwd=Path.cwd())
            async with (
                stdio_client(parameters) as streams,
                ClientSession(*streams) as session,
            ):
                await session.initialize()
                await session.validate_tool_result(
                    mcp_server.ACTIONS_TOOL,
                    CallToolResult(content=[], structured_content=action_result),
                )
                await session.validate_tool_result(
                    mcp_server.ATTEMPT_INSPECT_TOOL,
                    CallToolResult(content=[], structured_content=attempt_result),
                )
                for tool_name, invalid in (
                    (mcp_server.ACTIONS_TOOL, wrong_action_subject),
                    (mcp_server.ATTEMPT_INSPECT_TOOL, wrong_next_operation),
                    (mcp_server.ATTEMPT_INSPECT_TOOL, wrong_legal_action),
                ):
                    with self.assertRaises(RuntimeError):
                        await session.validate_tool_result(
                            tool_name,
                            CallToolResult(content=[], structured_content=invalid),
                        )

        _run_async(scenario())

    def test_attempt_projection_preserves_every_closed_continuation_without_parent_duplicates(self) -> None:
        forbidden = ("create-user-task", "wake-user-task", "return-ownership-to-parent")
        common = ("pinboard-attempt-continuation/v1", "attempt-1", "item-1", 1, "owner-task", False, False)
        continuations: tuple[query_models.AttemptContinuation, ...] = (
            query_models.ActiveAttemptContinuation(
                *common,
                query_models.ActionContinuation("continue:attempt-1", decision_models.ActionKind.CONTINUE, "Continue."),
                ("continue:attempt-1", "revise-item:item-1"),
                forbidden,
            ),
            query_models.ReviewAttemptContinuation(
                *common,
                query_models.ReviewContinuation("attempt-1", "candidate-1", "runtime-subagent"),
                ("accept-checkpoint:attempt-1", "return-for-correction:attempt-1"),
                forbidden,
            ),
            query_models.PausedAttemptContinuation(
                *common,
                query_models.ActionContinuation("resume:item-1", decision_models.ActionKind.RESUME, "Resume."),
                ("resume:item-1",),
                forbidden,
            ),
            query_models.BlockedAttemptContinuation(
                *common,
                query_models.DependencyContinuation(("dependency-1",)),
                ("resume:item-1",),
                forbidden,
            ),
            query_models.TerminalAttemptContinuation(
                "pinboard-attempt-continuation/v1",
                "attempt-1",
                "item-1",
                1,
                None,
                True,
                False,
                None,
                (),
                forbidden,
            ),
        )

        for continuation in continuations:
            with self.subTest(state=continuation.state):
                presented = mcp_server._mcp_attempt_continuation(continuation)
                encoded = msgspec.to_builtins(presented)
                assert isinstance(encoded, dict)
                self.assertEqual("attempt-1", encoded["attempt_id"])
                self.assertEqual("item-1", encoded["item_id"])
                self.assertNotIn("attempt_id", encoded.get("next_operation") or {})
                for action_identity in encoded["legal_actions"]:
                    self.assertEqual({"target", "action_kind"}, set(action_identity))

        with self.assertRaises(ValueError):
            contracts.ContinuationDependencies(("dependency-1", "dependency-1"))
        with self.assertRaises(ValueError):
            contracts.TerminalAttemptContinuation(
                "pinboard-attempt-continuation/v1",
                "attempt-1",
                "item-1",
                1,
                None,
                False,
                False,
                None,
                (),
                forbidden,
            )

    def test_sdk_stdio_workflow_discovery_and_verification_are_read_only(self) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        before = SQLiteWorkStore(roots.database_path).validated_snapshot()
        brief_reference = before.artifact_references[0]
        brief_bytes = (roots.work_root / brief_reference.selector).read_bytes()
        view_files = tuple(
            sorted(
                path.relative_to(roots.work_root) for path in (roots.work_root / "views").rglob("*") if path.is_file()
            )
        )

        async def scenario() -> tuple[CallToolResult, ...]:
            parameters = StdioServerParameters(command=sys.executable, args=["-m", "pinboard.mcp"], cwd=Path.cwd())
            async with (
                stdio_client(parameters) as streams,
                ClientSession(*streams) as session,
            ):
                await session.initialize()
                common: dict[str, contracts.JsonValue] = {
                    "project_root": str(project),
                    "work_root": str(roots.work_root),
                }
                continue_id: dict[str, contracts.JsonValue] = {"kind": "continue", "subject": "work-a-1"}
                return (
                    await session.call_tool(mcp_server.OVERVIEW_TOOL, common),
                    await session.call_tool(
                        mcp_server.ACTIONS_TOOL, _mcp_arguments(mcp_server.ACTIONS_TOOL, common | {"role": "observer"})
                    ),
                    await session.call_tool(
                        mcp_server.ACTIONS_TOOL, _mcp_arguments(mcp_server.ACTIONS_TOOL, common | {"role": "project"})
                    ),
                    await session.call_tool(
                        mcp_server.ACTIONS_TOOL,
                        _mcp_arguments(
                            mcp_server.ACTIONS_TOOL,
                            common
                            | {
                                "role": "project",
                                "action_id": continue_id,
                            },
                        ),
                    ),
                    await session.call_tool(
                        mcp_server.ACTIONS_TOOL,
                        _mcp_arguments(
                            mcp_server.ACTIONS_TOOL,
                            common
                            | {
                                "role": "worker",
                                "lease_id": "attempt-lease-a",
                                "generation": 3,
                            },
                        ),
                    ),
                    await session.call_tool(
                        mcp_server.ACTIONS_TOOL,
                        _mcp_arguments(
                            mcp_server.ACTIONS_TOOL,
                            common | {"role": "preparer", "lease_id": "missing-lease", "generation": 1},
                        ),
                    ),
                    await session.call_tool(
                        mcp_server.ACTIONS_TOOL,
                        _mcp_arguments(
                            mcp_server.ACTIONS_TOOL, common | {"role": "worker", "lease_id": "attempt-lease-a"}
                        ),
                    ),
                    await session.call_tool(
                        mcp_server.ATTEMPT_INSPECT_TOOL,
                        common | {"attempt_id": "work-a-1"},
                    ),
                    await session.call_tool(
                        mcp_server.ARTIFACT_VERIFY_TOOL,
                        common
                        | {
                            "artifact_ref_id": int(brief_reference.artifact_ref_id),
                            "selector": brief_reference.selector,
                            "sha256": brief_reference.content_sha256,
                            "size_bytes": brief_reference.size_bytes,
                        },
                    ),
                    await session.call_tool(
                        mcp_server.ARTIFACT_VERIFY_TOOL,
                        common
                        | {
                            "artifact_ref_id": int(brief_reference.artifact_ref_id),
                            "selector": brief_reference.selector,
                            "sha256": "0" * 64,
                            "size_bytes": brief_reference.size_bytes,
                        },
                    ),
                )

        (
            overview,
            observer,
            project_actions,
            selected_action,
            worker,
            preparer,
            invalid_worker,
            attempt,
            verified,
            mismatch,
        ) = _run_async(scenario())
        for successful in (overview, observer, project_actions, selected_action, worker, attempt, verified):
            self.assertFalse(successful.is_error)
            self.assertIsInstance(successful.structured_content, dict)
        overview_content = overview.structured_content
        assert isinstance(overview_content, dict)
        self.assertEqual("pinboard-overview/v5", overview_content["schema"])
        self.assertEqual("sqlite-v6", overview_content["authority"])
        self.assertEqual(["work-a-1"], overview_content["active_attempts"])
        observer_content = observer.structured_content
        assert isinstance(observer_content, dict)
        self.assertEqual(["inspect"], [value["action_id"]["kind"] for value in observer_content["actions"]])
        project_content = project_actions.structured_content
        assert isinstance(project_content, dict)
        self.assertTrue(project_content["actions"])
        self.assertTrue(all(value["input_contract"] is not None for value in project_content["actions"]))
        selected_content = selected_action.structured_content
        assert isinstance(selected_content, dict)
        self.assertEqual(
            [{"kind": "continue", "subject": "work-a-1"}],
            [value["action_id"] for value in selected_content["actions"]],
        )
        worker_content = worker.structured_content
        assert isinstance(worker_content, dict)
        self.assertIn("continue", [value["action_id"]["kind"] for value in worker_content["actions"]])
        for failure, code in (
            (preparer, "ACTION_NOT_AVAILABLE"),
            (invalid_worker, "ACTIONS_INVALID"),
            (mismatch, "ARTIFACT_REFERENCE_MISMATCH"),
        ):
            self.assertFalse(failure.is_error)
            failure_content = failure.structured_content
            assert isinstance(failure_content, dict)
            self.assertEqual(code, failure_content["code"])
            self.assertFalse(failure_content["state_changed"])
            self.assertEqual([], failure_content["changed_surfaces"])
        attempt_content = attempt.structured_content
        assert isinstance(attempt_content, dict)
        self.assertEqual("work-a-1", attempt_content["continuation"]["attempt_id"])
        self.assertEqual(brief_reference.content_sha256, attempt_content["accepted_brief"]["sha256"])
        verified_content = verified.structured_content
        assert isinstance(verified_content, dict)
        self.assertEqual("pinboard-verified-artifact-reference/v1", verified_content["schema"])
        self.assertTrue(verified_content["verified"])
        self.assertEqual(before, SQLiteWorkStore(roots.database_path).validated_snapshot())
        self.assertEqual(brief_bytes, (roots.work_root / brief_reference.selector).read_bytes())
        self.assertEqual(
            view_files,
            tuple(
                sorted(
                    path.relative_to(roots.work_root)
                    for path in (roots.work_root / "views").rglob("*")
                    if path.is_file()
                )
            ),
        )

    def test_workflow_read_handlers_cover_success_and_structured_rejections(self) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        token = mcp_server.CancellationToken()
        reference = SQLiteWorkStore(roots.database_path).validated_snapshot().artifact_references[0]
        common = (str(project), str(roots.work_root))

        results = (
            (mcp_server.OVERVIEW_TOOL, mcp_server._read_overview(*common, token)),
            (mcp_server.OVERVIEW_TOOL, mcp_server._read_overview("", str(roots.work_root), token)),
            (
                mcp_server.ACTIONS_TOOL,
                mcp_server._read_actions(
                    {"request": {"project_root": common[0], "work_root": common[1], "role": "observer"}}, token
                ),
            ),
            (
                mcp_server.ACTIONS_TOOL,
                mcp_server._read_actions(
                    {"request": {"project_root": common[0], "work_root": common[1], "role": "project"}}, token
                ),
            ),
            (
                mcp_server.ACTIONS_TOOL,
                mcp_server._read_actions(
                    {
                        "request": {
                            "project_root": common[0],
                            "work_root": common[1],
                            "role": "worker",
                            "lease_id": "attempt-lease-a",
                            "generation": 3,
                            "action_id": None,
                        }
                    },
                    token,
                ),
            ),
            (
                mcp_server.ACTIONS_TOOL,
                mcp_server._read_actions(
                    {
                        "request": {
                            "project_root": common[0],
                            "work_root": common[1],
                            "role": "preparer",
                            "lease_id": "missing-lease",
                            "generation": 1,
                            "action_id": None,
                        }
                    },
                    token,
                ),
            ),
            (
                mcp_server.ACTIONS_TOOL,
                mcp_server._read_actions(
                    {
                        "request": {
                            "project_root": common[0],
                            "work_root": common[1],
                            "role": "worker",
                            "lease_id": "attempt-lease-a",
                            "generation": None,
                            "action_id": None,
                        }
                    },
                    token,
                ),
            ),
            (
                mcp_server.ACTIONS_TOOL,
                mcp_server._read_actions(
                    {
                        "request": {
                            "project_root": common[0],
                            "work_root": common[1],
                            "role": "project",
                            "action_id": {"kind": "continue", "subject": "missing"},
                        }
                    },
                    token,
                ),
            ),
            (
                mcp_server.ATTEMPT_INSPECT_TOOL,
                mcp_server._read_attempt_inspection(*common, "work-a-1", token),
            ),
            (
                mcp_server.ATTEMPT_INSPECT_TOOL,
                mcp_server._read_attempt_inspection(*common, "missing-attempt", token),
            ),
            (
                mcp_server.ATTEMPT_INSPECT_TOOL,
                mcp_server._read_attempt_inspection(*common, "", token),
            ),
            (
                mcp_server.ARTIFACT_VERIFY_TOOL,
                mcp_server._verify_artifact(
                    *common,
                    int(reference.artifact_ref_id),
                    reference.selector,
                    reference.content_sha256,
                    reference.size_bytes,
                    token,
                ),
            ),
            (
                mcp_server.ARTIFACT_VERIFY_TOOL,
                mcp_server._verify_artifact(
                    *common,
                    999,
                    reference.selector,
                    reference.content_sha256,
                    reference.size_bytes,
                    token,
                ),
            ),
            (
                mcp_server.ARTIFACT_VERIFY_TOOL,
                mcp_server._verify_artifact(
                    *common,
                    int(reference.artifact_ref_id),
                    reference.selector,
                    "0" * 64,
                    reference.size_bytes,
                    token,
                ),
            ),
            (
                mcp_server.ARTIFACT_VERIFY_TOOL,
                mcp_server._verify_artifact(
                    *common,
                    int(reference.artifact_ref_id),
                    "",
                    reference.content_sha256,
                    reference.size_bytes,
                    token,
                ),
            ),
        )
        for tool_name, result in results:
            with self.subTest(tool_name=tool_name, classification=result.classification):
                self.assertEqual(result.content, contracts.validate_result(tool_name, result.content))

        artifact_path = roots.work_root / reference.selector
        artifact_path.write_bytes(b"corrupt")
        corrupted = mcp_server._verify_artifact(
            *common,
            int(reference.artifact_ref_id),
            reference.selector,
            reference.content_sha256,
            reference.size_bytes,
            token,
        )
        self.assertEqual("ARTIFACT_BYTES_INVALID", corrupted.content["code"])
        self.assertEqual(
            corrupted.content,
            contracts.validate_result(mcp_server.ARTIFACT_VERIFY_TOOL, corrupted.content),
        )

    def test_mcp_cancellation_releases_running_and_queued_admission(self) -> None:  # noqa: PLR0915 - one MCP cancellation journey
        executor = mcp_server.BoundedExecutor(worker_count=1, unfinished_limit=2)
        diagnostics_stream = io.StringIO()
        diagnostics = mcp_server.Diagnostics(diagnostics_stream, event_limit=16, line_limit=256)
        server = mcp_server.create_server(executor, diagnostics)
        original_emit = diagnostics.emit
        running_started = threading.Event()
        running_release = threading.Event()
        queued_effect = threading.Event()
        cooperative_cancelled = threading.Event()
        cancellation_events = (threading.Event(), threading.Event())
        token_cancellation_events = (threading.Event(), threading.Event())
        cancellations_lock = threading.Lock()
        cancellation_count = 0
        token_cancellation_count = 0
        calls_lock = threading.Lock()
        call_count = 0
        admitted = (threading.Event(), threading.Event())
        executions: list[mcp_server.Execution[mcp_server.OperationResult]] = []
        original_submit = executor.submit
        original_cancel = mcp_server.CancellationToken.cancel

        def controlled_read(
            _project_root: str,
            _work_root: str,
            _item_id: str,
            token: mcp_server.CancellationToken,
        ) -> mcp_server.OperationResult:
            nonlocal call_count
            with calls_lock:
                call_count += 1
                if call_count > 1:
                    queued_effect.set()
            running_started.set()
            if not running_release.wait(2):
                raise AssertionError("The MCP operation was not released.")
            try:
                token.checkpoint()
            except mcp_server.OperationCancelled:
                cooperative_cancelled.set()
                raise
            return mcp_server.OperationResult({}, "ok", None)

        def observed_submit(
            callback: Callable[[mcp_server.CancellationToken], mcp_server.OperationResult],
        ) -> mcp_server.Execution[mcp_server.OperationResult]:
            execution = original_submit(callback)
            executions.append(execution)
            admitted[len(executions) - 1].set()
            return execution

        def observed_emit(
            *,
            event: str,
            request_id: int | None,
            operation: str | None,
            project_id: str | None,
            duration_ms: int | None,
            classification: str | None,
            commit_reference: str | None,
        ) -> None:
            nonlocal cancellation_count
            original_emit(
                event=event,
                request_id=request_id,
                operation=operation,
                project_id=project_id,
                duration_ms=duration_ms,
                classification=classification,
                commit_reference=commit_reference,
            )
            if classification == "cancelled":
                with cancellations_lock:
                    cancellation_events[cancellation_count].set()
                    cancellation_count += 1

        def observed_cancel(token: mcp_server.CancellationToken) -> None:
            nonlocal token_cancellation_count
            original_cancel(token)
            with cancellations_lock:
                token_cancellation_events[token_cancellation_count].set()
                token_cancellation_count += 1

        async def scenario() -> CallToolResult:
            client_send, server_receive = anyio.create_memory_object_stream[SessionMessage | Exception](0)
            server_send, client_receive = anyio.create_memory_object_stream[SessionMessage](0)
            lowlevel = server._lowlevel_server

            async def run_server() -> None:
                await lowlevel.run(
                    server_receive,
                    server_send,
                    lowlevel.create_initialization_options(),
                )

            async with server_receive, server_send, client_receive, anyio.create_task_group() as server_tasks:
                server_tasks.start_soon(run_server)
                async with client_send, ClientSession(client_receive, client_send) as session:
                    await session.initialize()
                    arguments = {"project_root": "/project", "work_root": "/work", "item_id": "item"}
                    with (
                        patch.object(mcp_server, "_read_item_status", controlled_read),
                        patch.object(executor, "submit", observed_submit),
                        patch.object(diagnostics, "emit", observed_emit),
                        patch.object(mcp_server.CancellationToken, "cancel", observed_cancel),
                    ):
                        active = asyncio.create_task(session.call_tool(mcp_server.ITEM_STATUS_TOOL, arguments))
                        await _wait_for(admitted[0])
                        await _wait_for(running_started)
                        queued = asyncio.create_task(session.call_tool(mcp_server.ITEM_STATUS_TOOL, arguments))
                        await _wait_for(admitted[1])

                        busy = await session.call_tool(mcp_server.ITEM_STATUS_TOOL, arguments)
                        if not isinstance(busy, CallToolResult):
                            raise AssertionError("The saturated tool returned an unexpected MCP result.")

                        queued.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await queued
                        await _wait_for(cancellation_events[0])
                        await _wait_for(executions[1].finished)
                        self.assertFalse(queued_effect.is_set())

                        active.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await active
                        self.assertFalse(cancellation_events[1].is_set())
                        await _wait_for(token_cancellation_events[1])
                        running_release.set()
                        await _wait_for(cancellation_events[1])
                        await _wait_for(executions[0].finished)
                await client_send.aclose()

            self.assertTrue(cooperative_cancelled.is_set())
            replacement = executor.submit(lambda token: token.cancelled)
            self.assertFalse(await replacement.result())
            return busy

        try:
            busy = _run_async(scenario())
        finally:
            running_release.set()
            executor.shutdown()

        self.assertEqual(2, diagnostics_stream.getvalue().count("classification=cancelled"))
        busy_content = busy.structured_content
        self.assertIsInstance(busy_content, dict)
        self.assertEqual("busy", busy_content["status"])
        self.assertEqual("EXECUTOR_BUSY", busy_content["code"])
        self.assertFalse(busy_content["state_changed"])
        self.assertEqual("retry-same-input", busy_content["retry"])
        self.assertIn("classification=busy", diagnostics_stream.getvalue())

    def test_running_mutation_cancellation_waits_for_committed_result(self) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=4)
        diagnostics_stream = io.StringIO()
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(diagnostics_stream, event_limit=16, line_limit=256)
        )
        committed = threading.Event()
        release = threading.Event()
        original_refresh = mcp_server._refresh_affected_views

        def delayed_refresh(
            durable: DurableRoots,
            store: SQLiteWorkStore,
            affected: mcp_server.AffectedViews,
            now: datetime,
        ) -> ViewRefreshResult:
            result = original_refresh(durable, store, affected, now)
            committed.set()
            if not release.wait(2):
                raise AssertionError("The committed mutation was not released.")
            return result

        async def scenario() -> CallToolResult:
            with patch.object(mcp_server, "_refresh_affected_views", delayed_refresh):
                request = asyncio.create_task(
                    server.call_tool(
                        mcp_server.PROPOSAL_CREATE_TOOL,
                        {
                            "project_root": str(project),
                            "work_root": str(roots.work_root),
                            "proposal": proposal_input(),
                            "actor_task_id": "mcp-test-task",
                            "actor_host_id": "local",
                        },
                    )
                )
                await _wait_for(committed)
                request.cancel()
                await asyncio.sleep(0)
                self.assertFalse(request.done())
                release.set()
                result = await request
            if not isinstance(result, CallToolResult):
                raise AssertionError("The mutation tool returned an unexpected MCP result.")
            return result

        try:
            result = _run_async(scenario())
        finally:
            release.set()
            executor.shutdown()

        content = result.structured_content
        self.assertIsInstance(content, dict)
        self.assertEqual("committed", content["status"])
        self.assertTrue(content["state_changed"])
        self.assertIsNotNone(SQLiteWorkStore(roots.database_path).read_item_status(ItemId("proposal-1")))
        diagnostics = diagnostics_stream.getvalue()
        self.assertIn("classification=committed", diagnostics)
        self.assertIn("commit=", diagnostics)

    def test_concurrent_calls_keep_request_and_store_identity_isolated(self) -> None:
        first_temporary, first_project, first_roots = self._project()
        second_temporary, second_project, second_roots = self._project()
        self.addCleanup(first_temporary.cleanup)
        self.addCleanup(second_temporary.cleanup)
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=2)
        diagnostics = mcp_server.Diagnostics(io.StringIO(), event_limit=16, line_limit=256)
        server = mcp_server.create_server(executor, diagnostics)
        barrier = threading.Barrier(2)
        stores: list[tuple[int, Path, SQLiteWorkStore]] = []
        stores_lock = threading.Lock()

        def compose(durable: DurableRoots) -> SQLiteWorkStore:
            store = SQLiteWorkStore(durable.database_path)
            with stores_lock:
                stores.append((threading.get_ident(), durable.database_path, store))
            barrier.wait(2)
            return store

        async def scenario() -> tuple[CallToolResult, CallToolResult]:
            arguments = (
                {
                    "project_root": str(first_project),
                    "work_root": str(first_roots.work_root),
                    "item_id": "work-a",
                },
                {
                    "project_root": str(second_project),
                    "work_root": str(second_roots.work_root),
                    "item_id": "work-c",
                },
            )
            with patch.object(mcp_server, "compose_store", compose):
                first, second = await asyncio.gather(
                    *(server.call_tool(mcp_server.ITEM_STATUS_TOOL, value) for value in arguments)
                )
            if not isinstance(first, CallToolResult) or not isinstance(second, CallToolResult):
                raise AssertionError("The representative tool returned an unexpected MCP result.")
            return first, second

        try:
            first, second = _run_async(scenario())
        finally:
            executor.shutdown()

        self.assertEqual(self._expected_bytes(first_roots, "work-a"), msgspec.json.encode(first.structured_content))
        self.assertEqual(self._expected_bytes(second_roots, "work-c"), msgspec.json.encode(second.structured_content))
        self.assertEqual(2, len({id(store) for _thread, _path, store in stores}))
        self.assertEqual(
            {first_roots.database_path, second_roots.database_path}, {path for _thread, path, _store in stores}
        )
        self.assertEqual(2, len({thread for thread, _path, _store in stores}))
        self.assertNotIn(threading.get_ident(), {thread for thread, _path, _store in stores})

    def test_structured_rejections_leave_ledger_unchanged_and_duplicate_is_recoverable(self) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=4)
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(io.StringIO(), event_limit=16, line_limit=256)
        )
        store = SQLiteWorkStore(roots.database_path)
        before = store.validated_snapshot()
        invalid = {**proposal_input(), "unexpected": True}
        arguments = {
            "project_root": str(project),
            "work_root": str(roots.work_root),
            "actor_task_id": "mcp-test-task",
            "actor_host_id": "local",
        }

        async def scenario() -> tuple[CallToolResult, stored_state.StoredWorkState, CallToolResult, CallToolResult]:
            rejected = await server.call_tool(
                mcp_server.PROPOSAL_CREATE_TOOL,
                {**arguments, "proposal": invalid},
            )
            after_rejection = store.validated_snapshot()
            created = await server.call_tool(
                mcp_server.PROPOSAL_CREATE_TOOL,
                {**arguments, "proposal": proposal_input()},
            )
            duplicate = await server.call_tool(
                mcp_server.PROPOSAL_CREATE_TOOL,
                {**arguments, "proposal": proposal_input()},
            )
            if not isinstance(rejected, CallToolResult):
                raise AssertionError("The mutation tool returned an unexpected MCP result.")
            if not isinstance(created, CallToolResult):
                raise AssertionError("The mutation tool returned an unexpected MCP result.")
            if not isinstance(duplicate, CallToolResult):
                raise AssertionError("The mutation tool returned an unexpected MCP result.")
            return rejected, after_rejection, created, duplicate

        try:
            rejected, after_rejection, created, duplicate = _run_async(scenario())
        finally:
            executor.shutdown()

        rejected_content = rejected.structured_content
        created_content = created.structured_content
        duplicate_content = duplicate.structured_content
        self.assertIsInstance(rejected_content, dict)
        self.assertIsInstance(created_content, dict)
        self.assertIsInstance(duplicate_content, dict)
        self.assertEqual("rejected", rejected_content["status"])
        self.assertFalse(rejected_content["state_changed"])
        self.assertEqual(before, after_rejection)
        self.assertEqual("committed", created_content["status"])
        self.assertEqual("PROPOSAL_ALREADY_EXISTS", duplicate_content["code"])
        self.assertEqual("do-not-retry", duplicate_content["retry"])
        self.assertIn("Read item status", duplicate_content["recovery"])
        self.assertFalse(duplicate_content["state_changed"])

    def test_invalid_actor_identity_is_structured_and_leaves_fresh_store_unchanged(self) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=4)
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(io.StringIO(), event_limit=16, line_limit=256)
        )
        before = SQLiteWorkStore(roots.database_path).validated_snapshot()

        async def scenario() -> tuple[CallToolResult, CallToolResult]:
            common = {
                "project_root": str(project),
                "work_root": str(roots.work_root),
                "proposal": proposal_input(),
            }
            empty = await server.call_tool(
                mcp_server.PROPOSAL_CREATE_TOOL,
                {**common, "actor_task_id": "", "actor_host_id": "local"},
            )
            malformed = await server.call_tool(
                mcp_server.PROPOSAL_CREATE_TOOL,
                {**common, "actor_task_id": "mcp-test-task", "actor_host_id": "local/host"},
            )
            if not isinstance(empty, CallToolResult) or not isinstance(malformed, CallToolResult):
                raise AssertionError("The mutation tool returned an unexpected MCP result.")
            return empty, malformed

        try:
            results = _run_async(scenario())
        finally:
            executor.shutdown()

        for result in results:
            content = result.structured_content
            self.assertIsInstance(content, dict)
            self.assertEqual("rejected", content["status"])
            self.assertEqual("PROPOSAL_INVALID", content["code"])
            self.assertFalse(content["state_changed"])
            self.assertEqual("unchanged", content["effect"])
            self.assertEqual("correct-input", content["retry"])
            self.assertEqual([], content["changed_surfaces"])
        self.assertEqual(before, SQLiteWorkStore(roots.database_path).validated_snapshot())

    def test_invalid_roots_are_rejected_before_durable_state_resolution(self) -> None:
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=4)
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(io.StringIO(), event_limit=16, line_limit=256)
        )
        requests = (
            (
                mcp_server.ITEM_STATUS_TOOL,
                {"project_root": "/project", "work_root": "/work", "item_id": "work-a"},
                "ITEM_STATUS_INVALID",
            ),
            (
                mcp_server.PROPOSAL_CREATE_TOOL,
                {
                    "project_root": "/project",
                    "work_root": "/work",
                    "proposal": proposal_input(),
                    "actor_task_id": "mcp-test-task",
                    "actor_host_id": "local",
                },
                "PROPOSAL_INVALID",
            ),
            (
                mcp_server.BRIEF_PUBLISH_TOOL,
                {
                    "project_root": "/project",
                    "work_root": "/work",
                    "brief": msgspec.to_builtins(example_work_brief()),
                },
                "WORK_BRIEF_INVALID",
            ),
        )

        async def scenario() -> None:
            for tool_name, arguments, expected_code in requests:
                for field in ("project_root", "work_root"):
                    for invalid_root in ("", "\x00", "/project/\x00child"):
                        result = await server.call_tool(
                            tool_name, _mcp_arguments(tool_name, {**arguments, field: invalid_root})
                        )
                        if not isinstance(result, CallToolResult):
                            raise AssertionError("The invalid request returned an unexpected MCP result.")
                        content = result.structured_content
                        self.assertIsInstance(content, dict)
                        self.assertEqual("rejected", content["status"])
                        self.assertEqual(expected_code, content["code"])
                        self.assertFalse(content["state_changed"])

        try:
            with patch.object(mcp_server, "_resolve_durable") as resolve_durable:
                _run_async(scenario())
            resolve_durable.assert_not_called()
        finally:
            executor.shutdown()

    def test_post_commit_reply_loss_preserves_proposal_and_brief_for_fresh_store_recovery(self) -> None:
        for operation in (mcp_server.PROPOSAL_CREATE_TOOL, mcp_server.BRIEF_PUBLISH_TOOL):
            with self.subTest(operation=operation):
                temporary, project, roots = self._project()
                self.addCleanup(temporary.cleanup)
                executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=4)
                server = mcp_server.create_server(
                    executor,
                    mcp_server.Diagnostics(io.StringIO(), event_limit=16, line_limit=256),
                )
                if operation == mcp_server.PROPOSAL_CREATE_TOOL:
                    arguments = {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "proposal": proposal_input(),
                        "actor_task_id": "mcp-test-task",
                        "actor_host_id": "local",
                    }
                else:
                    arguments = {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "brief": msgspec.to_builtins(example_work_brief()),
                    }
                with (
                    patch.object(mcp_server, "_refresh_affected_views", side_effect=RuntimeError("reply lost")),
                    self.assertRaises(UnexpectedToolError),
                ):
                    _run_async(server.call_tool(operation, _mcp_arguments(operation, arguments)))
                executor.shutdown()

                reopened = SQLiteWorkStore(roots.database_path)
                if operation == mcp_server.PROPOSAL_CREATE_TOOL:
                    self.assertIsNotNone(reopened.read_item_status(ItemId("proposal-1")))
                else:
                    reference = reopened.read_artifact_reference(
                        kind=work_models.ArtifactKind.BRIEF,
                        key="make-canonical-briefs-typed-json-1",
                        revision=1,
                    )
                    self.assertIsNotNone(reference)

    def test_brief_acceptance_failure_reports_discoverable_published_artifact(self) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=4)
        diagnostics_stream = io.StringIO()
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(diagnostics_stream, event_limit=16, line_limit=256)
        )
        brief = example_work_brief()
        references_before = SQLiteWorkStore(roots.database_path).validated_snapshot().artifact_references

        with patch.object(
            SQLiteWorkStore,
            "accept_artifact_reference",
            side_effect=WorkStoreError("database unavailable"),
        ):
            result = _run_async(
                server.call_tool(
                    mcp_server.BRIEF_PUBLISH_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "brief": msgspec.to_builtins(brief),
                    },
                )
            )
        executor.shutdown()

        if not isinstance(result, CallToolResult):
            raise AssertionError("The mutation tool returned an unexpected MCP result.")
        content = result.structured_content
        self.assertIsInstance(content, dict)
        self.assertEqual("failed-after-publication", content["status"])
        self.assertEqual("ARTIFACT_ACCEPTANCE_FAILED", content["code"])
        self.assertTrue(content["state_changed"])
        self.assertEqual("committed", content["effect"])
        self.assertEqual("do-not-retry", content["retry"])
        self.assertEqual(["immutable-artifact"], content["changed_surfaces"])
        selector = content["published_selector"]
        self.assertIsInstance(selector, str)
        self.assertEqual(canonical_work_brief_bytes(brief), (roots.work_root / selector).read_bytes())
        self.assertEqual(
            references_before,
            SQLiteWorkStore(roots.database_path).validated_snapshot().artifact_references,
        )
        diagnostics = diagnostics_stream.getvalue()
        self.assertIn("classification=infrastructure-failure", diagnostics)
        self.assertIn(f"commit={selector}", diagnostics)

    def test_view_refresh_failure_reports_committed_warning_and_rebuild_recovery(self) -> None:
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        executor = mcp_server.BoundedExecutor(worker_count=2, unfinished_limit=4)
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(io.StringIO(), event_limit=16, line_limit=256)
        )
        warning = ViewRefreshResult(
            12,
            ViewWarning("Generated views need repair.", "Run 'pinboard views rebuild'."),
        )
        with patch.object(mcp_server, "_refresh_affected_views", return_value=warning):
            result = _run_async(
                server.call_tool(
                    mcp_server.PROPOSAL_CREATE_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "proposal": proposal_input(),
                        "actor_task_id": "mcp-test-task",
                        "actor_host_id": "local",
                    },
                )
            )
        executor.shutdown()
        if not isinstance(result, CallToolResult):
            raise AssertionError("The mutation tool returned an unexpected MCP result.")
        content = result.structured_content
        self.assertIsInstance(content, dict)
        self.assertEqual("committed-with-warning", content["status"])
        self.assertEqual("do-not-retry", content["retry"])
        warning_content = content["warning"]
        self.assertIsInstance(warning_content, dict)
        self.assertEqual("Run 'pinboard views rebuild'.", warning_content["recovery"])
        self.assertIsNotNone(SQLiteWorkStore(roots.database_path).read_item_status(ItemId("proposal-1")))

    def test_server_rejects_an_internal_result_that_violates_its_advertised_contract(self) -> None:
        executor = mcp_server.BoundedExecutor(worker_count=1, unfinished_limit=1)
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(io.StringIO(), event_limit=8, line_limit=256)
        )

        def contradictory_result(
            _project_root: str,
            _work_root: str,
            _item_id: str,
            _token: mcp_server.CancellationToken,
        ) -> mcp_server.OperationResult:
            return mcp_server.OperationResult(
                {
                    "schema": "pinboard-mcp-execution-result/v1",
                    "status": "busy",
                    "code": "EXECUTOR_BUSY",
                    "message": "Busy.",
                    "state_changed": True,
                    "effect": "unchanged",
                    "retry": "retry-same-input",
                    "changed_surfaces": [],
                    "observed": [],
                    "mismatches": [],
                },
                "busy",
                None,
            )

        try:
            with (
                patch.object(mcp_server, "_read_item_status", contradictory_result),
                self.assertRaises(UnexpectedToolError),
            ):
                _run_async(
                    server.call_tool(
                        mcp_server.ITEM_STATUS_TOOL,
                        {"project_root": "/project", "work_root": "/work", "item_id": "item"},
                    )
                )
        finally:
            executor.shutdown()

    def test_sdk_stdio_negotiates_discovers_reads_and_rejects_unknown_tool(self) -> None:  # noqa: PLR0915
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        transport_errors: list[Exception] = []

        async def record_transport_error(message: IncomingMessage) -> None:
            if isinstance(message, Exception):
                transport_errors.append(message)

        async def scenario(
            diagnostics: io.TextIOWrapper,
        ) -> tuple[
            CallToolResult,
            tuple[Tool, ...],
            CallToolResult,
            CallToolResult,
            CallToolResult,
            CallToolResult,
        ]:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "pinboard.mcp"],
                cwd=Path.cwd(),
            )
            async with (
                stdio_client(parameters, errlog=diagnostics) as streams,
                ClientSession(*streams, message_handler=record_transport_error) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool(
                    mcp_server.ITEM_STATUS_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "item_id": "work-a",
                    },
                )
                rejected = await session.call_tool("unsupported_tool", {})
                invalid = await session.call_tool(
                    mcp_server.ITEM_STATUS_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "item_id": "",
                    },
                )
                missing = await session.call_tool(
                    mcp_server.ITEM_STATUS_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "item_id": "missing-item",
                    },
                )
                unknown_field = await session.call_tool(
                    mcp_server.ITEM_STATUS_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "item_id": "work-a",
                        "unknown": True,
                    },
                )
                return result, tuple(tools.tools), rejected, invalid, missing, unknown_field

        with tempfile.TemporaryFile(mode="w+") as diagnostics:
            result, tools, rejected, invalid, missing, unknown_field = _run_async(scenario(diagnostics))
            diagnostics.seek(0)
            stderr = diagnostics.read()
        self.assertEqual(
            {
                mcp_server.ITEM_STATUS_TOOL,
                mcp_server.PROPOSAL_CREATE_TOOL,
                mcp_server.BRIEF_PUBLISH_TOOL,
                mcp_server.OVERVIEW_TOOL,
                mcp_server.ACTIONS_TOOL,
                mcp_server.ATTEMPT_INSPECT_TOOL,
                mcp_server.ARTIFACT_VERIFY_TOOL,
                mcp_server.PREPARATION_AUTHORITY_TOOL,
                mcp_server.ATTEMPT_AUTHORITY_TOOL,
                mcp_server.TRANSITION_TOOL,
                mcp_server.DISPATCH_TOOL,
                mcp_server.REVIEW_JOB_TOOL,
                mcp_server.ITEM_DEFINITION_TOOL,
                mcp_server.BRIEF_REVIEW_TOOL,
                mcp_server.BRIEF_CONTRACT_TOOL,
                mcp_server.BRIEF_SOURCES_TOOL,
                mcp_server.ORDER_TOOL,
                mcp_server.PARALLEL_PREVIEW_TOOL,
            },
            {tool.name for tool in tools},
        )
        tools_by_name = {tool.name: tool for tool in tools}
        expected_required = {
            mcp_server.DISPATCH_TOOL: {"project_root", "work_root", "dispatch"},
            mcp_server.REVIEW_JOB_TOOL: {"project_root", "work_root", "review"},
            mcp_server.ITEM_STATUS_TOOL: {"project_root", "work_root", "item_id"},
            mcp_server.PROPOSAL_CREATE_TOOL: {
                "project_root",
                "work_root",
                "proposal",
                "actor_task_id",
                "actor_host_id",
            },
            mcp_server.BRIEF_PUBLISH_TOOL: {"project_root", "work_root", "brief"},
        }
        for tool_name, required in expected_required.items():
            tool = tools_by_name[tool_name]
            self.assertFalse(tool.input_schema["additionalProperties"])
            self.assertEqual(required, set(tool.input_schema["required"]))
            self.assertEqual(1, tool.input_schema["properties"]["project_root"]["minLength"])
            self.assertEqual(1, tool.input_schema["properties"]["work_root"]["minLength"])
            self.assertEqual(r"\A[^\x00]+\z", tool.input_schema["properties"]["project_root"]["pattern"])
            self.assertEqual(r"\A[^\x00]+\z", tool.input_schema["properties"]["work_root"]["pattern"])
            self.assertIn("$defs", tool.input_schema)
            self.assertIsNotNone(tool.output_schema)
            assert tool.output_schema is not None
            self.assertIn("anyOf", tool.output_schema)
            self.assertIn("$defs", tool.output_schema)
        item_schema = tools_by_name[mcp_server.ITEM_STATUS_TOOL].input_schema
        self.assertEqual(r"\A(?!\.{1,2}\z)[^/\r\n\x00]+\z", item_schema["properties"]["item_id"]["pattern"])
        proposal_schema = tools_by_name[mcp_server.PROPOSAL_CREATE_TOOL].input_schema
        self.assertEqual("#/$defs/Proposal", proposal_schema["properties"]["proposal"]["$ref"])
        self.assertFalse(proposal_schema["$defs"]["Proposal"]["additionalProperties"])
        brief_schema = tools_by_name[mcp_server.BRIEF_PUBLISH_TOOL].input_schema
        self.assertEqual("#/$defs/WorkBrief", brief_schema["properties"]["brief"]["$ref"])
        self.assertFalse(brief_schema["$defs"]["WorkBrief"]["additionalProperties"])
        brief_output_schema = tools_by_name[mcp_server.BRIEF_PUBLISH_TOOL].output_schema
        assert brief_output_schema is not None
        reference_schema = brief_output_schema["$defs"]["ArtifactReferenceResult"]
        self.assertEqual(1, reference_schema["properties"]["artifact_ref_id"]["minimum"])
        self.assertEqual(1, reference_schema["properties"]["revision"]["minimum"])
        self.assertEqual(1, reference_schema["properties"]["size_bytes"]["minimum"])
        self.assertEqual(r"\A[0-9a-f]{64}\z", reference_schema["properties"]["sha256"]["pattern"])
        unchanged_schema = brief_output_schema["$defs"]["BriefUnchanged"]
        self.assertFalse(unchanged_schema["properties"]["state_changed"]["const"])
        self.assertEqual(["unchanged"], unchanged_schema["properties"]["effect"]["enum"])
        self.assertEqual(0, unchanged_schema["properties"]["changed_surfaces"]["maxItems"])
        committed_schema = brief_output_schema["$defs"]["BriefCommitted"]
        self.assertTrue(committed_schema["properties"]["state_changed"]["const"])
        self.assertEqual(3, committed_schema["properties"]["changed_surfaces"]["minItems"])
        self.assertEqual({"type": "null"}, committed_schema["properties"]["warning"])
        self.assertEqual(self._expected_bytes(roots, "work-a"), msgspec.json.encode(result.structured_content))
        self.assertTrue(rejected.is_error)
        self.assertEqual(1, len(rejected.content))
        self.assertIsInstance(rejected.content[0], TextContent)
        self.assertIn("Unknown tool", rejected.content[0].text)
        for failure, code in ((invalid, "ITEM_STATUS_INVALID"), (missing, "ITEM_NOT_FOUND")):
            self.assertFalse(failure.is_error)
            content = failure.structured_content
            self.assertIsInstance(content, dict)
            self.assertEqual("rejected", content["status"])
            self.assertEqual(code, content["code"])
            self.assertFalse(content["state_changed"])
            self.assertEqual([], content["changed_surfaces"])
        self.assertTrue(unknown_field.is_error)
        self.assertLessEqual(len(stderr.encode()), 2_048)
        self.assertNotIn(str(project), stderr)
        self.assertNotIn("work-a", stderr)
        self.assertIn("classification=ok", stderr)
        self.assertEqual([], transport_errors)

    def test_sdk_stdio_creates_proposal_and_publishes_idempotent_brief(self) -> None:  # noqa: PLR0915 - one installed transport journey
        temporary, project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        proposal = proposal_input()
        brief_value = example_work_brief()
        brief = msgspec.to_builtins(brief_value)
        self.assertIsInstance(brief, dict)

        async def scenario(diagnostics: io.TextIOWrapper) -> tuple[CallToolResult, CallToolResult, CallToolResult]:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "pinboard.mcp"],
                cwd=Path.cwd(),
            )
            async with (
                stdio_client(parameters, errlog=diagnostics) as streams,
                ClientSession(*streams) as session,
            ):
                await session.initialize()
                created = await session.call_tool(
                    mcp_server.PROPOSAL_CREATE_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "proposal": proposal,
                        "actor_task_id": "mcp-test-task",
                        "actor_host_id": "local",
                    },
                )
                published = await session.call_tool(
                    mcp_server.BRIEF_PUBLISH_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "brief": brief,
                    },
                )
                repeated = await session.call_tool(
                    mcp_server.BRIEF_PUBLISH_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "brief": brief,
                    },
                )
                repeated_content = repeated.structured_content
                if not isinstance(repeated_content, dict):
                    raise AssertionError("The repeated publication did not return structured content.")
                contradictory = CallToolResult(
                    content=[],
                    structured_content={**repeated_content, "effect": "committed"},
                )
                with self.assertRaisesRegex(RuntimeError, "Invalid structured content"):
                    await session.validate_tool_result(mcp_server.BRIEF_PUBLISH_TOOL, contradictory)
                return created, published, repeated

        with tempfile.TemporaryFile(mode="w+") as diagnostics:
            created, published, repeated = _run_async(scenario(diagnostics))
            diagnostics.seek(0)
            stderr = diagnostics.read()

        created_content = created.structured_content
        published_content = published.structured_content
        repeated_content = repeated.structured_content
        self.assertIsInstance(created_content, dict)
        self.assertIsInstance(published_content, dict)
        self.assertIsInstance(repeated_content, dict)
        self.assertEqual("committed", created_content["status"])
        self.assertEqual("proposal-1", created_content["proposal_id"])
        self.assertEqual("committed", published_content["status"])
        self.assertTrue(published_content["state_changed"])
        self.assertEqual("committed", published_content["effect"])
        self.assertNotEqual([], published_content["changed_surfaces"])
        self.assertEqual("do-not-retry", published_content["retry"])
        self.assertEqual("unchanged", repeated_content["status"])
        self.assertFalse(repeated_content["state_changed"])
        self.assertEqual("unchanged", repeated_content["effect"])
        self.assertEqual([], repeated_content["changed_surfaces"])
        self.assertEqual("retry-same-input", repeated_content["retry"])
        self.assertEqual(published_content["reference"], repeated_content["reference"])

        reopened = SQLiteWorkStore(roots.database_path)
        status = reopened.read_item_status(ItemId("proposal-1"))
        self.assertIsNotNone(status)
        reference_content = published_content["reference"]
        self.assertIsInstance(reference_content, dict)
        reference = reopened.read_artifact_reference_by_id(ArtifactRefId(reference_content["artifact_ref_id"]))
        self.assertIsNotNone(reference)
        assert reference is not None
        self.assertEqual(canonical_work_brief_bytes(brief_value), ArtifactRepository(roots).read(reference))
        self.assertIn(f"operation={mcp_server.PROPOSAL_CREATE_TOOL}", stderr)
        self.assertIn(f"operation={mcp_server.BRIEF_PUBLISH_TOOL}", stderr)
        self.assertIn("classification=unchanged", stderr)
        self.assertIn("duration_ms=", stderr)
        self.assertIn("commit=", stderr)
        self.assertNotIn("proposal-1", stderr)
        self.assertNotIn(brief_value.title, stderr)


if __name__ == "__main__":
    unittest.main()
