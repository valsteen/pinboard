"""Read accepted brief content and refresh replaceable generated views.

An ordinary refresh reads only facts and accepted brief bytes named by the
committed effect, plus the composed board read for the two board projections.
Explicit rebuild reads the complete declared projection and reconciles its
generated files. SQLite and accepted artifacts remain authoritative.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from pinboard.adapters import review_operations
from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.files.models import AffectedViews, UnavailableViewRefresh, ViewRefreshResult, ViewWarning
from pinboard.adapters.files.views import rebuild_facts as rebuild_file_views
from pinboard.adapters.files.views import refresh_facts as refresh_file_views
from pinboard.adapters.generated_views import ComposedBoardReader
from pinboard.application import (
    history_archives,
    ports,
    queries,
    query_models,
    stored_state,
    work_brief_models,
)
from pinboard.application.mutation_models import CommittedEffect
from pinboard.application.work_briefs import build_attempt_brief_views, build_selected_attempt_brief_views
from pinboard.domain import work_models
from pinboard.domain.identifiers import AttemptId


@dataclass(frozen=True, slots=True)
class _LoadedReviewReferences:
    """Look up accepted candidate-review references in one loaded SQLite snapshot."""

    references: Mapping[str, stored_state.ArtifactReference]

    def read_candidate_review_references(self, keys: tuple[str, ...]) -> Mapping[str, stored_state.ArtifactReference]:
        return {key: reference for key in keys if (reference := self.references.get(key)) is not None}


def read_loaded_board_history(
    durable: DurableRoots, state: stored_state.StoredWorkState
) -> query_models.BoardHistoryFacts:
    """Decode the board facts of a loaded snapshot with the same file reads and ready-review decision as a refresh."""

    return review_operations.read_board_history(
        durable,
        _LoadedReviewReferences(
            {
                reference.key: reference
                for reference in state.artifact_references
                if reference.kind == work_models.ArtifactKind.EVIDENCE and reference.revision == 1
            }
        ),
        queries.select_board_records(state),
    )


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
) -> ViewRefreshResult | UnavailableViewRefresh:
    try:
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
                ViewWarning(
                    archives.message, "Resolve the historical archive mismatch and run 'pinboard views rebuild'."
                ),
            )
        return refresh_file_views(facts, durable.work_root, archives, ComposedBoardReader(durable, store), now)
    except ports.WorkStoreError as error:
        return UnavailableViewRefresh(
            ViewWarning(
                f"The ledger change committed, but generated views under '{durable.work_root}' could not read current projection facts: {error}",
                "Repair the named database condition, then run 'pinboard views rebuild'; do not replay the committed mutation.",
            )
        )


def refresh_effect(
    durable: DurableRoots, store: ports.GeneratedViewReader, effect: CommittedEffect, now: datetime
) -> ViewRefreshResult | UnavailableViewRefresh:
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
    return rebuild_file_views(facts, durable.work_root, archives, ComposedBoardReader(durable, store), now)
