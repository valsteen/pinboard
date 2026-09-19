import asyncio
import contextlib
import io
import json
import os
import re
import sqlite3
import subprocess
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
from mcp_types import CallToolResult

from pinboard.adapters import candidate_evidence, dispatch_operations
from pinboard.adapters.files import root
from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.brief_sources import select_checkout_brief_source
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import (
    candidate_snapshot_compatibility_models,
    candidate_snapshots,
    stored_state,
    work_brief_models,
    work_briefs,
)
from pinboard.application.artifacts import NewArtifact
from pinboard.application.brief_source_models import BriefSourceFailure, authority_selector
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import common as mcp_common
from pinboard.mcp import contract_schemas, contracts
from pinboard.mcp import execution as mcp_execution
from pinboard.mcp import job_operations as mcp_jobs
from pinboard.mcp import read_operations as mcp_reads
from pinboard.mcp import server as mcp_server
from tests import test_correction_source_review, test_dispatch
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.work_brief_support import CHECKPOINT_ID, ready_review


class McpJobsTest(CheckpointPackageSupport):
    def test_authority_tools_describe_temporary_local_mutation_to_clients(self) -> None:
        executor = mcp_execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        transport = mcp_server.create_server(
            executor, mcp_execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256)
        )
        for name in (mcp_server.PREPARATION_AUTHORITY_TOOL, mcp_server.ATTEMPT_AUTHORITY_TOOL):
            with self.subTest(name=name):
                tool = transport._tool_manager.get_tool(name)
                assert tool is not None and tool.annotations is not None
                self.assertFalse(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.destructive_hint)
                self.assertFalse(tool.annotations.idempotent_hint)
                self.assertFalse(tool.annotations.open_world_hint)

    def test_candidate_observation_rejects_invalid_input_context_and_result_claims(self) -> None:
        with patch.object(mcp_jobs, "resolve_source_checkout_root", side_effect=AssertionError("Must not resolve")):
            invalid = mcp_jobs._observe_candidate("", "/work", "work-a-1", mcp_execution.CancellationToken())
            self.assertEqual("CANDIDATE_OBSERVATION_INVALID", invalid.content["code"])
        fixture = self.checkpoint_fixture()
        for project, attempt, code in (
            (str(fixture.project), "missing", "CANDIDATE_CONTEXT_UNAVAILABLE"),
            ("/nonexistent/pinboard-observation-checkout", "work-a-1", "CANDIDATE_GIT_UNAVAILABLE"),
        ):
            with self.subTest(code=code):
                result = mcp_jobs._observe_candidate(
                    project, str(fixture.work), attempt, mcp_execution.CancellationToken()
                )
                contract_schemas.validate_result("pinboard_candidate_observe", result.content)
                self.assertEqual(code, result.content["code"])
                self.assertEqual([], result.content["changed_surfaces"])
        observed = mcp_jobs._observe_candidate(
            str(fixture.project), str(fixture.work), "work-a-1", mcp_execution.CancellationToken()
        )
        invalid_claims: tuple[dict[str, contracts.JsonValue], ...] = (
            {"state_changed": True},
            {"changed_surfaces": ["ledger"]},
            {"candidate": "invented"},
            {"accepted": True},
        )
        for override in invalid_claims:
            with self.subTest(override=override), self.assertRaises(msgspec.ValidationError):
                contract_schemas.validate_result("pinboard_candidate_observe", observed.content | override)
        for override in ({"operation": "commit"}, {"lease_id": "borrowed"}, {"attempt_id": "../other"}):
            with self.subTest(override=override), self.assertRaises(msgspec.ValidationError):
                msgspec.convert(
                    {"project_root": str(fixture.project), "work_root": str(fixture.work), "attempt_id": "work-a-1"}
                    | override,
                    type=contracts.CandidateObserveRequest,
                    strict=True,
                )

    def test_native_candidate_observation_reports_omissions_without_effects_and_submits_exact_state(self) -> None:  # noqa: PLR0915 - one negotiated observation and persisted submission journey
        fixture = self.checkpoint_fixture(candidate_form="current-head")
        returned = self.transition_result(
            fixture, self.project_action(fixture, "return-for-correction:work-a-1"), {"reason": "New candidate."}
        )
        self.assertEqual("committed", returned["status"])
        (fixture.project / "GREETING.md").write_text("Hello\n", encoding="utf-8")
        (fixture.project / "unrelated-note.md").write_text("Private note\n", encoding="utf-8")
        (fixture.project / ".git" / "info" / "exclude").write_text("/.codex/pinboard/\nignored\n", encoding="utf-8")
        (fixture.project / "ignored").write_text("Ignored\n", encoding="utf-8")
        roots = {"project_root": str(fixture.project), "work_root": str(fixture.work), "attempt_id": "work-a-1"}
        executor = mcp_execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        server = mcp_server.create_server(
            executor, mcp_execution.Diagnostics(io.StringIO(), event_limit=8, line_limit=256)
        )

        def observe() -> dict[str, contracts.JsonValue]:
            result = asyncio.run(server.call_tool("pinboard_candidate_observe", roots))
            assert isinstance(result, CallToolResult)
            assert isinstance(result.structured_content, dict)
            return result.structured_content

        async def negotiated_observe() -> dict[str, contracts.JsonValue]:
            parameters = StdioServerParameters(command=sys.executable, args=["-m", "pinboard.mcp"])
            async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                await session.initialize()
                tools = await session.list_tools()
                self.assertIn("pinboard_candidate_observe", {tool.name for tool in tools.tools})
                result = await session.call_tool("pinboard_candidate_observe", roots)
                assert isinstance(result.structured_content, dict)
                return result.structured_content

        before = fixture.store.validated_snapshot()
        index_before = (fixture.project / ".git" / "index").read_bytes()
        os.utime(fixture.project / "tracked.txt", ns=(1_500_000_000_000_000_000, 1_500_000_000_000_000_000))
        artifacts_before = tuple(sorted((fixture.work / "artifacts").rglob("*")))
        accepted_bytes_before = tuple(
            (reference.selector, (fixture.work / reference.selector).read_bytes())
            for reference in before.artifact_references
        )
        source_bytes_before = tuple(
            (name, (fixture.project / name).read_bytes())
            for name in ("GREETING.md", "unrelated-note.md", "tracked.txt")
        )
        first = asyncio.run(negotiated_observe())
        self.assertEqual("observed", first["status"], first)
        self.assertEqual(["GREETING.md", "unrelated-note.md"], first["omitted_untracked_paths"])
        self.assertEqual(root.read_working_tree_candidate(fixture.project).identity, first["candidate"])
        self.assertEqual(before, fixture.store.validated_snapshot())
        self.assertEqual(index_before, (fixture.project / ".git" / "index").read_bytes())
        self.assertEqual(artifacts_before, tuple(sorted((fixture.work / "artifacts").rglob("*"))))
        for selector, contents in accepted_bytes_before:
            self.assertEqual(contents, (fixture.work / selector).read_bytes())
        for name, contents in source_bytes_before:
            self.assertEqual(contents, (fixture.project / name).read_bytes())
        subprocess.run(["git", "add", "--intent-to-add", "--", "GREETING.md"], cwd=fixture.project, check=True)
        prepared = observe()
        self.assertNotEqual(first["candidate"], prepared["candidate"])
        self.assertEqual(["unrelated-note.md"], prepared["omitted_untracked_paths"])
        self.assertIn(b"GREETING.md", root.read_working_tree_candidate(fixture.project).diff)
        lease = self.native_attempt_acquire(fixture, "native-observer-worker")
        selected = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        before_stale_submission = fixture.store.validated_snapshot()
        (fixture.project / "GREETING.md").write_text("Changed after observation\n", encoding="utf-8")
        stale = self.transition_result(fixture, selected, {"candidate": prepared["candidate"]})
        self.assertEqual("rejected", stale["status"], stale)
        self.assertEqual([], stale["changed_surfaces"])
        self.assertEqual(before_stale_submission, fixture.store.validated_snapshot())
        (fixture.project / "GREETING.md").write_text("Hello\n", encoding="utf-8")
        selected = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        self.assertEqual(
            "committed", self.transition_result(fixture, selected, {"candidate": prepared["candidate"]})["status"]
        )
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3")
        context = fresh.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        snapshot = candidate_snapshots.decode_candidate_snapshot(
            (fixture.work / context.reference.selector).read_bytes()
        )
        self.assertEqual(prepared["candidate"], snapshot.candidate)
        self.assertEqual(prepared["preimage_revision"], snapshot.preimage_revision)
        self.assertIn(b"GREETING.md", snapshot.diff)
        self.assertNotIn(b"unrelated-note.md", snapshot.diff)
        subprocess.run(["git", "switch", "-c", "wrong-branch"], cwd=fixture.project, check=True, capture_output=True)
        rejected = observe()
        self.assertEqual("CANDIDATE_BRANCH_MISMATCH", rejected["code"])
        self.assertFalse(rejected["state_changed"])
        self.assertEqual("unchanged", rejected["effect"])

    def test_dispatch_and_review_are_installed_strict_tools(self) -> None:
        executor = mcp_execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        server = mcp_server.create_server(
            executor, mcp_execution.Diagnostics(io.StringIO(), event_limit=8, line_limit=256)
        )
        try:
            tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
            self.assertEqual(20, len(tools))
            for name in (
                "pinboard_dispatch",
                "pinboard_review_job",
                "pinboard_candidate_restore",
                "pinboard_candidate_observe",
            ):
                self.assertIn(name, tuple(tools))
                self.assertIsNotNone(tools[name].output_schema)
                self.assertEqual(tools[name].input_schema["additionalProperties"], False)
        finally:
            executor.shutdown()

    def test_native_restore_executes_inspected_snapshot_and_preserves_board_on_real_git(self) -> None:  # noqa: PLR0915 - complete native persisted-snapshot journey
        for form in ("working-tree", "current-head", "retained-working-tree"):
            with self.subTest(form=form):
                fixture = self.checkpoint_fixture(
                    candidate_form="current-head" if form == "current-head" else "working-tree"
                )
                first_inspection = mcp_reads._read_attempt_inspection(
                    str(fixture.project), str(fixture.work), "work-a-1", mcp_execution.CancellationToken()
                )
                previously_inspected_candidate = str(
                    self.json_object(first_inspection.content["candidate_recovery"])["candidate"]
                )
                payload = fixture.work / "real-submission.json"
                payload.write_text('{"reason":"Exercise fresh actual submission."}', encoding="utf-8")
                self.transition_json(fixture, self.project_action(fixture, "return-for-correction:work-a-1"), payload)
                (fixture.project / "tracked.txt").write_text("fresh candidate\n", encoding="utf-8")
                candidate = (
                    self.commit_all(fixture.project, "fresh candidate")
                    if form == "current-head"
                    else root.read_working_tree_candidate(fixture.project).identity
                )
                lease = self.native_attempt_acquire(fixture, "native-restore")
                selected = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
                payload.write_text(json.dumps({"candidate": candidate}), encoding="utf-8")
                self.transition_json(fixture, selected, payload)
                if form == "retained-working-tree":
                    store = SQLiteWorkStore(fixture.work / "state.sqlite3")
                    context = store.read_candidate_snapshot_context(AttemptId("work-a-1"))
                    assert context is not None
                    current = candidate_snapshots.decode_candidate_snapshot(
                        (fixture.work / context.reference.selector).read_bytes()
                    )
                    candidate = "working-tree-sha256:" + sha256(current.diff).hexdigest()
                    retained = candidate_snapshot_compatibility_models.WorkingTreeCandidateSnapshot(
                        "pinboard-candidate-snapshot/v1",
                        current.attempt_id,
                        current.item_id,
                        candidate,
                        current.branch,
                        current.preimage_revision,
                        current.accepted_base_revision,
                        current.recorded_at,
                        current.diff,
                    )
                    repository = ArtifactRepository(resolve_durable_roots(fixture.project, fixture.work))
                    published = repository.publish(
                        NewArtifact(
                            work_models.ArtifactKind.EVIDENCE,
                            candidate_snapshots.candidate_snapshot_key(retained),
                            1,
                            ".json",
                            candidate_snapshots.canonical_candidate_snapshot_bytes(retained),
                        )
                    )
                    accepted = store.accept_artifact_reference(
                        fixture.work, published.reference, context.receipt.committed_at
                    )
                    assert not isinstance(accepted, DecisionFailure)
                    with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
                        connection.execute(
                            "DELETE FROM artifact_refs WHERE artifact_ref_id = ?",
                            (int(context.reference.artifact_ref_id),),
                        )
                        connection.execute(
                            "UPDATE artifact_refs SET accepted_revision = ? WHERE artifact_ref_id = ?",
                            (context.reference.accepted_revision, int(accepted.reference.artifact_ref_id)),
                        )
                        connection.execute(
                            "UPDATE attempts SET candidate_revision = ? WHERE attempt_id = 'work-a-1'", (candidate,)
                        )
                        connection.execute(
                            "UPDATE transition_history SET artifact_ref_id = ?, input_schema = ?, "
                            "input_json = json_set(input_json, '$.candidate', ?, '$.snapshot_artifact_ref_id', ?), "
                            "outcome_json = json_set(outcome_json, '$.candidate', ?) WHERE history_id = ?",
                            (
                                int(accepted.reference.artifact_ref_id),
                                retained.schema,
                                candidate,
                                int(accepted.reference.artifact_ref_id),
                                candidate,
                                int(context.receipt.history_id),
                            ),
                        )
                    (fixture.work / context.reference.selector).unlink()
                    self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])
                inspected = mcp_reads._read_attempt_inspection(
                    str(fixture.project), str(fixture.work), "work-a-1", mcp_execution.CancellationToken()
                )
                recovery = self.json_object(inspected.content["candidate_recovery"])
                invocation = self.json_object(recovery["restore"])
                arguments = self.json_object(invocation["arguments"])
                self.assertIsNone(arguments["project_root"])
                before = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
                evidence = candidate_evidence.read_candidate_evidence(
                    fixture.work, SQLiteWorkStore(fixture.work / "state.sqlite3"), AttemptId("work-a-1"), candidate
                )
                self.assertNotIsInstance(evidence, DecisionFailure)
                assert not isinstance(evidence, DecisionFailure)
                target_directory = tempfile.TemporaryDirectory()
                self.addCleanup(target_directory.cleanup)
                target = Path(target_directory.name).resolve() / "checkout"
                clone = Path(target_directory.name).resolve() / "repository" if form == "current-head" else target
                subprocess.run(
                    ["git", "clone", "-q", str(fixture.project), str(clone)], check=True, capture_output=True
                )
                if form == "current-head":
                    subprocess.run(["git", "switch", "--detach"], cwd=clone, check=True, capture_output=True)
                    subprocess.run(
                        ["git", "worktree", "add", "-q", str(target), fixture.brief.branch],
                        cwd=clone,
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(
                        ["git", "switch", "-C", fixture.brief.branch, evidence.snapshot.preimage_revision],
                        cwd=target,
                        check=True,
                        capture_output=True,
                    )
                self.run_json_cli("--project-root", str(target), "init")
                other_store = SQLiteWorkStore(clone / ".codex" / "pinboard" / "state.sqlite3")
                other_before = other_store.validated_snapshot()
                arguments["project_root"] = str(target)

                async def scenario(
                    fixture: CheckpointFixture,
                    invocation: dict[str, contracts.JsonValue],
                    arguments: dict[str, contracts.JsonValue],
                    before: stored_state.StoredWorkState,
                    previously_inspected_candidate: str,
                ) -> None:
                    parameters = StdioServerParameters(command=sys.executable, args=("-m", "pinboard.mcp"))
                    async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                        await session.initialize()
                        stale = await session.call_tool(
                            str(invocation["tool"]), arguments | {"candidate": previously_inspected_candidate}
                        )
                        self.assertFalse(stale.is_error, stale.content)
                        assert isinstance(stale.structured_content, dict)
                        self.assertFalse(stale.structured_content["state_changed"])
                        for changed in (True, False):
                            result = await session.call_tool(str(invocation["tool"]), arguments)
                            self.assertFalse(result.is_error, result.content)
                            assert isinstance(result.structured_content, dict)
                            self.assertEqual("restored", result.structured_content["status"])
                            self.assertEqual(changed, result.structured_content["state_changed"])
                            self.assertEqual(
                                ["source-checkout"] if changed else [], result.structured_content["changed_surfaces"]
                            )
                    self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

                asyncio.run(scenario(fixture, invocation, arguments, before, previously_inspected_candidate))
                self.assertEqual(other_before, other_store.validated_snapshot())
                self.assertEqual("fresh candidate\n", (target / "tracked.txt").read_text())
                if form in ("working-tree", "retained-working-tree"):
                    self.assertIn(
                        "M  tracked.txt",
                        subprocess.run(
                            ["git", "status", "--short"], cwd=target, check=True, capture_output=True, text=True
                        ).stdout,
                    )
                else:
                    self.assertEqual(candidate, root.observe_checkout_identity(target)[1])

    def test_restore_strict_ingress_terminal_effects_and_entered_cancellation(self) -> None:
        for invalid in (
            {"project_root": "", "work_root": "/board", "attempt_id": "a", "candidate": "c"},
            {"project_root": "/repo", "work_root": "/board", "attempt_id": "../a", "candidate": "c"},
            {"project_root": "/repo", "work_root": "/board", "attempt_id": "a", "candidate": ""},
        ):
            with self.subTest(invalid=invalid), patch.object(mcp_jobs, "resolve_source_checkout_root") as roots:
                outcome = mcp_jobs._candidate_restore(**invalid, token=mcp_execution.CancellationToken())
                self.assertEqual("CANDIDATE_RESTORE_INVALID", outcome.content["code"])
                roots.assert_not_called()
        fixture = self.checkpoint_fixture()
        entered, release = threading.Event(), threading.Event()

        def mutation(*_args: object, **_kwargs: object) -> root.CandidateRestoreSuccess:
            entered.set()
            if not release.wait(2):
                raise AssertionError("Restore was not released")
            raise root.CandidateRestoreAfterMutationError(
                root.RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE, "changed"
            )

        async def scenario() -> None:
            executor = mcp_execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
            try:
                execution = executor.submit(
                    lambda token: mcp_jobs._candidate_restore(
                        str(fixture.project), str(fixture.work), "work-a-1", fixture.candidate_revision, token
                    )
                )
                waiter = asyncio.create_task(execution.result())
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                waiter.cancel()
                await asyncio.sleep(0)
                release.set()
                outcome = await waiter
                self.assertEqual("failed-after-mutation", outcome.content["status"])
                self.assertEqual(["source-checkout"], outcome.content["changed_surfaces"])
                self.assertEqual("do-not-retry", outcome.content["retry"])
                contract_schemas.validate_result("pinboard_candidate_restore", outcome.content)
            finally:
                release.set()
                executor.shutdown()

        with patch.object(candidate_evidence.root, "restore_working_tree_candidate", side_effect=mutation):
            asyncio.run(scenario())

    def test_restore_cancellation_before_entry_and_exact_effect_correlation(self) -> None:
        fixture = self.checkpoint_fixture()
        for checkpoint in ("before-roots", "after-store"):
            token = mcp_execution.CancellationToken()
            if checkpoint == "before-roots":
                token.cancel()

            def store_then_cancel(_roots: object) -> SQLiteWorkStore:
                token.cancel()  # noqa: B023 - invoked synchronously within this subtest
                return SQLiteWorkStore(fixture.work / "state.sqlite3")

            with (
                self.subTest(checkpoint=checkpoint),
                patch.object(mcp_common, "compose_store", side_effect=store_then_cancel),
                patch.object(candidate_evidence, "restore_candidate") as restore,
                self.assertRaises(mcp_execution.OperationCancelled),
            ):
                mcp_jobs._candidate_restore(
                    str(fixture.project), str(fixture.work), "work-a-1", fixture.candidate_revision, token
                )
            restore.assert_not_called()
        valid: dict[str, contracts.JsonValue] = {
            "schema": "pinboard-mcp-candidate-restore-result/v1",
            "status": "restored",
            "attempt_id": "work-a-1",
            "candidate": fixture.candidate_revision,
            "source_checkout": str(fixture.project),
            "state_changed": False,
            "effect": "unchanged",
            "retry": "safe-to-repeat",
            "changed_surfaces": [],
        }
        contract_schemas.validate_result("pinboard_candidate_restore", valid)
        changes: tuple[dict[str, contracts.JsonValue], ...] = (
            {"state_changed": True},
            {"effect": "committed"},
            {"retry": "do-not-retry"},
            {"changed_surfaces": ["source-checkout"]},
            {"changed_surfaces": ["ledger"]},
            {"unexpected": True},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(msgspec.ValidationError):
                contract_schemas.validate_result("pinboard_candidate_restore", valid | change)

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
            with self.subTest(invalid=invalid), patch.object(mcp_jobs, "resolve_source_checkout_root") as roots:
                outcome = mcp_jobs._dispatch_job(str(project), str(work), invalid, mcp_execution.CancellationToken())
                self.assertEqual("DISPATCH_INVALID", outcome.content["code"])
                self.assertFalse(outcome.content["state_changed"])
                roots.assert_not_called()

    def test_dispatch_publication_reloads_and_repetition_is_unchanged(self) -> None:
        project, work, choice = self.dispatch_fixture()
        before = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()
        first = mcp_jobs._dispatch_job(str(project), str(work), choice, mcp_execution.CancellationToken())
        self.assertEqual("ready", first.content["status"], first.content)
        self.assertEqual("committed", first.content["effect"])
        contract_schemas.validate_result("pinboard_dispatch", first.content)
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
        second = mcp_jobs._dispatch_job(str(project), str(work), ordinary, mcp_execution.CancellationToken())
        self.assertEqual("unchanged", second.content["effect"])
        self.assertEqual(reference["sha256"], self.json_object(second.content["prompt_reference"])["sha256"])

    def test_current_native_worker_launch_inputs_decode_exact_wrapped_leaves(self) -> None:
        project, work, choice = self.dispatch_fixture()
        outcome = mcp_jobs._dispatch_job(str(project), str(work), choice, mcp_execution.CancellationToken())
        self.assertEqual("ready", outcome.content["status"])
        launch = self.json_object(outcome.content["native_launch"])
        self.assertEqual("pinboard-native-agent-launch/v2", launch["schema"])
        self.assertEqual("codex", launch["runtime"])
        self.assertEqual("spawn_agent", launch["tool"])
        self.assertFalse(launch["background"])
        arguments = self.json_object(launch["arguments"])
        self.assertEqual({"task_name", "message", "fork_turns"}, set(arguments))
        self.assertEqual("none", arguments["fork_turns"])
        self.assertNotIn("isolation", arguments)
        message = arguments["message"]
        assert isinstance(message, str)
        reference = self.json_object(outcome.content["prompt_reference"])
        published_prompt = (work / str(reference["selector"])).read_text(encoding="utf-8")
        self.assertIn("BEGIN ACCEPTED PINBOARD TASK", message)
        self.assertIn(published_prompt, message)
        self.assertNotIn("follow those exact bytes", message)
        self.assertIn("Do not return successful delivery before", message)
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

    def test_claude_worker_launch_is_complete_and_cannot_request_another_checkout(self) -> None:
        project, work, choice = self.dispatch_fixture()
        environment = self.json_object(choice["environment"])
        choice["environment"] = environment | {"runtime": "claude-code", "background": False}
        outcome = mcp_jobs._dispatch_job(str(project), str(work), choice, mcp_execution.CancellationToken())
        self.assertEqual("ready", outcome.content["status"])
        launch = self.json_object(outcome.content["native_launch"])
        self.assertEqual("pinboard-native-agent-launch/v2", launch["schema"])
        self.assertEqual("claude-code", launch["runtime"])
        self.assertEqual("Agent", launch["tool"])
        self.assertFalse(launch["background"])
        arguments = self.json_object(launch["arguments"])
        self.assertEqual({"description", "prompt", "run_in_background"}, set(arguments))
        self.assertFalse(arguments["run_in_background"])
        self.assertNotIn("isolation", arguments)
        prompt = arguments["prompt"]
        assert isinstance(prompt, str)
        reference = self.json_object(outcome.content["prompt_reference"])
        published_prompt = (work / str(reference["selector"])).read_text(encoding="utf-8")
        self.assertIn("BEGIN ACCEPTED PINBOARD TASK", prompt)
        self.assertIn(published_prompt, prompt)
        self.assertNotIn("follow those exact bytes", prompt)
        self.assertIn("Do not return successful delivery before", prompt)

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
                outcome = mcp_jobs._review_job(
                    str(fixture.project),
                    str(fixture.work),
                    {
                        "kind": kind,
                        "attempt_id": "work-a-1",
                        "candidate_revision": "b" * 40,
                        "runtime": "claude-code",
                        "background": False,
                        **ids,
                    },
                    mcp_execution.CancellationToken(),
                )
                self.assertEqual("ready", outcome.content["status"], outcome.content)
                contract_schemas.validate_result("pinboard_review_job", outcome.content)
                launch = self.json_object(outcome.content["native_launch"])
                self.assertEqual("pinboard-native-agent-launch/v2", launch["schema"])
                self.assertEqual("claude-code", launch["runtime"])
                self.assertEqual("Agent", launch["tool"])
                self.assertFalse(launch["background"])
                arguments = self.json_object(launch["arguments"])
                self.assertEqual({"description", "prompt", "run_in_background"}, set(arguments))
                self.assertFalse(arguments["run_in_background"])
                self.assertNotIn("isolation", arguments)
                prompt = arguments["prompt"]
                assert isinstance(prompt, str)
                reference = self.json_object(outcome.content["prompt_reference"])
                published_prompt = (fixture.work / str(reference["selector"])).read_text(encoding="utf-8")
                self.assertIn("BEGIN ACCEPTED PINBOARD TASK", prompt)
                self.assertIn(published_prompt, prompt)
                self.assertIn(work_briefs.canonical_work_brief_bytes(fixture.brief).decode(), published_prompt)
                self.assertNotIn("follow those exact bytes", prompt)
                round_ = self.json_object(outcome.content["review_round"])
                if "correction" in kind:
                    self.assertEqual("candidate-a", round_["candidate_revision"])
                    self.assertNotEqual(outcome.content["result_sha256"], round_["review_sha256"])
                after = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
                self.assertEqual(before.lifecycle.work_items, after.lifecycle.work_items)
                self.assertEqual(before.lifecycle.attempts, after.lifecycle.attempts)
                self.assertEqual(before.authority, after.authority)

    def test_review_sibling_fields_are_rejected_before_state_access(self) -> None:
        with patch.object(mcp_common, "compose_store") as store:
            outcome = mcp_jobs._review_job(
                "/not-a-repository",
                "/not-a-board",
                {
                    "kind": "initial",
                    "attempt_id": "work-a-1",
                    "candidate_revision": "a",
                    "runtime": "codex",
                    "background": False,
                    "correction_history_id": 1,
                },
                mcp_execution.CancellationToken(),
            )
            self.assertEqual("REVIEW_JOB_INVALID", outcome.content["code"])
            store.assert_not_called()

    def test_correction_dispatch_requires_current_return_and_fresh_independent_review(self) -> None:
        fixture = self.checkpoint_fixture()
        payload = fixture.project / "return.json"
        payload.write_text('{"reason":"Revalidate the changed authority."}', encoding="utf-8")
        receipt = self.transition_json(fixture, self.project_action(fixture, "return-for-correction:work-a-1"), payload)
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
        action = self.project_action(fixture, "dispatch:work-a-1")
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
        stale = mcp_jobs._dispatch_job(
            str(fixture.project), str(fixture.work), stale_choice, mcp_execution.CancellationToken()
        )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", stale.content["code"])
        self.assertEqual(before, fixture.store.validated_snapshot())
        ready = mcp_jobs._dispatch_job(
            str(fixture.project), str(fixture.work), choice, mcp_execution.CancellationToken()
        )
        self.assertEqual("ready", ready.content["status"], ready.content)
        contract_schemas.validate_result("pinboard_dispatch", ready.content)

    def test_dispatch_output_rejects_false_effect_claims(self) -> None:
        project, work, choice = self.dispatch_fixture()
        outcome = mcp_jobs._dispatch_job(str(project), str(work), choice, mcp_execution.CancellationToken())
        self.assertEqual("ready", outcome.content["status"], outcome.content)
        changes: tuple[dict[str, contracts.JsonValue], ...] = (
            {"state_changed": False},
            {"effect": "unchanged"},
            {"retry": "safe-to-repeat"},
            {"changed_surfaces": ["ledger"]},
        )
        for changed in changes:
            with self.subTest(changed=changed), self.assertRaises(msgspec.ValidationError):
                contract_schemas.validate_result("pinboard_dispatch", deepcopy(outcome.content) | changed)

    def test_dispatch_failure_preserves_ready_review_publication(self) -> None:
        project, work, choice = self.dispatch_fixture()
        outcome = mcp_jobs._dispatch_job(
            str(project), str(work), choice | {"prompt": "not canonical"}, mcp_execution.CancellationToken()
        )
        self.assertEqual("failed-after-publication", outcome.content["status"])
        self.assertEqual("DISPATCH_PROMPT_NOT_CANONICAL", outcome.content["code"])
        self.assertEqual("committed", outcome.content["effect"])
        self.assertEqual("do-not-retry", outcome.content["retry"])
        self.assertEqual(
            ["immutable-artifact", "accepted-artifact-reference", "ledger"], outcome.content["changed_surfaces"]
        )
        contract_schemas.validate_result("pinboard_dispatch", outcome.content)
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
            executor = mcp_execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
            try:
                execution = executor.submit(
                    lambda token: mcp_jobs._dispatch_job(str(project), str(work), choice, token)
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
                                "runtime": "codex",
                                "background": False,
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
