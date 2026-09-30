"""The behavioral harness's comparison arithmetic and improved / no worse / inconclusive decision rule."""

import tempfile
import unittest
from pathlib import Path

from evals.behavioral import decision
from evals.behavioral.decision import Classification, Criteria, Estimate, RunFailures
from evals.behavioral.layout import Layout
from evals.behavioral.records import (
    ChecklistItem,
    ItemVerdict,
    ItemVerdicts,
    LabelMapping,
    ReplyScore,
    RunKey,
    Scored,
    ScoreRecord,
    ScorerSession,
    ScoringFailure,
    Verdict,
    write_new,
)

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
            scorer_model="scorer",
            checklist_sha256="0" * 64,
            cost_usd=0.0,
            outcome=Scored() if valid else ScoringFailure(reason="malformed"),
        ),
    )
    write_new(layout.label_file(label), LabelMapping(schema="pinboard-behavioral-label/v1", label=label, run=run))


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
                outputs.append(decision.compare(layout, ["s1"], (ChecklistItem.P9,), "base", "cand"))
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
            output = decision.compare(layout, scenarios, (ChecklistItem.P9,), "base", "cand")
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
            comparison = decision.compare(layout, ["s1", "s2"], (), "base", "cand")
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


if __name__ == "__main__":
    unittest.main()
