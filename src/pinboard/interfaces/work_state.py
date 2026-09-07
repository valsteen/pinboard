"""Compose initialization and read-only integrity validation.

Initialization may publish SQLite state and rebuild generated views. Validation
reads one SQLite snapshot, verifies accepted artifacts, and only classifies
replaceable view drift; it never repairs state.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from pinboard.adapters.files.artifacts import ArtifactRepository, verify_reference
from pinboard.adapters.files.errors import ArtifactError, FileIOError, FileIOErrorCode
from pinboard.adapters.files.file_io import ensure_directory_chain, resolve_durable_roots
from pinboard.adapters.files.root import ensure_default_git_exclude
from pinboard.adapters.files.views import derive_expected_view_bytes, rebuild_state
from pinboard.adapters.sqlite.database import initialize_database, open_database, reconcile_database_publication
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.models import InitReceipt, OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.domain.identifiers import AttemptId
from pinboard.interfaces.errors import WorkBriefFailure, WorkBriefResult
from pinboard.interfaces.work_briefs import build_attempt_brief_views
from pinboard.interfaces.work_state_models import Diagnostic, Severity, ValidationReport


def initialize_work_state(
    shared_repository_root: Path,
    work_root: Path | None = None,
    *,
    now: datetime | None = None,
) -> WorkBriefResult[InitReceipt]:
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
    current_state = store.snapshot()
    rendered_attempt_briefs = build_attempt_brief_views(current_state, ArtifactRepository(roots))
    if isinstance(rendered_attempt_briefs, WorkBriefFailure):
        return rendered_attempt_briefs
    rebuild_result = rebuild_state(current_state, roots.work_root, rendered_attempt_briefs, now=operation_time)
    if rebuild_result.warning is not None:
        raise FileIOError(FileIOErrorCode.VIEW_REFRESH_FAILED, rebuild_result.warning.message)
    return InitReceipt(
        roots.work_root,
        roots.database_path,
        current_state.lifecycle.project.revision,
        database_already_exists,
    )


def _error_diagnostic(code: str, path: Path, message: str, hint: str | None = None) -> Diagnostic:
    return Diagnostic(code=code, severity=Severity.ERROR, path=path, message=message, hint=hint)


def read_state_for_validation(work_root: Path) -> stored_state.StoredWorkState | ValidationReport:
    """Read and structurally validate the authoritative SQLite snapshot once."""

    database = work_root / "state.sqlite3"
    try:
        return SQLiteWorkStore(database).validated_snapshot()
    except StorageError as error:
        return ValidationReport((_error_diagnostic(error.code.value, database, str(error)),))


def validate_loaded_work_state(
    work_root: Path,
    state: stored_state.StoredWorkState,
    attempt_briefs: Mapping[AttemptId, bytes] | None = None,
    *,
    now: datetime,
) -> ValidationReport:
    """Verify accepted bytes, then classify replaceable generated-view drift."""

    diagnostics: list[Diagnostic] = []
    for reference in state.artifact_references:
        try:
            verify_reference(work_root, reference)
        except ArtifactError as error:
            diagnostics.append(_error_diagnostic(error.code.value, work_root / reference.selector, str(error)))
    view_root = work_root / "views"
    for selector, expected in derive_expected_view_bytes(state, attempt_briefs, now=now).items():
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
    return ValidationReport(tuple(diagnostics))
