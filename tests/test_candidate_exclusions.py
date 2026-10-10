"""Declared local files survive exact candidate persistence, qualification and recovery."""

import subprocess
import tempfile
from pathlib import Path

from pinboard.adapters import candidate_evidence, dispatch_operations
from pinboard.adapters.files import root
from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import candidate_snapshots, query_models, work_brief_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import AttemptId, HistoryId
from pinboard.mcp import server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool
from tests.support import JsonObject, JsonValue


class CandidateExclusionsTest(CheckpointPackageSupport):
    def git(self, checkout: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments], cwd=checkout, check=True, capture_output=True, text=True
        ).stdout.strip()

    def active_candidate(self) -> CheckpointFixture:
        fixture = self.checkpoint_fixture(local=True)
        returned = self.transition_result(
            fixture, self.project_action(fixture, "return-for-correction:work-a-1"), {"reason": "Prepare candidate."}
        )
        self.assertEqual("committed", returned["status"], returned)
        (fixture.project / "tracked.txt").write_text("declared candidate\n", encoding="utf-8")
        return fixture

    def submit_declared(self, fixture: CheckpointFixture, candidate: str, paths: list[str]) -> JsonObject:
        lease = self.native_attempt_acquire(fixture, "exclusion-worker")
        selected = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        request = self.native_transition_request(
            fixture,
            selected,
            {
                "schema": "pinboard-candidate-declaration/v1",
                "candidate": candidate,
                "excluded_untracked_paths": list[JsonValue](paths),
            },
        )
        return call_advertised_tool(server.TRANSITION_TOOL, request)

    def test_native_declared_candidates_reload_qualify_and_restore_without_touching_local_files(self) -> None:
        for committed in (False, True):
            with self.subTest(committed=committed):
                fixture = self.active_candidate()
                working = root.read_working_tree_candidate(fixture.project)
                if committed:
                    self.git(fixture.project, "add", "tracked.txt")
                    self.git(
                        fixture.project,
                        "-c",
                        "user.name=Test",
                        "-c",
                        "user.email=test@example.invalid",
                        "commit",
                        "-m",
                        "candidate",
                    )
                candidate = self.git(fixture.project, "rev-parse", "HEAD") if committed else working.identity
                local = fixture.project / ".DS_Store"
                local.write_bytes(b"private local bytes")
                result = self.submit_declared(fixture, candidate, [".DS_Store"])
                self.assertEqual("committed", result["status"], result)
                store = SQLiteWorkStore(fixture.work / "state.sqlite3")
                evidence = candidate_evidence.read_candidate_evidence(
                    fixture.work, store, AttemptId("work-a-1"), candidate
                )
                assert not isinstance(evidence, DecisionFailure)
                snapshot = evidence.snapshot
                self.assertEqual("pinboard-candidate-snapshot/v3", snapshot.schema)
                self.assertEqual(snapshot.schema, evidence.receipt.input_schema)
                self.assertEqual((".DS_Store",), candidate_snapshots.excluded_untracked_paths(snapshot))
                self.assertEqual(
                    snapshot,
                    candidate_snapshots.decode_candidate_snapshot(
                        (fixture.work / evidence.reference.selector).read_bytes()
                    ),
                )
                self.assertEqual(
                    query_models.CandidateLineage.COMMIT_CURRENT
                    if committed
                    else query_models.CandidateLineage.WORKING_TREE_CURRENT,
                    candidate_evidence.observe_candidate_lineage(fixture.project, evidence),
                )
                if not committed:
                    unrelated = fixture.project / "unlisted-working-local.txt"
                    unrelated.write_bytes(b"local working-tree files remain supported")
                    self.assertEqual(
                        query_models.CandidateLineage.WORKING_TREE_CURRENT,
                        candidate_evidence.observe_candidate_lineage(fixture.project, evidence),
                    )
                    unrelated.unlink()
                    self.git(fixture.project, "add", "tracked.txt")
                    self.git(
                        fixture.project,
                        "-c",
                        "user.name=Test",
                        "-c",
                        "user.email=test@example.invalid",
                        "commit",
                        "-m",
                        "same reviewed bytes",
                    )
                self.assertEqual(
                    query_models.CandidateLineage.COMMIT_CURRENT,
                    candidate_evidence.observe_candidate_lineage(fixture.project, evidence),
                )
                sibling = fixture.project / ".DS_Store.sibling"
                sibling.write_bytes(b"unlisted")
                self.assertEqual(
                    query_models.CandidateLineage.DRIFTED,
                    candidate_evidence.observe_candidate_lineage(fixture.project, evidence),
                )
                sibling.unlink()
                self.git(fixture.project, "mv", "tracked.txt", "?? misleading-name")
                self.assertEqual(
                    query_models.CandidateLineage.DRIFTED,
                    candidate_evidence.observe_candidate_lineage(fixture.project, evidence),
                )
                self.git(fixture.project, "reset", "--hard", "HEAD")
                target = Path(tempfile.mkdtemp()).resolve()
                self.git(target, "clone", "--quiet", str(fixture.project), ".")
                self.git(target, "reset", "--hard", snapshot.preimage_revision)
                excluded = target / ".DS_Store"
                excluded.write_bytes(b"recovery local bytes")
                restored = candidate_evidence.restore_candidate(
                    target, fixture.work, store, AttemptId("work-a-1"), candidate
                )
                self.assertIsInstance(restored, root.CandidateRestoreSuccess)
                self.assertEqual(b"recovery local bytes", excluded.read_bytes())
                self.assertEqual(b"private local bytes", local.read_bytes())

    def test_declared_submission_validates_actual_files_and_preserves_stale_authority(self) -> None:
        fixture = self.active_candidate()
        candidate = root.read_working_tree_candidate(fixture.project).identity
        lease = self.native_attempt_acquire(fixture, "rejection-worker")
        selected = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        for paths in (["missing.txt"], ["tracked.txt"]):
            before = fixture.store.validated_snapshot()
            result = self.transition_result(
                fixture,
                selected,
                {
                    "schema": "pinboard-candidate-declaration/v1",
                    "candidate": candidate,
                    "excluded_untracked_paths": list[JsonValue](paths),
                },
            )
            self.assertEqual("rejected", result["status"], result)
            self.assertEqual(before, fixture.store.validated_snapshot())
        (fixture.project / "local.txt").write_text("local", encoding="utf-8")
        receipt = self.json_object(selected["action_id"])
        stale = selected | {"subject_revision": "0", "action_id": receipt}
        result = self.transition_result(
            fixture,
            stale,
            {
                "schema": "pinboard-candidate-declaration/v1",
                "candidate": candidate,
                "excluded_untracked_paths": ["local.txt"],
            },
        )
        self.assertEqual("rejected", result["status"], result)

    def test_recovery_collision_rejects_unchanged_and_preserves_excluded_bytes(self) -> None:
        fixture = self.active_candidate()
        new = fixture.project / "local.txt"
        new.write_text("candidate output\n", encoding="utf-8")
        self.git(fixture.project, "add", "local.txt")
        observed = root.read_working_tree_candidate(fixture.project)
        target = Path(tempfile.mkdtemp()).resolve()
        self.git(target, "clone", "--quiet", str(fixture.project), ".")
        excluded = target / "local.txt"
        excluded.write_bytes(b"preserve on collision")
        before = self.git(target, "status", "--porcelain")
        result = root.restore_working_tree_candidate(
            target,
            expected_branch=fixture.brief.branch,
            preimage_revision=observed.preimage_revision,
            candidate=observed.identity,
            diff=observed.diff,
            excluded_untracked_paths=("local.txt",),
        )
        self.assertIsInstance(result, root.CandidateRestoreRejection)
        self.assertEqual(before, self.git(target, "status", "--porcelain"))
        self.assertEqual(b"preserve on collision", excluded.read_bytes())
        self.git(fixture.project, "add", "tracked.txt")
        self.git(
            fixture.project,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "candidate output",
        )
        committed = self.git(fixture.project, "rev-parse", "HEAD")
        self.git(target, "fetch", "--quiet", str(fixture.project), committed)
        rejected = root.restore_commit_candidate(
            target,
            expected_branch=fixture.brief.branch,
            preimage_revision=observed.preimage_revision,
            accepted_base_revision=observed.preimage_revision,
            candidate=committed,
            diff=observed.diff,
            excluded_untracked_paths=("local.txt",),
        )
        self.assertIsInstance(rejected, root.CandidateRestoreRejection)
        self.assertEqual(before, self.git(target, "status", "--porcelain"))
        self.assertEqual(b"preserve on collision", excluded.read_bytes())

    def test_correction_start_preserves_reloaded_current_snapshot_formats(self) -> None:
        for committed, declared in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(committed=committed, declared=declared):
                fixture = self.active_candidate()
                if committed:
                    self.git(fixture.project, "add", "tracked.txt")
                    self.git(
                        fixture.project,
                        "-c",
                        "user.name=Test",
                        "-c",
                        "user.email=test@example.invalid",
                        "commit",
                        "-m",
                        "candidate",
                    )
                candidate = (
                    self.git(fixture.project, "rev-parse", "HEAD")
                    if committed
                    else root.read_working_tree_candidate(fixture.project).identity
                )
                if declared or not committed:
                    (fixture.project / "local.txt").write_bytes(b"local")
                if declared:
                    submitted = self.submit_declared(fixture, candidate, ["local.txt"])
                else:
                    lease = self.native_attempt_acquire(fixture, "candidate-only-worker")
                    selected = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
                    submitted = self.transition_result(fixture, selected, {"candidate": candidate})
                self.assertEqual("committed", submitted["status"], submitted)
                store = SQLiteWorkStore(fixture.work / "state.sqlite3")
                evidence = candidate_evidence.read_candidate_evidence(
                    fixture.work, store, AttemptId("work-a-1"), candidate
                )
                assert not isinstance(evidence, DecisionFailure)
                reference = evidence.reference
                returned = self.transition_result(
                    fixture,
                    self.project_action(fixture, "return-for-correction:work-a-1"),
                    {"reason": "Repair candidate."},
                )
                history_id = returned["history_id"]
                assert isinstance(history_id, int)
                identity = work_brief_models.PortableArtifactIdentity(
                    "candidate",
                    "evidence",
                    reference.key,
                    reference.revision,
                    reference.selector,
                    reference.content_sha256,
                    reference.size_bytes,
                )
                artifacts = ArtifactRepository(resolve_durable_roots(fixture.project))
                result = dispatch_operations._read_correction_snapshot(
                    store,
                    artifacts,
                    fixture.project,
                    fixture.brief,
                    HistoryId(history_id),
                    identity,
                    "Repair candidate.",
                )
                self.assertEqual(evidence.snapshot, result)
                context = call_advertised_tool(
                    server.CORRECTION_CONTEXT_TOOL,
                    {
                        "project_root": str(fixture.project),
                        "work_root": str(fixture.work),
                        "attempt_id": "work-a-1",
                        "correction_history_id": history_id,
                    },
                )
                self.assertEqual("ready", context["status"], context)
                view = self.json_object(context["starting_snapshot"])
                self.assertEqual(
                    "pinboard-candidate-snapshot/v3"
                    if declared
                    else ("pinboard-candidate-snapshot/v1" if committed else "pinboard-candidate-snapshot/v2"),
                    view["schema"],
                )
                self.assertEqual("commit" if committed else "working-tree", view["candidate_kind"])
                self.assertEqual(["local.txt"] if declared else None, view.get("excluded_untracked_paths"))
                (fixture.project / "tracked.txt").write_text("wrong candidate\n", encoding="utf-8")
                rejected = dispatch_operations._read_correction_snapshot(
                    store,
                    artifacts,
                    fixture.project,
                    fixture.brief,
                    HistoryId(history_id),
                    identity,
                    "Repair candidate.",
                )
                self.assertIsInstance(rejected, dispatch_operations.DispatchFailure)
