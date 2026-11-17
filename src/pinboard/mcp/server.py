"""Register the local-stdio MCP surface and compose thematic operations."""

from __future__ import annotations

import itertools
import sys
from collections.abc import Callable, Iterator, Sequence
from copy import deepcopy
from functools import cache, partial
from pathlib import Path
from typing import Any, TypeForm, assert_never  # noqa: TID251 - SDK middleware exposes raw protocol mappings

import anyio
import msgspec
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from pinboard import __version__
from pinboard.application import (
    proposal_models,
    work_brief_models,
)
from pinboard.mcp import (
    cancellation_operations,
    common,
    consolidation_operations,
    contract_schemas,
    contracts,
    evidence_read,
    execution,
    host_profiles,
    initiative_contracts,
    initiative_operations,
    job_operations,
    mutation_operations,
    obligation_operations,
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
    BRIEF_LOOKUP_TOOL,
    BRIEF_PUBLISH_FILE_TOOL,
    BRIEF_PUBLISH_TOOL,
    BRIEF_REVIEW_TOOL,
    BRIEF_SOURCE_PLAN_OUTPUT_TOOL,
    BRIEF_SOURCES_TOOL,
    CANDIDATE_OBSERVE_TOOL,
    CANDIDATE_RESTORE_TOOL,
    CLOSE_TOOL,
    CORRECTION_CONTEXT_TOOL,
    DISPATCH_TOOL,
    EVIDENCE_READ_TOOL,
    FIXTURE_REVIEW_TOOL,
    INITIATIVE_TOOL,
    ITEM_DEFINITION_TOOL,
    ITEM_STATUS_TOOL,
    OBLIGATION_COVERAGE_TOOL,
    ORDER_TOOL,
    OVERVIEW_TOOL,
    PARALLEL_PREVIEW_TOOL,
    PR_REVIEW_TOOL,
    PREPARATION_AUTHORITY_TOOL,
    PROPOSAL_CREATE_TOOL,
    RETURN_CONTEXT_TOOL,
    REVIEW_JOB_TOOL,
    TRANSITION_TOOL,
)
from pinboard.mcp.tool_names import (
    CANCEL_TOOL as CANCEL_TOOL,
)
from pinboard.mcp.tool_names import (
    CONSOLIDATE_TOOL as CONSOLIDATE_TOOL,
)

type IntegerBoundaryValue = bool | int | float | str | None

LOCAL_AUTHORITY_ANNOTATIONS: ToolAnnotations = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
# Full-invocation read-only hints exclude auxiliary settings and trace writes.
# Source and exact brief lookup tools bypass capture; ordinary ledger reads may use it.
READ_ONLY_ANNOTATIONS: ToolAnnotations = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

PR_REVIEW: execution.ToolContract[contracts.PrReviewEnvelope, contracts.PrReviewResult, host_profiles.HostProfile] = (
    execution.ToolContract(
        name=PR_REVIEW_TOOL,
        description="Discover and record a human-owned PR review brief, observed heads, exact-head rounds, and human-directed closure without an implementation attempt. Put all fields inside request. File leaves start-file, review-brief-file and round-file retain the inline controls and replace only brief, brief_review or round with the matching <field>_path and <field>_sha256. Exact selected bytes are verified once and decoded into the same strict record before actor, independence and freshness checks.",
        request=contracts.PrReviewEnvelope,
        results=contracts.PrReviewResult,
        operate=pr_review_operations.execute,
        meta=None,
    )
)


DISPATCH: execution.ToolContract[contracts.DispatchRequest, contracts.DispatchResult] = execution.ToolContract(
    name=DISPATCH_TOOL,
    description=(
        "Publish verified Pinboard worker launch instructions or independently assessed replacement-brief readiness; creates no worker or worker authority. pinboard_actions returns no payload_schema for dispatch; build the request from this tool's input schema, which states every leaf, required field and constant. Do not guess an input that neither the schema nor the rules below settle.\nreceipt copies ONLY action_id and subject_revision from the fresh project dispatch action returned by pinboard_actions, not the whole action. checkpoint_id is the accepted brief's stable checkpoint ID. project_root and environment.checkout must both be the source checkout the attempt's accepted brief records (for an isolated attempt, its linked worktree, not the primary checkout); environment.branch and starting_revision are the attempt's recorded branch and base, and host_id is the trusted runtime host. Use prompt:null for canonical prompt construction; a supplied prompt string must match the canonical prompt. permissions declare only access already authorized; declarations grant no runtime access.\nChoose the leaf by situation. ordinary: no new review evidence; a cross-boundary checkpoint reuses its exact accepted ready review. reviewed: a complete independent ready WorkBriefReview. correction: a CorrectionSourceReview for the selected current canonical return-for-correction/v1 receipt named by correction_history_id, not an initial review. reuse-correction: only when the accepted brief, every reviewed source and the accepted ready review are unchanged. local-correction: a local checkpoint. candidate-correction: read pinboard_return_context current-candidate selection and obtain fresh independent assessment first; its current_candidate binds that observed working-tree-state identity and the sorted exact omitted untracked names, and it permits reviewed tracked diff/path drift while preserving the current return and returned branch/HEAD/base. Historical selection supplies provenance only for replacement readiness, never current correction authority.\nEach -file leaf replaces only brief_review with brief_review_path:<exact selected file> and brief_review_sha256:<raw SHA-256> and keeps that leaf's other controls inline; candidate-correction-file's file holds the complete outer CurrentCorrectionReview. Selected bytes are verified once before exact decoding and the same independent checks.\nreview_id is a caller-chosen label, not an ID that Pinboard issues or any context read returns, for example 'correction-review-1'. It only names the preserved rejected evidence when supplied review evidence differs from evidence already accepted for the same review subject; first publication and byte-identical reuse ignore it. Retry identical evidence with the same label. Differing evidence rejects as a do-not-retry review collision, preserves the later evidence at the returned selector and changes no lifecycle; use a new label for each distinct submission.\nreplacement-readiness selects the latest returned round made historical by replacement/rebind; pinboard_return_context historical selection supplies its original receipt and starting snapshot. Independently assess the current replacement brief and current_candidate (assessed working-tree identity or clean commit). A working-tree current_candidate whose HEAD differs from the replacement brief base is refused unchanged, because submit-review would refuse it: commit it on the attempt branch so its HEAD is clean, then obtain a fresh independent assessment of that clean HEAD commit identity before a new readiness request. Success publishes proof and plain readiness only, with no worker launch, authority, lifecycle change or implementation verdict; protect the current candidate and commission its complete review next.\nWorkBriefReview: copy the three digest bindings from pinboard_brief_review status for the exact accepted brief reference, bind the current checkpoint and ordered reviewed authority set, and use an independent reviewer; coverage must match the brief's complete coverage. Needs-correction evidence is not ready evidence. Correction reviews: contract_review is the current effective WorkBriefReview, reviewer_task_id names an independent reviewer, starting_candidate is the exact accepted candidate identity (role 'candidate', kind 'evidence'), correction_input.reason is the exact selected correction reason, and assessment is fresh independent candidate-bound prose. Changed or missing ready coverage requires kind 'correction' with a complete new contract review.\nThe strict schema and decoder remain authoritative. Dispatch publication may change immutable-artifact, accepted-artifact-reference and ledger surfaces, never lifecycle or worker authority; honor returned effect/retry facts. On ready, call the exact returned native_launch.tool with exactly native_launch.arguments. Do not add, remove or rewrite an argument. prompt_reference remains independently required immutable provenance, not an alternative launch input. Missing native launch capability stops execution; publication alone is not a launch. A worker launched from other arguments is invalid: stop it and use a fresh native launch from the returned recipe."
    ),
    request=contracts.DispatchRequest,
    results=contracts.DispatchResult,
    operate=job_operations._dispatch_job,
    meta=None,
)


