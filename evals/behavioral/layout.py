"""The evaluation output directory: one caller-named private directory holding every run, score and spend record.

runs/<scenario>/<variant>-<n>/    run.json, scorer-input.json, turn-<k>.jsonl, state-<k>.txt, hooks.log,
                                  rollout.jsonl (Codex)
scores/<label>/                   session.json, score.json, prompt.txt, raw.json (no run identity)
labels/<label>.json               the label-to-run map, kept apart from the scores
assessments/<scenario>/<variant>-<n>/assessment.json, prompt.txt, raw.json
probes/<name>/probe.json
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import msgspec

from evals.behavioral.compatibility_records import CompatibilityRunRecord
from evals.behavioral.records import (
    AssessmentRecord,
    ClaudeInvestigationRunRecord,
    CodexAccounting,
    InvestigationAssessmentRecord,
    InvestigationRunRecord,
    LabelMapping,
    ProbeRecord,
    RunKey,
    RunRecord,
    ScoreRecord,
    ScorerInput,
    ScorerSession,
)

RUN_RECORD = "run.json"
SCORER_INPUT = "scorer-input.json"
SCENARIO_RECORD = "scenario.json"
type RecordedRun = RunRecord | CompatibilityRunRecord


class InvestigationSchema(msgspec.Struct, frozen=True):
    schema: str


@dataclass(frozen=True)
class Layout:
    root: Path

    def run_directory(self, run: RunKey) -> Path:
        return self.root / "runs" / run.relative()

    def score_directory(self, label: str) -> Path:
        return self.root / "scores" / label

    def label_file(self, label: str) -> Path:
        return self.root / "labels" / f"{label}.json"

    def assessment_directory(self, run: RunKey) -> Path:
        return self.root / "assessments" / run.relative()

    def probe_file(self, name: str) -> Path:
        return self.root / "probes" / name / "probe.json"

    def run_records(self) -> Iterator[RecordedRun]:
        for path in sorted((self.root / "runs").glob(f"*/*/{RUN_RECORD}")):
            yield decode_run(path.read_bytes())

    def run_record(self, key: RunKey) -> RecordedRun | None:
        path = self.run_directory(key) / RUN_RECORD
        return decode_run(path.read_bytes()) if path.is_file() else None

    def scorer_inputs(self) -> Iterator[ScorerInput]:
        for path in sorted((self.root / "runs").glob(f"*/*/{SCORER_INPUT}")):
            yield msgspec.json.decode(path.read_bytes(), type=ScorerInput)

    def scorer_sessions(self) -> Iterator[ScorerSession]:
        for path in sorted((self.root / "scores").glob("*/session.json")):
            yield msgspec.json.decode(path.read_bytes(), type=ScorerSession)

    def score(self, label: str) -> ScoreRecord:
        return msgspec.json.decode((self.score_directory(label) / "score.json").read_bytes(), type=ScoreRecord)

    def labels(self) -> Iterator[LabelMapping]:
        for path in sorted((self.root / "labels").glob("*.json")):
            yield msgspec.json.decode(path.read_bytes(), type=LabelMapping)

    def assessments(self) -> Iterator[AssessmentRecord]:
        for path in sorted((self.root / "assessments").glob("*/*/assessment.json")):
            yield msgspec.json.decode(path.read_bytes(), type=AssessmentRecord)

    def probes(self) -> Iterator[ProbeRecord]:
        for path in sorted((self.root / "probes").glob("*/probe.json")):
            yield msgspec.json.decode(path.read_bytes(), type=ProbeRecord)

    def codex_accounting(self) -> Iterator[tuple[Path, CodexAccounting]]:
        for path in sorted(
            [*(self.root / "runs").glob("*/*/accounting.json"), *(self.root / "probes").glob("*/accounting.json")]
        ):
            yield path, msgspec.json.decode(path.read_bytes(), type=CodexAccounting)

    def investigation_runs(self) -> Iterator[InvestigationRunRecord | ClaudeInvestigationRunRecord]:
        for path in sorted((self.root / "investigations").glob("*/*/run.json")):
            content = path.read_bytes()
            schema = msgspec.json.decode(content, type=InvestigationSchema).schema
            match schema:
                case "pinboard-investigation-run/v1":
                    yield msgspec.json.decode(content, type=InvestigationRunRecord)
                case "pinboard-investigation-claude-run/v1":
                    yield msgspec.json.decode(content, type=ClaudeInvestigationRunRecord)
                case _:
                    raise ValueError(f"unsupported investigation record schema in {path}: {schema}")

    def investigation_assessments(self) -> Iterator[InvestigationAssessmentRecord]:
        for path in sorted((self.root / "investigation-assessments").glob("*/*/session.json")):
            yield msgspec.json.decode(path.read_bytes(), type=InvestigationAssessmentRecord)


def decode_run(content: bytes) -> RecordedRun:
    """Select an exact current or retained schema before decoding its complete shape."""
    schema = msgspec.json.decode(content, type=InvestigationSchema).schema
    match schema:
        case "pinboard-behavioral-run/v3":
            return msgspec.json.decode(content, type=RunRecord)
        case "pinboard-behavioral-run/v2":
            return msgspec.json.decode(content, type=CompatibilityRunRecord)
        case _:
            raise ValueError(f"unsupported behavioral run schema: {schema}")
