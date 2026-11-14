"""Per-rule failures per run and the improved / no worse / inconclusive decision rule of ``CRITERIA.md``.

A run's failures for a checklist item are its failing replies for that item; a run scored more than once uses the
mean over its valid scores. Rates are failures per run over every run of a side; a side or scenario without a scored
run has no rate. Each checklist item and the total get an estimate: the scenario-stratified mean difference of
candidate and baseline, with an interval of ``K`` standard errors built from the pooled within-scenario standard
deviation (never below its measured floor). The comparison's verdict rests on the total and the checklist rules its
scenario set declares as targeted; every other item is advisory and only warns when its interval reaches past its
no-worse margin. Beside it, and never combined with it, the tool-use verdict applies the same estimate to the same
scored runs' not-accepted calls to the changed Pinboard tools, with its own floor and margin. Output is independent of
file order and random state.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import assert_never

import msgspec

from evals.behavioral import scoring, spend, tool_use
from evals.behavioral.layout import SCORER_INPUT, Layout
from evals.behavioral.records import (
    AgyEffort,
    AntigravityRunDetails,
    ChecklistItem,
    ClaudeEffort,
    ClaudeRunDetails,
    CodexRunDetails,
    ItemVerdicts,
    RunKey,
    RunRecord,
    ScenarioSet,
    ScoreRecord,
    ScorerInput,
    Verdict,
)
from evals.behavioral.scenarios import DataIntegrityError

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


@dataclass(frozen=True)
class ToolUseCriteria:
    """The tool-use verdict's parameters ``CRITERIA.md`` derives from recorded runs; run counts and the interval
    multiplier are the checklist's."""

    sd_floor: float
    no_worse_margin: float


TOOL_USE_CRITERIA = ToolUseCriteria(sd_floor=0.8, no_worse_margin=0.5)


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
    by_run: dict[str, tuple[RunKey, list[dict[ChecklistItem, int]]]] = {}
    for evidence in scoring.score_evidence(layout, scoring.SCORER_MODEL):
        if not isinstance(evidence, scoring.EligibleScore):
            continue
        run = evidence.run
        if run.variant != variant or run.scenario_id not in scenario_ids:
            continue
        by_run.setdefault(run.display(), (run, []))[1].append(score_failures(evidence.score))
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
    return grouped_estimate(criteria, floor, margin, groups, scenarios)


def grouped_estimate(
    criteria: Criteria, floor: float, margin: float, groups: dict[tuple[str, str], list[float]], scenarios: list[str]
) -> Estimate:
    """Classify the scenario-stratified candidate-minus-baseline difference of per-run values grouped by side and
    scenario."""
    squares = sum((x - mean(values)) ** 2 for values in groups.values() for x in values)
    freedom = sum(len(values) - 1 for values in groups.values())
    pooled = math.sqrt(squares / freedom) if freedom > 0 else 0.0
    sd = max(pooled, floor)
    sides = {key: groups.get(key, []) for s in scenarios for key in (("baseline", s), ("candidate", s))}
    present = [s for s in scenarios if sides[("baseline", s)] and sides[("candidate", s)]]
    if not present:
        return Estimate(
            0.0, -math.inf, math.inf, pooled, margin, Classification.INCONCLUSIVE, "no scenario has both sides"
        )
    difference = sum(mean(sides[("candidate", s)]) - mean(sides[("baseline", s)]) for s in present) / len(present)
    spread = sum(1 / len(sides[("candidate", s)]) + 1 / len(sides[("baseline", s)]) for s in present)
    half_width = criteria.interval_multiplier * sd * math.sqrt(spread) / len(present)
    lower, upper = difference - half_width, difference + half_width
    needed = required_runs(criteria, len(scenarios))
    short = [s for s in scenarios if min(len(sides[("baseline", s)]), len(sides[("candidate", s)])) < needed]
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
    registration: ScenarioSet,
    baseline_variant: str,
    candidate_variant: str,
) -> str:
    scenarios = [member.id for member in registration.scenarios]
    targeted = tuple(registration.targeted_rules)
    selected = frozenset(scenarios)
    try:
        baseline = collect(layout, selected, baseline_variant)
        candidate = collect(layout, selected, candidate_variant)
    except (OSError, msgspec.DecodeError, ValueError) as error:
        return f"decision: inconclusive (comparison evidence is unavailable or conflicting: {error})\n"
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
    reasons = qualification(layout, registration, baseline_variant, candidate_variant, [*baseline, *candidate])
    if reasons:
        verdict, reason = Classification.INCONCLUSIVE, "; ".join(reasons)
    lines.append(f"decision: {verdict.value} ({reason})")
    lines.append(f"advisory warnings: {'; '.join(warnings) if warnings else 'none'}")
    lines.append(criteria_line(len(scenarios)))
    return "\n".join(lines) + "\n"


