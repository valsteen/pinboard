"""Fictional investigation worlds and no-cost evidence assessment inputs.

The registered scenario describes only material an evaluated agent may see. A
separate caller-supplied key lives outside the source checkout and exported
plugin. This module does not make an agent call or claim an agent-performance
result.
"""

import hashlib
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

import msgspec

from evals.behavioral import processes
from evals.behavioral.records import Record, Sha256

DATA = Path(__file__).parent / "data" / "investigation"


class EvidenceStatus(Enum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    CONTRADICTED = "contradicted"
    UNKNOWN = "unknown"


class SessionMode(Enum):
    START = "start"
    CONTINUE = "continue"
    FRESH = "fresh"


class SourceCommit(Record, frozen=True):
    message: str
    files: dict[str, str]


class SourceRepository(Record, frozen=True):
    name: str
    commits: Annotated[list[SourceCommit], msgspec.Meta(min_length=2)]


class SourceFile(Record, frozen=True):
    path: str
    text: str


class InvestigationTurn(Record, frozen=True):
    human: str
    mode: SessionMode


class InvestigationScenario(Record, frozen=True):
    schema: Literal["pinboard-investigation-scenario/v1"]
    id: str
    kind: Literal["urgent", "cross-service"]
    repositories: Annotated[list[SourceRepository], msgspec.Meta(min_length=2)]
    files: list[SourceFile]
    turns: Annotated[list[InvestigationTurn], msgspec.Meta(min_length=2)]

    def __post_init__(self) -> None:
        if self.turns[0].mode is not SessionMode.START or not any(t.mode is SessionMode.FRESH for t in self.turns):
            raise ValueError("investigation needs a start and an independent fresh-session turn")
        paths = [f.path for f in self.files]
        if len(paths) != len(set(paths)) or any(not safe_relative(p) for p in paths):
            raise ValueError("investigation source paths must be unique and local")
        names = [repo.name for repo in self.repositories]
        if len(names) != len(set(names)) or any(not safe_relative(name) or "/" in name for name in names):
            raise ValueError("investigation repository names must be unique and local")
        if any(not safe_relative(p) for repo in self.repositories for commit in repo.commits for p in commit.files):
            raise ValueError("investigation repository file paths must be local")


class RegisteredInvestigation(Record, frozen=True):
    id: str
    sha256: Sha256


class InvestigationSet(Record, frozen=True):
    schema: Literal["pinboard-investigation-set/v1"]
    name: str
    purpose: Literal["tuning", "held-out"]
    cases: Annotated[list[RegisteredInvestigation], msgspec.Meta(min_length=1)]
    targeted_measures: list[str]


class KeyFact(Record, frozen=True):
    id: str
    status: EvidenceStatus
    claim: str
    locators: list[str]
    window: str
    relation: str


class InvestigationKey(Record, frozen=True):
    schema: Literal["pinboard-investigation-key/v1"]
    scenario_id: str
    facts: list[KeyFact]
    consequential_fact: str
    red_herring: str
    human_choice: str
    unavailable_fact: str
    valid_alternatives: list[str]


class SessionObservation(Record, frozen=True):
    turn: Annotated[int, msgspec.Meta(ge=1)]
    mode: SessionMode
    runtime_identity: str
    saved_evidence_read: bool
    compaction_event: str | None


class Measure(Enum):
    GROUNDED_CONCLUSION = "grounded-conclusion"
    SOURCE_DISCOVERY = "source-discovery"
    CONTRADICTION = "contradiction-retention"
    UNKNOWN = "unknown-retention"
    PROVENANCE = "provenance-and-window"
    RECOVERY = "fresh-session-recovery"
    REPEATED_WORK = "repeated-work"
    HUMAN_EFFORT = "human-effort"
    DOCUMENT_USEFULNESS = "document-usefulness"
    STOPPING = "bounded-stopping"
    SALIENCE = "consequential-finding-salience"
    ACKNOWLEDGEMENT = "human-acknowledgement"
    CHECKABILITY = "direct-checkability"
    RED_HERRING = "dismissed-red-herring"
    FOCUS = "focus-coaching"
    SOURCE_BREADTH = "source-breadth-after-choice"


class MeasureResult(Record, frozen=True):
    measure: Measure
    passed: bool
    evidence: str


class InvestigationAssessment(Record, frozen=True):
    schema: Literal["pinboard-investigation-assessment/v1"]
    scenario_id: str
    results: list[MeasureResult]

    def __post_init__(self) -> None:
        if {r.measure for r in self.results} != set(Measure) or len(self.results) != len(Measure):
            raise ValueError("score every investigation measure exactly once")


def safe_relative(value: str) -> bool:
    path = Path(value)
    return (
        bool(value)
        and value != "."
        and not path.is_absolute()
        and path.as_posix() == value
        and all(part not in ("", ".", "..") for part in path.parts)
    )


def load_set(path: Path) -> tuple[InvestigationSet, list[InvestigationScenario]]:
    registered = msgspec.json.decode(path.read_bytes(), type=InvestigationSet)
    cases = []
    for member in registered.cases:
        source = DATA / "scenarios" / f"{member.id}.json"
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != member.sha256:
            raise ValueError(f"investigation scenario digest changed: {member.id}")
        case = msgspec.json.decode(content, type=InvestigationScenario)
        if case.id != member.id:
            raise ValueError(f"investigation scenario id mismatch: {member.id}")
        cases.append(case)
    return registered, cases


def load_key(path: Path, case: InvestigationScenario, expected_sha256: str) -> InvestigationKey:
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("private investigation key digest changed")
    key = msgspec.json.decode(content, type=InvestigationKey)
    if key.scenario_id != case.id:
        raise ValueError("private investigation key names another scenario")
    return key


def build_world(root: Path, case: InvestigationScenario, window: processes.Window) -> Path:
    """Create independently inspectable source histories and supplied records."""
    root.mkdir(parents=True, exist_ok=False)
    sources = root / "sources"
    sources.mkdir()
    for repo in case.repositories:
        location = sources / repo.name
        location.mkdir()
        processes.git_checked(["init", "-q", "-b", "main"], cwd=location, window=window)
        processes.git_checked(["config", "user.name", "Fictional Investigator"], cwd=location, window=window)
        processes.git_checked(["config", "user.email", "investigator@example.test"], cwd=location, window=window)
        for commit in repo.commits:
            for relative, content in commit.files.items():
                target = location / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            processes.git_checked(["add", "-A"], cwd=location, window=window)
            processes.git_checked(["commit", "-q", "-m", commit.message], cwd=location, window=window)
    for source in case.files:
        target = sources / source.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.text)
    home = root / "inquiry"
    home.mkdir()
    (home / "README.md").write_text(
        "# Investigation workspace\n\nRead evidence under ../sources and Git histories in its repositories. "
        "Save inquiry state and audience-specific drafts here. Evidence sources are read-only. "
        "The inventory may be incomplete; record inaccessible and unsearched facts separately.\n"
    )
    return home