CANDIDATE_OBSERVE: execution.ToolContract[contracts.CandidateObserveRequest, contracts.CandidateObservationResult] = (
    execution.ToolContract(
        name=CANDIDATE_OBSERVE_TOOL,
        description="Read one attempt's actual tracked working-tree candidate identity and omitted Git-visible nonignored untracked paths. Arguments are top-level project_root, work_root and attempt_id (no request wrapper). Changes nothing; does not prepare files, freeze evidence, acquire authority, submit or decide acceptance. Prepare only intended files under separate authority, then reobserve before existing leased submission. Its submission fact says whether this identity is submittable: on a HEAD that differs from the accepted base it is not, and the fact names that full HEAD revision and the commit step to submit instead.",
        request=contracts.CandidateObserveRequest,
        results=contracts.CandidateObservationResult,
        operate=job_operations._observe_candidate,
        meta=None,
    )
)


CANDIDATE_RESTORE: execution.ToolContract[contracts.CandidateRestoreRequest, contracts.CandidateRestoreResult] = (
    execution.ToolContract(
        name=CANDIDATE_RESTORE_TOOL,
        description="Restore exact verified accepted candidate bytes into a caller-selected exact clean checkout. Arguments are top-level project_root, work_root, attempt_id and candidate (no request wrapper). Changes only source checkout; no lifecycle, authority or automatic launch.",
        request=contracts.CandidateRestoreRequest,
        results=contracts.CandidateRestoreResult,
        operate=job_operations._candidate_restore,
        meta=None,
    )
)


REVIEW_JOB: execution.ToolContract[contracts.ReviewJobRequest, contracts.ReviewJobResult] = execution.ToolContract(
    name=REVIEW_JOB_TOOL,
    description=(
        "Publish one candidate-bound reviewer launch with exact caller-selected historical evidence, or record one exact favorable candidate review with kind:'record-ready'. Arguments are top-level project_root, work_root and review (no request wrapper). Launch leaves require runtime:'codex', 'claude-code' or 'antigravity' and background:<boolean>; record-ready takes neither and returns no native launch. kind:'amendment-delta' reviews only the diff since the ready-reviewed candidate an attempt amendment captured, and is refused unchanged when no such review applies. record-ready requires reviewer_prompt_sha256: the prompt_reference.sha256 that the launch for this exact candidate and result returned, with the reviewer_task_id of the reviewer launched from it. Completion requires that recorded review and the same reviewer_task_id. Run separate full CLI validation before package reuse. On ready, call the exact returned native_launch.tool with exactly native_launch.arguments; do not add, remove or rewrite an argument. A rejection publishes no reviewer prompt: correct its precondition and never synthesize a substitute launch."
    ),
    request=contracts.ReviewJobRequest,
    results=contracts.ReviewJobResult,
    operate=job_operations._review_job,
    meta=None,
)

ORDER: execution.ToolContract[contracts.OrderEnvelope, contracts.OrderResult] = execution.ToolContract(
    name=ORDER_TOOL,
    description="Save an explicitly human-authorized complete priority permutation against the current live order; fresh overview reconciles state, not caller commitment. Put all fields inside request. Never grants launch authority.",
    request=contracts.OrderEnvelope,
    results=contracts.OrderResult,
    operate=read_operations._order,
    meta=None,
)

PROPOSAL_CREATE: execution.ToolContract[contracts.ProposalCreateRequest, contracts.ProposalResult] = (
    execution.ToolContract(
        name=PROPOSAL_CREATE_TOOL,
        description="Create one durable Pinboard proposal with one explicit human-confirmed initiative, off-roadmap or exact no-initiatives intent. Follow-up and planned-replacement inputs bind displayed related memberships but never inherit them. Arguments are top-level project_root, work_root, proposal, actor_task_id and actor_host_id (no request wrapper).\n",
        request=contracts.ProposalCreateRequest,
        results=contracts.ProposalResult,
        operate=mutation_operations._proposal_created,
        meta=None,
    )
)

BRIEF_PUBLISH: execution.ToolContract[contracts.BriefPublishRequest, contracts.BriefPublicationResult] = (
    execution.ToolContract(
        name=BRIEF_PUBLISH_TOOL,
        description="Publish one canonical Pinboard work brief and accept its artifact reference. Arguments are top-level project_root, work_root and brief (no request wrapper). Publication is not readiness review or activation. Follow the Pinboard coordination Skill for preparation.",
        request=contracts.BriefPublishRequest,
        results=contracts.BriefPublicationResult,
        operate=mutation_operations._brief_published,
        meta=None,
    )
)

BRIEF_PUBLISH_FILE: execution.ToolContract[
    contracts.BriefPublishFileRequest, contracts.BriefPublicationResult, Callable[[], mutation_operations.SelectedBrief]
] = execution.ToolContract(
    name=BRIEF_PUBLISH_FILE_TOOL,
    description="Publish one complete work brief from the exact selected file and its raw SHA-256. Arguments are top-level project_root, work_root, brief_path and brief_sha256 (no request wrapper). Verify the bytes once, decode the strict current brief, then use canonical publication. Publication is not readiness, activation or authenticated authorship. Existing inline publication remains supported.",
    request=contracts.BriefPublishFileRequest,
    results=contracts.BriefPublicationResult,
    operate=mutation_operations._brief_published_file,
    meta=None,
)

PREPARATION_AUTHORITY: execution.ToolContract[
    contracts.PreparationAuthorityEnvelope, contracts.PreparationAuthorityResult
] = execution.ToolContract(
    name=PREPARATION_AUTHORITY_TOOL,
    description="Read or change one exact Pinboard preparation authority. Put all fields inside request.",
    request=contracts.PreparationAuthorityEnvelope,
    results=contracts.PreparationAuthorityResult,
    operate=mutation_operations._preparation_authority,
    meta=None,
)

ATTEMPT_AUTHORITY: execution.ToolContract[contracts.AttemptAuthorityEnvelope, contracts.AttemptAuthorityResult] = (
    execution.ToolContract(
        name=ATTEMPT_AUTHORITY_TOOL,
        description="Read or change one exact Pinboard attempt authority. Put all fields inside request. Use operation status before worker claims; preparation release or revocation says nothing about this worker authority.",
        request=contracts.AttemptAuthorityEnvelope,
        results=contracts.AttemptAuthorityResult,
        operate=mutation_operations._attempt_authority,
        meta=None,
    )
)

