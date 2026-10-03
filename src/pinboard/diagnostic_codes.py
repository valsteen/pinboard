"""Shared MCP diagnostic declarations for exact result schemas and installed code lookup."""

from typing import Annotated, Literal

from pinboard.domain.errors import CodeMeanings, DescribedCode


class ProducerOnlyCode(DescribedCode):
    CORRECTION_CONTEXT_INVALID = (
        "CORRECTION_CONTEXT_INVALID",
        "The correction context failed validation for this operation.",
    )
    PR_REVIEW_INVALID = (
        "PR_REVIEW_INVALID",
        "The PR-review request could not be decoded or its caller identity did not match.",
    )


class TraceEvent(DescribedCode):
    STARTUP = ("startup", "The MCP server recorded its startup before handling requests.")
    RESULT = ("result", "An MCP request produced a correlated result record.")
    RESULT_VALIDATION_ERROR = (
        "result-validation-error",
        "The result failed its declared MCP output schema after execution.",
    )
    CAPTURE_UNAVAILABLE = ("capture-unavailable", "Exact invocation capture was unavailable for the request.")
    CAPTURE_COMMITTED_WITH_WARNING = (
        "capture-committed-with-warning",
        "Exact invocation capture published bytes but reported a later warning.",
    )


type ItemStatusGitCode = Literal[
    "PROJECT_GIT_CHECKOUT_UNAVAILABLE",
    "PROJECT_GIT_EXCLUDE_UNAVAILABLE",
    "PROJECT_GIT_LAYOUT_UNSUPPORTED",
    "PROJECT_GIT_ROOT_UNAVAILABLE",
]

type BriefContractRejectedCode = Annotated[
    Literal["BRIEF_CONTRACT_REQUEST_INVALID"],
    CodeMeanings(((0, "The brief-contract request could not be decoded or validated."),)),
]

type BriefSourcesRejectedCode = Annotated[
    Literal[
        "BRIEF_SOURCES_REQUEST_INVALID",
        "BRIEF_SOURCE_BATCH_NOT_FOUND",
        "BRIEF_SOURCE_LINE_TOO_LARGE",
        "BRIEF_SOURCE_MANIFEST_INVALID",
        "BRIEF_SOURCE_PLAN_INVALID",
        "BRIEF_SOURCE_SELECTOR_INVALID",
        "BRIEF_SOURCE_SELECTOR_OVERLAP",
        "BRIEF_SOURCE_NOT_UTF8",
        "BRIEF_SOURCE_CHANGED",
        "BRIEF_SOURCE_UNREADABLE",
        "DIRECTORY_CREATE_FAILED",
        "DIRECTORY_INVALID",
        "DIRECTORY_SYNC_FAILED",
        "DIRECTORY_VERIFY_FAILED",
        "FILE_ALREADY_EXISTS",
        "FILE_PUBLISH_FAILED",
        "PROJECT_GIT_CHECKOUT_UNAVAILABLE",
        "PROJECT_GIT_EXCLUDE_UNAVAILABLE",
        "PROJECT_GIT_LAYOUT_UNSUPPORTED",
        "PROJECT_GIT_ROOT_UNAVAILABLE",
    ],
    CodeMeanings(((0, "The brief-source preparation request could not be decoded or validated."),)),
]

type BriefSourcesPublishedFailureCode = Literal["DIRECTORY_SYNC_FAILED"]

type ItemStatusInvalidCode = Annotated[
    Literal["ITEM_STATUS_INVALID"],
    CodeMeanings(((0, "The item status read failed validation for this operation."),)),
]

type ItemStatusUnavailableCode = Literal["ITEM_NOT_FOUND", "ITEM_DEFINITION_INVALID"]

type ItemStatusInconsistentCode = Literal["ITEM_STATUS_INCONSISTENT"]

type BranchOwnerNotFoundCode = Annotated[
    Literal["BRANCH_OWNER_NOT_FOUND"],
    CodeMeanings(((0, "The branch ownership lookup has no record at the requested identity."),)),
]

type DamagedReceiptResultCode = Literal["TRANSITION_RECEIPT_DAMAGED"]

type IntegrationTargetUnresolvedCode = Annotated[
    Literal["INTEGRATION_TARGET_UNRESOLVED"],
    CodeMeanings(((0, "The requested integration target could not be resolved to a repository revision."),)),
]

type IntegrationCandidateUnavailableCode = Annotated[
    Literal["INTEGRATION_CANDIDATE_UNAVAILABLE"],
    CodeMeanings(
        ((0, "The candidate evidence for the integration read could not be obtained from the selected source."),)
    ),
]

type IntegrationCandidateEvidenceInvalidCode = Annotated[
    Literal["INTEGRATION_CANDIDATE_EVIDENCE_INVALID"],
    CodeMeanings(((0, "The candidate evidence for the integration read failed validation for this operation."),)),
]

type OverviewRejectedCode = Annotated[
    Literal["OVERVIEW_INVALID"],
    CodeMeanings(((0, "The board overview request failed validation for this operation."),)),
]

type ActionsInvalidCode = Annotated[
    Literal["ACTIONS_INVALID"],
    CodeMeanings(((0, "The action-discovery request failed validation for this operation."),)),
]

type ItemDefinitionRejectedCode = Annotated[
    Literal["ITEM_DEFINITION_REQUEST_INVALID", "ITEM_NOT_FOUND", "ITEM_DEFINITION_INVALID"],
    CodeMeanings(((0, "The item-definition read request could not be decoded or validated."),)),
]

