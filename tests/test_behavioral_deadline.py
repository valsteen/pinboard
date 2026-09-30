"""One evaluation window bounds real effect owners; controlled clocks prove expiry without elapsed-time assertions."""

import signal
import subprocess
import tempfile
import unittest
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import TextIO
from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
from mcp.client.stdio import StdioServerParameters
from mcp_types import CallToolResult

from evals.behavioral import board, cli, credentials, export, oneshot, processes, runner, scoring, spend, substance
from evals.behavioral.layout import Layout
from evals.behavioral.records import (
    Assessed,
    AssessmentFailure,
    AssessmentRecord,
    CodexRunDetails,
    Completed,
    CoverageResult,
    ExportRecord,
    LabelMapping,
    ProbeRecord,
    RunKey,
    RunRecord,
    Runtime,
    Scored,
    ScorerInput,
    ScorerSession,
    ScoringFailure,
    SubstanceAnswer,
    SubstanceVerdict,
    TurnEvidence,
    TurnSubstance,
    write_new,
)


class EffectDeadlineTest(unittest.TestCase):
    def test_expired_window_prevents_subprocess_start_and_shortens_an_inflight_wait(self) -> None:
        window = processes.Window(103)
        with (
            patch.object(processes.time, "monotonic", return_value=103),
            patch.object(processes.subprocess, "Popen") as start,
        ):
            with self.assertRaises(TimeoutError):
                processes.run_tool(
                    processes.Tool.GIT, [], cwd=Path(), environment={}, stdin=None, timeout_seconds=300, window=window
                )
            start.assert_not_called()
        child = MagicMock()
        child.pid, child.returncode = 777, -9
        child.communicate.side_effect = [subprocess.TimeoutExpired("git", 3), (b"partial", b"diagnostic")]
        with (
            patch.object(processes.time, "monotonic", return_value=100),
            patch.object(processes.subprocess, "Popen") as start,
            patch.object(processes.os, "killpg") as kill,
        ):
            start.return_value.__enter__.return_value = child
            result = processes.run_tool(
                processes.Tool.GIT, [], cwd=Path(), environment={}, stdin=None, timeout_seconds=300, window=window
            )
        self.assertTrue(result.timed_out)
        self.assertEqual("partial", result.stdout)
        self.assertEqual(3, child.communicate.call_args_list[0].kwargs["timeout"])
        self.assertTrue(start.call_args.kwargs["start_new_session"])
        kill.assert_called_once_with(777, signal.SIGINT)

    def test_child_cleanup_precedes_credential_settlement_and_removal(self) -> None:
        order: list[str] = []
        child = MagicMock()
        child.pid, child.returncode = 777, 1
        child.communicate.side_effect = [subprocess.TimeoutExpired("codex", 3), (b"", b"")]

        detached_tool_alive = [True]

        def native_shutdown(selected: signal.Signals) -> None:
            self.assertEqual(signal.SIGINT, selected)
            detached_tool_alive[0] = False
            order.append("native shutdown")

        child.send_signal.side_effect = native_shutdown

        original = credentials.settle

        def settled(home: credentials.IsolatedHome) -> credentials.CredentialSettlement:
            self.assertFalse(detached_tool_alive[0])
            order.append("settle")
            return original(home)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "auth.json"
            source.write_bytes(b"{}")
            with (
                patch.object(credentials, "settle", side_effect=settled),
                patch.object(processes.subprocess, "Popen") as start,
                patch.object(processes.os, "killpg") as forced,
            ):
                start.return_value.__enter__.return_value = child
                with credentials.isolated_home(source, root, processes.Window(None)) as home:
                    processes.run_tool(
                        processes.Tool.CODEX,
                        [],
                        cwd=root,
                        environment={},
                        stdin=None,
                        timeout_seconds=3,
                        window=processes.Window(None),
                    )
                self.assertFalse(home.path.exists())
        self.assertEqual(["native shutdown", "settle"], order)
        forced.assert_not_called()

    def test_unconfirmed_native_shutdown_retains_home_and_prevents_credential_settlement(self) -> None:
        child = MagicMock()
        child.pid, child.returncode = 777, -9
        child.communicate.side_effect = [
            subprocess.TimeoutExpired("codex", 1),
            subprocess.TimeoutExpired("shutdown", 10),
            (b"partial", b"still uncertain"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "auth.json"
            source.write_bytes(b"{}")
            with (
                patch.object(processes.subprocess, "Popen") as start,
                patch.object(processes.os, "killpg") as force,
                patch.object(credentials, "settle") as settle,
            ):
                start.return_value.__enter__.return_value = child
                with (
                    self.assertRaises(processes.CleanupUnconfirmed) as failure,
                    credentials.isolated_home(source, root, processes.Window(None)) as home,
                ):
                    processes.run_tool(
                        processes.Tool.CODEX,
                        [],
                        cwd=root,
                        environment={},
                        stdin=None,
                        timeout_seconds=1,
                        window=processes.Window(None),
                    )
                self.assertTrue(home.path.exists())
                self.assertEqual(b"{}", source.read_bytes())
                self.assertIn(str(home.path), str(failure.exception))
                self.assertEqual("partial", failure.exception.stdout)
                settle.assert_not_called()
                child.send_signal.assert_called_once_with(signal.SIGINT)
                force.assert_called_once_with(777, signal.SIGKILL)

    def test_busy_credential_lock_observes_the_same_deadline_before_copying_a_login(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(credentials.fcntl, "flock", side_effect=BlockingIOError),
            patch.object(credentials.time, "sleep") as pause,
            patch.object(processes.time, "monotonic", side_effect=[100, 100, 101]),
        ):
            with (
                self.assertRaises(TimeoutError),
                credentials.exclusive_codex_session(Path(directory), processes.Window(101)),
            ):
                self.fail("expired lock must not enter the credential scope")
            pause.assert_called_once_with(0.05)

    def test_timed_out_oneshot_retains_partial_output_with_unknown_dollars(self) -> None:
        with patch.object(processes, "run_tool", return_value=processes.Completed(-9, "partial", "", True)) as run:
            answer = oneshot.ask("prompt", "scorer", processes.Window(123))
        self.assertIsNone(answer.cost_usd)
        self.assertEqual("partial", answer.stdout)
        self.assertEqual(123, run.call_args.kwargs["window"].deadline)


class PaidInterruptionTest(unittest.TestCase):
    def test_started_scorer_and_assessor_preserve_partial_output_and_unknown_usage(self) -> None:
        for owner in ("scorer", "assessor"):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as directory:
                layout = Layout(Path(directory))
                budget = spend.Budget(layout, 120, processes.Window(None), True)
                key = RunKey(scenario_id="s14-motivating-replay", variant="candidate", index=1)
                source = ScorerInput(
                    schema="pinboard-behavioral-scorer-input/v1",
                    run=key,
                    replies=["reply"] * 6,
                    states=[],
                    hooks_log=None,
                    redactions=[],
                )
                child = MagicMock()
                child.pid, child.returncode = 777, 0
                child.communicate.side_effect = [KeyboardInterrupt(), (b"paid partial bytes", b"diagnostic")]
                with (
                    patch.object(scoring, "prompt", return_value="prompt"),
                    patch.object(processes.subprocess, "Popen") as start,
                    patch.object(processes.os, "killpg"),
                ):
                    start.return_value.__enter__.return_value = child
                    with self.assertRaises(processes.ProcessInterrupted):
                        if owner == "scorer":
                            scoring.ScoringRun(layout, budget).score(source)
                        else:
                            record = MagicMock(spec=RunRecord)
                            record.run = key
                            with (
                                patch.object(substance, "prompt", return_value="prompt"),
                                patch.object(substance, "turn_words", return_value=[]),
                            ):
                                substance.assess_run(record, layout.assessment_directory(key), budget.window)
                self.assertTrue(spend.main_usage_unknown(layout))
                self.assertEqual(0, spend.total(spend.items(layout)))
                self.assertIsNone(budget.reserve(spend.Category.SCORER))
                raw = next(layout.root.rglob("raw.json"))
                self.assertEqual("paid partial bytes", raw.read_text())
                records = list(layout.scorer_sessions()) if owner == "scorer" else list(layout.assessments())
                self.assertEqual(1, len(records))
                self.assertIsNone(records[0].cost_usd)

    def test_prestart_refusal_does_not_invent_started_paid_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = Layout(Path(directory))
            budget = spend.Budget(layout, 120, processes.Window(None), True)
            source = ScorerInput(
                schema="pinboard-behavioral-scorer-input/v1",
                run=RunKey(scenario_id="s14-motivating-replay", variant="candidate", index=1),
                replies=[],
                states=[],
                hooks_log=None,
                redactions=[],
            )
            with (
                patch.object(scoring, "prompt", return_value="prompt"),
                patch.object(oneshot, "ask", side_effect=TimeoutError("expired before process start")),
                self.assertRaises(TimeoutError),
            ):
                scoring.ScoringRun(layout, budget).score(source)
            self.assertEqual([], list(layout.scorer_sessions()))
            self.assertFalse(spend.main_usage_unknown(layout))


class McpDeadlineTest(unittest.IsolatedAsyncioTestCase):
    async def test_initialization_and_calls_receive_the_selected_window_and_expiry_starts_no_server(self) -> None:
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.initialize = AsyncMock()
        session.call_tool = AsyncMock(
            return_value=CallToolResult(content=[], structured_content={"status": "committed"}, is_error=False)
        )

        @asynccontextmanager
        async def transport(_server: StdioServerParameters, *, errlog: TextIO) -> AsyncGenerator[tuple[None, None]]:
            self.assertFalse(errlog is None)
            yield (None, None)

        def bounded_wait(_seconds: float) -> nullcontext[None]:
            return nullcontext()

        window = processes.Window(105)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(processes.time, "monotonic", return_value=100),
            patch.object(board, "stdio_client", transport),
            patch.object(board, "ClientSession", return_value=session),
            patch.object(board.anyio, "fail_after", side_effect=bounded_wait) as bounded,
        ):
            async with board.connect(Path("launcher"), Path(directory) / "mcp.log", window) as client:
                result = await client.call("tool", board.Roots(project_root="p", work_root="w"), board.Status)
            self.assertEqual("committed", result.status)
            self.assertEqual([5, 5], [call.args[0] for call in bounded.call_args_list])
            session.initialize.assert_awaited_once()
        with patch.object(processes.time, "monotonic", return_value=105), patch.object(board, "stdio_client") as start:
            with self.assertRaises(export.SeedFailure):
                async with board.connect(Path("launcher"), Path("log"), window):
                    self.fail("no expired connection")
            start.assert_not_called()


class CoverageDeadlineTest(unittest.TestCase):
    def test_missing_both_target_scores_is_incomplete_even_when_iteration_returns_normally(self) -> None:
        evaluated = ExportRecord(
            schema="pinboard-behavioral-export/v1", commit="0" * 40, skills_sha256="0" * 64, plugin_root="/plugin"
        )
        observed = ProbeRecord(
            schema="pinboard-behavioral-probe/v1",
            name="isolation",
            runtime=Runtime.CODEX,
            description="controlled",
            cost_usd=0,
            passed=True,
            findings=[],
            inventory=[],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = cli.CoverageCodex(
                root,
                "0" * 40,
                root / "export",
                Path("evals/behavioral/data/scenario-sets/s13-s17.json"),
                "candidate",
                1,
                100,
                "gpt-6-sol",
                "high",
                root / "out",
                root / "worlds",
                120,
                True,
            )
            with (
                patch.object(runner, "require_codex_world_location"),
                patch.object(export, "export_revision", return_value=evaluated),
                patch.object(cli.probe, "probe_codex", return_value=observed),
                patch.object(runner, "run_codex", return_value=[]),
            ):
                self.assertEqual(2, cli.coverage_codex(command))
            result = msgspec.json.decode((command.out / "coverage.json").read_bytes(), type=CoverageResult)
            self.assertEqual("incomplete", result.status)
            self.assertEqual([], result.scored_runs)
            self.assertIn("s14-motivating-replay", result.reason)

    def test_malformed_score_or_assessment_is_retained_as_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = Layout(Path(directory))
            budget = spend.Budget(layout, 120, processes.Window(None), True)
            key = RunKey(scenario_id="s14-motivating-replay", variant="candidate", index=1)
            source = ScorerInput(
                schema="pinboard-behavioral-scorer-input/v1",
                run=key,
                replies=[],
                states=[],
                hooks_log=None,
                redactions=[],
            )
            write_new(layout.run_directory(key) / "scorer-input.json", source)
            with (
                patch.object(scoring.ScoringRun, "score", return_value=ScoringFailure(reason="malformed")),
                self.assertRaises(ValueError),
            ):
                cli.score_pending(layout, budget)
            write_new(
                layout.assessment_directory(key) / "assessment.json",
                AssessmentRecord(
                    schema="pinboard-behavioral-substance/v1",
                    run=key,
                    assessor_model="test",
                    cost_usd=0.01,
                    words=[],
                    outcome=AssessmentFailure(reason="malformed"),
                ),
            )
            with patch.object(substance, "assess", return_value=[]), self.assertRaises(ValueError):
                cli.assess_pending(layout, budget)
            self.assertIsInstance(next(layout.assessments()).outcome, AssessmentFailure)

    def test_export_probe_run_scoring_and_assessment_share_one_window_and_preserve_completed_evidence(self) -> None:
        stages: list[str] = []
        windows: list[processes.Window] = []
        clock = [100.0]

        def exported(_source: Path, revision: str, _destination: Path, window: processes.Window) -> ExportRecord:
            stages.append("export")
            windows.append(window)
            return ExportRecord(
                schema="pinboard-behavioral-export/v1", commit=revision, skills_sha256="0" * 64, plugin_root="/plugin"
            )

        def probed(
            _layout: Layout,
            budget: spend.Budget,
            _export: ExportRecord,
            _name: str,
            _model: str,
            _effort: str,
            _worlds: Path,
            _credentials: Path,
        ) -> ProbeRecord:
            stages.append("probe")
            windows.append(budget.window)
            return ProbeRecord(
                schema="pinboard-behavioral-probe/v1",
                name="isolation",
                runtime=Runtime.CODEX,
                description="test",
                cost_usd=0.01,
                passed=True,
                findings=[],
                inventory=[],
            )

        def run(plan: runner.CodexPlan, budget: spend.Budget) -> list[str]:
            stages.append("run")
            windows.append(plan.run.window)
            self.assertIs(budget.window, plan.run.window)
            _scenario, key = plan.run.planned()[0]
            directory = plan.run.layout.run_directory(key)
            turn = TurnEvidence(
                index=1,
                human="action",
                hook_ran=None,
                session_id="thread",
                final_reply="done",
                commentary=[],
                started_at="start",
                finished_at="finish",
                cost_usd=0.1,
                uncached_input_tokens=1,
                cached_input_tokens=0,
                cache_write_input_tokens=0,
                output_tokens=1,
                reasoning_output_tokens=0,
                permission_denials=[],
            )
            write_new(
                directory / "run.json",
                RunRecord(
                    schema="pinboard-behavioral-run/v2",
                    run=key,
                    runtime=Runtime.CODEX,
                    cli_version="test",
                    model="gpt-6-sol",
                    details=CodexRunDetails(
                        reasoning_effort="high",
                        permission_profile="pinboard",
                        sandbox_mode="workspace-write",
                        approval_policy="on-request",
                        writable_roots=[],
                        price_source="test",
                        credential_write_back=False,
                    ),
                    evaluated=plan.run.export,
                    fixture_difference=None,
                    seeded_host_id="host",
                    seeded_items=[],
                    observed_host_ids=[],
                    inventory=[],
                    turns=[turn],
                    started_at="start",
                    finished_at="finish",
                    outcome=Completed(),
                ),
            )
            write_new(
                directory / "scorer-input.json",
                ScorerInput(
                    schema="pinboard-behavioral-scorer-input/v1",
                    run=key,
                    replies=["done"],
                    states=[],
                    hooks_log=None,
                    redactions=[],
                ),
            )
            return []

        def score(session: scoring.ScoringRun, source: ScorerInput) -> Scored:
            stages.append("score")
            windows.append(session.budget.window)
            write_new(
                session.layout.score_directory("T1") / "session.json",
                ScorerSession(
                    schema="pinboard-behavioral-scorer-session/v1",
                    label="T1",
                    scorer_model="test",
                    checklist_sha256="0" * 64,
                    cost_usd=0.01,
                    outcome=Scored(),
                ),
            )
            write_new(
                session.layout.label_file("T1"),
                LabelMapping(schema="pinboard-behavioral-label/v1", label="T1", run=source.run),
            )
            return Scored()

        def assess(layout: Layout, budget: spend.Budget) -> list[str]:
            stages.append("assess")
            windows.append(budget.window)
            record = next(layout.run_records())
            write_new(
                layout.assessment_directory(record.run) / "assessment.json",
                AssessmentRecord(
                    schema="pinboard-behavioral-substance/v1",
                    run=record.run,
                    assessor_model="test",
                    cost_usd=0.01,
                    words=[],
                    outcome=Assessed(
                        answer=SubstanceAnswer(
                            label="S1",
                            turns=[
                                TurnSubstance(
                                    turn=1, verdict=SubstanceVerdict.FINAL_REPLY_CARRIES_SUBSTANCE, evidence="done"
                                )
                            ],
                        )
                    ),
                ),
            )
            clock[0] = 200.0
            return []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "out"
            command = cli.CoverageCodex(
                root,
                "0" * 40,
                root / "export",
                Path("evals/behavioral/data/scenario-sets/s13-s17.json"),
                "candidate",
                6,
                100,
                "gpt-6-sol",
                "high",
                out,
                root / "worlds",
                120,
                True,
            )
            with (
                patch.object(runner, "require_codex_world_location"),
                patch.object(processes.time, "monotonic", side_effect=lambda: clock[0]),
                patch.object(cli.time, "monotonic", side_effect=lambda: clock[0]),
                patch.object(export, "export_revision", side_effect=exported),
                patch.object(cli.probe, "probe_codex", side_effect=probed),
                patch.object(runner, "run_codex", side_effect=run),
                patch.object(scoring.ScoringRun, "score", score),
                patch.object(substance, "assess", side_effect=assess),
            ):
                self.assertEqual(2, cli.coverage_codex(command))
            result = msgspec.json.decode((out / "coverage.json").read_bytes(), type=CoverageResult)
            self.assertEqual("incomplete", result.status)
            self.assertEqual(1, len(result.completed_runs))
            self.assertEqual(1, len(result.scored_runs))
            self.assertEqual(1, len(result.assessed_runs))
            self.assertIsNone(result.total_cost_usd)
        self.assertEqual(["export", "probe", "run", "score", "assess"], stages)
        self.assertTrue(all(window is windows[0] for window in windows))


if __name__ == "__main__":
    unittest.main()