TRANSITION: execution.ToolContract[
    contracts.TransitionEnvelope[contracts.TransitionRequest], contracts.TransitionResult
] = execution.ToolContract(
    name=TRANSITION_TOOL,
    description="Apply one current Pinboard lifecycle action other than close. Put project_root, work_root, role, receipt, payload and authority fields inside request. receipt contains ONLY action_id and subject_revision from pinboard_actions, not the whole action. For role project, put actor_task_id and actor_host_id in request; for role worker or preparer, put lease_id and generation there instead. Get the action-specific payload schema from pinboard_actions. Close is not accepted here: apply it only through pinboard_close with the human's own decision. Complete requires the human's exact whole-item completion words as payload.human_decision in the current reviewed input, with any stated boundary satisfied. Reuse applicable prior explicit authorization; never invent it. Complete only after pinboard_review_job commissioned the reviewer and record-ready recorded its ready verdict, naming that reviewer_task_id.",
    request=contracts.TransitionEnvelope[contracts.TransitionRequest],
    results=contracts.TransitionResult,
    operate=mutation_operations._transition,
    meta=None,
)

CLOSE: execution.ToolContract[contracts.CloseRequest, contracts.TransitionResult] = execution.ToolContract(
    name=CLOSE_TOOL,
    description="Close one unstarted Pinboard item as done, or an absorbed item with an unresolved planned replacement as superseded, on the human's explicit decision. Superseded closure requires moving live dependents to the replacement or its checkpoints first and also closes a preserved paused or blocked attempt. To abandon work, use pinboard_cancel preview and separate exact-item human confirmation. Never close in the same turn as your own source change for that item unless the human explicitly asked you to close it; otherwise report the change and ask the human first. Claude Code v2.1.199 or later asks the human to confirm every call; Codex and earlier Claude Code versions may not ask. Antigravity asks through Pinboard's PreToolUse force_ask decision, so this tool is unavailable in headless agy runs; when that hook does not run, Antigravity's own approval settings decide. Arguments are top-level project_root, work_root, receipt, payload, actor_task_id and actor_host_id (no request wrapper, no role). receipt contains ONLY action_id:{kind:'close',subject:<item_id>} and subject_revision from the close action pinboard_actions returned. payload is {outcome:'done'|'superseded', reason:<one line>, human_decision:<the human's explicit close decision in their own words, one line>}. If you do not have the human's own words, ask the human and stop; never write them yourself. If this call is denied, report that the human must confirm the close and seek no other route: no shell or CLI close, pause, block or other transition.",
    request=contracts.CloseRequest,
    results=contracts.TransitionResult,
    operate=mutation_operations._close,
    meta={"anthropic/requiresUserInteraction": True},
)

INITIATIVE: execution.ToolContract[initiative_contracts.Envelope, initiative_contracts.Result] = execution.ToolContract(
    name=INITIATIVE_TOOL,
    description="Read or revise ledger-owned initiatives, dated membership episodes, human decisions and goal assessments; explicitly mark or clear human-attributed off-roadmap episodes; read the global offline roadmap. Member-add closes active off-roadmap state atomically, while member-remove never invents it. Put all fields inside request. Initiative membership, off-roadmap state and dependency waves grant no execution authority.\n",
    request=initiative_contracts.Envelope,
    results=initiative_contracts.Result,
    operate=initiative_operations.operate,
    meta=None,
)


PARALLEL_PREVIEW: execution.ToolContract[contracts.ParallelPreviewEnvelope, contracts.ParallelPreviewResult] = (
    execution.ToolContract(
        name=PARALLEL_PREVIEW_TOOL,
        description="Read exact selected or current-only all-safe structural parallel constraints. Put all fields inside request. Does not certify readiness, acquire authority or launch native tasks.",
        request=contracts.ParallelPreviewEnvelope,
        results=contracts.ParallelPreviewResult,
        operate=read_operations._parallel_preview,
        meta=None,
    )
)


BRIEF_CONTRACT: execution.ToolContract[contracts.BriefContractEnvelope, contracts.BriefContractResult] = (
    execution.ToolContract(
        name=BRIEF_CONTRACT_TOOL,
        description="Construct the strict work-brief contract or unresolved local/cross-boundary starter; no project facts or authority. Put all fields inside request. Structural construction is not readiness review or activation. Follow the Pinboard coordination Skill for preparation.",
        request=contracts.BriefContractEnvelope,
        results=contracts.BriefContractResult,
        operate=read_operations._brief_contract,
        meta=None,
    )
)


OBLIGATION_COVERAGE: execution.ToolContract[
    contracts.ObligationCoverageEnvelope, contracts.ObligationCoverageResult
] = execution.ToolContract(
    name=OBLIGATION_COVERAGE_TOOL,
    description="Read exact current obligation coverage for a named pair (source_item, replacement_item) or an item's planned-replacement. Lists every source obligation and all exact carrying obligations or uncovered. Put all fields inside request. Does not infer equivalence or authorize ending work.",
    request=contracts.ObligationCoverageEnvelope,
    results=contracts.ObligationCoverageResult,
    operate=obligation_operations.read,
    meta=None,
)


BRIEF_SOURCES: execution.ToolContract[contracts.BriefSourcesEnvelope, contracts.BriefSourcesResult] = (
    execution.ToolContract(
        name=BRIEF_SOURCES_TOOL,
        description="Read a selected-checkout source plan or one complete bounded verified inline/saved-plan batch; never writes output or work state. Put all fields inside request.",
        request=contracts.BriefSourcesEnvelope,
        results=contracts.BriefSourcesResult,
        operate=read_operations._brief_sources,
        meta=None,
    )
)


EVIDENCE_READ: execution.ToolContract[
    contracts.EvidenceReadEnvelope, contracts.EvidenceReadResult, host_profiles.HostProfile
] = execution.ToolContract(
    name=EVIDENCE_READ_TOOL,
    description=(
        "Read one verified evidence record, an accepted artifact or a selected file, in bounded UTF-8 pages. Keep exact artifact/file identity, byte size, digest and contiguous character ranges and follow each returned continuation unchanged to null. Missing, changed or unreadable bytes reject with truthful unchanged effects and exact diagnosis. An evidence read grants no readiness, authority, permission or native launch."
    ),
    request=contracts.EvidenceReadEnvelope,
    results=contracts.EvidenceReadResult,
    operate=evidence_read.read,
    meta=None,
)


FIXTURE_REVIEW: execution.ToolContract[contracts.FixtureReviewEnvelope, contracts.FixtureReviewResult] = (
    execution.ToolContract(
        name=FIXTURE_REVIEW_TOOL,
        description=(
            "Read two exact SHA-256-bound JSON-string fixture envelopes as a decoded field-change view. Put all fields inside request. Relative paths select from project_root; returned source identities and continuations use absolute paths. Supply one repeat_text/repeat_count recipe; compact only its uniquely corresponding identical body, retaining changed frames and surrounding text. Literal source data cannot act as a marker. Pages (including exact continuation) and resource rejections fit usable max_batch_bytes (up to 16000). An unusable budget or invalid request returns its diagnosis even when that budget cannot fit it. Concatenate page text in order to read complete JSON Lines changes; character offsets preserve UTF-8. Follow continuation verbatim under request. Sources are fully reverified on every page. Whole inputs and the selected view remain materialized in memory. A projection is presentation only: full-byte verification does not establish semantic review, readiness or lifecycle authority. Never writes source, artifacts, ledger, settings or optional invocation traces."
        ),
        request=contracts.FixtureReviewEnvelope,
        results=contracts.FixtureReviewResult,
        operate=read_operations._fixture_review,
        meta=None,
    )
)