def conditions(record: RunRecord) -> tuple[str | ClaudeEffort | AgyEffort | None, ...]:
    """Declared consequential settings; per-run paths and settlement effects are not execution settings.

    A Claude run's effort is ``None`` when none was sent, so absent and present effort never match.
    """
    common = (record.runtime.value, record.cli_version, record.model)
    match record.details:
        case ClaudeRunDetails(permission_mode=mode, cost_basis=basis, reasoning_effort=effort):
            return (*common, mode, basis, effort)
        case CodexRunDetails() as details:
            return (
                *common,
                details.reasoning_effort,
                details.permission_profile,
                details.sandbox_mode,
                details.approval_policy,
                details.price_source,
            )
        case AntigravityRunDetails() as details:
            return (
                *common,
                details.reasoning_effort,
                details.observed_model,
                *details.permissions_allow,
                details.price_source,
            )
        case _ as unreachable:
            assert_never(unreachable)


def qualification(
    layout: Layout, registration: ScenarioSet, baseline: str, candidate: str, selected_runs: list[RunFailures]
) -> list[str]:
    """Aggregate coverage, identity and full-dollar accounting qualify the frozen single-score arithmetic."""
    try:
        reasons = spend.comparison_reasons(layout)
    except (OSError, msgspec.DecodeError, ValueError) as error:
        return [f"comparison spending evidence is unavailable or conflicting: {error}"]
    scenario_ids = {member.id for member in registration.scenarios}
    selected_variants = {baseline, candidate}
    if baseline == candidate:
        reasons.append("baseline and candidate identify the same variant")
    for evidence in scoring.score_evidence(layout, scoring.SCORER_MODEL):
        if isinstance(evidence, scoring.IneligibleScore) and (
            evidence.run is None
            or (evidence.run.variant in selected_variants and evidence.run.scenario_id in scenario_ids)
        ):
            reasons.append(evidence.reason)
    exports: dict[str, set[tuple[str, str]]] = {variant: set() for variant in selected_variants}
    settings: set[tuple[str | ClaudeEffort | AgyEffort | None, ...]] = set()
    for selected in selected_runs:
        key = selected.run
        if selected.scores != 1:
            reasons.append(
                f"{key.display()} has {selected.scores} eligible scores; single-score selection is ambiguous"
            )
        record = layout.run_record(key)
        if record is None:
            reasons.append(f"linked source run is absent: {key.display()}")
            continue
        reasons.extend(run_qualification(layout, registration, record))
        exports[key.variant].add((record.evaluated.commit, record.evaluated.skills_sha256))
        settings.add(conditions(record))
    for variant, identities in exports.items():
        if len(identities) != 1:
            reasons.append(f"{variant} has no single identified evaluated export")
    if len(settings) != 1:
        reasons.append("selected runs have absent or inconsistent runtime/model/reasoning conditions")
    return sorted(set(reasons))


def run_qualification(layout: Layout, registration: ScenarioSet, record: RunRecord) -> list[str]:
    """Reconcile the recorded registration with the exact saved bytes later judging consumes."""
    key = record.run
    reasons = []
    if record.registration != registration:
        reasons.append(f"{key.display()} used another scenario registration or targeted rules")
    members = [member for member in registration.scenarios if member.id == key.scenario_id]
    if len(members) != 1 or members[0].sha256 != record.scenario_sha256:
        reasons.append(f"{key.display()} scenario byte identity differs from registration")
    try:
        source = msgspec.json.decode((layout.run_directory(key) / SCORER_INPUT).read_bytes(), type=ScorerInput)
        if source.run != key:
            return [*reasons, f"{key.display()} scorer input names another run"]
        scenario = scoring.scoring_scenario(layout, source)
        if [turn.human for turn in record.turns] != [turn.human for turn in scenario.turns]:
            reasons.append(f"{key.display()} recorded turns differ from the registered scenario")
    except (OSError, msgspec.DecodeError, ValueError, DataIntegrityError) as error:
        reasons.append(f"{key.display()} saved scoring evidence is unavailable or conflicting: {error}")
    return reasons


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


CHECKLIST_ONLY = "no changed tool surface was declared with --changed-tool, so this comparison is checklist-only"

type SideTallies = list[tuple[RunKey, tool_use.Tally]]


