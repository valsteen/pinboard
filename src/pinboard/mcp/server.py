"""Register the local-stdio MCP surface and compose thematic operations."""

from __future__ import annotations

import itertools
import sys
from functools import partial

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from pinboard import __version__
from pinboard.application import (
    proposal_models,
    work_brief_models,
)
from pinboard.mcp import contract_schemas, contracts, execution, job_operations, mutation_operations, read_operations
from pinboard.mcp.contracts import JsonValue
from pinboard.mcp.tool_names import (
    ACTIONS_TOOL,
    ARTIFACT_VERIFY_TOOL,
    ATTEMPT_AUTHORITY_TOOL,
    ATTEMPT_INSPECT_TOOL,
    BRIEF_CONTRACT_TOOL,
    BRIEF_PUBLISH_TOOL,
    BRIEF_REVIEW_TOOL,
    BRIEF_SOURCES_TOOL,
    CANDIDATE_OBSERVE_TOOL,
    CANDIDATE_RESTORE_TOOL,
    DISPATCH_TOOL,
    ITEM_DEFINITION_TOOL,
    ITEM_STATUS_TOOL,
    ORDER_TOOL,
    OVERVIEW_TOOL,
    PARALLEL_PREVIEW_TOOL,
    PREPARATION_AUTHORITY_TOOL,
    PROPOSAL_CREATE_TOOL,
    REVIEW_JOB_TOOL,
    TRANSITION_TOOL,
)

type IntegerBoundaryValue = bool | int | float | str | None

LOCAL_AUTHORITY_ANNOTATIONS: ToolAnnotations = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


