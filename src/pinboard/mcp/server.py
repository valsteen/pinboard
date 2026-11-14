"""Register the local-stdio MCP surface and compose thematic operations."""

from __future__ import annotations

import itertools
import sys
from collections.abc import Callable, Iterator, Sequence
from functools import cache, partial
from pathlib import Path
from typing import Any, assert_never  # noqa: TID251 - SDK middleware exposes raw protocol mappings

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
    execution,
    host_profiles,
    initiative_contracts,
    initiative_operations,
    job_operations,
    mutation_operations,
    obligation_operations,
    pr_review_operations,
    read_operations,
    startup_text,
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
    STARTUP_TEXT_TOOL,
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
        "Publish verified Pinboard worker launch instructions or independently assessed replacement-brief readiness; creates no worker or worker authority. If compact host type metadata shows unknown, read the complete construction fields below; do not guess unspecified inputs or infer missing capability or a regression from that display. Action discovery omits payload_schema for dispatch because it has no lifecycle-transition payload. Construct its separate launch request from the fields below.\nArguments are top-level project_root, work_root and dispatch (no request wrapper). Unknown fields reject.\nThe six worker-launch leaves require kind, receipt, checkpoint_id, environment and prompt; replacement-readiness has its distinct fields below. receipt contains ONLY action_id:{kind:'dispatch',subject:<attempt_id>} and subject_revision copied from the fresh project dispatch action returned by pinboard_actions, not the whole action. checkpoint_id is the accepted brief's stable checkpoint ID. Use explicit prompt:null for canonical prompt construction. A supplied prompt string must match the canonical prompt.\nenvironment requires all ten fields: schema:'pinboard-dispatch/v2', runtime:'codex', 'claude-code' or 'antigravity', background:<boolean>, checkout:<exact source checkout>, branch:<recorded branch>, starting_revision:<accepted attempt base>, host_id:<trusted runtime host>, fresh_context:true, lease_ttl_seconds:<positive integer>, permissions:<array of already-authorized 'repository-read', 'repository-write', 'network', 'external-write' or 'live-application' declarations>. Declarations grant no runtime access.\nkind:'ordinary' has only those common fields; a cross-boundary checkpoint reuses its exact accepted ready review. kind:'reviewed' additionally requires review_id and brief_review:<complete independent ready WorkBriefReview>. kind:'correction' additionally requires review_id, correction_history_id:<positive ID of the selected current canonical return-for-correction/v1 receipt>, and brief_review:<CorrectionSourceReview>, not an initial review. kind:'reuse-correction' takes the same correction fields with brief_review:<ReusedCoverageCorrectionReview> only when the accepted brief, every reviewed source, and accepted ready review are unchanged. kind:'local-correction' takes the same correction fields with brief_review:<LocalCorrectionSourceReview> for a local checkpoint.\nFile counterparts are reviewed-file, correction-file, reuse-correction-file, local-correction-file, candidate-correction-file and replacement-readiness-file. Replace only brief_review with brief_review_path:<exact selected file> and brief_review_sha256:<raw SHA-256>; keep the corresponding leaf's controls inline, including current_candidate for replacement-readiness-file. candidate-correction-file contains the complete outer CurrentCorrectionReview, including boundary, source_review and current_candidate, for either boundary variant. Selected bytes are verified once before exact decoding and the same independent checks.\nreview_id is a caller-chosen label, not an ID that Pinboard issues or any context read returns: one path component (nonempty; no '/', CR, LF or NUL; not '.' or '..'), for example 'correction-review-1'. It only names the preserved rejected evidence when supplied review evidence differs from evidence already accepted for the same review subject; first publication and byte-identical reuse ignore it. Retry identical evidence with the same label. Differing evidence rejects as a do-not-retry review collision, preserves the later evidence at the returned selector and changes no lifecycle; use a new label for each distinct submission.\nkind:'replacement-readiness' requires ONLY kind, receipt, checkpoint_id, review_id, correction_history_id, brief_review:<full CorrectionSourceReview>, and current_candidate:<assessed working-tree identity or clean commit>. Select the latest returned round made historical by replacement/rebind; pinboard_return_context historical selection supplies its original receipt and starting snapshot. Independently assess the current replacement brief and candidate. Pass no environment or prompt. Success publishes proof and plain readiness only, with no worker launch, authority, lifecycle change or implementation verdict; protect the current candidate and commission its complete review next.\nWorkBriefReview requires schema:'pinboard-work-brief-review/v3', attempt_id, checkpoint_id, accepted_brief_sha256, checkpoint_sha256, reviewed_authority_set_sha256, reviewer_task_id, status:'complete', verdict:'ready', and nonempty coverage. Copy the three digest bindings from pinboard_brief_review status for the exact accepted brief reference. Bind the current checkpoint and ordered reviewed authority set; the reviewer must be independent. Every coverage record requires authority_id, family, owner, verdict:'covered' and counterexample_result. owner is exactly one of {disposition:'contract',contract_invariant:<text>}, {disposition:'acceptance',criterion:<positive integer>}, {disposition:'deferred',deferral_id:<ID>} or {disposition:'not-applicable',reason:<text>}, matching the brief's complete coverage. Needs-correction evidence is not ready evidence.\ncandidate-correction takes all worker-launch fields plus review_id, correction_history_id and brief_review:{schema:'pinboard-current-correction-source-review/v1',boundary:'cross-boundary' or 'local',source_review:<full CorrectionSourceReview or LocalCorrectionSourceReview>,current_candidate:{candidate:<observed working-tree-state identity>,omitted_untracked_paths:<sorted exact omitted names>}}. Read pinboard_return_context current-candidate selection and obtain fresh independent assessment first. It permits reviewed tracked diff/path drift while preserving current return and returned branch/HEAD/base; historical selection supplies provenance only for replacement readiness, never current correction authority.\nCorrectionSourceReview requires schema:'pinboard-correction-source-review/v1', contract_review:<current effective WorkBriefReview>, starting_candidate:<exact accepted candidate identity>, correction_input:{reason:<exact selected correction reason>}, and assessment:<nonempty JSON string containing independent prose assessment>. starting_candidate requires role:'candidate', kind:'evidence', key, revision:<positive integer>, selector, content_sha256 and size_bytes:<nonnegative integer>. Preserve candidate/history binding and fresh source review.\nReusedCoverageCorrectionReview requires schema:'pinboard-correction-source-review/v2', accepted_brief_sha256:<exact accepted brief digest>, reviewer_task_id:<independent task>, starting_candidate:<exact accepted candidate identity>, correction_input:{reason:<exact selected correction reason>}, and assessment:<nonempty JSON string containing fresh independent candidate-bound prose>. Changed or missing ready coverage requires kind:'correction' with a complete new contract review.\nLocalCorrectionSourceReview requires schema:'pinboard-local-correction-source-review/v1', accepted_brief_sha256:<exact accepted brief digest>, reviewer_task_id:<independent task>, starting_candidate:<exact accepted candidate identity>, correction_input:{reason:<exact selected correction reason>}, and assessment:<nonempty JSON string containing independent candidate-bound prose>.\nThe negotiated strict schema and decoder remain authoritative. Dispatch publication may change immutable-artifact, accepted-artifact-reference and ledger surfaces, never lifecycle or worker authority; honor returned effect/retry facts. On ready, call the exact returned native_launch.tool with exactly native_launch.arguments. Do not add, remove or rewrite an argument. prompt_reference remains independently required immutable provenance, not an alternative launch input. Missing native launch capability stops execution; publication alone is not a launch. A worker launched from other arguments is invalid: stop it and use a fresh native launch from the returned recipe."
    ),
    request=contracts.DispatchRequest,
    results=contracts.DispatchResult,
    operate=job_operations._dispatch_job,
    meta=None,
)


