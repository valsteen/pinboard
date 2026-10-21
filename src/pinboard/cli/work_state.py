"""Compose initialization and read-only integrity validation.

Initialization may publish SQLite state and rebuild generated views from exact
projection facts. Validation reads one complete SQLite snapshot, verifies accepted
artifacts, and only classifies replaceable view drift; it never repairs state.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import msgspec

from pinboard.adapters.files.artifacts import ArtifactRepository, read_reference
from pinboard.adapters.files.errors import ArtifactError, FileIOError, FileIOErrorCode
from pinboard.adapters.files.file_io import DurableRoots, ensure_directory_chain
from pinboard.adapters.files.root import ensure_git_exclude
from pinboard.adapters.files.views import derive_expected_view_bytes, rebuild_facts
from pinboard.adapters.sqlite.database import initialize_database, open_database, reconcile_database_publication
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.models import InitReceipt, OpenMode
from pinboard.application import (
    action_models,
    cancellation,
    candidate_snapshots,
    history_archives,
    lifecycle_packages,
    ports,
    queries,
    query_models,
    stored_state,
    work_brief_models,
)
from pinboard.application.work_briefs import (
    build_selected_attempt_brief_views,
)
from pinboard.cli.errors import (
    InitializationAfterCommittedEffects,
)
from pinboard.cli.work_state_models import Diagnostic, Severity, ValidationDiagnosticCode, ValidationReport
from pinboard.cli.work_views import ComposedBoardReader, archive_attempt_views
from pinboard.domain import decision_models
from pinboard.domain.identifiers import ArtifactRefId, AttemptId


def initialize_work_state(
    shared_repository_root: Path,
    roots: DurableRoots,
    *,
    default_work_root: bool,
    store: ports.GeneratedViewSetReader,
    now: datetime | None = None,
) -> work_brief_models.WorkBriefResult[InitReceipt | InitializationAfterCommittedEffects]:
    git_exclude_path = ensure_git_exclude(shared_repository_root) if default_work_root else None
    database_path: Path | None = None
    try:
        database_already_exists = roots.database_path.exists()
        operation_time = now or datetime.now(UTC)
        if database_already_exists:
            connection = open_database(roots.database_path, OpenMode.READ_WRITE)
            connection.close()
            reconcile_database_publication(roots.database_path)
            ensure_directory_chain(roots)
        else:
            initialize_database(roots, operation_time)
            database_path = roots.database_path
        projection_facts = store.read_all_generated_view_facts(operation_time)
        rendered_attempt_briefs = build_selected_attempt_brief_views(
            projection_facts.attempts, ArtifactRepository(roots)
        )
        if isinstance(rendered_attempt_briefs, work_brief_models.WorkBriefFailure):
            if git_exclude_path is None and database_path is None:
                return rendered_attempt_briefs
            return InitializationAfterCommittedEffects(
                git_exclude_path,
                database_path,
                rendered_attempt_briefs,
            )
        archived_briefs = archive_attempt_views(projection_facts, ArtifactRepository(roots), rendered_attempt_briefs)
        if isinstance(archived_briefs, work_brief_models.WorkBriefFailure):
            return (
                archived_briefs
                if git_exclude_path is None and database_path is None
                else InitializationAfterCommittedEffects(git_exclude_path, database_path, archived_briefs)
            )
        rebuild_result = rebuild_facts(
            projection_facts, roots.work_root, archived_briefs, ComposedBoardReader(roots, store), operation_time
        )
        if rebuild_result.warning is not None:
            raise FileIOError(
                FileIOErrorCode.VIEW_REFRESH_FAILED, f"{rebuild_result.warning.message} {rebuild_result.warning.repair}"
            )
    except (StorageError, ArtifactError, FileIOError) as error:
        if isinstance(error, StorageError) and error.invariant_violation:
            raise
        if git_exclude_path is None and database_path is None:
            raise
        return InitializationAfterCommittedEffects(git_exclude_path, database_path, error)
    return InitReceipt(
        roots.work_root,
        roots.database_path,
        projection_facts.project_revision,
        database_already_exists,
    )


def _error_diagnostic(code: str, path: Path, message: str, hint: str | None = None) -> Diagnostic:
    return Diagnostic(code=code, severity=Severity.ERROR, path=path, message=message, hint=hint)


def read_state_for_validation(
    database_path: Path, store: ports.ValidatedStateReader
) -> stored_state.StoredWorkState | ValidationReport:
    """Read and structurally validate the authoritative SQLite snapshot once."""

    try:
        return store.validated_snapshot()
    except StorageError as error:
        return ValidationReport((_error_diagnostic(error.code.value, database_path, str(error)),))


def _close_decision_failure(receipt: stored_state.StoredTransitionReceipt) -> str | None:
    """Strictly decode one close receipt's stored decision input; retained decision/v1 closes carry none."""

    match receipt.input_schema:
        case "decision/v1":
            if bytes(receipt.input_payload) != b"{}":
                return f"Close history {int(receipt.history_id)} has a retained decision/v1 input that is not empty."
            return None
        case "pinboard-close-decision/v1":
            try:
                value = msgspec.json.decode(bytes(receipt.input_payload), type=action_models.CloseInputPayload)
            except (msgspec.DecodeError, ValueError) as error:
                return f"Close history {int(receipt.history_id)} has an invalid close decision: {error}"
            if msgspec.json.encode(value, order="sorted") != bytes(receipt.input_payload):
                return f"Close history {int(receipt.history_id)} has a noncanonical close decision."
            return None
        case _:
            return f"Close history {int(receipt.history_id)} has unsupported input schema {receipt.input_schema!r}."


