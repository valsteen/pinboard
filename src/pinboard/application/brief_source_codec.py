"""Source-plan boundary conversion, canonical bytes and selected-output receipts; no effects."""

import hashlib

import msgspec

from pinboard.application import brief_source_models as models


def _segment_view(segment: models.BriefSourceSegment) -> models.BriefSourceSegmentView:
    return models.BriefSourceSegmentView(
        segment.authority_id,
        segment.selector,
        segment.index,
        segment.start_line,
        segment.end_line,
        segment.content_byte_count,
        segment.content_sha256,
        segment.ends_with_newline,
    )


def project_brief_source_plan(plan: models.BriefSourcePlan) -> models.BriefSourcePlanView:
    return models.BriefSourcePlanView(
        plan.schema,
        plan.manifest_sha256,
        plan.max_batch_bytes,
        tuple(
            models.BriefSourceView(
                source.authority_id,
                source.selector,
                source.families,
                source.selected_sha256,
                source.selected_byte_count,
                source.start_line,
                source.end_line,
                source.whole_file,
                tuple(_segment_view(segment) for segment in source.segments),
            )
            for source in plan.sources
        ),
        tuple(
            models.BriefSourceBatchView(
                batch.index,
                batch.content_byte_count,
                batch.estimated_rendered_byte_count,
                tuple(_segment_view(segment) for segment in batch.segments),
            )
            for batch in plan.batches
        ),
    )


def plan_from_view(plan: models.BriefSourcePlanView) -> models.BriefSourcePlan:
    def segment_from_view(segment: models.BriefSourceSegmentView) -> models.BriefSourceSegment:
        return models.BriefSourceSegment(
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
        models.PlannedBriefSource(
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
        models.BriefSourceBatch(
            batch.index,
            batch.content_byte_count,
            batch.estimated_rendered_byte_count,
            tuple(segment_from_view(segment) for segment in batch.segments),
        )
        for batch in plan.batches
    )
    return models.BriefSourcePlan(plan.schema, plan.manifest_sha256, plan.max_batch_bytes, sources, batches)


def decode_brief_source_plan(raw: bytes) -> models.BriefSourceResult[models.BriefSourcePlan]:
    try:
        plan = msgspec.json.decode(raw, type=models.BriefSourcePlanView)
    except (msgspec.DecodeError, ValueError) as error:
        return models.BriefSourceFailure(
            models.BriefSourceErrorCode.PLAN_INVALID, f"Cannot decode brief source plan: {error}"
        )
    return plan_from_view(plan)


def encode_brief_source_plan(plan: models.BriefSourcePlan) -> bytes:
    return msgspec.json.format(msgspec.json.encode(project_brief_source_plan(plan), order="sorted"), indent=2) + b"\n"


def plan_output_receipt(
    destination: str,
    created: bool,
    plan_bytes: bytes,
    source_plan: models.BriefSourcePlan,
) -> models.BriefSourcePlanOutputReceipt:
    return models.BriefSourcePlanOutputReceipt(
        "pinboard-brief-source-plan-output/v1",
        destination,
        created,
        hashlib.sha256(plan_bytes).hexdigest(),
        len(plan_bytes),
        len(source_plan.sources),
        len(source_plan.batches),
        sum(source.selected_byte_count for source in source_plan.sources),
    )
