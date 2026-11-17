"""Attempt amendments persist, change the review contract and keep attempt conditions through the MCP boundary."""

import contextlib
import dataclasses
import re
import sqlite3
from unittest.mock import patch

import msgspec

from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.migration import schema_procedure
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.domain import work_models
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture
from tests.native_support import native_tools
from tests.sqlite_support import downgrade_to_v13
from tests.support import JsonObject
from tests.test_item_status_facts import ItemLifecycleSupport

STALE_DOCUMENTATION: JsonObject = {
    "schema": "pinboard-attempt-amendment/v1",
    "amendment_class": "stale-documentation",
    "change": "Correct the stale install command in INSTALL.md.",
    "reason": "The documented command no longer exists.",
    "human_decision": "Yes, fix that stale install line while you are there.",
}


class AttemptAmendmentJourneyTest(ItemLifecycleSupport):
    def amend(self, fixture: CheckpointFixture, payload: JsonObject = STALE_DOCUMENTATION) -> JsonObject:
        action = self.native_actions(fixture, "record-amendment", fixture.brief.attempt_id)
        result = fixture.call_advertised_tool(
            mcp_server.TRANSITION_TOOL, self.native_transition_request(fixture, action, payload)
        )
        self.assertIn(result["status"], ("committed", "committed-with-warning"), result)
        return result

    def resubmit(self, fixture: CheckpointFixture, label: str) -> str:
        return self.submit_review(fixture, label, "coordinator-task")

    def prompt_text(self, fixture: CheckpointFixture, job: JsonObject) -> str:
        reference = self.json_object(job["prompt_reference"])
        return (fixture.work / str(reference["selector"])).read_text(encoding="utf-8")

    def native[T: CheckpointFixture](self, fixture: T) -> T:
        tools = self.enterContext(native_tools())
        return dataclasses.replace(
            fixture, call_native_tool=tools.call_native_tool, call_advertised_tool=tools.call_advertised_tool
        )

    def reviewed_fixture(self) -> tuple[CheckpointFixture, str]:
        """Return a native review-state fixture whose submitted candidate has a commissioned ready review."""

        fixture = self.native(self.checkpoint_fixture())
        self.return_for_review(fixture, "Rework the candidate.")
        reviewed = self.resubmit(fixture, "reviewed")
        self.record_commissioned_review(fixture, reviewed, "reviewer-one")
        self.assertEqual("ready", self.verdict(fixture)["kind"])
        return fixture, reviewed

    def condition(self, fixture: CheckpointFixture) -> tuple[str, str | None]:
        """Read work-a's recorded board condition from the generated board page data."""

        html = (fixture.work / "views" / "board.html").read_text(encoding="utf-8")
        match = re.search(r'<script id="board-data" type="application/json">(.*?)</script>', html, re.DOTALL)
        assert match is not None
        page = msgspec.json.decode(match.group(1))
        (item,) = (value for value in page["items"] if value["item_id"] == "work-a")
        condition = item["standing"]["condition"]
        return condition["label"], condition["text"]

    def test_review_amendment_returns_to_active_and_an_amendment_only_change_gets_a_delta_review(self) -> None:
        fixture, reviewed = self.reviewed_fixture()

        self.amend(fixture)

        self.assertEqual("none", self.verdict(fixture)["kind"])
        inspected = self.inspection(fixture)
        self.assertEqual("active", self.json_object(inspected["continuation"])["state"])
        (amendment,) = self.json_array(inspected["amendments"])
        self.assertEqual(
            ("stale-documentation", STALE_DOCUMENTATION["human_decision"], reviewed),
            tuple(
                self.json_object(amendment)[key] for key in ("amendment_class", "human_decision", "cleared_candidate")
            ),
        )
        label, text = self.condition(fixture)
        self.assertIn("Candidate being amended", label)
        self.assertEqual(STALE_DOCUMENTATION["change"], text)

        resubmitted = self.resubmit(fixture, "amended")
        self.assertNotIn("Candidate being amended", self.condition(fixture)[0])
        job = self.review_result(fixture, {"kind": "amendment-delta", "candidate_revision": resubmitted})
        self.assertEqual("ready", job["status"], job)
        review_round = self.json_object(job["review_round"])
        self.assertEqual(("amendment-delta", reviewed), (review_round["kind"], review_round["reviewed_candidate"]))
        self.assertEqual([self.json_object(amendment)["history_id"]], review_round["amendment_history_ids"])
        prompt = self.prompt_text(fixture, job)
        self.assertIn(str(STALE_DOCUMENTATION["change"]), prompt)
        self.assertIn(str(STALE_DOCUMENTATION["human_decision"]), prompt)
        prompt_digest = self.json_object(job["prompt_reference"])["sha256"]
        assert isinstance(prompt_digest, str)
        recorded = fixture.call_advertised_tool(
            mcp_server.REVIEW_JOB_TOOL,
            {
                **self.roots(fixture),
                "review": self.ready_review_request(fixture, resubmitted, "reviewer-two", prompt_digest),
            },
        )
        self.assertEqual("recorded", recorded["status"], recorded)
        self.assertEqual("ready", self.verdict(fixture)["kind"])
        self.assert_valid(fixture)

    def test_delta_review_is_refused_after_an_intervening_return(self) -> None:
        fixture, _reviewed = self.reviewed_fixture()
        self.amend(fixture)
        self.resubmit(fixture, "amended")
        self.return_for_review(fixture, "Rework the amended candidate.")
        resubmitted = self.resubmit(fixture, "reworked")
        before = (fixture.work / "state.sqlite3").read_bytes()
        refused = self.review_result(fixture, {"kind": "amendment-delta", "candidate_revision": resubmitted})
        self.assertEqual(("rejected", "unchanged"), (refused["status"], refused["effect"]), refused)
        self.assertIn("initial", str(refused))
        self.assertEqual(before, (fixture.work / "state.sqlite3").read_bytes())

    def test_active_amendment_keeps_the_return_condition_and_full_review_carries_it(self) -> None:
        fixture = self.native(self.checkpoint_fixture())
        reason = "Tighten the route validation."
        self.return_for_review(fixture, reason)
        self.assertEqual(("Returned for correction", reason), self.condition(fixture))

        self.amend(fixture)

        self.assertEqual(("Returned for correction", reason), self.condition(fixture))
        self.assertEqual("returned-for-correction", self.verdict(fixture)["kind"])
        attempt_view = (fixture.work / "views" / "attempts" / "work-a-1.md").read_text(encoding="utf-8")
        self.assertIn("## Amendments", attempt_view)
        self.assertIn(str(STALE_DOCUMENTATION["change"]), attempt_view)
        resubmitted = self.resubmit(fixture, "corrected")
        refused = self.review_result(fixture, {"kind": "amendment-delta", "candidate_revision": resubmitted})
        self.assertEqual("rejected", refused["status"], refused)
        self.assertEqual("unchanged", refused["effect"])
        job = self.review_result(fixture, {"kind": "initial", "candidate_revision": resubmitted})
        self.assertEqual("ready", job["status"], job)
        prompt = self.prompt_text(fixture, job)
        self.assertIn(str(STALE_DOCUMENTATION["reason"]), prompt)

    def test_active_amendment_keeps_accepted_and_continued_evidence(self) -> None:
        fixture, reviewed = self.reviewed_fixture()
        evidence = "The routes checkpoint is reviewed; continue with the map."
        self.transition(fixture, "accept-review-and-continue:work-a-1", {"candidate": reviewed, "evidence": evidence})
        expected = ("Review accepted, work continues", evidence)
        self.assertEqual(expected, self.condition(fixture))
        self.amend(fixture)
        self.assertEqual(expected, self.condition(fixture))
        self.assertEqual("accepted-and-continued", self.verdict(fixture)["kind"])

    def test_amendment_after_a_ready_review_resubmitted_unchanged_has_no_ready_verdict(self) -> None:
        fixture, reviewed = self.reviewed_fixture()
        self.amend(fixture)
        resubmitted = self.resubmit(fixture, "reviewed")
        self.assertEqual(reviewed, resubmitted)
        self.assertEqual("none", self.verdict(fixture)["kind"])
        job = self.review_result(fixture, {"kind": "initial", "candidate_revision": resubmitted})
        self.assertEqual("ready", job["status"], job)

    def test_rebind_ends_amendments_while_history_and_the_attempt_view_keep_them(self) -> None:
        fixture = self.active_fixture()
        recorded = self.amend(fixture)
        history_id = recorded["history_id"]
        brief = msgspec.structs.replace(fixture.brief, artifact_revision=2, branch="codex/rebound")
        published = fixture.call_native_tool(
            mcp_server.BRIEF_PUBLISH_TOOL,
            {**self.roots(fixture), "brief": self.json_object(msgspec.json.decode(msgspec.json.encode(brief)))},
        )
        self.assertEqual("committed", published["status"], published)
        rebound = self.transition(
            fixture,
            "rebind-attempt:work-a-1",
            {
                "branch": "codex/rebound",
                "base_revision": brief.base_revision,
                "brief_artifact_ref_id": self.json_object(published["reference"])["artifact_ref_id"],
            },
        )
        self.assertIn(f"history {history_id}", str(rebound["continuation"]))
        self.assertEqual([], self.inspection(fixture)["amendments"])
        attempt_view = (fixture.work / "views" / "attempts" / "work-a-1.md").read_text(encoding="utf-8")
        self.assertIn("ended when the attempt was rebound", attempt_view)
        history_view = (fixture.work / "views" / "history" / f"{history_id}.md").read_text(encoding="utf-8")
        self.assertIn(str(STALE_DOCUMENTATION["human_decision"]), history_view)
        self.assert_valid(fixture)

    def test_active_amendment_keeps_the_worker_lease_but_refreshes_its_selected_actions(self) -> None:
        fixture = self.active_fixture()
        lease = self.native_attempt_acquire(fixture, "worker-task")
        selected = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        self.amend(fixture)
        stale = self.transition_result(fixture, selected, {"candidate": fixture.candidate_revision})
        self.assertEqual(("rejected", "refresh-action"), (stale["status"], stale["retry"]), stale)
        refreshed = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        self.assertNotEqual(selected["subject_revision"], refreshed["subject_revision"])
        (status_attempt,) = self.json_array(self.item_leaf(fixture)["attempts"])
        (amendment,) = self.json_array(self.json_object(status_attempt)["amendments"])
        self.assertEqual(STALE_DOCUMENTATION["change"], self.json_object(amendment)["change"])

    def test_every_class_persists_reloads_exports_and_rolls_back_on_failure(self) -> None:
        fixture = self.active_fixture()
        for amendment_class in work_models.AmendmentClass:
            self.amend(fixture, STALE_DOCUMENTATION | {"amendment_class": amendment_class.value})
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        self.assertEqual(
            tuple(work_models.AmendmentClass),
            tuple(value.amendment.amendment_class for value in fresh.lifecycle.amendments),
        )
        exported = self.run_json_cli(*fixture.common, "export")
        self.assertEqual("pinboard-project-export/v6", exported["schema"])
        self.assertEqual(
            [value.value for value in work_models.AmendmentClass],
            [self.json_object(value)["amendment_class"] for value in self.json_array(exported["amendments"])],
        )
        self.assert_valid(fixture)
        action = self.native_actions(fixture, "record-amendment", fixture.brief.attempt_id)
        before = (fixture.work / "state.sqlite3").read_bytes()
        with (
            patch(
                "pinboard.adapters.sqlite.persistence.insert_amendment",
                side_effect=StorageError(StorageErrorCode.INVALID_STATE, "Controlled amendment write failure."),
            ),
            contextlib.suppress(Exception),
        ):
            fixture.call_native_tool(
                mcp_server.TRANSITION_TOOL,
                self.native_transition_request(fixture, action, STALE_DOCUMENTATION),
            )
        self.assertEqual(before, (fixture.work / "state.sqlite3").read_bytes())
        self.assertEqual(
            fresh.lifecycle.amendments,
            SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot().lifecycle.amendments,
        )

    def test_v13_ledgers_advance_reversibly_until_an_amendment_is_written(self) -> None:
        fixture = self.active_fixture()
        path = fixture.work / "state.sqlite3"
        expected = fixture.store.validated_snapshot()
        downgrade_to_v13(path)
        original = path.read_bytes()
        preview = schema_procedure.preview_schema(path)
        assert isinstance(preview, schema_procedure.PlannedSchema), preview
        self.assertIsInstance(preview.plan, schema_procedure.AmendmentAdvancePlan)
        self.assertEqual(original, path.read_bytes())
        applied = schema_procedure.apply_schema(path, preview.plan_id)
        assert isinstance(applied, schema_procedure.AppliedSchema), applied
        self.assertEqual(("forward", 14), (applied.direction, applied.resulting_version))
        self.assertEqual(original, (path.parent / "migration" / f"{preview.plan_id}.v13").read_bytes())
        self.assertEqual(expected, SQLiteWorkStore(path).validated_snapshot())
        resumed = schema_procedure.apply_schema(path, preview.plan_id)
        assert isinstance(resumed, schema_procedure.AppliedSchema), resumed
        self.assertEqual((), resumed.changed_surfaces)
        reverse = schema_procedure.preview_schema(path, preview.plan_id)
        assert isinstance(reverse, schema_procedure.PlannedSchema), reverse
        restored = schema_procedure.apply_schema(path, reverse.plan_id)
        assert isinstance(restored, schema_procedure.AppliedSchema), restored
        self.assertEqual(original, path.read_bytes())
        self.assertIsInstance(schema_procedure.apply_schema(path, preview.plan_id), schema_procedure.AppliedSchema)
        self.amend(fixture)
        later = path.read_bytes()
        self.assertIsInstance(
            schema_procedure.preview_schema(path, preview.plan_id), schema_procedure.SchemaProcedureFailure
        )
        self.assertEqual(later, path.read_bytes())
        with contextlib.closing(sqlite3.connect(path)) as connection:
            self.assertEqual(1, connection.execute("SELECT count(*) FROM attempt_amendments").fetchone()[0])
