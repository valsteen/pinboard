"""Launcher recovery results and MCP contract mismatches become typed seed failures, never repaired, and each world
extra seeds its declared board state through the exact Pinboard request contracts."""

import asyncio
import unittest
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
from mcp.client.session import ClientSession
from mcp_types import CallToolResult, TextContent

from evals.behavioral import board, export, processes, seeding, world, world_content
from evals.behavioral.export import SeedFailure
from evals.behavioral.processes import Completed, Window
from evals.behavioral.records import Runtime, Scenario, Turn, WorldExtra, WorldKind
from pinboard.application import action_models, work_brief_models
from pinboard.mcp import contracts

RECOVERY = (
    '{"schema":"pinboard-launcher-result/v1","status":"runtime-preparation-required","pinboard_started":false,'
    '"runtime_location":{"base":"launcher-root","relative":".pinboard-runtime"},"observations":["not ready"],'
    '"upstream_exit_code":null,"retry_disposition":"retry-original-command","effect_disposition":"unchanged",'
    '"changed_surfaces":[],"next_action":{"launcher":"self","arguments":["--prepare-runtime"],'
    '"display_command":"scripts/pinboard --prepare-runtime","requires":["uv"]}}'
)


class LauncherRecoveryTest(unittest.TestCase):
    def test_a_recovery_result_from_board_init_is_a_seed_failure(self) -> None:
        with (
            patch.object(world.processes, "launcher_init", return_value=Completed(0, RECOVERY, "", False)),
            self.assertRaises(SeedFailure),
        ):
            world.init_board(Path("launcher"), Path("project"), None, Window(None))

    def test_an_unready_runtime_preparation_is_a_seed_failure(self) -> None:
        with (
            patch.object(export.processes, "launcher_prepare_runtime", return_value=Completed(1, RECOVERY, "", False)),
            self.assertRaises(SeedFailure),
        ):
            export.prepare_runtime(Path("plugin"), Window(None))

    def test_preparation_without_a_launcher_result_is_a_seed_failure(self) -> None:
        with (
            patch.object(
                export.processes, "launcher_prepare_runtime", return_value=Completed(127, "", "not found", False)
            ),
            self.assertRaises(SeedFailure),
        ):
            export.prepare_runtime(Path("plugin"), Window(None))


class ToolResultTest(unittest.TestCase):
    def test_a_result_that_lacks_a_consumed_field_is_a_seed_failure(self) -> None:
        result = CallToolResult(content=[], structured_content={"status": "committed"}, is_error=False)
        with self.assertRaises(SeedFailure):
            board.read_result("pinboard_attempt_authority", result, board.Lease)

    def test_a_tool_error_is_a_seed_failure(self) -> None:
        result = CallToolResult(content=[TextContent(type="text", text="unknown tool")], is_error=True)
        with self.assertRaises(SeedFailure):
            board.read_result("pinboard_missing", result, board.Status)

    def test_unrelated_additional_fields_are_ignored(self) -> None:
        result = CallToolResult(content=[], structured_content={"status": "committed", "revision": "7"}, is_error=False)
        self.assertEqual("committed", board.read_result("pinboard_transition", result, board.Status).status)


class ItemStateReadTest(unittest.IsolatedAsyncioTestCase):
    async def test_seed_verification_uses_the_current_native_item_request(self) -> None:
        async def read_item(tool: str, arguments: dict[str, contracts.JsonValue]) -> CallToolResult:
            self.assertEqual("pinboard_item_status", tool)
            selected = msgspec.convert(arguments, type=contracts.ItemStatusEnvelope, strict=True).request
            self.assertIsInstance(selected, contracts.ItemStatusItemRequest)
            self.assertEqual("/project", selected.project_root)
            self.assertEqual("/scratch-board", selected.work_root)
            return CallToolResult(content=[], structured_content={"state": "done"}, is_error=False)

        session = MagicMock(spec=board.ClientSession)
        session.call_tool = AsyncMock(side_effect=read_item)

        @asynccontextmanager
        async def connected(_launcher: Path, _log: Path, window: Window) -> AsyncGenerator[board.BoardClient]:
            yield board.BoardClient(session, window)

        with patch.object(board, "connect", connected):
            observed = await board.item_states(
                Path("launcher"), Path("log"), Path("/project"), Path("/scratch-board"), ("one", "two"), Window(None)
            )
        self.assertEqual([("one", "done"), ("two", "done")], observed)
        self.assertEqual(2, session.call_tool.await_count)


