import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files.artifacts import write_revision
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.artifacts import NewArtifact
from pinboard.application.handover import ProjectHandover
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.history import CheckpointAcceptanceOutcome
from pinboard.domain.identifiers import AttemptId, ItemId
from pinboard.interfaces import work_brief_models
from pinboard.interfaces.cli import main
from pinboard.interfaces.errors import WorkBriefFailure
from pinboard.interfaces.work_briefs import (
    canonical_checkpoint_review_package_bytes,
    canonical_work_brief_bytes,
    canonical_work_brief_review_bytes,
    decode_canonical_checkpoint_review_package,
    decode_canonical_work_brief_review,
)
from tests.support import SQLITE_NOW, JsonObject, JsonValue, complete_sqlite_state, initialize_store
from tests.work_brief_support import ready_review, work_a_brief


@dataclass(frozen=True, slots=True)
class AcceptedPackageFixture:
    project: Path
    work: Path
    store: SQLiteWorkStore
    brief: work_brief_models.WorkBrief
    package_reference: stored_state.ArtifactReference

    @property
    def common(self) -> tuple[str, ...]:
        return "--project-root", str(self.project), "--work-root", str(self.work)


class CheckpointPackageTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def json_object(self, value: JsonValue) -> JsonObject:
        if not isinstance(value, dict):
            self.fail("JSON value must be an object")
        return value

    def run_json_cli(self, *arguments: str) -> JsonObject:
        result, stdout, stderr = self.run_cli(*arguments, "--json")
        self.assertEqual(0, result, stderr)
        return self.json_object(json.loads(stdout))

    def project_action(self, common: tuple[str, ...], action_id: str) -> JsonObject:
        actions = self.run_json_cli(*common, "actions", "--role", "project", "--action-id", action_id)
        values = actions["actions"]
        if not isinstance(values, list) or len(values) != 1:
            self.fail("Expected one exact project action")
        return self.json_object(values[0])

    def accept_checkpoint(
        self,
        common: tuple[str, ...],
        action: JsonObject,
        payload: Path,
    ) -> None:
        arguments = [
            *common,
            "transition",
            "--action-id",
            str(action["action_id"]),
            "--expected-revision",
            str(action["expected_revision"]),
            "--authorization",
            str(action["authorization"]),
            "--task-id",
            "package-test-owner",
            "--host-id",
            "local",
            "--payload",
            str(payload),
        ]
        subject_revision = action.get("subject_revision")
        if subject_revision:
            arguments.extend(("--subject-revision", str(subject_revision)))
        result, _stdout, stderr = self.run_cli(*arguments)
        self.assertEqual(0, result, stderr)

    def local_brief(self, project: Path) -> work_brief_models.WorkBrief:
        candidate = work_a_brief(project)
        checkpoint = candidate.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        return replace_struct(
            candidate,
            checkpoint=work_brief_models.LocalCheckpoint(
                "local-package",
                "Preserve local evidence",
                work_brief_models.NoArchitectureImpact("The package owner remains unchanged."),
                "The local package remains reusable.",
                checkpoint.acceptance_criteria,
                (
                    work_brief_models.VerificationRecord(
                        work_brief_models.AcceptedScopeAuthorization("work-a", 1),
                        "Run the installed package path.",
                    ),
                ),
                (),
            ),
        )

    def accepted_package_fixture(self, *, local: bool = False) -> AcceptedPackageFixture:
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=tuple(
                    replace(value, state=stored_state.StoredWorkItemState.REVIEW)
                    if value.item_id == ItemId("work-a")
                    else value
                    for value in state.lifecycle.work_items
                ),
                attempts=tuple(
                    replace(
                        value,
                        state=work_models.AttemptState.REVIEW,
                        candidate_revision="candidate-a",
                        candidate_recorded_at=now,
                    )
                    if value.attempt_id == AttemptId("work-a-1")
                    else value
                    for value in state.lifecycle.attempts
                ),
            ),
            artifact_references=(state.artifact_references[0],),
            transition_receipts=(
                replace(
                    state.transition_receipts[0],
                    artifact_ref_id=None,
                    outcome_schema="transition-receipt/v1",
                ),
            ),
        )
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        brief = self.local_brief(project) if local else work_a_brief(project)
        published_brief = write_revision(
            roots,
            NewArtifact(
                work_models.ArtifactKind.BRIEF,
                brief.attempt_id,
                brief.artifact_revision,
                ".json",
                canonical_work_brief_bytes(brief),
            ),
        )
        brief_reference = replace(
            state.artifact_references[0],
            key=published_brief.key,
            revision=published_brief.revision,
            selector=published_brief.selector,
            content_sha256=published_brief.content_sha256,
            size_bytes=published_brief.size_bytes,
        )
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, replace(state, artifact_references=(brief_reference,)))
        if not local:
            checkpoint = brief.checkpoint
            assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
            checkpoint_sha256 = hashlib.sha256(msgspec.json.encode(checkpoint, order="sorted")).hexdigest()
            published_review = write_revision(
                roots,
                NewArtifact(
                    work_models.ArtifactKind.EVIDENCE,
                    f"work-a-1-brief-review-{checkpoint_sha256}",
                    1,
                    ".json",
                    ready_review(brief),
                ),
            )
            accepted_review = store.accept_artifact_reference(roots.work_root, published_review, now)
            if isinstance(accepted_review, DecisionFailure):
                self.fail(str(accepted_review))
        attempt_root = roots.work_root / "attempts" / "work-a-1"
        attempt_root.mkdir(parents=True)
        (attempt_root / "result.md").write_text("candidate result\n", encoding="utf-8")
        (attempt_root / "review.md").write_text("implementation review\n", encoding="utf-8")
        checkpoint_id = brief.checkpoint.checkpoint_id
        payload = project / "accept-checkpoint.json"
        payload.write_text(
            json.dumps(
                {
                    "checkpoint": checkpoint_id,
                    "candidate": "candidate-a",
                    "evidence": "Accepted evidence.",
                }
            ),
            encoding="utf-8",
        )
        common = "--project-root", str(project), "--work-root", str(roots.work_root)
        self.accept_checkpoint(common, self.project_action(common, "accept-checkpoint:work-a-1"), payload)
        reloaded = store.validated_snapshot()
        package_reference = next(
            value for value in reloaded.artifact_references if value.key == f"work-a-1-{checkpoint_id}-review-package"
        )
        return AcceptedPackageFixture(project, roots.work_root, store, brief, package_reference)

    def package(self, fixture: AcceptedPackageFixture) -> work_brief_models.CheckpointReviewPackage:
        package = decode_canonical_checkpoint_review_package(
            (fixture.work / fixture.package_reference.selector).read_bytes()
        )
        if isinstance(package, WorkBriefFailure):
            self.fail(str(package))
        return package

    def replace_artifact_bytes(
        self,
        fixture: AcceptedPackageFixture,
        reference: stored_state.ArtifactReference,
        content: bytes,
    ) -> None:
        (fixture.work / reference.selector).write_bytes(content)
        connection = sqlite3.connect(fixture.work / "state.sqlite3")
        try:
            connection.execute(
                "UPDATE artifact_refs SET content_sha256 = ?, size_bytes = ? WHERE artifact_ref_id = ?",
                (hashlib.sha256(content).hexdigest(), len(content), int(reference.artifact_ref_id)),
            )
            connection.commit()
        finally:
            connection.close()

    def replace_package(
        self,
        fixture: AcceptedPackageFixture,
        package: work_brief_models.CheckpointReviewPackage,
    ) -> None:
        self.replace_artifact_bytes(
            fixture,
            fixture.package_reference,
            canonical_checkpoint_review_package_bytes(package),
        )

    def package_failure_codes(self, fixture: AcceptedPackageFixture) -> tuple[str, str]:
        validation_result, validation_stdout, validation_stderr = self.run_cli(*fixture.common, "validate", "--json")
        self.assertEqual(10, validation_result, validation_stderr)
        validation = self.json_object(json.loads(validation_stdout))
        diagnostics = validation["diagnostics"]
        if not isinstance(diagnostics, list):
            self.fail("Validation diagnostics must be a list")
        package_diagnostic = next(
            self.json_object(value)
            for value in diagnostics
            if isinstance(value, dict) and str(value.get("code", "")).startswith("CHECKPOINT_REVIEW_PACKAGE_")
        )
        handover_result, handover_stdout, handover_stderr = self.run_cli(*fixture.common, "handover", "--json")
        self.assertEqual(16, handover_result, handover_stderr)
        handover = self.json_object(json.loads(handover_stdout))
        self.assertNotIn("checkpoint_packages", handover)
        return str(package_diagnostic["code"]), str(handover["code"])

    def test_installed_commands_validate_and_export_one_linked_package_without_writes(self) -> None:
        fixture = self.accepted_package_fixture()
        database_before = (fixture.work / "state.sqlite3").read_bytes()
        files_before = {
            path.relative_to(fixture.work).as_posix(): path.read_bytes()
            for path in fixture.work.rglob("*")
            if path.is_file() and path.name != "state.sqlite3"
        }
        original_snapshot = SQLiteWorkStore.validated_snapshot
        snapshot_reads = 0

        def counted_snapshot(store: SQLiteWorkStore) -> stored_state.StoredWorkState:
            nonlocal snapshot_reads
            snapshot_reads += 1
            return original_snapshot(store)

        with patch.object(SQLiteWorkStore, "validated_snapshot", counted_snapshot):
            validation = self.run_json_cli(*fixture.common, "validate")
        self.assertTrue(validation["valid"])
        self.assertEqual(1, snapshot_reads)

        result, stdout, stderr = self.run_cli(*fixture.common, "handover", "--json")
        self.assertEqual(0, result, stderr)
        handover = msgspec.json.decode(stdout, type=ProjectHandover, strict=True)
        self.assertEqual("pinboard-project-handover/v3", handover.schema)
        self.assertEqual(1, len(handover.checkpoint_packages))
        exported = handover.checkpoint_packages[0]
        receipt = next(value for value in handover.transitions if value.outcome_schema == "checkpoint-acceptance/v2")
        self.assertEqual(
            {"transition-receipt/v1", "checkpoint-acceptance/v2"},
            {value.outcome_schema for value in handover.transitions},
        )
        self.assertEqual(receipt.history_id, exported.history_id)
        self.assertEqual(int(fixture.package_reference.artifact_ref_id), exported.package_artifact_ref_id)
        self.assertEqual("candidate-a", exported.candidate)
        self.assertEqual(
            {value.artifact_ref_id for value in handover.artifact_references},
            {value.artifact_ref_id for value in handover.artifact_contents},
        )
        self.assertIn(
            exported.package_artifact_ref_id, {value.artifact_ref_id for value in handover.artifact_references}
        )
        self.assertEqual(database_before, (fixture.work / "state.sqlite3").read_bytes())
        self.assertEqual(
            files_before,
            {
                path.relative_to(fixture.work).as_posix(): path.read_bytes()
                for path in fixture.work.rglob("*")
                if path.is_file() and path.name != "state.sqlite3"
            },
        )

    def test_validation_uses_only_its_snapshot_across_a_disjoint_commit(self) -> None:
        fixture = self.accepted_package_fixture()
        original_snapshot = SQLiteWorkStore.validated_snapshot
        snapshot_reads = 0

        def snapshot_then_commit(store: SQLiteWorkStore) -> stored_state.StoredWorkState:
            nonlocal snapshot_reads
            snapshot_reads += 1
            state = original_snapshot(store)
            connection = sqlite3.connect(fixture.work / "state.sqlite3")
            try:
                connection.execute("UPDATE project_meta SET revision = revision + 1")
                connection.commit()
            finally:
                connection.close()
            return state

        with patch.object(SQLiteWorkStore, "validated_snapshot", snapshot_then_commit):
            validation = self.run_json_cli(*fixture.common, "validate")
        self.assertTrue(validation["valid"])
        self.assertEqual(1, snapshot_reads)

    def corrupt_package_identity(
        self,
        case: str,
        fixture: AcceptedPackageFixture,
        package: work_brief_models.CheckpointReviewPackage,
    ) -> bool:
        if case == "malformed":
            self.replace_artifact_bytes(fixture, fixture.package_reference, b"{}\n")
        elif case == "noncanonical":
            self.replace_artifact_bytes(
                fixture,
                fixture.package_reference,
                b" " + canonical_checkpoint_review_package_bytes(package),
            )
        elif case == "receipt-outcome":
            receipt = next(
                value
                for value in fixture.store.validated_snapshot().transition_receipts
                if value.outcome_schema == "checkpoint-acceptance/v2"
            )
            outcome = msgspec.json.decode(bytes(receipt.outcome_payload), type=CheckpointAcceptanceOutcome)
            connection = sqlite3.connect(fixture.work / "state.sqlite3")
            try:
                connection.execute(
                    "UPDATE transition_history SET outcome_json = ? WHERE history_id = ?",
                    (
                        msgspec.json.encode(replace_struct(outcome, candidate="different"), order="sorted").decode(),
                        int(receipt.history_id),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
        elif case == "attempt-item":
            self.replace_package(fixture, replace_struct(package, item_id="work-c"))
        elif case == "accepted-scope":
            self.replace_package(
                fixture,
                replace_struct(
                    package,
                    accepted_scope=work_brief_models.AcceptedScope(package.accepted_scope.revision, "f" * 64),
                ),
            )
        elif case == "checkpoint-digest":
            basis = package.review_basis
            assert isinstance(basis, work_brief_models.CrossBoundaryReviewBasis)
            digest = "f" * 64
            self.replace_package(
                fixture,
                replace_struct(
                    package,
                    checkpoint=replace_struct(package.checkpoint, sha256=digest),
                    review_basis=replace_struct(basis, checkpoint_sha256=digest),
                ),
            )
        elif case == "accepted-brief":
            self.replace_package(
                fixture,
                replace_struct(
                    package,
                    accepted_brief=replace_struct(package.accepted_brief, key="missing-accepted-brief"),
                ),
            )
        elif case == "result":
            self.replace_package(
                fixture, replace_struct(package, result=replace_struct(package.result, key="missing-result"))
            )
        elif case == "implementation-review":
            self.replace_package(
                fixture,
                replace_struct(
                    package,
                    implementation_review=replace_struct(
                        package.implementation_review,
                        key="missing-implementation-review",
                    ),
                ),
            )
        else:
            return False
        return True

    def corrupt_package_review(
        self,
        case: str,
        fixture: AcceptedPackageFixture,
        package: work_brief_models.CheckpointReviewPackage,
    ) -> None:
        basis = package.review_basis
        assert isinstance(basis, work_brief_models.CrossBoundaryReviewBasis)
        if case == "brief-review":
            replacement = replace_struct(basis, brief_review=replace_struct(basis.brief_review, key="missing-review"))
            self.replace_package(fixture, replace_struct(package, review_basis=replacement))
            return
        if case == "authority-set":
            replacement = replace_struct(basis, reviewed_authority_set_sha256="f" * 64)
            self.replace_package(fixture, replace_struct(package, review_basis=replacement))
            return
        references = fixture.store.validated_snapshot().artifact_references
        ready_reference = next(
            value
            for value in references
            if (value.kind.value, value.key, value.revision)
            == (basis.brief_review.kind, basis.brief_review.key, basis.brief_review.revision)
        )
        review = decode_canonical_work_brief_review((fixture.work / ready_reference.selector).read_bytes())
        if isinstance(review, WorkBriefFailure):
            self.fail(str(review))
        review_bytes = canonical_work_brief_review_bytes(
            replace_struct(review, reviewer_task_id=fixture.brief.owner_task_id)
        )
        self.replace_artifact_bytes(fixture, ready_reference, review_bytes)
        updated_review_identity = replace_struct(
            basis.brief_review,
            content_sha256=hashlib.sha256(review_bytes).hexdigest(),
            size_bytes=len(review_bytes),
        )
        self.replace_package(
            fixture,
            replace_struct(
                package,
                review_basis=replace_struct(basis, brief_review=updated_review_identity),
            ),
        )

    def test_validation_and_handover_share_the_package_failure_family(self) -> None:
        cases = (
            "malformed",
            "noncanonical",
            "receipt-outcome",
            "attempt-item",
            "accepted-scope",
            "checkpoint-digest",
            "accepted-brief",
            "result",
            "implementation-review",
            "brief-review",
            "authority-set",
            "reviewer-relationship",
        )
        for case in cases:
            with self.subTest(case=case):
                fixture = self.accepted_package_fixture()
                package = self.package(fixture)
                if not self.corrupt_package_identity(case, fixture, package):
                    self.corrupt_package_review(case, fixture, package)
                validation_code, handover_code = self.package_failure_codes(fixture)
                self.assertEqual(validation_code, handover_code)

    def test_historical_package_survives_current_brief_link_drift(self) -> None:
        fixture = self.accepted_package_fixture()
        checkpoint = fixture.brief.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        replacement_brief = replace_struct(
            fixture.brief,
            artifact_revision=2,
            branch="codex/later-work-a",
            base_revision="later-base-revision",
            checkpoint=replace_struct(checkpoint, checkpoint_id="later-checkpoint"),
        )
        published = write_revision(
            resolve_durable_roots(fixture.project),
            NewArtifact(
                work_models.ArtifactKind.BRIEF,
                replacement_brief.attempt_id,
                replacement_brief.artifact_revision,
                ".json",
                canonical_work_brief_bytes(replacement_brief),
            ),
        )
        accepted = fixture.store.accept_artifact_reference(fixture.work, published, SQLITE_NOW)
        if isinstance(accepted, DecisionFailure):
            self.fail(str(accepted))
        connection = sqlite3.connect(fixture.work / "state.sqlite3")
        try:
            connection.execute(
                """
                UPDATE attempts
                SET brief_artifact_ref_id = ?, branch = ?, base_revision = ?
                WHERE attempt_id = 'work-a-1'
                """,
                (
                    int(accepted.reference.artifact_ref_id),
                    replacement_brief.branch,
                    replacement_brief.base_revision,
                ),
            )
            connection.commit()
        finally:
            connection.close()

        validation = self.run_json_cli(*fixture.common, "validate")
        self.assertTrue(validation["valid"])
        result, stdout, stderr = self.run_cli(*fixture.common, "handover", "--json")
        self.assertEqual(0, result, stderr)
        handover = msgspec.json.decode(stdout, type=ProjectHandover, strict=True)
        self.assertEqual(fixture.brief.checkpoint.checkpoint_id, handover.checkpoint_packages[0].checkpoint.id)

    def test_local_package_validates_without_a_ready_review(self) -> None:
        fixture = self.accepted_package_fixture(local=True)

        validation = self.run_json_cli(*fixture.common, "validate")
        self.assertTrue(validation["valid"])
        result, stdout, stderr = self.run_cli(*fixture.common, "handover", "--json")
        self.assertEqual(0, result, stderr)
        handover = msgspec.json.decode(stdout, type=ProjectHandover, strict=True)
        self.assertEqual("local-package", handover.checkpoint_packages[0].checkpoint.id)


if __name__ == "__main__":
    unittest.main()
