"""Local-stdio MCP boundary with bounded synchronous application execution."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TextIO

import anyio
import msgspec
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from pinboard import __version__
from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from pinboard.adapters.files.models import AffectedViews, ViewRefreshResult, ViewWarning
from pinboard.adapters.files.root import resolve_shared_repository_root, resolve_source_checkout_root
from pinboard.adapters.files.views import refresh_facts
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import (
    proposal_models,
    proposals,
    queries,
    query_models,
    service,
    work_brief_models,
    work_briefs,
)
from pinboard.application.artifact_publication import AcceptedArtifactPublication
from pinboard.application.ports import GeneratedViewReader
from pinboard.domain.errors import (
    ArtifactAcceptanceAfterPublicationError,
    ChangedSurface,
    DecisionFailure,
    DecisionFailureCode,
    EffectDisposition,
    FailureDetails,
    RetryDisposition,
)
from pinboard.domain.identifiers import HostId, ItemId, TaskId
from pinboard.mcp import contracts

ITEM_STATUS_TOOL = "pinboard_item_status"
PROPOSAL_CREATE_TOOL = "pinboard_proposal_create"
BRIEF_PUBLISH_TOOL = "pinboard_brief_publish"
THREAD_NAME_PREFIX = "pinboard-mcp-worker"

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


@dataclass(frozen=True, slots=True)
class OperationResult:
    content: dict[str, JsonValue]
    classification: str
    commit_reference: str | None


class ExecutorBusy(RuntimeError):
    """The bounded executor rejected work before its callback began."""


class ExecutorClosed(RuntimeError):
    """The bounded executor no longer accepts work."""


class OperationCancelled(RuntimeError):
    """A running callback observed its request-local cancellation token."""


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._cancelled.set()

    def checkpoint(self) -> None:
        if self.cancelled:
            raise OperationCancelled("The request was cancelled at a cooperative checkpoint.")


class Execution[Result]:
    def __init__(
        self,
        future: Future[Result],
        token: CancellationToken,
        finished: threading.Event,
    ) -> None:
        self._future = future
        self._token = token
        self.finished: threading.Event = finished

    async def result(self) -> Result:
        try:
            return await asyncio.wrap_future(self._future)
        except asyncio.CancelledError:
            self._token.cancel()
            if self._future.cancel():
                raise
            return await asyncio.wrap_future(self._future)


class BoundedExecutor:
    """A fixed worker pool whose semaphore bounds every unfinished callback."""

    def __init__(self, *, worker_count: int, unfinished_limit: int) -> None:
        if worker_count < 1 or unfinished_limit < worker_count:
            raise ValueError("The unfinished-work limit must cover at least one positive worker set.")
        self._pool = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix=THREAD_NAME_PREFIX)
        self._admission = threading.BoundedSemaphore(unfinished_limit)
        self._lock = threading.Lock()
        self._closed = False

    def submit[Result](self, callback: Callable[[CancellationToken], Result]) -> Execution[Result]:
        with self._lock:
            if self._closed:
                raise ExecutorClosed("The executor is closed.")
            if not self._admission.acquire(blocking=False):
                raise ExecutorBusy("The executor has reached its unfinished-work limit.")
            token = CancellationToken()
            finished = threading.Event()
            try:
                future = self._pool.submit(callback, token)
            except BaseException:
                self._admission.release()
                raise

            def release(_future: Future[Result]) -> None:
                self._admission.release()
                finished.set()

            future.add_done_callback(release)
            return Execution(future, token, finished)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._pool.shutdown(wait=True, cancel_futures=True)


class Diagnostics:
    """Emit a fixed number of short, metadata-only stderr records."""

    def __init__(self, stream: TextIO, *, event_limit: int, line_limit: int) -> None:
        if event_limit < 1 or line_limit < 2:
            raise ValueError("Diagnostic bounds must be positive.")
        self._stream = stream
        self._event_limit = event_limit
        self._line_limit = line_limit
        self._events = 0
        self._lock = threading.Lock()

    def emit(
        self,
        *,
        event: str,
        request_id: int | None,
        operation: str | None,
        project_id: str | None,
        duration_ms: int | None,
        classification: str | None,
        commit_reference: str | None,
    ) -> None:
        fields = ["pinboard_mcp", f"version={__version__}", f"event={event}"]
        if request_id is not None:
            fields.append(f"request_id={request_id}")
        if operation is not None:
            fields.append(f"operation={operation}")
        if project_id is not None:
            fields.append(f"project={project_id}")
        if duration_ms is not None:
            fields.append(f"duration_ms={duration_ms}")
        if classification is not None:
            fields.append(f"classification={classification}")
        if commit_reference is not None:
            fields.append(f"commit={commit_reference}")
        line = " ".join(fields)
        with self._lock:
            if self._events >= self._event_limit:
                return
            self._events += 1
            self._stream.write(line[: self._line_limit - 1] + "\n")
            self._stream.flush()


def _item_status_json(status: query_models.ItemStatus) -> dict[str, JsonValue]:
    preparation = status.preparation
    return {
        "schema": status.schema,
        "authority": status.authority,
        "revision": status.revision,
        "item_id": status.item_id,
        "label": status.label,
        "state": status.state.value,
        "timing": None if status.timing is None else status.timing.value,
        "outcome_evidence": status.outcome_evidence,
        "next_action": status.next_action,
        "source": status.source,
        "notes": status.notes,
        "queue_position": status.queue_position,
        "attempts": [
            {
                "attempt_id": attempt.attempt_id,
                "state": attempt.state.value,
                "candidate_revision": attempt.candidate_revision,
            }
            for attempt in status.attempts
        ],
        "preparation": (
            None
            if preparation is None
            else {
                "definition_revision": preparation.definition_revision,
                "definition_digest": preparation.definition_digest,
                "task_id": preparation.task_id,
                "host_id": preparation.host_id,
                "lease_id": preparation.lease_id,
                "generation": preparation.generation,
                "expires_at": preparation.expires_at,
                "status": preparation.status.value,
            }
        ),
    }


def _read_item_status(
    project_root: str,
    work_root: str,
    item_id: str,
    token: CancellationToken,
) -> OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(
            {"project_root": project_root, "work_root": work_root, "item_id": item_id},
            type=contracts.ItemStatusRequest,
            strict=True,
        )
        durable = _resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError) as error:
        return _item_status_failure("ITEM_STATUS_INVALID", f"Cannot read item status: {error}", None)
    token.checkpoint()
    projected = queries.project_item_status(compose_store(durable), ItemId(request.item_id), datetime.now(UTC))
    if isinstance(projected, DecisionFailure):
        return _item_status_failure(projected.code.value, projected.message, projected.details)
    token.checkpoint()
    return OperationResult(_item_status_json(projected), "ok", projected.revision)


def _item_status_failure(
    code: str,
    message: str,
    details: FailureDetails | None,
) -> OperationResult:
    rendered = _details_json(details)
    return OperationResult(
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


def _resolve_durable(project_root: str, work_root: str) -> DurableRoots:
    source_checkout = resolve_source_checkout_root(Path(project_root))
    shared_repository = resolve_shared_repository_root(source_checkout)
    return resolve_durable_roots(shared_repository, Path(work_root))


def compose_store(durable: DurableRoots) -> SQLiteWorkStore:
    return SQLiteWorkStore(durable.database_path)


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


def _proposal_failure(failure: proposal_models.ProposalFailure | DecisionFailure) -> OperationResult:
    details = _details_json(failure.details)
    if failure.details is None and failure.code == DecisionFailureCode.PROPOSAL_ALREADY_EXISTS:
        details["retry"] = RetryDisposition.DO_NOT_RETRY.value
    content: dict[str, JsonValue] = {
        "schema": "pinboard-mcp-proposal-result/v1",
        "status": "rejected",
        "code": failure.code.value,
        "message": failure.message,
        "state_changed": details["effect"] == EffectDisposition.COMMITTED.value,
        **details,
    }
    if failure.code == DecisionFailureCode.PROPOSAL_ALREADY_EXISTS:
        content["recovery"] = "Read item status for the proposal_id before deciding whether any new proposal is needed."
    else:
        content["recovery"] = None
    return OperationResult(content, "rejected", None)


def _brief_failure(failure: work_brief_models.WorkBriefFailure | DecisionFailure) -> OperationResult:
    details = _details_json(failure.details if isinstance(failure, DecisionFailure) else None)
    return OperationResult(
        {
            "schema": "pinboard-mcp-brief-publication-result/v1",
            "status": "rejected",
            "code": failure.code.value,
            "message": failure.message,
            "state_changed": details["effect"] == EffectDisposition.COMMITTED.value,
            **details,
        },
        "rejected",
        None,
    )


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


def _proposal_created(
    project_root: str,
    work_root: str,
    proposal: dict[str, proposal_models.ProposalJsonValue],
    actor_task_id: str,
    actor_host_id: str,
    token: CancellationToken,
) -> OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(
            {
                "project_root": project_root,
                "work_root": work_root,
                "proposal": proposal,
                "actor_task_id": actor_task_id,
                "actor_host_id": actor_host_id,
            },
            type=contracts.ProposalCreateRequest,
            strict=True,
        )
    except (msgspec.ValidationError, ValueError) as error:
        return _proposal_failure(
            proposal_models.ProposalFailure(
                DecisionFailureCode.PROPOSAL_INVALID,
                f"Cannot decode proposal request: {error}",
                None,
            )
        )
    decoded = request.proposal
    durable = _resolve_durable(request.project_root, request.work_root)
    store = compose_store(durable)
    token.checkpoint()
    now = datetime.now(UTC)
    committed = service.create_proposal(
        store,
        proposals.convert_proposal(decoded),
        now,
        actor_task_id=TaskId(request.actor_task_id),
        actor_host_id=HostId(request.actor_host_id),
    )
    if isinstance(committed, DecisionFailure):
        return _proposal_failure(committed)
    view_result = _refresh_affected_views(
        durable,
        store,
        AffectedViews(committed.item_ids, committed.attempt_ids, (committed.receipt.history_id,)),
        now,
    )
    status = store.read_item_status(ItemId(decoded.proposal_id))
    if status is None or status.item.queue_position is None:
        raise RuntimeError("Committed proposal status did not reload exactly.")
    warning = view_result.warning
    content: dict[str, JsonValue] = {
        "schema": "pinboard-mcp-proposal-result/v1",
        "status": "committed" if warning is None else "committed-with-warning",
        "proposal_id": decoded.proposal_id,
        "position": status.item.queue_position,
        "item_state": status.item.state.value,
        "committed_revision": committed.receipt.project_revision,
        "history_id": int(committed.receipt.history_id),
        "state_changed": True,
        "effect": EffectDisposition.COMMITTED.value,
        "retry": RetryDisposition.DO_NOT_RETRY.value,
        "changed_surfaces": [ChangedSurface.LEDGER.value],
        "continuation": "Read item status or inspect the proposal before choosing a project disposition.",
        "warning": None if warning is None else {"message": warning.message, "recovery": warning.repair},
    }
    return OperationResult(
        content, "committed" if warning is None else "committed-warning", str(committed.receipt.project_revision)
    )


def _brief_published(
    project_root: str,
    work_root: str,
    brief: dict[str, work_brief_models.WorkBriefJsonValue],
    token: CancellationToken,
) -> OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(
            {"project_root": project_root, "work_root": work_root, "brief": brief},
            type=contracts.BriefPublishRequest,
            strict=True,
        )
    except (msgspec.ValidationError, ValueError) as error:
        return _brief_failure(
            work_brief_models.WorkBriefFailure(
                work_brief_models.WorkBriefErrorCode.BRIEF_INVALID,
                f"Cannot decode brief publication request: {error}",
            )
        )
    decoded = request.brief
    durable = _resolve_durable(request.project_root, request.work_root)
    store = compose_store(durable)
    token.checkpoint()
    now = datetime.now(UTC)
    try:
        publication = work_briefs.publish_work_brief(store, ArtifactRepository(durable), decoded, now)
    except ArtifactAcceptanceAfterPublicationError as error:
        changed_surfaces = [surface.value for surface in error.changed_surfaces]
        return OperationResult(
            {
                "schema": "pinboard-mcp-brief-publication-result/v1",
                "status": "failed-after-publication",
                "code": "ARTIFACT_ACCEPTANCE_FAILED",
                "message": "The brief was published, but its accepted reference could not be committed.",
                "state_changed": True,
                "effect": EffectDisposition.COMMITTED.value,
                "retry": RetryDisposition.DO_NOT_RETRY.value,
                "changed_surfaces": changed_surfaces,
                "observed": [],
                "mismatches": [],
                "published_selector": error.selector,
                "recovery": "Preserve the published selector and repair artifact-reference acceptance before continuing.",
            },
            "infrastructure-failure",
            error.selector,
        )
    if isinstance(publication, DecisionFailure):
        return _brief_failure(publication)
    view_result = _refresh_affected_views(durable, store, AffectedViews((), (), ()), now)
    warning = view_result.warning
    changed_surfaces: list[JsonValue] = [
        *([ChangedSurface.IMMUTABLE_ARTIFACT.value] if publication.artifact_created else []),
        *(
            [ChangedSurface.ACCEPTED_ARTIFACT_REFERENCE.value, ChangedSurface.LEDGER.value]
            if publication.ledger_changed
            else []
        ),
    ]
    state_changed = bool(changed_surfaces)
    if state_changed:
        status = "committed" if warning is None else "committed-with-warning"
        classification = "committed" if warning is None else "committed-warning"
    else:
        status = "unchanged" if warning is None else "unchanged-with-warning"
        classification = "unchanged" if warning is None else "unchanged-warning"
    content: dict[str, JsonValue] = {
        "schema": "pinboard-mcp-brief-publication-result/v1",
        "status": status,
        "reference": _artifact_reference_json(publication),
        "state_changed": state_changed,
        "effect": (EffectDisposition.COMMITTED.value if state_changed else EffectDisposition.UNCHANGED.value),
        "retry": (RetryDisposition.DO_NOT_RETRY.value if state_changed else RetryDisposition.RETRY_SAME_INPUT.value),
        "changed_surfaces": changed_surfaces,
        "continuation": "Use the verified reference when preparing or rebinding the matching attempt.",
        "warning": None if warning is None else {"message": warning.message, "recovery": warning.repair},
    }
    return OperationResult(
        content,
        classification,
        str(publication.reference.artifact_ref_id),
    )


def _artifact_reference_json(publication: AcceptedArtifactPublication) -> dict[str, JsonValue]:
    reference = publication.reference
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


async def _run_request(
    executor: BoundedExecutor,
    diagnostics: Diagnostics,
    request_id: int,
    operation: str,
    project_root: str,
    callback: Callable[[CancellationToken], OperationResult],
) -> dict[str, JsonValue]:
    project_id = hashlib.sha256(project_root.encode()).hexdigest()[:12]
    started = time.monotonic_ns()

    def emit(classification: str, commit_reference: str | None) -> None:
        diagnostics.emit(
            event="result",
            request_id=request_id,
            operation=operation,
            project_id=project_id,
            duration_ms=(time.monotonic_ns() - started) // 1_000_000,
            classification=classification,
            commit_reference=commit_reference,
        )

    try:
        execution = executor.submit(callback)
    except ExecutorBusy:
        emit("busy", None)
        busy: dict[str, JsonValue] = {
            "schema": "pinboard-mcp-execution-result/v1",
            "status": "busy",
            "code": "EXECUTOR_BUSY",
            "message": "The MCP executor is busy; retry after current work finishes.",
            "state_changed": False,
            "effect": EffectDisposition.UNCHANGED.value,
            "retry": RetryDisposition.RETRY_SAME_INPUT.value,
            "changed_surfaces": [],
            "observed": [],
            "mismatches": [],
        }
        return contracts.validate_result(operation, busy)
    try:
        result = await execution.result()
    except asyncio.CancelledError:
        emit("cancelled", None)
        raise
    except OperationCancelled as error:
        emit("cancelled", None)
        raise ToolError("The request was cancelled at a cooperative checkpoint.") from error
    except ToolError:
        emit("rejected", None)
        raise
    except Exception:
        emit("error", None)
        raise
    emit(result.classification, result.commit_reference)
    return contracts.validate_result(operation, result.content)


def create_server(executor: BoundedExecutor, diagnostics: Diagnostics) -> MCPServer:
    server = MCPServer("pinboard", version=__version__, log_level="ERROR")
    request_ids = itertools.count(1)

    @server.tool(
        name=ITEM_STATUS_TOOL,
        description="Read one current Pinboard item status from an explicit local project and work root.",
    )
    async def item_status(project_root: str, work_root: str, item_id: str) -> dict[str, JsonValue]:
        return await _run_request(
            executor,
            diagnostics,
            next(request_ids),
            ITEM_STATUS_TOOL,
            project_root,
            partial(_read_item_status, project_root, work_root, item_id),
        )

    @server.tool(
        name=PROPOSAL_CREATE_TOOL,
        description="Create one durable Pinboard proposal from its canonical structured input.",
    )
    async def proposal_create(
        project_root: str,
        work_root: str,
        proposal: dict[str, proposal_models.ProposalJsonValue],
        actor_task_id: str,
        actor_host_id: str,
    ) -> dict[str, JsonValue]:
        return await _run_request(
            executor,
            diagnostics,
            next(request_ids),
            PROPOSAL_CREATE_TOOL,
            project_root,
            partial(_proposal_created, project_root, work_root, proposal, actor_task_id, actor_host_id),
        )

    @server.tool(
        name=BRIEF_PUBLISH_TOOL,
        description="Publish and accept one canonical Pinboard work brief from structured input.",
    )
    async def brief_publish(
        project_root: str,
        work_root: str,
        brief: dict[str, work_brief_models.WorkBriefJsonValue],
    ) -> dict[str, JsonValue]:
        return await _run_request(
            executor,
            diagnostics,
            next(request_ids),
            BRIEF_PUBLISH_TOOL,
            project_root,
            partial(_brief_published, project_root, work_root, brief),
        )

    _install_boundary_contracts(server)
    return server


def _install_boundary_contracts(server: MCPServer) -> None:
    """Install exact schemas through the pinned SDK's mutable tool metadata seam."""
    definitions = (
        (
            ITEM_STATUS_TOOL,
            contracts.schema_for(contracts.ItemStatusRequest),
            contracts.union_schema_for(contracts.ITEM_STATUS_RESULT_TYPES),
        ),
        (
            PROPOSAL_CREATE_TOOL,
            contracts.schema_for(contracts.ProposalCreateRequest),
            contracts.union_schema_for(contracts.PROPOSAL_RESULT_TYPES),
        ),
        (
            BRIEF_PUBLISH_TOOL,
            contracts.schema_for(contracts.BriefPublishRequest),
            contracts.union_schema_for(contracts.BRIEF_PUBLICATION_RESULT_TYPES),
        ),
    )
    for name, input_schema, output_schema in definitions:
        tool = server._tool_manager.get_tool(name)
        if tool is None:
            raise RuntimeError(f"MCP tool '{name}' was not registered.")
        tool.parameters = input_schema
        tool.fn_metadata.output_schema = output_schema
        tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
        tool.fn_metadata.arg_model.model_rebuild(force=True)


def main() -> None:
    executor = BoundedExecutor(worker_count=2, unfinished_limit=4)
    diagnostics = Diagnostics(sys.stderr, event_limit=32, line_limit=256)
    diagnostics.emit(
        event="startup",
        request_id=None,
        operation=None,
        project_id=None,
        duration_ms=None,
        classification="ready",
        commit_reference=None,
    )
    try:
        anyio.run(create_server(executor, diagnostics).run_stdio_async)
    finally:
        executor.shutdown()
