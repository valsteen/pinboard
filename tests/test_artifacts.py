import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Barrier
from typing import Never, override
from unittest.mock import patch

from pinboard.adapters.files.artifacts import ArtifactRepository, verify_reference, write_revision
from pinboard.adapters.files.errors import (
    ArtifactError,
    ArtifactErrorCode,
    FileIOError,
    FileIOErrorCode,
)
from pinboard.adapters.files.file_io import create_immutable, resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database, open_database
from pinboard.adapters.sqlite.errors import SQLiteReadOnlyError
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.artifact_publication import publish_accepted_artifact
from pinboard.application.artifacts import ArtifactPublication, ArtifactRef, NewArtifact
from pinboard.domain import work_models
from pinboard.domain.errors import ArtifactAcceptanceAfterPublicationError, DecisionFailure, DecisionFailureCode
from tests.domain_support import expect_success
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store


class _AlwaysReadOnlyArtifactStore(SQLiteWorkStore):
    @override
    def accept_artifact_reference(
        self,
        work_root: Path,
        published: ArtifactRef,
        accepted_at: datetime,
    ) -> Never:
        del work_root, published, accepted_at
        raise SQLiteReadOnlyError(self._path)


class _BarrierArtifactPublisher:
    def __init__(self, repository: ArtifactRepository, barrier: Barrier) -> None:
        self.repository = repository
        self.barrier = barrier

    @property
    def work_root(self) -> Path:
        return self.repository.work_root

    def publish(self, artifact: NewArtifact) -> ArtifactPublication:
        self.barrier.wait(timeout=5)
        return self.repository.publish(artifact)


def _publish_to_readonly_store(
    store: _AlwaysReadOnlyArtifactStore,
    publisher: _BarrierArtifactPublisher,
    artifact: NewArtifact,
) -> Exception:
    try:
        publish_accepted_artifact(store, publisher, artifact, SQLITE_NOW)
    except Exception as error:
        return error
    raise AssertionError("Read-only artifact acceptance unexpectedly succeeded.")


class ArtifactPersistenceTest(unittest.TestCase):
    def test_concurrent_reuse_is_not_attributed_to_the_losing_publisher(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        repository = ArtifactRepository(roots)
        store = _AlwaysReadOnlyArtifactStore(roots.database_path)
        artifact = NewArtifact(work_models.ArtifactKind.EVIDENCE, "concurrent", 1, ".md", b"ready\n")
        before = store.validated_snapshot()
        barrier = Barrier(2)
        publishers = (_BarrierArtifactPublisher(repository, barrier), _BarrierArtifactPublisher(repository, barrier))

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = tuple(
                executor.submit(_publish_to_readonly_store, store, publisher, artifact) for publisher in publishers
            )
            failures = tuple(future.result() for future in futures)

        self.assertEqual(1, sum(isinstance(error, ArtifactAcceptanceAfterPublicationError) for error in failures))
        self.assertEqual(1, sum(isinstance(error, SQLiteReadOnlyError) for error in failures))
        created_failure = next(
            error for error in failures if isinstance(error, ArtifactAcceptanceAfterPublicationError)
        )
        reused_failure = next(error for error in failures if isinstance(error, SQLiteReadOnlyError))
        self.assertEqual("artifacts/evidence/concurrent/1.md", created_failure.selector)
        self.assertIsInstance(created_failure.cause, SQLiteReadOnlyError)
        self.assertEqual(roots.database_path, reused_failure.database_path)
        self.assertEqual(b"ready\n", (roots.work_root / created_failure.selector).read_bytes())
        self.assertEqual(before, store.validated_snapshot())

    def test_accepting_transaction_verifies_bytes_and_fresh_reload_contains_reference(self) -> None:
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
                roots.work_root,
                published,
                SQLITE_NOW,
            )
        )

        reloaded = SQLiteWorkStore(roots.database_path).validated_snapshot()
        self.assertTrue(accepted.ledger_changed)
        self.assertIn(accepted.reference, reloaded.artifact_references)
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

    def test_prelink_failure_does_not_claim_an_existing_artifact(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        artifact = NewArtifact(work_models.ArtifactKind.BRIEF, "attempt-a", 1, ".json", b"{}\n")
        repository = ArtifactRepository(roots)
        publication = repository.publish(artifact)
        artifact_directory = (roots.work_root / publication.reference.selector).parent
        artifact_directory.chmod(0o500)
        try:
            with self.assertRaises(ArtifactError) as failure:
                repository.publish(artifact)
        finally:
            artifact_directory.chmod(0o700)

        self.assertEqual(ArtifactErrorCode.STORAGE_IO_ERROR, failure.exception.code)
        self.assertEqual(b"{}\n", (roots.work_root / publication.reference.selector).read_bytes())

    def test_post_publication_sync_failure_reports_the_created_artifact(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        artifact = NewArtifact(work_models.ArtifactKind.RESULT, "attempt-a", 1, ".md", b"result\n")

        def fail_after_publication(path: Path, content: bytes) -> bool:
            with patch(
                "pinboard.adapters.files.file_io._sync_directory",
                side_effect=FileIOError(FileIOErrorCode.DIRECTORY_SYNC_FAILED, "injected directory sync failure"),
            ):
                return create_immutable(path, content)

        with (
            patch("pinboard.adapters.files.artifacts.create_immutable", side_effect=fail_after_publication),
            self.assertRaises(ArtifactAcceptanceAfterPublicationError) as publication_failure,
        ):
            ArtifactRepository(roots).publish(artifact)

        path = roots.work_root / publication_failure.exception.selector
        self.assertEqual("artifacts/results/attempt-a/1.md", publication_failure.exception.selector)
        self.assertEqual(b"result\n", path.read_bytes())
        self.assertFalse(create_immutable(path, b"result\n"))

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
                roots.work_root,
                published,
                SQLITE_NOW,
            )
        )
        before_retry = store.validated_snapshot()
        retry = expect_success(store.accept_artifact_reference(roots.work_root, published, SQLITE_NOW))
        self.assertEqual(accepted.reference, retry.reference)
        self.assertFalse(retry.ledger_changed)
        self.assertEqual(before_retry, store.validated_snapshot())

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
        before = store.validated_snapshot()
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
            result = store.accept_artifact_reference(roots.work_root, published, SQLITE_NOW)

        self.assertIsInstance(result, DecisionFailure)
        assert isinstance(result, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ACTION_NOT_AVAILABLE, result.code)
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        self.assertEqual(before, store.validated_snapshot())


if __name__ == "__main__":
    unittest.main()
