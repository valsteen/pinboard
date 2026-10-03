from dataclasses import dataclass
from enum import Enum

type FailureFactValue = str | int | bool | None


class RetryDisposition(Enum):
    CORRECT_INPUT = "correct-input"
    REFRESH_ACTION = "refresh-action"
    REACQUIRE_AUTHORITY = "reacquire-authority"
    RETRY_SAME_INPUT = "retry-same-input"
    DO_NOT_RETRY = "do-not-retry"


class EffectDisposition(Enum):
    UNCHANGED = "unchanged"
    COMMITTED = "committed"


class ChangedSurface(Enum):
    IMMUTABLE_ARTIFACT = "immutable-artifact"
    ACCEPTED_ARTIFACT_REFERENCE = "accepted-artifact-reference"
    LEDGER = "ledger"
    REPOSITORY_GIT_EXCLUDE = "repository-git-exclude"
    WORK_ROOT = "work-root"
    COMPATIBILITY_ALIAS = "compatibility-alias"
    MIGRATION_EVIDENCE = "migration-evidence"
    SELECTED_OUTPUT = "selected-output"
    SOURCE_CHECKOUT = "source-checkout"


class DescribedCode(str, Enum):  # noqa: UP042 - stable code vocabularies do not use StrEnum
    """A stable code with its catalog meaning at the defining vocabulary."""

    meaning: str

    def __new__(cls, value: str, meaning: str) -> DescribedCode:
        member = str.__new__(cls, value)
        member._value_ = value
        member.meaning = meaning
        return member


@dataclass(frozen=True, slots=True)
class CodeMeanings:
    """Descriptions indexed by alternatives in one result code Literal."""

    entries: tuple[tuple[int, str], ...]


@dataclass(frozen=True, slots=True)
class FailureFact:
    field: str
    value: FailureFactValue


@dataclass(frozen=True, slots=True)
class FailureMismatch:
    field: str
    expected: FailureFactValue
    observed: FailureFactValue


@dataclass(frozen=True, slots=True)
class FailureAction:
    action_id: str
    role: str
    subject_revision: str
    authorization: str | None
    lease_id: str | None
    generation: int | None


@dataclass(frozen=True, slots=True)
class FailureDetails:
    observed: tuple[FailureFact, ...]
    mismatches: tuple[FailureMismatch, ...]
    retry: RetryDisposition
    effect: EffectDisposition
    changed_surfaces: tuple[ChangedSurface, ...]
    alternatives: tuple[FailureAction, ...]


class DecisionFailureCode(DescribedCode):
    ACTION_NOT_AVAILABLE = (
        "ACTION_NOT_AVAILABLE",
        "The requested lifecycle action is unavailable for the current state or authority.",
    )
    ACTION_NOT_MUTATING = (
        "ACTION_NOT_MUTATING",
        "The requested lifecycle action is advisory and cannot perform a state transition.",
    )
    ATTEMPT_AUTHORITY_REQUIRED = (
        "ATTEMPT_AUTHORITY_REQUIRED",
        "This operation requires current attempt authority held by the caller.",
    )
    ATTEMPT_LEASE_REQUIRED = (
        "ATTEMPT_LEASE_REQUIRED",
        "The worker request lacks the current attempt lease and generation.",
    )
    ATTEMPT_LEASE_EXPIRED = ("ATTEMPT_LEASE_EXPIRED", "The supplied worker lease has passed its recorded expiry.")
    ATTEMPT_NOT_FOUND = ("ATTEMPT_NOT_FOUND", "The implementation attempt has no record at the requested identity.")
    CANDIDATE_REVIEW_REQUIRED = (
        "CANDIDATE_REVIEW_REQUIRED",
        "The selected candidate lacks the exact independent review required for this transition.",
    )
    DEPENDENCY_NOT_SATISFIED = (
        "DEPENDENCY_NOT_SATISFIED",
        "An accepted prerequisite item has not reached its required state.",
    )
    HISTORY_RECORD_EXISTS = (
        "HISTORY_RECORD_EXISTS",
        "A history record already occupies the identity reserved for this transition.",
    )
    ITEM_ALREADY_EXISTS = ("ITEM_ALREADY_EXISTS", "The work item already has a record at the selected identity.")
    ITEM_DEFINITION_INVALID = (
        "ITEM_DEFINITION_INVALID",
        "The accepted item definition failed validation for this operation.",
    )
    ITEM_DEFINITION_STALE = (
        "ITEM_DEFINITION_STALE",
        "The accepted item definition no longer matches the current recorded revision.",
    )
    ITEM_DEFINITION_LIFECYCLE_INVALID = (
        "ITEM_DEFINITION_LIFECYCLE_INVALID",
        "A terminal work item cannot have its definition revised.",
    )
    ITEM_DEPENDENCY_CYCLE = ("ITEM_DEPENDENCY_CYCLE", "The proposed item dependencies would form a cycle.")
    ITEM_NOT_FOUND = ("ITEM_NOT_FOUND", "The work item has no record at the requested identity.")
    ITEM_STATUS_INCONSISTENT = (
        "ITEM_STATUS_INCONSISTENT",
        "The item's current ledger facts disagree across the status read.",
    )
    LIVE_DEPENDENTS = ("LIVE_DEPENDENTS", "Other live items still depend on the selected item.")
    LEASE_FENCED = ("LEASE_FENCED", "The supplied lease belongs to a superseded authority generation.")
    PROPOSAL_NOT_FOUND = ("PROPOSAL_NOT_FOUND", "The intake proposal has no record at the requested identity.")
    PROPOSAL_ALREADY_EXISTS = (
        "PROPOSAL_ALREADY_EXISTS",
        "The intake proposal already has a record at the selected identity.",
    )
    PROPOSAL_INVALID = ("PROPOSAL_INVALID", "The intake proposal failed validation for this operation.")
    REPLACEMENT_INVALID = ("REPLACEMENT_INVALID", "The planned replacement failed validation for this operation.")
    REPLACEMENT_STALE = (
        "REPLACEMENT_STALE",
        "The planned replacement no longer matches the current recorded revision.",
    )
    REVIEWER_PROMPT_NOT_COMMISSIONED = (
        "REVIEWER_PROMPT_NOT_COMMISSIONED",
        "The reviewer commission has no matching published reviewer prompt.",
    )
    TRANSITION_INPUT_INVALID = (
        "TRANSITION_INPUT_INVALID",
        "The lifecycle transition failed validation for this operation.",
    )
    WORK_ROOT_MIGRATION_REQUIRED = (
        "WORK_ROOT_MIGRATION_REQUIRED",
        "The project still uses the legacy work-root layout and needs the explicit migration route.",
    )
    WORK_ROOT_MIGRATION_INVALID = (
        "WORK_ROOT_MIGRATION_INVALID",
        "The selected work-root migration failed validation for this operation.",
    )
    WORK_ROOT_MIGRATION_FAILED = (
        "WORK_ROOT_MIGRATION_FAILED",
        "Work-root migration failed after zero or more reported filesystem changes.",
    )
    SCHEMA_MIGRATION_INVALID = (
        "SCHEMA_MIGRATION_INVALID",
        "The selected schema migration failed validation for this operation.",
    )
    SCHEMA_MIGRATION_FAILED = (
        "SCHEMA_MIGRATION_FAILED",
        "Schema migration failed after zero or more reported filesystem changes.",
    )


@dataclass(frozen=True, slots=True)
class DecisionFailure:
    code: DecisionFailureCode
    message: str
    details: FailureDetails | None


type DecisionResult[T] = T | DecisionFailure
