"""Select reviewed source bytes and plan exact context-bounded batches.

Selection reads only project-relative files from the caller's chosen source
checkout. Planning normalizes, segments, and batches those bytes without
opening Pinboard work state or writing project files.
"""

import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Final

import msgspec

from pinboard.interfaces.brief_source_models import (
    AuthoritySelector,
    BriefSourceBatch,
    BriefSourceLine,
    BriefSourceManifest,
    BriefSourcePlan,
    BriefSourcePlanView,
    BriefSourceRequest,
    BriefSourceSegment,
    BriefSourceSegmentView,
    PlannedBriefSource,
    SelectedBriefSource,
    authority_selector,
)
from pinboard.interfaces.errors import BriefSourceErrorCode, BriefSourceFailure, BriefSourceResult

MARKDOWN_HEADING: Final = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def decode_brief_source_manifest(raw: bytes) -> BriefSourceResult[BriefSourceManifest]:
    try:
        manifest = msgspec.json.decode(raw, type=BriefSourceManifest)
    except (msgspec.DecodeError, ValueError) as error:
        return BriefSourceFailure(
            BriefSourceErrorCode.MANIFEST_INVALID,
            f"Cannot decode brief source manifest: {error}",
        )
    return manifest


def decode_brief_source_plan(raw: bytes) -> BriefSourceResult[BriefSourcePlan]:
    try:
        plan = msgspec.json.decode(raw, type=BriefSourcePlanView)
    except (msgspec.DecodeError, ValueError) as error:
        return BriefSourceFailure(
            BriefSourceErrorCode.PLAN_INVALID,
            f"Cannot decode brief source plan: {error}",
        )

    def segment_from_view(segment: BriefSourceSegmentView) -> BriefSourceSegment:
        return BriefSourceSegment(
            segment.authority_id,
            segment.selector,
            segment.index,
            segment.start_line,
            segment.end_line,
            segment.content_byte_count,
            segment.content_sha256,
            segment.ends_with_newline,
        )

    sources = tuple(
        PlannedBriefSource(
            source.authority_id,
            source.selector,
            source.families,
            source.selected_sha256,
            source.selected_byte_count,
            source.start_line,
            source.end_line,
            source.whole_file,
            tuple(segment_from_view(segment) for segment in source.segments),
        )
        for source in plan.sources
    )
    batches = tuple(
        BriefSourceBatch(
            batch.index,
            batch.content_byte_count,
            batch.estimated_rendered_byte_count,
            tuple(segment_from_view(segment) for segment in batch.segments),
        )
        for batch in plan.batches
    )
    return BriefSourcePlan(plan.schema, plan.manifest_sha256, plan.max_batch_bytes, sources, batches)


def _find_heading_range(lines: tuple[str, ...], heading: str, path: Path) -> BriefSourceResult[tuple[int, int]]:
    matches: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = MARKDOWN_HEADING.fullmatch(line)
        if match is not None and match.group(2) == heading:
            matches.append((index, len(match.group(1))))
    if not matches:
        return BriefSourceFailure(
            BriefSourceErrorCode.SELECTOR_INVALID,
            f"Heading '{heading}' is not in '{path}'.",
        )
    if len(matches) != 1:
        return BriefSourceFailure(
            BriefSourceErrorCode.SELECTOR_INVALID,
            f"Heading '{heading}' is not unique in '{path}'.",
        )
    start, level = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = MARKDOWN_HEADING.fullmatch(lines[index])
        if match is not None and len(match.group(1)) <= level:
            end = index
            break
    return start, end


def select_brief_source(
    source_checkout_root: Path,
    selector: AuthoritySelector,
    *,
    require_utf8: bool,
) -> BriefSourceResult[SelectedBriefSource]:
    path = source_checkout_root / Path(*selector.relative_path.parts)
    try:
        raw = path.read_bytes()
    except OSError as error:
        return BriefSourceFailure(
            BriefSourceErrorCode.SOURCE_UNREADABLE,
            f"Cannot read authority at '{path}': {error}",
        )
    if selector.heading is None:
        if require_utf8:
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                return BriefSourceFailure(
                    BriefSourceErrorCode.SOURCE_NOT_UTF8,
                    f"Authority '{path}' is not UTF-8 text.",
                )
        raw_lines = raw.splitlines(keepends=True)
        lines = tuple(BriefSourceLine(index, content) for index, content in enumerate(raw_lines, start=1))
        return SelectedBriefSource(selector, raw, 1 if lines else 0, len(lines), True, lines)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return BriefSourceFailure(
            BriefSourceErrorCode.SOURCE_NOT_UTF8,
            f"Heading-selected authority '{path}' is not UTF-8 text.",
        )
    text_lines = tuple(text.splitlines())
    heading_range = _find_heading_range(text_lines, selector.heading, path)
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
    prior_ranges: list[tuple[str, PurePosixPath, int, int]],
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