BRIEF_SOURCE_PLAN_OUTPUT: execution.ToolContract[
    contracts.BriefSourcePlanOutputEnvelope, contracts.BriefSourcePlanOutputMcpResult
] = execution.ToolContract(
    name=BRIEF_SOURCE_PLAN_OUTPUT_TOOL,
    description="Publish one immutable source plan to an explicit destination; writes only that destination. Put all fields inside request.",
    request=contracts.BriefSourcePlanOutputEnvelope,
    results=contracts.BriefSourcePlanOutputMcpResult,
    operate=read_operations._brief_source_plan_output,
    meta=None,
)


ITEM_DEFINITION: execution.ToolContract[contracts.ItemDefinitionEnvelope, contracts.ItemDefinitionResult] = (
    execution.ToolContract(
        name=ITEM_DEFINITION_TOOL,
        description="Read one full accepted Pinboard item definition or bounded descending definition history. Put all fields inside request.",
        request=contracts.ItemDefinitionEnvelope,
        results=contracts.ItemDefinitionResult,
        operate=read_operations._read_item_definition,
        meta=None,
    )
)


BRIEF_REVIEW: execution.ToolContract[contracts.BriefReviewEnvelope, contracts.BriefReviewResult] = (
    execution.ToolContract(
        name=BRIEF_REVIEW_TOOL,
        description=(
            "Publish independent needs-correction evidence, or read the verified accepted brief and latest findings. Put all fields inside request. Status takes request:{operation:'status',project_root,work_root,brief_artifact_ref_id}. Both successful status variants supply accepted_brief_sha256, checkpoint_sha256 and reviewed_authority_set_sha256 from verified accepted bytes; copy these exact initial-review bindings. Supply independent reviewer identity and judgments separately. Local checkpoints do not use brief review. Missing fields or capability stop the affected review: report the exact subject, missing input and known effects to the human/coordinator without inspecting installed implementation. Corrupt accepted bytes return ARTIFACT_BYTES_INVALID and do-not-retry. Status callback effects are unchanged; optional trace preflight/capture can write separately. Neither findings nor their absence grants readiness, lifecycle change or authority. publish-file keeps roots and brief_artifact_ref_id inline and replaces review with exact review_path and raw review_sha256, decoded as the same strict negative review."
        ),
        request=contracts.BriefReviewEnvelope,
        results=contracts.BriefReviewResult,
        operate=read_operations._brief_review,
        meta=None,
    )
)


ITEM_STATUS: execution.ToolContract[contracts.ItemStatusEnvelope, contracts.ItemStatusResult] = execution.ToolContract(
    name=ITEM_STATUS_TOOL,
    description=(
        "Read one current Pinboard item status, map an exact branch to the items and attempts that own it, or check whether an item's reviewed change is present by content in a named local Git target. Put all fields inside request. Use operation item and its review_verdict for current-attempt review claims; terminal none and overview omission do not deny historical exact coverage. Qualify unavailable or conflicting historical evidence. The item leaf's current_checkpoint names the current brief's checkpoint and its exact recorded remaining_work; it matches a checkpoint-accepted verdict's checkpoint until a rebind or revised-brief resume replaces the brief. A held item's hold gives its plain sentence and structured unblocker."
    ),
    request=contracts.ItemStatusEnvelope,
    results=contracts.ItemStatusResult,
    operate=read_operations._read_item_status,
    meta=None,
)


OVERVIEW: execution.ToolContract[contracts.OverviewRequest, contracts.OverviewResult] = execution.ToolContract(
    name=OVERVIEW_TOOL,
    description=(
        "Read one same-revision discovery page with distinct initiative, unassigned and off-roadmap counts. Use exact roots, max_items 1-25 and continuation:null to start; follow continuations through null and restart stale traversal. Partial pages prove no absence. Known work uses exact item status and definition.\n"
    ),
    request=contracts.OverviewRequest,
    results=contracts.OverviewResult,
    operate=read_operations._read_overview,
    meta={"anthropic/alwaysLoad": True},
)


ACTIONS: execution.ToolContract[contracts.ActionsEnvelope, contracts.ActionsResult, host_profiles.HostProfile] = (
    execution.ToolContract(
        name=ACTIONS_TOOL,
        description="Discover exact current legal Pinboard actions and their strict payload contracts. Put all fields inside request.",
        request=contracts.ActionsEnvelope,
        results=contracts.ActionsResult,
        operate=read_operations._read_actions,
        meta=None,
    )
)


ATTEMPT_INSPECT: execution.ToolContract[contracts.AttemptInspectRequest, contracts.AttemptInspectionResult] = (
    execution.ToolContract(
        name=ATTEMPT_INSPECT_TOOL,
        description=(
            "Inspect one exact Pinboard attempt, its accepted brief, evidence references, and continuation. Arguments are top-level project_root, work_root, attempt_id and reconciliation (no request wrapper). Supply reconciliation:null for ordinary inspection or exact outer-owned repository observations for one resumed reviewed-work continuation."
        ),
        request=contracts.AttemptInspectRequest,
        results=contracts.AttemptInspectionResult,
        operate=read_operations._read_attempt_inspection,
        meta=None,
    )
)


RETURN_CONTEXT: execution.ToolContract[contracts.ReturnContextRequest, contracts.ReturnContextResult] = (
    execution.ToolContract(
        name=RETURN_CONTEXT_TOOL,
        description="Read one exact same-attempt return with compact v2 evidence: accepted snapshot artifact reference, executable pinboard_artifact_verify arguments and typed metadata, without inline diff bytes. Arguments are top-level project_root, work_root, attempt_id and selection (no request wrapper). Verify the named file's complete raw digest and size, decode it and inspect the complete original diff through bounded output before assessment; metadata or byte verification alone is not review. Explicit selection kind historical returns provenance after rebind/rebase without current correction authority. Selection kind current-candidate requires the current active return and returned branch/HEAD, and supplies the effective correction context plus observed tracked candidate and omitted path names for fresh independent candidate-correction review. No publication, restoration, authority or lifecycle effects.",
        request=contracts.ReturnContextRequest,
        results=contracts.ReturnContextResult,
        operate=read_operations._read_return_context,
        meta=None,
    )
)


CORRECTION_CONTEXT: execution.ToolContract[contracts.CorrectionContextRequest, contracts.CorrectionContextResult] = (
    execution.ToolContract(
        name=CORRECTION_CONTEXT_TOOL,
        description="Read the exact effective correction brief and compact v2 starting evidence: accepted snapshot artifact reference, executable pinboard_artifact_verify arguments and typed metadata, without inline diff bytes. Arguments are top-level project_root, work_root, attempt_id and correction_history_id (no request wrapper). Verify the named file's complete raw digest and size, decode it and inspect the complete original diff through bounded output before assessment; metadata or byte verification alone is not review. Advisory coverage-reuse eligibility and blockers apply to a current return on its exact returned checkout; dispatch rechecks every fact and this read changes no state. It returns no review_id: correction dispatch takes a caller-chosen label described by pinboard_dispatch.",
        request=contracts.CorrectionContextRequest,
        results=contracts.CorrectionContextResult,
        operate=read_operations._read_correction_context,
        meta=None,
    )
)


