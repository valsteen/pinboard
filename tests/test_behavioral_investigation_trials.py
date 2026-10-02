"""No-cost checks for investigation trial accounting and recovery evidence."""

import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.behavioral import (
    claude_driver,
    cli,
    codex_driver,
    investigation,
    investigation_trials,
    oneshot,
    processes,
    spend,
)
from evals.behavioral.layout import Layout
from evals.behavioral.records import (
    ClaudeInvestigationRunRecord,
    CodexAccounting,
    ExportRecord,
    InvestigationRunRecord,
    write_new,
)


class InvestigationTrialTests(unittest.TestCase):
    def test_haiku_route_uses_two_fresh_sessions_one_world_and_records_actual_cost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = Layout(root / "out")
            calls: list[list[str]] = []
            actual_run_tool = processes.run_tool

            def controlled(tool: processes.Tool, arguments: list[str], **kwargs: object) -> processes.Completed:
                if tool is processes.Tool.GIT:
                    return actual_run_tool(tool, arguments, **kwargs)  # type: ignore[arg-type]
                self.assertIs(tool, processes.Tool.CLAUDE)
                self.assertNotIn("--effort", arguments)
                self.assertNotIn("--thinking", arguments)
                self.assertEqual(arguments[arguments.index("--tools") + 1], "Bash,Read,Write,Edit,Glob,Grep")
                self.assertIn("--session-id", arguments)
                self.assertNotIn("--resume", arguments)
                calls.append(arguments)
                identity = arguments[arguments.index("--session-id") + 1]
                inquiry = kwargs["cwd"]
                assert isinstance(inquiry, Path)
                brief = inquiry / "kiosk-brief.md"
                events: list[dict[str, object]] = []
                if len(calls) == 1:
                    brief.write_text("Saved brief with source window 09:00-09:30Z\n")
                else:
                    self.assertIn("09:00-09:30Z", brief.read_text())
                    events.extend(
                        [
                            {
                                "type": "assistant",
                                "message": {
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "id": "read-brief",
                                            "name": "Bash",
                                            "input": {"command": "cat kiosk-brief.md"},
                                        }
                                    ]
                                },
                            },
                            {
                                "type": "user",
                                "message": {
                                    "content": [
                                        {
                                            "type": "tool_result",
                                            "tool_use_id": "read-brief",
                                            "is_error": False,
                                            "content": brief.read_text(),
                                        }
                                    ]
                                },
                            },
                        ]
                    )
                events.append(
                    {
                        "type": "result",
                        "subtype": "success",
                        "session_id": identity,
                        "is_error": False,
                        "total_cost_usd": 0.5,
                        "usage": {
                            "input_tokens": 100,
                            "cache_read_input_tokens": 20,
                            "cache_creation_input_tokens": 10,
                            "output_tokens": 30,
                        },
                        "permission_denials": [],
                        "result": "Complete brief",
                    }
                )
                return processes.Completed(0, "\n".join(json.dumps(event) for event in events) + "\n", "", False)

            exported = ExportRecord("pinboard-behavioral-export/v1", "0" * 40, "0" * 64, str(root))
            with (
                patch.object(claude_driver.processes, "run_tool", side_effect=controlled),
                patch.object(claude_driver, "claude_version", return_value="test"),
                patch.object(claude_driver.ClaudeSession, "isolation_findings", return_value=[]),
            ):
                records = investigation_trials.run_claude(
                    layout,
                    cli.investigation_budget(layout, "haiku"),
                    exported,
                    investigation.DATA / "sets" / "heldout.json",
                    "bounded-heldout",
                    "guidance",
                    1,
                    root / "worlds",
                )
            self.assertEqual(2, len(calls))
            self.assertNotEqual(
                calls[0][calls[0].index("--session-id") + 1], calls[1][calls[1].index("--session-id") + 1]
            )
            self.assertTrue(all(record.outcome == "completed" for record in records))
            self.assertEqual(records[0].world, records[1].world)
            self.assertTrue(records[1].turn and records[1].turn.saved_evidence_read)
            self.assertEqual(1.0, spend.total(spend.items(layout)))
            self.assertEqual(
                2, sum(isinstance(record, ClaudeInvestigationRunRecord) for record in layout.investigation_runs())
            )

    def test_haiku_unknown_or_overshot_cost_stops_before_second_paid_session(self) -> None:
        for reported_cost in (None, 16.0):
            with self.subTest(reported_cost=reported_cost), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                layout = Layout(root / "out")
                inquiry = root / "world" / "inquiry"
                inquiry.mkdir(parents=True)
                calls = 0

                def controlled(
                    _tool: processes.Tool, arguments: list[str], cost: float | None = reported_cost, **_kwargs: object
                ) -> processes.Completed:
                    nonlocal calls
                    calls += 1
                    result: dict[str, object] = {
                        "type": "result",
                        "subtype": "success",
                        "session_id": arguments[arguments.index("--session-id") + 1],
                        "is_error": False,
                        "usage": {
                            "input_tokens": 100,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                            "output_tokens": 10,
                        },
                        "permission_denials": [],
                        "result": "brief",
                    }
                    if cost is not None:
                        result["total_cost_usd"] = cost
                    return processes.Completed(0, json.dumps(result) + "\n", "", False)

                exported = ExportRecord("pinboard-behavioral-export/v1", "0" * 40, "0" * 64, str(root))
                with (
                    patch.object(
                        investigation_trials, "prepare_world", return_value=(root / "world", "0" * 64, "0" * 64)
                    ),
                    patch.object(claude_driver.processes, "run_tool", side_effect=controlled),
                    patch.object(claude_driver, "claude_version", return_value="test"),
                    patch.object(claude_driver.ClaudeSession, "isolation_findings", return_value=[]),
                ):
                    records = investigation_trials.run_claude(
                        layout,
                        cli.investigation_budget(layout, "haiku"),
                        exported,
                        investigation.DATA / "sets" / "heldout.json",
                        "bounded-heldout",
                        "guidance",
                        1,
                        root / "worlds",
                    )
                self.assertEqual(1, calls)
                self.assertEqual(1, len(records))
                self.assertEqual(reported_cost, records[0].cost_usd)
                self.assertIsNotNone(records[0].problem if reported_cost is None else records[0].cost_usd)
                self.assertEqual(reported_cost or 0.0, spend.total(spend.items(layout)))
                if reported_cost is None:
                    self.assertTrue(spend.main_usage_unknown(layout))
                    with self.assertRaisesRegex(ValueError, "unknown"):
                        cli.investigation_budget(layout, "next")
                else:
                    with self.assertRaisesRegex(ValueError, "batch overshot"):
                        cli.investigation_budget(layout, "next")

    def test_reviewer_choice_is_explicit_in_each_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = root / "plugin"
            marketplace = plugin / ".agents" / "plugins" / "marketplace.json"
            marketplace.parent.mkdir(parents=True)
            marketplace.write_text('{"name":"test-marketplace"}')
            with patch.object(codex_driver, "codex", return_value=processes.Completed(0, "", "", False)):
                for reviewer in ("auto_review", "user"):
                    home = root / reviewer
                    home.mkdir()
                    codex_driver.write_config(home, plugin, "gpt-6-luna", "high", reviewer, processes.Window(None))
                    config = tomllib.loads((home / "config.toml").read_text())
                    self.assertEqual(reviewer, config["approvals_reviewer"])

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
            self.assertIsNone(
                spend.Budget(layout, 120, processes.Window(None), False).reserve(spend.Category.CODEX_AGENT_RUN)
            )

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
        self.assertTrue(
            investigation_trials.saved_evidence_read(read.replace("cat inquiry/evidence.json", "cat inquiry.md"))
        )
        self.assertTrue(
            investigation_trials.saved_evidence_read(read.replace("cat inquiry/evidence.json", "cat inquiry-note.md"))
        )
        self.assertFalse(investigation_trials.saved_evidence_read(read.replace('"exit_code": 0', '"exit_code": 1')))
        self.assertFalse(
            investigation_trials.saved_evidence_read(read.replace("cat inquiry/evidence.json", "ls inquiry"))
        )

    def test_busy_codex_lock_leaves_no_started_run_or_unknown_cost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = Layout(root / "out")
            exported = ExportRecord("pinboard-behavioral-export/v1", "0" * 40, "0" * 64, str(root))
            with (
                patch.object(investigation_trials.runner, "require_codex_world_location"),
                patch.object(
                    investigation_trials.credentials,
                    "exclusive_codex_session",
                    side_effect=TimeoutError("another evaluation owns the lock"),
                ),
                self.assertRaises(TimeoutError),
            ):
                investigation_trials.run(
                    layout,
                    spend.Budget(layout, 15, processes.Window(None), False),
                    exported,
                    investigation.DATA / "sets" / "tuning.json",
                    "urgent-tuning",
                    "ordinary",
                    1,
                    root / "worlds",
                )
            self.assertFalse((layout.root / "investigations").exists())
            self.assertEqual([], spend.items(layout))

    def test_invalid_assessor_cost_is_unknown_and_blocks_further_paid_work(self) -> None:
        for payload in (
            '{"is_error":false,"result":"ok"}',
            '{"is_error":false,"total_cost_usd":-0.01,"result":"ok"}',
        ):
            with (
                self.subTest(payload=payload),
                patch.object(
                    oneshot.processes,
                    "run_tool",
                    return_value=processes.Completed(0, payload, "", False),
                ),
            ):
                answer = oneshot.ask("prompt", "claude-opus-5-5", processes.Window(None))
                self.assertIsNone(answer.cost_usd)
                self.assertIsNotNone(answer.problem)

    def test_batch_overshoot_cannot_be_reset_with_another_batch_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = Layout(Path(temporary))
            first = cli.investigation_budget(layout, "first")
            self.assertEqual(15.0, first.cap_usd)
            directory = layout.root / "investigations" / "urgent-tuning" / "ordinary-1"
            write_new(
                directory / "run.json",
                InvestigationRunRecord(
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
                        main_known_cost_usd=16.0,
                        main_usage_complete=True,
                        reviewer_usage=[],
                        reviewer_price_usd=None,
                    ),
                    outcome="completed",
                    problem=None,
                ),
            )
            with self.assertRaisesRegex(ValueError, "batch overshot"):
                cli.investigation_budget(layout, "second")
            self.assertFalse((layout.root / "batches" / "second.json").exists())
            write_new(
                layout.root / "batches" / "second.json",
                investigation_trials.Batch(schema="pinboard-investigation-batch/v1", start_usd=16.0, cap_usd=15.0),
            )
            with self.assertRaisesRegex(ValueError, "batch overshot"):
                cli.investigation_budget(layout, "third")

    def test_investigation_assessor_obtains_the_shared_session_lock_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = Layout(Path(temporary))
            budget = spend.Budget(layout, 15, processes.Window(None), False)
            with (
                patch.object(
                    investigation_trials.credentials,
                    "exclusive_codex_session",
                    side_effect=TimeoutError("another evaluation owns the lock"),
                ),
                self.assertRaises(TimeoutError),
            ):
                investigation_trials.assess(
                    layout,
                    budget,
                    investigation.DATA / "sets" / "tuning.json",
                    Path(temporary),
                    "urgent-tuning",
                    "ordinary",
                    1,
                )
            self.assertFalse((layout.root / "investigation-assessments").exists())


if __name__ == "__main__":
    unittest.main()
