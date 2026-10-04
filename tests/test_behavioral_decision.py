"""The behavioral harness's comparison arithmetic and improved / no worse / inconclusive decision rule."""

import hashlib
import json
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import Mock, patch

import msgspec

from evals.behavioral import claude_driver, decision, processes, runner, scoring, spend
from evals.behavioral.compatibility_records import CompatibilityRunRecord
from evals.behavioral.decision import Classification, Criteria, Estimate, RunFailures
from evals.behavioral.export import SeedFailure
from evals.behavioral.layout import SCORER_INPUT, Layout
from evals.behavioral.records import (
    ChecklistItem,
    ClaudeRunDetails,
    CodexAccounting,
    CodexRunDetails,
    Completed,
    ExportRecord,
    Failed,
    ItemVerdict,
    ItemVerdicts,
    LabelMapping,
    RegisteredScenario,
    ReplyScore,
    RunKey,
    RunRecord,
    Runtime,
    Scenario,
    ScenarioSet,
    Scored,
    ScoreRecord,
    ScorerSession,
    ScoringFailure,
    SeededItem,
    Stopped,
    Turn,
    Verdict,
    WorldKind,
    encode,
    write_new,
)
from evals.behavioral.scenarios import CHECKLIST_SHA256, DataIntegrityError, RegisteredSet
from tests.test_behavioral_claude_stream import result_event
from tests.test_behavioral_spend import turn

CRITERIA = Criteria(
    minimum_runs_per_scenario=3,
    minimum_runs_per_side=3,
    interval_multiplier=2.0,
    no_worse_margin_total=1.0,
    no_worse_margin_item=0.5,
    sd_floor_total=0.5,
    sd_floor_item=0.2,
)


def runs(scenario: str, variant: str, totals: list[float]) -> list[RunFailures]:
    return [
        RunFailures(
            run=RunKey(scenario_id=scenario, variant=variant, index=index),
            scores=1,
            per_item={item: (total if item is ChecklistItem.P3 else 0.0) for item in ChecklistItem},
        )
        for index, total in enumerate(totals, start=1)
    ]


def verdicts(failing: set[str]) -> ItemVerdicts:
    def one(name: str) -> ItemVerdict:
        return ItemVerdict(verdict=Verdict.FAIL if name in failing else Verdict.PASS, evidence="")

    return ItemVerdicts(
        p1=one("P1"),
        p2=one("P2"),
        p3=one("P3"),
        p4=one("P4"),
        p5=one("P5"),
        p6=one("P6"),
        p7=one("P7"),
        p8=one("P8"),
        p9=one("P9"),
        p10=one("P10"),
        p11=one("P11"),
        p12=one("P12"),
        n1=one("N1"),
    )


def record_score(layout: Layout, label: str, run: RunKey, replies: list[set[str]], valid: bool) -> None:
    if layout.run_record(run) is None:
        selected = fixture_set([f"s{number}" for number in range(1, 6)], len(replies), (ChecklistItem.P9,))
        scenario = next(member for member in selected.scenarios if member.id == run.scenario_id)
        plan = runner.RunPlan(
            layout,
            layout.root / "worlds",
            ExportRecord(
                schema="pinboard-behavioral-export/v1",
                commit=("a" if run.variant == "base" else "b") * 40,
                skills_sha256="c" * 64,
                plugin_root=str(layout.root / run.display() / "export"),
            ),
            run.variant,
            selected,
            run.index,
            1,
            "controlled-model",
            processes.Window(None),
        )
        state = runner.start(plan, scenario, run)
        state.turns.extend(
            msgspec.structs.replace(turn(index, 0.01), human=scripted.human, final_reply="reply")
            for index, scripted in enumerate(scenario.turns, start=1)
        )
        state.finish(
            runner.Runtime.CLAUDE_CODE,
            "controlled-clean",
            ClaudeRunDetails(permission_mode="bypassPermissions", cost_basis="claude total_cost_usd"),
            [],
            [],
            Completed(),
        )
    score = ScoreRecord(
        label=label,
        replies=[ReplyScore(turn=turn, items=verdicts(failing)) for turn, failing in enumerate(replies, start=1)],
        scenario_pass=False,
        failed_items=[],
        permission_denial_suspected=False,
    )
    directory = layout.score_directory(label)
    write_new(directory / "score.json", score)
    write_new(
        directory / "session.json",
        ScorerSession(
            schema="pinboard-behavioral-scorer-session/v1",
            label=label,
            scorer_model=scoring.SCORER_MODEL,
            checklist_sha256=CHECKLIST_SHA256,
            cost_usd=0.0,
            outcome=Scored() if valid else ScoringFailure(reason="malformed"),
        ),
    )
    write_new(layout.label_file(label), LabelMapping(schema="pinboard-behavioral-label/v1", label=label, run=run))


