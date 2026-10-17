"""Human-read record text keeps its line breaks from input through storage, reload and every projection."""

import dataclasses
import hashlib
import json
import re
from pathlib import Path

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import checkpoint_packages, history_archives, work_brief_models, work_briefs
from pinboard.application.project_export import ProjectExport
from pinboard.domain import history
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture
from tests.native_support import native_tools
from tests.support import JsonObject, JsonValue
from tests.test_item_status_facts import ItemLifecycleSupport

RETURN_REASON = (
    "The views test misses the second line.\n\n"
    "Problem:\n- `views` renders only the first line.\n"
    "Fix:\n1. Indent continuation lines.\n2. Add a regression.\n"
    "Not authorized: new product decisions.\n\n"
    "References: review.md finding 1."
)
CONTINUE_EVIDENCE = (
    "The corrected views candidate is accepted; work continues.\n\n"
    "Verified:\n- The independent review is ready.\n"
    "Next:\n1. Accept the checkpoint.\n\n"
    "References: ready record 1."
)
ACCEPT_EVIDENCE = (
    "Record text keeps its line breaks; the item continues.\n\n"
    "Verified:\n- Criteria 1 and 2 hold.\n"
    "Accepted limits:\n- Older builds reject multi-line evidence.\n"
    "Not claimed: item completion.\n\n"
    "References: review.md sha256 0000."
)
PAUSE_REASON = (
    "Waiting for your choice of rendering.\n\n- `Indent` keeps the bullet.\n1. Then resume.\n\nReferences: board.html"
)
REVISE_REASON = "You chose to rename the item.\n\nThe outcome and criteria are unchanged."
# Markdown also ends a line at a bare CR or CRLF; each variant tries to open a heading and a list item.
CR_AND_CRLF_REASONS = tuple(
    f"Waiting for the maintainer.{line_break}# Injected heading{line_break}- injected item{line_break}{line_break}"
    f"References: board.html"
    for line_break in ("\r", "\r\n")
)
MARKDOWN_LINE_BREAK = re.compile(r"\r\n|\r|\n")


class RecordTextSupport(ItemLifecycleSupport):
    def native_fixture[T: CheckpointFixture](self, fixture: T) -> T:
        native = self.enterContext(native_tools())
        return dataclasses.replace(
            fixture,
            call_native_tool=native.call_native_tool,
            call_advertised_tool=native.call_advertised_tool,
        )

    def publish_brief(self, fixture: CheckpointFixture, brief: work_brief_models.WorkBrief) -> int:
        published = fixture.call_native_tool(
            mcp_server.BRIEF_PUBLISH_TOOL,
            {**self.roots(fixture), "brief": self.json_object(msgspec.json.decode(msgspec.json.encode(brief)))},
        )
        self.assertEqual("committed", published["status"], published)
        reference = self.json_object(published["reference"])["artifact_ref_id"]
        assert isinstance(reference, int)
        return reference

    def assert_nested_inside_container(self, container: str, prefix: str, text: str) -> None:
        """Every continuation line sits under the container, and removing that indent restores the text exactly."""

        self.assertTrue(container.startswith(prefix), container)
        nested = container.removeprefix(prefix)
        self.assertEqual(text, re.sub(r"(\r\n|\r|\n)  ", r"\1", nested))
        self.assertTrue(all(line == "" or line.startswith("  ") for line in MARKDOWN_LINE_BREAK.split(nested)[1:]))