def _split_source_into_segments(
    request: BriefSourceRequest,
    selected: SelectedBriefSource,
    max_batch_bytes: int,
) -> BriefSourceResult[tuple[BriefSourceSegment, ...]]:
    if not selected.lines:
        return (_compose_segment(request, 0, ()),)
    segments: list[BriefSourceSegment] = []
    current_segment_lines: list[BriefSourceLine] = []
    current_segment_bytes = 0
    for line in selected.lines:
        line_bytes = len(line.content)
        if line_bytes > max_batch_bytes:
            return BriefSourceFailure(
                BriefSourceErrorCode.LINE_TOO_LARGE,
                f"Line {line.number} selected by '{request.authority_id}' is {line_bytes} bytes; "
                f"the limit is {max_batch_bytes}.",
            )
        if current_segment_lines and current_segment_bytes + line_bytes > max_batch_bytes:
            segments.append(_compose_segment(request, len(segments), tuple(current_segment_lines)))
            current_segment_lines = []
            current_segment_bytes = 0
        current_segment_lines.append(line)
        current_segment_bytes += line_bytes
    if current_segment_lines:
        segments.append(_compose_segment(request, len(segments), tuple(current_segment_lines)))
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


def _group_segments_into_batches(
    segments: tuple[BriefSourceSegment, ...], max_batch_bytes: int
) -> tuple[BriefSourceBatch, ...]:
    batches: list[BriefSourceBatch] = []
    current_batch_segments: list[BriefSourceSegment] = []
    current_batch_bytes = 0
    for segment in segments:
        if current_batch_segments and current_batch_bytes + segment.content_byte_count > max_batch_bytes:
            batches.append(_compose_batch(len(batches), tuple(current_batch_segments)))
            current_batch_segments = []
            current_batch_bytes = 0
        current_batch_segments.append(segment)
        current_batch_bytes += segment.content_byte_count
    if current_batch_segments:
        batches.append(_compose_batch(len(batches), tuple(current_batch_segments)))
    return tuple(batches)


def plan_brief_sources(
    source_checkout_root: Path,
    manifest: BriefSourceManifest,
    max_batch_bytes: int,
) -> BriefSourceResult[BriefSourcePlan]:
    selected_ranges: list[tuple[str, PurePosixPath, int, int]] = []
    planned_sources: list[PlannedBriefSource] = []
    all_segments: list[BriefSourceSegment] = []
    for request in manifest.sources:
        selected = select_brief_source(
            source_checkout_root,
            authority_selector(request.selector),
            require_utf8=True,
        )
        if isinstance(selected, BriefSourceFailure):
            return selected
        if (failure := _reject_overlap(request, selected, selected_ranges)) is not None:
            return failure
        selected_ranges.append(
            (request.authority_id, selected.selector.relative_path, selected.start_line, selected.end_line)
        )
        segments = _split_source_into_segments(request, selected, max_batch_bytes)
        if isinstance(segments, BriefSourceFailure):
            return segments
        all_segments.extend(segments)
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
    return BriefSourcePlan(
        "pinboard-brief-source-plan/v1",
        hashlib.sha256(canonical_manifest).hexdigest(),
        max_batch_bytes,
        tuple(planned_sources),
        _group_segments_into_batches(tuple(all_segments), max_batch_bytes),
    )


def render_brief_source_batch(
    source_checkout_root: Path, plan: BriefSourcePlan, batch_index: int
) -> BriefSourceResult[bytes]:
    if batch_index < 0 or batch_index >= len(plan.batches):
        return BriefSourceFailure(
            BriefSourceErrorCode.BATCH_NOT_FOUND,
            f"Batch {batch_index} is outside the available range 0..{len(plan.batches) - 1}.",
        )
    sources = {source.authority_id: source for source in plan.sources}
    rendered_segments: list[tuple[BriefSourceSegment, bytes]] = []
    selected_id: str | None = None
    selected_source: SelectedBriefSource | None = None
    for segment in plan.batches[batch_index].segments:
        source = sources[segment.authority_id]
        if segment.authority_id != selected_id:
            selected = select_brief_source(
                source_checkout_root,
                authority_selector(source.selector),
                require_utf8=True,
            )
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
    return rendered
