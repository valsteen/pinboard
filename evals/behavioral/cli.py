"""Command entry: decode arguments into one exact leaf command, then dispatch it exhaustively.

Leaves follow the representative path: export a skills revision, run a scenario set for one runtime, score,
compare (or report one variant), assess Codex reply substance, and total spend.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from evals.behavioral import decision, export, probe, runner, scoring, spend, substance
from evals.behavioral.credentials import default_source
from evals.behavioral.layout import Layout
from evals.behavioral.records import ScorerInput
from evals.behavioral.scenarios import load_set


@dataclass(frozen=True)
class Export:
    source: Path
    revision: str
    destination: Path


@dataclass(frozen=True)
class RunClaude:
    export: Path
    scenario_set: Path
    variant: str
    runs: int
    first_index: int
    model: str
    out: Path
    worlds: Path
    cap_usd: float
    jobs: int


@dataclass(frozen=True)
class RunCodex:
    export: Path
    scenario_set: Path
    variant: str
    runs: int
    first_index: int
    model: str
    reasoning_effort: str
    out: Path
    worlds: Path
    cap_usd: float


@dataclass(frozen=True)
class ProbeCodex:
    export: Path
    name: str
    model: str
    reasoning_effort: str
    out: Path
    worlds: Path
    cap_usd: float


@dataclass(frozen=True)
class Score:
    out: Path
    cap_usd: float
    scores_per_run: int
    variants: tuple[str, ...]


@dataclass(frozen=True)
class Assess:
    out: Path
    cap_usd: float


@dataclass(frozen=True)
class Compare:
    out: Path
    scenario_set: Path
    baseline: str
    candidate: str


@dataclass(frozen=True)
class Report:
    out: Path
    scenario_set: Path
    variant: str


@dataclass(frozen=True)
class Spend:
    out: Path


type Command = Export | RunClaude | RunCodex | ProbeCodex | Score | Assess | Compare | Report | Spend


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m evals.behavioral", description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    exporting = commands.add_parser("export", help="export and prepare one skills revision as a plugin root")
    exporting.add_argument("--source", type=Path, default=Path())
    exporting.add_argument("--revision", required=True)
    exporting.add_argument("--dest", type=Path, required=True)

    running = commands.add_parser("run", help="run a scenario set against one runtime").add_subparsers(
        dest="runtime", required=True
    )
    for runtime in ("claude", "codex"):
        leaf = running.add_parser(runtime)
        leaf.add_argument("--export", type=Path, required=True)
        leaf.add_argument("--scenario-set", type=Path, required=True)
        leaf.add_argument("--variant", required=True)
        leaf.add_argument("--runs", type=positive, required=True)
        leaf.add_argument("--first-index", type=positive, default=1)
        leaf.add_argument("--model", required=True)
        leaf.add_argument("--out", type=Path, required=True)
        leaf.add_argument("--worlds", type=Path, required=True)
        leaf.add_argument("--cap-usd", type=float, required=True)
        if runtime == "claude":
            leaf.add_argument("--jobs", type=positive, default=1)
        else:
            leaf.add_argument("--reasoning-effort", required=True)

    probing = commands.add_parser("probe", help="prove runtime isolation cheaply").add_subparsers(
        dest="runtime", required=True
    )
    codex_probe = probing.add_parser("codex")
    codex_probe.add_argument("--export", type=Path, required=True)
    codex_probe.add_argument("--name", required=True)
    codex_probe.add_argument("--model", required=True)
    codex_probe.add_argument("--reasoning-effort", required=True)
    codex_probe.add_argument("--out", type=Path, required=True)
    codex_probe.add_argument("--worlds", type=Path, required=True)
    codex_probe.add_argument("--cap-usd", type=float, required=True)

    scoring_parser = commands.add_parser("score", help="blind-score recorded runs with the frozen checklist")
    scoring_parser.add_argument("--out", type=Path, required=True)
    scoring_parser.add_argument("--cap-usd", type=float, required=True)
    scoring_parser.add_argument("--scores-per-run", type=positive, default=1)
    scoring_parser.add_argument("--variant", action="append", default=[])

    assessing = commands.add_parser("assess", help="assess whether Codex final replies carry the substance")
    assessing.add_argument("--out", type=Path, required=True)
    assessing.add_argument("--cap-usd", type=float, required=True)

    comparing = commands.add_parser("compare", help="compare a baseline and a candidate per checklist rule")
    comparing.add_argument("--out", type=Path, required=True)
    comparing.add_argument("--scenario-set", type=Path, required=True)
    comparing.add_argument("--baseline", required=True)
    comparing.add_argument("--candidate", required=True)

    reporting = commands.add_parser("report", help="per-rule failures per run for one variant")
    reporting.add_argument("--out", type=Path, required=True)
    reporting.add_argument("--scenario-set", type=Path, required=True)
    reporting.add_argument("--variant", required=True)

    spending = commands.add_parser("spend", help="itemize and total recorded spend")
    spending.add_argument("--out", type=Path, required=True)
    return root


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def decode(arguments: Sequence[str]) -> Command:
    options = parser().parse_args(arguments)
    command: str = options.command
    match command:
        case "export":
            return Export(options.source, options.revision, options.dest)
        case "run":
            return decode_run(options)
        case "probe":
            return decode_probe(options)
        case "score":
            return Score(options.out, options.cap_usd, options.scores_per_run, tuple(options.variant))
        case "assess":
            return Assess(options.out, options.cap_usd)
        case "compare":
            return Compare(options.out, options.scenario_set, options.baseline, options.candidate)
        case "report":
            return Report(options.out, options.scenario_set, options.variant)
        case "spend":
            return Spend(options.out)
        case _:
            raise AssertionError(command)


def decode_run(options: argparse.Namespace) -> RunClaude | RunCodex:
    runtime: str = options.runtime
    match runtime:
        case "claude":
            return RunClaude(
                options.export,
                options.scenario_set,
                options.variant,
                options.runs,
                options.first_index,
                options.model,
                options.out,
                options.worlds,
                options.cap_usd,
                options.jobs,
            )
        case "codex":
            return RunCodex(
                options.export,
                options.scenario_set,
                options.variant,
                options.runs,
                options.first_index,
                options.model,
                options.reasoning_effort,
                options.out,
                options.worlds,
                options.cap_usd,
            )
        case _:
            raise AssertionError(runtime)


def decode_probe(options: argparse.Namespace) -> ProbeCodex:
    runtime: str = options.runtime
    match runtime:
        case "codex":
            return ProbeCodex(
                options.export,
                options.name,
                options.model,
                options.reasoning_effort,
                options.out,
                options.worlds,
                options.cap_usd,
            )
        case _:
            raise AssertionError(runtime)


def run_plan(
    export_directory: Path,
    scenario_set: Path,
    variant: str,
    runs: int,
    first_index: int,
    model: str,
    out: Path,
    worlds: Path,
) -> runner.RunPlan:
    return runner.RunPlan(
        layout=Layout(out),
        worlds=worlds,
        export=export.load_export(export_directory),
        variant=variant,
        scenarios=load_set(scenario_set),
        first_index=first_index,
        runs=runs,
        model=model,
    )


def dispatch(command: Command) -> int:
    match command:
        case Export(source=source, revision=revision, destination=destination):
            record = export.export_revision(source, revision, destination)
            print(f"exported {record.commit} skills {record.skills_sha256} at {record.plugin_root}")
        case RunClaude() as c:
            return run_claude(c)
        case RunCodex() as c:
            return run_codex(c)
        case ProbeCodex() as c:
            return probe_codex(c)
        case Score() as c:
            report_skipped(score(c))
        case Assess() as c:
            report_skipped(substance.assess(Layout(c.out), spend.Budget(Layout(c.out), c.cap_usd)))
            print(substance.summarize(Layout(c.out)), end="")
        case Compare() as c:
            registered = load_set(c.scenario_set)
            ids = [scenario.id for scenario in registered.scenarios]
            print(decision.compare(Layout(c.out), ids, registered.targeted_rules, c.baseline, c.candidate), end="")
        case Report() as c:
            ids = [scenario.id for scenario in load_set(c.scenario_set).scenarios]
            print(decision.report(Layout(c.out), ids, c.variant), end="")
        case Spend(out=out):
            print(spend.report(Layout(out)), end="")
        case _ as unreachable:
            raise AssertionError(unreachable)
    return 0


def run_claude(command: RunClaude) -> int:
    c = command
    plan = run_plan(c.export, c.scenario_set, c.variant, c.runs, c.first_index, c.model, c.out, c.worlds)
    try:
        report_skipped(runner.run_claude(plan, spend.Budget(Layout(c.out), c.cap_usd), c.jobs))
    except runner.IsolationBreachError as stop:
        print(f"Claude Code runs stopped: {stop}")
        return 2
    return 0


def run_codex(command: RunCodex) -> int:
    c = command
    plan = run_plan(c.export, c.scenario_set, c.variant, c.runs, c.first_index, c.model, c.out, c.worlds)
    try:
        report_skipped(
            runner.run_codex(
                runner.CodexPlan(plan, c.reasoning_effort, default_source()), spend.Budget(Layout(c.out), c.cap_usd)
            )
        )
    except (runner.CredentialConflictError, runner.IsolationBreachError) as stop:
        print(f"Codex runs stopped: {stop}")
        return 2
    return 0


def probe_codex(command: ProbeCodex) -> int:
    c = command
    record = probe.probe_codex(
        Layout(c.out),
        spend.Budget(Layout(c.out), c.cap_usd),
        export.load_export(c.export),
        c.name,
        c.model,
        c.reasoning_effort,
        c.worlds,
        default_source(),
    )
    if record is None:
        print("probe not started: the cap leaves no room for it")
        return 1
    print(f"probe {record.name}: {'passed' if record.passed else 'failed'}; cost {record.cost_usd:.4f} USD")
    for finding in record.findings:
        print(f"  finding: {finding}")
    return 0 if record.passed else 1


def score(command: Score) -> list[str]:
    layout = Layout(command.out)
    session = scoring.ScoringRun(layout, spend.Budget(layout, command.cap_usd))
    counts = scoring.valid_score_counts(layout)
    skipped: list[str] = []
    pending: list[ScorerInput] = [
        source for source in layout.scorer_inputs() if not command.variants or source.run.variant in command.variants
    ]
    for source in pending:
        for _ in range(command.scores_per_run - counts.get(source.run.display(), 0)):
            outcome = session.score(source)
            if outcome is None:
                skipped.append(source.run.display())
                break
            print(f"scored {source.run.display()}: {type(outcome).__name__}")
    return skipped


def report_skipped(skipped: list[str]) -> None:
    if skipped:
        print("not started because the cap was reached: " + ", ".join(skipped))


def main(arguments: Sequence[str] | None = None) -> int:
    return dispatch(decode(sys.argv[1:] if arguments is None else arguments))
