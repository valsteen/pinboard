import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode, ImmutableFilePublishedError
from pinboard.adapters.files.file_io import (
    atomic_replace,
    create_immutable,
    ensure_child_directory,
    ensure_directory_chain,
    resolve_durable_roots,
)
from pinboard.adapters.sqlite.database import (
    _sync_directory as sync_database_directory,
)
from pinboard.adapters.sqlite.database import (
    initialize_database,
    open_database,
    read_operation,
    read_schema_bytes,
    write_transaction,
)
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.mutations import project_transition_mutation
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.decisions import available_actions as available_actions_outcome
from pinboard.domain.decisions import decide as decision_outcome
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    HostId,
    ItemId,
    LeaseId,
    TaskId,
)
from pinboard.domain.ledger import LedgerSnapshot
from tests.decision_support import project_decision_snapshot
from tests.domain_support import expect_success
from tests.domain_support import replace as replace_dataclass
from tests.support import SQLITE_DIGEST, SQLITE_NOW, complete_sqlite_state, initialize_store, mutation_allocation


def available_actions(
    snapshot: LedgerSnapshot, actor: decision_models.ActorAuthority
) -> tuple[decision_models.Action, ...]:
    return expect_success(available_actions_outcome(snapshot, actor))


def decide(
    snapshot: LedgerSnapshot, command: decision_models.TransitionCommand, now: datetime
) -> decision_models.TransitionDecision:
    result = expect_success(decision_outcome(snapshot, command, now))
    if not isinstance(result, decision_models.TransitionDecision):
        raise AssertionError(f"Expected a non-checkpoint decision, received {result!r}")
    return result