def tool_use_compare(
    layout: Layout,
    registration: ScenarioSet,
    declared: dict[str, frozenset[str]],
    baseline_variant: str,
    candidate_variant: str,
    surface: frozenset[str],
) -> str:
    """The tool-use verdict beside the checklist verdict, over the same scored runs; it never changes that verdict.

    A run's value is its number of calls to the changed tools that the server did not accept. The verdict is refused
    when any selected run's calls cannot be observed, and inconclusive when no member scenario declares exercising the
    changed tools, when either side made no call to them, or when the runs do not qualify.
    """
    if not surface:
        return f"tool-use decision: refused ({CHECKLIST_ONLY})\n"
    scenarios = [member.id for member in registration.scenarios]
    declared_coverage = tool_use.coverage(declared, surface)
    lines = [f"tool use: {declared_coverage.describe()}"]
    try:
        baseline = collect(layout, frozenset(scenarios), baseline_variant)
        candidate = collect(layout, frozenset(scenarios), candidate_variant)
    except (OSError, msgspec.DecodeError, ValueError) as error:
        lines.append(f"tool-use decision: inconclusive (comparison evidence is unavailable: {error})")
        return "\n".join(lines) + "\n"
    baseline_tallies, baseline_refusals = side_tallies(layout, baseline, surface)
    candidate_tallies, candidate_refusals = side_tallies(layout, candidate, surface)
    if refusals := sorted({*baseline_refusals, *candidate_refusals}):
        lines.append(f"tool-use decision: refused ({'; '.join(refusals)})")
        return "\n".join(lines) + "\n"
    lines.append("measure\tbaseline/run\tcandidate/run")
    lines.append(f"runs\t{len(baseline_tallies)}\t{len(candidate_tallies)}")
    for index, (name, _) in enumerate(tool_use.EMPTY_TALLY.measures()):
        lines.append(f"{name}\t{per_run(baseline_tallies, index)}\t{per_run(candidate_tallies, index)}")
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for side, counted in (("baseline", baseline_tallies), ("candidate", candidate_tallies)):
        for run, counts in counted:
            groups[(side, run.scenario_id)].append(float(counts.not_accepted))
    result = grouped_estimate(
        CRITERIA, TOOL_USE_CRITERIA.sd_floor, TOOL_USE_CRITERIA.no_worse_margin, groups, scenarios
    )
    lines.append(
        f"not accepted difference {number(result.difference)}\tinterval [{number(result.lower)}, "
        f"{number(result.upper)}]\t{result.classification.value} ({result.reason}; pooled sd {number(result.pooled_sd)})"
    )
    lines.append("per-run calls to the changed tools:")
    lines.extend(
        f"  {run.display()}\t{tool_use.tally_text(counts)}" for run, counts in [*baseline_tallies, *candidate_tallies]
    )
    unexercised = [
        side
        for side, counted in (("baseline", baseline_tallies), ("candidate", candidate_tallies))
        if not any(counts.calls for _, counts in counted)
    ]
    if not declared_coverage.covered:
        verdict, reason = Classification.INCONCLUSIVE, "no scenario in the set declares exercising the changed tools"
    elif unexercised:
        verdict = Classification.INCONCLUSIVE
        reason = f"no call to the changed tools was observed on the {' and '.join(unexercised)} side"
    elif reasons := qualification(layout, registration, baseline_variant, candidate_variant, [*baseline, *candidate]):
        verdict, reason = Classification.INCONCLUSIVE, "; ".join(reasons)
    else:
        verdict, reason = result.classification, result.reason
    lines.append(f"tool-use decision: {verdict.value} ({reason})")
    lines.append(
        f"tool-use criteria: value not-accepted calls to the changed tools per run; runs per scenario and side "
        f"{required_runs(CRITERIA, len(scenarios))}; interval +/- {number(CRITERIA.interval_multiplier)} standard "
        f"errors; no-worse margin {number(TOOL_USE_CRITERIA.no_worse_margin)}; sd floor "
        f"{number(TOOL_USE_CRITERIA.sd_floor)}"
    )
    return "\n".join(lines) + "\n"


def side_tallies(layout: Layout, runs: list[RunFailures], surface: frozenset[str]) -> tuple[SideTallies, list[str]]:
    """Each selected run's calls to the changed tools, and why any selected run's calls cannot be observed."""
    tallies: SideTallies = []
    refusals: list[str] = []
    for run in runs:
        record = layout.run_record(run.run)
        if record is None:
            refusals.append(f"{run.run.display()} has no run record")
            continue
        match tool_use.read_run(layout, record):
            case tool_use.Unobservable(reason=reason):
                refusals.append(reason)
            case tool_use.Observed(calls=calls):
                tallies.append((run.run, tool_use.tally(calls, surface)))
            case _ as unreachable:
                assert_never(unreachable)
    return tallies, refusals


def per_run(counted: SideTallies, measure: int) -> str:
    """The mean of one ``Tally.measures`` entry over a side's runs."""
    if not counted:
        return NO_SCORED_RUN
    return number(sum(counts.measures()[measure][1] for _, counts in counted) / len(counted))
