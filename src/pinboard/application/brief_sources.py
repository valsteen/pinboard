"""Select reviewed source bytes and plan exact context-bounded batches."""

import hashlib
import re
from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath
from typing import Final, Protocol

import msgspec

from pinboard.application.brief_source_models import (
    AuthoritySelector,
    BriefSourceBatch,
    BriefSourceErrorCode,
    BriefSourceFailure,
    BriefSourceLine,
    BriefSourceManifest,
    BriefSourcePlan,
    BriefSourceRequest,
    BriefSourceResult,
    BriefSourceSegment,
    PlannedBriefSource,
    SelectedBriefSource,
    authority_selector,
)

MARKDOWN_HEADING: Final = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
MAX_PRESENTED_BATCH_BYTES: Final = 16_000


class BriefSourceSelector(Protocol):
    """Acquire and select one authority through an outer-owned capability."""

    def __call__(
        self,
        selector: AuthoritySelector,
        require_utf8: bool,
    ) -> BriefSourceResult[SelectedBriefSource]: ...


def _find_heading_range(
    lines: tuple[str, ...], heading: str, relative_path: PurePosixPath
) -> BriefSourceResult[tuple[int, int]]:
    matches: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = MARKDOWN_HEADING.fullmatch(line)
        if match is not None and match.group(2) == heading:
            matches.append((index, len(match.group(1))))
    if not matches:
        return BriefSourceFailure(
            BriefSourceErrorCode.SELECTOR_INVALID,
            f"Heading '{heading}' is not in '{relative_path}'.",
        )
    if len(matches) != 1:
        return BriefSourceFailure(
            BriefSourceErrorCode.SELECTOR_INVALID,
            f"Heading '{heading}' is not unique in '{relative_path}'.",
        )
    start, level = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = MARKDOWN_HEADING.fullmatch(lines[index])
        if match is not None and len(match.group(1)) <= level:
            end = index
            break
    return start, end


def select_brief_source_bytes(
    selector: AuthoritySelector,
    raw: bytes,
    require_utf8: bool,
) -> BriefSourceResult[SelectedBriefSource]:
    if selector.heading is None:
        if require_utf8:
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                return BriefSourceFailure(
                    BriefSourceErrorCode.SOURCE_NOT_UTF8,
                    f"Authority '{selector.relative_path}' is not UTF-8 text.",
                )
        raw_lines = raw.splitlines(keepends=True)
        lines = tuple(BriefSourceLine(index, content) for index, content in enumerate(raw_lines, start=1))
        return SelectedBriefSource(selector, raw, 1 if lines else 0, len(lines), True, lines)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return BriefSourceFailure(
            BriefSourceErrorCode.SOURCE_NOT_UTF8,
            f"Heading-selected authority '{selector.relative_path}' is not UTF-8 text.",
        )
    text_lines = tuple(text.splitlines())
    heading_range = _find_heading_range(text_lines, selector.heading, selector.relative_path)
    if isinstance(heading_range, BriefSourceFailure):
        return heading_range
    start, end = heading_range
    selected_lines = tuple(BriefSourceLine(index + 1, f"{text_lines[index]}\n".encode()) for index in range(start, end))
    return SelectedBriefSource(
        selector,
        b"".join(line.content for line in selected_lines),
        start + 1,
        end,
        False,
        selected_lines,
    )


def _reject_overlap(
    request: BriefSourceRequest,
    selected: SelectedBriefSource,
    prior_ranges: Sequence[tuple[str, PurePosixPath, int, int]],
) -> BriefSourceFailure | None:
    for prior_id, prior_path, prior_start, prior_end in prior_ranges:
        if selected.selector.relative_path != prior_path:
            continue
        if max(selected.start_line, prior_start) <= min(selected.end_line, prior_end):
            return BriefSourceFailure(
                BriefSourceErrorCode.SELECTOR_OVERLAP,
                f"Authorities '{prior_id}' and '{request.authority_id}' select overlapping lines in "
                f"'{selected.selector.relative_path}'.",
            )
    return None


def _compose_segment(
    request: BriefSourceRequest,
    index: int,
    lines: tuple[BriefSourceLine, ...],
) -> BriefSourceSegment:
    content = b"".join(line.content for line in lines)
    return BriefSourceSegment(
        request.authority_id,
        request.selector,
        index,
        lines[0].number if lines else 0,
        lines[-1].number if lines else 0,
        len(content),
        hashlib.sha256(content).hexdigest(),
        content.endswith(b"\n"),
    )


