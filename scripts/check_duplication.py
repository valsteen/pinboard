"""Enforce exact reviewed native pairs, zero unreviewed new clones and the existing ceiling.

Certification binds occurrence bytes and semantic-review records, not the truth of
the reviewer's judgment. Native jscpd owns baseline classification and statistics:
https://github.com/kucherenko/jscpd/blob/6b25b3a6b84fe59740956594134f7b379a78d30c/rust/crates/cpd-finder/src/statistics.rs
"""

import argparse
import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import msgspec

type NonEmpty = Annotated[str, msgspec.Meta(min_length=1)]
type Sha256 = Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{64}\z")]
type TaskUuid = Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\z")]
type Positive = Annotated[int, msgspec.Meta(ge=1)]
type NonNegative = Annotated[int, msgspec.Meta(ge=0)]


class Location(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    line: Positive
    column: NonNegative
    position: NonNegative


class Occurrence(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: NonEmpty
    start: Positive
    end: Positive
    startLoc: Location
    endLoc: Location

    def __post_init__(self) -> None:
        if self.startLoc.line != self.start or self.endLoc.line != self.end:
            raise ValueError("Native occurrence labels and locations disagree")
        if self.end < self.start or self.endLoc.position <= self.startLoc.position:
            raise ValueError("Native occurrence span must be nonempty and ordered")
        path = Path(self.name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Native occurrence must be relative to the scanned source root")


class NativePair(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    firstFile: Occurrence
    secondFile: Occurrence
    format: Literal["python"]
    fragment: str
    isNew: bool
    lines: Positive
    tokens: Positive


class Statistics(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    clones: NonNegative
    duplicatedLines: NonNegative
    duplicatedTokens: NonNegative
    lines: NonNegative
    newClones: NonNegative
    newDuplicatedLines: NonNegative
    percentage: float
    percentageTokens: float
    sources: NonNegative
    tokens: NonNegative


class ReportStatistics(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    detectionDate: NonEmpty
    formats: dict[str, Statistics]
    total: Statistics


class NativeReport(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    duplicates: tuple[NativePair, ...]
    statistics: ReportStatistics


class CertifiedOccurrence(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    endpoint: Occurrence
    sha256: Sha256


class ReviewedPair(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    id: NonEmpty
    format: Literal["python"]
    tokens: Positive
    lines: Positive
    occurrences: tuple[CertifiedOccurrence, CertifiedOccurrence]
    owners: Annotated[tuple[NonEmpty, ...], msgspec.Meta(min_length=1)]
    rationale: NonEmpty


class Certification(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    reviewer_task_id: TaskUuid
    reviewed_pair_sha256: Sha256
    evidence_sha256: Sha256


class ExceptionRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    identity: ReviewedPair
    certification: Certification | None


class Exceptions(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-duplication-exceptions/v1"]
    detector_version: Literal["5.1.2"]
    records: tuple[ExceptionRecord, ...]


type StableOccurrence = tuple[str, int, int, int, int]
type PairIdentity = tuple[frozenset[StableOccurrence], str, int, int]
type ContentOccurrence = tuple[str, int, int, str]
type ContentPairIdentity = tuple[tuple[ContentOccurrence, ContentOccurrence], str, int, int]


@dataclass(frozen=True, slots=True)
class PolicyResult:
    raw_pairs: int
    exempt_pairs: int
    nonexempt_lines: int
    source_lines: int
    percentage: float
    errors: tuple[str, ...]


def reviewed_pair_digest(identity: ReviewedPair) -> str:
    return hashlib.sha256(msgspec.json.encode(identity, order="sorted")).hexdigest()


def occurrence_digest(source_root: Path, occurrence: Occurrence) -> str:
    raw = (source_root / occurrence.name).read_bytes()
    if occurrence.endLoc.position > len(raw):
        raise ValueError("Certified matched bytes are missing")
    return hashlib.sha256(raw[occurrence.startLoc.position : occurrence.endLoc.position]).hexdigest()


def _stable_occurrence(occurrence: Occurrence) -> StableOccurrence:
    return (
        occurrence.name,
        occurrence.start,
        occurrence.end,
        occurrence.startLoc.column,
        occurrence.endLoc.column,
    )


def native_pair_identity(pair: NativePair) -> PairIdentity:
    endpoints = frozenset((_stable_occurrence(pair.firstFile), _stable_occurrence(pair.secondFile)))
    return endpoints, pair.format, pair.tokens, pair.lines


def _content_pair_identity(pair: NativePair, source_root: Path) -> ContentPairIdentity:
    endpoints = tuple(
        sorted(
            (
                value.name,
                value.startLoc.column,
                value.endLoc.column,
                occurrence_digest(source_root, value),
            )
            for value in (pair.firstFile, pair.secondFile)
        )
    )
    return (endpoints[0], endpoints[1]), pair.format, pair.tokens, pair.lines


def _certified_pair_identity(identity: ReviewedPair) -> ContentPairIdentity:
    endpoints = tuple(
        sorted(
            (
                value.endpoint.name,
                value.endpoint.startLoc.column,
                value.endpoint.endLoc.column,
                value.sha256,
            )
            for value in identity.occurrences
        )
    )
    return (endpoints[0], endpoints[1]), identity.format, identity.tokens, identity.lines


def evaluate(report: NativeReport, exceptions: Exceptions, source_root: Path) -> PolicyResult:
    """Validate certification before omitting any native pair contribution."""
    errors: list[str] = []
    exempt: set[PairIdentity] = set()
    reviewed_occurrences: set[StableOccurrence] = set()
    for record in exceptions.records:
        identity = record.identity
        certification = record.certification
        if certification is None or certification.reviewed_pair_sha256 != reviewed_pair_digest(identity):
            errors.append(f"{identity.id}: missing or changed certification; separate review is required.")
            continue
        expected_pair = _certified_pair_identity(identity)
        matches: list[NativePair] = []
        for pair in report.duplicates:
            try:
                if _content_pair_identity(pair, source_root) == expected_pair:
                    matches.append(pair)
            except OSError, ValueError:
                continue
        if not matches:
            errors.append(f"{identity.id}: certified native pair is missing or changed.")
            continue
        if len(matches) != 1:
            errors.append(f"{identity.id}: certified native pair matches multiple current pairs.")
            continue
        current_pair = matches[0]
        identity_key = native_pair_identity(current_pair)
        exempt.add(identity_key)
        reviewed_occurrences.update(_stable_occurrence(value) for value in (current_pair.firstFile, current_pair.secondFile))
    raw_lines = sum(pair.firstFile.end - pair.firstFile.start for pair in report.duplicates)
    if (len(report.duplicates), raw_lines) != (report.statistics.total.clones, report.statistics.total.duplicatedLines):
        errors.append("Native raw statistics disagree with pinned pair contributions.")
    nonexempt = tuple(pair for pair in report.duplicates if native_pair_identity(pair) not in exempt)
    errors.extend(
        f"Unreviewed pair: {pair.firstFile.name}:{pair.firstFile.start} ~ {pair.secondFile.name}:{pair.secondFile.start}."
        for pair in nonexempt
        if pair.isNew or native_pair_identity(pair)[0] & reviewed_occurrences
    )
    # Native 5.1.2 sums the first endpoint's end-minus-start for every pair;
    # JSON pair.lines includes one additional endpoint line. Never union spans:
    # an overlapping nonexempt pair still contributes its entire native count.
    nonexempt_lines = sum(pair.firstFile.end - pair.firstFile.start for pair in nonexempt)
    source_lines = report.statistics.total.lines
    if nonexempt_lines * 1000 > source_lines * 3:
        errors.append("Nonexempt duplication exceeds the existing 0.3 percent ceiling.")
    return PolicyResult(
        len(report.duplicates),
        len(report.duplicates) - len(nonexempt),
        nonexempt_lines,
        source_lines,
        100 * nonexempt_lines / source_lines if source_lines else 0.0,
        tuple(errors),
    )


def run_detector(
    detector_root: Path, repository_root: Path, source_root: Path, baseline_ref: str, report_dir: Path
) -> NativeReport:
    binary = detector_root / "node_modules/.bin/jscpd"
    version = subprocess.check_output([str(binary), "--version"], text=True).strip()
    if version != "jscpd 5.1.2":
        raise ValueError("Duplication certification requires pinned native jscpd 5.1.2")
    subprocess.run(
        [
            str(binary),
            "--config",
            str(detector_root / ".jscpd.json"),
            "--min-lines",
            "8",
            "--min-tokens",
            "60",
            "--baseline-from-ref",
            baseline_ref,
            "--reporters",
            "json,ai",
            "--output",
            str(report_dir),
            "--no-tips",
            str(source_root),
        ],
        cwd=repository_root,
        check=True,
    )
    return msgspec.json.decode((report_dir / "jscpd-report.json").read_bytes(), type=NativeReport)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="origin/main")
    parser.add_argument("--report-dir", type=Path, default=Path(".codex/audits/duplication"))
    arguments = parser.parse_args()
    repository_root = Path.cwd()
    source_root = repository_root / "src/pinboard"
    exceptions = msgspec.json.decode((repository_root / ".jscpd-exceptions.json").read_bytes(), type=Exceptions)
    report = run_detector(
        repository_root, repository_root, source_root, arguments.baseline_ref, arguments.report_dir.resolve()
    )
    result = evaluate(report, exceptions, source_root)
    print(
        f"Raw pairs: {result.raw_pairs}; reviewed exempt pairs: {result.exempt_pairs}; "
        f"nonexempt: {result.nonexempt_lines}/{result.source_lines} lines ({result.percentage:.6f}%)."
    )
    print(f"Raw native evidence: {arguments.report_dir / 'jscpd-report.json'}")
    for error in result.errors:
        print(error)
    return bool(result.errors)


if __name__ == "__main__":
    raise SystemExit(main())
