"""Retained checkpoint v1 recovery and its committed-effect contract."""

import asyncio
import base64
import hashlib
import sqlite3
import sys
from unittest.mock import patch

import msgspec
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from pinboard.adapters import checkpoint_compatibility, review_operations
from pinboard.adapters.files.errors import ArtifactError
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import candidate_snapshots, checkpoint_compatibility_models, stored_state, work_brief_models
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.mcp import contracts
from pinboard.mcp import execution as mcp_execution
from pinboard.mcp import job_operations as mcp_jobs
from tests import test_correction_source_review
from tests.checkpoint_support import AcceptedPackageFixture, CheckpointPackageSupport


class CheckpointCompatibilityTest(CheckpointPackageSupport):
    def test_successful_historical_patch_remedy_cannot_authorize_new_correction_start(self) -> None:
        fixture, history_id, _correction_id, patch_bytes = self.compatibility_review_fixture()
        remedy: dict[str, contracts.JsonValue] = {
            "kind": "package-initial-recovery",
            "attempt_id": "work-a-1",
            "candidate_revision": "b" * 40,
            "runtime": "codex",
            "background": False,
            "checkpoint_history_id": history_id,
            "candidate_patch": base64.b64encode(patch_bytes).decode(),
        }
        ready = mcp_jobs._review_job(str(fixture.project), str(fixture.work), remedy, mcp_execution.CancellationToken())
        self.assertEqual("ready", ready.content["status"])
        recovered = fixture.store.read_artifact_reference(
            work_models.ArtifactKind.EVIDENCE,
            f"work-a-1-{fixture.brief.checkpoint.checkpoint_id}-candidate",
            1,
        )
        assert recovered is not None
        fixture.payload.write_text('{"reason":"Prepare an actual new correction start."}', encoding="utf-8")
        self.transition_json(fixture, self.project_action(fixture, "return-for-correction:work-a-1"), fixture.payload)
        (fixture.project / "tests").mkdir()
        (fixture.project / "tests" / "test_only.py").write_text("assert True\n", encoding="utf-8")
        self.commit_all(fixture.project, "new correction preimage")
        helper = test_correction_source_review.CorrectionSourceReviewTest()
        _returned, choice = helper.submit_and_return(fixture, "new-test", committed=False)
        review = self.json_object(choice["brief_review"])
        review["starting_candidate"] = {
            "role": "candidate",
            "kind": "evidence",
            "key": recovered.key,
            "revision": recovered.revision,
            "selector": recovered.selector,
            "content_sha256": recovered.content_sha256,
            "size_bytes": recovered.size_bytes,
        }
        before = fixture.store.validated_snapshot()
        rejected = mcp_jobs._dispatch_job(
            str(fixture.project), str(fixture.work), choice, mcp_execution.CancellationToken()
        )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_INVALID", rejected.content["code"], rejected.content)
        self.assertEqual([], rejected.content["changed_surfaces"])
        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

    def test_native_recovery_attributes_collision_orphan_acceptance_and_reuse(self) -> None:
        for disposition in ("collision", "orphan", "acceptance-rejection", "reuse"):
            with self.subTest(disposition=disposition):
                fixture, history_id, _correction_id, patch_bytes = self.compatibility_review_fixture()
                choice: dict[str, contracts.JsonValue] = {
                    "kind": "package-initial-recovery",
                    "attempt_id": "work-a-1",
                    "candidate_revision": "b" * 40,
                    "runtime": "codex",
                    "background": False,
                    "checkpoint_history_id": history_id,
                    "candidate_patch": base64.b64encode(patch_bytes).decode(),
                }
                patch_path = (
                    fixture.work
                    / "artifacts"
                    / "evidence"
                    / f"work-a-1-{fixture.brief.checkpoint.checkpoint_id}-candidate"
                    / "1.patch"
                )
                if disposition in ("collision", "orphan"):
                    patch_path.parent.mkdir(parents=True, exist_ok=True)
                    patch_path.write_bytes(b"different" if disposition == "collision" else patch_bytes)
                if disposition == "reuse":
                    ready = mcp_jobs._review_job(
                        str(fixture.project), str(fixture.work), choice, mcp_execution.CancellationToken()
                    )
                    self.assertEqual("ready", ready.content["status"])
                before = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
                (fixture.work / "attempts" / "work-a-1" / "result.md").unlink()
                if disposition == "collision":
                    with self.assertRaises(ArtifactError):
                        mcp_jobs._review_job(
                            str(fixture.project), str(fixture.work), choice, mcp_execution.CancellationToken()
                        )
                    self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())
                    self.assertEqual(b"different", patch_path.read_bytes())
                    continue
                rejection = DecisionFailure(DecisionFailureCode.TRANSITION_INPUT_INVALID, "acceptance rejected", None)
                selected = (
                    patch.object(SQLiteWorkStore, "accept_artifact_reference", return_value=rejection)
                    if disposition == "acceptance-rejection"
                    else patch.object(
                        SQLiteWorkStore,
                        "accept_artifact_reference",
                        autospec=True,
                        side_effect=SQLiteWorkStore.accept_artifact_reference,
                    )
                )
                with selected:
                    outcome = mcp_jobs._review_job(
                        str(fixture.project), str(fixture.work), choice, mcp_execution.CancellationToken()
                    )
                expected_by_disposition: dict[str, list[str]] = {
                    "orphan": ["accepted-artifact-reference", "ledger"],
                    "acceptance-rejection": ["immutable-artifact"],
                    "reuse": [],
                }
                expected = expected_by_disposition[disposition]
                self.assertEqual(expected, outcome.content["changed_surfaces"], outcome.content)
                self.assertEqual("failed-after-publication" if expected else "rejected", outcome.content["status"])
                self.assertEqual(
                    "TRANSITION_INPUT_INVALID" if disposition == "acceptance-rejection" else "ACTION_NOT_AVAILABLE",
                    outcome.content["code"],
                )
                contracts.validate_result("pinboard_review_job", outcome.content)
                self.assertEqual(patch_bytes, patch_path.read_bytes())
                if disposition != "orphan":
                    self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

    def test_native_advertised_retained_remedies_preserve_selected_history_and_reload(self) -> None:
        for round_kind in ("package-initial", "package-correction"):
            with self.subTest(round_kind=round_kind):
                fixture, history_id, correction_id, patch_bytes = self.compatibility_review_fixture()
                before = fixture.store.validated_snapshot()
                roots: dict[str, contracts.JsonValue] = {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                }
                choice: dict[str, contracts.JsonValue] = {
                    "kind": round_kind,
                    "attempt_id": "work-a-1",
                    "candidate_revision": "b" * 40,
                    "runtime": "codex",
                    "background": False,
                    "checkpoint_history_id": history_id,
                }
                if round_kind == "package-correction":
                    choice["correction_history_id"] = correction_id

                async def scenario(
                    fixture: AcceptedPackageFixture,
                    history_id: int,
                    patch_bytes: bytes,
                    round_kind: str,
                    roots: dict[str, contracts.JsonValue],
                    choice: dict[str, contracts.JsonValue],
                    before: stored_state.StoredWorkState,
                ) -> None:
                    parameters = StdioServerParameters(command=sys.executable, args=("-m", "pinboard.mcp"))
                    async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                        await session.initialize()
                        negotiated = await session.list_tools()
                        self.assertEqual(20, len(negotiated.tools))
                        missing = await session.call_tool("pinboard_review_job", roots | {"review": choice})
                        self.assertFalse(missing.is_error, missing.content)
                        assert isinstance(missing.structured_content, dict)
                        remedy = self.json_object(missing.structured_content["recovery"])
                        self.assertEqual(hashlib.sha256(patch_bytes).hexdigest(), remedy["expected_patch_sha256"])
                        arguments = self.json_object(remedy["arguments"])
                        leaf = self.json_object(arguments["review"])
                        self.assertIsNone(leaf["candidate_patch"])
                        self.assertEqual("b" * 40, leaf["candidate_revision"])
                        self.assertNotEqual(remedy["historical_candidate"], leaf["candidate_revision"])
                        leaf["candidate_patch"] = base64.b64encode(patch_bytes).decode()
                        wrong = await session.call_tool(
                            str(remedy["tool"]),
                            arguments | {"review": leaf | {"candidate_patch": base64.b64encode(b"wrong").decode()}},
                        )
                        self.assertFalse(wrong.is_error, wrong.content)
                        assert isinstance(wrong.structured_content, dict)
                        self.assertFalse(wrong.structured_content["state_changed"])
                        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())
                        result = await session.call_tool(str(remedy["tool"]), arguments)
                        self.assertFalse(result.is_error, result.content)
                        assert isinstance(result.structured_content, dict)
                        self.assertEqual("ready", result.structured_content["status"])
                        prior = self.json_object(result.structured_content["prior_checkpoint_package"])
                        self.assertEqual(history_id, prior["history_id"])
                        self.assertEqual(hashlib.sha256(patch_bytes).hexdigest(), prior["candidate_sha256"])
                        round_ = self.json_object(result.structured_content["review_round"])
                        self.assertEqual("correction" if "correction" in round_kind else "initial", round_["kind"])
                        reloaded = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
                        self.assertEqual(before.lifecycle.work_items, reloaded.lifecycle.work_items)
                        self.assertEqual(before.lifecycle.attempts, reloaded.lifecycle.attempts)
                        self.assertEqual(before.authority, reloaded.authority)
                    async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                        await session.initialize()
                        repeated = await session.call_tool(str(remedy["tool"]), arguments)
                        self.assertFalse(repeated.is_error, repeated.content)
                        assert isinstance(repeated.structured_content, dict)
                        self.assertEqual([], repeated.structured_content["changed_surfaces"])
                    self.assertEqual(reloaded, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

                asyncio.run(scenario(fixture, history_id, patch_bytes, round_kind, roots, choice, before))

    def test_recovery_ingress_and_current_packages_reject_before_publication(self) -> None:
        base: dict[str, contracts.JsonValue] = {
            "kind": "package-initial-recovery",
            "attempt_id": "work-a-1",
            "candidate_revision": "b" * 40,
            "runtime": "codex",
            "background": False,
            "checkpoint_history_id": 1,
            "candidate_patch": "",
        }
        decoded = msgspec.convert(base, type=contracts.ReviewChoice, strict=True)
        self.assertEqual(b"", decoded.candidate_patch)
        changes: tuple[dict[str, contracts.JsonValue], ...] = (
            {"candidate_patch": None},
            {"candidate_patch": "???"},
            {"correction_history_id": 1},
            {"kind": "unknown"},
            {"checkpoint_history_id": 0},
            {"attempt_id": "../invalid"},
        )
        for change in changes:
            with self.subTest(change=change), patch.object(mcp_jobs, "resolve_source_checkout_root") as roots:
                result = mcp_jobs._review_job("/repo", "/board", base | change, mcp_execution.CancellationToken())
                self.assertEqual("REVIEW_JOB_INVALID", result.content["code"])
                roots.assert_not_called()
        for version in ("v2", "v3"):
            fixture, history_id, _correction_id = self.review_job_fixture()
            if version == "v2":
                fixture = self.accepted_package_fixture(candidate_form="current-head", local=True)
                self.retain_v2_checkpoint(fixture)
                history_id = int(
                    next(
                        row.history_id
                        for row in fixture.store.validated_snapshot().transition_receipts
                        if row.outcome_schema == "checkpoint-acceptance/v2"
                    )
                )
            before = fixture.store.validated_snapshot()
            outcome = mcp_jobs._review_job(
                str(fixture.project),
                str(fixture.work),
                base | {"checkpoint_history_id": history_id},
                mcp_execution.CancellationToken(),
            )
            self.assertEqual("rejected", outcome.content["status"], outcome.content)
            self.assertEqual(before, fixture.store.validated_snapshot())

    def test_native_recovery_preserves_publication_before_later_failures_and_unknown_code_rejection(self) -> None:
        for boundary in ("missing-result", "bad-correction", "context-reread", "prompt-publication"):
            with self.subTest(boundary=boundary):
                fixture, history_id, correction_id, patch_bytes = self.compatibility_review_fixture()
                choice: dict[str, contracts.JsonValue] = {
                    "kind": "package-correction-recovery",
                    "attempt_id": "work-a-1",
                    "candidate_revision": "b" * 40,
                    "runtime": "codex",
                    "background": False,
                    "checkpoint_history_id": history_id,
                    "correction_history_id": correction_id,
                    "candidate_patch": base64.b64encode(patch_bytes).decode(),
                }
                if boundary == "missing-result":
                    (fixture.work / "attempts" / "work-a-1" / "result.md").unlink()
                elif boundary == "bad-correction":
                    choice["correction_history_id"] = 99999
                failure = StorageError(StorageErrorCode.IO_ERROR, "later failure")
                if boundary == "context-reread":
                    selected_patch = patch.object(
                        checkpoint_compatibility.review_operations, "prepare_review_job", side_effect=failure
                    )
                elif boundary == "prompt-publication":
                    selected_patch = patch.object(
                        review_operations.dispatch_models, "publish_agent_prompt", side_effect=failure
                    )
                else:
                    selected_patch = patch.object(
                        review_operations, "_read_required_evidence", wraps=review_operations._read_required_evidence
                    )
                before = fixture.store.validated_snapshot()
                with selected_patch:
                    outcome = mcp_jobs._review_job(
                        str(fixture.project), str(fixture.work), choice, mcp_execution.CancellationToken()
                    )
                after = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
                self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
                self.assertEqual(before.lifecycle.work_items, after.lifecycle.work_items)
                self.assertEqual(before.lifecycle.attempts, after.lifecycle.attempts)
                self.assertEqual(before.authority, after.authority)
                self.assertEqual("failed-after-publication", outcome.content["status"], outcome.content)
                self.assertEqual(
                    "ACTION_NOT_AVAILABLE"
                    if boundary in ("missing-result", "bad-correction")
                    else "ARTIFACT_ACCEPTANCE_FAILED",
                    outcome.content["code"],
                )
                self.assertEqual(
                    ["immutable-artifact", "accepted-artifact-reference", "ledger"], outcome.content["changed_surfaces"]
                )
                self.assertEqual("do-not-retry", outcome.content["retry"])
                contracts.validate_result("pinboard_review_job", outcome.content)
                with self.assertRaises(msgspec.ValidationError):
                    contracts.validate_result(
                        "pinboard_review_job", outcome.content | {"code": "NOT_A_RECOGNIZED_CODE"}
                    )
                self.assertIsNotNone(
                    SQLiteWorkStore(fixture.work / "state.sqlite3").read_artifact_reference(
                        work_models.ArtifactKind.EVIDENCE,
                        f"work-a-1-{fixture.brief.checkpoint.checkpoint_id}-candidate",
                        1,
                    )
                )
                schema = contracts.union_schema_for(contracts.REVIEW_JOB_RESULT_TYPES)
                definitions = self.json_object(schema["$defs"])
                failed = self.json_object(definitions["ReviewJobFailedAfterPublication"])
                properties = self.json_object(failed["properties"])
                codes = self.json_array(self.json_object(properties["code"])["enum"])
                self.assertIn(outcome.content["code"], codes)
                self.assertNotIn("NOT_A_RECOGNIZED_CODE", codes)

    def compatibility_review_fixture(self) -> tuple[AcceptedPackageFixture, int, int, bytes]:
        fixture, history_id, correction_history_id = self.review_job_fixture()
        package = self.package(fixture)
        assert isinstance(package, work_brief_models.CheckpointReviewPackageV3)
        candidate_reference = next(
            value
            for value in fixture.store.validated_snapshot().artifact_references
            if value.key == package.candidate_snapshot.key
        )
        candidate_bytes = candidate_snapshots.decode_candidate_snapshot(
            (fixture.work / candidate_reference.selector).read_bytes()
        ).diff
        historical_candidate = f"working-tree-sha256:{hashlib.sha256(candidate_bytes).hexdigest()}"
        legacy = checkpoint_compatibility_models.CheckpointReviewPackage(
            package.attempt_id,
            package.item_id,
            historical_candidate,
            package.acceptance_evidence,
            package.accepted_scope,
            package.checkpoint,
            package.accepted_brief,
            package.result,
            package.implementation_review,
            package.verdict,
            package.review_basis,
        )
        self.replace_package(fixture, legacy)
        connection = sqlite3.connect(fixture.work / "state.sqlite3")
        try:
            connection.execute(
                "UPDATE transition_history SET outcome_json = json_set(outcome_json, '$.candidate', ?) WHERE history_id = ?",
                (historical_candidate, history_id),
            )
            connection.execute(
                "DELETE FROM artifact_refs WHERE artifact_ref_id = ?", (int(candidate_reference.artifact_ref_id),)
            )
            connection.commit()
        finally:
            connection.close()
        (fixture.work / candidate_reference.selector).unlink()
        return fixture, history_id, correction_history_id, candidate_bytes
