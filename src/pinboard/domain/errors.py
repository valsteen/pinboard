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


class ArtifactAcceptanceAfterPublicationError(RuntimeError):
    """Infrastructure failed after this invocation published new immutable bytes."""

    selector: str
    cause: Exception

    def __init__(self, selector: str, cause: Exception) -> None:
        self.selector = selector
        self.cause = cause
        super().__init__(str(cause))


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


class DecisionFailureCode(Enum):
    ACTION_NOT_AVAILABLE = "ACTION_NOT_AVAILABLE"
    ACTION_NOT_MUTATING = "ACTION_NOT_MUTATING"
    ATTEMPT_AUTHORITY_REQUIRED = "ATTEMPT_AUTHORITY_REQUIRED"
    ATTEMPT_LEASE_REQUIRED = "ATTEMPT_LEASE_REQUIRED"
    ATTEMPT_LEASE_EXPIRED = "ATTEMPT_LEASE_EXPIRED"
    ATTEMPT_NOT_FOUND = "ATTEMPT_NOT_FOUND"
    DEPENDENCY_NOT_SATISFIED = "DEPENDENCY_NOT_SATISFIED"
    HISTORY_RECORD_EXISTS = "HISTORY_RECORD_EXISTS"
    ITEM_ALREADY_EXISTS = "ITEM_ALREADY_EXISTS"
    ITEM_DEFINITION_INVALID = "ITEM_DEFINITION_INVALID"
    ITEM_DEFINITION_STALE = "ITEM_DEFINITION_STALE"
    ITEM_DEFINITION_LIFECYCLE_INVALID = "ITEM_DEFINITION_LIFECYCLE_INVALID"
    ITEM_DEPENDENCY_CYCLE = "ITEM_DEPENDENCY_CYCLE"
    ITEM_NOT_FOUND = "ITEM_NOT_FOUND"
    LIVE_DEPENDENTS = "LIVE_DEPENDENTS"
    LEASE_FENCED = "LEASE_FENCED"
    PROPOSAL_NOT_FOUND = "PROPOSAL_NOT_FOUND"
    PROPOSAL_ALREADY_EXISTS = "PROPOSAL_ALREADY_EXISTS"
    PROPOSAL_INVALID = "PROPOSAL_INVALID"
    REPLACEMENT_INVALID = "REPLACEMENT_INVALID"
    REPLACEMENT_STALE = "REPLACEMENT_STALE"
    TRANSITION_INPUT_INVALID = "TRANSITION_INPUT_INVALID"


@dataclass(frozen=True, slots=True)
class DecisionFailure:
    code: DecisionFailureCode
    message: str
    details: FailureDetails | None


type DecisionResult[T] = T | DecisionFailure
