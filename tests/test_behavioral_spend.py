"""Spend is recomputed from recorded runs, scorer sessions, assessments and probes, and the cap guard holds."""

import tempfile
import unittest
from pathlib import Path
from typing import override

from evals.behavioral import processes, spend
from evals.behavioral.compatibility_records import CompatibilityRunRecord
from evals.behavioral.layout import Layout
from evals.behavioral.records import (
    ClaudeRunDetails,
    CodexAccounting,
    Completed,
    ExportRecord,
    ProbeRecord,
    ReviewerUsage,
    RunKey,
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
        CompatibilityRunRecord(
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
        budget = Budget(self.layout, cap_usd=6.5, window=processes.Window(None), allow_unknown_reviewer_price=False)
        self.assertEqual(3.0, budget.reserve(Category.CLAUDE_AGENT_RUN))
        self.assertIsNone(budget.reserve(Category.CLAUDE_AGENT_RUN))

    def test_a_released_reservation_frees_room_and_recorded_spend_uses_it(self) -> None:
        record_run(self.layout, "repeat", 1, [3.0])
        budget = Budget(self.layout, cap_usd=7.0, window=processes.Window(None), allow_unknown_reviewer_price=False)
        projected = budget.reserve(Category.CLAUDE_AGENT_RUN)
        assert projected is not None
        budget.release(projected)
        self.assertEqual(projected, budget.reserve(Category.CLAUDE_AGENT_RUN))
        budget.release(projected)
        record_run(self.layout, "repeat", 2, [3.0])
        self.assertIsNone(budget.reserve(Category.CLAUDE_AGENT_RUN))

    def test_interrupted_probe_keeps_known_spend_and_blocks_further_paid_work(self) -> None:
        write_new(
            self.layout.probe_file("cutoff").parent / "accounting.json",
            CodexAccounting(
                schema="pinboard-behavioral-codex-accounting/v1",
                main_known_cost_usd=0.25,
                main_usage_complete=False,
                reviewer_usage=[],
                reviewer_price_usd=None,
            ),
        )
        self.assertEqual(0.25, spend.total(spend.items(self.layout)))
        self.assertTrue(spend.main_usage_unknown(self.layout))
        self.assertIsNone(Budget(self.layout, 120, processes.Window(None), True).reserve(Category.SCORER))
        self.assertIn("total dollars unknown", spend.report(self.layout))

    def test_unknown_reviewer_price_requires_exception_and_never_becomes_zero_dollars(self) -> None:
        record_probe(self.layout, "isolation", 0.25)
        write_new(
            self.layout.probe_file("isolation").parent / "accounting.json",
            CodexAccounting(
                schema="pinboard-behavioral-codex-accounting/v1",
                main_known_cost_usd=0.25,
                main_usage_complete=True,
                reviewer_usage=[
                    ReviewerUsage(
                        thread_id="reviewer",
                        model="codex-auto-review",
                        input_tokens=12,
                        cached_input_tokens=3,
                        cache_write_input_tokens=0,
                        output_tokens=2,
                        reasoning_output_tokens=1,
                    )
                ],
                reviewer_price_usd=None,
            ),
        )
        self.assertEqual(0.25, spend.total(spend.items(self.layout)))
        self.assertIsNone(Budget(self.layout, 120, processes.Window(None), False).reserve(Category.SCORER))
        self.assertIsNotNone(Budget(self.layout, 120, processes.Window(None), True).reserve(Category.SCORER))
        self.assertIn("price unknown", spend.report(self.layout))
        self.assertIn("total dollars unknown", spend.report(self.layout))
        self.assertIn("comparison reviewer dollar cost is unknown", spend.comparison_reasons(self.layout))

    def test_comparison_spend_counts_orphan_accounting_and_incomplete_session_directories(self) -> None:
        # The 120 USD cap is the independently accepted CRITERIA.md spending contract.
        for family, record in (
            ("runs", "run.json"),
            ("scores", "session.json"),
            ("assessments", "assessment.json"),
            ("probes", "probe.json"),
            ("investigations", "run.json"),
            ("investigation-assessments", "session.json"),
        ):
            with self.subTest(family=family), tempfile.TemporaryDirectory() as temporary:
                layout = Layout(Path(temporary))
                path = (
                    layout.root / family / "case" / "session"
                    if family not in {"scores", "probes"}
                    else layout.root / family / "session"
                )
                path.mkdir(parents=True)
                self.assertFalse((path / record).exists())
                self.assertTrue(
                    any("incomplete session evidence" in reason for reason in spend.comparison_reasons(layout))
                )
        write_new(
            self.layout.probe_file("orphan").parent / "accounting.json",
            CodexAccounting("pinboard-behavioral-codex-accounting/v1", 120.01, True, [], None),
        )
        self.assertEqual(120.01, spend.total(spend.items(self.layout)))
        reasons = spend.comparison_reasons(self.layout)
        self.assertIn("comparison spending exceeds 120 USD", reasons)
        self.assertTrue(any("incomplete session evidence" in reason for reason in reasons))

    def test_without_a_recorded_session_the_projection_is_the_category_default(self) -> None:
        budget = Budget(
            self.layout,
            cap_usd=spend.DEFAULT_PROJECTION_USD[Category.SCORER],
            window=processes.Window(None),
            allow_unknown_reviewer_price=False,
        )
        self.assertIsNotNone(budget.reserve(Category.SCORER))
        self.assertIsNone(budget.reserve(Category.SCORER))


if __name__ == "__main__":
    unittest.main()
