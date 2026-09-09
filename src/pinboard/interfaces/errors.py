from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pinboard.adapters.files.errors import ArtifactError, FileIOError
from pinboard.adapters.sqlite.errors import SQLiteReadOnlyError, StorageError
from pinboard.domain.errors import (
    ChangedSurface,
    DecisionFailureCode,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    RetryDisposition,
)
from pinboard.interfaces import cli_commands


def storage_failure_details(
    error: StorageError,
    operation: str,
    roots: cli_commands.ResolvedRoots | None,
    effect: EffectDisposition,
    changed_surfaces: tuple[ChangedSurface, ...],
    prior_observations: tuple[FailureFact, ...],
) -> FailureDetails:
    observations = prior_observations
    if isinstance(error, SQLiteReadOnlyError):
        if roots is None:
            raise AssertionError("A SQLite read-only failure requires resolved project roots.") from error
        permission_work_root = (
            ".codex/pinboard"
            if not roots.explicit_work_root and roots.source_checkout == roots.shared_repository
            else str(roots.work)
        )
        observations = (
            *observations,
            FailureFact("database_path", str(error.database_path)),
            FailureFact("operation", operation),
            FailureFact("sqlite_error_code", error.code.value),
            FailureFact(
                "permission_recovery",
                "For routine Pinboard commands, select a Codex permission profile extending ':workspace' whose "
                f"narrow filesystem write rule grants access to '{permission_work_root}', the effective work root for "
                "this command. A normal checkout uses the relative '.codex/pinboard' rule; a linked worktree uses "
                "only the resolved absolute shared-repository '.codex/pinboard' directory; an explicit '--work-root' "
                "uses that exact directory. Remove legacy 'sandbox_mode' and 'sandbox_workspace_write' settings "
                "because they override permission profiles. For fresh default initialization, approve the exact "
                "'pinboard init' command once so it can also update '.git/info/exclude'; do not grant persistent "
                "'.git' access.",
            ),
        )
    return FailureDetails(
        observed=observations,
        mismatches=(),
        retry=(
            RetryDisposition.DO_NOT_RETRY
            if effect == EffectDisposition.COMMITTED or not error.retryable
            else RetryDisposition.RETRY_SAME_INPUT
        ),
        effect=effect,
        changed_surfaces=changed_surfaces,
        alternatives=(),
    )


@dataclass(frozen=True, slots=True)
class CommittedEffectFailure:
    """A typed failure after one or more durable surfaces were already changed."""

    code: str
    message: str
    details: FailureDetails

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class CommandErrorCode(Enum):
    ACTION_ID_INVALID = "ACTION_ID_INVALID"
    ACTION_ID_MALFORMED = "ACTION_ID_MALFORMED"
    ACTION_KIND_UNKNOWN = "ACTION_KIND_UNKNOWN"
    ACTION_REVISION_STALE = "ACTION_REVISION_STALE"
    ACTION_AUTHORITY_WRONG = "ACTION_AUTHORITY_WRONG"
    ACTION_AUTHORITY_EXPIRED = "ACTION_AUTHORITY_EXPIRED"
    ACTION_AUTHORITY_RELEASED = "ACTION_AUTHORITY_RELEASED"
    ACTION_LIFECYCLE_UNAVAILABLE = "ACTION_LIFECYCLE_UNAVAILABLE"
    PARALLEL_SELECTION_INVALID = "PARALLEL_SELECTION_INVALID"
    STALE_ACTION = "STALE_ACTION"
    WORK_STATE_INVALID = "WORK_STATE_INVALID"


type CommandFailureCode = CommandErrorCode | DecisionFailureCode


@dataclass(frozen=True, slots=True)
class CommandFailure:
    code: CommandFailureCode
    message: str
    details: FailureDetails | None

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


type CommandResult[T] = T | CommandFailure


@dataclass(frozen=True, slots=True)
class ProposalFailure:
    code: DecisionFailureCode
    message: str
    details: FailureDetails | None

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


type ProposalResult[T] = T | ProposalFailure


@dataclass(frozen=True, slots=True)
class TransitionInputFailure:
    code: DecisionFailureCode
    message: str
    details: FailureDetails | None

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
    details: FailureDetails | None

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


type DispatchResult[T] = T | DispatchFailure


