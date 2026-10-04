"""Scenario, scenario-set and checklist data shipped with the harness, verified by digest before use.

A scenario set registers each member by the SHA-256 of its scenario file and declares the checklist rules a
comparison over it targets. A comparison's held-out scenarios and targeted rules are registered this way before its
last guidance edit, so a later edit to a registered scenario file is detected rather than silently evaluated.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import msgspec

from evals.behavioral.records import Scenario, ScenarioId, ScenarioSet, WorldKind

DATA = Path(__file__).parent / "data"
CHECKLIST = DATA / "checklist.md"
CHECKLIST_SHA256 = "bbc59ff4ae66b28a9cf8be1b4924e28e39d9da1c0e582fb7eb71b8ec2293d90a"
WORLD_FACTS_FULL = DATA / "world-facts-full.md"
FIXTURE = DATA / "fixture"


class DataIntegrityError(Exception):
    """A shipped checklist, scenario or scenario set does not match its registered digest or shape."""


@dataclass(frozen=True)
class RegisteredSet:
    scenarios: tuple[Scenario, ...]
    registration: ScenarioSet
    scenario_sources: tuple[bytes, ...]

    def content(self, scenario: Scenario) -> bytes:
        """The verified raw bytes for a selected member, independent of selection subsets."""
        for member, content in zip(self.registration.scenarios, self.scenario_sources, strict=True):
            if member.id == scenario.id:
                if hashlib.sha256(content).hexdigest() != member.sha256:
                    raise DataIntegrityError(f"registered scenario bytes changed: {scenario.id}")
                if msgspec.json.decode(content, type=Scenario) != scenario:
                    raise DataIntegrityError(f"selected scenario differs from registered bytes: {scenario.id}")
                return content
        raise DataIntegrityError(f"selected scenario is unregistered: {scenario.id}")


def checklist_text() -> str:
    data = CHECKLIST.read_bytes()
    if hashlib.sha256(data).hexdigest() != CHECKLIST_SHA256:
        raise DataIntegrityError(f"the frozen checklist at {CHECKLIST} does not match {CHECKLIST_SHA256}")
    return data.decode()


def scenario_path(scenario_id: ScenarioId) -> Path:
    return DATA / "scenarios" / f"{scenario_id}.json"


def load_scenario(scenario_id: ScenarioId) -> Scenario:
    scenario = msgspec.json.decode(scenario_path(scenario_id).read_bytes(), type=Scenario)
    if scenario.id != scenario_id:
        raise DataIntegrityError(f"{scenario_path(scenario_id)} declares id {scenario.id}")
    return scenario


def world_facts(scenario: Scenario) -> str | None:
    match scenario.world:
        case WorldKind.FULL:
            return WORLD_FACTS_FULL.read_text()
        case WorldKind.MINIMAL:
            return None
        case _ as unreachable:
            raise AssertionError(unreachable)


def load_set(path: Path) -> RegisteredSet:
    """Load a scenario set file and verify every member against its registered digest."""
    scenario_set = msgspec.json.decode(path.read_bytes(), type=ScenarioSet)
    scenarios = []
    sources = []
    for member in scenario_set.scenarios:
        file = scenario_path(ScenarioId(member.id))
        content = file.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if actual != member.sha256:
            raise DataIntegrityError(f"{file} has SHA-256 {actual}, but {path} registers {member.sha256}")
        scenario = msgspec.json.decode(content, type=Scenario)
        if scenario.id != member.id:
            raise DataIntegrityError(f"{file} declares id {scenario.id}")
        scenarios.append(scenario)
        sources.append(content)
    return RegisteredSet(tuple(scenarios), scenario_set, tuple(sources))
