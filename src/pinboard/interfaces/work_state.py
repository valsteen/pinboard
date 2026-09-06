"""Compose initialization and read-only integrity validation.

Fresh initialization publishes SQLite state and its empty generated projection;
reopening existing state never repairs views. Validation reads one SQLite
snapshot, verifies each accepted artifact once, and only classifies replaceable
view drift; it never repairs state.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pinboard.adapters.files.artifacts import ArtifactRepository, read_reference
from pinboard.adapters.files.errors import ArtifactError, FileIOError, FileIOErrorCode, ViewProjectionError
from pinboard.adapters.files.file_io import ensure_directory_chain, resolve_durable_roots
from pinboard.adapters.files.root import ensure_default_git_exclude
from pinboard.adapters.files.views import derive_expected_view_bytes, rebuild_state
from pinboard.adapters.sqlite.database import (
    initialize_database,
    open_database,
    read_operation,
    reconcile_database_publication,
    validate_database_integrity,
)
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.models import InitReceipt, OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.domain import work_models
from pinboard.domain.identifiers import ArtifactRefId, AttemptId
from pinboard.interfaces.errors import WorkBriefError
from pinboard.interfaces.work_briefs import build_attempt_brief_views
from pinboard.interfaces.work_state_models import Diagnostic, Severity, ValidationReport


def initialize_work_state(
    shared_repository_root: Path,
    work_root: Path | None = None,
    *,
    now: datetime | None = None,
) -> InitReceipt:
    if work_root is None:
        ensure_default_git_exclude(shared_repository_root)
    roots = resolve_durable_roots(shared_repository_root, work_root)
    database_already_exists = roots.database_path.exists()
    operation_time = now or datetime.now(UTC)
    if database_already_exists:
        connection = open_database(roots.database_path, OpenMode.READ_WRITE)
        connection.close()
        reconcile_database_publication(roots.database_path)
        ensure_directory_chain(roots)
    else:
        initialize_database(roots, operation_time)
    store = SQLiteWorkStore(roots.database_path)
    if not database_already_exists:
        current_state = store.snapshot()
        rendered_attempt_briefs = build_attempt_brief_views(current_state, ArtifactRepository(roots))
        rebuild_result = rebuild_state(current_state, roots.work_root, rendered_attempt_briefs, now=operation_time)
        if rebuild_result.warning is not None:
            raise FileIOError(FileIOErrorCode.VIEW_REFRESH_FAILED, rebuild_result.warning.message)
        revision = current_state.lifecycle.project.revision
    else:
        revision = store.status_facts().project.revision
    return InitReceipt(
        roots.work_root,
        roots.database_path,
        revision,
        database_already_exists,
    )


def _error_diagnostic(code: str, path: Path, message: str, hint: str | None = None) -> Diagnostic:
    return Diagnostic(code=code, severity=Severity.ERROR, path=path, message=message, hint=hint)


def read_state_for_validation(work_root: Path) -> stored_state.StoredWorkState | ValidationReport:
    """Read and structurally validate the authoritative SQLite snapshot once."""

    database = work_root / "state.sqlite3"
    try:
        connection = open_database(database, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                validate_database_integrity(connection)
                return SQLiteWorkStore.read_complete_state(connection)
        finally:
            connection.close()
    except StorageError as error:
        return ValidationReport((_error_diagnostic(error.code.value, database, str(error)),))


@dataclass(frozen=True, slots=True)
class _ValidatedArtifacts:
    contents: Mapping[ArtifactRefId, bytes]

    def read(self, reference: stored_state.ArtifactReference) -> bytes:
        return self.contents[reference.artifact_ref_id]


def validate_loaded_work_state(
    work_root: Path,
    state: stored_state.StoredWorkState,
    *,
    now: datetime,
) -> ValidationReport:
    """Verify accepted bytes, then classify replaceable generated-view drift."""

    diagnostics: list[Diagnostic] = []
    artifact_contents: dict[ArtifactRefId, bytes] = {}
    for reference in state.artifact_references:
        try:
            artifact_contents[reference.artifact_ref_id] = read_reference(work_root, reference)
        except ArtifactError as error:
            diagnostics.append(_error_diagnostic(error.code.value, work_root / reference.selector, str(error)))
    attempt_briefs: Mapping[AttemptId, bytes] | None = None
    live_brief_ids = {
        attempt.brief_artifact_ref_id
        for attempt in state.lifecycle.attempts
        if attempt.state != work_models.AttemptState.DONE
    }
    if live_brief_ids <= artifact_contents.keys():
        try:
            attempt_briefs = build_attempt_brief_views(state, _ValidatedArtifacts(artifact_contents))
        except WorkBriefError as error:
            diagnostics.append(_error_diagnostic(error.code.value, work_root, error.message))
    view_root = work_root / "views"
    try:
        expected_views = derive_expected_view_bytes(state, attempt_briefs, now=now)
    except ViewProjectionError as error:
        diagnostics.append(_error_diagnostic("VIEW_PROJECTION_FAILED", view_root, str(error)))
        expected_views: dict[str, bytes] = {}
    for selector, expected in expected_views.items():
        path = view_root / selector
        try:
            actual_view_bytes = path.read_bytes()
        except OSError:
            actual_view_bytes = None
        if actual_view_bytes != expected:
            diagnostics.append(
                Diagnostic(
                    "VIEW_REFRESH_REQUIRED",
                    Severity.WARNING,
                    path,
                    "Generated view is absent or stale; SQLite remains authoritative.",
                    "Run 'pinboard views rebuild'.",
                )
            )
    for selector in ("queue.md", "history.md"):
        path = view_root / selector
        if path.exists():
            diagnostics.append(
                Diagnostic(
                    "LEGACY_VIEW_RESIDUE",
                    Severity.WARNING,
                    path,
                    "Legacy aggregate generated view remains; SQLite remains authoritative.",
                    "Run 'pinboard views rebuild'.",
                )
            )
    return ValidationReport(tuple(diagnostics))