def create_server(executor: execution.BoundedExecutor, diagnostics: execution.Diagnostics) -> MCPServer:  # noqa: C901 - explicit installed SDK tool registration
    server = MCPServer(
        "pinboard",
        version=__version__,
        log_level="ERROR",
        instructions=(
            "Pinboard coordinates local repository work: intake, canonical briefs, status, legal actions, "
            "own leases, dispatch, independent review and recovery.\n\n"
            "For intake or coordination, load the complete existing workflow skill before constructing "
            "attributed calls: pinboard-intake for new work, or pinboard for coordination of existing work. "
            "Use this runtime's advertised native skill loader; if unavailable, read that skill's actual "
            "resolved SKILL.md completely. Follow that owner's sequencing and runtime identity instructions.\n\n"
            "For deferred tools, find the required Pinboard operation in this host's actual announced tool "
            "inventory. Select its full advertised callable name, including the connector prefix, not its "
            "short wire name. Resolve each host's names independently. If the full name is unknown, use "
            "supported native keyword discovery. If exact selection finds no match, reconcile the selected "
            "name with the advertised inventory before declaring the tool unavailable.\n\n"
            "Inspect the selected tool's negotiated strict schema and invoke that native callable with exact "
            "project_root and work_root. When its sole top-level property is request, keep the selected leaf inside "
            "that request object instead of flattening it. These instructions grant no identity, authority or permissions. "
            "A missing required MCP tool stops its operation; retired agent-workflow CLI commands are not "
            "substitutes."
        ),
    )
    request_ids = itertools.count(1)

    @server.tool(
        name=ORDER_TOOL,
        description="Save an explicitly human-authorized complete priority permutation against the current live order; fresh overview reconciles state, not caller commitment. Never grants launch authority.",
    )
    async def order(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            ORDER_TOOL,
            str(request.get("project_root", "")),
            partial(read_operations._order, {"request": request}),
        )

    @server.tool(
        name=PARALLEL_PREVIEW_TOOL,
        description="Read exact selected or current-only all-safe structural parallel constraints. Does not certify readiness, acquire authority or launch native tasks.",
    )
    async def parallel_preview(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            PARALLEL_PREVIEW_TOOL,
            str(request.get("project_root", "")),
            partial(read_operations._parallel_preview, {"request": request}),
        )

    @server.tool(
        name=BRIEF_CONTRACT_TOOL,
        description="Construct the strict work-brief contract or unresolved local/cross-boundary starter; no project facts or authority. Structural construction is not readiness review or activation. Follow the Pinboard coordination Skill for preparation.",
    )
    async def brief_contract(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            BRIEF_CONTRACT_TOOL,
            str(request.get("project_root", "")),
            partial(read_operations._brief_contract, {"request": request}),
        )

    @server.tool(
        name=BRIEF_SOURCES_TOOL,
        description="Plan selected-checkout sources, optionally publish an immutable explicit plan, or emit one verified inline/saved-plan batch; never opens ledger or acquires authority.",
    )
    async def source_preparation(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            BRIEF_SOURCES_TOOL,
            str(request.get("project_root", "")),
            partial(read_operations._brief_sources, {"request": request}),
        )

    @server.tool(
        name=ITEM_DEFINITION_TOOL,
        description="Read one full accepted Pinboard item definition or bounded descending definition history.",
    )
    async def item_definition(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            ITEM_DEFINITION_TOOL,
            str(request.get("project_root", "")),
            partial(read_operations._read_item_definition, {"request": request}),
        )

    @server.tool(
        name=BRIEF_REVIEW_TOOL,
        description="Publish an independent needs-correction brief review or read exact verified findings; neither grants dispatch readiness or authority.",
    )
    async def brief_review(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            BRIEF_REVIEW_TOOL,
            str(request.get("project_root", "")),
            partial(read_operations._brief_review, {"request": request}),
        )

    @server.tool(
        name=ITEM_STATUS_TOOL,
        description="Read one current Pinboard item status from an explicit local project and work root.",
    )
    async def item_status(project_root: str, work_root: str, item_id: str) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            ITEM_STATUS_TOOL,
            project_root,
            partial(read_operations._read_item_status, project_root, work_root, item_id),
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
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            PROPOSAL_CREATE_TOOL,
            project_root,
            partial(
                mutation_operations._proposal_created, project_root, work_root, proposal, actor_task_id, actor_host_id
            ),
        )

    @server.tool(
        name=BRIEF_PUBLISH_TOOL,
        description="Publish one canonical Pinboard work brief and accept its artifact reference. Publication is not readiness review or activation. Follow the Pinboard coordination Skill for preparation.",
    )
    async def brief_publish(
        project_root: str,
        work_root: str,
        brief: dict[str, work_brief_models.WorkBriefJsonValue],
    ) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            BRIEF_PUBLISH_TOOL,
            project_root,
            partial(mutation_operations._brief_published, project_root, work_root, brief),
        )

    @server.tool(
        name=OVERVIEW_TOOL,
        description="Read the current authoritative Pinboard work overview without changing durable state.",
        meta={"anthropic/alwaysLoad": True},
    )
    async def overview(project_root: str, work_root: str) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            OVERVIEW_TOOL,
            project_root,
            partial(read_operations._read_overview, project_root, work_root),
        )

    @server.tool(
        name=ACTIONS_TOOL,
        description="Discover exact current legal Pinboard actions and their strict payload contracts.",
    )
    async def action_discovery(
        request: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            ACTIONS_TOOL,
            str(request.get("project_root", "")),
            partial(read_operations._read_actions, {"request": request}),
        )

    @server.tool(
        name=ATTEMPT_INSPECT_TOOL,
        description="Inspect one exact Pinboard attempt, its accepted brief, evidence references, and continuation.",
    )
    async def attempt_inspect(project_root: str, work_root: str, attempt_id: str) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            ATTEMPT_INSPECT_TOOL,
            project_root,
            partial(read_operations._read_attempt_inspection, project_root, work_root, attempt_id),
        )

    @server.tool(
        name=ARTIFACT_VERIFY_TOOL,
        description="Verify an exact accepted Pinboard artifact reference and its immutable bytes.",
    )
    async def artifact_verify(
        project_root: str,
        work_root: str,
        artifact_ref_id: IntegerBoundaryValue,
        selector: str,
        sha256: str,
        size_bytes: IntegerBoundaryValue,
    ) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            ARTIFACT_VERIFY_TOOL,
            project_root,
            partial(
                read_operations._verify_artifact,
                project_root,
                work_root,
                artifact_ref_id,
                selector,
                sha256,
                size_bytes,
            ),
        )

    @server.tool(
        name=PREPARATION_AUTHORITY_TOOL,
        description="Read or change one exact Pinboard preparation authority.",
        annotations=LOCAL_AUTHORITY_ANNOTATIONS,
    )
    async def preparation_authority(
        request: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            PREPARATION_AUTHORITY_TOOL,
            str(request.get("project_root", "")),
            partial(mutation_operations._preparation_authority, {"request": request}),
        )

    @server.tool(
        name=ATTEMPT_AUTHORITY_TOOL,
        description="Read or change one exact Pinboard attempt authority.",
        annotations=LOCAL_AUTHORITY_ANNOTATIONS,
    )
    async def attempt_authority(
        request: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            ATTEMPT_AUTHORITY_TOOL,
            str(request.get("project_root", "")),
            partial(mutation_operations._attempt_authority, {"request": request}),
        )

    @server.tool(
        name=TRANSITION_TOOL,
        description="Apply one current Pinboard lifecycle action. Put project_root, work_root, role, receipt, payload and authority fields inside request. receipt contains ONLY action_id and subject_revision from pinboard_actions, not the whole action. For role project, put actor_task_id and actor_host_id in request; for role worker or preparer, put lease_id and generation there instead. Get the action-specific payload schema from pinboard_actions.",
    )
    async def lifecycle_transition(
        request: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            TRANSITION_TOOL,
            str(request.get("project_root", "")),
            partial(mutation_operations._transition, {"request": request}),
        )

    @server.tool(
        name=DISPATCH_TOOL,
        description=(
            "Publish verified Pinboard worker launch instructions; creates no worker or worker authority. "
            "Arguments are project_root, work_root and dispatch (no request wrapper). Unknown fields reject.\n"
            "All three dispatch leaves require kind, receipt, checkpoint_id, environment and prompt. "
            "receipt contains ONLY action_id:{kind:'dispatch',subject:<attempt_id>} and subject_revision "
            "copied from the fresh project dispatch action returned by pinboard_actions, not the whole action. "
            "checkpoint_id is the accepted brief's stable checkpoint ID. Use explicit prompt:null for "
            "canonical prompt construction. A supplied prompt string must match the canonical prompt.\n"
            "environment requires all ten fields: schema:'pinboard-dispatch/v2', runtime:'codex' or "
            "'claude-code', background:<boolean>, checkout:<exact source checkout>, branch:<recorded branch>, "
            "starting_revision:<accepted attempt base>, host_id:<trusted "
            "runtime host>, fresh_context:true, lease_ttl_seconds:<positive integer>, permissions:<array of "
            "already-authorized 'repository-read', 'repository-write', 'network', 'external-write' or "
            "'live-application' declarations>. Declarations grant no runtime access.\n"
            "kind:'ordinary' has only those common fields; a cross-boundary checkpoint reuses its exact "
            "accepted ready review. kind:'reviewed' additionally requires review_id "
            "and brief_review:<complete independent ready WorkBriefReview>. kind:'correction' additionally "
            "requires review_id, correction_history_id:<positive ID of the selected current canonical "
            "return-for-correction/v1 receipt>, and brief_review:<CorrectionSourceReview>, not an initial review.\n"
            "WorkBriefReview requires schema:'pinboard-work-brief-review/v3', attempt_id, checkpoint_id, "
            "accepted_brief_sha256, checkpoint_sha256, reviewed_authority_set_sha256, reviewer_task_id, status:'complete', "
            "verdict:'ready', and nonempty coverage. Bind the current checkpoint and ordered reviewed "
            "authority set; the reviewer must be independent. Every coverage record requires authority_id, "
            "family, owner, verdict:'covered' and counterexample_result. owner is exactly one of "
            "{disposition:'contract',contract_invariant:<text>}, {disposition:'acceptance',criterion:<positive "
            "integer>}, {disposition:'deferred',deferral_id:<ID>} or {disposition:'not-applicable',reason:<text>}, "
            "matching the brief's complete coverage. Needs-correction evidence is not ready evidence.\n"
            "CorrectionSourceReview requires schema:'pinboard-correction-source-review/v1', "
            "contract_review:<current effective WorkBriefReview>, starting_candidate:<exact accepted "
            "candidate identity>, correction_input:{reason:<exact selected correction reason>}, and "
            "assessment:<independent assessment>. starting_candidate requires role:'candidate', "
            "kind:'evidence', key, revision:<positive integer>, selector, content_sha256 and "
            "size_bytes:<nonnegative integer>. Preserve candidate/history binding and fresh source review.\n"
            "The negotiated strict schema and decoder remain authoritative. Dispatch publication may "
            "change immutable-artifact, accepted-artifact-reference and ledger surfaces, never lifecycle "
            "or worker authority; honor returned effect/retry facts. On ready, call the exact returned "
            "native_launch.tool with exactly native_launch.arguments. Do not add, remove or rewrite an "
            "argument. prompt_reference remains independently required immutable provenance, not an "
            "alternative launch input. Missing native launch capability stops execution; publication alone "
            "is not a launch. A worker launched from other arguments is invalid: stop it and use a fresh "
            "native launch from the returned recipe."
        ),
    )
    async def dispatch_job(project_root: str, work_root: str, dispatch: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            DISPATCH_TOOL,
            project_root,
            partial(job_operations._dispatch_job, project_root, work_root, dispatch),
        )

    @server.tool(
        name=CANDIDATE_OBSERVE_TOOL,
        description="Read one attempt's actual tracked working-tree candidate identity and omitted Git-visible nonignored untracked paths. Changes nothing; does not prepare files, freeze evidence, acquire authority, submit or decide acceptance. Prepare only intended files under separate authority, then reobserve before existing leased submission.",
    )
    async def candidate_observe(project_root: str, work_root: str, attempt_id: str) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            CANDIDATE_OBSERVE_TOOL,
            project_root,
            partial(job_operations._observe_candidate, project_root, work_root, attempt_id),
        )

    @server.tool(
        name=CANDIDATE_RESTORE_TOOL,
        description="Restore exact verified accepted candidate bytes into a caller-selected exact clean checkout. Changes only source checkout; no lifecycle, authority or automatic launch.",
    )
    async def candidate_restore(
        project_root: str, work_root: str, attempt_id: str, candidate: str
    ) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            CANDIDATE_RESTORE_TOOL,
            project_root,
            partial(job_operations._candidate_restore, project_root, work_root, attempt_id, candidate),
        )

    @server.tool(
        name=REVIEW_JOB_TOOL,
        description=(
            "Publish one candidate-bound reviewer launch with exact caller-selected historical evidence. "
            "Every review leaf requires runtime:'codex' or 'claude-code' and background:<boolean>. Run separate "
            "full CLI validation before package reuse. On ready, call the exact returned native_launch.tool "
            "with exactly native_launch.arguments; do not add, remove or rewrite an argument. A rejection "
            "publishes no reviewer prompt: correct its precondition and never synthesize a substitute launch."
        ),
    )
    async def review_job(project_root: str, work_root: str, review: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            REVIEW_JOB_TOOL,
            project_root,
            partial(job_operations._review_job, project_root, work_root, review),
        )

    _install_boundary_contracts(server)
    return server