def fixture_set(ids: list[str], turns: int, targeted: tuple[ChecklistItem, ...]) -> RegisteredSet:
    scenarios = tuple(
        Scenario(
            id=name,
            title="controlled scenario",
            world=WorldKind.MINIMAL,
            world_extra=None,
            source="independent fixture",
            ground_truth="controlled answer",
            turns=[Turn(human=f"question {index}", before=None) for index in range(1, turns + 1)],
        )
        for name in ids
    )
    contents = tuple(encode(scenario) for scenario in scenarios)
    registered = ScenarioSet(
        name="controlled",
        scenarios=[
            RegisteredScenario(id=scenario.id, sha256=hashlib.sha256(content).hexdigest())
            for scenario, content in zip(scenarios, contents, strict=True)
        ],
        targeted_rules=list(targeted),
    )
    return RegisteredSet(scenarios, registered, contents)


class EstimateTest(unittest.TestCase):
    def test_too_few_runs_per_scenario_is_inconclusive_even_when_the_candidate_looks_better(self) -> None:
        baseline = runs("a", "b", [9.0, 9.0])
        candidate = runs("a", "c", [1.0, 1.0])
        result = decision.estimate(CRITERIA, baseline, candidate, ["a"], None)
        self.assertIs(Classification.INCONCLUSIVE, result.classification)
        self.assertLess(result.upper, 0)

    def test_consistent_reduction_with_enough_runs_is_improved(self) -> None:
        baseline = runs("a", "b", [8.0, 9.0, 10.0]) + runs("x", "b", [6.0, 7.0, 8.0])
        candidate = runs("a", "c", [2.0, 3.0, 4.0]) + runs("x", "c", [1.0, 2.0, 3.0])
        result = decision.estimate(CRITERIA, baseline, candidate, ["a", "x"], None)
        self.assertIs(Classification.IMPROVED, result.classification)
        self.assertAlmostEqual(-5.5, result.difference)

    def test_equal_sides_within_the_margin_are_no_worse(self) -> None:
        baseline = runs("a", "b", [3.0] * 6)
        candidate = runs("a", "c", [3.0] * 6)
        result = decision.estimate(CRITERIA, baseline, candidate, ["a"], None)
        self.assertIs(Classification.NO_WORSE, result.classification)
        self.assertLessEqual(result.upper, CRITERIA.no_worse_margin_total)

    def test_noisy_sides_whose_interval_exceeds_the_margin_are_inconclusive(self) -> None:
        baseline = runs("a", "b", [0.0, 10.0, 5.0])
        candidate = runs("a", "c", [0.0, 12.0, 6.0])
        result = decision.estimate(CRITERIA, baseline, candidate, ["a"], None)
        self.assertIs(Classification.INCONCLUSIVE, result.classification)
        self.assertGreater(result.upper, CRITERIA.no_worse_margin_total)

    def test_the_standard_deviation_never_falls_below_its_floor(self) -> None:
        baseline = runs("a", "b", [4.0] * 3)
        candidate = runs("a", "c", [4.0] * 3)
        result = decision.estimate(CRITERIA, baseline, candidate, ["a"], None)
        self.assertEqual(0.0, result.pooled_sd)
        self.assertGreater(result.upper - result.lower, 0)

    def test_scenarios_are_weighted_equally_rather_than_by_run_count(self) -> None:
        baseline = runs("a", "b", [4.0] * 3) + runs("x", "b", [0.0] * 3)
        candidate = runs("a", "c", [4.0] * 9) + runs("x", "c", [2.0] * 3)
        result = decision.estimate(CRITERIA, baseline, candidate, ["a", "x"], None)
        self.assertAlmostEqual(1.0, result.difference)

    def test_fewer_scenarios_need_more_runs_each_for_the_same_runs_per_side(self) -> None:
        self.assertEqual(6, decision.required_runs(decision.CRITERIA, 5))
        self.assertEqual(10, decision.required_runs(decision.CRITERIA, 3))
        self.assertEqual(6, decision.required_runs(decision.CRITERIA, 8))

    def test_six_runs_on_three_scenarios_are_too_few_under_the_criteria(self) -> None:
        scenarios = ["a", "x", "y"]
        baseline = [run for s in scenarios for run in runs(s, "b", [3.0] * 6)]
        candidate = [run for s in scenarios for run in runs(s, "c", [3.0] * 6)]
        result = decision.estimate(decision.CRITERIA, baseline, candidate, scenarios, None)
        self.assertIs(Classification.INCONCLUSIVE, result.classification)
        self.assertIn("fewer than 10 runs", result.reason)