type ActionUnavailableCode = Literal["ACTION_NOT_AVAILABLE"]

type AttemptLeaseRequiredCode = Literal["ATTEMPT_LEASE_REQUIRED"]

type AttemptInspectInvalidCode = Annotated[
    Literal["ATTEMPT_INSPECT_INVALID"],
    CodeMeanings(((0, "The attempt inspection request failed validation for this operation."),)),
]

type AttemptNotFoundCode = Literal["ATTEMPT_NOT_FOUND"]

type AttemptBriefInvalidCode = Annotated[
    Literal["ATTEMPT_BRIEF_INVALID"],
    CodeMeanings(((0, "The accepted brief, candidate snapshot, or related attempt evidence could not be verified."),)),
]

type ArtifactVerificationInvalidCode = Annotated[
    Literal["ARTIFACT_VERIFY_INVALID"],
    CodeMeanings(((0, "The artifact-verification request could not be decoded or validated."),)),
]

type ArtifactReferenceMismatchCode = Annotated[
    Literal["ARTIFACT_REFERENCE_MISMATCH"],
    CodeMeanings(((0, "The requested artifact identity differs from its accepted reference."),)),
]

type ArtifactBytesInvalidCode = Annotated[
    Literal["ARTIFACT_BYTES_INVALID"],
    CodeMeanings(((0, "Published artifact bytes differ from the accepted digest or size."),)),
]

type TracePreflightResultCode = Annotated[
    Literal["TRACE_PREFLIGHT_FAILED"],
    CodeMeanings(((0, "Trace capture preparation failed before the requested MCP operation ran."),)),
]

type ExecutorBusyResultCode = Annotated[
    Literal["EXECUTOR_BUSY"],
    CodeMeanings(((0, "Another Pinboard operation currently owns the executor; this request did not run."),)),
]

type OrderRejectedCode = Annotated[
    Literal["ORDER_INVALID", "ACTION_NOT_AVAILABLE", "TRANSITION_INPUT_INVALID"],
    CodeMeanings(((0, "The live priority order failed validation for this operation."),)),
]

type ParallelPreviewRejectedCode = Annotated[
    Literal["PARALLEL_PREVIEW_INVALID", "PARALLEL_SELECTION_INVALID"],
    CodeMeanings(
        (
            (0, "The parallel-work preview failed validation for this operation."),
            (1, "The selected parallel preview contains an item that is not current."),
        )
    ),
]

type ProposalRejectedCode = Literal[
    "PROPOSAL_INVALID",
    "ITEM_ALREADY_EXISTS",
    "ITEM_NOT_FOUND",
    "ACTION_NOT_AVAILABLE",
    "ITEM_DEFINITION_INVALID",
]

type ProposalDuplicateCode = Literal["PROPOSAL_ALREADY_EXISTS"]

type BriefRejectedCode = Literal["WORK_BRIEF_INVALID", "ACTION_NOT_AVAILABLE"]

type BriefArchitectureImpactRejectedCode = Literal["WORK_BRIEF_INVALID"]

type PublicationInfrastructureFailureResultCode = Annotated[
    Literal["ARTIFACT_ACCEPTANCE_FAILED", "ARTIFACT_PUBLICATION_FAILED"],
    CodeMeanings(
        (
            (0, "The published artifact could not be accepted as a ledger reference."),
            (
                1,
                "Immutable artifact publication failed; inspect changed surfaces before deciding whether any bytes were committed.",
            ),
        )
    ),
]

type PublicationInfrastructureUnchangedFailureResultCode = Literal[
    "ARTIFACT_ACCEPTANCE_FAILED", "ARTIFACT_PUBLICATION_FAILED"
]

type BriefReviewRejectedCode = Annotated[
    Literal[
        "BRIEF_REVIEW_REQUEST_INVALID",
        "WORK_BRIEF_INVALID",
        "WORK_BRIEF_NOT_CANONICAL",
        "WORK_BRIEF_REVIEW_INVALID",
        "WORK_BRIEF_REVIEW_NOT_CANONICAL",
        "WORK_BRIEF_REVIEW_NOT_INDEPENDENT",
        "WORK_BRIEF_REVIEW_STALE",
        "ACTION_NOT_AVAILABLE",
    ],
    CodeMeanings(((0, "The brief-review request could not be decoded or validated."),)),
]

type DispatchInvalidCode = Annotated[
    Literal["DISPATCH_INVALID"], CodeMeanings(((0, "The dispatch request could not be decoded or validated."),))
]

type ReviewJobInvalidCode = Annotated[
    Literal["REVIEW_JOB_INVALID"],
    CodeMeanings(((0, "The independent review job failed validation for this operation."),)),
]

type CandidateRestoreInvalidCode = Annotated[
    Literal["CANDIDATE_RESTORE_INVALID"],
    CodeMeanings(((0, "The candidate restoration request could not be decoded or validated."),)),
]

type CandidateObservationRejectedCode = Annotated[
    Literal[
        "CANDIDATE_OBSERVATION_INVALID",
        "CANDIDATE_CONTEXT_UNAVAILABLE",
        "CANDIDATE_BRANCH_MISMATCH",
        "CANDIDATE_GIT_UNAVAILABLE",
    ],
    CodeMeanings(
        (
            (0, "The candidate observation request could not be decoded or validated."),
            (1, "The candidate's required accepted-brief or checkout context is unavailable."),
            (2, "The review candidate does not match the recorded attempt or request facts."),
            (3, "Git evidence needed to identify the candidate could not be read."),
        )
    ),
]
