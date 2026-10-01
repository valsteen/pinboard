"""MCP client for disposable boards: seeding operations and overview reads through the evaluated revision's server.

Requests are declared records sent to the exported revision's own ``scripts/pinboard --mcp`` through the declared
``mcp`` stdio client. Results are read through projections of only the fields the harness consumes; the harness
owns no Pinboard format, so it validates exactly those fields and ignores the rest. A missing tool, a tool error,
an unexpected status or a projection mismatch is a ``SeedFailure`` and is never repaired by compatibility code:
an older revision whose MCP contract differs cannot be seeded by this harness.
"""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import anyio
import msgspec
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp_types import CallToolResult

from evals.behavioral import processes
from evals.behavioral.export import SeedFailure

SEEDED_HOST_ID = "eval-host"
SEEDED_AT = "2026-09-26T09:00:00Z"


class Request(msgspec.Struct, frozen=True):
    """Base for request payloads sent to the evaluated revision."""


class Projection(msgspec.Struct, frozen=True):
    """Base for result projections: consumed fields are validated, unrelated fields are ignored."""


class Relation(Request, frozen=True):
    kind: Literal["independent", "follow-up"]
    item: str | None


class Obligation(Request, frozen=True):
    obligation_id: str
    statement: str
    deferral_policy: Literal["forbidden"]


class Proposal(Request, frozen=True):
    schema: Literal["pinboard-proposal/v2"]
    proposal_id: str
    created_at: str
    source_task_id: str
    user_label: str
    trigger: str
    evidence: list[str]
    why_it_matters: str
    relation: Relation
    effect: str
    unlock: str
    urgency_evidence: str
    freshness_assumptions: list[str]
    checkout_policy: Literal["isolated"]
    obligations: list[Obligation]


class ProposalCreate(Request, frozen=True):
    project_root: str
    work_root: str
    actor_task_id: str
    actor_host_id: str
    proposal: Proposal


class PreparationStart(Request, frozen=True):
    operation: Literal["start"]
    project_root: str
    work_root: str
    item_id: str
    task_id: str
    host_id: str
    ttl_seconds: int


class DefinitionCurrent(Request, frozen=True):
    operation: Literal["current"]
    project_root: str
    work_root: str
    item_id: str


class AcceptedScope(Request, frozen=True):
    digest: str
    revision: int


class Criterion(Request, frozen=True):
    number: int
    requirement: str


class ArchitectureImpact(Request, frozen=True):
    kind: Literal["none"]
    reason: str


class Disposition(Request, frozen=True):
    kind: Literal["terminal"]


class AuthorizationBasis(Request, frozen=True):
    item_id: str
    kind: Literal["accepted-scope"]
    scope_revision: int


class Verification(Request, frozen=True):
    authorization_basis: AuthorizationBasis
    obligation: str


class Checkpoint(Request, frozen=True):
    acceptance_criteria: list[Criterion]
    architecture_impact: ArchitectureImpact
    boundary: Literal["local"]
    checkpoint_id: str
    deferrals: list[str]
    disposition: Disposition
    outcome_description: str
    title: str
    verification: list[Verification]


class CorrespondenceTarget(Request, frozen=True):
    kind: Literal["criterion"]
    number: int


class Correspondence(Request, frozen=True):
    obligation_id: str
    target: CorrespondenceTarget


class Brief(Request, frozen=True):
    accepted_scope: AcceptedScope
    artifact_revision: int
    attempt_id: str
    base_revision: str
    bootstrap: list[str]
    branch: str
    checkout_selection: Literal["isolated"]
    checkpoint: Checkpoint
    compatibility: list[str]
    item_id: str
    non_goals: list[str]
    obligation_correspondence: list[Correspondence]
    outcome: str
    owner_task_id: str
    product_decision_and_provenance: str
    schema: Literal["pinboard-work-brief/v4"]
    scope: list[str]
    supported_production_roots: list[str]
    testing_strategy: str
    title: str


class BriefPublish(Request, frozen=True):
    project_root: str
    work_root: str
    brief: Brief


class ActionId(Request, frozen=True):
    kind: str
    subject: str


class LeasedActions(Request, frozen=True):
    role: Literal["preparer", "worker"]
    project_root: str
    work_root: str
    lease_id: str
    generation: int
    action_id: ActionId


class ProjectActions(Request, frozen=True):
    role: Literal["project"]
    project_root: str
    work_root: str
    action_id: ActionId


class Receipt(Request, frozen=True):
    action_id: ActionId
    subject_revision: str


class ActivatePayload(Request, frozen=True):
    brief_artifact_ref_id: int


class SubmitPayload(Request, frozen=True):
    candidate: str


class ReasonPayload(Request, frozen=True):
    reason: str


class ReviewedCompletion(Request, frozen=True):
    schema: Literal["pinboard-reviewed-completion/v2"]
    candidate: str
    evidence: str
    reviewer_task_id: str
    result_sha256: str
    review_sha256: str
    packages: list[str]


class LeasedTransition(Request, frozen=True):
    role: Literal["preparer", "worker"]
    project_root: str
    work_root: str
    lease_id: str
    generation: int
    receipt: Receipt
    payload: ActivatePayload | SubmitPayload


class ProjectTransition(Request, frozen=True):
    role: Literal["project"]
    project_root: str
    work_root: str
    receipt: Receipt
    payload: ReasonPayload | ReviewedCompletion
    actor_task_id: str
    actor_host_id: str


class AuthorityAcquire(Request, frozen=True):
    operation: Literal["acquire"]
    project_root: str
    work_root: str
    attempt_id: str
    task_id: str
    host_id: str
    ttl_seconds: int


