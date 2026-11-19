"""Native candidate-launcher evidence for changed-hash integration and its review/authority controls."""

import asyncio
import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from evals.behavioral import board, processes, seeding, world
from evals.behavioral.records import Hook, Runtime, Scenario, Turn, WorldExtra, WorldKind
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
                schema="pinboard-reviewed-completion/v3",
                human_decision="Complete this merged item now.",
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


class Landed(board.Request, frozen=True):
    target: str
    pre_landing_revision: str
    landed_revision: str


class LandedCompletion(board.ReviewedCompletion, frozen=True):
    landed: Landed


class LandedRefusal(board.Projection, frozen=True):
    status: str
    code: str
    retry: str
    effect: str
    state_changed: bool
    message: str


def reviewed_change_scenario(hook: Hook) -> Scenario:
    return Scenario(
        id=f"fixture-{hook.value}",
        title="Reviewed saved change before a moved main",
        world=WorldKind.MINIMAL,
        world_extra=WorldExtra.REVIEWED_CHANGE,
        source="Deterministic world-hook fixture",
        ground_truth="Built without an agent",
        turns=[Turn(human="What is the status of merge-change?", before=hook)],
    )


class RebaseWorldTest(unittest.TestCase):
    """The moved-main and conflicting-rebase hooks build their declared Git worlds against the reviewed change."""

    def test_overlapping_main_push_makes_a_rebase_onto_origin_main_conflict(self) -> None:
        observed, checkout = self.pushed_world(Hook.PUSH_OVERLAPPING_MAIN)
        rebase = processes.git(["rebase", "origin/main"], cwd=checkout, window=observed.window)
        self.assertNotEqual(0, rebase.returncode)
        conflicted = self.git(observed, checkout, "diff", "--name-only", "--diff-filter=U")
        self.assertEqual(["docs/usage.md"], conflicted.split())

    def test_unrelated_main_push_rebases_cleanly_and_keeps_the_reviewed_content(self) -> None:
        observed, checkout = self.pushed_world(Hook.PUSH_UNRELATED_MAIN)
        rebase = processes.git(["rebase", "origin/main"], cwd=checkout, window=observed.window)
        self.assertEqual(0, rebase.returncode, rebase.stderr)
        rebased = seeding.head(checkout, observed.window)
        integration = asyncio.run(self.integration(observed, rebased))
        self.assertEqual(("content-present", rebased), (integration.presence, integration.target_revision))

    def test_conflicting_rebase_lands_changed_content_and_refuses_landed_completion(self) -> None:
        observed, _, _ = self.hooked_world(Hook.CONFLICTING_REBASE_SAVED_CHANGE)
        self.git(observed, observed.project, "fetch", "-q", "origin")
        for target in ("main", "origin/main"):
            usage = self.git(observed, observed.project, "show", f"{target}:docs/usage.md")
            self.assertIn(world.COMBINED_USAGE_NOTE, usage.splitlines())
        asyncio.run(self.assert_landed_refusal(observed))

    def pushed_world(self, hook: Hook) -> tuple[world.World, Path]:
        """Run a maintainer push hook, confirm it moved only origin, then fetch it into the project."""
        observed, checkout, before = self.hooked_world(hook)
        self.assertEqual(before, self.heads(observed, checkout))
        self.git(observed, observed.project, "fetch", "-q", "origin")
        return observed, checkout

    def hooked_world(self, hook: Hook) -> tuple[world.World, Path, tuple[str, str]]:
        """Build the reviewed-change world and run one hook, returning the main and saved-change heads before it."""
        temporary = self.enterContext(TemporaryDirectory())
        observed, seeded = world.build_world(
            Path(temporary) / "world",
            reviewed_change_scenario(hook),
            Runtime.CLAUDE_CODE,
            Path(__file__).resolve().parents[1],
            "fixture-owner",
            processes.Window(None),
        )
        self.assertEqual([("merge-change", "review")], [(item.item_id, item.state) for item in seeded])
        checkout = observed.project.parent / "wt-merge-change"
        before = self.heads(observed, checkout)
        world.run_hook(hook, observed)
        return observed, checkout, before

    def heads(self, observed: world.World, checkout: Path) -> tuple[str, str]:
        return seeding.head(observed.project, observed.window), seeding.head(checkout, observed.window)

    async def assert_landed_refusal(self, observed: world.World) -> None:
        project, work_root = str(observed.project), str(observed.work_root)
        main = seeding.head(observed.project, observed.window)
        pre_landing = self.git(observed, observed.project, "rev-parse", "HEAD^1").strip()
        integration = await self.integration(observed, "main")
        self.assertEqual(main, integration.target_revision)
        self.assertNotEqual("content-present", integration.presence)
        async with board.connect(observed.launcher, observed.mcp_log, observed.window) as client:
            seeder = seeding.Seeder(client, observed.project, observed.work_root, "fixture-owner")
            action = board.ActionId(kind="complete", subject="merge-change-1")
            actions = await client.call(
                "pinboard_actions",
                board.Enveloped(
                    board.ProjectActions(role="project", project_root=project, work_root=work_root, action_id=action)
                ),
                board.Actions,
            )
            refusal = await client.call(
                "pinboard_transition",
                board.Enveloped(
                    board.ProjectTransition(
                        role="project",
                        project_root=project,
                        work_root=work_root,
                        receipt=board.Receipt(
                            action_id=action, subject_revision=seeding.first_revision(actions, "complete fixture")
                        ),
                        payload=LandedCompletion(
                            schema="pinboard-reviewed-completion/v3",
                            human_decision="Complete this merged item now.",
                            candidate=integration.source.candidate_revision,
                            evidence=f"main {main} carries the resolved usage line",
                            reviewer_task_id="review-merge-change",
                            result_sha256=seeder.evidence_sha256("merge-change-1", "result.md"),
                            review_sha256=seeder.evidence_sha256("merge-change-1", "review.md"),
                            packages=[],
                            landed=Landed(target="main", pre_landing_revision=pre_landing, landed_revision=main),
                        ),
                        actor_task_id="fixture-owner",
                        actor_host_id=board.SEEDED_HOST_ID,
                    )
                ),
                LandedRefusal,
            )
            self.assertEqual(
                ("rejected", "TRANSITION_INPUT_INVALID", "correct-input", "unchanged", False),
                (refusal.status, refusal.code, refusal.retry, refusal.effect, refusal.state_changed),
            )
            self.assertIn(main, refusal.message)
            status = await client.call(
                "pinboard_item_status",
                board.Enveloped(
                    board.ItemStatus(
                        operation="item", project_root=project, work_root=work_root, item_id="merge-change"
                    )
                ),
                ItemReport,
            )
            self.assertEqual("review", status.state)

    async def integration(self, observed: world.World, target: str) -> IntegrationReport:
        async with board.connect(observed.launcher, observed.mcp_log, observed.window) as client:
            return await client.call(
                "pinboard_item_status",
                IntegrationEnvelope(
                    IntegrationRequest(
                        operation="integration",
                        project_root=str(observed.project),
                        work_root=str(observed.work_root),
                        item_id="merge-change",
                        target=target,
                    )
                ),
                IntegrationReport,
            )

    def git(self, observed: world.World, checkout: Path, *arguments: str) -> str:
        return processes.git_checked(list(arguments), cwd=checkout, window=observed.window)


if __name__ == "__main__":
    unittest.main()
