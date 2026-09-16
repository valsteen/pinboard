import asyncio
import io
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable, Coroutine
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
from pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from pinboard.adapters.files.models import ViewRefreshResult, ViewWarning
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import authority_operations, queries, query_models, stored_state, work_brief_models
from pinboard.application.artifact_publication import ArtifactPublication
from pinboard.application.artifacts import NewArtifact
from pinboard.application.ports import WorkStoreError
from pinboard.application.work_briefs import canonical_work_brief_bytes
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import (
    ChangedSurface,
    DecisionFailure,
    DecisionFailureCode,
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
from tests.work_brief_support import example_work_brief, work_a_brief, work_c_brief


def _run_async[Result](operation: Coroutine[None, None, Result]) -> Result:
    return asyncio.run(operation)


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
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "operation": "status",
                        "item_id": "work-c",
                    },
                )
                preparation_start = await session.call_tool(
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
                )
                started = preparation_start.structured_content
                if not isinstance(started, dict):
                    raise AssertionError("Preparation start did not return structured content.")
                preparation_renew = await session.call_tool(
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
                )
                renewed = preparation_renew.structured_content
                if not isinstance(renewed, dict):
                    raise AssertionError("Preparation renewal did not return structured content.")
                preparation_release = await session.call_tool(
                    mcp_server.PREPARATION_AUTHORITY_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "operation": "release",
                        "item_id": "work-c",
                        "lease_id": renewed["lease_id"],
                        "generation": renewed["generation"],
                    },
                )
                attempt_status = await session.call_tool(
                    mcp_server.ATTEMPT_AUTHORITY_TOOL,
                    {
                        "project_root": str(project),
                        "work_root": str(roots.work_root),
                        "operation": "status",
                        "attempt_id": "work-a-1",
                    },
                )
                invalid_payload = await session.call_tool(
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
                )
                transition = await session.call_tool(
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
                )
                stale = await session.call_tool(
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
        leaves = schema["oneOf"]
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
                str(project),
                str(roots.work_root),
                "worker",
                {"action_id": {"kind": "submit-review", "subject": "work-a-1"}, "subject_revision": "8"},
                {"candidate": candidate},
                "attempt-lease-a",
                3,
                mcp_server._OmittedAuthorityField.VALUE,
                mcp_server._OmittedAuthorityField.VALUE,
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
                    result = await session.call_tool(tool, common | arguments)
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
        request = contracts.decode_transition_request(raw)
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
                contracts.decode_transition_request(invalid)
        with patch.object(mcp_server, "_resolve_durable") as resolve:
            rejected = mcp_server._transition(
                "/project",
                "/work",
                "project",
                {"action_id": {"kind": "pause", "subject": "attempt-1"}, "subject_revision": "7"},
                {"reason": "Pause for correction.", "unknown": True},
                mcp_server._OmittedAuthorityField.VALUE,
                mcp_server._OmittedAuthorityField.VALUE,
                "project-task",
                "local",
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
                decoded = contracts.decode_transition_request(raw)
                self.assertIsInstance(decoded.payload, msgspec.Struct)
                with self.assertRaises((msgspec.ValidationError, ValueError)):
                    contracts.decode_transition_request({**raw, "payload": {**payload, "unexpected": True}})
                with self.assertRaises((msgspec.ValidationError, ValueError)):
                    contracts.decode_transition_request({**raw, "role": "observer"})

    def test_attempt_acquisition_selects_initial_or_transfer_only_under_the_write_lock(self) -> None:
        temporary, _project, roots = self._project()
        self.addCleanup(temporary.cleanup)
        store = SQLiteWorkStore(roots.database_path)
        now = datetime.now(UTC)
        retained = store.read_attempt_authority_status(AttemptId("work-a-1"))
        self.assertIsNotNone(retained)
        assert retained is not None
        released = authority_operations.change_attempt_authority(
            store,
            operation="release",
            attempt_id=AttemptId("work-a-1"),
            lease_id=retained.lease_id,
            generation=retained.generation,
            operation_time=now,
            expires_at=None,
            actor_task_id=None,
            actor_host_id=None,
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
            result = await server.call_tool(tool_name, arguments)
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

    def test_shared_authority_operations_reject_invalid_internal_requests(self) -> None:
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
        missing_change = authority_operations.change_attempt_authority(
            store,
            operation="release",
            attempt_id=AttemptId("missing-attempt"),
            lease_id=LeaseId("missing-lease"),
            generation=1,
            operation_time=now,
            expires_at=None,
            actor_task_id=None,
            actor_host_id=None,
        )
        self.assertIsInstance(missing_change, DecisionFailure)
        for operation, expires_at, actor_task_id, actor_host_id in (
            ("renew", None, None, None),
            ("revoke", None, None, None),
            ("unsupported", None, None, None),
        ):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                authority_operations.change_attempt_authority(
                    store,
                    operation=operation,
                    attempt_id=AttemptId("work-a-1"),
                    lease_id=LeaseId("attempt-lease-a"),
                    generation=3,
                    operation_time=now,
                    expires_at=expires_at,
                    actor_task_id=actor_task_id,
                    actor_host_id=actor_host_id,
                )

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
        missing_preparation = authority_operations.change_preparation_authority(
            store,
            operation="release",
            item_id=ItemId("work-b"),
            lease_id=LeaseId("missing-lease"),
            generation=1,
            operation_time=now,
            expires_at=None,
            actor_task_id=None,
            actor_host_id=None,
        )
        self.assertIsInstance(missing_preparation, DecisionFailure)
        for operation, expires_at in (("renew", None), ("revoke", None), ("unsupported", None)):
            with self.subTest(preparation_operation=operation), self.assertRaises(ValueError):
                authority_operations.change_preparation_authority(
                    store,
                    operation=operation,
                    item_id=ItemId("work-c"),
                    lease_id=LeaseId("preparation-lease"),
                    generation=1,
                    operation_time=now,
                    expires_at=expires_at,
                    actor_task_id=None,
                    actor_host_id=None,
                )

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
                common = {"project_root": str(project), "work_root": str(roots.work_root)}
                return (
                    await session.call_tool(
                        mcp_server.ACTIONS_TOOL,
                        common | {"role": "project", "lease_id": None},
                    ),
                    await session.call_tool(
                        mcp_server.ACTIONS_TOOL,
                        common | {"role": "observer", "generation": None},
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
                common = {"project_root": str(project), "work_root": str(roots.work_root)}
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
                            common | {"role": "worker", "lease_id": "attempt-lease-a", "generation": "3"},
                        ),
                        "ACTIONS_INVALID",
                    ),
                    (
                        await session.call_tool(
                            mcp_server.ACTIONS_TOOL,
                            common | {"role": "worker", "lease_id": "attempt-lease-a", "generation": True},
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
                    *common,
                    "project",
                    mcp_server._OmittedAuthorityField.VALUE,
                    mcp_server._OmittedAuthorityField.VALUE,
                    {"kind": "continue", "subject": "work-a-1"},
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
        store = SQLiteWorkStore(roots.database_path)
        before = store.validated_snapshot()
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
                common = {"project_root": str(project), "work_root": str(roots.work_root)}
                return (
                    await session.call_tool(mcp_server.OVERVIEW_TOOL, common),
                    await session.call_tool(mcp_server.ACTIONS_TOOL, common | {"role": "observer"}),
                    await session.call_tool(mcp_server.ACTIONS_TOOL, common | {"role": "project"}),
                    await session.call_tool(
                        mcp_server.ACTIONS_TOOL,
                        common
                        | {
                            "role": "project",
                            "action_id": {"kind": "continue", "subject": "work-a-1"},
                        },
                    ),
                    await session.call_tool(
                        mcp_server.ACTIONS_TOOL,
                        common
                        | {
                            "role": "worker",
                            "lease_id": "attempt-lease-a",
                            "generation": 3,
                        },
                    ),
                    await session.call_tool(
                        mcp_server.ACTIONS_TOOL,
                        common | {"role": "preparer", "lease_id": "missing-lease", "generation": 1},
                    ),
                    await session.call_tool(
                        mcp_server.ACTIONS_TOOL,
                        common | {"role": "worker", "lease_id": "attempt-lease-a"},
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
            (mcp_server.ACTIONS_TOOL, mcp_server._read_actions(*common, "observer", None, None, None, token)),
            (mcp_server.ACTIONS_TOOL, mcp_server._read_actions(*common, "project", None, None, None, token)),
            (
                mcp_server.ACTIONS_TOOL,
                mcp_server._read_actions(*common, "worker", "attempt-lease-a", 3, None, token),
            ),
            (
                mcp_server.ACTIONS_TOOL,
                mcp_server._read_actions(*common, "preparer", "missing-lease", 1, None, token),
            ),
            (
                mcp_server.ACTIONS_TOOL,
                mcp_server._read_actions(*common, "worker", "attempt-lease-a", None, None, token),
            ),
            (
                mcp_server.ACTIONS_TOOL,
                mcp_server._read_actions(
                    *common,
                    "project",
                    None,
                    None,
                    {"kind": "continue", "subject": "missing"},
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
            rejected = await server.call_tool(mcp_server.PROPOSAL_CREATE_TOOL, {**arguments, "proposal": invalid})
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
                        result = await server.call_tool(tool_name, {**arguments, field: invalid_root})
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
                    _run_async(server.call_tool(operation, arguments))
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
            },
            {tool.name for tool in tools},
        )
        tools_by_name = {tool.name: tool for tool in tools}
        expected_required = {
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
