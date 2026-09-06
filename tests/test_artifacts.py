import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.files.artifacts import verify_reference, write_revision
from pinboard.adapters.files.errors import (
    ArtifactError,
    ArtifactErrorCode,
    FileIOError,
    FileIOErrorCode,
)
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database, open_database
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.artifacts import NewArtifact
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from tests.domain_support import expect_success
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store


class ArtifactPersistenceTest(unittest.TestCase):
    def test_accepting_publisher_verified_reference_survives_fresh_reload(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, complete_sqlite_state())
        published = write_revision(
            roots,
            NewArtifact(work_models.ArtifactKind.EVIDENCE, "review-a", 1, ".md", b"ready\n"),
        )

        accepted = expect_success(
            store.accept_artifact_reference(
                published,
                SQLITE_NOW,
            )
        )

        reloaded = SQLiteWorkStore(roots.database_path).snapshot()
        self.assertIn(accepted, reloaded.artifact_references)
        self.assertEqual(13, reloaded.lifecycle.project.revision)

    def test_revision_is_published_immutably_and_identical_retry_is_reused(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        artifact = NewArtifact(work_models.ArtifactKind.BRIEF, "attempt-a", 1, ".json", b"{}\n")

        reference = write_revision(roots, artifact)
        path = roots.work_root / reference.selector
        initial_metadata = path.stat()

        self.assertEqual("artifacts/briefs/attempt-a/1.json", reference.selector)
        self.assertEqual(reference, write_revision(roots, artifact))
        reused_metadata = path.stat()
        self.assertEqual(
            (initial_metadata.st_ino, initial_metadata.st_mtime_ns),
            (reused_metadata.st_ino, reused_metadata.st_mtime_ns),
        )
        verify_reference(roots.work_root, reference)
        with self.assertRaises(ArtifactError) as collision:
            write_revision(roots, NewArtifact(work_models.ArtifactKind.BRIEF, "attempt-a", 1, ".json", b"different\n"))
        self.assertEqual(ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION, collision.exception.code)
        self.assertEqual(b"{}\n", path.read_bytes())

    def test_reference_verification_rejects_escape_size_and_digest(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        reference = write_revision(
            roots,
            NewArtifact(work_models.ArtifactKind.EVIDENCE, "review-a", 1, ".json", b"{}\n"),
        )

        for changed in (
            replace(reference, selector="../outside"),
            replace(reference, size_bytes=reference.size_bytes + 1),
            replace(reference, content_sha256="0" * 64),
        ):
            with self.subTest(reference=changed), self.assertRaises(ArtifactError):
                verify_reference(roots.work_root, changed)

    def test_artifact_identity_and_publication_failure_matrix_is_stable(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        invalid = (
            NewArtifact(work_models.ArtifactKind.BRIEF, "../escape", 1, ".json", b"x"),
            NewArtifact(work_models.ArtifactKind.BRIEF, "brief", 0, ".json", b"x"),
            NewArtifact(work_models.ArtifactKind.BRIEF, "brief", 1, "json", b"x"),
        )
        for artifact in invalid:
            with self.subTest(artifact=artifact), self.assertRaises(ArtifactError) as raised:
                write_revision(roots, artifact)
            self.assertEqual(ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION, raised.exception.code)

        published = write_revision(roots, NewArtifact(work_models.ArtifactKind.BRIEF, "brief", 1, ".json", b"x"))
        for selector in (
            "artifacts/briefs/other/1.json",
            "artifacts/briefs/brief/not-a-revision.json",
            "artifacts/briefs/brief/1.json/extra",
        ):
            with self.subTest(selector=selector), self.assertRaises(ArtifactError):
                verify_reference(roots.work_root, replace(published, selector=selector))

        failing_project = Path(tempfile.mkdtemp()).resolve()
        failing_roots = resolve_durable_roots(failing_project)
        with (
            patch(
                "pinboard.adapters.files.artifacts.ensure_directory_chain",
                side_effect=FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, "unavailable"),
            ),
            self.assertRaises(ArtifactError) as io_error,
        ):
            write_revision(failing_roots, NewArtifact(work_models.ArtifactKind.RESULT, "result", 1, ".md", b"x"))
        self.assertEqual(ArtifactErrorCode.STORAGE_IO_ERROR, io_error.exception.code)

    def test_artifact_acceptance_reuses_identical_reference_without_mutation(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, complete_sqlite_state())
        published = write_revision(
            roots,
            NewArtifact(work_models.ArtifactKind.EVIDENCE, "review-reuse", 1, ".md", b"ready\n"),
        )
        accepted = expect_success(
            store.accept_artifact_reference(
                published,
                SQLITE_NOW,
            )
        )
        before_retry = store.snapshot()
        self.assertEqual(
            accepted,
            expect_success(store.accept_artifact_reference(published, SQLITE_NOW)),
        )
        self.assertEqual(before_retry, store.snapshot())

    def test_expected_stale_artifact_acceptance_returns_failure_and_rolls_back(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, complete_sqlite_state())
        published = write_revision(
            roots,
            NewArtifact(work_models.ArtifactKind.EVIDENCE, "stale-artifact", 1, ".md", b"ready\n"),
        )
        before = store.snapshot()
        connection = open_database(roots.database_path, OpenMode.READ_WRITE)
        connection.execute(
            """
            CREATE TEMP TRIGGER arrange_real_artifact_staleness
            BEFORE INSERT ON artifact_refs
            BEGIN
                UPDATE project_meta SET revision = revision + 1 WHERE singleton = 1;
            END
            """
        )
        with patch("pinboard.adapters.sqlite.store.open_database", return_value=connection):
            result = store.accept_artifact_reference(published, SQLITE_NOW)

        self.assertIsInstance(result, DecisionFailure)
        assert isinstance(result, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ACTION_NOT_AVAILABLE, result.code)
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        self.assertEqual(before, store.snapshot())


if __name__ == "__main__":
    unittest.main()
