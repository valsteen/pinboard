import subprocess
import tempfile
import unittest
from pathlib import Path

from pinboard.adapters.files.root import (
    CandidateRestoreRejection,
    CandidateRestoreSuccess,
    read_working_tree_candidate,
    restore_commit_candidate,
    restore_working_tree_candidate,
)
from pinboard.application.candidate_snapshots import (
    CommitCandidateSnapshot,
    WorkingTreeCandidateSnapshot,
    candidate_snapshot_key,
    canonical_candidate_snapshot_bytes,
    decode_candidate_snapshot,
)


class CandidateSnapshotTest(unittest.TestCase):
    def git(self, cwd: Path, *arguments: str) -> str:
        return subprocess.run(["git", *arguments], cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()

    def repository(self) -> tuple[Path, str]:
        root = Path(tempfile.mkdtemp()).resolve()
        self.git(root, "init", "-b", "main")
        (root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git(root, "add", "tracked.txt")
        self.git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base")
        return root, self.git(root, "rev-parse", "HEAD")

    def test_working_tree_snapshot_is_canonical_and_restores_with_index(self) -> None:
        source, base = self.repository()
        (source / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        observed = read_working_tree_candidate(source)
        snapshot = WorkingTreeCandidateSnapshot(
            "pinboard-candidate-snapshot/v1",
            "attempt-1",
            "item-1",
            observed.identity,
            "main",
            base,
            base,
            "2026-09-15T20:00:00+00:00",
            observed.diff,
        )
        encoded = canonical_candidate_snapshot_bytes(snapshot)
        self.assertEqual(snapshot, decode_candidate_snapshot(encoded))
        self.assertIn("attempt-1-candidate-snapshot-", candidate_snapshot_key(snapshot))

        target = Path(tempfile.mkdtemp()).resolve()
        self.git(target.parent, "clone", "--quiet", str(source), str(target))
        restored = restore_working_tree_candidate(
            target,
            expected_branch="main",
            preimage_revision=base,
            candidate=snapshot.candidate,
            diff=snapshot.diff,
        )
        self.assertEqual(CandidateRestoreSuccess(True, snapshot.candidate), restored)
        self.assertIn("M  tracked.txt", self.git(target, "status", "--short"))
        self.assertEqual(
            CandidateRestoreSuccess(False, snapshot.candidate),
            restore_working_tree_candidate(
                target,
                expected_branch="main",
                preimage_revision=base,
                candidate=snapshot.candidate,
                diff=snapshot.diff,
            ),
        )

    def test_commit_snapshot_fast_forwards_only_from_exact_clean_preimage(self) -> None:
        source, base = self.repository()
        (source / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        self.git(source, "add", "tracked.txt")
        self.git(source, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "candidate")
        candidate = self.git(source, "rev-parse", "HEAD")
        diff = subprocess.run(
            ["git", "diff", "--binary", base, candidate, "--"], cwd=source, check=True, capture_output=True
        ).stdout
        snapshot = CommitCandidateSnapshot(
            "pinboard-candidate-snapshot/v1",
            "attempt-1",
            "item-1",
            candidate,
            "main",
            base,
            base,
            "2026-09-15T20:00:00+00:00",
            diff,
        )
        self.assertEqual(snapshot, decode_candidate_snapshot(canonical_candidate_snapshot_bytes(snapshot)))

        target = Path(tempfile.mkdtemp()).resolve()
        self.git(source, "worktree", "add", "--detach", str(target), base)
        self.git(target, "switch", "-c", "restore-main")
        rejected = restore_commit_candidate(
            target,
            expected_branch="main",
            preimage_revision=base,
            accepted_base_revision=base,
            candidate=candidate,
            diff=diff,
        )
        self.assertIsInstance(rejected, CandidateRestoreRejection)
