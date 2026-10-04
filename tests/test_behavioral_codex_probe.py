"""A matching prior probe is required at the shared paid-run boundary, independently of budget and isolation."""

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from evals.behavioral import codex_driver, probe, runner
from evals.behavioral.layout import Layout
from evals.behavioral.processes import Completed as ProcessResult
from evals.behavioral.processes import Window
from evals.behavioral.records import Completed, ExportRecord, InventoryEntry, ProbeRecord, RunKey, Runtime, write_new
from evals.behavioral.scenarios import RegisteredSet, load_set
from evals.behavioral.spend import Budget


class PriorProbeTest(unittest.TestCase):
    def test_each_run_observes_version_and_drift_stops_before_an_agent_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            plan = self.plan(root, 2)
            self.fake_effects(stack, root)
            stack.enter_context(mock.patch.object(codex_driver, "codex_version", return_value="codex-cli test-a"))
            record = probe.probe_codex(
                plan.run.layout,
                Budget(plan.run.layout, 120, Window(None), False),
                plan.run.export,
                "before",
                plan.run.model,
                plan.reasoning_effort,
                plan.run.worlds,
                plan.credential_source,
            )
            assert record is not None
            self.assertTrue(record.passed)
            paid = stack.enter_context(mock.patch.object(runner, "codex_turns", return_value=Completed()))
            with (
                mock.patch.object(codex_driver, "codex_version", side_effect=["codex-cli test-a", "codex-cli test-b"]),
                self.assertRaises(codex_driver.CodexUnavailableError),
            ):
                runner.run_codex(plan, Budget(plan.run.layout, 120, Window(None), False))
            self.assertEqual(1, paid.call_count)
            records = list(plan.run.layout.run_records())
            self.assertEqual(["codex-cli test-a"], [run.cli_version for run in records])
            second = plan
            stack.enter_context(mock.patch.object(codex_driver, "codex_version", return_value="codex-cli test-b"))
            probe.probe_codex(
                second.run.layout,
                Budget(second.run.layout, 120, Window(None), False),
                second.run.export,
                "changed",
                second.run.model,
                second.reasoning_effort,
                second.run.worlds,
                second.credential_source,
            )
            runner.run_codex(second, Budget(second.run.layout, 120, Window(None), False))
            self.assertEqual(2, paid.call_count)

    def test_missing_failed_and_mismatching_probes_never_reach_an_agent_turn(self) -> None:
        for case in ("missing", "failed", "version", "export", "model", "effort", "missing-version"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
                root = Path(temporary)
                plan = self.plan(root, 1)
                self.fake_effects(stack, root)
                stack.enter_context(mock.patch.object(codex_driver, "codex_version", return_value="codex-cli test-a"))
                if case != "missing":
                    record = probe.probe_codex(
                        plan.run.layout,
                        Budget(plan.run.layout, 120, Window(None), False),
                        plan.run.export,
                        "original",
                        plan.run.model,
                        plan.reasoning_effort,
                        plan.run.worlds,
                        plan.credential_source,
                    )
                    assert record is not None
                    entries = record.inventory
                    if case in {"version", "export", "model", "effort", "missing-version"}:
                        name = {
                            "version": "cli-version",
                            "export": "export-commit",
                            "model": "model",
                            "effort": "reasoning-effort",
                            "missing-version": "cli-version",
                        }[case]
                        entries = [
                            InventoryEntry(kind=e.kind, name=e.name, source="different") if e.name == name else e
                            for e in entries
                            if case != "missing-version" or e.name != name
                        ]
                    plan.run.layout.probe_file("original").unlink()
                    write_new(
                        plan.run.layout.probe_file("altered"),
                        ProbeRecord(
                            schema=record.schema,
                            name="altered",
                            runtime=Runtime.CODEX,
                            description=record.description,
                            cost_usd=0,
                            passed=case != "failed",
                            findings=[],
                            inventory=entries,
                        ),
                    )
                paid = stack.enter_context(mock.patch.object(runner, "codex_turns", return_value=Completed()))
                with self.assertRaises(codex_driver.CodexUnavailableError):
                    runner.codex_run(plan, mock.Mock(), RunKey(scenario_id="probe", variant="test", index=1))
                paid.assert_not_called()

    def test_passing_probe_does_not_bypass_budget_or_isolation(self) -> None:
        for condition in ("budget", "isolation"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
                root = Path(temporary)
                plan = self.plan(root, 1)
                self.fake_effects(stack, root)
                stack.enter_context(mock.patch.object(codex_driver, "codex_version", return_value="codex-cli test-a"))
                probe.probe_codex(
                    plan.run.layout,
                    Budget(plan.run.layout, 120, Window(None), False),
                    plan.run.export,
                    "before",
                    plan.run.model,
                    plan.reasoning_effort,
                    plan.run.worlds,
                    plan.credential_source,
                )
                paid = stack.enter_context(mock.patch.object(runner, "codex_turns", return_value=Completed()))
                if condition == "budget":
                    self.assertEqual(
                        [f"{plan.run.scenarios.scenarios[0].id}/test-1"],
                        runner.run_codex(plan, Budget(plan.run.layout, 0, Window(None), False)),
                    )
                else:
                    with (
                        mock.patch.object(codex_driver, "isolation_findings", return_value=["foreign skill"]),
                        self.assertRaises(runner.IsolationBreachError),
                    ):
                        runner.run_codex(plan, Budget(plan.run.layout, 120, Window(None), False))
                paid.assert_not_called()

    def test_missing_failed_or_timed_out_version_observation_is_rejected(self) -> None:
        for completed in (
            ProcessResult(1, "codex-cli test", "failed", False),
            ProcessResult(0, "", "", False),
            ProcessResult(0, "codex-cli test", "", True),
        ):
            with (
                self.subTest(completed=completed),
                mock.patch.object(codex_driver.processes, "run_tool", return_value=completed),
                self.assertRaises(codex_driver.CodexUnavailableError),
            ):
                codex_driver.codex_version(Window(None))

    def plan(self, root: Path, runs: int) -> runner.CodexPlan:
        worlds = root / "worlds"
        worlds.mkdir()
        export = ExportRecord(
            schema="pinboard-behavioral-export/v1",
            commit="1" * 40,
            skills_sha256="2" * 64,
            plugin_root=str(root / "plugin"),
        )
        registered = load_set(Path("evals/behavioral/data/scenario-sets/s13-s17.json"))
        plan = runner.RunPlan(
            layout=Layout(root / "out"),
            worlds=worlds,
            export=export,
            variant="test",
            scenarios=RegisteredSet(registered.scenarios[:1], registered.registration, registered.scenario_sources),
            first_index=1,
            runs=runs,
            model="gpt-6-sol",
            window=Window(None),
        )
        return runner.CodexPlan(plan, "high", root / "auth.json")

    def fake_effects(self, stack: ExitStack, root: Path) -> None:
        home = mock.Mock()
        stack.enter_context(mock.patch.object(runner, "require_codex_world_location"))
        stack.enter_context(mock.patch.object(probe, "require_codex_world_location"))
        home.path = root / "home"
        home.settlement = None
        isolated = stack.enter_context(mock.patch.object(runner.credentials, "isolated_home"))
        isolated.return_value.__enter__.return_value = home
        stack.enter_context(mock.patch.object(runner.credentials, "exclusive_codex_session"))
        stack.enter_context(mock.patch.object(runner.world, "create_project"))
        stack.enter_context(mock.patch.object(runner.world, "init_board"))
        stack.enter_context(mock.patch.object(runner.world, "build_world", return_value=(mock.Mock(), [])))
        stack.enter_context(mock.patch.object(runner.RunState, "snapshot"))
        stack.enter_context(mock.patch.object(codex_driver, "write_config"))
        context = codex_driver.LoadedContext(
            entries=[], sandbox_mode="test", approval_policy="never", writable_roots=[], prompt_text="synthetic"
        )
        stack.enter_context(mock.patch.object(codex_driver, "loaded_context", return_value=context))
        stack.enter_context(mock.patch.object(codex_driver, "isolation_findings", return_value=[]))
        stack.enter_context(mock.patch.object(probe, "served_tools", return_value=[]))
        stack.enter_context(mock.patch.object(probe, "probe_thread", return_value=(0, [])))


if __name__ == "__main__":
    unittest.main()
