from dataclasses import dataclass
from enum import Enum

from pinboard.domain.errors import DecisionFailureCode


class CommandErrorCode(Enum):
    ACTION_ID_INVALID = "ACTION_ID_INVALID"
    PARALLEL_SELECTION_INVALID = "PARALLEL_SELECTION_INVALID"
    STALE_ACTION = "STALE_ACTION"
    WORK_STATE_INVALID = "WORK_STATE_INVALID"


type CommandFailureCode = CommandErrorCode | DecisionFailureCode


@dataclass(frozen=True, slots=True)
class CommandFailure:
    code: CommandFailureCode
    message: str

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


type CommandResult[T] = T | CommandFailure


@dataclass(frozen=True, slots=True)
class ProposalFailure:
    code: DecisionFailureCode
    message: str

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


type ProposalResult[T] = T | ProposalFailure


@dataclass(frozen=True, slots=True)
class TransitionInputFailure:
    code: DecisionFailureCode
    message: str

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


type TransitionInputResult[T] = T | TransitionInputFailure


class DispatchErrorCode(Enum):
    DISPATCH_ACTION_INVALID = "DISPATCH_ACTION_INVALID"
    DISPATCH_ACTION_UNAVAILABLE = "DISPATCH_ACTION_UNAVAILABLE"
    DISPATCH_ATTEMPT_NOT_ACTIVE = "DISPATCH_ATTEMPT_NOT_ACTIVE"
    DISPATCH_AUTHORITY_STALE = "DISPATCH_AUTHORITY_STALE"
    DISPATCH_AUTHORITY_UNREADABLE = "DISPATCH_AUTHORITY_UNREADABLE"
    DISPATCH_BASE_REVISION_MISMATCH = "DISPATCH_BASE_REVISION_MISMATCH"
    DISPATCH_BRANCH_MISMATCH = "DISPATCH_BRANCH_MISMATCH"
    DISPATCH_BRIEF_INVALID = "DISPATCH_BRIEF_INVALID"
    DISPATCH_BRIEF_MISSING = "DISPATCH_BRIEF_MISSING"
    DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID = "DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID"
    DISPATCH_BRIEF_REVIEW_COLLISION = "DISPATCH_BRIEF_REVIEW_COLLISION"
    DISPATCH_BRIEF_REVIEW_INVALID = "DISPATCH_BRIEF_REVIEW_INVALID"
    DISPATCH_BRIEF_REVIEW_MISSING = "DISPATCH_BRIEF_REVIEW_MISSING"
    DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT = "DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT"
    DISPATCH_BRIEF_REVIEW_NOT_READY = "DISPATCH_BRIEF_REVIEW_NOT_READY"
    DISPATCH_BRIEF_REVIEW_STALE = "DISPATCH_BRIEF_REVIEW_STALE"
    DISPATCH_CHECKOUT_MISSING = "DISPATCH_CHECKOUT_MISSING"
    DISPATCH_CHECKOUT_MISMATCH = "DISPATCH_CHECKOUT_MISMATCH"
    DISPATCH_CHECKPOINT_MISSING = "DISPATCH_CHECKPOINT_MISSING"
    DISPATCH_ENVIRONMENT_INVALID = "DISPATCH_ENVIRONMENT_INVALID"
    DISPATCH_ENVIRONMENT_UNREADABLE = "DISPATCH_ENVIRONMENT_UNREADABLE"
    DISPATCH_PROMPT_NOT_CANONICAL = "DISPATCH_PROMPT_NOT_CANONICAL"
    DISPATCH_PROMPT_UNREADABLE = "DISPATCH_PROMPT_UNREADABLE"
    STALE_ACTION = "STALE_ACTION"


type DispatchFailureCode = DispatchErrorCode | DecisionFailureCode


@dataclass(frozen=True, slots=True)
class DispatchFailure:
    code: DispatchFailureCode
    message: str

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


type DispatchResult[T] = T | DispatchFailure


type CliFailure = CommandFailure | ProposalFailure | DispatchFailure
type CliResult[T] = T | CliFailure


class BriefSourceErrorCode(Enum):
    BATCH_NOT_FOUND = "BRIEF_SOURCE_BATCH_NOT_FOUND"
    LINE_TOO_LARGE = "BRIEF_SOURCE_LINE_TOO_LARGE"
    MANIFEST_INVALID = "BRIEF_SOURCE_MANIFEST_INVALID"
    SELECTOR_INVALID = "BRIEF_SOURCE_SELECTOR_INVALID"
    SELECTOR_OVERLAP = "BRIEF_SOURCE_SELECTOR_OVERLAP"
    SOURCE_NOT_UTF8 = "BRIEF_SOURCE_NOT_UTF8"
    SOURCE_UNREADABLE = "BRIEF_SOURCE_UNREADABLE"


class BriefSourceError(ValueError):
    code: BriefSourceErrorCode
    message: str

    def __init__(self, code: BriefSourceErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")


class WorkBriefErrorCode(Enum):
    BRIEF_INVALID = "WORK_BRIEF_INVALID"
    BRIEF_NOT_CANONICAL = "WORK_BRIEF_NOT_CANONICAL"
    REVIEW_INVALID = "WORK_BRIEF_REVIEW_INVALID"
    REVIEW_NOT_CANONICAL = "WORK_BRIEF_REVIEW_NOT_CANONICAL"
    REVIEW_NOT_INDEPENDENT = "WORK_BRIEF_REVIEW_NOT_INDEPENDENT"
    REVIEW_NOT_READY = "WORK_BRIEF_REVIEW_NOT_READY"
    REVIEW_STALE = "WORK_BRIEF_REVIEW_STALE"


class WorkBriefError(ValueError):
    code: WorkBriefErrorCode
    message: str

    def __init__(self, code: WorkBriefErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")
