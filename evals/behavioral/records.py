"""Harness-owned record formats: scenarios, scenario sets, run evidence, scores, label maps, assessments and spend.

Every record here is a format the harness alone writes and reads, so each decodes strictly into a frozen
msgspec record that forbids unknown fields. Runtime output streams are decoded separately at their accepted
external boundaries in the runtime drivers.
"""

from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, NewType

import msgspec

ScenarioId = NewType("ScenarioId", str)
Variant = NewType("Variant", str)
Label = NewType("Label", str)

type NonEmpty = Annotated[str, msgspec.Meta(min_length=1)]
type Sha256 = Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{64}\Z")]
type Usd = Annotated[float, msgspec.Meta(ge=0)]
type Count = Annotated[int, msgspec.Meta(ge=0)]
type Identifier = Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9][a-z0-9.-]*\Z")]


class Record(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Base for harness-owned records."""


class WorldKind(Enum):
    FULL = "full"
    MINIMAL = "minimal"


class WorldExtra(Enum):
    SKIP_COMMENTS_EXPERIMENT = "skip-comments-experiment"
    REVIEWED_CHANGE = "reviewed-change"
    UNREVIEWED_CHANGE = "unreviewed-change"


class Hook(Enum):
    MERGE_EXPERIMENT = "merge-experiment"
    PUSH_SAM_FIX = "push-sam-fix"
    REBASE_SAVED_CHANGE = "rebase-saved-change"
    SQUASH_MERGE_SAVED_CHANGE = "squash-merge-saved-change"


class Turn(Record, frozen=True):
    human: NonEmpty
    before: Hook | None


class Scenario(Record, frozen=True):
    id: Identifier
    title: NonEmpty
    world: WorldKind
    world_extra: WorldExtra | None
    source: NonEmpty
    ground_truth: NonEmpty
    turns: Annotated[list[Turn], msgspec.Meta(min_length=1)]


class ChecklistItem(Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"
    P5 = "P5"
    P6 = "P6"
    P7 = "P7"
    P8 = "P8"
    P9 = "P9"
    P10 = "P10"
    P11 = "P11"
    P12 = "P12"
    N1 = "N1"


class RegisteredScenario(Record, frozen=True):
    id: Identifier
    sha256: Sha256


class ScenarioSet(Record, frozen=True):
    """A registered scenario set; for a comparison it also declares the checklist rules its guidance change targets.

    An empty ``targeted_rules`` declares that the comparison targets no single rule, so its verdict rests on the
    total alone.
    """

    name: Identifier
    scenarios: Annotated[list[RegisteredScenario], msgspec.Meta(min_length=1)]
    targeted_rules: list[ChecklistItem]


class Runtime(Enum):
    CLAUDE_CODE = "claude-code"
    CODEX = "codex"


class RunKey(Record, frozen=True):
    scenario_id: Identifier
    variant: Identifier
    index: Annotated[int, msgspec.Meta(ge=1)]

    def relative(self) -> Path:
        return Path(self.scenario_id) / f"{self.variant}-{self.index}"

    def display(self) -> str:
        return f"{self.scenario_id}/{self.variant}-{self.index}"


class ExportRecord(Record, frozen=True):
    schema: Literal["pinboard-behavioral-export/v1"]
    commit: Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{40}\Z")]
    skills_sha256: Sha256
    plugin_root: NonEmpty


class InventoryEntry(Record, frozen=True):
    kind: Literal["plugin", "skill", "mcp-server", "instruction-file", "hook", "runtime-bundled"]
    name: NonEmpty
    source: NonEmpty


class PermissionDenial(Record, frozen=True):
    tool: NonEmpty
    detail: str


class TurnEvidence(Record, frozen=True):
    """One scripted turn of a run.

    ``cost_usd`` and the token counts are this turn's own share. Both runtimes report session totals when a session
    is resumed, so each driver records the difference from the previous turn's totals, and a run's cost is the sum
    of its turns.
    """

    index: Annotated[int, msgspec.Meta(ge=1)]
    human: NonEmpty
    hook_ran: Hook | None
    session_id: NonEmpty
    final_reply: str
    commentary: list[str]
    started_at: NonEmpty
    finished_at: NonEmpty
    cost_usd: Usd | None
    uncached_input_tokens: Count | None | None
    cached_input_tokens: Count | None
    cache_write_input_tokens: Count | None
    output_tokens: Count | None
    reasoning_output_tokens: Count | None | None
    permission_denials: list[PermissionDenial]


class ClaudeRunDetails(Record, frozen=True, tag="claude-code"):
    permission_mode: Literal["bypassPermissions"]
    cost_basis: Literal["claude total_cost_usd"]


class CodexRunDetails(Record, frozen=True, tag="codex"):
    reasoning_effort: NonEmpty
    permission_profile: NonEmpty
    sandbox_mode: NonEmpty
    approval_policy: NonEmpty
    writable_roots: list[str]
    price_source: NonEmpty
    credential_write_back: bool


type RunDetails = ClaudeRunDetails | CodexRunDetails


class Completed(Record, frozen=True, tag="completed"):
    pass


class Stopped(Record, frozen=True, tag="stopped"):
    reason: NonEmpty


class Failed(Record, frozen=True, tag="failed"):
    stage: NonEmpty
    reason: NonEmpty


type RunOutcome = Completed | Stopped | Failed


class SeededItem(Record, frozen=True):
    board: Literal["project", "scratch"]
    item_id: NonEmpty
    state: NonEmpty


class RunEvidence(Record, frozen=True):
    """Fields shared by current runs and the exact retained v2 format."""

    run: RunKey
    runtime: Runtime
    cli_version: NonEmpty
    model: NonEmpty
    details: RunDetails
    evaluated: ExportRecord
    fixture_difference: str | None
    seeded_host_id: NonEmpty
    seeded_items: list[SeededItem]
    observed_host_ids: list[str]
    inventory: list[InventoryEntry]
    turns: list[TurnEvidence]
    started_at: NonEmpty
    finished_at: NonEmpty
    outcome: RunOutcome

    def cost_usd(self) -> float:
        return sum(turn.cost_usd for turn in self.turns if turn.cost_usd is not None)


class RunRecord(RunEvidence, frozen=True):
    schema: Literal["pinboard-behavioral-run/v3"]
    registration: ScenarioSet
    scenario_sha256: Sha256


class ObservedState(Record, frozen=True):
    name: NonEmpty
    text: str


class Redaction(Record, frozen=True):
    value: NonEmpty
    placeholder: NonEmpty


class ScorerInput(Record, frozen=True):
    """Everything a blind scorer may see for one run, before redaction."""

    schema: Literal["pinboard-behavioral-scorer-input/v1"]
    run: RunKey
    replies: list[str]
    states: list[ObservedState]
    hooks_log: str | None
    redactions: list[Redaction]


class Verdict(Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "n/a"


class ItemVerdict(Record, frozen=True):
    verdict: Verdict
    evidence: str


class ItemVerdicts(Record, frozen=True, rename="upper"):
    p1: ItemVerdict
    p2: ItemVerdict
    p3: ItemVerdict
    p4: ItemVerdict
    p5: ItemVerdict
    p6: ItemVerdict
    p7: ItemVerdict
    p8: ItemVerdict
    p9: ItemVerdict
    p10: ItemVerdict
    p11: ItemVerdict
    p12: ItemVerdict
    n1: ItemVerdict


class ReplyScore(Record, frozen=True):
    turn: Annotated[int, msgspec.Meta(ge=1)]
    items: ItemVerdicts


class ScoreRecord(Record, frozen=True):
    """The frozen checklist's scorer output shape."""

    label: NonEmpty
    replies: Annotated[list[ReplyScore], msgspec.Meta(min_length=1)]
    scenario_pass: bool
    failed_items: list[str]
    permission_denial_suspected: bool


class Scored(Record, frozen=True, tag="scored"):
    pass


class ScoringFailure(Record, frozen=True, tag="scoring-failure"):
    reason: NonEmpty


type ScoringOutcome = Scored | ScoringFailure


class ScorerSession(Record, frozen=True):
    schema: Literal["pinboard-behavioral-scorer-session/v1"]
    label: NonEmpty
    scorer_model: NonEmpty
    checklist_sha256: Sha256
    cost_usd: Usd | None
    outcome: ScoringOutcome


class LabelMapping(Record, frozen=True):
    schema: Literal["pinboard-behavioral-label/v1"]
    label: NonEmpty
    run: RunKey


class SubstanceVerdict(Enum):
    FINAL_REPLY_CARRIES_SUBSTANCE = "final-reply-carries-substance"
    SUBSTANCE_ONLY_IN_COMMENTARY = "substance-only-in-commentary"


class TurnSubstance(Record, frozen=True):
    turn: Annotated[int, msgspec.Meta(ge=1)]
    verdict: SubstanceVerdict
    evidence: str


class SubstanceAnswer(Record, frozen=True):
    """The substance assessor's output shape."""

    label: NonEmpty
    turns: Annotated[list[TurnSubstance], msgspec.Meta(min_length=1)]


class TurnWords(Record, frozen=True):
    turn: Annotated[int, msgspec.Meta(ge=1)]
    commentary_words: Count
    final_reply_words: Count


class Assessed(Record, frozen=True, tag="assessed"):
    answer: SubstanceAnswer


class AssessmentFailure(Record, frozen=True, tag="assessment-failure"):
    reason: NonEmpty


type AssessmentOutcome = Assessed | AssessmentFailure


class AssessmentRecord(Record, frozen=True):
    schema: Literal["pinboard-behavioral-substance/v1"]
    run: RunKey
    assessor_model: NonEmpty
    cost_usd: Usd | None
    words: list[TurnWords]
    outcome: AssessmentOutcome


class ProbeRecord(Record, frozen=True):
    schema: Literal["pinboard-behavioral-probe/v1"]
    name: Identifier
    runtime: Runtime
    description: NonEmpty
    cost_usd: Usd | None
    passed: bool
    findings: list[str]
    inventory: list[InventoryEntry]


class ReviewerUsage(Record, frozen=True):
    thread_id: NonEmpty
    model: NonEmpty
    input_tokens: Count
    cached_input_tokens: Count
    cache_write_input_tokens: Count
    output_tokens: Count
    reasoning_output_tokens: Count


class CodexAccounting(Record, frozen=True):
    schema: Literal["pinboard-behavioral-codex-accounting/v1"]
    main_known_cost_usd: Usd
    main_usage_complete: bool
    reviewer_usage: list[ReviewerUsage]
    reviewer_price_usd: None


class InvestigationTurnRecord(Record, frozen=True):
    index: int
    human: str
    mode: str
    runtime_identity: str
    final_reply: str
    commentary: list[str]
    cost_usd: Usd | None
    input_tokens: int | None
    output_tokens: int | None
    duration_seconds: float
    saved_evidence_read: bool
    compaction_event: str | None
    record_valid: bool | None


class InvestigationRunRecord(Record, frozen=True):
    schema: Literal["pinboard-investigation-run/v1"]
    case_id: str
    scenario_sha256: Sha256
    set_sha256: Sha256
    arm: str
    arm_sha256: Sha256
    export_commit: str
    model: str
    reasoning_effort: str
    cli_version: str
    started_at: str
    finished_at: str
    turns: list[InvestigationTurnRecord]
    accounting: CodexAccounting | None
    outcome: str
    problem: str | None


class ClaudeInvestigationRunRecord(Record, frozen=True):
    """One fresh Claude session, recorded before another paid session can start."""

    schema: Literal["pinboard-investigation-claude-run/v1"]
    case_id: str
    scenario_sha256: Sha256
    set_sha256: Sha256
    arm: str
    arm_sha256: Sha256
    export_commit: str
    model: Literal["claude-haiku-4-5-20251001"]
    cli_version: str
    world: str
    started_at: str
    finished_at: str
    turn: InvestigationTurnRecord | None
    cache_read_input_tokens: Count | None
    cache_creation_input_tokens: Count | None
    cost_usd: Usd | None
    outcome: Literal["completed", "failed"]
    problem: str | None


class InvestigationAssessmentRecord(Record, frozen=True):
    schema: Literal["pinboard-investigation-assessment-session/v1"]
    case_id: str
    arm: str
    index: int
    assessor_model: str
    cost_usd: Usd | None
    problem: str | None


class CoverageWindow(Record, frozen=True):
    schema: Literal["pinboard-behavioral-coverage-window/v1"]
    started_at: NonEmpty
    deadline_at: NonEmpty
    maximum_seconds: Annotated[int, msgspec.Meta(ge=1, le=10800)]
    maximum_runs: Annotated[int, msgspec.Meta(ge=1, le=12)]
    candidate_revision: NonEmpty
    scenario_sha256: list[RegisteredScenario]
    targeted_rules: list[ChecklistItem]
    known_price_cap_usd: Usd
    reviewer_price_exception: Literal["human-authorized-unknown-price"]


class CoverageResult(Record, frozen=True):
    schema: Literal["pinboard-behavioral-coverage-result/v1"]
    status: Literal["completed", "incomplete"]
    reason: str
    completed_runs: list[str]
    scored_runs: list[str]
    assessed_runs: list[str]
    known_cost_usd: Usd
    unreported_main_usage: bool
    reviewer_cost_usd: None
    total_cost_usd: None


def encode(record: msgspec.Struct) -> bytes:
    return msgspec.json.format(msgspec.json.encode(record), indent=2) + b"\n"


def write_new(path: Path, record: msgspec.Struct) -> None:
    """Write one record to a path that must not exist yet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(encode(record))
