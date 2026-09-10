"""Retrospective agent-legibility baseline; intentionally excluded from the installed package."""

import re
from typing import Annotated, Literal

import msgspec

type NonEmptyString = Annotated[str, msgspec.Meta(min_length=1)]
type CommitIdentity = Annotated[str, msgspec.Meta(pattern="^[0-9a-f]{40}$")]
type PositiveInt = Annotated[int, msgspec.Meta(ge=1)]
type NonNegativeInt = Annotated[int, msgspec.Meta(ge=0)]
type OptionalNonNegativeInt = NonNegativeInt | None
type NonEmptyStrings = Annotated[tuple[NonEmptyString, ...], msgspec.Meta(min_length=1)]
type CaseId = Literal[
    "large-cross-boundary-delivery",
    "persistence-fixed-point-cleanup",
    "local-dto-simplification",
]
type CandidateKind = Literal["commit", "working-tree-sha256"]


class EvidenceSource(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    authority_id: NonEmptyString
    selected_bytes: PositiveInt
    selector: NonEmptyString
    sha256: Annotated[str, msgspec.Meta(pattern="^[0-9a-f]{64}$")]


class OwnerLocalization(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    correct_owners: NonEmptyStrings
    observation: NonEmptyString


class MaintenanceCase(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    base_identity: CommitIdentity
    branch: NonEmptyString
    candidate_identity: NonEmptyString
    candidate_kind: CandidateKind
    case_id: CaseId
    context_evidence_ids: tuple[NonEmptyString, ...]
    correction_rounds: NonNegativeInt
    elapsed_seconds: OptionalNonNegativeInt
    meaningful_edit_site_count: PositiveInt
    meaningful_edit_sites: NonEmptyStrings
    owner_localization: OwnerLocalization
    primary_evidence_id: NonEmptyString
    selected_source_bytes: PositiveInt
    title: NonEmptyString
    token_count: OptionalNonNegativeInt
    verification: NonEmptyStrings
    wrong_paths_explored: tuple[NonEmptyString, ...]

    def __post_init__(self) -> None:
        pattern = "^[0-9a-f]{40}$" if self.candidate_kind == "commit" else "^working-tree-sha256:[0-9a-f]{64}$"
        if re.fullmatch(pattern, self.candidate_identity) is None:
            raise ValueError(f"candidate identity does not match {self.candidate_kind}")


class Bottlenecks(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    correction: NonEmptyString
    locality: NonEmptyString
    navigation: NonEmptyString
    recommendations: NonEmptyStrings
    verification: NonEmptyString


class Methodology(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    cost_policy: NonEmptyString
    evidence_plan_digest: Annotated[str, msgspec.Meta(pattern="^[0-9a-f]{64}$")]
    evidence_selected_bytes: PositiveInt
    measurement_scope: NonEmptyString
    readiness_interpretation: NonEmptyString


class AgentReadinessBaseline(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    bottlenecks: Bottlenecks
    cases: Annotated[tuple[MaintenanceCase, ...], msgspec.Meta(min_length=3, max_length=3)]
    evidence_sources: Annotated[tuple[EvidenceSource, ...], msgspec.Meta(min_length=1)]
    methodology: Methodology
    schema: Literal["pinboard-agent-readiness-baseline/v1"]

    def __post_init__(self) -> None:
        evidence_by_id = {source.authority_id: source for source in self.evidence_sources}
        if len(evidence_by_id) != len(self.evidence_sources):
            raise ValueError("evidence authority IDs must be unique")
        expected_case_ids: tuple[CaseId, ...] = (
            "large-cross-boundary-delivery",
            "persistence-fixed-point-cleanup",
            "local-dto-simplification",
        )
        if tuple(case.case_id for case in self.cases) != expected_case_ids:
            raise ValueError("the three representative cases must appear once in canonical order")
        evidence_assignments = dict.fromkeys(evidence_by_id, 0)
        for case in self.cases:
            case_evidence_ids = (case.primary_evidence_id, *case.context_evidence_ids)
            if len(set(case_evidence_ids)) != len(case_evidence_ids):
                raise ValueError("a case cannot reference the same evidence twice")
            try:
                selected_bytes = sum(evidence_by_id[evidence_id].selected_bytes for evidence_id in case_evidence_ids)
            except KeyError as error:
                raise ValueError(f"unknown evidence authority: {error.args[0]}") from error
            if selected_bytes != case.selected_source_bytes:
                raise ValueError("case selected source bytes must equal its exact evidence selections")
            for evidence_id in case_evidence_ids:
                evidence_assignments[evidence_id] += 1
        if any(count != 1 for count in evidence_assignments.values()):
            raise ValueError("every selected evidence authority must belong to exactly one representative case")
        if self.methodology.evidence_selected_bytes != sum(source.selected_bytes for source in self.evidence_sources):
            raise ValueError("methodology evidence bytes must equal the selected evidence total")


def decode_baseline(source: bytes) -> AgentReadinessBaseline:
    baseline = msgspec.json.decode(source, type=AgentReadinessBaseline, strict=True)
    if source != msgspec.json.encode(baseline, order="sorted") + b"\n":
        raise msgspec.ValidationError("baseline JSON must use sorted canonical encoding with one trailing newline")
    return baseline


def render_baseline(baseline: AgentReadinessBaseline) -> str:
    lines = [
        "# Agent readiness and change-locality baseline",
        "",
        baseline.methodology.measurement_scope,
        "",
        f"Evidence plan: `{baseline.methodology.evidence_plan_digest}`; "
        f"{baseline.methodology.evidence_selected_bytes:,} exact selected bytes.",
        "",
        f"Cost policy: {baseline.methodology.cost_policy}",
        "",
        f"Interpretation: {baseline.methodology.readiness_interpretation}",
        "",
        "## Evidence sources",
        "",
        "| Authority | Exact selector | Selected bytes | SHA-256 |",
        "| --- | --- | ---: | --- |",
    ]
    lines.extend(
        f"| `{source.authority_id}` | `{source.selector}` | {source.selected_bytes:,} | `{source.sha256}` |"
        for source in baseline.evidence_sources
    )
    lines.extend(["", "## Representative cases", ""])
    for case in baseline.cases:
        elapsed = "not preserved" if case.elapsed_seconds is None else f"{case.elapsed_seconds:,} seconds"
        tokens = "not preserved" if case.token_count is None else f"{case.token_count:,}"
        context_evidence = ", ".join(case.context_evidence_ids) if case.context_evidence_ids else "none"
        lines.extend(
            [
                f"### {case.title}",
                "",
                f"- Case: `{case.case_id}`",
                f"- Candidate: `{case.candidate_identity}` ({case.candidate_kind}) over `{case.base_identity}` on `{case.branch}`",
                f"- Primary evidence: `{case.primary_evidence_id}`",
                f"- Context evidence: `{context_evidence}`",
                f"- Selected source bytes: {case.selected_source_bytes:,}",
                f"- Meaningful edit sites: {case.meaningful_edit_site_count}",
                f"- Correction rounds: {case.correction_rounds}",
                f"- Elapsed cost: {elapsed}",
                f"- Token cost: {tokens}",
                "",
                "Correct-owner localization:",
                "",
                *[f"- {owner}" for owner in case.owner_localization.correct_owners],
                "",
                case.owner_localization.observation,
                "",
                "Meaningful edit-site groups:",
                "",
                *[f"- {site}" for site in case.meaningful_edit_sites],
                "",
                "Wrong paths explored:",
                "",
                *(
                    [f"- {path}" for path in case.wrong_paths_explored]
                    if case.wrong_paths_explored
                    else ["- None directly recorded in the selected result evidence."]
                ),
                "",
                "Verification breadth:",
                "",
                *[f"- {check}" for check in case.verification],
                "",
            ]
        )
    lines.extend(
        [
            "## Observed bottlenecks",
            "",
            "### Navigation",
            "",
            baseline.bottlenecks.navigation,
            "",
            "### Locality",
            "",
            baseline.bottlenecks.locality,
            "",
            "### Verification",
            "",
            baseline.bottlenecks.verification,
            "",
            "### Correction",
            "",
            baseline.bottlenecks.correction,
            "",
            "## Gradual improvements",
            "",
            *[f"- {recommendation}" for recommendation in baseline.bottlenecks.recommendations],
            "",
        ]
    )
    return "\n".join(lines)
