"""No-cost checks for investigation trial accounting and recovery evidence."""

import json
import tempfile
import unittest
from pathlib import Path

from evals.behavioral import codex_driver, investigation_trials, processes, spend
from evals.behavioral.layout import Layout
from evals.behavioral.records import CodexAccounting, InvestigationRunRecord, write_new


class InvestigationTrialTests(unittest.TestCase):
    def test_luna_pricing_uses_the_conservative_long_context_tier(self) -> None:
        short = codex_driver.Usage(272_000, 0, 0, 1_000, 0)
        long = codex_driver.Usage(272_001, 0, 0, 1_000, 0)
        self.assertAlmostEqual(0.0277, codex_driver.turn_cost("gpt-6-luna", short))
        self.assertAlmostEqual(0.0551502, codex_driver.turn_cost("gpt-6-luna", long))

    def test_unknown_primary_or_reviewer_cost_prevents_another_paid_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = Layout(Path(temporary))
            directory = layout.root / "investigations" / "urgent-tuning" / "ordinary-1"
            directory.mkdir(parents=True)
            record = InvestigationRunRecord(
                schema="pinboard-investigation-run/v1",
                case_id="urgent-tuning",
                scenario_sha256="0" * 64,
                set_sha256="0" * 64,
                arm="ordinary",
                arm_sha256="0" * 64,
                export_commit="0" * 40,
                model="gpt-6-luna",
                reasoning_effort="high",
                cli_version="test",
                started_at="start",
                finished_at="finish",
                turns=[],
                accounting=CodexAccounting(
                    schema="pinboard-behavioral-codex-accounting/v1",
                    main_known_cost_usd=0.1,
                    main_usage_complete=False,
                    reviewer_usage=[],
                    reviewer_price_usd=None,
                ),
                outcome="failed",
                problem="incomplete usage",
            )
            write_new(directory / "run.json", record)
            self.assertIsNone(spend.Budget(layout, 120, processes.Window(None), False).reserve(spend.Category.CODEX_AGENT_RUN))

    def test_fresh_recovery_requires_a_successful_saved_file_read_in_the_trace(self) -> None:
        read = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "cat inquiry/evidence.json",
                    "aggregated_output": "saved facts",
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )
        self.assertTrue(investigation_trials.saved_evidence_read(read))
        self.assertFalse(investigation_trials.saved_evidence_read(read.replace('"exit_code": 0', '"exit_code": 1')))
        self.assertFalse(investigation_trials.saved_evidence_read(read.replace("cat inquiry/evidence.json", "ls inquiry")))


if __name__ == "__main__":
    unittest.main()