class SeededReviewTest(unittest.IsolatedAsyncioTestCase):
    async def test_commissioned_prompt_and_reviewer_reach_ready_and_completion(self) -> None:
        prompt_sha256 = "a" * 64
        requests: list[contracts.ReviewJobRequest | contracts.TransitionRequest] = []

        async def call_tool(tool: str, arguments: dict[str, contracts.JsonValue]) -> CallToolResult:
            match tool:
                case "pinboard_review_job":
                    request = msgspec.convert(arguments, type=contracts.ReviewJobRequest, strict=True)
                    requests.append(request)
                    match request.review:
                        case contracts.InitialReviewChoice():
                            content = {"status": "ready", "prompt_reference": {"sha256": prompt_sha256}}
                        case contracts.RecordReadyReviewChoice():
                            content = {"status": "recorded"}
                        case _:
                            self.fail("unexpected review leaf")
                case "pinboard_attempt_inspect":
                    content = {"candidate_recovery": {"sha256": "b" * 64}, "accepted_brief": {"sha256": "c" * 64}}
                case "pinboard_actions":
                    content = {"actions": [{"subject_revision": "7"}]}
                case "pinboard_transition":
                    requests.append(contracts.decode_transition_request(arguments))
                    content = {"status": "committed"}
                case _:
                    self.fail(f"unexpected tool {tool}")
            return CallToolResult(content=[], structured_content=content, is_error=False)

        session = AsyncMock(spec=ClientSession)
        session.call_tool.side_effect = call_tool
        with TemporaryDirectory() as temporary, patch.object(seeding, "head", return_value="d" * 40):
            root = Path(temporary)
            fixture = seeding.Seeder(
                board.BoardClient(session, Window(None)), root / "project", root / "board", "fixture-owner"
            )
            attempt = fixture.work_root / "attempts" / "example-1"
            attempt.mkdir(parents=True)
            (attempt / "result.md").write_text("Verified fixture result\n")
            digest = await fixture.review_publish("example", "Verdict: ready.")
            await fixture.review_ready("example", digest)
            await fixture.complete_reviewed("example", "The fixture change was reviewed")

        self.assertEqual(3, len(requests))
        ready = requests[1]
        self.assertIsInstance(ready, contracts.ReviewJobRequest)
        assert isinstance(ready, contracts.ReviewJobRequest)
        self.assertIsInstance(ready.review, contracts.RecordReadyReviewChoice)
        assert isinstance(ready.review, contracts.RecordReadyReviewChoice)
        self.assertEqual(prompt_sha256, ready.review.reviewer_prompt_sha256)
        completion = requests[2]
        self.assertIsInstance(completion, contracts.ProjectTransitionRequest)
        assert isinstance(completion, contracts.ProjectTransitionRequest)
        self.assertIsInstance(completion.payload, action_models.ReviewedCompleteInputPayload)
        assert isinstance(completion.payload, action_models.ReviewedCompleteInputPayload)
        self.assertEqual(ready.review.reviewer_task_id, completion.payload.reviewer_task_id)


@dataclass
class FakeBoard:
    """Answers the seeder's MCP calls after decoding each consumed request through Pinboard's own contracts."""

    briefs: list[contracts.BriefPublishRequest]
    reviews: list[contracts.ReviewJobRequest]
    transitions: list[contracts.TransitionRequest]

    async def answer(self, tool: str, arguments: dict[str, contracts.JsonValue]) -> CallToolResult:
        match tool:
            case "pinboard_proposal_create":
                msgspec.convert(arguments, type=contracts.ProposalCreateRequest, strict=True)
                content = {"status": "committed"}
            case "pinboard_preparation_authority" | "pinboard_attempt_authority":
                content = {"status": "committed", "lease_id": "lease-1", "generation": 1}
            case "pinboard_item_definition":
                content = {"definition_revision": 1, "definition_digest": "e" * 64}
            case "pinboard_brief_publish":
                self.briefs.append(msgspec.convert(arguments, type=contracts.BriefPublishRequest, strict=True))
                content = {"status": "committed", "reference": {"artifact_ref_id": 3}}
            case "pinboard_actions":
                content = {"actions": [{"subject_revision": "7"}]}
            case "pinboard_transition":
                self.transitions.append(contracts.decode_transition_request(arguments))
                content = {"status": "committed"}
            case "pinboard_review_job":
                request = msgspec.convert(arguments, type=contracts.ReviewJobRequest, strict=True)
                self.reviews.append(request)
                initial = isinstance(request.review, contracts.InitialReviewChoice)
                content = (
                    {"status": "ready", "prompt_reference": {"sha256": "a" * 64}} if initial else {"status": "recorded"}
                )
            case "pinboard_attempt_inspect":
                content = {"candidate_recovery": {"sha256": "b" * 64}, "accepted_brief": {"sha256": "c" * 64}}
            case _:
                raise AssertionError(f"unexpected tool {tool}")
        return CallToolResult(content=[], structured_content=content, is_error=False)


