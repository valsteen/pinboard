"""Present accepted candidate recovery and CLI restoration results."""

import sys
from pathlib import Path

from pinboard.adapters import candidate_evidence
from pinboard.application import candidate_snapshots, ports
from pinboard.cli import cli_commands, work_inspection_models
from pinboard.cli.cli_output import write_json
from pinboard.cli.errors import CommandErrorCode, CommandFailure, CommandResult, CommittedEffectFailure
from pinboard.domain.errors import (
    DecisionFailure,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    RetryDisposition,
)
from pinboard.domain.identifiers import AttemptId


def candidate_failure_view(failure: DecisionFailure, attempt_id: AttemptId) -> CommandFailure:
    """Preserve the CLI's state-error presentation for unavailable verified bytes."""
    if failure.details is not None:
        return CommandFailure(failure.code, failure.message, failure.details)
    return CommandFailure(
        CommandErrorCode.WORK_STATE_INVALID,
        failure.message,
        FailureDetails(
            observed=(FailureFact("attempt_id", str(attempt_id)),),
            mismatches=(),
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def recovery_view(
    roots: cli_commands.ResolvedRoots,
    evidence: candidate_snapshots.CandidateSnapshotEvidence,
) -> work_inspection_models.CandidateRecovery:
    snapshot = evidence.snapshot
    executable = str(Path(sys.executable).with_name("pinboard"))
    return work_inspection_models.CandidateRecovery(
        candidate_snapshots.candidate_kind(snapshot),
        snapshot.candidate,
        snapshot.branch,
        snapshot.preimage_revision,
        int(evidence.reference.artifact_ref_id),
        evidence.reference.selector,
        evidence.reference.content_sha256,
        evidence.reference.size_bytes,
        (
            executable,
            "--project-root",
            "<exact-clean-checkout>",
            "--work-root",
            str(roots.work),
            "candidate",
            "restore",
            "--attempt-id",
            snapshot.attempt_id,
            "--json",
        ),
    )


def restore_candidate(
    roots: cli_commands.ResolvedRoots,
    store: ports.WorkStore,
    command: cli_commands.CandidateRestoreCommand,
) -> CommandResult[int] | CommittedEffectFailure:
    restored = candidate_evidence.restore_candidate(
        roots.source_checkout,
        roots.work,
        store,
        command.attempt_id,
        None,
    )
    if isinstance(restored, DecisionFailure):
        if restored.details is not None and restored.details.effect == EffectDisposition.COMMITTED:
            return CommittedEffectFailure(restored.code.value, restored.message, restored.details)
        return candidate_failure_view(restored, command.attempt_id)
    write_json(
        work_inspection_models.CandidateRestoreView(
            "pinboard-candidate-restore/v1",
            str(command.attempt_id),
            restored.candidate,
            restored.changed,
            str(roots.source_checkout),
        )
    )
    return 0
