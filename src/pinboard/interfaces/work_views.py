"""Read accepted brief content and refresh replaceable generated views.

Each refresh or rebuild reads one SQLite snapshot, derives complete attempt
brief projections from verified artifacts, and then writes only generated view
files. SQLite and accepted artifacts remain authoritative.
"""

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
) -> dict[AttemptId, bytes]:
    return build_attempt_brief_views(
        state,
        ArtifactRepository(resolve_durable_roots(roots.shared_repository, roots.work)),
    )


def refresh(
    roots: cli_commands.ResolvedRoots,
    store: SQLiteWorkStore,
    affected: AffectedViews,
    now: datetime,
) -> ViewRefreshResult:
    current_state = store.snapshot()
    try:
        attempt_briefs = read_attempt_brief_views(roots, current_state)
    except WorkBriefError as error:
        return ViewRefreshResult(
            current_state.lifecycle.project.revision,
            ViewWarning(
                f"The SQLite transition succeeded, but generated views need repair: {error} "
                "Run 'pinboard views rebuild'.",
                "Run 'pinboard views rebuild'.",
            ),
        )
    return refresh_file_views(current_state, roots.work, affected, attempt_briefs, now=now)


def refresh_shared_authority_views(
    roots: cli_commands.ResolvedRoots,
    store: SQLiteWorkStore,
    now: datetime,
) -> ViewRefreshResult:
    """Refresh the queue and history affected by subject authority changes."""

    return refresh(roots, store, AffectedViews(queue=True, history=True), now)


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