def committed_project(root: Path, window: Window) -> Path:
    project = root / "tally"
    processes.git_checked(["init", "-q", "-b", "main", str(project)], cwd=root, window=window)
    processes.git_checked(["config", "user.name", "Fixture"], cwd=project, window=window)
    processes.git_checked(["config", "user.email", "fixture@example.test"], cwd=project, window=window)
    (project / "docs").mkdir()
    (project / "docs" / "usage.md").write_text("# Usage\n")
    processes.git_checked(["add", "-A"], cwd=project, window=window)
    processes.git_checked(["commit", "-q", "-m", "Initial"], cwd=project, window=window)
    return project


class ContinueCheckpointWorldTest(unittest.TestCase):
    """The continue-checkpoint extra publishes a continue brief and records commissioned ready review in review."""

    def test_seeding_publishes_a_continue_checkpoint_and_records_ready_review(self) -> None:
        fake = FakeBoard(briefs=[], reviews=[], transitions=[])
        session = AsyncMock(spec=ClientSession)
        session.call_tool.side_effect = fake.answer
        window = Window(None)
        with TemporaryDirectory() as temporary:
            project = committed_project(Path(temporary), window)
            seeder = seeding.Seeder(board.BoardClient(session, window), project, project / ".pinboard", "owner")
            asyncio.run(world_content.seed_continue_checkpoint(seeder))
            candidate = seeding.head(seeder.worktree("usage-guide"), window)

        (published,) = fake.briefs
        self.assertEqual("usage-guide", published.brief.item_id)
        disposition = published.brief.checkpoint.disposition
        self.assertIsInstance(disposition, work_brief_models.ContinueCheckpointDisposition)
        assert isinstance(disposition, work_brief_models.ContinueCheckpointDisposition)
        self.assertIn("exit status", disposition.remaining_work)
        self.assertEqual(
            ["activate", "submit-review"], [transition.receipt.action_id.kind for transition in fake.transitions]
        )
        initial, ready = (review.review for review in fake.reviews)
        self.assertIsInstance(initial, contracts.InitialReviewChoice)
        self.assertIsInstance(ready, contracts.RecordReadyReviewChoice)
        assert isinstance(ready, contracts.RecordReadyReviewChoice)
        self.assertEqual("usage-guide-1", ready.attempt_id)
        self.assertEqual(candidate, ready.candidate_revision)

    def test_build_world_seeds_the_extra_on_the_project_board_and_verifies_review(self) -> None:
        scenario = Scenario(
            id="continue-checkpoint",
            title="Accept the reviewed checkpoint",
            world=WorldKind.MINIMAL,
            world_extra=WorldExtra.CONTINUE_CHECKPOINT_REVIEW,
            source="fixture",
            ground_truth="fixture",
            turns=[Turn(human="What remains?", before=None)],
        )
        seeded_content: list[Path] = []

        @asynccontextmanager
        async def connected(_launcher: Path, _log: Path, window: Window) -> AsyncGenerator[board.BoardClient]:
            yield board.BoardClient(MagicMock(spec=ClientSession), window)

        async def seed(seeder: seeding.Seeder) -> None:
            seeded_content.append(seeder.work_root)

        async def states(*arguments: Path | tuple[str, ...] | Window) -> list[tuple[str, str]]:
            self.assertEqual(("usage-guide",), arguments[4])
            return [("usage-guide", "review")]

        with (
            TemporaryDirectory() as temporary,
            patch.object(world, "create_project"),
            patch.object(world, "init_board"),
            patch.object(board, "connect", connected),
            patch.object(world, "seed_continue_checkpoint", seed),
            patch.object(board, "item_states", states),
        ):
            built, seeded = world.build_world(
                Path(temporary) / "world", scenario, Runtime.CLAUDE_CODE, Path("plugin"), "owner", Window(None)
            )
        self.assertEqual([built.work_root], seeded_content)
        self.assertEqual([("project", "usage-guide", "review")], [(s.board, s.item_id, s.state) for s in seeded])


if __name__ == "__main__":
    unittest.main()
