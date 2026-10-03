"""Register the local-stdio MCP surface and compose thematic operations."""

from __future__ import annotations

import itertools
import sys
from collections.abc import Sequence
from functools import partial
from pathlib import Path

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from pinboard import __version__
from pinboard.application import (
    proposal_models,
    work_brief_models,
)
from pinboard.mcp import (
    common,
    contract_schemas,
    contracts,
    execution,
    job_operations,
    mutation_operations,
    pr_review_operations,
    read_operations,
)
from pinboard.mcp.contracts import JsonValue
from pinboard.mcp.tool_names import (
    ACTIONS_TOOL,
    ARTIFACT_VERIFY_TOOL,
    ATTEMPT_AUTHORITY_TOOL,
    ATTEMPT_INSPECT_TOOL,
    BRIEF_CONTRACT_TOOL,
    BRIEF_PUBLISH_TOOL,
    BRIEF_REVIEW_TOOL,
    BRIEF_SOURCE_PLAN_OUTPUT_TOOL,
    BRIEF_SOURCES_TOOL,
    CANDIDATE_OBSERVE_TOOL,
    CANDIDATE_RESTORE_TOOL,
    CLOSE_TOOL,
    CORRECTION_CONTEXT_TOOL,
    DISPATCH_TOOL,
    ITEM_DEFINITION_TOOL,
    ITEM_STATUS_TOOL,
    ORDER_TOOL,
    OVERVIEW_TOOL,
    PARALLEL_PREVIEW_TOOL,
    PR_REVIEW_TOOL,
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
READ_ONLY_ANNOTATIONS: ToolAnnotations = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


def create_server(  # noqa: C901 - explicit installed SDK tool registration
    executor: execution.BoundedExecutor,
    diagnostics: execution.Diagnostics,
    capture: execution.SemanticCapture | execution.AutomaticCapture | None = None,
) -> MCPServer:
    server = MCPServer(
        "pinboard",
        version=__version__,
        log_level="ERROR",
        instructions=(
            "Pinboard coordinates local repository work: intake, canonical briefs, status, legal actions, "
            "own leases, dispatch, independent review and recovery.\n\n"
            "A project's saved work lives in its board at <project_root>/.pinboard. Before answering about or acting "
            "on saved work, including quick read-only status and planning, load the complete pinboard main skill "
            "through this runtime's native skill loader, or read its actual resolved SKILL.md completely if no "
            "loader is available. Then load only its reference for the requested phase; status uses status and "
            "selection. Read pinboard_overview; if no board exists, say that nothing was saved and offer "
            "Pinboard setup instead of claiming to remember. Before reading or editing source for a saved item, load "
            "the pinboard skill and follow its start, pause and review route; do not implement or commit a saved "
            "item outside it unless the human chooses that. For a paused item, read pinboard_item_status operation "
            "item and bring its pause_reason decision to the human before resuming. Call board work done only when "
            "the board records it complete; a merge is not completion or review. Close only an unstarted item, only "
            "through pinboard_close, and only with the human's explicit decision in their own words as "
            "human_decision. An item with an attempt cannot be closed: it completes only after a reviewer "
            "commissioned through pinboard_review_job has its ready verdict recorded, so explain that and offer that "
            "review. Never pause or block work to approximate a refused or denied close. "
            "After your own "
            "source change for an item, do not close it unless the human explicitly asked you to close it, and do "
            "not complete it until pinboard_review_job has commissioned a separate reviewer whose ready verdict is "
            "recorded: report the change and ask first. Reach board state only "
            "through these tools, never through Bash, Python or .pinboard/state.sqlite3.\n\n"
            "Before current-attempt review claims, read pinboard_item_status operation item and its review_verdict. "
            "That verdict selects the nonterminal attempt; terminal none and overview omission do not deny "
            "historical review. For historical candidate or merged-commit coverage, verify selected available "
            "evidence for that exact subject and time; retain verified coverage and qualify unavailable or "
            "conflicting coverage. An informal favorable note cannot authorize ready recording or completion. "
            "Preserve the effect of explicit human retention or deferral: product absence does not reopen it. "
            "Distinguish unresolved landing from retained work and merged-unreviewed work from implementation "
            "in flight. Before any worker claim, read "
            "pinboard_attempt_authority operation status for the exact attempt. Overview's preparation is a "
            "different authority; its release or revocation never proves worker release or revocation. A rejected "
            "or malformed read provides no observation: keep unavailable facts unknown. Every separate decision "
            "asked of the human carries its own recommendation or default beside that question.\n\n"
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
            "substitutes. Automatic trace preflight may return TRACE_PREFLIGHT_FAILED before the target callback; "
            "read its exact resource, repair, target_ran and auxiliary-effect fields before retrying."
        ),
    )
    request_ids = itertools.count(1)

    # Async SDK callbacks let cancellation reach _run_request and its queued or running executor checkpoints.
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
            arguments={"request": request},
            capture=capture,
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
            arguments={"request": request},
            capture=capture,
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
            arguments={"request": request},
            capture=capture,
        )

    @server.tool(
        name=BRIEF_SOURCES_TOOL,
        description="Read a selected-checkout source plan or one complete bounded verified inline/saved-plan batch; never writes output or work state.",
        annotations=READ_ONLY_ANNOTATIONS,
    )
    async def source_preparation(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            BRIEF_SOURCES_TOOL,
            str(request.get("project_root", "")),
            partial(read_operations._brief_sources, {"request": request}),
            arguments={"request": request},
            capture=None,
        )

    @server.tool(
        name=BRIEF_SOURCE_PLAN_OUTPUT_TOOL,
        description="Publish one immutable source plan to an explicit destination; writes only that destination.",
        annotations=LOCAL_AUTHORITY_ANNOTATIONS,
    )
    async def source_plan_output(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            BRIEF_SOURCE_PLAN_OUTPUT_TOOL,
            str(request.get("project_root", "")),
            partial(read_operations._brief_source_plan_output, {"request": request}),
            arguments={"request": request},
            capture=None,
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
            arguments={"request": request},
            capture=capture,
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
            arguments={"request": request},
            capture=capture,
        )

    @server.tool(
        name=ITEM_STATUS_TOOL,
        description=(
            "Read one current Pinboard item status, map an exact branch to the items and attempts that own it, "
            "or check whether an item's reviewed change is present by content in a named local Git target. "
            "Use operation item and its review_verdict for current-attempt review claims; terminal none and overview "
            "omission do not deny historical exact coverage. Qualify unavailable or conflicting historical evidence."
        ),
    )
    async def item_status(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            ITEM_STATUS_TOOL,
            str(request.get("project_root", "")),
            partial(read_operations._read_item_status, {"request": request}),
            arguments={"request": request},
            capture=capture,
        )

    @server.tool(
        name=PR_REVIEW_TOOL,
        description="Discover and record a human-owned PR review brief, observed heads, exact-head rounds, and human-directed closure without an implementation attempt.",
    )
    async def pr_review(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            PR_REVIEW_TOOL,
            str(request.get("project_root", "")),
            partial(pr_review_operations.execute, {"request": request}),
            arguments={"request": request},
            capture=capture,
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
            arguments={
                "project_root": project_root,
                "work_root": work_root,
                "proposal": proposal,
                "actor_task_id": actor_task_id,
                "actor_host_id": actor_host_id,
            },
            capture=capture,
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
            arguments={"project_root": project_root, "work_root": work_root, "brief": brief},
            capture=capture,
        )

    @server.tool(
        name=OVERVIEW_TOOL,
        description=(
            "Read the current authoritative Pinboard work overview without changing durable state. "
            "Call it before answering or acting on this project's saved work: status, what to do next, "
            "priority, paused, merged or done work, or starting, continuing or closing a saved item. "
            "work_root is <project_root>/.pinboard unless the user named another board. "
            "Files under .pinboard/views are generated copies, not authority. "
            "Before answering, including quick read-only status, load the complete pinboard main skill through "
            "the native loader or read its actual resolved SKILL.md completely, then its status-and-selection "
            "reference. Overview omits review_verdict and its preparation is not worker authority: read "
            "pinboard_item_status operation item for current-attempt review claims; terminal none is not historical "
            "denial. Verify selected historical coverage when needed, qualify unknowns and preserve settled "
            "human retention decisions. Read pinboard_attempt_authority operation "
            "status for worker claims. Use pinboard-intake to save new work."
        ),
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
            arguments={"project_root": project_root, "work_root": work_root},
            capture=capture,
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
            arguments={"request": request},
            capture=capture,
        )

    @server.tool(
        name=ATTEMPT_INSPECT_TOOL,
        description=(
            "Inspect one exact Pinboard attempt, its accepted brief, evidence references, and continuation. "
            "Supply reconciliation:null for ordinary inspection or exact outer-owned repository observations "
            "for one resumed reviewed-work continuation."
        ),
    )
    async def attempt_inspect(
        project_root: str,
        work_root: str,
        attempt_id: str,
        reconciliation: dict[str, JsonValue] | None,
    ) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            ATTEMPT_INSPECT_TOOL,
            project_root,
            partial(read_operations._read_attempt_inspection, project_root, work_root, attempt_id, reconciliation),
            arguments={
                "project_root": project_root,
                "work_root": work_root,
                "attempt_id": attempt_id,
                "reconciliation": reconciliation,
            },
            capture=capture,
        )

    @server.tool(
        name=CORRECTION_CONTEXT_TOOL,
        description="Read the exact effective correction brief, accepted starting snapshot, and advisory coverage-reuse eligibility and blockers for a current returned candidate; dispatch rechecks every fact and this read changes no state.",
    )
    async def correction_context(
        project_root: str,
        work_root: str,
        attempt_id: str,
        correction_history_id: IntegerBoundaryValue,
    ) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            CORRECTION_CONTEXT_TOOL,
            project_root,
            partial(
                read_operations._read_correction_context,
                project_root,
                work_root,
                attempt_id,
                correction_history_id,
            ),
            arguments={
                "project_root": project_root,
                "work_root": work_root,
                "attempt_id": attempt_id,
                "correction_history_id": correction_history_id,
            },
            capture=capture,
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
            arguments={
                "project_root": project_root,
                "work_root": work_root,
                "artifact_ref_id": artifact_ref_id,
                "selector": selector,
                "sha256": sha256,
                "size_bytes": size_bytes,
            },
            capture=capture,
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
            arguments={"request": request},
            capture=capture,
        )

    @server.tool(
        name=ATTEMPT_AUTHORITY_TOOL,
        description="Read or change one exact Pinboard attempt authority. Use operation status before worker claims; overview preparation release or revocation says nothing about this worker authority.",
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
            arguments={"request": request},
            capture=capture,
        )

    @server.tool(
        name=TRANSITION_TOOL,
        description="Apply one current Pinboard lifecycle action other than close. Put project_root, work_root, role, receipt, payload and authority fields inside request. receipt contains ONLY action_id and subject_revision from pinboard_actions, not the whole action. For role project, put actor_task_id and actor_host_id in request; for role worker or preparer, put lease_id and generation there instead. Get the action-specific payload schema from pinboard_actions. Close is not accepted here: apply it only through pinboard_close with the human's own decision. Complete only after pinboard_review_job commissioned the reviewer and record-ready recorded its ready verdict, naming that reviewer_task_id.",
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
            arguments={"request": request},
            capture=capture,
        )

    @server.tool(
        name=CLOSE_TOOL,
        description=(
            "Close one unstarted Pinboard item as done or dropped on the human's explicit decision. Never close in "
            "the same turn as your own source change for that item unless the human explicitly asked you to close "
            "it; otherwise report the change and ask the human first. Claude Code v2.1.199 or later asks the human "
            "to confirm every call; Codex and earlier Claude Code versions may not ask. Arguments are project_root, work_root, receipt, payload, actor_task_id "
            "and actor_host_id (no request wrapper, no role). receipt contains ONLY action_id:{kind:'close',"
            "subject:<item_id>} and subject_revision from the close action pinboard_actions returned. payload is "
            "{outcome:'done'|'dropped', reason:<one line>, human_decision:<the human's explicit close decision in "
            "their own words, one line>}. If you do not have the human's own words, ask the human and stop; never "
            "write them yourself. If this call is denied, report that the human must confirm the close and seek no "
            "other route: no shell or CLI close, pause, block, defer or other transition."
        ),
        meta={"anthropic/requiresUserInteraction": True},
    )
    async def close(
        project_root: str,
        work_root: str,
        receipt: dict[str, JsonValue],
        payload: dict[str, JsonValue],
        actor_task_id: str,
        actor_host_id: str,
    ) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            CLOSE_TOOL,
            project_root,
            partial(
                mutation_operations._close, project_root, work_root, receipt, payload, actor_task_id, actor_host_id
            ),
            arguments={
                "project_root": project_root,
                "work_root": work_root,
                "receipt": receipt,
                "payload": payload,
                "actor_task_id": actor_task_id,
                "actor_host_id": actor_host_id,
            },
            capture=capture,
        )

    @server.tool(
        name=DISPATCH_TOOL,
        description=(
            "Publish verified Pinboard worker launch instructions; creates no worker or worker authority. "
            "Arguments are project_root, work_root and dispatch (no request wrapper). Unknown fields reject.\n"
            "All five dispatch leaves require kind, receipt, checkpoint_id, environment and prompt. "
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
            "return-for-correction/v1 receipt>, and brief_review:<CorrectionSourceReview>, not an initial review. "
            "kind:'reuse-correction' takes the same correction fields with brief_review:<ReusedCoverageCorrectionReview> "
            "only when the accepted brief, every reviewed source, and accepted ready review are unchanged. "
            "kind:'local-correction' takes the same correction fields with "
            "brief_review:<LocalCorrectionSourceReview> for a local checkpoint.\n"
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
            "ReusedCoverageCorrectionReview requires schema:'pinboard-correction-source-review/v2', "
            "accepted_brief_sha256:<exact accepted brief digest>, reviewer_task_id:<independent task>, "
            "starting_candidate:<exact accepted candidate identity>, correction_input:{reason:<exact selected "
            "correction reason>}, and assessment:<fresh independent candidate-bound assessment>. "
            "Changed or missing ready coverage requires kind:'correction' with a complete new contract review.\n"
            "LocalCorrectionSourceReview requires schema:'pinboard-local-correction-source-review/v1', "
            "accepted_brief_sha256:<exact accepted brief digest>, reviewer_task_id:<independent task>, "
            "starting_candidate:<exact accepted candidate identity>, "
            "correction_input:{reason:<exact selected correction reason>}, and assessment:<independent assessment>.\n"
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
            arguments={"project_root": project_root, "work_root": work_root, "dispatch": dispatch},
            capture=capture,
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
            arguments={"project_root": project_root, "work_root": work_root, "attempt_id": attempt_id},
            capture=capture,
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
            arguments={
                "project_root": project_root,
                "work_root": work_root,
                "attempt_id": attempt_id,
                "candidate": candidate,
            },
            capture=capture,
        )

    @server.tool(
        name=REVIEW_JOB_TOOL,
        description=(
            "Publish one candidate-bound reviewer launch with exact caller-selected historical evidence, or record "
            "one exact favorable candidate review with kind:'record-ready'. Launch leaves require runtime:'codex' "
            "or 'claude-code' and background:<boolean>; record-ready takes neither and returns no native launch. "
            "record-ready requires reviewer_prompt_sha256: the prompt_reference.sha256 that the launch for this exact "
            "candidate and result returned, with the reviewer_task_id of the reviewer launched from it. Completion "
            "requires that recorded review and the same reviewer_task_id. Run separate "
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
            arguments={"project_root": project_root, "work_root": work_root, "review": review},
            capture=capture,
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
            BRIEF_SOURCE_PLAN_OUTPUT_TOOL,
            contract_schemas.schema_for(contracts.BriefSourcePlanOutputEnvelope),
            contract_schemas.union_schema_for(contract_schemas.BRIEF_SOURCE_PLAN_OUTPUT_RESULT_TYPES),
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
            contract_schemas.schema_for(contracts.ItemStatusEnvelope),
            contract_schemas.union_schema_for(contracts.ITEM_STATUS_RESULT_TYPES),
        ),
        (
            PR_REVIEW_TOOL,
            contract_schemas.schema_for(contracts.PrReviewEnvelope),
            contract_schemas.union_schema_for(contracts.PR_REVIEW_RESULT_TYPES),
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
            CORRECTION_CONTEXT_TOOL,
            contract_schemas.schema_for(contracts.CorrectionContextRequest),
            contract_schemas.union_schema_for(contracts.CORRECTION_CONTEXT_RESULT_TYPES),
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
            CLOSE_TOOL,
            contract_schemas.schema_for(contracts.CloseRequest),
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
        contract_schemas.project_regex_patterns(input_schema)
        contract_schemas.project_regex_patterns(output_schema)
        tool.parameters = input_schema
        tool.fn_metadata.output_schema = output_schema
        tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
        tool.fn_metadata.arg_model.model_rebuild(force=True)


def _capture_from_arguments(arguments: Sequence[str]) -> execution.SemanticCapture | None:
    if not arguments:
        return None
    if len(arguments) != 3 or arguments[0] != "--capture-evidence-dir" or arguments[2] != "--safe-to-persist-exactly":
        raise ValueError("MCP capture requires --capture-evidence-dir <existing-directory> --safe-to-persist-exactly.")
    return execution.SemanticCapture(Path(arguments[1]))


def main() -> None:
    try:
        capture: execution.SemanticCapture | execution.AutomaticCapture | None = _capture_from_arguments(
            tuple(sys.argv[1:])
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(64) from error
    if capture is None:
        capture = execution.AutomaticCapture(common.select_capture_item)
    executor = execution.BoundedExecutor(worker_count=2, unfinished_limit=4)
    diagnostics = execution.Diagnostics(sys.stderr, event_limit=32, line_limit=512)
    diagnostics.emit(
        event=execution.TraceEvent.STARTUP,
        request_id=None,
        operation=None,
        project_id=None,
        duration_ms=None,
        classification="ready",
        commit_reference=None,
        capture_selector=None,
    )
    try:
        anyio.run(create_server(executor, diagnostics, capture).run_stdio_async)
    finally:
        executor.shutdown()
