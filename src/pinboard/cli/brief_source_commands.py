"""Plan, persist, or emit deterministic reviewed-authority source batches.

This command owner acquires selected files and presents stdout or an explicit
immutable destination. Application owners retain selection and plan codecs.
"""

import sys
from functools import partial
from typing import assert_never

from pinboard.adapters.files.brief_sources import select_checkout_brief_source
from pinboard.adapters.files.file_io import create_immutable
from pinboard.application import brief_source_codec, brief_sources
from pinboard.application.brief_source_models import BriefSourceErrorCode, BriefSourceFailure, BriefSourceResult
from pinboard.cli import cli_commands, cli_output


def plan_or_emit_brief_sources(
    roots: cli_commands.ResolvedRoots,
    command: (
        cli_commands.BriefSourcesPlanCommand
        | cli_commands.BriefSourcesPlanToFileCommand
        | cli_commands.BriefSourcesEmitCommand
    ),
) -> BriefSourceResult[int]:
    match command:
        case (
            cli_commands.BriefSourcesPlanCommand(file=manifest_path, max_batch_bytes=max_batch_bytes)
            | cli_commands.BriefSourcesPlanToFileCommand(file=manifest_path, max_batch_bytes=max_batch_bytes)
        ):
            try:
                manifest_bytes = manifest_path.read_bytes()
            except OSError as error:
                return BriefSourceFailure(
                    BriefSourceErrorCode.MANIFEST_INVALID,
                    f"Cannot read brief source manifest '{manifest_path}': {error}",
                )
            decoded_manifest = brief_sources.decode_brief_source_manifest(manifest_bytes)
            if isinstance(decoded_manifest, BriefSourceFailure):
                return decoded_manifest
            source_plan = brief_sources.plan_brief_sources(
                partial(select_checkout_brief_source, roots.source_checkout),
                decoded_manifest,
                max_batch_bytes,
            )
            if isinstance(source_plan, BriefSourceFailure):
                return source_plan
            plan_bytes = brief_source_codec.encode_brief_source_plan(source_plan)
            if isinstance(command, cli_commands.BriefSourcesPlanCommand):
                sys.stdout.write(plan_bytes.decode())
            else:
                destination = command.output_plan.absolute()
                created = create_immutable(destination, plan_bytes)
                cli_output.write_json(
                    brief_source_codec.plan_output_receipt(str(destination), created, plan_bytes, source_plan)
                )
        case cli_commands.BriefSourcesEmitCommand(plan=plan_path, emit_batch=batch_index):
            try:
                plan_bytes = plan_path.read_bytes()
            except OSError as error:
                return BriefSourceFailure(
                    BriefSourceErrorCode.PLAN_INVALID,
                    f"Cannot read brief source plan '{plan_path}': {error}",
                )
            source_plan = brief_source_codec.decode_brief_source_plan(plan_bytes)
            if isinstance(source_plan, BriefSourceFailure):
                return source_plan
            rendered_batch = brief_sources.render_brief_source_batch(
                partial(select_checkout_brief_source, roots.source_checkout),
                source_plan,
                batch_index,
            )
            if isinstance(rendered_batch, BriefSourceFailure):
                return rendered_batch
            sys.stdout.write(rendered_batch.decode("utf-8"))
        case _ as unreachable:
            assert_never(unreachable)
    return 0