def record_sessions(case: InvestigationScenario, observations: list[SessionObservation]) -> str:
    """Validate identity evidence; a resumed turn cannot prove fresh recovery or compaction."""
    if [o.turn for o in observations] != list(range(1, len(case.turns) + 1)):
        raise ValueError("one ordered runtime observation is required per turn")
    seen: set[str] = set()
    current = ""
    for turn, observed in zip(case.turns, observations, strict=True):
        if observed.mode is not turn.mode or not observed.runtime_identity:
            raise ValueError("runtime observation disagrees with scripted continuation")
        match observed.mode:
            case SessionMode.START | SessionMode.FRESH:
                if observed.runtime_identity in seen:
                    raise ValueError("fresh-session recovery reused a runtime identity")
                if observed.mode is SessionMode.FRESH and not observed.saved_evidence_read:
                    raise ValueError("fresh session did not read saved inquiry evidence")
                current = observed.runtime_identity
                seen.add(current)
            case SessionMode.CONTINUE:
                if observed.runtime_identity != current:
                    raise ValueError("continued turn changed runtime identity")
            case _ as unreachable:
                raise AssertionError(unreachable)
    return "observed" if any(o.compaction_event for o in observations) else "unobserved"


def exercise_sessions(
    case: InvestigationScenario,
    send: Callable[[str, str | None], tuple[str, bool, str | None]],
) -> tuple[list[SessionObservation], str]:
    """Send a fresh turn with no prior runtime identity; continuation receives the current one."""
    observations: list[SessionObservation] = []
    current: str | None = None
    for index, turn in enumerate(case.turns, start=1):
        previous = current if turn.mode is SessionMode.CONTINUE else None
        current, saved_read, compaction_event = send(turn.human, previous)
        observations.append(SessionObservation(index, turn.mode, current, saved_read, compaction_event))
    return observations, record_sessions(case, observations)


def assessment_prompt(case: InvestigationScenario, key: InvestigationKey, answer: str) -> str:
    """Keep the new substance rubric separate from the frozen delivery checklist."""
    facts = "\n".join(
        f"{f.id}: {f.status.value}; {f.claim}; locators={f.locators}; window={f.window}; relation={f.relation}"
        for f in key.facts
    )
    measures = ", ".join(m.value for m in Measure)
    return (
        "Assess investigation substance only. Grade every measure independently as pass or fail with direct "
        "answer evidence. Do not treat inference as observation, absence as negation, a resumed identity as "
        "fresh recovery, or length as usefulness. No statistical verdict follows from this assessment.\n"
        'Return JSON: {"schema":"pinboard-investigation-assessment/v1","scenario_id":"<id>",'
        '"results":[{"measure":"<one listed measure>","passed":true,"evidence":"<specific reason>"}]}. '
        "Include every listed measure exactly once.\n"
        f"Case: {case.id}\nFacts:\n{facts}\nConsequential fact: {key.consequential_fact}\n"
        f"Red herring: {key.red_herring}\nHuman choice: {key.human_choice}\n"
        f"Unavailable fact: {key.unavailable_fact}\nValid alternatives: {key.valid_alternatives}\n"
        f"Measures: {measures}\nAgent answer:\n{answer}\n"
    )


def decode_assessment(content: bytes, case: InvestigationScenario) -> InvestigationAssessment:
    assessment = msgspec.json.decode(content, type=InvestigationAssessment)
    if assessment.scenario_id != case.id:
        raise ValueError("assessment names another investigation scenario")
    return assessment