BRIEF_LOOKUP: execution.ToolContract[contracts.BriefLookupRequest, contracts.BriefLookupResult] = (
    execution.ToolContract(
        name=BRIEF_LOOKUP_TOOL,
        description=(
            "Recover the verified accepted brief receipt for the original exact attempt_id and artifact_revision. Arguments are top-level project_root, work_root, attempt_id and artifact_revision (no request wrapper). Read-only, including when optional tracing is enabled; does not publish, bind or certify readiness."
        ),
        request=contracts.BriefLookupRequest,
        results=contracts.BriefLookupResult,
        operate=read_operations._lookup_brief,
        meta=None,
    )
)


ARTIFACT_VERIFY: execution.ToolContract[contracts.ArtifactVerifyRequest, contracts.ArtifactVerificationResult] = (
    execution.ToolContract(
        name=ARTIFACT_VERIFY_TOOL,
        description="Verify an exact accepted Pinboard artifact reference and its immutable bytes. Arguments are top-level project_root, work_root, artifact_ref_id, selector, sha256 and size_bytes (no request wrapper).",
        request=contracts.ArtifactVerifyRequest,
        results=contracts.ArtifactVerificationResult,
        operate=read_operations._verify_artifact,
        meta=None,
    )
)


def _client_profile(ctx: Context) -> host_profiles.HostProfile:
    try:
        client_params = ctx.request_context.session.client_params
    except ValueError:
        client_params = None
    return host_profiles.select(client_params)


