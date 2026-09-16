"""Restore retained patch-only snapshots from their recorded exact preimage."""

from pathlib import Path

from pinboard.adapters.files import root
from pinboard.application.candidate_identity import working_tree_identity


def restore_working_tree_candidate(
    cwd: Path, *, expected_branch: str, preimage_revision: str, candidate: str, diff: bytes
) -> root.CandidateRestoreResult:
    restored = root.restore_working_tree_candidate(
        cwd,
        expected_branch=expected_branch,
        preimage_revision=preimage_revision,
        candidate=working_tree_identity(preimage_revision, diff),
        diff=diff,
    )
    if isinstance(restored, root.CandidateRestoreRejection):
        return restored
    return root.CandidateRestoreSuccess(restored.changed, candidate)