class RecordTextJourneyTest(RecordTextSupport):
    """Commit one multi-line record of each kind through the native workflow and read it back everywhere."""

    def item_view(self, fixture: CheckpointFixture) -> str:
        return (fixture.work / "views" / "items" / "work-a.md").read_text(encoding="utf-8")

    def board_item(self, fixture: CheckpointFixture) -> JsonObject:
        html = (fixture.work / "views" / "board.html").read_text(encoding="utf-8")
        match = re.search(r'<script id="board-data" type="application/json">(.*?)</script>', html, re.DOTALL)
        assert match is not None
        items = self.json_array(self.json_object(json.loads(match.group(1)))["items"])
        return next(self.json_object(value) for value in items if self.json_object(value)["item_id"] == "work-a")

    def condition_text(self, fixture: CheckpointFixture) -> JsonValue:
        standing = self.json_object(self.board_item(fixture)["standing"])
        return self.json_object(standing["condition"])["text"]

    def pause_reason(self, fixture: CheckpointFixture) -> JsonValue:
        attempts = self.json_array(self.item_leaf(fixture)["attempts"])
        return self.json_object(attempts[-1])["pause_reason"]

    def assert_item_view_pause_reason(self, fixture: CheckpointFixture) -> None:
        nested = PAUSE_REASON.replace("\n", "\n  ").replace("\n  \n", "\n\n")
        view = self.item_view(fixture)
        self.assertIn(f"- Pause reason: {nested}\n- Dependency eligibility:", view)
        bullet = view.partition("- Pause reason: ")[2].partition("\n- Dependency eligibility:")[0]
        self.assertTrue(all(line == "" or line.startswith("  ") for line in bullet.split("\n")[1:]))

    def test_multiline_records_travel_from_input_through_reload_to_every_projection(self) -> None:  # noqa: PLR0915 - one ordered lifecycle journey
        fixture = self.native_fixture(self.checkpoint_fixture())
        checkpoint_id = fixture.brief.checkpoint.checkpoint_id

        returned = self.return_for_review(fixture, RETURN_REASON)
        context = fixture.call_advertised_tool(
            mcp_server.CORRECTION_CONTEXT_TOOL,
            {**self.roots(fixture), "attempt_id": "work-a-1", "correction_history_id": returned},
        )
        self.assertEqual(RETURN_REASON, context["correction_reason"])
        self.assertEqual(RETURN_REASON, self.json_object(self.item_leaf(fixture)["review_verdict"])["reason"])
        self.assertEqual(RETURN_REASON, self.condition_text(fixture))

        continued = self.submit_candidate(fixture, "continued")
        self.record_ready(fixture, continued)
        self.transition(
            fixture, "accept-review-and-continue:work-a-1", {"candidate": continued, "evidence": CONTINUE_EVIDENCE}
        )
        self.assertEqual(CONTINUE_EVIDENCE, self.condition_text(fixture))

        accepted = self.submit_candidate(fixture, "accepted")
        self.record_ready(fixture, accepted)
        self.transition(
            fixture,
            "accept-checkpoint:work-a-1",
            {"checkpoint": checkpoint_id, "candidate": accepted, "evidence": ACCEPT_EVIDENCE},
        )
        package_reference = next(
            value
            for value in fixture.store.validated_snapshot().artifact_references
            if value.key == f"work-a-1-{checkpoint_id}-review-package"
        )
        package = msgspec.json.decode(
            (fixture.work / package_reference.selector).read_bytes(), type=work_brief_models.CheckpointReviewPackageV3
        )
        self.assertEqual(ACCEPT_EVIDENCE, package.acceptance_evidence)
        self.assertEqual(ACCEPT_EVIDENCE, self.condition_text(fixture))

        self.close_prerequisite(fixture)
        next_brief = replace_struct(fixture.brief, artifact_revision=2)
        self.transition(fixture, "resume:work-a", {"brief_artifact_ref_id": self.publish_brief(fixture, next_brief)})
        self.transition(fixture, "pause:work-a-1", {"reason": PAUSE_REASON})
        self.assertEqual(PAUSE_REASON, self.pause_reason(fixture))
        self.assertEqual(PAUSE_REASON, self.condition_text(fixture))
        self.assert_item_view_pause_reason(fixture)

        definition = fixture.call_native_tool(
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
                "reason": REVISE_REASON,
                "definition": {**self.json_object(definition["definition"]), "title": "Renamed item"},
            },
        )
        revised = fixture.call_native_tool(
            mcp_server.ITEM_DEFINITION_TOOL,
            {"request": {**self.roots(fixture), "operation": "current", "item_id": "work-a"}},
        )
        revision = revised["definition_revision"]
        digest = revised["definition_digest"]
        assert isinstance(revision, int) and isinstance(digest, str)
        self.assertEqual(
            REVISE_REASON, self.json_object(self.json_array(self.board_item(fixture)["scope_revisions"])[-1])["reason"]
        )

        authorization = work_brief_models.AcceptedScopeAuthorization("work-a", revision)
        checkpoint = fixture.brief.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        rebound_brief = replace_struct(
            fixture.brief,
            artifact_revision=3,
            accepted_scope=work_brief_models.AcceptedScope(revision, digest),
            checkpoint=replace_struct(
                checkpoint,
                disposition=work_brief_models.TerminalCheckpointDisposition(),
                contracts=tuple(
                    replace_struct(value, authorization_basis=authorization) for value in checkpoint.contracts
                ),
                verification=tuple(
                    replace_struct(value, authorization_basis=authorization) for value in checkpoint.verification
                ),
            ),
        )
        self.transition(
            fixture,
            "rebind-attempt:work-a-1",
            {
                "branch": rebound_brief.branch,
                "base_revision": rebound_brief.base_revision,
                "brief_artifact_ref_id": self.publish_brief(fixture, rebound_brief),
            },
        )
        rebind_receipt = fixture.store.validated_snapshot().transition_receipts[-1]
        self.assertEqual("rebind-attempt", rebind_receipt.action_kind.value)
        self.assertEqual(
            PAUSE_REASON,
            msgspec.json.decode(bytes(rebind_receipt.outcome_payload), type=history.TransitionReceiptOutcome).evidence,
        )
        self.assertEqual(PAUSE_REASON, self.pause_reason(fixture))
        self.assertEqual(PAUSE_REASON, self.condition_text(fixture))
        self.assert_item_view_pause_reason(fixture)

        reloaded = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        evidence = {
            str(receipt.action_kind.value): msgspec.json.decode(
                bytes(receipt.outcome_payload),
                type=self.outcome_type(receipt.outcome_schema),
            ).evidence
            for receipt in reloaded.transition_receipts
            if receipt.action_kind.value
            in {"return-for-correction", "accept-review-and-continue", "accept-checkpoint", "pause"}
        }
        self.assertEqual(
            {
                "return-for-correction": RETURN_REASON,
                "accept-review-and-continue": CONTINUE_EVIDENCE,
                "accept-checkpoint": ACCEPT_EVIDENCE,
                "pause": PAUSE_REASON,
            },
            evidence,
        )
        self.assertEqual(
            REVISE_REASON,
            [value.reason for value in reloaded.lifecycle.definition_revisions if str(value.item_id) == "work-a"][-1],
        )
        board_before = (fixture.work / "views" / "board.html").read_bytes()
        view_before = self.item_view(fixture)
        self.assert_valid(fixture)
        self.rebuild_views(fixture)
        self.assertEqual(board_before, (fixture.work / "views" / "board.html").read_bytes())
        self.assertEqual(view_before, self.item_view(fixture))
        code, exported, errors = self.run_cli(*fixture.common, "export", "--json")
        self.assertEqual(0, code, errors)
        portable = msgspec.json.decode(exported, type=ProjectExport)
        self.assertEqual([ACCEPT_EVIDENCE], [value.acceptance_evidence for value in portable.checkpoint_packages])
        self.assertIn(
            ("work-a", revision, REVISE_REASON),
            [(value.item_id, value.revision, value.reason) for value in portable.definition_revisions],
        )
        exported_evidence = {
            value.action_kind: msgspec.json.decode(value.outcome, type=self.outcome_type(value.outcome_schema)).evidence
            for value in portable.transitions
            if value.action_kind in evidence
        }
        self.assertEqual(evidence, exported_evidence)

        self.transition(fixture, "resume:work-a", {})
        completed = self.submit_candidate(fixture, "completed")
        self.record_ready(fixture, completed)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        receipt = next(
            value for value in reloaded.transition_receipts if value.outcome_schema == "checkpoint-acceptance/v2"
        )
        self.transition(
            fixture,
            "complete:work-a-1",
            {
                "schema": "pinboard-reviewed-completion/v3",
                "human_decision": "Complete this item with its reviewed outcome.",
                "candidate": completed,
                "evidence": "The item outcome is complete.",
                "reviewer_task_id": "independent-reviewer",
                "result_sha256": self.digest(attempt_root / "result.md"),
                "review_sha256": self.digest(attempt_root / "review.md"),
                "packages": [
                    {
                        "history_id": int(receipt.history_id),
                        "package_sha256": package_reference.content_sha256,
                        "disposition": "revalidated",
                        "evidence": "The accepted package still holds.",
                    }
                ],
            },
        )
        final = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        attempt = next(value for value in final.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        facts = history_archives.select_archive_facts(
            attempt, final.artifact_references, final.transition_receipts, final.lifecycle.definition_revisions
        )
        artifacts = ArtifactRepository(resolve_durable_roots(fixture.project, fixture.work))
        archive = history_archives.derive_archive(
            facts, {value.artifact_ref_id: artifacts.read(value) for value in facts.artifact_references}, ()
        )
        assert isinstance(archive, history_archives.HistoryArchive)
        self.assertEqual([ACCEPT_EVIDENCE], [value.acceptance_evidence for value in archive.checkpoints])
        rendered = history_archives.render_archive(archive).decode()
        bullet = rendered.partition("## Original accepted checkpoints\n\n")[2].partition("\n\n## Original completion")[
            0
        ]
        self.assertTrue(bullet.startswith("- History "))
        self.assertTrue(bullet.endswith(history.nest_record_text(ACCEPT_EVIDENCE, "  ")))
        self.assertTrue(all(line == "" or line.startswith(("- History ", "  ")) for line in bullet.split("\n")))
        for evidence in CR_AND_CRLF_REASONS:
            with self.subTest(evidence=evidence):
                rendered = history_archives.render_archive(
                    replace_struct(
                        archive,
                        checkpoints=tuple(
                            replace_struct(value, acceptance_evidence=evidence) for value in archive.checkpoints
                        ),
                    )
                ).decode()
                bullet = rendered.partition("## Original accepted checkpoints\n\n")[2].partition(
                    "\n\n## Original completion"
                )[0]
                self.assert_nested_inside_container(bullet, bullet.partition(". Waiting")[0] + ". ", evidence)

    def outcome_type(
        self, schema: str
    ) -> type[history.TransitionReceiptOutcome] | type[history.CheckpointAcceptanceOutcome]:
        return (
            history.CheckpointAcceptanceOutcome
            if schema == "checkpoint-acceptance/v2"
            else history.TransitionReceiptOutcome
        )

    def digest(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def rebuild_views(self, fixture: CheckpointFixture) -> None:
        rebuilt, stdout, stderr = self.run_cli(*fixture.common, "views", "rebuild")
        self.assertEqual(0, rebuilt, f"{stdout}\n{stderr}")


class RecordTextProjectionTest(RecordTextSupport):
    def test_reviewer_prompt_keeps_a_multiline_correction_reason_inside_its_sentence(self) -> None:
        for reason in (RETURN_REASON, *CR_AND_CRLF_REASONS):
            with self.subTest(reason=reason):
                fixture = self.native_fixture(self.checkpoint_fixture())
                returned = self.return_for_review(fixture, reason)
                candidate = self.submit_candidate(fixture, "corrected")
                job = self.review_result(
                    fixture, {"kind": "correction", "candidate_revision": candidate, "correction_history_id": returned}
                )
                self.assertEqual("ready", job["status"], job)
                self.assertEqual(reason, self.json_object(job["review_round"])["reason"])
                selector = str(self.json_object(job["prompt_reference"])["selector"])
                prompt = (fixture.work / selector).read_bytes().decode()
                self.assertIn(f"reason (JSON string): {msgspec.json.encode(reason).decode()}. ", prompt)
                self.assertNotIn("\r", prompt)
                prompt_lines = set(MARKDOWN_LINE_BREAK.split(prompt))
                self.assertFalse(prompt_lines.intersection(filter(None, MARKDOWN_LINE_BREAK.split(reason)[1:])))

    def test_item_view_keeps_a_cr_or_crlf_pause_reason_inside_its_bullet(self) -> None:
        for reason in CR_AND_CRLF_REASONS:
            with self.subTest(reason=reason):
                fixture = self.native_fixture(self.checkpoint_fixture())
                self.return_for_review(fixture, "Rework the candidate.")
                self.transition(fixture, "pause:work-a-1", {"reason": reason})
                attempts = self.json_array(self.item_leaf(fixture)["attempts"])
                self.assertEqual(reason, self.json_object(attempts[-1])["pause_reason"])
                view = (fixture.work / "views" / "items" / "work-a.md").read_bytes().decode()
                bullet = view.partition("\n- Pause reason: ")[2].partition("\n- Dependency eligibility:")[0]
                self.assert_nested_inside_container(bullet, "", reason)

    def test_continuing_completion_refusal_carries_remaining_work_as_written(self) -> None:
        remaining = "Two pieces of work remain.\n\n1. Render the board.\n2. Update the guidance.\n\nReferences: a.md"
        fixture = self.native_fixture(self.checkpoint_fixture())
        self.return_for_review(fixture, "Continue the accepted work.")
        checkpoint = fixture.brief.checkpoint
        rebound = replace_struct(
            fixture.brief,
            artifact_revision=2,
            checkpoint=replace_struct(
                checkpoint, disposition=work_brief_models.ContinueCheckpointDisposition(remaining)
            ),
        )
        self.transition(
            fixture,
            "rebind-attempt:work-a-1",
            {
                "branch": rebound.branch,
                "base_revision": rebound.base_revision,
                "brief_artifact_ref_id": self.publish_brief(fixture, rebound),
            },
        )
        result = self.actions_result(
            fixture, {"role": "project", "action_id": {"kind": "complete", "subject": "work-a-1"}}
        )
        self.assertEqual("rejected", result["status"], result)
        self.assertIn(remaining, str(result["message"]))

    def test_stored_records_decode_record_text_and_bindings_stay_exact(self) -> None:
        for evidence in (ACCEPT_EVIDENCE, "x", "  surrounding whitespace  ", "carriage\rreturn"):
            with self.subTest(evidence=evidence):
                encoded = history.encode_checkpoint_acceptance_outcome(
                    candidate="candidate", checkpoint="checkpoint-a", evidence=evidence, outcome="accept-checkpoint"
                )
                decoded = msgspec.json.decode(encoded, type=history.CheckpointAcceptanceOutcome, strict=True)
                self.assertEqual(evidence, decoded.evidence)
        with self.assertRaises(msgspec.ValidationError):
            msgspec.json.decode(
                history.encode_checkpoint_acceptance_outcome(
                    candidate="candidate", checkpoint="checkpoint-a", evidence="", outcome="accept-checkpoint"
                ),
                type=history.CheckpointAcceptanceOutcome,
            )
        fixture = self.accepted_package_fixture()
        package = self.package(fixture)
        receipt = next(
            value
            for value in fixture.store.validated_snapshot().transition_receipts
            if value.outcome_schema == "checkpoint-acceptance/v2"
        )
        for evidence, accepted in ((package.acceptance_evidence, True), (package.acceptance_evidence + "\n", False)):
            with self.subTest(evidence=evidence):
                validated = checkpoint_packages.validate_selected_checkpoint_review_package(
                    receipt,
                    fixture.package_reference,
                    work_briefs.canonical_checkpoint_review_package_bytes(
                        replace_struct(package, acceptance_evidence=evidence)
                    ),
                    attempt_id="work-a-1",
                    item_id="work-a",
                )
                self.assertEqual(accepted, not isinstance(validated, work_brief_models.WorkBriefFailure))
