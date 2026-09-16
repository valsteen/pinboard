"""Decode dispatch files and present CLI-owned launch instructions."""

import shlex
from dataclasses import replace
from pathlib import Path
from typing import Literal, assert_never

import msgspec

from pinboard.adapters import dispatch_operations
from pinboard.adapters.dispatch_operations import DispatchErrorCode, DispatchFailure, DispatchResult
from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.application import work_brief_models, work_briefs
from pinboard.application.dispatch_models import (
    DispatchEnvironment,
    NativeLaunchEnvelope,
    PromptReferenceView,
    PublishedAgentPrompt,
    dispatch_environment_dec_hook,
)
from pinboard.application.ports import WorkStore
from pinboard.cli import action_selection, agent_launch, cli_commands
from pinboard.cli.cli_output import write_json
from pinboard.cli.errors import CliResult, CommandFailure
from pinboard.domain.errors import EffectDisposition, FailureDetails, FailureFact, FailureMismatch, RetryDisposition
from pinboard.domain.identifiers import HistoryId


class DispatchReadyView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-dispatch-ready/v2"]
    status: Literal["ready"]
    prompt_reference: PromptReferenceView
    native_launch: NativeLaunchEnvelope
    changed_surfaces: tuple[str, ...]


class _DispatchEnvironmentIdentity(msgspec.Struct, frozen=True, forbid_unknown_fields=False):
    schema: str


def _present_dispatch_ready(launch: PublishedAgentPrompt, native_launch: NativeLaunchEnvelope, *, json: bool) -> None:
    if json:
        write_json(
            DispatchReadyView(
                "pinboard-dispatch-ready/v2",
                "ready",
                launch.reference,
                native_launch,
                tuple(surface.value for surface in launch.changed_surfaces),
            )
        )
    else:
        print(native_launch.message)


def read_dispatch_environment(path: Path) -> DispatchResult[DispatchEnvironment]:
    try:
        data = path.read_bytes()
    except OSError as error:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_ENVIRONMENT_UNREADABLE,
            f"Cannot read '{path}': {error}",
            None,
        )
    try:
        identity = msgspec.json.decode(data, type=_DispatchEnvironmentIdentity)
    except msgspec.DecodeError:
        identity = None
    try:
        return msgspec.json.decode(data, type=DispatchEnvironment, dec_hook=dispatch_environment_dec_hook)
    except msgspec.DecodeError as error:
        details = None
        if identity is not None and identity.schema != "pinboard-dispatch/v2":
            details = FailureDetails(
                observed=(FailureFact("environment_schema", identity.schema),),
                mismatches=(FailureMismatch("environment_schema", "pinboard-dispatch/v2", identity.schema),),
                retry=RetryDisposition.CORRECT_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            )
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_ENVIRONMENT_INVALID,
            f"Cannot decode dispatch environment: {error}",
            details,
        )


def _decode_review_choice(
    command: cli_commands.ProjectReviewedDispatchCommand | cli_commands.ProjectCorrectionDispatchCommand,
    review_bytes: bytes,
) -> DispatchResult[dispatch_operations.ReviewedDispatch | dispatch_operations.CorrectionDispatch]:
    if isinstance(command, cli_commands.ProjectCorrectionDispatchCommand):
        correction_review = work_briefs.decode_correction_source_review(review_bytes)
        if isinstance(correction_review, work_brief_models.WorkBriefFailure):
            return dispatch_operations.review_failure(correction_review)
        return dispatch_operations.CorrectionDispatch(
            correction_review, command.review_id, HistoryId(command.correction_history_id)
        )
    decoded_review = work_briefs.decode_work_brief_review(review_bytes)
    if isinstance(decoded_review, work_brief_models.WorkBriefFailure):
        return dispatch_operations.review_failure(decoded_review)
    return dispatch_operations.ReviewedDispatch(decoded_review, command.review_id)


def prepare_dispatch_command(
    roots: cli_commands.ResolvedRoots,
    durable: DurableRoots,
    store: WorkStore,
    command: cli_commands.DispatchCommand,
) -> CliResult[int]:
    """Prepare one installed dispatch request and return every advertised rejection."""

    decoded_environment = read_dispatch_environment(command.environment)
    if isinstance(decoded_environment, DispatchFailure):
        return decoded_environment
    supplied_prompt_bytes: bytes | None = None
    if command.prompt is not None:
        try:
            supplied_prompt_bytes = command.prompt.read_bytes()
        except OSError as error:
            return DispatchFailure(
                DispatchErrorCode.DISPATCH_PROMPT_UNREADABLE,
                f"Cannot read '{command.prompt}': {error}",
                None,
            )
    match command:
        case (
            cli_commands.ProjectReviewedDispatchCommand(brief_review=brief_review_path)
            | cli_commands.ProjectCorrectionDispatchCommand(brief_review=brief_review_path)
        ):
            try:
                review_bytes = brief_review_path.read_bytes()
            except OSError as error:
                return DispatchFailure(
                    DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID,
                    f"Cannot read '{brief_review_path}': {error}",
                    None,
                )
            decoded_choice = _decode_review_choice(command, review_bytes)
            if isinstance(decoded_choice, DispatchFailure):
                return decoded_choice
            choice = decoded_choice
        case cli_commands.ProjectDispatchCommand():
            choice = dispatch_operations.OrdinaryDispatch()
        case _ as unreachable:
            assert_never(unreachable)
    parsed_action = action_selection.parse_action_receipt(command)
    if isinstance(parsed_action, CommandFailure):
        return parsed_action
    selected_action = action_selection.select_current_action(store, parsed_action)
    if isinstance(selected_action, CommandFailure):
        return selected_action
    published_prompt = dispatch_operations.prepare_dispatch(
        store,
        ArtifactRepository(durable),
        roots.source_checkout,
        selected_action,
        command.checkpoint,
        decoded_environment,
        supplied_prompt_bytes,
        choice,
    )
    if isinstance(published_prompt, DispatchFailure):
        published_prompt = agent_launch.dispatch_diagnostics(
            roots.source_checkout, roots.work, command.action_id, published_prompt
        )
        if (
            isinstance(command, cli_commands.ProjectCorrectionDispatchCommand)
            and published_prompt.details is not None
            and any(value.field == "correction_project_revision" for value in published_prompt.details.mismatches)
        ):
            suffix = (
                "dispatch",
                "--action-id",
                command.action_id,
                "--subject-revision",
                command.subject_revision,
                "--task-id",
                command.task_id,
                "--host-id",
                command.host_id,
                "--checkpoint",
                command.checkpoint,
                "--environment",
                str(command.environment),
                "--brief-review",
                str(command.brief_review),
                "--review-id",
                command.review_id,
                *(("--prompt", str(command.prompt)) if command.prompt is not None else ()),
                *(("--json",) if command.json else ()),
            )
            recovery_command = shlex.join(
                (
                    *agent_launch.pinboard_launcher_command(),
                    "--project-root",
                    str(roots.source_checkout),
                    "--work-root",
                    str(roots.work),
                    *suffix,
                )
            )
            published_prompt = replace(
                published_prompt,
                details=replace(
                    published_prompt.details,
                    observed=(
                        *published_prompt.details.observed,
                        FailureFact("reviewed_dispatch_command", recovery_command),
                    ),
                ),
            )
        return published_prompt
    _present_dispatch_ready(
        published_prompt,
        agent_launch.launch_envelope(
            roots.source_checkout,
            roots.work,
            "worker",
            selected_action.capability.subject,
            published_prompt,
            decoded_environment,
        ),
        json=command.json,
    )
    return 0
