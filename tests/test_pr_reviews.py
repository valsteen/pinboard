"""One supported human-owned PR review through the native boundary and fresh SQLite readers."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import msgspec

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import pr_reviews, queries, query_models
from pinboard.domain.identifiers import WorkItemId
from pinboard.mcp import contracts
from tests.decision_support import BOARD
from tests.native_support import call_native_tool
from tests.support import SQLITE_NOW, JsonObject, NoReadyCandidateReviews, complete_sqlite_state, initialize_store


class HumanOwnedPrReviewTest(unittest.TestCase):
    def test_close_rejects_equal_actual_heads_unpaired_rounds_and_unowned_findings(self) -> None:
        close: JsonObject = {
            "schema": "pinboard-pr-review-close/v1",
            "item_id": "work-c",
            "final_round_history_id": None,
            "last_reviewed_head": None,
            "newer_observed_head": None,
            "newer_observation_source": None,
            "final_dispositions": [],
            "human_direction": "Stop now.",
            "human_task_id": "human",
            "outcome": "stopped",
        }
        invalid: tuple[JsonObject, ...] = (
            {"final_round_history_id": 1},
            {"last_reviewed_head": "a" * 40},
            {"newer_observed_head": "a" * 40},
            {"newer_observation_source": "harness fetch"},
            {
                "final_round_history_id": 1,
                "last_reviewed_head": "a" * 40,
                "newer_observed_head": "a" * 40,
                "newer_observation_source": "harness fetch",
            },
            {"final_dispositions": [{"finding_id": "unowned", "disposition": "resolved", "evidence": "done"}]},
        )
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(msgspec.ValidationError):
                msgspec.convert(close | fields, type=pr_reviews.ReviewClose, strict=True)

    def test_human_can_close_before_or_after_head_observation_without_a_round(self) -> None:
        for observed_head in (False, True):
            with self.subTest(observed_head=observed_head):
                self._assert_human_close_without_a_round(observed_head)

    def _assert_human_close_without_a_round(self, observed_head: bool) -> None:
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            work_root = Path(temporary) / ".pinboard"
            roots = resolve_durable_roots(project, work_root)
            initialize_database(roots, SQLITE_NOW)
            store = SQLiteWorkStore(roots.database_path)
            initialize_store(store, complete_sqlite_state())
            item_id = WorkItemId("work-c")
            definition = store.read_item_definition(item_id).definition
            assert definition is not None
            common = {"project_root": str(project), "work_root": str(work_root), "item_id": str(item_id)}

            def act(operation: str, field: str, payload: object, revision: int) -> contracts.PrReviewSuccess:
                response = call_native_tool(
                    "pinboard_pr_review",
                    {
                        "request": {
                            **common,
                            "operation": operation,
                            "expected_subject_revision": revision,
                            "actor_task_id": "human",
                            "actor_host_id": "test-host",
                            field: msgspec.to_builtins(payload),
                        }
                    },
                )
                self.assertEqual("committed", response["status"])
                return msgspec.convert(response, type=contracts.PrReviewSuccess, strict=True)

            started = act(
                "start",
                "brief",
                pr_reviews.ReviewBrief(
                    "pinboard-pr-review-brief/v1",
                    str(item_id),
                    definition.revision,
                    definition.digest,
                    "https://github.com/example/repo/pull/42",
                    "colleague",
                    (pr_reviews.Requirement("issue:42", "Expected behavior.", "CLI", "application"),),
                    ("Repository tests",),
                    "human",
                ),
                7,
            )
            self.assertIn("close", started.available_actions)
            head = "a" * 40 if observed_head else None
            revision = started.subject_revision
            if head is not None:
                observed = act(
                    "observe",
                    "observation",
                    pr_reviews.HeadObservation("pinboard-pr-head-observation/v1", str(item_id), head, "harness fetch"),
                    revision,
                )
                revision = observed.subject_revision
            closed = act(
                "close",
                "close",
                pr_reviews.ReviewClose(
                    "pinboard-pr-review-close/v1",
                    str(item_id),
                    None,
                    None,
                    head,
                    "harness fetch" if head is not None else None,
                    (),
                    "Stop before a PR round is completed.",
                    "human",
                    "stopped",
                ),
                revision,
            )
            self.assertEqual((), closed.rounds)
            self.assertEqual("no-pr-round-completed", closed.round_status)
            self.assertEqual(head, closed.unreviewed_head)
            fresh = SQLiteWorkStore(roots.database_path).validated_snapshot()
            self.assertEqual(
                "dropped", next(item.state.value for item in fresh.lifecycle.work_items if item.item_id == item_id)
            )
            view = (work_root / "views" / "items" / f"{item_id}.md").read_text()
            self.assertIn("no PR round completed", view)

    def test_two_heads_and_human_stop_survive_fresh_store(self) -> None:  # noqa: PLR0915
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            work_root = Path(temporary) / ".pinboard"
            roots = resolve_durable_roots(project, work_root)
            initialize_database(roots, SQLITE_NOW)
            store = SQLiteWorkStore(roots.database_path)
            initialize_store(store, complete_sqlite_state())
            item_id = WorkItemId("work-c")
            definition = store.read_item_definition(item_id).definition
            assert definition is not None
            common = {"project_root": str(project), "work_root": str(work_root), "item_id": str(item_id)}

            def action(
                operation: str, payload_name: str, payload: object, revision: int, actor: str = "project-owner"
            ) -> JsonObject:
                return call_native_tool(
                    "pinboard_pr_review",
                    {
                        "request": {
                            **common,
                            "operation": operation,
                            "expected_subject_revision": revision,
                            "actor_task_id": actor,
                            "actor_host_id": "test-host",
                            payload_name: msgspec.to_builtins(payload),
                        }
                    },
                )

            def accepted(
                operation: str, payload_name: str, payload: object, revision: int, actor: str = "project-owner"
            ) -> contracts.PrReviewSuccess:
                result = action(operation, payload_name, payload, revision, actor)
                self.assertEqual("committed", result["status"])
                return msgspec.convert(result, type=contracts.PrReviewSuccess, strict=True)

            brief = pr_reviews.ReviewBrief(
                "pinboard-pr-review-brief/v1",
                str(item_id),
                definition.revision,
                definition.digest,
                "https://github.com/example/repo/pull/42",
                "colleague",
                (pr_reviews.Requirement("issue:42", "The change works for consumers.", "CLI", "application"),),
                ("Repository tests and architecture rules",),
                "preparer",
            )
            self.assertEqual("rejected", action("start", "brief", brief, 7)["status"])
            started = accepted("start", "brief", brief, 7, "preparer")
            self.assertEqual((), started.rounds)
            self.assertEqual("unverified", started.remote_freshness)
            brief_history_id = started.brief_history_id
            assert brief_history_id is not None
            item_status = queries.project_item_status(
                SQLiteWorkStore(roots.database_path), NoReadyCandidateReviews(), item_id, SQLITE_NOW, BOARD
            )
            assert isinstance(item_status, query_models.ItemStatus)
            self.assertEqual((), item_status.attempts)
            self.assertEqual(query_models.NoReviewVerdict(), item_status.review_verdict)

            premature = action(
                "round",
                "round",
                pr_reviews.ReviewRound(
                    "pinboard-pr-review-round/v1",
                    str(item_id),
                    brief_history_id,
                    None,
                    "a" * 40,
                    "harness git fetch",
                    (),
                    (),
                    (),
                    "review-agent",
                ),
                started.subject_revision,
            )
            self.assertEqual("rejected", premature["status"])
            self_review = action(
                "review-brief",
                "brief_review",
                pr_reviews.BriefReview(
                    "pinboard-pr-review-brief-review/v1",
                    str(item_id),
                    brief_history_id,
                    "preparer",
                    "ready",
                    "Self review is not independent.",
                ),
                started.subject_revision,
                "preparer",
            )
            self.assertEqual("rejected", self_review["status"])
            self.assertEqual(
                "rejected",
                action(
                    "review-brief",
                    "brief_review",
                    pr_reviews.BriefReview(
                        "pinboard-pr-review-brief-review/v1",
                        str(item_id),
                        brief_history_id,
                        "brief-reviewer",
                        "ready",
                        "Spoofed independent reviewer.",
                    ),
                    started.subject_revision,
                    "preparer",
                )["status"],
            )
            approved = accepted(
                "review-brief",
                "brief_review",
                pr_reviews.BriefReview(
                    "pinboard-pr-review-brief-review/v1",
                    str(item_id),
                    brief_history_id,
                    "brief-reviewer",
                    "ready",
                    "Checked requirements and owners.",
                ),
                started.subject_revision,
                "brief-reviewer",
            )
            head_one = "a" * 40
            head_two = "b" * 40
            observed = accepted(
                "observe",
                "observation",
                pr_reviews.HeadObservation(
                    "pinboard-pr-head-observation/v1", str(item_id), head_one, "harness git fetch"
                ),
                approved.subject_revision,
            )
            first_round = pr_reviews.ReviewRound(
                "pinboard-pr-review-round/v1",
                str(item_id),
                brief_history_id,
                None,
                head_one,
                "harness git fetch",
                (pr_reviews.Finding("f1", "concern", "The edge case is unclear.", "src/example.py:12"),),
                ("Hosted checks were not inspected.",),
                (),
                "review-agent",
            )
            self.assertEqual("rejected", action("round", "round", first_round, observed.subject_revision)["status"])
            first = accepted(
                "round",
                "round",
                first_round,
                observed.subject_revision,
                "review-agent",
            )
            observed_two = accepted(
                "observe",
                "observation",
                pr_reviews.HeadObservation(
                    "pinboard-pr-head-observation/v1", str(item_id), head_two, "harness git fetch"
                ),
                first.subject_revision,
            )
            second = accepted(
                "round",
                "round",
                pr_reviews.ReviewRound(
                    "pinboard-pr-review-round/v1",
                    str(item_id),
                    brief_history_id,
                    first.rounds[-1].history_id,
                    head_two,
                    "harness git fetch",
                    (pr_reviews.Finding("f1", "concern", "The edge case remains unclear.", "src/example.py:14"),),
                    ("Hosted checks were not inspected.",),
                    (pr_reviews.PriorFindingDisposition("f1", "carried-forward", "Same concern at the new head."),),
                    "review-agent",
                ),
                observed_two.subject_revision,
                "review-agent",
            )
            head_three = "c" * 40
            newer = accepted(
                "observe",
                "observation",
                pr_reviews.HeadObservation(
                    "pinboard-pr-head-observation/v1", str(item_id), head_three, "harness git fetch"
                ),
                second.subject_revision,
            )
            stale_close = action(
                "close",
                "close",
                pr_reviews.ReviewClose(
                    "pinboard-pr-review-close/v1",
                    str(item_id),
                    second.rounds[-1].history_id,
                    head_two,
                    None,
                    None,
                    (pr_reviews.FinalFindingDisposition("f1", "accepted-residual", "Human accepted the concern."),),
                    "Stop at the reviewed head despite the newer observation.",
                    "human",
                    "stopped",
                ),
                newer.subject_revision,
                "human",
            )
            self.assertEqual("rejected", stale_close["status"])
            current = call_native_tool("pinboard_pr_review", {"request": {**common, "operation": "status"}})
            self.assertEqual(head_three, msgspec.convert(current, type=contracts.PrReviewSuccess).unreviewed_head)
            closed = accepted(
                "close",
                "close",
                pr_reviews.ReviewClose(
                    "pinboard-pr-review-close/v1",
                    str(item_id),
                    second.rounds[-1].history_id,
                    head_two,
                    head_three,
                    "harness git fetch",
                    (pr_reviews.FinalFindingDisposition("f1", "accepted-residual", "Human accepted the concern."),),
                    "Stop at the reviewed head despite the newer observation.",
                    "human",
                    "stopped",
                ),
                newer.subject_revision,
                "human",
            )
            self.assertIsNotNone(closed.close)
            assert closed.close is not None
            self.assertEqual(head_two, closed.close.last_reviewed_head)
            self.assertEqual(head_three, closed.close.newer_observed_head)
            after = call_native_tool("pinboard_pr_review", {"request": {**common, "operation": "status"}})
            after_state = msgspec.convert(after, type=contracts.PrReviewSuccess)
            self.assertEqual(2, len(after_state.rounds))
            self.assertEqual("dropped", after_state.item_state)
            self.assertEqual((), after_state.available_actions)
            self.assertEqual(
                "rejected",
                action(
                    "round",
                    "round",
                    pr_reviews.ReviewRound(
                        "pinboard-pr-review-round/v1",
                        str(item_id),
                        brief_history_id,
                        second.rounds[-1].history_id,
                        head_three,
                        "harness git fetch",
                        (),
                        (),
                        (),
                        "review-agent",
                    ),
                    closed.subject_revision,
                )["status"],
            )
            fresh = SQLiteWorkStore(roots.database_path).validated_snapshot()
            self.assertEqual(
                "dropped", next(value.state.value for value in fresh.lifecycle.work_items if value.item_id == item_id)
            )
            exported = SQLiteWorkStore(roots.database_path).read_project_export_batches()[0]
            round_inputs = tuple(
                bytes(value.input_payload).decode("utf-8")
                for value in exported.transition_receipts
                if value.action_kind.value == "record-pr-round"
            )
            self.assertEqual(2, len(round_inputs))
            self.assertIn(head_one, round_inputs[0])
            self.assertIn(head_two, round_inputs[1])
            item_view = (work_root / "views" / "items" / f"{item_id}.md").read_text()
            self.assertIn(head_one, item_view)
            self.assertIn(head_two, item_view)
            self.assertIn(head_three, item_view)

            connection = sqlite3.connect(roots.database_path)
            try:
                second_round_id = second.rounds[-1].history_id
                close_row = connection.execute(
                    "SELECT history_id FROM transition_history WHERE subject_id = ? AND action_kind = 'close-pr-review'",
                    (item_id,),
                ).fetchone()
                assert close_row is not None
                close_id = close_row[0]
                for history_id, mutation in (
                    (second_round_id, "$.prior_dispositions[0].disposition"),
                    (close_id, "$.outcome"),
                ):
                    original = connection.execute(
                        "SELECT input_json FROM transition_history WHERE history_id = ?", (history_id,)
                    ).fetchone()
                    assert original is not None
                    connection.execute(
                        "UPDATE transition_history SET input_json = json_set(input_json, ?, ?) WHERE history_id = ?",
                        (mutation, "resolved" if history_id == second_round_id else "accepted", history_id),
                    )
                    connection.commit()
                    with self.assertRaises(StorageError) as raised:
                        SQLiteWorkStore(roots.database_path).validated_snapshot()
                    self.assertEqual(StorageErrorCode.INVALID_STATE, raised.exception.code)
                    connection.execute(
                        "UPDATE transition_history SET input_json = ? WHERE history_id = ?", (original[0], history_id)
                    )
                    connection.commit()
            finally:
                connection.close()
