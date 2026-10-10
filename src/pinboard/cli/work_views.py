"""Read accepted brief content and refresh replaceable generated views.

An ordinary refresh reads only facts and accepted brief bytes named by the
committed effect, plus the live portfolio for the two board projections. Explicit
rebuild reads the complete declared projection and reconciles its generated files.
SQLite and accepted artifacts remain authoritative.
"""

from collections.abc import Mapping
from datetime import datetime

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.files.models import AffectedViews, ViewRefreshResult, ViewWarning
from pinboard.adapters.files.views import rebuild_facts as rebuild_file_views
from pinboard.adapters.files.views import refresh_facts as refresh_file_views
from pinboard.application import history_archives, ports, query_models, stored_state, work_brief_models
from pinboard.application.mutation_models import CommittedEffect
from pinboard.application.work_briefs import build_attempt_brief_views, build_selected_attempt_brief_views
from pinboard.domain.identifiers import AttemptId


def read_attempt_brief_views(
    durable: DurableRoots,
    state: stored_state.StoredWorkState,
) -> work_brief_models.WorkBriefResult[Mapping[AttemptId, bytes]]:
    return build_attempt_brief_views(state, ArtifactRepository(durable))


def refresh(
    durable: DurableRoots,
    store: ports.GeneratedViewReader,
    affected: AffectedViews,
    now: datetime,
) -> ViewRefreshResult:
    facts = store.read_generated_view_facts(affected.items, affected.attempts, affected.history_receipts, now)
    attempt_briefs = build_selected_attempt_brief_views(facts.attempts, ArtifactRepository(durable))
    if isinstance(attempt_briefs, work_brief_models.WorkBriefFailure):
        return ViewRefreshResult(
            facts.project_revision,
            ViewWarning(
                f"The SQLite transition succeeded, but generated views need repair: {attempt_briefs} "
                "Run 'pinboard views rebuild'.",
                "Run 'pinboard views rebuild'.",
            ),
        )
    archives = archive_attempt_views(facts, ArtifactRepository(durable), attempt_briefs)
    if isinstance(archives, work_brief_models.WorkBriefFailure):
        return ViewRefreshResult(
            facts.project_revision,
            ViewWarning(archives.message, "Resolve the historical archive mismatch and run 'pinboard views rebuild'."),
        )
    return refresh_file_views(facts, durable.work_root, archives, store, now)


def refresh_effect(
    durable: DurableRoots, store: ports.GeneratedViewReader, effect: CommittedEffect, now: datetime
) -> ViewRefreshResult:
    return refresh(
        durable,
        store,
        AffectedViews(effect.work_item_ids, effect.attempt_ids, (effect.receipt.history_id,)),
        now,
    )


def archive_attempt_views(
    facts: query_models.GeneratedViewFacts,
    artifacts: ArtifactRepository,
    attempt_briefs: Mapping[AttemptId, bytes],
) -> work_brief_models.WorkBriefResult[Mapping[AttemptId, bytes]]:
    rendered = dict(attempt_briefs)
    for selected in facts.attempts:
        if selected.archive is None:
            continue
        reference, original = selected.archive
        source_bytes = {value.artifact_ref_id: artifacts.read(value) for value in original.artifact_references}
        archive = history_archives.verify_archive(reference, artifacts.read(reference), original, source_bytes)
        if isinstance(archive, work_brief_models.WorkBriefFailure):
            return archive
        rendered[selected.attempt.attempt_id] = history_archives.render_archive(archive)
    return rendered


def rebuild(durable: DurableRoots, store: ports.GeneratedViewSetReader, now: datetime) -> ViewRefreshResult:
    facts = store.read_all_generated_view_facts(now)
    attempt_briefs = build_selected_attempt_brief_views(facts.attempts, ArtifactRepository(durable))
    if isinstance(attempt_briefs, work_brief_models.WorkBriefFailure):
        return ViewRefreshResult(
            facts.project_revision,
            ViewWarning(
                f"Generated views could not be rebuilt: {attempt_briefs} "
                "Resolve the accepted work-brief problem and run 'pinboard views rebuild' again.",
                "Resolve the accepted work-brief problem and run 'pinboard views rebuild' again.",
            ),
        )
    archives = archive_attempt_views(facts, ArtifactRepository(durable), attempt_briefs)
    if isinstance(archives, work_brief_models.WorkBriefFailure):
        return ViewRefreshResult(
            facts.project_revision,
            ViewWarning(archives.message, "Resolve the historical archive mismatch and run 'pinboard views rebuild'."),
        )
    return rebuild_file_views(facts, durable.work_root, archives, store, now)