async def _compact_json_text(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
    """Compact only an equivalent JSON companion, retaining the SDK's raw metadata."""
    result = await call_next(ctx)
    if ctx.method != "tools/call" or not isinstance(result, dict):
        return result
    structured = result.get("structuredContent")
    content = result.get("content")
    if not isinstance(structured, dict) or not isinstance(content, list) or len(content) != 1:
        return result
    block: Any = content[0]
    if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
        return result
    try:
        decoded = msgspec.json.decode(block["text"])
    except msgspec.DecodeError:
        return result
    # Canonical JSON comparison preserves boolean/number types, unlike Python equality.
    if msgspec.json.encode(decoded, order="sorted") != msgspec.json.encode(structured, order="sorted"):
        return result
    return result | {"content": [block | {"text": msgspec.json.encode(structured).decode()}]}


async def _single_evidence_representation(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
    """Send evidence-page results once for a host profile selecting structured-only delivery."""
    result = await call_next(ctx)
    if (
        ctx.method != "tools/call"
        or ctx.params is None
        or ctx.params.get("name") != EVIDENCE_READ_TOOL
        or not isinstance(result, dict)
        or not isinstance(result.get("structuredContent"), dict)
    ):
        return result
    match host_profiles.representation(host_profiles.select(ctx.session.client_params)):
        case host_profiles.Representation.STRUCTURED_ONLY:
            return result | {"content": []}
        case host_profiles.Representation.STRUCTURED_AND_TEXT:
            return result
        case _ as unreachable:
            assert_never(unreachable)


def _register_preview_and_confirm[Request: msgspec.Struct, Result: msgspec.Struct](
    server: MCPServer,
    contract: execution.ToolContract[Request, Result],
    executor: execution.BoundedExecutor,
    diagnostics: execution.Diagnostics,
    request_ids: Iterator[int],
    capture: execution.SemanticCapture | execution.AutomaticCapture | None,
) -> None:
    @server.tool(name=contract.name, description=contract.description)
    async def apply(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {"request": request}
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            contract.name,
            str(request.get("project_root", "")),
            partial(contract.operate, arguments),
            arguments=arguments,
            capture=None if request.get("operation") == "preview" else capture,
        )


def create_server(  # noqa: C901, PLR0915 - explicit installed SDK tool registration
    executor: execution.BoundedExecutor,
    diagnostics: execution.Diagnostics,
    capture: execution.SemanticCapture | execution.AutomaticCapture | None = None,
) -> MCPServer:
    server = MCPServer(
        "pinboard",
        version=__version__,
        log_level="ERROR",
        middleware=[_compact_json_text, _single_evidence_representation],
        instructions=(
            "Pinboard is a local process that defaults to the project's own `.pinboard` and sends nothing off-device. "
            "Use the single native Pinboard coordinator core or the verified launch's role/runtime document. Connected tools, attribution and verified bytes grant no permission or authority."
        ),
    )
    request_ids = itertools.count(1)

    # Async SDK callbacks let cancellation reach _run_request and its queued or running executor checkpoints.
    @server.tool(name=ORDER.name, description=ORDER.description)
    async def order(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            ORDER.name,
            str(request.get("project_root", "")),
            partial(ORDER.operate, {"request": request}),
            arguments={"request": request},
            capture=capture,
        )

    @server.tool(
        name=PARALLEL_PREVIEW.name,
        description=PARALLEL_PREVIEW.description,
    )
    async def parallel_preview(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            PARALLEL_PREVIEW.name,
            str(request.get("project_root", "")),
            partial(PARALLEL_PREVIEW.operate, {"request": request}),
            arguments={"request": request},
            capture=capture,
        )

    @server.tool(
        name=BRIEF_CONTRACT.name,
        description=BRIEF_CONTRACT.description,
    )
    async def brief_contract(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            BRIEF_CONTRACT.name,
            str(request.get("project_root", "")),
            partial(BRIEF_CONTRACT.operate, {"request": request}),
            arguments={"request": request},
            capture=capture,
        )

    @server.tool(
        name=OBLIGATION_COVERAGE.name,
        description=OBLIGATION_COVERAGE.description,
        annotations=READ_ONLY_ANNOTATIONS,
    )
    async def obligation_coverage(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {"request": request}
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            OBLIGATION_COVERAGE.name,
            str(request.get("project_root", "")),
            partial(OBLIGATION_COVERAGE.operate, arguments),
            arguments=arguments,
            capture=None,
        )

    @server.tool(
        name=BRIEF_SOURCES.name,
        description=BRIEF_SOURCES.description,
        annotations=READ_ONLY_ANNOTATIONS,
    )
    async def source_preparation(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            BRIEF_SOURCES.name,
            str(request.get("project_root", "")),
            partial(BRIEF_SOURCES.operate, {"request": request}),
            arguments={"request": request},
            capture=None,
        )

    @server.tool(
        name=EVIDENCE_READ.name,
        description=EVIDENCE_READ.description,
        annotations=READ_ONLY_ANNOTATIONS,
    )
    async def evidence_page_read(request: dict[str, JsonValue], ctx: Context) -> dict[str, JsonValue]:
        profile = _client_profile(ctx)
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            EVIDENCE_READ.name,
            str(request.get("project_root", "")),
            lambda token: EVIDENCE_READ.operate({"request": request}, token, profile),
            arguments={"request": request},
            capture=None,
        )

    @server.tool(
        name=FIXTURE_REVIEW.name,
        description=FIXTURE_REVIEW.description,
        annotations=READ_ONLY_ANNOTATIONS,
    )
    async def fixture_review(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            FIXTURE_REVIEW.name,
            str(request.get("project_root", "")),
            partial(FIXTURE_REVIEW.operate, {"request": request}),
            arguments={"request": request},
            capture=None,
        )

    @server.tool(
        name=BRIEF_SOURCE_PLAN_OUTPUT.name,
        description=BRIEF_SOURCE_PLAN_OUTPUT.description,
        annotations=LOCAL_AUTHORITY_ANNOTATIONS,
    )
    async def source_plan_output(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            BRIEF_SOURCE_PLAN_OUTPUT.name,
            str(request.get("project_root", "")),
            partial(BRIEF_SOURCE_PLAN_OUTPUT.operate, {"request": request}),
            arguments={"request": request},
            capture=None,
        )

    @server.tool(
        name=ITEM_DEFINITION.name,
        description=ITEM_DEFINITION.description,
    )
    async def item_definition(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            ITEM_DEFINITION.name,
            str(request.get("project_root", "")),
            partial(ITEM_DEFINITION.operate, {"request": request}),
            arguments={"request": request},
            capture=capture,
        )

    @server.tool(
        name=BRIEF_REVIEW.name,
        description=BRIEF_REVIEW.description,
    )
    async def brief_review(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            BRIEF_REVIEW.name,
            str(request.get("project_root", "")),
            partial(BRIEF_REVIEW.operate, {"request": request}),
            arguments={"request": request},
            capture=capture,
        )

    @server.tool(
        name=ITEM_STATUS.name,
        description=ITEM_STATUS.description,
    )
    async def item_status(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            ITEM_STATUS.name,
            str(request.get("project_root", "")),
            partial(ITEM_STATUS.operate, {"request": request}),
            arguments={"request": request},
            capture=capture,
        )

    @server.tool(name=PR_REVIEW.name, description=PR_REVIEW.description)
    async def pr_review(request: dict[str, JsonValue], ctx: Context) -> dict[str, JsonValue]:
        profile = _client_profile(ctx)
        arguments: dict[str, JsonValue] = {"request": request}
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            PR_REVIEW.name,
            str(request.get("project_root", "")),
            lambda token: PR_REVIEW.operate(arguments, token, profile),
            arguments=arguments,
            capture=capture,
        )

    @server.tool(
        name=PROPOSAL_CREATE.name,
        description=PROPOSAL_CREATE.description,
    )
    async def proposal_create(
        project_root: str,
        work_root: str,
        proposal: dict[str, proposal_models.ProposalJsonValue],
        actor_task_id: str,
        actor_host_id: str,
    ) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {
            "project_root": project_root,
            "work_root": work_root,
            "proposal": proposal,
            "actor_task_id": actor_task_id,
            "actor_host_id": actor_host_id,
        }
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            PROPOSAL_CREATE.name,
            project_root,
            partial(PROPOSAL_CREATE.operate, arguments),
            arguments=arguments,
            capture=capture,
        )

    @server.tool(
        name=BRIEF_PUBLISH.name,
        description=BRIEF_PUBLISH.description,
    )
    async def brief_publish(
        project_root: str,
        work_root: str,
        brief: dict[str, work_brief_models.WorkBriefJsonValue],
    ) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {"project_root": project_root, "work_root": work_root, "brief": brief}
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            BRIEF_PUBLISH.name,
            project_root,
            partial(BRIEF_PUBLISH.operate, arguments),
            arguments=arguments,
            capture=capture,
        )

    @server.tool(
        name=BRIEF_PUBLISH_FILE.name,
        description=BRIEF_PUBLISH_FILE.description,
    )
    async def brief_publish_file(
        project_root: str, work_root: str, brief_path: str, brief_sha256: str
    ) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {
            "project_root": project_root,
            "work_root": work_root,
            "brief_path": brief_path,
            "brief_sha256": brief_sha256,
        }

        @cache
        def read_brief() -> mutation_operations.SelectedBrief:
            return mutation_operations._read_brief_file(arguments)

        def select_brief_item(
            _shared_repository: Path, _work_root: str | None, _arguments: dict[str, JsonValue]
        ) -> str | None:
            brief = read_brief()
            return brief.item_id if isinstance(brief, work_brief_models.WorkBrief) else None

        selected_capture = (
            execution.AutomaticCapture(select_brief_item)
            if isinstance(capture, execution.AutomaticCapture)
            else capture
        )
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            BRIEF_PUBLISH_FILE.name,
            project_root,
            lambda token: BRIEF_PUBLISH_FILE.operate(arguments, token, read_brief),
            arguments=arguments,
            capture=selected_capture,
        )

    @server.tool(
        name=OVERVIEW.name,
        description=OVERVIEW.description,
        meta=OVERVIEW.meta,
    )
    async def overview(
        project_root: str, work_root: str, max_items: int, continuation: dict[str, JsonValue] | None
    ) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {
            "project_root": project_root,
            "work_root": work_root,
            "max_items": max_items,
            "continuation": continuation,
        }
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            OVERVIEW.name,
            project_root,
            partial(OVERVIEW.operate, arguments),
            arguments=arguments,
            capture=capture,
        )

    @server.tool(
        name=ACTIONS.name,
        description=ACTIONS.description,
    )
    async def action_discovery(
        request: dict[str, JsonValue],
        ctx: Context,
    ) -> dict[str, JsonValue]:
        profile = _client_profile(ctx)
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            ACTIONS.name,
            str(request.get("project_root", "")),
            lambda token: ACTIONS.operate({"request": request}, token, profile),
            arguments={"request": request},
            capture=capture,
        )

    @server.tool(
        name=ATTEMPT_INSPECT.name,
        description=ATTEMPT_INSPECT.description,
    )
    async def attempt_inspect(
        project_root: str,
        work_root: str,
        attempt_id: str,
        reconciliation: dict[str, JsonValue] | None,
    ) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {
            "project_root": project_root,
            "work_root": work_root,
            "attempt_id": attempt_id,
            "reconciliation": reconciliation,
        }
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            ATTEMPT_INSPECT.name,
            project_root,
            partial(ATTEMPT_INSPECT.operate, arguments),
            arguments=arguments,
            capture=capture,
        )

    @server.tool(
        name=RETURN_CONTEXT.name,
        description=RETURN_CONTEXT.description,
    )
    async def return_context(
        project_root: str, work_root: str, attempt_id: str, selection: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {
            "project_root": project_root,
            "work_root": work_root,
            "attempt_id": attempt_id,
            "selection": selection,
        }
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            RETURN_CONTEXT.name,
            project_root,
            partial(RETURN_CONTEXT.operate, arguments),
            arguments=arguments,
            capture=capture,
        )

    @server.tool(
        name=CORRECTION_CONTEXT.name,
        description=CORRECTION_CONTEXT.description,
    )
    async def correction_context(
        project_root: str,
        work_root: str,
        attempt_id: str,
        correction_history_id: IntegerBoundaryValue,
    ) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {
            "project_root": project_root,
            "work_root": work_root,
            "attempt_id": attempt_id,
            "correction_history_id": correction_history_id,
        }
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            CORRECTION_CONTEXT.name,
            project_root,
            partial(CORRECTION_CONTEXT.operate, arguments),
            arguments=arguments,
            capture=capture,
        )

    @server.tool(
        name=BRIEF_LOOKUP.name,
        description=BRIEF_LOOKUP.description,
        annotations=READ_ONLY_ANNOTATIONS,
    )
    async def brief_lookup(
        project_root: str,
        work_root: str,
        attempt_id: str,
        artifact_revision: IntegerBoundaryValue,
    ) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {
            "project_root": project_root,
            "work_root": work_root,
            "attempt_id": attempt_id,
            "artifact_revision": artifact_revision,
        }
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            BRIEF_LOOKUP.name,
            project_root,
            partial(BRIEF_LOOKUP.operate, arguments),
            arguments=arguments,
            capture=None,
        )

    @server.tool(
        name=ARTIFACT_VERIFY.name,
        description=ARTIFACT_VERIFY.description,
    )
    async def artifact_verify(
        project_root: str,
        work_root: str,
        artifact_ref_id: IntegerBoundaryValue,
        selector: str,
        sha256: str,
        size_bytes: IntegerBoundaryValue,
    ) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {
            "project_root": project_root,
            "work_root": work_root,
            "artifact_ref_id": artifact_ref_id,
            "selector": selector,
            "sha256": sha256,
            "size_bytes": size_bytes,
        }
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            ARTIFACT_VERIFY.name,
            project_root,
            partial(ARTIFACT_VERIFY.operate, arguments),
            arguments=arguments,
            capture=capture,
        )

    @server.tool(
        name=PREPARATION_AUTHORITY.name,
        description=PREPARATION_AUTHORITY.description,
        annotations=LOCAL_AUTHORITY_ANNOTATIONS,
    )
    async def preparation_authority(
        request: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            PREPARATION_AUTHORITY.name,
            str(request.get("project_root", "")),
            partial(PREPARATION_AUTHORITY.operate, {"request": request}),
            arguments={"request": request},
            capture=capture,
        )

    @server.tool(
        name=ATTEMPT_AUTHORITY.name,
        description=ATTEMPT_AUTHORITY.description,
        annotations=LOCAL_AUTHORITY_ANNOTATIONS,
    )
    async def attempt_authority(
        request: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            ATTEMPT_AUTHORITY.name,
            str(request.get("project_root", "")),
            partial(ATTEMPT_AUTHORITY.operate, {"request": request}),
            arguments={"request": request},
            capture=capture,
        )

    @server.tool(
        name=TRANSITION.name,
        description=TRANSITION.description,
    )
    async def lifecycle_transition(
        request: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            TRANSITION.name,
            str(request.get("project_root", "")),
            partial(TRANSITION.operate, {"request": request}),
            arguments={"request": request},
            capture=capture,
        )

    @server.tool(
        name=CLOSE.name,
        description=CLOSE.description,
        meta=CLOSE.meta,
    )
    async def close(
        project_root: str,
        work_root: str,
        receipt: dict[str, JsonValue],
        payload: dict[str, JsonValue],
        actor_task_id: str,
        actor_host_id: str,
    ) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {
            "project_root": project_root,
            "work_root": work_root,
            "receipt": receipt,
            "payload": payload,
            "actor_task_id": actor_task_id,
            "actor_host_id": actor_host_id,
        }
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            CLOSE.name,
            project_root,
            partial(CLOSE.operate, arguments),
            arguments=arguments,
            capture=capture,
        )

    @server.tool(name=DISPATCH.name, description=DISPATCH.description)
    async def dispatch_job(project_root: str, work_root: str, dispatch: dict[str, JsonValue]) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {"project_root": project_root, "work_root": work_root, "dispatch": dispatch}
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            DISPATCH.name,
            project_root,
            partial(DISPATCH.operate, arguments),
            arguments=arguments,
            capture=capture,
        )

    @server.tool(name=CANDIDATE_OBSERVE.name, description=CANDIDATE_OBSERVE.description)
    async def candidate_observe(project_root: str, work_root: str, attempt_id: str) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {
            "project_root": project_root,
            "work_root": work_root,
            "attempt_id": attempt_id,
        }
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            CANDIDATE_OBSERVE.name,
            project_root,
            partial(CANDIDATE_OBSERVE.operate, arguments),
            arguments=arguments,
            capture=capture,
        )

    @server.tool(name=CANDIDATE_RESTORE.name, description=CANDIDATE_RESTORE.description)
    async def candidate_restore(
        project_root: str, work_root: str, attempt_id: str, candidate: str
    ) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {
            "project_root": project_root,
            "work_root": work_root,
            "attempt_id": attempt_id,
            "candidate": candidate,
        }
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            CANDIDATE_RESTORE.name,
            project_root,
            partial(CANDIDATE_RESTORE.operate, arguments),
            arguments=arguments,
            capture=capture,
        )

    @server.tool(name=REVIEW_JOB.name, description=REVIEW_JOB.description)
    async def review_job(project_root: str, work_root: str, review: dict[str, JsonValue]) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {"project_root": project_root, "work_root": work_root, "review": review}
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            REVIEW_JOB.name,
            project_root,
            partial(REVIEW_JOB.operate, arguments),
            arguments=arguments,
            capture=capture,
        )

    _register_preview_and_confirm(
        server, consolidation_operations.CONSOLIDATE, executor, diagnostics, request_ids, capture
    )

    _register_preview_and_confirm(server, cancellation_operations.CANCEL, executor, diagnostics, request_ids, capture)

    @server.tool(
        name=INITIATIVE.name,
        description=INITIATIVE.description,
    )
    async def initiative(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {"request": request}
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            INITIATIVE.name,
            str(request.get("project_root", "")),
            partial(INITIATIVE.operate, arguments),
            arguments=arguments,
            capture=capture,
        )

    _install_boundary_contracts(server)
    return server