class SQLiteStoreTest(unittest.TestCase):
    def _store(self, *, populated: bool = True) -> tuple[Path, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        if populated:
            state = complete_sqlite_state()
            initialize_store(store, state)
        return roots.database_path, store

    def _assert_state_rejected(self, state_name: str, state: stored_state.StoredWorkState) -> None:
        path, store = self._store(populated=False)
        self.assertTrue(path.exists())
        with self.subTest(state_name=state_name), self.assertRaises(StorageError) as raised:
            initialize_store(store, state)
        self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, raised.exception.code, state_name)
        self.assertEqual(0, store.validated_snapshot().lifecycle.project.revision)

    def _assert_action_not_available[T](self, result: T | DecisionFailure) -> None:
        self.assertIsInstance(result, DecisionFailure)
        assert isinstance(result, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ACTION_NOT_AVAILABLE, result.code)

    def test_active_preparation_requires_ready_item_and_current_definition(self) -> None:
        state = complete_sqlite_state()
        definition = next(value for value in state.lifecycle.definition_revisions if value.item_id == ItemId("work-c"))
        lease = stored_state.StoredPreparationLease(
            ItemId("work-c"),
            1,
            definition.revision,
            definition.digest,
            SQLITE_NOW,
            SQLITE_NOW + timedelta(minutes=5),
            authority_models.PreparationLeaseStatus.ACTIVE,
        )
        authority = replace_dataclass(
            state.authority,
            preparation_counters=(stored_state.PreparationLeaseCounter(ItemId("work-c"), 1),),
            preparation_generations=(
                stored_state.PreparationLeaseGeneration(
                    ItemId("work-c"), 1, LeaseId("preparation-c"), TaskId("preparer-c"), HostId("host-a")
                ),
            ),
            preparation_leases=(lease,),
        )
        non_ready_items = tuple(
            replace_dataclass(value, state=stored_state.StoredWorkItemState.ACTIVE)
            if value.item_id == ItemId("work-c")
            else value
            for value in state.lifecycle.work_items
        )
        self._assert_state_rejected(
            "active preparation for non-ready item",
            replace_dataclass(
                state,
                lifecycle=replace_dataclass(state.lifecycle, work_items=non_ready_items),
                authority=authority,
            ),
        )

        revised_definition = replace_dataclass(definition.definition, title="Revised work C")
        revised_digest = expect_success(work_item_definition_digest(revised_definition))
        revision = replace_dataclass(
            definition,
            revision=2,
            digest=revised_digest,
            definition=revised_definition,
            before_digest=definition.digest,
            after_digest=revised_digest,
        )
        revised_state = replace_dataclass(
            state,
            lifecycle=replace_dataclass(
                state.lifecycle,
                definition_revisions=(*state.lifecycle.definition_revisions, revision),
            ),
            authority=authority,
        )
        self._assert_state_rejected("active preparation for stale definition", revised_state)

        inactive = replace_dataclass(lease, state=authority_models.PreparationLeaseStatus.RELEASED)
        _path, store = self._store(populated=False)
        initialize_store(
            store,
            replace_dataclass(
                revised_state,
                authority=replace_dataclass(authority, preparation_leases=(inactive,)),
            ),
        )
        self.assertEqual(
            authority_models.PreparationLeaseStatus.RELEASED,
            store.validated_snapshot().authority.preparation_leases[0].state,
        )

    def test_schema_identity_initialization_and_reopen_contract(self) -> None:
        path, store = self._store()
        self.assertEqual(complete_sqlite_state(), store.validated_snapshot())
        connection = open_database(path, OpenMode.READ_ONLY)
        try:
            self.assertEqual(1, connection.execute("PRAGMA foreign_keys").fetchone()[0])
            self.assertEqual("delete", connection.execute("PRAGMA journal_mode").fetchone()[0])
            attempts = {row[1]: row for row in connection.execute("PRAGMA table_info(attempts)")}
            self.assertIsNone(attempts["brief_artifact_kind"][4])
        finally:
            connection.close()

    def test_retained_leases_require_their_exact_generation_anchor(self) -> None:
        path, store = self._store()
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DELETE FROM attempt_lease_generations WHERE attempt_id = ?", ("work-a-1",))
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError) as attempt_error:
            store.validated_snapshot()
        self.assertEqual(StorageErrorCode.INVALID_STATE, attempt_error.exception.code)

        state = complete_sqlite_state()
        definition = next(value for value in state.lifecycle.definition_revisions if value.item_id == ItemId("work-c"))
        preparation_authority = replace(
            state.authority,
            preparation_counters=(stored_state.PreparationLeaseCounter(ItemId("work-c"), 1),),
            preparation_generations=(
                stored_state.PreparationLeaseGeneration(
                    ItemId("work-c"), 1, LeaseId("preparation-c"), TaskId("preparer-c"), HostId("host-a")
                ),
            ),
            preparation_leases=(
                stored_state.StoredPreparationLease(
                    ItemId("work-c"),
                    1,
                    definition.revision,
                    definition.digest,
                    SQLITE_NOW,
                    SQLITE_NOW + timedelta(minutes=5),
                    authority_models.PreparationLeaseStatus.ACTIVE,
                ),
            ),
        )
        preparation_path, preparation_store = self._store(populated=False)
        initialize_store(preparation_store, replace(state, authority=preparation_authority))
        connection = sqlite3.connect(preparation_path)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DELETE FROM preparation_lease_generations WHERE item_id = ?", ("work-c",))
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError) as preparation_error:
            preparation_store.validated_snapshot()
        self.assertEqual(StorageErrorCode.INVALID_STATE, preparation_error.exception.code)

    def test_directory_sync_requires_the_platform_directory_flag(self) -> None:
        directory_flag = os.O_DIRECTORY
        del os.O_DIRECTORY
        try:
            parent = Path(tempfile.mkdtemp()).resolve()
            with self.assertRaises(FileIOError) as file_error:
                ensure_child_directory(parent, "child")
            self.assertEqual(FileIOErrorCode.DIRECTORY_SYNC_FAILED, file_error.exception.code)

            with self.assertRaises(StorageError) as database_error:
                sync_database_directory(parent)
            self.assertEqual(StorageErrorCode.IO_ERROR, database_error.exception.code)
        finally:
            os.O_DIRECTORY = directory_flag

        for field, value, expected in (
            ("application", "wrong-application", StorageErrorCode.INVALID_STATE),
            ("schema_version", 0, StorageErrorCode.SCHEMA_UNSUPPORTED),
            ("schema_version", 4, StorageErrorCode.SCHEMA_UNSUPPORTED),
        ):
            tampered, _ = self._store(populated=False)
            connection = sqlite3.connect(tampered)
            try:
                connection.execute("PRAGMA ignore_check_constraints = ON")
                connection.execute(f"UPDATE project_meta SET {field} = ?", (value,))
                connection.commit()
            finally:
                connection.close()
            with self.subTest(field=field, value=value), self.assertRaises(StorageError) as raised:
                open_database(tampered, OpenMode.READ_WRITE)
            self.assertEqual(expected, raised.exception.code)

        malformed, _ = self._store(populated=False)
        connection = sqlite3.connect(malformed)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DROP TABLE transition_history")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError) as malformed_error:
            open_database(malformed, OpenMode.READ_WRITE)
        self.assertEqual(StorageErrorCode.INVALID_STATE, malformed_error.exception.code)

    def test_unsupported_wal_schema_is_rejected_without_mutation(self) -> None:
        unsupported_wal, _ = self._store(populated=False)
        connection = sqlite3.connect(unsupported_wal)
        try:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute("UPDATE project_meta SET schema_version = 4")
            connection.commit()
        finally:
            connection.close()
        before_rejection = tuple(
            (candidate.name, candidate.read_bytes())
            for candidate in sorted(unsupported_wal.parent.iterdir())
            if candidate.is_file()
        )

        with self.assertRaises(StorageError) as newer_error:
            open_database(unsupported_wal, OpenMode.READ_WRITE)

        self.assertEqual(StorageErrorCode.SCHEMA_UNSUPPORTED, newer_error.exception.code)
        self.assertEqual(
            before_rejection,
            tuple(
                (candidate.name, candidate.read_bytes())
                for candidate in sorted(unsupported_wal.parent.iterdir())
                if candidate.is_file()
            ),
        )
        mode_probe = sqlite3.connect(unsupported_wal)
        try:
            self.assertEqual("wal", mode_probe.execute("PRAGMA journal_mode").fetchone()[0])
        finally:
            mode_probe.close()

    def test_storage_error_contract_is_stable_across_real_failures(self) -> None:
        path, _store = self._store(populated=False)

        blocker = sqlite3.connect(path, isolation_level=None)
        contender = open_database(path, OpenMode.READ_WRITE)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            contender.execute("PRAGMA busy_timeout = 1")
            with self.assertRaises(StorageError) as busy_error, write_transaction(contender):
                self.fail("a competing immediate writer must not enter the transaction")
            self.assertEqual(StorageErrorCode.BUSY, busy_error.exception.code)
            self.assertTrue(busy_error.exception.retryable)
        finally:
            blocker.rollback()
            blocker.close()
            contender.close()

        read_only = open_database(path, OpenMode.READ_ONLY)
        try:
            with self.assertRaises(StorageError) as read_only_error, write_transaction(read_only):
                read_only.execute("UPDATE project_meta SET revision = revision + 1")
            self.assertEqual(StorageErrorCode.READ_ONLY, read_only_error.exception.code)
        finally:
            read_only.close()

        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            with self.assertRaises(StorageError) as invariant_error, write_transaction(connection):
                connection.execute("INSERT INTO item_dependencies VALUES ('missing', 'missing', 0)")
            self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, invariant_error.exception.code)
            self.assertEqual(0, connection.execute("SELECT revision FROM project_meta").fetchone()[0])

            storage_error = StorageError(StorageErrorCode.IO_ERROR, "injected storage failure")
            with self.assertRaises(StorageError) as preserved, write_transaction(connection):
                raise storage_error
            self.assertIs(storage_error, preserved.exception)

            application_error = RuntimeError("injected application failure")
            with self.assertRaises(RuntimeError) as propagated, write_transaction(connection):
                connection.execute("UPDATE project_meta SET revision = 9")
                raise application_error
            self.assertIs(application_error, propagated.exception)
            self.assertEqual(0, connection.execute("SELECT revision FROM project_meta").fetchone()[0])
        finally:
            connection.close()

        closed = open_database(path, OpenMode.READ_ONLY)
        closed.close()
        with self.assertRaises(StorageError) as read_error, read_operation(closed):
            closed.execute("SELECT 1")
        self.assertEqual(StorageErrorCode.INVALID_STATE, read_error.exception.code)

    def test_database_open_rejects_missing_and_malformed_identity(self) -> None:
        path, _store = self._store(populated=False)
        roots = resolve_durable_roots(path.parents[2])
        with self.assertRaises(StorageError) as existing_database_error:
            initialize_database(roots, SQLITE_NOW)
        self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, existing_database_error.exception.code)

        missing_database = path.with_name("missing.sqlite3")
        with self.assertRaises(StorageError) as missing_database_error:
            open_database(missing_database, OpenMode.READ_ONLY)
        self.assertEqual(StorageErrorCode.IO_ERROR, missing_database_error.exception.code)

        no_metadata = path.with_name("no-metadata.sqlite3")
        sqlite3.connect(no_metadata).close()
        with self.assertRaises(StorageError) as no_metadata_error:
            open_database(no_metadata, OpenMode.READ_WRITE)
        self.assertEqual(StorageErrorCode.INVALID_STATE, no_metadata_error.exception.code)

        incomplete_schema = path.with_name("incomplete-schema.sqlite3")
        connection = sqlite3.connect(incomplete_schema)
        try:
            connection.execute(
                "CREATE TABLE project_meta (singleton INTEGER, application TEXT, schema_version INTEGER)"
            )
            connection.execute("INSERT INTO project_meta VALUES (1, 'pinboard', 5)")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError) as incomplete_schema_error:
            open_database(incomplete_schema, OpenMode.READ_WRITE)
        self.assertEqual(StorageErrorCode.INVALID_STATE, incomplete_schema_error.exception.code)

        invalid_types = path.with_name("invalid-types.sqlite3")
        invalid_types_connection = sqlite3.connect(invalid_types)
        try:
            invalid_types_connection.execute("CREATE TABLE project_meta (application, schema_version)")
            invalid_types_connection.execute("INSERT INTO project_meta VALUES (7, 'sqlite-v5')")
            invalid_types_connection.commit()
        finally:
            invalid_types_connection.close()
        with self.assertRaises(StorageError) as invalid_types_error:
            open_database(invalid_types, OpenMode.READ_WRITE)
        self.assertEqual(StorageErrorCode.INVALID_STATE, invalid_types_error.exception.code)

    def test_schema_initialization_failures_are_stable(self) -> None:
        path, _store = self._store(populated=False)
        malformed = path.with_name("malformed.sqlite3")
        malformed_connection = sqlite3.connect(malformed)
        try:
            malformed_connection.execute("CREATE TABLE project_meta (application, schema_version)")
            malformed_connection.executemany(
                "INSERT INTO project_meta VALUES (?, ?)",
                (("pinboard", 1), ("pinboard", 1)),
            )
            malformed_connection.commit()
        finally:
            malformed_connection.close()
        with self.assertRaises(StorageError) as metadata_error:
            open_database(malformed, OpenMode.READ_WRITE)
        self.assertEqual(StorageErrorCode.INVALID_STATE, metadata_error.exception.code)

        with (
            patch("pathlib.Path.read_bytes", side_effect=OSError("injected schema read failure")),
            self.assertRaises(StorageError) as schema_error,
        ):
            read_schema_bytes()
        self.assertEqual(StorageErrorCode.IO_ERROR, schema_error.exception.code)

        interrupted_project = Path(tempfile.mkdtemp()).resolve()
        interrupted_roots = resolve_durable_roots(interrupted_project)
        with (
            patch("pinboard.adapters.sqlite.database.read_schema_bytes", return_value=b"\xff"),
            self.assertRaises(StorageError) as initialize_error,
        ):
            initialize_database(interrupted_roots, SQLITE_NOW)
        self.assertEqual(StorageErrorCode.IO_ERROR, initialize_error.exception.code)
        self.assertFalse(interrupted_roots.database_path.exists())

    def test_database_publication_failures_leave_no_accepted_file(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        with (
            patch(
                "pinboard.adapters.sqlite.database._sync_database",
                side_effect=StorageError(StorageErrorCode.IO_ERROR, "injected database synchronization failure"),
            ),
            self.assertRaises(StorageError) as synchronization_error,
        ):
            initialize_database(roots, SQLITE_NOW)
        self.assertEqual(StorageErrorCode.IO_ERROR, synchronization_error.exception.code)
        self.assertFalse(roots.database_path.exists())

    def test_database_cleanup_failures_preserve_typed_errors_and_resume(self) -> None:
        prepublication_project = Path(tempfile.mkdtemp()).resolve()
        prepublication_roots = resolve_durable_roots(prepublication_project)
        with (
            patch("pinboard.adapters.sqlite.database.read_schema_bytes", return_value=b"\xff"),
            patch(
                "pinboard.adapters.sqlite.database.Path.unlink",
                side_effect=OSError("injected pre-publication cleanup failure"),
            ),
            self.assertRaises(StorageError) as prepublication_error,
        ):
            initialize_database(prepublication_roots, SQLITE_NOW)
        self.assertEqual(StorageErrorCode.IO_ERROR, prepublication_error.exception.code)
        self.assertFalse(prepublication_roots.database_path.exists())
        initialize_database(prepublication_roots, SQLITE_NOW)

        synchronization_project = Path(tempfile.mkdtemp()).resolve()
        synchronization_roots = resolve_durable_roots(synchronization_project)
        synchronization_failure = StorageError(
            StorageErrorCode.IO_ERROR,
            "injected final database synchronization failure",
        )
        with (
            patch(
                "pinboard.adapters.sqlite.database._sync_database",
                side_effect=(None, synchronization_failure),
            ),
            patch(
                "pinboard.adapters.sqlite.database.Path.unlink",
                side_effect=OSError("injected published database cleanup failure"),
            ),
            self.assertRaises(StorageError) as synchronization_error,
        ):
            initialize_database(synchronization_roots, SQLITE_NOW)
        self.assertIs(synchronization_failure, synchronization_error.exception)
        initialize_database(synchronization_roots, SQLITE_NOW)

    def test_typed_row_boundary_rejects_malformed_current_state(self) -> None:
        cases = (
            (
                "required timestamp",
                "UPDATE project_meta SET created_at = 'not-a-time'",
                False,
            ),
            (
                "optional timestamp",
                """
                UPDATE attempts SET state = 'review', candidate_revision = 'candidate-a',
                    candidate_recorded_at = 'not-a-time' WHERE attempt_id = 'work-a-1'
                """,
                False,
            ),
            (
                "stored history input JSON",
                "UPDATE transition_history SET input_json = '{' WHERE history_id = 1",
                False,
            ),
            (
                "stored history outcome JSON",
                "UPDATE transition_history SET outcome_json = '{' WHERE history_id = 1",
                False,
            ),
            (
                "attempt generation exceeds its counter",
                "UPDATE attempt_lease_counters SET generation_high_water = 2 WHERE attempt_id = 'work-a-1'",
                False,
            ),
        )
        for name, statement, ignore_constraints in cases:
            path, store = self._store()
            connection = sqlite3.connect(path)
            try:
                if ignore_constraints:
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                connection.execute(statement, (SQLITE_DIGEST, SQLITE_DIGEST) if "?" in statement else ())
                connection.commit()
            finally:
                connection.close()
            with self.subTest(name=name), self.assertRaises(StorageError) as raised:
                store.validated_snapshot()
            self.assertEqual(StorageErrorCode.INVALID_STATE, raised.exception.code)

    def test_durable_root_and_single_file_interruption_contract(self) -> None:
        for failure in range(1, 4):
            project = Path(tempfile.mkdtemp()).resolve()
            roots = resolve_durable_roots(project)
            calls = 0

            def interrupt(_path: Path, *, failure_at: int = failure) -> None:
                nonlocal calls
                calls += 1
                if calls == failure_at:
                    raise FileIOError(
                        FileIOErrorCode.DIRECTORY_SYNC_FAILED,
                        "injected directory synchronization stop",
                    )

            with (
                self.subTest(failure=failure),
                patch("pinboard.adapters.files.file_io._sync_directory", side_effect=interrupt),
                self.assertRaises(StorageError) as raised,
            ):
                initialize_database(roots, SQLITE_NOW)
            self.assertEqual(StorageErrorCode.IO_ERROR, raised.exception.code)
            self.assertFalse(roots.database_path.exists())
            initialize_database(roots, SQLITE_NOW)
            self.assertTrue(roots.artifacts_root.is_dir())

        external_parent = Path(tempfile.mkdtemp()).resolve()
        external = resolve_durable_roots(external_parent, external_parent / "external-work")
        initialize_database(external, SQLITE_NOW)
        self.assertTrue(external.database_path.exists())
        self.assertTrue(external.artifacts_root.is_dir())

        (external_parent / ".codex").mkdir()
        explicit_legacy = resolve_durable_roots(external_parent, external_parent / ".codex" / "work")
        self.assertEqual(external_parent / ".codex" / "work", explicit_legacy.work_root)

        immutable = external.artifacts_root / "evidence.md"
        self.assertTrue(create_immutable(immutable, b"accepted evidence"))
        self.assertEqual(b"accepted evidence", immutable.read_bytes())
        self.assertFalse(create_immutable(immutable, b"accepted evidence"))
        with self.assertRaises(FileIOError):
            create_immutable(immutable, b"replacement")
        replaceable = external.artifacts_root / "view.md"
        atomic_replace(replaceable, b"revision one")
        initial_metadata = replaceable.stat()
        atomic_replace(replaceable, b"revision one")
        unchanged_metadata = replaceable.stat()
        self.assertEqual(b"revision one", replaceable.read_bytes())
        self.assertEqual(
            (initial_metadata.st_ino, initial_metadata.st_mtime_ns),
            (unchanged_metadata.st_ino, unchanged_metadata.st_mtime_ns),
        )
        atomic_replace(replaceable, b"revision two")
        self.assertEqual(b"revision two", replaceable.read_bytes())
        self.assertNotEqual(initial_metadata.st_ino, replaceable.stat().st_ino)

        immutable_staging = external.artifacts_root / "staged-evidence.md"
        with (
            patch("pinboard.adapters.files.file_io.secrets.token_hex", return_value="immutable-token"),
            patch("pinboard.adapters.files.file_io.os.link", wraps=os.link) as linked,
        ):
            create_immutable(immutable_staging, b"staged evidence")
        self.assertEqual(".pinboard-stage-immutable-token", Path(linked.call_args.args[0]).name)

        original_replace = Path.replace

        def replace_staged(source: Path, target: Path) -> Path:
            return original_replace(source, target)

        replace_staging = external.artifacts_root / "staged-view.md"
        with (
            patch("pinboard.adapters.files.file_io.secrets.token_hex", return_value="replace-token"),
            patch(
                "pinboard.adapters.files.file_io.Path.replace",
                autospec=True,
                side_effect=replace_staged,
            ) as replaced_path,
        ):
            atomic_replace(replace_staging, b"staged view")
        self.assertEqual(".pinboard-stage-replace-token", Path(replaced_path.call_args.args[0]).name)

    def test_file_boundary_translates_root_and_publication_failures(self) -> None:
        missing_project = Path(tempfile.mkdtemp()).resolve() / "missing-project"
        with self.assertRaises(FileIOError):
            resolve_durable_roots(missing_project)

        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        with (
            patch("pinboard.adapters.files.file_io.Path.mkdir", side_effect=OSError("injected mkdir failure")),
            self.assertRaises(StorageError) as creation_error,
        ):
            initialize_database(roots, SQLITE_NOW)
        self.assertEqual(StorageErrorCode.IO_ERROR, creation_error.exception.code)
        self.assertFalse(roots.database_path.exists())

        with (
            patch("pinboard.adapters.files.file_io.os.fsync", side_effect=OSError("injected sync failure")),
            self.assertRaises(StorageError) as synchronization_error,
        ):
            initialize_database(roots, SQLITE_NOW)
        self.assertEqual(StorageErrorCode.IO_ERROR, synchronization_error.exception.code)
        self.assertFalse(roots.database_path.exists())

        initialize_database(roots, SQLITE_NOW)
        missing_parent_file = roots.artifacts_root / "missing" / "evidence.md"
        with self.assertRaises(FileIOError):
            create_immutable(missing_parent_file, b"evidence")

        publication = roots.artifacts_root / "publication.md"
        with (
            patch("pinboard.adapters.files.file_io.os.link", side_effect=OSError("injected link failure")),
            self.assertRaises(FileIOError),
        ):
            create_immutable(publication, b"evidence")
        self.assertFalse(publication.exists())

        cleanup_tolerant = roots.artifacts_root / "cleanup-tolerant.md"
        with patch(
            "pinboard.adapters.files.file_io.Path.unlink",
            side_effect=OSError("injected staging cleanup failure"),
        ):
            create_immutable(cleanup_tolerant, b"durable evidence")
        self.assertEqual(b"durable evidence", cleanup_tolerant.read_bytes())

    def test_immutable_publication_reports_post_link_sync_failure_and_reuses_exact_bytes(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        publication = roots.artifacts_root / "post-link.md"
        original_fsync = os.fsync

        def fail_after_link(descriptor: int) -> None:
            if publication.exists():
                raise OSError("injected post-link directory sync failure")
            original_fsync(descriptor)

        with (
            patch("pinboard.adapters.files.file_io.os.fsync", side_effect=fail_after_link),
            self.assertRaises(ImmutableFilePublishedError) as failure,
        ):
            create_immutable(publication, b"published bytes")

        self.assertEqual(publication, failure.exception.path)
        self.assertEqual(FileIOErrorCode.DIRECTORY_SYNC_FAILED, failure.exception.code)
        self.assertEqual(b"published bytes", publication.read_bytes())
        self.assertFalse(create_immutable(publication, b"published bytes"))

    def test_immutable_publication_rejects_non_regular_destinations(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        ensure_directory_chain(roots)
        immutable_directory = roots.artifacts_root / "immutable-directory"
        immutable_directory.mkdir()
        immutable_target = roots.artifacts_root / "immutable-target"
        immutable_target.write_bytes(b"accepted evidence")
        immutable_symlink = roots.artifacts_root / "immutable-symlink"
        immutable_symlink.symlink_to(immutable_target)

        for non_regular in (immutable_directory, immutable_symlink):
            with self.subTest(non_regular=non_regular), self.assertRaises(FileIOError) as collision:
                create_immutable(non_regular, b"accepted evidence")
            self.assertEqual(FileIOErrorCode.FILE_ALREADY_EXISTS, collision.exception.code)

    def test_complete_stored_state_and_relational_contract_matrix(self) -> None:
        path, store = self._store()
        state = complete_sqlite_state()
        self.assertEqual(state, store.validated_snapshot())
        connection = sqlite3.connect(path)
        try:
            table_count = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(17, table_count)
        review_items = list(state.lifecycle.work_items)
        review_items[1] = replace(review_items[1], state=stored_state.StoredWorkItemState.REVIEW)
        review_attempt = replace(state.lifecycle.attempts[0], state=work_models.AttemptState.REVIEW)
        review_without_candidate = replace(
            state,
            lifecycle=replace(state.lifecycle, work_items=tuple(review_items), attempts=(review_attempt,)),
        )
        for name, candidate in (("review candidate", review_without_candidate),):
            self._assert_state_rejected(name, candidate)

        collected_anchors = replace(
            state,
            authority=replace(state.authority, attempt_generations=(), attempt_leases=()),
        )
        _path, collected_store = self._store(populated=False)
        initialize_store(collected_store, collected_anchors)
        self.assertEqual(collected_anchors, collected_store.validated_snapshot())

    def test_schema_rejects_removed_and_incomplete_closed_variants(self) -> None:
        path, _store = self._store()
        connection = sqlite3.connect(path)
        try:
            for statement in (
                "UPDATE attempts SET state = 'closed' WHERE attempt_id = 'work-a-1'",
                "UPDATE artifact_refs SET kind = 'plan' WHERE artifact_ref_id = 2",
                "UPDATE artifact_refs SET kind = 'design' WHERE artifact_ref_id = 2",
                "UPDATE artifact_refs SET kind = 'blocker' WHERE artifact_ref_id = 2",
                "UPDATE proposals SET relation_item_id = NULL WHERE proposal_id = 'zz-proposal-a'",
                """
                UPDATE proposals
                SET disposition = 'accepted', disposition_recorded_at = '2026-08-28T00:00:00+00:00'
                WHERE proposal_id = 'zz-proposal-a'
                """,
            ):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(statement)
                connection.rollback()
        finally:
            connection.close()

    def test_candidate_and_relational_acceptance_matrix(self) -> None:
        state = complete_sqlite_state()
        same_identity_other_kind = replace(
            state.artifact_references[2],
            artifact_ref_id=ArtifactRefId(4),
            key=state.artifact_references[1].key,
            selector="artifacts/evidence/same-key.md",
        )
        accepted_relational_state = replace(
            state,
            artifact_references=(*state.artifact_references, same_identity_other_kind),
        )
        _accepted_path, accepted_store = self._store(populated=False)
        initialize_store(accepted_store, accepted_relational_state)
        self.assertEqual(accepted_relational_state, accepted_store.validated_snapshot())

        for item_state, attempt_state, candidate, accepted in (
            (stored_state.StoredWorkItemState.ACTIVE, work_models.AttemptState.ACTIVE, "candidate-a", False),
            (stored_state.StoredWorkItemState.PAUSED, work_models.AttemptState.PAUSED, "candidate-a", False),
            (stored_state.StoredWorkItemState.BLOCKED, work_models.AttemptState.BLOCKED, "candidate-a", False),
            (stored_state.StoredWorkItemState.REVIEW, work_models.AttemptState.REVIEW, None, False),
            (stored_state.StoredWorkItemState.REVIEW, work_models.AttemptState.REVIEW, "candidate-a", True),
            (stored_state.StoredWorkItemState.DONE, work_models.AttemptState.DONE, None, True),
            (stored_state.StoredWorkItemState.DONE, work_models.AttemptState.DONE, "candidate-a", True),
        ):
            items = list(state.lifecycle.work_items)
            items[1] = replace(
                items[1],
                state=item_state,
                outcome_evidence="accepted completion" if item_state == stored_state.StoredWorkItemState.DONE else None,
                queue_position=None if item_state == stored_state.StoredWorkItemState.DONE else items[1].queue_position,
            )
            if item_state == stored_state.StoredWorkItemState.DONE:
                items = [
                    replace(value, queue_position=value.queue_position - 1)
                    if value.queue_position is not None and value.queue_position > 2
                    else value
                    for value in items
                ]
            attempt = replace(
                state.lifecycle.attempts[0],
                state=attempt_state,
                candidate_revision=candidate,
                candidate_recorded_at=None if candidate is None else SQLITE_NOW,
            )
            candidate_state = replace(
                state,
                lifecycle=replace(state.lifecycle, work_items=tuple(items), attempts=(attempt,)),
            )
            with self.subTest(item_state=item_state, candidate=candidate, accepted=accepted):
                if accepted:
                    _candidate_path, candidate_store = self._store(populated=False)
                    initialize_store(candidate_store, candidate_state)
                    self.assertEqual(candidate_state, candidate_store.validated_snapshot())
                else:
                    self._assert_state_rejected(f"{item_state.value}/{candidate}", candidate_state)

    def test_domain_decision_commit_staleness_and_failure_rollback(self) -> None:
        _path, store = self._store()
        initial = store.validated_snapshot()
        snapshot = project_decision_snapshot(initial, SQLITE_NOW)
        actor = decision_models.ActorAuthority(
            decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0
        )
        action = next(
            value for value in available_actions(snapshot, actor) if value.kind == decision_models.ActionKind.PAUSE
        )
        assert isinstance(action, decision_models.PauseAction)
        decision = decide(
            snapshot,
            decision_models.PauseCommand(action, work_models.ReasonInput("Pause at the checkpoint boundary.")),
            SQLITE_NOW,
        )
        mutation = project_transition_mutation(mutation_allocation(initial), decision)

        with store.write() as transaction:
            self.assertEqual(initial, store.validated_snapshot())
            receipt = expect_success(transaction.commit(mutation))
        committed = store.validated_snapshot()
        self.assertEqual(decision_models.ActionKind.PAUSE.value, receipt.receipt.transition.outcome)
        self.assertEqual(13, committed.lifecycle.project.revision)
        self.assertEqual(stored_state.StoredWorkItemState.PAUSED, committed.lifecycle.work_items[1].state)
        self.assertEqual(work_models.AttemptState.PAUSED, committed.lifecycle.attempts[0].state)
        self.assertEqual(decision_models.ActionKind.PAUSE, committed.transition_receipts[-1].action_kind)

        with store.write() as transaction:
            stale = transaction.commit(mutation)
        self._assert_action_not_available(stale)
        self.assertEqual(committed, store.validated_snapshot())

        stale_subject_decision = replace(
            decision,
            action=replace_dataclass(
                decision.action,
                capability=replace_dataclass(
                    decision.action.capability,
                    expected_revision="",
                    subject_revision="stale-subject",
                ),
            ),
        )
        with store.write() as transaction:
            stale_subject = transaction.commit(replace(mutation, decision=stale_subject_decision))
        self._assert_action_not_available(stale_subject)
        self.assertEqual(committed, store.validated_snapshot())

        failed_path, failed_store = self._store()
        failed_initial = failed_store.validated_snapshot()
        failed_snapshot = project_decision_snapshot(failed_initial, SQLITE_NOW)
        failed_action = next(
            value
            for value in available_actions(
                failed_snapshot,
                decision_models.ActorAuthority(
                    decision_models.Role.PROJECT,
                    decision_models.AuthorizationKind.PROJECT,
                    0,
                ),
            )
            if value.kind == decision_models.ActionKind.PAUSE
        )
        assert isinstance(failed_action, decision_models.PauseAction)
        failed_decision = decide(
            failed_snapshot,
            decision_models.PauseCommand(failed_action, work_models.ReasonInput("This write is interrupted.")),
            SQLITE_NOW,
        )
        failed_mutation = project_transition_mutation(mutation_allocation(failed_initial), failed_decision)
        connection = open_database(failed_path, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                connection.execute(
                    """
                    CREATE TRIGGER reject_test_history BEFORE INSERT ON transition_history
                    BEGIN SELECT RAISE(ABORT, 'injected write failure'); END
                    """
                )
        finally:
            connection.close()
        with self.assertRaises(StorageError), failed_store.write() as transaction:
            transaction.commit(failed_mutation)
        cleanup = sqlite3.connect(failed_path)
        try:
            cleanup.execute("DROP TRIGGER reject_test_history")
            cleanup.commit()
        finally:
            cleanup.close()
        self.assertEqual(failed_initial, failed_store.validated_snapshot())

    def test_runtime_write_scope_propagates_programming_failure_and_closes(self) -> None:
        path, store = self._store()
        before = store.validated_snapshot()
        snapshot = project_decision_snapshot(before, SQLITE_NOW)
        actor = decision_models.ActorAuthority(
            decision_models.Role.PROJECT,
            decision_models.AuthorizationKind.PROJECT,
            0,
        )
        action = next(
            value for value in available_actions(snapshot, actor) if value.kind == decision_models.ActionKind.PAUSE
        )
        assert isinstance(action, decision_models.PauseAction)
        decision = decide(
            snapshot,
            decision_models.PauseCommand(action, work_models.ReasonInput("This write raises a programming failure.")),
            SQLITE_NOW,
        )
        mutation = project_transition_mutation(mutation_allocation(before), decision)
        application_error = RuntimeError("injected runtime store failure")
        runtime_connection = open_database(path, OpenMode.READ_WRITE)

        with (
            patch("pinboard.adapters.sqlite.store.open_database", return_value=runtime_connection),
            patch("pinboard.adapters.sqlite.store._persist", side_effect=application_error),
            self.assertRaises(RuntimeError) as propagated,
            store.write() as transaction,
        ):
            transaction.commit(mutation)

        self.assertIs(application_error, propagated.exception)
        with self.assertRaises(sqlite3.ProgrammingError):
            runtime_connection.execute("SELECT 1")
        self.assertEqual(before, store.validated_snapshot())

    def test_pause_updates_only_affected_relations_and_reloads(self) -> None:
        path, store = self._store()
        before = store.validated_snapshot()
        snapshot = project_decision_snapshot(before, SQLITE_NOW)
        actor = decision_models.ActorAuthority(
            decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0
        )
        action = next(
            value for value in available_actions(snapshot, actor) if value.kind == decision_models.ActionKind.PAUSE
        )
        assert isinstance(action, decision_models.PauseAction)
        decision = decide(
            snapshot,
            decision_models.PauseCommand(
                action, work_models.ReasonInput("Pause without rewriting unrelated relations.")
            ),
            SQLITE_NOW,
        )

        with store.write() as transaction:
            transaction.connection.execute(
                """
                CREATE TRIGGER reject_unrelated_artifact_rewrite BEFORE DELETE ON artifact_refs
                BEGIN SELECT RAISE(ABORT, 'unrelated artifact relation was rewritten'); END
                """
            )
            transaction.commit(project_transition_mutation(mutation_allocation(before), decision))
            transaction.connection.execute("DROP TRIGGER reject_unrelated_artifact_rewrite")

        reopened = SQLiteWorkStore(path).validated_snapshot()
        self.assertEqual(before.artifact_references, reopened.artifact_references)
        self.assertEqual(stored_state.StoredWorkItemState.PAUSED, reopened.lifecycle.work_items[1].state)
        self.assertEqual(work_models.AttemptState.PAUSED, reopened.lifecycle.attempts[0].state)
        self.assertEqual(before.lifecycle.project.revision + 1, reopened.lifecycle.project.revision)
        self.assertEqual(len(before.transition_receipts) + 1, len(reopened.transition_receipts))
        self.assertEqual(decision_models.ActionKind.PAUSE, reopened.transition_receipts[-1].action_kind)

    def test_direct_completion_commits_one_domain_decision_atomically(self) -> None:
        _path, store = self._store()
        before = store.validated_snapshot()
        snapshot = project_decision_snapshot(before, SQLITE_NOW)
        actor = decision_models.ActorAuthority(
            decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0
        )
        action = next(
            value for value in available_actions(snapshot, actor) if value.kind == decision_models.ActionKind.COMPLETE
        )
        assert isinstance(action, decision_models.CompleteAction)
        decision = decide(
            snapshot,
            decision_models.CompleteCommand(action, work_models.EvidenceInput("accepted direct completion")),
            SQLITE_NOW + timedelta(seconds=1),
        )

        with store.write() as transaction:
            receipt = expect_success(
                transaction.commit(project_transition_mutation(mutation_allocation(before), decision))
            )

        completed = store.validated_snapshot()
        item = next(value for value in completed.lifecycle.work_items if value.item_id == ItemId("work-a"))
        attempt = completed.lifecycle.attempts[0]
        self.assertEqual(
            ("complete", "accepted direct completion"),
            (receipt.receipt.transition.outcome, receipt.receipt.transition.evidence),
        )
        self.assertEqual(
            (stored_state.StoredWorkItemState.DONE, "accepted direct completion"), (item.state, item.outcome_evidence)
        )
        self.assertEqual(
            (work_models.AttemptState.DONE, None, None),
            (attempt.state, attempt.candidate_revision, attempt.candidate_recorded_at),
        )
        self.assertEqual(4, completed.authority.attempt_counters[0].generation_high_water)
        self.assertEqual(authority_models.AttemptLeaseStatus.REVOKED, completed.authority.attempt_leases[0].state)

    def test_review_submission_commits_exact_caller_supplied_candidate(self) -> None:
        _path, store = self._store()
        before = store.validated_snapshot()
        snapshot = project_decision_snapshot(before, SQLITE_NOW)
        actor = decision_models.ActorAuthority(
            decision_models.Role.WORKER,
            decision_models.AuthorizationKind.ATTEMPT,
            3,
            LeaseId("attempt-lease-a"),
            (AttemptId("work-a-1"),),
            False,
        )
        action = next(
            value
            for value in available_actions(snapshot, actor)
            if value.kind == decision_models.ActionKind.SUBMIT_REVIEW
        )
        assert isinstance(action, decision_models.SubmitReviewAction)
        candidate = CandidateId("candidate-from-caller")
        decision = decide(
            snapshot,
            decision_models.SubmitReviewCommand(action, work_models.SubmitReviewInput(candidate)),
            SQLITE_NOW + timedelta(seconds=1),
        )

        with store.write() as transaction:
            transaction.commit(project_transition_mutation(mutation_allocation(before), decision))

        committed = store.validated_snapshot()
        attempt = committed.lifecycle.attempts[0]
        self.assertEqual((work_models.AttemptState.REVIEW, candidate), (attempt.state, attempt.candidate_revision))
        self.assertEqual(SQLITE_NOW + timedelta(seconds=1), attempt.candidate_recorded_at)
        self.assertIn(b'"candidate":"candidate-from-caller"', committed.transition_receipts[-1].outcome_payload)

    def test_review_return_clears_candidate_and_fences_mutation_authority(self) -> None:
        state = complete_sqlite_state()
        items = list(state.lifecycle.work_items)
        items[1] = replace(items[1], state=stored_state.StoredWorkItemState.REVIEW)
        attempt = replace(
            state.lifecycle.attempts[0],
            state=work_models.AttemptState.REVIEW,
            candidate_revision="candidate-a",
            candidate_recorded_at=SQLITE_NOW,
        )
        review_state = replace(
            state,
            lifecycle=replace(state.lifecycle, work_items=tuple(items), attempts=(attempt,)),
        )
        _path, store = self._store(populated=False)
        initialize_store(store, review_state)
        snapshot = project_decision_snapshot(store.validated_snapshot(), SQLITE_NOW)
        actor = decision_models.ActorAuthority(
            decision_models.Role.PROJECT,
            decision_models.AuthorizationKind.PROJECT,
            0,
        )
        action = next(
            value
            for value in available_actions(snapshot, actor)
            if value.kind == decision_models.ActionKind.RETURN_FOR_CORRECTION
        )
        assert isinstance(action, decision_models.ReturnForCorrectionAction)
        decision = decide(
            snapshot,
            decision_models.ReturnForCorrectionCommand(action, work_models.ReasonInput("Address the review feedback.")),
            SQLITE_NOW + timedelta(seconds=1),
        )

        with store.write() as transaction:
            transaction.commit(project_transition_mutation(mutation_allocation(review_state), decision))

        returned = store.validated_snapshot()
        returned_attempt = returned.lifecycle.attempts[0]
        self.assertEqual(
            (work_models.AttemptState.ACTIVE, None, None),
            (
                returned_attempt.state,
                returned_attempt.candidate_revision,
                returned_attempt.candidate_recorded_at,
            ),
        )
        self.assertEqual(4, returned.authority.attempt_counters[0].generation_high_water)
        self.assertEqual("revoked", returned.authority.attempt_leases[0].state.value)


if __name__ == "__main__":
    unittest.main()
