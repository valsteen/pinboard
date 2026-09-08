"""Read accepted brief content and refresh replaceable generated views.

An ordinary refresh reads only facts and accepted brief bytes named by the
committed effect. Explicit rebuild reads the complete project and reconciles
the declared generated files. SQLite and accepted artifacts remain authoritative.
"""

from datetime import datetime

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.files.models import AffectedViews, ViewRefreshResult, ViewWarning
from pinboard.adapters.files.views import rebuild_state as rebuild_file_views
from pinboard.adapters.files.views import refresh_facts as refresh_file_views
from pinboard.application import ports, stored_state
from pinboard.application.mutation_models import CommittedEffect
from pinboard.domain.identifiers import AttemptId
from pinboard.interfaces.errors import WorkBriefFailure, WorkBriefResult
from pinboard.interfaces.work_briefs import build_attempt_brief_views, build_selected_attempt_brief_views


def read_attempt_brief_views(
    durable: DurableRoots,
    state: stored_state.StoredWorkState,
) -> WorkBriefResult[dict[AttemptId, bytes]]:
    return build_attempt_brief_views(
        state,
        ArtifactRepository(durable),
    )


def refresh(
    durable: DurableRoots,
    store: ports.GeneratedViewReader,
    affected: AffectedViews,
    now: datetime,
) -> ViewRefreshResult:
    facts = store.read_generated_view_facts(affected.items, affected.attempts, affected.history_receipts, now)
    attempt_briefs = build_selected_attempt_brief_views(facts.attempts, ArtifactRepository(durable))
    if isinstance(attempt_briefs, WorkBriefFailure):
        return ViewRefreshResult(
            facts.project_revision,
            ViewWarning(
                f"The SQLite transition succeeded, but generated views need repair: {attempt_briefs} "
                "Run 'pinboard views rebuild'.",
                "Run 'pinboard views rebuild'.",
            ),
        )
    return refresh_file_views(facts, durable.work_root, attempt_briefs)


def refresh_effect(
    durable: DurableRoots, store: ports.GeneratedViewReader, effect: CommittedEffect, now: datetime
) -> ViewRefreshResult:
    return refresh(
        durable,
        store,
        AffectedViews(effect.item_ids, effect.attempt_ids, (effect.receipt.history_id,)),
        now,
    )


def rebuild(durable: DurableRoots, store: ports.CompleteStateReader, now: datetime) -> ViewRefreshResult:
    current_state = store.snapshot()
    attempt_briefs = read_attempt_brief_views(durable, current_state)
    if isinstance(attempt_briefs, WorkBriefFailure):
        return ViewRefreshResult(
            current_state.lifecycle.project.revision,
            ViewWarning(
                f"Generated views could not be rebuilt: {attempt_briefs} "
                "Resolve the accepted work-brief problem and run 'pinboard views rebuild' again.",
                "Resolve the accepted work-brief problem and run 'pinboard views rebuild' again.",
            ),
        )
    return rebuild_file_views(current_state, durable.work_root, attempt_briefs, now=now)