type SchemaProjector = Callable[[dict[str, contracts.JsonSchemaValue]], None]


@cache
def _projected_request_schema(
    request: type[msgspec.Struct],
    projector: SchemaProjector,
) -> dict[str, contracts.JsonSchemaValue]:
    schema = contract_schemas.schema_for(request)
    projector(schema)
    return schema


@cache
def _projected_actions_request_schema(
    projector: SchemaProjector,
) -> dict[str, contracts.JsonSchemaValue]:
    schema = contract_schemas.actions_request_schema()
    projector(schema)
    return schema


@cache
def _projected_transition_request_schema(
    projector: SchemaProjector,
) -> dict[str, contracts.JsonSchemaValue]:
    schema = contract_schemas.transition_request_schema()
    projector(schema)
    return schema


@cache
def _projected_dispatch_request_schema(
    request: type[contracts.DispatchRequest],
    projector: SchemaProjector,
) -> dict[str, contracts.JsonSchemaValue]:
    schema = contract_schemas.dispatch_request_schema(request)
    projector(schema)
    return schema


@cache
def _projected_result_schema(
    results: TypeForm[msgspec.Struct],
    projector: SchemaProjector,
) -> dict[str, contracts.JsonSchemaValue]:
    schema = contract_schemas.typed_result_schema(results)
    projector(schema)
    return schema


