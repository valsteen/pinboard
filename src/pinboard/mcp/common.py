"""Shared MCP root composition and transport projection helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from pinboard.adapters.files.models import AffectedViews, ViewRefreshResult, ViewWarning
from pinboard.adapters.files.root import resolve_shared_repository_root, resolve_source_checkout_root
from pinboard.adapters.files.views import refresh_facts
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import (
    candidate_snapshots,
    stored_state,
    work_brief_models,
    work_briefs,
)
from pinboard.application.mutation_models import CommittedEffect
from pinboard.application.ports import GeneratedViewReader
from pinboard.domain.errors import (
    ChangedSurface,
    EffectDisposition,
    FailureDetails,
    RetryDisposition,
)
from pinboard.mcp import contracts, execution, tool_names
from pinboard.mcp.contracts import JsonValue

type IntegerBoundaryValue = bool | int | float | str | None


def _item_status_failure(
    code: str,
    message: str,
    details: FailureDetails | None,
) -> execution.OperationResult:
    rendered = _details_json(details)
    return execution.OperationResult(
        {
            "schema": "pinboard-mcp-item-status-result/v1",
            "status": "rejected",
            "code": code,
            "message": message,
            "state_changed": False,
            **rendered,
        },
        "rejected",
        None,
    )


def _read_failure(
    schema: str,
    code: str,
    message: str,
    details: FailureDetails | None,
) -> execution.OperationResult:
    return execution.OperationResult(
        {
            "schema": schema,
            "status": "rejected",
            "code": code,
            "message": message,
            "state_changed": False,
            **_details_json(details),
        },
        "rejected",
        None,
    )


def _resolve_durable(project_root: str, work_root: str) -> DurableRoots:
    source_checkout = resolve_source_checkout_root(Path(project_root))
    shared_repository = resolve_shared_repository_root(source_checkout)
    return _require_initialized_durable(shared_repository, Path(work_root))


def compose_store(durable: DurableRoots) -> SQLiteWorkStore:
    return SQLiteWorkStore(durable.database_path)


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


def _details_json(details: FailureDetails | None) -> dict[str, JsonValue]:
    if details is None:
        return {
            "effect": EffectDisposition.UNCHANGED.value,
            "retry": RetryDisposition.CORRECT_INPUT.value,
            "changed_surfaces": [],
            "observed": [],
            "mismatches": [],
        }
    return {
        "effect": details.effect.value,
        "retry": details.retry.value,
        "changed_surfaces": [surface.value for surface in details.changed_surfaces],
        "observed": [{"field": fact.field, "value": fact.value} for fact in details.observed],
        "mismatches": [
            {"field": mismatch.field, "expected": mismatch.expected, "observed": mismatch.observed}
            for mismatch in details.mismatches
        ],
    }


def _refresh_affected_views(
    durable: DurableRoots,
    store: GeneratedViewReader,
    affected: AffectedViews,
    now: datetime,
) -> ViewRefreshResult:
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
    return refresh_facts(facts, durable.work_root, briefs)


def _artifact_reference_json(reference: stored_state.ArtifactReference) -> dict[str, JsonValue]:
    return {
        "artifact_ref_id": int(reference.artifact_ref_id),
        "kind": reference.kind.value,
        "key": reference.key,
        "revision": reference.revision,
        "selector": reference.selector,
        "sha256": reference.content_sha256,
        "size_bytes": reference.size_bytes,
        "accepted_revision": reference.accepted_revision,
    }


def _transition_action_json(action_id: contracts.ActionIdentity) -> dict[str, JsonValue]:
    return {"kind": action_id.kind.value, "subject": action_id.subject}


def _committed_authority_fields(effect: CommittedEffect, warning: ViewWarning | None) -> dict[str, JsonValue]:
    """Render the durable receipt and optional warning; callers refresh views explicitly."""
    return {
        "status": "committed" if warning is None else "committed-with-warning",
        "committed_revision": effect.receipt.project_revision,
        "history_id": int(effect.receipt.history_id),
        "state_changed": True,
        "effect": EffectDisposition.COMMITTED.value,
        "retry": RetryDisposition.DO_NOT_RETRY.value,
        "changed_surfaces": [ChangedSurface.LEDGER.value],
        "warning": None if warning is None else {"message": warning.message, "recovery": warning.repair},
    }


def _require_initialized_durable(shared_repository: Path, work_root: Path) -> DurableRoots:
    durable = resolve_durable_roots(shared_repository, work_root)
    if not durable.database_path.is_file():
        default_work_root = shared_repository / ".codex" / "pinboard"
        raise ValueError(
            f"Pinboard work state is unavailable at {durable.work_root}; use the exact initialized work root. "
            f"The default for this repository is {default_work_root}."
        )
    return durable
