"""No-cost checks for investigation trial accounting and recovery evidence."""

import json
import tempfile
import tomllib
import unittest
from contextlib import nullcontext
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
from tests.test_behavioral_claude_stream import result_event


class InvestigationTrialTests(unittest.TestCase):
    def test_actual_codex_trial_retains_opposite_recovery_observations_for_blind_assessment(self) -> None:
        prompts: list[str] = []
        for recovered in (True, False):
            with self.subTest(recovered=recovered), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                layout = Layout(root / "out")
                source = root / "auth.fixture"
                source.write_bytes(b"controlled credential")
                world = root / "world"
                (world / "inquiry").mkdir(parents=True)
                requested: list[str | None] = []

                def turn(
                    _home: Path,
                    _inquiry: Path,
                    previous: str | None,
                    _human: str,
                    raw_path: Path,
                    _window: processes.Window,
                    requested: list[str | None] = requested,
                    recovered: bool = recovered,
                ) -> tuple[processes.Completed, codex_driver.TurnReading]:
                    requested.append(previous)
                    identity = "private-new-id" if recovered and len(requested) == 3 else "private-old-id"
                    raw = (
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "command_execution",
                                    "command": "cat inquiry-note.md",
                                    "aggregated_output": "saved facts",
                                    "exit_code": 0 if recovered else 1,
                                    "status": "completed",
                                },
                            }
                        )
                        + "\n"
                    )
                    raw_path.write_text(raw)
                    usage = codex_driver.Usage(100 * (2 if previous else 1), 0, 0, 10, 0)
                    return processes.Completed(0, raw, "", False), codex_driver.TurnReading(
                        identity,
                        ["identical final reply"],
                        usage,
                        [],
                        False,
                        False,
                        [],
                        [],
                    )

                exported = ExportRecord("pinboard-behavioral-export/v1", "1" * 40, "2" * 64, str(root))
                scenario_set = investigation.DATA / "sets" / "heldout.json"
                case = investigation.load_set(scenario_set)[1][0]
                accounting = CodexAccounting("pinboard-behavioral-codex-accounting/v1", 0.1, True, [], None)
                selected_model, selected_effort = "gpt-6-sol", "controlled-effort"
                with (
                    patch.object(investigation_trials, "INVESTIGATION_MODEL", selected_model),
                    patch.object(investigation_trials, "INVESTIGATION_EFFORT", selected_effort),
                    patch.object(investigation_trials.runner, "require_codex_world_location"),
                    patch.object(
                        investigation_trials.credentials, "exclusive_codex_session", return_value=nullcontext()
                    ),
                    patch.object(investigation_trials.credentials, "default_source", return_value=source),
                    patch.object(investigation_trials, "prepare_world", return_value=(world, "3" * 64, "4" * 64)),
                    patch.object(codex_driver, "codex_version", return_value="controlled-version"),
                    patch.object(codex_driver, "write_config") as config,
                    patch.object(
                        codex_driver,
                        "loaded_context",
                        return_value=codex_driver.LoadedContext([], "test", "test", [], "context"),
                    ),
                    patch.object(codex_driver, "isolation_findings", return_value=[]),
                    patch.object(codex_driver, "run_turn", side_effect=turn),
                    patch.object(codex_driver, "rollout_text", return_value=""),
                    patch.object(codex_driver, "rollout_accounting", return_value=accounting) as price,
                ):
                    record = investigation_trials.run(
                        layout,
                        spend.Budget(layout, 15, processes.Window(None), False),
                        exported,
                        scenario_set,
                        case.id,
                        "guidance",
                        1,
                        root / "worlds",
                    )
                self.assertIsNotNone(record)
                assert record is not None
                self.assertEqual("completed", record.outcome)
                self.assertEqual([None, "private-old-id", None], requested)
                self.assertEqual(selected_model, record.model)
                self.assertEqual(selected_effort, record.reasoning_effort)
                self.assertEqual(record.model, config.call_args.args[2])
                self.assertEqual(record.reasoning_effort, config.call_args.args[3])
                self.assertEqual(record.model, price.call_args.args[1])
                reloaded = next(iter(Layout(layout.root).investigation_runs()))
                self.assertEqual(record, reloaded)
                self.assertEqual(recovered, record.turns[-1].saved_evidence_read)
                self.assertTrue(all(turn.compaction_event is None for turn in record.turns))

                key = investigation.InvestigationKey(
                    "pinboard-investigation-key/v1", case.id, [], "impact", "lead", "choose", "gap", []
                )
                keys = root / "private-keys"
                write_new(
                    keys / "registry.json",
                    investigation_trials.PrivateKeys("pinboard-investigation-private-registry/v1", {case.id: "5" * 64}),
                )

                def assessor(
                    prompt: str, model: str, _window: processes.Window, record: InvestigationRunRecord = record
                ) -> oneshot.Answer:
                    prompts.append(prompt)
                    self.assertEqual(investigation_trials.ASSESSOR_MODEL, model)
                    for private in (
                        record.model,
                        record.reasoning_effort,
                        record.arm,
                        "private-old-id",
                        "private-new-id",
                    ):
                        self.assertNotIn(private, prompt)
                    return oneshot.Answer(0.05, "not JSON", "controlled raw", None)

                with (
                    patch.object(investigation_trials, "ASSESSOR_MODEL", "controlled-assessor"),
                    patch.object(
                        investigation_trials.credentials, "exclusive_codex_session", return_value=nullcontext()
                    ),
                    patch.object(investigation, "load_key", return_value=key),
                    patch.object(oneshot, "ask", side_effect=assessor),
                ):
                    assessed = investigation_trials.assess(
                        layout,
                        spend.Budget(layout, 15, processes.Window(None), False),
                        scenario_set,
                        keys,
                        case.id,
                        "guidance",
                        1,
                    )
                assert assessed is not None
                self.assertEqual("controlled-assessor", assessed.assessor_model)
        self.assertNotEqual(prompts[0], prompts[1])
        self.assertIn("session-2", prompts[0])
        self.assertNotIn("session-2", prompts[1])

    def test_haiku_raw_publication_failure_retains_known_session_cost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = Layout(root / "out")
            exported = ExportRecord("pinboard-behavioral-export/v1", "0" * 40, "0" * 64, str(root))
            original_write = Path.write_text

            def write(path: Path, data: str) -> int:
                if path.name == "turn.jsonl":
                    raise OSError("controlled raw-evidence write failure after paid process")
                return original_write(path, data)

            with (
                patch.object(investigation_trials, "prepare_world", return_value=(root / "world", "3" * 64, "4" * 64)),
                patch.object(claude_driver, "claude_version", return_value="controlled"),
                patch.object(
                    processes,
                    "run_tool",
                    return_value=processes.Completed(
                        0, json.dumps(result_event(0.5, subtype="success", is_error=False)), "", False
                    ),
                ) as paid,
                patch.object(Path, "write_text", write),
            ):
                records = investigation_trials.run_claude(
                    layout,
                    spend.Budget(layout, 15, processes.Window(None), False),
                    exported,
                    investigation.DATA / "sets" / "heldout.json",
                    "bounded-heldout",
                    "guidance",
                    1,
                    root / "worlds",
                )
            paid.assert_called_once()
            self.assertEqual(1, len(records))
            self.assertEqual("failed", records[0].outcome)
            self.assertEqual(0.5, records[0].cost_usd)
            self.assertEqual(records[0], next(iter(Layout(layout.root).investigation_runs())))
            self.assertAlmostEqual(0.5, spend.total(spend.items(Layout(layout.root))))

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
        for offset, rates in ((0, codex_driver.price("gpt-6-luna")), (1, codex_driver.LUNA_LONG_CONTEXT)):
            with self.subTest(offset=offset):
                tokens = codex_driver.LUNA_SHORT_CONTEXT_MAX_INPUT + offset
                usage = codex_driver.Usage(tokens, 30, 20, 1_000, 40)
                expected = (
                    (tokens - 50) * rates.uncached_input
                    + 30 * rates.cached_input
                    + 20 * rates.cache_write
                    + 1_000 * rates.output
                ) / 1_000_000
                self.assertAlmostEqual(expected, codex_driver.turn_cost("gpt-6-luna", usage))

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