def classified(classification: Classification) -> Estimate:
    return Estimate(0.0, -1.0, 1.0, 0.0, 1.0, classification, classification.value)


class OverallTest(unittest.TestCase):
    def test_without_a_targeted_rule_the_total_alone_decides(self) -> None:
        self.assertIs(Classification.IMPROVED, decision.overall({}, classified(Classification.IMPROVED))[0])
        self.assertIs(Classification.NO_WORSE, decision.overall({}, classified(Classification.NO_WORSE))[0])
        self.assertIs(Classification.INCONCLUSIVE, decision.overall({}, classified(Classification.INCONCLUSIVE))[0])

    def test_improved_targeted_rules_with_a_no_worse_total_are_improved(self) -> None:
        targeted = {ChecklistItem.P9: classified(Classification.IMPROVED)}
        self.assertIs(Classification.IMPROVED, decision.overall(targeted, classified(Classification.NO_WORSE))[0])

    def test_a_targeted_rule_that_is_only_no_worse_makes_the_comparison_no_worse(self) -> None:
        targeted = {
            ChecklistItem.P9: classified(Classification.IMPROVED),
            ChecklistItem.P12: classified(Classification.NO_WORSE),
        }
        self.assertIs(Classification.NO_WORSE, decision.overall(targeted, classified(Classification.IMPROVED))[0])

    def test_an_inconclusive_targeted_rule_or_total_makes_the_comparison_inconclusive(self) -> None:
        unclear_rule = {ChecklistItem.P9: classified(Classification.INCONCLUSIVE)}
        verdict, reason = decision.overall(unclear_rule, classified(Classification.IMPROVED))
        self.assertIs(Classification.INCONCLUSIVE, verdict)
        self.assertIn("P9", reason)
        improved_rule = {ChecklistItem.P9: classified(Classification.IMPROVED)}
        self.assertIs(
            Classification.INCONCLUSIVE, decision.overall(improved_rule, classified(Classification.INCONCLUSIVE))[0]
        )