class BriefSourceErrorCode(Enum):
    BATCH_NOT_FOUND = "BRIEF_SOURCE_BATCH_NOT_FOUND"
    LINE_TOO_LARGE = "BRIEF_SOURCE_LINE_TOO_LARGE"
    MANIFEST_INVALID = "BRIEF_SOURCE_MANIFEST_INVALID"
    PLAN_INVALID = "BRIEF_SOURCE_PLAN_INVALID"
    SELECTOR_INVALID = "BRIEF_SOURCE_SELECTOR_INVALID"
    SELECTOR_OVERLAP = "BRIEF_SOURCE_SELECTOR_OVERLAP"
    SOURCE_NOT_UTF8 = "BRIEF_SOURCE_NOT_UTF8"
    SOURCE_CHANGED = "BRIEF_SOURCE_CHANGED"
    SOURCE_UNREADABLE = "BRIEF_SOURCE_UNREADABLE"


@dataclass(frozen=True, slots=True)
class BriefSourceFailure:
    code: BriefSourceErrorCode
    message: str

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


type BriefSourceResult[T] = T | BriefSourceFailure


class WorkBriefErrorCode(Enum):
    BRIEF_INVALID = "WORK_BRIEF_INVALID"
    BRIEF_NOT_CANONICAL = "WORK_BRIEF_NOT_CANONICAL"
    REVIEW_INVALID = "WORK_BRIEF_REVIEW_INVALID"
    REVIEW_NOT_CANONICAL = "WORK_BRIEF_REVIEW_NOT_CANONICAL"
    REVIEW_NOT_INDEPENDENT = "WORK_BRIEF_REVIEW_NOT_INDEPENDENT"
    REVIEW_NOT_READY = "WORK_BRIEF_REVIEW_NOT_READY"
    REVIEW_STALE = "WORK_BRIEF_REVIEW_STALE"


@dataclass(frozen=True, slots=True)
class WorkBriefFailure:
    code: WorkBriefErrorCode
    message: str

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


type WorkBriefResult[T] = T | WorkBriefFailure
type InitializationFailure = StorageError | ArtifactError | FileIOError | WorkBriefFailure


class InitializationAfterCommittedEffectsError(RuntimeError):
    """Initialization failed after this invocation durably changed named surfaces."""

    git_exclude_path: Path | None
    database_path: Path | None
    cause: InitializationFailure

    def __init__(
        self,
        git_exclude_path: Path | None,
        database_path: Path | None,
        cause: InitializationFailure,
    ) -> None:
        self.git_exclude_path = git_exclude_path
        self.database_path = database_path
        self.cause = cause
        super().__init__(str(cause))


def initialization_failure_details(
    error: InitializationAfterCommittedEffectsError,
    operation: str,
    roots: cli_commands.ResolvedRoots | None,
) -> FailureDetails:
    """Describe exactly the initialization surfaces committed before failure."""

    changed_surfaces = (
        *((ChangedSurface.REPOSITORY_GIT_EXCLUDE,) if error.git_exclude_path is not None else ()),
        *((ChangedSurface.LEDGER,) if error.database_path is not None else ()),
    )
    observations = (
        *(
            (
                FailureFact("git_exclude_path", str(error.git_exclude_path)),
                FailureFact("git_exclude_entry", "/.codex/pinboard/"),
            )
            if error.git_exclude_path is not None
            else ()
        ),
        *(
            (FailureFact("database_path", str(error.database_path)),)
            if error.database_path is not None and not isinstance(error.cause, SQLiteReadOnlyError)
            else ()
        ),
    )
    if isinstance(error.cause, StorageError):
        return storage_failure_details(
            error.cause,
            operation,
            roots,
            EffectDisposition.COMMITTED,
            changed_surfaces,
            observations,
        )
    return FailureDetails(
        observed=observations,
        mismatches=(),
        retry=RetryDisposition.DO_NOT_RETRY,
        effect=EffectDisposition.COMMITTED,
        changed_surfaces=changed_surfaces,
        alternatives=(),
    )


type CliFailure = (
    CommandFailure | ProposalFailure | DispatchFailure | BriefSourceFailure | WorkBriefFailure | CommittedEffectFailure
)
type CliResult[T] = T | CliFailure