class AuthorityRelease(Request, frozen=True):
    operation: Literal["release"]
    project_root: str
    work_root: str
    attempt_id: str
    lease_id: str
    generation: int


class InitialReview(Request, frozen=True):
    kind: Literal["initial"]
    attempt_id: str
    candidate_revision: str
    runtime: Literal["claude-code"]
    background: bool


class RecordReady(Request, frozen=True):
    kind: Literal["record-ready"]
    attempt_id: str
    candidate_revision: str
    candidate_snapshot_sha256: str
    accepted_brief_sha256: str
    result_sha256: str
    review_sha256: str
    reviewer_prompt_sha256: str
    reviewer_task_id: str
    verdict: Literal["ready"]
    acceptance_evidence: str


class ReviewJob(Request, frozen=True):
    project_root: str
    work_root: str
    review: InitialReview | RecordReady


class AttemptInspect(Request, frozen=True):
    project_root: str
    work_root: str
    attempt_id: str
    reconciliation: None


class Roots(Request, frozen=True):
    project_root: str
    work_root: str


class ItemStatus(Request, frozen=True):
    operation: Literal["item"]
    project_root: str
    work_root: str
    item_id: str


class Enveloped(Request, frozen=True):
    request: (
        PreparationStart
        | DefinitionCurrent
        | LeasedActions
        | ProjectActions
        | LeasedTransition
        | ProjectTransition
        | ItemStatus
    )


class AuthorityEnvelope(Request, frozen=True):
    request: AuthorityAcquire | AuthorityRelease


class Status(Projection, frozen=True):
    status: str


class Lease(Projection, frozen=True):
    status: str
    lease_id: str
    generation: int


class Definition(Projection, frozen=True):
    definition_revision: int
    definition_digest: str


class ArtifactReference(Projection, frozen=True):
    artifact_ref_id: int


class Published(Projection, frozen=True):
    status: str
    reference: ArtifactReference


class DiscoveredAction(Projection, frozen=True):
    subject_revision: str


class Actions(Projection, frozen=True):
    actions: list[DiscoveredAction]


class Digest(Projection, frozen=True):
    sha256: str


class ReviewCommission(Projection, frozen=True):
    status: str
    prompt_reference: Digest


class Inspection(Projection, frozen=True):
    candidate_recovery: Digest
    accepted_brief: Digest


class OverviewItem(Projection, frozen=True):
    item_id: str
    state: str
    attempt_id: str | None
    position: int | None
    depends_on: list[str]


class ItemState(Projection, frozen=True):
    state: str


class Overview(Projection, frozen=True):
    items: list[OverviewItem]


@dataclass(frozen=True)
class BoardClient:
    session: ClientSession
    window: processes.Window

    async def call[T: Projection](self, tool: str, arguments: Request, projection: type[T]) -> T:
        with anyio.fail_after(self.window.timeout(300)):
            return read_result(tool, await self.session.call_tool(tool, msgspec.to_builtins(arguments)), projection)


def read_result[T: Projection](tool: str, result: CallToolResult, projection: type[T]) -> T:
    """Project one tool result onto its consumed fields; any error or mismatch is a seed failure."""
    content = result.structured_content
    if result.is_error or not isinstance(content, dict):
        raise SeedFailure(f"{tool} returned an error or no structured result: {result.content!r}"[:2000])
    try:
        return msgspec.convert(content, type=projection)
    except msgspec.ValidationError as error:
        raise SeedFailure(f"{tool} result does not match the seeding contract: {error}: {content!r}"[:2000]) from error


@asynccontextmanager
async def connect(launcher: Path, log: Path, window: processes.Window) -> AsyncGenerator[BoardClient]:
    """Serve the evaluated revision's MCP server over stdio; its diagnostics append to ``log``."""
    parameters = StdioServerParameters(command=str(launcher), args=["--mcp"], env=os.environ.copy())
    # SDK teardown bounds: 0.5s writer flush, 2s grace, 2s termination, 2s reap, and exit polling.
    operation_window = window.reserving(7)
    try:
        with anyio.fail_after(operation_window.timeout(300)), log.open("a") as errlog:
            async with stdio_client(parameters, errlog=errlog) as streams, ClientSession(*streams) as session:
                await session.initialize()
                yield BoardClient(session, operation_window)
    except OSError as error:
        raise SeedFailure(f"the evaluated launcher could not serve MCP: {error}") from error


def require(status: str, accepted: tuple[str, ...], what: str) -> None:
    if status not in accepted:
        raise SeedFailure(f"{what} returned status {status}")


async def overview(
    launcher: Path, log: Path, project_root: Path, work_root: Path, window: processes.Window
) -> Overview:
    async with connect(launcher, log, window) as board:
        return await board.call(
            "pinboard_overview", Roots(project_root=str(project_root), work_root=str(work_root)), Overview
        )


async def tool_names(launcher: Path, log: Path, window: processes.Window) -> list[str]:
    async with connect(launcher, log, window) as board:
        return sorted(tool.name for tool in (await board.session.list_tools()).tools)


async def item_states(
    launcher: Path, log: Path, project_root: Path, work_root: Path, items: tuple[str, ...], window: processes.Window
) -> list[tuple[str, str]]:
    async with connect(launcher, log, window) as board:
        states = []
        for item in items:
            request = Enveloped(
                ItemStatus(operation="item", project_root=str(project_root), work_root=str(work_root), item_id=item)
            )
            states.append((item, (await board.call("pinboard_item_status", request, ItemState)).state))
        return states