def _append_source_segments(
    request: BriefSourceRequest,
    selected: SelectedBriefSource,
    max_batch_bytes: int,
    batches: list[BriefSourceBatch],
    current_batch: list[tuple[BriefSourceSegment, bytes]],
) -> BriefSourceResult[tuple[BriefSourceSegment, ...]]:
    """Build source segments in their actual batches; mutate only these local accumulators."""
    segments: list[BriefSourceSegment] = []
    current_segment_lines: list[BriefSourceLine] = []
    line_groups: Iterable[tuple[BriefSourceLine, ...]] = (
        ((line,) for line in selected.lines) if selected.lines else ((),)
    )
    # ponytail: Reprice each bounded segment; use incremental escaping counts if very long line sets become slow.
    for new_lines in line_groups:
        candidate_lines = (*current_segment_lines, *new_lines)
        candidate = _compose_segment(request, len(segments), candidate_lines)
        content = b"".join(line.content for line in candidate_lines)
        if _presented_size(len(batches), (*current_batch, (candidate, content))) > max_batch_bytes:
            if current_segment_lines:
                segment = _compose_segment(request, len(segments), tuple(current_segment_lines))
                segments.append(segment)
                current_batch.append((segment, b"".join(line.content for line in current_segment_lines)))
                current_segment_lines = []
            if current_batch:
                batches.append(_compose_batch(len(batches), tuple(segment for segment, _ in current_batch)))
                current_batch.clear()
            candidate = _compose_segment(request, len(segments), new_lines)
            content = b"".join(line.content for line in new_lines)
            size = _presented_size(len(batches), ((candidate, content),))
            if size > max_batch_bytes:
                selection = (
                    f"Line {new_lines[0].number} selected by '{request.authority_id}'"
                    if new_lines
                    else f"Empty selection '{request.authority_id}'"
                )
                return BriefSourceFailure(
                    BriefSourceErrorCode.LINE_TOO_LARGE,
                    f"{selection} requires {size} presented bytes; limit is {max_batch_bytes}. "
                    f"Use a shorter selector, split this line or raise the limit (maximum {MAX_PRESENTED_BATCH_BYTES}).",
                )
        current_segment_lines.extend(new_lines)
    segments.append(candidate)
    current_batch.append((candidate, content))
    return tuple(segments)


def _segment_header(segment: BriefSourceSegment) -> bytes:
    return (
        f"===== BEGIN BRIEF SOURCE authority={segment.authority_id} selector={segment.selector} "
        f"lines={segment.start_line}-{segment.end_line} segment={segment.index} =====\n"
    ).encode()


def _segment_footer(segment: BriefSourceSegment) -> bytes:
    return f"===== END BRIEF SOURCE authority={segment.authority_id} segment={segment.index} =====\n".encode()


def _render_segments(segments: tuple[tuple[BriefSourceSegment, bytes], ...]) -> bytes:
    rendered: list[bytes] = []
    for segment, content in segments:
        rendered.append(_segment_header(segment))
        rendered.append(content)
        if content and not content.endswith(b"\n"):
            rendered.append(b"\n")
        rendered.append(_segment_footer(segment))
    return b"".join(rendered)


def brief_source_batch_payload(index: int, content_byte_count: int, rendered: bytes) -> dict[str, str | int]:
    payload: dict[str, str | int] = {
        "schema": "pinboard-brief-source-batch/v1",
        "batch_index": index,
        "content_byte_count": content_byte_count,
        "rendered_byte_count": len(rendered),
        "presented_byte_count": 0,
        "text": rendered.decode("utf-8"),
    }
    while (size := len(msgspec.json.encode(payload))) != payload["presented_byte_count"]:
        payload["presented_byte_count"] = size
    return payload


def _presented_size(index: int, segments: tuple[tuple[BriefSourceSegment, bytes], ...]) -> int:
    rendered = _render_segments(segments)
    return int(
        brief_source_batch_payload(index, sum(len(content) for _, content in segments), rendered)[
            "presented_byte_count"
        ]
    )


def _compose_batch(index: int, segments: tuple[BriefSourceSegment, ...]) -> BriefSourceBatch:
    return BriefSourceBatch(
        index,
        sum(segment.content_byte_count for segment in segments),
        sum(
            len(_segment_header(segment))
            + segment.content_byte_count
            + (1 if segment.content_byte_count and not segment.ends_with_newline else 0)
            + len(_segment_footer(segment))
            for segment in segments
        ),
        segments,
    )