CANDIDATE_OBSERVE: execution.ToolContract[contracts.CandidateObserveRequest, contracts.CandidateObservationResult] = (
    execution.ToolContract(
        name=CANDIDATE_OBSERVE_TOOL,
        description="Read one attempt's actual tracked working-tree candidate identity and omitted Git-visible nonignored untracked paths. Arguments are top-level project_root, work_root and attempt_id (no request wrapper). Changes nothing; does not prepare files, freeze evidence, acquire authority, submit or decide acceptance. Prepare only intended files under separate authority, then reobserve before existing leased submission.",
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
        "Publish one candidate-bound reviewer launch with exact caller-selected historical evidence, or record one exact favorable candidate review with kind:'record-ready'. Arguments are top-level project_root, work_root and review (no request wrapper). Launch leaves require runtime:'codex', 'claude-code' or 'antigravity' and background:<boolean>; record-ready takes neither and returns no native launch. record-ready requires reviewer_prompt_sha256: the prompt_reference.sha256 that the launch for this exact candidate and result returned, with the reviewer_task_id of the reviewer launched from it. Completion requires that recorded review and the same reviewer_task_id. Run separate full CLI validation before package reuse. On ready, call the exact returned native_launch.tool with exactly native_launch.arguments; do not add, remove or rewrite an argument. A rejection publishes no reviewer prompt: correct its precondition and never synthesize a substitute launch."
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
        description="Create one durable Pinboard proposal from its canonical structured input. Arguments are top-level project_root, work_root, proposal, actor_task_id and actor_host_id (no request wrapper).",
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
    description="Close one unstarted Pinboard item as done, or an absorbed item with an unresolved planned replacement as superseded, on the human's explicit decision. Superseded closure requires moving live dependents to the replacement or its checkpoints first and also closes a preserved paused or blocked attempt. To abandon work, use pinboard_cancel preview and separate exact-item human confirmation. Never close in the same turn as your own source change for that item unless the human explicitly asked you to close it; otherwise report the change and ask the human first. Claude Code v2.1.199 or later asks the human to confirm every call; Codex and earlier Claude Code versions may not ask. Antigravity asks through Pinboard's PreToolUse force_ask decision, so this tool is unavailable in headless agy runs; when that hook does not run, Antigravity's own approval settings decide. Arguments are top-level project_root, work_root, receipt, payload, actor_task_id and actor_host_id (no request wrapper, no role). receipt contains ONLY action_id:{kind:'close',subject:<item_id>} and subject_revision from the close action pinboard_actions returned. payload is {outcome:'done'|'superseded', reason:<one line>, human_decision:<the human's explicit close decision in their own words, one line>}. If you do not have the human's own words, ask the human and stop; never write them yourself. If this call is denied, report that the human must confirm the close and seek no other route: no shell or CLI close, pause, block, defer or other transition.",
    request=contracts.CloseRequest,
    results=contracts.TransitionResult,
    operate=mutation_operations._close,
    meta={"anthropic/requiresUserInteraction": True},
)

INITIATIVE: execution.ToolContract[initiative_contracts.Envelope, initiative_contracts.Result] = execution.ToolContract(
    name=INITIATIVE_TOOL,
    description="Read or revise ledger-owned initiatives, dated membership episodes, human decisions and goal assessments; read the global offline roadmap. Put all fields inside request. Initiative membership and dependency waves grant no execution authority.",
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


STARTUP_TEXT: execution.ToolContract[
    contracts.StartupTextEnvelope, contracts.StartupTextResult, host_profiles.HostProfile
] = execution.ToolContract(
    name=STARTUP_TEXT_TOOL,
    description=(
        "Read one verified oversized evidence record in bounded UTF-8 pages; startup never uses this tool. Keep exact artifact/file identity, byte size, digest and contiguous character ranges and follow each returned continuation unchanged to null. Missing, changed or unreadable bytes reject with truthful unchanged effects and exact diagnosis. An evidence read grants no readiness, authority, permission or native launch."
    ),
    request=contracts.StartupTextEnvelope,
    results=contracts.StartupTextResult,
    operate=startup_text.read,
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
        "Read one current Pinboard item status, map an exact branch to the items and attempts that own it, or check whether an item's reviewed change is present by content in a named local Git target. Put all fields inside request. Use operation item and its review_verdict for current-attempt review claims; terminal none and overview omission do not deny historical exact coverage. Qualify unavailable or conflicting historical evidence. The item leaf's current_checkpoint names the current brief's checkpoint and its exact recorded remaining_work; it matches a checkpoint-accepted verdict's checkpoint until a rebind or revised-brief resume replaces the brief."
    ),
    request=contracts.ItemStatusEnvelope,
    results=contracts.ItemStatusResult,
    operate=read_operations._read_item_status,
    meta=None,
)


OVERVIEW: execution.ToolContract[contracts.OverviewRequest, contracts.OverviewResult] = execution.ToolContract(
    name=OVERVIEW_TOOL,
    description=(
        "Read one same-revision discovery page; exact top-level roots, max_items 1-25, continuation:null to start. Follow continuations through null; stale traversal restarts. Partial pages prove no absence. Known work uses exact item status and definition."
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


async def _single_startup_representation(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
    """Send startup-text results once for a host profile selecting structured-only delivery."""
    result = await call_next(ctx)
    if (
        ctx.method != "tools/call"
        or ctx.params is None
        or ctx.params.get("name") != STARTUP_TEXT_TOOL
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
        middleware=[_compact_json_text, _single_startup_representation],
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
        name=STARTUP_TEXT.name,
        description=STARTUP_TEXT.description,
        annotations=READ_ONLY_ANNOTATIONS,
    )
    async def startup_read(request: dict[str, JsonValue], ctx: Context) -> dict[str, JsonValue]:
        profile = _client_profile(ctx)
        return await execution._run_request(
            executor,
            diagnostics,
            next(request_ids),
            STARTUP_TEXT.name,
            str(request.get("project_root", "")),
            lambda token: STARTUP_TEXT.operate({"request": request}, token, profile),
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


def _install_boundary_contracts(server: MCPServer) -> None:
    """Install exact schemas through the pinned SDK's mutable tool metadata seam."""
    definitions = (
        (
            INITIATIVE.name,
            contract_schemas.schema_for(INITIATIVE.request),
            contract_schemas.typed_result_schema(INITIATIVE.results),
        ),
        (
            consolidation_operations.CONSOLIDATE.name,
            contract_schemas.schema_for(consolidation_operations.CONSOLIDATE.request),
            contract_schemas.typed_result_schema(consolidation_operations.CONSOLIDATE.results),
        ),
        (
            cancellation_operations.CANCEL.name,
            contract_schemas.schema_for(cancellation_operations.CANCEL.request),
            contract_schemas.typed_result_schema(cancellation_operations.CANCEL.results),
        ),
        (
            STARTUP_TEXT.name,
            contract_schemas.schema_for(STARTUP_TEXT.request),
            contract_schemas.typed_result_schema(STARTUP_TEXT.results),
        ),
        (
            FIXTURE_REVIEW.name,
            contract_schemas.schema_for(FIXTURE_REVIEW.request),
            contract_schemas.typed_result_schema(FIXTURE_REVIEW.results),
        ),
        (
            ORDER.name,
            contract_schemas.schema_for(ORDER.request),
            contract_schemas.typed_result_schema(ORDER.results),
        ),
        (
            PARALLEL_PREVIEW.name,
            contract_schemas.schema_for(PARALLEL_PREVIEW.request),
            contract_schemas.typed_result_schema(PARALLEL_PREVIEW.results),
        ),
        (
            BRIEF_CONTRACT.name,
            contract_schemas.schema_for(BRIEF_CONTRACT.request),
            contract_schemas.typed_result_schema(BRIEF_CONTRACT.results),
        ),
        (
            BRIEF_SOURCES.name,
            contract_schemas.schema_for(BRIEF_SOURCES.request),
            contract_schemas.typed_result_schema(BRIEF_SOURCES.results),
        ),
        (
            BRIEF_SOURCE_PLAN_OUTPUT.name,
            contract_schemas.schema_for(BRIEF_SOURCE_PLAN_OUTPUT.request),
            contract_schemas.typed_result_schema(BRIEF_SOURCE_PLAN_OUTPUT.results),
        ),
        (
            OBLIGATION_COVERAGE.name,
            contract_schemas.schema_for(OBLIGATION_COVERAGE.request),
            contract_schemas.typed_result_schema(OBLIGATION_COVERAGE.results),
        ),
        (
            ITEM_DEFINITION.name,
            contract_schemas.schema_for(ITEM_DEFINITION.request),
            contract_schemas.typed_result_schema(ITEM_DEFINITION.results),
        ),
        (
            BRIEF_REVIEW.name,
            contract_schemas.schema_for(BRIEF_REVIEW.request),
            contract_schemas.typed_result_schema(BRIEF_REVIEW.results),
        ),
        (
            ITEM_STATUS.name,
            contract_schemas.schema_for(ITEM_STATUS.request),
            contract_schemas.typed_result_schema(ITEM_STATUS.results),
        ),
        (
            PR_REVIEW.name,
            contract_schemas.schema_for(PR_REVIEW.request),
            contract_schemas.typed_result_schema(PR_REVIEW.results),
        ),
        (
            PROPOSAL_CREATE.name,
            contract_schemas.schema_for(PROPOSAL_CREATE.request),
            contract_schemas.typed_result_schema(PROPOSAL_CREATE.results),
        ),
        (
            BRIEF_PUBLISH.name,
            contract_schemas.schema_for(BRIEF_PUBLISH.request),
            contract_schemas.typed_result_schema(BRIEF_PUBLISH.results),
        ),
        (
            BRIEF_PUBLISH_FILE.name,
            contract_schemas.schema_for(BRIEF_PUBLISH_FILE.request),
            contract_schemas.typed_result_schema(BRIEF_PUBLISH_FILE.results),
        ),
        (
            OVERVIEW.name,
            contract_schemas.schema_for(OVERVIEW.request),
            contract_schemas.typed_result_schema(OVERVIEW.results),
        ),
        (
            ACTIONS.name,
            contract_schemas.actions_request_schema(),
            contract_schemas.typed_result_schema(ACTIONS.results),
        ),
        (
            ATTEMPT_INSPECT.name,
            contract_schemas.schema_for(ATTEMPT_INSPECT.request),
            contract_schemas.typed_result_schema(ATTEMPT_INSPECT.results),
        ),
        (
            CORRECTION_CONTEXT.name,
            contract_schemas.schema_for(CORRECTION_CONTEXT.request),
            contract_schemas.typed_result_schema(CORRECTION_CONTEXT.results),
        ),
        (
            RETURN_CONTEXT.name,
            contract_schemas.schema_for(RETURN_CONTEXT.request),
            contract_schemas.typed_result_schema(RETURN_CONTEXT.results),
        ),
        (
            ARTIFACT_VERIFY.name,
            contract_schemas.schema_for(ARTIFACT_VERIFY.request),
            contract_schemas.typed_result_schema(ARTIFACT_VERIFY.results),
        ),
        (
            BRIEF_LOOKUP.name,
            contract_schemas.schema_for(BRIEF_LOOKUP.request),
            contract_schemas.typed_result_schema(BRIEF_LOOKUP.results),
        ),
        (
            PREPARATION_AUTHORITY.name,
            contract_schemas.schema_for(PREPARATION_AUTHORITY.request),
            contract_schemas.typed_result_schema(PREPARATION_AUTHORITY.results),
        ),
        (
            ATTEMPT_AUTHORITY.name,
            contract_schemas.schema_for(ATTEMPT_AUTHORITY.request),
            contract_schemas.typed_result_schema(ATTEMPT_AUTHORITY.results),
        ),
        (
            TRANSITION.name,
            contract_schemas.transition_request_schema(),
            contract_schemas.typed_result_schema(TRANSITION.results),
        ),
        (
            CLOSE.name,
            contract_schemas.schema_for(CLOSE.request),
            contract_schemas.typed_result_schema(CLOSE.results),
        ),
        (
            DISPATCH.name,
            contract_schemas.dispatch_request_schema(DISPATCH.request),
            contract_schemas.typed_result_schema(DISPATCH.results),
        ),
        (
            CANDIDATE_RESTORE.name,
            contract_schemas.schema_for(CANDIDATE_RESTORE.request),
            contract_schemas.typed_result_schema(CANDIDATE_RESTORE.results),
        ),
        (
            CANDIDATE_OBSERVE.name,
            contract_schemas.schema_for(CANDIDATE_OBSERVE.request),
            contract_schemas.typed_result_schema(CANDIDATE_OBSERVE.results),
        ),
        (
            REVIEW_JOB.name,
            contract_schemas.schema_for(REVIEW_JOB.request),
            contract_schemas.typed_result_schema(REVIEW_JOB.results),
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
