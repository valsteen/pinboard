"""The startup pilot reuses verified private cases, native observations and bounded session admission."""

import datetime
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Self, override
from unittest.mock import Mock, patch

import msgspec

from evals.behavioral import (
    cli,
    codex_driver,
    export,
    oneshot,
    processes,
    runner,
    scenarios,
    scoring,
    spend,
    startup_pilot,
)
from evals.behavioral.layout import Layout
from evals.behavioral.records import (
    ChecklistItem,
    CodexAccounting,
    CodexRunDetails,
    Completed,
    ExportRecord,
    ProbeRecord,
    Runtime,
    encode,
    write_new,
)
from tests.test_behavioral_decision import fixture_set, record_score
from tests.test_behavioral_scoring import answer
from tests.test_behavioral_spend import record_scorer, turn


class FixedDateTime(datetime.datetime):
    @classmethod
    @override
    def now(cls, tz: datetime.tzinfo | None = None) -> Self:
        return cls(2030, 1, 1, 1, tzinfo=tz or datetime.UTC)


class ExpiredDateTime(datetime.datetime):
    @classmethod
    @override
    def now(cls, tz: datetime.tzinfo | None = None) -> Self:
        return cls(2030, 1, 1, 3, tzinfo=tz or datetime.UTC)


class StartupPilotTest(unittest.TestCase):
    def test_continuation_refuses_an_assessment_from_another_model_before_any_purchase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected, digest = self.retained(root)
            layout = Layout(root)
            budget = spend.Budget(layout, 30, processes.Window(None), True)
            for arm in ("baseline", "candidate"):
                plan = runner.RunPlan(
                    layout,
                    root / "worlds",
                    export.load_export(root / "exports" / arm),
                    arm,
                    selected,
                    1,
                    1,
                    startup_pilot.MODEL,
                    processes.Window(None),
                )
                self.record_task(runner.CodexPlan(plan, "high", root), budget)
            with patch.object(oneshot, "ask", side_effect=self.response):
                for pending in layout.scorer_inputs():
                    scoring.ScoringRun(layout, budget, startup_pilot.DESCRIPTIVE_MODEL).score(pending)
                self.assertEqual([], startup_pilot.substance.assess(layout, budget, startup_pilot.DESCRIPTIVE_MODEL))
            assessment = next(layout.assessments())
            path = layout.assessment_directory(assessment.run) / "assessment.json"
            path.write_bytes(encode(msgspec.structs.replace(assessment, assessor_model="claude-opus-5-5")))
            originals = {file: file.read_bytes() for file in root.rglob("*") if file.is_file()}
            with (
                patch.object(startup_pilot, "load_set", return_value=selected),
                patch.object(startup_pilot.runner, "require_codex_world_location"),
                patch.object(startup_pilot.datetime, "datetime", FixedDateTime),
                patch.object(startup_pilot.time, "monotonic", return_value=100),
                patch.object(startup_pilot.runner, "run_codex") as running,
                patch.object(oneshot, "ask") as paid,
            ):
                self.assertEqual(
                    2, startup_pilot.continue_run(root, "a" * 40, "b" * 40, root / "set", root, root / "worlds", digest)
                )
            running.assert_not_called()
            paid.assert_not_called()
            result = msgspec.json.decode((root / "continuation.json").read_bytes(), type=startup_pilot.PilotResult)
            self.assertIn("retained assessment model", result.reason)
            self.assertIn(str(path), result.reason)
            for file, content in originals.items():
                self.assertEqual(content, file.read_bytes())

    def test_cli_selects_the_retained_pilot_route_and_exact_window(self) -> None:
        arguments = [
            "startup-pilot-continue",
            "--source",
            "/source",
            "--baseline-revision",
            "a" * 40,
            "--candidate-revision",
            "b" * 40,
            "--scenario-set",
            "/set",
            "--out",
            "/out",
            "--worlds",
            "/worlds",
            "--window-sha256",
            "c" * 64,
        ]
        command = cli.decode(arguments)
        self.assertIsInstance(command, cli.ContinueStartupPilot)
        with patch.object(startup_pilot, "continue_run", return_value=2) as continuing:
            self.assertEqual(2, cli.dispatch(command))
        self.assertEqual("c" * 64, continuing.call_args.args[-1])
        stopped = cli.decode(
            [
                "startup-pilot-continue-stopped",
                *arguments[1:],
                "--previous-result-sha256",
                "d" * 64,
                "--result",
                "/out/fresh.json",
            ]
        )
        self.assertIsInstance(stopped, cli.ContinueStoppedStartupPilot)
        with patch.object(startup_pilot, "continue_stopped_run", return_value=2) as continuing_stopped:
            self.assertEqual(2, cli.dispatch(stopped))
        self.assertEqual(("d" * 64, Path("/out/fresh.json")), continuing_stopped.call_args.args[-2:])

    def retained(self, root: Path) -> tuple[scenarios.RegisteredSet, str]:
        return self.retained_cases(root, ["s1"])

    def retained_cases(self, root: Path, ids: list[str]) -> tuple[scenarios.RegisteredSet, str]:
        selected = fixture_set(ids, 1, (ChecklistItem.P9,))
        for arm, revision in (("baseline", "a" * 40), ("candidate", "b" * 40)):
            directory = root / "exports" / arm
            plugin = directory / "plugin"
            (plugin / "skills").mkdir(parents=True)
            (plugin / ".pinboard-runtime").mkdir()
            (plugin / ".pinboard-runtime" / ".pinboard-ready").write_text("ready")
            evaluated = ExportRecord(
                "pinboard-behavioral-export/v1", revision, export.skills_sha256(plugin), str(plugin)
            )
            write_new(directory / "export.json", evaluated)
            write_new(
                Layout(root).probe_file(arm),
                ProbeRecord(
                    "pinboard-behavioral-probe/v1",
                    arm,
                    Runtime.CODEX,
                    "controlled passing isolation probe",
                    0.01,
                    True,
                    [],
                    runner.codex_probe_inventory("controlled", evaluated, startup_pilot.MODEL, startup_pilot.EFFORT),
                ),
            )
        write_new(
            root / "window.json",
            startup_pilot.PilotWindow(
                "pinboard-startup-pilot-window/v1",
                "2030-01-01T00:00:00+00:00",
                "2030-01-01T02:00:00+00:00",
                7200,
                30,
                str(root),
                "a" * 40,
                "b" * 40,
                selected.registration.scenarios,
                selected.registration.targeted_rules,
            ),
        )
        write_new(
            root / "pilot.json",
            startup_pilot.PilotResult(
                "pinboard-startup-pilot-result/v1",
                "incomplete",
                "retained original stop",
                0,
                True,
                True,
                [],
                "inconclusive",
            ),
        )
        return selected, hashlib.sha256((root / "window.json").read_bytes()).hexdigest()

    def record_task(self, plan: runner.CodexPlan, _budget: spend.Budget) -> list[str]:
        scenario, key = plan.run.planned()[0]
        state = runner.start(plan.run, scenario, key)
        state.turns.append(msgspec.structs.replace(turn(1, 0.01), human=scenario.turns[0].human))
        state.finish(
            Runtime.CODEX,
            "controlled",
            CodexRunDetails("high", "pinboard", "workspace-write", "auto_review", [], "fixture", False),
            [],
            [],
            Completed(),
        )
        raw = []
        for identity, model in (("owner", "gpt-5.6-terra"), ("approval", "codex-auto-review")):
            raw.extend(
                [
                    {
                        "type": "turn_context",
                        "payload": {
                            "turn_id": identity,
                            "model": model,
                            "effort": "high" if identity == "owner" else "low",
                        },
                    },
                    {
                        "type": "token_usage_record",
                        "payload": {
                            "thread_id": identity,
                            "turn_id": identity,
                            "response_id": identity,
                            "thread_token_usage": {
                                "input_tokens": 100,
                                "cached_input_tokens": 0,
                                "cache_write_input_tokens": 0,
                                "output_tokens": 10,
                                "reasoning_output_tokens": 1,
                            },
                        },
                    },
                ]
            )
        rollout = "\n".join(json.dumps(record) for record in raw)
        (state.directory / "rollout.jsonl").write_text(rollout)
        write_new(
            state.directory / "accounting.json", codex_driver.rollout_accounting(rollout, startup_pilot.MODEL, True)
        )
        write_new(state.directory / "workflow.json", startup_pilot.observe(rollout))
        return []

    def response(self, text: str, model: str, _window: processes.Window) -> oneshot.Answer:
        self.assertEqual("claude-sonnet-5-5", model)
        match = re.search(r"Label: (\w+)", text)
        assert match is not None
        label = match[1]
        body = (
            answer(label, [1])
            if "blind evaluator" in text
            else "```json\n"
            + json.dumps(
                {
                    "label": label,
                    "turns": [{"turn": 1, "verdict": "final-reply-carries-substance", "evidence": "answer"}],
                }
            )
            + "\n```"
        )
        return oneshot.Answer(0.01, body, "{}", None)

    def test_continuation_reuses_purchases_and_accounting_without_replacing_originals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected, digest = self.retained(root)
            layout = Layout(root)
            evaluated = export.load_export(root / "exports" / "baseline")
            plan = runner.RunPlan(
                layout,
                root / "worlds",
                evaluated,
                "baseline",
                selected,
                1,
                1,
                startup_pilot.MODEL,
                processes.Window(None),
            )
            budget = spend.Budget(layout, 30, processes.Window(None), True)
            self.record_task(runner.CodexPlan(plan, "high", root), budget)
            with patch.object(oneshot, "ask", side_effect=self.response):
                scoring.ScoringRun(layout, budget, startup_pilot.DESCRIPTIVE_MODEL).score(next(layout.scorer_inputs()))
                substance_result = startup_pilot.substance.assess(layout, budget, startup_pilot.DESCRIPTIVE_MODEL)
                self.assertEqual([], substance_result)
            originals = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            with (
                patch.object(startup_pilot, "load_set", return_value=selected),
                patch.object(startup_pilot.runner, "require_codex_world_location"),
                patch.object(startup_pilot.datetime, "datetime", FixedDateTime),
                patch.object(startup_pilot.time, "monotonic", return_value=100),
                patch.object(startup_pilot.export, "export_revision") as exporting,
                patch.object(startup_pilot.probe, "probe_codex") as probing,
                patch.object(startup_pilot.runner, "run_codex", side_effect=self.record_task) as running,
                patch.object(oneshot, "ask", side_effect=self.response) as paid,
            ):
                self.assertEqual(
                    0, startup_pilot.continue_run(root, "a" * 40, "b" * 40, root / "set", root, root / "worlds", digest)
                )
            exporting.assert_not_called()
            probing.assert_not_called()
            self.assertEqual(1, running.call_count)
            self.assertEqual("candidate", running.call_args.args[0].run.variant)
            self.assertEqual(3700, running.call_args.args[1].window.deadline)
            self.assertEqual(2, paid.call_count)
            for path, content in originals.items():
                self.assertEqual(content, path.read_bytes(), str(path))
            outcome = msgspec.json.decode((root / "continuation.json").read_bytes(), type=startup_pilot.PilotResult)
            self.assertEqual("completed", outcome.status)
            self.assertFalse(outcome.unreported_usage)
            self.assertTrue(outcome.unknown_reviewer_cost)
            self.assertGreater(outcome.known_cost_usd, 0.04)

    def refuse_purchase(self, _plan: runner.CodexPlan, budget: spend.Budget) -> list[str]:
        if budget.reserve(spend.Category.CODEX_AGENT_RUN) is not None:
            self.fail("unexpected purchase")
        return ["not admitted"]

    def stopped_result(self, root: Path) -> str:
        layout = Layout(root)
        write_new(
            root / "continuation.json",
            startup_pilot.PilotResult(
                "pinboard-startup-pilot-result/v1",
                "incomplete",
                "projected fit refused",
                spend.total(spend.items(layout)),
                False,
                any(a.reviewer_usage for _, a in layout.codex_accounting()),
                [r.run.display() for r in layout.run_records() if isinstance(r.outcome, Completed)],
                "inconclusive",
            ),
        )
        return hashlib.sha256((root / "continuation.json").read_bytes()).hexdigest()

    def test_stopped_continuation_reuses_six_runs_and_buys_only_four_missing_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected, digest = self.retained_cases(root, ["s1", "s2", "s3", "s4", "s5"])
            layout = Layout(root)
            budget = spend.PilotBudget(layout, 30, processes.Window(None), True)
            for scenario in selected.scenarios[:3]:
                for arm in ("baseline", "candidate"):
                    plan = runner.RunPlan(
                        layout,
                        root / "worlds",
                        export.load_export(root / "exports" / arm),
                        arm,
                        scenarios.RegisteredSet((scenario,), selected.registration, selected.scenario_sources),
                        1,
                        1,
                        startup_pilot.MODEL,
                        processes.Window(None),
                    )
                    self.record_task(runner.CodexPlan(plan, "high", root), budget)
            with patch.object(oneshot, "ask", side_effect=self.response):
                for pending in layout.scorer_inputs():
                    scoring.ScoringRun(layout, budget, startup_pilot.DESCRIPTIVE_MODEL).score(pending)
                self.assertEqual([], startup_pilot.substance.assess(layout, budget, startup_pilot.DESCRIPTIVE_MODEL))
            previous = self.stopped_result(root)
            originals = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            with (
                patch.object(startup_pilot, "load_set", return_value=selected),
                patch.object(startup_pilot.runner, "require_codex_world_location"),
                patch.object(startup_pilot.datetime, "datetime", FixedDateTime),
                patch.object(startup_pilot.time, "monotonic", return_value=100),
                patch.object(startup_pilot.export, "export_revision") as exporting,
                patch.object(startup_pilot.probe, "probe_codex") as probing,
                patch.object(startup_pilot.runner, "run_codex", side_effect=self.record_task) as running,
                patch.object(oneshot, "ask", side_effect=self.response) as paid,
            ):
                self.assertEqual(
                    0,
                    startup_pilot.continue_stopped_run(
                        root,
                        "a" * 40,
                        "b" * 40,
                        root / "set",
                        root,
                        root / "worlds",
                        digest,
                        previous,
                        root / "recorded-spend.json",
                    ),
                )
            exporting.assert_not_called()
            probing.assert_not_called()
            self.assertEqual(4, running.call_count)
            self.assertEqual(8, paid.call_count)
            self.assertEqual(
                {"s4", "s5"}, {call.args[0].run.scenarios.scenarios[0].id for call in running.call_args_list}
            )
            for path, content in originals.items():
                self.assertEqual(content, path.read_bytes(), str(path))
            outcome = msgspec.json.decode((root / "recorded-spend.json").read_bytes(), type=startup_pilot.PilotResult)
            self.assertEqual("completed", outcome.status)
            self.assertEqual(10, len(outcome.completed_runs))
            self.assertFalse(outcome.unreported_usage)

    def test_stopped_continuation_rejects_changed_predecessor_occupied_output_and_unknown_usage(self) -> None:
        for defect in ("digest", "occupied", "accounting", "deadline", "scorer-model"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                selected, digest = self.retained(root)
                previous = self.stopped_result(root)
                destination = root / "new.json"
                if defect == "digest":
                    (root / "continuation.json").write_text("changed")
                elif defect == "occupied":
                    destination.write_text("existing")
                elif defect == "accounting":
                    write_new(
                        Layout(root).probe_file("baseline").parent / "accounting.json",
                        CodexAccounting("pinboard-behavioral-codex-accounting/v1", 0.01, False, [], None),
                    )
                elif defect == "scorer-model":
                    record_scorer(Layout(root), "wrong", 0)
                with (
                    patch.object(startup_pilot, "load_set", return_value=selected),
                    patch.object(startup_pilot.runner, "require_codex_world_location"),
                    patch.object(
                        startup_pilot.datetime, "datetime", ExpiredDateTime if defect == "deadline" else FixedDateTime
                    ),
                    patch.object(startup_pilot.time, "monotonic", return_value=100),
                    patch.object(startup_pilot.runner, "run_codex") as running,
                    patch.object(oneshot, "ask") as paid,
                ):
                    arguments = (
                        root,
                        "a" * 40,
                        "b" * 40,
                        root / "set",
                        root,
                        root / "worlds",
                        digest,
                        previous,
                        destination,
                    )
                    if defect in {"digest", "occupied"}:
                        with self.assertRaises(ValueError):
                            startup_pilot.continue_stopped_run(*arguments)
                    else:
                        self.assertEqual(2, startup_pilot.continue_stopped_run(*arguments))
                        outcome = msgspec.json.decode(destination.read_bytes(), type=startup_pilot.PilotResult)
                        self.assertEqual("incomplete", outcome.status)
                    running.assert_not_called()
                    paid.assert_not_called()

    def test_continuation_rejects_identity_drift_unknown_main_and_expired_window_before_purchases(self) -> None:
        for defect in ("digest", "revision", "scenario", "export", "probe", "main", "deadline", "cap"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                selected, digest = self.retained(root)
                baseline = "a" * 40
                if defect == "digest":
                    digest = "0" * 64
                elif defect == "revision":
                    baseline = "c" * 40
                elif defect == "scenario":
                    selected = fixture_set(["s2"], 1, (ChecklistItem.P9,))
                elif defect == "export":
                    path = root / "exports/baseline/export.json"
                    record = msgspec.json.decode(path.read_bytes(), type=ExportRecord)
                    path.write_bytes(encode(msgspec.structs.replace(record, commit="d" * 40)))
                elif defect == "probe":
                    path = Layout(root).probe_file("baseline")
                    record = msgspec.json.decode(path.read_bytes(), type=ProbeRecord)
                    path.write_bytes(encode(msgspec.structs.replace(record, passed=False)))
                elif defect == "main":
                    write_new(
                        Layout(root).probe_file("baseline").parent / "accounting.json",
                        CodexAccounting("pinboard-behavioral-codex-accounting/v1", 0, False, [], None),
                    )
                elif defect == "cap":
                    path = Layout(root).probe_file("baseline")
                    record = msgspec.json.decode(path.read_bytes(), type=ProbeRecord)
                    path.write_bytes(encode(msgspec.structs.replace(record, cost_usd=30.0)))
                with (
                    patch.object(startup_pilot, "load_set", return_value=selected),
                    patch.object(startup_pilot.runner, "require_codex_world_location"),
                    patch.object(
                        startup_pilot.datetime, "datetime", FixedDateTime if defect != "deadline" else ExpiredDateTime
                    ),
                    patch.object(startup_pilot.time, "monotonic", return_value=100),
                    patch.object(
                        startup_pilot.runner,
                        "run_codex",
                        side_effect=self.refuse_purchase,
                    ) as running,
                    patch.object(oneshot, "ask") as paid,
                ):
                    if defect in ("digest", "revision", "scenario"):
                        with self.assertRaises(ValueError):
                            startup_pilot.continue_run(
                                root, baseline, "b" * 40, root / "set", root, root / "worlds", digest
                            )
                    else:
                        result = startup_pilot.continue_run(
                            root, baseline, "b" * 40, root / "set", root, root / "worlds", digest
                        )
                        self.assertEqual(2, result)
                        outcome = msgspec.json.decode(
                            (root / "continuation.json").read_bytes(), type=startup_pilot.PilotResult
                        )
                        self.assertEqual("incomplete", outcome.status)
                        if defect != "cap":
                            running.assert_not_called()
                    paid.assert_not_called()

    def test_both_arms_use_sonnet_and_do_not_purchase_existing_pilot_scores_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out = root / "out"
            selected = fixture_set(["s1"], 1, (ChecklistItem.P9,))
            exported = ExportRecord("pinboard-behavioral-export/v1", "0" * 40, "0" * 64, str(root))

            def run(plan: runner.CodexPlan, budget: spend.Budget) -> list[str]:
                self.assertEqual(30, budget.cap_usd)
                key = plan.run.planned()[0][1]
                layout = Layout(out)
                record_score(layout, key.variant, key, [set()], True)
                record = layout.run_record(key)
                assert record is not None
                (layout.run_directory(key) / "run.json").write_bytes(
                    encode(msgspec.structs.replace(record, runtime=Runtime.CODEX))
                )
                (layout.run_directory(key) / "rollout.jsonl").write_text("")
                write_new(
                    layout.run_directory(key) / "accounting.json",
                    CodexAccounting("pinboard-behavioral-codex-accounting/v1", 0.01, True, [], None),
                )
                return []

            def respond(text: str, model: str, _window: processes.Window) -> oneshot.Answer:
                self.assertEqual("claude-sonnet-5-5", model)
                label = re.search(r"Label: (\w+)", text)
                assert label is not None
                name = label[1]
                if "blind evaluator" in text:
                    body = answer(name, [1])
                else:
                    body = (
                        "```json\n"
                        + json.dumps(
                            {
                                "label": name,
                                "turns": [{"turn": 1, "verdict": "final-reply-carries-substance", "evidence": "reply"}],
                            }
                        )
                        + "\n```"
                    )
                return oneshot.Answer(0.01, body, "{}", None)

            with (
                patch.object(startup_pilot, "load_set", return_value=selected),
                patch.object(startup_pilot.runner, "require_codex_world_location"),
                patch.object(startup_pilot.export, "export_revision", return_value=exported),
                patch.object(startup_pilot.probe, "probe_codex", return_value=Mock(passed=True)),
                patch.object(startup_pilot.runner, "run_codex", side_effect=run),
                patch.object(
                    startup_pilot,
                    "observe",
                    return_value=startup_pilot.WorkflowObservation(
                        "pinboard-native-workflow-observation/v1",
                        [startup_pilot.TurnConfiguration("owner", "gpt-5.6-terra", "high")],
                        [],
                        "observed-calls-only",
                    ),
                ),
                patch.object(oneshot, "ask", side_effect=respond) as paid,
            ):
                self.assertEqual(0, startup_pilot.run(root, "a", "b", root / "set.json", out, root / "worlds"))
            self.assertEqual(["claude-sonnet-5-5"] * 4, [call.args[1] for call in paid.call_args_list])
            self.assertEqual(2, len(scoring.valid_score_counts(Layout(out), "claude-sonnet-5-5")))
            self.assertEqual({"claude-sonnet-5-5"}, {record.assessor_model for record in Layout(out).assessments()})

    def test_unpriced_approval_reviewer_stops_before_actor_matching_or_another_paid_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out = root / "out"
            selected = fixture_set(["s1"], 1, (ChecklistItem.P9,))
            exported = ExportRecord("pinboard-behavioral-export/v1", "0" * 40, "0" * 64, str(root))

            def run(plan: runner.CodexPlan, _budget: spend.Budget) -> list[str]:
                directory = Layout(out).run_directory(plan.run.planned()[0][1])
                directory.mkdir(parents=True)
                (directory / "rollout.jsonl").write_text("")
                return []

            with (
                patch.object(startup_pilot, "load_set", return_value=selected),
                patch.object(startup_pilot.runner, "require_codex_world_location"),
                patch.object(startup_pilot.export, "export_revision", return_value=exported),
                patch.object(startup_pilot.probe, "probe_codex", return_value=Mock(passed=True)),
                patch.object(startup_pilot.runner, "run_codex", side_effect=run) as running,
                patch.object(
                    startup_pilot,
                    "observe",
                    return_value=startup_pilot.WorkflowObservation(
                        "pinboard-native-workflow-observation/v1",
                        [
                            startup_pilot.TurnConfiguration("owner", "gpt-5.6-terra", "high"),
                            startup_pilot.TurnConfiguration("approval", "codex-auto-review", "low"),
                        ],
                        [],
                        "observed-calls-only",
                    ),
                ),
                patch.object(
                    startup_pilot.spend,
                    "comparison_reasons",
                    return_value=["comparison reviewer dollar cost is unknown"],
                ),
                patch.object(oneshot, "ask") as paid,
            ):
                self.assertEqual(2, startup_pilot.run(root, "a", "b", root / "set.json", out, root / "worlds"))
            self.assertEqual(1, running.call_count)
            paid.assert_not_called()
            result = msgspec.json.decode((out / "pilot.json").read_bytes(), type=startup_pilot.PilotResult)
            self.assertEqual("comparison reviewer dollar cost is unknown", result.reason)

    def test_task_matching_keeps_auxiliary_approval_context_visible_and_distinct(self) -> None:
        owner = startup_pilot.TurnConfiguration("owner", "gpt-5.6-terra", "high")
        approval = startup_pilot.TurnConfiguration("approval", "codex-auto-review", "low")
        for contexts, matches in (
            ([owner, approval], True),
            ([approval], False),
            ([msgspec.structs.replace(owner, effort="low"), approval], False),
        ):
            with self.subTest(contexts=contexts):
                observation = startup_pilot.WorkflowObservation(
                    "pinboard-native-workflow-observation/v1", contexts, [], "observed-calls-only"
                )
                with tempfile.TemporaryDirectory() as temporary:
                    if matches:
                        startup_pilot.validate_workflow(Layout(Path(temporary)), observation, False)
                    else:
                        with self.assertRaisesRegex(ValueError, "task actor configuration"):
                            startup_pilot.validate_workflow(Layout(Path(temporary)), observation, False)
                self.assertEqual(contexts, observation.configurations)

    def private_set(self, root: Path) -> Path:
        original = scenarios.DATA / "scenario-sets" / "review-after-merge.json"
        registration = msgspec.json.decode(original.read_bytes(), type=scenarios.ScenarioSet)
        (root / "scenario-sets").mkdir()
        (root / "scenarios").mkdir()
        target = root / "scenario-sets" / "private.json"
        target.write_bytes(original.read_bytes())
        for member in registration.scenarios:
            (root / "scenarios" / f"{member.id}.json").write_bytes(
                (scenarios.DATA / "scenarios" / f"{member.id}.json").read_bytes()
            )
        return target

    def test_private_registered_sources_are_verified_at_the_selected_set_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = self.private_set(root)
            registered = scenarios.load_set(selected)
            scenario = registered.scenarios[0]
            self.assertEqual(
                hashlib.sha256(registered.content(scenario)).hexdigest(), registered.registration.scenarios[0].sha256
            )
            (root / "scenarios" / f"{scenario.id}.json").write_text("changed")
            with self.assertRaises(scenarios.DataIntegrityError):
                scenarios.load_set(selected)

    def test_failed_probe_preserves_deadline_cap_and_prevents_trial_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = self.private_set(root)
            out = root / "out"
            exported = ExportRecord("pinboard-behavioral-export/v1", "0" * 40, "0" * 64, str(root))
            with (
                patch.object(startup_pilot.runner, "require_codex_world_location"),
                patch.object(startup_pilot.time, "monotonic", return_value=100),
                patch.object(startup_pilot.export, "export_revision", return_value=exported) as exporting,
                patch.object(startup_pilot.probe, "probe_codex", return_value=Mock(passed=False)) as probing,
                patch.object(startup_pilot.runner, "run_codex") as running,
            ):
                self.assertEqual(2, startup_pilot.run(root, "a", "b", selected, out, root / "worlds"))
            running.assert_not_called()
            self.assertEqual(1, exporting.call_count)
            budget = probing.call_args.args[1]
            self.assertEqual(7300, budget.window.deadline)
            self.assertEqual(30, budget.cap_usd)
            self.assertFalse(budget.allow_unknown_reviewer_price)
            result = msgspec.json.decode((out / "pilot.json").read_bytes(), type=startup_pilot.PilotResult)
            self.assertEqual("incomplete", result.status)
            self.assertEqual("inconclusive", result.formal_verdict)
            self.assertEqual(0, len(list(Layout(out).run_records())))

    def test_observations_record_calls_without_inferring_delivery_and_missing_child_usage_stays_unknown(self) -> None:
        records = [
            {
                "type": "turn_context",
                "payload": {"turn_id": "owner", "model": "gpt-5.6-terra", "effort": "high", "additive": True},
            },
            {"type": "turn_context", "payload": {"turn_id": "child", "model": "gpt-5.6-terra", "effort": "high"}},
            {
                "type": "response_item",
                "payload": {"type": "function_call", "name": "spawn_agent", "arguments": "opaque"},
            },
            {
                "type": "token_usage_record",
                "payload": {
                    "thread_id": "o",
                    "turn_id": "owner",
                    "response_id": "r",
                    "thread_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 0,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 10,
                        "reasoning_output_tokens": 5,
                    },
                },
            },
        ]
        rollout = "\n".join(json.dumps(r) for r in records)
        observed = startup_pilot.observe(rollout)
        self.assertEqual(["spawn_agent"], observed.native_call_names)
        self.assertEqual({"owner", "child"}, {c.turn_id for c in observed.configurations})
        self.assertEqual("observed-calls-only", observed.evidence)
        self.assertFalse(codex_driver.rollout_accounting(rollout, "gpt-5.6-terra", True).main_usage_complete)
        payload = records[-1]["payload"]
        assert isinstance(payload, dict)
        usage = dict(payload)
        usage.update(thread_id="c", turn_id="child", response_id="child-r")
        complete = rollout + "\n" + json.dumps({"type": "token_usage_record", "payload": usage})
        accounting = codex_driver.rollout_accounting(complete, "gpt-5.6-terra", True)
        self.assertTrue(accounting.main_usage_complete)
        self.assertAlmostEqual(0.00064, accounting.main_known_cost_usd)