class CompareTest(unittest.TestCase):
    def test_unlinked_scores_cannot_qualify_a_comparison(self) -> None:
        scenarios = [f"s{number}" for number in range(1, 6)]
        with tempfile.TemporaryDirectory() as directory:
            layout = Layout(Path(directory))
            for scenario in scenarios:
                for index in range(1, decision.required_runs(decision.CRITERIA, len(scenarios)) + 1):
                    for variant in ("base", "cand"):
                        record_score(
                            layout,
                            f"{variant}-{scenario}-{index}",
                            RunKey(scenario_id=scenario, variant=variant, index=index),
                            [set()],
                            True,
                        )
            for record in layout.run_records():
                (layout.run_directory(record.run) / "run.json").unlink()
            output = decision.compare(layout, fixture_set(scenarios, 1, ()).registration, "base", "cand")
        self.assertIn("decision: inconclusive", output)

    def test_rates_count_failing_replies_per_run_average_repeat_scores_and_skip_failed_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = Layout(Path(directory))
            first = RunKey(scenario_id="s1", variant="base", index=1)
            second = RunKey(scenario_id="s1", variant="base", index=2)
            record_score(layout, "T1", first, [{"P3", "N1"}, {"P3"}], valid=True)
            record_score(layout, "T2", first, [{"P3"}, set()], valid=True)
            record_score(layout, "T3", second, [{"P11"}, set()], valid=True)
            record_score(layout, "T4", second, [{"P1", "P2", "P4"}, set()], valid=False)
            report = decision.report(layout, ["s1"], "base")
        rows = {line.split("\t")[0]: line.split("\t") for line in report.splitlines()}
        self.assertEqual("0.75", rows["P3"][1])
        self.assertEqual("0.25", rows["N1"][1])
        self.assertEqual("0.5", rows["P11"][1])
        self.assertEqual("0", rows["P1"][1])
        self.assertEqual("1.5", rows["total"][1])
        self.assertEqual("2", rows["runs"][1])

    def test_output_does_not_depend_on_the_order_records_were_written(self) -> None:
        outputs = []
        for order in ([1, 2, 3], [3, 1, 2]):
            with tempfile.TemporaryDirectory() as directory:
                layout = Layout(Path(directory))
                for index in order:
                    record_score(
                        layout, f"B{index}", RunKey(scenario_id="s1", variant="base", index=index), [{"P9"}], True
                    )
                    record_score(
                        layout,
                        f"C{index}",
                        RunKey(scenario_id="s1", variant="cand", index=index),
                        [set() if index > 1 else {"P9"}],
                        True,
                    )
                outputs.append(
                    decision.compare(layout, fixture_set(["s1"], 1, (ChecklistItem.P9,)).registration, "base", "cand")
                )
        self.assertEqual(outputs[0], outputs[1])

    def test_an_advisory_item_warns_without_deciding_while_targeted_rules_and_total_decide(self) -> None:
        scenarios = [f"s{number}" for number in range(1, 6)]
        with tempfile.TemporaryDirectory() as directory:
            layout = Layout(Path(directory))
            for scenario in scenarios:
                for index in range(1, 7):
                    base = RunKey(scenario_id=scenario, variant="base", index=index)
                    cand = RunKey(scenario_id=scenario, variant="cand", index=index)
                    record_score(layout, f"B-{scenario}-{index}", base, [{"P9"}], valid=True)
                    record_score(layout, f"C-{scenario}-{index}", cand, [{"P5"}], valid=True)
            output = decision.compare(
                layout, fixture_set(scenarios, 1, (ChecklistItem.P9,)).registration, "base", "cand"
            )
        lines = output.splitlines()
        rows = {line.split("\t")[0]: line.split("\t") for line in lines}
        self.assertEqual("targeted", rows["P9"][1])
        self.assertEqual("advisory", rows["P5"][1])
        self.assertIn("decision: improved", output)
        warnings = next(line for line in lines if line.startswith("advisory warnings:"))
        self.assertIn("P5", warnings)
        self.assertNotIn("P9", warnings)

    def test_a_scenario_or_side_without_scored_runs_has_no_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = Layout(Path(directory))
            record_score(layout, "T1", RunKey(scenario_id="s1", variant="base", index=1), [{"P3"}], valid=True)
            report = decision.report(layout, ["s1", "s2"], "base")
            comparison = decision.compare(layout, fixture_set(["s1", "s2"], 1, ()).registration, "base", "cand")
        rows = {line.split("\t")[0]: line.split("\t") for line in report.splitlines()}
        self.assertEqual(["P3", "1", "1", "none"], rows["P3"])
        self.assertIn("no scored runs: s2", report)
        compared = {line.split("\t")[0]: line.split("\t") for line in comparison.splitlines()}
        self.assertEqual(["P3", "advisory", "1", "none", "n/a"], compared["P3"][:5])

    def test_numbers_round_half_away_from_zero_like_the_prior_pass_bar(self) -> None:
        self.assertEqual("0.67", decision.number(4 / 6))
        self.assertEqual("1.3", decision.number(13 / 10))
        self.assertEqual("10", decision.number(10.0))
        self.assertEqual("0.13", decision.number(0.125))