def _install_boundary_contracts(server: MCPServer) -> None:
    """Install exact schemas through the pinned SDK's mutable tool metadata seam."""
    definitions = (
        (
            ORDER_TOOL,
            contract_schemas.schema_for(contracts.OrderEnvelope),
            contract_schemas.union_schema_for(contracts.ORDER_RESULT_TYPES),
        ),
        (
            PARALLEL_PREVIEW_TOOL,
            contract_schemas.schema_for(contracts.ParallelPreviewEnvelope),
            contract_schemas.union_schema_for(contracts.PARALLEL_PREVIEW_RESULT_TYPES),
        ),
        (
            BRIEF_CONTRACT_TOOL,
            contract_schemas.schema_for(contracts.BriefContractEnvelope),
            contract_schemas.union_schema_for(contract_schemas.BRIEF_CONTRACT_RESULT_TYPES),
        ),
        (
            BRIEF_SOURCES_TOOL,
            contract_schemas.schema_for(contracts.BriefSourcesEnvelope),
            contract_schemas.union_schema_for(contract_schemas.BRIEF_SOURCES_RESULT_TYPES),
        ),
        (
            ITEM_DEFINITION_TOOL,
            contract_schemas.schema_for(contracts.ItemDefinitionEnvelope),
            contract_schemas.union_schema_for(contracts.ITEM_DEFINITION_RESULT_TYPES),
        ),
        (
            BRIEF_REVIEW_TOOL,
            contract_schemas.schema_for(contracts.BriefReviewEnvelope),
            contract_schemas.union_schema_for(contracts.BRIEF_REVIEW_RESULT_TYPES),
        ),
        (
            ITEM_STATUS_TOOL,
            contract_schemas.schema_for(contracts.ItemStatusRequest),
            contract_schemas.union_schema_for(contracts.ITEM_STATUS_RESULT_TYPES),
        ),
        (
            PROPOSAL_CREATE_TOOL,
            contract_schemas.schema_for(contracts.ProposalCreateRequest),
            contract_schemas.union_schema_for(contracts.PROPOSAL_RESULT_TYPES),
        ),
        (
            BRIEF_PUBLISH_TOOL,
            contract_schemas.schema_for(contracts.BriefPublishRequest),
            contract_schemas.union_schema_for(contracts.BRIEF_PUBLICATION_RESULT_TYPES),
        ),
        (
            OVERVIEW_TOOL,
            contract_schemas.schema_for(contracts.OverviewRequest),
            contract_schemas.union_schema_for(contracts.OVERVIEW_RESULT_TYPES),
        ),
        (
            ACTIONS_TOOL,
            contract_schemas.actions_request_schema(),
            contract_schemas.union_schema_for(contracts.ACTIONS_RESULT_TYPES),
        ),
        (
            ATTEMPT_INSPECT_TOOL,
            contract_schemas.schema_for(contracts.AttemptInspectRequest),
            contract_schemas.union_schema_for(contracts.ATTEMPT_INSPECTION_RESULT_TYPES),
        ),
        (
            ARTIFACT_VERIFY_TOOL,
            contract_schemas.schema_for(contracts.ArtifactVerifyRequest),
            contract_schemas.union_schema_for(contracts.ARTIFACT_VERIFICATION_RESULT_TYPES),
        ),
        (
            PREPARATION_AUTHORITY_TOOL,
            contract_schemas.preparation_authority_request_schema(),
            contract_schemas.union_schema_for(contracts.PREPARATION_AUTHORITY_RESULT_TYPES),
        ),
        (
            ATTEMPT_AUTHORITY_TOOL,
            contract_schemas.attempt_authority_request_schema(),
            contract_schemas.union_schema_for(contracts.ATTEMPT_AUTHORITY_RESULT_TYPES),
        ),
        (
            TRANSITION_TOOL,
            contract_schemas.transition_request_schema(),
            contract_schemas.union_schema_for(contracts.TRANSITION_RESULT_TYPES),
        ),
        (
            DISPATCH_TOOL,
            contract_schemas.schema_for(contracts.DispatchRequest),
            contract_schemas.union_schema_for(contracts.DISPATCH_RESULT_TYPES),
        ),
        (
            CANDIDATE_RESTORE_TOOL,
            contract_schemas.schema_for(contracts.CandidateRestoreRequest),
            contract_schemas.union_schema_for(contracts.CANDIDATE_RESTORE_RESULT_TYPES),
        ),
        (
            CANDIDATE_OBSERVE_TOOL,
            contract_schemas.schema_for(contracts.CandidateObserveRequest),
            contract_schemas.union_schema_for(contracts.CANDIDATE_OBSERVATION_RESULT_TYPES),
        ),
        (
            REVIEW_JOB_TOOL,
            contract_schemas.schema_for(contracts.ReviewJobRequest),
            contract_schemas.union_schema_for(contracts.REVIEW_JOB_RESULT_TYPES),
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
    executor = execution.BoundedExecutor(worker_count=2, unfinished_limit=4)
    diagnostics = execution.Diagnostics(sys.stderr, event_limit=32, line_limit=256)
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
