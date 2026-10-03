"""Native candidate-launcher evidence for changed-hash integration and its review/authority controls."""

import asyncio
import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from evals.behavioral import board, processes, seeding, world
from evals.behavioral.records import Runtime
from evals.behavioral.scenarios import DATA, load_set


class IntegrationRequest(board.Request, frozen=True):
    operation: Literal["integration"]
    project_root: str
    work_root: str
    item_id: str
    target: str


class IntegrationEnvelope(board.Request, frozen=True):
    request: IntegrationRequest


class ReadyVerdict(board.Projection, frozen=True, tag="ready", tag_field="kind"):
    candidate_revision: str


class MissingVerdict(board.Projection, frozen=True, tag="none", tag_field="kind"):
    pass


class ItemReport(board.Projection, frozen=True):
    state: str
    revision: str
    review_verdict: ReadyVerdict | MissingVerdict


class IntegrationSource(board.Projection, frozen=True):
    candidate_revision: str


class IntegrationReport(board.Projection, frozen=True):
    presence: str
    target_revision: str
    source: IntegrationSource


class NextOperation(board.Projection, frozen=True):
    kind: str


class Continuation(board.Projection, frozen=True):
    next_operation: NextOperation


class InspectionReport(board.Projection, frozen=True):
    continuation: Continuation


class TransitionReport(board.Projection, frozen=True):
    status: str
    code: str
    effect: str
    state_changed: bool


class MergeEvidenceTest(unittest.TestCase):
    def test_registered_hooks_preserve_review_and_content_as_independent_evidence(self) -> None:
        registered = load_set(DATA / "scenario-sets" / "review-after-merge.json")
        launcher_root = Path(__file__).resolve().parents[1]
        for scenario in registered.scenarios:
            with self.subTest(scenario=scenario.id), TemporaryDirectory() as temporary:
                observed, seeded = world.build_world(
                    Path(temporary) / "world",
                    scenario,
                    Runtime.CLAUDE_CODE,
                    launcher_root,
                    "fixture-owner",
                    processes.Window(None),
                )
                self.assertEqual([("merge-change", "review")], [(item.item_id, item.state) for item in seeded])
                hook = scenario.turns[0].before
                assert hook is not None
                log = world.run_hook(hook, observed)
                asyncio.run(self.assert_native_evidence(observed, scenario.id, log))

    async def assert_native_evidence(self, observed: world.World, scenario_id: str, log: str) -> None:
        project, work_root = str(observed.project), str(observed.work_root)
        # The status-only control observes exactly these reads, without a transition or review commission.
        before = {
            path.relative_to(observed.work_root): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in observed.work_root.rglob("*")
            if path.is_file()
        }
        refs = processes.git_checked(["show-ref"], cwd=observed.project, window=observed.window)
        async with board.connect(observed.launcher, observed.mcp_log, observed.window) as client:
            status = await client.call(
                "pinboard_item_status",
                board.Enveloped(
                    board.ItemStatus(
                        operation="item", project_root=project, work_root=work_root, item_id="merge-change"
                    )
                ),
                ItemReport,
            )
            integration = await client.call(
                "pinboard_item_status",
                IntegrationEnvelope(
                    IntegrationRequest(
                        operation="integration",
                        project_root=project,
                        work_root=work_root,
                        item_id="merge-change",
                        target="main",
                    )
                ),
                IntegrationReport,
            )
            inspection = await client.call(
                "pinboard_attempt_inspect",
                board.AttemptInspect(
                    project_root=project, work_root=work_root, attempt_id="merge-change-1", reconciliation=None
                ),
                InspectionReport,
            )
            self.assertEqual("review", status.state)
            self.assertEqual("content-present", integration.presence)
            self.assertNotEqual(integration.source.candidate_revision, integration.target_revision)
            self.assertIn(integration.source.candidate_revision, log)
            self.assertIn(integration.target_revision, log)
            expected_ready = scenario_id != "s26-merged-missing-review"
            if expected_ready:
                self.assertIsInstance(status.review_verdict, ReadyVerdict)
                assert isinstance(status.review_verdict, ReadyVerdict)
                self.assertEqual(integration.source.candidate_revision, status.review_verdict.candidate_revision)
                self.assertEqual("reconcile-repository", inspection.continuation.next_operation.kind)
            else:
                self.assertIsInstance(status.review_verdict, MissingVerdict)
                self.assertEqual("review-subagent", inspection.continuation.next_operation.kind)
            after = {
                path.relative_to(observed.work_root): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in observed.work_root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)
            self.assertEqual(refs, processes.git_checked(["show-ref"], cwd=observed.project, window=observed.window))
            if scenario_id == "s27-reviewed-merged-status":
                return
            seeder = seeding.Seeder(client, observed.project, observed.work_root, "fixture-owner")
            action = board.ActionId(kind="complete", subject="merge-change-1")
            actions = await client.call(
                "pinboard_actions",
                board.Enveloped(
                    board.ProjectActions(role="project", project_root=project, work_root=work_root, action_id=action)
                ),
                board.Actions,
            )
            payload = board.ReviewedCompletion(
                schema="pinboard-reviewed-completion/v2",
                candidate=integration.source.candidate_revision,
                evidence=f"Accepted usage diff is present at main {integration.target_revision}; original candidate review preserved",
                reviewer_task_id="review-merge-change",
                result_sha256=seeder.evidence_sha256("merge-change-1", "result.md"),
                review_sha256=seeder.evidence_sha256("merge-change-1", "review.md") if expected_ready else "0" * 64,
                packages=[],
            )
            result = await client.call(
                "pinboard_transition",
                board.Enveloped(
                    board.ProjectTransition(
                        role="project",
                        project_root=project,
                        work_root=work_root,
                        receipt=board.Receipt(
                            action_id=action, subject_revision=seeding.first_revision(actions, "complete fixture")
                        ),
                        payload=payload,
                        actor_task_id="fixture-owner",
                        actor_host_id=board.SEEDED_HOST_ID,
                    )
                ),
                board.Status if expected_ready else TransitionReport,
            )
            if expected_ready:
                self.assertEqual("committed", result.status)
            else:
                self.assertIsInstance(result, TransitionReport)
                assert isinstance(result, TransitionReport)
                self.assertEqual(
                    ("rejected", "CANDIDATE_REVIEW_REQUIRED", "unchanged", False),
                    (result.status, result.code, result.effect, result.state_changed),
                )


if __name__ == "__main__":
    unittest.main()
