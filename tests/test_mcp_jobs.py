import asyncio
import io
import json
import re
import sys
import tempfile
import threading
from collections.abc import Callable
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import msgspec
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from pinboard.adapters import dispatch_operations
from pinboard.adapters.files.brief_sources import select_checkout_brief_source
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import work_brief_models, work_briefs
from pinboard.application.brief_source_models import BriefSourceFailure, authority_selector
from pinboard.mcp import contracts
from pinboard.mcp import server as mcp_server
from tests import test_correction_source_review, test_dispatch
from tests.checkpoint_support import CheckpointPackageSupport
from tests.work_brief_support import CHECKPOINT_ID, ready_review


class McpJobsTest(CheckpointPackageSupport):
    def test_dispatch_and_review_are_installed_strict_tools(self) -> None:
        executor = mcp_server.BoundedExecutor(worker_count=1, unfinished_limit=1)
        server = mcp_server.create_server(
            executor, mcp_server.Diagnostics(io.StringIO(), event_limit=8, line_limit=256)
        )
        try:
            tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
            for name in ("pinboard_dispatch", "pinboard_review_job"):
                self.assertIn(name, tuple(tools))
                self.assertIsNotNone(tools[name].output_schema)
                self.assertEqual(tools[name].input_schema["additionalProperties"], False)
        finally:
            executor.shutdown()

    def dispatch_fixture(self) -> tuple[Path, Path, dict[str, contracts.JsonValue]]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        project = Path(temporary.name).resolve()
        fixture = test_dispatch.DispatchTest()
        _, roots, _, brief, action, environment = fixture.initialized(project)
        fixture.run_git(project, "init", "-q")
        choice = msgspec.to_builtins(
            {
                "kind": "reviewed",
                "receipt": {
                    "action_id": {"kind": "dispatch", "subject": "work-a-1"},
                    "subject_revision": action().capability.subject_revision,
                },
                "checkpoint_id": CHECKPOINT_ID,
                "environment": environment,
                "prompt": None,
                "brief_review": msgspec.json.decode(ready_review(brief)),
                "review_id": "ready-review",
            },
            enc_hook=test_dispatch.dispatch_environment_enc_hook,
        )
        assert isinstance(choice, dict)
        return project, roots.work_root, choice

    def test_dispatch_rejects_sibling_options_before_roots_or_store(self) -> None:
        project, work, choice = self.dispatch_fixture()
        invalid_changes: tuple[dict[str, contracts.JsonValue], ...] = (
            {"actor_task_id": "coordinator"},
            {"actor_host_id": "local"},
            {"correction_history_id": 1},
            {"environment": {"schema": "unknown"}},
            {"receipt": {"action_id": {"kind": "submit", "subject": "work-a-1"}, "subject_revision": 1}},
        )
        for change in invalid_changes:
            invalid = choice | change
            with self.subTest(invalid=invalid), patch.object(mcp_server, "resolve_source_checkout_root") as roots:
                outcome = mcp_server._dispatch_job(str(project), str(work), invalid, mcp_server.CancellationToken())
                self.assertEqual("DISPATCH_INVALID", outcome.content["code"])
                self.assertFalse(outcome.content["state_changed"])
                roots.assert_not_called()

    def test_dispatch_publication_reloads_and_repetition_is_unchanged(self) -> None:
        project, work, choice = self.dispatch_fixture()
        before = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()
        first = mcp_server._dispatch_job(str(project), str(work), choice, mcp_server.CancellationToken())
        self.assertEqual("ready", first.content["status"], first.content)
        self.assertEqual("committed", first.content["effect"])
        contracts.validate_result("pinboard_dispatch", first.content)
        after = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()
        self.assertEqual(before.lifecycle.work_items, after.lifecycle.work_items)
        self.assertEqual(before.lifecycle.attempts, after.lifecycle.attempts)
        self.assertEqual(before.authority, after.authority)
        reference = first.content["prompt_reference"]
        assert isinstance(reference, dict)
        self.assertTrue(
            any(
                int(value.artifact_ref_id) == reference["accepted_artifact_reference_id"]
                for value in after.artifact_references
            )
        )
        # Publication changes the project revision, not this operation's subject receipt.
        ordinary = {key: value for key, value in choice.items() if key not in {"brief_review", "review_id"}}
        ordinary["kind"] = "ordinary"
        second = mcp_server._dispatch_job(str(project), str(work), ordinary, mcp_server.CancellationToken())
        self.assertEqual("unchanged", second.content["effect"])
        self.assertEqual(reference["sha256"], self.json_object(second.content["prompt_reference"])["sha256"])

    def test_current_native_worker_launch_inputs_decode_exact_wrapped_leaves(self) -> None:
        project, work, choice = self.dispatch_fixture()
        outcome = mcp_server._dispatch_job(str(project), str(work), choice, mcp_server.CancellationToken())
        self.assertEqual("ready", outcome.content["status"])
        launch = self.json_object(outcome.content["native_launch"])
        message = launch["message"]
        assert isinstance(message, str)
        matched = re.search(
            r"call `pinboard_attempt_authority` with (.*?), then `pinboard_actions` with (.*?)\. ", message
        )
        assert matched is not None
        acquisition = json.loads(matched[1])
        acquisition["request"]["task_id"] = "native-worker"
        decoded = msgspec.convert(acquisition, type=contracts.AttemptAuthorityEnvelope, strict=True).request
        self.assertIsInstance(decoded, contracts.AttemptAuthorityAcquireRequest)
        self.assertEqual(str(project), decoded.project_root)
        self.assertEqual(str(work), decoded.work_root)
        continuation = json.loads(matched[2])
        continuation["request"]["lease_id"] = "returned-lease"
        continuation["request"]["generation"] = 1
        selected = msgspec.convert(continuation, type=contracts.ActionsEnvelope, strict=True).request
        self.assertIsInstance(selected, contracts.WorkerActionsRequest)
        self.assertEqual(str(project), selected.project_root)
        self.assertEqual(str(work), selected.work_root)

    def test_review_jobs_select_four_rounds_and_keep_mutable_review_digest_separate(self) -> None:
        fixture, package_id, correction_id = self.review_job_fixture()
        before = fixture.store.validated_snapshot()
        rounds: tuple[tuple[str, dict[str, contracts.JsonValue]], ...] = (
            ("initial", {}),
            ("package-initial", {"checkpoint_history_id": package_id}),
            ("correction", {"correction_history_id": correction_id}),
            ("package-correction", {"checkpoint_history_id": package_id, "correction_history_id": correction_id}),
        )
        for kind, ids in rounds:
            with self.subTest(kind=kind):
                outcome = mcp_server._review_job(
                    str(fixture.project),
                    str(fixture.work),
                    {"kind": kind, "attempt_id": "work-a-1", "candidate_revision": "b" * 40, **ids},
                    mcp_server.CancellationToken(),
                )
                self.assertEqual("ready", outcome.content["status"], outcome.content)
                contracts.validate_result("pinboard_review_job", outcome.content)
                round_ = self.json_object(outcome.content["review_round"])
                if "correction" in kind:
                    self.assertEqual("candidate-a", round_["candidate_revision"])
                    self.assertNotEqual(outcome.content["result_sha256"], round_["review_sha256"])
                after = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
                self.assertEqual(before.lifecycle.work_items, after.lifecycle.work_items)
                self.assertEqual(before.lifecycle.attempts, after.lifecycle.attempts)
                self.assertEqual(before.authority, after.authority)

    def test_review_sibling_fields_are_rejected_before_state_access(self) -> None:
        with patch.object(mcp_server, "compose_store") as store:
            outcome = mcp_server._review_job(
                "/not-a-repository",
                "/not-a-board",
                {"kind": "initial", "attempt_id": "work-a-1", "candidate_revision": "a", "correction_history_id": 1},
                mcp_server.CancellationToken(),
            )
            self.assertEqual("REVIEW_JOB_INVALID", outcome.content["code"])
            store.assert_not_called()

    def test_correction_dispatch_requires_current_return_and_fresh_independent_review(self) -> None:
        fixture = self.checkpoint_fixture()
        payload = fixture.project / "return.json"
        payload.write_text('{"reason":"Revalidate the changed authority."}', encoding="utf-8")
        receipt = self.transition_json(
            fixture, self.project_action(fixture.common, "return-for-correction:work-a-1"), payload
        )
        correction_history_id = receipt["history_id"]
        source = fixture.project / "architecture.md"
        source.write_text("# Architecture\n\n## Contract\n\nThe changed source remains exact.\n", encoding="utf-8")
        (fixture.project / "tests").mkdir()
        correction_history_id, actual_choice = (
            test_correction_source_review.CorrectionSourceReviewTest().submit_and_return(
                fixture, "actual-mcp-correction", committed=False
            )
        )
        checkpoint = fixture.brief.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        reviewed_authorities: list[work_brief_models.ReviewedAuthority] = []
        for authority in checkpoint.reviewed_authorities:
            selected = select_checkout_brief_source(fixture.project, authority_selector(authority.selector), True)
            assert not isinstance(selected, BriefSourceFailure)
            reviewed_authorities.append(
                msgspec.structs.replace(authority, reviewed_sha256=sha256(selected.content).hexdigest())
            )
        refreshed = msgspec.structs.replace(checkpoint, reviewed_authorities=tuple(reviewed_authorities))
        effective_brief = msgspec.structs.replace(fixture.brief, checkpoint=refreshed)
        self.assertEqual(
            sha256(work_briefs.canonical_checkpoint_bytes(effective_brief.checkpoint)).hexdigest(),
            self.json_object(self.json_object(actual_choice["brief_review"])["contract_review"])["checkpoint_sha256"],
        )
        action = self.project_action(fixture.common, "dispatch:work-a-1")
        environment = msgspec.structs.replace(
            test_dispatch.DispatchTest().environment(fixture.project), starting_revision=fixture.brief.base_revision
        )
        choice = msgspec.to_builtins(
            {
                "kind": "correction",
                "receipt": {
                    "action_id": {"kind": "dispatch", "subject": "work-a-1"},
                    "subject_revision": action["subject_revision"],
                },
                "checkpoint_id": CHECKPOINT_ID,
                "environment": environment,
                "prompt": None,
                "brief_review": actual_choice["brief_review"],
                "review_id": "correction-review",
                "correction_history_id": correction_history_id,
            },
            enc_hook=test_dispatch.dispatch_environment_enc_hook,
        )
        assert isinstance(choice, dict)
        stale_review = self.json_object(deepcopy(choice["brief_review"]))
        stale_review["contract_review"] = msgspec.json.decode(ready_review(fixture.brief))
        stale_choice = choice | {"brief_review": stale_review}
        before = fixture.store.validated_snapshot()
        stale = mcp_server._dispatch_job(
            str(fixture.project), str(fixture.work), stale_choice, mcp_server.CancellationToken()
        )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", stale.content["code"])
        self.assertEqual(before, fixture.store.validated_snapshot())
        ready = mcp_server._dispatch_job(
            str(fixture.project), str(fixture.work), choice, mcp_server.CancellationToken()
        )
        self.assertEqual("ready", ready.content["status"], ready.content)
        contracts.validate_result("pinboard_dispatch", ready.content)

    def test_dispatch_output_rejects_false_effect_claims(self) -> None:
        project, work, choice = self.dispatch_fixture()
        outcome = mcp_server._dispatch_job(str(project), str(work), choice, mcp_server.CancellationToken())
        self.assertEqual("ready", outcome.content["status"], outcome.content)
        changes: tuple[dict[str, contracts.JsonValue], ...] = (
            {"state_changed": False},
            {"effect": "unchanged"},
            {"retry": "safe-to-repeat"},
            {"changed_surfaces": ["ledger"]},
        )
        for changed in changes:
            with self.subTest(changed=changed), self.assertRaises(msgspec.ValidationError):
                contracts.validate_result("pinboard_dispatch", deepcopy(outcome.content) | changed)

    def test_dispatch_failure_preserves_ready_review_publication(self) -> None:
        project, work, choice = self.dispatch_fixture()
        outcome = mcp_server._dispatch_job(
            str(project), str(work), choice | {"prompt": "not canonical"}, mcp_server.CancellationToken()
        )
        self.assertEqual("failed-after-publication", outcome.content["status"])
        self.assertEqual("DISPATCH_PROMPT_NOT_CANONICAL", outcome.content["code"])
        self.assertEqual("committed", outcome.content["effect"])
        self.assertEqual("do-not-retry", outcome.content["retry"])
        self.assertEqual(
            ["immutable-artifact", "accepted-artifact-reference", "ledger"], outcome.content["changed_surfaces"]
        )
        contracts.validate_result("pinboard_dispatch", outcome.content)
        review = msgspec.convert(choice["brief_review"], type=work_brief_models.WorkBriefReview, strict=True)
        review_sha256 = sha256(work_briefs.canonical_work_brief_review_bytes(review)).hexdigest()
        self.assertTrue(
            any(
                value.content_sha256 == review_sha256
                for value in SQLiteWorkStore(work / "state.sqlite3").validated_snapshot().artifact_references
            )
        )

    def test_entered_dispatch_finishes_publication_after_cancellation_and_response_loss(self) -> None:
        project, work, choice = self.dispatch_fixture()
        entered, release = threading.Event(), threading.Event()
        real_prepare = dispatch_operations.prepare_dispatch

        def block_at_publication[**Parameters, Result](
            operation: Callable[Parameters, Result],
        ) -> Callable[Parameters, Result]:
            def entering(*arguments: Parameters.args, **keywords: Parameters.kwargs) -> Result:
                entered.set()
                if not release.wait(2):
                    raise AssertionError("Publication was not released.")
                return operation(*arguments, **keywords)

            return entering

        async def scenario() -> None:
            executor = mcp_server.BoundedExecutor(worker_count=1, unfinished_limit=1)
            try:
                execution = executor.submit(
                    lambda token: mcp_server._dispatch_job(str(project), str(work), choice, token)
                )
                waiter = asyncio.create_task(execution.result())
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                waiter.cancel()
                await asyncio.sleep(0)
                release.set()
                outcome = await waiter
                self.assertEqual("ready", outcome.content["status"], outcome.content)
                self.assertEqual("do-not-retry", outcome.content["retry"])
                reference = self.json_object(outcome.content["prompt_reference"])
                recovered = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()
                self.assertTrue(
                    any(
                        int(value.artifact_ref_id) == reference["accepted_artifact_reference_id"]
                        for value in recovered.artifact_references
                    )
                )
            finally:
                release.set()
                executor.shutdown()

        with patch.object(dispatch_operations, "prepare_dispatch", side_effect=block_at_publication(real_prepare)):
            asyncio.run(scenario())

    def test_stdio_dispatch_and_historical_review_publish_verifiable_prompts(self) -> None:
        project, work, choice = self.dispatch_fixture()
        fixture, package_id, correction_id = self.review_job_fixture()

        async def scenario() -> None:
            parameters = StdioServerParameters(command=sys.executable, args=("-m", "pinboard.mcp"))
            async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                await session.initialize()
                for tool, roots, leaf in (
                    ("pinboard_dispatch", {"project_root": str(project), "work_root": str(work)}, {"dispatch": choice}),
                    (
                        "pinboard_review_job",
                        {"project_root": str(fixture.project), "work_root": str(fixture.work)},
                        {
                            "review": {
                                "kind": "package-correction",
                                "attempt_id": "work-a-1",
                                "candidate_revision": "b" * 40,
                                "checkpoint_history_id": package_id,
                                "correction_history_id": correction_id,
                            }
                        },
                    ),
                ):
                    result = await session.call_tool(tool, roots | leaf)
                    self.assertFalse(result.is_error, result.content)
                    assert isinstance(result.structured_content, dict)
                    self.assertEqual("ready", result.structured_content["status"], result.structured_content)
                    reference = result.structured_content["prompt_reference"]
                    assert isinstance(reference, dict)
                    verified = await session.call_tool(
                        "pinboard_artifact_verify",
                        roots
                        | {
                            "artifact_ref_id": reference["accepted_artifact_reference_id"],
                            "selector": reference["selector"],
                            "sha256": reference["sha256"],
                            "size_bytes": reference["size_bytes"],
                        },
                    )
                    self.assertFalse(verified.is_error)
                    assert isinstance(verified.structured_content, dict)
                    self.assertTrue(verified.structured_content["verified"])

        asyncio.run(scenario())

    def test_stdio_lost_dispatch_reply_recovers_accepted_prompt_without_replay(self) -> None:
        project, work, choice = self.dispatch_fixture()
        before = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()
        roots: dict[str, contracts.JsonValue] = {"project_root": str(project), "work_root": str(work)}

        async def scenario() -> None:
            parameters = StdioServerParameters(command=sys.executable, args=("-m", "pinboard.mcp"))
            async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                await session.initialize()
                # Deliberately drop the completed reply at the caller boundary.
                # Recovery below receives no returned prompt identity or effect claim.
                await session.call_tool("pinboard_dispatch", roots | {"dispatch": choice})
            recovered = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()
            self.assertEqual(before.lifecycle.work_items, recovered.lifecycle.work_items)
            self.assertEqual(before.lifecycle.attempts, recovered.lifecycle.attempts)
            self.assertEqual(before.authority, recovered.authority)
            before_ids = {value.artifact_ref_id for value in before.artifact_references}
            prompts = tuple(
                value
                for value in recovered.artifact_references
                if value.artifact_ref_id not in before_ids and value.selector.endswith(".txt")
            )
            self.assertEqual(1, len(prompts))
            reference = prompts[0]
            async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                await session.initialize()
                verified = await session.call_tool(
                    "pinboard_artifact_verify",
                    roots
                    | {
                        "artifact_ref_id": int(reference.artifact_ref_id),
                        "selector": reference.selector,
                        "sha256": reference.content_sha256,
                        "size_bytes": reference.size_bytes,
                    },
                )
                self.assertFalse(verified.is_error)
                assert isinstance(verified.structured_content, dict)
                self.assertTrue(verified.structured_content["verified"])
            self.assertEqual(recovered, SQLiteWorkStore(work / "state.sqlite3").validated_snapshot())

        asyncio.run(scenario())
