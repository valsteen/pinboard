"""CLI file acquisition and rooted retained-v1 recovery presentation."""

import shlex
from pathlib import Path

from pinboard.adapters import review_operations
from pinboard.cli import agent_launch, cli_commands, errors
from pinboard.domain import errors as domain_errors


def read_candidate_patch(path: Path) -> errors.CommandResult[bytes]:
    try:
        return path.read_bytes()
    except OSError as error:
        return errors.CommandFailure(
            domain_errors.DecisionFailureCode.TRANSITION_INPUT_INVALID,
            f"Cannot read candidate patch: {error}",
            domain_errors.FailureDetails(
                observed=(domain_errors.FailureFact("candidate_patch", str(path)),),
                mismatches=(),
                retry=domain_errors.RetryDisposition.CORRECT_INPUT,
                effect=domain_errors.EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )


def candidate_required_failure(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.ReviewJobCommand,
    required: review_operations.CompatibilityCandidateRequired,
) -> errors.CommandFailure:
    recovery_command = shlex.join(
        (
            *agent_launch.pinboard_launcher_command(),
            "--project-root",
            str(roots.source_checkout),
            "--work-root",
            str(roots.work),
            "review-job",
            "--attempt-id",
            str(command.attempt_id),
            "--candidate-revision",
            command.candidate_revision,
            "--checkpoint-history-id",
            str(int(required.checkpoint_history_id)),
            *(
                ("--correction-history-id", str(command.correction_history_id))
                if isinstance(
                    command,
                    (
                        cli_commands.PackageCorrectionReviewJobCommand,
                        cli_commands.CompatibilityPackageCorrectionRecoveryReviewJobCommand,
                    ),
                )
                else ()
            ),
            "--candidate-patch",
            "CANDIDATE_PATCH",
            "--json",
        )
    )
    return errors.CommandFailure(
        domain_errors.DecisionFailureCode.ACTION_NOT_AVAILABLE,
        "Selected checkpoint candidate bytes are not accepted; use the exact recovery command with the matching patch.",
        domain_errors.FailureDetails(
            observed=(
                domain_errors.FailureFact("checkpoint_history_id", int(required.checkpoint_history_id)),
                domain_errors.FailureFact("accepted_candidate", required.package.candidate),
                domain_errors.FailureFact("candidate_evidence_reference", None),
                domain_errors.FailureFact("recovery_command", recovery_command),
            ),
            mismatches=(domain_errors.FailureMismatch("candidate_evidence", "accepted immutable reference", None),),
            retry=domain_errors.RetryDisposition.CORRECT_INPUT,
            effect=domain_errors.EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )
