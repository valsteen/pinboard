"""Attempt amendments persist, change the review contract and keep attempt conditions through the MCP boundary."""

import contextlib
import dataclasses
import re
import sqlite3
from pathlib import Path
from unittest.mock import patch

import msgspec

from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.migration import schema_procedure
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import decision_batches, query_models, stored_state, work_brief_models
from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture
from tests.native_support import native_tools
from tests.sqlite_support import advanced_v14_copy, downgrade_to_v13, downgrade_to_v15
from tests.support import HOLD_UNBLOCKER, JsonObject, JsonValue
from tests.test_item_status_facts import ItemLifecycleSupport
from tests.work_brief_support import work_c_brief

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

    def publish_brief(self, fixture: CheckpointFixture, brief: work_brief_models.AcceptedBrief) -> int:
        published = fixture.call_advertised_tool(
            mcp_server.BRIEF_PUBLISH_TOOL,
            {**self.roots(fixture), "brief": self.json_object(msgspec.json.decode(msgspec.json.encode(brief)))},
        )
        self.assertEqual("committed", published["status"], published)
        reference = self.json_object(published["reference"])["artifact_ref_id"]
        assert isinstance(reference, int)
        return reference

    def replacement(self, fixture: CheckpointFixture, revision: int) -> JsonObject:
        brief = msgspec.structs.replace(
            fixture.brief, artifact_revision=revision, testing_strategy=f"Verify replacement brief {revision}."
        )
        return {
            "branch": brief.branch,
            "base_revision": brief.base_revision,
            "brief_artifact_ref_id": self.publish_brief(fixture, brief),
        }

    def current_amendments(self, fixture: CheckpointFixture) -> list[JsonObject]:
        return [self.json_object(value) for value in self.json_array(self.inspection(fixture)["amendments"])]

    def assert_rejected_unchanged(
        self, fixture: CheckpointFixture, action: JsonObject, payload: JsonObject, code: str
    ) -> JsonObject:
        path = fixture.work / "state.sqlite3"
        before = path.read_bytes()
        result = fixture.call_advertised_tool(
            mcp_server.TRANSITION_TOOL, self.native_transition_request(fixture, action, payload)
        )
        self.assertEqual(("rejected", "unchanged", code), (result["status"], result["effect"], result["code"]), result)
        self.assertEqual(before, path.read_bytes())
        return result

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

    def test_rebind_carries_original_approval_twice_and_ends_only_the_selected_approval(self) -> None:
        fixture, _reviewed = self.reviewed_fixture()
        first = self.amend(fixture)["history_id"]
        words = "Yes, fix that test.\nOnly the flaky time assertion."
        second = self.amend(
            fixture,
            STALE_DOCUMENTATION
            | {
                "amendment_class": "flaky-or-broken-test",
                "change": "Fix the flaky time assertion.",
                "human_decision": words,
            },
        )["history_id"]
        current = self.current_amendments(fixture)
        action_id = "rebind-attempt:work-a-1"
        for revision in (2, 3):
            result = self.transition(fixture, action_id, self.replacement(fixture, revision))
            self.assertIn("Current amendment approvals", str(result["continuation"]))
            self.assertEqual(current, self.current_amendments(fixture))
            self.assertEqual(self.item_leaf(fixture), self.fresh_status(fixture))

        proposed = self.replacement(fixture, 4)
        action = self.project_action(fixture, action_id)
        superseding_words = "Replace the install instructions instead.\nStop the earlier line repair."
        supersession: JsonObject = {"history_id": first, "human_decision": superseding_words}
        invalid_selections: tuple[list[JsonValue], ...] = (
            [supersession, supersession],
            [supersession | {"history_id": 99_999}],
        )
        for invalid in invalid_selections:
            rejected = self.assert_rejected_unchanged(
                fixture, action, proposed | {"supersessions": invalid}, "TRANSITION_INPUT_INVALID"
            )
            self.assertIn("pinboard_attempt_inspect", str(rejected["message"]))
        rejected = self.assert_rejected_unchanged(
            fixture,
            action,
            proposed | {"supersessions": [supersession | {"human_decision": "\x00" + superseding_words}]},
            "TRANSITION_INPUT_INVALID",
        )
        self.assertIn("supersessions.human_decision", str(rejected["message"]))
        self.assertEqual(
            (False, "correct-input", []), (rejected["state_changed"], rejected["retry"], rejected["changed_surfaces"])
        )
        self.assertEqual(current, self.current_amendments(fixture))
        reply = self.transition(fixture, action_id, proposed | {"supersessions": [supersession]})
        self.assertIn(f"history IDs {first}", str(reply["continuation"]))
        self.assertEqual([current[1]], self.current_amendments(fixture))
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        original, adjacent = fresh.lifecycle.amendments
        self.assertEqual(
            (first, STALE_DOCUMENTATION["human_decision"]), (original.history_id, original.amendment.human_decision)
        )
        self.assertIsInstance(original.end, stored_state.SupersededAmendment)
        assert isinstance(original.end, stored_state.SupersededAmendment)
        self.assertEqual(
            (reply["history_id"], superseding_words), (original.end.history_id, original.end.human_decision)
        )
        self.assertEqual((second, None), (adjacent.history_id, adjacent.end))
        next_brief = self.replacement(fixture, 5)
        self.assert_rejected_unchanged(fixture, action, next_brief, "ACTION_NOT_AVAILABLE")
        self.assert_rejected_unchanged(
            fixture,
            self.project_action(fixture, action_id),
            next_brief | {"supersessions": [supersession]},
            "TRANSITION_INPUT_INVALID",
        )
        attempt_view = (fixture.work / "views" / "attempts" / "work-a-1.md").read_text(encoding="utf-8")
        self.assertIn("Stop the earlier line repair.", attempt_view)
        original_history = (fixture.work / "views" / "history" / f"{first}.md").read_text(encoding="utf-8")
        self.assertIn(str(STALE_DOCUMENTATION["human_decision"]), original_history)
        self.assertNotIn(superseding_words, original_history)
        candidate = self.resubmit(fixture, "new-brief")
        delta = self.review_result(fixture, {"kind": "amendment-delta", "candidate_revision": candidate})
        self.assertEqual(("rejected", "unchanged"), (delta["status"], delta["effect"]), delta)
        self.assertIn("'initial'", str(delta))
        job = self.review_result(fixture, {"kind": "initial", "candidate_revision": candidate})
        self.assertEqual("ready", job["status"], job)
        prompt = self.prompt_text(fixture, job)
        self.assertIn("Verify replacement brief 4.", prompt)
        self.assertIn(msgspec.json.encode(words).decode(), prompt)
        self.assertNotIn(str(STALE_DOCUMENTATION["human_decision"]), prompt)
        self.assert_valid(fixture)

    def test_rebind_refuses_an_approval_owned_by_another_attempt(self) -> None:
        fixture = self.native(self.active_fixture())
        branch = fixture.brief.branch
        brief = msgspec.structs.replace(
            work_c_brief(), branch=branch, base_revision=fixture.brief.base_revision, owner_task_id="coordinator-task"
        )
        prepared = fixture.call_advertised_tool(
            mcp_server.PREPARATION_AUTHORITY_TOOL,
            {
                "request": {
                    **self.roots(fixture),
                    "operation": "start",
                    "item_id": "work-c",
                    "task_id": "coordinator-task",
                    "host_id": "local",
                    "ttl_seconds": 300,
                }
            },
        )
        self.assertEqual("committed", prepared["status"], prepared)
        brief_id = self.publish_brief(fixture, brief)
        activate = self.native_actions(fixture, "activate", "work-c", role="preparer", lease=prepared)
        invocation = self.json_object(activate["invocation"])
        arguments = self.json_object(invocation["arguments"])
        request = self.json_object(arguments["request"])
        activated = fixture.call_advertised_tool(
            mcp_server.TRANSITION_TOOL, {"request": request | {"payload": {"brief_artifact_ref_id": brief_id}}}
        )
        self.assertEqual("committed", activated["status"], activated)
        foreign = self.amend(dataclasses.replace(fixture, brief=brief))["history_id"]
        self.assert_rejected_unchanged(
            fixture,
            self.project_action(fixture, "rebind-attempt:work-a-1"),
            self.replacement(fixture, 2)
            | {"supersessions": [{"history_id": foreign, "human_decision": "Supersede it."}]},
            "TRANSITION_INPUT_INVALID",
        )

    def test_revised_resume_preserves_approval_and_fences_delta_after_switch_away_and_back(self) -> None:
        fixture, _reviewed = self.reviewed_fixture()
        self.close_prerequisite(fixture)
        self.amend(fixture)
        (original,) = self.current_amendments(fixture)
        second = self.amend(fixture, STALE_DOCUMENTATION | {"change": "Fix the old usage example."})
        replacement = self.replacement(fixture, 2)["brief_artifact_ref_id"]
        human_decision = "Reject invalid input."
        self.transition(
            fixture,
            "pause:work-a-1",
            {
                "schema": "pinboard-pause-decisions/v1",
                "reason": "Choose input behavior.",
                "decisions": [
                    {
                        "decision_id": "input-policy",
                        "question": "Which input policy?",
                        "options": ["Reject", "Replace"],
                        "recommendation": "Reject",
                        "unblocks": "Input handling",
                        "deferral_cost": "Input work waits.",
                    }
                ],
            },
        )
        resolution: JsonObject = {"kind": "answer", "decision_id": "input-policy", "human_decision": human_decision}
        first_resume = self.transition(
            fixture,
            "resume:work-a",
            {
                "schema": "pinboard-resolve-pause-decisions/v1",
                "brief_artifact_ref_id": replacement,
                "resolutions": [resolution],
                "supersessions": [
                    {"history_id": second["history_id"], "human_decision": "Use the replacement example."}
                ],
            },
        )
        snapshot = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        self.assertEqual(decision_batches.REVISED_RESUME_SCHEMA, snapshot.transition_receipts[-1].input_schema)
        context = fixture.store.read_attempt_context(AttemptId("work-a-1"))
        assert isinstance(context, query_models.NonterminalAttemptContextFacts)
        assert context.decision_batch is not None and context.decision_batch.resolution is not None
        self.assertEqual(human_decision, context.decision_batch.resolution.resolutions[0].human_decision)
        self.assertEqual(first_resume["history_id"], context.decision_batch.resolution.resume_history_id)
        self.assertEqual([original], self.current_amendments(fixture))
        self.transition(
            fixture, "pause:work-a-1", {"reason": "Switch to the original brief.", "unblocker": HOLD_UNBLOCKER}
        )
        self.transition(fixture, "resume:work-a", {"brief_artifact_ref_id": original["brief_artifact_ref_id"]})
        self.assertEqual([original], self.current_amendments(fixture))
        candidate = self.resubmit(fixture, "after-resume-back")
        refused = self.review_result(fixture, {"kind": "amendment-delta", "candidate_revision": candidate})
        self.assertEqual(("rejected", "unchanged"), (refused["status"], refused["effect"]), refused)
        self.assertIn("'initial'", str(refused))
        job = self.review_result(fixture, {"kind": "initial", "candidate_revision": candidate})
        self.assertEqual("ready", job["status"], job)
        self.assertIn(str(STALE_DOCUMENTATION["human_decision"]), self.prompt_text(fixture, job))
        self.assert_valid(fixture)

    def test_delta_distinguishes_a_known_same_brief_resume_from_an_unbound_v15_resume(self) -> None:
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                fixture, reviewed = self.reviewed_fixture()
                self.close_prerequisite(fixture)
                approval = self.amend(fixture)["history_id"]
                self.transition(
                    fixture, "pause:work-a-1", {"reason": "Wait for the owner.", "unblocker": HOLD_UNBLOCKER}
                )
                resumed = self.transition(fixture, "resume:work-a", {})
                if legacy:
                    path = fixture.work / "state.sqlite3"
                    # This disposable v15 predecessor represents the exact old input with no brief binding.
                    with contextlib.closing(sqlite3.connect(path, autocommit=True)) as connection:
                        connection.execute(
                            """UPDATE transition_history SET input_schema = 'decision/v1', input_json = '{}'
                               WHERE history_id = ?""",
                            (resumed["history_id"],),
                        )
                    downgrade_to_v15(path)
                    plan = self.run_json_cli(*fixture.common, "migrate-schema")["plan_id"]
                    self.run_json_cli(*fixture.common, "migrate-schema", "--apply", str(plan))
                self.assertEqual([approval], [value["history_id"] for value in self.current_amendments(fixture)])
                candidate = self.resubmit(fixture, "after-resume")
                delta = self.review_result(fixture, {"kind": "amendment-delta", "candidate_revision": candidate})
                if legacy:
                    self.assertEqual(("rejected", "unchanged"), (delta["status"], delta["effect"]), delta)
                    self.assertIn(f"Resume history {resumed['history_id']}", str(delta["message"]))
                    self.assertIn("'initial'", str(delta))
                else:
                    self.assertEqual("ready", delta["status"], delta)
                    self.assertEqual(reviewed, self.json_object(delta["review_round"])["reviewed_candidate"])

    def test_inspection_work_is_independent_of_unrelated_history_after_rebind(self) -> None:
        original_open = sqlite_store.open_database

        def inspect(fixture: CheckpointFixture) -> tuple[query_models.AttemptAmendmentFacts, int]:
            steps = 0

            def count_step() -> int:
                nonlocal steps
                steps += 1
                return 0

            def measured_open(path: Path, mode: OpenMode) -> sqlite3.Connection:
                connection = original_open(path, mode)
                connection.set_progress_handler(count_step, 1)
                return connection

            with patch.object(sqlite_store, "open_database", measured_open):
                selected = fixture.store.read_attempt_inspection_context(AttemptId("work-a-1"))
            assert isinstance(selected, query_models.NonterminalAttemptContextFacts)
            return selected.amendments, steps

        def add_unrelated_hold_cycle(fixture: CheckpointFixture) -> None:
            self.transition(fixture, "block-item:work-c", {"unblocker": HOLD_UNBLOCKER})
            self.transition(fixture, "resume:work-c", {})

        for migrated in (False, True):
            with self.subTest(migrated=migrated):
                fixture = self.active_fixture()
                approval = self.amend(fixture)["history_id"]
                if migrated:
                    downgrade_to_v15(fixture.work / "state.sqlite3")
                    plan = self.run_json_cli(*fixture.common, "migrate-schema")["plan_id"]
                    self.run_json_cli(*fixture.common, "migrate-schema", "--apply", str(plan))
                self.transition(fixture, "rebind-attempt:work-a-1", self.replacement(fixture, 2))
                # Seed an unrelated key so both reads encounter the same index range boundary.
                add_unrelated_hold_cycle(fixture)
                before, before_steps = inspect(fixture)
                self.assertEqual([approval], [value.history_id for value in before.current])
                self.assertEqual((decision_models.ActionKind.REBIND_ATTEMPT,), before.later_review_kinds)
                self.assertGreater(before_steps, 0)

                for _ in range(25):
                    add_unrelated_hold_cycle(fixture)

                after, after_steps = inspect(fixture)
                self.assertEqual(before, after)
                self.assertEqual(before_steps, after_steps)

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
        self.assertEqual("pinboard-project-export/v8", exported["schema"])
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
        self.assertEqual(expected, SQLiteWorkStore(advanced_v14_copy(path)).validated_snapshot())
        resumed = schema_procedure.apply_schema(path, preview.plan_id)
        assert isinstance(resumed, schema_procedure.AppliedSchema), resumed
        self.assertEqual((), resumed.changed_surfaces)
        reverse = schema_procedure.preview_schema(path, preview.plan_id)
        assert isinstance(reverse, schema_procedure.PlannedSchema), reverse
        restored = schema_procedure.apply_schema(path, reverse.plan_id)
        assert isinstance(restored, schema_procedure.AppliedSchema), restored
        self.assertEqual(original, path.read_bytes())
        self.assertIsInstance(schema_procedure.apply_schema(path, preview.plan_id), schema_procedure.AppliedSchema)
        holds = schema_procedure.preview_schema(path)
        assert isinstance(holds, schema_procedure.PlannedSchema), holds
        self.assertIsInstance(schema_procedure.apply_schema(path, holds.plan_id), schema_procedure.AppliedSchema)
        lifetime = schema_procedure.preview_schema(path)
        assert isinstance(lifetime, schema_procedure.PlannedSchema), lifetime
        self.assertIsInstance(schema_procedure.apply_schema(path, lifetime.plan_id), schema_procedure.AppliedSchema)
        self.amend(fixture)
        later = path.read_bytes()
        self.assertIsInstance(
            schema_procedure.preview_schema(path, holds.plan_id), schema_procedure.SchemaProcedureFailure
        )
        self.assertIsInstance(
            schema_procedure.preview_schema(path, preview.plan_id), schema_procedure.SchemaProcedureFailure
        )
        self.assertEqual(later, path.read_bytes())
        with contextlib.closing(sqlite3.connect(path)) as connection:
            self.assertEqual(1, connection.execute("SELECT count(*) FROM attempt_amendments").fetchone()[0])

    def test_v15_advance_preserves_effective_membership_and_exact_old_approval_and_receipts(self) -> None:
        fixture = self.native(self.active_fixture())
        path = fixture.work / "state.sqlite3"
        historical = self.amend(fixture)["history_id"]
        self.transition(fixture, "rebind-attempt:work-a-1", self.replacement(fixture, 2))
        current = self.amend(fixture, STALE_DOCUMENTATION | {"human_decision": "Yes, revise that usage line."})[
            "history_id"
        ]
        before = fixture.store.validated_snapshot()
        self.assertEqual([historical, current], [value["history_id"] for value in self.current_amendments(fixture)])
        downgrade_to_v15(path)
        predecessor = path.read_bytes()
        refusal, stdout, _ = self.run_cli(*fixture.common, "status", "--json")
        self.assertNotEqual(0, refusal)
        self.assertIn("SCHEMA_UNSUPPORTED", stdout)
        self.assertEqual(predecessor, path.read_bytes())
        preview = self.run_json_cli(*fixture.common, "migrate-schema")
        self.assertEqual("amendment-lifetime-advance", self.json_object(preview["plan"])["kind"])
        self.assertEqual(predecessor, path.read_bytes())
        plan = str(preview["plan_id"])
        applied = self.run_json_cli(*fixture.common, "migrate-schema", "--apply", plan)
        self.assertEqual(
            ("migrated", "sqlite-v16", None), (applied["status"], applied["authority"], applied["warning"])
        )
        self.assertEqual(predecessor, (path.parent / "migration" / f"{plan}.v15").read_bytes())
        after = SQLiteWorkStore(path).validated_snapshot()
        self.assertEqual(before.transition_receipts, after.transition_receipts)
        self.assertEqual(before.lifecycle.project.revision, after.lifecycle.project.revision)
        self.assertEqual(before.artifact_references, after.artifact_references)
        self.assertEqual(
            before.lifecycle.amendments,
            tuple(dataclasses.replace(value, end=None) for value in after.lifecycle.amendments),
        )
        self.assertIsInstance(after.lifecycle.amendments[0].end, stored_state.MigratedAmendmentEnd)
        self.assertIsNone(after.lifecycle.amendments[1].end)
        self.assertEqual([current], [value["history_id"] for value in self.current_amendments(fixture)])
        self.assert_valid(fixture)
        reverse = self.run_json_cli(*fixture.common, "migrate-schema", "--reverse", plan)
        restored = self.run_json_cli(*fixture.common, "migrate-schema", "--apply", str(reverse["plan_id"]))
        self.assertEqual("sqlite-v15", restored["authority"])
        self.assertEqual(predecessor, path.read_bytes())
        self.run_json_cli(*fixture.common, "migrate-schema", "--apply", plan)
        self.transition(fixture, "rebind-attempt:work-a-1", self.replacement(fixture, 3))
        self.assertEqual([current], [value["history_id"] for value in self.current_amendments(fixture)])
        blocked = schema_procedure.preview_schema(path, plan)
        self.assertIsInstance(blocked, schema_procedure.SchemaProcedureFailure)
