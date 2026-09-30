"""Spend is recomputed from recorded runs, scorer sessions, assessments and probes, and the cap guard holds."""

import tempfile
import unittest
from pathlib import Path
from typing import override

from evals.behavioral import spend
from evals.behavioral.layout import Layout
from evals.behavioral.records import (
    ClaudeRunDetails,
    Completed,
    ExportRecord,
    ProbeRecord,
    RunKey,
    RunRecord,
    Runtime,
    Scored,
    ScorerSession,
    TurnEvidence,
    write_new,
)
from evals.behavioral.spend import Budget, Category


def turn(index: int, cost: float) -> TurnEvidence:
    return TurnEvidence(
        index=index,
        human="question",
        hook_ran=None,
        session_id="session",
        final_reply="answer",
        commentary=[],
        started_at="t0",
        finished_at="t1",
        cost_usd=cost,
        uncached_input_tokens=0,
        cached_input_tokens=0,
        cache_write_input_tokens=0,
        output_tokens=0,
        reasoning_output_tokens=0,
        permission_denials=[],
    )


def record_run(layout: Layout, variant: str, index: int, costs: list[float]) -> None:
    run = RunKey(scenario_id="s1", variant=variant, index=index)
    write_new(
        layout.run_directory(run) / "run.json",
        RunRecord(
            schema="pinboard-behavioral-run/v2",
            run=run,
            runtime=Runtime.CLAUDE_CODE,
            cli_version="test",
            model="model",
            details=ClaudeRunDetails(permission_mode="bypassPermissions", cost_basis="claude total_cost_usd"),
            evaluated=ExportRecord(
                schema="pinboard-behavioral-export/v1", commit="0" * 40, skills_sha256="0" * 64, plugin_root="/plugin"
            ),
            fixture_difference=None,
            seeded_host_id="host",
            seeded_items=[],
            observed_host_ids=[],
            inventory=[],
            turns=[turn(number, cost) for number, cost in enumerate(costs, start=1)],
            started_at="t0",
            finished_at="t1",
            outcome=Completed(),
        ),
    )


def record_scorer(layout: Layout, label: str, cost: float) -> None:
    write_new(
        layout.score_directory(label) / "session.json",
        ScorerSession(
            schema="pinboard-behavioral-scorer-session/v1",
            label=label,
            scorer_model="scorer",
            checklist_sha256="0" * 64,
            cost_usd=cost,
            outcome=Scored(),
        ),
    )


def record_probe(layout: Layout, name: str, cost: float) -> None:
    write_new(
        layout.probe_file(name),
        ProbeRecord(
            schema="pinboard-behavioral-probe/v1",
            name=name,
            runtime=Runtime.CODEX,
            description="probe",
            cost_usd=cost,
            passed=True,
            findings=[],
            inventory=[],
        ),
    )


class SpendTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.layout = Layout(Path(self.directory.name))

    def test_total_is_the_sum_of_every_recorded_amount_itemized_by_category(self) -> None:
        record_run(self.layout, "repeat", 1, [0.5, 0.25])
        record_run(self.layout, "repeat", 2, [1.0])
        record_scorer(self.layout, "T1", 0.125)
        record_probe(self.layout, "isolation", 0.0625)
        recorded = spend.items(self.layout)
        self.assertEqual(1.9375, spend.total(recorded))
        lines = spend.report(self.layout).splitlines()
        self.assertIn("agent runs (claude-code)\t2\t1.7500", lines)
        self.assertIn("  repeat\t2\t1.7500", lines)
        self.assertIn("scorer sessions\t1\t0.1250", lines)
        self.assertIn("probes\t1\t0.0625", lines)
        self.assertEqual("total\t4\t1.9375", lines[-1])

    def test_a_session_whose_projection_exceeds_the_remaining_cap_does_not_start(self) -> None:
        record_run(self.layout, "repeat", 1, [3.0])
        budget = Budget(self.layout, cap_usd=6.5)
        self.assertEqual(3.0, budget.reserve(Category.CLAUDE_AGENT_RUN))
        self.assertIsNone(budget.reserve(Category.CLAUDE_AGENT_RUN))

    def test_a_released_reservation_frees_room_and_recorded_spend_uses_it(self) -> None:
        record_run(self.layout, "repeat", 1, [3.0])
        budget = Budget(self.layout, cap_usd=7.0)
        projected = budget.reserve(Category.CLAUDE_AGENT_RUN)
        assert projected is not None
        budget.release(projected)
        record_run(self.layout, "repeat", 2, [3.0])
        self.assertAlmostEqual(1.0, budget.remaining())
        self.assertIsNone(budget.reserve(Category.CLAUDE_AGENT_RUN))

    def test_without_a_recorded_session_the_projection_is_the_category_default(self) -> None:
        budget = Budget(self.layout, cap_usd=spend.DEFAULT_PROJECTION_USD[Category.SCORER])
        self.assertIsNotNone(budget.reserve(Category.SCORER))
        self.assertIsNone(budget.reserve(Category.SCORER))


if __name__ == "__main__":
    unittest.main()