def validate_loaded_work_state(
    work_root: Path,
    state: stored_state.StoredWorkState,
    attempt_briefs: Mapping[AttemptId, bytes],
    board_history: query_models.BoardHistoryFacts,
    *,
    now: datetime,
) -> ValidationReport:
    """Verify accepted bytes, then classify replaceable generated-view drift against the decoded board facts."""

    diagnostics: list[Diagnostic] = []
    verified_artifacts: dict[ArtifactRefId, bytes] = {}
    for reference in state.artifact_references:
        try:
            verified_artifacts[reference.artifact_ref_id] = read_reference(work_root, reference)
        except ArtifactError as error:
            diagnostics.append(_error_diagnostic(error.code.value, work_root / reference.selector, str(error)))
    all_attempt_briefs = dict(attempt_briefs)
    archived = history_archives.verify_archives(state, verified_artifacts)
    if isinstance(archived, work_brief_models.WorkBriefFailure):
        diagnostics.append(_error_diagnostic(archived.code.value, work_root, archived.message))
    else:
        all_attempt_briefs.update(
            (identity, history_archives.render_archive(archive)) for identity, archive in archived.items()
        )
    try:
        candidate_snapshots.validate_candidate_snapshot_history(
            state, verified_artifacts, {} if isinstance(archived, work_brief_models.WorkBriefFailure) else archived
        )
    except ValueError as error:
        diagnostics.append(
            _error_diagnostic(ValidationDiagnosticCode.CANDIDATE_SNAPSHOT_INVALID.value, work_root, str(error))
        )
    packages = lifecycle_packages.validate_checkpoint_review_packages(
        state.lifecycle,
        state.artifact_references,
        state.transition_receipts,
        verified_artifacts,
        {} if isinstance(archived, work_brief_models.WorkBriefFailure) else archived,
    )
    if isinstance(packages, work_brief_models.WorkBriefFailure):
        diagnostics.append(_error_diagnostic(packages.code.value, work_root, packages.message))
    else:
        completion_packages = lifecycle_packages.validate_completion_review_packages(
            state.lifecycle,
            state.artifact_references,
            state.transition_receipts,
            verified_artifacts,
            packages,
            {} if isinstance(archived, work_brief_models.WorkBriefFailure) else archived,
        )
        if isinstance(completion_packages, work_brief_models.WorkBriefFailure):
            diagnostics.append(
                _error_diagnostic(completion_packages.code.value, work_root, completion_packages.message)
            )
    diagnostics.extend(
        _error_diagnostic(ValidationDiagnosticCode.CLOSE_DECISION_INVALID.value, work_root, message)
        for receipt in state.transition_receipts
        if receipt.action_kind == decision_models.ActionKind.CLOSE
        and (message := _close_decision_failure(receipt)) is not None
    )
    diagnostics.extend(
        _error_diagnostic(ValidationDiagnosticCode.TRANSITION_RECEIPT_DAMAGED.value, work_root, message)
        for receipt in state.transition_receipts
        if receipt.action_kind == decision_models.ActionKind.CANCEL
        and (message := cancellation.receipt_failure(receipt)) is not None
    )
    view_root = work_root / "views"
    expected_views = derive_expected_view_bytes(state, all_attempt_briefs, board_history, now=now)
    diagnostics.extend(
        _error_diagnostic(
            ValidationDiagnosticCode.TRANSITION_RECEIPT_DAMAGED.value,
            view_root / "history" / f"{damaged.history_id}.md",
            queries.damaged_receipt_message(damaged),
            queries.damaged_receipt_validation_recovery(damaged),
        )
        for damaged in expected_views.damaged
    )
    for selector, expected in expected_views.views.items():
        path = view_root / selector
        try:
            actual_view_bytes = path.read_bytes()
        except OSError:
            actual_view_bytes = None
        if actual_view_bytes != expected:
            diagnostics.append(
                Diagnostic(
                    ValidationDiagnosticCode.VIEW_REFRESH_REQUIRED.value,
                    Severity.WARNING,
                    path,
                    "Generated view is absent or stale; SQLite remains authoritative.",
                    "Run 'pinboard views rebuild'.",
                )
            )
    return ValidationReport(tuple(diagnostics))
