"""Present CLI-origin native launch verification and worker startup."""

import shlex
import sys
from dataclasses import replace
from pathlib import Path
from typing import Literal, assert_never

from pinboard.adapters.dispatch_operations import DispatchErrorCode, DispatchFailure
from pinboard.application import dispatch_models
from pinboard.domain.errors import DecisionFailureCode, FailureFact


def pinboard_launcher_command() -> tuple[str, ...]:
    """Use the installed console script without depending on PATH."""
    return (str(Path(sys.executable).with_name("pinboard")),)


FRESH_REVIEW_PREPARATION_COMMAND: str = shlex.join(
    (*pinboard_launcher_command(), "tool-contract", "--operation", "brief-sources:plan", "--json")
)


def dispatch_diagnostics(
    project_root: Path, work_root: Path, action_id: str, failure: DispatchFailure
) -> DispatchFailure:
    """Add installed CLI recovery presentation without changing shared failure facts."""
    details = failure.details
    if details is None:
        return failure
    prefix = (*pinboard_launcher_command(), "--project-root", str(project_root), "--work-root", str(work_root))
    observations: tuple[FailureFact, ...] = ()
    match failure.code:
        case DispatchErrorCode.DISPATCH_BASE_REVISION_MISMATCH:
            observations = (
                FailureFact(
                    "tool_contract_command", shlex.join((*prefix, "tool-contract", "--operation", "dispatch", "--json"))
                ),
                FailureFact(
                    "current_dispatch_action_command",
                    shlex.join((*prefix, "actions", "--role", "project", "--action-id", action_id, "--json")),
                ),
            )
        case DispatchErrorCode.DISPATCH_BRIEF_REVIEW_STALE | DispatchErrorCode.DISPATCH_AUTHORITY_STALE:
            observations = (FailureFact("fresh_review_preparation_command", FRESH_REVIEW_PREPARATION_COMMAND),)
        case DispatchErrorCode() | DecisionFailureCode():
            return failure
        case _ as unreachable:
            assert_never(unreachable)
    return replace(failure, details=replace(details, observed=(*details.observed, *observations)))


def launch_envelope(
    project_root: Path,
    work_root: Path,
    prompt_role: Literal["worker", "reviewer"],
    attempt_id: str,
    publication: dispatch_models.PublishedAgentPrompt,
    environment: dispatch_models.DispatchEnvironment | None,
) -> dispatch_models.NativeLaunchEnvelope:
    reference = publication.reference
    prefix = (
        *pinboard_launcher_command(),
        "--project-root",
        str(project_root),
        "--work-root",
        str(work_root),
    )
    verification_command = shlex.join(
        (
            *prefix,
            "artifact",
            "verify",
            "--artifact-ref-id",
            str(reference.accepted_artifact_reference_id),
            "--selector",
            reference.selector,
            "--sha256",
            reference.sha256,
            "--size-bytes",
            str(reference.size_bytes),
            "--json",
        )
    )
    message = (
        f"Use accepted artifact reference {reference.accepted_artifact_reference_id}, the immutable {prompt_role} "
        f"prompt at '{work_root / reference.selector}'. Before any acquisition, implementation, or review, run exactly: "
        f"{verification_command}. Require `pinboard-verified-artifact-reference/v1`, then read exactly "
        f"{reference.size_bytes} bytes from that path. Stop before acting if the accepted identity, selector, size, "
        "digest, verification result, or bytes differ. After verification, follow those exact bytes as the complete task prompt."
    )
    if environment is not None:
        acquisition = shlex.join(
            (
                *prefix,
                "attempt",
                "acquire",
                "--attempt-id",
                attempt_id,
                "--task-id",
                "<post-launch CODEX_THREAD_ID>",
                "--host-id",
                str(environment.host_id),
                "--ttl-seconds",
                str(environment.lease_ttl_seconds),
                "--json",
            )
        )
        continuation = shlex.join(
            (
                *prefix,
                "actions",
                "--role",
                "worker",
                "--lease-id",
                "<returned lease_id>",
                "--generation",
                "<returned generation>",
                "--action-id",
                f"continue:{attempt_id}",
                "--json",
            )
        )
        message += (
            " After reading the complete canonical brief/bootstrap, use its explicitly supported CLI-origin startup: "
            f"read CODEX_THREAD_ID after launch, then {acquisition}; follow the returned lease with {continuation}. "
            "Substitute only the trusted post-launch identity and returned lease facts. Never acquire as the invoking task."
        )
    return dispatch_models.NativeLaunchEnvelope("pinboard-native-agent-launch/v1", "native-subagent", message)
