from dataclasses import dataclass
from itertools import pairwise
from pathlib import PurePosixPath
from typing import Annotated, Literal

import msgspec

from pinboard.interfaces.errors import BriefSourceErrorCode, BriefSourceFailure, BriefSourceResult

type BriefSourceManifestSchema = Literal["pinboard-brief-sources/v1"]
type BriefSourcePlanSchema = Literal["pinboard-brief-source-plan/v1"]
type BriefSourceIdentity = Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]
type BriefSourceSelector = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\n]+\z")]
type BriefSourceSha256 = Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{64}\z")]
type NonNegativeInt = Annotated[int, msgspec.Meta(ge=0)]
type PositiveInt = Annotated[int, msgspec.Meta(ge=1)]


@dataclass(frozen=True, slots=True)
class AuthoritySelector:
    relative_path: PurePosixPath
    heading: str | None


def parse_authority_selector(value: str) -> BriefSourceResult[AuthoritySelector]:
    relative, separator, heading = value.partition("#")
    relative_path = PurePosixPath(relative)
    if (
        not relative
        or "\x00" in value
        or relative_path.is_absolute()
        or ".." in relative_path.parts
        or not relative_path.parts
        or (separator and not heading)
    ):
        return BriefSourceFailure(
            BriefSourceErrorCode.MANIFEST_INVALID,
            f"Authority selector '{value}' must name one project-relative file and optional literal heading.",
        )
    return AuthoritySelector(relative_path, heading if separator else None)


def authority_selector(value: BriefSourceSelector) -> AuthoritySelector:
    relative, separator, heading = value.partition("#")
    return AuthoritySelector(PurePosixPath(relative), heading if separator else None)


def _validate_source_identity(selector: BriefSourceSelector, families: tuple[BriefSourceIdentity, ...]) -> None:
    if isinstance(failure := parse_authority_selector(selector), BriefSourceFailure):
        raise ValueError(failure.message)
    if not families or len(set(families)) != len(families):
        raise ValueError("families must contain one or more unique kebab-case values")


class BriefSourceRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    authority_id: BriefSourceIdentity
    selector: BriefSourceSelector
    families: tuple[BriefSourceIdentity, ...]

    def __post_init__(self) -> None:
        _validate_source_identity(self.selector, self.families)


class BriefSourceManifest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: BriefSourceManifestSchema
    sources: tuple[BriefSourceRequest, ...]

    def __post_init__(self) -> None:
        authority_ids = tuple(source.authority_id for source in self.sources)
        if not authority_ids or len(set(authority_ids)) != len(authority_ids):
            raise ValueError("sources must contain one or more uniquely identified authorities")


@dataclass(frozen=True, slots=True)
class BriefSourceLine:
    number: int
    content: bytes


@dataclass(frozen=True, slots=True)
class SelectedBriefSource:
    selector: AuthoritySelector
    content: bytes
    start_line: int
    end_line: int
    whole_file: bool
    lines: tuple[BriefSourceLine, ...]


@dataclass(frozen=True, slots=True)
class BriefSourceSegment:
    authority_id: str
    selector: str
    index: int
    start_line: int
    end_line: int
    content_byte_count: int
    content_sha256: str
    ends_with_newline: bool


@dataclass(frozen=True, slots=True)
class PlannedBriefSource:
    authority_id: str
    selector: str
    families: tuple[str, ...]
    selected_sha256: str
    selected_byte_count: int
    start_line: int
    end_line: int
    whole_file: bool
    segments: tuple[BriefSourceSegment, ...]


@dataclass(frozen=True, slots=True)
class BriefSourceBatch:
    index: int
    content_byte_count: int
    estimated_rendered_byte_count: int
    segments: tuple[BriefSourceSegment, ...]


@dataclass(frozen=True, slots=True)
class BriefSourcePlan:
    schema: BriefSourcePlanSchema
    manifest_sha256: str
    max_batch_bytes: int
    sources: tuple[PlannedBriefSource, ...]
    batches: tuple[BriefSourceBatch, ...]


class BriefSourceSegmentView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    authority_id: BriefSourceIdentity
    selector: BriefSourceSelector
    index: NonNegativeInt
    start_line: NonNegativeInt
    end_line: NonNegativeInt
    content_byte_count: NonNegativeInt
    content_sha256: BriefSourceSha256
    ends_with_newline: bool


class BriefSourceView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    authority_id: BriefSourceIdentity
    selector: BriefSourceSelector
    families: tuple[BriefSourceIdentity, ...]
    selected_sha256: BriefSourceSha256
    selected_byte_count: NonNegativeInt
    start_line: NonNegativeInt
    end_line: NonNegativeInt
    whole_file: bool
    segments: tuple[BriefSourceSegmentView, ...]

    def __post_init__(self) -> None:
        _validate_source_identity(self.selector, self.families)
        if self.whole_file != (authority_selector(self.selector).heading is None):
            raise ValueError("source whole-file flag must match the selector")
        if not self.segments or tuple(segment.index for segment in self.segments) != tuple(range(len(self.segments))):
            raise ValueError("source segments must be nonempty and have contiguous zero-based indexes")
        if any(
            segment.authority_id != self.authority_id or segment.selector != self.selector for segment in self.segments
        ):
            raise ValueError("source segments must repeat their owning authority and selector")
        empty_partition = (
            self.start_line == 0
            and self.end_line == 0
            and len(self.segments) == 1
            and self.segments[0].start_line == 0
            and self.segments[0].end_line == 0
        )
        nonempty_partition = (
            self.start_line >= 1
            and self.end_line >= self.start_line
            and self.segments[0].start_line == self.start_line
            and self.segments[-1].end_line == self.end_line
            and all(segment.end_line >= segment.start_line for segment in self.segments)
            and all(previous.end_line + 1 == following.start_line for previous, following in pairwise(self.segments))
        )
        if not empty_partition and not nonempty_partition:
            raise ValueError("source segments must exactly partition the selected line range in order")
        if sum(segment.content_byte_count for segment in self.segments) != self.selected_byte_count:
            raise ValueError("source segment sizes must equal the selected source size")


class BriefSourceBatchView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    index: NonNegativeInt
    content_byte_count: NonNegativeInt
    estimated_rendered_byte_count: NonNegativeInt
    segments: tuple[BriefSourceSegmentView, ...]

    def __post_init__(self) -> None:
        if not self.segments:
            raise ValueError("source batches must contain at least one segment")
        if sum(segment.content_byte_count for segment in self.segments) != self.content_byte_count:
            raise ValueError("batch segment sizes must equal the batch content size")


class BriefSourcePlanView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: BriefSourcePlanSchema
    manifest_sha256: BriefSourceSha256
    max_batch_bytes: PositiveInt
    sources: tuple[BriefSourceView, ...]
    batches: tuple[BriefSourceBatchView, ...]

    def __post_init__(self) -> None:
        authority_ids = tuple(source.authority_id for source in self.sources)
        if not authority_ids or len(set(authority_ids)) != len(authority_ids):
            raise ValueError("plan sources must contain one or more uniquely identified authorities")
        if tuple(batch.index for batch in self.batches) != tuple(range(len(self.batches))):
            raise ValueError("source batches must have contiguous zero-based indexes")
        if any(batch.content_byte_count > self.max_batch_bytes for batch in self.batches):
            raise ValueError("source batch content sizes must not exceed the plan limit")
        source_segments = tuple(segment for source in self.sources for segment in source.segments)
        batch_segments = tuple(segment for batch in self.batches for segment in batch.segments)
        if source_segments != batch_segments:
            raise ValueError("source batches must contain every planned segment exactly once in source order")
