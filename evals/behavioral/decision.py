"""Per-rule failures per run and the improved / no worse / inconclusive decision rule of ``CRITERIA.md``.

A run's failures for a checklist item are its failing replies for that item; a run scored more than once uses the
mean over its valid scores. Rates are failures per run over every run of a side; a side or scenario without a scored
run has no rate. Each checklist item and the total get an estimate: the scenario-stratified mean difference of
candidate and baseline, with an interval of ``K`` standard errors built from the pooled within-scenario standard
deviation (never below its measured floor). The comparison's verdict rests on the total and the checklist rules its
scenario set declares as targeted; every other item is advisory and only warns when its interval reaches past its
no-worse margin. Output is independent of file order and random state.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from evals.behavioral.layout import Layout
from evals.behavioral.records import ChecklistItem, ItemVerdicts, RunKey, Scored, ScoreRecord, Verdict

ITEMS_BY_NAME = tuple(ChecklistItem[name] for name in sorted(item.value for item in ChecklistItem))


class Classification(Enum):
    IMPROVED = "improved"
    NO_WORSE = "no worse"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class Criteria:
    """The decision parameters ``CRITERIA.md`` derives from measured variance."""

    minimum_runs_per_scenario: int
    minimum_runs_per_side: int
    interval_multiplier: float
    no_worse_margin_total: float
    no_worse_margin_item: float
    sd_floor_total: float
    sd_floor_item: float


CRITERIA = Criteria(
    minimum_runs_per_scenario=6,
    minimum_runs_per_side=30,
    interval_multiplier=2.0,
    no_worse_margin_total=2.0,
    no_worse_margin_item=0.75,
    sd_floor_total=3.0,
    sd_floor_item=0.5,
)


def required_runs(criteria: Criteria, scenario_count: int) -> int:
    """Runs each scenario needs on each side: at least the per-scenario minimum and enough for the per-side total."""
    return max(criteria.minimum_runs_per_scenario, math.ceil(criteria.minimum_runs_per_side / scenario_count))


def verdicts(items: ItemVerdicts) -> tuple[tuple[ChecklistItem, Verdict], ...]:
    return (
        (ChecklistItem.P1, items.p1.verdict),
        (ChecklistItem.P2, items.p2.verdict),
        (ChecklistItem.P3, items.p3.verdict),
        (ChecklistItem.P4, items.p4.verdict),
        (ChecklistItem.P5, items.p5.verdict),
        (ChecklistItem.P6, items.p6.verdict),
        (ChecklistItem.P7, items.p7.verdict),
        (ChecklistItem.P8, items.p8.verdict),
        (ChecklistItem.P9, items.p9.verdict),
        (ChecklistItem.P10, items.p10.verdict),
        (ChecklistItem.P11, items.p11.verdict),
        (ChecklistItem.P12, items.p12.verdict),
        (ChecklistItem.N1, items.n1.verdict),
    )


def score_failures(score: ScoreRecord) -> dict[ChecklistItem, int]:
    failures = dict.fromkeys(ChecklistItem, 0)
    for reply in score.replies:
        for item, verdict in verdicts(reply.items):
            if verdict is Verdict.FAIL:
                failures[item] += 1
    return failures


@dataclass(frozen=True)
class RunFailures:
    run: RunKey
    scores: int
    per_item: dict[ChecklistItem, float]

    @property
    def total(self) -> float:
        return sum(self.per_item.values())


def collect(layout: Layout, scenario_ids: frozenset[str], variant: str) -> list[RunFailures]:
    valid = {session.label for session in layout.scorer_sessions() if isinstance(session.outcome, Scored)}
    by_run: dict[str, tuple[RunKey, list[dict[ChecklistItem, int]]]] = {}
    for mapping in layout.labels():
        run = mapping.run
        if mapping.label not in valid or run.variant != variant or run.scenario_id not in scenario_ids:
            continue
        by_run.setdefault(run.display(), (run, []))[1].append(score_failures(layout.score(mapping.label)))
    return [
        RunFailures(
            run=run,
            scores=len(scores),
            per_item={item: sum(score[item] for score in scores) / len(scores) for item in ChecklistItem},
        )
        for _, (run, scores) in sorted(by_run.items())
    ]


def number(value: float) -> str:
    if not math.isfinite(value):
        return str(value)
    rounded = Decimal(repr(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(rounded.normalize(), "f")


NO_SCORED_RUN = "none"


def rate(runs: list[RunFailures], item: ChecklistItem | None) -> float | None:
    """Failures per run, or ``None`` when there is no scored run to divide by."""
    if not runs:
        return None
    return sum(value(run, item) for run in runs) / len(runs)


def rate_text(runs: list[RunFailures], item: ChecklistItem | None) -> str:
    measured = rate(runs, item)
    return NO_SCORED_RUN if measured is None else number(measured)


def value(run: RunFailures, item: ChecklistItem | None) -> float:
    return run.total if item is None else run.per_item[item]


@dataclass(frozen=True)
class Estimate:
    difference: float
    lower: float
    upper: float
    pooled_sd: float
    margin: float
    classification: Classification
    reason: str

    def exceeds_margin(self) -> bool:
        return math.isfinite(self.upper) and self.upper > self.margin


def estimate(
    criteria: Criteria,
    baseline: list[RunFailures],
    candidate: list[RunFailures],
    scenarios: list[str],
    item: ChecklistItem | None,
) -> Estimate:
    floor = criteria.sd_floor_total if item is None else criteria.sd_floor_item
    margin = criteria.no_worse_margin_total if item is None else criteria.no_worse_margin_item
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for side, runs in (("baseline", baseline), ("candidate", candidate)):
        for run in runs:
            groups[(side, run.run.scenario_id)].append(value(run, item))
    squares = sum((x - mean(values)) ** 2 for values in groups.values() for x in values)
    freedom = sum(len(values) - 1 for values in groups.values())
    pooled = math.sqrt(squares / freedom) if freedom > 0 else 0.0
    sd = max(pooled, floor)
    present = [s for s in scenarios if groups[("baseline", s)] and groups[("candidate", s)]]
    if not present:
        return Estimate(
            0.0, -math.inf, math.inf, pooled, margin, Classification.INCONCLUSIVE, "no scenario has both sides"
        )
    difference = sum(mean(groups[("candidate", s)]) - mean(groups[("baseline", s)]) for s in present) / len(present)
    spread = sum(1 / len(groups[("candidate", s)]) + 1 / len(groups[("baseline", s)]) for s in present)
    half_width = criteria.interval_multiplier * sd * math.sqrt(spread) / len(present)
    lower, upper = difference - half_width, difference + half_width
    needed = required_runs(criteria, len(scenarios))
    short = [s for s in scenarios if min(len(groups[("baseline", s)]), len(groups[("candidate", s)])) < needed]
    if short:
        reason = f"fewer than {needed} runs per side in {len(short)} of {len(scenarios)} scenarios"
        return Estimate(difference, lower, upper, pooled, margin, Classification.INCONCLUSIVE, reason)
    if upper < 0:
        return Estimate(difference, lower, upper, pooled, margin, Classification.IMPROVED, "interval below zero")
    if upper <= margin:
        reason = f"interval upper bound within the no-worse margin {number(margin)}"
        return Estimate(difference, lower, upper, pooled, margin, Classification.NO_WORSE, reason)
    reason = f"interval upper bound above the no-worse margin {number(margin)}"
    return Estimate(difference, lower, upper, pooled, margin, Classification.INCONCLUSIVE, reason)


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def overall(targeted: dict[ChecklistItem, Estimate], total: Estimate) -> tuple[Classification, str]:
    """The verdict from the total and the targeted rules; other items never decide it.

    Improved: every targeted rule is improved (with no targeted rule, the total is improved) and the total is improved
    or no worse. No worse: the total and every targeted rule are improved or no worse. Otherwise inconclusive.
    """
    for item in ITEMS_BY_NAME:
        result = targeted.get(item)
        if result is not None and result.classification is Classification.INCONCLUSIVE:
            return Classification.INCONCLUSIVE, f"targeted rule {item.value} inconclusive: {result.reason}"
    if total.classification is Classification.INCONCLUSIVE:
        return Classification.INCONCLUSIVE, f"total inconclusive: {total.reason}"
    aims = list(targeted.values()) if targeted else [total]
    if all(result.classification is Classification.IMPROVED for result in aims):
        subject = "every targeted rule" if targeted else "the total"
        return Classification.IMPROVED, f"{subject} improved and the total improved or no worse"
    return Classification.NO_WORSE, "the total and every targeted rule improved or no worse"


def failed_items_line(run: RunFailures) -> str:
    counts = " ".join(f"{item.value}={number(run.per_item[item])}" for item in ITEMS_BY_NAME if run.per_item[item])
    return f"  {run.run.display()}\t{number(run.total)}\t{counts}"


def role(item: ChecklistItem | None, targeted: tuple[ChecklistItem, ...]) -> str:
    if item is None:
        return "total"
    return "targeted" if item in targeted else "advisory"


def compare(
    layout: Layout,
    scenarios: list[str],
    targeted: tuple[ChecklistItem, ...],
    baseline_variant: str,
    candidate_variant: str,
) -> str:
    selected = frozenset(scenarios)
    baseline = collect(layout, selected, baseline_variant)
    candidate = collect(layout, selected, candidate_variant)
    targets = ",".join(item.value for item in ITEMS_BY_NAME if item in targeted) or "none"
    lines = [
        f"baseline={baseline_variant}\tcandidate={candidate_variant}\tscenarios={','.join(scenarios)}\t"
        f"targeted={targets}",
        "item\trole\tbaseline/run\tcandidate/run\tcandidate<=baseline\tdifference\tinterval\tclassification",
    ]
    targeted_estimates: dict[ChecklistItem, Estimate] = {}
    warnings: list[str] = []
    for item in [*ChecklistItem, None]:
        result = estimate(CRITERIA, baseline, candidate, scenarios, item)
        if item is not None and item in targeted:
            targeted_estimates[item] = result
        elif item is not None and result.exceeds_margin():
            warnings.append(f"{item.value} (interval upper bound {number(result.upper)} above {number(result.margin)})")
        b, c = rate(baseline, item), rate(candidate, item)
        no_higher = "n/a" if b is None or c is None else str(c <= b).lower()
        lines.append(
            f"{'total' if item is None else item.value}\t{role(item, targeted)}\t{rate_text(baseline, item)}\t"
            f"{rate_text(candidate, item)}\t{no_higher}\t{number(result.difference)}\t"
            f"[{number(result.lower)}, {number(result.upper)}]\t"
            f"{result.classification.value} ({result.reason}; pooled sd {number(result.pooled_sd)})"
        )
    total_estimate = estimate(CRITERIA, baseline, candidate, scenarios, None)
    lines.append(f"runs\t\t{len(baseline)}\t{len(candidate)}")
    lines.append("per-run failed items:")
    lines.extend(failed_items_line(run) for run in [*baseline, *candidate])
    verdict, reason = overall(targeted_estimates, total_estimate)
    lines.append(f"decision: {verdict.value} ({reason})")
    lines.append(f"advisory warnings: {'; '.join(warnings) if warnings else 'none'}")
    lines.append(criteria_line(len(scenarios)))
    return "\n".join(lines) + "\n"


def criteria_line(scenario_count: int) -> str:
    c = CRITERIA
    return (
        f"criteria: runs per scenario and side {required_runs(c, scenario_count)} (at least "
        f"{c.minimum_runs_per_scenario} per scenario and {c.minimum_runs_per_side} per side); interval +/- "
        f"{number(c.interval_multiplier)} standard errors; no-worse margin total {number(c.no_worse_margin_total)}, "
        f"item {number(c.no_worse_margin_item)}; sd floor total {number(c.sd_floor_total)}, item "
        f"{number(c.sd_floor_item)}"
    )


def report(layout: Layout, scenarios: list[str], variant: str) -> str:
    runs = collect(layout, frozenset(scenarios), variant)
    per_scenario = {s: [run for run in runs if run.run.scenario_id == s] for s in scenarios}
    lines = [f"variant={variant}\tscenarios={','.join(scenarios)}", "item\tfailures/run\t" + "\t".join(scenarios)]
    lines.extend(
        f"{'total' if item is None else item.value}\t{rate_text(runs, item)}\t"
        + "\t".join(rate_text(per_scenario[s], item) for s in scenarios)
        for item in [*ChecklistItem, None]
    )
    lines.append(f"runs\t{len(runs)}\t" + "\t".join(str(len(per_scenario[s])) for s in scenarios))
    unscored = [s for s in scenarios if not per_scenario[s]]
    if unscored:
        lines.append(f"no scored runs: {', '.join(unscored)}")
    lines.append("per-run failed items:")
    lines.extend(failed_items_line(run) for run in runs)
    return "\n".join(lines) + "\n"
