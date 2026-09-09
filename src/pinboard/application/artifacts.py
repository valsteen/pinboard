from dataclasses import dataclass, field
from typing import Literal

from pinboard.domain import work_models


@dataclass(frozen=True, slots=True)
class NewArtifact:
    kind: work_models.ArtifactKind
    key: str
    revision: int
    suffix: str
    content: bytes


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    kind: work_models.ArtifactKind
    key: str
    revision: int
    selector: str
    content_sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ArtifactPublication:
    reference: ArtifactRef
    created: bool


@dataclass(frozen=True, slots=True)
class BriefArtifactRef:
    key: str
    revision: int
    selector: str
    content_sha256: str
    size_bytes: int
    kind: Literal[work_models.ArtifactKind.BRIEF]


@dataclass(frozen=True, slots=True)
class ResultArtifactRef:
    key: str
    revision: int
    selector: str
    content_sha256: str
    size_bytes: int
    kind: Literal[work_models.ArtifactKind.RESULT] = field(init=False, default=work_models.ArtifactKind.RESULT)


@dataclass(frozen=True, slots=True)
class EvidenceArtifactRef:
    key: str
    revision: int
    selector: str
    content_sha256: str
    size_bytes: int
    kind: Literal[work_models.ArtifactKind.EVIDENCE] = field(init=False, default=work_models.ArtifactKind.EVIDENCE)


@dataclass(frozen=True, slots=True)
class CheckpointArtifacts:
    result: ResultArtifactRef
    review: EvidenceArtifactRef
    package: EvidenceArtifactRef


@dataclass(frozen=True, slots=True)
class WorkBriefIdentity:
    attempt_id: str
    item_id: str
    branch: str
    base_revision: str
    accepted_scope_revision: int
    accepted_scope_digest: str