def _install_boundary_contracts(server: MCPServer) -> None:
    """Install exact schemas through the pinned SDK's mutable tool metadata seam."""
    projector = contract_schemas.project_regex_patterns
    definitions = (
        (
            INITIATIVE.name,
            _projected_request_schema(INITIATIVE.request, projector),
            _projected_result_schema(INITIATIVE.results, projector),
        ),
        (
            consolidation_operations.CONSOLIDATE.name,
            _projected_request_schema(consolidation_operations.CONSOLIDATE.request, projector),
            _projected_result_schema(consolidation_operations.CONSOLIDATE.results, projector),
        ),
        (
            cancellation_operations.CANCEL.name,
            _projected_request_schema(cancellation_operations.CANCEL.request, projector),
            _projected_result_schema(cancellation_operations.CANCEL.results, projector),
        ),
        (
            EVIDENCE_READ.name,
            _projected_request_schema(EVIDENCE_READ.request, projector),
            _projected_result_schema(EVIDENCE_READ.results, projector),
        ),
        (
            FIXTURE_REVIEW.name,
            _projected_request_schema(FIXTURE_REVIEW.request, projector),
            _projected_result_schema(FIXTURE_REVIEW.results, projector),
        ),
        (
            ORDER.name,
            _projected_request_schema(ORDER.request, projector),
            _projected_result_schema(ORDER.results, projector),
        ),
        (
            PARALLEL_PREVIEW.name,
            _projected_request_schema(PARALLEL_PREVIEW.request, projector),
            _projected_result_schema(PARALLEL_PREVIEW.results, projector),
        ),
        (
            BRIEF_CONTRACT.name,
            _projected_request_schema(BRIEF_CONTRACT.request, projector),
            _projected_result_schema(BRIEF_CONTRACT.results, projector),
        ),
        (
            BRIEF_SOURCES.name,
            _projected_request_schema(BRIEF_SOURCES.request, projector),
            _projected_result_schema(BRIEF_SOURCES.results, projector),
        ),
        (
            BRIEF_SOURCE_PLAN_OUTPUT.name,
            _projected_request_schema(BRIEF_SOURCE_PLAN_OUTPUT.request, projector),
            _projected_result_schema(BRIEF_SOURCE_PLAN_OUTPUT.results, projector),
        ),
        (
            OBLIGATION_COVERAGE.name,
            _projected_request_schema(OBLIGATION_COVERAGE.request, projector),
            _projected_result_schema(OBLIGATION_COVERAGE.results, projector),
        ),
        (
            ITEM_DEFINITION.name,
            _projected_request_schema(ITEM_DEFINITION.request, projector),
            _projected_result_schema(ITEM_DEFINITION.results, projector),
        ),
        (
            BRIEF_REVIEW.name,
            _projected_request_schema(BRIEF_REVIEW.request, projector),
            _projected_result_schema(BRIEF_REVIEW.results, projector),
        ),
        (
            ITEM_STATUS.name,
            _projected_request_schema(ITEM_STATUS.request, projector),
            _projected_result_schema(ITEM_STATUS.results, projector),
        ),
        (
            PR_REVIEW.name,
            _projected_request_schema(PR_REVIEW.request, projector),
            _projected_result_schema(PR_REVIEW.results, projector),
        ),
        (
            PROPOSAL_CREATE.name,
            _projected_request_schema(PROPOSAL_CREATE.request, projector),
            _projected_result_schema(PROPOSAL_CREATE.results, projector),
        ),
        (
            BRIEF_PUBLISH.name,
            _projected_request_schema(BRIEF_PUBLISH.request, projector),
            _projected_result_schema(BRIEF_PUBLISH.results, projector),
        ),
        (
            BRIEF_PUBLISH_FILE.name,
            _projected_request_schema(BRIEF_PUBLISH_FILE.request, projector),
            _projected_result_schema(BRIEF_PUBLISH_FILE.results, projector),
        ),
        (
            OVERVIEW.name,
            _projected_request_schema(OVERVIEW.request, projector),
            _projected_result_schema(OVERVIEW.results, projector),
        ),
        (
            ACTIONS.name,
            _projected_actions_request_schema(projector),
            _projected_result_schema(ACTIONS.results, projector),
        ),
        (
            ATTEMPT_INSPECT.name,
            _projected_request_schema(ATTEMPT_INSPECT.request, projector),
            _projected_result_schema(ATTEMPT_INSPECT.results, projector),
        ),
        (
            CORRECTION_CONTEXT.name,
            _projected_request_schema(CORRECTION_CONTEXT.request, projector),
            _projected_result_schema(CORRECTION_CONTEXT.results, projector),
        ),
        (
            RETURN_CONTEXT.name,
            _projected_request_schema(RETURN_CONTEXT.request, projector),
            _projected_result_schema(RETURN_CONTEXT.results, projector),
        ),
        (
            ARTIFACT_VERIFY.name,
            _projected_request_schema(ARTIFACT_VERIFY.request, projector),
            _projected_result_schema(ARTIFACT_VERIFY.results, projector),
        ),
        (
            BRIEF_LOOKUP.name,
            _projected_request_schema(BRIEF_LOOKUP.request, projector),
            _projected_result_schema(BRIEF_LOOKUP.results, projector),
        ),
        (
            PREPARATION_AUTHORITY.name,
            _projected_request_schema(PREPARATION_AUTHORITY.request, projector),
            _projected_result_schema(PREPARATION_AUTHORITY.results, projector),
        ),
        (
            ATTEMPT_AUTHORITY.name,
            _projected_request_schema(ATTEMPT_AUTHORITY.request, projector),
            _projected_result_schema(ATTEMPT_AUTHORITY.results, projector),
        ),
        (
            TRANSITION.name,
            _projected_transition_request_schema(projector),
            _projected_result_schema(TRANSITION.results, projector),
        ),
        (
            CLOSE.name,
            _projected_request_schema(CLOSE.request, projector),
            _projected_result_schema(CLOSE.results, projector),
        ),
        (
            DISPATCH.name,
            _projected_dispatch_request_schema(DISPATCH.request, projector),
            _projected_result_schema(DISPATCH.results, projector),
        ),
        (
            CANDIDATE_RESTORE.name,
            _projected_request_schema(CANDIDATE_RESTORE.request, projector),
            _projected_result_schema(CANDIDATE_RESTORE.results, projector),
        ),
        (
            CANDIDATE_OBSERVE.name,
            _projected_request_schema(CANDIDATE_OBSERVE.request, projector),
            _projected_result_schema(CANDIDATE_OBSERVE.results, projector),
        ),
        (
            REVIEW_JOB.name,
            _projected_request_schema(REVIEW_JOB.request, projector),
            _projected_result_schema(REVIEW_JOB.results, projector),
        ),
    )
    for name, input_schema, output_schema in definitions:
        tool = server._tool_manager.get_tool(name)
        if tool is None:
            raise RuntimeError(f"MCP tool '{name}' was not registered.")
        tool.parameters = deepcopy(input_schema)
        tool.fn_metadata.output_schema = deepcopy(output_schema)
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
