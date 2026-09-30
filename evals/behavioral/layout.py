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

from evals.behavioral.records import (
    AssessmentRecord,
    CodexAccounting,
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

    def run_records(self) -> Iterator[RunRecord]:
        for path in sorted((self.root / "runs").glob(f"*/*/{RUN_RECORD}")):
            yield msgspec.json.decode(path.read_bytes(), type=RunRecord)

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
