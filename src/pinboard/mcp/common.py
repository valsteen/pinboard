"""Shared MCP root composition and transport projection helpers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from pinboard.adapters.files.models import AffectedViews, UnavailableViewRefresh, ViewRefreshResult, ViewWarning
from pinboard.adapters.files.root import resolve_shared_repository_root, resolve_source_checkout_root
from pinboard.adapters.files.views import refresh_facts
from pinboard.adapters.generated_views import ComposedBoardReader
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import (
    candidate_snapshots,
    ports,
    stored_state,
    work_brief_models,
    work_briefs,
)
from pinboard.application.ports import GeneratedViewReader
from pinboard.domain import work_models
from pinboard.domain.errors import (
    ChangedSurface,
    FailureDetails,
)
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import contracts, tool_names
from pinboard.mcp.contracts import JsonValue

# Shared by every lease-protected worker rejection so an expired lease is never replaced over another owner.
ATTEMPT_LEASE_RECOVERY = (
    "Read pinboard_attempt_authority operation status for this attempt_id. Reacquire with operation acquire, using "
    "your own trusted task and host and the dispatched ttl_seconds, only when that status shows your own lease "
    "expired; then reinspect the attempt and discover a fresh action through pinboard_actions with the new lease and "
    "generation. If another owner holds live authority, or the lease was released or revoked, stop and report it; "
    "never revoke it or reuse the old lease."
)


def _resolve_durable(project_root: str, work_root: str) -> DurableRoots:
    return resolve_mcp_roots(project_root, work_root)[1]


def resolve_mcp_roots(project_root: str, work_root: str) -> tuple[Path, DurableRoots]:
    source_checkout = resolve_source_checkout_root(Path(project_root))
    shared_repository = resolve_shared_repository_root(source_checkout)
    return source_checkout, _require_initialized_durable(shared_repository, Path(work_root))


_resolve_source_and_durable = resolve_mcp_roots


def compose_store(durable: DurableRoots) -> SQLiteWorkStore:
    return SQLiteWorkStore(durable.database_path)


def select_capture_item(  # noqa: C901 - one MCP boundary interprets its supported item and attempt selectors
    shared_repository: Path, work_root: str | None, arguments: Mapping[str, JsonValue]
) -> str | None:
    request = arguments.get("request")
    selected = request if isinstance(request, dict) else arguments
    item_id = selected.get("item_id")
    if isinstance(item_id, str):
        return item_id
    for field in ("brief", "proposal"):
        value = arguments.get(field)
        if isinstance(value, dict):
            item_id = value.get("item_id")
            if isinstance(item_id, str):
                return item_id
            relation = value.get("relation")
            if isinstance(relation, dict) and isinstance(related_item := relation.get("item"), str):
                return related_item
    attempt_id = selected.get("attempt_id")
    if not isinstance(attempt_id, str):
        review = arguments.get("review")
        if isinstance(review, dict):
            attempt_id = review.get("attempt_id")
    action = selected.get("action_id") or selected.get("receipt")
    dispatch = arguments.get("dispatch")
    if action is None and isinstance(dispatch, dict):
        action = dispatch.get("receipt")
    subject: str | None = None
    if isinstance(action, dict):
        nested_action = action.get("action_id")
        action_id = nested_action if isinstance(nested_action, dict) else action
        subject_value = action_id.get("subject")
        if isinstance(subject_value, str):
            subject = subject_value
            attempt_id = subject_value
    if isinstance(attempt_id, str) and work_root is not None:
        durable = resolve_durable_roots(shared_repository, Path(work_root))
        context = compose_store(durable).read_attempt_context(AttemptId(attempt_id))
        if context is not None:
            return str(context.work_item_id)
    return subject


def _candidate_recovery_view(
    durable: DurableRoots,
    evidence: candidate_snapshots.CandidateSnapshotEvidence,
) -> contracts.CandidateRecoveryPresent:
    snapshot = evidence.snapshot
    return contracts.CandidateRecoveryPresent(
        candidate_snapshots.candidate_kind(snapshot),
        snapshot.candidate,
        snapshot.branch,
        snapshot.preimage_revision,
        int(evidence.reference.artifact_ref_id),
        evidence.reference.selector,
        evidence.reference.content_sha256,
        evidence.reference.size_bytes,
        contracts.CandidateRestoreInvocation(
            tool_names.CANDIDATE_RESTORE_TOOL,
            contracts.CandidateRestoreArguments(None, str(durable.work_root), snapshot.attempt_id, snapshot.candidate),
            ("project_root",),
        ),
    )


def _failure_evidence(
    details: FailureDetails | None,
) -> tuple[tuple[contracts.FailureObservation, ...], tuple[contracts.FailureMismatch, ...]]:
    if details is None:
        return (), ()
    return (
        tuple(contracts.FailureObservation(fact.field, fact.value) for fact in details.observed),
        tuple(contracts.FailureMismatch(fact.field, fact.expected, fact.observed) for fact in details.mismatches),
    )


def _refresh_affected_views(
    durable: DurableRoots,
    store: GeneratedViewReader,
    affected: AffectedViews,
    now: datetime,
) -> ViewRefreshResult | UnavailableViewRefresh:
    try:
        facts = store.read_generated_view_facts(affected.items, affected.attempts, affected.history_receipts, now)
        briefs = work_briefs.build_selected_attempt_brief_views(facts.attempts, ArtifactRepository(durable))
        if isinstance(briefs, work_brief_models.WorkBriefFailure):
            return ViewRefreshResult(
                facts.project_revision,
                ViewWarning(
                    f"The SQLite mutation succeeded, but generated views need repair: {briefs}",
                    "Run 'pinboard views rebuild'.",
                ),
            )
        return refresh_facts(facts, durable.work_root, briefs, ComposedBoardReader(durable, store), now)
    except ports.WorkStoreError as error:
        return UnavailableViewRefresh(
            ViewWarning(
                f"The ledger change committed, but generated views under '{durable.work_root}' could not read current projection facts: {error}",
                "Repair the named database condition, then run 'pinboard views rebuild'; do not replay the committed mutation.",
            )
        )


def _refresh_published_ledger(
    durable: DurableRoots, store: GeneratedViewReader, details: FailureDetails | None, now: datetime
) -> ViewWarning | None:
    """Refresh an earlier confirmed ledger commit even when later publication failed."""
    if details is None or ChangedSurface.LEDGER not in details.changed_surfaces:
        return None
    return _refresh_affected_views(durable, store, AffectedViews((), (), ()), now).warning


def _review_evidence_reference(reference: stored_state.ArtifactReference) -> contracts.ReviewEvidenceReference:
    if reference.kind != work_models.ArtifactKind.EVIDENCE:
        raise ValueError(f"Expected an accepted evidence reference, got {reference.kind.value}.")
    return contracts.ReviewEvidenceReference(
        int(reference.artifact_ref_id),
        "evidence",
        reference.key,
        reference.revision,
        reference.selector,
        reference.content_sha256,
        reference.size_bytes,
        reference.accepted_revision,
    )


def _require_initialized_durable(shared_repository: Path, work_root: Path) -> DurableRoots:
    durable = resolve_durable_roots(shared_repository, work_root)
    if not durable.database_path.is_file():
        default_work_root = resolve_durable_roots(shared_repository).work_root
        raise ValueError(
            f"Pinboard work state is unavailable at {durable.work_root}; use the exact initialized work root. "
            f"The default for this repository is {default_work_root}."
        )
    return durable