class QualificationTest(unittest.TestCase):
    def prepare(self, layout: Layout) -> ScenarioSet:
        ids = [f"s{number}" for number in range(1, 6)]
        for name in ids:
            for index in range(1, decision.required_runs(decision.CRITERIA, len(ids)) + 1):
                for variant in ("base", "cand"):
                    record_score(layout, f"{variant}-{name}-{index}", RunKey(name, variant, index), [set()], True)
        return fixture_set(ids, 1, (ChecklistItem.P9,)).registration

    def test_fresh_producer_records_reload_and_qualify_different_exports_and_per_run_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = Layout(Path(directory))
            registration = self.prepare(layout)
            fresh = Layout(layout.root)
            reloaded = list(fresh.run_records())
            self.assertTrue(all(isinstance(run, RunRecord) for run in reloaded))
            first = reloaded[0]
            assert isinstance(first, RunRecord)
            self.assertEqual(registration, first.registration)
            self.assertIn("decision: no worse", decision.compare(fresh, registration, "base", "cand"))

    def test_claude_effect_failures_preserve_started_spending_and_distinguish_prepaid_failures(self) -> None:
        known = json.dumps(result_event(1.25, subtype="success", is_error=False))
        expensive = json.dumps(result_event(spend.COMPARISON_CAP_USD + 1, subtype="success", is_error=False))
        cases = (
            (None, None, False, None),
            ('{"type":"assistant"}\n', None, True, None),
            ('{"type":', None, True, None),
            ('{"type":"assistant"}\n', "turn-1.jsonl", True, None),
            (known, "turn-1.jsonl", True, 1.25),
            (known, "turn-1.stderr", True, 1.25),
            (expensive, "turn-1.jsonl", True, spend.COMPARISON_CAP_USD + 1),
            (known, "launch", False, None),
        )
        original_write = Path.write_text

        def write(failure_at: str | None, path: Path, data: str) -> int:
            if path.name == failure_at:
                raise OSError("controlled raw-evidence write failure after paid process")
            return original_write(path, data)

        for output, failure_at, started, cost in cases:
            with self.subTest(failure_at=failure_at, output=output), tempfile.TemporaryDirectory() as directory:
                layout = Layout(Path(directory))
                registration = self.prepare(layout)
                first = layout.run_record(RunKey("s1", "base", 1))
                assert isinstance(first, RunRecord)
                selected = fixture_set(
                    [member.id for member in registration.scenarios], 1, tuple(registration.targeted_rules)
                )
                plan = runner.RunPlan(
                    layout,
                    layout.root / "worlds",
                    first.evaluated,
                    "base",
                    selected,
                    99,
                    1,
                    first.model,
                    processes.Window(None),
                )
                seeded: list[SeededItem] = []
                with (
                    patch.object(claude_driver, "claude_version", return_value=first.cli_version),
                    patch.object(
                        runner.world,
                        "build_world",
                        return_value=(Mock(), seeded),
                        side_effect=None if output is not None else SeedFailure("failed before paid work"),
                    ),
                    patch.object(runner.RunState, "snapshot"),
                    patch.object(
                        claude_driver.processes,
                        "run_tool",
                        return_value=processes.Completed(0, output or "", "", False),
                        side_effect=OSError("failed before process start") if failure_at == "launch" else None,
                    ) as paid,
                    patch.object(Path, "write_text", partial(write, failure_at)),
                ):
                    failed = runner.claude_run(plan, selected.scenarios[0], RunKey("s1", "base", 99))
                reloaded = Layout(layout.root).run_record(failed.run)
                assert isinstance(reloaded, RunRecord)
                self.assertEqual(failed, reloaded)
                assert isinstance(reloaded.outcome, Failed)
                self.assertEqual(1 if output is not None else 0, paid.call_count)
                self.assertEqual(1 if started else 0, len(reloaded.turns))
                if started:
                    self.assertEqual(cost, reloaded.turns[0].cost_usd)
                    self.assertIn("turn 1", reloaded.outcome.stage)
                fresh = Layout(layout.root)
                if started and cost is None:
                    self.assertIn("incomplete main/scorer/assessor", "; ".join(spend.comparison_reasons(fresh)))
                    self.assertIn("decision: inconclusive", decision.compare(fresh, registration, "base", "cand"))
                elif cost is not None and cost > spend.COMPARISON_CAP_USD:
                    self.assertIn("spending exceeds", "; ".join(spend.comparison_reasons(fresh)))
                    self.assertIn("decision: inconclusive", decision.compare(fresh, registration, "base", "cand"))
                else:
                    self.assertEqual([], spend.comparison_reasons(fresh))
                    self.assertIn("decision: no worse", decision.compare(fresh, registration, "base", "cand"))
                self.assertEqual(1, scoring.valid_score_counts(Layout(layout.root))[first.run.display()])

    def test_each_qualification_defect_alone_is_inconclusive_with_its_reason(self) -> None:  # noqa: C901, PLR0912, PLR0915
        # The explicit matrix keeps each independently varied qualification failure visible.
        cases = (
            ("scorer", "pinned scorer"),
            ("checklist", "checklist digest"),
            ("missing-run", "source run is absent"),
            ("stopped", "did not complete"),
            ("model", "inconsistent runtime/model/reasoning"),
            ("version", "inconsistent runtime/model/reasoning"),
            ("export", "single identified evaluated export"),
            ("registration", "another scenario registration"),
            ("targets", "targeted rules"),
            ("raw-bytes", "saved scenario bytes"),
            ("replies", "recorded replies"),
            ("input-link", "scorer input names another run"),
            ("unknown-cost", "incomplete main/scorer/assessor"),
            ("overspend", "spending exceeds"),
            ("failed-unknown-session", "incomplete main/scorer/assessor"),
            ("orphan", "incomplete session evidence"),
            ("repeated-score", "single-score selection is ambiguous"),
            ("legacy", "lacks recorded scenario registration"),
            ("malformed-run", "spending evidence is unavailable"),
            ("conflicting-labels", "conflicting linked labels"),
        )
        for defect, expected in cases:
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                layout = Layout(Path(directory))
                registration = self.prepare(layout)
                key = RunKey("s1", "base", 1)
                run_file = layout.run_directory(key) / "run.json"
                record = layout.run_record(key)
                assert isinstance(record, RunRecord)
                session_file = layout.score_directory("base-s1-1") / "session.json"
                session = msgspec.json.decode(session_file.read_bytes(), type=ScorerSession)
                match defect:
                    case "malformed-run":
                        run_file.write_bytes(b"{}")
                    case "conflicting-labels":
                        mapping = msgspec.json.decode(layout.label_file(session.label).read_bytes(), type=LabelMapping)
                        write_new(layout.root / "labels" / "another-file.json", mapping)
                    case "scorer":
                        session_file.write_bytes(encode(msgspec.structs.replace(session, scorer_model="other")))
                    case "checklist":
                        session_file.write_bytes(encode(msgspec.structs.replace(session, checklist_sha256="0" * 64)))
                    case "missing-run":
                        run_file.unlink()
                    case "stopped":
                        run_file.write_bytes(encode(msgspec.structs.replace(record, outcome=Stopped("decision"))))
                    case "model" | "version":
                        changed = (
                            msgspec.structs.replace(record, model="other")
                            if defect == "model"
                            else msgspec.structs.replace(record, cli_version="other")
                        )
                        run_file.write_bytes(encode(changed))
                    case "export":
                        changed = msgspec.structs.replace(record.evaluated, commit="d" * 40)
                        run_file.write_bytes(encode(msgspec.structs.replace(record, evaluated=changed)))
                    case "registration" | "targets":
                        changed = (
                            msgspec.structs.replace(record.registration, name="other")
                            if defect == "registration"
                            else msgspec.structs.replace(record.registration, targeted_rules=[])
                        )
                        run_file.write_bytes(encode(msgspec.structs.replace(record, registration=changed)))
                    case "raw-bytes":
                        path = layout.run_directory(key) / "scenario.json"
                        path.write_bytes(path.read_bytes() + b" ")
                    case "replies":
                        path = layout.run_directory(key) / "scorer-input.json"
                        source = msgspec.json.decode(path.read_bytes(), type=scoring.ScorerInput)
                        path.write_bytes(encode(msgspec.structs.replace(source, replies=["another reply"])))
                    case "input-link":
                        path = layout.run_directory(key) / SCORER_INPUT
                        source = msgspec.json.decode(path.read_bytes(), type=scoring.ScorerInput)
                        path.write_bytes(encode(msgspec.structs.replace(source, run=RunKey("s1", "base", 2))))
                    case "unknown-cost":
                        changed = msgspec.structs.replace(record.turns[0], cost_usd=None)
                        run_file.write_bytes(encode(msgspec.structs.replace(record, turns=[changed])))
                    case "overspend" | "failed-unknown-session":
                        write_new(
                            layout.score_directory("paid-failure") / "session.json",
                            ScorerSession(
                                schema="pinboard-behavioral-scorer-session/v1",
                                label="paid-failure",
                                scorer_model=scoring.SCORER_MODEL,
                                checklist_sha256=CHECKLIST_SHA256,
                                cost_usd=spend.COMPARISON_CAP_USD + 1 if defect == "overspend" else None,
                                outcome=ScoringFailure("interrupted paid session"),
                            ),
                        )
                    case "orphan":
                        path = layout.root / "scores" / "interrupted"
                        path.mkdir()
                        (path / "raw.json").write_text("partial paid answer")
                    case "repeated-score":
                        record_score(layout, "second", key, [set()], True)
                        self.assertEqual(2, scoring.valid_score_counts(layout)[key.display()])
                    case "legacy":
                        fields = json.loads(run_file.read_text())
                        fields.pop("registration")
                        fields.pop("scenario_sha256")
                        fields["schema"] = "pinboard-behavioral-run/v2"
                        run_file.write_text(json.dumps(fields))
                        self.assertIsInstance(Layout(layout.root).run_record(key), CompatibilityRunRecord)
                        self.assertEqual(1, scoring.valid_score_counts(layout)[key.display()])
                    case _:
                        raise AssertionError(defect)
                output = decision.compare(Layout(layout.root), registration, "base", "cand")
                self.assertIn("decision: inconclusive", output)
                self.assertIn(expected, output)

    def test_actual_scoring_uses_saved_bytes_and_rejects_changed_bytes_before_a_paid_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = Layout(Path(directory))
            self.prepare(layout)
            source = next(layout.scorer_inputs())
            scenario = scoring.scoring_scenario(layout, source)
            self.assertEqual("controlled answer", scenario.ground_truth)
            with patch.object(scoring, "load_scenario", side_effect=AssertionError("current file read")):
                self.assertEqual(scenario, scoring.scoring_scenario(Layout(layout.root), source))
            path = layout.run_directory(source.run) / "scenario.json"
            path.write_bytes(path.read_bytes() + b"\n")
            with patch.object(scoring.oneshot, "ask") as paid, self.assertRaises(DataIntegrityError):
                scoring.ScoringRun(layout, spend.Budget(layout, 120, processes.Window(None), False)).score(source)
            paid.assert_not_called()

    def test_codex_conditions_require_common_effort_and_complete_reviewer_accounting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = Layout(Path(directory))
            registration = self.prepare(layout)
            for record in list(layout.run_records()):
                details = CodexRunDetails(
                    "controlled-effort",
                    "profile",
                    "workspace-write",
                    "on-request",
                    [str(layout.run_directory(record.run))],
                    "controlled-price",
                    False,
                )
                changed = msgspec.structs.replace(record, runtime=Runtime.CODEX, details=details)
                (layout.run_directory(record.run) / "run.json").write_bytes(encode(changed))
                write_new(
                    layout.run_directory(record.run) / "accounting.json",
                    CodexAccounting("pinboard-behavioral-codex-accounting/v1", 0.01, True, [], None),
                )
            self.assertIn("decision: no worse", decision.compare(Layout(layout.root), registration, "base", "cand"))
            key = RunKey("s1", "base", 1)
            record = layout.run_record(key)
            assert isinstance(record, RunRecord) and isinstance(record.details, CodexRunDetails)
            changed = msgspec.structs.replace(
                record, details=msgspec.structs.replace(record.details, reasoning_effort="other")
            )
            (layout.run_directory(key) / "run.json").write_bytes(encode(changed))
            output = decision.compare(Layout(layout.root), registration, "base", "cand")
            self.assertIn("decision: inconclusive", output)
            self.assertIn("inconsistent runtime/model/reasoning", output)


if __name__ == "__main__":
    unittest.main()
