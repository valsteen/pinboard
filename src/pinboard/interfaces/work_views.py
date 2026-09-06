"""Read accepted brief content and refresh replaceable generated views.

An ordinary refresh receives committed state, reads only selected attempt briefs,
and writes only selected generated files. Explicit rebuild derives the complete
projection. SQLite and accepted artifacts remain authoritative.
"""

from collections.abc import Mapping
from datetime import datetime

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.files.models import AffectedViews, ViewRefreshResult, ViewWarning
from pinboard.adapters.files.views import rebuild_state as rebuild_file_views
from pinboard.adapters.files.views import refresh_state as refresh_file_views
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.domain.identifiers import AttemptId
from pinboard.interfaces import cli_commands
from pinboard.interfaces.errors import WorkBriefError
from pinboard.interfaces.work_briefs import build_attempt_brief_views


def read_attempt_brief_views(
    roots: cli_commands.ResolvedRoots,
    state: stored_state.StoredWorkState,
    attempt_ids: tuple[AttemptId, ...] | None = None,
) -> dict[AttemptId, bytes]:
    return build_attempt_brief_views(
        state,
        ArtifactRepository(resolve_durable_roots(roots.shared_repository, roots.work)),
        attempt_ids,
    )


def refresh(
    roots: cli_commands.ResolvedRoots,
    current_state: stored_state.StoredWorkState,
    affected: AffectedViews,
    now: datetime,
    attempt_briefs: Mapping[AttemptId, bytes] | None = None,
) -> ViewRefreshResult:
    try:
        supplied_briefs = {} if attempt_briefs is None else attempt_briefs
        missing_attempts = tuple(attempt for attempt in affected.attempts if attempt not in supplied_briefs)
        rendered_briefs = {
            **read_attempt_brief_views(roots, current_state, missing_attempts),
            **supplied_briefs,
        }
    except WorkBriefError as error:
        return ViewRefreshResult(
            current_state.lifecycle.project.revision,
            ViewWarning(
                f"The SQLite transition succeeded, but generated views need repair: {error} "
                "Run 'pinboard views rebuild'.",
                "Run 'pinboard views rebuild'.",
            ),
        )
    return refresh_file_views(current_state, roots.work, affected, rendered_briefs, now=now)


def rebuild(roots: cli_commands.ResolvedRoots, store: SQLiteWorkStore, now: datetime) -> ViewRefreshResult:
    current_state = store.snapshot()
    try:
        attempt_briefs = read_attempt_brief_views(roots, current_state)
    except WorkBriefError as error:
        return ViewRefreshResult(
            current_state.lifecycle.project.revision,
            ViewWarning(
                f"Generated views could not be rebuilt: {error} "
                "Resolve the accepted work-brief problem and run 'pinboard views rebuild' again.",
                "Resolve the accepted work-brief problem and run 'pinboard views rebuild' again.",
            ),
        )
    return rebuild_file_views(current_state, roots.work, attempt_briefs, now=now)
