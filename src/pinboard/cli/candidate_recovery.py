"""Inspect and restore immutable review candidates without mutating the ledger."""

import sys
from pathlib import Path

from pinboard.adapters.files.artifacts import read_reference
from pinboard.adapters.files.errors import ArtifactError, RootError
from pinboard.adapters.files.root import (
    CandidateRestoreAfterMutationError,
    CandidateRestoreRejection,
    CandidateRestoreSuccess,
    restore_commit_candidate,
    restore_working_tree_candidate,
)
from pinboard.application import candidate_snapshots, ports, query_models
from pinboard.cli import cli_commands, work_inspection_models
from pinboard.cli.cli_output import write_json
from pinboard.cli.errors import CommandErrorCode, CommandFailure, CommandResult, CommittedEffectFailure
from pinboard.domain.errors import (
    ChangedSurface,
    DecisionFailureCode,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    FailureMismatch,
    RetryDisposition,
)
from pinboard.domain.identifiers import AttemptId


def _candidate_failure(code: CommandErrorCode, message: str, attempt_id: AttemptId) -> CommandFailure:
    return CommandFailure(
        code,
        message,
        FailureDetails(
            observed=(FailureFact("attempt_id", str(attempt_id)),),
            mismatches=(),
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def read_candidate_evidence(
    work_root: Path,
    store: ports.WorkStore,
    attempt_id: AttemptId,
    candidate: str | None,
) -> CommandResult[candidate_snapshots.CandidateSnapshotEvidence]:
    context = store.read_candidate_snapshot_context(attempt_id)
    if context is None:
        return _candidate_failure(
            CommandErrorCode.WORK_STATE_INVALID,
            "The attempt has no accepted candidate snapshot.",
            attempt_id,
        )
    return read_candidate_evidence_from_context(work_root, context, candidate)


def read_candidate_evidence_from_context(
    work_root: Path,
    context: query_models.CandidateSnapshotContextFacts,
    candidate: str | None,
) -> CommandResult[candidate_snapshots.CandidateSnapshotEvidence]:
    try:
        encoded = read_reference(work_root, context.reference)
        return candidate_snapshots.verify_candidate_snapshot_context(context, candidate, encoded)
    except (ArtifactError, ValueError) as error:
        return _candidate_failure(CommandErrorCode.WORK_STATE_INVALID, str(error), context.attempt_id)


def recovery_view(
    roots: cli_commands.ResolvedRoots,
    evidence: candidate_snapshots.CandidateSnapshotEvidence,
) -> work_inspection_models.CandidateRecovery:
    snapshot = evidence.snapshot
    kind = "working-tree" if isinstance(snapshot, candidate_snapshots.WorkingTreeCandidateSnapshot) else "commit"
    executable = str(Path(sys.executable).with_name("pinboard"))
    return work_inspection_models.CandidateRecovery(
        kind,
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


def _restore_rejection(
    attempt_id: AttemptId,
    snapshot: candidate_snapshots.CandidateSnapshot,
    rejection: CandidateRestoreRejection,
) -> CommandFailure:
    return CommandFailure(
        DecisionFailureCode.TRANSITION_INPUT_INVALID,
        f"Candidate restore rejected the selected checkout: {rejection.reason}.",
        FailureDetails(
            observed=(
                FailureFact("attempt_id", str(attempt_id)),
                FailureFact("candidate", snapshot.candidate),
                FailureFact("branch", rejection.branch),
                FailureFact("head", rejection.head),
            ),
            mismatches=(FailureMismatch("checkout", "exact clean recovery preimage", rejection.reason),),
            retry=RetryDisposition.CORRECT_INPUT,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def restore_candidate(
    roots: cli_commands.ResolvedRoots,
    store: ports.WorkStore,
    command: cli_commands.CandidateRestoreCommand,
) -> CommandResult[int] | CommittedEffectFailure:
    evidence = read_candidate_evidence(roots.work, store, command.attempt_id, None)
    if isinstance(evidence, CommandFailure):
        return evidence
    snapshot = evidence.snapshot
    try:
        if isinstance(snapshot, candidate_snapshots.WorkingTreeCandidateSnapshot):
            restored = restore_working_tree_candidate(
                roots.source_checkout,
                expected_branch=snapshot.branch,
                preimage_revision=snapshot.preimage_revision,
                candidate=snapshot.candidate,
                diff=snapshot.diff,
            )
        else:
            restored = restore_commit_candidate(
                roots.source_checkout,
                expected_branch=snapshot.branch,
                preimage_revision=snapshot.preimage_revision,
                accepted_base_revision=snapshot.accepted_base_revision,
                candidate=snapshot.candidate,
                diff=snapshot.diff,
            )
    except CandidateRestoreAfterMutationError as error:
        return CommittedEffectFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID.value,
            str(error),
            FailureDetails(
                observed=(FailureFact("candidate", snapshot.candidate),),
                mismatches=(),
                retry=RetryDisposition.DO_NOT_RETRY,
                effect=EffectDisposition.COMMITTED,
                changed_surfaces=(ChangedSurface.SOURCE_CHECKOUT,),
                alternatives=(),
            ),
        )
    except RootError as error:
        return _candidate_failure(CommandErrorCode.WORK_STATE_INVALID, str(error), command.attempt_id)
    if isinstance(restored, CandidateRestoreRejection):
        return _restore_rejection(command.attempt_id, snapshot, restored)
    assert isinstance(restored, CandidateRestoreSuccess)
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
