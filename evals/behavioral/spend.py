"""Spend accounting for one output directory, and the cap guard every paid session passes before it starts.

Spend is always recomputed from the recorded run, scorer, assessment and probe records; nothing else is summed.
Before a session starts, its projected cost (the largest recorded cost of that category in the directory, or a
conservative default before any is recorded) plus the recorded spend and the projections of sessions already in
flight must fit the caller's explicit cap; otherwise the session does not start.
"""

import threading
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum

from evals.behavioral.layout import Layout
from evals.behavioral.records import Runtime


class Category(Enum):
    CLAUDE_AGENT_RUN = "agent runs (claude-code)"
    CODEX_AGENT_RUN = "agent runs (codex)"
    SCORER = "scorer sessions"
    SUBSTANCE_ASSESSMENT = "substance assessments"
    PROBE = "probes"


DEFAULT_PROJECTION_USD = {
    Category.CLAUDE_AGENT_RUN: 2.50,
    Category.CODEX_AGENT_RUN: 5.00,
    Category.SCORER: 0.50,
    Category.SUBSTANCE_ASSESSMENT: 0.40,
    Category.PROBE: 1.00,
}


@dataclass(frozen=True)
class Item:
    category: Category
    group: str
    usd: float


def agent_category(runtime: Runtime) -> Category:
    match runtime:
        case Runtime.CLAUDE_CODE:
            return Category.CLAUDE_AGENT_RUN
        case Runtime.CODEX:
            return Category.CODEX_AGENT_RUN
        case _ as unreachable:
            raise AssertionError(unreachable)


def items(layout: Layout) -> list[Item]:
    recorded = [Item(agent_category(run.runtime), run.run.variant, run.cost_usd()) for run in layout.run_records()]
    recorded.extend(
        Item(Category.SCORER, session.scorer_model, session.cost_usd) for session in layout.scorer_sessions()
    )
    recorded.extend(
        Item(Category.SUBSTANCE_ASSESSMENT, record.assessor_model, record.cost_usd) for record in layout.assessments()
    )
    recorded.extend(Item(Category.PROBE, probe.name, probe.cost_usd) for probe in layout.probes())
    return recorded


def total(recorded: list[Item]) -> float:
    return sum(item.usd for item in recorded)


def projection(category: Category, recorded: list[Item]) -> float:
    costs = [item.usd for item in recorded if item.category is category]
    return max(costs) if costs else DEFAULT_PROJECTION_USD[category]


def report(layout: Layout) -> str:
    recorded = items(layout)
    groups: dict[Category, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for item in recorded:
        groups[item.category][item.group].append(item.usd)
    lines = ["category\tsessions\tusd"]
    for category in Category:
        costs = [usd for values in groups[category].values() for usd in values]
        lines.append(f"{category.value}\t{len(costs)}\t{sum(costs):.4f}")
        lines.extend(
            f"  {group}\t{len(values)}\t{sum(values):.4f}" for group, values in sorted(groups[category].items())
        )
    lines.append(f"total\t{len(recorded)}\t{total(recorded):.4f}")
    return "\n".join(lines) + "\n"


class Budget:
    """Reserve a projected cost before each paid session; refuse a session that would not fit the cap."""

    def __init__(self, layout: Layout, cap_usd: float) -> None:
        self.layout = layout
        self.cap_usd = cap_usd
        self.reserved = 0.0
        self.lock = threading.Lock()

    def reserve(self, category: Category) -> float | None:
        with self.lock:
            recorded = items(self.layout)
            projected = projection(category, recorded)
            if total(recorded) + self.reserved + projected > self.cap_usd:
                return None
            self.reserved += projected
            return projected

    def release(self, projected: float) -> None:
        with self.lock:
            self.reserved -= projected

    def remaining(self) -> float:
        with self.lock:
            return self.cap_usd - total(items(self.layout)) - self.reserved