def plan_brief_sources(
    select_source: BriefSourceSelector,
    manifest: BriefSourceManifest,
    max_batch_bytes: int,
) -> BriefSourceResult[BriefSourcePlan]:
    max_batch_bytes = min(max_batch_bytes, MAX_PRESENTED_BATCH_BYTES)
    selected_ranges: list[tuple[str, PurePosixPath, int, int]] = []
    planned_sources: list[PlannedBriefSource] = []
    batches: list[BriefSourceBatch] = []
    current_batch: list[tuple[BriefSourceSegment, bytes]] = []
    for request in manifest.sources:
        selected = select_source(authority_selector(request.selector), True)
        if isinstance(selected, BriefSourceFailure):
            return selected
        if (failure := _reject_overlap(request, selected, selected_ranges)) is not None:
            return failure
        selected_ranges.append(
            (request.authority_id, selected.selector.relative_path, selected.start_line, selected.end_line)
        )
        segments = _append_source_segments(request, selected, max_batch_bytes, batches, current_batch)
        if isinstance(segments, BriefSourceFailure):
            return segments
        planned_sources.append(
            PlannedBriefSource(
                request.authority_id,
                request.selector,
                request.families,
                hashlib.sha256(selected.content).hexdigest(),
                len(selected.content),
                selected.start_line,
                selected.end_line,
                selected.whole_file,
                segments,
            )
        )
    canonical_manifest = msgspec.json.encode(manifest, order="sorted")
    if current_batch:
        batches.append(_compose_batch(len(batches), tuple(segment for segment, _ in current_batch)))
    return BriefSourcePlan(
        "pinboard-brief-source-plan/v1",
        hashlib.sha256(canonical_manifest).hexdigest(),
        max_batch_bytes,
        tuple(planned_sources),
        tuple(batches),
    )


def render_brief_source_batch(
    select_source: BriefSourceSelector, plan: BriefSourcePlan, batch_index: int
) -> BriefSourceResult[bytes]:
    if batch_index < 0 or batch_index >= len(plan.batches):
        return BriefSourceFailure(
            BriefSourceErrorCode.BATCH_NOT_FOUND,
            f"Batch {batch_index} is outside the available range 0..{len(plan.batches) - 1}.",
        )
    if plan.batches[batch_index].estimated_rendered_byte_count > min(plan.max_batch_bytes, MAX_PRESENTED_BATCH_BYTES):
        return BriefSourceFailure(
            BriefSourceErrorCode.PLAN_INVALID,
            f"Batch {batch_index} plans {plan.batches[batch_index].estimated_rendered_byte_count} rendered bytes; "
            f"the presented result limit is {min(plan.max_batch_bytes, MAX_PRESENTED_BATCH_BYTES)}. "
            "Create a smaller plan before emitting this batch.",
        )
    sources = {source.authority_id: source for source in plan.sources}
    rendered_segments: list[tuple[BriefSourceSegment, bytes]] = []
    selected_id: str | None = None
    selected_source: SelectedBriefSource | None = None
    for segment in plan.batches[batch_index].segments:
        source = sources[segment.authority_id]
        if segment.authority_id != selected_id:
            selected = select_source(authority_selector(source.selector), True)
            if isinstance(selected, BriefSourceFailure):
                return selected
            if (
                hashlib.sha256(selected.content).hexdigest() != source.selected_sha256
                or len(selected.content) != source.selected_byte_count
                or selected.start_line != source.start_line
                or selected.end_line != source.end_line
            ):
                return BriefSourceFailure(
                    BriefSourceErrorCode.SOURCE_CHANGED,
                    f"Authority '{segment.authority_id}' changed after its source plan was created.",
                )
            selected_source = selected
            selected_id = segment.authority_id
        assert selected_source is not None
        content = b"".join(
            line.content for line in selected_source.lines if segment.start_line <= line.number <= segment.end_line
        )
        if (
            len(content) != segment.content_byte_count
            or hashlib.sha256(content).hexdigest() != segment.content_sha256
            or content.endswith(b"\n") != segment.ends_with_newline
        ):
            return BriefSourceFailure(
                BriefSourceErrorCode.SOURCE_CHANGED,
                f"Authority '{segment.authority_id}' changed after its source plan was created.",
            )
        rendered_segments.append((segment, content))
    rendered = _render_segments(tuple(rendered_segments))
    if len(rendered) != plan.batches[batch_index].estimated_rendered_byte_count:
        return BriefSourceFailure(
            BriefSourceErrorCode.PLAN_INVALID,
            f"Batch {batch_index} rendered size does not match its source plan.",
        )
    size = _presented_size(batch_index, tuple(rendered_segments))
    if size > min(plan.max_batch_bytes, MAX_PRESENTED_BATCH_BYTES):
        return BriefSourceFailure(
            BriefSourceErrorCode.PLAN_INVALID,
            f"Batch {batch_index} requires {size} presented bytes; "
            f"the limit is {min(plan.max_batch_bytes, MAX_PRESENTED_BATCH_BYTES)}. "
            "Create a smaller plan before emitting this batch.",
        )
    return rendered
