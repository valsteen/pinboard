"""Exact-item cancellation through the installed MCP boundary and fresh SQLite reload."""

import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters.sqlite.lifecycle import move_item_state_count
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import cancellation, cancellation_models, pr_reviews, project_export, service, stored_state
from pinboard.domain import authority_models, work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import AttemptId, HostId, LeaseId, TaskId, WorkItemId
from pinboard.mcp import contracts
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.domain_support import expect_success
from tests.native_support import call_advertised_tool
from tests.support import SQLITE_NOW, JsonObject


class CancellationTest(CheckpointPackageSupport):
    def request(self, fixture: CheckpointFixture, operation: str, item_id: str) -> JsonObject:
        return {
            "operation": operation,
            "project_root": str(fixture.project),
            "work_root": str(fixture.work),
            "item_id": item_id,
        }

    def preview(self, fixture: CheckpointFixture, item_id: str) -> JsonObject:
        result = call_advertised_tool(mcp_server.CANCEL_TOOL, {"request": self.request(fixture, "preview", item_id)})
        self.assertEqual("ok", result["status"], result)
        return result

    def confirmation(self, fixture: CheckpointFixture, item_id: str, preview: JsonObject) -> JsonObject:
        return {
            **self.request(fixture, "confirm", item_id),
            "binding": preview["binding"],
            "human_confirmation": f"Yes, cancel {item_id}. Retain its work and history.",
            "actor_task_id": "human-coordinator",
            "actor_host_id": "local",
        }

    def confirm(self, fixture: CheckpointFixture, item_id: str, preview: JsonObject) -> JsonObject:
        return call_advertised_tool(mcp_server.CANCEL_TOOL, {"request": self.confirmation(fixture, item_id, preview)})

    def item_status(self, fixture: CheckpointFixture, item_id: str) -> JsonObject:
        return call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "operation": "item",
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "item_id": item_id,
                }
            },
        )

    def set_state(self, fixture: CheckpointFixture, state: stored_state.StoredWorkItemState) -> None:
        with fixture.store.write() as transaction:
            connection = transaction.connection
            connection.execute("UPDATE work_items SET state = ? WHERE item_id = 'work-a'", (state.value,))
            connection.execute(
                "UPDATE attempts SET state = ?, candidate_revision = NULL, candidate_recorded_at = NULL WHERE attempt_id = 'work-a-1'",
                (state.value,),
            )
            move_item_state_count(connection, stored_state.StoredWorkItemState.REVIEW, state)

    def test_uninitialized_work_root_rejects_without_effects(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        preview = self.preview(fixture, "work-a")
        missing = fixture.project / "uninitialized-board"
        before = fixture.store.validated_snapshot()
        for operation in ("preview", "confirm"):
            with self.subTest(operation=operation):
                request = (
                    self.request(fixture, operation, "work-a")
                    if operation == "preview"
                    else self.confirmation(fixture, "work-a", preview)
                )
                request["work_root"] = str(missing)
                result = call_advertised_tool(mcp_server.CANCEL_TOOL, {"request": request})
                self.assertEqual("rejected", result["status"], result)
                self.assertEqual("unchanged", result["effect"])
                self.assertIs(False, result["state_changed"])
                self.assertIn(str(missing), str(result["message"]))
                self.assertIn("initialized", str(result["recovery"]))
                self.assertFalse(missing.exists())
                self.assertEqual(before, fixture.store.validated_snapshot())

    def test_ordinary_close_cannot_bypass_cancellation_confirmation(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        action = self.project_action(fixture, "close:zz-proposal-a")
        before = fixture.store.validated_snapshot()
        arguments: JsonObject = {
            "project_root": str(fixture.project),
            "work_root": str(fixture.work),
            "receipt": {"action_id": action["action_id"], "subject_revision": action["subject_revision"]},
            "payload": {"outcome": "dropped", "reason": "Obsolete.", "human_decision": "Drop zz-proposal-a."},
            "actor_task_id": "human-coordinator",
            "actor_host_id": "local",
        }
        result = call_advertised_tool(mcp_server.CLOSE_TOOL, arguments)
        self.assertEqual("rejected", result["status"], result)
        self.assertIn("cancellation preview", str(result["message"]))
        self.assertEqual(before, fixture.store.validated_snapshot())
        code, output, error = self.run_cli(
            *fixture.common,
            "close",
            "zz-proposal-a",
            "--outcome",
            "dropped",
            "--reason",
            "Drop zz-proposal-a.",
            "--task-id",
            "human-coordinator",
            "--host-id",
            "local",
        )
        self.assertNotEqual(0, code, (output, error))
        self.assertIn("cancellation preview", output + error)
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_cancellation_covers_live_stages_preserves_evidence_and_is_not_completion(self) -> None:
        for state in (
            stored_state.StoredWorkItemState.ACTIVE,
            stored_state.StoredWorkItemState.PAUSED,
            stored_state.StoredWorkItemState.BLOCKED,
            stored_state.StoredWorkItemState.REVIEW,
        ):
            with self.subTest(state=state):
                fixture = self.checkpoint_fixture(local=True)
                if state != stored_state.StoredWorkItemState.REVIEW:
                    self.set_state(fixture, state)
                before = fixture.store.validated_snapshot()
                preview = self.preview(fixture, "work-a")
                self.assertEqual(before, fixture.store.validated_snapshot())
                result = self.confirm(fixture, "work-a", preview)
                self.assertEqual("committed", result["status"], result)
                fresh = SQLiteWorkStore(fixture.work / "state.sqlite3")
                after = fresh.validated_snapshot()
                item = next(value for value in after.lifecycle.work_items if value.item_id == "work-a")
                attempt = next(value for value in after.lifecycle.attempts if value.attempt_id == "work-a-1")
                self.assertEqual((stored_state.StoredWorkItemState.DROPPED, None), (item.state, item.queue_position))
                self.assertEqual(work_models.AttemptState.DONE, attempt.state)
                self.assertEqual(before.artifact_references, after.artifact_references)
                self.assertEqual(before.lifecycle.definition_revisions, after.lifecycle.definition_revisions)
                self.assertEqual(before.transition_receipts, after.transition_receipts[:-1])
                self.assertEqual("candidate\n", (fixture.project / "tracked.txt").read_text())
                record = msgspec.json.decode(
                    bytes(after.transition_receipts[-1].input_payload), type=cancellation_models.CancellationRecord
                )
                self.assertEqual(
                    self.confirmation(fixture, "work-a", preview)["human_confirmation"], record.human_confirmation
                )
                self.assertIsNone(cancellation.receipt_failure(after.transition_receipts[-1]))
                status = self.item_status(fixture, "work-a")
                self.assertEqual("dropped", status["state"])
                self.assertEqual("cancel", self.json_object(status["closure"])["action"])
                self.assertEqual([], status["attempts"])
                inspected = call_advertised_tool(
                    mcp_server.ATTEMPT_INSPECT_TOOL,
                    {
                        "project_root": str(fixture.project),
                        "work_root": str(fixture.work),
                        "attempt_id": "work-a-1",
                        "reconciliation": None,
                    },
                )
                self.assertEqual("dropped", self.json_object(inspected["continuation"])["state"])
                self.assertIn("cancelled (item dropped)", (fixture.work / "views/attempts/work-a-1.md").read_text())
                again = self.confirm(fixture, "work-a", preview)
                self.assertEqual("rejected", again["status"])
                self.assertEqual(after, fresh.validated_snapshot())

    def test_missing_empty_wrong_item_and_stale_authority_confirmation_reject_unchanged(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        preview = self.preview(fixture, "work-a")
        before = fixture.store.validated_snapshot()
        for kind in ("missing", "empty", "whitespace", "wrong-item"):
            with self.subTest(kind=kind):
                request = self.confirmation(fixture, "work-a", preview)
                if kind == "missing":
                    del request["human_confirmation"]
                elif kind == "empty":
                    request["human_confirmation"] = ""
                elif kind == "whitespace":
                    request["human_confirmation"] = "  "
                else:
                    request["item_id"] = "work-c"
                result = call_advertised_tool(mcp_server.CANCEL_TOOL, {"request": request})
                self.assertEqual("rejected", result["status"], result)
                self.assertEqual(before, fixture.store.validated_snapshot())
        now = datetime.now(UTC)
        expect_success(
            service.acquire_attempt_authority(
                fixture.store,
                attempt_id=AttemptId("work-a-1"),
                task_id=TaskId("new-worker"),
                host_id=HostId("local"),
                lease_id=LeaseId("new-token"),
                acquired_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        changed = fixture.store.validated_snapshot()
        self.assertEqual("rejected", self.confirm(fixture, "work-a", preview)["status"])
        self.assertEqual(changed, fixture.store.validated_snapshot())

    def test_human_owned_pr_review_cancellation_retains_history_through_reload_validation_and_export(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        definition = fixture.store.read_item_definition(WorkItemId("work-c")).definition
        assert definition is not None

        def act(
            operation: str,
            field: str,
            payload: pr_reviews.ReviewBrief
            | pr_reviews.BriefReview
            | pr_reviews.HeadObservation
            | pr_reviews.ReviewRound,
            revision: int,
            actor: str,
        ) -> contracts.PrReviewSuccess:
            result = call_advertised_tool(
                mcp_server.PR_REVIEW_TOOL,
                {
                    "request": {
                        **self.request(fixture, operation, "work-c"),
                        "expected_subject_revision": revision,
                        "actor_task_id": actor,
                        "actor_host_id": "local",
                        field: msgspec.to_builtins(payload),
                    }
                },
            )
            self.assertEqual("committed", result["status"], result)
            return msgspec.convert(result, type=contracts.PrReviewSuccess, strict=True)

        started = act(
            "start",
            "brief",
            pr_reviews.ReviewBrief(
                "pinboard-pr-review-brief/v1",
                "work-c",
                definition.revision,
                definition.digest,
                "https://github.com/example/repo/pull/42",
                "colleague",
                (pr_reviews.Requirement("issue:42", "Expected behavior.", "CLI", "application"),),
                ("Repository tests",),
                "preparer",
            ),
            7,
            "preparer",
        )
        brief_id = started.brief_history_id
        assert brief_id is not None
        approved = act(
            "review-brief",
            "brief_review",
            pr_reviews.BriefReview(
                "pinboard-pr-review-brief-review/v1",
                "work-c",
                brief_id,
                "brief-reviewer",
                "ready",
                "Checked requirements and owners.",
            ),
            started.subject_revision,
            "brief-reviewer",
        )
        head = "a" * 40
        observed = act(
            "observe",
            "observation",
            pr_reviews.HeadObservation("pinboard-pr-head-observation/v1", "work-c", head, "harness fetch"),
            approved.subject_revision,
            "reviewer",
        )
        reviewed = act(
            "round",
            "round",
            pr_reviews.ReviewRound(
                "pinboard-pr-review-round/v1",
                "work-c",
                brief_id,
                None,
                head,
                "harness fetch",
                (),
                ("Hosted checks unverified.",),
                (),
                "reviewer",
            ),
            observed.subject_revision,
            "reviewer",
        )
        before = fixture.store.validated_snapshot()
        preview = self.preview(fixture, "work-c")
        self.assertEqual("committed", self.confirm(fixture, "work-c", preview)["status"])
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        self.assertEqual(before.transition_receipts, fresh.transition_receipts[:-1])
        status = call_advertised_tool(mcp_server.PR_REVIEW_TOOL, {"request": self.request(fixture, "status", "work-c")})
        self.assertEqual("dropped", status["item_state"])
        self.assertEqual([], status["available_actions"])
        self.assertIsNone(status["close"])
        retained = msgspec.convert(status, type=contracts.PrReviewSuccess, strict=True)
        self.assertEqual(
            (reviewed.brief, reviewed.brief_review, reviewed.rounds, reviewed.latest_observation),
            (retained.brief, retained.brief_review, retained.rounds, retained.latest_observation),
        )
        self.assertIs(True, self.run_json_cli(*fixture.common, "validate")["valid"])
        self.assertEqual("pinboard-project-export/v3", self.run_json_cli(*fixture.common, "export")["schema"])

    def test_preparation_is_fenced_and_dropped_prerequisite_remains_unsatisfied_everywhere(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        now = datetime.now(UTC)
        self.set_state(fixture, stored_state.StoredWorkItemState.ACTIVE)
        preparation = expect_success(
            service.start_preparation(
                fixture.store,
                work_item_id=WorkItemId("work-c"),
                task_id=TaskId("preparer"),
                host_id=HostId("local"),
                lease_id=LeaseId("prepare-token"),
                acquired_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        preview = self.preview(fixture, "work-c")
        self.assertEqual("committed", self.confirm(fixture, "work-c", preview)["status"])
        self.assertIn("Depends on work-c", (fixture.work / "views/board.md").read_text())
        html = (fixture.work / "views/board.html").read_text()
        data = html.split('<script id="board-data" type="application/json">', 1)[1].split("</script>", 1)[0]
        page = self.json_object(msgspec.json.decode(data))
        dependent = next(
            self.json_object(value)
            for value in self.json_array(page["items"])
            if self.json_object(value)["item_id"] == "work-a"
        )
        prerequisite = next(
            self.json_object(value)
            for value in self.json_array(dependent["dependencies"])
            if self.json_object(value)["item_id"] == "work-c"
        )
        self.assertIs(False, prerequisite["on_board"])
        self.assertIs(True, prerequisite["unsatisfied"])
        live_ids = {self.json_object(value)["item_id"] for value in self.json_array(page["items"])}
        self.assertNotIn("work-c", live_ids)
        for value in self.json_array(page["items"]):
            for dependency in self.json_array(self.json_object(value)["dependencies"]):
                target = self.json_object(dependency)
                if bool(target["on_board"]):
                    self.assertIn(target["item_id"], live_ids)
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3")
        authority = fresh.read_preparation_authority_status(WorkItemId("work-c"))
        assert authority is not None
        self.assertEqual(
            (authority_models.PreparationLeaseStatus.REVOKED, preparation.authority.generation + 1),
            (authority.status, authority.generation),
        )
        result = service.start_preparation(
            fresh,
            work_item_id=WorkItemId("work-c"),
            task_id=TaskId("new-preparer"),
            host_id=HostId("local"),
            lease_id=LeaseId("another-token"),
            acquired_at=now,
            expires_at=now + timedelta(hours=1),
        )
        self.assertIsInstance(result, DecisionFailure)
        overview = call_advertised_tool(
            mcp_server.OVERVIEW_TOOL, {"project_root": str(fixture.project), "work_root": str(fixture.work)}
        )
        item = next(
            self.json_object(value)
            for value in self.json_array(overview["items"])
            if self.json_object(value)["item_id"] == "work-a"
        )
        self.assertIs(False, item["eligible"])
        self.assertEqual(["work-c"], item["unsatisfied_dependencies"])
        parallel = call_advertised_tool(
            mcp_server.PARALLEL_PREVIEW_TOOL,
            {
                "request": {
                    "selection": "selected",
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "item_ids": ["work-a"],
                }
            },
        )
        self.assertEqual("rejected", self.item_status(fixture, "absent")["status"])
        self.assertEqual("ok", parallel["status"], parallel)
        self.assertIn("dependency-unsatisfied", str(parallel))

    def test_concurrent_confirmations_commit_once_and_failure_rolls_back(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        current = expect_success(cancellation.preview(fixture.store, WorkItemId("work-a")))
        expected = cancellation.binding(current, fixture.work)
        before = fixture.store.validated_snapshot()
        with (
            patch(
                "pinboard.adapters.sqlite.persistence.sqlite_state.append_history",
                side_effect=RuntimeError("injected receipt failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "injected receipt failure"),
        ):
            cancellation.confirm(
                fixture.store,
                WorkItemId("work-a"),
                expected,
                "Cancel work-a.",
                TaskId("human"),
                HostId("local"),
                SQLITE_NOW,
                fixture.work,
            )
        self.assertEqual(before, fixture.store.validated_snapshot())
        barrier = Barrier(2)

        def commit(_index: int) -> bool:
            barrier.wait(timeout=5)
            store = SQLiteWorkStore(fixture.work / "state.sqlite3")
            result = cancellation.confirm(
                store,
                WorkItemId("work-a"),
                expected,
                "Cancel work-a.",
                TaskId("human"),
                HostId("local"),
                SQLITE_NOW,
                fixture.work,
            )
            return not isinstance(result, DecisionFailure)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(commit, range(2)))
        self.assertEqual([False, True], sorted(outcomes))
        self.assertEqual(
            len(before.transition_receipts) + 1, len(fixture.store.validated_snapshot().transition_receipts)
        )

    def test_binding_cannot_be_reused_on_an_identical_other_board(self) -> None:
        first = self.checkpoint_fixture(local=True)
        first_preview = self.preview(first, "work-a")
        copied = Path(tempfile.mkdtemp()).resolve() / "identical-project"
        self.addCleanup(shutil.rmtree, copied.parent)
        shutil.copytree(first.project, copied)
        second = replace(
            first, project=copied, work=copied / ".pinboard", store=SQLiteWorkStore(copied / ".pinboard/state.sqlite3")
        )
        second_preview = self.preview(second, "work-a")
        self.assertEqual(first_preview["preview"], second_preview["preview"])
        self.assertNotEqual(first_preview["binding"], second_preview["binding"])
        before = second.store.validated_snapshot()
        refusal = self.confirm(second, "work-a", first_preview)
        self.assertEqual("rejected", refusal["status"], refusal)
        self.assertEqual(before, second.store.validated_snapshot())

    def test_ready_deferred_and_unstarted_blocked_items_can_be_cancelled(self) -> None:
        for state in (
            stored_state.StoredWorkItemState.READY,
            stored_state.StoredWorkItemState.DEFERRED,
            stored_state.StoredWorkItemState.BLOCKED,
        ):
            with self.subTest(state=state):
                fixture = self.checkpoint_fixture(local=True)
                if state != stored_state.StoredWorkItemState.READY:
                    with fixture.store.write() as transaction:
                        transaction.connection.execute(
                            "UPDATE work_items SET state = ? WHERE item_id = 'work-c'", (state.value,)
                        )
                        move_item_state_count(transaction.connection, stored_state.StoredWorkItemState.READY, state)
                preview = self.preview(fixture, "work-c")
                result = self.confirm(fixture, "work-c", preview)
                self.assertEqual("committed", result["status"], result)
                self.assertEqual("dropped", self.item_status(fixture, "work-c")["state"])

    def test_cancellation_fences_worker_and_preparer_actions_and_reacquisition(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        self.set_state(fixture, stored_state.StoredWorkItemState.ACTIVE)
        acquired = self.native_attempt_acquire(fixture, "current-worker")
        action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=acquired)
        preview = self.preview(fixture, "work-a")
        self.assertEqual("committed", self.confirm(fixture, "work-a", preview)["status"])
        before = fixture.store.validated_snapshot()
        stale = self.transition_result(fixture, action, {"candidate": fixture.candidate_revision})
        self.assertEqual("rejected", stale["status"], stale)
        for operation in ("renew", "release"):
            request: JsonObject = {
                "operation": operation,
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "attempt_id": "work-a-1",
                "lease_id": acquired["lease_id"],
                "generation": acquired["generation"],
            }
            if operation == "renew":
                request["ttl_seconds"] = 3600
            rejected = call_advertised_tool(mcp_server.ATTEMPT_AUTHORITY_TOOL, {"request": request})
            self.assertEqual("rejected", rejected["status"], rejected)
        now = datetime.now(UTC)
        reacquired = service.acquire_attempt_authority(
            fixture.store,
            attempt_id=AttemptId("work-a-1"),
            task_id=TaskId("another-worker"),
            host_id=HostId("local"),
            lease_id=LeaseId("new-lease"),
            acquired_at=now,
            expires_at=now + timedelta(hours=1),
        )
        self.assertIsInstance(reacquired, DecisionFailure)
        self.assertEqual(before, fixture.store.validated_snapshot())
        branch = call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "operation": "branch",
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "branch": fixture.brief.branch,
                }
            },
        )
        self.assertEqual("cancelled", self.json_object(self.json_array(branch["owners"])[0])["attempt_state"])

    def test_unrelated_revision_does_not_invalidate_confirmation_and_current_cli_reads_retained_history(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        preview = self.preview(fixture, "work-a")
        order = fixture.store.read_current_action_snapshot(SQLITE_NOW)
        ordered = tuple(
            item_id
            for _position, item_id in sorted((item.queue_position or 0, item.work_item_id) for item in order.items)
        )
        expect_success(
            service.reorder(
                fixture.store, ordered, tuple(reversed(ordered)), TaskId("human"), HostId("local"), SQLITE_NOW
            )
        )
        result = self.confirm(fixture, "work-a", preview)
        self.assertEqual("committed", result["status"], result)
        rebuilt, output, error = self.run_cli(*fixture.common, "views", "rebuild")
        self.assertEqual(0, rebuilt, (output, error))
        validated = self.run_json_cli(*fixture.common, "validate")
        self.assertIs(True, validated["valid"])
        exported = self.run_json_cli(*fixture.common, "export")
        self.assertEqual("pinboard-project-export/v3", exported["schema"])

    def test_historical_v1_cancellation_keeps_exact_preview_binding_and_exported_bytes(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        preview = self.preview(fixture, "work-a")
        self.assertEqual("committed", self.confirm(fixture, "work-a", preview)["status"])
        receipt = fixture.store.validated_snapshot().transition_receipts[-1]
        record = msgspec.json.decode(bytes(receipt.input_payload), type=cancellation_models.CancellationRecord)
        historical_preview = replace_struct(
            record.preview,
            dependency_effect="Dropped prerequisites remain unsatisfied; dependent commitments are retained.",
        )
        historical_binding = cancellation.binding(historical_preview, fixture.work)
        historical_record = replace_struct(record, preview=historical_preview, binding=historical_binding)
        historical_bytes = msgspec.json.encode(historical_record, order="sorted")
        with fixture.store.write() as transaction:
            transaction.connection.execute(
                "UPDATE transition_history SET input_json = ? WHERE history_id = ?",
                (historical_bytes.decode(), receipt.history_id),
            )
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        retained = fresh.transition_receipts[-1]
        self.assertEqual(historical_bytes, bytes(retained.input_payload))
        decoded = msgspec.json.decode(bytes(retained.input_payload), type=cancellation_models.CancellationRecord)
        self.assertEqual(historical_binding, decoded.binding)
        self.assertIsNone(cancellation.receipt_failure(retained))
        tampered = msgspec.to_builtins(historical_record)
        tampered["binding"]["preview_sha256"] = "0" * 64
        with self.assertRaisesRegex(msgspec.ValidationError, "binding differs"):
            msgspec.json.decode(msgspec.json.encode(tampered), type=cancellation_models.CancellationRecord)
        rebuilt, output, error = self.run_cli(*fixture.common, "views", "rebuild")
        self.assertEqual(0, rebuilt, (output, error))
        self.assertIs(True, self.run_json_cli(*fixture.common, "validate")["valid"])
        exported, output, error = self.run_cli(*fixture.common, "export", "--json")
        self.assertEqual(0, exported, (output, error))
        package = msgspec.json.decode(output, type=project_export.ProjectExport)
        transition = next(value for value in package.transitions if value.history_id == receipt.history_id)
        exported_record = msgspec.json.decode(transition.input, type=cancellation_models.CancellationRecord)
        self.assertEqual(historical_bytes, msgspec.json.encode(exported_record, order="sorted"))

    def test_changed_item_or_dependency_preview_rejects_and_dropped_dependency_cannot_resume_or_prepare(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        preview = self.preview(fixture, "work-c")
        self.set_state(fixture, stored_state.StoredWorkItemState.ACTIVE)
        changed = fixture.store.validated_snapshot()
        self.assertEqual("rejected", self.confirm(fixture, "work-c", preview)["status"])
        self.assertEqual(changed, fixture.store.validated_snapshot())
        preview = self.preview(fixture, "work-c")
        self.assertEqual("committed", self.confirm(fixture, "work-c", preview)["status"])
        paused = self.transition_result(
            fixture, self.project_action(fixture, "pause:work-a-1"), {"reason": "Wait on the prerequisite decision."}
        )
        self.assertEqual("committed", paused["status"], paused)
        resume = self.actions_result(fixture, {"role": "project", "action_id": {"kind": "resume", "subject": "work-a"}})
        self.assertEqual("rejected", resume["status"], resume)
        now = datetime.now(UTC)
        result = service.start_preparation(
            fixture.store,
            work_item_id=WorkItemId("zz-proposal-a"),
            task_id=TaskId("preparer"),
            host_id=HostId("local"),
            lease_id=LeaseId("new-token"),
            acquired_at=now,
            expires_at=now + timedelta(hours=1),
        )
        self.assertIsInstance(result, DecisionFailure)
